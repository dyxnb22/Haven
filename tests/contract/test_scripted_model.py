import pytest

from haven.adapters.providers.scripted import ScriptedModel
from haven.contracts.model import (
    ModelEvent,
    ModelMessage,
    ModelRequest,
    StreamFinished,
    TextDelta,
    ToolCallProposal,
    ToolCallReady,
)
from haven.ports.model import ProviderError


def request() -> ModelRequest:
    return ModelRequest(messages=(ModelMessage(role="user", content="hi"),))


async def collect(model: ScriptedModel) -> list[ModelEvent]:
    return [event async for event in model.generate_stream(request())]


async def test_plays_turns_in_order() -> None:
    model = ScriptedModel(
        [
            [TextDelta(text="thinking"), StreamFinished(finish_reason="stop")],
            [TextDelta(text="done"), StreamFinished(finish_reason="stop")],
        ]
    )
    first = await collect(model)
    second = await collect(model)
    assert isinstance(first[0], TextDelta) and first[0].text == "thinking"
    assert isinstance(second[0], TextDelta) and second[0].text == "done"


async def test_exhausted_raises_provider_error() -> None:
    model = ScriptedModel([[StreamFinished()]])
    await collect(model)
    with pytest.raises(ProviderError) as exc:
        await collect(model)
    assert exc.value.code == "exhausted"


async def test_repeat_last_for_stuck_loop_scenarios() -> None:
    model = ScriptedModel(
        [
            [
                ToolCallReady(
                    call=ToolCallProposal(
                        call_id="c1", tool_name="repo.search", arguments_json='{"pattern": "x"}'
                    )
                ),
                StreamFinished(finish_reason="tool_calls"),
            ]
        ],
        repeat_last=True,
    )
    for _ in range(5):
        events = await collect(model)
        assert isinstance(events[0], ToolCallReady)


async def test_from_json_fixture() -> None:
    fixture = """
    {
      "turns": [
        [
          {"kind": "text_delta", "text": "hello"},
          {"kind": "tool_call", "call": {"call_id": "c1", "tool_name": "repo.read",
           "arguments_json": "{\\"path\\": \\"a.py\\"}"}},
          {"kind": "finished", "finish_reason": "tool_calls"}
        ]
      ]
    }
    """
    model = ScriptedModel.from_json(fixture)
    events = await collect(model)
    assert len(events) == 3
    assert isinstance(events[1], ToolCallReady)
    assert events[1].call.tool_name == "repo.read"


async def test_records_requests_for_assertions() -> None:
    model = ScriptedModel([[StreamFinished()]])
    await collect(model)
    assert len(model.requests_seen) == 1
    assert model.requests_seen[0].messages[0].content == "hi"
