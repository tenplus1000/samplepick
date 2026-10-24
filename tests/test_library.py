import json

import config
import library
import samplepick as sp
from conftest import make_wav


def build_library(tmp_path):
    lib = tmp_path / "Samples"
    make_wav(lib / "Dusty Keys" / "Warm Piano Loop 90bpm Am.wav", 2.0)
    make_wav(lib / "Dusty Keys" / "Kick_01.wav", 0.5)
    make_wav(lib / "Dusty Keys" / "Chords" / "Dm_Loop_120.wav", 4.0)
    make_wav(lib / "Drum Kit" / "One Shots" / "Snare Hit.wav", 0.25)
    make_wav(lib / "loose_oneshot.wav", 0.1)
    (lib / "Drum Kit" / "notes.txt").write_text("not audio")
    (lib / "Drum Kit" / "._junk.wav").write_bytes(b"x")
    (lib / ".hidden").mkdir()
    make_wav(lib / ".hidden" / "skip.wav")
    return lib


def test_index_tiny_wavs(tmp_path):
    lib = build_library(tmp_path)
    data = tmp_path / "data"
    r = library.build_index(data, [lib])
    assert r["files"] == 5 and r["missing"] == []
    rows = {json.loads(x)["path"].split("/")[-1]: json.loads(x) for x in (data / "index.jsonl").read_text().splitlines()}
    assert set(rows) == {"Warm Piano Loop 90bpm Am.wav", "Kick_01.wav", "Dm_Loop_120.wav", "Snare Hit.wav", "loose_oneshot.wav"}
    p = rows["Warm Piano Loop 90bpm Am.wav"]
    assert p["type"] == "loop" and p["bpm"] == 90.0 and p["key"] == "a" and p["pack"] == "Dusty Keys"
    assert p["sec"] == 2.0 and p["kind"] == "sample" and p["source"] == "loose"
    assert p["path"] == (lib / "Dusty Keys" / "Warm Piano Loop 90bpm Am.wav").as_posix()
    assert p["library"] == lib.as_posix() and p["tags"] == []
    assert rows["Kick_01.wav"]["type"] == "oneshot" and rows["Kick_01.wav"]["bpm"] is None
    c = rows["Dm_Loop_120.wav"]
    assert c["type"] == "loop" and c["bpm"] == 120.0 and c["key"] == "d" and c["tags"] == ["Chords"]
    assert rows["Snare Hit.wav"]["type"] == "oneshot" and rows["Snare Hit.wav"]["tags"] == ["One Shots"]
    assert rows["Snare Hit.wav"]["sec"] == 0.25
    # a file straight in the library folder belongs to a pack named after that folder
    assert rows["loose_oneshot.wav"]["pack"] == "Samples" and rows["loose_oneshot.wav"]["sec"] == 0.1


def test_index_groups_packs_and_keeps_unplugged_library(tmp_path):
    lib = tmp_path / "Big"
    for i in range(5):  # five sub-folders: the top folder only groups packs
        make_wav(lib / "Vendor" / f"Pack{i}" / "x.wav")
    other = tmp_path / "Other"
    make_wav(other / "P" / "a.wav")
    data = tmp_path / "data"
    library.build_index(data, [lib, other])
    packs = {json.loads(x)["pack"] for x in (data / "index.jsonl").read_text().splitlines()}
    assert packs == {"Pack0", "Pack1", "Pack2", "Pack3", "Pack4", "P"}
    # the second library goes missing: its rows stay, the report says so
    import shutil
    shutil.rmtree(other)
    r = library.build_index(data, [lib, other])
    assert r["missing"] == [other.as_posix()] and r["files"] == 6
    assert any(json.loads(x)["pack"] == "P" for x in (data / "index.jsonl").read_text().splitlines())


def test_index_overlapping_folders_dedupe(tmp_path):
    lib = tmp_path / "L"
    make_wav(lib / "P" / "a.wav")
    r = library.build_index(tmp_path / "d", [lib, lib / "P"])
    assert r["files"] == 1
    paths = [json.loads(x)["path"] for x in (tmp_path / "d" / "index.jsonl").read_text().splitlines()]
    assert len(paths) == len(set(paths)) == 1


def test_wav_seconds_and_non_wav(tmp_path):
    w = make_wav(tmp_path / "a.wav", 1.5)
    assert library.wav_seconds(w) == 1.5
    b = tmp_path / "b.wav"
    b.write_bytes(b"nope")
    assert library.wav_seconds(b) is None
    assert library.wav_seconds(tmp_path / "missing.wav") is None


def test_libraries_from_env_and_flag(tmp_path, monkeypatch):
    import os
    a, b = tmp_path / "a", tmp_path / "b"
    monkeypatch.setenv("SAMPLEPICK_LIBRARY", os.pathsep.join([str(a), str(b), str(a)]))
    assert config.libraries() == [a, b]
    assert config.libraries([str(b)]) == [b]  # the flag wins
    monkeypatch.delenv("SAMPLEPICK_LIBRARY")
    assert config.libraries() == []


def test_cli_index_then_candidates(tmp_path, monkeypatch, capsys):
    lib = build_library(tmp_path)
    monkeypatch.setattr(sp, "DATA", tmp_path / "data")
    assert sp.main(["index"]) == 1  # nothing configured
    assert "SAMPLEPICK_LIBRARY" in capsys.readouterr().out
    assert sp.main(["index", "--library", str(lib)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["files"] == 5
    import sample_judge
    got = sample_judge.candidates(sp.DATA, {"type": "loop", "words": ["piano"]})
    assert [r["path"].split("/")[-1] for r in got] == ["Warm Piano Loop 90bpm Am.wav"]
    assert len(sample_judge.candidates(sp.DATA, {"bpm": "100-130"})) == 1
    assert len(sample_judge.candidates(sp.DATA, {"key": "D"})) == 1
