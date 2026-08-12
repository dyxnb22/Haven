"""Deterministic compaction: dropped tool outputs become recorded facts.

The digest is derived from the dropped messages themselves, never from live run
state, so it stays byte-identical between compaction events and cannot move the
cacheable prefix (ADR 0008).
"""

from haven.application.compaction import build_run_digest, message_chars, summarize_dropped
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
        """The digest is labelled trusted, so it may carry only program-made
        facts — never repository text, which would launder untrusted bytes."""
        secret = "AKIAIOSFODNN7EXAMPLE-in-a-file"
        digest = build_run_digest(
            [tool_message("repo.read", read_result("src/calc.py", "a1b2", content=secret))]
        )
        assert secret not in digest

    def test_model_prose_is_not_summarized(self) -> None:
        """Only tool outputs are ever dropped, so assistant text cannot appear."""
        messages = [ModelMessage(role="assistant", content="I think the bug is obvious")]
        assert "obvious" not in build_run_digest(messages)


class TestRobustness:
    def test_malformed_json_degrades_to_a_count(self) -> None:
        """A bad entry must never abort a run."""
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
    """A realistically large read result: the bulk of it is file content."""
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
        """The whole point: the bytes go away, the facts stay."""
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
    """A dropped tool result must never orphan a kept assistant tool call —
    OpenAI and DeepSeek reject an assistant tool_call with no following result.
    """

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
        assert digest  # something was condensed

    def test_the_latest_turn_group_survives_whole(self) -> None:
        messages = self._turn_groups(5)
        kept, _, _ = summarize_dropped(messages, limit=1400)
        # the last assistant tool call and its result are both kept
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
        """Reasoning is replayed on the wire, so it must be able to trigger
        compaction; counting content alone would miss it."""
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
        # content is small, but reasoning pushes it over a tiny budget
        kept, digest, _ = summarize_dropped(messages, limit=1500)
        assert digest
