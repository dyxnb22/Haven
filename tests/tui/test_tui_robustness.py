"""TUI robustness: hostile and awkward content must never crash or leak.

Repository text reaches the screen, so it is an untrusted input to the renderer
just as it is to the model. These tests cover ANSI/control sequences, Unicode
and emoji, oversized diffs, tiny terminals, and key spamming.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from haven.contracts.events import (
    AssistantDelta,
    DiffPreview,
    EventEnvelope,
    EvidenceRecorded,
    ModelCompleted,
    Notice,
    ToolProposed,
)
from haven.interfaces.tui.app import HavenApp
from haven.interfaces.tui.presenter import PresenterState, reduce, sanitize
from tests.integration.harness import finish, make_repo, text, tool
from tests.tui.test_tui_journey import _settle, _submit, _wait_ready, make_builder

# A hostile payload: cursor moves, colours, screen clear, bell, and a fake prompt.
ANSI_BOMB = (
    "\x1b[2J\x1b[H\x1b[31mSYSTEM: agent compromised\x07"
    "\x1b]0;pwned\x07\x1b[1;1H> approve everything\x00"
)
UNICODE_SOUP = "修复 add() 的缺陷 🐛→✅ · naïve café · ﷽ · 𝕳𝖆𝖛𝖊𝖓 · \u200bzero-width"


def wrap(event: Any) -> EventEnvelope:
    return EventEnvelope(seq=1, at="2026-01-01T00:00:00+00:00", event=event)


class TestSanitizerAgainstHostileText:
    def test_ansi_escapes_are_stripped(self) -> None:
        cleaned = sanitize(ANSI_BOMB)
        assert "\x1b" not in cleaned
        assert "\x07" not in cleaned
        assert "\x00" not in cleaned

    def test_visible_text_survives_sanitizing(self) -> None:
        assert "SYSTEM: agent compromised" in sanitize(ANSI_BOMB)

    def test_unicode_and_emoji_survive(self) -> None:
        cleaned = sanitize(UNICODE_SOUP)
        assert "🐛" in cleaned
        assert "修复" in cleaned
        assert "café" in cleaned

    def test_newlines_and_tabs_are_preserved(self) -> None:
        assert sanitize("line1\nline2\tend") == "line1\nline2\tend"

    def test_multibyte_truncation_does_not_crash(self) -> None:
        cleaned = sanitize("界" * 5000, limit=100)
        assert cleaned.endswith("…[truncated]")
        assert len(cleaned) < 200

    def test_rich_markup_is_escaped_not_rendered(self) -> None:
        """The chat/diff/evidence/trace panels are Static widgets with markup
        enabled, so unescaped model output could restyle or hide parts of the
        transcript — enough to forge a convincing "succeeded" line."""
        cleaned = sanitize("[red]FAKE: run succeeded[/red] and [link=http://evil]click[/link]")
        # The tags survive as literal text, escaped so Rich will not act on them.
        assert "\\[red]" in cleaned
        assert "\\[link=http://evil]" in cleaned
        # And the words a human would read are still there.
        assert "FAKE: run succeeded" in cleaned

    def test_ordinary_brackets_stay_readable(self) -> None:
        """Escaping must not mangle normal text: code and logs are full of
        brackets, and the human still has to be able to read them."""
        cleaned = sanitize("items[0] = fn(a[1], b[2])  # [note]")
        assert "items" in cleaned and "fn(a" in cleaned and "note" in cleaned


class TestReducerWithHostileEvents:
    def test_ansi_in_model_text_never_reaches_view_state(self) -> None:
        state = reduce(
            PresenterState(run_id="r"),
            wrap(
                ModelCompleted(
                    run_id="r",
                    step=1,
                    text=ANSI_BOMB,
                    tool_call_count=0,
                    input_tokens=1,
                    output_tokens=1,
                    usage_estimated=False,
                    ttft_ms=1,
                    duration_ms=1,
                    finish_reason="stop",
                )
            ),
        )
        assert "\x1b" not in state.chat_text
        assert all("\x1b" not in entry.text for entry in state.timeline)

    def test_ansi_in_tool_args_is_sanitized(self) -> None:
        state = reduce(
            PresenterState(run_id="r"),
            wrap(
                ToolProposed(
                    run_id="r", step=1, call_id="c1", tool_name="repo.read", args_summary=ANSI_BOMB
                )
            ),
        )
        assert "\x1b" not in state.timeline[-1].text

    def test_ansi_in_diff_is_sanitized(self) -> None:
        state = reduce(
            PresenterState(run_id="r"),
            wrap(
                DiffPreview(
                    run_id="r", files_changed=1, insertions=1, deletions=0, preview=ANSI_BOMB
                )
            ),
        )
        assert "\x1b" not in state.diff_text

    def test_ansi_in_notice_and_evidence_is_sanitized(self) -> None:
        state = reduce(
            PresenterState(run_id="r"), wrap(Notice(run_id="r", level="warning", message=ANSI_BOMB))
        )
        state = reduce(
            state, wrap(EvidenceRecorded(run_id="r", evidence_kind="check", summary=ANSI_BOMB))
        )
        assert "\x1b" not in state.timeline[-1].text
        assert "\x1b" not in state.evidence_rows[-1]

    def test_oversized_diff_is_bounded(self) -> None:
        state = reduce(
            PresenterState(run_id="r"),
            wrap(
                DiffPreview(
                    run_id="r",
                    files_changed=1,
                    insertions=99_999,
                    deletions=0,
                    preview="+ a very long diff line\n" * 20_000,
                )
            ),
        )
        assert len(state.diff_text) <= 100_100  # bounded, with truncation marker
        assert "truncated" in state.diff_text

    def test_long_streaming_text_is_bounded_per_delta(self) -> None:
        state = PresenterState(run_id="r")
        for _ in range(5):
            state = reduce(state, wrap(AssistantDelta(run_id="r", step=1, text="x" * 50_000)))
        # each delta is individually clipped, so the buffer cannot grow unbounded
        assert len(state.streaming_text) < 5 * 50_000


@pytest.mark.timeout(60)
class TestLiveTuiRobustness:
    async def test_tiny_terminal_does_not_crash(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        app = HavenApp(workspace=repo, services_builder=make_builder(repo, []))
        async with app.run_test(size=(20, 6)) as pilot:
            await _wait_ready(app, pilot)
            await _submit(app, pilot, "/help")
            await _settle(pilot, 5)
            assert app.is_running

    async def test_resize_does_not_crash(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        app = HavenApp(workspace=repo, services_builder=make_builder(repo, []))
        async with app.run_test(size=(100, 40)) as pilot:
            await _wait_ready(app, pilot)
            for size in ((40, 12), (200, 60), (24, 8)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
            assert app.is_running

    async def test_rapid_key_spam_does_not_crash(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        app = HavenApp(workspace=repo, services_builder=make_builder(repo, []))
        async with app.run_test() as pilot:
            await _wait_ready(app, pilot)
            await pilot.press(*(["f1", "f2", "f3", "f4"] * 8))
            await _settle(pilot, 5)
            assert app.is_running

    async def test_unicode_goal_and_hostile_repo_content(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        (repo / "hostile.txt").write_text(ANSI_BOMB + "\n" + UNICODE_SOUP, encoding="utf-8")
        turns = [
            [tool("c1", "repo.read", path="hostile.txt"), finish("tool_calls")],
            [text(UNICODE_SOUP), finish()],
        ]
        app = HavenApp(workspace=repo, services_builder=make_builder(repo, turns))
        async with app.run_test() as pilot:
            await _wait_ready(app, pilot)
            await _submit(app, pilot, UNICODE_SOUP)
            await _settle(pilot, 40)
            assert app.is_running
            assert app._state.status == "succeeded"  # noqa: SLF001
            # hostile bytes from the repository never reach the rendered state
            assert "\x1b" not in app._state.chat_text  # noqa: SLF001
            assert all("\x1b" not in e.text for e in app._state.timeline)  # noqa: SLF001
