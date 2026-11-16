"""后台任务与受控进程池（ADR 0037）。

为开发服务器、长期编译构建或持续测试提供沙箱隔离的后台子进程池。
维护行缓冲环形队列，支持随时查询状态、查看有界日志与优雅终止。
在运行结束或异常退出时，确保所有后台孤儿进程被强制清理回收。
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import errno
import os
import signal
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
    terminal: bool = False
    master_fd: int | None = None
    grouped: bool = False
    input_closed: bool = False
    input_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def is_running(self) -> bool:
        return (
            self.process is not None and self.process.returncode is None and self.exit_code is None
        )


class _OutputCapture:
    """有界行缓冲，保留尚未换行的提示，避免 readline 被超长输出阻塞。"""

    def __init__(self, buffer: deque[str]) -> None:
        self.buffer = buffer
        self.partial = ""

    def append(self, text: str) -> None:
        if not text:
            return
        if self.partial and self.buffer:
            self.buffer.pop()
        parts = (self.partial + text.replace("\r\n", "\n").replace("\r", "\n")).split("\n")
        for line in parts[:-1]:
            self.buffer.append(line[-8192:])
        self.partial = parts[-1][-8192:]
        if self.partial:
            self.buffer.append(self.partial)


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
        terminal: bool = False,
    ) -> BackgroundTask:
        """在沙箱包装下启动后台任务，并启动异步日志缓冲监听协程。"""
        task_id = self.next_task_id()
        wrapped_argv = launcher.wrap(argv, sandbox_spec) if (launcher and sandbox_spec) else argv

        if terminal and os.name != "posix":
            raise ValueError("PTY requires a POSIX host")
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG", "LC_ALL", "VIRTUAL_ENV")
            if key in os.environ
        }
        env.update(TERM="dumb", NO_COLOR="1")
        if sandbox_spec is not None:
            sandbox_spec.scratch_dir.mkdir(parents=True, exist_ok=True)
            env["TMPDIR"] = str(sandbox_spec.scratch_dir)
        master: int | None = None
        slave: int | None = None
        if terminal:
            import pty

            master, slave = pty.openpty()
            os.set_blocking(master, False)
        try:
            process = await asyncio.create_subprocess_exec(
                *wrapped_argv,
                cwd=str(cwd),
                env=env,
                stdin=slave if terminal else asyncio.subprocess.PIPE,
                stdout=slave if terminal else asyncio.subprocess.PIPE,
                stderr=slave if terminal else asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
        except BaseException:
            if master is not None:
                os.close(master)
            raise
        finally:
            if slave is not None:
                os.close(slave)

        task = BackgroundTask(
            task_id=task_id,
            argv=argv,
            cwd=cwd,
            description=description,
            started_at=time.time(),
            process=process,
            terminal=terminal,
            master_fd=master,
            grouped=os.name == "posix",
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
        if master is not None:
            task.reader_tasks.append(asyncio.create_task(self._consume_pty(task, master)))

        # 挂载进程状态监听
        task.reader_tasks.append(asyncio.create_task(self._watch_process(task)))

        self._tasks[task_id] = task
        return task

    async def _consume_stream(self, reader: asyncio.StreamReader, buffer: deque[str]) -> None:
        """分块读取，未换行提示立即可见，单行也有长度上限。"""
        capture = _OutputCapture(buffer)
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while chunk := await reader.read(4096):
                capture.append(decoder.decode(chunk))
        except (OSError, ValueError):
            return
        finally:
            capture.append(decoder.decode(b"", final=True))

    @staticmethod
    async def _ready(fd: int, *, write: bool = False) -> None:
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()

        def signal_ready() -> None:
            if not ready.done():
                ready.set_result(None)

        if write:
            loop.add_writer(fd, signal_ready)
        else:
            loop.add_reader(fd, signal_ready)
        try:
            await ready
        finally:
            if write:
                loop.remove_writer(fd)
            else:
                loop.remove_reader(fd)

    async def _consume_pty(self, task: BackgroundTask, fd: int) -> None:
        capture = _OutputCapture(task.stdout_buffer)
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while True:
                await self._ready(fd)
                try:
                    chunk = os.read(fd, 4096)
                except BlockingIOError:
                    continue
                except OSError as exc:
                    if exc.errno == errno.EIO:  # PTY slave closed (Linux).
                        break
                    raise
                if not chunk:
                    break
                capture.append(decoder.decode(chunk))
        except OSError:
            return
        finally:
            capture.append(decoder.decode(b"", final=True))
            if task.master_fd == fd:
                task.master_fd = None
                os.close(fd)

    async def write_input(
        self, task_id: str, data: str, *, end_of_input: bool = False
    ) -> dict[str, Any]:
        """只写本任务池的进程；不增加权限，不解释 shell，不自动追加换行。"""
        task = self.get(task_id)
        if task is None:
            return {"status": "not_found", "task_id": task_id}
        if len(data) > 4096 or (not data and not end_of_input):
            raise ValueError("input must contain data or EOF, at most 4096 characters")
        async with task.input_lock:
            if not task.is_running or task.process is None:
                raise ValueError("task has exited")
            if task.input_closed:
                raise ValueError("task input is already closed")
            payload = data.encode("utf-8")
            async with asyncio.timeout(2.0):
                if task.terminal:
                    if task.master_fd is None:
                        raise ValueError("task terminal is closed")
                    # POSIX canonical EOF: once flushes a partial line, twice also ends input.
                    raw = memoryview(payload + (b"\x04\x04" if end_of_input else b""))
                    while raw:
                        try:
                            count = os.write(task.master_fd, raw)
                        except BlockingIOError:
                            await self._ready(task.master_fd, write=True)
                            continue
                        raw = raw[count:]
                else:
                    writer = task.process.stdin
                    if writer is None or writer.is_closing():
                        raise ValueError("task stdin is closed")
                    writer.write(payload)
                    await writer.drain()
                    if end_of_input:
                        writer.close()
                if end_of_input:
                    task.input_closed = True
            return {
                "task_id": task_id,
                "status": "sent",
                "bytes_sent": len(payload),
                "eof": end_of_input,
            }

    async def _watch_process(self, task: BackgroundTask) -> None:
        """等待子进程退出并记录终态与退出码。"""
        if task.process is None:
            return
        try:
            # wait() 可被后代继承的 pipe 阻塞；先观察 leader，再回收整个进程组。
            while task.process.returncode is None:
                await asyncio.sleep(0.02)
            task.exit_code = task.process.returncode
            task.ended_at = time.time()
            self._signal(task, signal.SIGKILL)
            await task.process.wait()
        except OSError:
            return

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
            "terminal": task.terminal,
            "input_closed": task.input_closed,
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
            self._signal(task, signal.SIGKILL)
            await self._release(task)
            return {
                "task_id": task_id,
                "status": "already_stopped",
                "exit_code": task.exit_code,
            }

        proc = task.process
        try:
            self._signal(task, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.5)
            except TimeoutError:
                self._signal(task, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
        except (ProcessLookupError, OSError):
            pass

        task.exit_code = proc.returncode
        task.ended_at = time.time()
        # 即便 leader 已退出，也不能留下继承了终端或管道的后代。
        self._signal(task, signal.SIGKILL)
        await self._release(task)

        return {
            "task_id": task_id,
            "status": "stopped",
            "exit_code": task.exit_code,
        }

    async def close_all(self) -> None:
        """清理所有正在运行的后台进程，防止产生孤儿进程泄漏。"""
        for task in list(self._tasks.values()):
            self._signal(task, signal.SIGKILL)
            if task.process is not None:
                with contextlib.suppress(TimeoutError, OSError):
                    await asyncio.wait_for(task.process.wait(), timeout=1.0)
                task.exit_code = task.process.returncode
                task.ended_at = task.ended_at or time.time()
            await self._release(task)

    @staticmethod
    def _signal(task: BackgroundTask, sig: signal.Signals) -> None:
        if task.process is None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            if task.grouped:
                os.killpg(task.process.pid, sig)
            elif task.process.returncode is None:
                if sig == signal.SIGTERM:
                    task.process.terminate()
                else:
                    task.process.kill()

    @staticmethod
    async def _release(task: BackgroundTask) -> None:
        for reader in task.reader_tasks:
            reader.cancel()
        await asyncio.gather(*task.reader_tasks, return_exceptions=True)
        if task.master_fd is not None:
            os.close(task.master_fd)
            task.master_fd = None
        if task.process is not None and task.process.stdin is not None:
            task.process.stdin.close()
        task.input_closed = True
