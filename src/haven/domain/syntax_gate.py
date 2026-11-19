"""基于 AST 和结构化格式的语法预检门禁（ADR 0040）。

在工作区写操作（repo.edit、repo.create、repo.apply_patch）落盘前，
对模拟生成的后像在内存中进行语法树解析与校验，防止破损 AST 污染工作区。
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import sys
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


#: 内容超过这个大小时，语法解析改到子进程里做。
#:
#: `ast.parse` 是纯 Python，整个解析期间占着 GIL：卸载到线程只能把 240 KB 文件一次编辑
#: 预览的事件循环停顿从约 150 ms 降到约 85 ms（A/B 实测，差异在噪声量级），界面照样卡
#: 一下。只有换进程才能真正让循环保持响应。小子块走进程反而更慢——解释器启动就要几十
#: 毫秒，而解析本身几乎是瞬时的。
SUBPROCESS_PARSE_THRESHOLD = 64 * 1024

#: 子进程 worker：从 stdin 读 {"path", "content"}，把诊断字典写到 stdout。
_PARSE_WORKER = (
    "import json, sys;"
    "from haven.domain.syntax_gate import validate_syntax;"
    "p = json.loads(sys.stdin.buffer.read());"
    "sys.stdout.write(json.dumps(validate_syntax(p['path'], p['content']).to_dict()))"
)


async def validate_syntax_async(path: str, content: str) -> SyntaxDiagnostic:
    """校验语法；大块在子进程里解析，小块的直接在调用线程里做。

    这是**预写门禁**：它决定一次编辑是否被允许落盘。所以子进程答不出来时必须退回到
    进程内解析——那是对的，只是慢。正确性优先于界面响应。
    """
    if len(content) < SUBPROCESS_PARSE_THRESHOLD:
        return validate_syntax(path, content)
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _PARSE_WORKER,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        # 起不来（极少见：进程表满、可执行文件消失）。退回进程内，结果一样。
        return validate_syntax(path, content)

    try:
        out, _ = await proc.communicate(json.dumps({"path": path, "content": content}).encode())
    except (OSError, ValueError):
        return validate_syntax(path, content)
    if proc.returncode != 0 or not out:
        return validate_syntax(path, content)
    try:
        return SyntaxDiagnostic(**json.loads(out.decode("utf-8")))
    except (TypeError, ValueError):
        # worker 答了个我们不认识形状的东西：宁可慢，也不能把门禁结果猜出来。
        return validate_syntax(path, content)


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
