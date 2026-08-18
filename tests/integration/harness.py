"""共享的离线测试工具：临时仓库上的 ScriptedModel + 真实适配器。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from haven.adapters.memory_session import MemorySessionStore
from haven.adapters.process_executor import ProcessExecutor
from haven.adapters.providers.scripted import ScriptedModel
from haven.adapters.workspace_fs import FsWorkspace
from haven.application.approvals import ApprovalResponder, AutoApprover
from haven.application.emitter import EventEmitter
from haven.application.run_service import RunService
from haven.contracts.events import ApplicationEvent, EventEnvelope
from haven.contracts.model import (
    ModelEvent,
    StreamFinished,
    TextDelta,
    ToolCallProposal,
    ToolCallReady,
    Usage,
    UsageReport,
)
from haven.contracts.tools import RecipeSpec
from haven.domain.budget import Budget
from haven.domain.enums import PermissionMode
from haven.domain.pricing import Pricing
from haven.ports.sandbox import SandboxLauncher
from tests.integration.fakes import RecordingLauncher

#: 区分“调用方没有提供内容”和显式的 `launcher=None`；测试通过后者请求
#: 没有沙箱后端的路径。
_UNSET_LAUNCHER: SandboxLauncher = RecordingLauncher()

BUGGY_CALC = (
    "def add(a, b):\n    return a - b  # BUG: should be +\n\n\ndef sub(a, b):\n    return a - b\n"
)

VERIFY_CALC = (
    "import sys\n"
    "sys.path.insert(0, 'src')\n"
    "from calc import add\n"
    "sys.exit(0 if add(2, 3) == 5 else 1)\n"
)


def text(content: str) -> TextDelta:
    return TextDelta(text=content)


def tool(call_id: str, name: str, **args: object) -> ToolCallReady:
    return ToolCallReady(
        call=ToolCallProposal(call_id=call_id, tool_name=name, arguments_json=json.dumps(args))
    )


def finish(reason: str = "stop") -> StreamFinished:
    mapped = reason if reason in ("stop", "tool_calls", "length", "error") else "stop"
    return StreamFinished(finish_reason=mapped)  # type: ignore[arg-type]


def usage(input_tokens: int = 100, output_tokens: int = 20) -> UsageReport:
    return UsageReport(
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens, estimated=False)
    )


class CollectingSink:
    def __init__(self) -> None:
        self.envelopes: list[EventEnvelope] = []

    async def emit(self, envelope: EventEnvelope) -> None:
        self.envelopes.append(envelope)

    def kinds(self) -> list[str]:
        return [env.event.kind for env in self.envelopes]

    def events_of(self, kind: str) -> list[ApplicationEvent]:
        return [env.event for env in self.envelopes if env.event.kind == kind]


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "calc.py").write_text(BUGGY_CALC)
    (repo / "verify_calc.py").write_text(VERIFY_CALC)
    (repo / "README.md").write_text("# Calc demo\n")
    return repo


def default_recipes() -> dict[str, RecipeSpec]:
    return {
        "verify-calc": RecipeSpec(
            id="verify-calc", argv=(sys.executable, "verify_calc.py"), timeout_seconds=30
        ),
        "always-fail": RecipeSpec(
            id="always-fail",
            argv=(sys.executable, "-c", "import sys; sys.exit(1)"),
            timeout_seconds=30,
        ),
        "always-pass": RecipeSpec(
            id="always-pass",
            argv=(sys.executable, "-c", "print('ok')"),
            timeout_seconds=30,
        ),
    }


class Harness:
    def __init__(
        self,
        repo: Path,
        turns: list[list[ModelEvent]],
        *,
        mode: PermissionMode = PermissionMode.INTERACTIVE,
        approver: ApprovalResponder | None = None,
        budget: Budget | None = None,
        repeat_last: bool = False,
        launcher: SandboxLauncher | None = _UNSET_LAUNCHER,
        recipes: dict[str, RecipeSpec] | None = None,
        model_name: str = "scripted",
        pricing: Pricing | None = None,
        supports_prefix_continuation: bool | None = None,
    ) -> None:
        self.workspace = FsWorkspace(repo)
        self.store = MemorySessionStore()
        self.sink = CollectingSink()
        self.emitter = EventEmitter(self.store, [self.sink])
        self.model = ScriptedModel(turns, repeat_last=repeat_last, name=model_name)
        self.approver = approver if approver is not None else AutoApprover("approve_all")
        # 默认使用记录型 launcher，使 exec 在这里的行为在所有平台上一致；
        # 真实限制由 tests/security 中的测试断言。
        # 传入 launcher=None 是为了专门测试无后端路径。
        resolved = RecordingLauncher() if launcher is _UNSET_LAUNCHER else launcher
        self.launcher = resolved
        self.service = RunService(
            model=self.model,
            workspace=self.workspace,
            executor=ProcessExecutor(launcher=resolved),
            store=self.store,
            emitter=self.emitter,
            approvals=self.approver,
            recipes=recipes if recipes is not None else default_recipes(),
            mode=mode,
            budget=budget if budget is not None else Budget(),
            launcher=resolved,
            pricing=pricing,
            supports_prefix_continuation=supports_prefix_continuation,
        )
