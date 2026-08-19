"""事件发射器：持久化权威事件，并将事件信封分发给各个 sink。"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from haven.contracts.events import TRANSIENT_KINDS, ApplicationEvent, EventEnvelope
from haven.ports.event_sink import EventSinkPort
from haven.ports.session import SessionStorePort


@dataclass(slots=True)
class _PendingDispatch:
    """一个已分配序号、等待按序广播的事件。"""

    envelope: EventEnvelope
    done: asyncio.Future[None] | None


class EventEmitter:
    """为事件分配序号并广播给持久化和界面消费者。"""

    def __init__(self, store: SessionStorePort, sinks: Sequence[EventSinkPort]) -> None:
        self._store = store
        self._sinks = list(sinks)
        self._last_seq: dict[str, int] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._queues: defaultdict[str, deque[_PendingDispatch]] = defaultdict(deque)
        self._dispatching: set[str] = set()
        self._dispatch_owners: dict[str, asyncio.Task[object]] = {}

    def add_sink(self, sink: EventSinkPort) -> None:
        """追加一个事件消费者；后续事件会按注册顺序广播给它。"""
        self._sinks.append(sink)

    def last_seq(self, run_id: str) -> int:
        """返回本进程最近为运行持久化的事件序号，尚无事件时为 0。"""
        return self._last_seq.get(run_id, 0)

    async def emit(self, run_id: str, event: ApplicationEvent) -> EventEnvelope:
        """持久化并按序广播事件，允许 sink 在回调中安全地产生新事件。

        锁只保护序号分配和入队，不能覆盖 sink 回调：steering 等合法回调会再次
        调用 ``emit``，若持锁等待它就会自锁。每个 run 仅有一个队列消费者；其他
        task 等待自己的事件完成广播，而消费者 task 的重入事件只入队并立即返回，
        由外层消费者在当前事件广播结束后继续处理。
        """
        current = asyncio.current_task()
        if current is None:  # pragma: no cover - coroutine 总在 task 中执行
            raise RuntimeError("event emission requires an asyncio task")

        async with self._locks[run_id]:
            if event.kind in TRANSIENT_KINDS:
                envelope = EventEnvelope(seq=0, at=datetime.now(UTC).isoformat(), event=event)
            else:
                envelope = await self._store.append_event(run_id, event)
                self._last_seq[run_id] = envelope.seq

            become_dispatcher = run_id not in self._dispatching
            reentrant = self._dispatch_owners.get(run_id) is current
            done = (
                None
                if become_dispatcher or reentrant
                else asyncio.get_running_loop().create_future()
            )
            self._queues[run_id].append(_PendingDispatch(envelope, done))
            if become_dispatcher:
                self._dispatching.add(run_id)
                self._dispatch_owners[run_id] = current

        if become_dispatcher:
            await self._drain(run_id)
        elif done is not None:
            await done
        return envelope

    async def _drain(self, run_id: str) -> None:
        """由单个消费者按入队顺序广播，队列状态的切换保持原子。"""
        while True:
            async with self._locks[run_id]:
                queue = self._queues[run_id]
                if not queue:
                    self._dispatching.discard(run_id)
                    self._dispatch_owners.pop(run_id, None)
                    self._queues.pop(run_id, None)
                    return
                pending = queue.popleft()

            try:
                for sink in tuple(self._sinks):
                    await sink.emit(pending.envelope)
            except BaseException as exc:
                if pending.done is not None and not pending.done.done():
                    pending.done.set_exception(exc)
                await self._abort_dispatch(run_id, exc)
                raise
            else:
                if pending.done is not None and not pending.done.done():
                    pending.done.set_result(None)

    async def _abort_dispatch(self, run_id: str, exc: BaseException) -> None:
        """广播失败时唤醒所有等待者，避免把一次 sink 错误变成永久挂起。"""
        async with self._locks[run_id]:
            for pending in self._queues.pop(run_id, ()):
                if pending.done is not None and not pending.done.done():
                    pending.done.set_exception(exc)
            self._dispatching.discard(run_id)
            self._dispatch_owners.pop(run_id, None)
