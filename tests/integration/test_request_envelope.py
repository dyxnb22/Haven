"""What the model was *told* — not just what it was asked — is in the journal.

`context.built` records the messages selected for a turn, but the rest of the
request is equally model-visible: the system rules, the tool schemas offered,
and the sampling parameters. None of it was journaled, so a replayed run could
reconstruct the conversation but not the instructions that shaped it — and a
prompt change between runs was invisible in the trace.

The envelope is logged on the first step and again only when it changes, so it
stays cheap on a long run while still answering "what was the model told, at
this step?" for every step.
"""

from pathlib import Path

from haven.contracts.events import RequestEnvelope
from tests.integration.harness import Harness, finish, make_repo, text, tool


def _envelopes(h: Harness) -> list[RequestEnvelope]:
    return [e for e in h.sink.events_of("request.envelope") if isinstance(e, RequestEnvelope)]


class TestEnvelopeIsRecorded:
    async def test_the_first_step_records_the_envelope(self, tmp_path: Path) -> None:
        h = Harness(make_repo(tmp_path), [[text("done"), finish()]])
        await h.service.run("Do a thing")

        recorded = _envelopes(h)
        assert len(recorded) == 1
        assert recorded[0].reason == "initial"
        assert recorded[0].step == 1

    async def test_it_names_the_tools_the_model_was_offered(self, tmp_path: Path) -> None:
        h = Harness(make_repo(tmp_path), [[text("done"), finish()]])
        await h.service.run("Do a thing")

        envelope = _envelopes(h)[0]
        assert "repo.read" in envelope.tool_names
        assert "task.plan" in envelope.tool_names

    async def test_it_carries_a_digest_rather_than_the_prompt_text(self, tmp_path: Path) -> None:
        """The journal keeps digests and bounded summaries, not payloads."""
        h = Harness(make_repo(tmp_path), [[text("done"), finish()]])
        await h.service.run("Do a thing")

        envelope = _envelopes(h)[0]
        assert len(envelope.system_prompt_digest) >= 8
        assert "You are Haven" not in envelope.system_prompt_digest
        assert envelope.system_prompt_chars > 0


class TestEnvelopeIsNotRepeated:
    async def test_an_unchanged_envelope_is_logged_once_across_steps(self, tmp_path: Path) -> None:
        """A stable prefix is the point (ADR 0008); re-logging it every step
        would add noise proportional to run length for no new information."""
        turns = [
            [tool("c1", "repo.list", path="."), finish("tool_calls")],
            [tool("c2", "repo.list", path="src"), finish("tool_calls")],
            [text("done"), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("Look around")

        assert outcome.steps >= 3
        assert len(_envelopes(h)) == 1, "the envelope never changed, so it is logged once"
