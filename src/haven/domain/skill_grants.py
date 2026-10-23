"""技能常驻授权的开关。

放在 `domain` 而不是 `application`：`haven.config` 和 `haven.application.skill_grants`
都需要它，而后者已经依赖前者——把它放在中间层会让两个方向同时想 import 它。
这是一个纯粹的环境变量判断，没有任何依赖，因此属于最底层。
"""

from __future__ import annotations

import os

#: 关闭技能常驻授权的环境变量值。默认开启，因为用户选择了"像一个主流 agent 那样
#: 方便"；这个开关存在的理由是：一旦有人不想让仓库里的 SKILL.md 影响审批，他们
#: 需要一个不用改代码的退路（ADR 0032）。
_DISABLED_VALUES = frozenset({"0", "false", "no"})


def grants_enabled() -> bool:
    """技能请求的常驻授权是否生效。"""
    return os.environ.get("HAVEN_SKILL_GRANTS", "1").strip().lower() not in _DISABLED_VALUES
