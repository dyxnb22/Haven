"""TUI 的下一轮模型选项与正在运行的 actor 分开。"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from haven import bootstrap
from haven.adapters.providers.scripted import ScriptedModel
from haven.contracts.model import ModelEvent, ModelRequest
from haven.interfaces.tui.app import HavenApp
from tests.integration.harness import finish, make_repo, text
from tests.integration.test_model_presets import provider_config as provider_config
from tests.tui.test_tui_journey import _settle, _submit, _wait_ready


@pytest.mark.timeout(30)
async def test_switching_next_preset_keeps_the_active_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_config: Path
) -> None:
    release = asyncio.Event()
    entered = asyncio.Event()

    class WaitingModel(ScriptedModel):
        async def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
            entered.set()
            await release.wait()
            async for event in super().generate_stream(request):
                yield event

    base = WaitingModel([[text("Base model reply."), finish()]], name="other-model")
    mini = ScriptedModel([[text("Mini model reply."), finish()]], name="gpt-4o-mini")
    models = {model.model_name: model for model in (base, mini)}
    monkeypatch.setattr(
        bootstrap, "_build_provider_model", lambda url, key, name, profile: models[name]
    )
    app = HavenApp(workspace=make_repo(tmp_path), worktree=False)
    async with app.run_test() as pilot:
        await _wait_ready(app, pilot)
        await _submit(app, pilot, "Inspect a component")
        await asyncio.wait_for(entered.wait(), timeout=3)
        try:
            assert app._state.running
            await _submit(app, pilot, "/model use mini")
            assert app._state.next_model_preset == "mini"
            assert app._state.model_name == "other-model"
            assert mini.requests_seen == []
        finally:
            release.set()
        await _settle(pilot, 10)
        assert app._state.status == "succeeded"
        assert app._state.last_model_call is not None
        assert app._state.last_model_call.model_name == "other-model"
        assert "cost unknown" in app._state.header_line()
        await _submit(app, pilot, "Check one more detail")
        await _settle(pilot, 10)
        assert app._state.status == "succeeded"
        assert app._state.model_preset == "mini"
        assert app._state.last_model_call is not None
        assert app._state.last_model_call.model_name == "gpt-4o-mini"
        assert app._state.last_model_call.usage_source == "estimated"
        assert "estimated" in app._state.header_line()
        assert len(base.requests_seen) == len(mini.requests_seen) == 1
        await _submit(app, pilot, "/stats")
        await _settle(pilot, 5)
        summary = app._state.timeline[-1].text
        assert "1 completed / 1 started steps" in summary
        assert "other-model" not in summary
        assert "gpt-4o-mini" in summary and "estimated" in summary
