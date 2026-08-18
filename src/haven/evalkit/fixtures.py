"""评估夹具快照和检查配方构建。"""

from __future__ import annotations

import sys
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from haven.adapters.process_executor import RECIPE_SCRATCH_DIRNAME
from haven.adapters.workspace_fs import PROTECTED_COMPONENTS
from haven.contracts.tools import RecipeSpec
from haven.domain.digest import sha256_bytes
from haven.evalkit.models import RecipeDef


def is_protected(path: str) -> bool:
    return any(part in PROTECTED_COMPONENTS for part in PurePosixPath(path).parts)


def is_allowed(path: str, patterns: tuple[str, ...]) -> bool:
    return any(path == pattern or fnmatch(path, pattern) for pattern in patterns)


def snapshot(root: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts or ".pytest_cache" in path.parts or path.suffix == ".pyc":
            continue
        if any(part.endswith(".egg-info") for part in path.parts):
            continue
        if RECIPE_SCRATCH_DIRNAME in path.parts:
            continue
        if path.is_file() and not path.is_symlink():
            digests[path.relative_to(root).as_posix()] = sha256_bytes(path.read_bytes())
    return digests


def materialize_recipes(defs: dict[str, RecipeDef]) -> dict[str, RecipeSpec]:
    recipes = {}
    for recipe_id, definition in defs.items():
        argv = tuple(sys.executable if item == "{python}" else item for item in definition.argv)
        recipes[recipe_id] = RecipeSpec(
            id=recipe_id,
            argv=argv,
            timeout_seconds=definition.timeout_seconds,
            readable_roots=definition.readable_roots,
        )
    return recipes


def discovered_recipes(repo: Path) -> dict[str, RecipeSpec]:
    from haven.domain.discovery import KNOWN_FILES, discover_recipes

    files: dict[str, str] = {}
    for name in KNOWN_FILES:
        candidate = repo / name
        if candidate.is_file():
            files[name] = candidate.read_text(encoding="utf-8", errors="replace")[:65536]
    paths: list[str] = []
    for sub in ("tests", "test", "src"):
        directory = repo / sub
        if not directory.is_dir():
            continue
        for child in directory.iterdir():
            paths.append(f"{sub}/{child.name}")
            if sub == "src" and child.is_dir() and (child / "__init__.py").is_file():
                paths.append(f"src/{child.name}/__init__.py")

    recipes = {}
    for recipe_candidate in discover_recipes(files, paths):
        argv = tuple(sys.executable if item == "python" else item for item in recipe_candidate.argv)
        recipes[recipe_candidate.id] = RecipeSpec(
            id=recipe_candidate.id, argv=argv, timeout_seconds=180.0
        )
    return recipes
