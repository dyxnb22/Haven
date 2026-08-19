"""评估用例契约与逐案例结果。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import Field

from haven.contracts.base import StrictModel
from haven.ports.model import ModelPort

ModelFactory = Callable[[], ModelPort]


class ExpectSpec(StrictModel):
    """评估用例对安全性、质量和范围的期望。"""

    #: 运行最终应达到的 RunStatus 名称。
    status: str
    #: 期望的停止原因；为空表示不检查该项。
    stop_reason: str = ""
    #: 期望的门禁原因；为空表示不检查该项。
    gate_reason: str = ""
    #: 文件路径 -> 必须出现的文本片段。
    file_contains: dict[str, str] = Field(default_factory=dict)
    #: 文件路径 -> 不得出现的文本片段。
    file_not_contains: dict[str, str] = Field(default_factory=dict)
    #: 允许被运行修改的相对路径集合。
    allowed_changed_files: tuple[str, ...] = ()
    #: 运行过程中允许出现的错误码集合。
    error_codes: tuple[str, ...] = ()
    #: 期望看到的策略拒绝原因集合。
    denied_reasons: tuple[str, ...] = ()
    #: transcript 中禁止出现的文本片段。
    transcript_must_not_contain: tuple[str, ...] = ()
    #: 评估允许消耗的最大步数；0 表示不额外限制。
    max_steps_used: int = 0


class RecipeDef(StrictModel):
    """评估用例中可执行的验证配方定义。"""

    #: 在受限执行器中启动的参数向量。
    argv: tuple[str, ...]
    #: 配方允许运行的最长时间，单位为秒。
    timeout_seconds: float = 60.0
    #: 评估夹具之外允许配方读取的额外根目录。
    readable_roots: tuple[str, ...] = ()


class EvalCase(StrictModel):
    """一个完整的离线评估场景及其预置工作区。"""

    #: 稳定的案例标识。
    id: str
    #: 用于聚合报告的场景类别。
    category: str
    #: 传给代理的自然语言目标。
    goal: str
    #: 夹具目录或构建器的名称。
    fixture: str
    #: 初始权限模式。
    mode: str = "interactive"
    #: 离线审批模拟采用的策略。
    approval_policy: str = "approve_all"
    #: 是否在模型流结束后重复最后一轮输入。
    repeat_last: bool = False
    #: 可选的多轮场景说明或模型模拟提示。
    scenario: str = ""
    #: 是否先执行发现阶段，再开始正式任务。
    discover: bool = False
    #: 隐藏验证配方或检查标识。
    hidden_check: str = ""
    #: 本案例构建上下文允许使用的字符上限；0 表示使用 profile 默认值。
    max_context_chars: int = 0
    #: 覆盖默认预算的字段和值。
    budget: dict[str, int | float] = Field(default_factory=dict)
    #: 按 id 注册的离线验证配方。
    recipes: dict[str, RecipeDef] = Field(default_factory=dict)
    #: 每一轮离线模型返回的消息/工具调用序列。
    turns: list[list[dict[str, Any]]] = Field(default_factory=list)
    #: 案例通过条件。
    expect: ExpectSpec


@dataclass(slots=True)
class CaseResult:
    """单个评估场景的执行结果和失败原因。"""

    #: 对应 EvalCase 的稳定标识。
    case_id: str
    #: 对应案例的场景类别。
    category: str
    #: 所有期望条件是否满足。
    passed: bool
    #: 未通过的断言或执行错误，按发现顺序记录。
    failures: list[str] = field(default_factory=list)
    #: 实际最终状态名称。
    status: str = ""
    #: 实际停止原因名称。
    stop_reason: str = ""
    #: 实际完成的模型交互轮数。
    steps: int = 0
    #: 实际执行的工具调用次数。
    tool_calls: int = 0
    #: 实际消耗的输入 token 数。
    input_tokens: int = 0
    #: 实际消耗的输出 token 数。
    output_tokens: int = 0
    #: 输入 token 中的缓存命中数。
    cached_input_tokens: int = 0
    #: 按评估 pricing 计算的费用，单位为美元。
    cost_usd: float = 0.0
    #: 案例执行耗时，单位为毫秒。
    duration_ms: int = 0
    #: 未经授权发生的工作区变更文件数。
    unauthorized_changes: int = 0
    #: 超出案例允许范围发生的变更文件数。
    out_of_scope_changes: int = 0
