"""从 presenter 状态生成 Textual 控件所需的纯展示文本。"""

from dataclasses import dataclass

from haven.domain.evidence import required_check_states
from haven.interfaces.process_output import process_result_lines
from haven.interfaces.tui.presenter import PresenterState, TimelineEntry, sanitize

_ICONS = {
    "user": ">",
    "agent": "●",
    "tool": "⚙",
    "policy": "✋",
    "approval": "?",
    "plan": "☰",
    "notice": "!",
    "system": "◆",
}


@dataclass(frozen=True, slots=True)
class PanelText:
    """一个 TUI 面板的标题和已格式化内容。"""

    #: 格式化后的对话面板内容。
    chat: str
    #: 格式化后的差异面板内容。
    diff: str
    #: 格式化后的证据面板内容。
    evidence: str
    #: 格式化后的追踪面板内容。
    trace: str


def timeline_text(entry: TimelineEntry) -> str:
    """为时间线条目添加稳定图标并返回展示文本。"""
    return f"{_ICONS.get(entry.kind, '·')} {entry.text}"


def verification_text(state: PresenterState) -> str:
    if state.verification is None:
        return "No required verification group for this run."
    label = f"Required checks ({sanitize(state.verification.group_name, 100)})"
    if state.agent_mode == "plan":
        label += " — deferred until implementation"
    lines = [label]
    lines.extend(
        f"  {sanitize(item.recipe_id, 100)}: {item.status}"
        for item in required_check_states(
            state.verification.to_domain(), state.verification_checks, state.last_edit_seq
        )
    )
    return "\n".join(lines)


def panel_text(state: PresenterState) -> PanelText:
    """将 PresenterState 组装为聊天、差异、证据和追踪四个面板文本。"""
    chat = state.chat_text
    if state.reasoning_text:
        chat += f"\n[dim]thinking… {state.reasoning_text[-800:]}[/dim]"
    if state.streaming_text:
        chat += f"\n● {state.streaming_text}▌"
    if state.plan_lines:
        plan = "\n".join(state.plan_lines)
        label = f"Plan ({state.plan_status})" if state.plan_status else "Plan"
        chat = f"[b]{label}[/b]\n{plan}\n\n{chat}"
    evidence = "\n".join(state.evidence_rows)
    if state.verification:
        evidence = verification_text(state) + ("\n\n" + evidence if evidence else "")
    if state.process_results:
        results = "\n".join(
            sanitize(line, 1200)
            for event in state.process_results
            for line in process_result_lines(event)
        )
        evidence += "\n\nProcess results (latest 10; observations, not acceptance):\n" + results
        evidence += "\nUse /output CALL_ID to read the saved log."
    return PanelText(
        chat=chat or "(no conversation yet)",
        diff=state.diff_text or "(no diff yet)",
        evidence=evidence or "(no evidence yet)",
        trace="\n".join(state.trace_rows) or "(no trace yet)",
    )
