# ADR 0016: The extension boundary, and what may cross it

## Status

Accepted. This records how Haven would take on new tools or external
capabilities without losing the invariant that makes it auditable, and why the
big-ticket extensions (MCP, LSP, plugins, multi-agent) remain deferred. It
extends ADR 0007 with the guardrails the intervening work put in place.

## Gate: problem

Every external review compared Haven's fixed tool set to Codex's and
opencode's MCP, plugins, skills, hooks, and LSP, and marked the difference a
gap. It is a gap only if breadth is the goal. Haven's thesis is the opposite:
the tool set is finite, compiled in, and *provably* classified, and that is
what lets a reader answer "why was this exact side effect allowed?" from the
code. The question this ADR settles is not "how do we add everything" but
"what must be true of anything we add, and what is not worth adding yet."

## The invariants any extension must preserve

These are now enforced, not aspirational, and any new capability inherits them:

1. **Compiled-in and enumerable.** `set(ARGS_MODELS) == KNOWN_TOOLS` is a test.
   A tool that appears at runtime from a source Haven does not control breaks
   this, so runtime tool discovery is the specific thing the boundary resists.
2. **Mandatory policy classification.** Every tool is in exactly one of
   `READ_ONLY_TOOLS`, `EFFECT_TOOLS`, `STATE_TOOLS`, or `EXEC_TOOLS`, and a test
   asserts no side-effecting tool is ever auto-allowed (with the single, pinned
   `repo.exec` safe-read exception). A new tool with no classification fails the
   completeness test loudly.
3. **One execution channel.** Registry → Schema → Facts → Policy → Approval →
   Ticket → Sandbox → Executor. Raw model JSON dies at the ticket; a process is
   sandboxed (ADR 0009/0013); a workspace change is attributed to the evidence
   ledger whatever tool caused it (ADR 0012).
4. **Success is still evidence.** Anything that edits must leave a diff and a
   passing check; a new tool cannot introduce a success path around the gate.

The shape a future tool takes is therefore fixed: a strict args model, a
policy class, a facts collector, and an executor branch — the same five things
`repo.delete` and `repo.move` needed. That is the "strict internal interface"
the roadmap asked for, and it already exists as a pattern; formalizing it into
a `ToolSpec` object is a safe refactor to make when a third or fourth tool of a
new kind arrives, not before (YAGNI).

## Decisions on specific extensions

- **LSP — deferred, first in line.** Highest value per unit of risk because
  go-to-definition and diagnostics are read-only. But it needs a language-server
  subprocess per language, which is real operational surface and cannot be
  tested deterministically offline the way everything else here is. Unlock
  condition: a design that runs the server under the existing sandbox, treats
  its output as untrusted, and has an offline fake-server contract test. A
  lightweight, deterministic stand-in (`ast`-based symbol lookup for Python)
  could deliver much of the value first if a real task shows `repo.search` is
  insufficient — pulled in on demand, not speculatively.

  > **Extended by ADR 0035 (2026-10-22).** The lightweight, deterministic `ast`-based
  > stand-in predicted above was adopted in ADR 0035 via `repo.outline` and `repo.symbol`.
  > It delivers structural AST outline and symbol reference navigation without the
  > runtime complexity or failure modes of an external language-server daemon.

- **MCP — deferred, conditions unchanged from ADR 0007.** Deny by default,
  per-server per-tool allowlists, pinned schema digests with fail-closed drift
  detection, and offline eval cases for schema drift, server-identity change,
  oversized results, and injection via tool results. None of these are built,
  and MCP would enlarge the one attack surface the security suite exists to
  cover. It stays out until a named task needs a named server.

  > **Amended by ADR 0039 (2026-10-23).** The hardened MCP gateway was adopted in
  > ADR 0039, satisfying all conditions: static declaration in configuration,
  > namespace isolation (`mcp.<server>.<tool>`), strict allowlist filtering, and
  > fail-closed SHA-256 schema pinning with deterministic policy evaluation.

- **Plugins / skills / hooks — deferred, lowest priority.** Each widens the
  auditable boundary for convenience, not for a metric Haven measures. They
  wait behind LSP and MCP, if ever.

  > **Extended by ADR 0031, ADR 0032 (2026-10-20), and ADR 0034 (2026-10-21).**
  > Declarative prompt-fragment *skills* were adopted in ADR 0031, made on-demand
  > loadable via `skill.read` with bounded standing tool approvals in ADR 0032,
  > and expanded with companion file manifests and lifecycle tooling in ADR 0034.
  > Plugins and hooks remain deferred.
- **Multi-agent — deferred, decision made measurable.** ADR 0007's benefit gate
  stands. The Phase 4 quality/safety split is the instrument: adopt a
  planner/subagent only when a real-task baseline shows a single agent failing
  on long tasks for reasons a planner would fix, so the choice is decidable
  from numbers rather than argued.

## Gate: metrics

Nothing to measure here — this ADR adds no behavior. Its value is that the
guardrails above are now testable facts (`test_policy.py`,
`test_exec_evidence_hole.py`, the sandbox enforcement suite), so a future
extension either preserves them or turns a test red.

## Rollback

Not applicable; this is a decision record. If an extension is later adopted, it
gets its own ADR recording how it satisfies invariants 1–4.
