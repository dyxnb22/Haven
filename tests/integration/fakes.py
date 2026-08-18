"""执行器、流水线和评估测试共享的测试替身。"""

from __future__ import annotations

from haven.ports.sandbox import SandboxSpec


class RecordingLauncher:
    """记录要求限制的内容，但不实际进行限制。

    使操作系统之上的每一层都能在任何平台上以相同方式测试；真实限制会在
    tests/security 中单独断言。
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
