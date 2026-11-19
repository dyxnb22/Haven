"""导出渲染，包括秘密脱敏。"""

import json

import pytest

from haven.contracts.events import (
    DiffPreview,
    EventEnvelope,
    ModelCompleted,
    Notice,
    RunFinished,
    ToolProposed,
)
from haven.domain.enums import RunStatus
from haven.interfaces.export import render_jsonl, render_markdown
from haven.ports.session import RunRecord


def run_record() -> RunRecord:
    return RunRecord(
        run_id="run-1",
        workspace="/tmp/ws",
        workspace_digest="d",
        goal="fix the bug",
        mode="interactive",
        status=RunStatus.SUCCEEDED,
        stop_reason="evidence_satisfied",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:01:00+00:00",
    )


def envelopes() -> list[EventEnvelope]:
    def wrap(seq: int, event: object) -> EventEnvelope:
        return EventEnvelope(seq=seq, at="2026-01-01T00:00:00+00:00", event=event)  # type: ignore[arg-type]

    return [
        wrap(
            1,
            ModelCompleted(
                run_id="run-1",
                step=1,
                text="Working on it",
                tool_call_count=1,
                input_tokens=10,
                output_tokens=5,
                usage_estimated=False,
                ttft_ms=12,
                duration_ms=100,
                finish_reason="tool_calls",
            ),
        ),
        wrap(
            2,
            ToolProposed(
                run_id="run-1",
                step=1,
                call_id="c1",
                tool_name="repo.edit",
                args_summary='{"path":"a.py"}',
            ),
        ),
        wrap(
            3,
            DiffPreview(
                run_id="run-1",
                files_changed=1,
                insertions=1,
                deletions=1,
                preview="--- a/a.py\n+++ b/a.py\n",
            ),
        ),
        wrap(
            4,
            RunFinished(
                run_id="run-1",
                status="succeeded",
                stop_reason="evidence_satisfied",
                gate_reason="evidence_satisfied",
                steps=3,
                tool_calls=1,
                input_tokens=10,
                output_tokens=5,
                cost_usd=0.0012,
            ),
        ),
    ]


def test_markdown_contains_key_sections() -> None:
    md = render_markdown(run_record(), envelopes())
    assert "# Haven run report: run-1" in md
    assert "fix the bug" in md
    assert "```diff" in md
    assert "## Outcome" in md
    assert "succeeded" in md

    injected = EventEnvelope(
        seq=5,
        at="2026-01-01T00:00:00+00:00",
        event=DiffPreview(
            run_id="run-1",
            files_changed=1,
            insertions=1,
            deletions=0,
            preview="+literal Markdown fence: ```\n",
        ),
    )
    fenced = render_markdown(run_record(), [injected])
    assert "````diff" in fenced
    assert fenced.rstrip().endswith("````")

    unknown_cost = EventEnvelope(
        seq=6,
        at="2026-01-01T00:00:00+00:00",
        event=RunFinished(
            run_id="run-1",
            status="succeeded",
            stop_reason="final_answer",
            cost_usd=0.0,
            cost_known=False,
        ),
    )
    outcome = render_markdown(run_record(), [unknown_cost])
    assert "cost: unknown" in outcome
    assert "$0.0000" not in outcome


def test_jsonl_is_one_event_per_line() -> None:
    out = render_jsonl(envelopes())
    lines = [line for line in out.splitlines() if line]
    assert len(lines) == 4
    for line in lines:
        json.loads(line)  # 每行都是有效 JSON


def test_markdown_redacts_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAVEN_API_KEY", "sk-super-secret-value")
    env = EventEnvelope(
        seq=1,
        at="2026-01-01T00:00:00+00:00",
        event=ModelCompleted(
            run_id="run-1",
            step=1,
            text="leaking sk-super-secret-value oops",
            tool_call_count=0,
            input_tokens=1,
            output_tokens=1,
            usage_estimated=False,
            ttft_ms=1,
            duration_ms=1,
            finish_reason="stop",
        ),
    )
    md = render_markdown(run_record(), [env])
    assert "sk-super-secret-value" not in md
    assert "[redacted]" in md


def test_redacts_any_provider_key_by_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """提供商特有的变量名也必须被覆盖，而不依赖硬编码列表。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-secret-value")
    env = EventEnvelope(
        seq=1,
        at="2026-01-01T00:00:00+00:00",
        event=ToolProposed(
            run_id="run-1",
            step=1,
            call_id="c1",
            tool_name="repo.read",
            args_summary="sk-deepseek-secret-value",
        ),
    )
    assert "sk-deepseek-secret-value" not in render_jsonl([env])
    assert "sk-deepseek-secret-value" not in render_markdown(run_record(), [env])


def test_short_env_values_are_not_treated_as_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """避免因为某个 *_KEY 变量保存了 'on' 而破坏报告。"""
    monkeypatch.setenv("SORT_KEY", "id")
    env = EventEnvelope(
        seq=1,
        at="2026-01-01T00:00:00+00:00",
        event=ToolProposed(
            run_id="run-1", step=1, call_id="c1", tool_name="repo.read", args_summary="id column"
        ),
    )
    assert "id column" in render_jsonl([env])


def test_foreign_credentials_are_masked_by_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境扫描只能知道当前进程持有的秘密。粘贴到目标中的密钥，或工具从文件读取
    的密钥，对它不可见——因此也要按形状遮盖常见凭据。"""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    leaks = (
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIAIOSFODNN7EXAMPLE",
        "sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
        "xoxb-1234567890-abcdefghij",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
    )
    for leak in leaks:
        env = EventEnvelope(
            seq=1,
            at="2026-01-01T00:00:00+00:00",
            event=ToolProposed(
                run_id="run-1", step=1, call_id="c1", tool_name="repo.read", args_summary=leak
            ),
        )
        assert leak not in render_jsonl([env]), leak
        assert leak not in render_markdown(run_record(), [env]), leak

    private_key = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "THIS-IS-THE-PRIVATE-MATERIAL\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    env = EventEnvelope(
        seq=2,
        at="2026-01-01T00:00:00+00:00",
        event=ToolProposed(
            run_id="run-1",
            step=1,
            call_id="c2",
            tool_name="repo.read",
            args_summary=private_key,
        ),
    )
    assert "PRIVATE-MATERIAL" not in render_jsonl([env])
    assert "PRIVATE-MATERIAL" not in render_markdown(run_record(), [env])


def test_ordinary_report_content_is_not_mangled(monkeypatch: pytest.MonkeyPatch) -> None:
    """会吞掉摘要和路径的脱敏器会使报告不可信，因此模式不得匹配每份报告都包含的
    普通内容。"""
    keep = (
        "sha256:3f58fbff3344719cad673dd17b497a3673fa28b3",
        "src/haven/adapters/workspace_fs.py",
        "run-140663c9e45c",
        "exit=0 in 143ms",
    )
    for value in keep:
        env = EventEnvelope(
            seq=1,
            at="2026-01-01T00:00:00+00:00",
            event=ToolProposed(
                run_id="run-1", step=1, call_id="c1", tool_name="repo.read", args_summary=value
            ),
        )
        assert value in render_jsonl([env]), value


def test_jsonl_redacts_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-leak-me-please")
    env = EventEnvelope(
        seq=1,
        at="2026-01-01T00:00:00+00:00",
        event=ToolProposed(
            run_id="run-1",
            step=1,
            call_id="c1",
            tool_name="repo.read",
            args_summary="sk-leak-me-please",
        ),
    )
    out = render_jsonl([env])
    assert "sk-leak-me-please" not in out


def test_jsonl_redacts_a_secret_that_json_escaping_would_hide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """含引号/反斜杠的秘密不能在 JSONL 里以明文出现。

    `_redact` 原先作用在 `model_dump_json()` **之后**，而 JSON 会把 `"` 和 `\\` 转义掉，
    子串匹配于是失效；`render_markdown` 脱敏的是原文、能成功。两条导出路径给出不同
    结果——"拿 Markdown 报告验证过脱敏"的用户会在 JSONL 里拿到明文凭据。
    """
    secret = 'p@ss"word\\1234567890'
    monkeypatch.setenv("HAVEN_TEST_PASSWORD", secret)
    env = EventEnvelope(
        seq=1,
        at="2026-01-01T00:00:00+00:00",
        event=Notice(run_id="run-1", level="warning", message=f"using {secret} now"),
    )

    jsonl = render_jsonl([env])
    assert secret not in jsonl, "JSONL 里出现了明文凭据"
    assert "[redacted]" in jsonl
    # 每条仍必须是有效 JSON，且格式没有被改动
    for line in jsonl.splitlines():
        if line:
            assert json.loads(line)["seq"] == 1


def test_jsonl_output_is_byte_identical_to_before_for_clean_events() -> None:
    """脱敏改的是"什么时候做"，不是导出格式。"""
    env = EventEnvelope(
        seq=7,
        at="2026-01-01T00:00:00+00:00",
        event=Notice(run_id="run-1", level="info", message='plain "text" with \\ and 中文'),
    )
    assert render_jsonl([env]) == env.model_dump_json() + "\n"
