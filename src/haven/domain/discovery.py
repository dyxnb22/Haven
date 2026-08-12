"""Propose verification recipes from a project's own files.

A fresh repository has no `.haven.toml`, so the Evidence Gate has no check to
demand and every edit dead-ends at `verification_unavailable`. This module
reads the ordinary project files a human would recognise — `pyproject.toml`,
`package.json`, `Makefile`, `Cargo.toml`, `go.mod` — and suggests the check
command each implies.

It is pure and runs nothing. The model never supplies a command; detection is
program-driven and the user authorizes what actually becomes a recipe by
registering it. Suggestions are conservative: a signal has to be present
(pytest configured or depended on, a `test` script, a `test:` target) before a
command is proposed, so the output is a short list a human can trust rather
than a guess.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RecipeCandidate:
    id: str
    argv: tuple[str, ...]
    #: Why this was suggested, shown to the user so the proposal is auditable.
    rationale: str


def _python(content: str) -> RecipeCandidate | None:
    has_pytest_config = "[tool.pytest" in content
    depends_on_pytest = bool(re.search(r"['\"]pytest\b", content))
    if has_pytest_config or depends_on_pytest:
        why = (
            "pyproject.toml configures pytest"
            if has_pytest_config
            else "pyproject.toml depends on pytest"
        )
        return RecipeCandidate("pytest", ("pytest", "-q"), why)
    return None


def _node(content: str) -> RecipeCandidate | None:
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return None
    scripts = parsed.get("scripts") if isinstance(parsed, dict) else None
    if isinstance(scripts, dict) and "test" in scripts:
        return RecipeCandidate("npm-test", ("npm", "test"), "package.json defines a test script")
    return None


def _makefile(content: str) -> RecipeCandidate | None:
    # A `test:` target at the start of a line, the usual Make convention.
    if re.search(r"^test:", content, re.MULTILINE):
        return RecipeCandidate("make-test", ("make", "test"), "Makefile has a test target")
    return None


def _cargo(_content: str) -> RecipeCandidate | None:
    return RecipeCandidate("cargo-test", ("cargo", "test"), "Cargo.toml present")


def _go(_content: str) -> RecipeCandidate | None:
    return RecipeCandidate("go-test", ("go", "test", "./..."), "go.mod present")


#: filename -> detector. Ordered, so the output is deterministic.
_DETECTORS = (
    ("pyproject.toml", _python),
    ("package.json", _node),
    ("Makefile", _makefile),
    ("Cargo.toml", _cargo),
    ("go.mod", _go),
)


def discover_recipes(files: dict[str, str]) -> list[RecipeCandidate]:
    """Suggest check recipes from project-file contents. Pure and total."""
    candidates: list[RecipeCandidate] = []
    for name, detector in _DETECTORS:
        if name in files:
            candidate = detector(files[name])
            if candidate is not None:
                candidates.append(candidate)
    return candidates
