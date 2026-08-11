"""Replay: re-deliver a run's persisted events to a sink.

Replay never calls the model or any tool — it is a pure projection of the
journal, which is what makes trace review and TUI reconstruction trustworthy.
"""

from __future__ import annotations

from haven.contracts.events import EventEnvelope
from haven.ports.event_sink import EventSinkPort
from haven.ports.session import SessionStorePort


class ReplayService:
    def __init__(self, store: SessionStorePort) -> None:
        self._store = store

    async def replay(self, run_id: str, sink: EventSinkPort) -> list[EventEnvelope]:
        envelopes = await self._store.load_events(run_id)
        for envelope in envelopes:
            await sink.emit(envelope)
        return envelopes
