# ADR 0028: the three capability gaps nobody had ever gated

Date: 2026-08-14
Status: Accepted (three deferrals, each with an unlock condition)

## Context

A dimension-by-dimension audit against DeepSeek Harness (dsh, 219 packages,
229k source lines, ~51 model-visible tool names against Haven's 12) sorted the
differences into three piles: deliberate scope, decided-and-deferred, and a
third pile that turned out to be the interesting one.

Most of the capability gap is already adjudicated. MCP, subagents, a planner,
and a read-only LSP were each measured against failure data and declined (ADR
0007, ADR 0016, ADR 0023), and those verdicts stand. But three capabilities
that dsh ships have **never been evaluated here at all**: background
tasks/persistent terminals, web access (search and fetch), and image input.
They are absent from Haven because the v1 non-goals list says so, which is a
statement of scope, not a decision on merit — and an unexamined absence is
exactly what the benefit gate exists to prevent, in either direction.

This ADR closes that gap by putting all three through the same gate.

## Gate: what the corpus says

The live corpus (tiers 1–5, the N=5 distribution, the opencode head-to-head;
`docs/EVAL_LIVE.md`) attributes every non-passing run to one of:
budget-tail non-convergence, oracle-gaming, one semantic-localization ceiling,
or harness defects since fixed. **No failure is attributed to a missing
background process, a missing web lookup, or a missing image.** That is the
same evidence standard that declined the LSP, and it points the same way here.

## Image input — deferred, blocked at the provider

Haven targets DeepSeek. Its chat-completions route is text-only: dsh's own
DeepSeek adapter declares `inputModalities: ['text']` and raises
`UNSUPPORTED_CONTENT` on image blocks *regardless* of catalogue membership,
with the comment that "unknown" there would let a host accept and persist
images the serializer must then reject. Haven's adapter has no image path at
all, which is the same answer arrived at by omission.

So this is not a design question yet. Building an image path for a provider
that refuses images would add contract surface, checkpoint payload, and a
redaction question for the journal, in exchange for nothing runnable.

**Unlock condition:** a routed model that declares image input, plus a decision
on where image bytes live (they are too large for the event journal, so the
content-addressed artifact store, with the trust label spelled out — an image
is untrusted repository content like any file).

## Web access — deferred, and the most expensive of the three

Search and fetch would be Haven's first outbound network capability, against a
design where **the sandbox denies network by default** (`(deny network*)` in
the Seatbelt profile; the Landlock rules cover TCP) and `repo.exec`'s safety
argument rests on that closure. The cost is not the HTTP client:

- Fetched text is attacker-controlled content entering the model's context. The
  injection suite currently covers repository content, which an attacker must
  first get into the repo; a fetch tool lets the *model* choose what to pull in,
  which is a strictly larger surface and needs its own eval cases before it
  ships, not after.
- The claim "Haven makes no network call except to the configured provider" is
  currently checkable in one place and would become a per-tool argument.
- A search provider is a second vendor relationship, key, and failure mode.

> **Scope correction (2026-08-14).** "Haven" in the first bullet means
> Haven-owned network clients. A user-authored `repo.check` recipe has always
> been able to set `allow_network = true` (ADR 0009); that child process is
> explicit user authority, while `repo.exec` remains network-denied. The product
> therefore has one built-in network peer, not a universal no-network guarantee.

Against that: no corpus failure needs it, and the tasks Haven is built for
(bounded edits in one local repository, verified by a registered recipe) are
answerable from the repository.

**Unlock condition:** a named task class Haven cannot complete without an
external lookup, plus injection eval cases for fetched content, plus a decision
on whether fetch runs inside or outside the sandbox closure.

## Background tasks and persistent terminals — deferred on a real conflict

This one is not merely unneeded; it is in tension with a load-bearing
invariant. `_handle_tool_calls` is *deliberately* sequential, and the reason is
written where it is enforced: "parallel side effects would make approvals,
preimage pins, and the journal order ambiguous." Digest-bound approval (ADR
0002) and preimage/postimage recovery classification (ADR 0004) both assume one
effect in flight at a time. A background job or a live PTY breaks that
assumption for whatever it does while the loop is elsewhere.

dsh can offer these because it made the opposite trade: a `ctx.jobs` registry, a
fail-closed `isConcurrencySafe` classifier, and a bounded parallel pool whose
results are still committed in model order. That is a coherent design — and
adopting it means adopting its ordering machinery, not just a tool.

The concrete need this would serve — long-running verification — is already
served: `repo.check` runs a registered recipe with a configurable timeout, and
the one case that needed longer (pygments) was handled by raising that timeout,
not by needing the run loop to continue meanwhile.

**Unlock condition:** a task class that genuinely requires a process to outlive
a turn (a dev server the agent must interact with), *and* a design that keeps
approval binding and effect attribution unambiguous while it runs.

## Decision

None of the three is built. The deliverable is the verdict plus the unlock
conditions, so the absences are decided rather than merely inherited from a
non-goals list.

## Consequences

- Haven's daily-use surface stays at 12 tools and one network peer.
- The v1 non-goals list is now backed by reasoning per item, and each item has
  a stated condition that would reopen it.
- The audit's other findings — which were about engineering infrastructure, not
  capability — were acted on instead: a single declarative gate graph shared by
  CI and developers, a generated tool/policy table, the request envelope in the
  journal, and a decision-note tier below ADRs.

## Rollback

Nothing to roll back; this ADR builds nothing. If an unlock condition is met,
the corresponding capability gets its own ADR showing how it satisfies the
extension invariants in ADR 0016.
