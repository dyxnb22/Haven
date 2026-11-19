"""ADR 0036 观测折叠与多层压缩单元测试。"""

import json

from haven.application.compaction import (
    DIGEST_HEADER,
    enforce_hard_limit,
    fold_text,
    mask_bulky_observations,
    summarize_dropped,
)
from haven.contracts.model import ModelMessage, ToolCallProposal


def bulky_tool_output(path: str, content_size: int = 20_000, call_id: str = "c1") -> ModelMessage:
    body = {
        "status": "ok",
        "result": {
            "path": path,
            "digest": "abc12345",
            "content": "\n".join(f"line {i}: " + ("x" * 60) for i in range(content_size // 70)),
        },
    }
    return ModelMessage(
        role="tool",
        content=f'<tool_output tool="repo.read">\n{json.dumps(body)}\n</tool_output>',
        tool_call_id=call_id,
    )


def assistant_tool_call(call_id: str, tool_name: str, args: dict[str, object]) -> ModelMessage:
    return ModelMessage(
        role="assistant",
        content="calling tool",
        tool_calls=(
            ToolCallProposal(
                call_id=call_id,
                tool_name=tool_name,
                arguments_json=json.dumps(args),
            ),
        ),
    )


class TestFoldText:
    def test_short_text_remains_unchanged(self) -> None:
        text = "short text here"
        folded, did_fold = fold_text(text, threshold_chars=100)
        assert not did_fold
        assert folded == text

    def test_multiline_text_folds_middle(self) -> None:
        lines = [f"line {i}" for i in range(50)]
        text = "\n".join(lines)
        folded, did_fold = fold_text(text, head_lines=3, tail_lines=3, threshold_chars=50)
        assert did_fold
        assert folded.startswith("line 0\nline 1\nline 2")
        assert folded.endswith("line 47\nline 48\nline 49")
        assert "lines folded" in folded

    def test_dense_single_line_text_folds_chars(self) -> None:
        text = "x" * 1000
        folded, did_fold = fold_text(text, threshold_chars=200)
        assert did_fold
        assert "chars folded" in folded
        assert len(folded) < 300

    def test_folding_never_makes_the_text_longer(self) -> None:
        """折叠的契约是"变短"。

        此前这里不检查：一段刚过阈值的密集文本（401 字符、只有一行）会被两个半截加
        一句标记撑成 426 字符，比原文还长。两个调用方各自比较了 saved，所以线上看不
        出问题——但这条界线该由 `fold_text` 自己把住。
        """
        for length in range(401, 900):
            text = "x" * length
            folded, did_fold = fold_text(text)
            assert did_fold
            assert len(folded) < length, f"{length} -> {len(folded)}"

    def test_the_degenerate_case_still_folds_and_keeps_content(self) -> None:
        folded, did_fold = fold_text("x" * 401)
        assert did_fold
        assert len(folded) < 401
        assert folded  # 不能折成空


class TestMaskBulkyObservations:
    def test_bulky_tool_observation_folded(self) -> None:
        msg = bulky_tool_output("big.py", content_size=5_000)
        masked_list, saved = mask_bulky_observations([msg], keep_recent_units=0)
        assert saved > 0
        assert len(masked_list) == 1
        assert "lines folded" in masked_list[0].content
        # Structure is valid json
        raw = masked_list[0].content
        start = raw.find(">")
        end = raw.rfind("</tool_output>")
        data = json.loads(raw[start + 1 : end].strip())
        assert data["result"]["path"] == "big.py"

    def test_protected_recent_units_are_not_folded(self) -> None:
        msg1 = bulky_tool_output("first.py", content_size=5_000, call_id="c1")
        msg2 = bulky_tool_output("second.py", content_size=5_000, call_id="c2")
        # keep_recent_units=1 should keep msg2 intact and fold msg1
        masked_list, saved = mask_bulky_observations([msg1, msg2], keep_recent_units=1)
        assert "lines folded" in masked_list[0].content
        assert "lines folded" not in masked_list[1].content


class TestMultiTierCompaction:
    def test_tier1_observation_masking_avoids_dropping(self) -> None:
        """当体积过大但可通过折叠适应预算时，保留因果调用链而不丢弃单元。"""
        call1 = assistant_tool_call("c1", "repo.read", {"path": "heavy.py"})
        res1 = bulky_tool_output("heavy.py", content_size=25_000, call_id="c1")
        call2 = assistant_tool_call("c2", "repo.read", {"path": "mid.py"})
        payload_ok = json.dumps({"status": "ok", "result": {"content": "ok"}})
        res2 = ModelMessage(
            role="tool",
            content=f'<tool_output tool="repo.read">{payload_ok}</tool_output>',
            tool_call_id="c2",
        )
        call3 = assistant_tool_call("c3", "repo.read", {"path": "recent.py"})
        payload_fn = json.dumps({"status": "ok", "result": {"content": "def hello(): pass"}})
        res3 = ModelMessage(
            role="tool",
            content=f'<tool_output tool="repo.read">{payload_fn}</tool_output>',
            tool_call_id="c3",
        )
        messages = [call1, res1, call2, res2, call3, res3]

        # Budget is 10,000 chars. Total is ~26,000 chars.
        # Unit 1 and 2 are protected recent tools. Unit 0 is folded from 25k to ~400 chars.
        kept, digest, pos = summarize_dropped(messages, limit=10_000, mask_observations=True)
        assert digest == ""  # No dropping occurred!
        assert pos == -1
        assert len(kept) == 6  # All 6 turns intact!
        assert kept[0].role == "assistant"
        assert kept[1].role == "tool"
        assert "lines folded" in kept[1].content

    def test_tier2_drops_into_digest_when_masking_insufficient(self) -> None:
        """即使折叠后仍超出预算，降级到 Tier 2 生成确定性 run_digest。"""
        # Many messages exceeding even folded limit
        units = []
        for i in range(10):
            call = assistant_tool_call(f"c{i}", "repo.read", {"path": f"f{i}.py"})
            res = bulky_tool_output(f"f{i}.py", content_size=10_000, call_id=f"c{i}")
            units.extend([call, res])

        # Extremely tight limit: 2000 chars
        kept, digest, pos = summarize_dropped(units, limit=2000, mask_observations=True)
        assert digest != ""
        assert DIGEST_HEADER in digest
        assert pos >= 0
        assert len(kept) < len(units)

    def test_prefix_stability_across_turns_with_masking(self) -> None:
        """折叠只在超预算时对最旧单元生效，后续追加轮次若在预算内绝不触动已折叠前缀。"""
        call1 = assistant_tool_call("c1", "repo.read", {"path": "f1.py"})
        res1 = bulky_tool_output("f1.py", content_size=25_000, call_id="c1")
        call2 = assistant_tool_call("c2", "repo.read", {"path": "f2.py"})
        payload_ok2 = json.dumps({"status": "ok", "result": {"content": "ok2"}})
        res2 = ModelMessage(
            role="tool",
            content=f'<tool_output tool="repo.read">{payload_ok2}</tool_output>',
            tool_call_id="c2",
        )
        call3 = assistant_tool_call("c3", "repo.read", {"path": "f3.py"})
        payload_ok3 = json.dumps({"status": "ok", "result": {"content": "ok3"}})
        res3 = ModelMessage(
            role="tool",
            content=f'<tool_output tool="repo.read">{payload_ok3}</tool_output>',
            tool_call_id="c3",
        )
        turn_n = [call1, res1, call2, res2, call3, res3]

        kept_n, _, _ = summarize_dropped(turn_n, limit=10_000, mask_observations=True)
        assert "lines folded" in kept_n[1].content
        prefix_n = [m.content for m in kept_n]

        # In turn N+1, append a new short turn
        turn_n1 = [
            *turn_n,
            ModelMessage(role="assistant", content="next step thinking"),
        ]
        kept_n1, _, _ = summarize_dropped(turn_n1, limit=10_000, mask_observations=True)
        prefix_n1 = [m.content for m in kept_n1]

        # prefix of turn N must exactly match prefix of turn N+1
        assert prefix_n1[: len(prefix_n)] == prefix_n


class TestEnforceHardLimitPriority:
    def test_evicts_non_user_units_before_user_units(self) -> None:
        """在硬限制挤压下，优先淘汰旧的 assistant 叙述单元，保留用户消息与门禁反馈。"""
        msg_user1 = ModelMessage(role="user", content="user instructions 1: " + "u" * 500)
        msg_asst1 = ModelMessage(role="assistant", content="assistant narrative 1: " + "a" * 800)
        msg_user2 = ModelMessage(role="user", content="gate feedback 2: " + "g" * 500)
        msg_asst2 = ModelMessage(role="assistant", content="assistant narrative 2: " + "a" * 200)

        messages = [msg_user1, msg_asst1, msg_user2, msg_asst2]
        # Total is ~2000 chars. Set limit to 1400.
        # msg_asst1 (800 chars) should be evicted before msg_user1 (500 chars).
        fitted = enforce_hard_limit(messages, limit=1400)
        contents = [m.content for m in fitted]
        assert any("user instructions 1" in c for c in contents)
        assert any("gate feedback 2" in c for c in contents)
        assert not any("assistant narrative 1" in c for c in contents)
