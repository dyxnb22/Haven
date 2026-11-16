"""逐文件/hunk 查看，以及验证后的隔离工作树合流审批。"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from haven.application.emitter import EventEmitter
from haven.application.recovery_service import RecoveryService
from haven.contracts.checkpoint import CheckpointV1
from haven.contracts.events import ReviewRequested, ReviewResolved
from haven.contracts.review import ReviewSnapshot
from haven.domain.digest import digest_of
from haven.domain.enums import RunStatus
from haven.domain.evidence import evaluate_evidence_gate
from haven.ports.session import SessionStorePort
from haven.ports.workspace import WorkspacePort


@dataclass(frozen=True)
class FileDiff:
    path: str
    text: str
    hunks: tuple[str, ...]


def parse_diff(text: str) -> tuple[FileDiff, ...]:
    """按 unified diff 行数识别文件边界，正文伪造的 ---/+++ 不会成为文件头。"""
    lines = text.splitlines(keepends=True)
    files: list[FileDiff] = []
    start = 0
    path = ""
    old_left = new_left = 0

    def add(end: int) -> None:
        if not path:
            return
        block = "".join(lines[start:end])
        offsets = [match.start() for match in re.finditer(r"(?m)^@@ ", block)]
        hunks = tuple(
            block[left:right]
            for left, right in zip(offsets, [*offsets[1:], len(block)], strict=False)
        )
        files.append(FileDiff(path, block, hunks or (block,)))

    index = 0
    while index < len(lines):
        line = lines[index]
        if (
            old_left == new_left == 0
            and line.startswith("--- ")
            and index + 1 < len(lines)
            and lines[index + 1].startswith("+++ ")
        ):
            add(index)
            start = index
            new = lines[index + 1][4:].rstrip("\r\n")
            old = line[4:].rstrip("\r\n")
            path = (
                (old[2:] if old.startswith("a/") else old)
                if new == "/dev/null"
                else (new[2:] if new.startswith("b/") else new)
            )
            index += 2
            continue
        hunk = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if hunk:
            old_left = int(hunk[2] or "1")
            new_left = int(hunk[4] or "1")
        elif old_left or new_left:
            if line.startswith((" ", "-")):
                old_left = max(0, old_left - 1)
            if line.startswith((" ", "+")):
                new_left = max(0, new_left - 1)
        index += 1
    add(len(lines))
    return tuple(files)


ApplyFiles = Callable[[tuple[str, ...], dict[str, str], dict[str, str]], Awaitable[list[str]]]


class ReviewService:
    def __init__(
        self,
        store: SessionStorePort,
        workspace: WorkspacePort,
        main: WorkspacePort,
        apply: ApplyFiles,
        emitter: EventEmitter | None = None,
    ) -> None:
        self._store = store
        self._workspace = workspace
        self._main = main
        self._apply = apply
        self._emitter = emitter if emitter is not None else EventEmitter(store, [])

    async def _capture(self, checkpoint: CheckpointV1) -> ReviewSnapshot:
        if checkpoint.status != RunStatus.SUCCEEDED.value or checkpoint.parent_run_id:
            raise ValueError("only a successful user run can be reconciled")
        if checkpoint.workspace_digest != self._workspace.workspace_digest:
            raise ValueError("review workspace identity changed")
        await RecoveryService(self._store, self._workspace).build_context(checkpoint)
        diff = await self._workspace.run_diff()
        ledger = checkpoint.evidence.to_domain()
        gate = evaluate_evidence_gate(ledger, diff.diff, verification_available=True)
        if not gate.passed:
            raise ValueError(f"review evidence is incomplete: {gate.reason_code}")
        first: dict[str, str] = {}
        final: dict[str, str] = {}
        for edit in ledger.edits:
            first.setdefault(edit.path, edit.preimage_digest)
            final[edit.path] = edit.postimage_digest
        preimages: dict[str, str] = {}
        postimages: dict[str, str] = {}
        for path in diff.files:
            local = self._workspace.path_facts(path)
            main = self._main.path_facts(path)
            if (
                not local.within_workspace
                or local.is_protected
                or not main.within_workspace
                or main.is_protected
            ):
                raise ValueError(f"unsafe reconciliation path: {path}")
            if (local.digest or "") != final.get(path):
                raise ValueError(f"{path} changed since verification; run verification again")
            if (main.digest or "") != first.get(path):
                raise ValueError(f"{path} changed in the main workspace; refusing to overwrite")
            preimages[path] = main.digest or ""
            postimages[path] = local.digest or ""
        binding = digest_of(
            {"run": checkpoint.run_id, "before": preimages, "after": postimages, "diff": diff.diff}
        )
        return ReviewSnapshot(
            run_id=checkpoint.run_id,
            binding=binding,
            files=diff.files,
            diff=diff.diff,
            preimages=preimages,
            postimages=postimages,
            truncated=diff.truncated,
        )

    async def prepare(self, run_id: str) -> ReviewSnapshot:
        checkpoint = await self._store.load_checkpoint(run_id)
        if checkpoint is None:
            raise ValueError("no checkpoint to review")
        if checkpoint.review and (checkpoint.review.applied or checkpoint.review.discarded):
            raise ValueError("this review has already been applied")
        snapshot = await self._capture(checkpoint)
        await self._store.save_checkpoint(checkpoint.model_copy(update={"review": snapshot}))
        await self._emitter.emit(
            run_id, ReviewRequested(run_id=run_id, binding=snapshot.binding, files=snapshot.files)
        )
        return snapshot

    async def apply(self, run_id: str, binding: str) -> list[str]:
        checkpoint = await self._store.load_checkpoint(run_id)
        if checkpoint is None or checkpoint.review is None:
            raise ValueError("prepare a review before applying changes")
        saved = checkpoint.review
        if saved.applied or saved.discarded or saved.binding != binding:
            raise ValueError("stale or already applied review")
        current = await self._capture(checkpoint)
        if current.binding != binding:
            raise ValueError("changes no longer match the reviewed snapshot")
        paths = await self._apply(current.files, current.preimages, current.postimages)
        await self._store.save_checkpoint(
            checkpoint.model_copy(
                update={
                    "review": current.model_copy(update={"applied": True}),
                }
            )
        )
        await self._emitter.emit(
            run_id, ReviewResolved(run_id=run_id, binding=binding, accepted=True)
        )
        return paths
