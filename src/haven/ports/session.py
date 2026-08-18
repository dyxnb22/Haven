"""会话存储端口：对运行、事件、审批、执行、检查点和构件进行事务性持久化。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from haven.contracts.checkpoint import CheckpointV1
from haven.contracts.events import ApplicationEvent, EventEnvelope
from haven.domain.enums import ApprovalDecision, EffectState, RunStatus


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    workspace: str
    workspace_digest: str
    goal: str
    mode: str
    status: RunStatus
    stop_reason: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    call_id: str
    run_id: str
    ticket_digest: str
    tool_name: str
    effect_state: EffectState
    preimage_digest: str
    #: 对于写操作，在 STARTED 时记录它作为“预期 postimage”（预览会在任何
    #: 字节落盘前计算它），完成时再用实际摘要确认。提前记录它，才能让
    #: 恢复逻辑判断“已写入但尚未记入日志”窗口中的崩溃。
    postimage_digest: str
    path: str
    #: 仅用于 repo.move：目标路径，使恢复逻辑可以检查移动的两端。
    dest_path: str = ""


class SessionStorePort(Protocol):
    async def create_run(
        self, run_id: str, workspace: str, workspace_digest: str, goal: str, mode: str
    ) -> None: ...

    async def update_run_status(self, run_id: str, status: RunStatus, stop_reason: str) -> None: ...

    async def get_run(self, run_id: str) -> RunRecord | None: ...

    async def list_runs(self, limit: int) -> list[RunRecord]: ...

    async def append_event(self, run_id: str, event: ApplicationEvent) -> EventEnvelope: ...

    async def load_events(self, run_id: str) -> list[EventEnvelope]: ...

    async def save_checkpoint(self, checkpoint: CheckpointV1) -> None: ...

    async def load_checkpoint(self, run_id: str) -> CheckpointV1 | None: ...

    async def record_approval(self, approval_id: str, run_id: str, request_digest: str) -> None: ...

    async def decide_approval(self, approval_id: str, decision: ApprovalDecision) -> None: ...

    async def consume_approval(self, approval_id: str, request_digest: str) -> bool: ...

    async def record_execution(self, record: ExecutionRecord) -> None: ...

    async def update_execution_state(
        self, call_id: str, effect_state: EffectState, postimage_digest: str = ""
    ) -> None: ...

    async def load_executions(self, run_id: str) -> list[ExecutionRecord]: ...

    async def put_artifact(self, content: bytes) -> str: ...

    async def get_artifact(self, digest: str) -> bytes | None: ...

    # -- 维护（haven gc）--------------------------------------------------------

    async def delete_run(self, run_id: str) -> None:
        """删除一次运行及其下记录的所有内容：事件、检查点、审批和执行日志行。
        构件按内容寻址且可能被共享，因此会根据仍由存活检查点引用的集合单独清理。"""
        ...

    async def list_artifacts(self) -> list[str]: ...

    async def delete_artifact(self, digest: str) -> None: ...

    async def close(self) -> None: ...
