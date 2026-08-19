"""用于测试和离线评估的内存会话存储（与 SQLite 契约相同）。"""

from __future__ import annotations

from datetime import UTC, datetime

from haven.contracts.checkpoint import CheckpointV1
from haven.contracts.events import ApplicationEvent, EventEnvelope
from haven.domain.digest import sha256_bytes
from haven.domain.enums import ApprovalDecision, EffectState, RunStatus
from haven.ports.session import ExecutionRecord, RunRecord


def _now() -> str:
    return datetime.now(UTC).isoformat()


class MemorySessionStore:
    """仅用于测试和离线评估的内存持久化实现。

    在进程内存中实现 SessionStorePort。
    """

    def __init__(self) -> None:
        self.runs: dict[str, RunRecord] = {}
        self.events: dict[str, list[EventEnvelope]] = {}
        self.checkpoints: dict[str, CheckpointV1] = {}
        self.approvals: dict[str, dict[str, str]] = {}
        self.executions: dict[tuple[str, str], ExecutionRecord] = {}
        self.artifacts: dict[str, bytes] = {}

    async def create_run(
        self, run_id: str, workspace: str, workspace_digest: str, goal: str, mode: str
    ) -> None:
        """创建运行记录并初始化该运行的事件流。"""
        now = _now()
        self.runs[run_id] = RunRecord(
            run_id=run_id,
            workspace=workspace,
            workspace_digest=workspace_digest,
            goal=goal,
            mode=mode,
            status=RunStatus.CREATED,
            stop_reason="",
            created_at=now,
            updated_at=now,
        )
        self.events.setdefault(run_id, [])

    async def update_run_status(self, run_id: str, status: RunStatus, stop_reason: str) -> None:
        """更新运行状态、停止原因和最后更新时间。"""
        record = self.runs[run_id]
        self.runs[run_id] = RunRecord(
            run_id=record.run_id,
            workspace=record.workspace,
            workspace_digest=record.workspace_digest,
            goal=record.goal,
            mode=record.mode,
            status=status,
            stop_reason=stop_reason,
            created_at=record.created_at,
            updated_at=_now(),
        )

    async def get_run(self, run_id: str) -> RunRecord | None:
        """按运行 ID 读取记录；不存在时返回 ``None``。"""
        return self.runs.get(run_id)

    async def list_runs(self, limit: int) -> list[RunRecord]:
        """按最近更新时间倒序返回最多 ``limit`` 条运行记录。"""
        if limit < 0:
            raise ValueError("limit must be non-negative")
        ordered = sorted(
            self.runs.values(),
            key=lambda r: (r.updated_at, r.created_at, r.run_id),
            reverse=True,
        )
        return ordered[:limit]

    async def append_event(self, run_id: str, event: ApplicationEvent) -> EventEnvelope:
        """为运行分配递增序号并追加事件，返回带序号的信封。"""
        stream = self.events.setdefault(run_id, [])
        envelope = EventEnvelope(seq=len(stream) + 1, at=_now(), event=event)
        stream.append(envelope)
        return envelope

    async def load_events(self, run_id: str) -> list[EventEnvelope]:
        """返回运行的完整事件流副本，避免调用方修改存储内部列表。"""
        return list(self.events.get(run_id, []))

    async def save_checkpoint(self, checkpoint: CheckpointV1) -> None:
        """保存检查点，并忽略比现有检查点更旧的乱序写入。"""
        # 保留 seq 最高的快照，与 SQLite 存储的 `ORDER BY seq DESC LIMIT 1` 读取
        # 逻辑一致。乱序保存不能让恢复退回到较旧的 transcript。
        existing = self.checkpoints.get(checkpoint.run_id)
        if existing is not None and existing.last_seq > checkpoint.last_seq:
            return
        self.checkpoints[checkpoint.run_id] = checkpoint

    async def load_checkpoint(self, run_id: str) -> CheckpointV1 | None:
        """读取运行当前序号最高的检查点；没有检查点时返回 ``None``。"""
        return self.checkpoints.get(run_id)

    async def record_approval(self, approval_id: str, run_id: str, request_digest: str) -> None:
        """记录待审批请求及其绑定的运行和请求摘要。"""
        self.approvals[approval_id] = {
            "run_id": run_id,
            "request_digest": request_digest,
            "decision": "",
            "consumed": "",
        }

    async def decide_approval(self, approval_id: str, decision: ApprovalDecision) -> None:
        """写入审批决定，供后续一次性消费校验。"""
        self.approvals[approval_id]["decision"] = decision.value

    async def consume_approval(self, approval_id: str, request_digest: str) -> bool:
        """仅在摘要匹配且决定为批准时消费审批，并保证只能成功一次。"""
        record = self.approvals.get(approval_id)
        if (
            record is None
            or record["request_digest"] != request_digest
            or record["decision"] != ApprovalDecision.APPROVED.value
            or record["consumed"]
        ):
            return False
        record["consumed"] = _now()
        return True

    async def record_execution(self, record: ExecutionRecord) -> None:
        """记录工具调用的票据、摘要和副作用状态。"""
        self.executions[(record.run_id, record.call_id)] = record

    async def update_execution_state(
        self,
        run_id: str,
        call_id: str,
        effect_state: EffectState,
        postimage_digest: str = "",
    ) -> None:
        """更新执行状态；空的后像摘要不会覆盖已有摘要。"""
        key = (run_id, call_id)
        old = self.executions[key]
        self.executions[key] = ExecutionRecord(
            call_id=old.call_id,
            run_id=old.run_id,
            ticket_digest=old.ticket_digest,
            tool_name=old.tool_name,
            effect_state=effect_state,
            preimage_digest=old.preimage_digest,
            postimage_digest=postimage_digest or old.postimage_digest,
            path=old.path,
            dest_path=old.dest_path,
        )

    async def load_executions(self, run_id: str) -> list[ExecutionRecord]:
        """返回指定运行的全部执行记录。"""
        return [r for r in self.executions.values() if r.run_id == run_id]

    async def put_artifact(self, content: bytes) -> str:
        """按内容摘要保存构件并返回其地址摘要。"""
        digest = sha256_bytes(content)
        self.artifacts[digest] = content
        return digest

    async def get_artifact(self, digest: str) -> bytes | None:
        """按内容摘要读取构件；不存在时返回 ``None``。"""
        return self.artifacts.get(digest)

    async def delete_run(self, run_id: str) -> None:
        """删除运行及其事件、检查点、审批和执行记录。"""
        self.runs.pop(run_id, None)
        self.events.pop(run_id, None)
        self.checkpoints.pop(run_id, None)
        self.approvals = {
            approval_id: record
            for approval_id, record in self.approvals.items()
            if record["run_id"] != run_id
        }
        self.executions = {
            key: record for key, record in self.executions.items() if record.run_id != run_id
        }

    async def list_artifacts(self) -> list[str]:
        """返回当前内存存储中的全部构件摘要。"""
        return sorted(self.artifacts)

    async def delete_artifact(self, digest: str) -> None:
        """删除指定构件；摘要不存在时保持幂等。"""
        self.artifacts.pop(digest, None)

    async def close(self) -> None:
        """释放存储资源；内存实现无需执行额外操作。"""
        return None
