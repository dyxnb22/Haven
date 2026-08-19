# External Integrations

## Core Sections (Required)

### 1) Integration Inventory

| System | Type | Purpose | Auth model | Criticality | Evidence |
|--------|------|---------|------------|-------------|----------|
| OpenAI-compatible Chat Completions API (default OpenAI endpoint; live measurements used DeepSeek) | HTTPS API (SSE streaming + tool calls) | The model provider | Bearer key from env (`HAVEN_API_KEY` or `$HAVEN_API_KEY_ENV`) | high | `src/haven/adapters/providers/openai_compatible.py`, `src/haven/config.py` |
| OS sandbox: Seatbelt (macOS) / Landlock (Linux ABI>=4) | Kernel confinement | Wrap exec/check on supported systems; exec fails closed without a backend | n/a (local kernel API) | high | `src/haven/adapters/sandbox/`, `src/haven/sandbox/landlock_launcher.py` |
| Git (local) | Subprocess | Baseline capture at run start; eval harness clones | n/a | low | `src/haven/adapters/git_baseline.py` |
| GitHub Actions | CI | Gates on macOS+Linux | repo-scoped | med | `.github/workflows/ci.yml` |

No message queues, no external databases, no telemetry/monitoring services —
deliberate local-only scope (README non-goals).

### 2) Data Stores

| Store | Role | Access layer | Key risk | Evidence |
|-------|------|--------------|----------|----------|
| SQLite (WAL) at `<data_dir>/haven.db` | Runs, append-only event journal, checkpoints, approvals, execution journal | `adapters/sqlite_session.py` (aiosqlite) | Unbounded growth (mitigated by `haven gc`, ADR-less, dry-run default); single-writer semantics | `src/haven/adapters/sqlite_session.py`, `application/maintenance.py` |
| Content-addressed artifact files at `<data_dir>/artifacts/` | Archived originals for diff/rewind | same adapter | Orphans (swept by gc against surviving checkpoint refs) | `sqlite_session.py::put_artifact` |
| Lease files at `<data_dir>/leases/` | Advisory single-writer workspace lease | `adapters/workspace_lease.py` | Local/advisory only; stale takeover is file-lock serialized (ADR 0020/0030) | `src/haven/adapters/workspace_lease.py` |

### 3) Secrets and Credentials Handling

- Credential sources: environment variables only; the var name is
  configurable (`HAVEN_API_KEY_ENV`). No secrets in config files by design.
- Hardcoding checks: no keys in the tracked tree (verified via ripgrep in the
  2026-08-13 audit); exports redact env-suffixed secrets
  (`interfaces/export.py`).
- Rotation/lifecycle: not applicable (user-supplied key; nothing stored).

### 4) Reliability and Failure Behavior

- Retry/backoff: model calls retried with exponential backoff, only for
  retryable `ProviderError`s and only when nothing streamed
  (`run_service.py::_stream_model`, `openai_compatible.py` reasoning retry
  guarded by `yielded_any`). Tool calls are never retried (by design —
  side effects).
- Timeout policy: httpx connect/read/write/pool timeouts + first-event and
  between-event idle deadlines in the adapter; the run budget supplies the
  total wall deadline. Recipe/exec timeouts terminate then kill the complete
  process group (`adapters/process_executor.py`).
- Circuit breaker/fallback: none — a run fails with a stop reason instead.

### 5) Observability for Integrations

- Logging around external calls: every model call emits `model.completed`
  with tokens/cached/ttft/duration; every process execution journals
  STARTED->outcome and emits `tool.completed` (event journal).
- Metrics/tracing: no external APM by design; `haven eval` reports and
  `scripts/refresh_metrics.py` produce the committed measurements.
- Missing visibility gaps: provider-side cache ingestion lag is observable
  only statistically (documented in EVAL_LIVE cache decomposition).

### 6) Evidence

- `src/haven/adapters/providers/openai_compatible.py`
- `src/haven/config.py`
- `src/haven/adapters/process_executor.py`
