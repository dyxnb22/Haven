"""由提供商确认的上下文溢出通过强制压缩恢复。

`max_context_chars` 是针对*估算* token 窗口检查的字符预算（ADR 0022）。中文/emoji
密集的对话记录即使在该预算内，也可能超过真实窗口，因此提供商会返回字符检查
未能捕获的 400。RunService 不会让运行失败，而是缩小预算，强制压缩更多历史并重试；
该过程有界，因此持续溢出的运行仍会停止。
"""

from collections.abc import AsyncIterator
from pathlib import Path

from haven.contracts.model import ModelEvent, ModelRequest
from haven.domain.enums import RunStatus, StopReason
from haven.ports.model import ProviderError
from tests.integration.harness import Harness, finish, make_repo, text


class OverflowingModel:
    """前 N 次调用抛出 `context_overflow`，之后回答。"""

    model_name = "flaky"

    def __init__(self, overflow_times: int) -> None:
        self._overflow_times = overflow_times
        self.attempts = 0
        self.built_sizes: list[int] = []

    def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.attempts += 1
        self.built_sizes.append(sum(len(m.content) for m in request.messages))
        return self._gen()

    async def _gen(self) -> AsyncIterator[ModelEvent]:
        if self.attempts <= self._overflow_times:
            raise ProviderError("context_overflow", "provider context window exceeded")
        yield text("recovered after compaction")
        yield finish()


def install(h: Harness, model: object) -> None:
    h.service._model = model  # type: ignore[attr-defined]  # noqa: SLF001


class TestContextOverflowRecovery:
    async def test_a_single_overflow_is_recovered_by_retrying(self, tmp_path: Path) -> None:
        model = OverflowingModel(overflow_times=1)
        h = Harness(make_repo(tmp_path), [])
        install(h, model)

        outcome = await h.service.run("Do a thing")

        assert outcome.status is RunStatus.SUCCEEDED
        assert model.attempts == 2, "the overflow was retried once after shrinking"
        assert "recovered after compaction" in outcome.final_text

    async def test_the_forced_compaction_is_announced(self, tmp_path: Path) -> None:
        model = OverflowingModel(overflow_times=1)
        h = Harness(make_repo(tmp_path), [])
        install(h, model)

        await h.service.run("Do a thing")

        notices = h.sink.events_of("notice")
        assert any("forcing compaction" in getattr(n, "message", "") for n in notices)

    async def test_a_persistent_overflow_stops_the_run_bounded(self, tmp_path: Path) -> None:
        model = OverflowingModel(overflow_times=99)
        h = Harness(make_repo(tmp_path), [])
        install(h, model)

        outcome = await h.service.run("Do a thing")

        assert outcome.status is RunStatus.FAILED
        assert outcome.stop_reason is StopReason.PROVIDER_ERROR
        # 包括初始尝试在内，只允许有限次数的缩小并重试，而不是无界循环。
        assert model.attempts == 3
