"""卡死循环检测。

如果模型不断以相同参数提出相同工具调用，并不断得到相同结果，就没有取得进展；
运行必须停止，而不是继续消耗预算。

这里曾尝试过警告等级，但后来移除：它在 42 次实时运行中一次也没有触发；对这些
日志进行的追踪研究（`evals/trace_study.py`）发现，不收敛的运行并不等于重复运行——
11 次慢运行中只有 1 次重复过调用，与快速组的比例相同。记录见
`docs/notes/rejected/0002`。该检测器仍然是针对字面重复操作的后备保护，不是解决
不收敛问题的办法。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from haven.domain.digest import digest_of


def call_fingerprint(tool_name: str, arguments_json: str, result_text: str) -> str:
    """一次（调用、结果）观测的标识。

    这是唯一的定义——运行循环调用此函数，而不是自行组合摘要，因此测试固定的
    行为就是实际发布的行为。该值只会与同一次运行中的其他指纹比较：不会持久化、
    写入日志或跨版本比较，因此其具体形状可以改变。

    `result_text` 是模型实际看到的观测结果（对它做摘要也能得到相同区分效果）；
    直接传入文本可以避免调用者重复哈希。
    """
    return digest_of({"tool": tool_name, "args": arguments_json, "result": result_text})


@dataclass(slots=True)
class StuckLoopDetector:
    """统计连续相同的（工具、参数、结果）观测。"""

    #: 判定循环卡死所需的连续相同观测次数。
    threshold: int = 3
    #: 上一次调用观察到的指纹。
    _last_fingerprint: str | None = field(default=None, repr=False)
    #: 与 _last_fingerprint 连续匹配的观测次数。
    _repeat_count: int = field(default=0, repr=False)

    def observe(self, fingerprint: str) -> bool:
        """记录一次观测；循环卡死时返回 True。"""
        if fingerprint == self._last_fingerprint:
            self._repeat_count += 1
        else:
            self._last_fingerprint = fingerprint
            self._repeat_count = 1
        return self._repeat_count >= self.threshold
