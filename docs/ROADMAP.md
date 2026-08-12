# Haven roadmap

Written 2026-08-12, after three external gap analyses (against Reasonix, Codex
CLI, and opencode) and a verification pass over their claims.

## Progress (2026-08-12)

Where each phase actually stands — "done" means implemented and tested, not
merely decided. Phase 4's real-repo measurement and Phase 5's extensions are
explicitly *not* built; what exists there is scaffolding and a recorded
decision, respectively.

- **Phase 0 — claims made true again: done.** Process writes attributed to the
  Evidence Gate (ADR 0012), exec/check sandbox scope decided (ADR 0013),
  provider client closed, `reasoning_effort` wired, `doctor` side-effect free,
  metrics generated with a CI drift guard (`scripts/refresh_metrics.py`), MIT
  license added.
- **Phase 1 — DeepSeek correctness: done.** Reasoning replay on tool-call turns
  (ADR 0014); output-truncation continuation and the live 400-confirmation
  documented as pending in `docs/EVAL_LIVE.md`.
- **Phase 2 — sessions: core done (ADR 0015).** `continue_run`, `haven
  continue`, and TUI follow-up. Live steering and rewind/fork UI deferred with
  reasons.
- **Phase 3 — real-repo survival: done.** Verification discovery (`haven
  discover`) and `repo.delete` / `repo.move` through the full channel.
  Grammar-based multi-file `apply_patch` deferred.
- **Phase 4 — measuring task success: first real number.** Beyond the
  quality/safety report split, there is now a real-repo live suite
  (`evals/real/`, 31 bug-injection tasks across five pinned third-party
  projects, each project's own tests as the oracle). On `deepseek-v4-flash`:
  27/31 as found, then **31/31** after fixing the two failure causes the run
  exposed (an unretried connection drop and the model gaming the oracle by
  editing tests). Zero-config is measured too: recipe discovery went 1/5 → 4/5
  working commands after four evidence-driven detector fixes, and the live
  `discover: true` cases score 5/5 — four end-to-end completions plus one
  honest stop where the repo's own pytest config is broken. A second tier —
  six green-field implement-the-missing-function tasks and three multi-file
  double bugs — scores 9/9. Written up in `docs/EVAL_LIVE.md`. This difficulty
  class is saturated; the remaining escalation is scale: larger repositories
  and vaguer, issue-style goals.
- **Phase 5 — extension boundary: decided (ADR 0016).** Guardrails documented;
  MCP, LSP, plugins, and multi-agent stay deferred with unlock conditions,
  consistent with the thesis.
- **Post-plan boundary fixes (ADR 0017).** External review surfaced four
  invariant gaps, all closed: `repo.exec` is now workspace-read-only (the
  Linux protected-path bypass), protected-path changes by any process are
  detected and surfaced, compaction drops tool-call turns as whole groups (a
  kept assistant can no longer reference a dropped tool result) and counts
  replayed reasoning toward the context budget, and `continue_run` verifies
  workspace identity and resets the run-scoped diff.

The sections below are the original plan, kept as written.

---

## The ordering principle

The three reviews largely agree on what Haven lacks, and reading them together
invites an obvious plan: work down the feature table until Haven looks like
Codex. That would be the wrong plan, and the reviews themselves say so.

Haven's claim is not "it can do a lot". It is **"every claim it makes is backed
by evidence you can reproduce"**. That brand sets the priority order:

> A false claim outranks a missing feature. Always.

A missing capability is honest — the README already lists what was deliberately
not built, and that scope control is a strength. A capability whose guarantee
leaks is dishonest, and on an evidence-driven project it costs more than the
feature was worth. So Phase 0 contains no new features at all.

The second principle, inherited from the existing ADRs: **each phase must end
with a measurement, not an assertion.** A phase that cannot say how it will be
proven does not start.

## What the reviews got right, and one thing they got backwards

Verified against the code before planning around it:

| Claim | Verdict |
|---|---|
| Writes via `repo.exec` bypass the Evidence Gate | **True, and proven by running it.** A run rewrote `src/calc.py` and was reported `succeeded / final_answer` |
| `repo.check` is not sandbox-fail-closed like `repo.exec` | True. Policy never consults `sandbox_available` for check |
| `reasoning_effort` on `ModelProfile` is unwired | True. The spec said it would be; the code never was |
| `AppServices.close()` leaks the provider's HTTPX client | True. It closes the store only |
| Docs have drifted (tests, ADR count, line counts, live pass rate) | True on every count. `PROJECT_CARD` says 486 tests in the table and 335 in the résumé bullets |
| No LICENSE file | True |
| DeepSeek requires `reasoning_content` replay on tool-call turns | **True**, and confirmed against DeepSeek's own thinking-mode docs |

The last one is the important correction, because Haven's source currently
asserts the opposite. `openai_compatible.py` carries the comment "most
providers reject their own reasoning on input", which was inferred from an
earlier defect. DeepSeek's rule is the reverse in the case Haven always hits:

- No tool call in the turn → `reasoning_content` may be omitted; if sent it is
  ignored.
- **Tool call in the turn → it must be replayed in every later request, or the
  API returns 400.**

Haven sends `tools` on every request and its whole loop is tool calls, so this
is a latent break, not a nicety. langchain, laravel/ai, and opencode all shipped
the same bug and fixed it. Why Haven's August live run passed anyway is an open
question — the honest answer is that it needs one live call to settle, which is
the first task of Phase 1.

## Phase 0 — make every existing claim true again

No features. This phase exists because Haven currently claims things that are
false, and it is the only phase that can be finished without judgement calls.

1. **Attribute every workspace mutation.** A file changed by `repo.exec` must
   reach the evidence ledger, or the Evidence Gate is decorative. Preferred
   design: take a bounded workspace digest snapshot before and after each
   process (exec *and* check), diff it, and record any change as edit evidence
   with the tool that caused it. This keeps attribution in the program rather
   than in the model's word, matching the existing preimage/postimage
   discipline. Fall back to declaring exec read-only-by-default if snapshotting
   proves too costly on large repos — but do not leave the gap open.
   *Proof:* the strict `xfail` in `tests/integration/test_exec_evidence_hole.py`
   flips to a pass, plus an eval case.
2. **One sandbox rule for every process.** Either `repo.check` also fails
   closed without a backend, or `SECURITY.md` stops saying "every child process
   is wrapped". The claim and the code must agree; I prefer making check
   fail-closed and giving users an explicit, documented escape hatch rather
   than weakening the sentence.
   *Proof:* a policy test per tool, and the enforcement suite covering check.
3. **Close what we open.** `AppServices.close()` must close the provider client.
   *Proof:* a test asserting no un-closed HTTPX client after `close()`.
4. **Wire or delete `reasoning_effort`.** Dead configuration is worse than none.
   Phase 1 wires it; if Phase 1 slips, delete the field.
5. **`doctor` must be side-effect free**, or stop saying it is.
6. **Generate every number.** The drift is not a typo problem, it is a process
   problem: the numbers are hand-copied. Add a `scripts/refresh_metrics.py`
   that reads the coverage XML, the eval JSON, `git`, and the ADR directory,
   and rewrites the marked regions of `README.md` and `PROJECT_CARD.md`. Make
   CI fail when the committed numbers differ from freshly generated ones.
   *Proof:* CI catches a deliberately stale number.
7. **Add a LICENSE.**

## Phase 1 — be actually correct against DeepSeek V4

The model Haven targets has a protocol requirement Haven does not implement.
This is the highest-value technical work in the plan and it does not enlarge
the security surface at all.

1. **Settle the open question first.** One live run with a tool call and a
   follow-up turn. Either it 400s (confirming the break) or it does not
   (meaning something about Haven's payload sidesteps it, which is itself worth
   understanding). Everything below is conditional on that result, and the
   result goes in `EVAL_LIVE.md` either way.
2. **Carry provider reasoning through the transcript without trusting it.**
   `ModelMessage` gains an opaque `provider_reasoning` field that is replayed
   on the wire and is otherwise inert: never rendered as the answer, never in
   the evidence ledger, never in the compaction digest, never labelled trusted.
   The existing rule that reasoning is not the answer stays exactly as it is —
   this is a wire-protocol obligation, not a change to what the model's
   thinking means to Haven.
3. **Replay only where the protocol demands it.** Gate it behind a capability
   flag on the profile (`requires_tool_call_reasoning`), so a provider that
   rejects the field never receives it. Back-fill an empty string when history
   predates the field, which is what the other fixed implementations do.
4. **Persist it in the checkpoint**, or a resumed run will 400 on its first
   turn — a failure mode that would only appear after recovery, which is the
   worst place to discover it.
5. **Handle truncated output** (`finish_reason: length`) with prefix
   continuation rather than discarding the turn.
6. **Upgrade `ModelProfile` from a price list to a capability descriptor:**
   reasoning replay, reasoning effort, output ceiling, cache semantics, context
   window. This is the seam that keeps model-specific behavior out of the core.

*Proof:* contract tests against recorded wire payloads (including a 400 for a
missing field), a checkpoint round-trip test, and one live run whose numbers
land in `EVAL_LIVE.md`.

## Phase 2 — a session, not a task runner

All three reviews independently identify the same largest UX gap, and they are
right: Haven runs one goal and stops. Asking a follow-up starts a fresh
`RunContext` with no memory, and the TUI refuses input while a run is active.
That makes it a bounded task runner with a TUI, not a coding session.

The design constraint is that Haven's durability semantics are its best asset,
so a Session must not replace `Run` — it must own a sequence of them:

- `Session` holds the goal thread, the accumulated transcript, and the plan;
  each user turn creates a `Run` under it, and each `Run` keeps today's
  checkpoint, journal, and recovery semantics unchanged.
- Follow-up turns inherit the session transcript, which is what makes
  compaction (ADR 0010) load-bearing rather than theoretical.
- Steer and queue while a run is active: accept input, show it as queued, and
  deliver it at the next turn boundary rather than mid-tool-call. The tool
  channel stays untouched.
- `rewind` and `fork` on top of the existing checkpoints. Crash recovery is not
  user-facing undo, and the reviews are right to separate them.

*Proof:* a TUI Pilot journey that asks a follow-up and gets an answer informed
by the first turn; a rewind test that restores both state and workspace; the
golden trace extended to a two-turn session.

## Phase 3 — survive a real repository

Haven is currently pleasant on a fixture and awkward on a real project. Two
specific blockers, both named by the reviews:

1. **Zero-configuration verification.** Today, a repository without
   `.haven.toml` makes every edit terminate in `verification_unavailable`. The
   agent can read but never usefully change anything — technically correct and
   practically useless. The fix must stay auditable: *detect* candidate
   commands from `pyproject.toml`, `package.json`, `Makefile`, or CI config,
   *propose* them to the user, and persist the accepted one as a normal
   registered recipe. The model still never supplies a command string; the
   discovery is program-driven and the authorization is human.
2. **Real editing operations.** `old_string → new_string` handles a one-line
   fix and struggles with everything else. Add delete, move/rename, and a
   structured multi-file patch — all through the existing
   Registry → Policy → Approval → Ticket channel, all with preimage binding.
   A patch is a better approval unit than a string replacement anyway: the user
   approves one reviewable diff instead of five opaque substitutions.

Deliberately excluded here: PTY, background processes, and stdin. They are real
Codex capabilities, but each one weakens the "one bounded process, one
result" invariant that makes Haven's execution channel auditable. Revisit only
with a specific task that needs them.

*Proof:* a fresh clone of a real third-party repository, no Haven config, task
completed end to end with evidence.

## Phase 4 — measure the thing that is not yet measured

Haven's eval suite proves boundaries hold. It does not yet prove the agent gets
work done, and the project's own documents say so. That gap is now the main
credibility risk, because "0 security violations" on toy fixtures is a weaker
claim than it looks.

Build a task suite of 50–100 small-to-medium tasks drawn from real
repositories, and report: patch correctness, test-suite regressions,
out-of-scope changes, tokens, wall clock, and how many approvals a human had to
give. Run it against `deepseek-v4-flash` and publish the numbers with the same
honesty as `EVAL_LIVE.md`, including the failures.

This phase is what converts Haven from "a well-argued architecture" into "a
tool with a measured success rate", and no amount of further ADR writing
substitutes for it.

## Phase 5 — extension, behind a boundary

Only after Phase 4 has a baseline, because every item here is a capability
whose value must be shown against that baseline rather than assumed.

Order matters: define a strict internal `ToolPlugin` capability interface
first, with per-tool policy classification still mandatory. Then LSP (highest
value per unit of risk, since it is read-only). Then MCP, with the conditions
ADR 0007 already set out: deny by default, per-server per-tool allowlists,
pinned schema digests, and eval cases for schema drift and injection. Skills,
hooks, and a plugin ecosystem last, if at all.

Multi-agent stays deferred until the Phase 4 numbers show a single agent
failing on long tasks for reasons a planner would actually fix. ADR 0007's
benefit gate already says this; the numbers will make it decidable instead of
arguable.

## What this plan deliberately does not chase

Desktop and browser clients, bots, remote execution, an app-server protocol,
image input, multi-provider breadth, and a plugin marketplace. These are the
bulk of the feature-table gap against Codex and opencode, and they are the
parts with the least to teach and the most to maintain. Haven competes on
being small, hard, and provable. A defensible one-line positioning:

> Haven is an auditable local agent runtime: every mutation is attributable,
> every success is evidence-backed, and every interrupted effect is
> recoverable.

Phase 0 and Phase 1 are what make that sentence true today. Phases 2–4 are what
make it useful. Phase 5 is optional.

## Sustainability

Three habits keep this from decaying, and the first one is why the drift
happened at all:

1. **No hand-written numbers.** Every metric in a document is generated and
   CI-checked. Phase 0 item 6.
2. **Every phase ends with a measurement**, recorded next to the claim it
   supports, including when the measurement is disappointing. ADR 0011 already
   contains one measurement that contradicted its own premise; that should be
   normal, not remarkable.
3. **An ADR per decision, with its rollback.** Eleven ADRs in, this is the
   project's most valuable habit and the cheapest to keep.
