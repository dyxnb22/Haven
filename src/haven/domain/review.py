"""对运行实际写入内容进行确定性审查。

Evidence Gate 证明变更存在且验证通过，但不判断变更的*内容*，因此运行即使加入
API 密钥或留下 `breakpoint()`，看起来仍可能是成功的。

本模块只检查运行新增的行，并使用经过选择、误报率较低的模式。这是无需 token、
也不调用第二个模型的程序判断——为何选择它而不是 Reviewer 代理，见 ADR 0007。
这些只是针对明显错误的启发式检查，不是抵御蓄意攻击者的防线。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 文件同时丢失至少这一比例和这么多行时，通常可视为空文件化结果。
MASS_DELETION_RATIO = 0.8
MASS_DELETION_MIN_LINES = 50


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    """差异审查发现的一个可疑模式及其严重程度。"""

    #: Evidence Gate 和报告使用的稳定发现码。
    code: str
    #: 对可疑新增内容的人类可读解释。
    detail: str
    #: 文件级发现对应的工作区相对路径；非文件级发现时为空。
    path: str = ""


@dataclass(frozen=True, slots=True)
class _Pattern:
    #: 此模式匹配时发出的稳定代码。
    code: str
    #: 应用于差异新增行的已编译表达式。
    regex: re.Pattern[str]
    #: ReviewFinding 使用的人类可读描述。
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

#: 示例和测试夹具中常见的占位凭据。
_PLACEHOLDER = re.compile(
    r"(?i)(x{6,}|changeme|placeholder|your[-_]?(?:key|token|password)|example\.com|<[^>]+>)"
)


def review_diff(diff_text: str) -> tuple[ReviewFinding, ...]:
    """检查统一差异中新增的行。

    只检查 `+` 行，因此仓库中原本就存在的内容永远不会产生发现——这与
    `repo.diff` 使用的是同一套“仅归属于本次运行”的归因规则。
    """
    findings: list[ReviewFinding] = []
    current_path = ""
    old_path = ""
    added: dict[str, int] = {}
    removed: dict[str, int] = {}

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("--- "):
            old_path = _strip_prefix(raw_line[4:].strip())
            continue
        if raw_line.startswith("+++ "):
            new_path = _strip_prefix(raw_line[4:].strip())
            current_path = old_path if new_path == "/dev/null" else new_path
            continue
        if raw_line.startswith("@@"):
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
    """将审查发现拼接为适合门禁和 UI 展示的单行诊断。"""
    return "; ".join(f"{f.detail} in {f.path}" if f.path else f.detail for f in findings)
