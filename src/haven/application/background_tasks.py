"""后台任务与受控进程池（ADR 0037）。

为开发服务器、长期编译构建或持续测试提供沙箱隔离的后台子进程池。
维护行缓冲环形队列，支持随时查询状态、查看有界日志与优雅终止。
在运行结束或异常退出时，确保所有后台孤儿进程被强制清理回收。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from haven.ports.sandbox import SandboxLauncher, SandboxSpec


@dataclass
class BackgroundTask:
    """一个受控的后台子进程实例。"""

    task_id: str
    argv: tuple[str, ...]
    cwd: Path
    description: str
    started_at: float
    process: asyncio.subprocess.Process | None = None
    ended_at: float | None = None
    exit_code: int | None = None
    stdout_buffer: deque[str] = field(default_factory=lambda: deque(maxlen=1000))
    stderr_buffer: deque[str] = field(default_factory=lambda: deque(maxlen=1000))
    reader_tasks: list[asyncio.Task[None]] = field(default_factory=list)

    @property
    def is_running(self) -> bool:
        return (
            self.process is not None and self.process.returncode is None and self.exit_code is None
        )


class TaskManager:
    """管理当前会话的后台任务生命周期。"""

    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        self._counter: int = 0

    def next_task_id(self) -> str:
        self._counter += 1
        return f"task_{self._counter}"

    async def start(
        self,
        *,
        argv: tuple[str, ...],
        cwd: Path,
        description: str = "",
        sandbox_spec: SandboxSpec | None = None,
        launcher: SandboxLauncher | None = None,
    ) -> BackgroundTask:
        """在沙箱包装下启动后台任务，并启动异步日志缓冲监听协程。"""
        task_id = self.next_task_id()
        wrapped_argv = launcher.wrap(argv, sandbox_spec) if (launcher and sandbox_spec) else argv

        # 启动非阻塞子进程
        process = await asyncio.create_subprocess_exec(
            *wrapped_argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        task = BackgroundTask(
            task_id=task_id,
            argv=argv,
            cwd=cwd,
            description=description,
            started_at=time.time(),
            process=process,
        )

        # 挂载流读取协程
        if process.stdout:
            task.reader_tasks.append(
                asyncio.create_task(self._consume_stream(process.stdout, task.stdout_buffer))
            )
        if process.stderr:
            task.reader_tasks.append(
                asyncio.create_task(self._consume_stream(process.stderr, task.stderr_buffer))
            )

        # 挂载进程状态监听
        task.reader_tasks.append(asyncio.create_task(self._watch_process(task)))

        self._tasks[task_id] = task
        return task

    async def _consume_stream(self, reader: asyncio.StreamReader, buffer: deque[str]) -> None:
        """逐行读取子进程标准输出或标准错误至环形缓冲区。"""
        try:
            while not reader.at_eof():
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                buffer.append(text)
        except (asyncio.CancelledError, Exception):
            pass

    async def _watch_process(self, task: BackgroundTask) -> None:
        """等待子进程退出并记录终态与退出码。"""
        if task.process is None:
            return
        try:
            exit_code = await task.process.wait()
            task.exit_code = exit_code
            task.ended_at = time.time()
        except (asyncio.CancelledError, Exception):
            pass

    def get(self, task_id: str) -> BackgroundTask | None:
        return self._tasks.get(task_id)

    def status(self, task_id: str) -> dict[str, Any]:
        """获取指定任务的实时运行状态。"""
        task = self.get(task_id)
        if task is None:
            return {"status": "not_found", "task_id": task_id}

        now = time.time()
        duration_ms = int(((task.ended_at or now) - task.started_at) * 1000)
        return {
            "task_id": task.task_id,
            "status": "running" if task.is_running else "exited",
            "exit_code": task.exit_code,
            "duration_ms": duration_ms,
            "argv": list(task.argv),
            "cwd": str(task.cwd),
            "description": task.description,
        }

    def logs(self, task_id: str, tail_lines: int = 50) -> dict[str, Any]:
        """获取指定任务的标准输出与标准错误末尾行。"""
        task = self.get(task_id)
        if task is None:
            return {"status": "not_found", "task_id": task_id}

        tail_lines = max(1, min(tail_lines, 500))
        stdout_tail = list(task.stdout_buffer)[-tail_lines:]
        stderr_tail = list(task.stderr_buffer)[-tail_lines:]
        return {
            "task_id": task.task_id,
            "status": "running" if task.is_running else "exited",
            "stdout": stdout_tail,
            "stderr": stderr_tail,
            "total_stdout_lines": len(task.stdout_buffer),
            "total_stderr_lines": len(task.stderr_buffer),
        }

    async def stop(self, task_id: str) -> dict[str, Any]:
        """优雅终止（SIGTERM）指定任务；超时后强制强杀（SIGKILL）。"""
        task = self.get(task_id)
        if task is None:
            return {"status": "not_found", "task_id": task_id}

        if not task.is_running or task.process is None:
            return {
                "task_id": task_id,
                "status": "already_stopped",
                "exit_code": task.exit_code,
            }

        proc = task.process
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.5)
            except TimeoutError:
                proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
        except (ProcessLookupError, OSError):
            pass

        task.exit_code = proc.returncode
        task.ended_at = time.time()

        for t in task.reader_tasks:
            t.cancel()

        return {
            "task_id": task_id,
            "status": "stopped",
            "exit_code": task.exit_code,
        }

    async def close_all(self) -> None:
        """清理所有正在运行的后台进程，防止产生孤儿进程泄漏。"""
        now = time.time()
        for task in list(self._tasks.values()):
            if task.is_running and task.process is not None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    task.process.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(task.process.wait(), timeout=0.5)
                task.exit_code = (
                    task.process.returncode if task.process.returncode is not None else -9
                )
                task.ended_at = now
            for t in task.reader_tasks:
                t.cancel()
