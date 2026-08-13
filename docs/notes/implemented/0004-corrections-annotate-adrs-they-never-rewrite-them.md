# Corrections annotate an ADR; they never rewrite it

Date: 2026-08-14

## Context

A full audit of all 28 ADRs found three that had gone stale or wrong:

- **ADR 0009** argued that misclassifying an exec command "costs a skipped
  prompt, not an escape". ADR 0026 later proved that false for reads — and 0009
  carried no pointer to it. Nothing in the repository referenced 0026 at all.
  A reader of 0009 alone took away a wrong security model.
- **ADR 0001** still described "six repo tools" (there are twelve) and declared
  OS sandboxing out of scope, which ADR 0009 reversed.
- **ADR 0010** offered a remedy for compaction thrash — lower the budget — that
  is directionally backwards, for a risk the later hard clamp had already made
  impossible.

The common failure is not that decisions changed. It is that the *record* of a
decision is read one file at a time, usually by someone who found exactly one
of them.

## Decision

Corrections are **annotations in place**, never edits to the original argument:

- A superseded claim keeps its original text and gains an adjacent blockquote
  saying what is wrong and which ADR corrects it.
- The ADR's Status line gains the pointer too, so a reader knows before reading
  the body.
- The correcting ADR states the relationship in the **active voice** in its own
  Status line (`Accepted (corrects ADR 0009)`), because that is what the gate
  below keys on.
- `scripts/check_adr_links.py` enforces the backlink: if ADR N says it amends,
  supersedes, corrects, or reverses ADR M, then M must mention N. It runs in the
  `adr-links` gate.

## Alternatives considered

- **Edit the wrong claim out of the original ADR.** Rejected: the reasoning that
  produced a real security gap is the most instructive thing in the file, and
  deleting it destroys the ability to ask "how did we convince ourselves?" It
  would also make ADR 0026's own Context section incoherent.
- **Mark the whole ADR superseded.** Too blunt: 0009's sandbox design, backend
  table, and threat analysis are all still current. Only two paragraphs are wrong.
- **Rely on the convention without a gate.** This is exactly what was already in
  place — 0009 correctly declared "Amended by ADR 0017" and then nobody added
  the 0026 pointer nine ADRs later. A forward reference is cheap to write and
  easy to forget, which is the profile of something to automate.
- **Have the gate detect contradictions rather than missing links.** Not
  mechanically decidable. The gate checks a cross-reference, which is a proxy —
  it fires only when an author has already noticed the relationship and said so.

## Consequences

The gate is a backlink checker, not a truth checker: it cannot catch an ADR that
silently contradicts another without saying so. Its teeth depend on authors using
a strong verb, which is why the active-voice Status line is part of the
convention rather than a matter of taste.

Building it also exposed a bug in its own first draft: the regex read "reversed
**by** ADR 0009" as a claim to overturn 0009, inverting the relationship. The
passive voice is written by the document *being* corrected, so it is the backlink
and must create no obligation. The distinction is now the load-bearing part of
the pattern and is pinned by a test.
