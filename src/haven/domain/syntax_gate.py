"""基于 AST 和结构化格式的语法预检门禁（ADR 0040）。

在工作区写操作（repo.edit、repo.create、repo.apply_patch）落盘前，
对模拟生成的后像在内存中进行语法树解析与校验，防止破损 AST 污染工作区。
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any


@dataclass(frozen=True, slots=True)
class SyntaxDiagnostic:
    """文件语法解析的事实与定位诊断。"""

    #: 被校验的文件路径。
    path: str
    #: 是否语法有效。
    is_valid: bool
    #: 错误类别（如 SyntaxError, IndentationError, JSONDecodeError, TOMLDecodeError）。
    error_type: str | None = None
    #: 错误描述。
    message: str | None = None
    #: 1-based 出错行号。
    line: int | None = None
    #: 1-based 出错列号。
    column: int | None = None
    #: 出错行的源代码片段。
    text: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为结构化字典。"""
        out: dict[str, Any] = {
            "path": self.path,
            "is_valid": self.is_valid,
        }
        if self.error_type is not None:
            out["error_type"] = self.error_type
        if self.message is not None:
            out["message"] = self.message
        if self.line is not None:
            out["line"] = self.line
        if self.column is not None:
            out["column"] = self.column
        if self.text is not None:
            out["text"] = self.text
        return out


_TOML_LINE_COL = re.compile(r"\(at line (\d+), column (\d+)\)")


def validate_syntax(path: str, content: str) -> SyntaxDiagnostic:
    """在内存中解析内容，返回语法诊断事实。

    仅对已知代码与结构化格式（.py, .pyi, .json, .toml）执行语法解析；
    其他文件类型默认视为有效。
    """
    ext = PurePath(path).suffix.lower()

    if ext in (".py", ".pyi"):
        try:
            ast.parse(content, filename=path)
            return SyntaxDiagnostic(path=path, is_valid=True)
        except (SyntaxError, ValueError) as exc:
            err_type = (
                "IndentationError"
                if isinstance(exc, IndentationError)
                else "SyntaxError"
                if isinstance(exc, SyntaxError)
                else type(exc).__name__
            )
            msg = getattr(exc, "msg", None) or str(exc) or "syntax error"
            line_no = getattr(exc, "lineno", None)
            col_no = getattr(exc, "offset", None)
            raw_text = getattr(exc, "text", None)
            return SyntaxDiagnostic(
                path=path,
                is_valid=False,
                error_type=err_type,
                message=msg,
                line=line_no,
                column=col_no,
                text=raw_text.rstrip() if raw_text else None,
            )

    if ext == ".json":
        try:
            json.loads(content)
            return SyntaxDiagnostic(path=path, is_valid=True)
        except json.JSONDecodeError as exc:
            lines = content.splitlines()
            line_text = lines[exc.lineno - 1] if 0 <= exc.lineno - 1 < len(lines) else None
            return SyntaxDiagnostic(
                path=path,
                is_valid=False,
                error_type="JSONDecodeError",
                message=exc.msg,
                line=exc.lineno,
                column=exc.colno,
                text=line_text,
            )

    if ext == ".toml":
        try:
            tomllib.loads(content)
            return SyntaxDiagnostic(path=path, is_valid=True)
        except tomllib.TOMLDecodeError as exc:
            msg = str(exc)
            line: int | None = None
            col: int | None = None
            match = _TOML_LINE_COL.search(msg)
            if match:
                line = int(match.group(1))
                col = int(match.group(2))
            lines = content.splitlines()
            line_text = lines[line - 1] if line is not None and 0 <= line - 1 < len(lines) else None
            return SyntaxDiagnostic(
                path=path,
                is_valid=False,
                error_type="TOMLDecodeError",
                message=msg,
                line=line,
                column=col,
                text=line_text,
            )

    return SyntaxDiagnostic(path=path, is_valid=True)


def format_syntax_error(diag: SyntaxDiagnostic) -> str:
    """将语法错误格式化为模型易于定位和修正的结构化提示文本。"""
    if diag.is_valid:
        return ""
    loc_parts: list[str] = []
    if diag.line is not None:
        loc_parts.append(f"line {diag.line}")
    if diag.column is not None:
        loc_parts.append(f"column {diag.column}")
    loc = f" at {', '.join(loc_parts)}" if loc_parts else ""

    err_name = diag.error_type or "SyntaxError"
    header = f"Syntax validation failed for '{diag.path}' ({err_name}{loc}): {diag.message}"
    lines = [header]
    if diag.text:
        lines.append(f"    {diag.text}")
        if diag.column is not None and diag.column > 0:
            lines.append("    " + " " * min(diag.column - 1, 200) + "^")
    return "\n".join(lines)
