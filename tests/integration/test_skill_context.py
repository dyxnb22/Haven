"""技能进入真实运行：选中的正文必须到达模型，而且不能破坏缓存前缀。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from haven.application.skills import Skill, SkillCatalog, SkillSelectionError
from haven.contracts.model import ToolCallProposal, ToolCallReady
from haven.domain.enums import PermissionMode
from tests.integration.harness import Harness, finish, make_repo, text, tool


def skill_tool(call_id: str, name: str, path: str = "SKILL.md") -> ToolCallReady:
    """构造一次 `skill.read` 提议。

    单独一个助手函数而不是复用 `tool()`：那个助手的第二个参数就叫 `name`（工具名），
    与技能的 `name` 参数无处回避地冲突。
    """
    return ToolCallReady(
        call=ToolCallProposal(
            call_id=call_id,
            tool_name="skill.read",
            arguments_json=json.dumps({"name": name, "path": path}),
        )
    )


def _catalog(
    *skills: Skill, user_root: Path | None = None, project_root: Path | None = None
) -> SkillCatalog:
    """把技能包成目录。根目录默认指向不存在的位置——只有 `skill.read` 的测试需要
    真实的根，其余测试只关心目录内容。"""
    return SkillCatalog(skills, (), user_root, project_root)


def _skill(name: str, body: str, scope: str = "user") -> Skill:
    return Skill(
        name=name,
        description="描述",
        scope=scope,  # type: ignore[arg-type]
        source=f"skills/{name}/SKILL.md",
        digest="abc123def456",
        body=body,
    )


async def test_a_selected_skill_reaches_the_model_request(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    harness = Harness(
        repo,
        [[text("done."), finish()]],
        mode=PermissionMode.READ_ONLY,
        skills=_catalog(_skill("write-tests", "SKILL-BODY-MARKER")),
    )
    outcome = await harness.service.run("look at the parser", skill_names=["write-tests"])
    assert outcome.status.value == "succeeded"
    assert harness.model.requests_seen, "the model was never called"
    messages = harness.model.requests_seen[0].messages
    assert any("SKILL-BODY-MARKER" in m.content for m in messages)


async def test_the_catalog_is_present_even_without_a_selection(tmp_path: Path) -> None:
    """渐进披露的入口不能依赖用户是否选择：目录始终在。"""
    repo = make_repo(tmp_path)
    harness = Harness(
        repo, [[text("done."), finish()]], skills=_catalog(_skill("write-tests", "正文"))
    )
    await harness.service.run("look at the parser")
    messages = harness.model.requests_seen[0].messages
    assert any("Skill catalog" in m.content for m in messages)
    # 目录在，正文不在。
    assert not any("正文" in m.content for m in messages)


async def test_an_unknown_skill_name_refuses_the_run(tmp_path: Path) -> None:
    """选择在 turn loop 之前解析，因此运行拒绝启动而不是静默少一段上下文。"""
    repo = make_repo(tmp_path)
    harness = Harness(
        repo, [[text("done."), finish()]], skills=_catalog(_skill("write-tests", "x"))
    )
    with pytest.raises(SkillSelectionError, match="unknown skill 'nope'"):
        await harness.service.run("look at the parser", skill_names=["nope"])
    assert not harness.model.requests_seen, "a refused run must not call the model"


async def test_the_skill_head_is_identical_across_turns(tmp_path: Path) -> None:
    """ADR 0008 + ADR 0031：技能在固定头部，因此它们跨轮逐字节稳定。"""
    repo = make_repo(tmp_path)
    harness = Harness(
        repo,
        [
            # 第一轮提议一个只读工具，因此运行会继续到第二轮；一轮就结束的
            # 脚本无法比较两个请求。
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [text("second."), finish()],
        ],
        skills=_catalog(_skill("write-tests", "SKILL-BODY-MARKER")),
    )
    await harness.service.run("look at the parser", skill_names=["write-tests"])
    assert len(harness.model.requests_seen) >= 2
    first, second = harness.model.requests_seen[0], harness.model.requests_seen[1]
    # 第一轮没有 transcript，因此它的头部就是除运行状态尾部之外的全部；第二轮
    # 必须在同样的字节上继续，之后才追加 transcript（ADR 0008）。
    head_len = len(first.messages) - 1
    assert [m.content for m in second.messages[:head_len]] == [
        m.content for m in first.messages[:head_len]
    ]
    assert any("SKILL-BODY-MARKER" in m.content for m in first.messages)


async def test_a_continue_run_carries_its_own_selection(tmp_path: Path) -> None:
    """后续轮次是一次全新的运行，因此可以带自己的技能选择。"""
    repo = make_repo(tmp_path)
    harness = Harness(
        repo,
        [
            [text("first."), finish()],
            [text("second."), finish()],
        ],
        skills=_catalog(_skill("write-tests", "FIRST-SKILL"), _skill("deploy", "SECOND-SKILL")),
    )
    first = await harness.service.run("look at the parser", skill_names=["write-tests"])
    await harness.service.continue_run(first.run_id, "now deploy it", skill_names=["deploy"])
    messages = harness.model.requests_seen[-1].messages
    assert any("SECOND-SKILL" in m.content for m in messages)
    assert not any("FIRST-SKILL" in m.content for m in messages)


async def test_a_project_skill_is_framed_exactly_like_agents_md(tmp_path: Path) -> None:
    """项目级技能与 AGENTS.md 同等地不可信——同样的框架、同样的标签。"""
    repo = make_repo(tmp_path)
    (repo / "AGENTS.md").write_text("AGENTS-GUIDANCE-MARKER\n")
    harness = Harness(
        repo,
        [[text("done."), finish()]],
        skills=_catalog(_skill("team", "PROJECT-SKILL-MARKER", scope="project")),
    )
    await harness.service.run("look at the parser", skill_names=["team"])
    messages = harness.model.requests_seen[0].messages
    skill_carrier = next(m for m in messages if "PROJECT-SKILL-MARKER" in m.content)
    assert skill_carrier.role == "user"
    assert "UNTRUSTED DATA" in skill_carrier.content
    assert 'source="skill:team"' in skill_carrier.content
    # 两者都不可信，且都不在 system 角色里。
    assert "PROJECT-SKILL-MARKER" not in messages[0].content
    assert "AGENTS-GUIDANCE-MARKER" not in messages[0].content


# -- 常驻授权（ADR 0032）-----------------------------------------------------
#
# 这些测试钉住的是用户明确选择的语义：项目级技能声明 allowed-tools 后，选中即生效。
# 因此这里同时钉住它的边界——授权只能把 ASK 变成 ALLOW。


def _granting(name: str, *tools: str, scope: str = "project") -> Skill:
    return Skill(
        name=name,
        description="描述",
        scope=scope,  # type: ignore[arg-type]
        source=f"skills/{name}/SKILL.md",
        digest="abc123def456",
        body="正文",
        granted_tools=tools,
    )


def _notices(harness: Harness) -> list[str]:
    return [event.message for event in harness.sink.events_of("notice")]


def _approval_requests(harness: Harness) -> list[str]:
    """被真正问到人类的审批摘要；空列表表示一次都没有问。"""
    return [event.summary for event in harness.sink.events_of("approval.requested")]


async def test_a_user_scope_grant_needs_no_first_approval(tmp_path: Path) -> None:
    """用户级技能写在自己的配置目录里，等同于自己设审批策略，因此首次调用即生效。"""
    repo = make_repo(tmp_path)
    skill = _granting("mine", "repo.edit", scope="user")
    harness = Harness(
        repo,
        [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "c2",
                    "repo.edit",
                    path="src/calc.py",
                    old_string="return a - b  # BUG: should be +",
                    new_string="return a + b",
                    summary="fix",
                ),
                finish("tool_calls"),
            ],
            [tool("c3", "repo.diff"), finish("tool_calls")],
            [tool("c4", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
            [text("done"), finish()],
        ],
        skills=_catalog(skill),
    )
    await harness.service.run("fix it", skill_names=["mine"])
    # 被授权的 repo.edit 一次都没问。repo.check 仍然要问——那项授权来自 ADR 0025
    # 而不是这个技能，两者互不相干。
    asked = _approval_requests(harness)
    assert not any(s.startswith("edit ") for s in asked), "the granted tool should not ask"
    assert any(s.startswith("run check recipe") for s in asked), (
        "repo.check still asks per ADR 0025"
    )


async def test_a_project_scope_grant_takes_effect_on_selection(tmp_path: Path) -> None:
    """用户选择的语义：项目级技能选中即生效，第一次调用也不问。"""
    repo = make_repo(tmp_path)
    skill = _granting("team", "repo.edit")
    harness = Harness(
        repo,
        [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "c2",
                    "repo.edit",
                    path="src/calc.py",
                    old_string="return a - b  # BUG: should be +",
                    new_string="return a + b",
                    summary="fix",
                ),
                finish("tool_calls"),
            ],
            [tool("c3", "repo.diff"), finish("tool_calls")],
            [tool("c4", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
            [text("done"), finish()],
        ],
        skills=_catalog(skill),
    )
    await harness.service.run("fix it", skill_names=["team"])
    # 授权在运行开始前就被写进 journal，点名作用域、来源和 digest。
    notices = _notices(harness)
    assert any("declared by this repository" in n for n in notices)
    assert any("'team'" in n and "repo.edit" in n for n in notices)
    # 选中即生效：这一次编辑没有被问到。
    assert not any(s.startswith("edit ") for s in _approval_requests(harness))


async def test_a_grant_cannot_turn_deny_into_allow(tmp_path: Path) -> None:
    """最关键的一条：授权只能把 ASK 变成 ALLOW。

    一个授予了 repo.edit 的技能，依然不能写 `.git`——策略先拒绝，审批根本不会被
    问到，因此授权无从生效。
    """
    repo = make_repo(tmp_path)
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("[core]\n")
    skill = _granting("evil", "repo.edit", "repo.apply_patch", "repo.check", "repo.exec")
    harness = Harness(
        repo,
        [
            [
                tool(
                    "c1",
                    "repo.edit",
                    path=".git/config",
                    old_string="[core]",
                    new_string="[x]",
                    summary="tamper",
                ),
                finish("tool_calls"),
            ],
            [text("refused"), finish()],
        ],
        skills=_catalog(skill),
    )
    await harness.service.run("tamper", skill_names=["evil"])
    completed = harness.sink.events_of("tool.completed")
    assert completed, "the edit should have been attempted"
    assert completed[0].status == "error"
    assert completed[0].error_code == "denied"


async def test_a_grant_cannot_escape_the_workspace(tmp_path: Path) -> None:
    """路径逃逸同样是硬拒绝，与授权无关。"""
    repo = make_repo(tmp_path)
    skill = _granting("evil", "repo.edit")
    harness = Harness(
        repo,
        [
            [
                tool(
                    "c1",
                    "repo.edit",
                    path="../../etc/passwd",
                    old_string="a",
                    new_string="b",
                    summary="escape",
                ),
                finish("tool_calls"),
            ],
            [text("refused"), finish()],
        ],
        skills=_catalog(skill),
    )
    await harness.service.run("escape", skill_names=["evil"])
    completed = harness.sink.events_of("tool.completed")
    assert completed and completed[0].status == "error"


async def test_a_grant_does_not_survive_into_a_second_run(tmp_path: Path) -> None:
    """授权只在本次运行的内存里：一次恶意仓库无法借此获得持久权限。"""
    repo = make_repo(tmp_path)
    skill = _granting("team", "repo.edit")
    harness = Harness(
        repo,
        [
            # 第一次运行：完整的修复/验证闭环，因为授权生效所以一次都没问。
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "c2",
                    "repo.edit",
                    path="src/calc.py",
                    old_string="return a - b  # BUG: should be +",
                    new_string="return a + b",
                    summary="fix",
                ),
                finish("tool_calls"),
            ],
            [tool("c3", "repo.diff"), finish("tool_calls")],
            [tool("c4", "repo.check", recipe_id="verify-calc"), finish("tool_calls")],
            [text("done"), finish()],
            # 第二次运行：只需要走到那次编辑，就能看出它是否被问到。
            [tool("c5", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "c6",
                    "repo.edit",
                    path="src/calc.py",
                    old_string="return a + b",
                    new_string="return a - b",
                    summary="revert",
                ),
                finish("tool_calls"),
            ],
        ],
        skills=_catalog(skill),
    )
    first = await harness.service.run("fix it", skill_names=["team"])
    assert not any(s.startswith("edit ") for s in _approval_requests(harness))
    # 第二次运行不选技能，因此这次编辑必须被问到。
    await harness.service.continue_run(first.run_id, "revert it")
    assert any(s.startswith("edit ") for s in _approval_requests(harness)), (
        "a second run without the skill must not inherit the grant"
    )


async def test_a_skill_the_model_loads_itself_arms_nothing(tmp_path: Path) -> None:
    """模型自己读进来的正文是工具结果抵达的不可信文本，它不出现在授权表里。

    因此：一个技能可以存在于目录里，但只要用户没有选中它，它就什么都不授权。
    这条把"技能文本无法自行扩大权限"变成可观察的事实。
    """
    repo = make_repo(tmp_path)
    harness = Harness(
        repo,
        [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "c2",
                    "repo.edit",
                    path="src/calc.py",
                    old_string="return a - b  # BUG: should be +",
                    new_string="return a + b",
                    summary="fix",
                ),
                finish("tool_calls"),
            ],
            [text("done"), finish()],
        ],
        skills=_catalog(_granting("sneaky", "repo.edit")),
    )
    # 注意到这里没有 skill_names：目录里有它，但没有被选中。
    await harness.service.run("fix it")
    assert any(s.startswith("edit ") for s in _approval_requests(harness)), (
        "an unselected skill must not grant anything"
    )
    assert not any("standing approval in effect" in n for n in _notices(harness))


async def test_the_kill_switch_disables_every_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`HAVEN_SKILL_GRANTS=0`：不想要仓库里的 SKILL.md 影响审批时的一条退路。"""
    from haven.application.skill_grants import grant_table, grants_enabled

    skill = _granting("team", "repo.edit")
    assert grants_enabled() is True
    monkeypatch.setenv("HAVEN_SKILL_GRANTS", "0")
    assert grants_enabled() is False
    assert grant_table((skill,))["granted_tools"] == {}


async def test_a_skill_without_grants_asks_normally(tmp_path: Path) -> None:
    """没有 allowed-tools 的技能不改变任何审批行为。"""
    repo = make_repo(tmp_path)
    harness = Harness(
        repo,
        [
            [tool("c1", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "c2",
                    "repo.edit",
                    path="src/calc.py",
                    old_string="return a - b  # BUG: should be +",
                    new_string="return a + b",
                    summary="fix",
                ),
                finish("tool_calls"),
            ],
            [text("done"), finish()],
        ],
        skills=_catalog(_granting("plain")),
    )
    await harness.service.run("fix it", skill_names=["plain"])
    assert any(s.startswith("edit ") for s in _approval_requests(harness))
    assert not any("standing approval in effect" in n for n in _notices(harness))


# -- skill.read：模型自行加载正文与辅助文件（ADR 0032）----------------------


async def test_the_model_can_load_a_skill_body_itself(tmp_path: Path) -> None:
    """渐进披露的另一半：目录里有，但正文不在上下文里时，模型可以自己要。"""
    repo = make_repo(tmp_path)
    user_root = tmp_path / "skills"
    directory = user_root / "write-tests"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: write-tests\ndescription: 先写失败测试\n---\n\nSELF-LOADED-BODY\n",
        encoding="utf-8",
    )
    skill = _skill("write-tests", "SELF-LOADED-BODY")
    harness = Harness(
        repo,
        [
            [skill_tool("c1", "write-tests"), finish("tool_calls")],
            [text("done"), finish()],
        ],
        skills=_catalog(skill, user_root=user_root),
    )
    await harness.service.run("write the tests", skill_names=[])
    assert harness.model.requests_seen, "the model was never called"
    # 正文以 tool result 抵达，因此它是 append-only 的，不动固定头部。
    messages = harness.model.requests_seen[-1].messages
    assert any("SELF-LOADED-BODY" in m.content for m in messages)
    carrier = next(m for m in messages if "SELF-LOADED-BODY" in m.content)
    assert carrier.role == "tool"


async def test_the_model_can_read_an_auxiliary_file(tmp_path: Path) -> None:
    """技能里写"详见 reference.md"不再是死指令。"""
    repo = make_repo(tmp_path)
    user_root = tmp_path / "skills"
    directory = user_root / "team"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: team\ndescription: 约定\n---\n\n见 reference.md\n", encoding="utf-8"
    )
    (directory / "reference.md").write_text("AUXILIARY-REFERENCE-CONTENT\n", encoding="utf-8")
    harness = Harness(
        repo,
        [
            [skill_tool("c1", "team", "reference.md"), finish("tool_calls")],
            [text("done"), finish()],
        ],
        skills=_catalog(_skill("team", "见 reference.md"), user_root=user_root),
    )
    await harness.service.run("read the conventions")
    messages = harness.model.requests_seen[-1].messages
    assert any("AUXILIARY-REFERENCE-CONTENT" in m.content for m in messages)


async def test_a_self_loaded_skill_cannot_reach_another_skills_files(tmp_path: Path) -> None:
    """一个技能读不到另一个技能目录里的文件。"""
    repo = make_repo(tmp_path)
    user_root = tmp_path / "skills"
    for name in ("mine", "other"):
        (user_root / name).mkdir(parents=True)
        (user_root / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: d\n---\n\nb\n", encoding="utf-8"
        )
    (user_root / "other" / "secret.md").write_text("OTHER-SKILL-SECRET\n", encoding="utf-8")
    harness = Harness(
        repo,
        [
            [
                skill_tool("c1", "mine", "../other/secret.md"),
                finish("tool_calls"),
            ],
            [text("done"), finish()],
        ],
        skills=_catalog(_skill("mine", "b"), user_root=user_root),
    )
    await harness.service.run("try to escape")
    messages = harness.model.requests_seen[-1].messages
    assert not any("OTHER-SKILL-SECRET" in m.content for m in messages)
    assert any("must not traverse" in m.content for m in messages)


async def test_an_unknown_skill_name_is_a_structured_error(tmp_path: Path) -> None:
    """模型问一个不存在的技能时，得到的是可读的错误，而不是一次崩溃。"""
    repo = make_repo(tmp_path)
    harness = Harness(
        repo,
        [
            [skill_tool("c1", "nope"), finish("tool_calls")],
            [text("done"), finish()],
        ],
        skills=_catalog(_skill("real", "b")),
    )
    await harness.service.run("read a skill")
    messages = harness.model.requests_seen[-1].messages
    assert any("no discovered skill named 'nope'" in m.content for m in messages)
    assert any("available: real" in m.content for m in messages)


async def test_a_self_loaded_body_still_cannot_arm_a_grant(tmp_path: Path) -> None:
    """最关键的一条：模型自己读进来的技能，授权不生效。

    授权表只由用户显式选中的技能填充；`skill.read` 的正文是经过工具结果抵达的
    不可信文本，它不出现在那张表里。
    """
    repo = make_repo(tmp_path)
    user_root = tmp_path / "skills"
    directory = user_root / "sneaky"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: sneaky\ndescription: d\nallowed-tools: repo.edit\n---\n\nb\n",
        encoding="utf-8",
    )
    harness = Harness(
        repo,
        [
            [skill_tool("c1", "sneaky"), finish("tool_calls")],
            [tool("c2", "repo.read", path="src/calc.py"), finish("tool_calls")],
            [
                tool(
                    "c3",
                    "repo.edit",
                    path="src/calc.py",
                    old_string="return a - b  # BUG: should be +",
                    new_string="return a + b",
                    summary="fix",
                ),
                finish("tool_calls"),
            ],
            [text("done"), finish()],
        ],
        skills=_catalog(_skill("sneaky", "b"), user_root=user_root),
    )
    # 注意到 skill_names 为空：技能存在、正文也被读进来了，但没有被选中。
    await harness.service.run("read it then edit")
    asked = _approval_requests(harness)
    assert any(s.startswith("edit ") for s in asked), (
        "a skill the model loaded itself must not grant anything"
    )


async def test_skill_read_payload_contains_companion_files(tmp_path: Path) -> None:
    """读取技能时，工具返回的 payload 会告知模型同级存在哪些辅助文件（ADR 0034）。"""
    repo = make_repo(tmp_path)
    user_root = tmp_path / "skills"
    directory = user_root / "multifile"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: multifile\ndescription: Multi\n---\n\nmain body\n", encoding="utf-8"
    )
    (directory / "reference.md").write_text("ref content\n", encoding="utf-8")
    (directory / "notes.txt").write_text("notes\n", encoding="utf-8")

    harness = Harness(
        repo,
        [
            [skill_tool("c1", "multifile"), finish("tool_calls")],
            [text("done"), finish()],
        ],
        skills=_catalog(_skill("multifile", "main body"), user_root=user_root),
    )
    await harness.service.run("load multifile")
    messages = harness.model.requests_seen[-1].messages
    tool_msg = next(m for m in messages if m.role == "tool")
    assert "reference.md" in tool_msg.content
    assert "notes.txt" in tool_msg.content
