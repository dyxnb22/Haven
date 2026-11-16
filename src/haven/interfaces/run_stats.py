"""从本地事件日志生成单次运行的耗时、重读与用量视图。"""

from collections import Counter
from collections.abc import Sequence

from pydantic import ValidationError

from haven.contracts.events import (
    ContextBuilt,
    EventEnvelope,
    ModelCompleted,
    RunFinished,
    StepStarted,
    ToolCompleted,
    ToolProposed,
)
from haven.contracts.tools import RepoReadArgs
from haven.domain.enums import ToolStatus
from haven.interfaces.model_display import cost_text
from haven.ports.session import RunRecord, SessionStorePort

DETAIL_LIMIT = 3


def _read_lines(reads: Sequence[str]) -> list[str]:
    comparable: Counter[tuple[str, int, int]] = Counter()
    for proposal in reads:
        try:
            args = RepoReadArgs.model_validate_json(proposal)
        except ValidationError:
            # 旧记录、缺失提议和截断的参数无法精确匹配行范围。
            continue
        comparable[args.path, args.start_line, args.max_lines] += 1
    total = comparable.total()
    lines = [
        f"Reads {len(reads)} successful, {total} comparable, "
        f"{total - len(comparable)} repeated at the same path and range"
    ]
    for (path, start, count), repeats in comparable.most_common(DETAIL_LIMIT):
        if repeats > 1:
            lines.append(f"  {path!r} from line {start}, up to {count} lines, {repeats} reads")
    return lines


def _usage_lines(
    models: Sequence[tuple[int, ModelCompleted]], finished: RunFinished | None
) -> list[str]:
    if finished is not None:
        if "duration_ms" in finished.model_fields_set:
            wall = f"Recorded active time {finished.duration_ms:,} ms"
        else:
            wall = "Recorded active time unavailable"
        source = "Run usage"
        input_tokens = finished.input_tokens
        output_tokens = finished.output_tokens
        cached = finished.cached_input_tokens
        estimated = finished.usage_estimated
        cost = finished.cost_usd
        known = finished.cost_known and "cost_known" in finished.model_fields_set
        lines = [wall]
    elif models:
        source = "Observed usage from completed calls"
        input_tokens = sum(event.input_tokens for _, event in models)
        output_tokens = sum(event.output_tokens for _, event in models)
        cached = sum(event.cached_input_tokens for _, event in models)
        estimated = any(event.usage_estimated for _, event in models)
        info = [event.model_info for _, event in models if event.model_info is not None]
        cost = sum(item.cost_usd for item in info)
        known = len(info) == len(models) and all(item.cost_known for item in info)
        missing = sum(not (event.input_tokens or event.output_tokens) for _, event in models)
        lines = [f"Usage missing on {missing} completed call(s)"] if missing else []
    else:
        return ["Usage unavailable", "Cost unknown"]
    suffix = " (includes estimates)" if estimated else ""
    lines.append(f"{source} {input_tokens:,} in / {output_tokens:,} out, {cached:,} cached{suffix}")
    subtotal = f" (priced subtotal {cost_text(cost, True)})" if not known and cost else ""
    lines.append(f"Cost {cost_text(cost, known)}{subtotal}")
    return lines


def render_run_stats(run: RunRecord, envelopes: Sequence[EventEnvelope]) -> str:
    """只使用已有观测值，显示计数及最慢调用的事件序号。"""
    models: list[tuple[int, ModelCompleted]] = []
    tools: list[tuple[int, ToolCompleted]] = []
    contexts: list[tuple[int, ContextBuilt]] = []
    read_proposals: dict[str, str] = {}
    reads: list[str] = []
    steps = proposed = 0
    finished: RunFinished | None = None
    for envelope in envelopes:
        event = envelope.event
        if isinstance(event, StepStarted):
            steps += 1
        elif isinstance(event, ModelCompleted):
            models.append((envelope.seq, event))
        elif isinstance(event, ToolProposed):
            proposed += 1
            if event.tool_name == "repo.read":
                read_proposals[event.call_id] = event.args_summary
        elif isinstance(event, ToolCompleted):
            tools.append((envelope.seq, event))
            if event.tool_name == "repo.read":
                args = read_proposals.pop(event.call_id, "")
                if event.status == ToolStatus.OK:
                    reads.append(args)
        elif isinstance(event, ContextBuilt):
            contexts.append((envelope.seq, event))
        elif isinstance(event, RunFinished) and event.status == run.status.value:
            finished = event

    lines = [f"Run {run.run_id} ({run.status.value})"]
    model_ms = sum(event.duration_ms for _, event in models)
    model_time = f"{model_ms:,} ms" if models else "unavailable"
    lines.append(f"Model streams {model_time}, {len(models)} completed / {steps} started steps")
    for seq, model in sorted(models, key=lambda row: -row[1].duration_ms)[:DETAIL_LIMIT]:
        name = model.model_info.model_name if model.model_info else "model unrecorded"
        lines.append(
            f"  Event {seq}, step {model.step}, {name}, {model.duration_ms:,} ms "
            f"(first event {model.ttft_ms:,} ms)"
        )

    timed_tools = [row for row in tools if "duration_ms" in row[1].model_fields_set]
    tool_ms = sum(event.duration_ms for _, event in timed_tools)
    tool_time = f"{tool_ms:,} ms" if timed_tools else "unavailable"
    lines.append(
        f"Tool pipeline {tool_time}, {len(timed_tools)} timed / {len(tools)} completed / "
        f"{proposed} proposed (includes approval wait)"
    )
    for seq, tool in sorted(timed_tools, key=lambda row: -row[1].duration_ms)[:DETAIL_LIMIT]:
        lines.append(
            f"  Event {seq}, {tool.tool_name[:100]} ({tool.status[:24]}), {tool.duration_ms:,} ms"
        )

    lines.extend(_read_lines(reads))
    compacted = sum(
        any(
            segment.source == "run_digest"
            or segment.reason == "truncated to fit the context budget"
            for segment in event.segments
        )
        for _, event in contexts
    )
    lines.append(f"Context {len(contexts)} builds, {compacted} with a digest or budget truncation")
    if contexts:
        seq, largest = max(contexts, key=lambda row: row[1].total_bytes)
        lines.append(f"  Largest {largest.total_bytes:,} bytes, event {seq}, step {largest.step}")
    lines.extend(_usage_lines(models, finished))
    return "\n".join(lines)


async def run_stats_text(store: SessionStorePort, run_id: str) -> str:
    run = await store.get_run(run_id)
    if run is None:
        raise ValueError(f"run not found: {run_id}")
    return render_run_stats(run, await store.load_events(run_id))
