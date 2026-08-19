import asyncio

from haven.adapters.memory_session import MemorySessionStore
from haven.application.emitter import EventEmitter
from haven.contracts.events import EventEnvelope, Notice, StepStarted


async def test_reentrant_sink_event_is_queued_without_deadlock_or_reordering() -> None:
    store = MemorySessionStore()
    await store.create_run("run-1", "/tmp/ws", "digest", "goal", "interactive")
    observed: list[int] = []
    emitter: EventEmitter

    class FollowUpSink:
        async def emit(self, envelope: EventEnvelope) -> None:
            if isinstance(envelope.event, StepStarted):
                await emitter.emit(
                    "run-1",
                    Notice(run_id="run-1", level="info", message="nested follow-up"),
                )

    class Observer:
        async def emit(self, envelope: EventEnvelope) -> None:
            observed.append(envelope.seq)

    # FollowUpSink intentionally comes first: broadcasting the nested event
    # immediately would make Observer see sequence 2 before sequence 1.
    emitter = EventEmitter(store, [FollowUpSink(), Observer()])
    async with asyncio.timeout(1):
        await emitter.emit("run-1", StepStarted(run_id="run-1", step=1))

    assert observed == [1, 2]
    assert [item.seq for item in await store.load_events("run-1")] == [1, 2]
