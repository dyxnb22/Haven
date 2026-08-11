"""Typed identifiers so run/step/call/session ids are never mixed up."""

from __future__ import annotations

import uuid
from typing import NewType

RunId = NewType("RunId", str)
StepId = NewType("StepId", str)
ToolCallId = NewType("ToolCallId", str)
ApprovalId = NewType("ApprovalId", str)


def new_run_id() -> RunId:
    return RunId(f"run-{uuid.uuid4().hex[:12]}")


def new_approval_id() -> ApprovalId:
    return ApprovalId(f"apr-{uuid.uuid4().hex[:12]}")
