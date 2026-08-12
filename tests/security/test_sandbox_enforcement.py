"""Does the sandbox actually stop things? Asserted by running real commands.

A profile that reads correctly and confines nothing is the failure mode these
tests exist to catch, so nothing here inspects a profile string. They skip when
the platform has no backend, and CI runs both Linux and macOS so a skip cannot
hide a regression on either.
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


def _spec(tmp_path: Path, private: Path | None = None) -> SandboxSpec:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return SandboxSpec(
        workspace_root=workspace,
        scratch_dir=scratch,
        writable=True,
        private_roots=(private,) if private is not None else (),
        extra_readable_roots=(Path(sys.base_prefix), Path(sys.prefix)),
    )


async def run(
    tmp_path: Path, code: str, *, private: Path | None = None, timeout: float = 30.0
) -> tuple[int, str]:
    spec = _spec(tmp_path, private)
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


class TestReadConfinement:
    async def test_a_private_root_is_unreadable(self, tmp_path: Path) -> None:
        """repo.exec validates cwd, not the paths inside argv, so this is the
        only thing standing between a command and a private key."""
        home = tmp_path / "home"
        (home / ".ssh").mkdir(parents=True)
        (home / ".ssh" / "id_rsa").write_text("SECRET-KEY-MATERIAL")
        exit_code, output = await run(
            tmp_path, f"print(open({str(home / '.ssh' / 'id_rsa')!r}).read())", private=home
        )
        assert exit_code != 0
        assert "SECRET-KEY-MATERIAL" not in output

    async def test_ordinary_programs_still_run(self, tmp_path: Path) -> None:
        """An over-tight profile is as much a bug as a leaky one."""
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
