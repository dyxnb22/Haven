"""Deterministic review of what a run actually wrote.

The Evidence Gate proves that a change exists and that verification passed. It
says nothing about the *content* of the change, so a run can add an API key or
leave a `breakpoint()` behind and still look successful.

This module inspects only the lines a run added, using patterns chosen for a low
false-positive rate. It is a program judgment with no tokens and no second
model — see ADR 0007 for why that was preferred over a Reviewer agent. These are
heuristics for obvious mistakes, not a defense against a determined adversary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: A file that loses at least this fraction and this many lines looks blanked.
MASS_DELETION_RATIO = 0.8
MASS_DELETION_MIN_LINES = 50


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    code: str
    detail: str
    path: str = ""


@dataclass(frozen=True, slots=True)
class _Pattern:
    code: str
    regex: re.Pattern[str]
    detail: str


_SECRET_PATTERNS = (
    _Pattern(
        "secret_private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        "a private key block was added",
    ),
    _Pattern(
        "secret_aws_key",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        "an AWS access key id was added",
    ),
    _Pattern(
        "secret_api_token",
        re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{20,}\b"),
        "an API token was added",
    ),
    _Pattern(
        "secret_hardcoded_password",
        re.compile(
            r"""(?ix)
            \b(?:password|passwd|secret|api_key|apikey|access_token)\b
            \s*[:=]\s*
            ['"][^'"\s]{8,}['"]
            """
        ),
        "a hardcoded credential was added",
    ),
)

_CONFLICT_PATTERN = _Pattern(
    "merge_conflict_marker",
    re.compile(r"^(?:<{7}|={7}|>{7})(?:\s|$)"),
    "a merge-conflict marker was added",
)

_DEBUG_PATTERN = _Pattern(
    "debug_leftover",
    re.compile(r"\b(?:breakpoint\(\)|pdb\.set_trace\(\)|debugger;)"),
    "a debugger statement was left in the code",
)

#: Placeholder credentials that are conventional in examples and fixtures.
_PLACEHOLDER = re.compile(
    r"(?i)(x{6,}|changeme|placeholder|your[-_]?(?:key|token|password)|example\.com|<[^>]+>)"
)


def review_diff(diff_text: str) -> tuple[ReviewFinding, ...]:
    """Inspect the added lines of a unified diff.

    Only `+` lines are examined, so content that already existed in the
    repository can never produce a finding — the same "this run only"
    attribution that `repo.diff` uses.
    """
    findings: list[ReviewFinding] = []
    current_path = ""
    added: dict[str, int] = {}
    removed: dict[str, int] = {}

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("+++ "):
            current_path = _strip_prefix(raw_line[4:].strip())
            continue
        if raw_line.startswith("--- ") or raw_line.startswith("@@"):
            continue

        if raw_line.startswith("+"):
            content = raw_line[1:]
            added[current_path] = added.get(current_path, 0) + 1
            findings.extend(_scan_added_line(content, current_path))
        elif raw_line.startswith("-"):
            removed[current_path] = removed.get(current_path, 0) + 1

    findings.extend(_scan_mass_deletion(added, removed))
    return tuple(_dedupe(findings))


def _scan_added_line(content: str, path: str) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    if _CONFLICT_PATTERN.regex.search(content):
        findings.append(ReviewFinding(_CONFLICT_PATTERN.code, _CONFLICT_PATTERN.detail, path))
    if _DEBUG_PATTERN.regex.search(content):
        findings.append(ReviewFinding(_DEBUG_PATTERN.code, _DEBUG_PATTERN.detail, path))
    for pattern in _SECRET_PATTERNS:
        if pattern.regex.search(content) and not _PLACEHOLDER.search(content):
            findings.append(ReviewFinding(pattern.code, pattern.detail, path))
    return findings


def _scan_mass_deletion(added: dict[str, int], removed: dict[str, int]) -> list[ReviewFinding]:
    findings = []
    for path, deleted in removed.items():
        if deleted < MASS_DELETION_MIN_LINES:
            continue
        kept = added.get(path, 0)
        if kept / max(deleted, 1) <= 1 - MASS_DELETION_RATIO:
            findings.append(
                ReviewFinding(
                    "mass_deletion",
                    f"{deleted} lines removed and only {kept} added; the file looks blanked",
                    path,
                )
            )
    return findings


def _strip_prefix(path: str) -> str:
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def _dedupe(findings: list[ReviewFinding]) -> list[ReviewFinding]:
    seen: set[tuple[str, str]] = set()
    unique = []
    for finding in findings:
        key = (finding.code, finding.path)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique


def describe(findings: tuple[ReviewFinding, ...]) -> str:
    return "; ".join(f"{f.detail} in {f.path}" if f.path else f.detail for f in findings)
