"""确定性策略：模型提议副作用的唯一权威。

策略是纯函数。它的输入是权限模式和程序收集的提议事实集合，绝不是模型文本。
审批可以将 ASK 转换为一次执行票据；任何东西都不能将 DENY 转换为 ALLOW。
"""

from __future__ import annotations

from dataclasses import dataclass

from haven.domain.enums import PermissionMode, PolicyDecision, RiskLevel
from haven.domain.exec_policy import ExecClass

#: 只观察工作区的工具。
READ_ONLY_TOOLS = frozenset({"repo.list", "repo.search", "repo.read", "repo.diff"})

#: 会对文件或进程产生副作用的工具。
EFFECT_TOOLS = frozenset(
    {"repo.edit", "repo.create", "repo.delete", "repo.move", "repo.apply_patch", "repo.check"}
)

#: 只修改当前运行自身内存状态的工具。它们既不接触磁盘，也不接触运行范围
#: 之外的内容，因此即使在 read_only 模式下也允许使用。
STATE_TOOLS = frozenset({"task.plan"})

#: 运行任意程序的工具。它们的影响范围由操作系统沙箱限制，而不是由参数
#: 允许列表限制，因此在没有可用沙箱时，策略必须完全拒绝它们。
EXEC_TOOLS = frozenset({"repo.exec"})

KNOWN_TOOLS = READ_ONLY_TOOLS | EFFECT_TOOLS | STATE_TOOLS | EXEC_TOOLS


@dataclass(frozen=True, slots=True)
class ToolFacts:
    """关于一项工具提议、经程序验证的事实。

    这些事实由流水线根据规范化参数和工作区收集；模型无法直接影响它们。
    """

    #: 模型提议的工具名称；策略只接受 ``KNOWN_TOOLS`` 中的值。
    tool_name: str
    #: 规范化后的目标是否位于当前工作区内；未知或越界必须失败关闭。
    within_workspace: bool = True
    #: 目标是否触及 .git、.haven 等受保护路径。
    touches_protected_path: bool = False
    #: 检查配方是否已在最终配置中注册；None 表示事实尚未收集。
    recipe_registered: bool | None = None
    #: ``repo.exec`` 的确定性执行分类，例如 safe_read 或 shell_passthrough。
    exec_class: str | None = None
    #: 运行时是否有可用的操作系统沙箱；None 与 False 一样采取失败关闭。
    sandbox_available: bool | None = None
    #: 写入前目标内容的摘要，用于绑定审批与执行之间的 preimage。
    preimage_digest: str | None = None
    #: 规范化后的目标路径（如工具适用）；仅用于审计和策略诊断。
    path: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyOutcome:
    """确定性策略对工具提议给出的决定、原因和风险等级。"""

    #: 确定性的 allow/ask/deny 决定。
    decision: PolicyDecision
    #: 该决定对应的稳定机器可读原因。
    reason_code: str
    #: 审批 UI 和审计输出使用的风险等级。
    risk: RiskLevel


def evaluate_policy(mode: PermissionMode, facts: ToolFacts) -> PolicyOutcome:
    """为一项提议决定 allow/ask/deny。这是纯函数，并且对所有输入都有定义。"""
    if facts.tool_name not in KNOWN_TOOLS:
        return PolicyOutcome(PolicyDecision.DENY, "unknown_tool", RiskLevel.HIGH)

    # 硬拒绝在所有模式下都生效；用户审批不能覆盖它们。
    if not facts.within_workspace:
        return PolicyOutcome(PolicyDecision.DENY, "outside_workspace", RiskLevel.HIGH)
    if facts.touches_protected_path:
        return PolicyOutcome(PolicyDecision.DENY, "protected_path", RiskLevel.HIGH)

    if facts.tool_name in STATE_TOOLS:
        return PolicyOutcome(PolicyDecision.ALLOW, "state_tool", RiskLevel.NONE)

    if facts.tool_name in READ_ONLY_TOOLS:
        return PolicyOutcome(PolicyDecision.ALLOW, "read_only_tool", RiskLevel.NONE)

    if facts.tool_name in EXEC_TOOLS:
        # 缺少事实和事实为假时都采取失败关闭：没有沙箱的 exec 是本项目
        # 唯一绝不提供的能力。
        if not facts.sandbox_available:
            return PolicyOutcome(PolicyDecision.DENY, "sandbox_unavailable", RiskLevel.HIGH)
        if mode is PermissionMode.READ_ONLY:
            return PolicyOutcome(PolicyDecision.DENY, "read_only_mode", RiskLevel.MEDIUM)
        if facts.exec_class == ExecClass.SAFE_READ.value:
            return PolicyOutcome(PolicyDecision.ALLOW, "safe_read_exec", RiskLevel.LOW)
        if facts.exec_class == ExecClass.SHELL_PASSTHROUGH.value:
            return PolicyOutcome(
                PolicyDecision.ASK, "shell_passthrough_requires_approval", RiskLevel.HIGH
            )
        return PolicyOutcome(PolicyDecision.ASK, "exec_requires_approval", RiskLevel.MEDIUM)

    # 从这里开始是有副作用的工具。
    if mode is PermissionMode.READ_ONLY:
        return PolicyOutcome(PolicyDecision.DENY, "read_only_mode", RiskLevel.MEDIUM)

    if facts.tool_name == "repo.check":
        if not facts.recipe_registered:
            return PolicyOutcome(PolicyDecision.DENY, "unregistered_recipe", RiskLevel.HIGH)
        return PolicyOutcome(PolicyDecision.ASK, "check_requires_approval", RiskLevel.MEDIUM)

    if facts.tool_name == "repo.create":
        return PolicyOutcome(PolicyDecision.ASK, "create_requires_approval", RiskLevel.MEDIUM)

    if facts.tool_name == "repo.delete":
        return PolicyOutcome(PolicyDecision.ASK, "delete_requires_approval", RiskLevel.MEDIUM)

    if facts.tool_name == "repo.move":
        return PolicyOutcome(PolicyDecision.ASK, "move_requires_approval", RiskLevel.MEDIUM)

    if facts.tool_name == "repo.apply_patch":
        # 整个补丁只需一次审批；预览包含完整 diff，审批摘要绑定每个被触及
        # 文件的 preimage。
        return PolicyOutcome(PolicyDecision.ASK, "patch_requires_approval", RiskLevel.MEDIUM)

    # repo.edit：普通编辑路径。
    return PolicyOutcome(PolicyDecision.ASK, "write_requires_approval", RiskLevel.MEDIUM)
