"""基于属性的检查：生成的路径都不能逃出工作区。"""

from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from haven.adapters.workspace_fs import FsWorkspace

# 通过拼接遍历片段、分隔符和异常片段构造类似路径的字符串。
_fragments = st.lists(
    st.sampled_from(["a", "b", "..", ".", "/", "\\", "~", "x", " ", "\x00", "%2e"]),
    min_size=0,
    max_size=12,
).map("".join)


def test_path_normalization_is_confined(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x")
    workspace = FsWorkspace(tmp_path)

    @given(raw=_fragments)
    def check(raw: str) -> None:
        facts = workspace.path_facts(raw)
        if facts.within_workspace:
            # 判定为内部的路径实际上必须解析到根目录之下
            resolved = (tmp_path / facts.normalized).resolve()
            assert resolved == tmp_path or tmp_path in resolved.parents

    check()


def test_known_escapes_all_rejected(tmp_path: Path) -> None:
    workspace = FsWorkspace(tmp_path)
    for raw in [
        "../outside",
        "../../etc/passwd",
        "/etc/passwd",
        "~/secret",
        "src/../../escape",
        "a/b/../../../c",
        "foo\x00bar",
    ]:
        assert not workspace.path_facts(raw).within_workspace
