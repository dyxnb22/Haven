"""日志记录模型被*告知*的内容，而不只是被要求做的内容。

`context.built` 记录某轮选中的消息，但请求的其余部分同样对模型可见：系统规则、
提供的工具模式和采样参数。过去这些没有写入日志，因此重放运行可以重建对话，
却不能重建塑造对话的指令；两次运行之间的提示词变化也会在追踪中消失。

事件在第一步记录，之后只有发生变化时才再次记录，因此长运行的成本很低，同时仍
能回答每一步“模型在这一步被告知了什么？”。
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
        """日志保存摘要和有界摘要信息，而不是原始载荷。"""
        h = Harness(make_repo(tmp_path), [[text("done"), finish()]])
        await h.service.run("Do a thing")

        envelope = _envelopes(h)[0]
        assert len(envelope.system_prompt_digest) >= 8
        assert "You are Haven" not in envelope.system_prompt_digest
        assert envelope.system_prompt_chars > 0


class TestEnvelopeIsNotRepeated:
    async def test_an_unchanged_envelope_is_logged_once_across_steps(self, tmp_path: Path) -> None:
        """稳定前缀正是设计目标（ADR 0008）；每一步重复记录它只会在没有新信息的
        情况下，按运行长度增加噪声。"""
        turns = [
            [tool("c1", "repo.list", path="."), finish("tool_calls")],
            [tool("c2", "repo.list", path="src"), finish("tool_calls")],
            [text("done"), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("Look around")

        assert outcome.steps >= 3
        assert len(_envelopes(h)) == 1, "the envelope never changed, so it is logged once"
