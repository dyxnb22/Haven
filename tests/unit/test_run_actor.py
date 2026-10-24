"""单元测试：RunActor 单次运行自治隔离与生命周期。"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from haven.application.approvals import AutoApprover
from haven.application.state import RunContext
from haven.bootstrap import build_services
from haven.contracts.model import (
    ModelEvent,
    ModelRequest,
    StreamFinished,
    TextDelta,
    ToolCallProposal,
)
from haven.domain.budget import Budget
from haven.domain.enums import PermissionMode, RunStatus, StopReason
from haven.domain.ids import new_run_id
from haven.domain.stuck import StuckLoopDetector


class ScriptedModel:
    """产生可预测输出的模型测试桩。"""

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


async def test_actor_isolation_and_scratch_cleanup(tmp_path: Path) -> None:
    services = await build_services(
        tmp_path,
        mode=PermissionMode.READ_ONLY,
        approvals=AutoApprover("reject_all"),
        sinks=[],
        model=ScriptedModel(),
        store_path=tmp_path / "haven.db",
    )
    try:
        ctx1 = RunContext(
            run_id=new_run_id(), goal="Goal 1", mode=PermissionMode.READ_ONLY, budget=Budget()
        )
        ctx2 = RunContext(
            run_id=new_run_id(), goal="Goal 2", mode=PermissionMode.READ_ONLY, budget=Budget()
        )

        actor1 = services.run_service._create_actor(ctx1)  # noqa: SLF001
        actor2 = services.run_service._create_actor(ctx2)  # noqa: SLF001

        # 状态隔离断言
        assert actor1.run_id != actor2.run_id
        assert actor1.steer_queue is not actor2.steer_queue
        assert actor1.scratch_dir != actor2.scratch_dir
        assert actor1.scratch_dir.is_dir()
        assert actor2.scratch_dir.is_dir()

        # actor1 销毁不影响 actor2 的 scratch 目录
        await actor1.close()
        assert not actor1.scratch_dir.exists()
        assert actor2.scratch_dir.exists()

        await actor2.close()
        assert not actor2.scratch_dir.exists()
    finally:
        await services.close()


async def test_actor_steering_queue(tmp_path: Path) -> None:
    services = await build_services(
        tmp_path,
        mode=PermissionMode.READ_ONLY,
        approvals=AutoApprover("reject_all"),
        sinks=[],
        model=ScriptedModel(),
        store_path=tmp_path / "haven.db",
    )
    try:
        ctx = RunContext(
            run_id=new_run_id(), goal="Goal", mode=PermissionMode.READ_ONLY, budget=Budget()
        )
        actor = services.run_service._create_actor(ctx)  # noqa: SLF001

        # 未处于 driving 状态时拒绝 steering
        assert await actor.steer("Instruction 1") is False

        # 模拟进入 driving 状态
        actor._is_driving = True  # noqa: SLF001
        assert await actor.steer("Instruction 1") is True
        assert await actor.steer("Instruction 2") is True
        assert await actor.steer("   ") is False

        with pytest.raises(ValueError, match="at most 4000 characters"):
            await actor.steer("x" * 4001)

        drained = actor.drain_steering()
        assert drained == ["Instruction 1", "Instruction 2"]
        assert len(actor.steer_queue) == 0

        await actor.close()
    finally:
        await services.close()


async def test_actor_drive_success(tmp_path: Path) -> None:
    services = await build_services(
        tmp_path,
        mode=PermissionMode.READ_ONLY,
        approvals=AutoApprover("reject_all"),
        sinks=[],
        model=ScriptedModel("All done cleanly."),
        store_path=tmp_path / "haven.db",
    )
    try:
        ctx = RunContext(
            run_id=new_run_id(),
            goal="Analyze workspace",
            mode=PermissionMode.READ_ONLY,
            budget=Budget(),
        )
        await services.run_service._announce_run(ctx, ctx.goal)  # noqa: SLF001

        actor = services.run_service._create_actor(ctx)  # noqa: SLF001
        scratch = actor.scratch_dir
        assert scratch.is_dir()

        outcome = await actor.drive()
        assert outcome.status is RunStatus.SUCCEEDED
        assert outcome.final_text == "All done cleanly."
        assert not scratch.exists(), "actor dedicated scratch dir must be cleaned up on drive exit"
        assert actor.is_closed
    finally:
        await services.close()


async def test_actor_stuck_detection(tmp_path: Path) -> None:
    services = await build_services(
        tmp_path,
        mode=PermissionMode.READ_ONLY,
        approvals=AutoApprover("reject_all"),
        sinks=[],
        model=ScriptedModel(),
        store_path=tmp_path / "haven.db",
    )
    try:
        ctx = RunContext(
            run_id=new_run_id(), goal="Check repo", mode=PermissionMode.READ_ONLY, budget=Budget()
        )
        ctx.status = RunStatus.RUNNING_MODEL
        await services.run_service._announce_run(ctx, ctx.goal)  # noqa: SLF001
        actor = services.run_service._create_actor(ctx)  # noqa: SLF001

        stuck = StuckLoopDetector(threshold=3)
        calls = (
            ToolCallProposal(
                call_id="call-1",
                tool_name="repo.read",
                arguments_json='{"path": "non-existent-file.txt"}',
            ),
        )

        # 连续三次相同的无效读取
        res1 = await actor.handle_tool_calls(step=1, calls=calls, stuck=stuck)
        assert res1 is None
        res2 = await actor.handle_tool_calls(step=2, calls=calls, stuck=stuck)
        assert res2 is None
        res3 = await actor.handle_tool_calls(step=3, calls=calls, stuck=stuck)

        assert res3 is not None
        assert res3.status is RunStatus.STOPPED
        assert res3.stop_reason is StopReason.NO_PROGRESS

        await actor.close()
    finally:
        await services.close()
