"""`haven skills`：发现、脚手架、体检与检查技能（ADR 0031、0032、0034）。"""

from __future__ import annotations

from pathlib import Path

import typer

from haven.application.skills import (
    MAX_DESCRIPTION_CHARS,
    Skill,
    SkillError,
    discover_skills,
    list_skill_files,
    scaffold_skill,
)
from haven.interfaces.cli_support.common import EXIT_POLICY, EXIT_USAGE


def skills(
    action: str = typer.Argument("list", help="Action: list, init, info, validate."),
    name: str | None = typer.Argument(None, help="Skill name (required for init, info)."),
    workspace: Path = typer.Option(Path("."), "--workspace", "-w", help="Workspace root."),
    scope: str = typer.Option(
        "project", "--scope", "-s", help="Skill scope ('project' or 'user')."
    ),
    description: str = typer.Option(
        "A new skill", "--description", "-d", help="Skill description."
    ),
    tools: str = typer.Option("", "--tools", "-t", help="Comma-separated allowed-tools."),
) -> None:
    """管理与检查技能：列出已发现技能、创建脚手架、查看技能详情与静态体检。"""
    from haven.config import user_skills_dir

    root = workspace.resolve()
    if action == "list":
        catalog = discover_skills(user_root=user_skills_dir(), project_root=root)
        typer.echo(f"skill catalog ({len(catalog.skills)} discovered)")
        typer.echo(f"  user root:    {user_skills_dir()}")
        typer.echo(f"  project root: {root / '.haven' / 'skills'}")
        typer.echo("")
        if not catalog.skills:
            typer.echo("  (none discovered)")
        for skill in catalog.skills:
            typer.echo(f"- {skill.name} [{skill.scope}] {skill.digest}")
            typer.echo(f"    {skill.source}")
            typer.echo(f"    {skill.description[:MAX_DESCRIPTION_CHARS]}")
            if skill.granted_tools:
                typer.echo(f"    standing approval requested: {', '.join(skill.granted_tools)}")
        if catalog.refused:
            typer.echo("")
            typer.echo(f"{len(catalog.refused)} skill file(s) refused:")
            for refusal in catalog.refused:
                typer.echo(f"  {refusal.source}: {refusal.reason}")
        return

    if action == "init":
        if not name:
            typer.echo(
                "error: `haven skills init` requires a skill name, e.g. `haven skills init foo`"
            )
            raise typer.Exit(EXIT_USAGE)
        if scope not in {"project", "user"}:
            typer.echo(f"error: invalid scope {scope!r}; must be 'project' or 'user'")
            raise typer.Exit(EXIT_USAGE)
        allowed_tools = tuple(t.strip() for t in tools.split(",") if t.strip())
        target_root = root / ".haven" / "skills" if scope == "project" else user_skills_dir()
        try:
            path = scaffold_skill(
                root=target_root,
                name=name,
                description=description,
                allowed_tools=allowed_tools,
            )
        except SkillError as exc:
            typer.echo(f"error: {exc}")
            raise typer.Exit(EXIT_USAGE) from exc
        typer.echo(f"created skill '{name}' at {path}")
        typer.echo(f"  scope: {scope}")
        if allowed_tools:
            typer.echo(f"  standing approval requested: {', '.join(allowed_tools)}")
        return

    if action == "info":
        if not name:
            typer.echo(
                "error: `haven skills info` requires a skill name, e.g. `haven skills info foo`"
            )
            raise typer.Exit(EXIT_USAGE)
        catalog = discover_skills(user_root=user_skills_dir(), project_root=root)
        target_skill: Skill | None = next((s for s in catalog.skills if s.name == name), None)
        if target_skill is None:
            refusal_match = next((r for r in catalog.refused if f"/{name}/" in r.source), None)
            if refusal_match:
                typer.echo(f"skill '{name}' was refused:")
                typer.echo(f"  source: {refusal_match.source}")
                typer.echo(f"  reason: {refusal_match.reason}")
                raise typer.Exit(EXIT_POLICY)
            available = ", ".join(catalog.names()) or "(none)"
            typer.echo(f"error: no discovered skill named '{name}'; available: {available}")
            raise typer.Exit(EXIT_USAGE)
        companion_files = list_skill_files(catalog, target_skill)
        typer.echo(f"skill: {target_skill.name} [{target_skill.scope}]")
        typer.echo(f"source: {target_skill.source}")
        typer.echo(f"digest: {target_skill.digest}")
        typer.echo(f"description: {target_skill.description}")
        if target_skill.granted_tools:
            typer.echo(f"standing approval: {', '.join(target_skill.granted_tools)}")
        else:
            typer.echo("standing approval: (none)")
        if companion_files:
            typer.echo(f"companion files: {', '.join(companion_files)}")
        else:
            typer.echo("companion files: (none)")
        typer.echo("")
        typer.echo("body:")
        typer.echo(target_skill.body)
        return

    if action in {"validate", "check"}:
        catalog = discover_skills(user_root=user_skills_dir(), project_root=root)
        typer.echo(
            f"validating skills ({len(catalog.skills)} valid, {len(catalog.refused)} refused)"
        )
        typer.echo(f"  user root:    {user_skills_dir()}")
        typer.echo(f"  project root: {root / '.haven' / 'skills'}")
        typer.echo("")
        for skill in catalog.skills:
            companions = list_skill_files(catalog, skill)
            comp_str = f", {len(companions)} companion file(s)" if companions else ""
            tools_str = (
                f", allowed-tools: {', '.join(skill.granted_tools)}" if skill.granted_tools else ""
            )
            typer.echo(
                f"✓ {skill.name} [{skill.scope}]: ok (digest {skill.digest}{tools_str}{comp_str})"
            )
        if catalog.refused:
            typer.echo("")
            for refusal in catalog.refused:
                typer.echo(f"✗ {refusal.source}: {refusal.reason}")
            typer.echo(f"\nvalidation failed: {len(catalog.refused)} skill(s) refused")
            raise typer.Exit(EXIT_POLICY)
        typer.echo(f"\nall {len(catalog.skills)} skill(s) valid")
        return

    typer.echo(f"error: unknown action {action!r}; supported actions: list, init, info, validate")
    raise typer.Exit(EXIT_USAGE)
