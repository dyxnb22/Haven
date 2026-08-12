"""Sandbox port: how a child process is confined by the operating system.

A launcher turns a command into a wrapped command. Keeping it a pure
transformation means the profile can be asserted in a test without running
anything, and the only thing that ever executes is a program the OS is already
holding to a policy.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    """What one confined process may touch."""

    workspace_root: Path
    #: Writable temp directory, so tools that must write somewhere do not need
    #: access outside the workspace.
    scratch_dir: Path
    writable: bool
    allow_network: bool = False
    #: Never readable. The workspace and scratch grants re-open the parts a run
    #: legitimately needs, which is how a workspace inside $HOME keeps working.
    private_roots: tuple[Path, ...] = ()
    #: Readable beyond the system roots — the Python prefix, so an interpreter
    #: living under $HOME stays executable.
    extra_readable_roots: tuple[Path, ...] = ()
    #: Kept in step with FsWorkspace.PROTECTED_COMPONENTS: git history, the
    #: local data dir, and the project config a run must never rewrite.
    protected_subpaths: tuple[str, ...] = (".git", ".haven", ".haven.toml")


class SandboxLauncher(Protocol):
    @property
    def backend(self) -> str: ...

    def available(self) -> bool: ...

    def wrap(self, argv: tuple[str, ...], spec: SandboxSpec) -> tuple[str, ...]: ...

    def describe(self, spec: SandboxSpec) -> str: ...


def default_private_roots() -> tuple[Path, ...]:
    """The user's home directory, where credentials actually live."""
    try:
        return (Path.home(),)
    except RuntimeError:
        return ()


def default_readable_roots() -> tuple[Path, ...]:
    """The running interpreter's prefixes, so a virtualenv under $HOME can
    still be executed by a check recipe."""
    roots = {Path(sys.prefix), Path(sys.base_prefix), Path(sys.executable).parent}
    return tuple(sorted(roots))
