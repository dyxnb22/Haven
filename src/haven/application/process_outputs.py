"""保存和读取人类可回看的进程日志；构件位置只从所属运行的事件解析。"""

from hashlib import sha256
from pathlib import Path

from haven.application.diagnostic_feeder import extract_diagnostics, format_diagnostic
from haven.contracts.events import ToolCompleted
from haven.contracts.process_output import ProcessReport
from haven.ports.executor import CapturedOutput, CheckOutcome, ExecOutcome
from haven.ports.session import SessionStorePort

MAX_OUTPUT_ARTIFACT_BYTES = 16 * 1024 * 1024


async def save_process_output(
    store: SessionStorePort,
    outcome: CheckOutcome | ExecOutcome,
    cwd: Path,
    recipe_id: str = "",
    *,
    run_id: str = "",
    call_id: str = "",
) -> ProcessReport:
    captured = outcome.output or CapturedOutput(
        outcome.stdout_tail, outcome.stderr_tail, outcome.truncated
    )
    capture_label = "TRUNCATED — partial output retained" if captured.truncated else "complete"
    content = (
        f"Process output (untrusted text)\nRun: {run_id}\nCall: {call_id}\n"
        f"Working directory: {cwd}\n"
        f"Exit code: {outcome.exit_code}\nTimed out: {outcome.timed_out}\n"
        f"Capture: {capture_label}\n\n--- stdout ---\n{captured.stdout}"
        f"\n--- stderr ---\n{captured.stderr}"
    )
    digest = await store.put_artifact(content.encode("utf-8"))
    diagnostics = extract_diagnostics(
        recipe_id or "exec", captured.stdout, captured.stderr, outcome.exit_code
    )
    return ProcessReport(
        recipe_id=recipe_id,
        exit_code=outcome.exit_code,
        timed_out=outcome.timed_out,
        cwd=str(cwd),
        output_digest=digest,
        output_truncated=captured.truncated,
        diagnostics=tuple(format_diagnostic(item)[:600] for item in diagnostics),
    )


async def read_process_output(store: SessionStorePort, run_id: str, call_id: str) -> str:
    """只有该运行已完成进程事件中的内容地址可用；不接受任意文件或构件摘要。"""
    events = await store.load_events(run_id)
    event = next(
        (
            envelope.event
            for envelope in reversed(events)
            if isinstance(envelope.event, ToolCompleted)
            and envelope.event.call_id == call_id
            and envelope.event.process is not None
        ),
        None,
    )
    if event is None or event.process is None:
        raise ValueError("no saved process output for this run/call (older runs may have none)")
    digest = event.process.output_digest
    content = await store.get_artifact(digest)
    if content is None:
        raise ValueError("saved output artifact is missing")
    if len(content) > MAX_OUTPUT_ARTIFACT_BYTES or sha256(content).hexdigest() != digest:
        raise ValueError("saved output artifact is invalid")
    return content.decode("utf-8", errors="replace")
