"""模型调用的重试策略。

模型调用没有副作用，因此在产生任何 token 前发生的连接失败可以安全重试。其他
情况——不可重试的错误，或输出已经开始流式传输后的失败——都不得重试。
"""

from collections.abc import AsyncIterator
from pathlib import Path

from haven.application.run_service import MODEL_RETRY_MAX_DELAY, _retry_delay
from haven.contracts.model import ModelEvent, ModelRequest
from haven.domain.enums import RunStatus, StopReason
from haven.ports.model import ProviderError
from tests.integration.harness import Harness, finish, make_repo, text


class FlakyModel:
    """先抛出给定错误，然后产生给定事件。"""

    model_name = "flaky"

    def __init__(self, errors: list[ProviderError], fail_after_first_event: bool = False) -> None:
        self._errors = list(errors)
        self._fail_after_first_event = fail_after_first_event
        self.attempts = 0

    def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.attempts += 1
        error = self._errors.pop(0) if self._errors else None
        return self._gen(error)

    async def _gen(self, error: ProviderError | None) -> AsyncIterator[ModelEvent]:
        if error is not None and self._fail_after_first_event:
            yield text("partial answer")
            raise error
        if error is not None:
            raise error
        yield text("recovered answer")
        yield finish()


def install(h: Harness, model: object) -> None:
    h.service._model = model  # type: ignore[attr-defined]  # noqa: SLF001


class TestRetryDelay:
    """重试前的等待时间取指数退避和提供商要求的 `Retry-After` 中较长者，同时设有
    上限，避免一个恶意标头毫无理由地阻塞整个运行。"""

    def test_backoff_grows_when_no_hint_is_given(self) -> None:
        assert _retry_delay(0, None) == 1.0
        assert _retry_delay(1, None) == 2.0

    def test_a_longer_retry_after_wins_over_backoff(self) -> None:
        assert _retry_delay(0, 5.0) == 5.0

    def test_backoff_wins_when_it_is_the_longer_wait(self) -> None:
        assert _retry_delay(2, 1.0) == 4.0

    def test_an_absurd_retry_after_is_capped(self) -> None:
        assert _retry_delay(0, 9999.0) == MODEL_RETRY_MAX_DELAY


class TestRetryableFailures:
    async def test_transient_network_error_is_retried(self, tmp_path: Path) -> None:
        model = FlakyModel([ProviderError("network", "ConnectError", retryable=True)])
        h = Harness(make_repo(tmp_path), [])
        install(h, model)

        outcome = await h.service.run("Do a thing")
        assert outcome.status is RunStatus.SUCCEEDED
        assert model.attempts == 2
        assert "recovered answer" in outcome.final_text

    async def test_retry_is_bounded(self, tmp_path: Path) -> None:
        model = FlakyModel([ProviderError("network", "ConnectError", retryable=True)] * 5)
        h = Harness(make_repo(tmp_path), [])
        install(h, model)

        outcome = await h.service.run("Do a thing")
        assert outcome.status is RunStatus.FAILED
        assert outcome.stop_reason is StopReason.PROVIDER_ERROR
        assert model.attempts == 3  # 初始尝试 + 2 次重试

    async def test_retry_is_reported_in_the_trace(self, tmp_path: Path) -> None:
        model = FlakyModel([ProviderError("rate_limited", "429", retryable=True)])
        h = Harness(make_repo(tmp_path), [])
        install(h, model)

        await h.service.run("Do a thing")
        notices = h.sink.events_of("notice")
        assert any("retrying" in getattr(n, "message", "") for n in notices)


class TestNonRetryableFailures:
    async def test_auth_error_is_not_retried(self, tmp_path: Path) -> None:
        model = FlakyModel([ProviderError("auth", "bad key", retryable=False)])
        h = Harness(make_repo(tmp_path), [])
        install(h, model)

        outcome = await h.service.run("Do a thing")
        assert outcome.status is RunStatus.FAILED
        assert model.attempts == 1


class TestMidStreamRecovery:
    """部分流式传输的轮次可以安全重试：组装的文本和工具调用只属于本次尝试，永远
    不会进入对话记录。"""

    async def test_mid_stream_drop_is_retried(self, tmp_path: Path) -> None:
        model = FlakyModel(
            [ProviderError("network", "mid-stream drop", retryable=True)],
            fail_after_first_event=True,
        )
        h = Harness(make_repo(tmp_path), [])
        install(h, model)

        outcome = await h.service.run("Do a thing")
        assert outcome.status is RunStatus.SUCCEEDED
        assert model.attempts == 2
        # 被丢弃的部分文本不能残留在答案中
        assert "partial answer" not in outcome.final_text
        assert "recovered answer" in outcome.final_text

    async def test_ui_is_told_to_discard_the_partial_output(self, tmp_path: Path) -> None:
        model = FlakyModel(
            [ProviderError("network", "mid-stream drop", retryable=True)],
            fail_after_first_event=True,
        )
        h = Harness(make_repo(tmp_path), [])
        install(h, model)

        await h.service.run("Do a thing")
        assert h.sink.events_of("stream.restarted")

    async def test_restart_event_clears_the_streaming_buffer(self) -> None:
        from haven.contracts.events import (
            AssistantDelta,
            EventEnvelope,
            StreamRestarted,
        )
        from haven.interfaces.tui.presenter import PresenterState, reduce

        def wrap(event: object) -> EventEnvelope:
            return EventEnvelope(seq=0, at="2026-01-01T00:00:00+00:00", event=event)  # type: ignore[arg-type]

        state = reduce(
            PresenterState(run_id="r"), wrap(AssistantDelta(run_id="r", step=1, text="partial"))
        )
        assert state.streaming_text == "partial"
        state = reduce(state, wrap(StreamRestarted(run_id="r", step=1)))
        assert state.streaming_text == ""
