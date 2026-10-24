"""单元测试：HavenRuntime 应用门面与会话协调。"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from haven.application.approvals import ApprovalDecision, AutoApprover
from haven.application.runtime import HavenRuntime
from haven.bootstrap import build_services
from haven.contracts.model import ModelEvent, ModelRequest, StreamFinished, TextDelta
from haven.domain.enums import PermissionMode, RunStatus


class ScriptedModel:
    """按预定文本事件流式输出的模型桩。"""

    def __init__(self, text: str = "Task completed.") -> None:
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


async def test_runtime_properties_and_from_services(tmp_path: Path) -> None:
    model = ScriptedModel()
    services = await build_services(
        tmp_path,
        mode=PermissionMode.READ_ONLY,
        approvals=AutoApprover("reject_all"),
        sinks=[],
        model=model,
        store_path=tmp_path / "haven.db",
    )
    try:
        runtime = services.runtime
        assert isinstance(runtime, HavenRuntime)
        assert runtime.run_service is services.run_service
        assert runtime.store is services.store
        assert runtime.emitter is services.emitter
        assert runtime.recovery is services.recovery
        assert runtime.active_run_id is None

        # 测试类方法构建
        runtime2 = HavenRuntime.from_services(services)
        assert runtime2.run_service is services.run_service
    finally:
        await services.close()


async def test_runtime_start_run_and_query(tmp_path: Path) -> None:
    model = ScriptedModel("Inspection finished.")
    services = await build_services(
        tmp_path,
        mode=PermissionMode.READ_ONLY,
        approvals=AutoApprover("reject_all"),
        sinks=[],
        model=model,
        store_path=tmp_path / "haven.db",
    )
    try:
        runtime = services.runtime
        outcome = await runtime.start_run("Read status")
        assert outcome.status is RunStatus.SUCCEEDED
        assert outcome.final_text == "Inspection finished."
        assert runtime.active_run_id is None

        # 查询检查点与运行状态
        checkpoint = await runtime.get_checkpoint(outcome.run_id)
        assert checkpoint is not None
        assert checkpoint.run_id == outcome.run_id

        run_rec = await runtime.get_run(outcome.run_id)
        assert run_rec is not None
        assert run_rec.run_id == outcome.run_id
    finally:
        await services.close()


async def test_runtime_continue_and_resume_run(tmp_path: Path) -> None:
    model = ScriptedModel("First step done.")
    services = await build_services(
        tmp_path,
        mode=PermissionMode.READ_ONLY,
        approvals=AutoApprover("reject_all"),
        sinks=[],
        model=model,
        store_path=tmp_path / "haven.db",
    )
    try:
        runtime = services.runtime
        first = await runtime.start_run("Do step one")
        assert first.status is RunStatus.SUCCEEDED

        # 针对同一检查点继续
        model.text = "Follow-up step done."
        second = await runtime.continue_run(first.run_id, "Do step two")
        assert second.status is RunStatus.SUCCEEDED
        assert second.run_id != first.run_id

        # 针对检查点 resume
        resumed = await runtime.resume_run(first.run_id)
        assert resumed.status is RunStatus.SUCCEEDED

        # 针对不存在的 run_id resume 应当抛出 ValueError
        with pytest.raises(ValueError, match="no checkpoint for run"):
            await runtime.resume_run("non-existent-run-id")
    finally:
        await services.close()


async def test_runtime_steer_and_cancel_when_idle(tmp_path: Path) -> None:
    model = ScriptedModel()
    services = await build_services(
        tmp_path,
        mode=PermissionMode.READ_ONLY,
        approvals=AutoApprover("reject_all"),
        sinks=[],
        model=model,
        store_path=tmp_path / "haven.db",
    )
    try:
        runtime = services.runtime
        # 空闲时转向应返回 False
        steered = await runtime.steer("Please check readme")
        assert steered is False

        # 取消不存在或未运行的任务返回 False
        cancelled = await runtime.cancel("non-existent-run")
        assert cancelled is False
    finally:
        await services.close()


async def test_runtime_approve(tmp_path: Path) -> None:
    model = ScriptedModel()
    services = await build_services(
        tmp_path,
        mode=PermissionMode.READ_ONLY,
        approvals=AutoApprover("reject_all"),
        sinks=[],
        model=model,
        store_path=tmp_path / "haven.db",
    )
    try:
        runtime = services.runtime
        decision = ApprovalDecision.APPROVED
        # 响应未挂起的审批请求返回 False
        delivered = await runtime.approve("non-existent-approval", decision)
        assert delivered is False
    finally:
        await services.close()
