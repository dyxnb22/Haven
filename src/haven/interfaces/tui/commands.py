"""TUI slash 命令的纯路由规则。"""

from dataclasses import dataclass
from typing import Literal

from haven.application.skills import MAX_DESCRIPTION_CHARS, SkillCatalog
from haven.domain.budget import Budget
from haven.interfaces.model_display import cost_text
from haven.interfaces.tui.presenter import PresenterState

HELP_TEXT = """\
Commands:
  /help      show this help
  /mode      show or change approval mode (/mode ask | smart | full)
  /plan GOAL prepare a read-only implementation plan
  /implement [ID] confirm and execute the ready plan (current run by default)
  /answer    reopen the pending question form
  /budget    show remaining budget
  /context   show what the model saw last turn
  /history [clear] view or clear task inputs in this workspace
  /memory [reload]  inspect guidance; reload while idle for the next run
  /remember TEXT   save a local project note while idle
  /memory forget ID | clear  delete one note or all notes for this workspace
  /verify [use NAME | clear]  inspect or choose required checks for the next run
  /model [use NAME | clear]  inspect models or choose a preset for new runs
  /stats     summarize timing, repeated reads and usage for this run
  /output [CALL_ID]  show Evidence, or read a saved process log
  /handoff [edit | prepare | clear | history]  review or revise task handoff notes
  /new       start a fresh conversation
  /sessions [QUERY] search, open, resume or fork stored sessions
  /skill     list discovered skills, or /skill use NAME... to load some
  /fork ID   start a new turn branched from run ID (fork the session)
  /rewind    undo this session's last run (fail-closed; asks to confirm)
  /diff [FILE] browse the recorded changes by file and hunk
  /review    review and apply a retained verified worktree
  /export    write a markdown report of the current run
  /quit      exit Haven
Input:
  @path      mention a file — the agent is pointed at it explicitly (it
             still reads the file itself through repo.read)
Keys:
  Enter      submit a task
  Up / Down  browse recent tasks; Down past the newest restores your draft
  a / r      approve / reject (in the approval dialog)
  F1..F4     switch tabs (Chat, Diff, Evidence, Trace)
  Ctrl+C     cancel the running task; press again to quit\
"""

CommandKind = Literal[
    "log",
    "sessions",
    "skill",
    "fork",
    "diff",
    "rewind",
    "export",
    "quit",
    "mode",
    "plan",
    "implement",
    "answer",
    "review",
    "new",
    "history",
    "memory",
    "remember",
    "verification",
    "model",
    "stats",
    "output",
    "handoff",
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
    if name == "/answer":
        return CommandAction("answer")
    if name == "/plan":
        parts = command.split(maxsplit=1)
        if len(parts) < 2 or len(parts[1].strip()) < 3:
            return CommandAction("log", "usage: /plan GOAL (at least 3 characters)")
        return CommandAction("plan", parts[1].strip())
    if name == "/implement":
        parts = command.split()
        if len(parts) > 2:
            return CommandAction("log", "usage: /implement [RUN_ID]")
        return CommandAction("implement", parts[1] if len(parts) == 2 else state.run_id)
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
        estimated = " (estimated)" if state.usage_estimated else ""
        message = (
            f"budget: step {state.step}/{budget.max_steps}, "
            f"tools {state.tool_calls}/{budget.max_tool_calls}, "
            f"tokens in {state.input_tokens}/{budget.max_input_tokens}, "
            f"out {state.output_tokens}/{budget.max_output_tokens}{estimated}, "
            f"cost {cost_text(state.cost_usd, state.cost_known)}, limit ${budget.max_cost_usd:.2f}"
        )
        return CommandAction("log", message)
    if name == "/context":
        return CommandAction("log", state.context_summary or "no context recorded yet")
    if name == "/stats":
        return (
            CommandAction("stats")
            if len(command.split()) == 1
            else CommandAction("log", "usage: /stats")
        )
    if name == "/history":
        parts = command.split()
        if len(parts) == 1:
            return CommandAction("history")
        if len(parts) == 2 and parts[1].lower() == "clear":
            return CommandAction("history", "clear")
        return CommandAction("log", "usage: /history [clear]")
    if name == "/memory":
        parts = command.split()
        if len(parts) == 1:
            return CommandAction("memory")
        if len(parts) == 2 and parts[1].lower() == "reload":
            return CommandAction("memory", "reload")
        if len(parts) == 2 and parts[1].lower() == "clear":
            return CommandAction("memory", "clear")
        if len(parts) == 3 and parts[1].lower() == "forget":
            return CommandAction("memory", f"forget {parts[2]}")
        return CommandAction("log", "usage: /memory [reload] | /memory forget ID | /memory clear")
    if name == "/verify":
        parts = command.split()
        if len(parts) == 1:
            return CommandAction("verification")
        if len(parts) == 2 and parts[1].lower() == "clear":
            return CommandAction("verification", "clear")
        if len(parts) == 3 and parts[1].lower() == "use":
            return CommandAction("verification", f"use {parts[2]}")
        return CommandAction("log", "usage: /verify [use NAME | clear]")
    if name == "/model":
        parts = command.split()
        if len(parts) == 1:
            return CommandAction("model")
        if len(parts) == 2 and parts[1].lower() == "clear":
            return CommandAction("model", "clear")
        if len(parts) == 3 and parts[1].lower() == "use":
            return CommandAction("model", f"use {parts[2]}")
        return CommandAction("log", "usage: /model [use NAME | clear]")
    if name == "/output":
        return CommandAction("output", command.partition(" ")[2].strip())
    if name == "/handoff":
        parts = command.split()
        if len(parts) == 1:
            return CommandAction("handoff")
        if len(parts) == 2 and parts[1] in {"edit", "prepare", "clear", "history"}:
            return CommandAction("handoff", parts[1])
        return CommandAction("log", "usage: /handoff [edit | prepare | clear | history]")
    if name == "/remember":
        parts = command.split(maxsplit=1)
        value = parts[1].strip() if len(parts) == 2 else ""
        if value:
            return CommandAction("remember", value)
        return CommandAction("log", "usage: /remember TEXT")
    if name == "/skill":
        return _skill_command(command, skills)
    if name == "/sessions":
        return CommandAction("sessions", command.partition(" ")[2].strip())
    if name == "/new":
        return CommandAction("new")
    if name == "/fork":
        parts = command.split(maxsplit=1)
        if len(parts) < 2:
            return CommandAction("log", "usage: /fork RUN_ID (see /sessions)")
        return CommandAction("fork", parts[1].strip())
    if name == "/diff":
        return CommandAction("diff", command.partition(" ")[2].strip())
    if name == "/review":
        return CommandAction("review")
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
