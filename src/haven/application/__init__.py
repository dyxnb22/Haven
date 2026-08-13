"""Application layer: use cases orchestrating domain logic through ports.

The two files that define the system live here:

    run_service.py    the bounded agent loop (one turn = context -> model ->
                      tools -> evidence -> checkpoint)
    tool_pipeline.py  the single execution channel every model-proposed
                      action must pass through

supported by: context_builder (what the model sees), compaction (dropped
history becomes recorded facts), approvals (broker between pipeline and
human/auto policies), registry (static tool lookup + strict validation),
recovery_service (crash recovery + user rewind), replay_service (journal
projection), maintenance (haven gc: prune runs + sweep artifacts),
profiles (per-model defaults), emitter (persist + fan out events),
state (the mutable per-run RunContext).

This layer knows domain, ports, and contracts - never a concrete adapter;
bootstrap.py injects those.
"""

from haven.application.approvals import ApprovalResponder, AutoApprover, QueueApprovalBroker
from haven.application.context_builder import ContextBuilder
from haven.application.emitter import EventEmitter
from haven.application.profiles import DEFAULT_PROFILE, ModelProfile, profile_for
from haven.application.recovery_service import (
    EffectFinding,
    RecoveryReport,
    RecoveryService,
)
from haven.application.registry import ToolRegistry, ValidationFailure
from haven.application.replay_service import ReplayService
from haven.application.run_service import (
    RunOutcome,
    RunService,
    build_run_context_from_checkpoint,
)
from haven.application.state import RunContext
from haven.application.tool_pipeline import ToolExecution, ToolPipeline

__all__ = [
    "DEFAULT_PROFILE",
    "ApprovalResponder",
    "AutoApprover",
    "ContextBuilder",
    "EffectFinding",
    "EventEmitter",
    "ModelProfile",
    "QueueApprovalBroker",
    "RecoveryReport",
    "RecoveryService",
    "ReplayService",
    "RunContext",
    "RunOutcome",
    "RunService",
    "ToolExecution",
    "ToolPipeline",
    "ToolRegistry",
    "ValidationFailure",
    "build_run_context_from_checkpoint",
    "profile_for",
]
