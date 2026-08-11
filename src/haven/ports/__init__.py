"""Ports owned by the core; adapters implement these protocols."""

from haven.ports.clock import ClockPort
from haven.ports.event_sink import EventSinkPort
from haven.ports.executor import CheckOutcome, ExecutorPort
from haven.ports.model import ModelPort, ProviderError, ProviderErrorCode
from haven.ports.session import ExecutionRecord, RunRecord, SessionStorePort
from haven.ports.workspace import (
    EditOutcome,
    EditPreview,
    ListEntry,
    ListResult,
    PathFacts,
    ReadResult,
    RunDiff,
    SearchMatch,
    SearchResult,
    WorkspaceError,
    WorkspacePort,
)

__all__ = [
    "CheckOutcome",
    "ClockPort",
    "EditOutcome",
    "EditPreview",
    "EventSinkPort",
    "ExecutionRecord",
    "ExecutorPort",
    "ListEntry",
    "ListResult",
    "ModelPort",
    "PathFacts",
    "ProviderError",
    "ProviderErrorCode",
    "ReadResult",
    "RunDiff",
    "RunRecord",
    "SearchMatch",
    "SearchResult",
    "SessionStorePort",
    "WorkspaceError",
    "WorkspacePort",
]
