"""Golden trace: the same fixture + ScriptedModel must always produce the same
core event sequence, and the TUI must produce the same trace as headless.

The golden file is committed. Regenerate deliberately with:

    HAVEN_UPDATE_GOLDEN=1 uv run pytest tests/golden -q

and review the diff — a change here means agent-visible behavior changed.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from haven.contracts.events import EventEnvelope
from haven.domain.enums import RunStatus
from tests.integration.harness import Harness, finish, make_repo, text, tool

GOLDEN_DIR = Path(__file__).parent / "data"

#: Fields that legitimately vary run to run and are therefore not part of the
#: golden contract (timings, absolute paths, content digests, generated ids).
_VOLATILE = {
    "run_id",
    "workspace",
    "workspace_digest",
    "approval_id",
    "request_digest",
    "ticket_digest",
    "duration_ms",
    "ttft_ms",
    "at",
    "seq",
    "git_branch",
    "git_commit",
    "cost_usd",
}


def normalize(envelopes: list[EventEnvelope]) -> list[dict[str, Any]]:
    """Reduce a trace to its stable, behavior-defining shape."""
    normalized: list[dict[str, Any]] = []
    for envelope in envelopes:
        payload = envelope.event.model_dump(mode="json")
        stable = {k: v for k, v in payload.items() if k not in _VOLATILE}
        if "summary" in stable and isinstance(stable["summary"], str):
            # digests and byte counts appear inside human summaries
            stable["summary"] = _mask(stable["summary"])
        if "preview" in stable:
            # Previews embed absolute paths (the interpreter a recipe runs,
            # the workspace root), so even their length varies by machine.
            # What a preview must contain is asserted by the integration tests
            # that care; here only its presence is part of the contract.
            stable["preview"] = "<preview>"
        if stable.get("kind") == "context.built":
            stable = _mask_timing_sizes(stable)
        if stable.get("usage_estimated") is True:
            # An estimated count is characters//4 over a transcript that
            # contains measured durations, so a check taking 99 ms instead of
            # 100 ms moves it by a token. That the estimate *was* used is
            # behavior and stays; its exact value is a stopwatch artifact.
            for key in ("input_tokens", "output_tokens"):
                if key in stable:
                    stable[key] = "<estimated>"
        if "text" in stable and len(str(stable["text"])) > 120:
            stable["text"] = f"<{len(str(stable['text']))} chars>"
        normalized.append(stable)
    return normalized


def _mask_timing_sizes(event: dict[str, Any]) -> dict[str, Any]:
    """Drop byte counts that a stopwatch can change.

    A tool output carries `duration_ms`, so a check that takes 99 ms produces a
    segment one byte smaller than one taking 100 ms. The sizes worth pinning
    are the program-authored ones — the system prompt above all — so those stay
    and only the timing-bearing segments are masked.
    """
    masked = dict(event)
    masked.pop("total_bytes", None)
    masked["segments"] = [
        {**segment, "size_bytes": "<varies>"} if segment.get("source") == "tool_output" else segment
        for segment in event.get("segments", [])
    ]
    return masked


def _mask(value: str) -> str:
    masked = re.sub(r"\b[0-9a-f]{8,}\b", "<digest>", value)
    masked = re.sub(r"\b\d+ms\b", "<ms>", masked)
    return re.sub(r'"duration_ms":\s*\d+', '"duration_ms": <ms>', masked)


def load_or_write_golden(name: str, actual: list[dict[str, Any]]) -> list[dict[str, Any]]:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    path = GOLDEN_DIR / f"{name}.json"
    if os.environ.get("HAVEN_UPDATE_GOLDEN") or not path.exists():
        path.write_text(json.dumps(actual, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def edit_journey_turns() -> list[list[Any]]:
    return [
        [
            text("Locating the bug."),
            tool("c1", "repo.search", pattern="BUG", path="."),
            finish("tool_calls"),
        ],
        [tool("c2", "repo.read", path="src/calc.py"), finish("tool_calls")],
        [
            tool(
                "c3",
                "repo.edit",
                path="src/calc.py",
                old_string="return a - b  # BUG: should be +",
                new_string="return a + b",
                summary="fix add()",
            ),
            finish("tool_calls"),
        ],
        [tool("c4", "repo.diff"), finish("tool_calls")],
        [tool("c5", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
        [text("Fixed add(); diff and verify-calc both recorded."), finish()],
    ]


class TestGoldenTrace:
    async def test_edit_journey_matches_golden(self, tmp_path: Path) -> None:
        h = Harness(make_repo(tmp_path), edit_journey_turns())
        outcome = await h.service.run("Fix the bug in add()")
        assert outcome.status is RunStatus.SUCCEEDED

        stored = await h.store.load_events(outcome.run_id)
        actual = normalize(stored)
        expected = load_or_write_golden("edit_journey", actual)
        assert actual == expected

    async def test_trace_is_stable_across_identical_runs(self, tmp_path: Path) -> None:
        first_repo = make_repo(tmp_path / "a")
        second_repo = make_repo(tmp_path / "b")

        h1 = Harness(first_repo, edit_journey_turns())
        outcome1 = await h1.service.run("Fix the bug in add()")
        h2 = Harness(second_repo, edit_journey_turns())
        outcome2 = await h2.service.run("Fix the bug in add()")

        assert normalize(await h1.store.load_events(outcome1.run_id)) == normalize(
            await h2.store.load_events(outcome2.run_id)
        )

    async def test_golden_covers_the_whole_pipeline(self, tmp_path: Path) -> None:
        """The golden trace is only meaningful if it exercises every gate."""
        h = Harness(make_repo(tmp_path), edit_journey_turns())
        outcome = await h.service.run("Fix the bug in add()")
        kinds = {env.event.kind for env in await h.store.load_events(outcome.run_id)}
        assert {
            "run.created",
            "step.started",
            "context.built",
            "model.completed",
            "tool.proposed",
            "policy.decided",
            "approval.requested",
            "approval.decided",
            "execution.started",
            "tool.completed",
            "evidence.recorded",
            "diff.preview",
            "run.finished",
        } <= kinds


@pytest.mark.timeout(60)
async def test_tui_and_headless_produce_the_same_trace(tmp_path: Path) -> None:
    """Week-6 invariant: the TUI is interface-only, so driving the same
    ScriptedModel through it must yield the same core trace as headless."""
    from haven.interfaces.tui.app import HavenApp
    from tests.tui.test_tui_journey import (
        _approve_pending,
        _settle,
        _submit,
        _wait_ready,
        make_builder,
    )

    headless_repo = make_repo(tmp_path / "headless")
    h = Harness(headless_repo, edit_journey_turns())
    outcome = await h.service.run("Fix the bug in add()")
    headless = normalize(await h.store.load_events(outcome.run_id))

    tui_repo = make_repo(tmp_path / "tui")
    app = HavenApp(
        workspace=tui_repo, services_builder=make_builder(tui_repo, edit_journey_turns())
    )
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "Fix the bug in add()")
        await _approve_pending(app, pilot, "a", count=2)
        await _settle(pilot, 60)
        assert app._state.status == "succeeded"  # noqa: SLF001
        tui_run_id = app._state.run_id  # noqa: SLF001
        tui = normalize(await app._services.store.load_events(tui_run_id))  # noqa: SLF001

    assert tui == headless
