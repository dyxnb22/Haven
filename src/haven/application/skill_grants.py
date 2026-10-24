"""技能请求的常驻授权：从 `allowed-tools` 到运行内的授权表。

技能是提示片段，不是能力。`allowed-tools` 因此只回答一个问题——"这个工具有
没有资格免审"，而从不回答"它是否被授权了"。授权由程序决定：策略仍然先决定
allow/ask/deny，授权只在策略已经说了 ASK 之后才可能把它变成一次免审的执行。
这个模块就是把"哪个技能请求了哪个工具"翻译成 `RunContext` 上的授权表。

用户选择的语义是"选中即生效"（与 Claude Code 一致），因此这里不做第二次确认；
换来这个便利的是下面三条不可谈判的边界：

1. **授权只能把 ASK 变成 ALLOW，绝不能把 DENY 变成 ALLOW。** 策略层没有任何
   授权输入（`domain/policy.py` 的 `evaluate_policy` 是纯函数），而
   `tool_pipeline` 在到达审批之前就已经对 DENY 返回了。因此一个被授权的技能
   依然不能写 `.git`、不能逃出工作区、不能在没有沙箱时 exec、也不能在只读模式
   下写入。
2. **只有用户显式选中的技能能授权。** 模型通过 `skill.read` 自己读进来的正文
   是经过工具结果抵达的不可信文本，它不出现在这里，因此什么都授权不了。
3. **授权只在本次运行的内存里有效。** 它不进 checkpoint、不跨进程，所以一个
   恶意仓库无法借此获得持久权限。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypedDict

from haven.application.skills import Skill
from haven.domain.skill_grants import grants_enabled as grants_enabled


class GrantFields(TypedDict):
    """`grant_table` 的产物，直接作为 `RunContext` 的关键字参数展开。

    用 TypedDict 而不是 `dict[str, object]`：这样 mypy 能检查展开时的每一个键，
    而不是把整个调用降级成无类型。
    """

    granted_tools: dict[str, str]


def grant_table(selected: Sequence[Skill]) -> GrantFields:
    """把选中的技能翻译成 `RunContext` 的关键字参数。

    用户级技能先填表，因此同名工具由用户级技能认领——这与 `discover_skills`
    的优先级一致，也让"到底是谁授权的"只有一个答案。

    作用域不影响授权是否生效：用户选择了"选中即生效"，两种作用域一样。作用域
    只影响 `grant_notice` 怎么把这件事说出来。
    """
    if not grants_enabled():
        return GrantFields(granted_tools={})
    claimed: dict[str, str] = {}
    for skill in selected:
        for tool_name in skill.granted_tools:
            claimed.setdefault(tool_name, skill.name)  # 第一个认领者说了算
    return GrantFields(granted_tools=claimed)


def grant_notice(selected: Sequence[Skill]) -> str:
    """运行开始前写入 journal 的授权说明；没有技能请求授权时返回空串。

    刻意使用 warning 级别并点名作用域、来源和 digest：一个刚 clone 的仓库给自己
    授予写权限，这件事必须出现在轨迹里，而不是只发生在策略内部。
    """
    if not grants_enabled():
        return ""
    granting = [skill for skill in selected if skill.granted_tools]
    if not granting:
        return ""
    parts = [
        f"{skill.name!r} ({skill.scope}, {skill.source}, digest {skill.digest}): "
        f"{', '.join(skill.granted_tools)}"
        for skill in granting
    ]
    scope = "user" if all(skill.scope == "user" for skill in granting) else "project"
    if scope == "user":
        lead = "skill standing approval in effect for this run (from your own skills):"
    else:
        lead = (
            "skill standing approval in effect for this run (declared by this repository, "
            "not by Haven):"
        )
    return f"{lead} " + "; ".join(parts)
