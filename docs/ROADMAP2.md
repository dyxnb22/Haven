# Haven roadmap v2

Written 2026-08-12, after the second round of external audits (against
Reasonix, Codex CLI, and opencode) — the round that followed the tier-3
real-task measurement. Every load-bearing claim in those audits was verified
against the code before this plan was written; file:line references below are
the verified evidence, not quotations.

## Execution status (2026-08-12)

All six phases below have been implemented and are covered by tests; the
detail lives in the ADRs and `docs/EVAL_LIVE.md`.

- **Phase 0 — done.** Exec prompts corrected to the read-only profile;
  protected-path tamper fails the call (ADR 0018); recovery windows narrowed
  (schema v2 journals move-dest and expected postimages); cross-turn process
  regressions pinned; per-case eval event streams persisted; a generated
  live-eval summary (`evals/real/results.json` → README/PROJECT_CARD) ended
  the drift; one same-version rerun published at 61/65, fully attributed.
- **Phase 1 — done.** `repo.apply_patch`: one approval, atomic apply,
  journaled rollback (ADR 0019).
- **Phase 2 — done.** Tier 4 (real historical bugs + cross-file refactors +
  honesty tasks) scored 9–10/13 — the first non-saturated tier, failures
  attributed to model capability, plus a hidden grader.
- **Phase 3 — done.** Queued steering, user-level rewind, fork pin, and a
  workspace writer lease (ADR 0020).
- **Phase 4 — done.** Wire-boundary history sanitizer, missing-reasoning
  precise retry, idle watchdog (already present), hidden grader.
- **Phase 5 — done.** Hard budget clamp with a builder assertion, scoped
  nested AGENTS.md, digest-preservation proxy (live compaction A/B remains
  the one open measurement, documented).
- **Phase 6 — done.** Headless write mode and one-step discovery accept
  (ADR 0021); command-generated patches assessed as already covered by
  `repo.check` recipes + `repo.apply_patch`, with the model-proposed-command
  sliver deliberately deferred (it contradicts ADR 0017).

The sections below are the plan as written.

## What the audits agree on

All three audits, independently:

- **The security kernel is no longer the gap.** Digest-bound single-use
  approvals, the Evidence Gate, workspace-read-only exec (ADR 0017), scope and
  gaming detection, crash recovery — at or ahead of the comparison systems.
  None of the plans below may trade this away for feature parity.
- **The gap moved.** From correctness to: editing expressiveness (multi-file
  atomic patches), session runtime (steering a *running* agent, fork/undo,
  cross-process leases), provider protocol edge recovery, context precision,
  evaluation realism, zero-config closure, and headless automation.
- **Documentation drifted again**, in the same direction as before: the code
  got better and the words did not keep up. A false claim outranks a missing
  feature, so that is Phase 0 again.

Where the audits differ: the Reasonix audit ranks the session runtime first;
the Codex CLI and opencode audits rank the multi-file patch first. This plan
orders patch before session runtime for two reasons: the patch is the smaller,
more self-contained change, and it unblocks the tier-4 refactor evaluation
that the project needs *before* deciding any larger architecture question.

## Ordering principles

Inherited from v1, plus one new:

1. **A false claim outranks a missing feature.** Phase 0 contains no features.
2. **Every phase ends with a measurement**, recorded in `EVAL_LIVE.md` next to
   the claim it supports, including when it is disappointing.
3. **New: capability lands only through the existing channel.** Registry →
   Policy → Approval → Sandbox → Executor → Evidence Gate. Anything that would
   bypass it is out of scope by construction, and *architecture* additions
   (planner, goal FSM, subagents, LSP, MCP) stay behind an ablation gate: they
   are built only when tier-4 failure data shows a specific failure class they
   would fix.

## Phase 0 — truth maintenance and correctness tails

The audits found the same disease as last round, milder. Close it and add the
antibodies.

1. **Fix the two stale exec descriptions.** `contracts/tools.py:199` and
   `context_builder.py:143` still tell the model "writes confined to the
   workspace"; since ADR 0017 the workspace is read-only and only scratch is
   writable. This is not cosmetic: it invites the model to attempt writes that
   policy will deny, burning steps and tokens. *Proof:* prompt-content test
   pinning the wording to the sandbox profile actually used.
2. **Generate the evaluation summary.** `PROJECT_CARD.md:65` still headlines
   the pre-suite 6/8 number. Extend `scripts/refresh_metrics.py` to own one
   generated eval-summary block (offline suite + all real-task tiers) consumed
   by README and PROJECT_CARD, so audit-facing numbers can no longer drift.
   Mark `ROADMAP.md`'s kept-as-written historical section more loudly — three
   auditors tripped over it.
3. **Close the stale "pending" claims.** `EVAL_LIVE.md:444` still says
   reasoning replay is "live-confirmation pending" and output continuation is
   deferred; both are implemented (`run_service.py:365` bounds continuation)
   and ~90 live tool-calling cases have exercised replay. Record what is
   confirmed; keep only what is genuinely unmeasured.
4. **Make the headline numbers unambiguous.** Tier 3's "20/20" is 15/20
   as-found plus a 5/5 click rerun after the oracle fix; zero-config's "5/5"
   includes one *expected* honest stop (4/5 completions). Re-run the full
   60-case suite once on the committed revision for a single same-version
   number, and reword both headlines. (~$0.5, ~2h wall.)
5. **Escalate protected-path mutation by a trusted check.** Today a Linux
   check that rewrites `.git` produces an error notice (ADR 0017); the audits
   are right that a boundary violation should fail the check result, not
   annotate it. Semantics change → short ADR, plus an eval case.
6. **Shrink the recovery-unknown windows.** Journal the move destination and
   the expected postimage at execution start so interrupted `move` (and the
   create/edit postimage window, `recovery_service.py:135-192`) classify as
   confirmed/not-run more often instead of blocking on `unknown`. Regression
   tests per class.
7. **Cross-turn process regressions.** `continue_run` → `repo.exec` /
   `repo.check` must behave identically to turn one (workspace identity, diff
   baseline, scratch recreation). One integration test each.
8. **Eval forensics.** Persist per-case event envelopes (JSONL beside the
   progress file) so failed-run archaeology is a read, not a paid replay —
   prerequisite for tier 4, where failures are expected to be model-caused.

*Phase measurement:* `refresh_metrics --check` guards the eval summary; a
stale-claim grep comes back empty; one same-version full-suite number is
published in `EVAL_LIVE.md`.

## Phase 1 — one patch, one approval, one transaction

`repo.apply_patch`: the unanimous top capability gap. Today a multi-file
change is N sequential `repo.edit` calls — N approvals, N chances for a stale
preimage, no whole-change review, no atomic failure story. The tier-2
"multi-file" tasks were two independent single-file bugs; genuine cross-file
work is untested because the primitive is missing.

Design constraints (ADR to fix the format):

- **Structured operations, not fuzzy text.** A patch is an ordered set of
  edit-hunk / create / delete / move operations with exact preimage digests
  for every touched file, collected from files the run actually read. Fuzzy
  or context-tolerant application is explicitly rejected: it would break the
  preimage discipline that makes approval binding meaningful.
- **One approval.** The approval digest covers the canonical encoding of the
  entire patch; the preview is one reviewable unified diff. Single-use, bound,
  re-verified against disk immediately before apply — exactly the existing
  edit discipline, widened.
- **Atomic apply with a journaled rollback.** Stage all writes to temp files,
  re-verify all preimages, then rename in sequence; any failure rolls back
  completed renames from the journal. An interruption mid-apply must classify
  in recovery (staged / partially-applied → reconcile, never silent).
- **Evidence unchanged.** One edit-evidence entry per patch; the Evidence Gate
  still requires a green registered check after the last write.

*Phase measurement:* offline eval cases for approval binding, stale preimage,
mid-apply crash rollback, and a protected path inside a patch; live, the
tier-4 refactor tasks (Phase 2) plus a measured drop in approvals-per-task on
multi-file work.

## Phase 2 — tier 4 evaluation: realism before architecture

The design drafted at the end of the tier-3 report, extended by the audits'
harder asks. This phase deliberately precedes the session runtime: its output
is the data that decides every architecture debate that follows.

1. **Real-issue reproduction set (10–15 tasks).** Check out the parent of a
   real bug-fix commit in the pinned repos, restore only the fix's regression
   test (the oracle), use the original issue text — lightly anonymized — as
   the goal. Kills the "injected by someone who knows the answer" bias. The
   sandboxed red/green gate applies unchanged.
2. **Cross-file refactors (needs Phase 1).** Rename-plus-imports, parameter
   threading, dependency inversion — 3+ files, oracle = project suite plus a
   builder-authored task test proven red/green before any model call.
3. **Honesty tier.** No-solution tasks where the correct outcome is an
   explicit stop (the wcwidth zero-config case, made deliberate), plus a
   hidden post-run grader that checks completion integrity beyond the visible
   oracle (no feature deleted to silence a test, no oracle-adjacent edits).
4. **Distributions, not points.** N≥5 repeats on a fixed subset; report the
   spread. Two runs of everything else stays the norm (as found / after fix).
5. **Head-to-head.** Same tasks, same model, same budgets through Codex CLI
   and opencode where their harnesses allow it; per-suite numbers reported
   side by side, never averaged.
6. **Stretch: one non-Python repository** (Node or Go) to take discovery and
   the task format beyond the single ecosystem everything has run on so far.

*Phase measurement:* published pass distributions with per-failure root-cause
attribution; the ablation verdict on whether any failure class needs planner /
FSM / subagent machinery — with numbers, ending that argument one way or the
other.

## Phase 3 — session runtime: control a running agent

The Reasonix audit's top gap and opencode's biggest UX gap, scoped so `Run`,
checkpoints, and recovery semantics stay untouched underneath.

1. **Durable queued input.** Accept input while a run is active
   (`tui/app.py:325` currently refuses), persist it to a session inbox, and
   inject at the next turn boundary. Tool-call execution stays atomic; nothing
   is delivered mid-effect.
2. **Rewind and fork.** User-level restore to any checkpoint, reconciling the
   workspace through recorded pre/postimages (an unknown effect blocks, same
   as crash recovery — no blind compensation); fork creates a sibling session
   from a checkpoint instead of rewriting history.
3. **Workspace writer lease.** A heartbeat lease (SQLite or lockfile) making
   concurrent Haven processes on one workspace explicit: the second process
   runs read-only with a clear message. Closes the cross-process TOCTOU window
   the Reasonix audit identified; small, and independent of remote execution.
4. **Session UX to make it usable:** session picker / `new` / `fork`, file
   mention, a diff-review flow, and visible permission mode in the TUI.

*Phase measurement:* TUI Pilot journeys — steer lands next turn, fork
diverges and both branches recover independently, lease contention degrades
safely; the golden trace extends to a steered two-turn session.

## Phase 4 — provider protocol edge hardening

Normal-path DeepSeek is proven by ~90 live tool-loop cases. The edges are
one generation behind, per the Reasonix audit — four bounded pieces. Pull this
phase forward if any tier-4 failure attributes to it.

1. **Malformed-history sanitizer:** validate and repair orphaned
   tool-calls/results and missing reasoning fields before every send —
   deterministic, logged, tested against recorded wire payloads.
2. **Missing-reasoning 400 → precise retry** of the frozen request with the
   reasoning re-attached, instead of surfacing a provider error.
3. **Streaming idle watchdog:** a no-token timeout distinct from wall clock,
   classified retryable.
4. **Native prefix continuation** for `finish_reason: length`, replacing the
   current append-a-user-message shim — which can duplicate content at the
   seam, spends a full extra request, and breaks the cache prefix — with
   usage merged across attempts.

*Phase measurement:* contract tests over recorded payloads including the 400;
one paid confirmation run; `EVAL_LIVE.md`'s remaining protocol "pending"
items close for good.

## Phase 5 — context precision

1. **Token budgeting from provider usage.** Replace character heuristics with
   a per-model calibration loop fed by returned usage; count tool schemas,
   JSON wire overhead, and the output reservation.
2. **A hard cap that is actually hard.** Today the clamp drops droppable tool
   groups only (`context_builder.py:204`); add a forced second pass so no
   request can exceed the window even when non-droppable content grows.
3. **Nested AGENTS.md.** Merge root→cwd scoped instructions, bounded, still
   untrusted — replacing the root-only 200-line read (`bootstrap.py:168`).
4. **Compaction comprehension A/B.** The restart benchmark: task performance
   before/after compaction on the same trajectory. A semantic (rather than
   structural) digest is built only if this benchmark shows the structural one
   losing task state — measured, not assumed.

*Phase measurement:* benchmark numbers in `EVAL_LIVE.md`; zero
context-overflow errors across the long-horizon tier-4 runs.

## Phase 6 — productization inside the boundary

1. **Zero-config closure.** `haven discover` currently prints TOML for manual
   copying (`cli.py:498`); make it propose → preview → one-keystroke accept →
   persisted recipe → usable in the same session. Non-Python detectors
   (npm/pnpm/cargo/go) get real fixtures instead of rules-only coverage.
2. **Headless write mode.** `haven run` is hard read-only (`cli.py:140`); add
   `--approval-policy reject|ask|trusted-recipe`, `--write`, and `--jsonl`
   for CI and batch use — every write still through the same approval and
   evidence channel; read-only stays the default.
3. **Command→patch channel.** An approved `repo.exec` writing to scratch gets
   its scratch-relative changes harvested into a *proposed patch* that enters
   the Phase 1 approval flow. Formatters, codegen, and migrations become
   usable without weakening ADR 0017 — the model still cannot write the
   workspace; the user approves a reviewable diff.

*Phase measurement:* an end-to-end zero-config TUI journey on a fresh clone;
a CI-style headless run producing evidence; a formatter task completed via
command→patch with one approval.

## What stays out, still

Desktop and browser clients, bots, remote execution, multi-provider breadth,
and a plugin marketplace — unchanged from v1. PTY, MCP, multi-agent, and full
LSP remain deferred behind the Phase 2 ablation gate; a minimal *read-only*
LSP (definition/references/diagnostics) is the one candidate the opencode
audit argues for, and it gets reconsidered only after Phase 5 data shows
grep/read localization failing where semantics would succeed.

## Sequencing and size

Phases are ordered by the principles above, not strictly by execution time:
Phase 0 is days; Phase 1 and Phase 4 are each a focused week-scale effort;
Phase 2 is mostly evaluation construction and can proceed in parallel with
Phase 1 (its refactor subset waits for the patch tool); Phases 3 and 5 are the
large ones. Phase 6 items are independent of each other and can be interleaved
once Phases 0–2 are done.

The summary judgment this plan is built on, shared by all three audits: the
single-agent, safe-mutation, evidence-gated, recoverable execution kernel is
done and measured. What remains is long-running-runtime maturity — sessions,
steering, leases, transactions, protocol edges — and evaluation realism deep
enough to justify (or permanently retire) every architecture idea beyond it.
