"""测试代码感知与轻量 AST 符号大纲/搜索（ADR 0035）。"""

from __future__ import annotations

from pathlib import Path

from haven.application.code_ast import find_symbol_occurrences, outline_source


def test_outline_python_extracts_classes_functions_and_constants() -> None:
    code = '''"""Module docstring."""

import os
from pathlib import Path

MAX_COUNT = 100
app_name = "test"

class User:
    """User model."""
    id: int
    name: str

    def __init__(self, id: int, name: str) -> None:
        self.id = id
        self.name = name

    async def save(self) -> bool:
        """Save user."""
        return True

def get_user(id: int) -> User | None:
    """Fetch user by id."""
    return None
'''
    outline = outline_source(code, "models.py")
    assert outline["language"] == "python"
    assert "os" in outline["imports"]
    assert "pathlib.Path" in outline["imports"]

    # 常量
    constants = outline["constants"]
    assert any(c["name"] == "MAX_COUNT" for c in constants)

    # 类与方法
    classes = outline["classes"]
    assert len(classes) == 1
    cls = classes[0]
    assert cls["name"] == "User"
    assert cls["docstring"] == "User model."
    assert "id" in cls["fields"]
    assert "name" in cls["fields"]
    methods = {m["name"]: m for m in cls["methods"]}
    assert "__init__" in methods
    assert "save" in methods
    assert methods["save"]["is_async"] is True
    assert methods["save"]["returns"] == "bool"

    # 函数
    functions = outline["functions"]
    assert len(functions) == 1
    fn = functions[0]
    assert fn["name"] == "get_user"
    assert fn["returns"] == "User | None"
    assert "id: int" in fn["args"][0]

    # .pyi 存根文件同样使用 Python AST 解析
    outline_pyi = outline_source("def stub() -> int: ...", "stub.pyi")
    assert outline_pyi["language"] == "python"
    assert outline_pyi["functions"][0]["name"] == "stub"


def test_outline_markdown_extracts_headers() -> None:
    md = """# Title

Some text.

## Section 1
Content.

### Subsection 1.1
More content.
"""
    outline = outline_source(md, "README.md")
    assert outline["language"] == "markdown"
    sections = outline["sections"]
    assert len(sections) == 3
    assert sections[0] == {"level": 1, "title": "Title", "line": 1}
    assert sections[1] == {"level": 2, "title": "Section 1", "line": 5}
    assert sections[2] == {"level": 3, "title": "Subsection 1.1", "line": 8}


def test_outline_fallback_for_generic_code() -> None:
    js = """export class MyService {
  constructor() {}
}

export function helper(x) {
  return x + 1;
}
"""
    outline = outline_source(js, "service.ts")
    assert outline["language"] == "generic"
    symbols = outline["symbols"]
    assert any(s["name"] == "MyService" and s["kind"] == "class" for s in symbols)
    assert any(s["name"] == "helper" and s["kind"] == "function" for s in symbols)


def test_find_symbol_occurrences_separates_definitions_and_references(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "calc.py").write_text(
        "def compute_total(items):\n    return sum(items)\n",
        encoding="utf-8",
    )
    (src_dir / "main.py").write_text(
        "from calc import compute_total\n\nresult = compute_total([1, 2, 3])\n",
        encoding="utf-8",
    )

    res = find_symbol_occurrences(tmp_path, "compute_total")
    assert res["symbol"] == "compute_total"
    assert res["total_definitions"] == 1
    assert res["total_references"] >= 2

    defn = res["definitions"][0]
    assert defn["path"] == "src/calc.py"
    assert defn["line"] == 1
    assert defn["kind"] == "definition"
    assert "def compute_total" in defn["preview"]

    refs = res["references"]
    assert any(r["path"] == "src/main.py" and "import compute_total" in r["preview"] for r in refs)
    assert any(
        r["path"] == "src/main.py" and "result = compute_total" in r["preview"] for r in refs
    )


def test_find_symbol_occurrences_rejects_invalid_identifier(tmp_path: Path) -> None:
    res = find_symbol_occurrences(tmp_path, "invalid symbol with spaces")
    assert "error" in res
    assert res["definitions"] == []
    assert res["references"] == []


def test_outline_python_graceful_on_syntax_error() -> None:
    broken = "def broken(\n   return 42\n"
    outline = outline_source(broken, "broken.py")
    assert outline["language"] == "generic"


def test_find_symbol_occurrences_rejects_escaping_target_path(tmp_path: Path) -> None:
    res = find_symbol_occurrences(tmp_path, "my_func", target_path="../../outside")
    assert "error" in res
    assert "escapes workspace root" in res["error"]


def test_find_symbol_occurrences_prunes_ignored_directories(tmp_path: Path) -> None:
    venv_dir = tmp_path / ".venv" / "lib"
    venv_dir.mkdir(parents=True)
    (venv_dir / "dep.py").write_text("def secret_ignored(): pass\n", encoding="utf-8")

    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "app.py").write_text("def secret_target(): pass\n", encoding="utf-8")

    res = find_symbol_occurrences(tmp_path, "secret_ignored")
    assert res["total_definitions"] == 0

    res2 = find_symbol_occurrences(tmp_path, "secret_target")
    assert res2["total_definitions"] == 1
    assert res2["definitions"][0]["path"] == "src/app.py"


def test_outline_multilanguage_constructs() -> None:
    # Rust 结构测试
    rs_code = """pub struct UserConfig {
    pub max_retries: u32,
}

pub trait Runner {
    fn execute(&self);
}

impl Runner for UserConfig {
    fn execute(&self) {}
}
"""
    rs_outline = outline_source(rs_code, "config.rs")
    kinds = [(s["kind"], s["name"]) for s in rs_outline["symbols"]]
    assert ("struct", "UserConfig") in kinds
    assert ("trait", "Runner") in kinds
    assert ("def", "execute") in kinds or ("function", "execute") in kinds

    # Go 结构测试
    go_code = """package main

type ServerConfig struct {
    Port int
}

func (s *ServerConfig) Start() error {
    return nil
}

func HandleRequest() {}
"""
    go_outline = outline_source(go_code, "main.go")
    go_kinds = [(s["kind"], s["name"]) for s in go_outline["symbols"]]
    assert ("struct", "ServerConfig") in go_kinds
    assert ("function", "Start") in go_kinds
    assert ("function", "HandleRequest") in go_kinds
    # 分组列表断言
    assert any(c["name"] == "ServerConfig" for c in go_outline["classes"])
    assert any(f["name"] == "Start" for f in go_outline["functions"])


def test_outline_binary_file_handled_gracefully() -> None:
    binary_content = "some text\x00with null bytes\x01\x02"
    outline = outline_source(binary_content, "image.png")
    assert outline["language"] == "binary"
    assert "error" in outline
    assert outline["symbols"] == []


def test_find_symbol_occurrences_multilanguage(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "service.ts").write_text(
        "export class AuthService {\n  login() {}\n}\nconst auth = new AuthService();\n",
        encoding="utf-8",
    )
    (src / "server.go").write_text(
        "package main\n\nfunc StartServer() {}\nfunc main() {\n  StartServer()\n}\n",
        encoding="utf-8",
    )
    res_ts = find_symbol_occurrences(tmp_path, "AuthService")
    assert res_ts["total_definitions"] >= 1
    assert any(d["path"] == "src/service.ts" for d in res_ts["definitions"])

    res_go = find_symbol_occurrences(tmp_path, "StartServer")
    assert res_go["total_definitions"] >= 1
    assert any(d["path"] == "src/server.go" for d in res_go["definitions"])


def test_find_symbol_occurrences_skips_oversized_files_and_says_so(tmp_path: Path) -> None:
    """超过大小上限的文件必须被跳过，而且要在结果里说出来。

    只按行查找，读整个大文件既没有意义也没有上界：扫描路径上一个巨大的数据文件会把
    进程撑爆（`load_editable` 早有 256 KiB 这道闸，这里漏了）。但跳过必须留下痕迹，
    否则"没找到"会被误读成"这里没有"——`total_matches` 悄悄少算，调用方无从知道。
    """
    from haven.application.code_ast import MAX_SCANNED_FILE_BYTES

    small = tmp_path / "small.py"
    small.write_text("def compute_total(items):\n    return sum(items)\n", encoding="utf-8")
    huge = tmp_path / "huge.py"
    huge.write_text(
        "# filler\n" * (MAX_SCANNED_FILE_BYTES // 9 + 10) + "def compute_total(x):\n    return x\n",
        encoding="utf-8",
    )
    assert huge.stat().st_size > MAX_SCANNED_FILE_BYTES

    result = find_symbol_occurrences(tmp_path, "compute_total")

    # 跳过了，且如实报告
    assert result["files_skipped_too_large"] == 1
    # 小文件里的那个定义仍然找得到——上限不能把正常结果一起吃掉
    assert result["total_definitions"] == 1
    assert result["definitions"][0]["path"] == "small.py"


def test_find_symbol_occurrences_reports_zero_skipped_when_all_fit(tmp_path: Path) -> None:
    """没有文件被跳过时这个计数必须是 0，不能是缺键。"""
    (tmp_path / "a.py").write_text("def helper():\n    pass\n", encoding="utf-8")
    result = find_symbol_occurrences(tmp_path, "helper")
    assert result["files_skipped_too_large"] == 0
