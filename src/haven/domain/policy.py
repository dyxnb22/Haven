"""确定性策略：模型提议副作用的唯一权威。

策略是纯函数。它的输入是权限模式和程序收集的提议事实集合，绝不是模型文本。
审批可以将 ASK 转换为一次执行票据；任何东西都不能将 DENY 转换为 ALLOW。
"""

from __future__ import annotations

from dataclasses import dataclass

from haven.domain.enums import AgentMode, PermissionMode, PolicyDecision, RiskLevel
from haven.domain.exec_policy import ExecClass

#: 只观察工作区的工具。
#: `skill.read` 读的是技能目录里的文件——用户级在工作区之外，项目级在 `.haven`
#: 之内，两者都在 `repo.read` 的管辖之外，因此它有自己的通道（ADR 0032）。
READ_ONLY_TOOLS = frozenset(
    {
        "repo.list",
        "repo.search",
        "repo.read",
        "skill.read",
        "repo.diff",
        "repo.outline",
        "repo.symbol",
        "task.status",
        "task.logs",
        "web.search",
        "web.fetch",
        "agent.delegate",
    }
)

#: 会对文件或进程产生副作用的工具。
EFFECT_TOOLS = frozenset(
    {
        "repo.edit",
        "repo.create",
        "repo.delete",
        "repo.move",
        "repo.apply_patch",
        "repo.check",
        "recipe.propose",
        "task.input",
    }
)

#: 只修改当前运行自身内存状态的工具。它们既不接触磁盘，也不接触运行范围
#: 之外的内容，因此即使在 read_only 模式下也允许使用。
STATE_TOOLS = frozenset({"task.plan", "task.stop", "user.ask"})

# task.stop 会终止进程，不属于规划阶段允许的状态修改。
PLAN_TOOLS = READ_ONLY_TOOLS | {"task.plan", "user.ask"}
CHILD_READ_TOOLS = frozenset(
    {
        "repo.list",
        "repo.search",
        "repo.read",
        "repo.outline",
        "repo.symbol",
        "repo.diff",
    }
)

#: 运行任意程序的工具。它们的影响范围由操作系统沙箱限制，而不是由参数
#: 允许列表限制，因此在没有可用沙箱时，策略必须完全拒绝它们。
EXEC_TOOLS = frozenset({"repo.exec", "task.start"})

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
    exec_class: ExecClass | None = None
    #: 运行时是否有可用的操作系统沙箱；None 与 False 一样采取失败关闭。
    sandbox_available: bool | None = None
    #: 写入前目标内容的摘要，用于绑定审批与执行之间的 preimage。
    preimage_digest: str | None = None
    #: 规范化后的目标路径（如工具适用）；仅用于审计和策略诊断。
    path: str | None = None
    #: MCP 扩展工具相关事实（ADR 0039）。
    mcp_server: str | None = None
    mcp_read_only: bool = False
    mcp_schema_drift: bool = False
    #: 流水线从 RunContext 绑定的行为模式，工具参数不能覆盖。
    agent_mode: AgentMode = AgentMode.AGENT
    allowed_in_run: bool = True
    within_scope: bool = True
    replaces_required_recipe: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.exec_class, str):
            object.__setattr__(self, "exec_class", ExecClass(self.exec_class))
        if isinstance(self.agent_mode, str):
            object.__setattr__(self, "agent_mode", AgentMode(self.agent_mode))


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
    if not facts.allowed_in_run:
        return PolicyOutcome(PolicyDecision.DENY, "delegated_tool_unavailable", RiskLevel.HIGH)
    if not facts.within_scope:
        return PolicyOutcome(PolicyDecision.DENY, "outside_file_scope", RiskLevel.HIGH)
    if facts.tool_name.startswith("mcp."):
        if facts.mcp_schema_drift:
            return PolicyOutcome(PolicyDecision.DENY, "mcp_schema_drift", RiskLevel.HIGH)
        if facts.agent_mode is AgentMode.PLAN and not facts.mcp_read_only:
            return PolicyOutcome(PolicyDecision.DENY, "plan_mode", RiskLevel.MEDIUM)
        if mode is PermissionMode.READ_ONLY and not facts.mcp_read_only:
            return PolicyOutcome(PolicyDecision.DENY, "read_only_mode", RiskLevel.MEDIUM)
        if facts.mcp_read_only:
            return PolicyOutcome(PolicyDecision.ALLOW, "mcp_read_only", RiskLevel.LOW)
        return PolicyOutcome(PolicyDecision.ASK, "mcp_requires_approval", RiskLevel.HIGH)

    if facts.tool_name not in KNOWN_TOOLS:
        return PolicyOutcome(PolicyDecision.DENY, "unknown_tool", RiskLevel.HIGH)

    # 硬拒绝在所有模式下都生效；用户审批不能覆盖它们。
    if not facts.within_workspace:
        return PolicyOutcome(PolicyDecision.DENY, "outside_workspace", RiskLevel.HIGH)
    if facts.touches_protected_path:
        return PolicyOutcome(PolicyDecision.DENY, "protected_path", RiskLevel.HIGH)
    if facts.replaces_required_recipe:
        return PolicyOutcome(PolicyDecision.DENY, "required_recipe_frozen", RiskLevel.HIGH)

    if facts.agent_mode is AgentMode.PLAN and facts.tool_name not in PLAN_TOOLS:
        return PolicyOutcome(PolicyDecision.DENY, "plan_mode", RiskLevel.MEDIUM)

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
        if facts.exec_class is ExecClass.SAFE_READ:
            return PolicyOutcome(PolicyDecision.ALLOW, "safe_read_exec", RiskLevel.LOW)
        if facts.exec_class is ExecClass.SHELL_PASSTHROUGH:
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

    if facts.tool_name == "recipe.propose":
        return PolicyOutcome(PolicyDecision.ASK, "propose_recipe_requires_approval", RiskLevel.HIGH)

    if facts.tool_name == "task.input":
        return PolicyOutcome(PolicyDecision.ASK, "task_input_requires_approval", RiskLevel.HIGH)

    if facts.tool_name == "repo.create":
        return PolicyOutcome(PolicyDecision.ASK, "create_requires_approval", RiskLevel.MEDIUM)

    if facts.tool_name == "repo.delete":
        return PolicyOutcome(PolicyDecision.ASK, "delete_requires_approval", RiskLevel.HIGH)

    if facts.tool_name == "repo.move":
        return PolicyOutcome(PolicyDecision.ASK, "move_requires_approval", RiskLevel.HIGH)

    if facts.tool_name == "repo.apply_patch":
        # 整个补丁只需一次审批；预览包含完整 diff，审批摘要绑定每个被触及
        # 文件的 preimage。
        return PolicyOutcome(PolicyDecision.ASK, "patch_requires_approval", RiskLevel.HIGH)

    # repo.edit：普通编辑路径。
    return PolicyOutcome(PolicyDecision.ASK, "write_requires_approval", RiskLevel.MEDIUM)
