import json

import pytest

import samplepick as sp
import server


def fake_node(tmp_path):
    node = tmp_path / "node"
    (node / "bin").mkdir(parents=True)
    (node / "bin" / "node").write_text("")
    (node / "bin" / "codex").write_text("")
    codex = node / "bin" / "codex"
    return node, codex


def test_codex_refuses_without_bwrap(tmp_path, monkeypatch):
    _, codex = fake_node(tmp_path)
    monkeypatch.setattr(server.shutil, "which", lambda n: str(codex) if n == "codex" else None)
    ran = []
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: ran.append(a))
    with pytest.raises(server.ChatError, match="bwrap"):
        server.run_codex("hello")
    assert ran == []  # nothing was run unjailed


def test_codex_command_is_jailed(tmp_path, monkeypatch):
    node, codex = fake_node(tmp_path)
    home = tmp_path / "cx"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    (home / "config.toml").write_text("secret = 1")
    (home / "AGENTS.md").write_text("private")
    monkeypatch.setattr(server.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setenv("SAMPLEPICK_CODEX_HOME", str(home))
    cmd = server.codex_command(str(codex))
    assert cmd[0] == "bwrap" and "--unshare-all" in cmd and "--clearenv" in cmd and "--die-with-parent" in cmd
    assert ["--bind", str(home / "auth.json"), "/cx/auth.json"] == cmd[cmd.index("--bind"):cmd.index("--bind") + 3]
    joined = " ".join(cmd)
    assert "config.toml" not in joined and "AGENTS.md" not in joined  # only the login files are shown
    assert "--sandbox" in cmd and cmd[cmd.index("--sandbox") + 1] == "read-only"
    for feature in ("shell_tool", "hooks", "plugins"):
        assert cmd[cmd.index(feature) - 1] == "--disable"
    assert cmd[cmd.index("CODEX_HOME") + 1] == "/cx"


def test_codex_run_reads_agent_message(tmp_path, monkeypatch):
    node, codex = fake_node(tmp_path)
    monkeypatch.setattr(server.shutil, "which", lambda n: str(codex) if n == "codex" else "/usr/bin/bwrap")

    class R:
        stdout = "\n".join(json.dumps(e) for e in (
            {"type": "thread.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": '{"reply":"hi"}'}}))
        stderr = ""
    seen = {}
    monkeypatch.setattr(server.subprocess, "run", lambda argv, **k: seen.update(argv=argv, **k) or R)
    assert server.run_codex("the prompt") == '{"reply":"hi"}'
    assert seen["argv"][0] == "bwrap" and "the prompt" in seen["input"] and "[SYSTEM]" in seen["input"]


def test_codex_missing_command(monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda n: None)
    with pytest.raises(server.ChatError, match="codex"):
        server.run_codex("x")


def test_claude_command_and_env(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENT_CWD", tmp_path / "agent")
    monkeypatch.setattr(server.shutil, "which", lambda n: "/usr/bin/claude")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/parent/session")
    seen = {}

    class R:
        returncode = 0
        stdout = json.dumps({"result": "answer"})
        stderr = ""
    monkeypatch.setattr(server.subprocess, "run", lambda argv, **k: seen.update(argv=argv, **k) or R)
    assert server.run_claude("prompt") == "answer"
    a = seen["argv"]
    assert a[0] == "claude" and a[a.index("--tools") + 1] == "" and "--no-session-persistence" in a
    assert "CLAUDE_CONFIG_DIR" not in seen["env"] and "CLAUDECODE" not in seen["env"]
    assert not any(k.startswith("CLAUDE_CODE_") for k in seen["env"])
    assert seen["cwd"] == tmp_path / "agent"
    monkeypatch.setenv("SAMPLEPICK_CLAUDE_CONFIG_DIR", "/my/claude")
    server.run_claude("prompt")
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == "/my/claude"


def test_claude_errors_are_chat_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AGENT_CWD", tmp_path / "agent")
    monkeypatch.setattr(server.shutil, "which", lambda n: None)
    with pytest.raises(server.ChatError, match="not found"):
        server.run_claude("x")
    monkeypatch.setattr(server.shutil, "which", lambda n: "/usr/bin/claude")

    class R:
        returncode = 1
        stdout = "not json"
        stderr = "boom"
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: R)
    with pytest.raises(server.ChatError, match="claude"):
        server.run_claude("x")


def test_only_two_backends_and_unknown_falls_back(tmp_path, monkeypatch):
    assert set(server.RUNNERS) == {"claude", "codex"}
    monkeypatch.setattr(sp, "DATA", tmp_path / "data")
    monkeypatch.setattr(sp, "ROOT", tmp_path / "picks")
    monkeypatch.setattr(server, "RUNNER", lambda b, p: '{"reply":"ok","actions":[]}')
    server.chat("hi", "codex-work")
    assert server.load_talk()["backend"] == "claude"


def test_timeout_is_a_chat_error_and_logged(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "DATA", tmp_path / "data")
    import subprocess

    def slow(b, p):
        raise subprocess.TimeoutExpired("claude", 1)
    monkeypatch.setattr(server, "RUNNER", slow)
    with pytest.raises(server.ChatError, match="timed out"):
        server.call_model("claude", "p")
    assert json.loads((sp.DATA / "calls.jsonl").read_text())["ok"] is False


def test_analyze_command_needs_setup(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sp, "DATA", tmp_path / "data")
    assert sp.main(["analyze"]) == 1
    assert "not set up" in capsys.readouterr().out


def test_ask_without_index_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sp, "DATA", tmp_path / "data")
    monkeypatch.setattr(sp, "ROOT", tmp_path / "picks")
    assert sp.main(["ask", "Is #{n} warm?"]) == 1
    assert "no index yet" in capsys.readouterr().out


def test_prompt_and_page_are_found():
    assert server.PROMPT.is_file() and "reply" in server.PROMPT.read_text()
    assert "Sample Pick" in server.config.asset("page.html").read_text()
