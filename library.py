"""Build the searchable index of your sample folders.

    python3 samplepick.py index                     # folders from SAMPLEPICK_LIBRARY
    python3 samplepick.py index --library ~/Samples # or name them here

Writes <data>/index.jsonl, one line per audio file:
  {"path": "/abs/path/to/file.wav", "library": "/abs/path/to/root", "kind": "sample",
   "type": "loop|oneshot", "key", "bpm", "sec", "tags", "pack", "source": "loose"}
`path` is absolute (with forward slashes) so every other file (stats, saved.jsonl, blocked.json,
analysis.jsonl) can name a sample by it. If a library folder cannot be read right now (an unplugged
drive), its previous rows are kept and the judge skips them until it is back.
Packs are plain folders with no metadata: type, tempo and key come from the file and folder names,
length from the WAV header.
"""
import json
import os
import re
from pathlib import Path

AUDIO = {".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg"}

BPM_RE = re.compile(r"(?<![\d.])(\d{2,3}(?:\.\d)?)\s?bpm", re.I)
BARE_NUM = re.compile(r"^(\d{2,3})$")
QUAL = re.compile(r"^(maj|major|min|minor)$", re.I)
KEY_TOK = re.compile(r"^([A-Ga-g])(#|b|s|sharp|flat)?(m|min|minor|maj|major)?$", re.I)
FLAT = {"db": "c#", "eb": "d#", "gb": "f#", "ab": "g#", "bb": "a#", "cb": "b", "fb": "e"}
LOOPY = re.compile(r"loop|groove|\bbeats?\b|riff|progression|melod|chords?\b|phrase|\btops?\b", re.I)
SHOTTY = re.compile(r"one[\s_-]?shots?|oneshot|\bhits?\b|\bshots?\b|\bkicks?\b|\bsnares?\b|\bclaps?\b|808s?\b|\bfx\b|impacts?", re.I)


def wav_seconds(p: Path):
    """Length from the RIFF header alone (fmt byte rate, data size): one small read per file."""
    try:
        with p.open("rb") as f:
            h = f.read(4096)
        if h[:4] != b"RIFF" or h[8:12] != b"WAVE":
            return None
        i, rate = 12, None
        while i + 8 <= len(h):
            cid, size = h[i:i + 4], int.from_bytes(h[i + 4:i + 8], "little")
            if cid == b"fmt ":
                rate = int.from_bytes(h[i + 16:i + 20], "little")
            elif cid == b"data" and rate:
                return round(size / rate, 2)
            i += 8 + size + (size & 1)
    except OSError:
        return None
    return None


def name_facts(parts: list[str]) -> dict:
    """Type, tempo and key read from a loose sample's folders and file name."""
    stem = re.sub(r"\.[^.]+$", "", parts[-1])
    toks = [t for t in re.split(r"[\s_\-()\[\],]+", stem) if t]
    bpm = None
    m = BPM_RE.search(stem) or next((BPM_RE.search(d) for d in reversed(parts[:-1]) if BPM_RE.search(d)), None)
    if m:
        bpm = float(m.group(1))
    key = None
    for d in parts[:-1]:  # a folder named like "Kit5_150_g_minor" or "Emin 128bpm" keys every file in it
        dt = [t for t in re.split(r"[\s_\-()\[\],]+", d) if t]
        for i, t in enumerate(dt):
            k = KEY_TOK.match(t)
            if k and (k.group(2) or k.group(3) or (i + 1 < len(dt) and QUAL.match(dt[i + 1]))):
                acc = {"#": "#", "s": "#", "sharp": "#", "b": "b", "flat": "b"}.get((k.group(2) or "").lower(), "")
                key = FLAT.get(k.group(1).lower() + acc, k.group(1).lower() + acc)
    for i, t in enumerate(toks):
        if i + 1 < len(toks) and QUAL.match(toks[i + 1]) and KEY_TOK.match(t):
            t = t + toks[i + 1]  # "A Major", "C# minor"
        k = KEY_TOK.match(t)
        if not k or t.lower() in {"a", "b", "e"} and i < len(toks) - 1:
            continue  # a lone A/B/E mid-name is a word or a take letter, not a key
        root, acc, qual = k.group(1).lower(), (k.group(2) or "").lower(), k.group(3)
        if not acc and not qual and i < len(toks) - 1:
            continue
        acc = "#" if acc in ("#", "s", "sharp") else "b" if acc in ("b", "flat") else ""
        key = FLAT.get(root + acc, root + acc) if acc == "b" else root + acc
    folders = " ".join(parts[:-1])
    if SHOTTY.search(stem) or SHOTTY.search(folders) and not LOOPY.search(stem):
        typ = "oneshot"
    elif LOOPY.search(folders) or LOOPY.search(stem) or bpm:
        typ = "loop"
    else:
        typ = "oneshot"
    if bpm is None and typ == "loop":
        nums = [int(t) for t in toks if BARE_NUM.match(t) and 60 <= int(t) <= 200]
        bpm = float(nums[0]) if len(nums) == 1 else None
    return {"type": typ, "bpm": bpm, "key": key}


def loose_entries(real: Path) -> list[dict]:
    """The audio files under one library folder. Pack = the top folder, or the one below it when the
    top folder only groups packs (five or more sub-folders). A file directly in the library folder
    belongs to a pack named after the library folder."""
    files = [p for p in real.rglob("*") if p.suffix.lower() in AUDIO and not p.name.startswith("._")
             and "__MACOSX" not in p.parts
             and not any(d.startswith(".") for d in p.relative_to(real).parts[:-1])]
    subdirs = {}
    for p in files:
        rp = p.relative_to(real).parts
        if len(rp) > 2:
            subdirs.setdefault(rp[0], set()).add(rp[1])
    rows = []
    for p in sorted(files):
        rp = p.relative_to(real).parts
        grouped = len(rp) > 2 and len(subdirs.get(rp[0], ())) >= 5
        pack = real.name if len(rp) == 1 else rp[1] if grouped else rp[0]
        f = name_facts(list(rp if len(rp) == 1 else rp[2 if grouped else 1:]))  # the pack's own name says nothing about one file
        tags = [re.sub(r"^[_\s]+|[_\s]+$", "", d).strip("()_ ") for d in rp[:-1]]
        rows.append({"path": p.as_posix(), "library": real.as_posix(), "kind": "sample", "type": f["type"],
                     "key": f["key"], "bpm": f["bpm"], "sec": wav_seconds(p) if p.suffix.lower() == ".wav" else None,
                     "tags": [t for t in tags if t and t != pack], "pack": pack, "source": "loose"})
    return rows


def reachable(path) -> bool:
    """Whether a path can be read right now. A drive that is unplugged or asleep answers "No such
    device", which Path.exists() raises instead of returning False; ask here and skip those folders."""
    try:
        return Path(path).exists()
    except OSError:
        return False


def load_index(data: Path) -> list[dict]:
    rows = []
    f = Path(data) / "index.jsonl"
    if f.exists():
        with f.open(encoding="utf-8") as fh:
            for raw in fh:
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("path"):
                    rows.append(r)
    return rows


def build_index(data: Path, libraries: list[Path]) -> dict:
    """Walk every library folder and rewrite <data>/index.jsonl. Returns {"files", "libraries", "missing"}."""
    data = Path(data)
    old = load_index(data)
    rows, missing, counts = [], [], {}
    for lib in libraries:
        if not reachable(lib):  # unplugged: keep what it had
            kept = [r for r in old if r.get("library") == lib.as_posix()]
            rows += kept
            missing.append(lib.as_posix())
            counts[lib.as_posix()] = len(kept)
            continue
        got = loose_entries(lib)
        rows += got
        counts[lib.as_posix()] = len(got)
    seen, unique = set(), []  # overlapping library folders list a file once
    for r in rows:
        if r["path"] not in seen:
            seen.add(r["path"])
            unique.append(r)
    rows = unique
    data.mkdir(parents=True, exist_ok=True)
    tmp = data / "index.jsonl.tmp"
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    os.replace(tmp, data / "index.jsonl")
    return {"files": len(rows), "libraries": counts, "missing": missing}
