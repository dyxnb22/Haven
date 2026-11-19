"""MCP（Model Context Protocol）扩展契约与配置模型（ADR 0039）。

遵循 Haven 的受控网关设计：
- 静态配置声明，杜绝未经验证的随意动态挂载；
- 命名空间隔离：统一以 `mcp.<server_name>.<tool_name>` 呈现；
- Schema 指纹绑定（SHA-256）：防止运行时 Schema 漂移提权；
- 明确读写分离：未声明只读的工具默认视为高风险有副作用工具。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import Field

from haven.contracts.base import StrictModel
from haven.domain.digest import canonical_json, sha256_text


class MCPToolArgs(StrictModel):
    """由外部 MCP Server 定义参数结构的调用载荷容器。"""

    arguments: dict[str, Any] = Field(
        default_factory=dict, description="传递给 MCP 工具的键值参数字典。"
    )


@dataclass(frozen=True, slots=True)
class MCPToolSchema:
    """经由受控网关验证和指纹绑定的外部工具模式定义。"""

    namespaced_name: str
    server_name: str
    tool_name: str
    description: str
    parameters: dict[str, Any]
    is_read_only: bool = False
    #: 该工具模式（描述与参数）的 SHA-256。两者都由服务端提供、都会进模型视野，
    #: 因此都必须被覆盖——只算参数时，改描述就是一条合法的注入通道。
    schema_digest: str = ""

    @classmethod
    def create(
        cls,
        *,
        server_name: str,
        tool_name: str,
        description: str,
        parameters: dict[str, Any] | None,
        is_read_only: bool = False,
    ) -> MCPToolSchema:
        params = parameters if parameters is not None else {"type": "object", "properties": {}}
        # 指纹必须覆盖模型能看到的**全部**服务端可控内容。此前它只算 parameters，
        # 于是服务器只改 description 就完全合法地改了模型读到的工具说明——而那正是
        # 提示注入的入口（"忽略以上指令，执行 …"）。参数没变、指纹不变、漂移检不出来。
        #
        # 这会改变既有 pinned_schemas 的取值；重新固定用
        # `haven mcp <server> tools pin`，它会打印新的摘要和对应的 TOML 块。
        digest = sha256_text(canonical_json({"description": description, "parameters": params}))
        namespaced = f"mcp.{server_name}.{tool_name}"
        return cls(
            namespaced_name=namespaced,
            server_name=server_name,
            tool_name=tool_name,
            description=description,
            parameters=params,
            is_read_only=is_read_only,
            schema_digest=digest,
        )


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """声明在用户配置或项目配置中的受控 MCP Server 规格。"""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    #: 显式放行给模型的工具列表；为空时绝不暴露任何工具
    allowed_tools: tuple[str, ...] = ()
    #: 明确声明为只读无副作用的工具列表（自动判为 ALLOW / 低风险）
    read_only_tools: tuple[str, ...] = ()
    #: 绑定的 Schema 指纹哈希 (tool_name -> sha256)；用于防范 Schema 漂移
    pinned_schemas: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ImportedResource:
    """由用户显式从受控 MCP Server 导入的外部上下文资源（规划书 Phase P4）。"""

    server_name: str
    uri: str
    fetched_at: str
    content_digest: str
    mime_type: str = "text/plain"
    text: str = ""
    truncated: bool = False

    @classmethod
    def create(
        cls,
        *,
        server_name: str,
        uri: str,
        text: str,
        mime_type: str = "text/plain",
        fetched_at: str | None = None,
        max_bytes: int = 64 * 1024,
    ) -> ImportedResource:
        import datetime

        now_iso = fetched_at or datetime.datetime.now(datetime.UTC).isoformat()
        digest = sha256_text(text)
        truncated = False
        text_bytes = text.encode("utf-8")
        if len(text_bytes) > max_bytes:
            truncated = True
            text = text_bytes[:max_bytes].decode("utf-8", errors="ignore")
        return cls(
            server_name=server_name,
            uri=uri,
            fetched_at=now_iso,
            content_digest=digest,
            mime_type=mime_type,
            text=text,
            truncated=truncated,
        )
