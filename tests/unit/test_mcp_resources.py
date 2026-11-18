"""单元测试：MCP 外部资源显式导入与上下文管理（ADR / 规划书 Phase P4）。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from haven.application.context_builder import ContextBuilder
from haven.application.mcp_client import MCPError
from haven.application.mcp_resources import (
    fetch_imported_resource,
    load_imported_resources,
    remove_imported_resource,
    save_imported_resource,
)
from haven.contracts.mcp import ImportedResource, MCPServerConfig
from haven.domain.budget import Budget, BudgetUsage
from haven.interfaces.cli import app

runner = CliRunner()


def test_imported_resource_creation_and_truncation() -> None:
    res = ImportedResource.create(
        server_name="docs_server",
        uri="docs://api/reference",
        text="Normal content.",
        mime_type="text/markdown",
    )
    assert res.server_name == "docs_server"
    assert res.uri == "docs://api/reference"
    assert res.mime_type == "text/markdown"
    assert res.text == "Normal content."
    assert not res.truncated
    assert len(res.content_digest) == 64

    # 超过 max_bytes 截断测试
    large_text = "A" * 100
    res_trunc = ImportedResource.create(
        server_name="docs_server",
        uri="docs://big",
        text=large_text,
        max_bytes=20,
    )
    assert res_trunc.truncated
    assert len(res_trunc.text.encode("utf-8")) <= 20


def test_resource_persistence_lifecycle(tmp_path: Path) -> None:
    # 1. 初始为空
    assert load_imported_resources(tmp_path) == ()

    # 2. 保存资源
    res1 = ImportedResource.create(
        server_name="issues",
        uri="github://issues/42",
        text="Issue #42 description",
    )
    save_imported_resource(tmp_path, res1)
    loaded = load_imported_resources(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].uri == "github://issues/42"

    # 3. 更新同一资源
    res1_updated = ImportedResource.create(
        server_name="issues",
        uri="github://issues/42",
        text="Issue #42 updated description",
    )
    save_imported_resource(tmp_path, res1_updated)
    loaded2 = load_imported_resources(tmp_path)
    assert len(loaded2) == 1
    assert loaded2[0].text == "Issue #42 updated description"

    # 4. 追加不同资源
    res2 = ImportedResource.create(
        server_name="issues",
        uri="github://issues/99",
        text="Issue #99 description",
    )
    save_imported_resource(tmp_path, res2)
    assert len(load_imported_resources(tmp_path)) == 2

    # 5. 删除资源
    assert remove_imported_resource(tmp_path, "issues", "github://issues/42") is True
    assert remove_imported_resource(tmp_path, "issues", "github://issues/42") is False
    remaining = load_imported_resources(tmp_path)
    assert len(remaining) == 1
    assert remaining[0].uri == "github://issues/99"


def test_context_builder_imported_resources_rendering() -> None:
    res = ImportedResource.create(
        server_name="wiki",
        uri="wiki://architecture",
        text="Architecture details here.",
    )
    builder = ContextBuilder(
        goal="Refactor system",
        tools=(),
        budget=Budget(max_steps=5, max_tool_calls=5),
        recipe_ids=(),
        imported_resources=(res,),
    )
    request, segments = builder.build([], BudgetUsage())

    # 查找来自 imported_resource 的片段
    found = [s for s in segments if s.source.startswith("imported_resource:wiki")]
    assert len(found) == 1
    seg = found[0]
    assert seg.trust == "untrusted"
    assert "user-imported MCP resource" in seg.reason
    assert any("Architecture details here." in m.content for m in request.messages)


async def test_fetch_imported_resource_flow() -> None:
    cfg = MCPServerConfig(name="docs", command="true")

    with patch("haven.application.mcp_resources.MCPClient") as mock_cls:
        client = mock_cls.return_value
        client.start = AsyncMock()
        client.close = AsyncMock()
        client.read_resource = AsyncMock(
            return_value={"contents": [{"text": "Doc text", "mimeType": "text/markdown"}]}
        )

        res = await fetch_imported_resource(cfg, "docs://v1/guide")
        assert res.server_name == "docs"
        assert res.uri == "docs://v1/guide"
        assert res.text == "Doc text"
        assert res.mime_type == "text/markdown"
        client.start.assert_awaited_once()
        client.close.assert_awaited_once()

    # 握手失败也必须 close：start() 自己 spawn 子进程，把它放在 try 之外会让
    # 服务端立刻退出或 initialize 超时的那一类失败带着孤儿进程逃出去。
    with patch("haven.application.mcp_resources.MCPClient") as mock_cls:
        client = mock_cls.return_value
        client.start = AsyncMock(side_effect=MCPError("handshake failed"))
        client.close = AsyncMock()
        with pytest.raises(MCPError, match="handshake failed"):
            await fetch_imported_resource(cfg, "docs://v1/guide")
        client.close.assert_awaited_once()

    # 测试空内容异常
    with patch("haven.application.mcp_resources.MCPClient") as mock_cls:
        client = mock_cls.return_value
        client.start = AsyncMock()
        client.close = AsyncMock()
        client.read_resource = AsyncMock(return_value={"contents": []})

        with pytest.raises(MCPError):
            await fetch_imported_resource(cfg, "docs://empty")


def test_cli_context_commands(tmp_path: Path) -> None:
    # 1. list 空
    res = runner.invoke(app, ["context", "list", "--workspace", str(tmp_path)])
    assert res.exit_code == 0
    assert "no resources imported" in res.stdout

    # 2. add 缺少参数
    res_err = runner.invoke(app, ["context", "add", "--workspace", str(tmp_path)])
    assert res_err.exit_code != 0

    # 3. add 服务不存在
    res_srv_err = runner.invoke(
        app, ["context", "add", "unknown_srv", "res://1", "--workspace", str(tmp_path)]
    )
    assert res_srv_err.exit_code != 0
    assert "not configured" in res_srv_err.stdout

    # 4. add 成功场景测试
    from unittest.mock import MagicMock

    with (
        patch("haven.interfaces.cli_support.context_commands.load_config") as mock_cfg,
        patch(
            "haven.interfaces.cli_support.context_commands.fetch_imported_resource"
        ) as mock_fetch,
    ):
        mock_cfg.return_value = MagicMock(
            mcp_servers=[MCPServerConfig(name="configured_srv", command="echo")]
        )
        mock_fetch.return_value = ImportedResource.create(
            server_name="configured_srv",
            uri="test://doc",
            text="Fetched doc content",
        )
        res_add_ok = runner.invoke(
            app, ["context", "add", "configured_srv", "test://doc", "--workspace", str(tmp_path)]
        )
        assert res_add_ok.exit_code == 0
        assert "Successfully imported" in res_add_ok.stdout

    # 5. 手动保存一个资源后执行 list 与 remove
    sample = ImportedResource.create(
        server_name="test_srv",
        uri="test://res/1",
        text="Sample data text",
    )
    save_imported_resource(tmp_path, sample)

    res_list = runner.invoke(app, ["context", "list", "--workspace", str(tmp_path)])
    assert res_list.exit_code == 0
    assert "test_srv :: test://res/1" in res_list.stdout

    res_rm = runner.invoke(
        app, ["context", "remove", "test_srv", "test://res/1", "--workspace", str(tmp_path)]
    )
    assert res_rm.exit_code == 0
    assert "Removed resource" in res_rm.stdout

    # 5. 未知 action
    res_bad = runner.invoke(app, ["context", "invalid_action", "--workspace", str(tmp_path)])
    assert res_bad.exit_code != 0
