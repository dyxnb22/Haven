"""TUI 和 CLI 共用的有限进程摘要；所有文本仍需由终端层转义。"""

import shlex

from haven.contracts.events import ToolCompleted
from haven.domain.enums import AgentMode
from haven.domain.evidence import required_check_states
from haven.ports.session import SessionStorePort

RECENT_PROCESS_LIMIT = 10


def process_result_lines(event: ToolCompleted) -> tuple[str, ...]:
    report = event.process
    if report is None:
        return ()
    target = f" ({report.recipe_id})" if report.recipe_id else ""
    status = f"; tool error={event.error_code}" if event.error_code else ""
    timeout = "; timed out" if report.timed_out else ""
    lines = [
        f"{event.call_id[:200]}: {event.tool_name}{target} "
        f"exit={report.exit_code}{timeout}{status}",
        *(f"  {item}" for item in report.diagnostics[:3]),
    ]
    if len(report.diagnostics) > 3:
        lines.append("  More diagnostics in saved output.")
    if report.output_truncated:
        lines.append(
            "  Saved output truncated; see capture status and omission markers in the log."
        )
    command = f"haven output {shlex.quote(event.run_id)} {shlex.quote(event.call_id)}"
    lines.append(f"  Output: {command[:1000]}")
    return tuple(lines)


async def process_overview(store: SessionStorePort, run_id: str) -> str:
    if await store.get_run(run_id) is None:
        raise ValueError(f"run not found: {run_id}")
    lines: list[str] = []
    checkpoint = await store.load_checkpoint(run_id)
    if checkpoint and checkpoint.verification:
        ledger = checkpoint.evidence.to_domain()
        label = f"Required checks ({checkpoint.verification.group_name})"
        if checkpoint.agent_mode is AgentMode.PLAN:
            label += " — deferred until implementation"
        lines.append(label)
        lines.extend(
            f"  {item.recipe_id}: {item.status}"
            for item in required_check_states(
                checkpoint.verification.to_domain(),
                ledger.checks,
                ledger.last_edit_seq,
            )
        )
    results = [
        envelope.event
        for envelope in await store.load_events(run_id)
        if isinstance(envelope.event, ToolCompleted) and envelope.event.process
    ]
    lines.append(f"Process results (latest {RECENT_PROCESS_LIMIT}; observations, not acceptance):")
    for event in results[-RECENT_PROCESS_LIMIT:]:
        lines.extend(process_result_lines(event))
    if not results:
        lines.append("  No saved process output (older runs may have none).")
    return "\n".join(lines)
