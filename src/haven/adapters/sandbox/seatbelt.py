"""macOS backend: Apple's Seatbelt via /usr/bin/sandbox-exec.

SBPL evaluates every matching rule and the last one wins, which is what makes
"read everything except the user's home, but do read the workspace inside it"
expressible as three ordered rules.
"""

from __future__ import annotations

from pathlib import Path

from haven.ports.sandbox import SandboxSpec

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

#: Allowed regardless of the filesystem policy. IPC isolation is not a goal
#: here; denying these breaks ordinary interpreters without closing the
#: filesystem or network holes this sandbox exists to close.
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
    """Resolve and quote one path for SBPL.

    Resolution matters: /tmp is a symlink to /private/tmp and Seatbelt matches
    the resolved path, so an unresolved scratch dir yields a profile that denies
    the sandbox its own scratch directory.
    """
    escaped = str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_profile(spec: SandboxSpec) -> str:
    lines = [_PREAMBLE, "(allow file-read*)"]

    for root in spec.private_roots:
        lines.append(f"(deny file-read* (subpath {_literal(root)}))")
    # After the denials, so a workspace nested inside a private root survives.
    for root in (spec.workspace_root, spec.scratch_dir, *spec.extra_readable_roots):
        lines.append(f"(allow file-read* (subpath {_literal(root)}))")

    if spec.writable:
        for root in (spec.workspace_root, spec.scratch_dir):
            lines.append(f"(allow file-write* (subpath {_literal(root)}))")
        for subpath in spec.protected_subpaths:
            lines.append(f"(deny file-write* (subpath {_literal(spec.workspace_root / subpath)}))")
    for device in _WRITABLE_DEVICES:
        lines.append(f"(allow file-write-data (literal {_literal(Path(device))}))")

    if not spec.allow_network:
        lines.append("(deny network*)")
    return "\n".join(lines) + "\n"


class SeatbeltLauncher:
    """Implements SandboxLauncher on macOS."""

    @property
    def backend(self) -> str:
        return "seatbelt"

    def available(self) -> bool:
        return Path(SANDBOX_EXEC).is_file()

    def wrap(self, argv: tuple[str, ...], spec: SandboxSpec) -> tuple[str, ...]:
        return (SANDBOX_EXEC, "-p", build_profile(spec), *argv)

    def describe(self, spec: SandboxSpec) -> str:
        writes = f"writes limited to {spec.workspace_root}" if spec.writable else "read-only"
        network = "network allowed" if spec.allow_network else "no network"
        return f"sandbox: seatbelt, {writes}, {network}, home directory unreadable"
