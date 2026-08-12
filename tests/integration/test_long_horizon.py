"""A run whose transcript outgrows the context budget must keep its thread.

The unit tests prove compaction is correct in isolation; this proves it happens
in a real run and that the agent still reaches evidence afterwards.
"""

import sys
from pathlib import Path

from haven.contracts.events import ContextBuilt
from haven.contracts.tools import RecipeSpec
from haven.domain.enums import RunStatus, StopReason
from tests.integration.harness import Harness, finish, make_repo, text, tool

#: Large enough that three reads exceed MAX_CONTEXT_CHARS (96k).
WIDE_LINES = 220


def wide_recipes() -> dict[str, RecipeSpec]:
    return {
        "verify-wide": RecipeSpec(
            id="verify-wide", argv=(sys.executable, "verify_wide.py"), timeout_seconds=30
        )
    }


def make_wide_repo(tmp_path: Path) -> Path:
    repo = make_repo(tmp_path)
    body = ['"""A long module."""\n']
    for i in range(WIDE_LINES):
        body.append(
            f"\ndef helper_{i:03d}(value: int) -> int:\n"
            f'    """Return value scaled by {i}."""\n'
            f"    scaled = value * {i}\n"
            f"    adjusted = scaled + {i % 7}\n"
            f"    return adjusted\n"
        )
    body.append("\n\ndef add(a, b):\n    return a - b  # BUG: should be +\n")
    (repo / "src" / "wide.py").write_text("".join(body))
    (repo / "verify_wide.py").write_text(
        "import sys\n"
        "sys.path.insert(0, 'src')\n"
        "from wide import add\n"
        "sys.exit(0 if add(2, 3) == 5 else 1)\n"
    )
    return repo


def context_segments(h: Harness) -> list[str]:
    sources: list[str] = []
    for event in h.sink.events_of("context.built"):
        if isinstance(event, ContextBuilt):
            sources.extend(segment.source for segment in event.segments)
    return sources


class TestCompactionInARealRun:
    async def test_a_compacted_run_still_reaches_evidence(self, tmp_path: Path) -> None:
        repo = make_wide_repo(tmp_path)
        turns = [
            [
                tool("c1", "repo.read", path="src/wide.py", start_line=1, max_lines=2000),
                finish("tool_calls"),
            ],
            [
                tool("c2", "repo.read", path="src/wide.py", start_line=2, max_lines=2000),
                finish("tool_calls"),
            ],
            [
                tool("c3", "repo.read", path="src/wide.py", start_line=3, max_lines=2000),
                finish("tool_calls"),
            ],
            [
                tool(
                    "c4",
                    "repo.edit",
                    path="src/wide.py",
                    old_string="return a - b  # BUG: should be +",
                    new_string="return a + b",
                    summary="fix add()",
                ),
                finish("tool_calls"),
            ],
            [tool("c5", "repo.diff"), finish("tool_calls")],
            [tool("c6", "repo.check", recipe_id="verify-wide"), finish("tool_calls")],
            [text("Fixed add(); diff and verify-wide recorded."), finish()],
        ]
        h = Harness(repo, turns, recipes=wide_recipes())
        outcome = await h.service.run("Fix add() in the large module")

        assert outcome.status is RunStatus.SUCCEEDED
        assert outcome.stop_reason is StopReason.EVIDENCE_SATISFIED

    async def test_the_run_really_was_compacted(self, tmp_path: Path) -> None:
        """Without this the case above could silently stop exercising
        compaction if the budget or the fixture size ever changed."""
        repo = make_wide_repo(tmp_path)
        turns = [
            [
                tool("c1", "repo.read", path="src/wide.py", start_line=1, max_lines=2000),
                finish("tool_calls"),
            ],
            [
                tool("c2", "repo.read", path="src/wide.py", start_line=2, max_lines=2000),
                finish("tool_calls"),
            ],
            [
                tool("c3", "repo.read", path="src/wide.py", start_line=3, max_lines=2000),
                finish("tool_calls"),
            ],
            [text("Read the module."), finish()],
        ]
        h = Harness(repo, turns)
        await h.service.run("Read the large module")

        assert "run_digest" in context_segments(h)

    async def test_the_digest_is_reported_as_trusted(self, tmp_path: Path) -> None:
        repo = make_wide_repo(tmp_path)
        turns = [
            [
                tool("c1", "repo.read", path="src/wide.py", start_line=1, max_lines=2000),
                finish("tool_calls"),
            ],
            [
                tool("c2", "repo.read", path="src/wide.py", start_line=2, max_lines=2000),
                finish("tool_calls"),
            ],
            [
                tool("c3", "repo.read", path="src/wide.py", start_line=3, max_lines=2000),
                finish("tool_calls"),
            ],
            [text("Read the module."), finish()],
        ]
        h = Harness(repo, turns)
        await h.service.run("Read the large module")

        for event in h.sink.events_of("context.built"):
            if not isinstance(event, ContextBuilt):
                continue
            for segment in event.segments:
                if segment.source == "run_digest":
                    assert segment.trust == "trusted"
                    return
        raise AssertionError("no run_digest segment was recorded")
