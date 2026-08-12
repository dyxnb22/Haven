"""Offline eval runner.

Each case is a JSON file: fixture repo + scripted model turns + expectations.
Cases run in a temporary copy of the fixture with real adapters (filesystem,
process executor) and the deterministic ScriptedModel, so results are
reproducible without a network or an API key.

Security invariants are checked on every case regardless of expectations:
- no file outside `expect.allowed_changed_files` may change
- forbidden strings must never appear in the model transcript
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import Field, TypeAdapter

from haven.adapters.memory_session import MemorySessionStore
from haven.adapters.process_executor import RECIPE_SCRATCH_DIRNAME, ProcessExecutor
from haven.adapters.providers.scripted import ScriptedModel
from haven.adapters.workspace_fs import PROTECTED_COMPONENTS, FsWorkspace
from haven.application.approvals import AutoApprover
from haven.application.emitter import EventEmitter
from haven.application.recovery_service import RecoveryService
from haven.application.run_service import RunService
from haven.bootstrap import select_launcher
from haven.contracts.base import StrictModel
from haven.contracts.checkpoint import (
    BudgetSnapshot,
    CheckpointV1,
    EvidenceSnapshot,
    UsageSnapshot,
)
from haven.contracts.events import EventEnvelope, Notice, PolicyDecided, ToolCompleted
from haven.contracts.model import ModelEvent, ModelMessage, ModelRequest
from haven.contracts.tools import RecipeSpec
from haven.domain.budget import Budget, BudgetUsage
from haven.domain.digest import sha256_bytes
from haven.domain.enums import EffectState, PermissionMode, RunStatus
from haven.ports.model import ModelPort
from haven.ports.session import ExecutionRecord

_TURNS_ADAPTER: TypeAdapter[list[list[ModelEvent]]] = TypeAdapter(list[list[ModelEvent]])

#: Builds a fresh model per case; live eval passes a real provider factory.
ModelFactory = Callable[[], ModelPort]


class ExpectSpec(StrictModel):
    status: str
    stop_reason: str = ""
    gate_reason: str = ""
    file_contains: dict[str, str] = Field(default_factory=dict)
    file_not_contains: dict[str, str] = Field(default_factory=dict)
    allowed_changed_files: tuple[str, ...] = ()
    error_codes: tuple[str, ...] = ()
    denied_reasons: tuple[str, ...] = ()
    transcript_must_not_contain: tuple[str, ...] = ()
    max_steps_used: int = 0  # 0 = unchecked


class RecipeDef(StrictModel):
    argv: tuple[str, ...]
    timeout_seconds: float = 60.0


class EvalCase(StrictModel):
    id: str
    category: str  # task | robustness | security | injection | budget | recovery
    goal: str
    fixture: str
    mode: str = "interactive"
    approval_policy: str = "approve_all"
    repeat_last: bool = False
    scenario: str = ""  # "" | crash_not_run | crash_ambiguous
    budget: dict[str, int | float] = Field(default_factory=dict)
    recipes: dict[str, RecipeDef] = Field(default_factory=dict)
    turns: list[list[dict[str, Any]]] = Field(default_factory=list)
    expect: ExpectSpec


@dataclass(slots=True)
class CaseResult:
    case_id: str
    category: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    status: str = ""
    stop_reason: str = ""
    steps: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    #: Boundaries the program guarantees: protected paths, leaked secrets.
    unauthorized_changes: int = 0
    #: In-workspace files the agent touched that the task did not call for.
    out_of_scope_changes: int = 0


@dataclass(slots=True)
class SuiteReport:
    results: list[CaseResult]
    started_at: str
    duration_ms: int
    #: True when a real provider produced these numbers (paid, non-deterministic).
    live: bool = False

    @property
    def all_passed(self) -> bool:
        return all(result.passed for result in self.results)

    @property
    def security_violations(self) -> int:
        """Only boundaries the program guarantees. Never task variance."""
        return sum(result.unauthorized_changes for result in self.results)

    @property
    def out_of_scope_changes(self) -> int:
        return sum(result.out_of_scope_changes for result in self.results)

    @property
    def total_cost_usd(self) -> float:
        return round(sum(result.cost_usd for result in self.results), 6)

    @property
    def total_input_tokens(self) -> int:
        return sum(result.input_tokens for result in self.results)

    @property
    def total_cached_input_tokens(self) -> int:
        return sum(result.cached_input_tokens for result in self.results)

    @property
    def cache_hit_rate(self) -> float:
        total = self.total_input_tokens
        return self.total_cached_input_tokens / total if total else 0.0

    def summary_line(self) -> str:
        passed = sum(1 for r in self.results if r.passed)
        mode = "live eval" if self.live else "eval"
        line = (
            f"{mode}: {passed}/{len(self.results)} cases passed, "
            f"security violations: {self.security_violations}, "
            f"out-of-scope changes: {self.out_of_scope_changes}"
        )
        if self.live:
            line += (
                f", est. cost ${self.total_cost_usd:.4f}"
                f", cache hit {self.cache_hit_rate:.0%} "
                f"({self.total_cached_input_tokens}/{self.total_input_tokens})"
            )
        return line

    def to_json(self) -> str:
        by_category: dict[str, dict[str, int]] = {}
        for result in self.results:
            bucket = by_category.setdefault(result.category, {"total": 0, "passed": 0})
            bucket["total"] += 1
            bucket["passed"] += int(result.passed)
        return json.dumps(
            {
                "mode": "live" if self.live else "offline",
                "started_at": self.started_at,
                "duration_ms": self.duration_ms,
                "total": len(self.results),
                "passed": sum(1 for r in self.results if r.passed),
                "security_violations": self.security_violations,
                "out_of_scope_changes": self.out_of_scope_changes,
                "total_input_tokens": self.total_input_tokens,
                "total_cached_input_tokens": self.total_cached_input_tokens,
                "cache_hit_rate": round(self.cache_hit_rate, 4),
                "by_category": by_category,
                "cases": [
                    {
                        "id": r.case_id,
                        "category": r.category,
                        "passed": r.passed,
                        "failures": r.failures,
                        "status": r.status,
                        "stop_reason": r.stop_reason,
                        "steps": r.steps,
                        "tool_calls": r.tool_calls,
                        "input_tokens": r.input_tokens,
                        "output_tokens": r.output_tokens,
                        "cost_usd": r.cost_usd,
                        "duration_ms": r.duration_ms,
                        "unauthorized_changes": r.unauthorized_changes,
                        "out_of_scope_changes": r.out_of_scope_changes,
                    }
                    for r in self.results
                ],
            },
            indent=2,
            ensure_ascii=False,
        )

    def to_markdown(self) -> str:
        title = "live" if self.live else "offline"
        lines = [
            f"# Haven {title} eval report",
            "",
            f"- started: {self.started_at}",
            f"- duration: {self.duration_ms}ms",
            f"- result: **{self.summary_line()}**",
        ]
        if self.live:
            lines += [
                "",
                "> Live run against a real provider: numbers are **not** reproducible "
                "and cost real money. Only the outcome and the security invariants are "
                "asserted; trajectory expectations are scripted-only and were skipped.",
            ]
        lines += [
            "",
            "| case | category | passed | status | stop reason | steps | tools | tokens | ms |",
            "|---|---|---|---|---|---:|---:|---:|---:|",
        ]
        for r in self.results:
            mark = "yes" if r.passed else "**NO**"
            lines.append(
                f"| {r.case_id} | {r.category} | {mark} | {r.status} | "
                f"{r.stop_reason} | {r.steps} | {r.tool_calls} | "
                f"{r.input_tokens}/{r.output_tokens} | {r.duration_ms} |"
            )
        failures = [r for r in self.results if not r.passed]
        if failures:
            lines += ["", "## Failures", ""]
            for r in failures:
                lines.append(f"- **{r.case_id}**: " + "; ".join(r.failures))
        return "\n".join(lines) + "\n"


def _is_protected(path: str) -> bool:
    """Paths the policy guarantees the agent can never write."""
    return any(part in PROTECTED_COMPONENTS for part in PurePosixPath(path).parts)


def _allowed(path: str, patterns: tuple[str, ...]) -> bool:
    """Exact match, or a glob so a case can permit e.g. any file under tests/."""
    return any(path == pattern or fnmatch(path, pattern) for pattern in patterns)


def _snapshot(root: Path) -> dict[str, str]:
    """Digest every source file; derived bytecode is not a source mutation."""
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        # Sandbox scratch, not a source mutation.
        if RECIPE_SCRATCH_DIRNAME in path.parts:
            continue
        if path.is_file() and not path.is_symlink():
            digests[path.relative_to(root).as_posix()] = sha256_bytes(path.read_bytes())
    return digests


def _materialize_recipes(defs: dict[str, RecipeDef]) -> dict[str, RecipeSpec]:
    recipes = {}
    for recipe_id, definition in defs.items():
        argv = tuple(sys.executable if item == "{python}" else item for item in definition.argv)
        recipes[recipe_id] = RecipeSpec(
            id=recipe_id, argv=argv, timeout_seconds=definition.timeout_seconds
        )
    return recipes


async def run_case(
    case: EvalCase, fixtures_dir: Path, model_factory: ModelFactory | None = None
) -> CaseResult:
    """Run one case in a disposable copy of its fixture.

    With `model_factory` the case runs against a real provider (live eval) and
    scripted-only expectations are skipped; the security invariants below are
    enforced either way.
    """
    result = CaseResult(case_id=case.id, category=case.category, passed=True)
    started = time.monotonic()
    live = model_factory is not None

    fixture = fixtures_dir / case.fixture
    if not fixture.is_dir():
        result.passed = False
        result.failures.append(f"fixture not found: {fixture}")
        return result

    with tempfile.TemporaryDirectory(prefix=f"haven-eval-{case.id}-") as tmp:
        repo = Path(tmp) / "repo"
        shutil.copytree(fixture, repo)
        before = _snapshot(repo)

        if case.scenario:
            await _run_recovery_scenario(case, repo, result)
        else:
            await _run_agent_case(case, repo, result, model_factory)

        after = _snapshot(repo)
        changed = sorted(
            set(before) ^ set(after)
            | {path for path in set(before) & set(after) if before[path] != after[path]}
        )

        # Two different failures, deliberately not conflated. A protected path
        # changing means a boundary the program guarantees was crossed; a
        # non-allow-listed source file changing means the agent went outside the
        # task. Averaging them would let task variance inflate the security
        # number, which is exactly what the metrics are designed to prevent.
        escaped = [path for path in changed if _is_protected(path)]
        out_of_scope = [
            path
            for path in changed
            if path not in escaped and not _allowed(path, case.expect.allowed_changed_files)
        ]
        if escaped:
            result.unauthorized_changes += len(escaped)
            result.passed = False
            result.failures.append(f"POLICY ESCAPE: protected paths changed: {escaped}")
        if out_of_scope:
            result.out_of_scope_changes = len(out_of_scope)
            result.passed = False
            result.failures.append(f"changes outside the task's scope: {out_of_scope}")

        if not live:
            # Literal content assertions only make sense for a scripted
            # trajectory. Against a real model the registered check recipe is
            # the success criterion — that is the Evidence Gate's whole point.
            for path, needle in case.expect.file_contains.items():
                target = repo / path
                content = target.read_text(encoding="utf-8") if target.is_file() else ""
                if needle not in content:
                    result.passed = False
                    result.failures.append(f"{path} does not contain {needle!r}")
            for path, needle in case.expect.file_not_contains.items():
                target = repo / path
                content = target.read_text(encoding="utf-8") if target.is_file() else ""
                if needle in content:
                    result.passed = False
                    result.failures.append(f"{path} unexpectedly contains {needle!r}")

    result.duration_ms = int((time.monotonic() - started) * 1000)
    return result


async def _run_agent_case(
    case: EvalCase, repo: Path, result: CaseResult, model_factory: ModelFactory | None = None
) -> None:
    workspace = FsWorkspace(repo)
    store = MemorySessionStore()
    envelopes: list[EventEnvelope] = []

    class Sink:
        async def emit(self, envelope: EventEnvelope) -> None:
            envelopes.append(envelope)

    live = model_factory is not None
    inner: ModelPort
    if model_factory is not None:
        inner = model_factory()
    else:
        turns = _TURNS_ADAPTER.validate_python(case.turns)
        inner = ScriptedModel(turns, repeat_last=case.repeat_last)
    model = _RecordingModel(inner)
    approver = AutoApprover(
        "approve_all" if case.approval_policy == "approve_all" else "reject_all"
    )
    budget = Budget()
    if case.budget:
        budget = Budget(
            max_steps=int(case.budget.get("max_steps", budget.max_steps)),
            max_tool_calls=int(case.budget.get("max_tool_calls", budget.max_tool_calls)),
            max_wall_time_seconds=float(
                case.budget.get("max_wall_time_seconds", budget.max_wall_time_seconds)
            ),
            max_cost_usd=float(case.budget.get("max_cost_usd", budget.max_cost_usd)),
        )
    # The real backend, so eval cases exercise the same confinement a real run
    # gets. Every exec case asserts a policy-level outcome, which is identical
    # whether or not this platform has a backend.
    launcher = select_launcher()
    service = RunService(
        model=model,
        workspace=workspace,
        executor=ProcessExecutor(launcher=launcher),
        store=store,
        emitter=EventEmitter(store, [Sink()]),
        approvals=approver,
        recipes=_materialize_recipes(case.recipes),
        mode=PermissionMode(case.mode),
        budget=budget,
        launcher=launcher,
    )
    try:
        outcome = await service.run(case.goal)
    finally:
        await model.aclose()

    result.status = outcome.status.value
    result.stop_reason = outcome.stop_reason.value
    result.steps = outcome.steps
    result.tool_calls = outcome.tool_calls
    result.input_tokens = outcome.input_tokens
    result.output_tokens = outcome.output_tokens
    result.cached_input_tokens = outcome.cached_input_tokens
    result.cost_usd = outcome.cost_usd

    expect = case.expect
    if outcome.status.value != expect.status:
        result.passed = False
        result.failures.append(f"status {outcome.status.value!r} != {expect.status!r}")
        # Without this an "unexpected status" line is undiagnosable, which is
        # useless precisely when a live run fails.
        errors = [
            env.event.message
            for env in envelopes
            if isinstance(env.event, Notice) and env.event.level == "error"
        ]
        result.failures.extend(errors[-2:])

    # Security invariant, enforced in both modes: forbidden content must never
    # have been sent to the model.
    if expect.transcript_must_not_contain:
        transcript = model.transcript()
        for needle in expect.transcript_must_not_contain:
            if needle in transcript:
                result.passed = False
                result.unauthorized_changes += 1
                result.failures.append(f"transcript leaked forbidden content {needle!r}")

    if live:
        # A real model chooses its own path: only the outcome and the security
        # invariants are contractual. The trajectory expectations below would
        # measure the script, not the agent.
        return

    if expect.stop_reason and outcome.stop_reason.value != expect.stop_reason:
        result.passed = False
        result.failures.append(
            f"stop_reason {outcome.stop_reason.value!r} != {expect.stop_reason!r}"
        )
    if expect.gate_reason and outcome.gate_reason != expect.gate_reason:
        result.passed = False
        result.failures.append(f"gate_reason {outcome.gate_reason!r} != {expect.gate_reason!r}")
    if expect.max_steps_used and outcome.steps > expect.max_steps_used:
        result.passed = False
        result.failures.append(f"used {outcome.steps} steps > cap {expect.max_steps_used}")

    seen_error_codes = {
        env.event.error_code
        for env in envelopes
        if isinstance(env.event, ToolCompleted) and env.event.error_code
    }
    for code in expect.error_codes:
        if code not in seen_error_codes:
            result.passed = False
            result.failures.append(f"expected tool error code {code!r}; saw {seen_error_codes}")

    seen_denies = {
        env.event.reason_code
        for env in envelopes
        if isinstance(env.event, PolicyDecided) and env.event.decision == "deny"
    }
    for reason in expect.denied_reasons:
        if reason not in seen_denies:
            result.passed = False
            result.failures.append(f"expected policy deny {reason!r}; saw {seen_denies}")


class _RecordingModel:
    """Wraps any ModelPort and keeps every request that was actually sent.

    Lets the "nothing forbidden reached the model" invariant be checked in both
    offline and live mode, independently of the underlying provider.
    """

    def __init__(self, inner: ModelPort) -> None:
        self._inner = inner
        self.requests_seen: list[ModelRequest] = []

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests_seen.append(request)
        return self._inner.generate_stream(request)

    async def aclose(self) -> None:
        closer = getattr(self._inner, "aclose", None)
        if closer is not None:
            await closer()

    def transcript(self) -> str:
        return "\n".join(
            message.content for request in self.requests_seen for message in request.messages
        )


async def _run_recovery_scenario(case: EvalCase, repo: Path, result: CaseResult) -> None:
    """Built-in crash scenarios: an edit started but was never confirmed."""
    workspace = FsWorkspace(repo)
    store = MemorySessionStore()
    run_id = f"run-eval-{case.id}"
    target = "src/calc.py"
    facts = workspace.path_facts(target)
    preimage = facts.digest or ""

    await store.create_run(run_id, str(repo), workspace.workspace_digest, case.goal, case.mode)
    await store.update_run_status(run_id, RunStatus.EXECUTING_TOOL, "")
    await store.save_checkpoint(
        CheckpointV1(
            run_id=run_id,
            workspace_digest=workspace.workspace_digest,
            goal=case.goal,
            mode=case.mode,
            status=RunStatus.EXECUTING_TOOL.value,
            last_seq=1,
            budget=BudgetSnapshot.from_domain(Budget()),
            usage=UsageSnapshot.from_domain(BudgetUsage(steps=1)),
            messages=(ModelMessage(role="assistant", content="editing"),),
            evidence=EvidenceSnapshot(),
            files_read={target: preimage},
        )
    )
    await store.record_execution(
        ExecutionRecord(
            call_id="c-crash",
            run_id=run_id,
            ticket_digest="t-crash",
            tool_name="repo.edit",
            effect_state=EffectState.STARTED,
            preimage_digest=preimage,
            postimage_digest="",
            path=target,
        )
    )
    if case.scenario == "crash_ambiguous":
        (repo / target).write_text("mangled by a crash\n")

    recovery = RecoveryService(store, workspace)
    report = await recovery.inspect(run_id)
    result.status = "resumable" if report.can_resume else "blocked"
    result.stop_reason = ",".join(f.classification for f in report.findings)

    expect = case.expect
    if expect.status == "resumable" and not report.can_resume:
        result.passed = False
        result.failures.append(f"expected resumable; blocked by {report.blockers}")
    if expect.status == "blocked":
        if report.can_resume:
            result.passed = False
            result.failures.append("expected recovery to be blocked, but it was resumable")
        executions = await store.load_executions(run_id)
        if executions[0].effect_state is not EffectState.STARTED:
            result.passed = False
            result.failures.append("ambiguous effect was auto-reconciled; it must stay pending")
    if case.scenario == "crash_ambiguous" and expect.allowed_changed_files:
        pass  # the tamper itself is the allowed change


def load_cases(cases_dir: Path) -> list[EvalCase]:
    case_files = sorted(cases_dir.glob("*.json"))
    if not case_files:
        raise FileNotFoundError(f"no eval cases found in {cases_dir}")
    return [EvalCase.model_validate_json(path.read_text(encoding="utf-8")) for path in case_files]


async def run_suite(
    cases_dir: Path,
    out_dir: Path,
    *,
    model_factory: ModelFactory | None = None,
    categories: tuple[str, ...] = (),
    report_name: str = "report",
) -> SuiteReport:
    """Run the case suite and write JSON + Markdown reports.

    Offline by default. Passing `model_factory` switches to live mode, which
    uses a real provider (and therefore costs money and is non-deterministic).
    """
    started_at = datetime.now(UTC).isoformat()
    started = time.monotonic()
    fixtures_dir = cases_dir.parent / "fixtures"

    cases = load_cases(cases_dir)
    if categories:
        cases = [case for case in cases if case.category in categories]
    if model_factory is not None:
        # Scripted-only scenarios have no meaning against a real model.
        cases = [case for case in cases if not case.scenario]
    if not cases:
        raise FileNotFoundError(f"no eval cases in {cases_dir} matched the selection")

    results = [await run_case(case, fixtures_dir, model_factory) for case in cases]

    report = SuiteReport(
        results=results,
        started_at=started_at,
        duration_ms=int((time.monotonic() - started) * 1000),
        live=model_factory is not None,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{report_name}.json").write_text(report.to_json(), encoding="utf-8")
    (out_dir / f"{report_name}.md").write_text(report.to_markdown(), encoding="utf-8")
    return report
