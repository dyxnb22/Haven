"""确定性压缩：被丢弃的工具输出会变成记录的事实。

摘要直接从被丢弃的消息计算，绝不依赖活动运行状态，因此在不同压缩事件之间保持
逐字节一致，也不会移动可缓存前缀（ADR 0008）。
"""

from haven.application.compaction import (
    build_run_digest,
    enforce_hard_limit,
    message_chars,
    summarize_dropped,
)
from haven.contracts.model import ModelMessage, ToolCallProposal


def tool_message(tool: str, body: str, call_id: str = "c1") -> ModelMessage:
    return ModelMessage(
        role="tool",
        content=f'<tool_output tool="{tool}">\n{body}\n</tool_output>',
        tool_call_id=call_id,
    )


def read_result(path: str, digest: str, content: str = "irrelevant") -> str:
    return (
        f'{{"status": "ok", "result": {{"path": "{path}", '
        f'"digest": "{digest}", "content": "{content}"}}}}'
    )


class TestFactsSurvive:
    def test_reads_are_listed_with_their_paths(self) -> None:
        digest = build_run_digest([tool_message("repo.read", read_result("src/calc.py", "a1b2"))])
        assert "src/calc.py" in digest

    def test_edits_are_listed(self) -> None:
        body = (
            '{"status": "ok", "result": {"path": "src/calc.py", '
            '"applied": true, "postimage_digest": "9c8d7e6f"}}'
        )
        digest = build_run_digest([tool_message("repo.edit", body)])
        assert "edited" in digest
        assert "src/calc.py" in digest

    def test_checks_keep_their_recipe_and_exit_code(self) -> None:
        body = '{"status": "ok", "result": {"recipe_id": "verify-calc", "exit_code": 0}}'
        digest = build_run_digest([tool_message("repo.check", body)])
        assert "verify-calc" in digest
        assert "exit 0" in digest

    def test_a_failing_check_is_not_reported_as_passing(self) -> None:
        body = '{"status": "ok", "result": {"recipe_id": "verify-calc", "exit_code": 1}}'
        assert "exit 1" in build_run_digest([tool_message("repo.check", body)])

    def test_other_tools_are_counted_not_detailed(self) -> None:
        messages = [
            tool_message("repo.list", '{"status": "ok", "result": {"entries": []}}'),
            tool_message("repo.search", '{"status": "ok", "result": {"matches": []}}'),
        ]
        digest = build_run_digest(messages)
        assert "2" in digest
        assert "repo.list" in digest


class TestTrustIsHonest:
    def test_file_content_never_reaches_the_digest(self) -> None:
        """摘要被标记为可信，因此只能携带程序生成的事实，绝不能携带仓库文本，否则
        会将不可信字节洗成可信内容。"""
        secret = "AKIAIOSFODNN7EXAMPLE-in-a-file"
        digest = build_run_digest(
            [tool_message("repo.read", read_result("src/calc.py", "a1b2", content=secret))]
        )
        assert secret not in digest

    def test_model_prose_is_not_summarized(self) -> None:
        """只有工具输出会被丢弃，因此 assistant 文本不能出现在摘要中。"""
        messages = [ModelMessage(role="assistant", content="I think the bug is obvious")]
        assert "obvious" not in build_run_digest(messages)


class TestRobustness:
    def test_malformed_json_degrades_to_a_count(self) -> None:
        """格式错误的条目绝不能中止运行。"""
        digest = build_run_digest([tool_message("repo.read", "not json at all")])
        assert digest
        assert "repo.read" in digest

    def test_missing_wrapper_is_tolerated(self) -> None:
        message = ModelMessage(role="tool", content="bare text", tool_call_id="c9")
        assert build_run_digest([message])

    def test_empty_input_produces_no_digest(self) -> None:
        assert build_run_digest([]) == ""


class TestDeterminism:
    def test_same_messages_give_byte_identical_digests(self) -> None:
        messages = [tool_message("repo.read", read_result("a.py", "d1"))]
        assert build_run_digest(messages) == build_run_digest(list(messages))

    def test_paths_are_deduplicated_in_order(self) -> None:
        messages = [
            tool_message("repo.read", read_result("b.py", "d2")),
            tool_message("repo.read", read_result("a.py", "d1")),
            tool_message("repo.read", read_result("b.py", "d2")),
        ]
        digest = build_run_digest(messages)
        assert digest.count("b.py") == 1
        assert digest.index("b.py") < digest.index("a.py")


def bulky_read(path: str, digest: str, size: int = 500) -> str:
    """符合实际的大型读取结果：主体是文件内容。"""
    return read_result(path, digest, content="c" * size)


class TestSummarizeDropped:
    def test_drops_oldest_tool_outputs_and_returns_the_digest_position(self) -> None:
        messages = [
            tool_message("repo.read", bulky_read("a.py", "d1"), "c1"),
            ModelMessage(role="assistant", content="thinking"),
            tool_message("repo.read", bulky_read("b.py", "d2"), "c2"),
            tool_message("repo.read", bulky_read("c.py", "d3"), "c3"),
        ]
        kept, digest, position = summarize_dropped(messages, limit=1400)

        assert position == 0
        assert "a.py" in digest
        assert len(kept) < len(messages)

    def test_the_two_newest_tool_outputs_are_kept(self) -> None:
        messages = [
            tool_message("repo.read", bulky_read(f"f{i}.py", "d"), f"c{i}") for i in range(5)
        ]
        kept, _, _ = summarize_dropped(messages, limit=1400)

        assert messages[-1] in kept
        assert messages[-2] in kept

    def test_assistant_and_user_messages_are_never_dropped(self) -> None:
        messages = [
            ModelMessage(role="assistant", content="my narrative"),
            ModelMessage(role="user", content="gate feedback"),
            *[tool_message("repo.read", bulky_read(f"f{i}.py", "d"), f"c{i}") for i in range(4)],
        ]
        kept, _, _ = summarize_dropped(messages, limit=1400)

        assert messages[0] in kept
        assert messages[1] in kept

    def test_dropped_content_does_not_survive_anywhere(self) -> None:
        """核心目标：字节被移除，事实保留下来。"""
        messages = [
            tool_message("repo.read", read_result("a.py", "d1", content="S" * 900), "c1"),
            tool_message("repo.read", bulky_read("b.py", "d2"), "c2"),
            tool_message("repo.read", bulky_read("c.py", "d3"), "c3"),
        ]
        kept, digest, _ = summarize_dropped(messages, limit=1400)

        assert all("S" * 900 not in message.content for message in kept)
        assert "S" * 900 not in digest
        assert "a.py" in digest

    def test_a_small_transcript_is_untouched(self) -> None:
        messages = [tool_message("repo.read", read_result("a.py", "d1"))]
        kept, digest, position = summarize_dropped(messages, limit=100_000)

        assert kept == messages
        assert digest == ""
        assert position == -1


def assistant_tool_call(call_id: str, name: str = "repo.read") -> ModelMessage:
    return ModelMessage(
        role="assistant",
        content="",
        tool_calls=(ToolCallProposal(call_id=call_id, tool_name=name, arguments_json="{}"),),
    )


def paired_tool(call_id: str, path: str) -> ModelMessage:
    return ModelMessage(
        role="tool",
        content=f'<tool_output tool="repo.read">\n{bulky_read(path, "d")}\n</tool_output>',
        tool_call_id=call_id,
    )


class TestToolCallPairing:
    """被丢弃的工具结果绝不能让保留的 assistant 工具调用变成孤立调用——OpenAI 和
    DeepSeek 会拒绝没有后续结果的 assistant tool_call。"""

    def _turn_groups(self, n: int) -> list[ModelMessage]:
        messages: list[ModelMessage] = []
        for i in range(n):
            messages.append(assistant_tool_call(f"c{i}"))
            messages.append(paired_tool(f"c{i}", f"f{i}.py"))
        return messages

    def test_a_dropped_result_takes_its_assistant_call_with_it(self) -> None:
        messages = self._turn_groups(5)
        kept, digest, _ = summarize_dropped(messages, limit=1400)

        kept_tool_ids = {m.tool_call_id for m in kept if m.role == "tool"}
        for message in kept:
            if message.role == "assistant":
                for call in message.tool_calls:
                    assert call.call_id in kept_tool_ids, (
                        "a kept assistant tool call lost its result to compaction"
                    )
        assert digest  # 有内容被压缩了

    def test_the_latest_turn_group_survives_whole(self) -> None:
        messages = self._turn_groups(5)
        kept, _, _ = summarize_dropped(messages, limit=1400)
        # 最后一条 assistant 工具调用及其结果都会保留
        assert messages[-1] in kept
        assert messages[-2] in kept


class TestSizing:
    def test_reasoning_and_tool_args_count_toward_the_budget(self) -> None:
        plain = ModelMessage(role="assistant", content="hello")
        with_reasoning = ModelMessage(
            role="assistant", content="hello", provider_reasoning="x" * 500
        )
        assert message_chars(with_reasoning) > message_chars(plain) + 400

    def test_a_transcript_over_budget_only_via_reasoning_is_compacted(self) -> None:
        """Reasoning 会在线协议上重放，因此必须能够触发压缩；只统计 content 会漏掉
        这种情况。"""
        messages = [
            assistant_tool_call("c0"),
            ModelMessage(
                role="tool",
                content='<tool_output tool="repo.read">\n'
                + read_result("a.py", "d")
                + "\n</tool_output>",
                tool_call_id="c0",
                provider_reasoning="r" * 2000,
            ),
            assistant_tool_call("c1"),
            paired_tool("c1", "b.py"),
            assistant_tool_call("c2"),
            paired_tool("c2", "c.py"),
        ]
        # content 很小，但 reasoning 会使其超过很小的预算
        kept, digest, _ = summarize_dropped(messages, limit=1500)
        assert digest


class TestHardLimit:
    """当 summarize_dropped 无法使内容适配时，保证适配的后备机制（剩余内容全部不
    可丢弃，或摘要本身很大）。"""

    def test_a_fitting_history_is_returned_unchanged(self) -> None:
        messages = [ModelMessage(role="user", content="small")]
        assert enforce_hard_limit(messages, 1000) is messages

    def test_oldest_messages_are_dropped_until_it_fits(self) -> None:
        messages = [ModelMessage(role="user", content=f"m{i}: " + "x" * 400) for i in range(5)]
        fitted = enforce_hard_limit(messages, 900)
        assert sum(message_chars(m) for m in fitted) <= 900
        # 保留最新内容，丢弃最旧内容。
        assert fitted[-1].content.startswith("m4:")
        assert not any(m.content.startswith("m0:") for m in fitted)

        # 即使它后面还有 assistant prose，最新用户意图也应保留并被有界截断。
        intent = ModelMessage(role="user", content="LATEST-INTENT " + "u" * 2000)
        narrative = ModelMessage(role="assistant", content="answer " + "a" * 2000)
        fitted = enforce_hard_limit([intent, narrative], 500)
        assert any("LATEST-INTENT" in message.content for message in fitted)
        assert sum(message_chars(message) for message in fitted) <= 500

    def test_a_single_oversized_message_is_truncated_in_place(self) -> None:
        messages = [ModelMessage(role="user", content="z" * 5000)]
        fitted = enforce_hard_limit(messages, 1000)
        assert len(fitted) == 1
        assert len(fitted[0].content) <= 1000
        assert "truncated to fit" in fitted[0].content

        reasoning_only = ModelMessage(
            role="assistant", content="done", provider_reasoning="r" * 5000
        )
        fitted = enforce_hard_limit([reasoning_only], 1000)
        assert sum(message_chars(message) for message in fitted) <= 1000

        call = assistant_tool_call("paired").model_copy(
            update={
                "tool_calls": (
                    ToolCallProposal(
                        call_id="paired",
                        tool_name="repo.read",
                        arguments_json='{"path":"' + "x" * 2000 + '"}',
                    ),
                )
            }
        )
        result = paired_tool("paired", "latest.py")
        fitted = enforce_hard_limit([call, result], 600)
        assert [message.role for message in fitted] == ["assistant", "tool"]
        assert fitted[0].tool_calls[0].call_id == fitted[1].tool_call_id
        assert sum(message_chars(message) for message in fitted) <= 600

    def test_never_returns_empty(self) -> None:
        messages = [ModelMessage(role="user", content="z" * 5000)]
        assert enforce_hard_limit(messages, 10)


class TestComprehensionPreservation:
    """压缩 A/B 基准的确定性替代测试：模型继续工作所需的每个关键事实（读取了哪些
    文件及其摘要、编辑了什么、运行了哪些检查及退出码）都必须保留在摘要中。实时
    任务表现的 A/B 仍是诚实的开放测量（见 EVAL_LIVE.md）；这里固定的是摘要不会
    悄悄丢失该基准要测试的事实。"""

    def test_digest_preserves_reads_edits_and_checks(self) -> None:
        dropped = [
            tool_message("repo.read", read_result("src/a.py", "aaaa1111", content="x" * 50_000)),
            tool_message(
                "repo.edit",
                '{"status":"ok","result":{"path":"src/a.py","postimage_digest":"bbbb2222"}}',
            ),
            tool_message(
                "repo.check",
                '{"status":"ok","result":{"recipe_id":"pytest","exit_code":1}}',
            ),
        ]
        digest = build_run_digest(dropped)
        # 恢复中的代理需要的事实全部存在。
        assert "src/a.py" in digest
        assert "aaaa1111"[:8] in digest  # read 摘要前缀
        assert "bbbb2222"[:8] in digest  # postimage 前缀
        assert "pytest exit 1" in digest
        # 这些字节绝不能保留（这正是摘要可信的原因）。
        assert "xxxx" not in digest

    def test_a_read_then_edit_of_one_file_keeps_both_facts(self) -> None:
        dropped = [
            tool_message("repo.read", read_result("m.py", "r0r0r0r0")),
            tool_message(
                "repo.edit",
                '{"status":"ok","result":{"path":"m.py","postimage_digest":"e1e1e1e1"}}',
            ),
        ]
        digest = build_run_digest(dropped)
        assert "read" in digest and "edited" in digest
        assert "m.py" in digest
