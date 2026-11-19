"""受控 MCP（Model Context Protocol）客户端与网关管理器（ADR 0039）。

基于标准 JSON-RPC 2.0 stdio 协议通信，管理外部 MCP Server 生命周期：
- 白名单约束：仅暴露配置中显式允许的工具 (`allowed_tools`)；
- 命名空间隔离：转换为 `mcp.<server_name>.<tool_name>`；
- Schema 指纹校验：比对 SHA-256，防御静默 Schema 漂移提权；
- 进程优雅退出与孤儿清理。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import Any

from haven.contracts.mcp import MCPServerConfig, MCPToolSchema


class MCPError(Exception):
    """MCP 协议或通信故障。"""


class MCPSchemaDriftError(MCPError):
    """检测到外部 MCP Server 的参数模式与预先绑定的指纹不一致。"""


DEFAULT_MCP_TIMEOUT: float = 30.0

#: stdio 流的单行上限。asyncio 默认 64 KiB，对 MCP 来说太小：一次带回内容的
#: `tools/call`、一份大的 `inputSchema`、或一次 `resources/read`（其文本预算本身
#: 就是 64 KiB，再加 JSON 转义与 content 数组的包装，更容易越过）都会撞上它。
#: 抬到 8 MiB 让正常负载走 `readline()` 的正常路径，只有真的异常大的行才落到
#: 下面那条"报错并继续"的兜底分支。流量控制会在 2 倍上限处暂停传输，所以内存
#: 占用仍然是有界的。
STREAM_LINE_LIMIT: int = 8 * 1024 * 1024

#: 保留的 stderr 诊断行数上限。只用于把故障讲清楚，不该无限增长。
MAX_STDERR_LINES: int = 50


class MCPClient:
    """单个外部 MCP Server 的 stdio JSON-RPC 客户端。"""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._tools: dict[str, MCPToolSchema] = {}
        self._drifted_tools: set[str] = set()
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_lines: list[str] = []

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def last_stderr(self) -> str:
        """返回最近保留的 stderr 诊断行。"""
        return "\n".join(self._stderr_lines)

    async def _drain_stderr(self) -> None:
        """异步消费并排空 stderr 管道，防止管道缓冲区写满导致进程死锁挂起。"""
        if self._process is None or self._process.stderr is None:
            return
        while True:
            try:
                line = await self._process.stderr.readline()
            except asyncio.CancelledError:
                raise
            except OSError:
                break
            except ValueError:
                # 一行超过流的单行上限（默认 64 KiB，见 STREAM_LINE_LIMIT）。这不是
                # 通信故障，只是这一行长：记一个截断标记后继续读。若在这里退出，stderr
                # 管道就没人读了，一个继续写 stderr 的子进程会永远阻塞下去。
                #
                # 特意**不**去"把剩余部分读完"：`readline()` 抛 ValueError 时已经动过
                # 内部缓冲区了——换行符若在缓冲区内，整行（含换行符）已被删掉，流已经
                # 停在下一行的行首；若不在，缓冲区被清空，行的剩余部分还在管道里。
                # 两种情况从外部无法区分，而盲读要么把已经排好的下一行一起吞掉，要么在
                # 数据永不到来时永久阻塞。直接继续 `readline()` 两种情况下都能前进：
                # 前者读到下一行，后者读到这一行的尾巴（一行无害的诊断）。
                self._remember_stderr("<stderr line exceeded the stream limit; truncated>")
                continue
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self._remember_stderr(text)

    def _remember_stderr(self, text: str) -> None:
        """记一行诊断，并把长度压在有界范围内。"""
        self._stderr_lines.append(text)
        if len(self._stderr_lines) > MAX_STDERR_LINES:
            self._stderr_lines.pop(0)

    async def start(self) -> None:
        """启动外部进程并完成标准 MCP 握手协议 (initialize -> initialized)。"""
        env = os.environ.copy()
        env.update(self.config.env)

        self._process = await asyncio.create_subprocess_exec(
            self.config.command,
            *self.config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=STREAM_LINE_LIMIT,
        )

        if self._process.stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr())

        # 1. 发送 initialize 请求
        init_res = await self._send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "haven", "version": "1.0"},
            },
        )
        if "error" in init_res:
            raise MCPError(f"server {self.config.name} rejected initialize: {init_res['error']}")

        # 2. 发送 initialized 通知
        await self._send_notification("notifications/initialized", {})

    async def discover_tools(self) -> list[MCPToolSchema]:
        """向服务端请求工具列表，并应用白名单过滤与 Schema 指纹比对。"""
        res = await self._send_request("tools/list", {})
        if "error" in res:
            raise MCPError(f"failed to list tools from {self.config.name}: {res['error']}")

        raw_tools = res.get("result", {}).get("tools", [])
        discovered: list[MCPToolSchema] = []

        for item in raw_tools:
            tool_name = item.get("name", "")
            if not tool_name:
                continue

            # 核心安全：白名单过滤（未声明的工具一律忽略）
            if tool_name not in self.config.allowed_tools:
                continue

            desc = item.get("description", "")
            schema = item.get("inputSchema", {})
            is_read_only = tool_name in self.config.read_only_tools

            tool_obj = MCPToolSchema.create(
                server_name=self.config.name,
                tool_name=tool_name,
                description=desc,
                parameters=schema,
                is_read_only=is_read_only,
            )

            # 核心安全：Schema 指纹校验（防范 Schema 漂移）
            pinned_digest = self.config.pinned_schemas.get(tool_name)
            if pinned_digest and pinned_digest != tool_obj.schema_digest:
                self._drifted_tools.add(tool_obj.namespaced_name)
            else:
                self._drifted_tools.discard(tool_obj.namespaced_name)

            self._tools[tool_obj.namespaced_name] = tool_obj
            discovered.append(tool_obj)

        return discovered

    def is_drifted(self, namespaced_name: str) -> bool:
        return namespaced_name in self._drifted_tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """调用外部 MCP 工具并返回结构化输出。"""
        namespaced = f"mcp.{self.config.name}.{tool_name}"
        if self.is_drifted(namespaced):
            raise MCPSchemaDriftError(
                f"tool {namespaced} schema digest drifted from pinned configuration"
            )

        res = await self._send_request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
        )

        if "error" in res:
            return {
                "is_error": True,
                "error": res["error"],
            }

        result_payload = res.get("result", {})
        is_error = result_payload.get("isError", False)
        content_items = result_payload.get("content", [])

        # 汇总文本内容
        text_pieces = []
        for c in content_items:
            if isinstance(c, dict) and c.get("type") == "text":
                text_pieces.append(c.get("text", ""))

        output_text = "\n".join(text_pieces) if text_pieces else json.dumps(result_payload)
        return {
            "is_error": is_error,
            "content": output_text,
            "raw": result_payload,
        }

    async def read_resource(self, uri: str, timeout: float = DEFAULT_MCP_TIMEOUT) -> dict[str, Any]:
        """从外部受控 MCP Server 读取指定 URI 的资源内容（ADR 规划书 Phase P4）。"""
        res = await self._send_request(
            "resources/read",
            {"uri": uri},
            timeout=timeout,
        )
        if "error" in res:
            err = res["error"]
            raise MCPError(f"server {self.config.name} failed to read resource {uri}: {err}")
        result = res.get("result")
        return result if isinstance(result, dict) else {}

    async def _send_request(
        self, method: str, params: dict[str, Any], timeout: float = DEFAULT_MCP_TIMEOUT
    ) -> dict[str, Any]:
        if (
            not self.is_running
            or self._process is None
            or self._process.stdin is None
            or self._process.stdout is None
        ):
            raise MCPError(f"server {self.config.name} is not running")

        self._request_id += 1
        expected_id = self._request_id
        msg = {
            "jsonrpc": "2.0",
            "id": expected_id,
            "method": method,
            "params": params,
        }

        line = json.dumps(msg) + "\n"
        self._process.stdin.write(line.encode("utf-8"))
        await self._process.stdin.drain()

        # 等待**属于本次请求**的响应行（有界超时，防止外部 MCP Server 阻塞整机代理）。
        # 必须按 id 匹配，而不能"读到一行就算"：JSON-RPC 允许服务端在请求在途时主动
        # 发通知（`notifications/progress` 等，没有 id），那种行没有 result，把它当
        # 响应会让本次调用拿到空结果；更糟的是超时放弃的请求，其真实响应仍留在管道
        # 里，下一次调用会读到它——从此每次调用都错位一格，且不报任何错误。
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise MCPError(
                    f"server {self.config.name} timed out after {timeout}s waiting for {method}"
                )
            try:
                resp_line = await asyncio.wait_for(self._process.stdout.readline(), remaining)
            except TimeoutError as exc:
                raise MCPError(
                    f"server {self.config.name} timed out after {timeout}s waiting for {method}"
                ) from exc
            except ValueError as exc:
                # 一行超过流的单行上限（见 _drain_stderr 里对两种成因的说明）。这次
                # 请求的响应已经拿不到了，所以报错；但不盲读补救——那会把下一个响应
                # 吞掉。由 MCPClientManager 决定是关掉这个 server 还是把错误还给模型。
                raise MCPError(
                    f"server {self.config.name} sent a line longer than the stream limit "
                    f"while answering {method}"
                ) from exc

            if not resp_line:
                raise MCPError(f"server {self.config.name} closed stdout unexpectedly")

            try:
                data = json.loads(resp_line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as exc:
                raise MCPError(f"invalid json-rpc from {self.config.name}: {exc}") from exc
            if not isinstance(data, dict):
                raise MCPError(f"invalid json-rpc from {self.config.name}: expected object")

            response_id = data.get("id")
            if response_id is None:
                # 服务端通知：与本请求无关，丢弃后继续等。
                continue
            if response_id != expected_id:
                raise MCPError(
                    f"server {self.config.name} answered request {expected_id} "
                    f"with a response for {response_id!r}; the session is desynchronized"
                )
            return data

    async def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        if not self.is_running or self._process is None or self._process.stdin is None:
            return
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        line = json.dumps(msg) + "\n"
        self._process.stdin.write(line.encode("utf-8"))
        await self._process.stdin.drain()

    async def close(self) -> None:
        """优雅关闭子进程并取消 stderr 监控任务。"""
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
            self._stderr_task = None

        if self._process is None or not self.is_running:
            return
        with contextlib.suppress(ProcessLookupError, OSError):
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=1.5)
            except TimeoutError:
                self._process.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._process.wait(), timeout=1.0)


class MCPClientManager:
    """管理本次运行加载的所有 MCP Server 实例与全局工具路由。"""

    def __init__(self, configs: tuple[MCPServerConfig, ...] = ()) -> None:
        self._configs = configs
        self._clients: dict[str, MCPClient] = {}
        self._tools: dict[str, MCPToolSchema] = {}
        self._failures: list[tuple[str, str]] = []

    @property
    def tools(self) -> tuple[MCPToolSchema, ...]:
        return tuple(self._tools.values())

    @property
    def failures(self) -> tuple[tuple[str, str], ...]:
        """启动失败的 server 及其原因，按配置顺序。

        一个配了却连不上的 MCP server 必须让人知道：用户是显式配它的，静默跳过会让
        "我明明加了工具却不存在"变成无从下手的问题。原来的 `except Exception:
        continue` 把连接拒绝、命令不存在、握手失败全都吞掉了。
        """
        return tuple(self._failures)

    def get_tool(self, namespaced_name: str) -> MCPToolSchema | None:
        return self._tools.get(namespaced_name)

    def is_drifted(self, namespaced_name: str) -> bool:
        tool = self.get_tool(namespaced_name)
        if not tool:
            return False
        client = self._clients.get(tool.server_name)
        return client.is_drifted(namespaced_name) if client else False

    async def initialize(self) -> tuple[MCPToolSchema, ...]:
        """连接所有已配置的 MCP 服务并发现工具。"""
        if self._clients:
            return tuple(self._tools.values())
        discovered: list[MCPToolSchema] = []
        for cfg in self._configs:
            client = MCPClient(cfg)
            try:
                await client.start()
                tools = await client.discover_tools()
                self._clients[cfg.name] = client
                for t in tools:
                    self._tools[t.namespaced_name] = t
                    discovered.append(t)
            except Exception as exc:
                # 不因为一个 server 失败就中止整次运行，但也不再静默：原因记在
                # `failures` 里，由调用方发一条 Notice 让用户看见。
                await client.close()
                self._failures.append((cfg.name, f"{type(exc).__name__}: {exc}"))
                continue
        return tuple(discovered)

    async def call_tool(self, namespaced_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self.get_tool(namespaced_name)
        if not tool:
            return {"is_error": True, "error": f"unknown MCP tool {namespaced_name!r}"}

        client = self._clients.get(tool.server_name)
        if not client or not client.is_running:
            return {"is_error": True, "error": f"MCP server {tool.server_name!r} is not running"}

        try:
            return await client.call_tool(tool.tool_name, arguments)
        except MCPSchemaDriftError as exc:
            return {"is_error": True, "error": f"schema drift detected: {exc}", "drift": True}
        except Exception as exc:
            return {"is_error": True, "error": f"MCP call failed: {exc}"}

    async def close_all(self) -> None:
        """关闭所有运行中的 MCP Server 进程。"""
        for client in list(self._clients.values()):
            with contextlib.suppress(Exception):
                await client.close()
        self._clients.clear()
        self._tools.clear()
