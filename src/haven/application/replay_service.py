"""Replay：将运行已持久化的事件重新发送给 sink。

Replay 从不调用模型或任何工具——它只是日志的纯投影，这正是轨迹审查和 TUI 重建
值得信任的原因。
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
