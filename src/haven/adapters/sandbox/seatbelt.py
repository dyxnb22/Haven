"""macOS 后端：通过 /usr/bin/sandbox-exec 使用 Apple 的 Seatbelt。

SBPL 会评估每条匹配的规则，并以最后一条规则为准；因此可以用三条有序规则表达
“读取除用户主目录之外的所有内容，但允许读取其中的工作区”。
"""

from __future__ import annotations

from pathlib import Path

from haven.ports.sandbox import SandboxSpec

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

#: 无论文件系统策略如何都允许。这里不追求 IPC 隔离；拒绝这些内容会破坏
#: 普通解释器，却无法堵住此沙箱要解决的文件系统或网络漏洞。
_PREAMBLE = """\
(version 1)
(deny default)
(allow process-exec)
(allow process-fork)
(allow signal (target self))
(allow sysctl-read)
(allow mach-lookup)
(allow ipc-posix-shm)
(allow file-read-metadata)"""

_WRITABLE_DEVICES = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/dtracehelper")


def _literal(path: Path) -> str:
    """解析并为 SBPL 引用一个路径。

    解析过程很重要：/tmp 是指向 /private/tmp 的符号链接，而 Seatbelt 匹配的是
    解析后的路径。因此，如果临时目录未解析，生成的 profile 会连沙箱自己的临时目录
    也拒绝访问。
    """
    escaped = str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_profile(spec: SandboxSpec) -> str:
    """按 SandboxSpec 生成 macOS Seatbelt 规则文本，不启动进程。"""
    lines = [_PREAMBLE, "(allow file-read*)"]

    for root in spec.private_roots:
        lines.append(f"(deny file-read* (subpath {_literal(root)}))")
    # 放在拒绝规则之后，使嵌套在私有根目录中的工作区仍能使用。
    for root in (spec.workspace_root, spec.scratch_dir, *spec.extra_readable_roots):
        lines.append(f"(allow file-read* (subpath {_literal(root)}))")

    # 临时目录始终可写——它的存在就是为了让受限进程有地方写入。
    # `writable` 只控制工作区。
    lines.append(f"(allow file-write* (subpath {_literal(spec.scratch_dir)}))")
    if spec.writable:
        lines.append(f"(allow file-write* (subpath {_literal(spec.workspace_root)}))")
        for subpath in spec.protected_subpaths:
            lines.append(f"(deny file-write* (subpath {_literal(spec.workspace_root / subpath)}))")
    for device in _WRITABLE_DEVICES:
        lines.append(f"(allow file-write-data (literal {_literal(Path(device))}))")

    if not spec.allow_network:
        lines.append("(deny network*)")
    return "\n".join(lines) + "\n"


class SeatbeltLauncher:
    """在 macOS 上实现 SandboxLauncher。"""

    @property
    def backend(self) -> str:
        """返回 macOS Seatbelt 后端名称。"""
        return "seatbelt"

    def available(self) -> bool:
        """检查系统是否存在 sandbox-exec。"""
        return Path(SANDBOX_EXEC).is_file()

    def wrap(self, argv: tuple[str, ...], spec: SandboxSpec) -> tuple[str, ...]:
        """把规则文本和目标 argv 组合成 sandbox-exec 启动命令。"""
        return (SANDBOX_EXEC, "-p", build_profile(spec), *argv)

    def describe(self, spec: SandboxSpec) -> str:
        """描述 Seatbelt 下的读写和网络边界。"""
        writes = (
            f"writes limited to {spec.workspace_root}"
            if spec.writable
            else "workspace read-only (scratch writable)"
        )
        network = "network allowed" if spec.allow_network else "no network"
        return f"sandbox: seatbelt, {writes}, {network}, home directory unreadable"
