"""结构化诊断提供器（ADR 0040）。

在 `repo.check` 或 `repo.exec` 执行退出码非零或产出测试/代码检查报错时，
解析原始 stdout/stderr 输出，提取高价值的结构化诊断条目（文件、行号、规则码、错误信息），
直接投喂给模型观察上下文，避免关键报错被冗长测试会话日志淹没。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

MAX_DIAGNOSTICS_ITEMS = 25

# 匹配 pytest short test summary 或 fixture / teardown 报错：
# FAILED tests/unit/test_calc.py::test_add - AssertionError: assert 0 == 4
# FAILED tests/test_foo.py::TestCls::test_m - ValueError: bad value
# ERROR tests/unit/test_calc.py::test_add - Exception: fixture failed
_PYTEST_FAILED = re.compile(r"^(?:FAILED|ERROR)\s+([^:\s]+)::([^\s]+)\s+-\s+(.+)$")
_PYTEST_COLLECTION_ERR = re.compile(r"^ERROR\s+collecting\s+([^:\s]+)")

# 匹配 pytest 失败用例头部段落：
# ___________________________________ test_add ___________________________________
# ____________________________ TestClass.test_method _____________________________
_PYTEST_SECTION_HEADER = re.compile(r"^_{5,}\s+(\S+)\s+_{5,}$")

# 匹配 pytest / python 文件行内错误定位：
# tests/unit/test_calc.py:12: in test_add
# tests/unit/test_calc.py:12: AssertionError: ...
_PYTEST_LOCATION_ERR = re.compile(r"^([a-zA-Z0-9_\-./\\]+\.py):(\d+):")

# 匹配 Mypy:
# src/calc.py:15: error: Incompatible types [assignment]
# src/calc.py:15:10: warning: ...
_MYPY_DIAG = re.compile(
    r"^([a-zA-Z0-9_\-./\\]+\.[a-zA-Z0-9_]+):(\d+)(?::(\d+))?:\s+(error|warning|note):\s+(.*?)(?:\s+\[([a-zA-Z0-9_\-]+)\])?$"
)

# 匹配 Ruff / Flake8:
# src/calc.py:10:5: F401 'math' imported but unused
_RUFF_DIAG = re.compile(
    r"^([a-zA-Z0-9_\-./\\]+\.[a-zA-Z0-9_]+):(\d+):(\d+):\s+([A-Z]\d+|[A-Z]+[0-9]+)\s+(.+)$"
)

# 匹配 Python 堆栈调用帧：
# File "/path/to/file.py", line 42, in func_name
_TRACEBACK_FRAME = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')

# 匹配 Python 未捕获异常行（支持带模块路径的点号与各类内置/自定义异常）：
# ZeroDivisionError: division by zero
# pydantic_core._pydantic_core.ValidationError: 1 validation error
# LeaseHeld: lock already held
_TRACEBACK_EXC = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*):(?:\s+(.*))?$")

# 匹配 Rust 编译器输出:
# error[E0425]: cannot find value
#   --> src/main.rs:12:5
_RUST_ERR = re.compile(r"^error\[([a-zA-Z0-9]+)\]:\s+(.*)$")
_RUST_LOC = re.compile(r"^\s*-->\s+([^:]+):(\d+):(\d+)$")

# 匹配 TypeScript 编译器 (tsc) 输出:
# src/index.ts:15:22 - error TS2304: Cannot find name 'x'.
# src/index.ts(15,22): error TS2304: Cannot find name 'x'.
_TSC_DIAG = re.compile(
    r"^([a-zA-Z0-9_\-./\\]+\.[tj]sx?)(?:\((\d+),(\d+)\)|:(\d+):(\d+))(?::|\s+-)?\s+(error|warning)\s+([A-Z0-9]+):\s+(.*)$"
)

# 匹配 Go 编译与 golangci-lint 输出:
# main.go:12:5: undefined: myVar
# pkg/server.go:45:10: S1000: should use for range instead of ...
_GO_DIAG = re.compile(
    r"^([a-zA-Z0-9_\-./\\]+\.go):(\d+):(\d+):\s+(?:([A-Z]\d+|[A-Z]+[0-9]+):\s+)?(.*)$"
)

# ANSI 颜色转义字符匹配器，防止终端带色彩输出干扰模式匹配
_ANSI_STRIP = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


@dataclass(frozen=True, slots=True)
class DiagnosticItem:
    """单条结构化错误或失败诊断。"""

    file: str | None = None
    line: int | None = None
    column: int | None = None
    severity: str = "error"  # "failure" | "error" | "warning"
    rule_or_code: str | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典供模型 payload 使用。"""
        out: dict[str, Any] = {
            "severity": self.severity,
            "message": self.message,
        }
        if self.file is not None:
            out["file"] = self.file
        if self.line is not None:
            out["line"] = self.line
        if self.column is not None:
            out["column"] = self.column
        if self.rule_or_code is not None:
            out["code"] = self.rule_or_code
        return out


def _extract_pytest_diagnostics(lines: list[str]) -> list[DiagnosticItem]:
    """从 pytest 输出行提取失败用例与收集错误，并结合段落行号丰富定位。"""
    items: list[DiagnosticItem] = []
    pytest_summary_items: list[DiagnosticItem] = []
    pytest_section_locations: dict[tuple[str, str], int] = {}
    pytest_file_locations: dict[str, int] = {}

    curr_pytest_section: str | None = None
    for line in lines:
        m_sec = _PYTEST_SECTION_HEADER.match(line)
        if m_sec:
            curr_pytest_section = m_sec.group(1)
            continue
        m_loc = _PYTEST_LOCATION_ERR.match(line)
        if m_loc:
            loc_file = m_loc.group(1)
            loc_line = int(m_loc.group(2))
            if curr_pytest_section:
                pytest_section_locations[(loc_file, curr_pytest_section)] = loc_line
            pytest_file_locations.setdefault(loc_file, loc_line)
            continue
        m_failed = _PYTEST_FAILED.match(line)
        if m_failed:
            file_path = m_failed.group(1)
            test_name = m_failed.group(2)
            err_msg = m_failed.group(3)
            pytest_summary_items.append(
                DiagnosticItem(
                    file=file_path,
                    severity="failure",
                    rule_or_code="pytest",
                    message=f"{test_name} - {err_msg}",
                )
            )
            continue
        m_collect = _PYTEST_COLLECTION_ERR.match(line)
        if m_collect:
            items.append(
                DiagnosticItem(
                    file=m_collect.group(1),
                    severity="error",
                    rule_or_code="pytest:collection",
                    message=f"Collection failed: {line}",
                )
            )

    if pytest_summary_items:
        for it in pytest_summary_items:
            test_part = it.message.split(" - ")[0]
            line_no = (
                pytest_section_locations.get((it.file or "", test_part))
                or pytest_section_locations.get((it.file or "", test_part.replace("::", ".")))
                or pytest_section_locations.get((it.file or "", test_part.split("::")[-1]))
                or pytest_section_locations.get((it.file or "", test_part.split("[")[0]))
                or pytest_file_locations.get(it.file or "")
            )
            items.append(
                DiagnosticItem(
                    file=it.file,
                    line=line_no,
                    severity=it.severity,
                    rule_or_code=it.rule_or_code,
                    message=it.message,
                )
            )

    return items


def _extract_ruff_diagnostics(lines: list[str]) -> list[DiagnosticItem]:
    """从 Ruff / Flake8 输出提取代码检查违规条目。"""
    items: list[DiagnosticItem] = []
    for line in lines:
        m = _RUFF_DIAG.match(line)
        if m:
            items.append(
                DiagnosticItem(
                    file=m.group(1),
                    line=int(m.group(2)),
                    column=int(m.group(3)),
                    severity="error",
                    rule_or_code=m.group(4),
                    message=m.group(5),
                )
            )
    return items


def _extract_mypy_diagnostics(lines: list[str]) -> list[DiagnosticItem]:
    """从 Mypy 输出提取类型检查错误与告警。"""
    items: list[DiagnosticItem] = []
    for line in lines:
        m = _MYPY_DIAG.match(line)
        if m:
            items.append(
                DiagnosticItem(
                    file=m.group(1),
                    line=int(m.group(2)),
                    column=int(m.group(3)) if m.group(3) else None,
                    severity=m.group(4),
                    rule_or_code=m.group(6),
                    message=m.group(5),
                )
            )
    return items


def _extract_rust_diagnostics(lines: list[str]) -> list[DiagnosticItem]:
    """从 rustc / cargo 输出提取 Rust 编译器错误与行列位置。"""
    items: list[DiagnosticItem] = []
    rust_err_code: str | None = None
    rust_msg: str | None = None
    for line in lines:
        m_err = _RUST_ERR.match(line)
        if m_err:
            rust_err_code = m_err.group(1)
            rust_msg = m_err.group(2)
            continue
        if rust_err_code and rust_msg:
            m_loc = _RUST_LOC.match(line)
            if m_loc:
                items.append(
                    DiagnosticItem(
                        file=m_loc.group(1),
                        line=int(m_loc.group(2)),
                        column=int(m_loc.group(3)),
                        severity="error",
                        rule_or_code=rust_err_code,
                        message=rust_msg,
                    )
                )
                rust_err_code = None
                rust_msg = None
            elif not line.startswith("-->"):
                rust_err_code = None
                rust_msg = None
    return items


def _extract_tsc_diagnostics(lines: list[str]) -> list[DiagnosticItem]:
    """从 TypeScript (tsc) 输出提取编译器错误与位置。"""
    items: list[DiagnosticItem] = []
    for line in lines:
        m = _TSC_DIAG.match(line)
        if m:
            f = m.group(1)
            line_no = int(m.group(2) or m.group(4))
            col_no = int(m.group(3) or m.group(5))
            severity = m.group(6)
            code = m.group(7)
            msg = m.group(8)
            items.append(
                DiagnosticItem(
                    file=f,
                    line=line_no,
                    column=col_no,
                    severity=severity,
                    rule_or_code=code,
                    message=msg,
                )
            )
    return items


def _extract_go_diagnostics(lines: list[str]) -> list[DiagnosticItem]:
    """从 Go 编译器与 golangci-lint 输出提取错误与告警。"""
    items: list[DiagnosticItem] = []
    for line in lines:
        m = _GO_DIAG.match(line)
        if m:
            items.append(
                DiagnosticItem(
                    file=m.group(1),
                    line=int(m.group(2)),
                    column=int(m.group(3)),
                    severity="error",
                    rule_or_code=m.group(4),
                    message=m.group(5),
                )
            )
    return items


def _extract_traceback_diagnostics(raw_lines: list[str]) -> list[DiagnosticItem]:
    """从原始输出中捕获 Python 回溯栈与未捕获异常。"""
    items: list[DiagnosticItem] = []
    last_frame_file: str | None = None
    last_frame_line: int | None = None
    in_tb = False
    for raw_line in raw_lines:
        s_line = raw_line.strip()
        if not s_line:
            continue
        if "Traceback (most recent call last):" in s_line:
            in_tb = True
            last_frame_file = None
            last_frame_line = None
            continue
        if in_tb:
            m_frame = _TRACEBACK_FRAME.search(s_line)
            if m_frame:
                last_frame_file = m_frame.group(1)
                last_frame_line = int(m_frame.group(2))
                continue
            # 异常行必定位于行首（无缩进），杜绝误匹配 Traceback 内部包含冒号的普通代码
            if not raw_line.startswith((" ", "\t")):
                m_exc = _TRACEBACK_EXC.match(s_line)
                if m_exc:
                    exc_type = m_exc.group(1)
                    exc_msg = m_exc.group(2) or ""
                    full_msg = f"{exc_type}: {exc_msg}" if exc_msg else exc_type
                    items.append(
                        DiagnosticItem(
                            file=last_frame_file,
                            line=last_frame_line,
                            severity="error",
                            rule_or_code=exc_type,
                            message=full_msg,
                        )
                    )
                    in_tb = False
                    last_frame_file = None
                    last_frame_line = None
    return items


def _extract_fallback_diagnostics(lines: list[str]) -> list[DiagnosticItem]:
    """兜底解析：寻找显式错误关键词行或末尾输出行。"""
    items: list[DiagnosticItem] = []
    for line in lines:
        upper = line.upper()
        if any(k in upper for k in ("ERROR:", "FAIL:", "FAILED:", "FATAL:")):
            items.append(DiagnosticItem(severity="error", message=line))
    if not items and lines:
        tail_lines = lines[-3:]
        for line in tail_lines:
            items.append(DiagnosticItem(severity="error", message=line))
    return items


def extract_diagnostics(
    recipe_id: str,
    stdout: str,
    stderr: str,
    exit_code: int,
) -> list[DiagnosticItem]:
    """从 stdout 与 stderr 中提取结构化错误诊断。"""
    if exit_code == 0:
        return []

    combined_text = _ANSI_STRIP.sub("", f"{stdout}\n{stderr}")
    raw_lines = combined_text.splitlines()
    lines = [line.strip() for line in raw_lines if line.strip()]

    items: list[DiagnosticItem] = []
    seen: set[tuple[str | None, int | None, str]] = set()

    def add_item(item: DiagnosticItem) -> None:
        key = (item.file, item.line, item.message)
        if key not in seen and len(items) < MAX_DIAGNOSTICS_ITEMS:
            seen.add(key)
            items.append(item)

    for item in _extract_pytest_diagnostics(lines):
        add_item(item)
    for item in _extract_ruff_diagnostics(lines):
        add_item(item)
    for item in _extract_mypy_diagnostics(lines):
        add_item(item)
    for item in _extract_rust_diagnostics(lines):
        add_item(item)
    for item in _extract_tsc_diagnostics(lines):
        add_item(item)
    for item in _extract_go_diagnostics(lines):
        add_item(item)
    for item in _extract_traceback_diagnostics(raw_lines):
        add_item(item)

    if not items:
        for item in _extract_fallback_diagnostics(lines):
            add_item(item)

    return items


def format_diagnostics_summary(diagnostics: list[DiagnosticItem]) -> str:
    """将诊断条目格式化为面向模型观察的一览摘要。"""
    if not diagnostics:
        return ""
    count = len(diagnostics)
    header = f"Found {count} diagnostic issue(s):"
    lines = [header]
    for d in diagnostics:
        loc = ""
        if d.file:
            loc = d.file
            if d.line is not None:
                loc += f":{d.line}"
                if d.column is not None:
                    loc += f":{d.column}"
            loc += " "
        code = f"[{d.rule_or_code}] " if d.rule_or_code else ""
        lines.append(f"- {loc}({d.severity}) {code}{d.message}".strip())
    return "\n".join(lines)
