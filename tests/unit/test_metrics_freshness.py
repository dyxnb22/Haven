"""生成的指标表绝不能发布过时的覆盖率数字。

`coverage report` 读取 `.coverage` 已有的内容，不会重新运行套件。因此在编辑源码后、
重新运行覆盖率前刷新表格，会将新增行报告为未覆盖，并将错误数字写入 CI 随后强制
执行的表格。曾经正是这样发现问题：88% 的数字仅因数据过时变成了“84%”。
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
