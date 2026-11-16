"""双轨混合模型路由器（ADR 0042）。

在任务探索阶段使用低延迟、低成本的轻量模型（Fast Explorer），
在方案构思、代码编辑与证据验证阶段切换至深度推理模型（Deep Coder）。
两者共享同一个 RunService、同一个 ContextBuilder 与同一个 ToolPipeline，
保持单执行通道与确定性状态机完全不变。
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from dataclasses import dataclass

from haven.application.state import RunContext
from haven.contracts.model import ModelEvent, ModelRequest
from haven.contracts.model_call import ModelRouteReason
from haven.ports.model import ModelPort


@dataclass(frozen=True, slots=True)
class ModelChoice:
    model: ModelPort
    reason: ModelRouteReason


class ModelRouter:
    """基于运行上下文自适应调度快速探索与深度编码模型的双轨路由器。"""

    def __init__(
        self,
        fast_model: ModelPort,
        deep_model: ModelPort,
        *,
        always_deep: bool = False,
        fast_turns: int | None = None,
    ) -> None:
        self.fast_model = fast_model
        self.deep_model = deep_model
        self.always_deep = always_deep
        self.fast_turns = fast_turns
        self._active_model: ModelPort = deep_model if always_deep else fast_model

    @property
    def model_name(self) -> str:
        """返回复合路由器标识。"""
        return f"routed({self.fast_model.model_name}+{self.deep_model.model_name})"

    @property
    def active_model(self) -> ModelPort:
        """返回当前选中的活动底层模型。"""
        return self._active_model

    def choose_model(self, ctx: RunContext, step: int) -> ModelChoice:
        """根据当前运行状态确定性选择本步应调用的模型。

        规则：
        1. 若配置 always_deep：始终使用 deep_model。
        2. 若 ctx.ledger.edits 非空（已经发生代码写入或修改）：切换至 deep_model。
        3. 若 fast_turns 达到限制：切换至 deep_model。
        4. 若模型计划（task.plan）表明已进入实现阶段：提前切换至 deep_model。
        5. 其余只读探索与摸底阶段（未发生代码修改）：使用 fast_model。
        """
        if self.always_deep:
            return ModelChoice(self.deep_model, "always_deep")

        if ctx.ledger.edits:
            return ModelChoice(self.deep_model, "edits")

        if self.fast_turns is not None and step > self.fast_turns:
            return ModelChoice(self.deep_model, "fast_turns")

        if ctx.plan and any(item.status in ("in_progress", "done") for item in ctx.plan):
            return ModelChoice(self.deep_model, "plan")

        return ModelChoice(self.fast_model, "exploration")

    def select_model(self, ctx: RunContext, step: int) -> ModelPort:
        """保留直接调用方的本地代理接口；运行器使用无可变状态的 choose_model。"""
        self._active_model = self.choose_model(ctx, step).model
        return self._active_model

    def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """流式代理至当前活跃模型。"""
        return self._active_model.generate_stream(request)

    async def aclose(self) -> None:
        """尽力关闭底层快速探索与深度编码模型的异步资源。"""
        try:
            closer = getattr(self.fast_model, "aclose", None)
            if closer is not None:
                res = closer()
                if inspect.isawaitable(res):
                    await res
        finally:
            closer = getattr(self.deep_model, "aclose", None)
            if closer is not None:
                res = closer()
                if inspect.isawaitable(res):
                    await res
