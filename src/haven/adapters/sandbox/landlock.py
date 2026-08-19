"""Linux 后端：Landlock，由重新执行目标命令的辅助程序应用。"""

from __future__ import annotations

import json
import sys

from haven.ports.sandbox import SandboxSpec
from haven.sandbox.landlock_launcher import MIN_ABI, abi_version

LAUNCHER_MODULE = "haven.sandbox.landlock_launcher"

#: 允许读取，以便普通程序能够启动。启动器会跳过不存在的条目，因此一份
#: 列表可以适用于所有发行版。
SYSTEM_ROOTS = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/etc",
    "/opt",
    "/proc",
    "/dev",
    "/var",
    "/run",
)


def encode_spec(spec: SandboxSpec) -> str:
    """构造启动器要应用的 JSON 载荷。

    `private_roots` 不需要规则：Landlock 的授权是累加的，因此只要路径不出现在
    可读列表中，就会受到限制。
    """
    readable = [
        *SYSTEM_ROOTS,
        str(spec.workspace_root.resolve()),
        str(spec.scratch_dir.resolve()),
        *(str(root.resolve()) for root in spec.extra_readable_roots),
    ]
    # 临时目录始终可写——它的存在就是为了让受限进程有地方写入。
    # `writable` 只控制工作区。
    writable = [str(spec.scratch_dir.resolve())]
    if spec.writable:
        writable.insert(0, str(spec.workspace_root.resolve()))
    return json.dumps(
        {"readable": readable, "writable": writable, "allow_network": spec.allow_network},
        separators=(",", ":"),
    )


class LandlockLauncher:
    """在 Linux 上实现 SandboxLauncher。"""

    @property
    def backend(self) -> str:
        """返回 Linux Landlock 后端名称。"""
        return "landlock"

    def available(self) -> bool:
        """检查当前内核是否提供满足最低要求的 Landlock ABI。"""
        return sys.platform.startswith("linux") and abi_version() >= MIN_ABI

    def wrap(self, argv: tuple[str, ...], spec: SandboxSpec) -> tuple[str, ...]:
        """通过辅助模块重新执行目标命令并传入序列化沙箱规格。"""
        return (sys.executable, "-m", LAUNCHER_MODULE, "--spec", encode_spec(spec), "--", *argv)

    def describe(self, spec: SandboxSpec) -> str:
        """描述 Landlock 下的读写、网络和保护路径边界。"""
        writes = (
            f"writes limited to {spec.workspace_root}"
            if spec.writable
            else "workspace read-only (scratch writable)"
        )
        network = "network allowed" if spec.allow_network else "no TCP"
        # 子树授权无法表达“工作区中排除 .git”，因此要明确指出真正负责守住
        # 这条边界的层。
        return (
            f"sandbox: landlock, {writes}, {network}, home directory unreadable "
            "(.git is protected by Haven's tool layer, not by the kernel)"
        )
