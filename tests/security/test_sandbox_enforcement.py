"""沙箱是否真的能阻止越界操作？通过运行真实命令来断言。

这些测试要捕获的正是“读取正常但完全没有约束”的配置，因此这里不会检查配置
字符串。平台没有后端时会跳过；CI 会同时运行 Linux 和 macOS，因此跳过不能
掩盖任一平台上的回归。
"""

import socket
import sys
from pathlib import Path

import pytest

from haven.adapters.process_executor import ProcessExecutor
from haven.bootstrap import select_launcher
from haven.ports.executor import ExecSpec
from haven.ports.sandbox import SandboxSpec

LAUNCHER = select_launcher()
pytestmark = pytest.mark.skipif(
    LAUNCHER is None or not LAUNCHER.available(),
    reason="no OS sandbox backend on this platform",
)


def _spec(
    tmp_path: Path,
    private: Path | None = None,
    *,
    writable: bool = True,
    granted: Path | None = None,
) -> SandboxSpec:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    extra = [Path(sys.base_prefix), Path(sys.prefix)]
    if granted is not None:
        extra.append(granted)
    return SandboxSpec(
        workspace_root=workspace,
        scratch_dir=scratch,
        writable=writable,
        private_roots=(private,) if private is not None else (),
        extra_readable_roots=tuple(extra),
    )


async def run(
    tmp_path: Path,
    code: str,
    *,
    private: Path | None = None,
    timeout: float = 30.0,
    writable: bool = True,
    granted: Path | None = None,
) -> tuple[int, str]:
    spec = _spec(tmp_path, private, writable=writable, granted=granted)
    outcome = await ProcessExecutor(launcher=LAUNCHER).run_exec(
        ExecSpec(
            argv=(sys.executable, "-c", code),
            cwd=spec.workspace_root,
            timeout_seconds=timeout,
            sandbox=spec,
        )
    )
    return outcome.exit_code, outcome.stdout_tail + outcome.stderr_tail


class TestWriteConfinement:
    async def test_write_inside_the_workspace_succeeds(self, tmp_path: Path) -> None:
        exit_code, output = await run(tmp_path, "open('inside.txt','w').write('ok')")
        assert exit_code == 0, output
        assert (tmp_path / "ws" / "inside.txt").is_file()

    async def test_write_outside_the_workspace_is_blocked(self, tmp_path: Path) -> None:
        target = tmp_path / "escaped.txt"
        exit_code, _ = await run(tmp_path, f"open({str(target)!r},'w').write('pwned')")
        assert exit_code != 0
        assert not target.exists()

    @pytest.mark.skipif(
        sys.platform != "darwin",
        reason="Landlock subtree grants cannot carve .git out of a writable workspace",
    )
    async def test_write_into_dot_git_is_blocked(self, tmp_path: Path) -> None:
        (tmp_path / "ws").mkdir(exist_ok=True)
        (tmp_path / "ws" / ".git").mkdir(parents=True, exist_ok=True)
        exit_code, _ = await run(tmp_path, "open('.git/config','w').write('x')")
        assert exit_code != 0

    async def test_a_read_only_profile_blocks_workspace_writes(self, tmp_path: Path) -> None:
        """`repo.exec` 使用的配置（ADR 0017）会在所有平台上将工作区设为只读；
        这正是它堵住 Linux 漏洞的方式，因为 Landlock 无法从可写工作区中单独
        排除 `.git`。"""
        exit_code, _ = await run(tmp_path, "open('inside.txt','w').write('x')", writable=False)
        assert exit_code != 0
        assert not (tmp_path / "ws" / "inside.txt").exists()

    async def test_a_read_only_profile_blocks_dot_git_on_all_platforms(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "ws" / ".git").mkdir(parents=True, exist_ok=True)
        exit_code, _ = await run(tmp_path, "open('.git/config','w').write('x')", writable=False)
        assert exit_code != 0

    async def test_a_read_only_profile_still_allows_scratch_writes(self, tmp_path: Path) -> None:
        scratch_file = tmp_path / "scratch" / "out.txt"
        exit_code, output = await run(
            tmp_path,
            f"open({str(scratch_file)!r},'w').write('ok')",
            writable=False,
        )
        assert exit_code == 0, output
        assert scratch_file.is_file()


class TestReadConfinement:
    async def test_a_private_root_is_unreadable(self, tmp_path: Path) -> None:
        """`repo.exec` 校验的是 `cwd`，而不是 `argv` 中的路径；因此这项限制是
        命令与私钥之间唯一的屏障。"""
        home = tmp_path / "home"
        (home / ".ssh").mkdir(parents=True)
        (home / ".ssh" / "id_rsa").write_text("SECRET-KEY-MATERIAL")
        exit_code, output = await run(
            tmp_path, f"print(open({str(home / '.ssh' / 'id_rsa')!r}).read())", private=home
        )
        assert exit_code != 0
        assert "SECRET-KEY-MATERIAL" not in output

    async def test_a_declared_root_is_readable_and_its_sibling_is_not(self, tmp_path: Path) -> None:
        """ADR 0029：放宽后的边界必须与声明的范围完全一致。
        已授权的目录可以打开，旁边的目录仍保持关闭；两个探测只改变一个变量，
        这就是本断言的全部内容。

        这里的私有根目录是 `tmp_path` 下模拟的 home，而不是真实的 home。两个后端
        对它的处理完全相同（Seatbelt 先拒绝根目录，再重新允许已授权目录；Landlock
        根本不会列出该根目录），因此可以在不写入用户 `$HOME` 的情况下验证相同的
        规则顺序。
        """
        home = tmp_path / "home"
        granted = home / ".m2"
        withheld = home / ".ssh"
        granted.mkdir(parents=True)
        withheld.mkdir(parents=True)
        (granted / "f.txt").write_text("GRANTED-MARKER", encoding="utf-8")
        (withheld / "f.txt").write_text("WITHHELD-MARKER", encoding="utf-8")

        ok_code, ok_output = await run(
            tmp_path,
            f"print(open({str(granted / 'f.txt')!r}).read())",
            private=home,
            granted=granted,
        )
        nope_code, nope_output = await run(
            tmp_path,
            f"print(open({str(withheld / 'f.txt')!r}).read())",
            private=home,
            granted=granted,
        )

        assert ok_code == 0, ok_output
        assert "GRANTED-MARKER" in ok_output
        assert nope_code != 0
        assert "WITHHELD-MARKER" not in nope_output

    async def test_ordinary_programs_still_run(self, tmp_path: Path) -> None:
        """限制过严的配置与限制过松的配置同样是缺陷。"""
        exit_code, output = await run(tmp_path, "import os; print(len(os.listdir('/usr')))")
        assert exit_code == 0, output
        assert output.strip().isdigit()


class TestNetworkConfinement:
    async def test_tcp_connect_is_blocked(self, tmp_path: Path) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            exit_code, _ = await run(
                tmp_path,
                f"import socket; socket.create_connection(('127.0.0.1',{port}),timeout=5)",
            )
        finally:
            listener.close()
        assert exit_code != 0


class TestResourceBounds:
    async def test_timeout_terminates_the_process(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        outcome = await ProcessExecutor(launcher=LAUNCHER).run_exec(
            ExecSpec(
                argv=(sys.executable, "-c", "import time; time.sleep(60)"),
                cwd=spec.workspace_root,
                timeout_seconds=2.0,
                sandbox=spec,
            )
        )
        assert outcome.timed_out is True
        assert outcome.exit_code == 124

    async def test_output_bomb_is_truncated(self, tmp_path: Path) -> None:
        exit_code, output = await run(tmp_path, "print('x' * 5_000_000)")
        assert exit_code == 0
        assert len(output) < 200_000
