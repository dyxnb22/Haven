# Codebase Concerns

Reviewed for the final learning baseline: 2026-08-20. The original raw scan (48k
files / 4.2M LOC,
TODO hits, largest files) is dominated by **gitignored third-party eval
fixtures** (`evals/real/repos|fixtures`, `evals/headtohead/runs`,
`.hypothesis/`). All findings below are about Haven-authored code only. `src/`
contains **zero** TODO/FIXME/HACK markers
(the single grep hit is injected eval-task content in `evals/real/tasks.py:779`).

## Core Sections (Required)

### 1) Top Risks (Prioritized)

| Severity | Concern | Evidence | Impact | Suggested action |
|----------|---------|----------|--------|------------------|
| med | Single-run-per-process assumption baked into `RunService` (`_active_run_id`, `_steer_queue` instance attrs) | `src/haven/application/run_service.py` | Blocks concurrent runs/subagent futures; a second `run()` on one service would interleave steering | Keep documented; refactor to per-run context object only if product shape changes |
| ~~med~~ RESOLVED | Extreme-pressure context clamp could orphan tool protocol or discard the latest user intent | `src/haven/application/compaction.py::enforce_hard_limit`; ADR 0024 | — | Fixed 2026-08-20: hard-limit units keep assistant calls with tool results, protect the latest user turn, and bound content, reasoning, and tool arguments together. A semantic summary tier remains deferred |
| low-med | 1.2MB `docs/demo.gif` is tracked in git | `git ls-files -s docs/demo.gif` | Repo clone size grows with every regeneration | Regenerate rarely; move to a release asset only if it starts growing |
| low | No single global coverage `fail_under` applies to UI/platform adapters | `scripts/check_coverage_floor.py` | Coverage in non-core surfaces can erode while the overall generated number remains stable | Accepted for the frozen scope: every core decision/boundary file is individually gated at 85%; TUI and sandbox adapters have dedicated suites |
| ~~low~~ RESOLVED | Export redaction masked only values of currently-set env secrets | `src/haven/interfaces/export.py` | — | Fixed 2026-08-13/20: a second pass masks well-known credential shapes and now removes complete PEM private-key blocks; dynamic Markdown fences prevent diff content from closing its own report block |
| ~~low~~ RESOLVED | Standing-approval test flake (1/8 runs) | root-caused 2026-08-13: the test made three *consecutive* identical checks — the no-progress condition — and only passed because check results carry `duration_ms` and the ms usually differed | — | Fixed: the test interleaves `repo.diff` like a real fix/verify loop, so it tests approvals not the clock |
| low | Stuck detection is slightly under-sensitive: the fingerprint includes the tool result, and check results carry `duration_ms`, so two genuinely-identical checks whose timings differ are not seen as repeats | `application/run_service.py` (`call_fingerprint` call), `application/tool_pipeline.py` check-result construction | A degenerate verify loop can run longer than the threshold implies | Not changed: excluding timing would make *more* runs stop as stuck; needs measurement before touching |

### 2) Technical Debt

| Debt item | Why it exists | Where | Risk if ignored | Suggested fix |
|-----------|---------------|-------|-----------------|---------------|
| Three parallel event renderers (presenter / ConsoleSink / export) | Three genuinely different outputs; unification judged low-value | `interfaces/tui/presenter.py`, `interfaces/cli.py`, `interfaces/export.py` | New event kinds must be added in 3 places (or deliberately skipped) | Acceptable; a completeness guard test would pin it. Note each now neutralizes untrusted text for its own medium (control chars everywhere, Rich markup escaped only where markup renders) — that part is per-surface by necessity, not duplication |
| ~~Eval fixture materialization duplicated across the two harnesses~~ RESOLVED | — | — | — | Fixed 2026-08-13: both use `evals/real/materialize.py`; verified by rebuilding all 79 fixtures byte-identical to the pre-refactor output |
| Char-based token budgeting (calibrated, not tokenized) | No offline tokenizer dependency by choice | `application/context_builder.py`, `profiles.py`, ADR 0022 | Provider/tokenizer change invalidates calibration | Re-run `evals/calibrate_context.py` on any provider change |
| ~~`/budget` TUI command read config via a `getattr` chain~~ RESOLVED | — | `interfaces/tui/app.py::_handle_command` | — | `config` is now part of the typed `SessionServices` protocol; the command reports separate input/output limits |
| Eval scripts (`evals/real/build.py`, `headtohead/*.py`) have no unit tests | They are harnesses, exercised by paid live runs | `evals/` | Regressions surface only during expensive runs | Keep `--verify` gate; smoke-test cheap paths if they churn |

### 3) Security Concerns

| Risk | OWASP category | Evidence | Current mitigation | Gap |
|------|----------------|----------|--------------------|-----|
| Prompt injection via repo content/tool output | LLM01 (OWASP LLM Top 10) | untrusted `<tool_output>` framing, system rules | Trust labels, injection eval cases (3), deterministic digest (never model summary) | Injection evals are scripted, not adversarial-generated |
| Recipes run arbitrary registered argv | A08-ish / by design | `.haven.toml [recipes]`, ADR 0013/0030 | Fixed argv; supported backends wrap it; model can never supply a command; every approval shows workspace write, network, and extra-read authority | Locally-trusted-repo assumption remains, especially when no backend exists |
| Linux: trusted check writing `.git` not kernel-prevented | n/a | ADR 0018, SECURITY.md §6 | Snapshot detects; call fails `protected_path_tampered`; no evidence recorded | Write itself still lands on disk (documented) |
| Workspace lease is advisory and local | n/a | `adapters/workspace_lease.py`, ADR 0020/0030 | Native file-lock guard serializes stale takeover; random token prevents PID-reuse/same-process ownership confusion | It does not coordinate non-Haven writers or distributed hosts; that is an explicit product boundary |
| Standing check approvals widen consent | n/a | ADR 0025 | Digest-identical only, run-scoped, memory-only, disclosed on card, journal 1:1 | Resumed-run re-ask relies on non-checkpointing — pinned by test |
| API key hygiene | A07-ish | env-only keys; two-pass export redaction; `config explain` prints presence only | No keys in tracked tree (audited 2026-08-13) | Keys appear in local shell history/terminal logs outside the repo (user-side) |
| ~~Silent out-of-workspace reads via auto-allowed exec~~ RESOLVED | LLM02/LLM06 | found by the 2026-08-13 review: `cat /proc/<parent>/environ` reached the parent process's whole environment, around the child's `ENV_ALLOWLIST` scrub, with no approval card | Fixed: `classify_argv` demotes a read whose operands leave the workspace to ordinary exec (ADR 0026) | Reads outside the workspace are now approved, not prevented — by design |
| ~~Untrusted text rendered without encoding~~ RESOLVED | LLM05 | headless sink echoed ANSI raw; TUI panels rendered Rich markup from model output | Fixed: control chars stripped on both surfaces, markup escaped in `sanitize` (SECURITY.md §7a) | Display integrity only; not a capability boundary |
| Python search fallback can be stalled by a hostile regex | LLM10-ish (unbounded consumption) | `adapters/workspace_fs.py::_search_walk`; measured: `re` holds the GIL, so a thread does not help | Wall-clock deadline bounds the walk and reports truncation | One pathological *subject* is still irreducible without a killable subprocess; ripgrep (default) is immune |

### 4) Performance and Scaling Concerns

| Concern | Evidence | Current symptom | Scaling risk | Suggested improvement |
|---------|----------|-----------------|-------------|-----------------------|
| Before/after tree snapshot digests every file on each `repo.exec`/`repo.check` | `adapters/workspace_fs.py::capture_snapshot` (ADR 0012); measured 150ms / 28MB retained on a 128k-line repo | CPU only — since 2026-08-13 it runs in a worker thread (`tool_pipeline._snapshot`), so it no longer blocks the event loop for ~300ms per check | O(repo size) per process call; the `.git` aggregate can exceed the worktree on a mature clone | Deliberately **not** cheapened with size caps or mtime stats: the digest is what makes a process write — and protected-path tampering on Linux (ADR 0018) — detectable, and mtime is forgeable |
| Context rebuilt from full transcript every turn | `application/context_builder.py::build` | Negligible at measured sizes (<=480k chars) | O(transcript) per turn CPU | None needed at current scale |
| Event journal + artifacts grow without bound until user acts | `sqlite_session.py`; mitigated by `haven gc` (dry-run default) | Platform data directory grows after heavy eval use | Disk growth for heavy users | Documented; `gc` exists since 2026-08-13 |
| ~~Checkpoint chain retained per run~~ RESOLVED | measured 12.3MB of 13.4MB payload on a real store, ~90% superseded, O(run length²) | — | — | Fixed 2026-08-13: `save_checkpoint` prunes rows it supersedes (`seq <`); only the newest is ever read |
| SQLite WAL single-writer | `sqlite_session.py` | None (single-process by design + lease) | Concurrent-process writes would contend | Out of scope per ADR 0020 |

### 5) Fragile/High-Churn Areas

| Area | Why fragile | Churn signal (90d) | Safe change strategy |
|------|-------------|--------------------|----------------------|
| `application/run_service.py` | Loop semantics, budgets, steering, gate interplay | 19 commits | Full suite + golden trace; outcomes only minted via `_finish` |
| `interfaces/cli.py` | Broad command surface and stable exit-code contract | 14 commits | `tests/test_cli.py`; `scripts/check_docs.py` pins README root commands |
| `application/tool_pipeline.py` | The security channel itself | 13 commits | Wiring test pins dispatch tables; security evals must stay green |
| `application/context_builder.py` | Prompt-cache prefix stability (ADR 0008) | 12 commits | Cache-hit measurement + `test_the_prefix_survives_a_turn_after_compaction` |
| `adapters/providers/openai_compatible.py` | Provider quirks (reasoning replay, sanitizer, retry) | 10 commits | respx contract tests; never retry after first yielded event |
| Docs with generated metrics (README, PROJECT_CARD, DESIGN_QA) | Regenerated by script; hand edits drift | high | Edit only outside the generated block; `refresh_metrics.py --check` gates |

### 6) Freeze Decisions

No unresolved question blocks the learning baseline. The remaining items above
are accepted scope boundaries, not implied future work: single-run services,
native-sandbox rather than VM isolation, the trusted-check assumption, and
measured char-based token budgeting. Any future change should reopen the
relevant ADR gate instead of treating this list as a backlog.

### 7) Evidence

- `docs/codebase/.codebase-scan.txt` (raw scan; fixture-noise caveat above)
- `.gitignore` (separates authored sources from materialized eval repositories)
- `pyproject.toml`, `.github/workflows/ci.yml`
- `docs/adr/0018|0020|0022|0024|0025|0030-*.md`, `docs/SECURITY.md`
- `src/haven/application/{run_service,tool_pipeline,context_builder,compaction,maintenance}.py`
- `src/haven/adapters/{workspace_fs,workspace_lease,sqlite_session}.py`
- `src/haven/interfaces/{cli.py,export.py,tui/app.py}`
