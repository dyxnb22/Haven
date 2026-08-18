"""用于配方和沙箱命令的受控子进程执行器。

只接受固定 argv（绝不经过 shell），使用清理后的环境，采用先终止后强杀的硬超时，
并进行能够应对输出爆炸的有界输出捕获。每个子进程都在同一处由沙箱启动器包装，
因此未来调用方不会因为忘记包装而引入不受限的执行路径。
"""

from __future__ import annotations

import asyncio
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

#: check recipe 使用的临时目录。它位于工作区内，因为 recipe profile 已经
#: 授予工作区权限，从而不需要第二个可写根目录。
RECIPE_SCRATCH_DIRNAME = ".haven-scratch"


class ProcessExecutor:
    """使用 asyncio 子进程实现 ExecutorPort。"""

    def __init__(
        self,
        launcher: SandboxLauncher | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._launcher = launcher
        self._extra_env = dict(extra_env or {})

    async def run_recipe(self, recipe: RecipeSpec, workspace_root: Path) -> CheckOutcome:
        outcome = await self._run(
            recipe.argv,
            cwd=workspace_root,
            timeout_seconds=recipe.timeout_seconds,
            sandbox=SandboxSpec(
                workspace_root=workspace_root,
                scratch_dir=workspace_root / RECIPE_SCRATCH_DIRNAME,
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
        import os

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
        try:
            async with asyncio.timeout(timeout_seconds):
                stdout_task = asyncio.create_task(self._read_bounded(proc.stdout))
                stderr_task = asyncio.create_task(self._read_bounded(proc.stderr))
                (out, out_trunc), (err, err_trunc) = await asyncio.gather(stdout_task, stderr_task)
                await proc.wait()
        except TimeoutError:
            timed_out = True
            out, out_trunc, err, err_trunc = b"", False, b"", False
            await self._shutdown(proc)
        except asyncio.CancelledError:
            await self._shutdown(proc)
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
    async def _shutdown(proc: Process) -> None:
        if proc.returncode is not None:
            return
        proc.terminate()
        try:
            async with asyncio.timeout(TERMINATE_GRACE_SECONDS):
                await proc.wait()
        except TimeoutError:
            proc.kill()
            await proc.wait()
