"""用于配方和沙箱命令的受控子进程执行器。

只接受固定 argv（绝不经过 shell），使用清理后的环境，采用先终止后强杀的硬超时，
并进行能够应对输出爆炸的有界输出捕获。每个子进程都在同一处由沙箱启动器包装，
因此未来调用方不会因为忘记包装而引入不受限的执行路径。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import tempfile
import time
from asyncio.subprocess import Process
from pathlib import Path

from haven.contracts.tools import RecipeSpec
from haven.ports.executor import CheckOutcome, ExecOutcome, ExecSpec
from haven.ports.sandbox import (
    SandboxLauncher,
    SandboxSpec,
    default_private_roots,
    default_readable_roots,
)

OUTPUT_CAP_BYTES = 64 * 1024
TERMINATE_GRACE_SECONDS = 2.0

ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "VIRTUAL_ENV")

#: 旧版本 check recipe 使用的工作区内临时目录名。保留该常量只为让评估夹具
#: 忽略历史残留；新执行不会信任或创建这个可由仓库预置的路径。
RECIPE_SCRATCH_DIRNAME = ".haven-scratch"


class ProcessExecutor:
    """通过沙箱启动进程，并将 stdout/stderr 截断为有限大小。

    使用 asyncio 子进程实现 ExecutorPort。
    """

    def __init__(
        self,
        launcher: SandboxLauncher | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._launcher = launcher
        self._extra_env = dict(extra_env or {})

    async def run_recipe(self, recipe: RecipeSpec, workspace_root: Path) -> CheckOutcome:
        """将已注册 recipe 转换为沙箱规格并执行，返回检查专用结果。

        每次调用都在工作区外原子创建并自行回收临时目录；绝不采用仓库可预置的
        固定路径，也不把这个资源生命周期泄漏到执行器端口中。
        """
        scratch_dir = Path(tempfile.mkdtemp(prefix="haven-recipe-scratch-"))
        try:
            outcome = await self._run(
                recipe.argv,
                cwd=workspace_root,
                timeout_seconds=recipe.timeout_seconds,
                sandbox=SandboxSpec(
                    workspace_root=workspace_root,
                    scratch_dir=scratch_dir,
                    writable=True,
                    allow_network=recipe.allow_network,
                    private_roots=default_private_roots(),
                    # 在解释器前缀之上追加，绝不替换，并且只读。它来自配置加载时固定在
                    # 磁盘上的 RecipeSpec，因此不会有模型提供的字符串到达这里（ADR 0029）。
                    extra_readable_roots=(
                        *default_readable_roots(),
                        *(Path(root).expanduser().resolve() for root in recipe.readable_roots),
                    ),
                ),
            )
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)
        return CheckOutcome(
            recipe_id=recipe.id,
            exit_code=outcome.exit_code,
            duration_ms=outcome.duration_ms,
            stdout_tail=outcome.stdout_tail,
            stderr_tail=outcome.stderr_tail,
            truncated=outcome.truncated,
            timed_out=outcome.timed_out,
        )

    async def run_exec(self, spec: ExecSpec) -> ExecOutcome:
        """执行已构造的沙箱命令规格，不解释 shell 语法。"""
        return await self._run(
            spec.argv, cwd=spec.cwd, timeout_seconds=spec.timeout_seconds, sandbox=spec.sandbox
        )

    async def _run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
        sandbox: SandboxSpec,
    ) -> ExecOutcome:
        env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
        sandbox.scratch_dir.mkdir(parents=True, exist_ok=True)
        env["TMPDIR"] = str(sandbox.scratch_dir)
        env.update(self._extra_env)

        command = self._launcher.wrap(argv, sandbox) if self._launcher is not None else argv

        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # 独立进程组使超时/取消能够终止命令生成的整棵子进程树；否则孙进程
                # 可以在快照完成后继续修改工作区，绕开副作用归因。
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            # 未安装的程序必须表现为命令失败，而不是以异常逃逸到代理循环中。
            # 127 是 shell 对“找不到命令”的约定值。
            return ExecOutcome(
                exit_code=127,
                duration_ms=int((time.monotonic() - started) * 1000),
                stdout_tail="",
                stderr_tail=f"could not start {argv[0]!r}: {exc.strerror or exc}",
                truncated=False,
                timed_out=False,
            )
        timed_out = False
        stdout_task = asyncio.create_task(self._read_bounded(proc.stdout))
        stderr_task = asyncio.create_task(self._read_bounded(proc.stderr))
        try:
            async with asyncio.timeout(timeout_seconds):
                await self._wait_for_leader_exit(proc)
                # 读取任务始终并发排空管道，防止主进程阻塞；但主进程一退出就先
                # 清理其进程组，再等待 EOF。否则继承管道的后台子进程可以把 EOF
                # 拖到自己完成副作用之后，令“正常退出后清理”失去意义。
                await self._cleanup_descendants(proc)
                (out, out_trunc), (err, err_trunc) = await asyncio.gather(stdout_task, stderr_task)
                await proc.wait()
        except TimeoutError:
            timed_out = True
            out, out_trunc, err, err_trunc = b"", False, b"", False
            await self._shutdown(proc)
            await self._cancel_readers(stdout_task, stderr_task)
        except asyncio.CancelledError:
            await self._shutdown(proc)
            await self._cancel_readers(stdout_task, stderr_task)
            raise

        duration_ms = int((time.monotonic() - started) * 1000)
        exit_code = proc.returncode if proc.returncode is not None else -1
        return ExecOutcome(
            exit_code=124 if timed_out else exit_code,
            duration_ms=duration_ms,
            stdout_tail=out.decode("utf-8", errors="replace"),
            stderr_tail=err.decode("utf-8", errors="replace"),
            truncated=out_trunc or err_trunc,
            timed_out=timed_out,
        )

    @staticmethod
    async def _read_bounded(stream: asyncio.StreamReader | None) -> tuple[bytes, bool]:
        """最多保留末尾的 OUTPUT_CAP_BYTES；其余内容继续排空，避免子进程
        因管道写满而阻塞。"""
        if stream is None:
            return b"", False
        buffer = b""
        truncated = False
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > OUTPUT_CAP_BYTES:
                buffer = buffer[-OUTPUT_CAP_BYTES:]
                truncated = True
        return buffer, truncated

    @staticmethod
    async def _cancel_readers(*tasks: asyncio.Task[tuple[bytes, bool]]) -> None:
        """终止并回收仍等待子进程管道的读取任务。"""
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    async def _wait_for_leader_exit(proc: Process) -> None:
        """等待主进程退出，但不等待其后代继承的管道关闭。

        ``asyncio.Process.wait()`` 的传输层可能要等 stdout/stderr EOF；后台后代
        恰好可以持有这些描述符。因此用公开的 ``returncode`` 状态等待子进程
        watcher 报告主进程退出，再立即清理专属进程组。
        """
        while proc.returncode is None:
            await asyncio.sleep(0.01)

    @classmethod
    async def _shutdown(cls, proc: Process) -> None:
        if os.name == "posix":
            cls._signal_group(proc.pid, signal.SIGTERM)
        elif proc.returncode is None:  # pragma: no cover - Windows is not a supported sandbox host
            proc.terminate()
        try:
            async with asyncio.timeout(TERMINATE_GRACE_SECONDS):
                if proc.returncode is None:
                    await proc.wait()
                if os.name == "posix":
                    await cls._wait_for_group_exit(proc.pid)
        except TimeoutError:
            if os.name == "posix":
                cls._signal_group(proc.pid, signal.SIGKILL)
            elif proc.returncode is None:  # pragma: no cover - see above
                proc.kill()
            if proc.returncode is None:
                await proc.wait()

    @classmethod
    async def _cleanup_descendants(cls, proc: Process) -> None:
        """正常退出后也清理仍留在专属进程组中的后台子进程。"""
        if os.name != "posix":  # pragma: no cover - see _shutdown
            return
        cls._signal_group(proc.pid, signal.SIGTERM)
        try:
            async with asyncio.timeout(TERMINATE_GRACE_SECONDS):
                await cls._wait_for_group_exit(proc.pid)
        except TimeoutError:
            cls._signal_group(proc.pid, signal.SIGKILL)

    @staticmethod
    async def _wait_for_group_exit(process_group: int) -> None:
        while True:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                return
            except PermissionError:
                return
            await asyncio.sleep(0.02)

    @staticmethod
    def _signal_group(process_group: int, sig: signal.Signals) -> None:
        try:
            os.killpg(process_group, sig)
        except ProcessLookupError:
            return
