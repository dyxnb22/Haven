"""代码感知与轻量 AST 符号分析引擎（ADR 0035）。

为模型提供结构化的代码大纲（`repo.outline`）与符号定义/引用导航（`repo.symbol`）。
采用纯 Python 标准库（`ast`、`re`、`pathlib`）实现，不依赖外部语言服务器（LSP）进程，
纯内存只读计算，执行高效且绝无进程崩溃风险。
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Any

#: 大纲中单条文档字符串的最大截断长度。
MAX_DOCSTRING_CHARS = 120
#: 单条代码预览行的最大截断长度。
MAX_PREVIEW_CHARS = 160
#: 符号搜索默认的最大返回结果条数。
DEFAULT_MAX_SYMBOL_RESULTS = 50
#: 符号搜索单个文件的大小上限。只按行查找，读整个大文件没有意义也没有上界；
#: 与 `load_editable` 的 MAX_EDIT_FILE_BYTES 同量级。
MAX_SCANNED_FILE_BYTES = 256 * 1024
#: 扫描跳过的目录名称（控制面、虚拟环境、缓存与产物）。
IGNORED_DIRS = frozenset(
    {
        ".git",
        ".haven",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        "dist",
        "build",
        ".tox",
        "egg-info",
    }
)
#: 符号搜索纳入扫描的源码文件后缀。
SCANNED_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".json",
        ".toml",
        ".yaml",
        ".yml",
        ".md",
        ".rs",
        ".go",
        ".java",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".sh",
    }
)
#: 符号合法标识符规则。
_IDENTIFIER_PATTERN = re.compile(r"\A[a-zA-Z_][a-zA-Z0-9_]*\Z")


def _format_arg(arg: ast.arg) -> str:
    """格式化 AST 参数节点，附带类型注解（如果有）。"""
    if arg.annotation is not None:
        return f"{arg.arg}: {ast.unparse(arg.annotation)}"
    return arg.arg


def _format_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, Any]:
    """将 AST 函数/方法节点转换为结构化字典。"""
    doc = ast.get_docstring(node)
    doc_summary = doc.splitlines()[0][:MAX_DOCSTRING_CHARS] if doc else ""
    args: list[str] = [_format_arg(a) for a in node.args.posonlyargs]
    args.extend(_format_arg(a) for a in node.args.args)
    if node.args.vararg:
        args.append(f"*{_format_arg(node.args.vararg)}")
    args.extend(_format_arg(a) for a in node.args.kwonlyargs)
    if node.args.kwarg:
        args.append(f"**{_format_arg(node.args.kwarg)}")

    return {
        "name": node.name,
        "line": node.lineno,
        "end_line": getattr(node, "end_lineno", node.lineno),
        "is_async": isinstance(node, ast.AsyncFunctionDef),
        "args": args,
        "returns": ast.unparse(node.returns) if node.returns else None,
        "decorators": [ast.unparse(d) for d in node.decorator_list],
        "docstring": doc_summary,
    }


def _outline_python(code: str, filename: str) -> dict[str, Any]:
    """使用 Python AST 提取结构化大纲。"""
    try:
        tree = ast.parse(code, filename=filename)
    except Exception:
        return _outline_fallback(code, filename)

    imports: list[str] = []
    classes: list[dict[str, Any]] = []
    functions: list[dict[str, Any]] = []
    constants: list[dict[str, Any]] = []

    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            imports.extend(f"{mod}.{alias.name}" if mod else alias.name for alias in node.names)
        elif isinstance(node, ast.ClassDef):
            doc = ast.get_docstring(node)
            doc_summary = doc.splitlines()[0][:MAX_DOCSTRING_CHARS] if doc else ""
            bases = [ast.unparse(b) for b in node.bases]
            methods: list[dict[str, Any]] = []
            fields: list[str] = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append(_format_function(item))
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    fields.append(item.target.id)
                elif isinstance(item, ast.Assign):
                    for target in item.targets:
                        if isinstance(target, ast.Name):
                            fields.append(target.id)
            classes.append(
                {
                    "name": node.name,
                    "line": node.lineno,
                    "end_line": getattr(node, "end_lineno", node.lineno),
                    "bases": bases,
                    "docstring": doc_summary,
                    "methods": methods,
                    "fields": fields,
                }
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(_format_function(node))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    constants.append(
                        {
                            "name": target.id,
                            "line": node.lineno,
                            "value_preview": ast.unparse(node.value)[:MAX_PREVIEW_CHARS],
                        }
                    )
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id.isupper()
        ):
            constants.append(
                {
                    "name": node.target.id,
                    "line": node.lineno,
                    "value_preview": ast.unparse(node.value)[:MAX_PREVIEW_CHARS]
                    if node.value
                    else None,
                }
            )

    return {
        "file": filename,
        "language": "python",
        "imports": imports[:40],
        "classes": classes,
        "functions": functions,
        "constants": constants,
    }


def _outline_markdown(code: str, filename: str) -> dict[str, Any]:
    """提取 Markdown 标题结构大纲。"""
    sections: list[dict[str, Any]] = []
    for idx, line in enumerate(code.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            hashes, _, title = stripped.partition(" ")
            if hashes and set(hashes) == {"#"}:
                sections.append(
                    {
                        "level": len(hashes),
                        "title": title.strip(),
                        "line": idx,
                    }
                )
    return {
        "file": filename,
        "language": "markdown",
        "sections": sections,
    }


def _outline_fallback(code: str, filename: str) -> dict[str, Any]:
    """针对通用源码文件（TS/JS、Rust、Go、Java、C/C++ 等）的结构化大纲提取。"""
    if "\x00" in code:
        return {
            "file": filename,
            "language": "binary",
            "error": "binary file cannot be parsed into code outline",
            "symbols": [],
            "classes": [],
            "functions": [],
            "interfaces": [],
            "types": [],
        }

    patterns: list[tuple[str, re.Pattern[str]]] = [
        # TypeScript / JavaScript / Java / C# 类与接口
        (
            "class",
            re.compile(
                r"^\s*(?:export\s+)?(?:default\s+)?(?:public|private|protected\s+)?(?:abstract\s+)?class\s+([A-Za-z0-9_]+)"
            ),
        ),
        (
            "interface",
            re.compile(r"^\s*(?:export\s+)?(?:public\s+)?interface\s+([A-Za-z0-9_]+)"),
        ),
        (
            "type",
            re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z0-9_]+)\s*(?:<[^=]*>)?\s*="),
        ),
        (
            "enum",
            re.compile(r"^\s*(?:export\s+)?(?:const\s+)?enum\s+([A-Za-z0-9_]+)"),
        ),
        # 函数与箭头函数 / 组件 / 类方法
        (
            "function",
            re.compile(r"^\s*(?:async\s+)?(?:export\s+)?(?:default\s+)?function\s+([A-Za-z0-9_]+)"),
        ),
        (
            "function",
            re.compile(
                r"^\s*(?:export\s+)?const\s+([A-Za-z0-9_]+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z0-9_]+)\s*=>"
            ),
        ),
        (
            "function",
            re.compile(
                r"^\s*(?:public|private|protected|static|final|native|synchronized|abstract)\s+"
                r"(?:[A-Za-z0-9_<>\[\],\s]+\s+)+([A-Za-z0-9_]+)\s*\("
            ),
        ),
        # Rust 语法
        (
            "struct",
            re.compile(r"^\s*(?:pub(?:\([^)]+\))?\s+)?struct\s+([A-Za-z0-9_]+)"),
        ),
        (
            "enum",
            re.compile(r"^\s*(?:pub(?:\([^)]+\))?\s+)?enum\s+([A-Za-z0-9_]+)"),
        ),
        (
            "trait",
            re.compile(r"^\s*(?:pub(?:\([^)]+\))?\s+)?trait\s+([A-Za-z0-9_]+)"),
        ),
        (
            "impl",
            re.compile(r"^\s*impl(?:<[^>]+>)?\s+(?:[A-Za-z0-9_:]+\s+for\s+)?([A-Za-z0-9_]+)"),
        ),
        (
            "def",
            re.compile(r"^\s*(?:pub(?:\([^)]+\))?\s+)?(?:async\s+)?fn\s+([A-Za-z0-9_]+)"),
        ),
        # Go 语法
        (
            "struct",
            re.compile(r"^\s*type\s+([A-Za-z0-9_]+)\s+struct\b"),
        ),
        (
            "interface",
            re.compile(r"^\s*type\s+([A-Za-z0-9_]+)\s+interface\b"),
        ),
        (
            "type",
            re.compile(r"^\s*type\s+([A-Za-z0-9_]+)\s+[A-Za-z0-9_\[\]]+"),
        ),
        (
            "function",
            re.compile(r"^\s*func\s+(?:\([^)]+\)\s+)?([A-Za-z0-9_]+)\s*\("),
        ),
    ]
    items: list[dict[str, Any]] = []
    for idx, line in enumerate(code.splitlines(), start=1):
        for kind, pat in patterns:
            match = pat.search(line)
            if match:
                items.append(
                    {
                        "kind": kind,
                        "name": match.group(1),
                        "line": idx,
                        "preview": line.strip()[:MAX_PREVIEW_CHARS],
                    }
                )
                break
    return {
        "file": filename,
        "language": "generic",
        "symbols": items,
        "classes": [s for s in items if s["kind"] in ("class", "struct")],
        "functions": [s for s in items if s["kind"] in ("function", "def", "method")],
        "interfaces": [s for s in items if s["kind"] in ("interface", "trait")],
        "types": [s for s in items if s["kind"] in ("type", "enum")],
    }


def outline_source(code: str, filename: str = "") -> dict[str, Any]:
    """解析给定代码并生成分层的大纲字典。"""
    if filename.endswith((".py", ".pyi")):
        return _outline_python(code, filename)
    if filename.endswith(".md"):
        return _outline_markdown(code, filename)
    return _outline_fallback(code, filename)


def find_symbol_occurrences(
    workspace_root: Path,
    symbol_name: str,
    target_path: str | None = None,
    max_results: int = DEFAULT_MAX_SYMBOL_RESULTS,
) -> dict[str, Any]:
    """在工作区内搜索符号的定义（definition）与引用（reference）。

    失败即关闭：符号名必须为合法的非空编程标识符。
    """
    if not _IDENTIFIER_PATTERN.match(symbol_name):
        return {
            "symbol": symbol_name,
            "error": f"invalid symbol name {symbol_name!r}: must be a valid identifier",
            "definitions": [],
            "references": [],
        }

    word_pattern = re.compile(rf"\b{re.escape(symbol_name)}\b")
    def_py_pattern = re.compile(
        rf"^\s*(?:class|def|async\s+def)\s+{re.escape(symbol_name)}\b|"
        rf"^\s*{re.escape(symbol_name)}\s*[:=]"
    )
    def_generic_pattern = re.compile(
        rf"^\s*(?:export\s+)?(?:default\s+)?(?:public|private|protected\s+)?(?:abstract\s+)?"
        rf"(?:class|interface|type|fn|function|def|struct|enum|trait)\s+{re.escape(symbol_name)}\b|"
        rf"^\s*(?:const|let|var)\s+{re.escape(symbol_name)}\s*[:=]|"
        rf"^\s*type\s+{re.escape(symbol_name)}\b|"
        rf"^\s*func\s+(?:\([^)]+\)\s+)?{re.escape(symbol_name)}\s*\(|"
        rf"^\s*(?:async\s+)?{re.escape(symbol_name)}\s*\([^)]*\)\s*(?::\s*[^{{]+)?\s*\{{|"
        rf"^\s*(?:pub(?:\([^)]+\))?\s+)?(?:async\s+)?fn\s+{re.escape(symbol_name)}\b"
    )

    definitions: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    skipped_files = 0

    search_root = workspace_root
    if target_path:
        candidate = (workspace_root / target_path).resolve()
        try:
            candidate.relative_to(workspace_root.resolve())
            search_root = candidate
        except ValueError:
            return {
                "symbol": symbol_name,
                "error": f"target path {target_path!r} escapes workspace root",
                "definitions": [],
                "references": [],
            }

    if search_root.is_file():
        files_to_scan = [search_root]
    elif search_root.is_dir():
        files_to_scan = []
        for root, dirnames, filenames in os.walk(search_root, followlinks=False):
            # 目录级快速剪枝：完全不递归进入过滤目录（极大提升大型仓库符号检索性能）
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
            root_path = Path(root)
            for fname in filenames:
                p = root_path / fname
                if not p.is_symlink() and (
                    p.suffix in SCANNED_EXTENSIONS or p.name in {"Dockerfile", "Makefile"}
                ):
                    files_to_scan.append(p)
    else:
        return {
            "symbol": symbol_name,
            "definitions": [],
            "references": [],
            "total_matches": 0,
        }

    for file_path in sorted(files_to_scan):
        try:
            rel_path = file_path.relative_to(workspace_root).as_posix()
        except ValueError:
            continue
        # 只按行查找，所以读整个文件没有意义，也没有上界：扫描路径上一个巨大的
        # 数据文件会把进程撑爆（`load_editable` 早有 256 KiB 这道闸，这里漏了）。
        # 超过上限的文件跳过，并计数——否则 `total_matches` 会悄悄少算，而调用方
        # 无从知道还有文件没看。
        try:
            if file_path.stat().st_size > MAX_SCANNED_FILE_BYTES:
                skipped_files += 1
                continue
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        is_python = file_path.suffix in {".py", ".pyi"}
        def_pattern = def_py_pattern if is_python else def_generic_pattern

        for line_idx, line in enumerate(text.splitlines(), start=1):
            if not word_pattern.search(line):
                continue

            preview = line.strip()[:MAX_PREVIEW_CHARS]
            is_def = bool(def_pattern.search(line))
            entry = {
                "path": rel_path,
                "line": line_idx,
                "kind": "definition" if is_def else "reference",
                "preview": preview,
            }
            if is_def:
                definitions.append(entry)
            else:
                references.append(entry)

            if len(definitions) + len(references) >= max_results * 2:
                break
        if len(definitions) + len(references) >= max_results * 2:
            break

    total_matches = len(definitions) + len(references)
    truncated = total_matches > max_results
    return {
        "symbol": symbol_name,
        "total_definitions": len(definitions),
        "total_references": len(references),
        "definitions": definitions[:max_results],
        "references": references[: max(0, max_results - len(definitions))],
        "truncated": truncated,
        # 有文件因体积被跳过时必须说出来，否则"没找到"会被误读成"这里没有"
        "files_skipped_too_large": skipped_files,
    }
