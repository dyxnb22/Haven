"""Provider-neutral model contract.

Adapters translate provider wire formats into these types; nothing
provider-specific may leak past this boundary into the core.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from haven.contracts.base import StrictModel


class Usage(StrictModel):
    input_tokens: int = 0
    output_tokens: int = 0
    #: Portion of output_tokens spent on hidden reasoning, when the provider
    #: reports it. Already included in output_tokens; tracked separately so a
    #: cost report can explain where the tokens went.
    reasoning_tokens: int = 0
    estimated: bool = False


class ToolCallProposal(StrictModel):
    """A complete tool call as proposed by the model. Arguments stay as raw
    JSON text until the pipeline validates them against the tool schema."""

    call_id: str
    tool_name: str
    arguments_json: str


class ToolSchema(StrictModel):
    name: str
    description: str
    parameters: dict[str, Any]


class ModelMessage(StrictModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: tuple[ToolCallProposal, ...] = ()
    tool_call_id: str | None = None


class ModelRequest(StrictModel):
    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolSchema, ...] = ()
    max_output_tokens: int | None = None
    temperature: float = 0.0


# --- streaming events -------------------------------------------------------


class TextDelta(StrictModel):
    kind: Literal["text_delta"] = "text_delta"
    text: str


class ReasoningDelta(StrictModel):
    """Hidden chain-of-thought from a reasoning model.

    Surfaced to the UI so a long think does not look like a hang, but never
    added to `ModelResult.text` and never echoed back in the transcript: it is
    not the answer, and most providers reject their own reasoning on input.
    """

    kind: Literal["reasoning_delta"] = "reasoning_delta"
    text: str


class ToolCallReady(StrictModel):
    kind: Literal["tool_call"] = "tool_call"
    call: ToolCallProposal


class UsageReport(StrictModel):
    kind: Literal["usage"] = "usage"
    usage: Usage


class StreamFinished(StrictModel):
    kind: Literal["finished"] = "finished"
    finish_reason: Literal["stop", "tool_calls", "length", "error"] = "stop"


ModelEvent = Annotated[
    TextDelta | ReasoningDelta | ToolCallReady | UsageReport | StreamFinished,
    Field(discriminator="kind"),
]


class ModelResult(StrictModel):
    """Assembled outcome of one streamed model turn."""

    text: str
    tool_calls: tuple[ToolCallProposal, ...] = ()
    usage: Usage = Usage()
    finish_reason: Literal["stop", "tool_calls", "length", "error"] = "stop"
    ttft_ms: int = 0
    duration_ms: int = 0
