"""Pick samples by describing a sound: ask the judge, get a numbered list, save the keepers to a folder.

  python3 samplepick.py index [--library DIR ...]   # scan your sample folders (SAMPLEPICK_LIBRARY) into data/index.jsonl
  python3 samplepick.py analyze [--type loop --words bell --limit 200]   # optional: listen ahead (timbre / chords)
  python3 samplepick.py ask "Does sample #{n} feel like Christmas?" --type loop --words bell --slot bells
  python3 samplepick.py ask ... --new        # start a fresh numbered list (numbers otherwise keep counting)
  python3 samplepick.py list                 # the current list
  python3 samplepick.py reveal 3             # show item 3 in the file manager (or reveal --folder NAME)
  python3 samplepick.py save "Xmas" 1 3 7 --open   # copy items into ROOT/Xmas
  python3 samplepick.py folders
  python3 samplepick.py rename OLD NEW     # rename a folder
  python3 samplepick.py rmdir NAME         # move a folder to ROOT/.trash
  python3 samplepick.py look "warm piano"  # what is in a folder, with the index's description of each file
  python3 samplepick.py ask ... --like "warm piano/x.wav"   # pick things similar to a file already saved
  python3 samplepick.py notes [--set TEXT | --edit-file F]   # the tool's own notes (data/notes.md)
  python3 samplepick.py stats [--top 20] [--sort shown|saved]   # how often each sample was shown / saved
  python3 samplepick.py block 4 7 --why "shown too often"; unblock NAME_OR_PATH; blocked   # never pick these again

Env: SAMPLEPICK_LIBRARY (sample folders, os.pathsep-separated), SAMPLEPICK_DATA (default ./data next to the code),
SAMPLEPICK_ROOT (default ~/Music/Sample Picks), judge key SAMPLEPICK_JUDGE_KEY or OPENROUTER_API_KEY.
Sample files are only ever copied, never moved. Copies stay on this machine.
"""
import argparse
import filecmp
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import config
import library

HERE = Path(__file__).resolve().parent
DATA = config.data_dir()
ROOT = config.root_dir()


def judge_ask(question, scope, options=None):
    """The one place that reaches the (paid) judge; tests replace it."""
    import sample_judge
    if not (DATA / "index.jsonl").exists():
        raise RuntimeError("no index yet: run `python3 samplepick.py index` (set SAMPLEPICK_LIBRARY or pass --library)")
    return sample_judge.ask(DATA, question, scope, options)


# ---- conversations: data/talks/<id>.json, each with its own messages and numbered list ----

def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    tmp.replace(path)


def _talks_dir() -> Path:
    d = DATA / "talks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _valid_id(cid) -> bool:
    return isinstance(cid, str) and re.fullmatch(r"[0-9A-Za-z_-]{1,64}", cid) is not None


def _conv_path(cid: str) -> Path:
    if not _valid_id(cid):
        raise ValueError(f"bad conversation id: {cid!r}")
    return _talks_dir() / f"{cid}.json"


def default_title(t=None) -> str:
    lt = time.localtime(t)
    return time.strftime("%b %d %H:%M", lt)


def title_from(text: str) -> str:
    text = re.sub(r"\[ticked: [^\]]*\]\s*$", "", text or "").strip().replace("\n", " ")
    return text[:16] if text else ""


def _new_id() -> str:
    base = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
    cid, k = base, 1
    while (_talks_dir() / f"{cid}.json").exists():
        k += 1
        cid = f"{base}-{k}"
    return cid


def _fresh(backend="claude") -> dict:
    now = _now()
    return {"id": _new_id(), "title": default_title(), "renamed": False, "created": now, "updated": now,
            "backend": backend, "messages": [], "items": []}


def _keep_old(p: Path):
    bak = p.with_name(p.name + ".bak")
    k = 1
    while bak.exists():
        k += 1
        bak = p.with_name(f"{p.name}.bak{k}")
    p.replace(bak)


def load_conv(cid: str) -> dict | None:
    try:
        d = json.loads(_conv_path(cid).read_text())
    except (OSError, ValueError):
        return None
    d.setdefault("id", cid)
    d.setdefault("title", cid)
    d.setdefault("backend", "claude")
    for k in ("messages", "items"):
        if not isinstance(d.get(k), list):
            d[k] = []
    return d


def save_conv(conv: dict):
    conv["updated"] = _now()
    conv["ts"] = time.time()
    _write_json(_conv_path(conv["id"]), conv)


def list_convs() -> list[dict]:
    out = []
    for f in _talks_dir().glob("*.json"):
        c = load_conv(f.stem)
        if c:
            out.append({"id": c["id"], "title": c["title"], "updated": c.get("updated") or "", "ts": c.get("ts") or 0,
                        "count": sum(1 for m in c["messages"] if m.get("role") == "user")})
    return sorted(out, key=lambda c: (c["ts"], c["updated"], c["id"]), reverse=True)


def set_current(cid: str):
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "current.txt").write_text(cid)


def current_conv() -> dict:
    try:
        cid = (DATA / "current.txt").read_text().strip()
    except OSError:
        cid = ""
    c = load_conv(cid) if _valid_id(cid) else None
    if c is None:
        rows = list_convs()
        c = load_conv(rows[0]["id"]) if rows else None
    if c is None:
        c = new_conv()
    elif c["id"] != cid:
        set_current(c["id"])
    return c


def new_conv() -> dict:
    try:
        backend = current_conv_backend()
    except Exception:
        backend = "claude"
    c = _fresh(backend)
    save_conv(c)
    set_current(c["id"])
    return c


def current_conv_backend() -> str:
    try:
        cid = (DATA / "current.txt").read_text().strip()
        return (load_conv(cid) or {}).get("backend") or "claude"
    except OSError:
        return "claude"


def switch_conv(cid: str) -> dict:
    c = load_conv(cid) if _valid_id(cid) else None
    if c is None:
        raise ValueError(f"no such conversation: {cid}")
    set_current(cid)
    return c


def rename_conv(cid: str, title: str) -> dict:
    title = (title or "").strip()
    if not title:
        raise ValueError("the name cannot be empty")
    c = load_conv(cid) if _valid_id(cid) else None
    if c is None:
        raise ValueError(f"no such conversation: {cid}")
    c["title"], c["renamed"] = title[:60], True
    save_conv(c)
    return c


def delete_conv(cid: str) -> dict:
    """Delete one; if it was the current one, switch to the newest other (or a fresh one). Returns the current."""
    if not (_valid_id(cid) and _conv_path(cid).exists()):
        raise ValueError(f"no such conversation: {cid}")
    cur = current_conv()["id"]
    _conv_path(cid).unlink()
    if cid == cur:
        rows = list_convs()
        return switch_conv(rows[0]["id"]) if rows else new_conv()
    return current_conv()


# ---- notes (the tool's own notebook, read before every pick) and the blocklist ----

def get_notes() -> str:
    try:
        return (DATA / "notes.md").read_text()
    except OSError:
        return ""


def set_notes(text: str, by: str = "user") -> bool:
    """Replace the notes; the previous version goes to notes-history.jsonl first. False when nothing changed."""
    text = text if isinstance(text, str) else str(text or "")
    old = get_notes()
    if text == old:
        return False
    DATA.mkdir(parents=True, exist_ok=True)
    if old:
        with (DATA / "notes-history.jsonl").open("a") as fh:
            fh.write(json.dumps({"time": _now(), "by": by, "text": old}, ensure_ascii=False) + "\n")
    tmp = DATA / "notes.md.tmp"
    tmp.write_text(text)
    tmp.replace(DATA / "notes.md")
    return True


def blocked() -> dict:
    try:
        d = json.loads((DATA / "blocked.json").read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def blocked_list() -> list[dict]:
    rows = [{"path": p, **{k: v.get(k, "") for k in ("name", "why", "time")}} for p, v in blocked().items()
            if isinstance(v, dict)]
    return sorted(rows, key=lambda r: (r["time"], r["path"]))


def block(items=(), paths=(), why: str = "") -> dict:
    """Never pick these again: list numbers and/or paths."""
    todo = []  # (path, name)
    for x in items or ():
        it = get_item(x)
        if not it:
            return {"error": f"not in the list: {x}"}
        todo.append((it["path"], it["name"]))
    for p in paths or ():
        p = str(p)
        row = _index()["by_path"].get(p) or {}
        todo.append((p, row.get("name") or p.split("/")[-1].rsplit(".", 1)[0]))
    d = blocked()
    for p, name in todo:
        d[p] = {"name": name, "why": str(why or ""), "time": _now()}
    if todo:
        _write_json(DATA / "blocked.json", d)
    return {"blocked": [{"path": p, "name": n} for p, n in todo], "why": str(why or "")}


def unblock(target: str) -> dict:
    """target: a path, or a name / file name (all blocked items carrying that name)."""
    d = blocked()
    t = str(target or "").strip()
    hit = [p for p, v in d.items() if p == t or (isinstance(v, dict) and v.get("name") == t)
           or p.split("/")[-1] == t or p.split("/")[-1].rsplit(".", 1)[0] == t]
    if not hit:
        return {"error": f"not blocked: {t}"}
    for p in hit:
        del d[p]
    _write_json(DATA / "blocked.json", d)
    return {"unblocked": hit}


# ---- per-sample counters: how often each sample was shown to the user, and how often it was saved ----
# data/stats.json {path: {name, shown, saved, last_shown, last_saved}}

_STATS_LOCK = threading.RLock()


def _backfill_stats() -> dict:
    """Rebuild the counters from history: every conversation's list items (shown) and saved.jsonl (saved)."""
    st = {}

    def row(path, name):
        r = st.setdefault(path, {"name": name or path.split("/")[-1].rsplit(".", 1)[0], "shown": 0, "saved": 0,
                                 "last_shown": "", "last_saved": ""})
        return r
    for f in sorted(_talks_dir().glob("*.json")):
        c = load_conv(f.stem)
        for it in (c or {}).get("items") or []:
            if isinstance(it, dict) and it.get("path"):
                r = row(it["path"], it.get("name"))
                r["shown"] += 1
                r["last_shown"] = max(r["last_shown"], c.get("updated") or c.get("created") or "")
    try:
        lines = (DATA / "saved.jsonl").read_text().splitlines()
    except OSError:
        lines = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        for fl in rec.get("files") or []:
            if isinstance(fl, dict) and fl.get("path"):
                name = (_index()["by_path"].get(fl["path"]) or {}).get("name")
                r = row(fl["path"], name or Path(str(fl.get("dest") or fl["path"])).stem)
                r["saved"] += 1
                r["last_saved"] = max(r["last_saved"], rec.get("time") or "")
    return st


def load_stats() -> dict:
    """The counters; built once from history when stats.json does not exist yet."""
    with _STATS_LOCK:
        f = DATA / "stats.json"
        if not f.exists():
                    _write_json(f, _backfill_stats())
        try:
            d = json.loads(f.read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}


def _bump(entries, field: str):
    """entries: [(path, name)]; +1 on `field` (shown|saved) for each."""
    if not entries:
        return
    with _STATS_LOCK:
        st = load_stats()
        now = _now()
        for path, name in entries:
            r = st.setdefault(path, {"name": name, "shown": 0, "saved": 0, "last_shown": "", "last_saved": ""})
            r[field] = int(r.get(field) or 0) + 1
            r["last_" + field] = now
        _write_json(DATA / "stats.json", st)


def stats(path=None):
    """One sample's counters {shown, saved, ...} by path, or the whole table."""
    st = load_stats()
    if path is None:
        return st
    return st.get(str(path)) or {"name": "", "shown": 0, "saved": 0, "last_shown": "", "last_saved": ""}


def with_counts(items) -> list[dict]:
    """Copies of list items carrying the current totals as "shown" / "saved"."""
    st = load_stats()
    out = []
    for it in items:
        r = st.get(it.get("path")) or {}
        out.append({**it, "shown": int(r.get("shown") or 0), "saved": int(r.get("saved") or 0)})
    return out


def stats_top(top: int = 20, sort: str = "shown") -> list[dict]:
    other = "saved" if sort == "shown" else "shown"
    rows = [{"path": p, **v} for p, v in load_stats().items() if isinstance(v, dict) and v.get(sort)]
    rows.sort(key=lambda r: (-int(r.get(sort) or 0), -int(r.get(other) or 0), r["path"]))
    return rows[:top]


# ---- the numbered list (the current conversation's) ----

def load_list() -> dict:
    return {"items": current_conv()["items"]}


def _store(d: dict):
    c = current_conv()
    c["items"] = d["items"]
    save_conv(c)


def new_list():
    """A new numbered list is a new conversation."""
    new_conv()


def list_items() -> list[dict]:
    return load_list()["items"]


def get_item(n) -> dict | None:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    return next((i for i in list_items() if i["n"] == n), None)


def to_win(p) -> str:
    try:
        r = subprocess.run(["wslpath", "-w", str(p)], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return str(p)


AUDIO_EXT = tuple(sorted(library.AUDIO))
_INDEX = {"key": None, "by_path": {}, "by_name": {}}


def _index() -> dict:
    """Index rows by path and by filename; built once, rebuilt if the index file changes."""
    f = DATA / "index.jsonl"
    key = (str(f), f.stat().st_mtime_ns, f.stat().st_size) if f.exists() else (str(f), None, None)
    if _INDEX["key"] != key:
        by_path, by_name = {}, {}
        for r in library.load_index(DATA):
            if r.get("kind", "sample") != "sample":
                continue
            by_path[r["path"]] = r
            by_name.setdefault(r["path"].split("/")[-1], r)
        _INDEX.update(key=key, by_path=by_path, by_name=by_name)
    return _INDEX


def _saved_origin(dest: Path) -> str | None:
    """The path a file was copied from, according to saved.jsonl."""
    try:
        lines = (DATA / "saved.jsonl").read_text().splitlines()
    except OSError:
        return None
    want = str(dest)
    for ln in reversed(lines):
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        for f in rec.get("files") or []:
            if f.get("dest") == want and f.get("path"):
                return f["path"]
    return None


def _clean_stem(filename: str) -> str:
    stem = re.sub(r"\.(wav|aif+|flac|mp3|ogg)$", "", filename, flags=re.I)
    return re.sub(r"[_-]+", " ", stem).strip()


def describe_file(folder: str, filename: str) -> dict:
    """What the index knows about ROOT/folder/filename (a copy made here, or a file the user dropped in)."""
    name = clean_folder(folder)
    if not filename or "/" in filename or "\\" in filename or filename in (".", ".."):
        raise ValueError(f"bad file name: {filename!r}")
    dest = ROOT / name / filename
    idx = _index()
    row = None
    origin = _saved_origin(dest)
    if origin:
        row = idx["by_path"].get(origin) or idx["by_name"].get(origin.split("/")[-1])
    if row is None:
        row = idx["by_name"].get(filename)
    base = {"file": filename, "folder": name, "exists": dest.is_file()}
    if row is None:
        return {**base, "found": False, "path": None, "line": _clean_stem(filename), "facts": {}}
    import sample_judge
    facts = {"type": row.get("type"), "key": row.get("key"), "bpm": row.get("bpm"),
             "tags": [t for t in row.get("tags") or [] if t], "pack": row.get("pack") or "", "sec": row.get("sec")}
    return {**base, "found": True, "path": row["path"], "line": sample_judge.line(row), "facts": facts}


def look(folder: str) -> dict:
    name = clean_folder(folder)
    p = ROOT / name
    if not p.is_dir():
        raise ValueError(f"no such folder: {name}")
    files = [describe_file(name, f.name) for f in sorted(p.iterdir())
             if f.is_file() and f.suffix.lower() in AUDIO_EXT]
    return {"folder": name, "files": files}


def folders_summary(cap: int = 12) -> str:
    rows = []
    for f in folders():
        names = sorted(x.name for x in (ROOT / f["name"]).iterdir()
                       if x.is_file() and x.suffix.lower() in AUDIO_EXT)
        shown = ", ".join(names[:cap]) + (f", … {len(names)} in all" if len(names) > cap else "")
        rows.append(f"{f['name']} ({len(names)} files)" + (": " + shown if names else ""))
    return "\n".join(rows) if rows else "(no folders yet)"


def _resolve_like(like) -> list[dict]:
    refs = []
    for ref in like or []:
        folder, _, fname = str(ref).partition("/")
        if not folder or not fname:
            raise ValueError(f"like must be \"folder/filename\": {ref!r}")
        d = describe_file(folder, fname)
        if not d["exists"] and not d["found"]:
            raise ValueError(f"reference file not found: {ref}")
        refs.append(d)
    return refs


def ask(question: str, type=None, words=None, key=None, bpm=None, options=None, slot="",
        new=False, like=None) -> dict:
    refs = _resolve_like(like)
    load_stats()  # history is counted before this showing is added to it
    if refs:
        question = question + " Reference sample(s): " + "; ".join(f'"{d["line"]}"' for d in refs)
    scope = {k: v for k, v in (("type", type), ("words", words), ("key", key), ("bpm", bpm)) if v}
    got = judge_ask(question, scope, options or None)
    if new:
        new_list()
    d = load_list()
    n = max([i["n"] for i in d["items"]] or [0])
    out = []
    ref_paths = {d["path"] for d in refs if d.get("path")}
    no = blocked()
    skipped = 0
    for t in got.get("top", []):
        if t["path"] in ref_paths:  # the reference itself always wins; it is not a find
            continue
        if t["path"] in no:  # the user said never again
            skipped += 1
            continue
        n += 1
        f = Path(t["path"])
        facts = t.get("facts") or {}
        item = {"n": n, "slot": slot or "", "question": question, "score": t.get("score"),
                "path": t["path"], "file": str(f),
                "name": facts.get("name") or f.stem, "about": t.get("about") or "", "facts": facts}
        if t.get("option") is not None:
            item["option"] = t["option"]
        out.append(item)
    d["items"] += out
    _store(d)
    _bump([(i["path"], i["name"]) for i in out], "shown")
    res = {"asked": got.get("asked", 0), "cost": got.get("cost", 0), "items": with_counts(out)}
    if refs:
        res["like"] = [d["line"] for d in refs]
    if skipped:
        res["blocked_skipped"] = skipped
    for k in ("too_many", "note"):
        if got.get(k):
            res[k] = got[k]
    return res


def is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def reveal_command(target, select: bool, platform: str | None = None, wsl: bool | None = None) -> list[str]:
    """The command that shows `target` in the file manager. `select`: target is a file to highlight
    (a folder is just opened). WSL: Windows Explorer (explorer.exe, path converted with wslpath);
    macOS: `open -R` for a file, `open` for a folder; other Linux: `xdg-open` on the folder
    (it cannot highlight a file, so a file opens its folder)."""
    platform = platform or sys.platform
    if wsl is None:  # Windows Explorer is only usable from WSL when interop is on
        wsl = platform.startswith("linux") and is_wsl() and shutil.which("explorer.exe") is not None
    target = Path(target)
    if wsl:
        return ["explorer.exe", ("/select," if select else "") + to_win(target)]
    if platform == "win32":
        return ["explorer.exe", ("/select," if select else "") + str(target)]
    if platform == "darwin":
        return ["open", "-R", str(target)] if select else ["open", str(target)]
    return ["xdg-open", str(target.parent if select else target)]


def _open(argv: list[str]):
    """Run a file-manager command; tests replace this. explorer.exe exits 1 even on success."""
    try:
        subprocess.run(argv, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass


def reveal(target) -> dict:
    """target: list number, or a file path that is in the list."""
    item = get_item(target) if str(target).isdigit() else next(
        (i for i in list_items() if target in (i["path"], i["file"])), None)
    if not item:
        return {"error": f"not in the list: {target}"}
    _open(reveal_command(item["file"], True))
    return {"revealed": item["n"], "file": item["file"]}


def clean_folder(name: str) -> str:
    name = (name or "").strip()
    if (not name or name.startswith(".") or "/" in name or "\\" in name or ".." in name
            or any(c in name for c in '<>:"|?*\0')):
        raise ValueError(f"bad folder name: {name!r}")
    return name


def reveal_folder(name: str) -> dict:
    p = ROOT / clean_folder(name)
    if not p.is_dir():
        return {"error": f"no such folder: {name}"}
    _open(reveal_command(p, False))
    return {"folder": name, "path": str(p)}


def _dest(src: Path, folder: Path) -> tuple[Path, bool]:
    """(destination, already_there)"""
    cand = folder / src.name
    k = 1
    while cand.exists():
        if filecmp.cmp(src, cand, shallow=False):
            return cand, True
        k += 1
        cand = folder / f"{src.stem} ({k}){src.suffix}"
    return cand, False


def save(folder: str, items=(), paths=(), open_after=False) -> dict:
    """Copy list items (numbers) and/or explicit files into ROOT/folder."""
    name = clean_folder(folder)
    srcs = []  # (n, path, Path)
    for x in items:
        it = get_item(x)
        if not it:
            return {"error": f"not in the list: {x}"}
        srcs.append((it["n"], it["path"], Path(it["file"])))
    for p in paths:
        srcs.append((None, str(p), Path(p)))
    for _, _, f in srcs:
        if not f.is_file():
            return {"error": f"file missing: {f}"}
    target = ROOT / name
    target.mkdir(parents=True, exist_ok=True)
    added, skipped = [], []
    for n, vp, f in srcs:
        dest, there = _dest(f, target)
        if there:
            skipped.append({"n": n, "path": vp, "dest": str(dest)})
            continue
        shutil.copy2(f, dest)
        added.append({"n": n, "path": vp, "dest": str(dest)})
    DATA.mkdir(parents=True, exist_ok=True)
    load_stats()  # history is counted before this save is added to it
    with (DATA / "saved.jsonl").open("a") as fh:
        fh.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "folder": name,
                             "files": added}, ensure_ascii=False) + "\n")
    _bump([(a["path"], (_index()["by_path"].get(a["path"]) or {}).get("name") or Path(a["dest"]).stem)
           for a in added], "saved")
    if open_after:
        _open(reveal_command(target, False))
    return {"folder": name, "path": str(target), "added": added, "skipped": skipped}


def folders() -> list[dict]:
    if not ROOT.is_dir():
        return []
    return [{"name": p.name, "count": sum(1 for f in p.iterdir() if f.is_file())}
            for p in sorted(ROOT.iterdir()) if p.is_dir() and not p.name.startswith(".")]


def rename_folder(old: str, new: str) -> dict:
    a, b = clean_folder(old), clean_folder(new)
    pa, pb = ROOT / a, ROOT / b
    if not pa.is_dir():
        return {"error": f"no such folder: {a}"}
    if a == b:
        return {"folder": b}
    if pb.exists() and not (a.lower() == b.lower() and pa.samefile(pb)):
        return {"error": f"a folder named \"{b}\" already exists"}
    pa.rename(pb)
    return {"folder": b, "from": a}


def delete_folder(name: str) -> dict:
    """Move ROOT/name to ROOT/.trash/<name>-<timestamp>; copies only, the library's originals are never touched."""
    n = clean_folder(name)
    p = ROOT / n
    if not p.is_dir():
        return {"error": f"no such folder: {n}"}
    count = sum(1 for f in p.iterdir() if f.is_file())
    trash = ROOT / ".trash"
    trash.mkdir(exist_ok=True)
    dest = trash / f"{n}-{time.strftime('%Y%m%d-%H%M%S')}"
    k = 1
    while dest.exists():
        k += 1
        dest = trash / f"{n}-{time.strftime('%Y%m%d-%H%M%S')}-{k}"
    shutil.move(str(p), str(dest))
    return {"deleted": n, "count": count, "trash": str(dest)}


# ---- index and analysis ----

def index_library(extra=()) -> dict:
    libs = config.libraries(extra)
    if not libs:
        raise ValueError("no sample folders: set SAMPLEPICK_LIBRARY or pass --library DIR")
    r = library.build_index(DATA, libs)
    _INDEX["key"] = None
    return r


def analyze_library(type=None, words=None, key=None, bpm=None, limit=200) -> dict:
    """Listen ahead: fill data/analysis.jsonl for the samples inside a scope, so a later ask finds them heard."""
    import analyze
    import sample_judge
    if not analyze.enabled():
        raise ValueError("listening is not set up: set SAMPLEPICK_TIMBRE_PYTHON and/or SAMPLEPICK_PHONON_URL (see README)")
    scope = {k: v for k, v in (("type", type), ("words", words), ("key", key), ("bpm", bpm)) if v}
    pool = sample_judge.candidates(DATA, scope)[:limit]
    analyze.analyse(DATA, pool, background=False)
    got = analyze.load(DATA)
    return {"samples": len(pool), "analysed": sum(1 for s in pool if got.get(s["path"]))}


# ---- CLI ----

def _out(x):
    print(json.dumps(x, ensure_ascii=False, indent=1))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="samplepick")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask")
    a.add_argument("question")
    a.add_argument("--type", choices=["loop", "oneshot"])
    a.add_argument("--words", nargs="*")
    a.add_argument("--key")
    a.add_argument("--bpm")
    a.add_argument("--options", help="JSON {id: description}")
    a.add_argument("--slot", default="")
    a.add_argument("--new", action="store_true")
    a.add_argument("--like", action="append", help='reference file "folder/filename" (repeatable)')
    ix = sub.add_parser("index")
    ix.add_argument("--library", action="append", help="a sample folder (repeatable; default: SAMPLEPICK_LIBRARY)")
    an = sub.add_parser("analyze")
    an.add_argument("--type", choices=["loop", "oneshot"])
    an.add_argument("--words", nargs="*")
    an.add_argument("--key")
    an.add_argument("--bpm")
    an.add_argument("--limit", type=int, default=200, help="at most this many samples (default 200)")
    lk = sub.add_parser("look")
    lk.add_argument("folder")
    sub.add_parser("list")
    r = sub.add_parser("reveal")
    r.add_argument("target", nargs="?")
    r.add_argument("--folder")
    s = sub.add_parser("save")
    s.add_argument("folder")
    s.add_argument("items", nargs="*")
    s.add_argument("--open", action="store_true")
    sub.add_parser("folders")
    rn = sub.add_parser("rename")
    rn.add_argument("old")
    rn.add_argument("new")
    rm = sub.add_parser("rmdir")
    rm.add_argument("name")
    nt = sub.add_parser("notes")
    nt.add_argument("--set")
    nt.add_argument("--edit-file")
    bl = sub.add_parser("block")
    bl.add_argument("items", nargs="+")
    bl.add_argument("--why", default="")
    ub = sub.add_parser("unblock")
    ub.add_argument("target")
    sub.add_parser("blocked")
    stp = sub.add_parser("stats")
    stp.add_argument("--top", type=int, default=20)
    stp.add_argument("--sort", choices=["shown", "saved"], default="shown")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "ask":
            _out(ask(args.question, args.type, args.words, args.key, args.bpm,
                     json.loads(args.options) if args.options else None, args.slot, args.new, args.like))
        elif args.cmd == "index":
            _out(index_library(args.library))
        elif args.cmd == "analyze":
            _out(analyze_library(args.type, args.words, args.key, args.bpm, args.limit))
        elif args.cmd == "look":
            _out(look(args.folder))
        elif args.cmd == "list":
            _out({"items": list_items()})
        elif args.cmd == "reveal":
            _out(reveal_folder(args.folder) if args.folder else reveal(args.target))
        elif args.cmd == "save":
            nums = [x for x in args.items if x.isdigit()]
            paths = [x for x in args.items if not x.isdigit()]
            _out(save(args.folder, nums, paths, args.open))
        elif args.cmd == "rename":
            r = rename_folder(args.old, args.new)
            _out(r)
            return 1 if "error" in r else 0
        elif args.cmd == "rmdir":
            r = delete_folder(args.name)
            _out(r)
            return 1 if "error" in r else 0
        elif args.cmd == "notes":
            if args.edit_file:
                args.set = Path(args.edit_file).read_text()
            if args.set is not None:
                _out({"changed": set_notes(args.set, "user")})
            else:
                print(get_notes())
        elif args.cmd == "block":
            r = block([x for x in args.items if x.isdigit()], [x for x in args.items if not x.isdigit()], args.why)
            _out(r)
            return 1 if "error" in r else 0
        elif args.cmd == "unblock":
            r = unblock(args.target)
            _out(r)
            return 1 if "error" in r else 0
        elif args.cmd == "blocked":
            _out({"blocked": blocked_list()})
        elif args.cmd == "stats":
            _out({"sort": args.sort, "samples": stats_top(args.top, args.sort)})
        elif args.cmd == "folders":
            _out({"root": str(ROOT), "folders": folders()})
    except (ValueError, RuntimeError) as e:
        _out({"error": str(e)})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
