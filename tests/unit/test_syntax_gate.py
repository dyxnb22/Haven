"""基于 AST 与结构化语法的预检门禁测试（ADR 0040）。"""

from pathlib import Path

import pytest

from haven.adapters.workspace_fs import FsWorkspace
from haven.domain.syntax_gate import (
    format_syntax_error,
    validate_syntax,
)
from haven.ports.workspace import WorkspaceError


class TestSyntaxGateUnit:
    def test_valid_python(self) -> None:
        code = "def add(a: int, b: int) -> int:\n    return a + b\n"
        diag = validate_syntax("src/calc.py", code)
        assert diag.is_valid
        assert diag.error_type is None
        assert format_syntax_error(diag) == ""

    def test_python_syntax_error(self) -> None:
        code = "def broken(\n    return 42\n"
        diag = validate_syntax("src/calc.py", code)
        assert not diag.is_valid
        assert diag.error_type == "SyntaxError"
        assert diag.line == 1
        formatted = format_syntax_error(diag)
        assert "Syntax validation failed for 'src/calc.py'" in formatted
        assert "SyntaxError at line 1" in formatted

    def test_python_indentation_error(self) -> None:
        code = "def foo():\nreturn 1\n"
        diag = validate_syntax("src/foo.py", code)
        assert not diag.is_valid
        assert diag.error_type == "IndentationError"
        assert diag.line == 2

    def test_valid_json(self) -> None:
        diag = validate_syntax("config.json", '{"key": "value", "list": [1, 2]}')
        assert diag.is_valid

    def test_invalid_json(self) -> None:
        diag = validate_syntax("config.json", '{"key": "value", trailing_comma,}')
        assert not diag.is_valid
        assert diag.error_type == "JSONDecodeError"
        assert diag.line is not None
        assert "JSONDecodeError" in format_syntax_error(diag)

    def test_valid_toml(self) -> None:
        diag = validate_syntax("config.toml", '[tool.haven]\nname = "test"\n')
        assert diag.is_valid

    def test_invalid_toml(self) -> None:
        diag = validate_syntax("config.toml", 'tool = "val"\n[tool]\nkey = "err"')
        assert not diag.is_valid
        assert diag.error_type == "TOMLDecodeError"

    def test_non_code_files_pass_by_default(self) -> None:
        diag = validate_syntax(
            "README.md", "Just plain text with whatever brackets {[( none closed"
        )
        assert diag.is_valid
        diag = validate_syntax("notes.txt", "def totally_not_python() no colon")
        assert diag.is_valid

    def test_python_null_byte_fails_validation(self) -> None:
        diag = validate_syntax("src/corrupt.py", "def foo():\n    return 'hello\x00world'\n")
        assert not diag.is_valid
        assert diag.error_type in ("SyntaxError", "ValueError")


@pytest.fixture()
def ws(tmp_path: Path) -> FsWorkspace:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    return FsWorkspace(tmp_path)


class TestWorkspaceSyntaxProtection:
    async def test_edit_with_broken_python_fails_closed(self, ws: FsWorkspace) -> None:
        preview = await ws.preview_edit("src/calc.py", "return a + b", "return a + b")

        with pytest.raises(WorkspaceError) as exc_preview:
            await ws.preview_edit("src/calc.py", "return a + b", "def broken(\n")
        assert exc_preview.value.code == "syntax_error"
        assert "Syntax validation failed" in str(exc_preview.value)

        with pytest.raises(WorkspaceError) as exc_apply:
            await ws.apply_edit(
                "src/calc.py",
                "return a + b",
                "def broken(\n",
                preview.preimage_digest,
            )
        assert exc_apply.value.code == "syntax_error"

        # 磁盘原内容完全未受污染
        assert (ws.root / "src" / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"

    async def test_create_with_broken_python_fails_closed(self, ws: FsWorkspace) -> None:
        with pytest.raises(WorkspaceError) as exc_preview:
            await ws.preview_create("src/broken.py", "def foo(\n")
        assert exc_preview.value.code == "syntax_error"

        with pytest.raises(WorkspaceError) as exc_apply:
            await ws.apply_create("src/broken.py", "def foo(\n")
        assert exc_apply.value.code == "syntax_error"

        assert not (ws.root / "src" / "broken.py").exists()

    async def test_skip_syntax_check_allows_bypass(self, ws: FsWorkspace) -> None:
        # 当显式指定 skip_syntax_check=True 时放行
        preview = await ws.preview_create(
            "src/raw.py", "invalid python syntax!", skip_syntax_check=True
        )
        assert preview.insertions == 1

        outcome = await ws.apply_create(
            "src/raw.py", "invalid python syntax!", skip_syntax_check=True
        )
        assert outcome.postimage_digest
        assert (ws.root / "src" / "raw.py").read_text() == "invalid python syntax!"
