# Codebase Concerns

Scan date: 2026-08-13. Scanner note: raw scan output (48k files / 4.2M LOC,
TODO hits, largest files) is dominated by **gitignored third-party eval
fixtures** (`evals/real/repos|fixtures`, `evals/headtohead/runs`,
`.hypothesis/`); the tracked tree is 266 files. All findings below are about
Haven-authored code only. `src/` contains **zero** TODO/FIXME/HACK markers
(the single grep hit is injected eval-task content in `evals/real/tasks.py:779`).

## Core Sections (Required)

### 1) Top Risks (Prioritized)

| Severity | Concern | Evidence | Impact | Suggested action |
|----------|---------|----------|--------|------------------|
| med | ~90 commits exist only on this machine (`main` ahead of `origin/main`) | `git status` vs origin | Total work loss on disk failure | Push; consider CI-required branch |
| med | Single-run-per-process assumption baked into `RunService` (`_active_run_id`, `_steer_queue` instance attrs) | `src/haven/application/run_service.py` | Blocks concurrent runs/subagent futures; a second `run()` on one service would interleave steering | Keep documented; refactor to per-run context object only if product shape changes |
| med | Extreme-pressure context clamp drops whole messages oldest-first regardless of role and truncates the last (user intent can vanish) | `src/haven/application/compaction.py::enforce_hard_limit`; ADR 0024 | Long multi-steer sessions could lose instructions silently | Gate pre-agreed in ADR 0024 (summary tier + user-message protection) |
| low-med | 1.2MB `docs/demo.gif` tracked in git; `.git` already 10MB | `git ls-files -s docs/demo.gif` | Repo clone size grows with every regeneration | Consider regenerating rarely or moving to a release asset |
| low | Coverage has no `fail_under`; drift is visible (metrics gate) but not blocking on the number itself | `pyproject.toml`, `.github/workflows/ci.yml` | Slow coverage erosion possible | Add `fail_under` if a floor is wanted |
| low | Export redaction masks only values of currently-set env secrets (suffix heuristic) | `src/haven/interfaces/export.py::_secret_values` | A foreign secret pasted into a goal/tool output would survive export | Document; optionally add pattern-based masking (sk-..., ghp_...) |
| ~~low~~ RESOLVED | Standing-approval test flake (1/8 runs) | root-caused 2026-08-13: the test made three *consecutive* identical checks — the no-progress condition — and only passed because check results carry `duration_ms` and the ms usually differed | — | Fixed: the test interleaves `repo.diff` like a real fix/verify loop, so it tests approvals not the clock |
| low | Stuck detection is slightly under-sensitive: the fingerprint includes the tool result, and check results carry `duration_ms`, so two genuinely-identical checks whose timings differ are not seen as repeats | `application/run_service.py` (`call_fingerprint` call), `application/tool_pipeline.py:1415` | A degenerate verify loop can run longer than the threshold implies | Not changed: excluding timing would make *more* runs stop as stuck; needs measurement before touching |

### 2) Technical Debt

| Debt item | Why it exists | Where | Risk if ignored | Suggested fix |
|-----------|---------------|-------|-----------------|---------------|
| Three parallel event renderers (presenter / ConsoleSink / export) | Three genuinely different outputs; unification judged low-value | `interfaces/tui/presenter.py`, `interfaces/cli.py`, `interfaces/export.py` | New event kinds must be added in 3 places (or deliberately skipped) | Acceptable; a completeness guard test would pin it |
| Char-based token budgeting (calibrated, not tokenized) | No offline tokenizer dependency by choice | `application/context_builder.py`, `profiles.py`, ADR 0022 | Provider/tokenizer change invalidates calibration | Re-run `evals/calibrate_context.py` on any provider change |
| `/budget` TUI command reads config via `getattr` chain | `config` deliberately not in `SessionServices` protocol | `interfaces/tui/app.py::_handle_command` | Silent "budget unavailable" if shape changes | Add `config` to the protocol or a typed accessor |
| Eval scripts (`evals/real/build.py`, `headtohead/*.py`) have no unit tests | They are harnesses, exercised by paid live runs | `evals/` | Regressions surface only during expensive runs | Keep `--verify` gate; smoke-test cheap paths if they churn |

### 3) Security Concerns

| Risk | OWASP category | Evidence | Current mitigation | Gap |
|------|----------------|----------|--------------------|-----|
| Prompt injection via repo content/tool output | LLM01 (OWASP LLM Top 10) | untrusted `<tool_output>` framing, system rules | Trust labels, injection eval cases (3), deterministic digest (never model summary) | Injection evals are scripted, not adversarial-generated |
| Recipes run arbitrary user-registered argv | A08-ish / by design | `.haven.toml [recipes]`, ADR 0013 | User authorizes once; fixed argv; sandbox wraps; model can never supply a command | Locally-trusted-repo assumption, stated in SECURITY.md |
| Linux: trusted check writing `.git` not kernel-prevented | n/a | ADR 0018, SECURITY.md §6 | Snapshot detects; call fails `protected_path_tampered`; no evidence recorded | Write itself still lands on disk (documented) |
| Workspace lease is advisory with a small last-writer race | n/a | `adapters/workspace_lease.py`, ADR 0020 | Read-back confirmation; pid probe; documented scope | Not a real lock; acceptable per ADR |
| Standing check approvals widen consent | n/a | ADR 0025 | Digest-identical only, run-scoped, memory-only, disclosed on card, journal 1:1 | Resumed-run re-ask relies on non-checkpointing — pinned by test |
| API key hygiene | A07-ish | env-only keys; export redaction; `config explain` prints presence only | No keys in tracked tree (audited 2026-08-13) | Keys appear in local shell history/terminal logs outside the repo (user-side) |

### 4) Performance and Scaling Concerns

| Concern | Evidence | Current symptom | Scaling risk | Suggested improvement |
|---------|----------|-----------------|-------------|-----------------------|
| Before/after tree snapshot digests every file on each `repo.exec`/`repo.check` | `adapters/workspace_fs.py::capture_snapshot` (ADR 0012); measured 150ms / 28MB retained on a 128k-line repo | CPU only — since 2026-08-13 it runs in a worker thread (`tool_pipeline._snapshot`), so it no longer blocks the event loop for ~300ms per check | O(repo size) per process call; the `.git` aggregate can exceed the worktree on a mature clone | Deliberately **not** cheapened with size caps or mtime stats: the digest is what makes a process write — and protected-path tampering on Linux (ADR 0018) — detectable, and mtime is forgeable |
| Context rebuilt from full transcript every turn | `application/context_builder.py::build` | Negligible at measured sizes (<=480k chars) | O(transcript) per turn CPU | None needed at current scale |
| Event journal + artifacts grow without bound until user acts | `sqlite_session.py`; mitigated by `haven gc` (dry-run default) | 10MB `.git`-adjacent data dirs after heavy eval use | Disk growth for heavy users | Documented; `gc` exists since 2026-08-13 |
| ~~Checkpoint chain retained per run~~ RESOLVED | measured 12.3MB of 13.4MB payload on a real store, ~90% superseded, O(run length²) | — | — | Fixed 2026-08-13: `save_checkpoint` prunes rows it supersedes (`seq <`); only the newest is ever read |
| SQLite WAL single-writer | `sqlite_session.py` | None (single-process by design + lease) | Concurrent-process writes would contend | Out of scope per ADR 0020 |

### 5) Fragile/High-Churn Areas

| Area | Why fragile | Churn signal (90d) | Safe change strategy |
|------|-------------|--------------------|----------------------|
| `application/run_service.py` | Loop semantics, budgets, steering, gate interplay | 19 commits | Full suite + golden trace; outcomes only minted via `_finish` |
| `interfaces/cli.py` | 18 commands, exit-code contract | 14 commits | `tests/test_cli.py`; keep README command surface in sync |
| `application/tool_pipeline.py` | The security channel itself | 13 commits | Wiring test pins dispatch tables; security evals must stay green |
| `application/context_builder.py` | Prompt-cache prefix stability (ADR 0008) | 12 commits | Cache-hit measurement + `test_the_prefix_survives_a_turn_after_compaction` |
| `adapters/providers/openai_compatible.py` | Provider quirks (reasoning replay, sanitizer, retry) | 10 commits | respx contract tests; never retry after first yielded event |
| Docs with generated metrics (README, PROJECT_CARD) | Regenerated by script; hand edits drift | 36/33 commits | Edit only outside the generated block; `refresh_metrics.py --check` gates |

### 6) `[ASK USER]` Questions

1. [ASK USER] ~90 local commits are unpushed — push now, or is the remote
   deliberately stale?
2. [ASK USER] Should coverage get an enforced floor (`fail_under`), or stay
   visible-only via the metrics drift gate?
3. [ASK USER] Keep `docs/demo.gif` (1.2MB) in git, or move to a release
   asset/GitHub-hosted image to cap clone size?
4. [ASK USER] The one-off standing-approval flake: accept with the added
   diagnostics, or invest in a reproduction hunt now?

### 7) Evidence

- `docs/codebase/.codebase-scan.txt` (raw scan; fixture-noise caveat above)
- `git ls-files | wc -l` (266 tracked) vs scan totals; `.gitignore`
- `pyproject.toml`, `.github/workflows/ci.yml`
- `docs/adr/0018|0020|0022|0024|0025-*.md`, `docs/SECURITY.md`
- `src/haven/application/{run_service,tool_pipeline,context_builder,compaction,maintenance}.py`
- `src/haven/adapters/{workspace_fs,workspace_lease,sqlite_session}.py`
- `src/haven/interfaces/{cli.py,export.py,tui/app.py}`
