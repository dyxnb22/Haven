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


class MCPClient:
    """单个外部 MCP Server 的 stdio JSON-RPC 客户端。"""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._tools: dict[str, MCPToolSchema] = {}
        self._drifted_tools: set[str] = set()

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

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
        )

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
        msg = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }

        line = json.dumps(msg) + "\n"
        self._process.stdin.write(line.encode("utf-8"))
        await self._process.stdin.drain()

        # 等待响应行（有界超时，防止外部 MCP Server 阻塞整机代理）
        try:
            resp_line = await asyncio.wait_for(self._process.stdout.readline(), timeout=timeout)
        except TimeoutError as exc:
            raise MCPError(
                f"server {self.config.name} timed out after {timeout}s waiting for {method}"
            ) from exc

        if not resp_line:
            raise MCPError(f"server {self.config.name} closed stdout unexpectedly")

        try:
            data = json.loads(resp_line.decode("utf-8", errors="replace"))
            if not isinstance(data, dict):
                raise MCPError(f"invalid json-rpc from {self.config.name}: expected object")
            return data
        except json.JSONDecodeError as exc:
            raise MCPError(f"invalid json-rpc from {self.config.name}: {exc}") from exc

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
        """优雅关闭子进程。"""
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

    @property
    def tools(self) -> tuple[MCPToolSchema, ...]:
        return tuple(self._tools.values())

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
            except Exception:
                await client.close()
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
