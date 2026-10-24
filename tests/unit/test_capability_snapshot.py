"""单元测试：CapabilitySnapshot 能力快照、摘要一致性与恢复校验（Phase P2）。"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from haven.application.approvals import AutoApprover
from haven.bootstrap import build_services
from haven.contracts.capability import (
    build_capability_snapshot,
    builtin_capabilities,
)
from haven.contracts.model import ModelEvent, ModelRequest, StreamFinished, TextDelta
from haven.contracts.tools import ARGS_MODELS
from haven.domain.enums import PermissionMode, RunStatus


class ScriptedModel:
    """按固定文本流式输出的模型桩。"""

    def __init__(self, text: str = "Success.") -> None:
        self.text = text
        self.closed = False

    @property
    def model_name(self) -> str:
        return "deepseek-chat"

    async def _stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        yield TextDelta(text=self.text)
        yield StreamFinished(finish_reason="stop")

    def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        return self._stream(request)

    async def aclose(self) -> None:
        self.closed = True


class MockMCPTool:
    """模拟外部 MCP 工具。"""

    def __init__(self, name: str, params: dict) -> None:
        self.namespaced_name = name
        self.description = f"Mock description for {name}"
        self.parameters = params
        self.server_name = "test-server"


def test_builtin_capabilities_coverage_and_spec() -> None:
    builtins = builtin_capabilities()
    cap_ids = {c.capability_id for c in builtins}

    # 1. 编译内置的所有工具必须 1:1 映射入能力目录
    assert cap_ids == set(ARGS_MODELS)

    # 2. 每个能力的规范字段与摘要生成有效
    for cap in builtins:
        assert len(cap.schema_digest) == 64
        assert cap.origin == "builtin"
        assert cap.version == "1.0"
        assert cap.effect_class in (
            "workspace_read",
            "workspace_effect",
            "exec",
            "state",
            "external_observation",
            "external_effect",
        )
        assert cap.replay_policy in ("safe", "idempotent_with_key", "never")
        assert cap.approval_policy in ("allow", "ask", "deny")

        # 验证导出为 ToolSchema 行为
        schema = cap.to_tool_schema()
        assert schema.name == cap.capability_id
        assert schema.description == cap.description
        assert schema.parameters == cap.input_schema


def test_capability_snapshot_digest_deterministic() -> None:
    snap1 = build_capability_snapshot()
    snap2 = build_capability_snapshot()

    # 相同内建工具集的摘要必须完全一致
    assert snap1.catalog_digest == snap2.catalog_digest
    assert len(snap1.catalog_digest) == 64

    # 包含外部 MCP 工具时，摘要必须敏感变化
    mock_tool = MockMCPTool(
        "mcp.github.create_issue",
        {"type": "object", "properties": {"title": {"type": "string"}}},
    )
    mcp_snap = build_capability_snapshot(mcp_tools=(mock_tool,))
    assert mcp_snap.catalog_digest != snap1.catalog_digest
    assert mcp_snap.has_capability("mcp.github.create_issue")
    assert not snap1.has_capability("mcp.github.create_issue")

    # MCP 工具 schema 微小变化也必须引起摘要变动
    mock_tool_modified = MockMCPTool(
        "mcp.github.create_issue",
        {"type": "object", "properties": {"title": {"type": "integer"}}},
    )
    mcp_snap_mod = build_capability_snapshot(mcp_tools=(mock_tool_modified,))
    assert mcp_snap_mod.catalog_digest != mcp_snap.catalog_digest


async def test_run_created_and_checkpoint_record_catalog_digest(tmp_path: Path) -> None:
    services = await build_services(
        tmp_path,
        mode=PermissionMode.READ_ONLY,
        approvals=AutoApprover("reject_all"),
        sinks=[],
        model=ScriptedModel("Digest check ok."),
        store_path=tmp_path / "haven.db",
    )
    try:
        expected_digest = build_capability_snapshot().catalog_digest

        outcome = await services.run_service.run("Verify catalog digest")
        assert outcome.status is RunStatus.SUCCEEDED

        # 检查持久化的 Checkpoint
        checkpoint = await services.store.load_checkpoint(outcome.run_id)
        assert checkpoint is not None
        assert checkpoint.catalog_digest == expected_digest

        # 检查记录的 RunCreated 事件
        events = await services.store.load_events(outcome.run_id)
        created_events = [e.event for e in events if e.event.kind == "run.created"]
        assert len(created_events) == 1
        assert getattr(created_events[0], "catalog_digest", "") == expected_digest
    finally:
        await services.close()


async def test_continue_and_resume_reject_drifted_catalog_digest(tmp_path: Path) -> None:
    services = await build_services(
        tmp_path,
        mode=PermissionMode.READ_ONLY,
        approvals=AutoApprover("reject_all"),
        sinks=[],
        model=ScriptedModel(),
        store_path=tmp_path / "haven.db",
    )
    try:
        outcome = await services.run_service.run("Initial run")
        assert outcome.status is RunStatus.SUCCEEDED

        # 读取检查点并篡改其 catalog_digest 模拟能力环境漂移
        checkpoint = await services.store.load_checkpoint(outcome.run_id)
        assert checkpoint is not None

        drifted_checkpoint = checkpoint.model_copy(update={"catalog_digest": "0" * 64})
        await services.store.save_checkpoint(drifted_checkpoint)

        # 1. continue_run 应当检测到漂移并抛出拒绝异常
        with pytest.raises(ValueError, match="capability catalog changed since checkpoint"):
            await services.run_service.continue_run(
                outcome.run_id, "Attempt continue with drifted catalog"
            )

        # 2. resume 流程也应当检测到漂移并拒绝恢复
        from haven.application.run_service import build_run_context_from_checkpoint

        ctx = build_run_context_from_checkpoint(drifted_checkpoint)
        with pytest.raises(ValueError, match="capability catalog changed since checkpoint"):
            await services.run_service.resume(ctx)
    finally:
        await services.close()
