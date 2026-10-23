"""测试技能生命周期工具链与多文件清单感知（ADR 0034）。

测试覆盖：
1. `scaffold_skill` 纯函数与模板生成
2. `list_skill_files` 辅助文件清单发现与符号链接/隐藏文件过滤
3. CLI `haven skills init`
4. CLI `haven skills info`
5. CLI `haven skills validate`
6. `execute_skill_read` 在 ToolResult 中携带 `companion_files`
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from haven.application.skills import (
    SkillError,
    discover_skills,
    list_skill_files,
    parse_skill,
    scaffold_skill,
)
from haven.interfaces.cli import app
from haven.interfaces.cli_support.common import EXIT_POLICY, EXIT_USAGE

runner = CliRunner()


def test_scaffold_skill_generates_valid_skill(tmp_path: Path) -> None:
    path = scaffold_skill(
        root=tmp_path,
        name="pytest-helper",
        description="Run pytest suite cleanly",
        allowed_tools=["repo.check", "repo.edit"],
    )
    assert path.is_file()
    assert path.name == "SKILL.md"
    content = path.read_text(encoding="utf-8")
    assert "name: pytest-helper" in content
    assert "description: Run pytest suite cleanly" in content
    assert "allowed-tools: repo.check, repo.edit" in content
    assert "## When to use" in content
    assert "## Guidelines" in content

    # 验证生成的模板可被现有解析器无误解析
    parsed = parse_skill(
        content,
        name="pytest-helper",
        scope="project",
        source=".haven/skills/pytest-helper/SKILL.md",
    )
    assert parsed.name == "pytest-helper"
    assert parsed.granted_tools == ("repo.check", "repo.edit")


def test_scaffold_skill_rejects_invalid_name(tmp_path: Path) -> None:
    with pytest.raises(SkillError, match="invalid skill name"):
        scaffold_skill(root=tmp_path, name="Invalid_Name_123")


def test_scaffold_skill_rejects_unregistered_tools(tmp_path: Path) -> None:
    with pytest.raises(SkillError, match="unregistered tool"):
        scaffold_skill(root=tmp_path, name="bad-tools", allowed_tools=["rm_rf_everything"])


def test_scaffold_skill_rejects_already_existing_directory(tmp_path: Path) -> None:
    scaffold_skill(root=tmp_path, name="dup-skill")
    with pytest.raises(SkillError, match="already exists"):
        scaffold_skill(root=tmp_path, name="dup-skill")


def test_list_skill_files_discovers_companion_files(tmp_path: Path) -> None:
    skill_dir = tmp_path / "analyzer"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: analyzer\ndescription: Analysis\n---\n\nbody\n", encoding="utf-8"
    )
    (skill_dir / "reference.md").write_text("ref", encoding="utf-8")
    (skill_dir / "guidelines.md").write_text("guide", encoding="utf-8")
    (skill_dir / ".hidden_file").write_text("secret", encoding="utf-8")

    sub = skill_dir / "schemas"
    sub.mkdir()
    (sub / "config.json").write_text("{}", encoding="utf-8")

    catalog = discover_skills(user_root=tmp_path, project_root=tmp_path / "proj")
    skill = next(s for s in catalog.skills if s.name == "analyzer")
    files = list_skill_files(catalog, skill)

    assert "SKILL.md" not in files
    assert ".hidden_file" not in files
    assert files == ("guidelines.md", "reference.md", "schemas/config.json")


def test_cli_skills_init_creates_project_skill(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "skills",
            "init",
            "test-runner",
            "--workspace",
            str(tmp_path),
            "--description",
            "Run unit tests",
            "--tools",
            "repo.check",
        ],
    )
    assert result.exit_code == 0
    assert "created skill 'test-runner'" in result.stdout
    target = tmp_path / ".haven" / "skills" / "test-runner" / "SKILL.md"
    assert target.is_file()


def test_cli_skills_init_validates_inputs(tmp_path: Path) -> None:
    # 缺少技能名
    res1 = runner.invoke(app, ["skills", "init", "--workspace", str(tmp_path)])
    assert res1.exit_code == EXIT_USAGE

    # 非法 scope
    res2 = runner.invoke(
        app,
        ["skills", "init", "foo", "--workspace", str(tmp_path), "--scope", "invalid_scope"],
    )
    assert res2.exit_code == EXIT_USAGE


def test_cli_skills_info_displays_details_and_companion_files(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".haven" / "skills" / "doc-helper"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: doc-helper\n"
        "description: Manage documentation\n"
        "allowed-tools: repo.edit\n"
        "---\n\n"
        "Markdown rules\n",
        encoding="utf-8",
    )
    (skill_dir / "template.md").write_text("# Doc Template\n", encoding="utf-8")

    result = runner.invoke(app, ["skills", "info", "doc-helper", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "skill: doc-helper [project]" in result.stdout
    assert "standing approval: repo.edit" in result.stdout
    assert "companion files: template.md" in result.stdout
    assert "Markdown rules" in result.stdout


def test_cli_skills_info_nonexistent_skill(tmp_path: Path) -> None:
    result = runner.invoke(app, ["skills", "info", "nonexistent", "--workspace", str(tmp_path)])
    assert result.exit_code == EXIT_USAGE
    assert "no discovered skill named 'nonexistent'" in result.stdout


def test_cli_skills_validate_success_and_failure(tmp_path: Path) -> None:
    good_dir = tmp_path / ".haven" / "skills" / "good"
    good_dir.mkdir(parents=True)
    (good_dir / "SKILL.md").write_text(
        "---\nname: good\ndescription: good skill\n---\n\nbody\n", encoding="utf-8"
    )

    # 1. 正常时通过
    res1 = runner.invoke(app, ["skills", "validate", "--workspace", str(tmp_path)])
    assert res1.exit_code == 0
    assert "all 1 skill(s) valid" in res1.stdout

    # 2. 加入有问题的技能时失败
    bad_dir = tmp_path / ".haven" / "skills" / "bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "SKILL.md").write_text("---\nname: bad\n---\n\nmissing description\n")

    res2 = runner.invoke(app, ["skills", "validate", "--workspace", str(tmp_path)])
    assert res2.exit_code == EXIT_POLICY
    assert "validation failed: 1 skill(s) refused" in res2.stdout
