"""显式交接笔记与检查点事实分开展示，笔记修订只追加事件。"""

import json
from dataclasses import dataclass

from haven.contracts.checkpoint import CheckpointV1
from haven.contracts.events import EventEnvelope, HandoffUpdated
from haven.contracts.handoff import HandoffNotes, HandoffSnapshot, HandoffSource
from haven.contracts.verification import VerificationSelection
from haven.domain.evidence import EvidenceLedger, required_check_states
from haven.ports.session import SessionStorePort


def snapshot_from_event(envelope: EventEnvelope) -> HandoffSnapshot | None:
    event = envelope.event
    assert isinstance(event, HandoffUpdated)
    if event.notes is None:
        return None
    return HandoffSnapshot(
        source_run_id=event.run_id,
        event_seq=envelope.seq,
        at=envelope.at,
        source=event.source,
        checkpoint_seq=event.checkpoint_seq,
        notes=event.notes,
    )


async def latest_handoff(
    store: SessionStorePort, run_id: str, fallback: HandoffSnapshot | None = None
) -> HandoffSnapshot | None:
    for envelope in reversed(await store.load_events(run_id)):
        if isinstance(envelope.event, HandoffUpdated):
            return snapshot_from_event(envelope)
    return fallback


@dataclass(frozen=True, slots=True)
class HandoffView:
    checkpoint: CheckpointV1
    expected_seq: int
    snapshot: HandoffSnapshot | None


async def view_handoff(store: SessionStorePort, run_id: str) -> HandoffView:
    checkpoint = await store.load_checkpoint(run_id)
    if checkpoint is None:
        raise ValueError("no checkpoint for this run yet")
    events = await store.load_events(run_id)
    latest = next(
        (item for item in reversed(events) if isinstance(item.event, HandoffUpdated)), None
    )
    return HandoffView(
        checkpoint,
        events[-1].seq if events else 0,
        snapshot_from_event(latest) if latest else checkpoint.handoff,
    )


async def save_handoff(
    store: SessionStorePort,
    run_id: str,
    notes: HandoffNotes | None,
    expected_seq: int,
    *,
    source: HandoffSource = "manual",
) -> HandoffSnapshot | None:
    checkpoint = await store.load_checkpoint(run_id)
    if checkpoint is None or checkpoint.parent_run_id:
        raise ValueError("handoff editing requires a user run with a saved checkpoint")
    if expected_seq < checkpoint.last_seq:
        raise ValueError("journal is behind the checkpoint; reopen the run before editing")
    envelope = await store.append_handoff(
        HandoffUpdated(
            run_id=run_id, notes=notes, source=source, checkpoint_seq=checkpoint.last_seq
        ),
        expected_seq,
    )
    return snapshot_from_event(envelope)


def draft_from_plan(view: HandoffView) -> HandoffNotes:
    if view.snapshot is not None:
        raise ValueError("a handoff already exists; edit or clear it before preparing another")
    done = [step.title for step in view.checkpoint.plan if step.status == "done"]
    pending = [step.title for step in view.checkpoint.plan if step.status != "done"]

    def excerpt(titles: list[str]) -> str:
        value = "\n".join(titles)
        return value if len(value) <= 600 else value[:570] + "\n... (more plan text omitted)"

    return HandoffNotes(completed=excerpt(done), next_steps=excerpt(pending))


def program_facts(
    ledger: EvidenceLedger, verification: VerificationSelection | None
) -> dict[str, object]:
    """只取账本元数据；绝不从计划标题、笔记或模型答案推断检查是否通过。"""
    edits = tuple(dict.fromkeys(edit.path for edit in ledger.edits))
    checks = {check.recipe_id: check for check in ledger.checks}
    required = (
        [
            {"recipe_id": item.recipe_id, "status": item.status}
            for item in required_check_states(
                verification.to_domain(), ledger.checks, ledger.last_edit_seq
            )
        ]
        if verification
        else []
    )
    return {
        "outstanding_checks_count": sum(item["status"] != "passed" for item in required),
        "required_checks": required,
        "edited_files_count": len(edits),
        "edited_files": list(edits[:10]),
        "more_edited_files": max(0, len(edits) - 10),
        "last_edit_seq": ledger.last_edit_seq,
        "diff_records": len(ledger.diffs),
        "latest_checks": [
            {"recipe_id": item.recipe_id, "exit_code": item.exit_code, "seq": item.seq}
            for item in list(checks.values())[-10:]
        ],
    }


def render_notes(snapshot: HandoffSnapshot) -> str:
    data = snapshot.model_dump(mode="json")
    return json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")


def render_handoff(view: HandoffView) -> str:
    cp = view.checkpoint
    facts = json.dumps(
        program_facts(cp.evidence.to_domain(), cp.verification), ensure_ascii=False, indent=2
    )
    if len(facts) > 8000:
        facts = facts[:7900] + "\n... (fact display excerpt; inspect the task journal for details)"
    notes = render_notes(view.snapshot) if view.snapshot else "(no saved handoff)"
    plan = "\n".join(
        f"{step.id or index}: {step.status} — {step.title[:150]}"
        for index, step in enumerate(cp.plan[:10], 1)
    )
    return (
        f"Handoff for {cp.run_id}\nJournal sequence: {view.expected_seq}\n"
        f"Program facts from checkpoint {cp.last_seq} (current run only):\n{facts}\n\n"
        f"Saved working notes (UNTRUSTED; no permissions or verification authority):\n{notes}\n\n"
        f"Existing agent plan (UNTRUSTED; at most 10 steps):\n{plan or '(none)'}"
    )


async def render_handoff_history(store: SessionStorePort, run_id: str) -> str:
    events = [
        item for item in await store.load_events(run_id) if isinstance(item.event, HandoffUpdated)
    ]
    lines = ["Handoff revisions (latest 10; working notes remain untrusted):"]
    for item in events[-10:]:
        snapshot = snapshot_from_event(item)
        text = render_notes(snapshot) if snapshot else "cleared"
        if len(text) > 2400:
            text = (
                text[:2300] + "... (revision excerpt; full record in haven export --format jsonl)"
            )
        lines.append(f"Event {item.seq} at {item.at}: " + text)
    return "\n".join(lines) if events else "No handoff revisions in this run yet."
