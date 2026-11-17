"""SQLite 会话存储：运行、只追加事件日志、检查点、绑定摘要的审批、执行日志和
内容寻址构件。

Schema 迁移采用失败即拒绝策略：遇到未知 schema 版本时直接中止，而不是猜测如何处理。
"""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from haven.contracts.checkpoint import CheckpointV1
from haven.contracts.events import (
    EVENT_ADAPTER,
    SCHEMA_VERSION,
    ApplicationEvent,
    EventEnvelope,
    HandoffUpdated,
)
from haven.contracts.project_memory import (
    NOTE_CAPACITY_MESSAGE,
    PROJECT_NOTE_LIMIT,
    PROJECT_NOTES_TOTAL_CHARS,
    ProjectNote,
    normalize_note_text,
)
from haven.domain.digest import sha256_bytes, sha256_text
from haven.domain.enums import ApprovalDecision, EffectState, RunStatus
from haven.ports.session import INPUT_HISTORY_LIMIT, ArtifactError, ExecutionRecord, RunRecord

DB_SCHEMA_VERSION = 6

_INPUT_HISTORY_TABLE = (
    "CREATE TABLE IF NOT EXISTS input_history ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL, text TEXT NOT NULL)"
)
_INPUT_HISTORY_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_input_history_workspace ON input_history(workspace, id)"
)
_PROJECT_NOTES_TABLE = (
    "CREATE TABLE IF NOT EXISTS project_notes ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL, "
    "text TEXT NOT NULL, created_at TEXT NOT NULL)"
)
_PROJECT_NOTES_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_project_notes_workspace ON project_notes(workspace, id)"
)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL,
    migrated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL,
    workspace_digest TEXT NOT NULL,
    goal TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    stop_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS checkpoints (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    decision TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    consumed_at TEXT
);
CREATE TABLE IF NOT EXISTS executions (
    run_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    ticket_digest TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    effect_state TEXT NOT NULL,
    preimage_digest TEXT NOT NULL DEFAULT '',
    postimage_digest TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    dest_path TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, call_id)
);
CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_approvals_run_id ON approvals(run_id);
CREATE INDEX IF NOT EXISTS idx_executions_tool_name ON executions(tool_name);
{_INPUT_HISTORY_TABLE};
{_INPUT_HISTORY_INDEX};
{_PROJECT_NOTES_TABLE};
{_PROJECT_NOTES_INDEX};
"""

#: 原地迁移，按存储版本依次应用。每个条目将 schema 从 `version` 迁移到
#: `version + 1`。需要改变主键时，在同一事务内重建目标表并复制已有数据。
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: ("ALTER TABLE executions ADD COLUMN dest_path TEXT NOT NULL DEFAULT ''",),
    2: (
        "CREATE TABLE executions_v3 ("
        "run_id TEXT NOT NULL, call_id TEXT NOT NULL, ticket_digest TEXT NOT NULL, "
        "tool_name TEXT NOT NULL, effect_state TEXT NOT NULL, "
        "preimage_digest TEXT NOT NULL DEFAULT '', "
        "postimage_digest TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '', "
        "dest_path TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, PRIMARY KEY (run_id, call_id))",
        "INSERT INTO executions_v3 (run_id, call_id, ticket_digest, tool_name, "
        "effect_state, preimage_digest, postimage_digest, path, dest_path, created_at, "
        "updated_at) SELECT run_id, call_id, ticket_digest, tool_name, effect_state, "
        "preimage_digest, postimage_digest, path, dest_path, created_at, updated_at "
        "FROM executions",
        "DROP TABLE executions",
        "ALTER TABLE executions_v3 RENAME TO executions",
    ),
    3: (
        "CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_approvals_run_id ON approvals(run_id)",
        "CREATE INDEX IF NOT EXISTS idx_executions_tool_name ON executions(tool_name)",
    ),
    4: (_INPUT_HISTORY_TABLE, _INPUT_HISTORY_INDEX),
    5: (_PROJECT_NOTES_TABLE, _PROJECT_NOTES_INDEX),
}


class StoreError(ArtifactError):
    """持久化层无法完成事务时抛出的稳定错误。"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _schema_version(db: aiosqlite.Connection) -> int | None:
    async with db.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'schema_meta'"
    ) as cursor:
        if await cursor.fetchone() is None:
            return None
    async with db.execute("SELECT version FROM schema_meta") as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    version = int(row["version"])
    if version != DB_SCHEMA_VERSION and version not in _MIGRATIONS:
        raise StoreError(
            f"database schema version {version} != expected "
            f"{DB_SCHEMA_VERSION} and no migration path exists; back up explicitly"
        )
    return version


async def _enable_wal(db: aiosqlite.Connection) -> None:
    # SQLite may return BUSY immediately when another connection first switches to WAL.
    for attempt in range(4):
        try:
            async with db.execute("PRAGMA journal_mode=WAL") as cursor:
                await cursor.fetchone()
            return
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorcode != sqlite3.SQLITE_BUSY or attempt == 3:
                raise
            await asyncio.sleep(0.01 * 2**attempt)


class SqliteSessionStore:
    """基于 SQLite 的持久化实现，保存运行索引、事件和恢复记录。

    在 aiosqlite 上实现 SessionStorePort。
    """

    def __init__(self, db: aiosqlite.Connection, artifacts_dir: Path) -> None:
        self._db = db
        self._artifacts_dir = artifacts_dir
        self._append_lock = asyncio.Lock()

    @classmethod
    async def open(cls, db_path: Path, artifacts_dir: Path) -> SqliteSessionStore:
        """打开存储并初始化 schema；未知版本或无迁移路径时拒绝启动。"""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(db_path)
        try:
            db.row_factory = aiosqlite.Row
            # 先只读检查版本，再在写锁内重读，避免两个进程按同一个旧版本迁移。
            await _schema_version(db)
            await _enable_wal(db)
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            version = await _schema_version(db)
            # executescript 会隐式提交，初始化 DDL 必须与迁移共用事务。
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    await db.execute(statement)
            if version is None:
                await db.execute(
                    "INSERT INTO schema_meta (version, migrated_at) VALUES (?, ?)",
                    (DB_SCHEMA_VERSION, _now()),
                )
            elif version != DB_SCHEMA_VERSION:
                for previous in range(version, DB_SCHEMA_VERSION):
                    for statement in _MIGRATIONS[previous]:
                        await db.execute(statement)
                await db.execute(
                    "UPDATE schema_meta SET version = ?, migrated_at = ?",
                    (DB_SCHEMA_VERSION, _now()),
                )
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            await db.commit()
        except BaseException:
            await db.close()
            raise
        return cls(db, artifacts_dir)

    async def close(self) -> None:
        """关闭 SQLite 连接。"""
        await self._db.close()

    # -- 运行 ------------------------------------------------------------------

    async def create_run(
        self, run_id: str, workspace: str, workspace_digest: str, goal: str, mode: str
    ) -> None:
        """持久化一条初始状态为 ``CREATED`` 的运行记录。"""
        now = _now()
        await self._db.execute(
            "INSERT INTO runs (id, workspace, workspace_digest, goal, mode, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, workspace, workspace_digest, goal, mode, RunStatus.CREATED.value, now, now),
        )
        await self._db.commit()

    async def update_run_status(self, run_id: str, status: RunStatus, stop_reason: str) -> None:
        """原子更新运行状态、停止原因和更新时间。"""
        await self._db.execute(
            "UPDATE runs SET status = ?, stop_reason = ?, updated_at = ? WHERE id = ?",
            (status.value, stop_reason, _now(), run_id),
        )
        await self._db.commit()

    async def get_run(self, run_id: str) -> RunRecord | None:
        """按 ID 读取运行记录；不存在时返回 ``None``。"""
        cursor = await self._db.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        row = await cursor.fetchone()
        return _row_to_run(row) if row else None

    async def list_runs(self, limit: int) -> list[RunRecord]:
        """按最近更新时间倒序返回最多 ``limit`` 条运行记录。"""
        if limit < 0:
            raise ValueError("limit must be non-negative")
        cursor = await self._db.execute(
            "SELECT * FROM runs ORDER BY updated_at DESC, created_at DESC, id DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [_row_to_run(row) for row in rows]

    async def search_runs(
        self, query: str, limit: int, workspace_digest: str | None = None
    ) -> list[RunRecord]:
        """在数据库全历史中搜索，不将通配符解释成搜索表达式。"""
        if limit < 0:
            raise ValueError("limit must be non-negative")
        needle = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        cursor = await self._db.execute(
            "SELECT * FROM runs WHERE id NOT LIKE 'child-%' "
            "AND (? IS NULL OR workspace_digest = ?) "
            "AND (id || ' ' || goal || ' ' || status) LIKE ? ESCAPE '\\' "
            "ORDER BY updated_at DESC, created_at DESC, id DESC LIMIT ?",
            (workspace_digest, workspace_digest, f"%{needle}%", limit),
        )
        return [_row_to_run(row) for row in await cursor.fetchall()]

    # -- 输入历史 --------------------------------------------------------------

    async def record_input(self, workspace: str, text: str) -> None:
        """将原始输入保存在同一数据库中，并按工作区保留最近 20 条。"""
        await self._db.execute(
            "INSERT INTO input_history (workspace, text) VALUES (?, ?)", (workspace, text)
        )
        await self._db.execute(
            "DELETE FROM input_history WHERE workspace = ? AND id NOT IN "
            "(SELECT id FROM input_history WHERE workspace = ? ORDER BY id DESC LIMIT ?)",
            (workspace, workspace, INPUT_HISTORY_LIMIT),
        )
        await self._db.commit()

    async def load_input_history(self, workspace: str) -> list[str]:
        """按提交顺序读取当前工作区的有限历史。"""
        cursor = await self._db.execute(
            "SELECT text FROM input_history WHERE workspace = ? ORDER BY id", (workspace,)
        )
        return [row["text"] for row in await cursor.fetchall()]

    async def clear_input_history(self, workspace: str) -> None:
        """只删除指定工作区的输入历史；已有会话和其他工作区不变。"""
        await self._db.execute("DELETE FROM input_history WHERE workspace = ?", (workspace,))
        await self._db.commit()

    async def add_project_note(self, workspace: str, text: str) -> ProjectNote:
        normalized = normalize_note_text(text)
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        # 单个 INSERT 的容量判定与插入不可分割；其他连接不能在二者之间插入。
        cursor = await self._db.execute(
            "INSERT INTO project_notes (workspace, text, created_at) SELECT ?, ?, ? "
            "WHERE (SELECT count(*) FROM project_notes WHERE workspace = ?) < ? "
            "AND (SELECT coalesce(sum(length(text)), 0) FROM project_notes "
            "WHERE workspace = ?) + ? <= ?",
            (
                workspace,
                normalized,
                now,
                workspace,
                PROJECT_NOTE_LIMIT,
                workspace,
                len(normalized),
                PROJECT_NOTES_TOTAL_CHARS,
            ),
        )
        await self._db.commit()
        if cursor.rowcount == 0:
            raise ValueError(NOTE_CAPACITY_MESSAGE)
        if cursor.lastrowid is None:
            raise StoreError("project note insert did not return an ID")
        return ProjectNote(id=cursor.lastrowid, text=normalized, created_at=now)

    async def list_project_notes(self, workspace: str) -> tuple[ProjectNote, ...]:
        cursor = await self._db.execute(
            "SELECT id, text, created_at FROM project_notes WHERE workspace = ? ORDER BY id",
            (workspace,),
        )
        return tuple(
            ProjectNote(id=row["id"], text=row["text"], created_at=row["created_at"])
            for row in await cursor.fetchall()
        )

    async def delete_project_note(self, workspace: str, note_id: int) -> bool:
        cursor = await self._db.execute(
            "DELETE FROM project_notes WHERE workspace = ? AND id = ?", (workspace, note_id)
        )
        await self._db.commit()
        return cursor.rowcount == 1

    async def clear_project_notes(self, workspace: str) -> None:
        await self._db.execute("DELETE FROM project_notes WHERE workspace = ?", (workspace,))
        await self._db.commit()

    # -- 事件 ------------------------------------------------------------------

    async def append_event(self, run_id: str, event: ApplicationEvent) -> EventEnvelope:
        """分配运行内递增序号，写入带摘要的事件并返回其信封。"""
        async with self._append_lock:
            payload = event.model_dump_json()
            at = _now()
            cursor = await self._db.execute(
                "INSERT INTO events (run_id, seq, kind, schema_version, payload_json, "
                "payload_digest, created_at) "
                "SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ?, ? "
                "FROM events WHERE run_id = ? RETURNING seq",
                (run_id, event.kind, SCHEMA_VERSION, payload, sha256_text(payload), at, run_id),
            )
            row = await cursor.fetchone()
            await self._db.commit()
            if row is None:
                raise StoreError("event append did not return a sequence")
            seq = int(row["seq"])
        return EventEnvelope(seq=seq, at=at, event=event)

    async def append_handoff(self, event: HandoffUpdated, expected_seq: int) -> EventEnvelope:
        from haven.ports.session import HANDOFF_SAVE_CONFLICT

        async with self._append_lock:
            payload, at = event.model_dump_json(), _now()
            cursor = await self._db.execute(
                "INSERT INTO events (run_id, seq, kind, schema_version, payload_json, "
                "payload_digest, created_at) SELECT ?, ?, ?, ?, ?, ?, ? "
                "WHERE EXISTS (SELECT 1 FROM runs WHERE id = ? "
                "AND status IN ('waiting_user', 'succeeded', 'failed', 'stopped', "
                "'cancelled', 'effect_unknown')) "
                "AND COALESCE((SELECT MAX(seq) FROM events WHERE run_id = ?), 0) = ? "
                "AND EXISTS (SELECT 1 FROM checkpoints WHERE run_id = ? "
                "AND seq = ? AND seq = (SELECT MAX(seq) FROM checkpoints WHERE run_id = ?) "
                "AND json_extract(state_json, '$.parent_run_id') IS NULL) RETURNING seq",
                (
                    event.run_id,
                    expected_seq + 1,
                    event.kind,
                    SCHEMA_VERSION,
                    payload,
                    sha256_text(payload),
                    at,
                    event.run_id,
                    event.run_id,
                    expected_seq,
                    event.run_id,
                    event.checkpoint_seq,
                    event.run_id,
                ),
            )
            row = await cursor.fetchone()
            await self._db.commit()
            if row is None:
                raise ValueError(HANDOFF_SAVE_CONFLICT)
        return EventEnvelope(seq=int(row["seq"]), at=at, event=event)

    async def load_events(self, run_id: str) -> list[EventEnvelope]:
        """按序读取并校验事件摘要，再恢复为领域事件对象。"""
        cursor = await self._db.execute(
            "SELECT seq, payload_json, payload_digest, created_at FROM events "
            "WHERE run_id = ? ORDER BY seq",
            (run_id,),
        )
        rows = await cursor.fetchall()
        envelopes: list[EventEnvelope] = []
        for row in rows:
            payload = str(row["payload_json"])
            if sha256_text(payload) != str(row["payload_digest"]):
                raise StoreError(f"event {run_id}/{row['seq']} failed digest verification")
            event = EVENT_ADAPTER.validate_json(payload)
            envelopes.append(
                EventEnvelope(seq=int(row["seq"]), at=str(row["created_at"]), event=event)
            )
        return envelopes

    # -- 检查点 --------------------------------------------------------------

    async def save_checkpoint(self, checkpoint: CheckpointV1) -> None:
        """保存检查点并删除同一运行更旧的快照，保留最新恢复点。"""
        state_json = checkpoint.model_dump_json()
        await self._db.execute(
            "INSERT OR REPLACE INTO checkpoints (run_id, seq, state_json, checksum, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (checkpoint.run_id, checkpoint.last_seq, state_json, checkpoint.checksum(), _now()),
        )
        # 在同一事务中删除本行所取代的内容。checkpoint 是截至当前 transcript
        # 的快速恢复快照，每个工具批次都会写入一个，因此保留整条链的成本
        # 为 O(run length^2) 字节——实际存储测得占 92%——而 `load_checkpoint`
        # 只会读取 `ORDER BY seq DESC LIMIT 1`。追加式历史属于事件日志，而
        # 不是这张表（ADR 0004）。
        # 严格使用 `<`：更高 seq 的行仍会被 load_checkpoint 优先选择，乱序
        # 保存绝不能删除它。
        await self._db.execute(
            "DELETE FROM checkpoints WHERE run_id = ? AND seq < ?",
            (checkpoint.run_id, checkpoint.last_seq),
        )
        await self._db.commit()

    async def load_checkpoint(self, run_id: str) -> CheckpointV1 | None:
        """读取序号最高的检查点并验证校验和与 schema 版本。"""
        cursor = await self._db.execute(
            "SELECT state_json, checksum FROM checkpoints WHERE run_id = ? "
            "ORDER BY seq DESC LIMIT 1",
            (run_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        # 校验存入的字节，再补齐新版本模型的默认字段。先反序列化再计算摘要会
        # 把新默认值算进去，使尚未包含这些字段的合法旧检查点无法加载。
        state_json = str(row["state_json"])
        if sha256_text(state_json) != str(row["checksum"]):
            raise StoreError(f"checkpoint for {run_id} failed checksum verification")
        checkpoint = CheckpointV1.model_validate_json(state_json)
        if checkpoint.schema_version != 1:
            raise StoreError(f"checkpoint schema {checkpoint.schema_version} is not supported")
        return checkpoint

    # -- 审批 --------------------------------------------------------------------

    async def record_approval(self, approval_id: str, run_id: str, request_digest: str) -> None:
        """记录绑定运行和请求摘要的待审批项。"""
        await self._db.execute(
            "INSERT INTO approvals (id, run_id, request_digest, created_at) VALUES (?, ?, ?, ?)",
            (approval_id, run_id, request_digest, _now()),
        )
        await self._db.commit()

    async def decide_approval(self, approval_id: str, decision: ApprovalDecision) -> None:
        """持久化审批决定及决定时间。"""
        await self._db.execute(
            "UPDATE approvals SET decision = ?, decided_at = ? WHERE id = ?",
            (decision.value, _now(), approval_id),
        )
        await self._db.commit()

    async def consume_approval(self, approval_id: str, request_digest: str) -> bool:
        """通过条件更新进行一次性消费：最多成功一次，并且只接受已审批的精确摘要。"""
        cursor = await self._db.execute(
            "UPDATE approvals SET consumed_at = ? WHERE id = ? AND request_digest = ? "
            "AND decision = ? AND consumed_at IS NULL",
            (_now(), approval_id, request_digest, ApprovalDecision.APPROVED.value),
        )
        await self._db.commit()
        return cursor.rowcount == 1

    # -- 执行 --------------------------------------------------------------------

    async def record_execution(self, record: ExecutionRecord) -> None:
        """写入工具执行记录及其初始副作用摘要。"""
        now = _now()
        await self._db.execute(
            "INSERT INTO executions (call_id, run_id, ticket_digest, tool_name, effect_state, "
            "preimage_digest, postimage_digest, path, dest_path, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.call_id,
                record.run_id,
                record.ticket_digest,
                record.tool_name,
                record.effect_state.value,
                record.preimage_digest,
                record.postimage_digest,
                record.path,
                record.dest_path,
                now,
                now,
            ),
        )
        await self._db.commit()

    async def update_execution_state(
        self,
        run_id: str,
        call_id: str,
        effect_state: EffectState,
        postimage_digest: str = "",
    ) -> None:
        """更新执行副作用状态；空后像摘要保留已有值以支持恢复分类。"""
        # 空 postimage 不能擦除 STARTED 时记录的值（恢复逻辑正是依据预期
        # postimage 进行分类），这与内存存储的语义一致。
        await self._db.execute(
            "UPDATE executions SET effect_state = ?, "
            "postimage_digest = CASE WHEN ? = '' THEN postimage_digest ELSE ? END, "
            "updated_at = ? WHERE run_id = ? AND call_id = ?",
            (effect_state.value, postimage_digest, postimage_digest, _now(), run_id, call_id),
        )
        await self._db.commit()

    async def load_executions(self, run_id: str) -> list[ExecutionRecord]:
        """按创建时间读取指定运行的全部执行记录。"""
        cursor = await self._db.execute(
            "SELECT * FROM executions WHERE run_id = ? ORDER BY created_at", (run_id,)
        )
        rows = await cursor.fetchall()
        return [
            ExecutionRecord(
                call_id=str(row["call_id"]),
                run_id=str(row["run_id"]),
                ticket_digest=str(row["ticket_digest"]),
                tool_name=str(row["tool_name"]),
                effect_state=EffectState(str(row["effect_state"])),
                preimage_digest=str(row["preimage_digest"]),
                postimage_digest=str(row["postimage_digest"]),
                path=str(row["path"]),
                dest_path=str(row["dest_path"]),
            )
            for row in rows
        ]

    # -- 构件 --------------------------------------------------------------------

    async def put_artifact(self, content: bytes) -> str:
        """以 SHA-256 摘要为名写入构件，并返回内容地址。"""
        digest = sha256_bytes(content)
        target = self._artifacts_dir / digest
        if target.is_file() and sha256_bytes(target.read_bytes()) == digest:
            return digest
        fd, staged_name = tempfile.mkstemp(prefix=".artifact-", dir=self._artifacts_dir)
        try:
            with os.fdopen(fd, "wb") as staged:
                staged.write(content)
                staged.flush()
                os.fsync(staged.fileno())
            try:
                os.replace(staged_name, target)
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    shutil.copy2(staged_name, target)
                    Path(staged_name).unlink(missing_ok=True)
                else:
                    raise
        finally:
            Path(staged_name).unlink(missing_ok=True)
        return digest

    async def get_artifact(self, digest: str) -> bytes | None:
        """读取合法摘要对应的构件；非法或不存在时返回 ``None``。"""
        if not _valid_digest(digest):
            return None
        target = self._artifacts_dir / digest
        if not target.is_file():
            return None
        content = target.read_bytes()
        if sha256_bytes(content) != digest:
            raise StoreError(f"artifact {digest} failed digest verification")
        return content

    async def delete_run(self, run_id: str) -> None:
        """在一个事务中删除运行及其事件、检查点、审批和执行记录。"""
        # 一个隐式事务：运行及其所有行要么全部删除，要么全部保留（连接在
        # 末尾一次性提交）。
        for table in ("events", "checkpoints", "approvals", "executions"):
            await self._db.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
        await self._db.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        await self._db.commit()

    async def list_artifacts(self) -> list[str]:
        """返回构件目录中的文件名摘要，按字典序排列。"""
        if not self._artifacts_dir.is_dir():
            return []
        return sorted(
            p.name for p in self._artifacts_dir.iterdir() if p.is_file() and _valid_digest(p.name)
        )

    async def delete_artifact(self, digest: str) -> None:
        """删除合法摘要对应的构件；不存在时保持幂等。"""
        if not _valid_digest(digest):
            return
        (self._artifacts_dir / digest).unlink(missing_ok=True)


def _valid_digest(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _row_to_run(row: aiosqlite.Row) -> RunRecord:
    return RunRecord(
        run_id=str(row["id"]),
        workspace=str(row["workspace"]),
        workspace_digest=str(row["workspace_digest"]),
        goal=str(row["goal"]),
        mode=str(row["mode"]),
        status=RunStatus(str(row["status"])),
        stop_reason=str(row["stop_reason"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
