"""Event emitter: persists authoritative events, fans out envelopes to sinks."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from haven.contracts.events import TRANSIENT_KINDS, ApplicationEvent, EventEnvelope
from haven.ports.event_sink import EventSinkPort
from haven.ports.session import SessionStorePort


class EventEmitter:
    def __init__(self, store: SessionStorePort, sinks: Sequence[EventSinkPort]) -> None:
        self._store = store
        self._sinks = list(sinks)
        self._last_seq: dict[str, int] = {}

    def add_sink(self, sink: EventSinkPort) -> None:
        self._sinks.append(sink)

    def last_seq(self, run_id: str) -> int:
        return self._last_seq.get(run_id, 0)

    async def emit(self, run_id: str, event: ApplicationEvent) -> EventEnvelope:
        if event.kind in TRANSIENT_KINDS:
            envelope = EventEnvelope(seq=0, at=datetime.now(UTC).isoformat(), event=event)
        else:
            envelope = await self._store.append_event(run_id, event)
            self._last_seq[run_id] = envelope.seq
        for sink in self._sinks:
            await sink.emit(envelope)
        return envelope
