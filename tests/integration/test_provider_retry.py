"""Retry policy for model calls.

A model call has no side effects, so a connection failure before any token is
safe to retry. Anything else — a non-retryable error, or a failure after output
already streamed — must not be retried.
"""

from collections.abc import AsyncIterator
from pathlib import Path

from haven.contracts.model import ModelEvent, ModelRequest
from haven.domain.enums import RunStatus, StopReason
from haven.ports.model import ProviderError
from tests.integration.harness import Harness, finish, make_repo, text


class FlakyModel:
    """Fails with the given errors, then plays the given events."""

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
        assert model.attempts == 3  # initial + 2 retries

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

    async def test_failure_after_partial_output_is_not_retried(self, tmp_path: Path) -> None:
        """Retrying here would duplicate what the user already saw."""
        model = FlakyModel(
            [ProviderError("network", "mid-stream drop", retryable=True)],
            fail_after_first_event=True,
        )
        h = Harness(make_repo(tmp_path), [])
        install(h, model)

        outcome = await h.service.run("Do a thing")
        assert outcome.status is RunStatus.FAILED
        assert model.attempts == 1
