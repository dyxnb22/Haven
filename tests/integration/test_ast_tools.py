"""集成测试：repo.outline 与 repo.symbol 运行时执行与安全边界（ADR 0035）。"""

from __future__ import annotations

import json
from pathlib import Path

from haven.contracts.model import ToolCallProposal, ToolCallReady
from haven.domain.enums import RunStatus
from tests.integration.harness import Harness, finish, make_repo, text, tool


def symbol_tool(call_id: str, symbol_name: str, path: str | None = None) -> ToolCallReady:
    payload: dict[str, object] = {"name": symbol_name}
    if path is not None:
        payload["path"] = path
    return ToolCallReady(
        call=ToolCallProposal(
            call_id=call_id,
            tool_name="repo.symbol",
            arguments_json=json.dumps(payload),
        )
    )


async def test_repo_outline_returns_ast_structure(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "src" / "service.py").write_text(
        "class Calculator:\n"
        "    def add(self, a: int, b: int) -> int:\n"
        "        return a + b\n\n"
        "def main():\n"
        "    pass\n",
        encoding="utf-8",
    )
    h = Harness(
        repo,
        [
            [tool("c1", "repo.outline", path="src/service.py"), finish("tool_calls")],
            [text("done"), finish()],
        ],
    )
    outcome = await h.service.run("inspect service.py outline")
    assert outcome.status is RunStatus.SUCCEEDED
    messages = h.model.requests_seen[-1].messages
    tool_msg = next(m for m in messages if m.role == "tool")
    assert "Calculator" in tool_msg.content
    assert "add" in tool_msg.content
    assert "main" in tool_msg.content


async def test_repo_outline_nonexistent_path(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    h = Harness(
        repo,
        [
            [tool("c1", "repo.outline", path="nonexistent.py"), finish("tool_calls")],
            [text("done"), finish()],
        ],
    )
    outcome = await h.service.run("outline nonexistent")
    assert outcome.status is RunStatus.SUCCEEDED
    messages = h.model.requests_seen[-1].messages
    tool_msg = next(m for m in messages if m.role == "tool")
    assert "path not found" in tool_msg.content


async def test_repo_symbol_locates_definitions_and_references(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "src" / "helper.py").write_text(
        "def format_output(val: str) -> str:\n    return val.strip()\n",
        encoding="utf-8",
    )
    (repo / "src" / "main.py").write_text(
        "from helper import format_output\n\nprint(format_output(' hello '))\n",
        encoding="utf-8",
    )
    h = Harness(
        repo,
        [
            [symbol_tool("c1", "format_output"), finish("tool_calls")],
            [text("done"), finish()],
        ],
    )
    outcome = await h.service.run("find format_output symbol")
    assert outcome.status is RunStatus.SUCCEEDED
    messages = h.model.requests_seen[-1].messages
    tool_msg = next(m for m in messages if m.role == "tool")
    assert "format_output" in tool_msg.content
    assert "definition" in tool_msg.content
    assert "reference" in tool_msg.content


async def test_repo_outline_outside_workspace_denied(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    h = Harness(
        repo,
        [
            [tool("c1", "repo.outline", path="../../etc/passwd"), finish("tool_calls")],
            [text("done"), finish()],
        ],
    )
    outcome = await h.service.run("outline outside")
    assert outcome.status is RunStatus.SUCCEEDED
    messages = h.model.requests_seen[-1].messages
    tool_msg = next(m for m in messages if m.role == "tool")
    assert "denied" in tool_msg.content or "outside_workspace" in tool_msg.content
