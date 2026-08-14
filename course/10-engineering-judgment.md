# Module 10 — Engineering judgment

> Files: `docs/adr/`, `docs/POSTMORTEM.md`, `docs/PROJECT_CARD.md`,
> `Haven_TUI_Coding_Agent_项目计划.md`
> ADR: all of them, but especially
> [0007 — what was *not* built](../docs/adr/0007-subagents-mcp-and-deterministic-review.md)

## Learning objectives

- Apply a **benefit gate** before building a feature, and write down why you did
  *not* build something.
- Record decisions as ADRs so future-you (and an interviewer) can see the
  reasoning, not just the result.
- Treat failures as artifacts: root cause, fix, and a regression test.
- Tell the difference between "narrow but rigorous" and "broad but shallow," and
  choose deliberately.

This module has less code and more thinking. It is also the part that most
distinguishes an engineer from a feature-lister, so do not skip it.

## The benefit gate

The project's rule: before adding a capability, write one page — the problem, the
current baseline, the options, the decision, the metric that will tell you if it
worked, and the rollback. If you cannot state a measurable benefit, you do not
build it yet.

ADR 0007 is the model case. Two capabilities common in "top-tier" agents — an
MCP client and a model-driven Reviewer subagent — were evaluated and
**deliberately not built**:

- **MCP** would break the invariant that every tool is compiled in and provably
  classified (`set(ARGS_MODELS) == KNOWN_TOOLS`), and it improves no metric Haven
  actually measures. Deferred, with a written list of what would have to be true
  to revisit.
- **A Reviewer subagent** cannot pass its own benefit gate offline: with a
  scripted model it would only "find" the defects its script authored. The
  cheaper, more reliable **deterministic review** (Module 06) was adopted
  instead — the plan's own fallback.

Notice the shape: a *reasoned no* with conditions for a future *yes*. In an
interview, "here's why I didn't build MCP" is stronger than a half-working MCP,
because it demonstrates the scarce skill — scope control under a real invariant.

## ADRs: decisions with their reasons attached

Read two or three ADRs in `docs/adr/`. Each has Status, Context, Decision,
Consequences, and Alternatives-considered. The value is not the decision; it is
the *discarded* options and the trade-off. Six months later nobody remembers why
`repo.create` refuses existing files — unless it is written down (it is: so
"create" can never blank a file the agent never read).

Habit to copy: when you make a load-bearing choice, write the ADR *before* the
memory of the alternatives fades. Number them; link them from the code and the
tests they govern.

## Failures are artifacts

Read `docs/POSTMORTEM.md`. Three real defects, each with symptom, root cause, the
*tempting wrong fix*, the actual fix, and the regression guard:

1. The security gate cried wolf on `__pycache__` — a measurement bug in the
   safety metric itself. The wrong fix (widen every allow-list) would have
   eroded the gate; the right fix excluded derived bytecode once, centrally.
2. Building the `debug-context` tool immediately exposed that `AGENTS.md` was
   mislabelled *trusted* and sitting in the system role. Observability paid for
   itself before it was ever used in anger.
3. The offline test suite quietly spent money because an exported provider key
   leaked into pytest; the fix strips *every* credential-shaped variable, not a
   named list, and a test now guards the guard.

The meta-lesson runs through all three: **when a safety signal fires, first ask
whether the measurement is right.** And prefer one central, explainable rule over
N local exceptions.

## Narrow-but-rigorous vs. broad-but-shallow

The most important judgment in the whole project is the scope itself. Haven has
twelve compiled-in tools, one provider connection, no shell-string tool, and no
multi-agent orchestration. An explicit interpreter in `repo.exec` is still
possible, but it always asks and runs inside the mandatory exec sandbox. In
exchange Haven can make *provable* claims: unauthorized changes at 0 across the
offline security gate, ambiguous effects never auto-replayed, and every run with
one named stop reason.

An approval digest can bind an interpreter argv exactly; it cannot predict the
program hidden behind that string. The kernel profile supplies that missing
capability boundary. The lesson is not “narrow is always better.” It is:
**choose your action space deliberately, and know which guarantee comes from
schema, approval, sandbox, or evidence.** Articulating that trade-off is the
deliverable.

## Exercises

1. **Write a gate.** Pick a feature you'd want in Haven (say, a `repo.symbols`
   tool via tree-sitter). Write the one-page benefit gate. Be honest about the
   baseline: is there evidence text search is failing? (ADR 0007's rejection of
   LSP turned on exactly this.)
2. **Write an ADR.** For a decision you have already made in your own code, write
   it up in the `docs/adr/` format, especially the alternatives you discarded.
3. **Write a postmortem.** Take a bug you fixed recently and write the
   symptom / root cause / tempting-wrong-fix / fix / regression-test entry.
4. **Argue the scope.** In a paragraph, defend Haven's “no implicit shell
   string” decision to a reviewer who wants pipes and redirection. Include what
   changes when the argv explicitly names `bash -c`, then argue the *other* side.

## Self-check

- What are the five parts of a benefit gate?
- Why is a written "no" (like ADR 0007) valuable rather than a sign of
  incompleteness?
- Give the meta-rule that connects all three postmortems.
- State the trade-off Haven's narrow scope buys, in one sentence.

## Further reading

- Every ADR in `docs/adr/`; `docs/PROJECT_CARD.md` for the trade-off table.
- The original plan (`Haven_TUI_Coding_Agent_项目计划.md`) — §10 risk ledger and
  §11 "enhancements behind a benefit gate."
