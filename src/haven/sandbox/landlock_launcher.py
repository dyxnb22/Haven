"""为当前进程应用 Landlock 规则集，然后执行目标命令。

Landlock 是可叠加的 LSM：进程可以不可逆地限制自身及其子进程。权限具有累加性——
未授予的任何权限都会被拒绝——因此读取限制通过枚举仍可读取的内容来表达，而不
是列出不可读取的内容。

无法应用沙箱时退出码为 125，绝不会回退到不受约束地运行命令。
"""

from __future__ import annotations

import ctypes
import json
import os
import sys

SETUP_FAILURE_EXIT = 125

# 通用系统调用编号；在 x86_64 和 aarch64 上相同。
_SYS_CREATE_RULESET = 444
_SYS_ADD_RULE = 445
_SYS_RESTRICT_SELF = 446

_CREATE_RULESET_VERSION = 1 << 0
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

_FS_EXECUTE = 1 << 0
_FS_WRITE_FILE = 1 << 1
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
_FS_REMOVE_DIR = 1 << 4
_FS_REMOVE_FILE = 1 << 5
_FS_MAKE_CHAR = 1 << 6
_FS_MAKE_DIR = 1 << 7
_FS_MAKE_REG = 1 << 8
_FS_MAKE_SOCK = 1 << 9
_FS_MAKE_FIFO = 1 << 10
_FS_MAKE_BLOCK = 1 << 11
_FS_MAKE_SYM = 1 << 12
_FS_REFER = 1 << 13  # Landlock ABI 2 权限
_FS_TRUNCATE = 1 << 14  # Landlock ABI 3 权限

_NET_BIND_TCP = 1 << 0  # Landlock ABI 4 权限
_NET_CONNECT_TCP = 1 << 1  # Landlock ABI 4 权限

_READ_RIGHTS = _FS_READ_FILE | _FS_READ_DIR | _FS_EXECUTE
_WRITE_RIGHTS = (
    _READ_RIGHTS
    | _FS_WRITE_FILE
    | _FS_REMOVE_DIR
    | _FS_REMOVE_FILE
    | _FS_MAKE_DIR
    | _FS_MAKE_REG
    | _FS_MAKE_SOCK
    | _FS_MAKE_FIFO
    | _FS_MAKE_SYM
    | _FS_TRUNCATE
)

#: 网络权限在 ABI 4 中加入；更低版本无法强制执行拒绝。
MIN_ABI = 4


class _RulesetAttr(ctypes.Structure):
    _fields_ = (("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64))


class _PathBeneathAttr(ctypes.Structure):
    # Packed：内核要求 rights 与 fd 之间没有填充。
    _pack_ = 1
    _fields_ = (("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32))


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL("libc.so.6", use_errno=True)


def abi_version() -> int:
    """当前内核支持的 Landlock ABI；不支持时返回 0。"""
    try:
        libc = _libc()
        result = int(
            libc.syscall(_SYS_CREATE_RULESET, None, ctypes.c_size_t(0), _CREATE_RULESET_VERSION)
        )
    except (OSError, AttributeError):
        return 0
    return result if result > 0 else 0


def _handled_fs_rights(abi: int) -> int:
    rights = (
        _FS_EXECUTE
        | _FS_WRITE_FILE
        | _FS_READ_FILE
        | _FS_READ_DIR
        | _FS_REMOVE_DIR
        | _FS_REMOVE_FILE
        | _FS_MAKE_CHAR
        | _FS_MAKE_DIR
        | _FS_MAKE_REG
        | _FS_MAKE_SOCK
        | _FS_MAKE_FIFO
        | _FS_MAKE_BLOCK
        | _FS_MAKE_SYM
    )
    if abi >= 2:
        rights |= _FS_REFER
    if abi >= 3:
        rights |= _FS_TRUNCATE
    return rights


def _add_path_rule(libc: ctypes.CDLL, ruleset_fd: int, path: str, rights: int, abi: int) -> None:
    """授予 `path` 下方的 `rights` 权限。

    不存在的路径会跳过，而不会导致致命错误：系统根目录列表是通用的，并非每个
    发行版都存在所有路径。
    """
    open_flags = getattr(os, "O_PATH", 0) | os.O_CLOEXEC
    try:
        parent_fd = os.open(path, open_flags)
    except OSError:
        return
    try:
        attr = _PathBeneathAttr(
            allowed_access=rights & _handled_fs_rights(abi), parent_fd=parent_fd
        )
        if libc.syscall(
            _SYS_ADD_RULE,
            ctypes.c_int(ruleset_fd),
            ctypes.c_int(_RULE_PATH_BENEATH),
            ctypes.byref(attr),
            ctypes.c_uint32(0),
        ):
            raise OSError(ctypes.get_errno(), f"landlock_add_rule failed for {path}")
    finally:
        os.close(parent_fd)


def apply_sandbox(payload: dict[str, object]) -> None:
    """在当前进程中安装不可逆的 Landlock 文件和网络访问规则。"""
    abi = abi_version()
    if abi < MIN_ABI:
        raise OSError(f"landlock ABI {abi} is too old; {MIN_ABI} or newer is required")
    libc = _libc()

    handled_net = 0 if payload.get("allow_network") else _NET_BIND_TCP | _NET_CONNECT_TCP
    attr = _RulesetAttr(handled_access_fs=_handled_fs_rights(abi), handled_access_net=handled_net)
    ruleset_fd = int(
        libc.syscall(
            _SYS_CREATE_RULESET,
            ctypes.byref(attr),
            ctypes.c_size_t(ctypes.sizeof(attr)),
            ctypes.c_uint32(0),
        )
    )
    if ruleset_fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset failed")

    try:
        readable = payload.get("readable") or []
        writable = payload.get("writable") or []
        if not isinstance(readable, list) or not isinstance(writable, list):
            raise ValueError("readable and writable must be lists of paths")
        for path in readable:
            _add_path_rule(libc, ruleset_fd, str(path), _READ_RIGHTS, abi)
        for path in writable:
            _add_path_rule(libc, ruleset_fd, str(path), _WRITE_RIGHTS, abi)

        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0):
            raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
        if libc.syscall(_SYS_RESTRICT_SELF, ctypes.c_int(ruleset_fd), ctypes.c_uint32(0)):
            raise OSError(ctypes.get_errno(), "landlock_restrict_self failed")
    finally:
        os.close(ruleset_fd)


def main(argv: list[str]) -> int:
    """解析启动器参数、应用沙箱并用目标命令替换当前进程。"""
    if "--spec" not in argv or "--" not in argv:
        print("usage: landlock_launcher --spec JSON -- PROGRAM [ARGS...]", file=sys.stderr)
        return SETUP_FAILURE_EXIT
    spec_json = argv[argv.index("--spec") + 1]
    target = argv[argv.index("--") + 1 :]
    if not target:
        print("no command given", file=sys.stderr)
        return SETUP_FAILURE_EXIT
    try:
        apply_sandbox(json.loads(spec_json))
    except (OSError, ValueError) as exc:
        print(f"sandbox setup failed: {exc}", file=sys.stderr)
        return SETUP_FAILURE_EXIT
    try:
        os.execvp(target[0], target)
    except OSError as exc:
        print(f"exec failed: {exc}", file=sys.stderr)
        return 127
    return 0  # 不可达：execvp 会替换当前进程


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
