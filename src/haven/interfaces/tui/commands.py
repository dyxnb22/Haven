"""TUI slash 命令的纯路由规则。"""

from dataclasses import dataclass
from typing import Literal

from haven.application.skills import MAX_DESCRIPTION_CHARS, SkillCatalog
from haven.domain.budget import Budget
from haven.interfaces.tui.presenter import PresenterState

HELP_TEXT = """\
Commands:
  /help      show this help
  /mode      show or change approval mode (/mode ask | smart | full)
  /budget    show remaining budget
  /context   show what the model saw last turn
  /sessions  list recent runs you can continue or fork
  /skill     list discovered skills, or /skill use NAME... to load some
  /fork ID   start a new turn branched from run ID (fork the session)
  /rewind    undo this session's last run (fail-closed; asks to confirm)
  /diff      switch to the Diff tab
  /export    write a markdown report of the current run
  /quit      exit Haven
Input:
  @path      mention a file — the agent is pointed at it explicitly (it
             still reads the file itself through repo.read)
Keys:
  Enter      submit a task
  a / r      approve / reject (in the approval dialog)
  F1..F4     switch tabs (Chat, Diff, Evidence, Trace)
  Ctrl+C     cancel the running task; press again to quit\
"""

CommandKind = Literal[
    "log", "sessions", "skill", "fork", "diff", "rewind", "export", "quit", "mode"
]


@dataclass(frozen=True, slots=True)
class CommandAction:
    """解析后的用户命令及其参数。"""

    #: 路由给 TUI 控制器的动作。
    kind: CommandKind
    #: 可选载荷，例如日志消息或运行 ID。
    value: str = ""


def route_command(
    command: str,
    state: PresenterState,
    budget: Budget | None = None,
    skills: SkillCatalog | None = None,
) -> CommandAction:
    """将 slash 命令解析为纯数据动作，不执行任何外部副作用。

    `skills` 与 `budget` 同样属于可选上下文：启动完成前两者都可能不可用，而路由
    本身必须保持纯函数，因此这里只产出数据，由控制器决定怎么呈现。
    """
    name = command.split()[0].lower()
    if name == "/help":
        return CommandAction("log", HELP_TEXT)
    if name == "/mode":
        parts = command.split(maxsplit=1)
        if len(parts) == 1:
            current = state.approval_mode or "ask"
            message = (
                f"approval mode: {current}\n"
                "Available modes:\n"
                "  /mode ask    — strict approval: ask for every mutation\n"
                "  /mode smart  — approve for me: auto-approve low/medium risk,\n"
                "                 ask on high risk (delete/move/patch/exec)\n"
                "  /mode full   — full access within workspace: auto-approve all non-denied actions"
            )
            return CommandAction("log", message)
        target = parts[1].strip().lower()
        if target in {"ask", "smart", "full"}:
            return CommandAction("mode", target)
        return CommandAction("log", f"unknown approval mode {target!r}; use ask, smart, or full")
    if name == "/budget":
        if budget is None:
            return CommandAction("log", "budget unavailable before startup completes")
        message = (
            f"budget: step {state.step}/{budget.max_steps}, "
            f"tools {state.tool_calls}/{budget.max_tool_calls}, "
            f"tokens in {state.input_tokens}/{budget.max_input_tokens}, "
            f"out {state.output_tokens}/{budget.max_output_tokens}, "
            f"cost ${state.cost_usd:.4f}/{budget.max_cost_usd:.2f}"
            + (" (estimated)" if state.usage_estimated else "")
        )
        return CommandAction("log", message)
    if name == "/context":
        return CommandAction("log", state.context_summary or "no context recorded yet")
    if name == "/skill":
        return _skill_command(command, skills)
    if name == "/sessions":
        return CommandAction("sessions")
    if name == "/fork":
        parts = command.split(maxsplit=1)
        if len(parts) < 2:
            return CommandAction("log", "usage: /fork RUN_ID (see /sessions)")
        return CommandAction("fork", parts[1].strip())
    if name == "/diff":
        return CommandAction("diff")
    if name == "/rewind":
        return CommandAction("rewind", command)
    if name == "/export":
        return CommandAction("export")
    if name == "/quit":
        return CommandAction("quit")
    return CommandAction("log", f"unknown command {name}; try /help")


def _skill_command(command: str, skills: SkillCatalog | None) -> CommandAction:
    """`/skill` 家族：列出目录、为下一次运行选择、或清除选择。

    选择在这里只被记录成名子串；真正的解析（未知名字报错）发生在运行启动时，
    因此路由保持纯函数。已加载的技能正文永远不在这里回显——它是上下文，不是
    给人看的日志。
    """
    parts = command.split()
    if len(parts) == 1:
        if skills is None:
            return CommandAction("log", "skills unavailable before startup completes")
        if not skills.skills:
            return CommandAction("log", "no skills discovered (see `haven doctor`)")
        lines = ["skills (advisory prompt fragments, not tools):"]
        lines += (
            f"- {skill.name} [{skill.scope}] {skill.digest} — "
            f"{skill.description[:MAX_DESCRIPTION_CHARS]}"
            for skill in skills.skills
        )
        if skills.refused:
            lines.append(f"{len(skills.refused)} skill file(s) refused — see `haven doctor`")
        lines.append("load with: /skill use NAME [NAME...]")
        return CommandAction("log", "\n".join(lines))
    action = parts[1].lower()
    if action == "clear":
        return CommandAction("skill", "clear")
    if action == "use":
        names = [name for name in parts[2:] if name]
        if not names:
            return CommandAction("log", "usage: /skill use NAME [NAME...]")
        return CommandAction("skill", " ".join(names))
    return CommandAction("log", "usage: /skill | /skill use NAME [NAME...] | /skill clear")
