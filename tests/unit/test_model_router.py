"""双轨混合模型路由器单元测试（ADR 0042）。"""

from collections.abc import AsyncIterator

from haven.application.model_router import ModelRouter
from haven.application.state import RunContext
from haven.contracts.model import ModelEvent, ModelRequest, StreamFinished
from haven.contracts.tools import PlanStep
from haven.domain.budget import Budget
from haven.domain.enums import PermissionMode
from haven.domain.evidence import EditEvidence
from haven.domain.ids import RunId
from haven.ports.model import ModelPort


class FakeModel(ModelPort):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def model_name(self) -> str:
        return self._name

    async def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        yield StreamFinished(finish_reason="stop")


class TestModelRouter:
    def test_routes_to_fast_explorer_before_any_edits(self) -> None:
        fast = FakeModel("fast-explorer-v3")
        deep = FakeModel("deep-reasoner-r1")
        router = ModelRouter(fast_model=fast, deep_model=deep)

        ctx = RunContext(
            run_id=RunId("run-1"),
            goal="Search and read files",
            mode=PermissionMode.INTERACTIVE,
            budget=Budget(),
        )
        assert router.select_model(ctx, step=1) is fast
        assert router.active_model is fast
        assert "routed(" in router.model_name

    def test_switches_to_deep_coder_after_edits_occur(self) -> None:
        fast = FakeModel("fast-explorer-v3")
        deep = FakeModel("deep-reasoner-r1")
        router = ModelRouter(fast_model=fast, deep_model=deep)

        ctx = RunContext(
            run_id=RunId("run-2"),
            goal="Refactor calculator",
            mode=PermissionMode.INTERACTIVE,
            budget=Budget(),
        )
        assert router.select_model(ctx, step=1) is fast

        # 记录一条文件编辑证据
        ctx.ledger = ctx.ledger.with_edit(
            EditEvidence(
                seq=1,
                path="src/calc.py",
                preimage_digest="abc",
                postimage_digest="def",
            )
        )

        assert router.select_model(ctx, step=2) is deep
        assert router.active_model is deep

    def test_switches_to_deep_coder_when_plan_is_active(self) -> None:
        fast = FakeModel("fast-explorer-v3")
        deep = FakeModel("deep-reasoner-r1")
        router = ModelRouter(fast_model=fast, deep_model=deep)

        ctx = RunContext(
            run_id=RunId("run-plan"),
            goal="Implement feature",
            mode=PermissionMode.INTERACTIVE,
            budget=Budget(),
        )
        # 初始未进入计划：使用 fast
        assert router.select_model(ctx, step=1) is fast

        # 计划所有步骤均为 pending：继续探索
        ctx.plan = (PlanStep(title="Step 1", status="pending"),)
        assert router.select_model(ctx, step=2) is fast

        # 计划有步骤变为 in_progress：提前切至 deep 深度推理
        ctx.plan = (PlanStep(title="Step 1", status="in_progress"),)
        assert router.select_model(ctx, step=3) is deep
        assert router.active_model is deep

    def test_always_deep_override(self) -> None:
        fast = FakeModel("fast-explorer-v3")
        deep = FakeModel("deep-reasoner-r1")
        router = ModelRouter(fast_model=fast, deep_model=deep, always_deep=True)

        ctx = RunContext(
            run_id=RunId("run-3"),
            goal="Search files",
            mode=PermissionMode.INTERACTIVE,
            budget=Budget(),
        )
        assert router.select_model(ctx, step=1) is deep
        assert router.active_model is deep

    async def test_generate_stream_proxies_to_active_model(self) -> None:
        fast = FakeModel("fast-v3")
        deep = FakeModel("deep-r1")
        router = ModelRouter(fast_model=fast, deep_model=deep)

        ctx = RunContext(
            run_id=RunId("run-4"),
            goal="Read code",
            mode=PermissionMode.INTERACTIVE,
            budget=Budget(),
        )
        router.select_model(ctx, step=1)

        req = ModelRequest(messages=(), tools=())
        events = [e async for e in router.generate_stream(req)]
        assert len(events) == 1
        assert isinstance(events[0], StreamFinished)

    async def test_aclose_closes_both_underlying_models(self) -> None:
        class ClosableFake(FakeModel):
            def __init__(self, name: str) -> None:
                super().__init__(name)
                self.closed = False

            async def aclose(self) -> None:
                self.closed = True

        fast = ClosableFake("fast")
        deep = ClosableFake("deep")
        router = ModelRouter(fast_model=fast, deep_model=deep)

        await router.aclose()
        assert fast.closed is True
        assert deep.closed is True

    async def test_aclose_tolerates_models_without_aclose(self) -> None:
        fast = FakeModel("fast")
        deep = FakeModel("deep")
        router = ModelRouter(fast_model=fast, deep_model=deep)
        await router.aclose()  # 不报错

    async def test_aclose_calls_deep_even_if_fast_raises(self) -> None:
        class FaultyFast(FakeModel):
            async def aclose(self) -> None:
                raise RuntimeError("fast close failed")

        class ClosableDeep(FakeModel):
            def __init__(self, name: str) -> None:
                super().__init__(name)
                self.closed = False

            async def aclose(self) -> None:
                self.closed = True

        fast = FaultyFast("fast")
        deep = ClosableDeep("deep")
        router = ModelRouter(fast_model=fast, deep_model=deep)

        import pytest

        with pytest.raises(RuntimeError, match="fast close failed"):
            await router.aclose()

        assert deep.closed is True

    def test_routed_profile_inherits_context_and_pricing(self) -> None:
        from haven.application.profiles import profile_for

        fast = FakeModel("deepseek-chat")
        deep = FakeModel("deepseek-reasoner")
        router = ModelRouter(fast_model=fast, deep_model=deep)

        profile = profile_for(router.model_name)
        assert profile.name == router.model_name
        assert profile.max_context_chars == 480_000
        assert profile.pricing.is_known is True
        assert profile.requires_tool_call_reasoning is True
        assert profile.supports_assistant_prefix is True
