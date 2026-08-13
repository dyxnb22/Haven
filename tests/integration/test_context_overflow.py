"""Provider-confirmed context overflow is recovered by forced compaction.

`max_context_chars` is a char budget checked against an *estimated* token
window (ADR 0022). A CJK/emoji-dense transcript can breach the real window even
inside that budget, so the provider returns a 400 the char check never caught.
Rather than fail the run, RunService shrinks the budget, forcing more history to
compact, and retries — bounded, so a persistently overflowing run still stops.
"""

from collections.abc import AsyncIterator
from pathlib import Path

from haven.contracts.model import ModelEvent, ModelRequest
from haven.domain.enums import RunStatus, StopReason
from haven.ports.model import ProviderError
from tests.integration.harness import Harness, finish, make_repo, text


class OverflowingModel:
    """Raises `context_overflow` on its first N calls, then answers."""

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
        # Initial attempt plus a bounded number of shrink-and-retry attempts,
        # not an unbounded loop.
        assert model.attempts == 3
