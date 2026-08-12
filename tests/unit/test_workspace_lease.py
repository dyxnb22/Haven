"""The single-writer workspace lease: first writer wins, stale holders are
broken, contenders learn who holds it."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from haven.adapters.workspace_lease import (
    LeaseHeld,
    acquire_workspace_lease,
)


def test_acquire_and_release(tmp_path: Path) -> None:
    lease = acquire_workspace_lease(tmp_path / "ws", tmp_path / "leases")
    assert lease.pid == os.getpid()
    assert lease.path.is_file()
    lease.release()
    assert not lease.path.exists()


def test_a_live_holder_blocks_a_second_acquire(tmp_path: Path) -> None:
    """Same pid is treated as re-acquire by us; simulate another live process
    by writing a lease for a pid that provably exists (pid 1 always does) —
    liveness is then decided by the heartbeat, which is fresh."""
    leases = tmp_path / "leases"
    lease = acquire_workspace_lease(tmp_path / "ws", leases)
    payload = json.loads(lease.path.read_text())
    payload["pid"] = 1  # init: alive on every unix, unkillable-by-us
    lease.path.write_text(json.dumps(payload))

    with pytest.raises(LeaseHeld) as err:
        acquire_workspace_lease(tmp_path / "ws", leases)
    assert err.value.holder_pid == 1


def test_a_dead_holders_lease_is_broken(tmp_path: Path) -> None:
    leases = tmp_path / "leases"
    first = acquire_workspace_lease(tmp_path / "ws", leases)
    payload = json.loads(first.path.read_text())
    payload["pid"] = 2**22 + 12345  # beyond default pid_max: never a live pid
    first.path.write_text(json.dumps(payload))

    second = acquire_workspace_lease(tmp_path / "ws", leases)
    assert second.pid == os.getpid()
    second.release()


def test_a_stale_heartbeat_from_another_host_is_broken(tmp_path: Path) -> None:
    leases = tmp_path / "leases"
    lease = acquire_workspace_lease(tmp_path / "ws", leases)
    payload = json.loads(lease.path.read_text())
    payload["host"] = "some-other-machine"
    payload["heartbeat_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    lease.path.write_text(json.dumps(payload))

    taken = acquire_workspace_lease(tmp_path / "ws", leases)
    assert taken.pid == os.getpid()


def test_a_fresh_heartbeat_from_another_host_blocks(tmp_path: Path) -> None:
    leases = tmp_path / "leases"
    lease = acquire_workspace_lease(tmp_path / "ws", leases)
    payload = json.loads(lease.path.read_text())
    payload["host"] = "some-other-machine"
    payload["heartbeat_at"] = datetime.now(UTC).isoformat()
    lease.path.write_text(json.dumps(payload))

    with pytest.raises(LeaseHeld):
        acquire_workspace_lease(tmp_path / "ws", leases)


def test_refresh_updates_the_heartbeat(tmp_path: Path) -> None:
    lease = acquire_workspace_lease(tmp_path / "ws", tmp_path / "leases")
    before = json.loads(lease.path.read_text())["heartbeat_at"]
    lease.refresh()
    after = json.loads(lease.path.read_text())["heartbeat_at"]
    assert after >= before


def test_release_leaves_someone_elses_lease_alone(tmp_path: Path) -> None:
    lease = acquire_workspace_lease(tmp_path / "ws", tmp_path / "leases")
    payload = json.loads(lease.path.read_text())
    payload["pid"] = 1
    lease.path.write_text(json.dumps(payload))
    lease.release()
    assert lease.path.exists(), "release must not remove a lease we no longer hold"
