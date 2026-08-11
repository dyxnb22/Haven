"""Controlled subprocess executor for registered verification recipes.

Fixed argv only (never a shell), scrubbed environment, hard timeout with
terminate-then-kill, and bounded output capture that survives output bombs.
"""

from __future__ import annotations

import asyncio
import time
from asyncio.subprocess import Process
from pathlib import Path

from haven.contracts.tools import RecipeSpec
from haven.ports.executor import CheckOutcome

OUTPUT_CAP_BYTES = 64 * 1024
TERMINATE_GRACE_SECONDS = 2.0

ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "VIRTUAL_ENV")


class ProcessExecutor:
    """Implements ExecutorPort with asyncio subprocesses."""

    def __init__(self, extra_env: dict[str, str] | None = None) -> None:
        self._extra_env = dict(extra_env or {})

    async def run_recipe(self, recipe: RecipeSpec, workspace_root: Path) -> CheckOutcome:
        import os

        env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
        env.update(self._extra_env)

        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *recipe.argv,
            cwd=workspace_root,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            async with asyncio.timeout(recipe.timeout_seconds):
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
        return CheckOutcome(
            recipe_id=recipe.id,
            exit_code=124 if timed_out else exit_code,
            duration_ms=duration_ms,
            stdout_tail=out.decode("utf-8", errors="replace"),
            stderr_tail=err.decode("utf-8", errors="replace"),
            truncated=out_trunc or err_trunc,
            timed_out=timed_out,
        )

    @staticmethod
    async def _read_bounded(stream: asyncio.StreamReader | None) -> tuple[bytes, bool]:
        """Keep at most the last OUTPUT_CAP_BYTES; drain the rest so the child
        never blocks on a full pipe."""
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
