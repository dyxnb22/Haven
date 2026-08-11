# ADR 0007: Subagents, MCP, and deterministic diff review

## Status

Accepted, with two rejections. MCP is **deferred**. A model-driven Reviewer
subagent is **deferred**. The fallback the plan itself names — a deterministic
review of the run diff — is **adopted and implemented**.

## Gate: problem

Two capabilities in `Haven_TUI_Coding_Agent_项目计划.md` §11 remain unbuilt:
a read-only MCP client (§11.B) and a Reviewer agent (§11.E). Both are common in
产品级 agents, so the question is whether either moves a metric Haven actually
measures, or whether they add surface that weakens the project's core claim.

The concrete problem worth solving is narrower than either: **an agent can
satisfy the Evidence Gate and still hand back a bad diff.** A run that adds an
API key, leaves a `breakpoint()` in, commits a merge-conflict marker, or blanks
most of a file will pass "diff exists + check exits 0" if the check does not
happen to cover it. Today nothing inspects *what* changed.

## Gate: current baseline

- Success requires a diff and a passing check recorded after the last write
  (ADR 0003). Nothing examines the diff's content.
- 24 offline eval cases, 0 security violations. The security cases cover *paths
  the agent must not touch*, not *content it must not write*.
- Tool set is compiled in; a test asserts every registered tool has a policy
  classification and that no side-effecting tool is ever auto-allowed.

## Option A: read-only MCP client — DEFERRED

MCP would let Haven consume tools from external servers. Evaluated against the
gate:

- **Benefit to a measured metric: none identified.** Haven's metrics are task
  outcome, policy denials, unauthorized effects, recovery correctness, and cost.
  Borrowing a third-party tool does not improve any of them; it broadens what the
  agent can reach.
- **Cost to the core claim: high.** Haven's strongest invariant is that the tool
  set is finite, compiled in, and *provably* classified — `set(ARGS_MODELS) ==
  KNOWN_TOOLS` is a test. MCP makes tools appear at runtime from a process Haven
  does not control, so the policy stops being a pure function over a known set.
  Restoring the invariant means per-server-per-tool allowlists, pinned schema
  digests with fail-closed drift detection, and treating a server's advertised
  "read-only" claim as unverifiable — a server can do anything behind
  `tools/call`.
- **Eval cost.** Every one of those behaviors needs its own offline cases plus a
  fake MCP server, or the security gate silently stops covering the largest
  attack surface in the system.
- **Prerequisite not met.** The project's own non-goals say MCP is evaluated only
  "只有核心工具合同稳定后" — after the tool contract is stable. That contract
  changed in this same development cycle (`repo.create`, edit scoping). It is not
  stable yet.

**Deferred.** What would have to be true to revisit: (a) a specific task Haven
cannot do that a named MCP server enables; (b) the tool contract unchanged for a
meaningful period; (c) a design where an MCP tool is `deny` by default and
reaches `ask` only via an explicit per-server, per-tool, digest-pinned allowlist;
(d) offline eval cases for schema drift, server identity change, oversized
results, and injection via tool results.

## Option B: model-driven Reviewer subagent — DEFERRED

§11.E requires comparing defect detection rate, false positives, cost, and
latency against the single-agent baseline before adopting. With `ScriptedModel`
those four numbers are authored, not measured — the Reviewer would "find" exactly
the defects its script was written to find. The gate is therefore unpassable
offline, and passing it would require live, paid evaluation that is not the CI
gate.

There is also a cheaper objection: a second model reviewing the first model's
work adds a second unfalsifiable opinion. For the specific defects that matter
here — secrets, conflict markers, debug leftovers, mass deletion — a program can
decide with no false-negative risk from sampling and no token cost.

**Deferred**, per the plan's own fallback: "没有净收益就回退为确定性检查或单模型自检".

## Decision: deterministic diff review — ADOPTED

Add `domain/review.py`: a pure function over the run diff that returns structured
findings, consumed by the Evidence Gate. A run that edited files cannot be
reported as succeeded while a blocking finding stands; the finding is fed back so
the agent can fix it within its remaining budget, exactly like a failed check.

Checks were chosen for **low false-positive rate on added lines only**:

| Finding | Rationale |
|---|---|
| Added private key blocks, `AKIA…`, `sk-…`, obvious `password = "…"` | A committed secret is the worst outcome a coding agent can produce, and the patterns are unambiguous |
| Added merge-conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`) | Never intentional; a reliable signature of a botched merge or a bad patch |
| Added `breakpoint()`, `pdb.set_trace()`, `debugger;` | Unambiguous debug leftovers; unlike `print`/`console.log` these have no legitimate steady-state use |
| A file losing >80% of its lines and more than 50 lines | The signature of an agent blanking a file it did not understand |

Only lines the run *added* are examined, so pre-existing content in the
repository can never trigger a finding — the same "only this run's changes"
attribution `repo.diff` already uses.

## Consequences

- Success now means: diff + passing check + nothing obviously dangerous in what
  was written. That is a strictly stronger, still program-decided, claim.
- The checks are heuristics and are documented as such: they catch the obvious
  cases, not a determined adversary, and they are not a substitute for review.
- False positives are possible (a test fixture that legitimately contains a fake
  key). The finding is advisory to the *model* — it blocks automatic success and
  is reported to the user, who can still see the diff and decide.
- Cost: zero tokens, sub-millisecond, no second model, no external process.

## Gate: metrics

Measurable offline and asserted by tests: a diff containing each finding class
blocks success with the corresponding reason code; a clean diff does not;
pre-existing content matching a pattern does not. A new eval case exercises the
secret-leak path end to end.

## Rollback

Delete `domain/review.py` and drop the `diff_text` argument from
`evaluate_evidence_gate`; the gate returns to diff + check only.
