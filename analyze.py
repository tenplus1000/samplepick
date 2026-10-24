"""Listen to a few samples so the judge can read what they sound like, not only what they are called.

Optional. Without it the judge ranks by names, tags, pack, key and tempo, and everything works.
Only the judge's shortlist is analysed when you ask (see sample_judge.ask), and each sample once:
results stay in <data>/analysis.jsonl. `samplepick.py analyze` can fill it ahead of time.
Two sources, each off until configured:
  timbre  — AudioCommons timbral models (hardness, depth, brightness, roughness, warmth, sharpness,
            boominess, reverb), on the CPU, via timbre_worker.py in the python named by
            SAMPLEPICK_TIMBRE_PYTHON (the package needs numpy 1.x and librosa 0.9, so give it its own venv)
  Phonon  — an analysis service at SAMPLEPICK_PHONON_URL: chords of pitched loops, and tempo when the
            index has none. Its key detection is not used: on labelled loops it answered the dominant
            three times out of three, and the key in the file or folder name is right. A short clip of
            the audio is uploaded to that URL, so leave it unset if the audio must not leave the machine.
A sample reaches both as its opening seconds, mono 44.1 kHz (a smaller upload; 22 kHz lost onsets);
that clip is made with ffmpeg, which must be on the PATH.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

WORKER = Path(__file__).resolve().parent / "timbre_worker.py"
CLIP = 12            # seconds sent to Phonon (chords need a few bars)
TIMBRE_CLIP = 4      # seconds the timbre models hear (their time grows with length)
PHONON_PARALLEL = 8
UNPITCHED = ("drum", "percussion", "perc", "hat", "kick", "snare", "clap", "shaker", "cymbal", "tops", "break", "fx")
WORDS = {"hardness": ("hard", "soft"), "depth": ("deep", "shallow"), "brightness": ("bright", "dark"),
         "roughness": ("rough", "smooth"), "warmth": ("warm", "cold"), "sharpness": ("sharp", "dull"),
         "boominess": ("boomy", "thin")}


def timbre_python() -> str:
    return os.environ.get("SAMPLEPICK_TIMBRE_PYTHON", "").strip()


def phonon_url() -> str:
    u = os.environ.get("SAMPLEPICK_PHONON_URL", "").strip()
    return u if not u or u.endswith("/") else u + "/"


def enabled() -> bool:
    return bool(timbre_python() or phonon_url())


def _cache_file(data: Path) -> Path:
    return Path(data) / "analysis.jsonl"


def load(data: Path, samples: list[dict] | None = None) -> dict:
    """Rows are appended as each part finishes (timbre now, Phonon later); a sample's rows merge."""
    got = {}
    f = _cache_file(data)
    if f.exists():
        with f.open(encoding="utf-8") as fh:
            for raw in fh:
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                got.setdefault(r["path"], {}).update(r)
    return got


def _append(cache: Path, rows: list[dict]) -> None:
    cache = Path(cache)
    cache.parent.mkdir(parents=True, exist_ok=True)
    with cache.open("a", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def _clip(src: Path, dst: Path, seconds: int) -> bool:
    try:
        r = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-t", str(seconds), "-i", str(src),
                            "-ac", "1", "-ar", "44100", str(dst)], capture_output=True)
    except OSError:  # no ffmpeg
        return False
    return r.returncode == 0 and dst.exists()


def pitched(s: dict) -> bool:
    hay = " ".join([s["path"].split("/")[-1], *s["tags"]]).lower()
    return not any(w in hay for w in UNPITCHED)


def _phonon(instrument: str, wav: Path) -> dict:
    bd = uuid.uuid4().hex
    meta = {"schema_version": "phonon.v1", "instrument_id": instrument, "parameters": {}}
    body = (f"--{bd}\r\nContent-Disposition: form-data; name=\"metadata\"\r\n\r\n{json.dumps(meta)}\r\n"
            f"--{bd}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"a.wav\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n").encode() + wav.read_bytes() + f"\r\n--{bd}--\r\n".encode()
    base = phonon_url()
    req = urllib.request.Request(base + "v1/jobs", body, {"Content-Type": f"multipart/form-data; boundary={bd}"})
    job = json.load(urllib.request.urlopen(req, timeout=60))
    t0 = time.time()
    while time.time() - t0 < 90:
        p = json.load(urllib.request.urlopen(base + f"v1/jobs/{job['job_id']}", timeout=30))
        if p["status"] == "done":
            return p["result"]["result"]
        if p["status"] == "error":
            raise RuntimeError(json.dumps(p.get("error"))[:200])
        time.sleep(0.4)
    raise TimeoutError(instrument)


def _chords(res: dict) -> str:
    out = []
    for c in res.get("chords", []):
        lab = c["label"]
        if lab == "N":
            continue
        root, _, q = lab.partition(":")
        lab = root + ("m" if q == "min" else "")
        if not out or out[-1] != lab:
            out.append(lab)
    return " ".join(out[:8])


def analyse(data: Path, samples: list[dict], background: bool = True) -> dict:
    """{path: analysis} for these samples. Timbre the cache lacks is measured now (seconds), when a timbre
    python is configured; what Phonon adds (when a URL is configured) is asked for by a background process
    and lands in the cache later, for the next question — nobody waits on the network. With neither
    configured this only reads the cache. `background=False` (the `analyze` command) waits for Phonon.
    A sample the tools cannot read simply has less to say."""
    have = load(data)
    cache = _cache_file(data)
    todo = [s for s in samples if timbre_python() and "timbre" not in have.get(s["path"], {})
            and "timbre_error" not in have.get(s["path"], {})]
    remote = [s for s in samples if phonon_url() and s["type"] == "loop" and (pitched(s) or not s.get("bpm"))
              and "phonon" not in have.get(s["path"], {})]
    if remote:
        jobs = [{**s, "src": s["path"], "cache": str(cache)} for s in remote]
        if background:  # detached: outlives this call and this process
            bg = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--phonon"], stdin=subprocess.PIPE,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                                  cwd=str(Path(__file__).resolve().parent))
            bg.stdin.write(json.dumps(jobs).encode())
            bg.stdin.close()
        else:
            phonon_background(jobs)
            have = load(data)
    if not todo:
        return have
    t0 = time.time()
    tmp = Path(tempfile.mkdtemp(prefix="timbre-"))
    files = {}
    for i, s in enumerate(todo):
        if _clip(Path(s["path"]), tmp / f"{i}.wav", TIMBRE_CLIP):
            files[str(tmp / f"{i}.wav")] = s
    rows = []
    if files:
        r = subprocess.run([timbre_python(), str(WORKER), *files], capture_output=True, text=True, timeout=600)
        for raw in r.stdout.splitlines():
            t = json.loads(raw)
            s = files[t["file"]]
            row = {"path": s["path"], "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                   **({"timbre": t["timbre"]} if "timbre" in t else {"timbre_error": t["error"]})}
            rows.append(row)
            have.setdefault(s["path"], {}).update(row)
    shutil.rmtree(tmp, ignore_errors=True)
    _append(cache, rows)
    print(f"  [analysis] timbre of {len(todo)} new samples, {time.time() - t0:.1f} s; "
          f"chords of {len(remote)} asked of Phonon{' in the background' if background else ''}", file=sys.stderr)
    return have


def phonon_background(samples: list[dict]) -> None:
    """Chords of pitched loops, and tempo where the index has none, PHONON_PARALLEL at a time;
    each sample's row is written as soon as it is back."""
    tmp = Path(tempfile.mkdtemp(prefix="phonon-"))

    def one(i_s):
        i, s = i_s
        row = {"path": s["path"], "phonon": time.strftime("%Y-%m-%d %H:%M:%S")}
        c = tmp / f"{i}.wav"
        try:
            if not _clip(Path(s["src"]), c, CLIP):
                raise RuntimeError("cannot read")
            if pitched(s):
                row["chords"] = _chords(_phonon("harmony.chord-recognition", c))
            if not s.get("bpm"):
                row["bpm"] = round(float(_phonon("rhythm.beat-grid", c).get("tempo_bpm") or 0), 1) or None
        except (OSError, urllib.error.URLError, RuntimeError, ValueError, KeyError, TimeoutError) as e:
            row["phonon_error"] = repr(e)[:160]
        _append(Path(s["cache"]), [row])

    with ThreadPoolExecutor(max_workers=PHONON_PARALLEL) as ex:
        list(ex.map(one, enumerate(samples)))
    shutil.rmtree(tmp, ignore_errors=True)


def features(samples: list[dict], got: dict) -> dict:
    """{path: {"words": [...], "reverb": bool, "chords": str, "bpm": float|None}} — each sample's
    timbre said relative to the others in this shortlist (the numbers only mean something side by
    side), plus reverb, chords and a measured tempo. `describe` says it in words for the judge; the
    page shows the same facts as tags."""
    import statistics
    scale = {}
    for k in WORDS:
        vals = sorted(got[s["path"]]["timbre"][k] for s in samples if "timbre" in got.get(s["path"], {}))
        if len(vals) >= 4:
            scale[k] = (vals[len(vals) * 3 // 10], statistics.median(vals), vals[len(vals) * 7 // 10])
    out = {}
    for s in samples:
        a = got.get(s["path"]) or {}
        t = a.get("timbre") or {}
        words = []
        for k, (hi, lo) in WORDS.items():
            if not t or k not in scale:
                continue
            low, mid, high = scale[k]
            if t[k] >= high and t[k] - mid >= 5:
                words.append(hi)
            elif t[k] <= low and mid - t[k] >= 5:
                words.append(lo)
        out[s["path"]] = {"words": words, "reverb": bool(t.get("reverb")), "chords": a.get("chords") or "",
                          "bpm": a.get("bpm") if a.get("bpm") and not s.get("bpm") else None}
    return out


def describe(samples: list[dict], got: dict, feats: dict | None = None) -> dict:
    """{path: text} for the judge, from `features`."""
    feats = feats or features(samples, got)
    out = {}
    for s in samples:
        f, bits = feats[s["path"]], []
        if f["words"]:
            bits.append("sounds " + ", ".join(f["words"]) + " (compared with the other candidates)")
        if f["reverb"]:
            bits.append("reverberant")
        if f["chords"]:
            bits.append(f"chords: {f['chords']}")
        if f["bpm"]:
            bits.append(f"measured {f['bpm']} bpm")
        out[s["path"]] = "; ".join(bits)
    return out


if __name__ == "__main__":
    if sys.argv[1] == "--phonon":
        phonon_background(json.loads(sys.stdin.read()))
