"""Local page for samplepick: chat with a picking agent, a numbered list you can play, check and save.

  python3 server.py [--port 8772]      # then open http://127.0.0.1:8772/

Binds 127.0.0.1 only. The conversations live in data/talks/; each model call is stateless (the whole
transcript goes in the prompt) so the claude and codex backends behave the same. The agent has no tools:
it answers with a JSON object and this program carries out its actions through samplepick.py.
"""
import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import config
import samplepick as sp

PROMPT = config.asset("prompt.md")
TIMEOUT = 180
MAX_EXTRA_ROUNDS = 3  # look -> ask -> reply is common
LOCK = __import__("threading").Lock()


class ChatError(Exception):
    pass


def data_dir() -> Path:
    sp.DATA.mkdir(parents=True, exist_ok=True)
    return sp.DATA


# ---- talk state: the current conversation (data/talks/<id>.json, kept by samplepick) ----

def load_talk() -> dict:
    c = sp.current_conv()
    return {"id": c["id"], "title": c["title"], "renamed": c.get("renamed", False),
            "messages": c["messages"], "backend": c["backend"]}


def save_talk(d: dict):
    """Write messages/backend back into the conversation; its numbered list is only ever changed by samplepick."""
    c = sp.load_conv(d["id"])
    if c is None:
        return
    c["messages"], c["backend"] = d["messages"], d["backend"]
    if d.get("title") is not None:
        c["title"] = d["title"]
    sp.save_conv(c)


def convs_state() -> dict:
    return {"current": sp.current_conv()["id"], "talks": sp.list_convs()}


def log_call(backend, seconds, prompt_chars, reply_chars, ok):
    with (data_dir() / "calls.jsonl").open("a") as f:
        f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "backend": backend,
                            "seconds": round(seconds, 1), "prompt_chars": prompt_chars,
                            "reply_chars": reply_chars, "ok": ok}) + "\n")


# ---- backends ----

def transcript(messages: list[dict]) -> str:
    out, last_user = [], None
    for m in messages:
        if m["role"] in ("error", "judge", "look", "note", "block"):  # display only
            continue
        if m["role"] == "assistant":
            out.append("[ASSISTANT]\n" + (m.get("raw") or m["text"]))
        else:
            out.append("[USER]\n" + m["text"])
            if m["role"] == "user":
                last_user = len(out) - 1
    # the folders as they are now ride on the latest user message, for the model only (never stored)
    if last_user is not None:
        out[last_user] += "\n[folders]\n" + sp.folders_summary()
        out[last_user] += "\n[notes]\n" + (sp.get_notes().strip() or "(no notes yet)")
        nb = len(sp.blocked())
        if nb:
            out[last_user] += f"\n[blocked] {nb} samples"
    return "\n\n".join(out) + "\n\n[ASSISTANT]\n"


# The backends run outside the user's own setup: no hooks (each call would register a session with the
# user's tools), no global CLAUDE.md / AGENTS.md, from an empty folder outside the home directory.
AGENT_CWD = Path(tempfile.gettempdir()) / "samplepick-agent"


def claude_env() -> dict:
    """SAMPLEPICK_CLAUDE_CONFIG_DIR names the login (config folder) to use; unset means claude's own default."""
    # also drop a parent Claude Code session's variables (CLAUDECODE, CLAUDE_CODE_*): with them the child
    # rides on that session's login instead of the one chosen here
    env = {k: v for k, v in os.environ.items()
           if k != "CLAUDE_CONFIG_DIR" and k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_") and k != "CLAUDE_PID"}
    if os.environ.get("SAMPLEPICK_CLAUDE_CONFIG_DIR"):
        env["CLAUDE_CONFIG_DIR"] = os.environ["SAMPLEPICK_CLAUDE_CONFIG_DIR"]
    return env


def run_claude(prompt: str) -> str:
    cwd = AGENT_CWD
    cwd.mkdir(exist_ok=True)
    if not shutil.which("claude"):
        raise ChatError("claude: the `claude` command was not found (install Claude Code, or use the codex backend)")
    r = subprocess.run(["claude", "-p", "--output-format", "json", "--system-prompt", PROMPT.read_text(),
                        "--no-session-persistence", "--disable-slash-commands", "--strict-mcp-config",
                        "--setting-sources", "project", "--settings", '{"disableAllHooks": true}',
                        "--tools", ""],
                       input=prompt, capture_output=True, text=True, timeout=TIMEOUT, cwd=cwd,
                       env=claude_env())
    try:
        body = json.loads(r.stdout)
    except ValueError:
        raise ChatError("claude: " + (r.stderr or r.stdout)[-300:])
    if r.returncode or body.get("is_error"):
        raise ChatError("claude: " + str(body.get("result") or r.stderr)[-300:])
    return body.get("result") or ""


def _codex_command() -> str:
    for name in ("codex", "codex.exe"):
        f = shutil.which(name)
        if f:
            return f
    raise ChatError("codex: the `codex` command was not found")


# bwrap shows only the system, node and the login files of CODEX_HOME, so the user's AGENTS.md, hooks,
# history and config stay out; every tool is off. Without bwrap the backend refuses to run rather than
# run unjailed. SAMPLEPICK_CODEX_HOME names the login folder; unset means codex's own default (~/.codex).
def codex_home() -> Path:
    return Path(os.environ.get("SAMPLEPICK_CODEX_HOME") or Path.home() / ".codex").expanduser()


CODEX_HOME_KEEP = ("auth.json", "version.json", "installation_id", "models_cache.json")
CODEX_DISABLED_FEATURES = (
    "code_mode_host", "goals", "multi_agent", "in_app_local_automation",
    "realtime_conversation", "worktrees", "workspace_dependencies",
    "tool_call_mcp_elicitation", "mentions_v2", "shell_tool", "unified_exec", "apps",
    "browser_use", "browser_use_external", "browser_use_full_cdp_access", "computer_use",
    "in_app_browser", "plugins", "remote_plugin", "hooks", "image_generation",
    "view_image", "skill_search", "tool_suggest", "sleep_tool",
    "skill_mcp_dependency_install", "shell_snapshot",
)


def _node_root(codex: str) -> Path:
    real = Path(os.path.realpath(codex))
    for parent in real.parents:
        if (parent / "bin" / "node").exists():
            return parent
    raise ChatError(f"codex: could not find the node install that codex runs on: {codex}")


def codex_command(codex: str, home: Path | None = None) -> list[str]:
    home = home or codex_home()
    if not shutil.which("bwrap"):
        raise ChatError("codex: bwrap (bubblewrap) is required to run the codex backend in its sandbox, and it was not "
                        "found. Install it (e.g. `apt install bubblewrap`) or use the claude backend. "
                        "Refusing to run codex without the jail.")
    node = _node_root(codex)
    inner = node / "bin" / "codex"
    keep = []
    for name in CODEX_HOME_KEEP:
        f = home / name
        if f.is_file():
            keep += ["--bind", str(f), f"/cx/{name}"]
    cmd = ["bwrap", "--unshare-all", "--share-net", "--die-with-parent", "--clearenv",
           "--ro-bind", "/usr", "/usr",
           "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64", "--symlink", "usr/bin", "/bin",
           "--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf", "--ro-bind", "/etc/ssl", "/etc/ssl",
           "--ro-bind", "/etc/ca-certificates", "/etc/ca-certificates", "--ro-bind", "/etc/hosts", "/etc/hosts",
           "--ro-bind", "/etc/nsswitch.conf", "/etc/nsswitch.conf",
           "--ro-bind", str(node), str(node),
           "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/cx", *keep,
           "--setenv", "PATH", f"{node}/bin:/usr/bin", "--setenv", "HOME", "/tmp", "--setenv", "CODEX_HOME", "/cx",
           "--chdir", "/tmp",
           str(inner) if inner.exists() else "codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check",
           "--sandbox", "read-only", "--ignore-user-config", "--ignore-rules",
           "-c", 'web_search="disabled"', "-c", "tools.web_search=false"]
    for feature in CODEX_DISABLED_FEATURES:
        cmd += ["--disable", feature]
    return cmd


def run_codex(prompt: str) -> str:
    text = ("[SYSTEM]\n" + PROMPT.read_text() +
            "\n\nDo not inspect local files, run commands, or describe your process. Reply with only the JSON object.\n\n" + prompt)
    r = subprocess.run(codex_command(_codex_command()),
                       input=text, capture_output=True, text=True, timeout=TIMEOUT)
    events = []
    for ln in (r.stdout or "").splitlines():
        try:
            events.append(json.loads(ln))
        except ValueError:
            pass
    for e in reversed(events):
        item = e.get("item") or {}
        if e.get("type") == "item.completed" and item.get("type") == "agent_message" and item.get("text"):
            return item["text"]
    raise ChatError("codex: " + ((r.stderr or "").strip()[-300:] or "no text came back"))


RUNNERS = {"claude": run_claude, "codex": run_codex}
RUNNER = None  # tests: a function (backend, prompt) -> reply text


def call_model(backend: str, prompt: str) -> str:
    t0 = time.time()
    try:
        out = RUNNER(backend, prompt) if RUNNER else RUNNERS[backend](prompt)
    except subprocess.TimeoutExpired:
        log_call(backend, time.time() - t0, len(prompt), 0, False)
        raise ChatError(f"{backend} timed out ({TIMEOUT} s)")
    except ChatError:
        log_call(backend, time.time() - t0, len(prompt), 0, False)
        raise
    log_call(backend, time.time() - t0, len(prompt), len(out), True)
    return out


def parse_reply(text: str) -> dict | None:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    for cand in (t, t[t.find("{"):t.rfind("}") + 1] if "{" in t else ""):
        try:
            d = json.loads(cand)
            if isinstance(d, dict) and isinstance(d.get("reply", ""), str):
                d.setdefault("reply", "")
                if not isinstance(d.get("actions"), list):
                    d["actions"] = []
                return d
        except ValueError:
            pass
    return None


def get_reply(backend: str, messages: list[dict]) -> tuple[dict, str]:
    prompt = transcript(messages)
    raw = call_model(backend, prompt)
    d = parse_reply(raw)
    if d is None:
        raw = call_model(backend, prompt + "\n[USER]\n(Your last reply was not JSON. Reply with a single JSON object {\"reply\":…, \"actions\":[…]} and nothing else.)\n\n[ASSISTANT]\n")
        d = parse_reply(raw)
    if d is None:
        raise ChatError("The model's reply was not JSON, and a retry failed too.")
    return d, raw


# ---- actions ----

def run_actions(actions: list) -> tuple[list[str], bool, list[dict]]:
    """Returns (outcome lines, whether any ask ran, display-only judge messages)."""
    lines, asked, shows = [], False, []
    for a in actions:
        if not isinstance(a, dict):
            continue
        do = a.get("do")
        try:
            if do == "ask":
                asked = True
                like = a.get("like") or None
                if isinstance(like, str):
                    like = [like]
                words = a.get("words") or None
                if isinstance(words, str):
                    words = words.split()
                got = sp.ask(str(a.get("question") or ""), a.get("type") or None, words,
                             a.get("key") or None, a.get("bpm") or None, a.get("options") or None,
                             a.get("slot") or "", like=like)
                head = f"[results] {a.get('slot') or ''}: asked {got['asked']}"
                if got.get("too_many") or got.get("note"):
                    head += ", " + str(got.get("note") or "scope too large, narrow it")
                if got.get("blocked_skipped"):
                    head += f" ({got['blocked_skipped']} more were blocked by the user and skipped)"
                lines.append(head)
                shows.append({"role": "judge", "slot": a.get("slot") or "", "question": str(a.get("question") or ""),
                              "asked": got["asked"], "ns": [i["n"] for i in got["items"]],
                              **({"like": got["like"]} if got.get("like") else {})})
                for i in got["items"]:
                    lines.append(f"{i['n']}. [{i['slot']}] {i['score']} {i['name']} — {i['about'][:160]}"
                                 f" (shown {i['shown']} times, saved {i['saved']} times)")
            elif do == "look":
                asked = True
                got = sp.look(str(a.get("folder") or ""))
                lines.append(f"[folder] {got['folder']}: " + ("(no audio files)" if not got["files"] else ""))
                for f in got["files"]:
                    lines.append(f"- {f['file']} — {f['line']}")
                shows.append({"role": "look", "folder": got["folder"], "files": [f["file"] for f in got["files"]]})
            elif do == "save":
                r = sp.save(str(a.get("folder") or ""), [int(x) for x in a.get("items") or []])
                lines.append("[saved] " + (r["error"] if "error" in r else
                             f"{r['folder']}: added {len(r['added'])}, skipped {len(r['skipped'])}"))
            elif do == "note":
                if not isinstance(a.get("text"), str):
                    raise ValueError("note needs text (the complete new notes)")
                changed = sp.set_notes(a["text"], by="agent")
                lines.append("[notes] " + ("updated" if changed else "unchanged"))
                if changed:
                    shows.append({"role": "note", "by": "agent"})
            elif do == "block":
                r = sp.block([int(x) for x in a.get("items") or []], why=str(a.get("why") or ""))
                if "error" in r:
                    lines.append("[block failed] " + r["error"])
                else:
                    names = [b["name"] for b in r["blocked"]]
                    lines.append("[blocked] " + ", ".join(names))
                    shows.append({"role": "block", "names": names, "why": r["why"]})
            elif do == "reveal":
                r = sp.reveal(a.get("item"))
                lines.append("[revealed] " + (r["error"] if "error" in r else f"number {r['revealed']}"))
            else:
                lines.append(f"[unknown action] {do}")
        except Exception as e:
            lines.append(f"[{do} failed] {e}")
    return lines, asked, shows


def chat(text: str, backend: str, selected=()) -> dict:
    talk = load_talk()
    talk["backend"] = backend if backend in RUNNERS else "claude"
    backend = talk["backend"]
    new = []

    def add(m):
        talk["messages"].append(m)
        new.append(m)
    first_user = not any(m["role"] == "user" for m in talk["messages"])
    sel = [int(x) for x in selected if str(x).lstrip("-").isdigit()]
    add({"role": "user", "text": text + (f"[ticked: {', '.join(map(str, sel))}]" if sel else "")})
    if first_user and not talk.get("renamed") and sp.title_from(text):
        talk["title"] = sp.title_from(text)
    try:
        for rnd in range(MAX_EXTRA_ROUNDS + 1):
            d, raw = get_reply(backend, talk["messages"])
            add({"role": "assistant", "text": d["reply"], "raw": raw})
            lines, asked, shows = run_actions(d["actions"])
            for sh in shows:
                add(sh)
            if lines:
                add({"role": "result", "text": "\n".join(lines)})
            if not asked or rnd == MAX_EXTRA_ROUNDS:
                break
    except ChatError as e:
        add({"role": "error", "text": str(e)})
    except Exception as e:
        add({"role": "error", "text": f"Error: {e}"})
    save_talk(talk)
    return {"messages": new, "items": sp.with_counts(sp.list_items()), "folders": sp.folders(), "id": talk["id"], "title": talk["title"],
            **notes_state()}


def notes_state() -> dict:
    return {"notes": sp.get_notes(), "blocked": sp.blocked_list()}


def state() -> dict:
    t = load_talk()
    return {**notes_state(), "id": t["id"], "title": t["title"], "messages": t["messages"], "backend": t["backend"],
            "items": sp.with_counts(sp.list_items()), "folders": sp.folders(), "talks": sp.list_convs()}


# ---- http ----

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            b = config.asset("page.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif u.path == "/api/state":
            self._json(state())
        elif u.path == "/api/notes":
            n = notes_state()
            self._json({"text": n["notes"], "blocked": n["blocked"]})
        elif u.path == "/api/talks":
            self._json(convs_state())
        elif u.path == "/api/audio":
            self.audio(parse_qs(u.query).get("n", [""])[0])
        else:
            self._json({"error": "not found"}, 404)

    def audio(self, n):
        it = sp.get_item(n)
        f = Path(it["file"]) if it else None
        if not f or not f.is_file():
            return self._json({"error": "not in the list"}, 404)
        size = f.stat().st_size
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            code = 206
        mime = mimetypes.guess_type(f.name)[0] or "audio/wav"
        if f.suffix.lower() in (".aif", ".aiff"):
            mime = "audio/aiff"
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with f.open("rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = fh.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        b = self._body()
        p = urlparse(self.path).path
        try:
            if p == "/api/chat":
                with LOCK:
                    self._json(chat(str(b.get("text") or ""), b.get("backend") or "claude", b.get("selected") or []))
            elif p in ("/api/new", "/api/talks/new"):
                with LOCK:
                    sp.new_conv()
                self._json(state())
            elif p == "/api/talks/switch":
                with LOCK:
                    sp.switch_conv(str(b.get("id") or ""))
                self._json(state())
            elif p == "/api/talks/rename":
                with LOCK:
                    sp.rename_conv(str(b.get("id") or ""), str(b.get("title") or ""))
                self._json({**convs_state(), "title": load_talk()["title"]})
            elif p == "/api/talks/delete":
                with LOCK:
                    sp.delete_conv(str(b.get("id") or ""))
                self._json(state())
            elif p == "/api/folders/rename":
                r = sp.rename_folder(str(b.get("old") or ""), str(b.get("new") or ""))
                r["folders"] = sp.folders()
                self._json(r, 400 if "error" in r else 200)
            elif p == "/api/folders/delete":
                r = sp.delete_folder(str(b.get("folder") or ""))
                r["folders"] = sp.folders()
                self._json(r, 400 if "error" in r else 200)
            elif p == "/api/save":
                r = sp.save(str(b.get("folder") or ""), [int(x) for x in b.get("items") or []])
                r["folders"] = sp.folders()
                self._json(r, 400 if "error" in r else 200)
            elif p == "/api/notes":
                if not isinstance(b.get("text"), str):
                    return self._json({"error": "text must be a string"}, 400)
                changed = sp.set_notes(b["text"], by="user")  # no LOCK: saving must not wait for a model call
                self._json({"changed": changed})
            elif p == "/api/block":
                r = sp.block([int(x) for x in b.get("items") or []], why=str(b.get("why") or ""))
                self._json({**r, "blocked": sp.blocked_list()}, 400 if "error" in r else 200)
            elif p == "/api/unblock":
                r = sp.unblock(str(b.get("path") or ""))
                self._json({**r, "blocked": sp.blocked_list()}, 400 if "error" in r else 200)
            elif p == "/api/reveal":
                r = sp.reveal_folder(b["folder"]) if b.get("folder") else sp.reveal(b.get("item"))
                self._json(r, 400 if "error" in r else 200)
            else:
                self._json({"error": "not found"}, 404)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": str(e)}, 500)


def make_server(port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), H)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="samplepick-server")
    ap.add_argument("--port", type=int, default=8772)
    a = ap.parse_args(argv)
    s = make_server(a.port)
    print(f"samplepick: http://127.0.0.1:{a.port}/")
    try:
        s.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
