"""`haven mcp`：受控 MCP Server 检查与 Schema 指纹固定（ADR 0039）。"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import typer

from haven.config import load_config
from haven.contracts.mcp import MCPToolSchema
from haven.interfaces.cli_support.common import EXIT_OK, EXIT_USAGE


def mcp(
    action: str = typer.Argument("list", help="Action: list, inspect, pin, serve."),
    server_name: str | None = typer.Argument(
        None, help="MCP server name from config (required for inspect, pin)."
    ),
    workspace: Path = typer.Option(Path("."), "--workspace", "-w", help="Workspace root."),
    stdio: bool = typer.Option(True, "--stdio", help="Serve via stdio JSON-RPC 2.0 (for serve)."),  # noqa: ARG001
    read_only: bool = typer.Option(
        True, "--read-only/--write", help="Enforce read-only execution (default true)."
    ),
    store_path: Path | None = typer.Option(
        None, "--store-path", help="Path to sqlite store database."
    ),
) -> None:
    """管理与检查受控 MCP 服务器：查看已配置服务、检查可用工具与自动计算/固定 Schema 指纹。"""
    ws = workspace.resolve()
    config = load_config(ws)
    servers = {s.name: s for s in config.mcp_servers}

    if action == "list":
        typer.echo(f"Registered MCP Servers ({len(servers)} configured):\n")
        if not servers:
            typer.echo("  (no MCP servers configured in user or project config)")
            typer.echo(
                "  Configure one under [mcp.servers.<name>] in .haven.toml or ~/.haven/config.toml"
            )
            raise typer.Exit(EXIT_OK)

        for name, s in servers.items():
            typer.echo(f"• {name}:")
            cmd_preview = f"{s.command} {' '.join(s.args)}".strip()
            typer.echo(f"    Command:        {cmd_preview}")
            allowed = ", ".join(s.allowed_tools) if s.allowed_tools else "(none - fail-closed)"
            typer.echo(f"    Allowed tools:  {allowed}")
            ro_tools = ", ".join(s.read_only_tools) if s.read_only_tools else "(none)"
            typer.echo(f"    Read-only:      {ro_tools}")
            typer.echo(f"    Pinned schemas: {len(s.pinned_schemas)} tool(s)")
            for t_name, digest in s.pinned_schemas.items():
                typer.echo(f"      - {t_name}: {digest[:16]}...")
            typer.echo("")
        raise typer.Exit(EXIT_OK)

    if action in ("inspect", "pin"):
        if not server_name:
            typer.echo(
                f"error: `haven mcp {action}` requires server_name, "
                f"e.g. `haven mcp {action} my_server`"
            )
            raise typer.Exit(EXIT_USAGE)

        srv = servers.get(server_name)
        if not srv:
            typer.echo(f"error: MCP server {server_name!r} not found in configuration.")
            typer.echo(f"Available servers: {', '.join(servers.keys()) if servers else 'none'}")
            raise typer.Exit(EXIT_USAGE)

        from haven.application.mcp_client import MCPClient

        client = MCPClient(srv)

        async def _run() -> None:
            typer.echo(f"Connecting to MCP server '{server_name}'...")
            try:
                await client.start()
                # 探测服务端上所有声明的工具元数据
                res = await client._send_request("tools/list", {})
            except Exception as exc:
                typer.echo(f"error: failed to connect or fetch tools from {server_name!r}: {exc}")
                await client.close()
                raise typer.Exit(EXIT_USAGE) from exc

            raw_tools = res.get("result", {}).get("tools", [])
            schemas: list[MCPToolSchema] = []
            for item in raw_tools:
                t_name = item.get("name", "")
                if not t_name:
                    continue
                desc = item.get("description", "")
                params = item.get("inputSchema", {})
                is_ro = t_name in srv.read_only_tools
                schemas.append(
                    MCPToolSchema.create(
                        server_name=srv.name,
                        tool_name=t_name,
                        description=desc,
                        parameters=params,
                        is_read_only=is_ro,
                    )
                )

            if not schemas:
                typer.echo(f"Server '{server_name}' connected, but reported no tools.")
                await client.close()
                return

            typer.echo(f"Server '{server_name}' declared {len(schemas)} tool(s):\n")
            for t in schemas:
                status = (
                    "ALLOWED"
                    if t.tool_name in srv.allowed_tools
                    else "BLOCKED (not in allowed_tools)"
                )
                ro_mark = " [read-only]" if t.is_read_only else " [mutating]"
                typer.echo(f"  • {t.tool_name}{ro_mark} - {status}")
                typer.echo(f"    Schema digest (SHA-256): {t.schema_digest}")
                if t.description:
                    typer.echo(f"    Description: {t.description.strip()[:100]}")

            if action == "pin":
                typer.echo(
                    "\n--- Recommended TOML configuration block to copy into .haven.toml ---"
                )
                typer.echo(f"[mcp.servers.{server_name}]")
                typer.echo(f"command = {srv.command!r}")
                if srv.args:
                    typer.echo(f"args = {list(srv.args)!r}")
                allowed_list = [t.tool_name for t in schemas]
                typer.echo(f"allowed_tools = {allowed_list!r}")
                if srv.read_only_tools:
                    typer.echo(f"read_only_tools = {list(srv.read_only_tools)!r}")
                typer.echo(f"[mcp.servers.{server_name}.pinned_schemas]")
                for t in schemas:
                    typer.echo(f"{t.tool_name} = {t.schema_digest!r}")
                typer.echo("-------------------------------------------------------------------")

            await client.close()

        asyncio.run(_run())
        raise typer.Exit(EXIT_OK)

    if action == "serve":
        from haven.interfaces.mcp_server import run_mcp_server

        with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
            asyncio.run(
                run_mcp_server(
                    workspace=ws,
                    read_only=read_only,
                    store_path=store_path,
                )
            )
        raise typer.Exit(EXIT_OK)

    typer.echo(f"error: unknown action {action!r}; use list, inspect, pin, or serve")
    raise typer.Exit(EXIT_USAGE)
