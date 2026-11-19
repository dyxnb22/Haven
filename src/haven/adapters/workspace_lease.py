"""跨同一台机器上的 Haven 进程使用的工作区单写者租约。

单个进程内部的状态已经一致（preimage 固定、TOCTOU 重新验证、原子写入）。但这些
机制无法覆盖另一条运行在审批和执行之间由“第二个 Haven 进程”修改同一工作区的情况，
这正是外部审计指出的典型跨进程竞态。租约明确了单写者假设：第一个交互进程持有租约；
第二个进程必须以只读模式运行。

作用域有意限定在本地：一台机器，提示性使用，以解析后的工作区路径为键。它不是分布式
锁，也不声称自己是分布式锁。
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import secrets
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from haven.domain.digest import sha256_text

_fcntl = importlib.import_module("fcntl") if os.name == "posix" else None

#: 持有者在这么长时间内没有心跳时，即使无法探测其 pid（例如它属于另一位
#: 用户），也视为已死亡。
STALE_AFTER_SECONDS = 15 * 60.0
_HELD_TOKENS: set[str] = set()


class LeaseHeld(Exception):
    """另一个存活的 Haven 进程持有此工作区的写入租约。"""

    def __init__(
        self,
        holder_pid: int,
        holder_host: str,
        since: str,
        lease_path: Path | None = None,
    ) -> None:
        detail = f"workspace is write-leased by pid {holder_pid} on {holder_host} (since {since})"
        # 只说"去把那个进程停掉"是不够的，而且常常是错的：pid 会被复用，持有者也
        # 可能是崩溃后没人回收的僵尸。把租约文件点出来，用户才能自己判断该不该动它。
        if lease_path is not None:
            detail += f"; lease file: {lease_path}"
        super().__init__(
            f"{detail}; run read-only, stop that process, or remove the lease file "
            f"if pid {holder_pid} is no longer a Haven process"
        )
        self.holder_pid = holder_pid
        self.holder_host = holder_host
        self.lease_path = lease_path


@dataclass
class WorkspaceLease:
    """运行期间持有的工作区锁，防止并发运行互相覆盖文件。

    活动时调用 ``refresh()``，退出时调用 ``release()``；租约对象只代表已经
    成功取得的本地单写者租约。
    """

    #: 进程共享租约目录中的租约文件路径。
    path: Path
    #: 写入租约内容的规范化工作区路径。
    workspace: str
    #: 记录为租约持有者的本地进程 ID。
    pid: int
    #: 区分同一 pid 中不同租约实例及 PID 复用的随机所有权令牌。
    token: str
    #: 持有者生命周期内一直开着的标记文件描述符。它比 pid 更能说明"持有者还活着"：
    #   pid 会被复用，而复用那个 pid 的无关进程并不会继承这个 fd——原进程一死
    #   （包括被 kill -9，没人调用 release），内核就把锁放了。
    _marker_fd: int | None = None

    def refresh(self) -> None:
        """刷新当前租约心跳；租约已被替换或删除时不再写回。"""
        with _acquire_guard(self.path):
            payload = _read(self.path)
            if payload is None or not self._is_mine(payload):
                return
            payload["heartbeat_at"] = _now()
            _write_atomic(self.path, payload)

    def release(self) -> None:
        """释放仍由当前进程持有的租约，重复调用保持幂等。"""
        try:
            with _acquire_guard(self.path):
                if self._is_mine():
                    self.path.unlink(missing_ok=True)
                    _unlink_missing_ok(_hold_marker(self.path))
        finally:
            self._drop_marker()
            _HELD_TOKENS.discard(self.token)

    def _drop_marker(self) -> None:
        """关掉持有标记；幂等。"""
        if self._marker_fd is None:
            return
        fd, self._marker_fd = self._marker_fd, None
        if _fcntl is not None:
            with contextlib.suppress(OSError):
                _fcntl.flock(fd, _fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)

    def _is_mine(self, payload: dict[str, object] | None = None) -> bool:
        if payload is None:
            payload = _read(self.path)
        if payload is None:
            return False
        return (
            _pid_of(payload) == self.pid
            and payload.get("host", "") == socket.gethostname()
            and payload.get("token", "") == self.token
            and payload.get("workspace", "") == self.workspace
        )


def acquire_workspace_lease(workspace_root: Path, leases_dir: Path) -> WorkspaceLease:
    """获取 `workspace_root` 的单写者租约，否则抛出 LeaseHeld。

    已死亡进程遗留的租约（pid 已消失，或心跳早于 STALE_AFTER_SECONDS）会被解除并接管；
    对于仍存活的持有者，则以持有者为准。
    """
    leases_dir.mkdir(parents=True, exist_ok=True)
    key = sha256_text(str(workspace_root.resolve()))[:24]
    path = leases_dir / f"{key}.json"

    workspace = str(workspace_root.resolve())
    token = secrets.token_hex(16)
    payload: dict[str, object] = {
        "workspace": workspace,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "token": token,
        "acquired_at": _now(),
        "heartbeat_at": _now(),
    }
    with _acquire_guard(path):
        existing = _read(path)
        if existing is not None and _is_live(existing, path):
            raise LeaseHeld(
                _pid_of(existing),
                str(existing.get("host", "?")),
                str(existing.get("acquired_at", "?")),
                path,
            )
        _write_atomic(path, payload)
        marker_fd = _acquire_hold_marker(_hold_marker(path))
        final = _read(path)
        if final is None or final.get("token") != token:
            raise LeaseHeld(
                _pid_of(final or {}),
                str((final or {}).get("host", "?")),
                str((final or {}).get("acquired_at", "?")),
                path,
            )
    _HELD_TOKENS.add(token)
    return WorkspaceLease(
        path=path,
        workspace=workspace,
        pid=os.getpid(),
        token=token,
        _marker_fd=marker_fd,
    )


@contextmanager
def _acquire_guard(path: Path) -> Iterator[None]:
    """串行化同一工作区的检查/接管，消除两个 stale breaker 都获胜的窗口。"""
    guard = path.with_suffix(".lock")
    with guard.open("a+", encoding="utf-8") as handle:
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
        try:
            yield
        finally:
            if _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)


def _read(path: Path) -> dict[str, object] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _pid_of(payload: dict[str, object]) -> int:
    raw = payload.get("pid", -1)
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else -1


def _hold_marker(path: Path) -> Path:
    """持有者生命周期内一直开着的标记文件路径。"""
    return path.with_suffix(".held")


def _marker_still_held(path: Path) -> bool:
    """标记文件的排他锁是否还被某个进程持有。

    这比 pid 探活强，也正是它能补上 pid 复用这个洞的原因：pid 会被复用，而复用那个
    pid 的无关进程并不会继承原持有者的文件描述符——原进程一死（含被 kill -9，没人调用
    `release`），内核就把锁放了，于是这里立刻拿得到。
    """
    if _fcntl is None:
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        # 没有标记文件：没有任何进程正在持有它。
        return False
    try:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            return True
        with contextlib.suppress(OSError):
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        return False
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def _unlink_missing_ok(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _acquire_hold_marker(path: Path) -> int | None:
    """拿下持有标记并返回其 fd；拿不到（平台不支持或已被占用）时返回 None。

    必须在 payload 落盘**之后**调用：先拿锁再写文件的话，另一个进程可能看到
    "锁被占但没有 payload"，从而判定持有者已死并接管。
    """
    if _fcntl is None:
        return None
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return None
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except OSError:
        with contextlib.suppress(OSError):
            os.close(fd)
        return None
    return fd


def _is_live(payload: dict[str, object], path: Path | None = None) -> bool:
    if str(payload.get("host", "")) != socket.gethostname():
        # 无法探测另一台机器上的持有者；信任心跳。
        return _heartbeat_age(payload) < STALE_AFTER_SECONDS
    pid = _pid_of(payload)
    if pid <= 0:
        return False
    if pid == os.getpid():
        token = str(payload.get("token", ""))
        return token in _HELD_TOKENS or _heartbeat_age(payload) < STALE_AFTER_SECONDS
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # pid 存在但属于另一位用户；回退到心跳判断。
        return _heartbeat_age(payload) < STALE_AFTER_SECONDS
    # 持有标记优先于 pid 探活：它才是"那个进程还活着"的证据，而 pid 只是"有个进程
    # 用着这个号"。标记不在手里（原持有者已死、pid 被复用、或从没写过标记）就不再
    # 看 pid——否则一个复用了 pid 的无关进程会让租约永久堵死。
    if path is not None and not _marker_still_held(_hold_marker(path)):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # pid 存在但属于另一位用户；回退到心跳判断。
        return _heartbeat_age(payload) < STALE_AFTER_SECONDS
    # 僵尸进程已经死了、只是还没被回收，而 `os.kill(pid, 0)` 对它照样成功。不识别
    # 的话，一个崩溃后没人 wait 的 Haven 进程会永远占着租约，且没有任何自动解法。
    # 同一主机上的 pid 对探测有响应，且不是僵尸：持有者仍然存活，结论确定。
    # 心跳绝不能覆盖存活探测——持有者不会按定时器刷新心跳，否则长时间
    # 交互会话会看起来“过期”，租约可能在运行中被夺走，而这正是租约要防止的事。
    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    """pid 对应一个僵尸进程时返回 True。

    只在有 `/proc` 的平台上可判断（Linux）；macOS 无从得知，此时返回 False，
    行为与之前一致。宁可漏判，也不要把活着的持有者判成死的。
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    # comm 字段可能含空格和括号，所以从最后一个 ')' 之后取状态位。
    _, _, rest = stat.rpartition(")")
    fields = rest.split()
    return bool(fields) and fields[0] == "Z"


def _heartbeat_age(payload: dict[str, object]) -> float:
    raw = str(payload.get("heartbeat_at", ""))
    try:
        beat = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return float("inf")
    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - beat).total_seconds())


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    tmp = path.with_suffix(f".tmp-{os.getpid()}-{time.monotonic_ns()}")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _now() -> str:
    return datetime.now(UTC).isoformat()
