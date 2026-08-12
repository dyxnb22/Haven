"""Single-writer lease for a workspace, across Haven processes on one machine.

Everything inside one process is already consistent (preimage pins, TOCTOU
re-verification, atomic writes). What none of that covers is a *second Haven
process* mutating the same workspace between another run's approval and its
execution — the classic cross-process race the external audits flagged. The
lease makes the single-writer assumption explicit: the first interactive
process holds it; a second one must run read-only.

Scope is deliberately local: one machine, advisory, keyed by the resolved
workspace path. It is not a distributed lock and does not claim to be.
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from haven.domain.digest import sha256_text

#: A holder that has not heartbeat for this long is presumed dead even when
#: its pid cannot be probed (e.g. a different user's process).
STALE_AFTER_SECONDS = 15 * 60.0


class LeaseHeld(Exception):
    """Another live Haven process holds the write lease for this workspace."""

    def __init__(self, holder_pid: int, holder_host: str, since: str) -> None:
        super().__init__(
            f"workspace is write-leased by pid {holder_pid} on {holder_host} "
            f"(since {since}); run read-only or stop that process"
        )
        self.holder_pid = holder_pid
        self.holder_host = holder_host


@dataclass
class WorkspaceLease:
    """A held lease. Call `refresh()` on activity and `release()` on exit."""

    path: Path
    workspace: str
    pid: int

    def refresh(self) -> None:
        payload = _read(self.path)
        if payload is None or not self._is_mine():
            return
        payload["heartbeat_at"] = _now()
        _write_atomic(self.path, payload)

    def release(self) -> None:
        if self._is_mine():
            self.path.unlink(missing_ok=True)

    def _is_mine(self) -> bool:
        payload = _read(self.path)
        if payload is None:
            return False
        return _pid_of(payload) == self.pid and payload.get("host", "") == socket.gethostname()


def acquire_workspace_lease(workspace_root: Path, leases_dir: Path) -> WorkspaceLease:
    """Acquire the single-writer lease for `workspace_root` or raise LeaseHeld.

    A lease left by a dead process (pid gone, or heartbeat older than
    STALE_AFTER_SECONDS) is broken and taken over; a live holder wins.
    """
    leases_dir.mkdir(parents=True, exist_ok=True)
    key = sha256_text(str(workspace_root.resolve()))[:24]
    path = leases_dir / f"{key}.json"

    existing = _read(path)
    if existing is not None and _is_live(existing):
        raise LeaseHeld(
            _pid_of(existing),
            str(existing.get("host", "?")),
            str(existing.get("acquired_at", "?")),
        )

    workspace = str(workspace_root.resolve())
    payload: dict[str, object] = {
        "workspace": workspace,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "acquired_at": _now(),
        "heartbeat_at": _now(),
    }
    _write_atomic(path, payload)
    # Read back and confirm we won: two processes breaking the same stale
    # lease at once both rename, but exactly one write lands last and a
    # loser must not believe it holds the lease.
    final = _read(path)
    if final is None or _pid_of(final) != os.getpid():
        raise LeaseHeld(
            _pid_of(final or {}),
            str((final or {}).get("host", "?")),
            str((final or {}).get("acquired_at", "?")),
        )
    return WorkspaceLease(path=path, workspace=workspace, pid=os.getpid())


def _read(path: Path) -> dict[str, object] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _pid_of(payload: dict[str, object]) -> int:
    raw = payload.get("pid", -1)
    return raw if isinstance(raw, int) else -1


def _is_live(payload: dict[str, object]) -> bool:
    if str(payload.get("host", "")) != socket.gethostname():
        # A different machine's holder cannot be probed; trust the heartbeat.
        return _heartbeat_age(payload) < STALE_AFTER_SECONDS
    pid = _pid_of(payload)
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The pid exists but belongs to another user; fall back to heartbeat.
        return _heartbeat_age(payload) < STALE_AFTER_SECONDS
    # Same host and the pid answers the probe: the holder is alive, full stop.
    # The heartbeat must NOT overrule a live probe — holders do not refresh on
    # a timer, so a long interactive session would otherwise look "stale" and
    # have its lease stolen mid-run, which is exactly what the lease prevents.
    return True


def _heartbeat_age(payload: dict[str, object]) -> float:
    raw = str(payload.get("heartbeat_at", ""))
    try:
        beat = datetime.fromisoformat(raw)
    except ValueError:
        return float("inf")
    return max(0.0, (datetime.now(UTC) - beat).total_seconds())


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    tmp = path.with_suffix(f".tmp-{os.getpid()}-{time.monotonic_ns()}")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _now() -> str:
    return datetime.now(UTC).isoformat()
