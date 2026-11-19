"""工具表的不变量：集合互斥、约束无陈旧条目、覆盖完整。

`--check` 门禁只比对"表格与策略代码是否一致"，因此任何让表格**自洽地说谎**的改动
它都抓不到——比如把一个工具加进两个集合（`_class_of` 取第一个，class 列静默出错），
或者删掉工具却留下约束条目（表格静默少一行）。这里把这三个不变量钉住，让它们由
CI 永久把守，而不是靠某一次人工检查。
"""

from haven.domain.policy import (
    EFFECT_TOOLS,
    EXEC_TOOLS,
    KNOWN_TOOLS,
    READ_ONLY_TOOLS,
    STATE_TOOLS,
)
from scripts.gen_tool_table import CONSTRAINTS, _class_of, rows_for_tools

_SETS = {
    "read-only": READ_ONLY_TOOLS,
    "effect": EFFECT_TOOLS,
    "state": STATE_TOOLS,
    "exec": EXEC_TOOLS,
}


class TestTheFourToolSetsAreDisjoint:
    """一个工具只能属于一个副作用类别。

    `_class_of` 按 `_CLASSES` 的顺序取第一个匹配，因此集合一旦重叠，class 列会
    静默报错——而表格仍然"与代码一致"，`--check` 照样通过。
    """

    def test_no_tool_appears_in_two_sets(self) -> None:
        names = list(_SETS)
        for index, first in enumerate(names):
            for second in names[index + 1 :]:
                assert not (_SETS[first] & _SETS[second]), (
                    f"{first} and {second} both contain {sorted(_SETS[first] & _SETS[second])}"
                )

    def test_the_union_is_exactly_known_tools(self) -> None:
        union = set().union(*_SETS.values())
        assert union == set(KNOWN_TOOLS)

    def test_every_tool_is_classified(self) -> None:
        for tool in KNOWN_TOOLS:
            assert _class_of(tool) != "unclassified", f"{tool} is in no set"


class TestConstraintsHaveNoStaleEntries:
    """约束字典里不该留着已不存在工具的条目。

    删掉一个工具时很容易只改 `policy.py`：条目留在这里，表格静默少一行，而没有任何
    检查会因此变红。
    """

    def test_every_entry_names_a_real_tool(self) -> None:
        stale = sorted(set(CONSTRAINTS) - set(KNOWN_TOOLS))
        assert not stale, f"stale constraints entries: {stale}"

    def test_every_tool_has_an_entry(self) -> None:
        missing = sorted(set(KNOWN_TOOLS) - set(CONSTRAINTS))
        assert not missing, f"tools without a constraints entry: {missing}"


class TestTheTableCoversEverything:
    def test_one_row_per_known_tool(self) -> None:
        rows = rows_for_tools()
        assert [row.tool for row in rows] == sorted(KNOWN_TOOLS)

    def test_the_decisions_come_from_the_real_policy(self) -> None:
        """每一格都是调用 `evaluate_policy` 得到的，不是手抄的。"""
        rows = {row.tool: row for row in rows_for_tools()}
        assert rows["repo.read"].interactive == "allow"
        assert rows["repo.read"].read_only == "allow"
        # 写入类工具在只读模式下必须是硬拒绝——这是 ADR 0017 的边界。
        assert rows["repo.edit"].read_only == "deny"
        assert rows["repo.edit"].interactive == "ask"
