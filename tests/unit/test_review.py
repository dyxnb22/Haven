"""确定性差异审查（ADR 0007）：捕获明显危险的内容。"""

from haven.domain.review import review_diff


def diff(added: list[str], removed: list[str] | None = None, path: str = "src/app.py") -> str:
    lines = [f"--- a/{path}", f"+++ b/{path}", "@@ -1,3 +1,3 @@"]
    lines += [f"-{line}" for line in (removed or [])]
    lines += [f"+{line}" for line in added]
    return "\n".join(lines) + "\n"


def codes(text: str) -> set[str]:
    return {f.code for f in review_diff(text)}


class TestSecrets:
    def test_private_key_block(self) -> None:
        assert "secret_private_key" in codes(diff(["-----BEGIN RSA PRIVATE KEY-----"]))

    def test_aws_access_key(self) -> None:
        assert "secret_aws_key" in codes(diff(['KEY = "AKIAIOSFODNN7EXAMPLE"']))

    def test_api_token(self) -> None:
        assert "secret_api_token" in codes(diff(['token = "sk-abcdefghijklmnopqrstuvwxyz012"']))

    def test_hardcoded_password(self) -> None:
        assert "secret_hardcoded_password" in codes(diff(['password = "hunter2hunter2"']))

    def test_placeholders_are_not_flagged(self) -> None:
        for line in (
            'password = "changeme"',
            'api_key = "your-key-here"',
            'password = "xxxxxxxxxxxx"',
            'api_key = "<YOUR_KEY>"',
        ):
            assert codes(diff([line])) == set(), line

    def test_short_values_are_not_flagged(self) -> None:
        assert codes(diff(['password = "abc"'])) == set()


class TestConflictMarkers:
    def test_conflict_markers_detected(self) -> None:
        for marker in ("<<<<<<< HEAD", "=======", ">>>>>>> feature"):
            assert "merge_conflict_marker" in codes(diff([marker])), marker

    def test_similar_text_is_not_a_marker(self) -> None:
        assert codes(diff(["x = a << 7", "# ====== section ======"])) == set()


class TestDebugLeftovers:
    def test_python_breakpoints(self) -> None:
        assert "debug_leftover" in codes(diff(["    breakpoint()"]))
        assert "debug_leftover" in codes(diff(["    import pdb; pdb.set_trace()"]))

    def test_js_debugger(self) -> None:
        assert "debug_leftover" in codes(diff(["  debugger;"]))

    def test_normal_logging_is_not_flagged(self) -> None:
        assert codes(diff(['print("hello")', "console.log(x)", "logger.debug(x)"])) == set()


class TestMassDeletion:
    def test_blanked_file_is_flagged(self) -> None:
        text = diff(added=["pass"], removed=[f"line {i}" for i in range(80)])
        assert "mass_deletion" in codes(text)

        deleted = text.replace("+++ b/src/app.py", "+++ /dev/null")
        findings = review_diff(deleted)
        assert findings[0].path == "src/app.py"

    def test_small_deletion_is_not_flagged(self) -> None:
        text = diff(added=["new"], removed=[f"line {i}" for i in range(10)])
        assert codes(text) == set()

    def test_large_rewrite_is_not_flagged(self) -> None:
        text = diff(
            added=[f"new {i}" for i in range(70)],
            removed=[f"old {i}" for i in range(80)],
        )
        assert codes(text) == set()


class TestScope:
    def test_only_added_lines_are_reviewed(self) -> None:
        """删除原本已存在的内容绝不能触发发现。"""
        text = diff(added=["clean line"], removed=['password = "hunter2hunter2"'])
        assert codes(text) == set()

    def test_context_lines_are_ignored(self) -> None:
        text = (
            "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n"
            ' password = "hunter2hunter2"\n'  # 未改变的上下文行
            "+clean = 1\n"
        )
        assert codes(text) == set()

    def test_finding_records_the_file(self) -> None:
        findings = review_diff(diff(["    breakpoint()"], path="src/deep/mod.py"))
        assert findings[0].path == "src/deep/mod.py"

    def test_clean_diff_has_no_findings(self) -> None:
        assert review_diff(diff(["    return a + b"], removed=["    return a - b"])) == ()

    def test_empty_diff_has_no_findings(self) -> None:
        assert review_diff("") == ()

    def test_findings_are_deduplicated_per_file(self) -> None:
        text = diff(["    breakpoint()", "    breakpoint()", "    breakpoint()"])
        assert len(review_diff(text)) == 1
