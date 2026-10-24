from pathlib import Path

import samplepick as sp


def test_wsl_uses_explorer(monkeypatch):
    monkeypatch.setattr(sp, "to_win", lambda p: "C:\\x\\" + Path(p).name)
    assert sp.reveal_command("/mnt/d/x/a.wav", True, platform="linux", wsl=True) == ["explorer.exe", "/select,C:\\x\\a.wav"]
    assert sp.reveal_command("/mnt/d/x", False, platform="linux", wsl=True) == ["explorer.exe", "C:\\x\\x"]


def test_macos_open(tmp_path):
    f = tmp_path / "a.wav"
    assert sp.reveal_command(f, True, platform="darwin", wsl=False) == ["open", "-R", str(f)]
    assert sp.reveal_command(tmp_path, False, platform="darwin", wsl=False) == ["open", str(tmp_path)]


def test_linux_xdg_open_on_the_folder(tmp_path):
    f = tmp_path / "a.wav"
    assert sp.reveal_command(f, True, platform="linux", wsl=False) == ["xdg-open", str(tmp_path)]
    assert sp.reveal_command(tmp_path, False, platform="linux", wsl=False) == ["xdg-open", str(tmp_path)]


def test_windows_native(tmp_path):
    f = tmp_path / "a.wav"
    assert sp.reveal_command(f, True, platform="win32", wsl=False) == ["explorer.exe", "/select," + str(f)]


def test_auto_detects_wsl(monkeypatch):
    monkeypatch.setattr(sp, "is_wsl", lambda: True)
    monkeypatch.setattr(sp.shutil, "which", lambda n: "/mnt/d/Windows/explorer.exe")
    assert sp.reveal_command("/tmp/a.wav", True, platform="linux")[0] == "explorer.exe"
    monkeypatch.setattr(sp.shutil, "which", lambda n: None)  # interop off: fall back to Linux
    assert sp.reveal_command("/tmp/a.wav", True, platform="linux")[0] == "xdg-open"
    monkeypatch.setattr(sp, "is_wsl", lambda: False)
    assert sp.reveal_command("/tmp/a.wav", True, platform="linux")[0] == "xdg-open"


def test_is_wsl_from_env(monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert sp.is_wsl() is True


def test_reveal_runs_the_command(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(sp, "_open", lambda argv: seen.append(argv))
    monkeypatch.setattr(sp, "ROOT", tmp_path / "picks")
    (sp.ROOT / "F").mkdir(parents=True)
    monkeypatch.setattr(sp, "reveal_command", lambda target, select: ["fm", str(target), str(select)])
    assert sp.reveal_folder("F")["folder"] == "F"
    assert seen == [["fm", str(sp.ROOT / "F"), "False"]]
    assert "error" in sp.reveal_folder("Nope")
