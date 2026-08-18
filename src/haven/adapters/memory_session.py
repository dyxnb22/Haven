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
    """在进程内存中实现 SessionStorePort。"""

    def __init__(self) -> None:
        self.runs: dict[str, RunRecord] = {}
        self.events: dict[str, list[EventEnvelope]] = {}
        self.checkpoints: dict[str, CheckpointV1] = {}
        self.approvals: dict[str, dict[str, str]] = {}
        self.executions: dict[str, ExecutionRecord] = {}
        self.artifacts: dict[str, bytes] = {}

    async def create_run(
        self, run_id: str, workspace: str, workspace_digest: str, goal: str, mode: str
    ) -> None:
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
        return self.runs.get(run_id)

    async def list_runs(self, limit: int) -> list[RunRecord]:
        ordered = sorted(self.runs.values(), key=lambda r: r.created_at, reverse=True)
        return ordered[:limit]

    async def append_event(self, run_id: str, event: ApplicationEvent) -> EventEnvelope:
        stream = self.events.setdefault(run_id, [])
        envelope = EventEnvelope(seq=len(stream) + 1, at=_now(), event=event)
        stream.append(envelope)
        return envelope

    async def load_events(self, run_id: str) -> list[EventEnvelope]:
        return list(self.events.get(run_id, []))

    async def save_checkpoint(self, checkpoint: CheckpointV1) -> None:
        # 保留 seq 最高的快照，与 SQLite 存储的 `ORDER BY seq DESC LIMIT 1` 读取
        # 逻辑一致。乱序保存不能让恢复退回到较旧的 transcript。
        existing = self.checkpoints.get(checkpoint.run_id)
        if existing is not None and existing.last_seq > checkpoint.last_seq:
            return
        self.checkpoints[checkpoint.run_id] = checkpoint

    async def load_checkpoint(self, run_id: str) -> CheckpointV1 | None:
        return self.checkpoints.get(run_id)

    async def record_approval(self, approval_id: str, run_id: str, request_digest: str) -> None:
        self.approvals[approval_id] = {
            "run_id": run_id,
            "request_digest": request_digest,
            "decision": "",
            "consumed": "",
        }

    async def decide_approval(self, approval_id: str, decision: ApprovalDecision) -> None:
        self.approvals[approval_id]["decision"] = decision.value

    async def consume_approval(self, approval_id: str, request_digest: str) -> bool:
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
        self.executions[record.call_id] = record

    async def update_execution_state(
        self, call_id: str, effect_state: EffectState, postimage_digest: str = ""
    ) -> None:
        old = self.executions[call_id]
        self.executions[call_id] = ExecutionRecord(
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
        return [r for r in self.executions.values() if r.run_id == run_id]

    async def put_artifact(self, content: bytes) -> str:
        digest = sha256_bytes(content)
        self.artifacts[digest] = content
        return digest

    async def get_artifact(self, digest: str) -> bytes | None:
        return self.artifacts.get(digest)

    async def delete_run(self, run_id: str) -> None:
        self.runs.pop(run_id, None)
        self.events.pop(run_id, None)
        self.checkpoints.pop(run_id, None)
        self.approvals = {
            approval_id: record
            for approval_id, record in self.approvals.items()
            if record["run_id"] != run_id
        }
        self.executions = {
            call_id: record
            for call_id, record in self.executions.items()
            if record.run_id != run_id
        }

    async def list_artifacts(self) -> list[str]:
        return sorted(self.artifacts)

    async def delete_artifact(self, digest: str) -> None:
        self.artifacts.pop(digest, None)

    async def close(self) -> None:
        return None
