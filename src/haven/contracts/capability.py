"""能力契约与快照模型。

支持编译内置能力与外部能力的统一规范化描述、固定摘要与运行生命周期冻结（ADR / 规划书 P2）。
运行开始时将可用能力冻结为不可变快照并计算 catalog_digest；
恢复时若发现 catalog 或 schema 漂移，将拒绝恢复。
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from haven.contracts.base import StrictModel
from haven.contracts.model import ToolSchema
from haven.contracts.tools import ARGS_MODELS, TOOL_DESCRIPTIONS, _clean_schema
from haven.domain.digest import canonical_json, sha256_text


class CapabilitySpec(StrictModel):
    """单个能力的不可变规范描述。"""

    #: 全局稳定名称，例如 repo.read、mcp.github.get_issue
    capability_id: str = Field(description="全局稳定名称，如 repo.read, mcp.github.get_issue")
    #: 能力来源：builtin / mcp / child_agent
    origin: str = Field(default="builtin", description="builtin | mcp | child_agent")
    #: 内建版本或外部协议/适配器版本
    version: str = Field(default="1.0", description="内建版本或外部协议/适配器版本")
    #: 面向模型或用户的能力说明
    description: str = Field(default="", description="面向模型或用户的能力说明")
    #: 提供给模型并用于本地验证的参数 JSON Schema
    input_schema: dict[str, Any] = Field(default_factory=dict, description="参数 JSON Schema")
    #: 规范化 input_schema 摘要
    schema_digest: str = Field(default="", description="规范化 input_schema 摘要")
    #: 副作用分类：state | workspace_read/effect | external_obs/effect | exec
    effect_class: str = Field(
        default="workspace_read",
        description="state | workspace_read | workspace_effect | external | exec",
    )
    #: 重放策略：safe | idempotent_with_key | never
    replay_policy: str = Field(
        default="safe",
        description="safe | idempotent_with_key | never",
    )
    #: 审批策略基线：allow | ask | deny
    approval_policy: str = Field(
        default="allow",
        description="allow | ask | deny",
    )
    #: 来源标识（内建模块、MCP Server 配置或 Agent profile）
    provenance: str = Field(
        default="haven.builtin",
        description="来源标识（内建模块、MCP Server 配置或 Agent profile）",
    )
    #: 约束（最大字节、截断与超时规则）
    result_limits: dict[str, Any] = Field(
        default_factory=dict, description="最大字节、截断与超时规则"
    )

    @classmethod
    def create(
        cls,
        *,
        capability_id: str,
        origin: str = "builtin",
        version: str = "1.0",
        description: str = "",
        input_schema: dict[str, Any] | None = None,
        effect_class: str = "workspace_read",
        replay_policy: str = "safe",
        approval_policy: str = "allow",
        provenance: str = "haven.builtin",
        result_limits: dict[str, Any] | None = None,
    ) -> CapabilitySpec:
        """规范化构造 CapabilitySpec 并生成确定的 schema_digest。"""
        schema = _clean_schema(dict(input_schema or {}))
        schema_digest = sha256_text(canonical_json(schema))
        return cls(
            capability_id=capability_id,
            origin=origin,
            version=version,
            description=description,
            input_schema=schema,
            schema_digest=schema_digest,
            effect_class=effect_class,
            replay_policy=replay_policy,
            approval_policy=approval_policy,
            provenance=provenance,
            result_limits=dict(result_limits or {}),
        )

    def to_tool_schema(self) -> ToolSchema:
        """导出为面向模型请求的 ToolSchema。"""
        return ToolSchema(
            name=self.capability_id,
            description=self.description,
            parameters=self.input_schema,
        )


class CapabilitySnapshot(StrictModel):
    """单次运行开始时冻结的完整能力快照与摘要。"""

    #: 所有能力项排序并规范化后的整体 SHA-256 摘要
    catalog_digest: str = Field(description="所有能力项排序并规范化后的整体摘要")
    #: 冻结的能力集合
    capabilities: tuple[CapabilitySpec, ...] = Field(default_factory=tuple)

    @classmethod
    def from_specs(cls, specs: tuple[CapabilitySpec, ...]) -> CapabilitySnapshot:
        """从能力规格列表生成不可变快照。

        按 capability_id 排序保证摘要确定性，同时保留能力列表原有呈现顺序。
        """
        sorted_for_digest = tuple(sorted(specs, key=lambda s: s.capability_id))
        digest_input = [
            {
                "id": s.capability_id,
                "origin": s.origin,
                "version": s.version,
                "schema_digest": s.schema_digest,
                "effect_class": s.effect_class,
            }
            for s in sorted_for_digest
        ]
        catalog_digest = sha256_text(canonical_json(digest_input))
        return cls(catalog_digest=catalog_digest, capabilities=specs)

    def get(self, capability_id: str) -> CapabilitySpec | None:
        """根据 capability_id 查找能力项。"""
        for cap in self.capabilities:
            if cap.capability_id == capability_id:
                return cap
        return None

    def has_capability(self, capability_id: str) -> bool:
        """判断是否包含指定能力项。"""
        return self.get(capability_id) is not None

    def to_tool_schemas(self) -> tuple[ToolSchema, ...]:
        """将快照中的所有能力导出为模型 ToolSchema 元组。"""
        return tuple(cap.to_tool_schema() for cap in self.capabilities)


def builtin_capabilities() -> tuple[CapabilitySpec, ...]:
    """生成 Haven 当前编译内置的全部能力项规格。"""
    specs: list[CapabilitySpec] = []
    for name, model_cls in ARGS_MODELS.items():
        desc = TOOL_DESCRIPTIONS.get(name, "")
        schema = model_cls.model_json_schema()
        if name in ("web.search", "web.fetch", "agent.delegate"):
            effect_class = "external_observation"
            replay_policy = "safe"
            approval_policy = "allow"
        elif name in ("task.plan", "task.stop"):
            effect_class = "state"
            replay_policy = "idempotent_with_key"
            approval_policy = "allow"
        elif name in ("repo.exec", "task.start"):
            effect_class = "exec"
            replay_policy = "never"
            approval_policy = "ask"
        elif name in (
            "repo.edit",
            "repo.create",
            "repo.delete",
            "repo.move",
            "repo.apply_patch",
        ):
            effect_class = "workspace_effect"
            replay_policy = "never"
            approval_policy = "ask"
        elif name in ("repo.check", "recipe.propose"):
            effect_class = "exec"
            replay_policy = "never"
            approval_policy = "ask"
        else:
            effect_class = "workspace_read"
            replay_policy = "safe"
            approval_policy = "allow"

        specs.append(
            CapabilitySpec.create(
                capability_id=name,
                origin="builtin",
                version="1.0",
                description=desc,
                input_schema=schema,
                effect_class=effect_class,
                replay_policy=replay_policy,
                approval_policy=approval_policy,
                provenance="haven.builtin",
            )
        )
    return tuple(specs)


def build_capability_snapshot(
    mcp_tools: tuple[Any, ...] = (),
) -> CapabilitySnapshot:
    """构建当前环境冻结的 CapabilitySnapshot。"""
    specs = list(builtin_capabilities())
    for tool in mcp_tools:
        specs.append(
            CapabilitySpec.create(
                capability_id=getattr(tool, "namespaced_name", getattr(tool, "name", "")),
                origin="mcp",
                version="2026-07-28",
                description=getattr(tool, "description", ""),
                input_schema=getattr(tool, "parameters", {}),
                effect_class="external_effect",
                replay_policy="never",
                approval_policy="ask",
                provenance=getattr(tool, "server_name", "mcp"),
            )
        )
    return CapabilitySnapshot.from_specs(tuple(specs))
