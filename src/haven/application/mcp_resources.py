"""MCP 外部资源显式导入与上下文持久化管理（ADR / 规划书 Phase P4）。

用户显式将受控 MCP Server 的资源导入工作区上下文（如 issue、PR、架构规范、Wiki）。
资源作为不可信数据（trust="untrusted"）提供给模型，带有清晰的来源命名空间与 SHA-256 摘要。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from haven.application.mcp_client import MCPClient, MCPError
from haven.contracts.mcp import ImportedResource, MCPServerConfig

IMPORTED_RESOURCES_FILENAME = "imported_resources.json"


def _get_resources_path(workspace: Path) -> Path:
    return workspace.resolve() / ".haven" / IMPORTED_RESOURCES_FILENAME


def load_imported_resources(workspace: Path) -> tuple[ImportedResource, ...]:
    """读取工作区 `.haven/imported_resources.json` 中配置的全部外部资源。"""
    path = _get_resources_path(workspace)
    if not path.is_file():
        return ()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return ()
        resources: list[ImportedResource] = []
        for item in data:
            if isinstance(item, dict) and "server_name" in item and "uri" in item:
                resources.append(
                    ImportedResource(
                        server_name=str(item.get("server_name", "")),
                        uri=str(item.get("uri", "")),
                        fetched_at=str(item.get("fetched_at", "")),
                        content_digest=str(item.get("content_digest", "")),
                        mime_type=str(item.get("mime_type", "text/plain")),
                        text=str(item.get("text", "")),
                        truncated=bool(item.get("truncated", False)),
                    )
                )
        return tuple(resources)
    except Exception:
        return ()


def save_imported_resource(workspace: Path, resource: ImportedResource) -> None:
    """原子化保存或更新一个导入的资源至 `.haven/imported_resources.json`。"""
    path = _get_resources_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = list(load_imported_resources(workspace))
    updated: list[ImportedResource] = []
    found = False
    for res in existing:
        if res.server_name == resource.server_name and res.uri == resource.uri:
            updated.append(resource)
            found = True
        else:
            updated.append(res)
    if not found:
        updated.append(resource)

    raw_list = [asdict(r) for r in updated]
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(raw_list, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def remove_imported_resource(workspace: Path, server_name: str, uri: str) -> bool:
    """从工作区中移除指定的已导入资源。"""
    existing = list(load_imported_resources(workspace))
    remained = [r for r in existing if not (r.server_name == server_name and r.uri == uri)]
    if len(remained) == len(existing):
        return False

    path = _get_resources_path(workspace)
    raw_list = [asdict(r) for r in remained]
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(raw_list, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)
    return True


async def fetch_imported_resource(
    server_config: MCPServerConfig,
    uri: str,
    *,
    max_bytes: int = 64 * 1024,
) -> ImportedResource:
    """连接外部 MCP Server 读取资源内容并封装为 ImportedResource。"""
    client = MCPClient(server_config)
    # `start()` 自己 spawn 子进程，然后才做 initialize/initialized 握手。把它放在
    # try 之外，意味着任何一种握手失败（服务端立刻退出、initialize 超时、畸形
    # JSON-RPC）都会带着一个活的子进程和它的 stderr 读取任务逃出去——CLI 打印
    # 一行错误后退出，那个 MCP server 就作为孤儿继续跑。
    try:
        await client.start()
        res = await client.read_resource(uri)
        contents = res.get("contents", [])
        if not contents or not isinstance(contents, list):
            raise MCPError(
                f"Resource {uri!r} returned no contents from server {server_config.name}"
            )

        first = contents[0]
        text = ""
        mime_type = "text/plain"
        if isinstance(first, dict):
            text = first.get("text", "")
            mime_type = first.get("mimeType", "text/plain")
            if not text and "blob" in first:
                text = f"[Binary data: {mime_type}]"

        return ImportedResource.create(
            server_name=server_config.name,
            uri=uri,
            text=text,
            mime_type=mime_type,
            max_bytes=max_bytes,
        )
    finally:
        await client.close()
