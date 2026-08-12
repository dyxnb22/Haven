"""Recovery from provider output limits and content-free replies.

A `finish_reason="length"` answer is incomplete by definition: accepting it
silently would hand the user half an answer that looks whole. Haven requests a
bounded continuation and stitches the parts. A reply with neither text nor tool
calls (a reasoning-only response) is re-prompted, bounded, and then stops as
no-progress rather than succeeding with an empty answer.
"""

from pathlib import Path

from haven.contracts.events import Notice
from haven.domain.enums import RunStatus, StopReason
from tests.integration.harness import Harness, finish, make_repo, text


def _warnings(h: Harness) -> list[str]:
    return [
        e.message
        for e in h.sink.events_of("notice")
        if isinstance(e, Notice) and e.level == "warning"
    ]


class TestTruncatedAnswers:
    async def test_a_truncated_answer_is_continued_and_stitched(self, tmp_path: Path) -> None:
        turns = [
            [text("The answer is: first half"), finish("length")],
            [text(" and second half."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("Explain something long")

        assert outcome.status is RunStatus.SUCCEEDED
        assert outcome.final_text == "The answer is: first half and second half."
        assert any("output token limit" in w for w in _warnings(h))

    async def test_the_continuation_request_tells_the_model_not_to_repeat(
        self, tmp_path: Path
    ) -> None:
        turns = [
            [text("part"), finish("length")],
            [text(" two"), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        await h.service.run("Explain")

        requests = h.model.requests_seen
        assert len(requests) >= 2
        user_texts = [m.content for m in requests[-1].messages if m.role == "user"]
        nudge = next((t for t in user_texts if "cut off" in t), None)
        assert nudge is not None, "the continuation request reached the model"
        assert "without repeating" in nudge

    async def test_continuations_are_bounded(self, tmp_path: Path) -> None:
        """A model that truncates forever gets two continuations, then the
        partial answer proceeds with a warning — it must not eat the budget."""
        turns = [
            [text("a"), finish("length")],
            [text("b"), finish("length")],
            [text("c"), finish("length")],
        ]
        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("Explain")

        assert outcome.status is RunStatus.SUCCEEDED
        assert outcome.final_text == "abc"
        assert any("still truncated" in w for w in _warnings(h))


class TestEmptyReplies:
    async def test_an_empty_reply_is_reprompted_then_answered(self, tmp_path: Path) -> None:
        turns = [
            [finish()],  # no content, no tool calls
            [text("Here is the answer."), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("Answer me")

        assert outcome.status is RunStatus.SUCCEEDED
        assert outcome.final_text == "Here is the answer."
        assert any("no content" in w for w in _warnings(h))

    async def test_persistently_empty_replies_stop_as_no_progress(self, tmp_path: Path) -> None:
        turns = [[finish()], [finish()], [finish()]]
        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("Answer me")

        assert outcome.status is RunStatus.STOPPED
        assert outcome.stop_reason is StopReason.NO_PROGRESS
