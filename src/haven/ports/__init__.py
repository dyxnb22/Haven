"""Ports owned by the core; adapters implement these protocols.

Each port is the application layer's entire knowledge of the outside world:

    model.py       stream one completion (ModelPort) + ProviderError taxonomy
    workspace.py   bounded file access: read/search/edit/patch previews and
                   applies, path facts, run-scoped diff
    executor.py    run a registered recipe / a sandboxed command
    sandbox.py     wrap an argv so the OS confines the child process
    session.py     durable store: runs, events, checkpoints, approvals,
                   execution journal, artifacts
    event_sink.py  where emitted envelopes go (UI, JSONL, replay)
    clock.py       injectable time

Swapping an adapter (e.g. SQLite -> memory in tests) can change performance
and persistence, never permissions or evidence rules - those live in domain.
"""

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
