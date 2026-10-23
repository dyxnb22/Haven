# ADR 0037: managed background tasks and process pool

Date: 2026-10-22
Status: Accepted

## Context

Haven previously only supported synchronous, blocking process execution via `repo.exec` (ADR 0009) and `repo.check` (ADR 0012).
ADR 0028 deferred background tasks due to lack of a concrete need and the risk of leaked orphan processes.

In practical development workflows, long-running processes are indispensable:
1. Running local dev servers (e.g. Vite, FastAPI, Django) while testing frontend/backend interactions.
2. Long compilation, packaging, or test suite execution (e.g. `cargo build`, `npm run build`, full e2e test suite taking 3+ minutes) that would otherwise exceed synchronous command timeouts and freeze the CLI/TUI interface.

## Decision

**Implement a managed `TaskManager` process pool with four first-class tools:**

1. **`task.start(argv, cwd=".", description="")`**:
   - Launches a command asynchronously in the background.
   - Enforces the exact same OS sandbox policy as `repo.exec`: Seatbelt (macOS) or Landlock (Linux) restricts filesystem writes to scratch and denies network access.
   - Immediately returns an assigned `task_id` (e.g. `task_1`) without blocking the agent loop.
   - Consumes `stdout` and `stderr` streams line-by-line into asynchronous ring buffers (`deque(maxlen=1000)`).

2. **`task.status(task_id)`**:
   - Non-blocking inquiry returning running status (`running` / `exited`), duration in milliseconds, and exit code.
   - Categorized as `READ_ONLY_TOOLS`, evaluated as `ALLOW`.

3. **`task.logs(task_id, tail_lines=50)`**:
   - Bounded retrieval of recent stdout and stderr lines from the ring buffer.
   - Categorized as `READ_ONLY_TOOLS`, evaluated as `ALLOW`.

4. **`task.stop(task_id)`**:
   - Graceful termination: sends `SIGTERM`, waits up to 1.5 seconds, then escalates to `SIGKILL` if unexited.
   - Categorized as `STATE_TOOLS`, evaluated as `ALLOW`.

5. **Lifecycle and Orphan Protection**:
   - `RunService.close()` and `RunService._finish()` invoke `TaskManager.close_all()`.
   - All background subprocesses are guaranteed to be terminated upon session exit or run completion, preventing orphan process leakage.

## Consequences

- Unblocks long-running background tasks without compromising the deterministic policy or OS sandbox invariants.
- Streaming ring buffers prevent subprocess logs from overflowing context budgets or consuming unbounded memory.
