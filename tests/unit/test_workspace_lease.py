"""工作区单写者租约：第一个写者获胜，过期持有者会被清理，竞争者可以获知持有者。"""

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
    """相同 pid 会被视为本进程重新获取；通过为确定存在的 pid（pid 1 始终存在）写入
    租约来模拟另一个活动进程——此时由新鲜的心跳判断其存活。"""
    leases = tmp_path / "leases"
    lease = acquire_workspace_lease(tmp_path / "ws", leases)
    payload = json.loads(lease.path.read_text())
    payload["pid"] = 1  # init：在所有 Unix 上都存活，我们无法杀死
    lease.path.write_text(json.dumps(payload))

    with pytest.raises(LeaseHeld) as err:
        acquire_workspace_lease(tmp_path / "ws", leases)
    assert err.value.holder_pid == 1


def test_a_dead_holders_lease_is_broken(tmp_path: Path) -> None:
    leases = tmp_path / "leases"
    first = acquire_workspace_lease(tmp_path / "ws", leases)
    payload = json.loads(first.path.read_text())
    payload["pid"] = 2**22 + 12345  # 超过默认 pid_max：不可能是存活的 pid
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
