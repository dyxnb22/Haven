"""技能：声明式提示片段的发现、失败即关闭解析、目录渲染与选择解析。

技能是上下文，不是能力。它可以描述"如何工作"，但不能授予权限：一个脚本、
一条命令或一个路径被技能提到，并不会因此变成受信工具（ADR 0031）。因此这里
只做三件事——以确定性顺序发现技能、拒绝任何不符合契约的文件、以及把目录渲染
成有界文本。

技能的正文只有用户显式选择后才会进入上下文，并且与 AGENTS.md 走完全相同的
不可信框架：user 角色、显式的 UNTRUSTED DATA 标签、`<tool_output>` 包裹，
绝不进入 system 角色（ADR 0031，先例见 `context_builder` 的 project_guidance）。

与 `bootstrap._read_guidance` 不同，这里是同步的：用户级根目录在工作区之外，
`WorkspacePort` 既到不了它，也会因为 `.haven` 是 PROTECTED_COMPONENT 而拒绝
项目级路径（`adapters/workspace_fs.py`）。直接读文件系统与
`config._read_toml`、`bootstrap._scoped_guidance_paths` 的做法一致。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from haven.domain.digest import sha256_text
from haven.domain.policy import KNOWN_TOOLS

SkillScope = Literal["user", "project"]

#: 目录中最多列出的技能数。设置上限是为了避免大型 monorepo 撑爆固定头部，
#: 与 `bootstrap._MAX_SCOPED_GUIDANCE` 同理由。
MAX_SKILLS = 12
#: 目录中单条 description 的截断长度：目录是索引，不是正文。
MAX_DESCRIPTION_CHARS = 200
#: 渲染后目录的总字符上限。目录每一轮都在头部，因此这段开销是每轮都付的。
MAX_CATALOG_CHARS = 1_500
#: `render_catalog` 为结尾那句"还有 N 个没列出"预留的字符数。有界性本身不能因为
#: 省字符被挤掉，所以重排时要先把它的位置占住；预留值只需大于那句的实际长度
#: （`... and N more skill(s) not listed (catalog is bounded)`，N 有 9 位数字也放得下）。
CATALOG_NOTE_RESERVE = 64
#: 单个被选中技能正文的截断长度。
MAX_BODY_CHARS = 4_000
#: `skill.read` 读取单个辅助文件的上限。它与 `MODEL_PAYLOAD_CHARS` 同量级：一个
#: 参考文件再大也不该挤掉 transcript。
MAX_SKILL_FILE_CHARS = 8_000
#: 一次运行最多可选中几个技能。这是第五个边界：没有它，`--skill` 重复足够多次
#: 就会让 `ContextBuilder.build` 触发自身的超预算保护，也就是用户的输入直接把
#: 一次运行打成 RuntimeError。
MAX_SELECTED_SKILLS = 3
#: 每个根目录最多检查多少个候选文件。`MAX_SKILLS` 限制的是"接受多少个"，不限制
#: "读多少个"：没有这一条，一个放着上万个技能目录的仓库会让每次启动都去读遍
#: 所有 SKILL.md。超出部分记为一条拒绝，因此截断在 `haven doctor` 里可见。
MAX_SCANNED_PER_ROOT = 4 * MAX_SKILLS

SKILL_FILENAME = "SKILL.md"

#: 技能名同时是目录名和用户在 `--skill` / `/skill use` 里输入的名字，因此它必须
#: 是一个可以安全嵌入提示词和错误消息的短标识符。
_NAME_PATTERN = re.compile(r"\A[a-z0-9][a-z0-9-]{0,63}\Z")


class SkillError(Exception):
    """一个技能文件被拒绝：解析失败、键未知、名称与目录不符等。

    失败即关闭：宁可少一个技能，也不要一个半加载的技能。调用方把原因记录在
    `SkillRefusal` 里交给 `haven doctor`，而不是静默丢弃。
    """


class SkillSelectionError(ValueError):
    """用户请求的选择无效：名称未知，或超过 `MAX_SELECTED_SKILLS`。

    继承 ValueError 与 `continue_` 既有的用法一致，但保持独立类型，使 CLI 能把
    它映射为 EXIT_USAGE，而不是吞掉真正的 bug。
    """


@dataclass(frozen=True, slots=True)
class Skill:
    """一个已加载的技能。`digest` 是正文摘要，用于让模型和追踪都能验证它看到
    的正文确实对应目录里的那一条。"""

    #: 目录名，也是用户在 `--skill` / `/skill use` 里输入的名字。
    name: str
    #: frontmatter 中的 description（渲染时按 `MAX_DESCRIPTION_CHARS` 截断）。
    description: str
    #: 用户级还是项目级；项目级与 AGENTS.md 同等地不可信。
    scope: SkillScope
    #: 发现该文件的相对位置，例如 `skills/foo/SKILL.md` 或
    #: `.haven/skills/foo/SKILL.md`。绝不包含绝对路径，以免把 $HOME 写进提示词。
    source: str
    #: 正文的 SHA-256 前 12 位十六进制。只覆盖 `body` 里实际存下的那些字节
    #: （即截断后的那一份），因此它永远可以被验证：`haven skills info` 把二者
    #: 并排打印，模型和追踪都能确认"看到的正文确实是这一条"。
    digest: str
    #: 正文原文（加载时按 `MAX_BODY_CHARS` 截断）。
    body: str
    #: 该技能请求常驻授权的工具名（ADR 0032）。它只是"有资格免审"，本身不授予
    #: 任何权限：授权是否生效由程序和人类决定，技能文本无法自行扩大它。
    granted_tools: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SkillRefusal:
    """一个被拒绝的技能及其原因，供 `haven doctor` 诚实呈现失败即关闭。"""

    #: 被拒绝文件的相对位置，与 `Skill.source` 同格式。
    source: str
    #: 人类可读的拒绝原因（来自 `SkillError` 或读取失败）。
    reason: str


@dataclass(frozen=True, slots=True)
class SkillCatalog:
    """一次发现的结果：可用技能（确定性顺序）加上被拒绝的文件。"""

    #: 可加载的技能，用户级优先，其次按名称排序。
    skills: tuple[Skill, ...] = ()
    #: 被拒绝的文件及原因；空表示一切正常。
    refused: tuple[SkillRefusal, ...] = field(default=())
    #: 用户级技能根目录的绝对路径。`skill.read` 需要它来读辅助文件，而 `Skill`
    #: 本身绝不携带绝对路径（那会把 $HOME 写进提示词）。
    user_root: Path | None = None
    #: 项目级技能根目录（`<workspace>/.haven/skills`）的绝对路径。
    project_root: Path | None = None

    def names(self) -> tuple[str, ...]:
        """按目录顺序返回可用技能名。"""
        return tuple(skill.name for skill in self.skills)

    def root_for(self, scope: SkillScope) -> Path | None:
        """返回某个作用域对应的绝对根目录；未提供时为 None。"""
        return self.user_root if scope == "user" else self.project_root


def parse_skill(text: str, *, name: str, scope: SkillScope, source: str) -> Skill:
    """解析一个 SKILL.md；任何不符合契约之处都抛 `SkillError`。

    失败即关闭：绝不返回部分加载的技能。未知键、缺 description、名称与目录名
    不符、字符集非法、缺 frontmatter 定界符、重复键、无冒号的行、空正文、
    `allowed-tools` 里出现未注册的工具名——全部拒绝。项目没有 YAML 依赖（见
    pyproject 的 dependencies），因此解析器是手写的，只认三个键。
    """
    if not text.startswith("---\n"):
        raise SkillError("missing opening '---' frontmatter delimiter")
    rest = text[4:]
    end = rest.find("\n---")
    if end < 0:
        raise SkillError("missing closing '---' frontmatter delimiter")
    fields: dict[str, str] = {}
    for line in rest[:end].splitlines():
        if not line.strip():
            raise SkillError(f"blank line inside frontmatter: {line!r}")
        key, separator, value = line.partition(":")
        if not separator:
            raise SkillError(f"frontmatter line is not 'key: value': {line!r}")
        key = key.strip()
        if key not in {"name", "description", "allowed-tools"}:
            raise SkillError(f"unknown frontmatter key {key!r}")
        if key in fields:
            raise SkillError(f"duplicate frontmatter key {key!r}")
        fields[key] = value.strip()
    body = rest[end + 4 :].strip()
    if not fields.get("description"):
        raise SkillError("missing 'description'")
    # 先校验字符集再比较：目录名是文件系统给的值，先确认它能作为标识符安全
    # 嵌入提示词和错误消息，再去问它和 frontmatter 声明是否一致。反过来会在一
    # 个非法目录名上报出"不匹配"，把真正的问题盖掉。
    if not _NAME_PATTERN.match(name):
        raise SkillError(f"invalid skill name {name!r}")
    declared = fields.get("name", "")
    if not declared:
        raise SkillError("missing 'name'")
    if declared != name:
        raise SkillError(f"name {declared!r} does not match directory {name!r}")
    if not body:
        raise SkillError("empty skill body")
    # 摘要只覆盖**会被存下来、会被展示出去**的那些字节。此前它算的是完整正文，
    # 而 body 字段存的是截断后的那一份：正文一旦超过 MAX_BODY_CHARS，digest 说的
    # 就是没人见过的字节，`haven skills info` 把 digest 和 body 并排打印时二者对不上，
    # 模型和追踪也就无法验证"我看到的正文确实是这一条"——那正是这个字段唯一的用途。
    visible = body[:MAX_BODY_CHARS]
    return Skill(
        name=name,
        description=fields["description"],
        scope=scope,
        source=source,
        digest=sha256_text(visible)[:12],
        body=visible,
        granted_tools=parse_allowed_tools(fields.get("allowed-tools", "")),
    )


def parse_allowed_tools(raw: str) -> tuple[str, ...]:
    """解析 `allowed-tools` 的值；任何未注册的工具名都让整个技能被拒绝。

    接受两种写法，因为项目没有 YAML 依赖，而用户会自然地两种都写：
    `allowed-tools: repo.edit, repo.check` 和 `allowed-tools: [repo.edit, repo.check]`。

    排序并去重：授权键由这个元组参与计算，因此同一个文件必须永远产出同一个
    元组，否则技能内容没变也会丢失授权。
    """
    inner = raw.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    names = {item.strip() for item in inner.split(",") if item.strip()}
    if not names:
        return ()
    unknown = sorted(names - KNOWN_TOOLS)
    if unknown:
        raise SkillError(f"allowed-tools names an unregistered tool: {unknown[0]!r}")
    return tuple(sorted(names))


def _first_symlinked_component(root: Path, *parts: str) -> str | None:
    """从 `root` 起逐层往下走，返回第一个是符号链接的组件名；没有则返回 None。

    必须逐层而不能只看终点：`Path.is_symlink()` 只回答"最后这一层是不是链接"，
    看不见 `skills/evil/SKILL.md` 里那个指向 `~/.ssh` 的 `evil` 目录——而
    `glob` 会跟着链接走进去，于是技能根之外的 SKILL.md 就被收进了提示词，
    边界却以为自己拦住了什么。
    """
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            return part
    return None


def _is_within(base: Path, candidate: Path) -> bool:
    """`candidate` 是 `base` 自身或它的后代吗。

    用父目录链做组件级比较，而不是字符串前缀：`str.startswith` 会把
    `/skills/foo` 和 `/skills/foobar` 判成包含关系，而那是两个不同的技能目录。
    """
    return candidate == base or base in candidate.parents


def discover_skills(*, user_root: Path, project_root: Path) -> SkillCatalog:
    """从两个根目录发现技能：用户级优先，然后是项目级。

    两个根都可以不存在——缺失即空目录，因此这里不急着 mkdir。项目级根取
    `<project_root>/.haven/skills`，与 `.haven.toml` 同属 Haven 自己的项目级
    配置区，也因此整个 `.haven` 是 PROTECTED_COMPONENT：模型无法改写技能文件
    （ADR 0031）。

    同名冲突时拒绝项目级那条：用户级优先，且选择必须无歧义——静默选一个会让
    "我加载的到底是哪一条"变得无法从轨迹回答。
    """
    accepted: list[Skill] = []
    refused: list[SkillRefusal] = []
    seen: set[str] = set()
    roots: tuple[tuple[SkillScope, Path, str], ...] = (
        ("user", user_root, "skills"),
        ("project", project_root / ".haven" / "skills", ".haven/skills"),
    )
    for scope, root, prefix in roots:
        if not root.is_dir():
            continue
        candidates = sorted(root.glob(f"*/{SKILL_FILENAME}"))
        for path in candidates[:MAX_SCANNED_PER_ROOT]:
            name = path.parent.name
            source = f"{prefix}/{name}/{SKILL_FILENAME}"
            # 与 `FsWorkspace.path_facts` 拒绝符号链接同理由：一个指向
            # ~/.ssh/id_rsa 的 SKILL.md 会让 Haven 把工作区之外的敏感文件
            # 读进提示词，而这条边界本应是"只读技能目录里的普通文件"。
            #
            # 必须逐组件走查，不能只看最后一层：`Path.is_symlink()` 只回答
            # "SKILL.md 是不是链接"，看不见 `skills/evil/SKILL.md` 里那个指向
            # 外部的 `evil` 目录——而 `glob("*/SKILL.md")` 确实会跟着它走进去
            # （已实证）。只查最后一层时，一个在 `git status` 里完全不可见的
            # 链接就能把技能根之外的正文、连同它 frontmatter 里的
            # `allowed-tools` 常驻授权，一起送进这次运行。
            linked = _first_symlinked_component(root, name, SKILL_FILENAME)
            if linked is not None:
                if linked == SKILL_FILENAME:
                    reason = "refused: skill file is a symlink"
                else:
                    reason = f"refused: skill directory {linked!r} is a symlink"
                refused.append(SkillRefusal(source, reason))
                continue
            try:
                skill = parse_skill(
                    path.read_text(encoding="utf-8"),
                    name=name,
                    scope=scope,
                    source=source,
                )
            except (OSError, UnicodeDecodeError) as exc:
                refused.append(SkillRefusal(source, f"could not read: {exc}"))
                continue
            except SkillError as exc:
                refused.append(SkillRefusal(source, str(exc)))
                continue
            if skill.name in seen:
                refused.append(SkillRefusal(source, "shadowed by the user skill of the same name"))
                continue
            seen.add(skill.name)
            accepted.append(skill)
        skipped = len(candidates) - MAX_SCANNED_PER_ROOT
        if skipped > 0:
            refused.append(
                SkillRefusal(f"{prefix}/", f"{skipped} more skill(s) not scanned (bounded scan)")
            )
    # 与上面"扫描有界"是同一条原则：截断必须自己说清楚。此前这里是
    # `accepted[:MAX_SKILLS]`，多出来的技能无声消失——一个装了几十个技能的仓库在
    # `haven skills list` 里看起来只装了 12 个，而 `haven skills info <被丢的那个>`
    # 只会回答"没有这个技能"，用户无从区分它是被拒绝了、还是根本没被发现。
    for dropped in accepted[MAX_SKILLS:]:
        refused.append(
            SkillRefusal(dropped.source, f"not listed (catalog holds at most {MAX_SKILLS})")
        )
    return SkillCatalog(
        tuple(accepted[:MAX_SKILLS]),
        tuple(refused),
        user_root=user_root,
        project_root=project_root / ".haven" / "skills",
    )


def read_skill_file(catalog: SkillCatalog, skill: Skill, relative: str) -> tuple[str, bool]:
    """读取某个技能目录下的一个文件，供 `skill.read` 使用（ADR 0032）。

    这是 Haven 唯一允许模型触及工作区之外字节的路径，因此边界写在这里而不是
    靠调用方自觉：

    - 只接受技能自己目录内的相对路径——绝对路径、`..`、以及指向另一个技能目录
      的路径全部拒绝，所以一个技能读不到另一个技能的文件；
    - 拒绝符号链接，与 `discover_skills` 同理由：一个指向 `~/.ssh/id_rsa` 的
      `reference.md` 会把工作区之外的秘密读进提示词。检查覆盖技能目录自身和
      路径上的每一层中间组件——`resolve()` 和 `read_text` 都会跟着链接走，
      而 `target.is_symlink()` 只看得见最后一层；
    - 按 `MAX_SKILL_FILE_CHARS` 截断，因此一个巨大的参考文件不会挤爆上下文。

    失败即关闭：任何不符合之处都抛 `SkillError`，由工具通道转成结构化的
    `ToolResult`，而不是让异常逃到模型面前。
    """
    if relative != relative.strip() or not relative:
        raise SkillError("skill file path must be a non-empty relative path")
    if relative.startswith("/") or Path(relative).is_absolute():
        raise SkillError("skill file path must be relative to the skill directory")
    parts = Path(relative).parts
    if any(part in {"..", ".", ""} for part in parts):
        raise SkillError("skill file path must not traverse outside the skill directory")
    root = catalog.root_for(skill.scope)
    if root is None:
        raise SkillError(f"no {skill.scope} skill root is configured")
    base = root / skill.name
    # 技能目录自身是链接，意味着这个"技能"根本不在技能根下面：它指向哪里，
    # `skill.read` 就能读哪里。`discover_skills` 和 `list_skill_files` 都拒绝这种
    # 情况，这里是第三道——catalog 是上一次发现的快照，目录此后可能已经变了。
    if base.is_symlink():
        raise SkillError(f"refused: skill directory {base.name!r} is a symlink")
    target = base / relative
    # 先查符号链接再做包含性检查：`resolve()` 会跟随链接，因此反过来会让一个
    # 指向外部的链接报成"越界"，把真正的问题盖掉——而"这是个链接"才是作者
    # 需要听到的那句话。逐组件，因为 `reference/links/x.md` 里那个中间的链接
    # 目录同样是一道出口，`target.is_symlink()` 看不见它。
    linked = _first_symlinked_component(base, *parts)
    if linked is not None:
        if linked == parts[-1]:
            raise SkillError("refused: skill file is a symlink")
        raise SkillError(f"refused: skill directory {linked!r} is a symlink")
    # 边界用父目录链比较而不是字符串前缀：`str.startswith` 会把
    # `<root>/foo` 和 `<root>/foobar` 判成包含关系，而那是两个不同的技能目录。
    if not _is_within(base.resolve(), target.resolve()):
        raise SkillError("skill file path escapes the skill directory")
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillError(f"could not read skill file: {exc}") from exc
    return text[:MAX_SKILL_FILE_CHARS], len(text) > MAX_SKILL_FILE_CHARS


def list_skill_files(catalog: SkillCatalog, skill: Skill) -> tuple[str, ...]:
    """列出某个技能目录下的辅助文件（相对路径，排除 SKILL.md、隐藏文件和符号链接）。

    这让模型和用户能一目了然地知道技能目录下还有哪些可供读取的参考材料（ADR 0034）。
    """
    root = catalog.root_for(skill.scope)
    if root is None:
        return ()
    skill_dir = root / skill.name
    if not skill_dir.is_dir() or skill_dir.is_symlink():
        return ()
    files: list[str] = []
    for p in sorted(skill_dir.rglob("*")):
        if p.is_file() and not p.is_symlink():
            rel = p.relative_to(skill_dir).as_posix()
            if rel == SKILL_FILENAME or any(part.startswith(".") for part in Path(rel).parts):
                continue
            files.append(rel)
    return tuple(files)


def scaffold_skill(
    *,
    root: Path,
    name: str,
    description: str = "A new skill",
    allowed_tools: Sequence[str] = (),
) -> Path:
    """在指定根目录下生成一个符合规范的技能脚手架（ADR 0034）。

    自动写入合法 YAML Frontmatter 与规范章节骨架，校验命名字符集与工具名合法性。
    """
    if not _NAME_PATTERN.match(name):
        raise SkillError(
            f"invalid skill name {name!r}: must match lowercase alphanumeric and hyphens"
        )
    for tool in allowed_tools:
        if tool not in KNOWN_TOOLS:
            raise SkillError(f"unregistered tool in allowed-tools: {tool!r}")
    # 脚手架写出的文件必须能通过它自己的 `parse_skill`，否则用户刚看到
    # "created skill"，转头就在 `haven skills validate` 里看到同一个文件被拒。
    #
    # frontmatter 是按行解析的（`parse_skill` 里 `rest[:end].splitlines()`），而
    # `str.splitlines()` 认定的行边界远不止 `\n`：`\r`、`\v`、`\f`、`\x1c`-`\x1e`、
    # `\x85`、U+2028、U+2029 都算。任何一个出现在 description 里，它就会变成第二条
    # frontmatter 行，于是文件带着 "frontmatter line is not 'key: value'" 被拒。
    # 这里用"拆开后必须正好还原成原串"来表达那条约束——比列举字符集更贴合成因。
    lines = description.splitlines()
    if not description.strip() or len(lines) != 1 or lines[0] != description:
        raise SkillError(f"description must be a single non-empty line; got {description!r}")
    skill_dir = root / name
    skill_file = skill_dir / SKILL_FILENAME
    if skill_file.exists():
        raise SkillError(f"skill {name!r} already exists at {skill_file}")
    skill_dir.mkdir(parents=True, exist_ok=True)

    tools_line = f"allowed-tools: {', '.join(allowed_tools)}\n" if allowed_tools else ""
    content = f"""---
name: {name}
description: {description}
{tools_line}---

# {name}

## When to use
<!-- Describe the triggers and scenarios where this skill should be applied -->

## Guidelines
<!-- Step-by-step guidance, rules, and best practices -->

## Examples
<!-- Code examples, common patterns, or verification recipes -->
"""
    skill_file.write_text(content, encoding="utf-8")
    return skill_file


def _layout_catalog(header: str, entries: tuple[Skill, ...], budget: int) -> list[str]:
    """在 `budget` 字符内尽量多地排下目录条目；返回含表头的所有行。"""
    lines = [header]
    used = len(header) + 1
    for skill in entries:
        description = skill.description[:MAX_DESCRIPTION_CHARS]
        entry = f"- {skill.name} [{skill.scope}] {skill.digest} — {description}"
        if skill.granted_tools:
            # 授权是程序事实，不是技能的自我声明：这句话由 Haven 渲染，因此模型
            # 读到的是"确实如此"，而不是"某段文本说它应该如此"。
            entry += f" (standing approval granted for this run: {', '.join(skill.granted_tools)})"
        if used + len(entry) + 1 > budget:
            break
        lines.append(entry)
        used += len(entry) + 1
    return lines


def render_catalog(skills: Iterable[Skill]) -> str:
    """把目录渲染成一段有界的英文文本，供 `skill_catalog` 片段使用。

    目录始终存在——正是它让渐进披露成立；只有正文需要用户显式选择。因此这段
    文本必须自己说清楚"这里只是索引，加载哪个由用户决定"。
    """
    header = (
        "Skill catalog (advisory prompt fragments; a skill is NOT a tool and grants no authority):"
    )
    entries = tuple(skills)
    lines = _layout_catalog(header, entries, MAX_CATALOG_CHARS)
    if len(lines) - 1 < len(entries):
        # 有界的索引必须自己说清楚它是有界的，理由与 `discover_skills` 里"扫描有界"
        # 完全相同：一个静默截断的目录会让模型和用户都认为没列出的技能不存在，于是
        # `--skill <那个技能>` 拿到一句"unknown skill"——一个纯粹由呈现方式造成的死胡同。
        # 实测 1500 字符只列得出 12 个技能中的 6 个左右，所以这条路经常走到。
        #
        # 先按扣掉预留值的预算重排，再按**重排后实际列出的条数**数那句话里的 N：
        # 若拿第一遍的数字，重排少列一个时那句话就会多数一个，数字对不上。
        lines = _layout_catalog(header, entries, MAX_CATALOG_CHARS - CATALOG_NOTE_RESERVE)
        omitted = len(entries) - (len(lines) - 1)
        lines.append(f"... and {omitted} more skill(s) not listed (catalog is bounded)")
    return "\n".join(lines)


def resolve_selected_skills(skills: Sequence[Skill], names: Sequence[str]) -> tuple[Skill, ...]:
    """把用户给出的名字解析成技能；未知、超量都失败，绝不静默忽略。

    接受技能序列而不是 `SkillCatalog`：调用方手里通常只有可用技能（`RunService`
    存的就是它），而拒绝清单对解析毫无用处。

    返回顺序按目录顺序（用户级优先，其次按名），而不是按用户输入顺序：这样
    同样的选择在不同次运行里产生逐字节相同的头部，正是 ADR 0008 要的形状。
    """
    wanted = list(dict.fromkeys(name.strip() for name in names if name.strip()))
    if not wanted:
        return ()
    if len(wanted) > MAX_SELECTED_SKILLS:
        raise SkillSelectionError(
            f"at most {MAX_SELECTED_SKILLS} skills may be selected per run; got {len(wanted)}"
        )
    available = {skill.name for skill in skills}
    unknown = [name for name in wanted if name not in available]
    if unknown:
        listed = ", ".join(sorted(available)) or "(none discovered)"
        raise SkillSelectionError(f"unknown skill {unknown[0]!r}; available: {listed}")
    chosen = set(wanted)
    return tuple(skill for skill in skills if skill.name in chosen)
