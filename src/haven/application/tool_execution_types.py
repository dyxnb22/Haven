"""工具执行层共享的结果类型和小型结果构造函数。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from haven.application.approval_cards import ToolPreview
from haven.application.state import RunContext
from haven.contracts.model import ToolCallProposal
from haven.contracts.tools import ToolArgs, ToolResult
from haven.domain.enums import ToolErrorCode, ToolStatus
from haven.ports.workspace import WorkspaceError

MODEL_PAYLOAD_CHARS = 8_000

_ERROR_CODES: dict[str, ToolErrorCode] = {
    "denied": ToolErrorCode.DENIED,
    "not_found": ToolErrorCode.NOT_FOUND,
    "invalid_arguments": ToolErrorCode.INVALID_ARGUMENTS,
    "stale_preimage": ToolErrorCode.STALE_PREIMAGE,
    "ambiguous_match": ToolErrorCode.AMBIGUOUS_MATCH,
    "timeout": ToolErrorCode.TIMEOUT,
    "internal": ToolErrorCode.INTERNAL,
}


@dataclass(frozen=True, slots=True)
class ToolExecution:
    result: ToolResult
    effect_unknown: bool = False


ExecuteHandler = Callable[
    [RunContext, ToolCallProposal, ToolArgs, str, ToolPreview],
    Awaitable[ToolExecution],
]


def ok_result(
    call: ToolCallProposal, payload: dict[str, object], truncated: bool = False
) -> ToolResult:
    return ToolResult(
        call_id=call.call_id,
        tool_name=call.tool_name,
        status=ToolStatus.OK,
        payload=payload,
        truncated=truncated,
    )


def error_result(call: ToolCallProposal, code: ToolErrorCode, message: str) -> ToolResult:
    return ToolResult(
        call_id=call.call_id,
        tool_name=call.tool_name,
        status=ToolStatus.ERROR,
        error_code=code,
        message=message,
    )


def map_workspace_error(exc: WorkspaceError) -> ToolErrorCode:
    return _ERROR_CODES.get(exc.code, ToolErrorCode.INTERNAL)


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"


def summarize_payload(result: ToolResult) -> str:
    if not result.payload:
        return result.status.value
    return clip(json.dumps(result.payload, ensure_ascii=False, default=str), 200)
