"""类型化标识符，避免混淆 run/step/call/session id。"""

from __future__ import annotations

import uuid
from typing import NewType

RunId = NewType("RunId", str)
StepId = NewType("StepId", str)
ToolCallId = NewType("ToolCallId", str)
ApprovalId = NewType("ApprovalId", str)


def new_run_id() -> RunId:
    """生成带 ``run-`` 前缀的短随机运行标识。"""
    return RunId(f"run-{uuid.uuid4().hex[:12]}")


def new_approval_id() -> ApprovalId:
    """生成带 ``apr-`` 前缀的短随机审批标识。"""
    return ApprovalId(f"apr-{uuid.uuid4().hex[:12]}")
