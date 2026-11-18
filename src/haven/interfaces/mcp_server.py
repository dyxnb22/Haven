"""Haven MCP (Model Context Protocol) Server 实现（ADR / 规划书 P3）。

通过标准 stdio JSON-RPC 2.0 提供有界、只读、可恢复的 Haven 执行内核：
- Tools:
  - haven.run_readonly: 启动有界只读运行
  - haven.continue_readonly: 基于既有运行检查点延续后续轮次
  - haven.cancel_run: 终止指定运行
- Resources:
  - haven://runs/{run_id}: 运行状态、停止原因与用量摘要
  - haven://runs/{run_id}/events: 持久化审计事件流
  - haven://runs/{run_id}/diff: 运行差异
  - haven://runs/{run_id}/evidence: 证据账本
  - haven://runs/{run_id}/export: 导出快照
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from haven.application.approvals import AutoApprover
from haven.application.runtime import HavenRuntime
from haven.bootstrap import build_services
from haven.domain.enums import PermissionMode

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "haven"
SERVER_VERSION = "0.1.0"


class MCPServer:
    """处理标准 MCP JSON-RPC 请求并协调底层 HavenRuntime。"""

    def __init__(self, runtime: HavenRuntime) -> None:
        self._runtime = runtime

    async def handle_request(self, req: dict[str, Any]) -> dict[str, Any] | None:
        """分发并处理单个 JSON-RPC 2.0 请求。"""
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        # 通知类消息（没有 id）不回复
        if req_id is None:
            return None

        if method == "initialize":
            return self._handle_initialize(req_id, params)
        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "tools/list":
            return self._handle_tools_list(req_id)
        if method == "tools/call":
            return await self._handle_tools_call(req_id, params)
        if method == "resources/list":
            return await self._handle_resources_list(req_id)
        if method == "resources/read":
            return await self._handle_resources_read(req_id, params)

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    def _handle_initialize(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                },
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    def _handle_tools_list(self, req_id: Any) -> dict[str, Any]:
        tools = [
            {
                "name": "haven.run_readonly",
                "description": (
                    "Run a bounded, auditable, read-only session in Haven on the fixed workspace. "
                    "Cannot modify files or escape sandbox. "
                    "Returns structured outcome and resource URI."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "goal": {
                            "type": "string",
                            "description": "Natural language goal for the agent.",
                            "minLength": 3,
                            "maxLength": 4000,
                        },
                        "skill_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional skill names to select for the run.",
                        },
                    },
                    "required": ["goal"],
                },
            },
            {
                "name": "haven.continue_readonly",
                "description": (
                    "Continue an existing Haven session turn with a follow-up "
                    "request in read-only mode."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "run_id": {
                            "type": "string",
                            "description": "Previous run_id to continue.",
                        },
                        "follow_up": {
                            "type": "string",
                            "description": "Follow-up request text.",
                            "minLength": 3,
                            "maxLength": 4000,
                        },
                        "skill_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional skill names to select.",
                        },
                    },
                    "required": ["run_id", "follow_up"],
                },
            },
            {
                "name": "haven.cancel_run",
                "description": "Cancel an active running Haven session by run_id.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "run_id": {
                            "type": "string",
                            "description": "The run_id of the active session to cancel.",
                        }
                    },
                    "required": ["run_id"],
                },
            },
        ]
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}

    async def _handle_tools_call(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}

        try:
            if tool_name == "haven.run_readonly":
                goal = arguments.get("goal")
                if not goal or not isinstance(goal, str):
                    return self._tool_error(req_id, "Missing or invalid 'goal' parameter")
                skill_names = arguments.get("skill_names") or ()
                outcome = await self._runtime.start_run(goal, skill_names=skill_names)
                data = {
                    "run_id": outcome.run_id,
                    "status": outcome.status.value,
                    "stop_reason": outcome.stop_reason.value,
                    "final_text": outcome.final_text,
                    "resource_uri": f"haven://runs/{outcome.run_id}",
                }
                return self._tool_result(req_id, data)

            if tool_name == "haven.continue_readonly":
                run_id = arguments.get("run_id")
                follow_up = arguments.get("follow_up")
                if not run_id or not follow_up:
                    return self._tool_error(req_id, "Missing 'run_id' or 'follow_up' parameter")
                skill_names = arguments.get("skill_names") or ()
                outcome = await self._runtime.continue_run(
                    run_id, follow_up, skill_names=skill_names
                )
                data = {
                    "run_id": outcome.run_id,
                    "status": outcome.status.value,
                    "stop_reason": outcome.stop_reason.value,
                    "final_text": outcome.final_text,
                    "resource_uri": f"haven://runs/{outcome.run_id}",
                }
                return self._tool_result(req_id, data)

            if tool_name == "haven.cancel_run":
                run_id = arguments.get("run_id")
                if not run_id:
                    return self._tool_error(req_id, "Missing 'run_id' parameter")
                cancelled = await self._runtime.cancel(run_id)
                return self._tool_result(req_id, {"run_id": run_id, "cancelled": cancelled})

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": f"Unknown tool: {tool_name}"},
            }
        except Exception as exc:
            return self._tool_error(req_id, f"Execution failed: {exc}")

    def _tool_result(self, req_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": text}], "isError": False},
        }

    def _tool_error(self, req_id: Any, error_msg: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": error_msg}], "isError": True},
        }

    async def _handle_resources_list(self, req_id: Any) -> dict[str, Any]:
        runs = await self._runtime.store.list_runs(limit=10)
        resources: list[dict[str, Any]] = []
        for r in runs:
            resources.extend(
                [
                    {
                        "uri": f"haven://runs/{r.run_id}",
                        "name": f"Run {r.run_id} Overview",
                        "description": f"Status and metrics for run {r.run_id}",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": f"haven://runs/{r.run_id}/events",
                        "name": f"Run {r.run_id} Events",
                        "description": f"Auditable event stream for run {r.run_id}",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": f"haven://runs/{r.run_id}/diff",
                        "name": f"Run {r.run_id} Diff",
                        "description": f"Diff changes for run {r.run_id}",
                        "mimeType": "text/plain",
                    },
                    {
                        "uri": f"haven://runs/{r.run_id}/evidence",
                        "name": f"Run {r.run_id} Evidence",
                        "description": f"Evidence ledger summary for run {r.run_id}",
                        "mimeType": "application/json",
                    },
                ]
            )
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": resources}}

    async def _handle_resources_read(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        uri = params.get("uri") or ""
        if not uri.startswith("haven://runs/"):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": f"Unsupported resource URI: {uri}"},
            }

        remainder = uri.removeprefix("haven://runs/").strip("/")
        parts = remainder.split("/")
        run_id = parts[0]
        sub = parts[1] if len(parts) > 1 else ""

        run_rec = await self._runtime.store.get_run(run_id)
        if run_rec is None:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": f"Run not found: {run_id}"},
            }

        if not sub:
            checkpoint = await self._runtime.get_checkpoint(run_id)
            body = {
                "run_id": run_rec.run_id,
                "goal": run_rec.goal,
                "status": run_rec.status.value,
                "stop_reason": run_rec.stop_reason,
                "created_at": run_rec.created_at,
                "updated_at": run_rec.updated_at,
                "checkpoint": checkpoint.model_dump(mode="json") if checkpoint else None,
            }
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": json.dumps(body, ensure_ascii=False, indent=2),
                        }
                    ]
                },
            }

        if sub == "events":
            events = await self._runtime.store.load_events(run_id)
            dumped = [e.event.model_dump(mode="json") for e in events]
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": json.dumps(dumped, ensure_ascii=False, indent=2),
                        }
                    ]
                },
            }

        if sub == "evidence":
            checkpoint = await self._runtime.get_checkpoint(run_id)
            evidence = checkpoint.evidence.model_dump(mode="json") if checkpoint else {}
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": json.dumps(evidence, ensure_ascii=False, indent=2),
                        }
                    ]
                },
            }

        if sub == "diff":
            # 只读模式通常无修改
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"contents": [{"uri": uri, "mimeType": "text/plain", "text": ""}]},
            }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": f"Unknown resource subpath: {sub}"},
        }

    async def serve_stdio(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """从 reader 逐行读取 JSON-RPC 消息，处理并向 writer 输出响应。"""
        while True:
            line = await reader.readline()
            if not line:
                break
            line_str = line.decode("utf-8").strip()
            if not line_str:
                continue

            try:
                req = json.loads(line_str)
            except Exception:
                err_resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error"},
                }
                writer.write(json.dumps(err_resp).encode("utf-8") + b"\n")
                await writer.drain()
                continue

            resp = await self.handle_request(req)
            if resp is not None:
                writer.write(json.dumps(resp, ensure_ascii=False).encode("utf-8") + b"\n")
                await writer.drain()


async def run_mcp_server(
    workspace: Path,
    *,
    read_only: bool = True,
    store_path: Path | None = None,
) -> None:
    """初始化并运行 MCP Server（stdio 传输）。"""
    mode = PermissionMode.READ_ONLY if read_only else PermissionMode.INTERACTIVE
    approvals = AutoApprover("reject_all") if read_only else AutoApprover("approve_all")
    services = await build_services(
        workspace,
        mode=mode,
        approvals=approvals,
        sinks=[],
        store_path=store_path,
    )
    try:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        write_transport, write_protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout
        )
        writer = asyncio.StreamWriter(write_transport, write_protocol, reader, loop)

        server = MCPServer(services.runtime)
        await server.serve_stdio(reader, writer)
    finally:
        await services.close()
