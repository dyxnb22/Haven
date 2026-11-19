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


async def test_release_drops_per_run_state_but_not_a_live_one() -> None:
    """终态运行的状态必须被释放；仍在广播的不能动。

    长期存活的界面进程（TUI）会在一个会话里跑很多次运行，而 `_last_seq` 与 `_locks`
    此前从不删除：一次运行漏一个 int 加一个 asyncio.Lock，直到进程退出。
    """
    store = MemorySessionStore()
    for run_id in ("run-a", "run-b"):
        await store.create_run(run_id, "/tmp/ws", "digest", "goal", "interactive")
    emitter = EventEmitter(store, [])

    await emitter.emit("run-a", StepStarted(run_id="run-a", step=1))
    await emitter.emit("run-b", StepStarted(run_id="run-b", step=1))
    assert emitter.last_seq("run-a") == 1

    emitter.release("run-a")
    # 状态没了：last_seq 归零，锁也不再占着
    assert emitter.last_seq("run-a") == 0
    assert "run-a" not in emitter._locks
    # 另一个运行不受影响
    assert emitter.last_seq("run-b") == 1

    # 释放之后再发事件不会炸：defaultdict 会重建那把锁，序号由 store 继续给
    envelope = await emitter.emit("run-a", StepStarted(run_id="run-a", step=2))
    assert envelope.seq == 2


async def test_release_does_nothing_while_a_broadcast_is_in_flight() -> None:
    """广播途中收权会让两个 task 拿到不同的锁对象，序号分配就不再互斥了。"""
    store = MemorySessionStore()
    await store.create_run("run-1", "/tmp/ws", "digest", "goal", "interactive")
    entered = asyncio.Event()
    release_sink = asyncio.Event()

    class SlowSink:
        async def emit(self, envelope: EventEnvelope) -> None:
            entered.set()
            await release_sink.wait()

    emitter = EventEmitter(store, [SlowSink()])
    task = asyncio.create_task(emitter.emit("run-1", StepStarted(run_id="run-1", step=1)))
    async with asyncio.timeout(2):
        await entered.wait()

    emitter.release("run-1")
    # 广播未结束：必须原样保留，否则锁对象被换掉
    assert "run-1" in emitter._locks
    assert emitter.last_seq("run-1") == 1

    release_sink.set()
    await task
    # 广播结束后再释放才有效
    emitter.release("run-1")
    assert "run-1" not in emitter._locks
