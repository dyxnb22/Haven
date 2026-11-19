"""工具执行层共享的结果类型和小型结果构造函数。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from haven.application.approval_cards import ToolPreview
from haven.application.state import RunContext
from haven.contracts.model import ToolCallProposal
from haven.contracts.process_output import ProcessReport
from haven.contracts.tools import AgentDelegateArgs, ToolArgs, ToolResult
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
    "syntax_error": ToolErrorCode.SYNTAX_ERROR,
    "internal": ToolErrorCode.INTERNAL,
}


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """工具执行的统一结果：成功结果、错误码和可展示摘要。"""

    #: 面向模型的结构化结果和追踪摘要。
    result: ToolResult
    #: 执行器无法证明副作用是否发生时为 True。
    effect_unknown: bool = False
    process: ProcessReport | None = None


ExecuteHandler = Callable[
    [RunContext, ToolCallProposal, ToolArgs, str, ToolPreview],
    Awaitable[ToolExecution],
]
DelegateHandler = Callable[
    [RunContext, ToolCallProposal, AgentDelegateArgs], Awaitable[ToolExecution]
]


def ok_result(
    call: ToolCallProposal, payload: dict[str, object], truncated: bool = False
) -> ToolResult:
    """构造成功的工具结果，并保留调用 ID、工具名和截断标记。"""
    return ToolResult(
        call_id=call.call_id,
        tool_name=call.tool_name,
        status=ToolStatus.OK,
        payload=payload,
        truncated=truncated,
    )


def error_result(call: ToolCallProposal, code: ToolErrorCode, message: str) -> ToolResult:
    """构造带稳定错误码和面向模型诊断文本的工具结果。"""
    return ToolResult(
        call_id=call.call_id,
        tool_name=call.tool_name,
        status=ToolStatus.ERROR,
        error_code=code,
        message=message,
    )


def map_workspace_error(exc: WorkspaceError) -> ToolErrorCode:
    """将工作区层错误码映射为工具协议中的稳定错误枚举。"""
    return _ERROR_CODES.get(exc.code, ToolErrorCode.INTERNAL)


def clip(text: str, limit: int) -> str:
    """将展示文本限制在指定字符数，并在截断时附加数量提示。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"


def clip_payload(value: object, limit: int) -> tuple[object, bool]:
    """递归地把结构化结果里过长的字符串截到 `limit`，返回（新值，是否截断过）。

    工具结果的结构化部分和展示文本一样会原样进入模型的 `<tool_output>`
    （`ToolResult.to_model_text` 把整个 payload 序列化进去），所以它同样需要上界。
    `mcp.*` 的 `raw` 就是活例子：那是一个完整的 JSON-RPC 结果，里面的正文与
    `content` 一样大——只截 `content` 的话这一步等于白做。
    """
    if isinstance(value, str):
        if len(value) <= limit:
            return value, False
        return clip(value, limit), True
    if isinstance(value, dict):
        clipped: dict[str, object] = {}
        truncated = False
        for key, item in value.items():
            new_item, did_truncate = clip_payload(item, limit)
            clipped[str(key)] = new_item
            truncated = truncated or did_truncate
        return clipped, truncated
    if isinstance(value, (list, tuple)):
        items: list[object] = []
        truncated = False
        for item in value:
            new_item, did_truncate = clip_payload(item, limit)
            items.append(new_item)
            truncated = truncated or did_truncate
        return (items if isinstance(value, list) else tuple(items)), truncated
    return value, False


def summarize_payload(result: ToolResult) -> str:
    """生成完成事件使用的短结果摘要，避免把完整 payload 写入事件。"""
    if not result.payload:
        return result.status.value
    return clip(json.dumps(result.payload, ensure_ascii=False, default=str), 200)
