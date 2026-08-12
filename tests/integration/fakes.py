"""Test doubles shared by the executor, pipeline, and eval tests."""

from __future__ import annotations

from haven.ports.sandbox import SandboxSpec


class RecordingLauncher:
    """Records what was asked to be confined, without confining it.

    Lets every layer above the OS be tested identically on any platform; real
    confinement is asserted separately in tests/security.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], SandboxSpec]] = []

    @property
    def backend(self) -> str:
        return "recording"

    def available(self) -> bool:
        return True

    def wrap(self, argv: tuple[str, ...], spec: SandboxSpec) -> tuple[str, ...]:
        self.calls.append((argv, spec))
        return argv

    def describe(self, spec: SandboxSpec) -> str:
        return "sandbox: recording, no network"
