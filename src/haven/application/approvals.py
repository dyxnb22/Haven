"""Approval brokering between the pipeline and the human (or a test policy)."""

from __future__ import annotations

import asyncio
from typing import Literal, Protocol

from haven.domain.approval import ApprovalRequest
from haven.domain.enums import ApprovalDecision


class ApprovalResponder(Protocol):
    """Answers one approval request. Implemented by the TUI bridge, the
    headless CLI (always reject), and scripted eval policies."""

    async def respond(self, request: ApprovalRequest) -> ApprovalDecision: ...


class AutoApprover:
    """Deterministic approval policy for tests and offline eval."""

    def __init__(self, policy: Literal["approve_all", "reject_all"] = "approve_all") -> None:
        self._policy = policy
        self.seen: list[ApprovalRequest] = []

    async def respond(self, request: ApprovalRequest) -> ApprovalDecision:
        self.seen.append(request)
        if self._policy == "approve_all":
            return ApprovalDecision.APPROVED
        return ApprovalDecision.REJECTED


class QueueApprovalBroker:
    """Bridges approvals to an interactive UI.

    The pipeline awaits `respond()`; the UI resolves the pending future via
    `resolve()` when the human decides. Cancellation propagates naturally.
    """

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[ApprovalDecision]] = {}

    async def respond(self, request: ApprovalRequest) -> ApprovalDecision:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalDecision] = loop.create_future()
        self._pending[request.approval_id] = future
        try:
            return await future
        finally:
            self._pending.pop(request.approval_id, None)

    def resolve(self, approval_id: str, decision: ApprovalDecision) -> bool:
        future = self._pending.get(approval_id)
        if future is None or future.done():
            return False
        future.set_result(decision)
        return True

    @property
    def pending_ids(self) -> tuple[str, ...]:
        return tuple(self._pending)
