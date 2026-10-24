import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import samplepick as sp
import server


@pytest.fixture
def env(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    files = []
    for i in range(6):
        f = lib / f"bell_{i}.wav"
        f.write_bytes(b"RIFF" + bytes([i]) * 200)
        files.append(f)
    monkeypatch.setattr(sp, "DATA", tmp_path / "data")
    monkeypatch.setattr(sp, "ROOT", tmp_path / "picks")
    monkeypatch.setattr(sp, "_open", lambda argv: calls.append(argv))
    calls.clear()
    asked = []

    def fake(q, scope, options=None):
        asked.append((q, scope, options))
        k = len(asked)
        pick = files[:3] if k == 1 else files[3:5]
        return {"asked": 10, "cost": 0.001, "top": [
            {"score": 0.9 - j / 10, "path": f.as_posix(), "about": "a " + f.name,
             "facts": {"name": f.stem, "type": "loop"}} for j, f in enumerate(pick)]}
    monkeypatch.setattr(sp, "judge_ask", fake)
    monkeypatch.setattr(server, "RUNNER", None)
    return files, asked


calls = []


def test_numbering_and_new(env):
    r1 = sp.ask("Does #{n} ring?", slot="a")
    r2 = sp.ask("Is #{n} warm?", slot="b")
    assert [i["n"] for i in r1["items"]] == [1, 2, 3]
    assert [i["n"] for i in r2["items"]] == [4, 5]
    assert len(sp.list_items()) == 5
    assert sp.list_items()[0]["file"].endswith("bell_0.wav")
    r3 = sp.ask("again", new=True)
    assert [i["n"] for i in r3["items"]] == [1, 2]
    assert len(sp.list_items()) == 2
    sp.new_list()
    assert sp.list_items() == []


def test_save_copy_skip_rename(env):
    files, _ = env
    sp.ask("q")
    r = sp.save("Xmas", [1, 2], open_after=True)
    assert len(r["added"]) == 2 and not r["skipped"]
    assert (sp.ROOT / "Xmas" / "bell_0.wav").read_bytes() == files[0].read_bytes()
    assert files[0].exists()  # original untouched
    r = sp.save("Xmas", [1])
    assert len(r["skipped"]) == 1 and not r["added"]
    # same name, different content -> " (2)"
    other = sp.ROOT / "other"
    other.mkdir()
    clash = other / "bell_0.wav"
    clash.write_bytes(b"different")
    r = sp.save("Xmas", paths=[str(clash)])
    assert r["added"][0]["dest"].endswith("bell_0 (2).wav")
    assert sp.save("Xmas", paths=[str(clash)])["skipped"]
    lines = (sp.DATA / "saved.jsonl").read_text().splitlines()
    assert len(lines) == 4 and json.loads(lines[0])["folder"] == "Xmas"
    assert {f["name"] for f in sp.folders() if f["name"] == "Xmas"} == {"Xmas"}
    assert calls and calls[-1][-1].endswith("Xmas")  # the file manager was asked to open the folder
    assert "error" in sp.save("Xmas", [99])


@pytest.mark.parametrize("bad", ["", "  ", "..", "a/b", "a\\b", "x..y", "."])
def test_folder_validation(env, bad):
    sp.ask("q")
    with pytest.raises(ValueError):
        sp.save(bad, [1])
    assert not sp.ROOT.exists() or not list(sp.ROOT.iterdir())


def test_reveal(env):
    files, _ = env
    sp.ask("q")
    assert sp.reveal(1)["revealed"] == 1
    assert calls[-1] == sp.reveal_command(files[0], True)
    assert "error" in sp.reveal(50)


def serve():
    s = server.make_server(0)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, f"http://127.0.0.1:{s.server_address[1]}"


def test_audio_endpoint(env):
    files, _ = env
    sp.ask("q")
    s, base = serve()
    try:
        r = urllib.request.urlopen(base + "/api/audio?n=1")
        assert r.read() == files[0].read_bytes()
        req = urllib.request.Request(base + "/api/audio?n=2", headers={"Range": "bytes=2-9"})
        r = urllib.request.urlopen(req)
        assert r.status == 206 and r.read() == files[1].read_bytes()[2:10]
        for bad in ("99", "../../etc/passwd", ""):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + "/api/audio?n=" + bad)
            assert e.value.code == 404
        assert b"<html" in urllib.request.urlopen(base + "/").read()
        st = json.loads(urllib.request.urlopen(base + "/api/state").read())
        assert len(st["items"]) == 3
    finally:
        s.shutdown()


def test_chat_loop(env, monkeypatch):
    prompts = []
    script = [
        '```json\n{"reply":"looking for bells","actions":[{"do":"ask","question":"Does sample #{n} sound like a bell?","slot":"bells","type":"loop","words":["bell"]}]}\n```',
        'not json at all',
        '{"reply":"the first fits best","actions":[{"do":"save","folder":"Bells","items":[1,2]}]}',
    ]

    def runner(backend, prompt):
        prompts.append(prompt)
        return script[len(prompts) - 1]
    monkeypatch.setattr(server, "RUNNER", runner)
    r = server.chat("I want bells", "codex", [4])
    roles = [m["role"] for m in r["messages"]]
    assert roles == ["user", "assistant", "judge", "result", "assistant", "result"]
    j = r["messages"][2]
    assert j["slot"] == "bells" and j["ns"] == [i["n"] for i in r["items"][:len(j["ns"])]] and j["ns"]
    assert j["question"].startswith("Does sample") and j["asked"] == 10
    assert '"role"' not in prompts[1] and "bells" in prompts[1]
    assert r["messages"][0]["text"].endswith("[ticked: 4]")
    assert "[results]" in prompts[1]  # results went back to the model
    assert "bell_0" in prompts[1]
    assert len(r["items"]) == 3
    assert (sp.ROOT / "Bells" / "bell_0.wav").exists()
    assert len(prompts) == 3  # ask, one unparseable reply, then the retry
    assert server.load_talk()["backend"] == "codex"
    log = (sp.DATA / "calls.jsonl").read_text().splitlines()
    assert len(log) == 3 and json.loads(log[0])["ok"]


def test_chat_unparseable_gives_error(env, monkeypatch):
    monkeypatch.setattr(server, "RUNNER", lambda b, p: "nope")
    r = server.chat("hi", "claude")
    assert r["messages"][-1]["role"] == "error"
    assert "nope" not in server.transcript(server.load_talk()["messages"])  # error lines stay display-only


def test_transcript_skips_judge_keeps_result():
    t = server.transcript([{"role": "user", "text": "hi"},
                           {"role": "judge", "slot": "SLOTX", "question": "QQQ", "asked": 3, "ns": [1]},
                           {"role": "result", "text": "[results]x"}])
    assert "SLOTX" not in t and "QQQ" not in t and "[results]x" in t


# ---- folders, look, like ----

def _index_rows(files):
    return [{"path": f.as_posix(), "library": f.parent.as_posix(), "kind": "sample", "type": "loop",
             "key": "c", "bpm": 90, "sec": 4.0, "tags": ["bell", "warm"], "pack": "P", "source": "loose"}
            for f in files]


@pytest.fixture
def idx(env, tmp_path):
    files, _ = env
    (sp.DATA).mkdir(parents=True, exist_ok=True)
    (sp.DATA / "index.jsonl").write_text("\n".join(json.dumps(r) for r in _index_rows(files)) + "\n")
    sp._INDEX["key"] = None
    sp.ask("q")
    sp.save("Warm", [1, 2])
    (sp.ROOT / "Warm" / "my own drop-in_take.wav").write_bytes(b"x")
    (sp.ROOT / "Warm" / "notes.txt").write_text("not audio")
    return files


def test_describe_file_via_saved_and_name(idx):
    d = sp.describe_file("Warm", "bell_0.wav")
    assert d["found"] and d["path"] == idx[0].as_posix()
    assert d["line"].startswith("bell 0 — tags: bell, warm") and d["facts"]["bpm"] == 90
    # not in saved.jsonl any more: matched by exact filename
    (sp.DATA / "saved.jsonl").unlink()
    d = sp.describe_file("Warm", "bell_1.wav")
    assert d["found"] and d["path"] == idx[1].as_posix()
    # a file the user dropped in
    d = sp.describe_file("Warm", "my own drop-in_take.wav")
    assert not d["found"] and d["path"] is None and d["line"] == "my own drop in take" and d["exists"]


def test_describe_saved_renamed_copy(idx):
    # a clash copy "bell_0 (2).wav" still maps to its origin through saved.jsonl
    other = sp.ROOT / "Warm" / "bell_0 (2).wav"
    other.write_bytes(b"y")
    with (sp.DATA / "saved.jsonl").open("a") as f:
        f.write(json.dumps({"folder": "Warm", "files": [{"path": idx[0].as_posix(), "dest": str(other)}]}) + "\n")
    assert sp.describe_file("Warm", "bell_0 (2).wav")["path"] == idx[0].as_posix()


def test_look(idx):
    r = sp.look("Warm")
    names = [f["file"] for f in r["files"]]
    assert names == ["bell_0.wav", "bell_1.wav", "my own drop-in_take.wav"]
    assert [f["found"] for f in r["files"]] == [True, True, False]
    with pytest.raises(ValueError):
        sp.look("Nope")
    with pytest.raises(ValueError):
        sp.look("../x")


def test_folders_summary(idx):
    assert sp.folders_summary() == "Warm (3 files): bell_0.wav, bell_1.wav, my own drop-in_take.wav"
    big = sp.ROOT / "Big"
    big.mkdir()
    for i in range(15):
        (big / f"s{i:02d}.wav").write_bytes(b"x")
    line = [l for l in sp.folders_summary().splitlines() if l.startswith("Big")][0]
    assert line.startswith("Big (15 files): s00.wav") and "s11.wav" in line and "s12.wav" not in line
    assert line.endswith("… 15 in all")


def test_folders_summary_empty(env):
    assert sp.folders_summary() == "(no folders yet)"


def test_ask_like(idx, monkeypatch):
    seen = []
    orig = sp.judge_ask
    monkeypatch.setattr(sp, "judge_ask", lambda q, *a, **k: (seen.append(q), orig(q, *a, **k))[1])
    r = sp.ask("Does sample #{n} feel like the reference?", like=["Warm/bell_0.wav", "Warm/my own drop-in_take.wav"])
    q = seen[-1]
    assert "#{n}" in q and q.startswith("Does sample #{n} feel like the reference? Reference sample(s): ")
    assert '"bell 0 — tags: bell, warm' in q and '"my own drop in take"' in q
    assert len(r["like"]) == 2
    with pytest.raises(ValueError):
        sp.ask("q", like=["Warm/missing.wav"])
    with pytest.raises(ValueError):
        sp.ask("q", like=["nofolder"])
    assert "like" not in sp.ask("plain")


def test_cli_look_and_like(idx, capsys):
    assert sp.main(["look", "Warm"]) == 0
    assert "bell_0.wav" in capsys.readouterr().out
    assert sp.main(["look", "Nope"]) == 1
    capsys.readouterr()
    assert sp.main(["ask", "Is #{n} warm?", "--like", "Warm/bell_0.wav", "--like", "Warm/bell_1.wav"]) == 0
    assert len(json.loads(capsys.readouterr().out)["like"]) == 2


def test_summary_goes_to_model_not_to_storage(idx, monkeypatch):
    prompts = []
    monkeypatch.setattr(server, "RUNNER", lambda b, p: (prompts.append(p), '{"reply":"ok","actions":[]}')[1])
    r = server.chat("hello", "claude")
    assert "[folders]\nWarm (3 files)" in prompts[0]
    assert prompts[0].count("[folders]") == 1
    assert r["messages"][0]["text"] == "hello"
    assert "folders" not in json.dumps(server.load_talk()["messages"], ensure_ascii=False)
    # only the latest user message carries it
    server.chat("again", "claude")
    assert prompts[1].count("[folders]") == 1 and prompts[1].index("again") < prompts[1].index("[folders]")


def test_look_round_trip_and_errors(idx, monkeypatch):
    prompts = []
    script = [
        '{"reply":"let me look","actions":[{"do":"look","folder":"Warm"}]}',
        '{"reply":"looking for similar ones","actions":[{"do":"ask","question":"Does sample #{n} match?","like":["Warm/bell_0.wav"],"slot":"s"}]}',
        '{"reply":"look again","actions":[{"do":"look","folder":"Missing"},{"do":"ask","question":"x #{n}","like":["Warm/gone.wav"]}]}',
        '{"reply":"ok, no such folder","actions":[]}',
    ]

    def runner(b, p):
        prompts.append(p)
        return script[len(prompts) - 1]
    monkeypatch.setattr(server, "RUNNER", runner)
    r = server.chat("something to go with Warm", "claude")
    roles = [m["role"] for m in r["messages"]]
    assert roles == ["user", "assistant", "look", "result", "assistant", "judge", "result",
                     "assistant", "result", "assistant"]
    lk = r["messages"][2]
    assert lk == {"role": "look", "folder": "Warm", "files": ["bell_0.wav", "bell_1.wav", "my own drop-in_take.wav"]}
    assert "[folder] Warm: " in prompts[1] and "- bell_0.wav — bell 0 — tags" in prompts[1]
    j = r["messages"][5]
    assert j["like"] and j["like"][0].startswith("bell 0")
    assert "look failed" in prompts[3] and "ask failed" in prompts[3] and "reference file not found" in prompts[3]
    assert len(prompts) == 4  # three extra rounds allowed
    assert "look" not in [m["role"] for m in server.load_talk()["messages"] if m["role"] == "user"]
    assert "Warm" in server.transcript(server.load_talk()["messages"])


# ---- conversations ----

def test_new_switch_rename_delete(env):
    sp.ask("q")                       # conversation A: items 1..3
    a = sp.current_conv()["id"]
    b = sp.new_conv()["id"]
    assert b != a and sp.list_items() == []
    r = sp.ask("q2")                  # numbering restarts in B
    assert [i["n"] for i in r["items"]] == [1, 2]
    sp.switch_conv(a)
    assert len(sp.list_items()) == 3
    assert [i["n"] for i in sp.ask("more")["items"]] == [4, 5]
    assert sp.rename_conv(b, "piano")["title"] == "piano"
    assert {c["id"]: c["title"] for c in sp.list_convs()}[b] == "piano"
    with pytest.raises(ValueError):
        sp.rename_conv(b, "  ")
    with pytest.raises(ValueError):
        sp.switch_conv("../x")
    assert sp.list_convs()[0]["id"] == b  # most recently updated first (b was just renamed)
    assert sp.delete_conv(b)["id"] == a   # not current: stays on a
    assert [c["id"] for c in sp.list_convs()] == [a]
    cur = sp.delete_conv(a)               # last one: a fresh one takes over
    assert cur["id"] != a and cur["items"] == [] and len(sp.list_convs()) == 1
    with pytest.raises(ValueError):
        sp.delete_conv("nope")


def test_delete_current_switches_to_newest_other(env):
    a = sp.current_conv()["id"]
    b = sp.new_conv()["id"]
    c = sp.new_conv()["id"]
    sp.switch_conv(b)
    sp.save_conv(sp.load_conv(a))         # a becomes the newest
    assert sp.delete_conv(b)["id"] == a


def test_cli_ask_new_makes_conversation(env, capsys):
    sp.ask("q")
    n = len(sp.list_convs())
    assert sp.main(["ask", "Is #{n} warm?", "--new"]) == 0
    assert len(sp.list_convs()) == n + 1


def test_server_talks_api_and_auto_title(env, monkeypatch):
    monkeypatch.setattr(server, "RUNNER", lambda b, p: '{"reply":"ok","actions":[]}')
    s, base = serve()

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req).read())
    try:
        st = call("/api/state")
        first = st["id"]
        r = call("/api/chat", {"text": "a warm piano loop, around eighty bpm, that kind", "backend": "claude"})
        assert r["title"] == "a warm piano loop, around eighty bpm, that kind"[:16]
        call("/api/chat", {"text": "the second sentence does not change the title", "backend": "claude"})
        assert call("/api/talks")["talks"][0]["title"] == r["title"]
        n = call("/api/talks/new", {})
        assert n["id"] != first and n["messages"] == []
        assert len(call("/api/talks")["talks"]) == 2
        sw = call("/api/talks/switch", {"id": first})
        assert len(sw["messages"]) > 0 and sw["id"] == first
        rn = call("/api/talks/rename", {"id": first, "title": "my piano"})
        assert rn["title"] == "my piano"
        call("/api/chat", {"text": "talk more", "backend": "claude"})
        assert call("/api/state")["title"] == "my piano"  # renamed stays
        d = call("/api/talks/delete", {"id": first})
        assert d["id"] == n["id"] and len(call("/api/talks")["talks"]) == 1
        with pytest.raises(urllib.error.HTTPError):
            call("/api/talks/switch", {"id": "zzz"})
    finally:
        s.shutdown()


def test_new_conversation_default_title_renamed_on_first_message(env, monkeypatch):
    monkeypatch.setattr(server, "RUNNER", lambda b, p: '{"reply":"ok","actions":[]}')
    c = sp.new_conv()
    assert c["title"] and not c["renamed"]
    server.chat("hello there", "claude")
    assert sp.current_conv()["title"] == "hello there"


# ---- folders: rename and delete ----

def test_folder_rename(env):
    sp.ask("q")
    sp.save("A", [1])
    sp.save("B", [2])
    assert sp.rename_folder("A", "C") == {"folder": "C", "from": "A"}
    assert (sp.ROOT / "C" / "bell_0.wav").exists() and not (sp.ROOT / "A").exists()
    assert "error" in sp.rename_folder("C", "B")          # target exists
    assert (sp.ROOT / "C").exists() and (sp.ROOT / "B").exists()
    assert "error" in sp.rename_folder("Nope", "Z")
    with pytest.raises(ValueError):
        sp.rename_folder("C", "a/b")
    with pytest.raises(ValueError):
        sp.rename_folder("C", ".trash")
    # describe_file still resolves after the rename (filename fallback)
    assert sp.describe_file("C", "bell_0.wav")["exists"]


def test_folder_delete_to_trash_and_hidden(env):
    files, _ = env
    sp.ask("q")
    sp.save("X", [1, 2])
    r = sp.delete_folder("X")
    assert r["deleted"] == "X" and r["count"] == 2
    assert not (sp.ROOT / "X").exists()
    moved = Path(r["trash"])
    assert moved.parent == sp.ROOT / ".trash" and moved.name.startswith("X-")
    assert (moved / "bell_0.wav").read_bytes() == files[0].read_bytes()
    assert files[0].exists()
    assert sp.folders() == [] and sp.folders_summary() == "(no folders yet)"
    assert "error" in sp.delete_folder("X")
    with pytest.raises(ValueError):
        sp.delete_folder(".trash")
    sp.save("X", [1])                                     # same name again: separate trash entries
    r2 = sp.delete_folder("X")
    assert r2["trash"] != r["trash"]


def test_cli_rename_rmdir(env, capsys):
    sp.ask("q")
    sp.save("A", [1])
    assert sp.main(["rename", "A", "B"]) == 0
    assert sp.main(["rename", "A", "B"]) == 1
    assert sp.main(["rmdir", "B"]) == 0
    assert (sp.ROOT / ".trash").is_dir()


def test_server_folder_endpoints(env):
    sp.ask("q")
    sp.save("A", [1])
    sp.save("B", [2])
    s, base = serve()

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            return 200, json.loads(urllib.request.urlopen(req).read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        code, r = post("/api/folders/rename", {"old": "A", "new": "B"})
        assert code == 400 and "error" in r
        code, r = post("/api/folders/rename", {"old": "A", "new": "C"})
        assert code == 200 and {f["name"] for f in r["folders"]} == {"B", "C"}
        code, r = post("/api/folders/delete", {"folder": "C"})
        assert code == 200 and r["count"] == 1 and [f["name"] for f in r["folders"]] == ["B"]
        assert post("/api/folders/delete", {"folder": "../x"})[0] == 400
    finally:
        s.shutdown()


# ---- notes and the blocklist ----

def test_notes_set_history_unchanged(env):
    assert sp.get_notes() == ""
    assert sp.set_notes("likes warm", "user") is True
    assert not (sp.DATA / "notes-history.jsonl").exists()  # nothing before the first version
    assert sp.set_notes("likes warm", "user") is False  # unchanged: no write, no history
    assert sp.set_notes("likes warm\nno flutes", "agent") is True
    assert sp.get_notes() == "likes warm\nno flutes"
    rows = [json.loads(x) for x in (sp.DATA / "notes-history.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["text"] == "likes warm" and rows[0]["by"] == "agent" and rows[0]["time"]
    assert sp.set_notes("", "user") is True and sp.get_notes() == ""
    assert len((sp.DATA / "notes-history.jsonl").read_text().splitlines()) == 2


def test_block_filters_ask_and_unblock(env):
    files, _ = env
    r1 = sp.ask("q")
    assert "blocked_skipped" not in r1
    r = sp.block([1], why="shown too often")
    assert r["blocked"][0]["path"] == files[0].as_posix() and "error" in sp.block([99])
    assert list(sp.blocked()) == [files[0].as_posix()]
    assert sp.blocked_list()[0]["why"] == "shown too often" and sp.blocked_list()[0]["name"] == "bell_0"
    sp.block(paths=[files[3].as_posix()], why="x")
    r2 = sp.ask("again", new=True)  # the judge returns bell_3 and bell_4
    assert [i["name"] for i in r2["items"]] == ["bell_4"] and r2["items"][0]["n"] == 1
    assert r2["blocked_skipped"] == 1
    assert "error" in sp.unblock("nothing")
    assert sp.unblock("bell_3")["unblocked"] == [files[3].as_posix()]
    assert sp.unblock(files[0].as_posix())["unblocked"]
    assert sp.blocked() == {}


def test_notes_block_cli(env, capsys, tmp_path):
    sp.ask("q")
    assert sp.main(["notes", "--set", "abc"]) == 0
    capsys.readouterr()
    sp.main(["notes"])
    assert capsys.readouterr().out.strip() == "abc"
    f = tmp_path / "n.txt"
    f.write_text("from file")
    sp.main(["notes", "--edit-file", str(f)])
    assert sp.get_notes() == "from file"
    assert sp.main(["block", "2", "--why", "w"]) == 0
    capsys.readouterr()
    sp.main(["blocked"])
    assert json.loads(capsys.readouterr().out)["blocked"][0]["name"] == "bell_1"
    assert sp.main(["unblock", "bell_1"]) == 0
    assert sp.main(["unblock", "bell_1"]) == 1


def test_transcript_notes_only_on_latest_user(env):
    msgs = [{"role": "user", "text": "first"}, {"role": "assistant", "text": "r", "raw": "{}"},
            {"role": "note", "by": "agent"}, {"role": "block", "names": ["a"], "why": "w"},
            {"role": "user", "text": "second"}]
    t = server.transcript(msgs)
    assert t.count("[notes]") == 1 and t.count("[folders]") == 1
    assert t.index("second") < t.index("[notes]") and "(no notes yet)" in t and "[blocked]" not in t
    sp.set_notes("prefers warm timbre", "user")
    sp.ask("q")
    sp.block([1, 2])
    t = server.transcript(msgs)
    assert "prefers warm timbre" in t and "[blocked] 2 samples" in t and t.index("[folders]") < t.index("[notes]")
    assert t.index("first") < t.index("r") < t.index("second")


def test_chat_note_and_block_actions(env, monkeypatch):
    prompts = []
    script = [
        '{"reply":"look","actions":[{"do":"ask","question":"Does #{n} ring?","slot":"bell"}]}',
        '{"reply":"noted","actions":[{"do":"note","text":"no flutes"},{"do":"block","items":[1,2],"why":"too often"},{"do":"block","items":[77]}]}',
    ]

    def runner(backend, prompt):
        prompts.append(prompt)
        return script[len(prompts) - 1]
    monkeypatch.setattr(server, "RUNNER", runner)
    r = server.chat("I want bells", "claude")
    roles = [m["role"] for m in r["messages"]]
    assert roles == ["user", "assistant", "judge", "result", "assistant", "note", "block", "result"]
    assert r["messages"][5] == {"role": "note", "by": "agent"}
    assert r["messages"][6]["names"] == ["bell_0", "bell_1"] and r["messages"][6]["why"] == "too often"
    res = r["messages"][7]["text"]
    assert "[notes] updated" in res and "[blocked] bell_0, bell_1" in res and "[block failed]" in res
    assert len(prompts) == 2  # note / block do not trigger another model round
    assert sp.get_notes() == "no flutes" and len(sp.blocked()) == 2
    assert r["notes"] == "no flutes" and len(r["blocked"]) == 2
    # the next user message carries the notes and the count
    monkeypatch.setattr(server, "RUNNER", lambda b, p: (prompts.append(p), '{"reply":"ok"}')[1])
    server.chat("again", "claude")
    assert "no flutes" in prompts[-1] and "[blocked] 2 samples" in prompts[-1]
    assert prompts[-1].count("[notes]\n") == 1


def test_server_notes_endpoints(env):
    sp.ask("q")
    s, base = serve()

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            return 200, json.loads(urllib.request.urlopen(req).read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        assert json.loads(urllib.request.urlopen(base + "/api/notes").read()) == {"text": "", "blocked": []}
        assert post("/api/notes", {"text": "hello"}) == (200, {"changed": True})
        assert post("/api/notes", {"text": "hello"}) == (200, {"changed": False})
        assert post("/api/notes", {"text": 5})[0] == 400
        code, r = post("/api/block", {"items": [1], "why": "by hand"})
        assert code == 200 and r["blocked"][0]["why"] == "by hand" and post("/api/block", {"items": [99]})[0] == 400
        g = json.loads(urllib.request.urlopen(base + "/api/notes").read())
        assert g["text"] == "hello" and g["blocked"][0]["name"] == "bell_0"
        st = json.loads(urllib.request.urlopen(base + "/api/state").read())
        assert st["notes"] == "hello" and len(st["blocked"]) == 1
        code, r = post("/api/unblock", {"path": g["blocked"][0]["path"]})
        assert code == 200 and r["blocked"] == []
        assert post("/api/unblock", {"path": "nope"})[0] == 400
        hist = [json.loads(x) for x in (sp.DATA / "notes-history.jsonl").read_text().splitlines()] \
            if (sp.DATA / "notes-history.jsonl").exists() else []
        assert hist == []  # first save had no previous version
    finally:
        s.shutdown()


# ---- per-sample counters ----

def _p(f):
    return f.as_posix()


def test_stats_update_on_ask_and_save(env):
    files, _ = env
    r = sp.ask("q1")
    assert [(i["shown"], i["saved"]) for i in r["items"]] == [(1, 0)] * 3
    sp.ask("q2")  # shows files 3, 4
    sp.ask("q3")  # files 0..2 again (fake alternates only after 1st; k=3 -> files[3:5])
    st = sp.stats(_p(files[3]))
    assert st["shown"] == 2 and st["saved"] == 0 and st["name"] == "bell_3" and st["last_shown"]
    assert sp.stats(_p(files[0]))["shown"] == 1
    sp.save("X", [1, 2])
    assert sp.stats(_p(files[0]))["saved"] == 1
    sp.save("X", [1])  # already there: skipped, not counted
    assert sp.stats(_p(files[0]))["saved"] == 1
    sp.save("Y", [1])  # a different folder: a real copy
    s = sp.stats(_p(files[0]))
    assert s["saved"] == 2 and s["last_saved"]
    assert {i["path"]: (i["shown"], i["saved"]) for i in sp.with_counts(sp.list_items())}[_p(files[0])] == (1, 2)
    assert sp.stats("nope") == {"name": "", "shown": 0, "saved": 0, "last_shown": "", "last_saved": ""}


def test_stats_blocked_and_reference_not_counted(env):
    files, _ = env
    sp.ask("q")
    sp.block([2], why="x")
    sp.ask("q again", new=True)  # second call: files 3,4
    sp.block(paths=[_p(files[4])])
    r = sp.ask("q3", new=True)  # third call: files 3,4 again; 4 is blocked
    assert [i["name"] for i in r["items"]] == ["bell_3"]
    assert sp.stats(_p(files[4]))["shown"] == 1  # only the showing before it was blocked
    assert sp.stats(_p(files[3]))["shown"] == 2
    # a reference file is not a find
    (sp.ROOT / "F").mkdir(parents=True)
    (sp.ROOT / "F" / "bell_3.wav").write_bytes(files[3].read_bytes())
    (sp.DATA / "index.jsonl").write_text(json.dumps(
        {"kind": "sample", "path": _p(files[3]), "name": "bell_3", "type": "loop", "tags": []}) + "\n")
    sp.ask("q4", like=["F/bell_3.wav"], new=True)  # 4th call: files[3:5]; 3 is the reference, 4 blocked
    assert sp.stats(_p(files[3]))["shown"] == 2


def _history(tmp_path):
    """A data dir with history but no stats.json: two conversations and a saved.jsonl."""
    d = tmp_path / "hist"
    (d / "talks").mkdir(parents=True)
    a = "/lib/a.wav"
    b = "/lib/b.wav"
    items = lambda *ps: [{"n": i + 1, "path": p, "name": p[-5:-4]} for i, p in enumerate(ps)]
    for cid, its in (("c1", items(a, b)), ("c2", items(a))):
        (d / "talks" / f"{cid}.json").write_text(json.dumps(
            {"id": cid, "title": cid, "messages": [], "items": its, "updated": "2026-10-01 10:00:00"}))
    (d / "saved.jsonl").write_text(
        json.dumps({"time": "2026-10-02 09:00:00", "folder": "F", "files": [{"n": 1, "path": a, "dest": "/x/F/a.wav"}]}) + "\n"
        + json.dumps({"time": "2026-10-03 09:00:00", "folder": "G", "files": [{"n": 1, "path": a, "dest": "/x/G/a.wav"}]}) + "\n"
        + json.dumps({"time": "2026-10-03 09:00:01", "folder": "G", "files": []}) + "\n")
    return d, a, b


def test_stats_backfill_and_idempotent(env, tmp_path, monkeypatch):
    d, a, b = _history(tmp_path)
    monkeypatch.setattr(sp, "DATA", d)
    st = sp.load_stats()
    assert (st[a]["shown"], st[a]["saved"], st[b]["shown"], st[b]["saved"]) == (2, 2, 1, 0)
    assert st[a]["last_saved"] == "2026-10-03 09:00:00" and st[a]["last_shown"] == "2026-10-01 10:00:00"
    assert (d / "stats.json").exists()
    before = (d / "stats.json").read_text()
    sp.load_stats()
    sp.stats(a)
    assert (d / "stats.json").read_text() == before  # reading again never recounts
    (d / "stats.json").unlink()
    assert sp.load_stats() == json.loads(before)  # rebuilding gives the same numbers


def test_stats_first_ask_after_backfill_counts_once(env, tmp_path, monkeypatch):
    files, _ = env
    d = tmp_path / "hist2"
    (d / "talks").mkdir(parents=True)
    (d / "talks" / "c1.json").write_text(json.dumps(
        {"id": "c1", "title": "c1", "messages": [], "updated": "2026-10-01 10:00:00",
         "items": [{"n": 1, "path": _p(files[0]), "name": "bell_0"}]}))
    (d / "current.txt").write_text("c1")
    monkeypatch.setattr(sp, "DATA", d)
    sp.ask("q")  # shows files 0..2; files[0] was shown once before
    assert sp.stats(_p(files[0]))["shown"] == 2
    assert sp.stats(_p(files[1]))["shown"] == 1


def test_stats_cli_and_top(env, capsys):
    sp.ask("q")
    sp.ask("q2")
    sp.save("X", [1])
    capsys.readouterr()
    assert sp.main(["stats", "--top", "2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["sort"] == "shown" and len(out["samples"]) == 2
    assert sp.main(["stats", "--sort", "saved"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in out["samples"]] == ["bell_0"] and out["samples"][0]["saved"] == 1


def test_model_result_and_api_items_carry_counts(env):
    seen = []

    def runner(b, p):
        seen.append(p)
        if "[results]" in p:
            return json.dumps({"reply": "ok", "actions": []})
        return json.dumps({"reply": "look", "actions": [{"do": "ask", "question": "q", "slot": "bell"}]})
    server.RUNNER = runner
    sp.ask("earlier")  # bell_0..2 shown once; the chat's ask is the 2nd call -> bell_3, bell_4
    sp.save("Z", [1])
    r = server.chat("give me a ring", "claude")
    res = next(m for m in r["messages"] if m["role"] == "result")["text"]
    assert res.count("(shown 1 times, saved 0 times)") == 2
    by = {i["name"]: (i["shown"], i["saved"]) for i in r["items"]}
    assert by["bell_3"] == (1, 0)
    sp.save("Z2", [1])
    st = server.state()  # old conversation items show the current totals
    by = {i["name"]: (i["shown"], i["saved"]) for i in st["items"]}
    assert by["bell_0"] == (1, 2) and by["bell_4"] == (1, 0)
