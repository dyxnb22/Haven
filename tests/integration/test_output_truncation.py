"""从提供商输出限制和无内容回复中恢复。

`finish_reason="length"` 的回答按定义是不完整的：静默接受会把看起来完整的半截
答案交给用户。Haven 会请求有界的续写并拼接各部分。既没有文本也没有工具调用的
回复（只有 reasoning 的回复）会重新提示，在有界重试后以无进展停止，而不是用空
答案报告成功。
"""

from pathlib import Path

from haven.contracts.events import Notice
from haven.domain.enums import RunStatus, StopReason
from tests.integration.harness import Harness, finish, make_repo, text, tool


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

        switched = Harness(
            make_repo(tmp_path / "switched"),
            [
                [text("obsolete partial"), finish("length")],
                [tool("c1", "repo.list", path="."), finish("tool_calls")],
                [text("fresh answer"), finish()],
            ],
        )
        switched_outcome = await switched.service.run("Investigate, then answer")
        assert switched_outcome.final_text == "fresh answer"

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
        """一直截断的模型最多获得两次续写，然后带着警告继续处理部分答案——不能让
        它耗尽预算。"""
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


class TestNativePrefixContinuation:
    """对于支持原生前缀续写的 profile（ADR 0022），截断的部分会作为 assistant
    *prefix* 重新发送，由模型原地延长，而不是向用户发送“继续”提示——不会在接缝
    处重复内容。"""

    async def test_prefix_capable_profile_resends_the_partial_as_assistant_prefix(
        self, tmp_path: Path, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        import haven.application.run_service as rs
        from haven.application.profiles import ModelProfile

        prefix_profile = ModelProfile(name="scripted", supports_assistant_prefix=True)
        monkeypatch.setattr(rs, "profile_for", lambda _name: prefix_profile)

        turns = [
            [text("first half"), finish("length")],
            [text(" second half"), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns)
        outcome = await h.service.run("Explain something long")

        assert outcome.status is RunStatus.SUCCEEDED
        # 续写请求以部分答案作为 assistant 前缀结尾，并且不携带“被截断”的
        # 用户追加提示。
        last = h.model.requests_seen[-1].messages
        assert last[-1].role == "assistant"
        assert last[-1].is_prefix and last[-1].content == "first half"
        assert not any("cut off" in m.content for m in last if m.role == "user")

    async def test_the_endpoint_guard_falls_back_to_the_shim(
        self, tmp_path: Path, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """DeepSeek 只在 beta endpoint 接受 `prefix: true`。部署指向其他位置时必须
        强制关闭该能力，否则每次截断轮次都会返回 400——因此显式覆盖优先于 profile
        自身的标志。"""
        import haven.application.run_service as rs
        from haven.application.profiles import ModelProfile

        capable = ModelProfile(name="scripted", supports_assistant_prefix=True)
        monkeypatch.setattr(rs, "profile_for", lambda _name: capable)

        turns = [
            [text("first half"), finish("length")],
            [text(" second half"), finish()],
        ]
        h = Harness(make_repo(tmp_path), turns, supports_prefix_continuation=False)
        outcome = await h.service.run("Explain something long")

        assert outcome.status is RunStatus.SUCCEEDED
        last = h.model.requests_seen[-1].messages
        assert not any(m.is_prefix for m in last), "the guard must suppress the prefix"
        assert any("cut off" in m.content for m in last if m.role == "user")


class TestEmptyReplies:
    async def test_an_empty_reply_is_reprompted_then_answered(self, tmp_path: Path) -> None:
        turns = [
            [finish()],  # 没有内容，也没有工具调用
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
