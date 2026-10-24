"""One call to the judge, logged: the only place that reaches the (paid) model.

The judge is TypeSafe Jev, a decision model on OpenRouter. It reads a text `state` and a dict of
`questions` and answers each one: {"type": "noul"} questions come back as a yes-probability under
"noul", {"type": "choice", "criteria": {...}} questions as a "choice" plus "probabilities".

Key: SAMPLEPICK_JUDGE_KEY, else OPENROUTER_API_KEY. Overrides: SAMPLEPICK_JUDGE_URL,
SAMPLEPICK_JUDGE_MODEL, SAMPLEPICK_JUDGE_TIMEOUT (seconds). No retries: a failed call raises
JudgeError and the caller decides. Every call, failed ones included, is appended to
<data>/judge.jsonl with the full state, questions and answers: that file is the record of what
was asked and what it cost.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request

import config

MODEL = "typesafe/jev-1.13"
URL = "https://openrouter.ai/api/alpha/decisions"
TIMEOUT = 30.0  # seconds; a slower self-hosted model can raise it with SAMPLEPICK_JUDGE_TIMEOUT

_LOG_LOCK = threading.Lock()  # batches are sent from several threads at once


class JudgeError(RuntimeError):
    """The judge did not answer properly: no key, a network error, HTTP not 200, or no `answers` in the JSON."""

    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class _Response:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


def _post(url: str, *, json: dict, headers: dict, timeout: float) -> _Response:  # noqa: A002 (mirrors requests.post)
    """The one line that really reaches the network; tests replace this module variable with a fake."""
    import json as _json
    req = urllib.request.Request(url, _json.dumps(json).encode(), {"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _Response(r.status, r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return _Response(e.code, e.read().decode("utf-8", "replace"))


def endpoint() -> tuple[str, str, dict]:
    """Where this call goes, which model it names, and its headers. Only an OpenRouter endpoint gets
    an Authorization header; a self-hosted one (SAMPLEPICK_JUDGE_URL elsewhere) usually takes no key."""
    url = os.environ.get("SAMPLEPICK_JUDGE_URL", "").strip() or URL
    model = os.environ.get("SAMPLEPICK_JUDGE_MODEL", "").strip() or MODEL
    headers = {}
    if "openrouter.ai" in url:
        key = (os.environ.get("SAMPLEPICK_JUDGE_KEY", "").strip()
               or os.environ.get("OPENROUTER_API_KEY", "").strip())
        headers["Authorization"] = f"Bearer {key}"
    return url, model, headers


def check_ready() -> None:
    """Raise before any work is done when the judge cannot be called at all (OpenRouter and no key)."""
    url, _, headers = endpoint()
    if "openrouter.ai" in url and headers.get("Authorization", "") in ("", "Bearer "):
        raise JudgeError(0, "no judge key: set SAMPLEPICK_JUDGE_KEY (or OPENROUTER_API_KEY)")


def _timeout() -> float:
    raw = os.environ.get("SAMPLEPICK_JUDGE_TIMEOUT", "").strip()
    return float(raw) if raw else TIMEOUT


def ask(state_text: str, questions: dict, *, data=None, caller: str = "samplepick") -> dict:
    """Ask once. Returns {"answers", "usage", "model", "id"}. **No retries.**"""
    check_ready()
    url, model, headers = endpoint()
    info = {"model": model, "url": url, "id": "", "usage": None, "provider": ""}
    started = time.time()

    def fail(answers):
        _log(state_text, questions, answers, time.time() - started, info, data, caller)

    try:
        response = _post(url, json={"model": model, "state": state_text, "questions": questions},
                         headers=headers, timeout=_timeout())
    except Exception as e:  # network down, timeout, DNS: still a call that was tried
        fail({"error": {"status": 0, "body": f"{type(e).__name__}: {e}"[:2000]}})
        raise JudgeError(0, f"{type(e).__name__}: {e}") from None
    if response.status_code != 200:
        fail({"error": {"status": response.status_code, "body": str(response.text)[:2000]}})
        raise JudgeError(response.status_code, response.text)
    try:
        body = response.json()
    except ValueError:
        fail({"error": {"status": response.status_code, "body": str(response.text)[:2000]}})
        raise JudgeError(response.status_code, response.text) from None
    if not isinstance(body, dict) or "answers" not in body:
        fail({"error": {"status": response.status_code, "body": body}})
        raise JudgeError(response.status_code, body)
    # the model that really answered (OpenRouter returns the dated name), the response id and the usage
    info.update({"model": body.get("model") or model, "id": body.get("id", ""),
                 "usage": body.get("usage"), "provider": body.get("provider", "")})
    _log(state_text, questions, body["answers"], time.time() - started, info, data, caller)
    return {"answers": body["answers"], "usage": info["usage"], "model": info["model"], "id": info["id"]}


def _log(state_text: str, questions: dict, answers: dict, elapsed: float, info: dict, data, caller: str) -> None:
    """Append one line to <data>/judge.jsonl: the state, the whole question set, the whole answer set
    (`{"error": ...}` for a failed call), which model and endpoint, the response id, the usage and cost."""
    path = (config.data_dir() if data is None else data) / "judge.jsonl"
    row = {"ts": round(time.time(), 2), "elapsed": round(elapsed, 3), "model": info.get("model", ""),
           "url": info.get("url", ""), "id": info.get("id", ""), "usage": info.get("usage"),
           "caller": caller, "state": state_text, "questions": questions, "answers": answers}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_LOCK, path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass  # a log that cannot be written must not break the call
