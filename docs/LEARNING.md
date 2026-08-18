# Learning from the frozen Haven baseline

**English** | [中文](LEARNING.zh-CN.md)

This repository is kept as a studyable engineering baseline. The code is the
authority for behavior; generated tables and executable gates are the authority
for changing counts. Documents have different roles, and reading them in the
wrong order can make an old plan look like the current implementation.

## What is current

Read these as the description of the frozen implementation:

1. `README.md` — product boundary, commands, measured results, and limitations.
2. `docs/PROJECT_CARD.md` — one-page design and trade-off summary.
3. `docs/ARCHITECTURE.md` — layers, tool channel, state machine, and storage.
4. `docs/SECURITY.md` — trust model, enforcement, and explicit gaps.
5. `docs/EVAL.md` and `docs/EVAL_LIVE.md` — what was measured and what the
   measurements do not prove.
6. `course/` — the guided reading path through the implementation.

`docs/codebase/` is a current audit companion. It summarizes the same code from
the perspectives of structure, stack, testing, integrations, conventions, and
known concerns. Volatile counts are intentionally delegated to the generated
metrics table instead of being copied there.

## What is historical

- `Haven_TUI_Coding_Agent_项目计划.md` is the original proposal. Its checkboxes
  show planned scope, not current completion.
- `docs/ROADMAP*.md` and `docs/superpowers/{specs,plans}/` preserve how the
  implementation was designed and sequenced. Their opening status notices say
  where later code or ADRs superseded them.
- ADRs are immutable decision history. A later discovery annotates or supersedes
  an ADR; it does not rewrite the original reasoning. Follow the amendment links
  at the top before relying on an older claim.
- `docs/POSTMORTEM.md` and historical portions of `docs/EVAL_LIVE.md` retain the
  numbers observed at that point in time. They are evidence of the development
  path, not the current repository totals.

## Truth hierarchy

When two statements appear to disagree, use this order:

1. executable policy, contracts, and runtime behavior in `src/haven/`;
2. generated tool/metrics tables and the full quality-gate result;
3. current architecture, security, evaluation, and course documentation;
4. ADRs, with later amendments taking precedence;
5. historical roadmaps, specs, plans, postmortems, and old measurements.

The repository enforces this boundary with:

```bash
uv run python scripts/gates.py --mode fast  # docs, format, lint, types, layering
uv run python scripts/gates.py --mode full  # adds tests, coverage, eval, metrics
```

The `docs` gate checks local Markdown links, the documented CLI/TUI command
surface, version agreement, and the historical labels. The generated metrics
and tool-table gates independently derive their facts from the code and reports.

## Safe exercises

Start by running the full offline suite. For modifications, work on a branch and
keep the frozen baseline as the comparison point. The best learning sequence is:

```text
policy + approval
  -> tool pipeline
  -> run loop + context
  -> evidence gate
  -> persistence + recovery
  -> TUI presenter
  -> offline and live evaluation reports
```

No API key is needed for the course or any committed test. Live exercises are
optional, paid, non-reproducible, and must be explicitly confirmed.
