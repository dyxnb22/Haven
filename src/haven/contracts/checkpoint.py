"""版本化的检查点模式。

检查点是用于快速恢复的快照；事件日志仍然是审计权威。模式版本或校验和不匹配
时，加载会失败并关闭。
"""

from __future__ import annotations

from pydantic import Field

from haven.contracts.base import StrictModel
from haven.contracts.model import ModelMessage
from haven.contracts.tools import PlanStep
from haven.domain.budget import Budget, BudgetUsage
from haven.domain.digest import sha256_text
from haven.domain.evidence import (
    CheckEvidence,
    DiffEvidence,
    EditEvidence,
    EvidenceLedger,
)

CHECKPOINT_SCHEMA_VERSION = 1


class BudgetSnapshot(StrictModel):
    """检查点中的预算上限快照。"""

    #: 模型循环轮次的硬上限。
    max_steps: int
    #: 工具调用总次数的硬上限。
    max_tool_calls: int
    #: 墙上时钟硬上限，单位为秒。
    max_wall_time_seconds: float
    #: 输入 token 的硬上限。
    max_input_tokens: int
    #: 生成 token 的硬上限。
    max_output_tokens: int
    #: 估算费用硬上限，单位为美元。
    max_cost_usd: float

    @classmethod
    def from_domain(cls, budget: Budget) -> BudgetSnapshot:
        """将领域预算上限转换为可序列化快照。"""
        return cls(
            max_steps=budget.max_steps,
            max_tool_calls=budget.max_tool_calls,
            max_wall_time_seconds=budget.max_wall_time_seconds,
            max_input_tokens=budget.max_input_tokens,
            max_output_tokens=budget.max_output_tokens,
            max_cost_usd=budget.max_cost_usd,
        )

    def to_domain(self) -> Budget:
        """将快照还原为不可变领域预算对象。"""
        return Budget(
            max_steps=self.max_steps,
            max_tool_calls=self.max_tool_calls,
            max_wall_time_seconds=self.max_wall_time_seconds,
            max_input_tokens=self.max_input_tokens,
            max_output_tokens=self.max_output_tokens,
            max_cost_usd=self.max_cost_usd,
        )


class UsageSnapshot(StrictModel):
    """检查点中的累计用量快照。"""

    #: 写入检查点时已完成的模型循环轮数。
    steps: int
    #: 写入检查点时已消耗的工具调用次数。
    tool_calls: int
    #: 累计墙上时钟预算用量，单位为秒。
    wall_time_seconds: float
    #: 输入 token 总数，包含缓存输入。
    input_tokens: int
    #: 生成的输出 token 总数。
    output_tokens: int
    #: 累计估算费用，单位为美元。
    cost_usd: float
    #: 本次运行中任意提供商用量为估算值时为 True。
    usage_estimated: bool
    #: 由提供商提示缓存提供的 input_tokens 部分。
    cached_input_tokens: int = 0

    @classmethod
    def from_domain(cls, usage: BudgetUsage) -> UsageSnapshot:
        """将领域累计用量转换为检查点快照。"""
        return cls(
            steps=usage.steps,
            tool_calls=usage.tool_calls,
            wall_time_seconds=usage.wall_time_seconds,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
            usage_estimated=usage.usage_estimated,
            cached_input_tokens=usage.cached_input_tokens,
        )

    def to_domain(self) -> BudgetUsage:
        """将用量快照还原为可继续累计的领域对象。"""
        return BudgetUsage(
            steps=self.steps,
            tool_calls=self.tool_calls,
            wall_time_seconds=self.wall_time_seconds,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=self.cost_usd,
            usage_estimated=self.usage_estimated,
            cached_input_tokens=self.cached_input_tokens,
        )


class EditEvidenceSnapshot(StrictModel):
    """可序列化的编辑证据。"""

    #: 记录该证据时的事件序号。
    seq: int
    #: 规范化工作区相对路径。
    path: str
    #: 编辑前文件的摘要。
    preimage_digest: str
    #: 编辑后文件的摘要。
    postimage_digest: str


class CheckEvidenceSnapshot(StrictModel):
    """可序列化的验证证据。"""

    #: 记录该检查时的事件序号。
    seq: int
    #: 实际运行的已注册 recipe 标识。
    recipe_id: str
    #: 进程退出状态；零表示检查通过。
    exit_code: int
    #: 检查墙上时钟耗时，单位为毫秒。
    duration_ms: int
    #: 记录前 stdout/stderr 被截断时为 True。
    truncated: bool


class DiffEvidenceSnapshot(StrictModel):
    """可序列化的差异证据。"""

    #: 记录该差异时的事件序号。
    seq: int
    #: 完整差异中的变更文件数。
    files_changed: int
    #: 新增行数。
    insertions: int
    #: 删除行数。
    deletions: int
    #: 完整差异的摘要，而不只是有界 UI 预览的摘要。
    diff_digest: str


class EvidenceSnapshot(StrictModel):
    """检查点中的完整证据账本快照。"""

    #: 按事件顺序排列的文件编辑证据。
    edits: tuple[EditEvidenceSnapshot, ...] = ()
    #: 按事件顺序排列的验证 recipe 证据。
    checks: tuple[CheckEvidenceSnapshot, ...] = ()
    #: 按事件顺序排列的差异证据。
    diffs: tuple[DiffEvidenceSnapshot, ...] = ()

    @classmethod
    def from_domain(cls, ledger: EvidenceLedger) -> EvidenceSnapshot:
        """将领域证据账本逐项转换为可序列化快照。"""
        return cls(
            edits=tuple(
                EditEvidenceSnapshot(
                    seq=e.seq,
                    path=e.path,
                    preimage_digest=e.preimage_digest,
                    postimage_digest=e.postimage_digest,
                )
                for e in ledger.edits
            ),
            checks=tuple(
                CheckEvidenceSnapshot(
                    seq=c.seq,
                    recipe_id=c.recipe_id,
                    exit_code=c.exit_code,
                    duration_ms=c.duration_ms,
                    truncated=c.truncated,
                )
                for c in ledger.checks
            ),
            diffs=tuple(
                DiffEvidenceSnapshot(
                    seq=d.seq,
                    files_changed=d.files_changed,
                    insertions=d.insertions,
                    deletions=d.deletions,
                    diff_digest=d.diff_digest,
                )
                for d in ledger.diffs
            ),
        )

    def to_domain(self) -> EvidenceLedger:
        """将证据快照还原为领域账本，保持原有事件顺序。"""
        return EvidenceLedger(
            edits=tuple(
                EditEvidence(
                    seq=e.seq,
                    path=e.path,
                    preimage_digest=e.preimage_digest,
                    postimage_digest=e.postimage_digest,
                )
                for e in self.edits
            ),
            checks=tuple(
                CheckEvidence(
                    seq=c.seq,
                    recipe_id=c.recipe_id,
                    exit_code=c.exit_code,
                    duration_ms=c.duration_ms,
                    truncated=c.truncated,
                )
                for c in self.checks
            ),
            diffs=tuple(
                DiffEvidence(
                    seq=d.seq,
                    files_changed=d.files_changed,
                    insertions=d.insertions,
                    deletions=d.deletions,
                    diff_digest=d.diff_digest,
                )
                for d in self.diffs
            ),
        )


class CheckpointV1(StrictModel):
    """版本 1 的恢复快照；事件日志仍然是审计事实的权威来源。"""

    #: 用于拒绝不兼容恢复数据的模式版本。
    schema_version: int = CHECKPOINT_SCHEMA_VERSION
    #: 用于加载匹配事件流的运行标识。
    run_id: str
    #: 检查点时的工作区身份；恢复前必须仍然匹配。
    workspace_digest: str
    #: 恢复运行所需的用户原始目标。
    goal: str
    #: 以稳定字符串值序列化的权限模式。
    mode: str
    #: 写入检查点时的 RunStatus。
    status: str
    #: 此快照所表示的最高持久化事件序号。
    last_seq: int
    #: 为本次运行保存的硬预算。
    budget: BudgetSnapshot
    #: 写入此快照前已计费的用量。
    usage: UsageSnapshot
    #: 重建下一次模型请求所需的对话记录。
    messages: tuple[ModelMessage, ...]
    #: 恢复后评估 Evidence Gate 所需的证据账本。
    evidence: EvidenceSnapshot = EvidenceSnapshot()
    #: 最近一次读取时的规范化路径 -> 摘要，用于拒绝过期编辑。
    files_read: dict[str, str] = Field(default_factory=dict)
    #: 当前结构化计划；独立于消息保存，避免压缩时丢失。
    plan: tuple[PlanStep, ...] = ()
    #: 文件第一次被本次运行编辑前的内容寻址快照。
    original_artifacts: dict[str, str] = Field(
        default_factory=dict,
        description="路径 -> 本次运行首次编辑前文件内容的构件摘要",
    )

    def checksum(self) -> str:
        """对当前快照的规范 JSON 求摘要，用于持久化完整性校验。"""
        return sha256_text(self.model_dump_json())
