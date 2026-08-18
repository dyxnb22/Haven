"""根据实时评估事件流测量字符/token 比例。

上下文预算（模型 profile 中的 `max_context_chars`）按字符计数，但提供商按 token
计费并以 token 设置窗口。本脚本读取已提交的实时运行事件流
（report-*/report-*-events/*.jsonl），将每个 `context.built` 请求大小（字节）与
它产生的 `model.completed` 输入 token 数配对，并报告字符/token 分布——这样就能
根据真实 token 窗口检查手工设置的字符预算，而不是靠猜测。参见
ModelProfile.context_window_tokens 以及 `tests/unit/test_profiles.py` 中的守卫。

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
    # 对于保持在窗口限制以内，这是最坏情况：每个 token 对应的字符数越少，
    # 给定字符预算所包含的 token 就越多。
    print(
        "\nAt the densest observed ratio, a 480k-char budget implies about "
        f"{int(480_000 / max(ratios[0], 0.01)):,} input tokens "
        "(well under the 1,000,000-token window)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
