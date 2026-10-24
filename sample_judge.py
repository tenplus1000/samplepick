"""Pick samples by asking the judge.

The collaborator writes the question; this program decides everything else: which samples are
candidates (from the index, narrowed by type, words, key, tempo), how many go into
one call (BATCH), and how the answers are ranked. The judge only reads text — a sample reaches it
as its name, tags, pack, key and tempo — so answers are used as a ranking, never against
a fixed threshold (first probes: names without a mood word sit around 0.3). The best SHORTLIST of
that first pass are then heard (analyze.py: timbre, and chords of pitched loops, each sample once;
only when listening is set up) and asked again with what they sound like, and that second answer
is the ranking returned.

Roughly what one ask costs: a batch of 300 answers in under a second for ~$0.0009, so about
$0.003-0.004 per thousand candidates, a minute and a half for a whole library at MAX_ASK.
"""
import re
import sys
import time
from pathlib import Path

import analyze
import judge
import library

BATCH = 300          # questions per call: 300 answer in under a second for ~$0.0009
SHOW = 12            # how many a question returns
PARALLEL = 16        # batches sent at the same time (the whole library is ~15)
SHORTLIST = 30       # how many of the first pass are heard and asked again
MAX_ASK = 3000       # more candidates than this and the question comes back unasked: narrow it first


def line(s: dict, heard: str = "") -> str:
    name = re.sub(r"\.(wav|aif+|flac|mp3|ogg)$", "", s["path"].split("/")[-1], flags=re.I)
    bits = [f"tags: {', '.join(t for t in s['tags'] if t)}", f"pack: {s.get('pack') or ''}"]
    if s.get("key"):
        bits.append(f"key: {s['key']}")
    if s.get("bpm"):
        bits.append(f"{s['bpm']} bpm")
    if heard:
        bits.append(heard)
    return f"{re.sub(r'[_-]+', ' ', name)} — " + "; ".join(bits) + f"; {s['type']}"


def candidates(data: Path, scope: dict) -> list[dict]:
    """Samples in the index, narrowed by the scope."""
    out = []
    lo = hi = None
    if scope.get("bpm"):
        a, _, b = str(scope["bpm"]).partition("-")
        lo, hi = float(a), float(b or a)
    words = [w.lower() for w in scope.get("words", [])]
    reach = {}  # a library folder whose drive is unplugged is left out until it is back
    for s in library.load_index(data):
        if s.get("kind", "sample") != "sample":
            continue
        top = s.get("library") or ""
        if top not in reach:
            reach[top] = library.reachable(top) if top else True
        if not reach[top]:
            continue
        if scope.get("type") and s["type"] != scope["type"]:
            continue
        hay = " ".join([s["path"], " ".join(s["tags"]), s.get("pack") or ""]).lower()
        if not all(w in hay for w in words):
            continue
        if scope.get("key") and (s.get("key") or "") != str(scope["key"]).lower():
            continue
        if lo is not None and not (s.get("bpm") and lo <= s["bpm"] <= hi):
            continue
        out.append(s)
    return out


def call(state: str, questions: dict, data: Path) -> dict:
    """One judge call (judge.py logs it); returns {"answers", "usage", ...}. Tests replace judge._post."""
    return judge.ask(state, questions, data=data)


def ask(data: Path, question: str, scope: dict, options: dict | None = None) -> dict:
    """Ask `question` of every candidate. `question` holds {n} where the sample number goes.
    Yes/no when `options` is empty; otherwise a choice among `options` ({id: description}).
    Returns {"asked", "heard", "listen_s", "calls", "cost", "top": [...]} — for a choice, the best few per option;
    each "about" carries what the sample sounded like when it was heard."""
    judge.check_ready()  # no key: say so now, before any work
    pool = candidates(data, scope)
    if not pool:
        return {"asked": 0, "calls": 0, "cost": 0.0, "top": []}
    if len(pool) > MAX_ASK:  # a whole library at ~$0.004 per thousand, a minute and a half: not for one question
        return {"asked": 0, "calls": 0, "cost": 0.0, "top": [], "too_many": len(pool),
                "note": f"{len(pool)} samples match this scope; the judge takes at most {MAX_ASK}. Narrow it: add words "
                        f"(an instrument or sound, e.g. ['erhu'] or ['kick']), type, bpm or key, then ask again."}
    if "{n}" not in question:
        question = question.rstrip("?？ ") + " — sample #{n}?"
    qtype = {"type": "choice", "criteria": options} if options else {"type": "noul"}
    scored, cost, calls = score(pool, question, qtype, options, data=data)
    # hear the best of the first pass, and ask them again with what they sound like
    if options:
        short = []
        for opt in options:
            for x in sorted(scored, key=lambda x: -x[2].get(opt, 0))[: max(3, SHORTLIST // len(options))]:
                if x[0] not in short:
                    short.append(x[0])
    else:
        short = [x[0] for x in sorted(scored, key=lambda x: -x[2]["yes"])[:SHORTLIST]]
    heard, feats = {}, {}
    t0 = time.time()
    try:
        got_a = analyze.analyse(data, short)
        feats = analyze.features(short, got_a)
        heard = analyze.describe(short, got_a, feats)
    except Exception as e:  # hearing is an addition: without it the first pass stands
        print(f"  [analysis] failed, ranking by names: {e!r}"[:300], file=sys.stderr)
    listen_s = round(time.time() - t0, 1)
    if any(heard.values()):
        scored, c2, n2 = score(short, question, qtype, options, heard, data=data)
        cost, calls = cost + c2, calls + n2
    about = lambda s: line(s, heard.get(s["path"], ""))

    def facts(s):  # what the page shows as tags on each row
        f = feats.get(s["path"]) or {}
        return {"name": re.sub(r"\.(wav|aif+|flac|mp3|ogg)$", "", s["path"].split("/")[-1], flags=re.I),
                "type": s["type"], "key": s.get("key"), "bpm": s.get("bpm") or f.get("bpm"), "pack": s.get("pack") or "",
                "sec": s.get("sec"), "words": f.get("words") or [], "reverb": f.get("reverb", False),
                "chords": f.get("chords") or ""}
    if options:
        top = []
        for opt in options:
            ranked = sorted(scored, key=lambda x: -x[2].get(opt, 0))[: max(3, SHOW // len(options))]
            top += [{"option": opt, "score": round(x[2].get(opt, 0), 3), "path": x[0]["path"], "about": about(x[0]),
                     "facts": facts(x[0])} for x in ranked]
    else:
        ranked = sorted(scored, key=lambda x: -x[2]["yes"])[:SHOW]
        top = [{"score": round(x[2]["yes"], 3), "path": x[0]["path"], "about": about(x[0]), "facts": facts(x[0])}
               for x in ranked]
    return {"asked": len(pool), "heard": len(short), "listen_s": listen_s, "calls": calls, "cost": round(cost, 5), "top": top}


def score(pool: list[dict], question: str, qtype: dict, options: dict | None, heard: dict | None = None,
          data: Path | None = None):
    """Ask the question of every sample in pool, BATCH per call, all calls at once."""
    from concurrent.futures import ThreadPoolExecutor
    heard = heard or {}
    parts = [pool[i:i + BATCH] for i in range(0, len(pool), BATCH)]

    def one(part):
        state = "Samples:\n" + "\n".join(f"#{i + 1}: {line(s, heard.get(s['path'], ''))}" for i, s in enumerate(part))
        qs = {f"s{i + 1}": {**qtype, "instructions": question.format(n=i + 1)} for i in range(len(part))}
        return part, call(state, qs, data)

    scored, cost = [], 0.0
    with ThreadPoolExecutor(max_workers=PARALLEL) as ex:  # every batch at once: ~1 s instead of ~3 s each
        for part, body in ex.map(one, parts):
            cost += float((body.get("usage") or {}).get("cost") or 0)
            for i, s in enumerate(part):
                a = body["answers"].get(f"s{i + 1}") or {}
                if options:
                    probs = {k: float(v) for k, v in (a.get("probabilities") or {}).items()}
                    scored.append((s, a.get("choice", ""), probs))
                else:
                    scored.append((s, "", {"yes": float(a.get("noul", 0))}))
    return scored, cost, len(parts)
