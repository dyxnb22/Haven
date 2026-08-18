"""工作区初始化、配方发现和配置解释命令。"""

from __future__ import annotations

from pathlib import Path

import typer

from haven.config import ConfigError, explain, load_config
from haven.domain.discovery import RecipeCandidate
from haven.interfaces.cli_support.common import EXIT_OK, EXIT_USAGE


def discover(
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    accept: bool = typer.Option(
        False,
        "--accept",
        help="Persist the suggested recipes into .haven.toml (creates or appends), "
        "so they are usable on the next run without hand-editing.",
    ),
) -> None:
    """根据项目文件提出验证配方。"""
    from haven.domain.discovery import discover_recipes

    ws = workspace.resolve()
    files, paths = _discovery_inputs(ws)
    recipes = discover_recipes(files, paths)
    if not recipes:
        typer.echo(
            "no verification commands detected; add a [recipes] block to .haven.toml by hand"
        )
        raise typer.Exit(EXIT_OK)

    if accept:
        config_path = ws / ".haven.toml"
        added, skipped = _persist_recipes(config_path, recipes)
        for recipe_id in added:
            typer.echo(f"added [recipes.{recipe_id}] to {config_path}")
        for recipe_id in skipped:
            typer.echo(f"kept existing [recipes.{recipe_id}] (not overwritten)")
        if added:
            typer.echo("\nusable on the next run; review the file before trusting it.")
        raise typer.Exit(EXIT_OK)

    typer.echo("# Suggested recipes for .haven.toml — review, then paste what you trust:\n")
    for recipe in recipes:
        argv = ", ".join(f'"{item}"' for item in recipe.argv)
        typer.echo(f"[recipes.{recipe.id}]  # {recipe.rationale}")
        typer.echo(f"argv = [{argv}]\n")
    typer.echo("re-run with --accept to write these into .haven.toml")
    raise typer.Exit(EXIT_OK)


def _discovery_inputs(ws: Path) -> tuple[dict[str, str], list[str]]:
    """读取配方发现所需的已知项目文件和浅层目录列表。"""
    from haven.domain.discovery import KNOWN_FILES

    files: dict[str, str] = {}
    for name in KNOWN_FILES:
        candidate = ws / name
        if candidate.is_file():
            try:
                files[name] = candidate.read_text(encoding="utf-8", errors="replace")[:65536]
            except OSError:
                continue
    paths: list[str] = []
    for sub in ("tests", "test", "src"):
        directory = ws / sub
        if not directory.is_dir():
            continue
        try:
            for child in directory.iterdir():
                paths.append(f"{sub}/{child.name}")
                if sub == "src" and child.is_dir() and (child / "__init__.py").is_file():
                    paths.append(f"src/{child.name}/__init__.py")
        except OSError:
            continue
    return files, paths


def init(
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    accept: bool = typer.Option(
        False, "--accept", help="Also persist the suggested recipes into .haven.toml."
    ),
) -> None:
    """一步完成初始化：环境摘要 + 配方发现。"""
    from haven.bootstrap import sandbox_backend_name, select_launcher
    from haven.domain.discovery import discover_recipes

    ws = workspace.resolve()
    if not ws.is_dir():
        typer.echo(f"error: workspace does not exist: {ws}")
        raise typer.Exit(EXIT_USAGE)

    try:
        config = load_config(ws)
    except ConfigError as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(EXIT_USAGE) from None

    key_state = (
        "present" if config.provider.api_key() else f"missing (${config.provider.api_key_env})"
    )
    typer.echo(f"workspace:  {ws}")
    typer.echo(f"model:      {config.provider.model} @ {config.provider.base_url}")
    typer.echo(f"api key:    {key_state}")
    typer.echo(f"sandbox:    {sandbox_backend_name(select_launcher())}")
    typer.echo(f"recipes:    {len(config.recipes)} registered in .haven.toml")
    typer.echo("")

    files, paths = _discovery_inputs(ws)
    suggestions = discover_recipes(files, paths)
    fresh = [s for s in suggestions if s.id not in config.recipes]
    if not fresh:
        if config.recipes:
            typer.echo("verification is configured; nothing further to suggest.")
        else:
            typer.echo(
                "no verification commands detected; add a [recipes] block to "
                ".haven.toml by hand so the Evidence Gate has an oracle."
            )
        raise typer.Exit(EXIT_OK)

    if accept:
        added, skipped = _persist_recipes(ws / ".haven.toml", fresh)
        for recipe_id in added:
            typer.echo(f"added [recipes.{recipe_id}] to .haven.toml")
        for recipe_id in skipped:
            typer.echo(f"kept existing [recipes.{recipe_id}] (not overwritten)")
        typer.echo("\nready: `haven` opens the TUI in this workspace.")
        raise typer.Exit(EXIT_OK)

    typer.echo("suggested recipes (review, then re-run with --accept to write them):\n")
    for recipe in fresh:
        argv = ", ".join(f'"{item}"' for item in recipe.argv)
        typer.echo(f"[recipes.{recipe.id}]  # {recipe.rationale}")
        typer.echo(f"argv = [{argv}]\n")
    raise typer.Exit(EXIT_OK)


def _persist_recipes(
    config_path: Path, recipes: list[RecipeCandidate]
) -> tuple[list[str], list[str]]:
    """追加新配方且不覆盖同 id 的现有配置。"""
    existing = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    added: list[str] = []
    skipped: list[str] = []
    blocks: list[str] = []
    for recipe in recipes:
        if f"[recipes.{recipe.id}]" in existing:
            skipped.append(recipe.id)
            continue
        argv = ", ".join(f'"{item}"' for item in recipe.argv)
        blocks.append(f"[recipes.{recipe.id}]  # {recipe.rationale}\nargv = [{argv}]\n")
        added.append(recipe.id)
    if blocks:
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        header = "" if existing else "# Written by `haven discover --accept`.\n"
        config_path.write_text(existing + prefix + header + "\n".join(blocks), encoding="utf-8")
    return added, skipped


def config_explain(
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
    action: str = typer.Argument("explain", help="Only 'explain' is supported."),
) -> None:
    """显示每个解析后的配置值及其来源。"""
    from haven.bootstrap import sandbox_backend_name, select_launcher

    if action != "explain":
        typer.echo("error: only `haven config explain` is supported")
        raise typer.Exit(EXIT_USAGE)
    try:
        config = load_config(workspace.resolve())
    except ConfigError as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(EXIT_USAGE) from None
    for key, value, source in explain(config, sandbox_backend_name(select_launcher())):
        typer.echo(f"{key:<32} {value:<48} [{source}]")
