"""从 presenter 状态生成 Textual 控件所需的纯展示文本。"""

from dataclasses import dataclass

from haven.interfaces.tui.presenter import PresenterState, TimelineEntry

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
    chat: str
    diff: str
    evidence: str
    trace: str


def timeline_text(entry: TimelineEntry) -> str:
    return f"{_ICONS.get(entry.kind, '·')} {entry.text}"


def panel_text(state: PresenterState) -> PanelText:
    chat = state.chat_text
    if state.reasoning_text:
        chat += f"\n[dim]thinking… {state.reasoning_text[-800:]}[/dim]"
    if state.streaming_text:
        chat += f"\n● {state.streaming_text}▌"
    if state.plan_lines:
        plan = "\n".join(state.plan_lines)
        chat = f"[b]Plan[/b]\n{plan}\n\n{chat}"
    return PanelText(
        chat=chat or "(no conversation yet)",
        diff=state.diff_text or "(no diff yet)",
        evidence="\n".join(state.evidence_rows) or "(no evidence yet)",
        trace="\n".join(state.trace_rows) or "(no trace yet)",
    )
