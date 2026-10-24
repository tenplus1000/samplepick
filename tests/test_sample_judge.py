import json

import pytest

import analyze
import judge
import sample_judge as sj
from conftest import make_wav
from test_judge import Resp, log_rows


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """An index of 8 bell loops (bell_0 is the warmest by name) and a fake judge transport."""
    data = tmp_path / "data"
    data.mkdir()
    rows = []
    for i in range(8):
        f = make_wav(tmp_path / "L" / f"bell_{i}.wav")
        rows.append({"path": f.as_posix(), "library": (tmp_path / "L").as_posix(), "kind": "sample", "type": "loop",
                     "key": None, "bpm": None, "sec": 1.0, "tags": ["bell"], "pack": "P", "source": "loose"})
    (data / "index.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setenv("SAMPLEPICK_JUDGE_KEY", "k")
    sent = []

    def post(url, json, headers, timeout):
        sent.append(json)
        answers = {}
        for qid, q in json["questions"].items():
            n = int(qid[1:])
            line = [l for l in json["state"].splitlines() if l.startswith(f"#{n}:")][0]
            # names alone: lower number = better; once "sounds warm" is in the line, that wins
            yes = 0.9 - n / 20 + (0.5 if "sounds warm" in line else 0)
            if q["type"] == "choice":
                answers[qid] = {"choice": "warm", "probabilities": {"warm": min(yes, 1), "dark": 1 - min(yes, 1)}}
            else:
                answers[qid] = {"noul": min(yes, 1)}
        return Resp(body={"answers": answers, "usage": {"cost": 0.001}})
    monkeypatch.setattr(judge, "_post", post)
    return data, rows, sent


def test_two_pass_names_then_heard(lib, monkeypatch):
    data, rows, sent = lib
    short_seen = []

    def fake_analyse(d, short, background=True):
        short_seen.append([s["path"] for s in short])
        # the sample ranked last by name is the one that sounds warm
        got = {s["path"]: {"timbre": {k: 50.0 for k in analyze.WORDS} | {"reverb": 0}} for s in short}
        for i, s in enumerate(short):
            got[s["path"]]["timbre"]["warmth"] = 90.0 if s["path"].endswith("bell_7.wav") else 40.0 + i
        return got
    monkeypatch.setattr(analyze, "analyse", fake_analyse)
    monkeypatch.setattr(sj, "SHORTLIST", 8)
    r = sj.ask(data, "Is sample #{n} a warm bell?", {})
    assert r["asked"] == 8 and r["heard"] == 8 and r["calls"] == 2  # one batch per pass
    assert len(sent) == 2 and "sounds" not in sent[0]["state"] and "sounds" in sent[1]["state"]
    assert r["top"][0]["path"].endswith("bell_7.wav")        # heard, it overtook the names
    assert "sounds warm" in r["top"][0]["about"]
    assert r["cost"] == 0.002
    assert len(log_rows(data)) == 2                           # both passes are in judge.jsonl


def test_names_only_when_listening_is_off(lib):
    data, rows, sent = lib
    r = sj.ask(data, "Is sample #{n} a bell?", {"type": "loop"})
    assert len(sent) == 1 and r["calls"] == 1 and r["top"][0]["path"].endswith("bell_0.wav")
    assert r["heard"] == 8 and not any(t["about"].count("sounds") for t in r["top"])
    assert [t["score"] for t in r["top"]] == sorted((t["score"] for t in r["top"]), reverse=True)


def test_batches_and_question_numbering(lib, monkeypatch):
    data, rows, sent = lib
    monkeypatch.setattr(sj, "BATCH", 3)
    r = sj.ask(data, "Is sample #{n} a bell?", {})
    first_pass = sent[:3]
    assert r["calls"] == 3 and [len(c["questions"]) for c in first_pass] == [3, 3, 2]
    assert first_pass[0]["questions"]["s1"] == {"type": "noul", "instructions": "Is sample #1 a bell?"}


def test_question_without_placeholder_gets_one(lib):
    data, rows, sent = lib
    sj.ask(data, "Is it a bell?", {})
    assert sent[0]["questions"]["s2"]["instructions"] == "Is it a bell — sample #2?"


def test_choice_question_shortlists_per_option(lib):
    data, rows, sent = lib
    r = sj.ask(data, "Is sample #{n} warm or dark?", {}, {"warm": "warm", "dark": "dark"})
    assert sent[0]["questions"]["s1"]["type"] == "choice" and sent[0]["questions"]["s1"]["criteria"] == {"warm": "warm", "dark": "dark"}
    assert {t["option"] for t in r["top"]} == {"warm", "dark"}


def test_too_many_comes_back_unasked(lib, monkeypatch):
    data, rows, sent = lib
    monkeypatch.setattr(sj, "MAX_ASK", 5)
    r = sj.ask(data, "q #{n}", {})
    assert r["asked"] == 0 and r["too_many"] == 8 and "Narrow it" in r["note"] and sent == []


def test_empty_scope_and_no_key(lib, monkeypatch):
    data, rows, sent = lib
    assert sj.ask(data, "q #{n}", {"words": ["nothing"]})["asked"] == 0
    monkeypatch.delenv("SAMPLEPICK_JUDGE_KEY")
    with pytest.raises(judge.JudgeError):
        sj.ask(data, "q #{n}", {})
    assert sent == []


def test_unreachable_library_is_skipped(lib, monkeypatch, tmp_path):
    data, rows, sent = lib
    rows[0]["library"] = "/no/such/drive"
    (data / "index.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows[:2]))
    assert len(sj.candidates(data, {})) == 1


def test_analysis_failure_falls_back_to_names(lib, monkeypatch, capsys):
    data, rows, sent = lib
    monkeypatch.setattr(analyze, "analyse", lambda *a, **k: 1 / 0)
    r = sj.ask(data, "q #{n}", {})
    assert r["calls"] == 1 and "analysis" in capsys.readouterr().err


def test_line_describes_a_sample():
    s = {"path": "/x/Warm_Bell-Loop.wav", "tags": ["a", "", "b"], "pack": "Pk", "key": "c#", "bpm": 90.0, "type": "loop"}
    assert sj.line(s) == "Warm Bell Loop — tags: a, b; pack: Pk; key: c#; 90.0 bpm; loop"
    assert sj.line(s, "sounds warm").endswith("90.0 bpm; sounds warm; loop")


# ---- analyze: features / describe keep the original wording; nothing runs unless configured ----

def _got(vals):
    return {f"/s{i}.wav": {"timbre": {**{k: 50.0 for k in analyze.WORDS}, "reverb": 0, "warmth": v}} for i, v in enumerate(vals)}


def test_features_and_describe():
    samples = [{"path": f"/s{i}.wav", "tags": [], "type": "loop", "bpm": None} for i in range(5)]
    got = _got([10, 30, 50, 70, 95])
    got["/s4.wav"]["chords"] = "Am F"
    got["/s4.wav"]["bpm"] = 87.5
    got["/s4.wav"]["timbre"]["reverb"] = 1
    f = analyze.features(samples, got)
    assert f["/s0.wav"]["words"] == ["cold"] and f["/s4.wav"]["words"] == ["warm"] and f["/s2.wav"]["words"] == []
    assert f["/s4.wav"]["reverb"] and f["/s4.wav"]["chords"] == "Am F" and f["/s4.wav"]["bpm"] == 87.5
    d = analyze.describe(samples, got, f)
    assert d["/s4.wav"] == "sounds warm (compared with the other candidates); reverberant; chords: Am F; measured 87.5 bpm"
    assert d["/s2.wav"] == ""
    # fewer than four timbres: no relative words
    assert analyze.features(samples[:3], got)["/s0.wav"]["words"] == []


def test_analyse_is_off_by_default_but_reads_the_cache(tmp_path, monkeypatch):
    assert not analyze.enabled()
    s = {"path": "/a.wav", "tags": [], "type": "loop", "bpm": None}
    ran = []
    monkeypatch.setattr(analyze.subprocess, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(analyze.subprocess, "Popen", lambda *a, **k: ran.append(a))
    assert analyze.analyse(tmp_path, [s]) == {}
    (tmp_path / "analysis.jsonl").write_text(json.dumps({"path": "/a.wav", "timbre": {"warmth": 1}}) + "\n")
    assert analyze.analyse(tmp_path, [s])["/a.wav"]["timbre"] == {"warmth": 1}
    assert ran == []  # neither ffmpeg, the timbre python nor Phonon was started


def test_analyse_timbre_with_fake_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMPLEPICK_TIMBRE_PYTHON", "/fake/python")
    src = make_wav(tmp_path / "a.wav")
    s = {"path": src.as_posix(), "tags": [], "type": "oneshot", "bpm": None}
    monkeypatch.setattr(analyze, "_clip", lambda a, b, sec: b.write_bytes(b"x") or True)

    class R:
        stdout = ""

    def run(argv, **kw):
        assert argv[0] == "/fake/python" and argv[1].endswith("timbre_worker.py")
        R.stdout = "".join(json.dumps({"file": f, "timbre": {"warmth": 77.0}}) + "\n" for f in argv[2:])
        return R
    monkeypatch.setattr(analyze.subprocess, "run", run)
    got = analyze.analyse(tmp_path, [s])
    assert got[s["path"]]["timbre"] == {"warmth": 77.0}
    assert json.loads((tmp_path / "analysis.jsonl").read_text())["path"] == s["path"]
    # cached: a second call does not run the worker again
    monkeypatch.setattr(analyze.subprocess, "run", lambda *a, **k: 1 / 0)
    assert analyze.analyse(tmp_path, [s])[s["path"]]["timbre"] == {"warmth": 77.0}


def test_phonon_only_when_url_set(tmp_path, monkeypatch):
    s = {"path": "/a.wav", "tags": ["pad"], "type": "loop", "bpm": None}
    started = []
    monkeypatch.setattr(analyze.subprocess, "Popen", lambda *a, **k: started.append(a) or type("P", (), {
        "stdin": type("S", (), {"write": lambda self, b: None, "close": lambda self: None})()})())
    analyze.analyse(tmp_path, [s])
    assert started == []
    monkeypatch.setenv("SAMPLEPICK_PHONON_URL", "https://phonon.example/api")
    assert analyze.phonon_url() == "https://phonon.example/api/"
    analyze.analyse(tmp_path, [s])
    assert len(started) == 1


def test_end_to_end_index_ask_save_log(tmp_path, monkeypatch):
    """index -> ask (fake transport) -> save, through the public functions the CLI and page use."""
    import samplepick as sp
    lib = tmp_path / "Lib"
    for n in ("Warm Bell Loop 100bpm", "Hard Kick"):
        make_wav(lib / "Pack" / f"{n}.wav", 1.0)
    monkeypatch.setattr(sp, "DATA", tmp_path / "data")
    monkeypatch.setattr(sp, "ROOT", tmp_path / "picks")
    monkeypatch.setenv("SAMPLEPICK_LIBRARY", str(lib))
    monkeypatch.setenv("SAMPLEPICK_JUDGE_KEY", "k")
    monkeypatch.setattr(judge, "_post", lambda url, json, headers, timeout: Resp(body={
        "answers": {q: {"noul": 0.9 if "Bell" in json["state"].split(f"#{q[1:]}:")[1].splitlines()[0] else 0.1}
                    for q in json["questions"]}, "usage": {"cost": 0.0001}}))
    assert sp.index_library()["files"] == 2
    r = sp.ask("Is sample #{n} a bell?", slot="bells")
    assert [i["name"] for i in r["items"]] == ["Warm Bell Loop 100bpm", "Hard Kick"] and r["cost"] > 0
    assert r["items"][0]["facts"]["bpm"] == 100.0
    out = sp.save("Picks", [1])
    assert (sp.ROOT / "Picks" / "Warm Bell Loop 100bpm.wav").exists() and len(out["added"]) == 1
    assert len(log_rows(sp.DATA)) == 1 and log_rows(sp.DATA)[0]["caller"] == "samplepick"
