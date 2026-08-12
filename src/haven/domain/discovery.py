"""Propose verification recipes from a project's own files.

A fresh repository has no `.haven.toml`, so the Evidence Gate has no check to
demand and every edit dead-ends at `verification_unavailable`. This module
reads the ordinary project files a human would recognise — `pyproject.toml`,
`tox.ini`, `setup.cfg`, `package.json`, `Makefile`, `Cargo.toml`, `go.mod` —
plus a shallow listing of the tree, and suggests the check command they imply.

It is pure and runs nothing. The model never supplies a command; detection is
program-driven and the user authorizes what actually becomes a recipe by
registering it. Suggestions are conservative: a signal has to be present before
a command is proposed, so the output is a short list a human can trust rather
than a guess.

The pytest suggestions were tuned against five real repositories
(docs/EVAL_LIVE.md):

- Always `python -m pytest`, never bare `pytest`: `python -m` puts the checkout
  on `sys.path`, while the bare binary quietly tested the *installed* copy of
  the same library (idna is a transitive dependency of this very project, and
  one behavioral difference produced one failing test).
- Projects with no pytest configuration anywhere still usually have a `tests/`
  or `test/` directory of `test_*.py` files (jmespath ships only `setup.py`);
  that structure is itself a signal, and the suggestion is scoped to the
  directory it came from.
- A `src/<package>/` layout is not importable from a bare checkout; pytest's
  own `pythonpath` override closes that without generated shims (tomli).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RecipeCandidate:
    id: str
    argv: tuple[str, ...]
    #: Why this was suggested, shown to the user so the proposal is auditable.
    rationale: str


@dataclass(frozen=True, slots=True)
class _TreeFacts:
    """Structure gleaned from a shallow path listing."""

    tests_dir: str | None
    src_layout: bool


def _tree_facts(paths: Iterable[str]) -> _TreeFacts:
    path_set = set(paths)
    tests_dir = None
    for candidate in ("tests", "test"):
        if any(re.fullmatch(rf"{candidate}/test_[^/]+\.py", p) for p in path_set):
            tests_dir = candidate
            break
    src_layout = any(re.fullmatch(r"src/[^/]+/__init__\.py", p) for p in path_set)
    return _TreeFacts(tests_dir=tests_dir, src_layout=src_layout)


def _pytest_candidate(files: dict[str, str], tree: _TreeFacts) -> RecipeCandidate | None:
    """One pytest suggestion, from the strongest signal present.

    A project that configures pytest itself gets an unscoped run — its config
    (testpaths, addopts, pythonpath) is the authority, and overriding it could
    break projects whose options are load-bearing. Only the structural fallback,
    where no configuration exists to respect, scopes the run and repairs a
    src layout.
    """
    pyproject = files.get("pyproject.toml", "")
    if "[tool.pytest" in pyproject:
        return RecipeCandidate(
            "pytest", ("python", "-m", "pytest", "-q"), "pyproject.toml configures pytest"
        )
    if re.search(r"['\"]pytest\b", pyproject):
        return RecipeCandidate(
            "pytest", ("python", "-m", "pytest", "-q"), "pyproject.toml depends on pytest"
        )
    if re.search(r"^\[pytest\]", files.get("tox.ini", ""), re.MULTILINE):
        return RecipeCandidate(
            "pytest", ("python", "-m", "pytest", "-q"), "tox.ini has a [pytest] section"
        )
    if re.search(r"^\[tool:pytest\]", files.get("setup.cfg", ""), re.MULTILINE):
        return RecipeCandidate(
            "pytest", ("python", "-m", "pytest", "-q"), "setup.cfg has a [tool:pytest] section"
        )
    if tree.tests_dir is not None:
        argv: tuple[str, ...] = ("python", "-m", "pytest", "-q")
        why = f"{tree.tests_dir}/ contains test_*.py files"
        if tree.src_layout:
            argv += ("-o", "pythonpath=src")
            why += " (src layout, so the checkout is put on the import path)"
        return RecipeCandidate("pytest", (*argv, tree.tests_dir), why)
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


#: filename -> detector, for the single-file ecosystems. Ordered, so the output
#: is deterministic; the pytest candidate (which weighs several files plus the
#: tree) always comes first.
_DETECTORS = (
    ("package.json", _node),
    ("Makefile", _makefile),
    ("Cargo.toml", _cargo),
    ("go.mod", _go),
)

#: Files the callers should read and pass in. Exposed so the CLI and the eval
#: harness stay in step with the detectors.
KNOWN_FILES = (
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
    "package.json",
    "Makefile",
    "Cargo.toml",
    "go.mod",
)


def discover_recipes(files: dict[str, str], paths: Iterable[str] = ()) -> list[RecipeCandidate]:
    """Suggest check recipes from project-file contents and a shallow listing.

    `files` maps the `KNOWN_FILES` present to their contents; `paths` is a
    relative-path listing (top level plus the `tests`/`test`/`src` directories
    is enough). Pure and total.
    """
    candidates: list[RecipeCandidate] = []
    pytest_candidate = _pytest_candidate(files, _tree_facts(paths))
    if pytest_candidate is not None:
        candidates.append(pytest_candidate)
    for name, detector in _DETECTORS:
        if name in files:
            candidate = detector(files[name])
            if candidate is not None:
                candidates.append(candidate)
    return candidates
