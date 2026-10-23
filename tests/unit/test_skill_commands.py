"""TUI slash 命令的纯路由规则：`/skill` 家族。

`route_command` 是纯函数，因此这里不启动 Textual——只喂数据、断言动作。真正
把选择送进运行的那个测试在 `tests/tui/test_tui_journey.py`。
"""

from __future__ import annotations

from haven.application.skills import Skill, SkillCatalog
from haven.interfaces.tui.commands import HELP_TEXT, route_command
from haven.interfaces.tui.presenter import PresenterState


def _skill(name: str, **overrides: object) -> Skill:
    fields: dict[str, object] = {
        "name": name,
        "description": "描述",
        "scope": "user",
        "source": f"skills/{name}/SKILL.md",
        "digest": "abc123def456",
        "body": "正文",
    }
    fields.update(overrides)
    return Skill(**fields)  # type: ignore[arg-type]


CATALOG = SkillCatalog((_skill("write-tests"), _skill("deploy")))


def test_help_lists_the_skill_command() -> None:
    """`scripts/check_docs.py` 要求 HELP_TEXT 里的每个命令都出现在 README 中，
    因此新增命令必须同时出现在两处。"""
    assert "/skill" in HELP_TEXT


def test_slash_skill_with_no_args_lists_the_catalog() -> None:
    action = route_command("/skill", PresenterState(), skills=CATALOG)
    assert action.kind == "log"
    assert "write-tests" in action.value
    assert "deploy" in action.value
    # 目录说明自己的性质：建议性片段，不是工具。
    assert "not tools" in action.value


def test_slash_skill_never_echoes_a_body() -> None:
    """正文是模型上下文，不是给人看的日志。"""
    action = route_command("/skill", PresenterState(), skills=CATALOG)
    assert "正文" not in action.value


def test_slash_skill_use_records_the_selection() -> None:
    action = route_command("/skill use write-tests deploy", PresenterState(), skills=CATALOG)
    assert action.kind == "skill"
    assert action.value == "write-tests deploy"


def test_slash_skill_clear_resets_the_selection() -> None:
    action = route_command("/skill clear", PresenterState(), skills=CATALOG)
    assert action == type(action)(kind="skill", value="clear")


def test_slash_skill_use_without_a_name_shows_usage() -> None:
    action = route_command("/skill use", PresenterState(), skills=CATALOG)
    assert action.kind == "log"
    assert "usage:" in action.value


def test_slash_skill_with_an_unknown_subcommand_shows_usage() -> None:
    action = route_command("/skill frobnicate", PresenterState(), skills=CATALOG)
    assert action.kind == "log"
    assert "usage:" in action.value


def test_slash_skill_before_startup_is_reported() -> None:
    """启动完成前没有目录；必须明说，而不是显示一个空的技能列表。"""
    action = route_command("/skill", PresenterState(), skills=None)
    assert action.kind == "log"
    assert "unavailable" in action.value


def test_an_empty_catalog_says_where_to_look() -> None:
    action = route_command("/skill", PresenterState(), skills=SkillCatalog())
    assert action.kind == "log"
    assert "no skills discovered" in action.value
    assert "haven doctor" in action.value


def test_refused_skills_are_surfaced_not_hidden() -> None:
    """失败即关闭必须可见，否则用户以为自己的技能没写对地方。"""
    from haven.application.skills import SkillRefusal

    catalog = SkillCatalog((_skill("ok"),), (SkillRefusal("skills/bad/SKILL.md", "no body"),))
    action = route_command("/skill", PresenterState(), skills=catalog)
    assert "1 skill file(s) refused" in action.value
    assert "haven doctor" in action.value
