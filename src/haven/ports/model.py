"""Model port: the only way the core talks to an LLM provider."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal, Protocol

from haven.contracts.model import ModelEvent, ModelRequest

ProviderErrorCode = Literal[
    "auth",
    "quota",
    "rate_limited",
    "server",
    "timeout",
    "network",
    "protocol",
    "context_overflow",
    "cancelled",
    "exhausted",
]


class ProviderError(Exception):
    """Stable provider failure surface; raw provider payloads never leak out."""

    def __init__(
        self,
        code: ProviderErrorCode,
        message: str,
        *,
        retryable: bool = False,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code: ProviderErrorCode = code
        self.retryable = retryable
        #: Provider-requested wait before retrying, parsed from a `Retry-After`
        #: header when present. Advisory: the retry loop takes the longer of
        #: this and its own backoff, so a provider asking for a pause is obeyed.
        self.retry_after_s = retry_after_s


class ModelPort(Protocol):
    """Streams provider-neutral model events for one request."""

    @property
    def model_name(self) -> str: ...

    def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...
