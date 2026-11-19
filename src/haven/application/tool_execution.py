"""已授权工具的执行路由和统一完成事件。"""

from __future__ import annotations

import time
from pathlib import Path

from haven.application.approval_cards import ToolPreview
from haven.application.emitter import EventEmitter
from haven.application.mcp_client import MCPClientManager
from haven.application.skills import SkillCatalog
from haven.application.state import RunContext
from haven.application.tool_execution_types import (
    MODEL_PAYLOAD_CHARS,
    DelegateHandler,
    clip,
    clip_payload,
    error_result,
    ok_result,
    summarize_payload,
)
from haven.application.tool_execution_types import (
    ExecuteHandler as ExecuteHandler,
)
from haven.application.tool_execution_types import (
    ToolExecution as ToolExecution,
)
from haven.application.tool_mutations import MutationToolExecutor
from haven.application.tool_processes import ProcessToolExecutor
from haven.application.tool_readers import ReadToolExecutor
from haven.contracts.events import ToolCompleted
from haven.contracts.model import ToolCallProposal
from haven.contracts.process_output import ProcessReport
from haven.contracts.tools import RecipeSpec, ToolArgs, ToolResult
from haven.domain.enums import ToolErrorCode
from haven.ports.executor import ExecutorPort
from haven.ports.sandbox import SandboxLauncher
from haven.ports.session import SessionStorePort
from haven.ports.workspace import WorkspacePort


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
        skills: SkillCatalog | None = None,
        mcp_manager: MCPClientManager | None = None,
        delegate: DelegateHandler | None = None,
    ) -> None:
        readers = ReadToolExecutor(workspace, emitter, skills, delegate=delegate, store=store)
        mutations = MutationToolExecutor(workspace, store, emitter)
        self._mcp_manager = mcp_manager
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

    async def replace_scratch_dir(self, scratch_dir: Path) -> None:
        """把新运行的独占临时目录传递给进程工具。"""
        await self._processes.replace_scratch_dir(scratch_dir)

    async def close_tasks(self) -> None:
        """终止所有正在运行的后台子进程与 MCP Server。"""
        await self._processes.close_tasks()
        if self._mcp_manager is not None:
            await self._mcp_manager.close_all()

    async def execute_mcp(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        if self._mcp_manager is None:
            return ToolExecution(
                error_result(call, ToolErrorCode.UNKNOWN_TOOL, "no MCP manager configured")
            )
        raw_args = getattr(args, "arguments", {})
        res = await self._mcp_manager.call_tool(call.tool_name, raw_args)
        if res.get("is_error"):
            err_msg = str(res.get("error", "MCP tool execution failed"))
            # 错误对象直接来自外部 server 的 JSON-RPC 响应，大小不受我们控制；
            # `ToolResult.message` 的契约是"始终保持有界"。
            err_msg = clip(err_msg, MODEL_PAYLOAD_CHARS)
            err_code = ToolErrorCode.DENIED if res.get("drift") else ToolErrorCode.INTERNAL
            return ToolExecution(error_result(call, err_code, err_msg))

        # 与其余工具一致地给模型可见部分设上界。此前这里原样返回 server 的输出：
        # 一个回了 5 MB 文本的 MCP server 会把这 5 MB 直接塞进上下文，而 `repo.read`
        # 之类早就有 MODEL_PAYLOAD_CHARS 这道闸。`raw` 也要管——它是同一份正文的第二
        # 份拷贝（完整的 JSON-RPC 结果），只截 `content` 省不下什么。
        content = res.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        raw_payload, raw_truncated = clip_payload(res.get("raw", {}), MODEL_PAYLOAD_CHARS)
        return ToolExecution(
            ok_result(
                call,
                {
                    "content": clip(content, MODEL_PAYLOAD_CHARS),
                    "raw": raw_payload,
                },
                truncated=len(content) > MODEL_PAYLOAD_CHARS or raw_truncated,
            )
        )

    async def run_ticketed(
        self,
        ctx: RunContext,
        call: ToolCallProposal,
        args: ToolArgs,
        ticket_digest: str,
        preview: ToolPreview,
    ) -> ToolExecution:
        """把已通过审批的调用路由到对应处理器，并保留票据摘要用于审计。"""
        if call.tool_name.startswith("mcp."):
            return await self.execute_mcp(ctx, call, args, ticket_digest, preview)
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
        process: ProcessReport | None = None,
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
                process=process,
            ),
        )
        return ToolExecution(result=result, effect_unknown=effect_unknown, process=process)

    def describe_sandbox(self) -> str:
        """返回当前进程工具执行所使用的沙箱边界描述。"""
        return self._processes.describe_sandbox()
