import wave

import pytest


def make_wav(path, seconds=1.0, rate=8000):
    """A tiny silent mono 16-bit WAV of the given length."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No test may see a real key, library or data dir from the machine it runs on."""
    for k in ("SAMPLEPICK_JUDGE_KEY", "OPENROUTER_API_KEY", "SAMPLEPICK_JUDGE_URL", "SAMPLEPICK_JUDGE_MODEL",
              "SAMPLEPICK_JUDGE_TIMEOUT", "SAMPLEPICK_LIBRARY", "SAMPLEPICK_DATA", "SAMPLEPICK_ROOT",
              "SAMPLEPICK_TIMBRE_PYTHON", "SAMPLEPICK_PHONON_URL", "SAMPLEPICK_CLAUDE_CONFIG_DIR",
              "SAMPLEPICK_CODEX_HOME"):
        monkeypatch.delenv(k, raising=False)
