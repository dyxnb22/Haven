"""Workspace port: bounded, normalized file access inside one repository."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PathFacts:
    """Program-verified facts about one path proposal."""

    raw: str
    normalized: str
    within_workspace: bool
    is_protected: bool
    exists: bool
    is_file: bool
    is_dir: bool
    size_bytes: int
    digest: str | None


@dataclass(frozen=True, slots=True)
class ListEntry:
    name: str
    is_dir: bool
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ListResult:
    path: str
    entries: tuple[ListEntry, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class SearchMatch:
    path: str
    line_number: int
    line: str


@dataclass(frozen=True, slots=True)
class SearchResult:
    matches: tuple[SearchMatch, ...]
    files_scanned: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class ReadResult:
    path: str
    content: str
    start_line: int
    end_line: int
    total_lines: int
    truncated: bool
    digest: str


@dataclass(frozen=True, slots=True)
class EditPreview:
    path: str
    diff: str
    preimage_digest: str
    postimage_digest: str
    insertions: int
    deletions: int


@dataclass(frozen=True, slots=True)
class EditOutcome:
    path: str
    preimage_digest: str
    postimage_digest: str


@dataclass(frozen=True, slots=True)
class RunDiff:
    diff: str
    files: tuple[str, ...] = field(default_factory=tuple)
    insertions: int = 0
    deletions: int = 0
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """A point-in-time view of the workspace, used to attribute process writes.

    `digests` covers every regular file, so a change to any of them — text or
    binary — is detectable. `contents` holds only the diffable subset (valid
    UTF-8 under the edit size cap), so the run diff can render what changed.
    """

    digests: dict[str, str] = field(default_factory=dict)
    contents: dict[str, str] = field(default_factory=dict)


class WorkspaceError(Exception):
    """Raised for workspace violations; always maps to a stable tool error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class WorkspacePort(Protocol):
    @property
    def root(self) -> Path: ...

    @property
    def workspace_digest(self) -> str: ...

    def path_facts(self, raw_path: str) -> PathFacts: ...

    async def list_dir(self, path: str, max_entries: int) -> ListResult: ...

    async def search(self, pattern: str, path: str, max_results: int) -> SearchResult: ...

    async def read_file(self, path: str, start_line: int, max_lines: int) -> ReadResult: ...

    async def preview_edit(
        self,
        path: str,
        old: str,
        new: str,
        *,
        occurrence: int | None = None,
        replace_all: bool = False,
    ) -> EditPreview: ...

    async def apply_edit(
        self,
        path: str,
        old: str,
        new: str,
        expected_preimage: str,
        *,
        occurrence: int | None = None,
        replace_all: bool = False,
    ) -> EditOutcome: ...

    async def preview_create(self, path: str, content: str) -> EditPreview: ...

    async def apply_create(self, path: str, content: str) -> EditOutcome: ...

    async def run_diff(self) -> RunDiff: ...

    def original_contents(self) -> dict[str, str]: ...

    def restore_originals(self, originals: dict[str, str]) -> None: ...

    def capture_snapshot(self) -> WorkspaceSnapshot: ...

    def register_run_original(self, path: str, content: str) -> None: ...
