"""The generated metrics table must never publish a stale coverage figure.

`coverage report` reads whatever `.coverage` already holds; it never re-runs the
suite. Refreshing the table after editing source — but before re-running
coverage — therefore reports the new lines as uncovered and writes a wrong
number into a table CI then enforces. Found exactly that way: an 88% figure
became "84%" purely from stale data.
"""

import os
from pathlib import Path

from scripts.refresh_metrics import _coverage_data_is_stale


def _touch(path: Path, mtime: int) -> Path:
    path.write_text("x", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


class TestCoverageFreshness:
    def test_data_older_than_an_input_is_stale(self, tmp_path: Path) -> None:
        data = _touch(tmp_path / ".coverage", 1_000)
        source = _touch(tmp_path / "a.py", 2_000)
        assert _coverage_data_is_stale(data, [source]) is True

    def test_data_newer_than_every_input_is_fresh(self, tmp_path: Path) -> None:
        source = _touch(tmp_path / "a.py", 1_000)
        data = _touch(tmp_path / ".coverage", 2_000)
        assert _coverage_data_is_stale(data, [source]) is False

    def test_a_missing_data_file_counts_as_stale(self, tmp_path: Path) -> None:
        source = _touch(tmp_path / "a.py", 1_000)
        assert _coverage_data_is_stale(tmp_path / ".coverage", [source]) is True

    def test_the_newest_input_decides(self, tmp_path: Path) -> None:
        old = _touch(tmp_path / "old.py", 1_000)
        data = _touch(tmp_path / ".coverage", 2_000)
        new = _touch(tmp_path / "new.py", 3_000)
        assert _coverage_data_is_stale(data, [old, new]) is True
