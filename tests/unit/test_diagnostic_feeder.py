"""结构化诊断提取器测试（ADR 0040）。"""

from haven.application.diagnostic_feeder import (
    MAX_DIAGNOSTICS_SUMMARY_CHARS,
    extract_diagnostics,
    format_diagnostics_summary,
)


def test_current_ruff_multiline_output_including_unicode_paths() -> None:
    stdout = "F401 [*] `json` imported but unused\n --> src/计算 模块.py:7:8\n  |\n"
    diagnostics = extract_diagnostics("ruff", stdout, "", 1)
    assert len(diagnostics) == 1
    assert diagnostics[0].file == "src/计算 模块.py"
    assert (diagnostics[0].line, diagnostics[0].column) == (7, 8)
    assert diagnostics[0].rule_or_code == "F401"


def test_diagnostic_messages_and_summary_have_independent_limits() -> None:
    stdout = "\n".join(f"src/a.py:{index}:1: F401 {'x' * 10000}" for index in range(1, 100))
    diagnostics = extract_diagnostics("ruff", stdout, "", 1)
    assert len(diagnostics) == 25
    assert all(len(item.message) <= 500 for item in diagnostics)
    summary = format_diagnostics_summary(diagnostics)
    assert len(summary) <= MAX_DIAGNOSTICS_SUMMARY_CHARS
    assert "saved output" in summary


def test_malformed_huge_line_numbers_do_not_crash_diagnostic_parsing() -> None:
    stdout = f"F401 unused import\n --> src/a.py:{'9' * 5000}:1\n"
    assert extract_diagnostics("ruff", stdout, "", 1)


class TestDiagnosticFeeder:
    def test_zero_exit_code_returns_empty(self) -> None:
        stdout = "================ 10 passed in 0.05s ================\n"
        diags = extract_diagnostics("pytest", stdout, "", 0)
        assert diags == []
        assert format_diagnostics_summary(diags) == ""

    def test_extracts_pytest_short_summary_and_enriches_line(self) -> None:
        stdout = """
============================= test session starts ==============================
collected 2 items

tests/unit/test_calc.py .F

=================================== FAILURES ===================================
___________________________________ test_add ___________________________________
    def test_add():
>       assert add(2, 2) == 4
E       AssertionError: assert 0 == 4

tests/unit/test_calc.py:12: AssertionError
=========================== short test summary info ============================
FAILED tests/unit/test_calc.py::test_add - AssertionError: assert 0 == 4
========================= 1 failed, 1 passed in 0.05s ==========================
"""
        diags = extract_diagnostics("pytest", stdout, "", 1)
        assert len(diags) == 1
        d = diags[0]
        assert d.file == "tests/unit/test_calc.py"
        assert d.line == 12
        assert d.severity == "failure"
        assert d.rule_or_code == "pytest"
        assert "test_add - AssertionError: assert 0 == 4" in d.message

        summary = format_diagnostics_summary(diags)
        assert "Found 1 diagnostic issue(s):" in summary
        assert "tests/unit/test_calc.py:12" in summary

    def test_extracts_ruff_linter_errors(self) -> None:
        stdout = """
src/haven/main.py:14:1: F401 `os` imported but unused
src/haven/main.py:25:80: E501 Line too long (120 > 80)
Found 2 errors.
"""
        diags = extract_diagnostics("ruff", stdout, "", 1)
        assert len(diags) == 2
        assert diags[0].file == "src/haven/main.py"
        assert diags[0].line == 14
        assert diags[0].column == 1
        assert diags[0].rule_or_code == "F401"
        assert "`os` imported but unused" in diags[0].message

        assert diags[1].file == "src/haven/main.py"
        assert diags[1].line == 25
        assert diags[1].column == 80
        assert diags[1].rule_or_code == "E501"

    def test_extracts_mypy_type_errors(self) -> None:
        stdout = """
src/calc.py:15: error: Incompatible return value type (got "str", expected "int")  [return-value]
src/calc.py:20:5: warning: Unused variable 'x'
Found 1 error and 1 warning in 1 file
"""
        diags = extract_diagnostics("mypy", stdout, "", 1)
        assert len(diags) == 2
        assert diags[0].file == "src/calc.py"
        assert diags[0].line == 15
        assert diags[0].severity == "error"
        assert diags[0].rule_or_code == "return-value"
        assert "Incompatible return value type" in diags[0].message

        assert diags[1].file == "src/calc.py"
        assert diags[1].line == 20
        assert diags[1].column == 5
        assert diags[1].severity == "warning"

    def test_extracts_python_traceback(self) -> None:
        stderr = """
Traceback (most recent call last):
  File "/app/src/runner.py", line 42, in <module>
    run()
  File "/app/src/runner.py", line 18, in run
    raise ValueError("invalid configuration provided")
ValueError: invalid configuration provided
"""
        diags = extract_diagnostics("exec", "", stderr, 1)
        assert len(diags) == 1
        d = diags[0]
        assert d.file == "/app/src/runner.py"
        assert d.line == 18
        assert d.severity == "error"
        assert d.rule_or_code == "ValueError"
        assert d.message == "ValueError: invalid configuration provided"

    def test_extracts_multiple_pytest_failures_in_same_file_without_collision(self) -> None:
        stdout = """
=================================== FAILURES ===================================
___________________________________ test_add ___________________________________
def test_add():
>   assert add(2, 2) == 4
E   AssertionError: assert 0 == 4

tests/unit/test_calc.py:12: AssertionError
___________________________________ test_sub ___________________________________
def test_sub():
>   assert sub(2, 2) == 1
E   AssertionError: assert 0 == 1

tests/unit/test_calc.py:35: AssertionError
=========================== short test summary info ============================
FAILED tests/unit/test_calc.py::test_add - AssertionError: assert 0 == 4
FAILED tests/unit/test_calc.py::test_sub - AssertionError: assert 0 == 1
======================== 2 failed in 0.05s ==========================
"""
        diags = extract_diagnostics("pytest", stdout, "", 1)
        assert len(diags) == 2
        assert diags[0].file == "tests/unit/test_calc.py"
        assert diags[0].line == 12
        assert "test_add" in diags[0].message

        assert diags[1].file == "tests/unit/test_calc.py"
        assert diags[1].line == 35
        assert "test_sub" in diags[1].message

    def test_extracts_pytest_fixture_error_and_collection_error(self) -> None:
        stdout = """
ERROR collecting tests/test_broken.py
=========================== short test summary info ============================
ERROR tests/unit/test_calc.py::test_init - RuntimeError: fixture setup failed
"""
        diags = extract_diagnostics("pytest", stdout, "", 1)
        assert len(diags) == 2
        assert diags[0].file == "tests/test_broken.py"
        assert diags[0].rule_or_code == "pytest:collection"

        assert diags[1].file == "tests/unit/test_calc.py"
        assert diags[1].severity == "failure"
        assert "test_init" in diags[1].message

    def test_extracts_python_traceback_with_dotted_and_custom_exceptions(self) -> None:
        stderr = """
Traceback (most recent call last):
  File "/app/src/parser.py", line 10, in parse
    x: int = 1
    raise pydantic_core._pydantic_core.ValidationError("validation failed")
pydantic_core._pydantic_core.ValidationError: validation failed
"""
        diags = extract_diagnostics("exec", "", stderr, 1)
        assert len(diags) == 1
        d = diags[0]
        assert d.file == "/app/src/parser.py"
        assert d.line == 10
        assert d.rule_or_code == "pydantic_core._pydantic_core.ValidationError"
        assert "validation failed" in d.message

    def test_extracts_python_traceback_custom_exception(self) -> None:
        stderr = """
Traceback (most recent call last):
  File "/app/src/lease.py", line 40, in acquire
    raise LeaseHeld("lock is already held")
LeaseHeld: lock is already held
"""
        diags = extract_diagnostics("exec", "", stderr, 1)
        assert len(diags) == 1
        d = diags[0]
        assert d.file == "/app/src/lease.py"
        assert d.line == 40
        assert d.rule_or_code == "LeaseHeld"
        assert "lock is already held" in d.message

    def test_fallback_diagnostics_for_generic_error(self) -> None:
        stderr = (
            "Command failed with some unknown internal error: disk is full\n"
            "FATAL: process terminated"
        )
        diags = extract_diagnostics("exec", "", stderr, 1)
        assert len(diags) >= 1
        assert any("FATAL:" in d.message for d in diags)

    def test_extracts_ansi_colored_pytest_summary(self) -> None:
        stdout = (
            "\x1b[31m=== FAILURES ===\x1b[0m\n"
            "\x1b[31m\x1b[1m_____ test_color _____\x1b[0m\n"
            "\x1b[31mtests/unit/test_color.py\x1b[0m:15: AssertionError\n"
            "\x1b[31mFAILED\x1b[0m tests/unit/test_color.py::test_color - "
            "\x1b[31mAssertionError: colored fail\x1b[0m\n"
        )
        diags = extract_diagnostics("pytest", stdout, "", 1)
        assert len(diags) == 1
        d = diags[0]
        assert d.file == "tests/unit/test_color.py"
        assert d.line == 15
        assert "AssertionError: colored fail" in d.message

    def test_extracts_rust_compiler_errors(self) -> None:
        stderr = """
error[E0425]: cannot find value `foo` in this scope
  --> src/main.rs:12:5
   |
12 |     foo();
   |     ^^^ not found in this scope
"""
        diags = extract_diagnostics("cargo", "", stderr, 1)
        assert len(diags) == 1
        d = diags[0]
        assert d.file == "src/main.rs"
        assert d.line == 12
        assert d.column == 5
        assert d.rule_or_code == "E0425"
        assert "cannot find value `foo`" in d.message

    def test_extracts_typescript_compiler_errors(self) -> None:
        stderr = (
            "src/app.ts(15,22): error TS2304: Cannot find name 'myVar'.\n"
            "src/utils.tsx:42:10 - error TS2322: Type 'string' is not assignable to number.\n"
        )
        diags = extract_diagnostics("tsc", "", stderr, 1)
        assert len(diags) == 2
        assert diags[0].file == "src/app.ts"
        assert diags[0].line == 15
        assert diags[0].column == 22
        assert diags[0].rule_or_code == "TS2304"
        assert "Cannot find name 'myVar'" in diags[0].message

        assert diags[1].file == "src/utils.tsx"
        assert diags[1].line == 42
        assert diags[1].column == 10
        assert diags[1].rule_or_code == "TS2322"

    def test_extracts_go_compiler_errors(self) -> None:
        stderr = "main.go:12:5: undefined: myHandler\n"
        diags = extract_diagnostics("go", "", stderr, 1)
        assert len(diags) == 1
        assert diags[0].file == "main.go"
        assert diags[0].line == 12
        assert diags[0].column == 5
        assert "undefined: myHandler" in diags[0].message
