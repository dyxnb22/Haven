"""已授权工具的执行路由和统一完成事件。"""

from __future__ import annotations

import time
from pathlib import Path

from haven.application.approval_cards import ToolPreview
from haven.application.emitter import EventEmitter
from haven.application.state import RunContext
from haven.application.tool_execution_types import (
    ExecuteHandler as ExecuteHandler,
)
from haven.application.tool_execution_types import (
    ToolExecution as ToolExecution,
)
from haven.application.tool_execution_types import (
    clip,
    error_result,
    map_workspace_error,
    summarize_payload,
)
from haven.application.tool_mutations import MutationToolExecutor
from haven.application.tool_processes import ProcessToolExecutor
from haven.application.tool_readers import ReadToolExecutor
from haven.contracts.events import ToolCompleted
from haven.contracts.model import ToolCallProposal
from haven.contracts.tools import RecipeSpec, ToolArgs, ToolResult
from haven.domain.enums import ToolErrorCode
from haven.ports.executor import ExecutorPort
from haven.ports.sandbox import SandboxLauncher
from haven.ports.session import SessionStorePort
from haven.ports.workspace import WorkspacePort

# 兼容别名：历史上有调用方从本模块导入这些辅助函数。
_clip = clip
_error = error_result
_map_ws_code = map_workspace_error


class ToolExecutor:
    """使用执行票据调用具体工具处理器，并负责统一错误映射。

    组合按职责分组的工具执行器，并提供单一分发入口。
    """

    def __init__(
        self,
        *,
        workspace: WorkspacePort,
        executor: ExecutorPort,
        store: SessionStorePort,
        emitter: EventEmitter,
        recipes: dict[str, RecipeSpec],
        launcher: SandboxLauncher | None,
        scratch_dir: Path,
    ) -> None:
        readers = ReadToolExecutor(workspace, emitter)
        mutations = MutationToolExecutor(workspace, store, emitter)
        self._processes = ProcessToolExecutor(
            workspace=workspace,
            executor=executor,
            store=store,
            emitter=emitter,
            recipes=recipes,
            launcher=launcher,
            scratch_dir=scratch_dir,
        )
        self._emitter = emitter
        self.handlers: dict[str, ExecuteHandler] = {
            **readers.handlers,
            **mutations.handlers,
            **self._processes.handlers,
        }

    def replace_scratch_dir(self, scratch_dir: Path) -> None:
        """把新运行的独占临时目录传递给进程工具。"""
        self._processes.replace_scratch_dir(scratch_dir)

    async def run_ticketed(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        """把已通过审批的调用路由到对应处理器，并保留票据摘要用于审计。"""
        handler = self.handlers.get(call.tool_name)
        if handler is None:  # pragma: no cover - 注册表连线测试覆盖全部键集合
            return ToolExecution(
                error_result(
                    call, ToolErrorCode.UNKNOWN_TOOL, f"no executor for {call.tool_name!r}"
                )
            )
        return await handler(ctx, call, args, ticket_digest, preview)

    async def finish(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        result: ToolResult,
        started: float,
        effect_unknown: bool = False,
    ) -> ToolExecution:
        """补充执行耗时、发出统一完成事件并返回最终执行结果。"""
        duration_ms = int((time.monotonic() - started) * 1000)
        result = result.model_copy(update={"duration_ms": duration_ms})
        await self._emitter.emit(
            ctx.run_id,
            ToolCompleted(
                run_id=ctx.run_id,
                call_id=call.call_id,
                tool_name=call.tool_name,
                status=result.status.value,
                error_code=result.error_code.value if result.error_code else "",
                summary=clip(result.message or summarize_payload(result), 200),
                truncated=result.truncated,
                duration_ms=duration_ms,
            ),
        )
        return ToolExecution(result=result, effect_unknown=effect_unknown)

    def describe_sandbox(self) -> str:
        """返回当前进程工具执行所使用的沙箱边界描述。"""
        return self._processes.describe_sandbox()
