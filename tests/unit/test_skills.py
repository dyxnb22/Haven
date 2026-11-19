"""技能发现、失败即关闭解析、目录渲染与选择解析。

与 `test_guidance.py` 同风格：真实文件系统 + tmp_path，不 mock。这里的核心契约
是"失败即关闭"——一个不符合契约的 SKILL.md 必须被拒绝并带上原因，而不是被部分
加载或静默忽略（ADR 0031）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from haven.application.skills import (
    MAX_BODY_CHARS,
    MAX_CATALOG_CHARS,
    MAX_DESCRIPTION_CHARS,
    MAX_SCANNED_PER_ROOT,
    MAX_SELECTED_SKILLS,
    MAX_SKILL_FILE_CHARS,
    MAX_SKILLS,
    SKILL_FILENAME,
    Skill,
    SkillCatalog,
    SkillError,
    SkillSelectionError,
    _is_within,
    discover_skills,
    parse_skill,
    read_skill_file,
    render_catalog,
    resolve_selected_skills,
)
from haven.domain.digest import sha256_text

MINIMAL = "---\nname: {name}\ndescription: 先写失败测试\n---\n\n正文。\n"


def _write(root: Path, name: str, text: str | None = None) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / SKILL_FILENAME
    path.write_text(MINIMAL.format(name=name) if text is None else text, encoding="utf-8")
    return path


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


class TestFrontmatterParsing:
    @pytest.mark.parametrize(
        ("dirname", "text", "reason"),
        [
            ("demo", "正文，没有 frontmatter\n", "delimiter"),
            ("demo", "---\nname: demo\ndescription: x\n", "closing"),
            ("demo", "---\nname: demo\ndescription: x\n---\n", "body"),
            ("demo", "---\nname: demo\n---\n\n正文\n", "description"),
            ("demo", "---\ndescription: x\n---\n\n正文\n", "'name'"),
            ("demo", "---\nname: other\ndescription: x\n---\n\n正文\n", "match"),
            ("demo", "---\nname: demo\ndescription: x\nmodel: gpt\n---\n\n正文\n", "unknown"),
            ("demo", "---\nname: demo\nname: demo2\ndescription: x\n---\n\n正文\n", "duplicate"),
            ("demo", "---\nname demo\ndescription: x\n---\n\n正文\n", "key: value"),
            ("demo", "---\nname: demo\n\ndescription: x\n---\n\n正文\n", "blank line"),
            # 以 `-` 开头的名字在错误消息里会被看成 CLI 参数。
            ("--flag", "---\nname: --flag\ndescription: x\n---\n\n正文\n", "invalid"),
            ("_priv", "---\nname: _priv\ndescription: x\n---\n\n正文\n", "invalid"),
        ],
    )
    def test_a_malformed_skill_is_refused(self, dirname: str, text: str, reason: str) -> None:
        with pytest.raises(SkillError, match=reason):
            parse_skill(text, name=dirname, scope="user", source=f"skills/{dirname}/SKILL.md")

    def test_a_minimal_skill_parses(self) -> None:
        skill = parse_skill(MINIMAL.format(name="demo"), name="demo", scope="user", source="s")
        assert skill.name == "demo"
        assert skill.description == "先写失败测试"
        assert skill.body == "正文。"
        assert skill.scope == "user"
        assert len(skill.digest) == 12

    def test_the_digest_is_stable_and_body_sensitive(self) -> None:
        text = MINIMAL.format(name="demo")
        first = parse_skill(text, name="demo", scope="user", source="s")
        other = parse_skill(
            text.replace("正文。", "别的正文。"), name="demo", scope="user", source="s"
        )
        assert first.digest == parse_skill(text, name="demo", scope="user", source="s").digest
        assert first.digest != other.digest

    def test_a_name_that_looks_like_a_flag_is_rejected(self) -> None:
        """`--flag` 作为目录名会在错误消息里被看成 CLI 参数；正则要求首字符为
        字母数字，正是为了把它挡在外面。"""
        with pytest.raises(SkillError, match="invalid"):
            parse_skill(MINIMAL.format(name="--flag"), name="--flag", scope="user", source="s")


class TestBounds:
    def test_the_description_is_truncated_to_the_cap(self) -> None:
        text = f"---\nname: demo\ndescription: {'d' * 500}\n---\n\n正文\n"
        skill = parse_skill(text, name="demo", scope="user", source="s")
        assert len(skill.description) == 500  # 原文保留
        catalog = render_catalog([skill])
        # 目录条目里只应出现截断后的 description，且整段仍在上限之内。
        assert catalog.count("d" * MAX_DESCRIPTION_CHARS) == 1
        assert "d" * (MAX_DESCRIPTION_CHARS + 1) not in catalog

    def test_the_body_is_truncated_to_the_cap(self) -> None:
        text = f"---\nname: demo\ndescription: x\n---\n\n{'b' * 9000}\n"
        skill = parse_skill(text, name="demo", scope="user", source="s")
        assert len(skill.body) == MAX_BODY_CHARS

    def test_the_digest_covers_the_body_that_is_actually_shown(self) -> None:
        """digest 必须等于 `body` 的摘要，而不是完整正文的摘要。

        `body` 存的是截断后的那一份，`haven skills info` 又把 digest 和 body 并排打印，
        用户自然会拿它们互验。此前 digest 算的是完整正文：正文一旦超过
        MAX_BODY_CHARS，digest 说的就是没人见过的字节，二者对不上——而这个字段
        唯一的用途正是"让模型和追踪验证看到的正文确实是这一条"。
        """
        text = f"---\nname: demo\ndescription: x\n---\n\n{'b' * 9000}\n"
        skill = parse_skill(text, name="demo", scope="user", source="s")
        assert len(skill.body) == MAX_BODY_CHARS
        assert skill.digest == sha256_text(skill.body)[:12]

    def test_the_catalog_is_truncated_to_the_cap(self) -> None:
        skills = [
            _skill(f"skill-{index:02d}", description="x" * 100) for index in range(MAX_SKILLS)
        ]
        catalog = render_catalog(skills)
        assert len(catalog) <= MAX_CATALOG_CHARS
        assert len(catalog.splitlines()) < MAX_SKILLS + 1

    def test_a_bounded_catalog_says_that_it_is_bounded(self) -> None:
        """目录条目排不下时必须明说，而不是静默少列几个。

        一个静默截断的目录会让模型和用户都认为没列出的技能不存在，于是
        `--skill <那个技能>` 拿到一句"unknown skill"——一个纯粹由呈现方式造成的死胡同。
        实测 1500 字符下，12 个 description 用满的技能只列得出 6 个左右，所以这条
        路径经常走到，不是理论问题。
        """
        skills = [
            _skill(f"skill-{index:02d}", description="d" * MAX_DESCRIPTION_CHARS)
            for index in range(MAX_SKILLS)
        ]
        catalog = render_catalog(skills)
        listed = catalog.splitlines()[1:]
        assert any("not listed" in line for line in listed), (
            "条目被预算挤掉时必须留下一句'还有 N 个没列出'"
        )
        # 那句话自己也要数对。
        omitted = MAX_SKILLS - sum(1 for line in listed if "not listed" not in line)
        assert any(f"and {omitted} more skill(s) not listed" in line for line in listed)
        # 并且不能因为多了一句就超预算——有界性本身不能因为省字符被挤掉。
        assert len(catalog) <= MAX_CATALOG_CHARS

    def test_an_untruncated_catalog_has_no_such_note(self) -> None:
        skills = [_skill(f"skill-{index:02d}", description="short") for index in range(MAX_SKILLS)]
        assert "not listed" not in render_catalog(skills)

    def test_at_most_max_skills_are_listed(self, tmp_path: Path) -> None:
        for index in range(MAX_SKILLS + 5):
            _write(tmp_path, f"skill-{index:02d}")
        catalog = discover_skills(user_root=tmp_path, project_root=tmp_path / "no-project")
        assert len(catalog.skills) == MAX_SKILLS

    def test_the_catalog_cap_is_not_a_silent_drop(self, tmp_path: Path) -> None:
        """超出 MAX_SKILLS 的技能必须留下拒绝记录，而不是无声消失。

        与 `test_the_scan_is_bounded_and_says_so` 是同一条原则。此前这里是
        `accepted[:MAX_SKILLS]`：一个装了几十个技能的仓库在 `haven skills list` 里
        看起来只装了 12 个，而 `haven skills info <被丢的那个>` 只会回答"没有这个技能"，
        用户无从区分它是被拒绝了、还是根本没被发现。
        """
        for index in range(MAX_SKILLS + 5):
            _write(tmp_path, f"skill-{index:02d}")
        catalog = discover_skills(user_root=tmp_path, project_root=tmp_path / "no-project")
        dropped = [r for r in catalog.refused if "not listed" in r.reason]
        assert len(dropped) == 5
        # 拒绝记录要点名是哪一个，而不只是报个数。
        assert {r.source for r in dropped} == {
            f"skills/skill-{index:02d}/SKILL.md" for index in range(MAX_SKILLS, MAX_SKILLS + 5)
        }

    def test_the_scan_is_bounded_and_says_so(self, tmp_path: Path) -> None:
        for index in range(MAX_SCANNED_PER_ROOT + 6):
            _write(tmp_path, f"skill-{index:03d}")
        catalog = discover_skills(user_root=tmp_path, project_root=tmp_path / "no-project")
        reasons = " ".join(refusal.reason for refusal in catalog.refused)
        assert "not scanned" in reasons


class TestDiscovery:
    def test_both_roots_are_discovered(self, tmp_path: Path) -> None:
        user_root = tmp_path / "user"
        project_root = tmp_path / "repo"
        _write(user_root, "from-user")
        (project_root / ".haven").mkdir(parents=True)
        _write(project_root / ".haven" / "skills", "from-project")
        catalog = discover_skills(user_root=user_root, project_root=project_root)
        assert catalog.names() == ("from-user", "from-project")
        assert catalog.skills[0].scope == "user"
        assert catalog.skills[1].scope == "project"
        assert catalog.skills[1].source == ".haven/skills/from-project/SKILL.md"

    def test_a_missing_root_yields_an_empty_catalog(self, tmp_path: Path) -> None:
        catalog = discover_skills(user_root=tmp_path / "nope", project_root=tmp_path / "also-nope")
        assert catalog.skills == ()
        assert catalog.refused == ()

    def test_a_project_skill_shadowing_a_user_skill_is_refused(self, tmp_path: Path) -> None:
        user_root = tmp_path / "user"
        project_root = tmp_path / "repo"
        _write(user_root, "demo")
        (project_root / ".haven" / "skills").mkdir(parents=True)
        _write(project_root / ".haven" / "skills", "demo")
        catalog = discover_skills(user_root=user_root, project_root=project_root)
        assert catalog.names() == ("demo",)
        assert catalog.skills[0].scope == "user"
        assert [refusal.reason for refusal in catalog.refused] == [
            "shadowed by the user skill of the same name"
        ]

    def test_refusals_are_recorded_with_a_reason(self, tmp_path: Path) -> None:
        _write(tmp_path, "good")
        _write(tmp_path, "bad", "---\nname: bad\n---\n\n没有 description\n")
        catalog = discover_skills(user_root=tmp_path, project_root=tmp_path / "no-project")
        assert catalog.names() == ("good",)
        assert [(r.source, r.reason) for r in catalog.refused] == [
            ("skills/bad/SKILL.md", "missing 'description'")
        ]

    def test_a_symlinked_skill_file_is_refused(self, tmp_path: Path) -> None:
        """与 `FsWorkspace.path_facts` 拒绝符号链接同理由：一个指向敏感文件的
        SKILL.md 会让 Haven 把工作区之外的秘密读进提示词。"""
        secret = tmp_path / "secret.txt"
        secret.write_text("TOPSECRET", encoding="utf-8")
        (tmp_path / "sneaky").mkdir()
        (tmp_path / "sneaky" / SKILL_FILENAME).symlink_to(secret)
        catalog = discover_skills(user_root=tmp_path, project_root=tmp_path / "no-project")
        assert catalog.names() == ()
        assert [r.reason for r in catalog.refused] == ["refused: skill file is a symlink"]

    def test_an_unreadable_skill_is_refused(self, tmp_path: Path) -> None:
        """把 SKILL.md 换成目录：`read_text` 抛 IsADirectoryError（一个 OSError）。
        不 mock——DI 优于 monkeypatch（CONVENTIONS.md）。"""
        (tmp_path / "weird" / SKILL_FILENAME).mkdir(parents=True)
        catalog = discover_skills(user_root=tmp_path, project_root=tmp_path / "no-project")
        assert catalog.names() == ()
        assert "could not read" in catalog.refused[0].reason

    def test_discovery_is_deterministic(self, tmp_path: Path) -> None:
        for name in ("c", "a", "b"):
            _write(tmp_path, name)
        first = discover_skills(user_root=tmp_path, project_root=tmp_path / "no-project")
        second = discover_skills(user_root=tmp_path, project_root=tmp_path / "no-project")
        assert first == second
        assert first.names() == ("a", "b", "c")

    def test_a_non_utf8_skill_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "binary").mkdir()
        (tmp_path / "binary" / SKILL_FILENAME).write_bytes(
            b"---\nname: binary\ndescription: \xff\xfe\n---\n\nx\n"
        )
        catalog = discover_skills(user_root=tmp_path, project_root=tmp_path / "no-project")
        assert catalog.names() == ()
        assert "could not read" in catalog.refused[0].reason


class TestCatalogRendering:
    def test_the_catalog_names_scope_and_digest(self) -> None:
        catalog = render_catalog([_skill("demo", scope="project")])
        assert "- demo [project] abc123def456 — 描述" in catalog

    def test_the_catalog_never_contains_a_body(self) -> None:
        skill = _skill("demo", body="SECRET-BODY-THAT-MUST-NOT-LEAK")
        catalog = render_catalog([skill])
        assert "SECRET-BODY-THAT-MUST-NOT-LEAK" not in catalog

    def test_the_catalog_states_that_a_skill_grants_no_authority(self) -> None:
        catalog = render_catalog([_skill("demo")])
        assert "NOT a tool" in catalog
        assert "grants no authority" in catalog


class TestSelection:
    def test_selection_is_returned_in_catalog_order(self) -> None:
        catalog = SkillCatalog((_skill("a"), _skill("b"), _skill("c")))
        selected = resolve_selected_skills(catalog.skills, ["c", "a"])
        assert [skill.name for skill in selected] == ["a", "c"]

    def test_an_unknown_name_is_a_clean_error(self) -> None:
        catalog = SkillCatalog((_skill("demo"),))
        with pytest.raises(SkillSelectionError, match="unknown skill 'nope'"):
            resolve_selected_skills(catalog.skills, ["nope"])

    def test_the_error_lists_what_is_available(self) -> None:
        catalog = SkillCatalog((_skill("a"), _skill("b")))
        with pytest.raises(SkillSelectionError, match="available: a, b"):
            resolve_selected_skills(catalog.skills, ["zzz"])

    def test_an_empty_catalog_names_the_reason(self) -> None:
        with pytest.raises(SkillSelectionError, match=r"\(none discovered\)"):
            resolve_selected_skills((), ["anything"])

    def test_more_than_max_selected_is_refused(self) -> None:
        catalog = SkillCatalog(
            tuple(_skill(f"s{index}") for index in range(MAX_SELECTED_SKILLS + 2))
        )
        with pytest.raises(SkillSelectionError, match=f"at most {MAX_SELECTED_SKILLS}"):
            resolve_selected_skills(
                catalog.skills, [f"s{index}" for index in range(MAX_SELECTED_SKILLS + 2)]
            )

    def test_repeated_names_are_deduplicated(self) -> None:
        catalog = SkillCatalog((_skill("a"), _skill("b")))
        assert resolve_selected_skills(catalog.skills, ["a", "a", " a "]) == (catalog.skills[0],)

    def test_an_empty_selection_is_empty(self) -> None:
        assert resolve_selected_skills((_skill("a"),), []) == ()
        assert resolve_selected_skills((_skill("a"),), ["", "  "]) == ()


class TestAllowedTools:
    """`allowed-tools` 只决定"有资格免审"，本身不授予任何权限（ADR 0032）。"""

    def test_a_comma_separated_list_parses(self) -> None:
        text = (
            "---\nname: demo\ndescription: x\nallowed-tools: repo.edit, repo.check\n---\n\n正文\n"
        )
        skill = parse_skill(text, name="demo", scope="project", source="s")
        assert skill.granted_tools == ("repo.check", "repo.edit")

    def test_a_bracketed_list_parses(self) -> None:
        """用户会自然地两种都写；项目没有 YAML 依赖，因此两种都要认。"""
        text = (
            "---\nname: demo\ndescription: x\nallowed-tools: [repo.edit, repo.check]\n---\n\n正文\n"
        )
        assert parse_skill(text, name="demo", scope="user", source="s").granted_tools == (
            "repo.check",
            "repo.edit",
        )

    def test_the_order_is_normalized_so_the_grant_key_is_stable(self) -> None:
        """同一个文件必须永远产出同一个元组，否则技能内容没变也会丢授权。"""
        first = "---\nname: d\ndescription: x\nallowed-tools: repo.check, repo.edit\n---\n\nb\n"
        second = "---\nname: d\ndescription: x\nallowed-tools: repo.edit, repo.check\n---\n\nb\n"
        assert parse_skill(first, name="d", scope="user", source="s").granted_tools == (
            parse_skill(second, name="d", scope="user", source="s").granted_tools
        )

    def test_duplicates_are_collapsed(self) -> None:
        text = "---\nname: d\ndescription: x\nallowed-tools: repo.edit, repo.edit\n---\n\nb\n"
        assert parse_skill(text, name="d", scope="user", source="s").granted_tools == ("repo.edit",)

    def test_an_absent_key_grants_nothing(self) -> None:
        assert (
            parse_skill(MINIMAL.format(name="d"), name="d", scope="user", source="s").granted_tools
            == ()
        )

    def test_an_empty_value_grants_nothing(self) -> None:
        text = "---\nname: d\ndescription: x\nallowed-tools:\n---\n\nb\n"
        assert parse_skill(text, name="d", scope="user", source="s").granted_tools == ()

    def test_an_unregistered_tool_name_refuses_the_whole_skill(self) -> None:
        """失败即关闭：宁可没有这个技能，也不要一个授予了不存在工具的技能。"""
        text = "---\nname: d\ndescription: x\nallowed-tools: repo.edit, shell\n---\n\nb\n"
        with pytest.raises(SkillError, match=r"unregistered tool: 'shell'"):
            parse_skill(text, name="d", scope="user", source="s")

    def test_every_real_tool_name_is_accepted(self) -> None:
        """授权集合必须覆盖全部真实工具，否则作者无法为自己需要的工具授权。"""
        from haven.domain.policy import KNOWN_TOOLS

        listing = ", ".join(sorted(KNOWN_TOOLS))
        text = f"---\nname: d\ndescription: x\nallowed-tools: {listing}\n---\n\nb\n"
        assert parse_skill(text, name="d", scope="user", source="s").granted_tools == tuple(
            sorted(KNOWN_TOOLS)
        )

    def test_the_catalog_states_a_grant_as_program_fact(self) -> None:
        skill = _skill("team", granted_tools=("repo.check",))
        catalog = render_catalog([skill])
        assert "standing approval granted for this run: repo.check" in catalog
        # 这是 Haven 渲染的事实，不是技能文本自己的声明。
        assert "grants no authority" in catalog

    def test_a_skill_without_grants_adds_no_suffix(self) -> None:
        assert "standing approval" not in render_catalog([_skill("plain")])


class TestSkillFileReader:
    """`skill.read` 的边界：一个技能只能读自己目录内的普通文件。"""

    def _catalog(self, tmp_path: Path) -> SkillCatalog:
        return discover_skills(user_root=tmp_path, project_root=tmp_path / "no-project")

    def _skill_dir(self, tmp_path: Path, name: str) -> Path:
        directory = tmp_path / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / SKILL_FILENAME).write_text(MINIMAL.format(name=name), encoding="utf-8")
        return directory

    def test_the_body_and_auxiliary_files_both_read(self, tmp_path: Path) -> None:
        directory = self._skill_dir(tmp_path, "demo")
        (directory / "reference.md").write_text("参考内容", encoding="utf-8")
        catalog = self._catalog(tmp_path)
        skill = catalog.skills[0]
        assert read_skill_file(catalog, skill, "SKILL.md")[0].startswith("---")
        body, truncated = read_skill_file(catalog, skill, "reference.md")
        assert body == "参考内容"
        assert truncated is False

    def test_an_absolute_path_is_refused(self, tmp_path: Path) -> None:
        self._skill_dir(tmp_path, "demo")
        catalog = self._catalog(tmp_path)
        with pytest.raises(SkillError, match="must be relative"):
            read_skill_file(catalog, catalog.skills[0], "/etc/passwd")

    def test_a_traversing_path_is_refused(self, tmp_path: Path) -> None:
        self._skill_dir(tmp_path, "demo")
        catalog = self._catalog(tmp_path)
        with pytest.raises(SkillError, match="must not traverse"):
            read_skill_file(catalog, catalog.skills[0], "../../etc/passwd")

    def test_another_skills_file_is_out_of_reach(self, tmp_path: Path) -> None:
        """一个技能读不到另一个技能目录里的文件。"""
        self._skill_dir(tmp_path, "demo")
        other = self._skill_dir(tmp_path, "other")
        (other / "secret.md").write_text("SECRET", encoding="utf-8")
        catalog = self._catalog(tmp_path)
        with pytest.raises(SkillError, match="must not traverse"):
            read_skill_file(catalog, catalog.skills[0], "../other/secret.md")

    def test_a_path_through_a_symlinked_directory_is_refused(self, tmp_path: Path) -> None:
        """目录组件是链接时，先报"这是个链接"，再谈包含性。

        `target.is_symlink()` 只看最后一层，看不见中间的 `link`；逐组件检查之后，
        作者听到的是"link 是个符号链接"，而不是一句泛泛的越界。
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("SECRET", encoding="utf-8")
        self._skill_dir(tmp_path, "demo")
        (tmp_path / "demo" / "link").symlink_to(outside, target_is_directory=True)
        catalog = self._catalog(tmp_path)
        with pytest.raises(SkillError, match="refused: skill directory 'link' is a symlink"):
            read_skill_file(catalog, catalog.skills[0], "link/secret.md")

    def test_a_skill_directory_that_became_a_symlink_is_refused(self, tmp_path: Path) -> None:
        """catalog 是上一次发现的快照：目录此后可能已经换成了链接。

        `discover_skills` 拒绝链接目录，但 `skill.read` 不能因此就相信快照——
        它指向哪里，这一道边界就必须在哪里拦住。
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("SECRET", encoding="utf-8")
        directory = self._skill_dir(tmp_path, "demo")
        catalog = self._catalog(tmp_path)
        (directory / SKILL_FILENAME).unlink()
        directory.rmdir()
        directory.symlink_to(outside, target_is_directory=True)
        with pytest.raises(SkillError, match="refused: skill directory 'demo' is a symlink"):
            read_skill_file(catalog, catalog.skills[0], "secret.md")

    def test_the_boundary_is_a_directory_not_a_string_prefix(self) -> None:
        """`<root>/foo` 与 `<root>/foobar` 是两个技能目录，不是包含关系。

        字符串前缀会把它们判成同一个，于是 `foo` 里的技能可以读到 `foobar` 的
        文件。用父目录链比较，这对兄弟就必须分开。
        """
        assert _is_within(Path("/s/foo"), Path("/s/foo/x.md"))
        assert _is_within(Path("/s/foo"), Path("/s/foo"))
        assert not _is_within(Path("/s/foo"), Path("/s/foobar/x.md"))
        assert not _is_within(Path("/s/foo"), Path("/s"))

    def test_a_symlinked_auxiliary_file_is_refused(self, tmp_path: Path) -> None:
        """与 `discover_skills` 拒绝符号链接同理由：一个指向 ~/.ssh/id_rsa 的
        reference.md 会把工作区之外的秘密读进提示词。"""
        secret = tmp_path / "secret.txt"
        secret.write_text("TOPSECRET", encoding="utf-8")
        directory = self._skill_dir(tmp_path, "demo")
        (directory / "reference.md").symlink_to(secret)
        catalog = self._catalog(tmp_path)
        with pytest.raises(SkillError, match="symlink"):
            read_skill_file(catalog, catalog.skills[0], "reference.md")

    def test_an_oversized_file_is_clipped_and_flagged(self, tmp_path: Path) -> None:
        directory = self._skill_dir(tmp_path, "demo")
        (directory / "big.md").write_text("x" * (MAX_SKILL_FILE_CHARS + 500), encoding="utf-8")
        catalog = self._catalog(tmp_path)
        body, truncated = read_skill_file(catalog, catalog.skills[0], "big.md")
        assert len(body) == MAX_SKILL_FILE_CHARS
        assert truncated is True

    def test_a_missing_file_is_a_clean_error(self, tmp_path: Path) -> None:
        self._skill_dir(tmp_path, "demo")
        catalog = self._catalog(tmp_path)
        with pytest.raises(SkillError, match="could not read"):
            read_skill_file(catalog, catalog.skills[0], "nope.md")

    def test_a_catalog_without_roots_refuses(self) -> None:
        with pytest.raises(SkillError, match="no user skill root"):
            read_skill_file(SkillCatalog(), _skill("demo"), "SKILL.md")

    def test_padded_whitespace_in_the_path_is_refused(self, tmp_path: Path) -> None:
        self._skill_dir(tmp_path, "demo")
        catalog = self._catalog(tmp_path)
        with pytest.raises(SkillError, match="non-empty relative path"):
            read_skill_file(catalog, catalog.skills[0], " SKILL.md ")
