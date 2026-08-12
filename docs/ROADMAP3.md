# Haven roadmap v3

Written 2026-08-13, immediately after roadmap v2 was executed in full (see
`ROADMAP2.md` execution status). The gap analysis behind this plan is the
post-v2 self-assessment against Codex CLI, opencode, and Reasonix: the
*mechanism* gaps the audits named are now substantially closed, and what
remains has changed character. This plan is organized around that change.

## Where the gaps moved

After v2, Haven has: a multi-file patch transaction (ADR 0019), queued
steering / rewind / fork / a writer lease (ADR 0020), a hardened provider
edge (history sanitizer, precise reasoning retry), a genuinely hard context
budget, scoped guidance, headless write mode and one-step discovery accept
(ADR 0021), and a four-tier real-task evaluation whose newest tier is the
first non-saturated one (9–10/13), with a hidden grader and per-case
forensics.

What it does not have:

1. **Mileage.** The session-runtime mechanisms are hours old. They are pinned
   by 666 offline tests and ~30 live eval cases — that proves correctness of
   the paths tested, not maturity under real, messy, long sessions. The
   comparison systems' equivalent features have years of soak.
2. **Comparability.** Every number Haven publishes is Haven-only. No
   same-task, same-model, same-budget run against Codex CLI or opencode has
   ever been done; there is no SWE-bench-style external anchor; repeat-run
   distributions are two or three runs deep. "~75–80% of Codex within scope"
   is a calibrated self-estimate, and it should be a measurement.
3. **Semantic code understanding.** No LSP-class capability. Tier 4 produced
   the first failure attributable to a semantic-localization ceiling (the
   jinja idtracking case) — one data point, not yet a mandate.
4. **Interaction surface.** The runtime can steer/rewind/fork, but the TUI
   cannot yet *show* most of it: no session picker, no fork UI, no file
   mention, no diff-review flow. Pure engineering, zero architecture risk.
5. **Protocol and polish tails.** Native prefix continuation (the current
   continuation is a working conversational shim), streaming per-step events
   for headless CI, token-calibrated budgeting.
6. **Breadth** — deliberately out of scope, unchanged (desktop, browser,
   bots, remote exec, multi-provider, plugin marketplace).

## Ordering principles

Inherited: a false claim outranks a missing feature; every phase ends with a
measurement recorded next to the claim it supports; capability lands only
through the single channel (Registry → Policy → Approval → Sandbox →
Executor → Evidence Gate); architecture additions stay behind an ablation
gate.

New, and the theme of this plan: **a mechanism is not a capability until it
has mileage, and a self-measured number is not a result until it has a
baseline.** Phases 0 and 1 exist to convert v2's mechanisms and numbers into
soaked, externally-anchored ones before anything new is built on top.

## Phase 0 — soak: give the new mechanisms real mileage

The cheapest way to find what the tests missed is to actually live in the
tool. Everything here uses Haven on real work (including on Haven's own
repository), with the event journal as the record.

1. **Dogfood sessions.** Run genuine multi-turn sessions in the TUI on this
   repository: at least one long task steered mid-run, one rewind of a
   finished run, one fork from an older turn, one multi-file change made via
   `apply_patch`, one lease contention (second process) observed. Each
   becomes a committed golden journey (TUI Pilot where scriptable, exported
   journal where not).
2. **Long-horizon live runs.** One eval case class with a 40+ step budget
   that forces compaction and the hard clamp under pressure, live — the
   stress the offline tests approximate.
3. **Fix what falls out, at the class level,** with the same discipline the
   eval tiers used: every defect gets a regression test and a line in the
   docs.

*Measurement:* the golden journeys exist and replay; a soak log in
`EVAL_LIVE.md` lists every defect found → fixed (an empty list is a claim,
so it must be an honest one); zero invariant breaks (approval binding,
evidence, scope) across the soak.

## Phase 1 — comparability: measure Haven against the field

The single highest-credibility item in this plan. All infrastructure, no new
capability.

1. **Head-to-head harness.** Export the real-task tiers (1–4) in a
   tool-agnostic form: fixture directory + goal text + verify command. Run
   Codex CLI (it is installed here; configure its OSS-provider path to the
   same DeepSeek model) and opencode (install) on the same tasks with
   matched budgets. Score every tool's output tree with the same external
   grader Haven uses (the verify recipe run post-hoc, plus the scope check
   via git diff) — the grader must not favor the home team.
2. **Distributions.** N≥5 runs of a fixed 12-case subset (spanning tiers and
   difficulty) per tool → report spread, not points.
3. **One non-Python repository.** A pinned Node or Go project: discovery
   must propose a working verify command, plus 4–6 injected/real tasks.
   This tests the harness's Python-shaped assumptions more than the model.
4. **Stretch: an external anchor.** A 10–20 instance SWE-bench Verified
   slice if the per-instance environment cost proves manageable; otherwise
   record why not, with the cost estimate.

*Measurement:* a side-by-side table in `EVAL_LIVE.md` (per-suite, never
averaged), per-failure attribution for every tool including the others, and
a revision of the "% of Codex" self-estimate into a measured statement.

## Phase 2 — tier 5: scale the realism that tier 4 started

Tier 4's findings (the model cannot reliably conclude "no bug"; hardest
localizations exceed its ceiling) rest on 13 tasks. Scale until the
distributions are load-bearing — this phase produces the data every
architecture decision downstream consumes.

1. **Real-issue set 8 → ~24** (2–3 per repo across the nine pinned repos,
   including the non-Python one), same revert-the-fix construction, same
   sandboxed red/green gate.
2. **Honesty set 3 → ~8.** The 1-in-3 honest-stop rate is the single most
   interesting model number Haven has produced; it needs N.
3. **Long-horizon session tasks.** Multi-turn tasks (fix, then follow-up
   refactor, steered midway) with 40+ step budgets — this is also where the
   **compaction comprehension A/B** finally runs honestly: the same task
   with compaction forced early vs. late, task success as the metric.
4. **Refactors 2 → ~6**, now that `apply_patch` is proven on the shape.

*Measurement:* published distributions with root-cause attribution;
the compaction A/B number; explicit inputs to the Phase 3 and Phase 6 gates.

## Phase 3 — semantic understanding, strictly data-gated

The biggest remaining functional gap vs. opencode — and the one this project
has always refused to build on vibes.

- **Gate:** proceed only if Phases 1–2 attribute ≥5 failures to
  semantic-localization limits (currently: 1). If the data does not arrive,
  record the verdict and skip — that outcome is explicitly acceptable.
- **Scope if gated in:** a read-only LSP adapter (definition / references /
  diagnostics, likely pyright or jedi for Python) exposed as `repo.*` read
  tools through the existing channel — read-only policy class, no approval
  friction, no LSP writes, no code actions.
- **Proof:** the ablation the audits asked for — rerun the failed
  localization cases with the LSP tools available vs. not, same model, same
  budgets. Ship only if the delta is real; otherwise remove.

## Phase 4 — session surface: let the TUI show what the runtime can do

All engineering, no architecture. The runtime semantics from v2 get their
user-facing half:

1. Session picker (`/sessions`: list, resume, fork from any checkpoint) and
   a visible branch parent in the header.
2. File mention (`@path` completion that performs the `repo.read` and pins
   context provenance).
3. Diff-review flow (`/diff`: per-file view; approve pending write requests
   from the diff view).
4. Lease and permission-mode visibility in the header; steering queue shown
   inline (queued → delivered).
5. **Headless streaming events:** `haven run --events out.jsonl` emitting
   the per-step envelope stream live (CI-parseable), completing what
   `--jsonl` (final outcome) started.

*Measurement:* a TUI Pilot journey per feature; one scripted end-to-end
"day in the life" golden session chaining them.

## Phase 5 — protocol and budget tails

1. **Native prefix continuation** behind the model-profile flag, with one
   paid confirmation run against DeepSeek's assistant-prefix mode; keep the
   conversational shim as fallback. Measure seam duplication shim-vs-native
   on forced-truncation cases.
2. **Token-calibrated budgeting:** learn chars-per-token per model from
   observed usage (the eval corpus already logs both sides), and recalibrate
   `max_context_chars` from it — replacing a hand-tuned constant with a
   measured one, without changing the hard-clamp semantics.
3. Watchdog and retry tuning from Phase 0 soak data.

## Phase 6 — the architecture verdict

With Phases 1–2 published: write the ADR that decides, from data, whether
any of planner / goal-FSM / subagents / MCP / full LSP crosses the benefit
gate ADR 0007 defined — in either direction. The deliverable is the verdict
with its evidence, not (necessarily) any of those systems.

## Non-goals, still

Desktop and browser clients, bots, remote execution, multi-provider breadth,
plugin marketplace, PTY/background processes/stdin. Unchanged from v1/v2;
they remain the parts with the least to teach and the most to maintain.

## Sequencing and cost

Phase 0 and Phase 1 are independent and both cheap (days; Phase 1's live
spend estimated low single-digit dollars, SWE-bench stretch excluded); they
should land before anything else because every later phase consumes their
output. Phase 2 is construction-heavy but mechanical (the tier-4 tooling
already exists). Phase 3 is a week-scale build *if* its gate opens. Phase 4
is steady UI work that can interleave with anything. Phases 5–6 are small.

The summary judgment this plan encodes: v2 closed the mechanism gaps; v3's
job is to make the mechanisms *seasoned*, the numbers *comparable*, and the
next architecture decision *decided by data* — after which Haven's remaining
distance to the comparison systems is breadth it has deliberately chosen not
to pursue.
