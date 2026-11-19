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

#: `resources/read` 一次最多返回多少个事件。事件流本身无上界，而 Haven 自己的客户端
#: 会拒绝超过 8 MiB 的响应行——不截断的话发出去也读不回来。
MAX_EVENTS_PER_READ = 500


class MCPServer:
    """处理标准 MCP JSON-RPC 请求并协调底层 HavenRuntime。"""

    def __init__(self, runtime: HavenRuntime, *, read_only: bool = True) -> None:
        self._runtime = runtime
        # 服务器实际以什么模式运行。它必须体现在对外声明里：外部客户端选工具全靠
        # 名称 + inputSchema + description，这是它唯一的信号。
        self._read_only = read_only
        # 会话存储是**全局**的（~/Library/Application Support/haven/haven.db，所有工作区
        # 共用），而 RunRecord 带着 workspace_digest。不收窄范围的话，`resources/list`
        # 会把机器上每个工作区最近 10 次运行的 id 白送出去，而根资源返回的 checkpoint
        # 含完整对话与文件内容。`search_runs` 本来就接受 workspace_digest，这里用上它。
        self._workspace_digest = runtime.workspace_digest
        # 串行化"会占用单活跃运行名额"的请求；其余请求可并发，好让 cancel/ping 能插队。
        self._run_lock = asyncio.Lock()
        self._inflight: set[asyncio.Task[None]] = set()

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
                # 让客户端能发现本次连接的有效权限，而不是只能从描述文字里猜。
                "haven": {"readOnly": self._read_only},
            },
        }

    async def _dispatch(self, req: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        """处理一个请求并把响应写回去；失败不影响读循环。"""
        try:
            method = req.get("method")
            params = req.get("params") or {}
            tool = params.get("name") if isinstance(params, dict) else None
            # 只有这两个工具会启动或延续一次运行，需要串行（RunService 的
            # `_active_run_id` 是单数，没有并发守卫）；其余请求——ping、cancel、
            # resources/*——可以随时插进来。
            needs_run_slot = method == "tools/call" and tool in (
                "haven.run_readonly",
                "haven.continue_readonly",
            )
            if needs_run_slot:
                async with self._run_lock:
                    resp = await self.handle_request(req)
            else:
                resp = await self.handle_request(req)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 处理器自身的意外也不能把读循环带走；能拿到 id 就按 id 回一个错误。
            req_id = req.get("id") if isinstance(req, dict) else None
            if req_id is None:
                return
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": "Internal error"},
            }
        if resp is not None:
            await _write_json(writer, resp)

    def _handle_tools_list(self, req_id: Any) -> dict[str, Any]:
        # 描述必须与实际授予的能力一致。`haven mcp serve --write` 会以 INTERACTIVE 模式
        # 加 AutoApprover("approve_all") 运行，此时若照旧声称 "Cannot modify files"，
        # 客户端就是凭一句假话把整个工作区的写权限交出去——而它无从发现真相。
        if self._read_only:
            run_desc = (
                "Run a bounded, auditable, read-only session in Haven on the fixed workspace. "
                "Cannot modify files or escape sandbox. "
                "Returns structured outcome and resource URI."
            )
        else:
            run_desc = (
                "Run a bounded, auditable session in Haven on the fixed workspace. "
                "THIS SERVER WAS STARTED WITH --write: the session may modify files in the "
                "workspace, and every approval is granted automatically with no human in the "
                "loop. Treat it as fully privileged. "
                "Returns structured outcome and resource URI."
            )
        tools = [
            {
                "name": "haven.run_readonly",
                "description": run_desc,
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
                # 按 tools/list 里声明的 inputSchema 校验：minLength/maxLength 与类型。
                # 只查真值会让 `skill_names: "abc"` 被当成三个单字符技能名，
                # 超长 goal 则把内部错误消息当 schema 违规返回。
                if not goal or not isinstance(goal, str) or not (3 <= len(goal) <= 4000):
                    return self._tool_error(req_id, "Invalid or missing 'goal' parameter")
                names = arguments.get("skill_names") or ()
                if isinstance(names, str) or not isinstance(names, (list, tuple)):
                    return self._tool_error(req_id, "Invalid 'skill_names' parameter")
                if not all(isinstance(name, str) for name in names):
                    return self._tool_error(req_id, "Invalid 'skill_names' parameter")
                skill_names = tuple(names)
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
        except Exception:
            # 不回显异常载荷：那是信任边界，而 `feedback.py` 的既定做法是给固定建议。
            # 原样反射会把本地技能目录、环境变量名、sqlite 路径和绝对路径一起送出去。
            return self._tool_error(req_id, "Execution failed; see the Haven logs for details")

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
        runs = await self._runtime.store.search_runs(
            "", limit=10, workspace_digest=self._workspace_digest
        )
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
        # 存储是全局的：别的跑在同一个库里的工作区，其运行对本次连接不可见。
        if run_rec.workspace_digest != self._workspace_digest:
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
            # 有界：`load_events` 会返回整条事件流，而 ModelCompleted.text 是无界字符串。
            # 不设上限的话，任何客户端都能让这个长驻进程反复做一次任意大的分配。
            events = await self._runtime.store.load_events(run_id)
            dumped = [e.event.model_dump(mode="json") for e in events[-MAX_EVENTS_PER_READ:]]
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
            # 真diff：`repo.diff` 是只读工具，只读运行（这里的常态）照样会产生差异。
            # 此前这里恒返回空串，客户端无法区分"这次运行没改任何东西"和"这个资源没实现"。
            diff = await self._runtime.run_service.get_diff(run_id)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"contents": [{"uri": uri, "mimeType": "text/plain", "text": diff}]},
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
        """从 reader 逐行读取 JSON-RPC 消息，处理并向 writer 输出响应。

        这里是信任边界：对端是不可信的外部客户端。三种输入都曾让异常直接逃出本方法，
        顺着 `run_mcp_server` 的 finally 掀翻整个 server、丢掉在飞的运行——

        - 非 UTF-8 字节（`.decode("utf-8")` 抛 UnicodeDecodeError）；
        - 超过 asyncio 流默认 64 KiB 上限的一行（`readline()` 抛 ValueError）；
        - `params` 不是对象，或整个消息是 JSON-RPC 批量数组（`req.get(...)` 抛
          AttributeError）。

        Haven 自己的 MCP **客户端**这三样全防（见 `application/mcp_client.py`），
        server 不该比它脆。现在逐条转成 JSON-RPC 错误响应，连接继续。
        """
        while True:
            try:
                line = await reader.readline()
            except ValueError:
                # 一行超过流的单行上限。**不要**去"把剩余部分读完"：`readline()` 抛
                # ValueError 时已经动过内部缓冲区了——换行符若在缓冲区内，整行已被删掉；
                # 若不在，缓冲区被清空、剩余部分还在流里。两种情况从外部无法区分，而按
                # 原始块盲读会把已经排好的下一行一起吞掉。直接继续：下一次 readline()
                # 要么读到下一行，要么读到这一行的尾巴（那会是一次 parse error，诚实）。
                await _write_json(
                    writer,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": "line exceeded the size limit"},
                    },
                )
                continue
            if not line:
                break
            try:
                line_str = line.decode("utf-8").strip()
            except UnicodeDecodeError:
                await _write_json(
                    writer,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    },
                )
                continue
            if not line_str:
                continue

            try:
                req = json.loads(line_str)
            except Exception:
                await _write_json(
                    writer,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    },
                )
                continue

            # 批量数组与畸形 params：JSON-RPC 2.0 的 Request 必须是对象。
            # 两处顺序都不能反：`isinstance(req, dict)` 必须先判（批量数组时
            # `req.get` 自己就会抛），而 `params` 不能写 `or {}`——空列表为假，
            # 会被换成 {} 从而绕过检查，随后 `params.get(...)` 抛 AttributeError。
            if not isinstance(req, dict):
                malformed = True
            else:
                raw_params = req.get("params")
                malformed = raw_params is not None and not isinstance(raw_params, dict)
            if malformed:
                await _write_json(
                    writer,
                    {
                        "jsonrpc": "2.0",
                        "id": req.get("id") if isinstance(req, dict) else None,
                        "error": {"code": -32600, "message": "Invalid Request"},
                    },
                )
                continue

            # 每个请求各自跑在一个 task 里，读循环永不堵塞。
            #
            # 此前 `await self.handle_request(req)` 是串行的：一次 `haven.run_readonly`
            # 会把 `serve_stdio` 堵在整个运行期间，之后所有请求——包括 `haven.cancel_run`
            # 和 `ping`——都躺在 StreamReader 里没人读。广告里的取消工具因此在唯一可用的
            # 传输上永远是死代码，带活性超时的 MCP 客户端还会在运行中途把 server 拆掉。
            task = asyncio.create_task(self._dispatch(req, writer))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

        # 对端断开前把在途请求排空：响应是从各自的 task 写回的，不排就会丢。
        if self._inflight:
            await asyncio.gather(*list(self._inflight), return_exceptions=True)


async def _write_json(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    """写一行 JSON 并 flush；写失败（对端已断开）时安静结束。"""
    try:
        writer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        await writer.drain()
    except (ConnectionError, OSError):
        # 对端已经走了：这不是错误，只是没有人在听。
        pass


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

        server = MCPServer(services.runtime, read_only=read_only)
        await server.serve_stdio(reader, writer)
    finally:
        await services.close()
