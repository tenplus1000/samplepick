"""Where things live: the data dir, the save-folders root, the sample libraries, the bundled files.

Everything is read from the environment on each call (nothing is fixed at import), so a changed
variable takes effect on the next call and tests can point the tool at a temp folder.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def data_dir() -> Path:
    """index.jsonl, analysis.jsonl, judge.jsonl, talks/, notes.md, stats.json ... (gitignored)."""
    return Path(os.environ["SAMPLEPICK_DATA"]).expanduser() if os.environ.get("SAMPLEPICK_DATA") else HERE / "data"


def root_dir() -> Path:
    """Where the save folders (the keepers, copied) go."""
    return Path(os.environ.get("SAMPLEPICK_ROOT") or Path.home() / "Music" / "Sample Picks").expanduser()


def libraries(extra=()) -> list[Path]:
    """The sample folders to index: --library values first, else SAMPLEPICK_LIBRARY (os.pathsep-separated).
    Each value may itself hold several folders separated by os.pathsep."""
    raw = [v for e in extra or () for v in str(e).split(os.pathsep)]
    if not raw:
        raw = os.environ.get("SAMPLEPICK_LIBRARY", "").split(os.pathsep)
    out = []
    for v in raw:
        v = v.strip()
        if v:
            p = Path(os.path.abspath(Path(v).expanduser()))
            if p not in out:
                out.append(p)
    return out


def asset(name: str) -> Path:
    """A bundled file (page.html, prompt.md): next to the code, else where `pip install` put it."""
    for d in (HERE, Path(sys.prefix) / "share" / "samplepick"):
        if (d / name).is_file():
            return d / name
    return HERE / name
