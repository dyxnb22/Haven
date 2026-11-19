"""`haven context`：外部受控 MCP 资源显式导入与管理（ADR / 规划书 Phase P4）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from haven.application.mcp_resources import (
    fetch_imported_resource,
    load_imported_resources,
    remove_imported_resource,
    save_imported_resource,
)
from haven.config import ConfigError, load_config
from haven.interfaces.cli_support.common import EXIT_OK, EXIT_USAGE


def context(
    action: str = typer.Argument("list", help="Action: list, add, remove."),
    server_name: str | None = typer.Argument(
        None, help="Configured MCP server name (for add, remove)."
    ),
    resource_uri: str | None = typer.Argument(
        None, help="Resource URI to import or remove (for add, remove)."
    ),
    workspace: Path = typer.Option(Path("."), "--workspace", "-w", help="Workspace root."),
) -> None:
    """管理外部受控 MCP 导入资源：查看已导入资源、导入新资源或移除废弃资源。"""
    ws = workspace.resolve()

    if action == "list":
        resources = load_imported_resources(ws)
        typer.echo(f"Imported Context Resources ({len(resources)} configured):\n")
        if not resources:
            typer.echo("  (no resources imported)")
            typer.echo("  Import one with: haven context add <server-name> <resource-uri>")
            raise typer.Exit(EXIT_OK)

        for r in resources:
            trunc_mark = " [truncated to 64KB]" if r.truncated else ""
            typer.echo(f"• {r.server_name} :: {r.uri}{trunc_mark}")
            typer.echo(f"    MIME type:      {r.mime_type}")
            typer.echo(f"    SHA-256 digest: {r.content_digest[:16]}...")
            typer.echo(f"    Fetched at:     {r.fetched_at}")
            preview = r.text.strip().replace("\n", " ")[:80]
            typer.echo(f"    Preview:        {preview}...\n")
        raise typer.Exit(EXIT_OK)

    if action == "add":
        if not server_name or not resource_uri:
            typer.echo("error: `haven context add` requires server_name and resource_uri, e.g.:")
            typer.echo("  haven context add my_server haven://runs/123/evidence")
            raise typer.Exit(EXIT_USAGE)

        try:
            config = load_config(ws)
        except ConfigError as exc:
            typer.echo(f"error: {exc}")
            raise typer.Exit(EXIT_USAGE) from None
        servers = {s.name: s for s in config.mcp_servers}
        srv = servers.get(server_name)
        if not srv:
            typer.echo(f"error: MCP server {server_name!r} not configured.")
            typer.echo(f"Available servers: {', '.join(servers.keys()) if servers else 'none'}")
            raise typer.Exit(EXIT_USAGE)

        async def _run() -> None:
            typer.echo(f"Fetching resource {resource_uri!r} from server {server_name!r}...")
            try:
                imported = await fetch_imported_resource(srv, resource_uri)
                save_imported_resource(ws, imported)
                typer.echo(
                    f"Successfully imported resource {resource_uri} ({len(imported.text)} chars)."
                )
                typer.echo(f"Digest: {imported.content_digest}")
            except Exception as exc:
                typer.echo(f"error: failed to fetch resource: {exc}")
                raise typer.Exit(EXIT_USAGE) from exc

        asyncio.run(_run())
        raise typer.Exit(EXIT_OK)

    if action == "remove":
        if not server_name or not resource_uri:
            typer.echo("error: `haven context remove` requires server_name and resource_uri.")
            raise typer.Exit(EXIT_USAGE)

        removed = remove_imported_resource(ws, server_name, resource_uri)
        if removed:
            typer.echo(f"Removed resource {resource_uri!r} of server {server_name!r}.")
        else:
            typer.echo(f"Resource {resource_uri!r} was not found.")
        raise typer.Exit(EXIT_OK)

    typer.echo(f"error: unknown action {action!r}; use list, add, or remove")
    raise typer.Exit(EXIT_USAGE)
