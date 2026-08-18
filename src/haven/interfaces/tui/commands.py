"""TUI slash 命令的纯路由规则。"""

from dataclasses import dataclass
from typing import Any, Literal

from haven.interfaces.tui.presenter import PresenterState

HELP_TEXT = """\
Commands:
  /help      show this help
  /budget    show remaining budget
  /context   show what the model saw last turn
  /sessions  list recent runs you can continue or fork
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

CommandKind = Literal["log", "sessions", "fork", "diff", "rewind", "export", "quit"]


@dataclass(frozen=True, slots=True)
class CommandAction:
    kind: CommandKind
    value: str = ""


def route_command(command: str, state: PresenterState, budget: Any = None) -> CommandAction:
    name = command.split()[0].lower()
    if name == "/help":
        return CommandAction("log", HELP_TEXT)
    if name == "/budget":
        if budget is None:
            return CommandAction("log", "budget unavailable before startup completes")
        message = (
            f"budget: step {state.step}/{budget.max_steps}, "
            f"tools {state.tool_calls}/{budget.max_tool_calls}, "
            f"tokens {state.input_tokens}/{state.output_tokens}, "
            f"cost ${state.cost_usd:.4f}/{budget.max_cost_usd:.2f}"
            + (" (estimated)" if state.usage_estimated else "")
        )
        return CommandAction("log", message)
    if name == "/context":
        return CommandAction("log", state.context_summary or "no context recorded yet")
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
