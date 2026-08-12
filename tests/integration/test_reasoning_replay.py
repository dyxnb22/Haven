"""Provider reasoning is captured and carried, without becoming the answer.

DeepSeek V4 requires the reasoning that preceded a tool call to be replayed on
later requests (ADR 0014). Haven captures it onto the assistant message so the
adapter can replay it — but it must never leak into the answer, the transcript
content, or the evidence path.
"""

from pathlib import Path

from haven.contracts.model import ModelMessage, ReasoningDelta
from tests.integration.harness import Harness, finish, make_repo, text, tool

THINK = "PRIVATE-CHAIN-OF-THOUGHT-XYZ"


def assistant_tool_turns(model: object) -> list[ModelMessage]:
    seen: list[ModelMessage] = []
    for req in model.requests_seen:  # type: ignore[attr-defined]
        for message in req.messages:
            if message.role == "assistant" and message.tool_calls:
                seen.append(message)
    return seen


class TestReasoningCapture:
    async def test_reasoning_is_carried_on_the_assistant_message(self, tmp_path: Path) -> None:
        turns = [
            [
                ReasoningDelta(text=THINK),
                tool("c1", "repo.read", path="src/calc.py"),
                finish("tool_calls"),
            ],
            [text("Read the file; here is the answer."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("Inspect calc.py")

        carriers = assistant_tool_turns(h.model)
        assert carriers, "the tool-call turn should have been re-sent on turn 2"
        assert any(m.provider_reasoning == THINK for m in carriers)

        # Reasoning is not the answer: it is absent from content and final text.
        assert all(THINK not in m.content for m in carriers)
        assert THINK not in outcome.final_text

    async def test_reasoning_survives_a_checkpoint(self, tmp_path: Path) -> None:
        turns = [
            [
                ReasoningDelta(text=THINK),
                tool("c1", "repo.read", path="src/calc.py"),
                finish("tool_calls"),
            ],
            [text("Done."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("Inspect calc.py")

        checkpoint = await h.store.load_checkpoint(outcome.run_id)
        assert checkpoint is not None
        persisted = [m for m in checkpoint.messages if m.role == "assistant" and m.tool_calls]
        assert any(m.provider_reasoning == THINK for m in persisted)
