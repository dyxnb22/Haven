"""双轨混合模型路由器（ADR 0042）。

在任务探索阶段使用低延迟、低成本的轻量模型（Fast Explorer），
在方案构思、代码编辑与证据验证阶段切换至深度推理模型（Deep Coder）。
两者共享同一个 RunService、同一个 ContextBuilder 与同一个 ToolPipeline，
保持单执行通道与确定性状态机完全不变。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass

from haven.application.state import RunContext
from haven.contracts.model_call import ModelRouteReason
from haven.ports.model import ModelPort


@dataclass(frozen=True, slots=True)
class ModelChoice:
    model: ModelPort
    reason: ModelRouteReason


class ModelRouter:
    """基于运行上下文自适应调度快速探索与深度编码模型的双轨路由器。

    只做调度，不当模型：`choose_model` 是无副作用的纯函数，同一步问多少次都得到
    同一个答案，因此轨迹可以逐字节复现。
    """

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

    @property
    def model_name(self) -> str:
        """返回复合路由器标识。"""
        return f"routed({self.fast_model.model_name}+{self.deep_model.model_name})"

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


#: 运行器真正需要的"模型"：一个具体的 ModelPort，或者一个决定本步用哪个 ModelPort
#: 的路由器。两者都有 `model_name` 与 `aclose`；`choose_model` 只有路由器有，由
#: `isinstance` 区分。路由器自己**不是**模型——它没有 `generate_stream`，因为离开了
#: "本步选了谁"这个上下文，它无从知道该把流导向哪里。
RunnableModel = ModelPort | ModelRouter
