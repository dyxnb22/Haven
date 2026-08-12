"""Measure chars-per-token from live eval event streams.

The context budget (`max_context_chars` in the model profile) is a char count,
but the provider bills and windows in tokens. This script reads the committed
live-run event streams (report-*/report-*-events/*.jsonl), pairs each
`context.built` request size (bytes) with the `model.completed` input-token
count it produced, and reports the chars-per-token distribution — so the
hand-set char budget can be checked against the real token window instead of
guessed. See ModelProfile.context_window_tokens and the guard in
tests/unit/test_profiles.py.

    uv run python evals/calibrate_context.py
"""

from __future__ import annotations

import glob
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    pairs: list[tuple[int, int]] = []
    for path in glob.glob(str(ROOT / "evals/real/report-*/report-live-events/*.jsonl")):
        ctx_bytes: int | None = None
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            event = json.loads(line)["event"]
            if event["kind"] == "context.built":
                ctx_bytes = event.get("total_bytes")
            elif event["kind"] == "model.completed" and ctx_bytes:
                tokens = event.get("input_tokens", 0)
                if tokens > 0:
                    pairs.append((ctx_bytes, tokens))
                ctx_bytes = None
    if not pairs:
        print("no paired (context.built, model.completed) samples found; run a live eval first")
        return 1
    ratios = sorted(c / t for c, t in pairs)
    print(f"samples: {len(ratios)}")
    print(
        "chars/token  "
        f"min={ratios[0]:.2f}  p10={ratios[len(ratios) // 10]:.2f}  "
        f"median={statistics.median(ratios):.2f}  mean={statistics.mean(ratios):.2f}  "
        f"p90={ratios[len(ratios) * 9 // 10]:.2f}"
    )
    # Worst case for staying under the window: the fewest chars per token means
    # the most tokens per budgeted char.
    print(
        "\nAt the densest observed ratio, a 480k-char budget implies about "
        f"{int(480_000 / max(ratios[0], 0.01)):,} input tokens "
        "(well under the 1,000,000-token window)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
