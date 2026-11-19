"""TUI 展示层的信任边界：不可信文本不得进入启用标记的面板，缓冲不得无界增长。

这里钉住四件事，都属于"模型或仓库控制的字节影响用户所见"这一类：

1. `tool_name` 与 `selected_skills` 也必须过 sanitize——它们和模型输出一样不可信；
2. 时间线用 plain sanitize（它的 RichLog 禁用标记，转义只会显示成字面反斜杠）；
3. 时间线有条目上限（一个进程跑很多次运行，而 RunCreated 不重置它）；
4. 截断标记必须最后附加，否则会被自己转义。
"""

from __future__ import annotations

from textual.content import Content

from haven.contracts.events import EventEnvelope, RunCreated, ToolCompleted
from haven.interfaces.tui.presenter import (
    MAX_TIMELINE_ENTRIES,
    PresenterState,
    reduce,
    sanitize,
    sanitize_plain,
)


def wrap(event: object, seq: int = 1) -> EventEnvelope:
    return EventEnvelope(seq=seq, at="2026-01-01T00:00:00+00:00", event=event)  # type: ignore[arg-type]


def _started() -> PresenterState:
    return reduce(
        PresenterState(),
        wrap(
            RunCreated(
                run_id="run-1",
                goal="g",
                mode="interactive",
                agent_mode="agent",
                workspace="/tmp/ws",
                workspace_digest="d" * 64,
                git_branch="main",
                model_name="m",
                max_steps=10,
            )
        ),
    )


class TestToolNameIsUntrusted:
    """`tool_name` 由服务端原样透传（`openai_wire.from_wire.get(name, name)`），
    模型完全可以提议一个不存在的函数名，而 `ToolCallProposal.tool_name` 没有字符约束。
    它流到 `ToolCompleted`，再进 trace 面板与时间线——两处都是展示面。
    """

    def test_a_markup_tool_name_does_not_reach_the_trace_panel(self) -> None:
        state = reduce(
            _started(),
            wrap(
                ToolCompleted(
                    run_id="run-1",
                    call_id="c1",
                    tool_name="repo.read[link=https://evil.example/spoof]ok[/link]",
                    status="error",
                    error_code="unknown_tool",
                )
            ),
        )
        row = state.trace_rows[-1]
        # 原样保留会让 Content.from_markup 抛 MarkupError，直接打死 TUI 事件循环
        assert (
            "repo.read[link=https://evil.example/spoof]ok[/link]" in Content.from_markup(row).plain
        )

    def test_a_malformed_markup_tool_name_is_escaped(self) -> None:
        """孤立的 `[/i]` 是 Content.from_markup 明确会抛的那一类。"""
        state = reduce(
            _started(),
            wrap(
                ToolCompleted(
                    run_id="run-1",
                    call_id="c1",
                    tool_name="repo.read[/i]x",
                    status="error",
                    error_code="unknown_tool",
                )
            ),
        )
        row = state.trace_rows[-1]
        assert "repo.read[/i]x" in Content.from_markup(row).plain

    def test_control_bytes_never_reach_the_timeline(self) -> None:
        state = reduce(
            _started(),
            wrap(
                ToolCompleted(
                    run_id="run-1",
                    call_id="c1",
                    tool_name="repo.read\x1b[2J\x1b[H\x07",
                    status="ok",
                )
            ),
        )
        entry = state.timeline[-1]
        assert "\x1b" not in entry.text
        assert "\x07" not in entry.text


class TestTimelineUsesPlainSanitize:
    """时间线是 `markup=False` 的 RichLog：转义 Rich 标记只会以字面反斜杠显示。"""

    def test_a_bracketed_goal_shows_without_stray_backslashes(self) -> None:
        state = reduce(
            PresenterState(),
            wrap(
                RunCreated(
                    run_id="run-1",
                    goal="fix [bug] in [red]calc[/red] now",
                    mode="interactive",
                    agent_mode="agent",
                    workspace="/tmp/ws",
                    workspace_digest="d" * 64,
                    git_branch="main",
                    model_name="m",
                    max_steps=10,
                )
            ),
        )
        assert "fix [bug] in [red]calc[/red] now" in state.timeline[-1].text
        assert "\\[" not in state.timeline[-1].text

    def test_control_bytes_are_still_stripped(self) -> None:
        assert sanitize_plain("a\x1b[31mb\x07c") == "a[31mbc"

    def test_the_panel_variant_still_escapes(self) -> None:
        assert sanitize("fix [bug]") == "fix \\[bug]"


class TestHeaderSanitizesSkillsAndWorkspace:
    """`/skill use` 的原文不过滤就进标题栏，而标题栏每个事件都刷新一次。"""

    def test_a_markup_skill_name_cannot_kill_the_header(self) -> None:
        state = PresenterState(
            workspace="/tmp/ws",
            branch="main",
            model_name="m",
            mode="interactive",
            approval_mode="ask",
            selected_skills=("a[link=https://evil.example]b",),
        )
        header = state.header_line()
        # 不抛即通过；且技能名以字面文本呈现，而不是被 Rich 吞掉
        assert "a[link=https://evil.example]b" in Content.from_markup(header).plain

    def test_the_approval_mode_indicator_is_not_swallowed_as_markup(self) -> None:
        """`[ask]` 原先会被 Rich 当成样式标记吃掉，用户在标题栏看不到审批模式。"""
        state = PresenterState(
            workspace="/tmp/ws",
            branch="main",
            model_name="m",
            mode="interactive",
            approval_mode="ask",
        )
        header = state.header_line()
        assert "[ask]" in Content.from_markup(header).plain
        Content.from_markup(header)

    def test_skill_names_still_render(self) -> None:
        state = PresenterState(
            workspace="/tmp/ws",
            branch="main",
            model_name="m",
            mode="interactive",
            approval_mode="ask",
            selected_skills=("tdd", "review"),
        )
        # 断言渲染后的纯文本：header 是给启用标记的 Static 的，带转义是正确形态
        assert "skills: [tdd, review]" in Content.from_markup(state.header_line()).plain


class TestTimelineIsBounded:
    """一个进程跑很多次运行，而 RunCreated 不重置时间线。"""

    def test_the_timeline_does_not_grow_without_bound(self) -> None:
        state = PresenterState()
        for index in range(MAX_TIMELINE_ENTRIES + 200):
            state = reduce(
                state,
                wrap(
                    ToolCompleted(
                        run_id="run-1",
                        call_id=f"c{index}",
                        tool_name="repo.read",
                        status="ok",
                    ),
                    seq=index + 1,
                ),
            )
        assert len(state.timeline) == MAX_TIMELINE_ENTRIES
        # 保留的是最新的那些
        assert state.timeline[-1].text.endswith("(0ms)")

    def test_other_buffers_keep_their_own_bounds(self) -> None:
        """没有把别处的边界顺手改掉。"""
        state = _started()
        for index in range(80):
            state = reduce(
                state,
                wrap(
                    ToolCompleted(
                        run_id="run-1",
                        call_id=f"c{index}",
                        tool_name="repo.read",
                        status="ok",
                        process=None,
                    ),
                    seq=index + 2,
                ),
            )
        assert len(state.evidence_rows) <= 50


class TestTruncationMarkerSurvivesEscaping:
    def test_the_marker_is_not_escaped(self) -> None:
        assert sanitize("x" * 5000, limit=100).endswith("…[truncated]")
        assert sanitize_plain("x" * 5000, limit=100).endswith("…[truncated]")

    def test_short_text_is_unchanged(self) -> None:
        assert sanitize("short") == "short"
        assert sanitize_plain("short") == "short"
