"""基于 AST 与结构化语法的预检门禁测试（ADR 0040）。"""

import asyncio
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


class TestDiagnosticQuality:
    """错误诊断要让模型能直接定位并修好——这半边此前无人把守。

    变异测试实测：把 JSON/TOML 的出错行文本、插入符行、`to_dict` 的字段省略、
    后缀大小写不敏感分别改坏，1587 条测试依然全绿。
    """

    def test_json_errors_carry_the_offending_line(self) -> None:
        content = '{\n  "a": 1,\n  "b": oops,\n}\n'
        diag = validate_syntax("config.json", content)
        assert not diag.is_valid
        assert diag.line == 3
        assert diag.column is not None
        assert diag.text is not None and "oops" in diag.text, "模型需要看到出错的那一行"

    def test_toml_errors_carry_line_column_and_text(self) -> None:
        content = '[tool]\nname = "ok"\nbad line here\n'
        diag = validate_syntax("config.toml", content)
        assert not diag.is_valid
        assert diag.line == 3, "TOML 报错信息里带 (at line N, column M)，必须解析出来"
        assert diag.column is not None
        assert diag.text is not None and "bad line" in diag.text

    def test_the_caret_points_at_the_column(self) -> None:
        diag = validate_syntax("src/calc.py", "def broken(\n    return 42\n")
        formatted = format_syntax_error(diag)
        caret_lines = [line for line in formatted.splitlines() if line.strip() == "^"]
        assert caret_lines, "出错行下面必须有一根插入符，否则模型还得自己数字符"
        # 插入符缩进应指向出错列。
        assert caret_lines[0].index("^") == 4 + (diag.column or 1) - 1

    def test_the_offending_source_line_is_indented_under_the_header(self) -> None:
        """出错行的原文要缩进排在标题下面——`SyntaxError.text` 是*检出*错误的
        那一行，不一定是问题真正的来源，所以这里断言的是排版而非具体内容。"""
        diag = validate_syntax("src/calc.py", "def broken(\n    return 42\n")
        formatted = format_syntax_error(diag)
        assert diag.text is not None
        assert f"    {diag.text}" in formatted

    def test_to_dict_omits_fields_that_are_not_set(self) -> None:
        """有效诊断不该带一堆 null——那是给模型看的 JSON。"""
        ok = validate_syntax("src/calc.py", "x = 1\n").to_dict()
        assert ok == {"path": "src/calc.py", "is_valid": True}

    def test_to_dict_includes_only_the_present_fields(self) -> None:
        diag = validate_syntax("config.json", '{"a": oops}')
        out = diag.to_dict()
        assert out["is_valid"] is False
        assert out["error_type"] == "JSONDecodeError"
        assert set(out) == {"path", "is_valid", "error_type", "message", "line", "column", "text"}

    def test_the_suffix_match_ignores_case(self) -> None:
        """`Foo.PY` 也要校验——大小写不同的后缀同样是要落盘的 Python。"""
        assert not validate_syntax("src/Foo.PY", "def broken(\n").is_valid
        assert validate_syntax("Config.JSON", '{"a": 1}').is_valid

    def test_an_empty_file_is_valid(self) -> None:
        assert validate_syntax("src/empty.py", "").is_valid
        assert validate_syntax("config.json", "").is_valid is False  # 空 JSON 不是合法 JSON

    def test_a_path_without_a_suffix_is_valid(self) -> None:
        assert validate_syntax("Makefile", "all:\n\techo hi").is_valid


class TestSubprocessParseGate:
    """大块的语法解析必须走子进程，否则事件循环仍被 GIL 按住。

    实测：240 KB 文件一次编辑预览，卸载到线程时循环停顿约 85 ms（`ast.parse` 是纯
    Python，占着 GIL，A/B 差异在噪声量级）；换成进程后 6.3 ms。
    """

    async def test_a_large_block_is_parsed_in_a_subprocess(self) -> None:
        from haven.domain import syntax_gate

        content = "x = 1\n" * 40000  # 远超阈值
        assert len(content) > syntax_gate.SUBPROCESS_PARSE_THRESHOLD
        calls: list[str] = []
        real = asyncio.create_subprocess_exec

        async def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append("subprocess")
            return await real(*args, **kwargs)

        asyncio.create_subprocess_exec = spy  # type: ignore[assignment]
        try:
            diag = await syntax_gate.validate_syntax_async("big.py", content)
        finally:
            asyncio.create_subprocess_exec = real  # type: ignore[assignment]

        assert calls == ["subprocess"], "大块必须走子进程"
        assert diag.is_valid is True

    async def test_a_small_block_stays_in_process(self) -> None:
        """小子块走进程反而更慢（解释器启动几十毫秒），阈值以下不该绕远路。"""
        from haven.domain import syntax_gate

        content = "x = 1\n"
        assert len(content) < syntax_gate.SUBPROCESS_PARSE_THRESHOLD
        calls: list[str] = []
        real = asyncio.create_subprocess_exec

        async def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append("subprocess")
            return await real(*args, **kwargs)

        asyncio.create_subprocess_exec = spy  # type: ignore[assignment]
        try:
            diag = await syntax_gate.validate_syntax_async("small.py", content)
        finally:
            asyncio.create_subprocess_exec = real  # type: ignore[assignment]

        assert calls == []
        assert diag.is_valid is True

    async def test_a_broken_file_is_still_rejected_through_the_subprocess(self) -> None:
        from haven.domain import syntax_gate

        content = "def f(:\n  pass\n" + "y = 2\n" * 40000
        diag = await syntax_gate.validate_syntax_async("big.py", content)
        assert diag.is_valid is False
        assert diag.error_type == "SyntaxError"
        assert diag.line == 1

    @pytest.mark.parametrize("failure", ["spawn", "exit", "garbage", "exit_but_valid_json"])
    async def test_a_failing_worker_falls_back_to_in_process(
        self, monkeypatch: pytest.MonkeyPatch, failure: str
    ) -> None:
        """这是**预写门禁**：worker 答不出来必须退回进程内解析，而不是猜一个结果。

        正确性优先于界面响应——慢一点没关系，把门禁结果猜出来不行。
        """
        from haven.domain import syntax_gate

        # 只有"非零退出但吐合法 JSON"那一项需要坏内容——否则撒谎的 worker 会碰巧
        # 说出正确答案，那道 returncode 检查就被掩盖了。
        content = (
            "def f(:\n  pass\n" + "y = 2\n" * 40000
            if failure == "exit_but_valid_json"
            else "x = 1\n" * 40000
        )

        if failure == "spawn":

            async def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
                raise OSError("cannot fork")

            monkeypatch.setattr(syntax_gate.asyncio, "create_subprocess_exec", boom)
        elif failure == "exit":

            class BadProc:
                returncode = 3

                async def communicate(self, _data):  # type: ignore[no-untyped-def]
                    return b"", b""

            async def fake_spawn(*args, **kwargs):  # type: ignore[no-untyped-def]
                return BadProc()

            monkeypatch.setattr(syntax_gate.asyncio, "create_subprocess_exec", fake_spawn)
        elif failure == "garbage":

            class GarbageProc:
                returncode = 0

                async def communicate(self, _data):  # type: ignore[no-untyped-def]
                    return b'{"not":"a diagnostic"}', b""

            async def fake_spawn(*args, **kwargs):  # type: ignore[no-untyped-def]
                return GarbageProc()

            monkeypatch.setattr(syntax_gate.asyncio, "create_subprocess_exec", fake_spawn)
        else:
            # 非零退出，却对一份**坏**内容声称合法：绝不能因为"解析得出来"就采信一个
            # 已经失败的进程——那等于让一次崩溃决定一次写操作能否落盘。
            class LyingProc:
                returncode = 3

                async def communicate(self, _data):  # type: ignore[no-untyped-def]
                    import json

                    return json.dumps({"path": "big.py", "is_valid": True}).encode(), b""

            async def fake_spawn(*args, **kwargs):  # type: ignore[no-untyped-def]
                return LyingProc()

            monkeypatch.setattr(syntax_gate.asyncio, "create_subprocess_exec", fake_spawn)

        diag = await syntax_gate.validate_syntax_async("big.py", content)
        if failure == "exit_but_valid_json":
            # `content` 在这个用例里是坏语法，所以"is_valid=True"只可能来自那个撒谎的
            # worker；采信它就意味着一次崩溃放行了破损代码。
            assert "def f(:" in content
            assert diag.is_valid is False, "不能采信一个已经失败的进程"
        else:
            assert diag.is_valid is True, "回退路径必须给出和进程内一致的正确结果"

    async def test_a_broken_file_falls_back_correctly_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """回退路径对坏输入也必须失败即关闭，不能因为绕了远路就放过。"""
        from haven.domain import syntax_gate

        content = "def f(:\n  pass\n" + "y = 2\n" * 40000

        async def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise OSError("cannot fork")

        monkeypatch.setattr(syntax_gate.asyncio, "create_subprocess_exec", boom)
        diag = await syntax_gate.validate_syntax_async("big.py", content)
        assert diag.is_valid is False
        assert diag.error_type == "SyntaxError"
