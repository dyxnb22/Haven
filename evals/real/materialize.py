"""Turning a pinned clone into a task checkout.

Shared by the two eval harnesses — `build.py` (the live real-repo suite) and
`headtohead/harness.py` (the tool-vs-tool comparison) — because they must
produce *equivalent* trees for their numbers to mean the same thing. When
each kept its own copy, the conftest shim was written verbatim in three
places and the injection check twice; a silent drift in one of them would
have invalidated a head-to-head result with no test failing.

Nothing here decides anything: it copies, injects, and writes the import
shim. What to inject and how to grade stays with each caller.
"""

from __future__ import annotations

import shutil
from pathlib import Path

#: `.venv`: uv creates one inside a clone if `uv run` is ever invoked there
#: (the larger tier-3 repos ship their own pyproject/uv.lock); copying it into
#: every fixture would be huge and meaningless.
IGNORE = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache", ".tox", ".venv")


def apply_once(text: str, old: str, new: str, where: str) -> str:
    """Replace `old`, requiring exactly one occurrence.

    An injection that matches twice (or zero times) silently produces a
    different task than the one authored, so it fails the build instead.
    """
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{where}: snippet must appear exactly once, found {count}:\n  {old!r}")
    return text.replace(old, new)


def copy_clone(source: Path, dest: Path) -> None:
    """Fresh copy of a pinned clone at `dest`, replacing whatever was there."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest, ignore=IGNORE)


def write_src_layout_shim(dest: Path, src_path: str) -> None:
    """Put a `src/`-layout package on `sys.path` for the checkout.

    src-layout projects are not installed in a fixture, so `python -m pytest`
    cannot import them without this. Zero-config fixtures deliberately skip
    it: the shim is pre-configuration, and a discovered command has to solve
    the import path on its own.
    """
    (dest / "conftest.py").write_text(
        "import sys, pathlib\n"
        f"sys.path.insert(0, str(pathlib.Path(__file__).parent / {src_path!r}))\n",
        encoding="utf-8",
    )
