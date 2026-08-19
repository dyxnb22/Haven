"""模型端口：核心层与 LLM 提供商通信的唯一方式。"""

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
    """稳定的提供商失败接口；原始提供商载荷绝不会泄漏出去。"""

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
        #: 提供商要求重试前等待的时长（如果存在 `Retry-After` 标头，则从中
        #: 解析）。这是建议值：重试循环会取它与自身退避时长中较大的一个，
        #: 因而会遵守提供商要求的暂停时间。
        self.retry_after_s = retry_after_s


class ModelPort(Protocol):
    """为一次请求流式产生与提供商无关的模型事件。"""

    @property
    def model_name(self) -> str:
        """返回用于追踪、计费和重放展示的模型标识。"""
        ...

    def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """按顺序流式产生文本、工具调用、用量和结束事件。"""
        ...
