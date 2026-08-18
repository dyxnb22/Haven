"""失败的工具绝不能将异常抛入代理循环。

实时运行中发现：搜索不存在的路径会使 ripgrep 以 2 退出，该异常曾向上传播并中止
整个评估套件，而不是变成模型可以恢复的结构化 `not_found` 结果。
"""

from pathlib import Path

import pytest

from haven.adapters.workspace_fs import FsWorkspace
from haven.contracts.events import ToolCompleted
from haven.domain.enums import RunStatus
from haven.ports.workspace import WorkspaceError
from tests.integration.harness import Harness, finish, make_repo, text, tool


class TestSearchMissingPath:
    @pytest.mark.parametrize("use_ripgrep", [True, False])
    async def test_missing_path_is_not_found(self, tmp_path: Path, use_ripgrep: bool) -> None:
        workspace = FsWorkspace(make_repo(tmp_path), use_ripgrep=use_ripgrep)
        with pytest.raises(WorkspaceError) as exc:
            await workspace.search("anything", "does/not/exist", 10)
        assert exc.value.code == "not_found"

    async def test_run_recovers_instead_of_crashing(self, tmp_path: Path) -> None:
        turns = [
            [tool("c1", "repo.search", pattern="add", path="nope.py"), finish("tool_calls")],
            [tool("c2", "repo.search", pattern="add", path="src"), finish("tool_calls")],
            [text("Recovered and found it in src/calc.py."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("Find add()")

        assert outcome.status is RunStatus.SUCCEEDED
        completed = [e for e in h.sink.events_of("tool.completed") if isinstance(e, ToolCompleted)]
        assert completed[0].error_code == "not_found"
        assert completed[1].status == "ok"


class TestExecutionErrorsAreStructured:
    async def test_read_of_a_directory_is_structured(self, tmp_path: Path) -> None:
        turns = [
            [tool("c1", "repo.read", path="src"), finish("tool_calls")],
            [text("That is a directory."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("Read the src directory")
        assert outcome.status is RunStatus.SUCCEEDED
        completed = h.sink.events_of("tool.completed")
        assert isinstance(completed[0], ToolCompleted)
        assert completed[0].error_code == "not_found"

    async def test_list_of_a_file_is_structured(self, tmp_path: Path) -> None:
        turns = [
            [tool("c1", "repo.list", path="src/calc.py"), finish("tool_calls")],
            [text("That is a file, not a directory."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("List the calc file")
        assert outcome.status is RunStatus.SUCCEEDED
        completed = h.sink.events_of("tool.completed")
        assert isinstance(completed[0], ToolCompleted)
        assert completed[0].error_code == "not_found"

    async def test_binary_read_is_structured(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        (repo / "blob.bin").write_bytes(b"\x00\x01\x02\xff")
        turns = [
            [tool("c1", "repo.read", path="blob.bin"), finish("tool_calls")],
            [text("Binary file."), finish()],
        ]
        h = Harness(repo, turns)
        outcome = await h.service.run("Read the blob")
        assert outcome.status is RunStatus.SUCCEEDED
        completed = h.sink.events_of("tool.completed")
        assert isinstance(completed[0], ToolCompleted)
        assert completed[0].error_code == "invalid_arguments"
