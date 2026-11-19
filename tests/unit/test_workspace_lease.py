"""工作区单写者租约：第一个写者获胜，过期持有者会被清理，竞争者可以获知持有者。"""

import json
import os
import socket
import subprocess
import sys
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
    with pytest.raises(LeaseHeld):
        acquire_workspace_lease(tmp_path / "ws", tmp_path / "leases")
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
    payload["token"] = "replacement-owner"
    lease.path.write_text(json.dumps(payload))
    lease.release()
    assert lease.path.exists(), "release must not remove a lease we no longer hold"


def _write_lease(path: Path, pid: int, *, heartbeat_at: datetime | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "workspace": str(path.parent),
                "pid": pid,
                "host": os.uname().nodename if hasattr(os, "uname") else "localhost",
                "token": "left-behind",
                "acquired_at": datetime.now(UTC).isoformat(),
                "heartbeat_at": (heartbeat_at or datetime.now(UTC)).isoformat(),
            }
        ),
        encoding="utf-8",
    )


class TestAStaleLeaseIsBreakable:
    """崩溃留下的租约必须能被接管，否则它会永久堵死这个工作区。

    这里的关键是"pid 还活着但已经不是 Haven 了"——pid 会被复用，而
    `os.kill(pid, 0)` 只回答"这个 pid 有没有进程"，不回答"它是不是原来的持有者"。
    """

    def test_a_zombie_stat_is_recognized(self, tmp_path: Path) -> None:
        """僵尸进程已经死了、只是还没被回收，而 `os.kill(pid, 0)` 对它照样成功。

        不识别的话，一个崩溃后没人 wait 的 Haven 进程会永远占着租约，且没有任何
        自动解法。这里直接喂 `/proc/<pid>/stat` 的内容，所以不依赖运行平台。
        """
        import haven.adapters.workspace_lease as lease_module

        zombie_stat = "1234 (python) Z 1 1 0 0 -1 0 0 0 0 0 0 0 0 0 0 1 0 0 0 0\n"
        original = Path.read_text

        def fake_read(self: Path, *args: object, **kwargs: object) -> str:  # type: ignore[no-untyped-def]
            if str(self).startswith("/proc/"):
                return zombie_stat
            return original(self, *args, **kwargs)  # type: ignore[arg-type]

        lease_module.Path.read_text = fake_read  # type: ignore[method-assign]
        try:
            assert lease_module._is_zombie(1234) is True
        finally:
            lease_module.Path.read_text = original  # type: ignore[method-assign]

    def test_a_running_stat_is_not_a_zombie(self) -> None:
        """comm 里可能含空格和括号，所以状态位要从最后一个 ')' 之后取。"""
        import haven.adapters.workspace_lease as lease_module

        # 一个进程名里带空格和括号的正常运行进程
        running = "4321 (weird (name) here) S 1 1 0 0 -1 0 0 0 0 0 0 0 0 0 0 1 0 0 0 0\n"
        original = Path.read_text

        def fake_read(self: Path, *args: object, **kwargs: object) -> str:  # type: ignore[no-untyped-def]
            if str(self).startswith("/proc/"):
                return running
            return original(self, *args, **kwargs)  # type: ignore[arg-type]

        lease_module.Path.read_text = fake_read  # type: ignore[method-assign]
        try:
            assert lease_module._is_zombie(4321) is False
        finally:
            lease_module.Path.read_text = original  # type: ignore[method-assign]

    def test_a_missing_proc_entry_is_not_a_zombie(self) -> None:
        """没有 /proc 的平台（macOS）无从判断，必须返回 False 而不是抛异常。"""
        from haven.adapters.workspace_lease import _is_zombie

        assert _is_zombie(999_999) is False

    def test_a_zombie_holder_does_not_block_acquisition(self) -> None:
        """`_is_live` 必须把僵尸判成死，否则租约永远收不回来。"""
        import haven.adapters.workspace_lease as lease_module

        zombie_stat = "1234 (python) Z 1 1 0 0 -1 0 0 0 0 0 0 0 0 0 0 1 0 0 0 0\n"
        original = Path.read_text

        def fake_read(self: Path, *args: object, **kwargs: object) -> str:  # type: ignore[no-untyped-def]
            if str(self).startswith("/proc/"):
                return zombie_stat
            return original(self, *args, **kwargs)  # type: ignore[arg-type]

        # host 必须与本机一致才会走 pid 探测分支；pid 用一个真实活着的子进程，
        # 于是唯一让结论变成"死"的理由就是僵尸识别（`pid == os.getpid()` 会提前返回）。
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            payload = {
                "host": socket.gethostname(),
                "pid": child.pid,
                "token": "left-behind",
                "heartbeat_at": datetime.now(UTC).isoformat(),
            }
            lease_module.Path.read_text = fake_read  # type: ignore[method-assign]
            try:
                assert lease_module._is_live(payload) is False
            finally:
                lease_module.Path.read_text = original  # type: ignore[method-assign]
        finally:
            child.terminate()
            child.wait()

    def test_the_error_names_the_lease_file_so_the_user_can_act(self, tmp_path: Path) -> None:
        """光说"去把那个进程停掉"不够，而且常常是错的：pid 会被复用。

        消息必须把租约文件点出来，用户才能自己判断该不该删它。
        """
        from haven.domain.digest import sha256_text

        workspace = tmp_path / "ws"
        leases = tmp_path / "leases"
        # 路径和 acquire 用同一套规则算出来，否则写的不是同一个文件
        key = sha256_text(str(workspace.resolve()))[:24]
        path = leases / f"{key}.json"
        # 一个活着的、但显然不是 Haven 的 pid（launchd / init）
        _write_lease(path, 1)

        with pytest.raises(LeaseHeld) as exc:
            acquire_workspace_lease(tmp_path / "ws", leases)

        message = str(exc.value)
        assert str(path) in message, "必须把租约文件路径说出来"
        assert "remove the lease file" in message
        assert exc.value.lease_path == path
