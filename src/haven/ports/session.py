"""会话存储端口：对运行、事件、审批、执行、检查点和构件进行事务性持久化。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from haven.contracts.checkpoint import CheckpointV1
from haven.contracts.events import ApplicationEvent, EventEnvelope
from haven.domain.enums import ApprovalDecision, EffectState, RunStatus


class ArtifactError(Exception):
    """内容寻址构件缺失其完整性保证时抛出的端口级错误。"""


@dataclass(frozen=True, slots=True)
class RunRecord:
    """持久化运行的索引信息，不包含完整事件和检查点。"""

    #: 所有关联记录共用的稳定运行标识。
    run_id: str
    #: 与运行关联的规范化工作区根路径。
    workspace: str
    #: 创建运行时记录的工作区摘要。
    workspace_digest: str
    #: 用户最初的目标。
    goal: str
    #: 序列化后用于持久化的 PermissionMode。
    mode: str
    #: 最近一次持久化的生命周期状态。
    status: RunStatus
    #: 序列化为字符串的 StopReason，以兼容旧日志。
    stop_reason: str
    #: ISO-8601 创建时间戳。
    created_at: str
    #: 最近一次持久化更新的 ISO-8601 时间戳。
    updated_at: str


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    """一项可能产生副作用的执行所对应的持久化日志行。"""

    #: 工具调用标识；补丁子效果会追加 ``#<index>``。
    call_id: str
    #: 拥有该日志行的运行。
    run_id: str
    #: 流水线消费的一次性执行票据摘要。
    ticket_digest: str
    #: 可能造成该副作用的工具形态。
    tool_name: str
    #: STARTED/CONFIRMED/FAILED，或恢复调和阶段的状态。
    effect_state: EffectState
    #: 适用时记录操作前观察到的摘要。
    preimage_digest: str
    #: 对于写操作，在 STARTED 时记录它作为“预期 postimage”（预览会在任何
    #: 字节落盘前计算它），完成时再用实际摘要确认。提前记录它，才能让
    #: 恢复逻辑判断“已写入但尚未记入日志”窗口中的崩溃。
    postimage_digest: str
    #: 规范化源路径；非 move 操作时为受影响路径。
    path: str
    #: 仅用于 repo.move：目标路径，使恢复逻辑可以检查移动的两端。
    dest_path: str = ""


class SessionStorePort(Protocol):
    """运行数据的持久化端口；SQLite 和内存实现共享此事务边界。"""

    async def create_run(
        self, run_id: str, workspace: str, workspace_digest: str, goal: str, mode: str
    ) -> None:
        """创建运行索引行，并固定其工作区、目标和权限模式。"""
        ...

    async def update_run_status(self, run_id: str, status: RunStatus, stop_reason: str) -> None:
        """持久化运行终态或中间状态及其停止原因。"""
        ...

    async def get_run(self, run_id: str) -> RunRecord | None:
        """按稳定运行标识读取索引行；不存在时返回 None。"""
        ...

    async def list_runs(self, limit: int) -> list[RunRecord]:
        """按最近更新时间倒序返回最多 limit 条运行记录。"""
        ...

    async def append_event(self, run_id: str, event: ApplicationEvent) -> EventEnvelope:
        """为持久化事件分配单调序号并原子写入事件流。"""
        ...

    async def load_events(self, run_id: str) -> list[EventEnvelope]:
        """按序读取运行的持久化事件；临时事件不在其中。"""
        ...

    async def save_checkpoint(self, checkpoint: CheckpointV1) -> None:
        """保存运行最新检查点，供快速恢复使用。"""
        ...

    async def load_checkpoint(self, run_id: str) -> CheckpointV1 | None:
        """读取运行最新检查点；没有检查点时返回 None。"""
        ...

    async def record_approval(self, approval_id: str, run_id: str, request_digest: str) -> None:
        """记录尚未决定的一次性审批请求及其摘要绑定。"""
        ...

    async def decide_approval(self, approval_id: str, decision: ApprovalDecision) -> None:
        """持久化审批决定，但不自动消费审批凭证。"""
        ...

    async def consume_approval(self, approval_id: str, request_digest: str) -> bool:
        """仅在决定通过且摘要匹配时原子消费一次性审批。"""
        ...

    async def record_execution(self, record: ExecutionRecord) -> None:
        """记录可能产生副作用的执行日志起点。"""
        ...

    async def update_execution_state(
        self,
        run_id: str,
        call_id: str,
        effect_state: EffectState,
        postimage_digest: str = "",
    ) -> None:
        """更新指定运行内的执行效果状态及可选的实际后像摘要。"""
        ...

    async def load_executions(self, run_id: str) -> list[ExecutionRecord]:
        """读取运行的执行日志，供恢复服务分类未确认副作用。"""
        ...

    async def put_artifact(self, content: bytes) -> str:
        """按内容寻址保存构件并返回其摘要。"""
        ...

    async def get_artifact(self, digest: str) -> bytes | None:
        """按摘要读取构件；不存在时返回 None。"""
        ...

    # -- 维护（haven gc）--------------------------------------------------------

    async def delete_run(self, run_id: str) -> None:
        """删除一次运行及其下记录的所有内容：事件、检查点、审批和执行日志行。
        构件按内容寻址且可能被共享，因此会根据仍由存活检查点引用的集合单独清理。"""
        ...

    async def list_artifacts(self) -> list[str]:
        """列出当前存储中的构件摘要。"""
        ...

    async def delete_artifact(self, digest: str) -> None:
        """删除一个未被保留检查点引用的构件。"""
        ...

    async def close(self) -> None:
        """关闭底层数据库或释放其他持久化资源。"""
        ...
