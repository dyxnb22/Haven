"""用户预设走真实 CLI、运行循环和持久化流程，Provider 流量留在本地。"""

import json
from functools import partial
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from haven import bootstrap
from haven.adapters.providers.scripted import ScriptedModel
from haven.application.approvals import AutoApprover
from haven.config import ConfigError, load_config, user_config_path
from haven.contracts.events import EventEnvelope, ModelCompleted, RunCreated
from haven.contracts.user_input import UserInputResponse
from haven.domain.enums import PermissionMode, RunStatus
from haven.interfaces.cli import app
from tests.contract.test_openai_compatible import chunk, sse
from tests.integration.harness import CollectingSink, finish, make_repo, text
from tests.integration.test_user_input import ask_turn


@pytest.fixture
def provider_config(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HAVEN_API_KEY", "test-model-preset-key")
    for name in ("HAVEN_FAST_MODEL", "HAVEN_DEEP_MODEL", "HAVEN_FAST_TURNS"):
        monkeypatch.delenv(name, raising=False)
    path = user_config_path()
    path.write_text(
        '[provider]\nmodel = "other-model"\n[model_presets.mini]\nmodel = "gpt-4o-mini"\n'
    )
    return path


@pytest.mark.parametrize("official", [True, False])
def test_cli_prices_only_official_model_and_shows_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_config: Path, official: bool
) -> None:
    repo = make_repo(tmp_path)
    if not official:
        monkeypatch.setenv("HAVEN_BASE_URL", "https://gateway.example/v1")
    requests = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(json.loads(req.content))
        return httpx.Response(
            200,
            content=chunk({"content": "Inspected."}, finish="stop")
            + sse({"choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 500}})
            + b"data: [DONE]\n\n",
        )

    monkeypatch.setattr(
        bootstrap,
        "OpenAICompatibleModel",
        partial(bootstrap.OpenAICompatibleModel, transport=httpx.MockTransport(handler)),
    )
    runner = CliRunner()
    description = runner.invoke(app, ["config", "explain", "-w", str(repo)])
    assert description.exit_code == 0, description.output
    assert "Model preset mini [user]" in description.stdout
    assert ("128,000 tokens" in description.stdout) == official
    assert requests == []

    trace = tmp_path / "events.jsonl"
    run = runner.invoke(
        app,
        [
            "run",
            "Inspect the project",
            "-w",
            str(repo),
            "--model-preset",
            "mini",
            "--json",
            "--events",
            str(trace),
        ],
    )
    assert run.exit_code == 0, run.output
    assert len(requests) == 1 and requests[0]["model"] == "gpt-4o-mini"
    assert "reasoning_effort" not in requests[0]
    outcome = json.loads(run.stdout)
    assert outcome["cost_known"] is official
    assert outcome["cost_usd"] == pytest.approx(0.00045 if official else 0.0)
    events = [
        EventEnvelope.model_validate_json(line).event for line in trace.read_text().splitlines()
    ]
    created = next(event for event in events if isinstance(event, RunCreated))
    completed = next(event for event in events if isinstance(event, ModelCompleted))
    assert created.model_preset == "mini"
    info = completed.model_info
    assert info is not None and info.usage_source == "provider"
    assert info.selection_reason == "single" and info.cost_known is official
    assert info.pricing_source == ("model-profile:gpt-4o-mini" if official else "unknown")


def test_presets_cannot_configure_a_different_endpoint_or_come_from_the_project(
    tmp_path: Path, provider_config: Path
) -> None:
    repo = make_repo(tmp_path)
    (repo / ".haven.toml").write_text('[model_presets.mini]\nmodel = "gpt-4o-mini"\n')
    with pytest.raises(ConfigError, match="model_presets"):
        load_config(repo)
    (repo / ".haven.toml").unlink()
    provider_config.write_text(
        '[model_presets.mini]\nmodel = "gpt-4o-mini"\nbase_url = "https://example.org"\n'
    )
    with pytest.raises(ConfigError, match="model preset 'mini'"):
        load_config(repo)


def test_luna_uses_chat_tool_calling_with_reasoning_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_config: Path
) -> None:
    provider_config.write_text('[model_presets.luna]\nmodel = "gpt-6-luna"\n')
    repo = make_repo(tmp_path)
    requests = []

    def handler(req: httpx.Request) -> httpx.Response:
        payload = json.loads(req.content)
        requests.append(payload)
        assert req.url.path == "/v1/chat/completions"
        assert payload["model"] == "gpt-6-luna" and payload["reasoning_effort"] == "none"
        assert payload["max_completion_tokens"] > 0 and "max_tokens" not in payload
        assert any(item["function"]["name"] == "repo__read" for item in payload["tools"])
        if len(requests) == 1:
            body = chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "read",
                            "type": "function",
                            "function": {
                                "name": "repo__read",
                                "arguments": '{"path":"src/calc.py"}',
                            },
                        }
                    ]
                },
                finish="tool_calls",
            )
        else:
            assert any(
                m["role"] == "tool" and m["tool_call_id"] == "read" for m in payload["messages"]
            )
            body = chunk({"content": "Inspected the file."}, finish="stop")
        return httpx.Response(200, content=body + b"data: [DONE]\n\n")

    monkeypatch.setattr(
        bootstrap,
        "OpenAICompatibleModel",
        partial(bootstrap.OpenAICompatibleModel, transport=httpx.MockTransport(handler)),
    )
    run = CliRunner().invoke(
        app, ["run", "Inspect a file", "-w", str(repo), "--model-preset", "luna", "--json"]
    )
    assert run.exit_code == 0, run.output
    assert len(requests) == 2
    outcome = json.loads(run.stdout)
    assert outcome["final_text"] == "Inspected the file."
    assert outcome["cost_known"] is False and outcome["usage_estimated"] is True


async def test_reopen_uses_saved_route_and_refuses_changed_endpoint_before_answering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_config: Path
) -> None:
    provider_config.write_text(
        '[model_presets.inspect]\nfast_model = "fast-fixture"\n'
        'deep_model = "deep-fixture"\nfast_turns = 1\n'
    )
    repo = make_repo(tmp_path)
    models = {
        "fast-fixture": ScriptedModel([ask_turn()], name="fast-fixture"),
        "deep-fixture": ScriptedModel([[text("Inspected parser."), finish()]], name="deep-fixture"),
        "gpt-4o-mini": ScriptedModel([[text("New inspection."), finish()]], name="gpt-4o-mini"),
    }
    monkeypatch.setattr(
        bootstrap, "_build_provider_model", lambda url, key, name, profile: models[name]
    )
    sink = CollectingSink()
    kwargs = dict(mode=PermissionMode.READ_ONLY, approvals=AutoApprover("reject_all"), sinks=[sink])
    services = await bootstrap.build_services(repo, **kwargs)
    try:
        paused = await services.runtime.start_run("Inspect parser", model_preset="inspect")
        assert paused.pending_input is not None
        checkpoint = await services.store.load_checkpoint(paused.run_id)
        assert checkpoint is not None and checkpoint.model_selection is not None
        response = UserInputResponse(
            request_id=paused.pending_input.request_id, answers={"scope": "Parser"}
        )
    finally:
        await services.close()

    provider_config.write_text('[model_presets.inspect]\nmodel = "gpt-4o-mini"\n')
    with monkeypatch.context() as changed:
        changed.setenv("HAVEN_BASE_URL", "https://another.example/v1")
        services = await bootstrap.build_services(repo, **kwargs)
        try:
            with pytest.raises(ValueError, match="provider endpoint or configured pricing changed"):
                await services.runtime.answer_run(paused.run_id, response)
            assert await services.store.load_checkpoint(paused.run_id) == checkpoint
            assert not models["deep-fixture"].requests_seen
        finally:
            await services.close()

    services = await bootstrap.build_services(repo, **kwargs)
    try:
        resumed = await services.runtime.answer_run(paused.run_id, response)
        assert resumed.status is RunStatus.SUCCEEDED and resumed.run_id == paused.run_id
        calls = [
            event
            for event in sink.events_of("model.completed")
            if isinstance(event, ModelCompleted)
        ]
        assert [event.model_info.model_name for event in calls if event.model_info] == [
            "fast-fixture",
            "deep-fixture",
        ]
        assert (
            calls[-1].model_info is not None
            and calls[-1].model_info.selection_reason == "fast_turns"
        )
        assert not models["gpt-4o-mini"].requests_seen
        new_run = await services.runtime.continue_run(
            paused.run_id, "Inspect a different component", model_preset="inspect"
        )
        assert new_run.status is RunStatus.SUCCEEDED
        assert len(models["gpt-4o-mini"].requests_seen) == 1
    finally:
        await services.close()
