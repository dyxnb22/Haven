"""覆盖率下限门禁必须找到它该读的那份数据。

`coverage run` 本身用 `COVERAGE_FILE` 决定数据落点，而 `refresh_metrics.py` 的
提示也正是这样建议的。本门禁曾经硬编码读 `.coverage`：照提示把数据写到自定义路径
之后，它会报一句莫名其妙的 "no .coverage data"——一条无法执行的建议。修好之后，
这里把两条路径都钉住。
"""

import os
from pathlib import Path

from scripts.check_coverage_floor import _coverage_data_file


class TestCoverageDataResolution:
    def test_the_default_is_the_repositories_own_data_file(self) -> None:
        assert _coverage_data_file().name == ".coverage"

    def test_coverage_file_overrides_the_default(self, tmp_path: Path, monkeypatch) -> None:
        custom = tmp_path / "custom.data"
        monkeypatch.setenv("COVERAGE_FILE", str(custom))
        assert _coverage_data_file() == custom

    def test_an_empty_variable_falls_back_to_the_default(self, monkeypatch) -> None:
        """空串不是路径：`coverage` 自己也会忽略它，这里必须一致。"""
        monkeypatch.setenv("COVERAGE_FILE", "")
        assert _coverage_data_file().name == ".coverage"

    def test_the_variable_is_not_read_from_a_parent_process(self, monkeypatch) -> None:
        """换掉之后立刻生效，不缓存。"""
        monkeypatch.delenv("COVERAGE_FILE", raising=False)
        assert _coverage_data_file().name == ".coverage"
        monkeypatch.setenv("COVERAGE_FILE", "/tmp/somewhere-else")
        assert _coverage_data_file() == Path("/tmp/somewhere-else")
        assert os.environ["COVERAGE_FILE"] == "/tmp/somewhere-else"
