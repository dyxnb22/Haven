"""模型路由的上下文和费用必须属于实际调用的模型。"""

from pathlib import Path

import pytest

from haven import bootstrap
from haven.adapters.providers.scripted import ScriptedModel
from haven.application import profiles
from haven.application.approvals import AutoApprover
from haven.application.compaction import message_chars
from haven.application.model_router import ModelRouter
from haven.application.profiles import ModelProfile
from haven.contracts.events import ModelCompleted
from haven.domain.enums import PermissionMode
from haven.domain.pricing import Pricing
from tests.integration.harness import CollectingSink, finish, make_repo, text, tool, usage


async def routed_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, unknown_deep: bool = False
):
    fast_profile = ModelProfile(
        name="fast-fixture",
        max_context_chars=16_000,
        reasoning_effort="low",
        pricing=Pricing(input_per_1m_usd=1, output_per_1m_usd=2),
    )
    deep_profile = ModelProfile(
        name="deep-fixture",
        max_context_chars=48_000,
        reasoning_effort="high",
        pricing=Pricing() if unknown_deep else Pricing(input_per_1m_usd=10, output_per_1m_usd=20),
    )
    monkeypatch.setitem(profiles._PROFILES, fast_profile.name, fast_profile)
    monkeypatch.setitem(profiles._PROFILES, deep_profile.name, deep_profile)
    repo = make_repo(tmp_path)
    for index in range(3):
        (repo / f"large-{index}.txt").write_text("long line " * 8000)
    fast = ScriptedModel(
        [
            [
                tool(f"read-{index}", "repo.read", path=f"large-{index}.txt"),
                usage(100, 10),
                finish("tool_calls"),
            ]
            for index in range(3)
        ],
        name=fast_profile.name,
    )
    deep = ScriptedModel([[text("Inspected"), usage(100, 10), finish()]], name=deep_profile.name)
    sink = CollectingSink()
    services = await bootstrap.build_services(
        repo,
        mode=PermissionMode.READ_ONLY,
        approvals=AutoApprover("reject_all"),
        sinks=[sink],
        model=ModelRouter(fast, deep, fast_turns=3),
        store_path=tmp_path / "sessions.db",
    )
    return services, fast, deep, sink, fast_profile, deep_profile


async def test_routed_context_and_reasoning_follow_the_actual_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services, fast, deep, _, fast_profile, deep_profile = await routed_services(
        tmp_path, monkeypatch
    )
    try:
        await services.run_service.run("Inspect large files")
        assert len(fast.requests_seen) == 3 and len(deep.requests_seen) == 1
        assert all(
            sum(message_chars(item) for item in request.messages) <= fast_profile.max_context_chars
            for request in fast.requests_seen
        )
        assert all(request.reasoning_effort == "low" for request in fast.requests_seen)
        assert deep.requests_seen[0].reasoning_effort == "high"
        assert (
            sum(message_chars(item) for item in deep.requests_seen[0].messages)
            <= deep_profile.max_context_chars
        )
    finally:
        await services.close()


async def test_routed_cost_uses_each_actual_rate_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services, _, _, _, fast_profile, deep_profile = await routed_services(tmp_path, monkeypatch)
    try:
        outcome = await services.run_service.run("Inspect large files")
        expected = 3 * fast_profile.pricing.cost(100, 10) + deep_profile.pricing.cost(100, 10)
        assert outcome.cost_usd == pytest.approx(expected)
        assert outcome.cost_known
    finally:
        await services.close()


async def test_one_unpriced_model_makes_the_total_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services, _, _, _, fast_profile, _ = await routed_services(
        tmp_path, monkeypatch, unknown_deep=True
    )
    try:
        outcome = await services.run_service.run("Inspect large files")
        assert outcome.cost_known is False
        assert outcome.cost_usd == pytest.approx(3 * fast_profile.pricing.cost(100, 10))
    finally:
        await services.close()


async def test_completed_event_reports_the_usage_actually_charged_to_the_run(
    tmp_path: Path,
) -> None:
    from tests.integration.harness import Harness

    h = Harness(make_repo(tmp_path), [[text("Inspected"), finish()]])
    outcome = await h.service.run("Inspect a detail")
    event = next(
        item for item in h.sink.events_of("model.completed") if isinstance(item, ModelCompleted)
    )
    assert event.usage_estimated and event.input_tokens > 0
    assert (event.input_tokens, event.output_tokens) == (
        outcome.input_tokens,
        outcome.output_tokens,
    )
