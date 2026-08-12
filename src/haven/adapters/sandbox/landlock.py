"""Linux backend: Landlock, applied by a helper that re-execs the target."""

from __future__ import annotations

import json
import sys

from haven.ports.sandbox import SandboxSpec
from haven.sandbox.landlock_launcher import MIN_ABI, abi_version

LAUNCHER_MODULE = "haven.sandbox.landlock_launcher"

#: Readable so ordinary programs can start. The launcher skips entries that do
#: not exist, so one list serves every distribution.
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
    """Build the JSON payload the launcher applies.

    `private_roots` needs no rule: Landlock grants are additive, so a path is
    confined by never appearing in the readable list.
    """
    readable = [
        *SYSTEM_ROOTS,
        str(spec.workspace_root.resolve()),
        str(spec.scratch_dir.resolve()),
        *(str(root.resolve()) for root in spec.extra_readable_roots),
    ]
    # Scratch is always writable — it exists so a confined process has
    # somewhere to write. `writable` governs the workspace alone.
    writable = [str(spec.scratch_dir.resolve())]
    if spec.writable:
        writable.insert(0, str(spec.workspace_root.resolve()))
    return json.dumps(
        {"readable": readable, "writable": writable, "allow_network": spec.allow_network},
        separators=(",", ":"),
    )


class LandlockLauncher:
    """Implements SandboxLauncher on Linux."""

    @property
    def backend(self) -> str:
        return "landlock"

    def available(self) -> bool:
        return sys.platform.startswith("linux") and abi_version() >= MIN_ABI

    def wrap(self, argv: tuple[str, ...], spec: SandboxSpec) -> tuple[str, ...]:
        return (sys.executable, "-m", LAUNCHER_MODULE, "--spec", encode_spec(spec), "--", *argv)

    def describe(self, spec: SandboxSpec) -> str:
        writes = (
            f"writes limited to {spec.workspace_root}"
            if spec.writable
            else "workspace read-only (scratch writable)"
        )
        network = "network allowed" if spec.allow_network else "no TCP"
        # Subtree grants cannot express "the workspace except .git", so name the
        # layer that is really holding that line.
        return (
            f"sandbox: landlock, {writes}, {network}, home directory unreadable "
            "(.git is protected by Haven's tool layer, not by the kernel)"
        )
