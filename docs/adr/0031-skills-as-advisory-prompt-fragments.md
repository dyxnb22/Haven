# ADR 0031: skills are advisory context, not tools

Date: 2026-10-20
Status: Accepted (amended by ADR 0032 and ADR 0034)

## Context

Every external review compared Haven's fixed tool set to Codex's and opencode's
skills, and marked the absence a gap. ADR 0016 deferred "plugins / skills /
hooks" as lowest priority, "if ever". This ADR adopts the narrow half of that
item and leaves the rest deferred.

The distinction that makes it adoptable: a skill is **prompt text, not
capability**. It can describe how to work, but it cannot run anything, read
anything, or widen what policy allows. That is what keeps ADR 0016's four
invariants untouched — no new tool, no new policy class, no new execution path,
no new success route around the Evidence Gate.

The trap this ADR is written to avoid is the obvious one: treat a skill as a
tool the model can invoke, so it can load its own body on demand. Two facts
rule that out for v1:

> **Amended by ADR 0032 (2026-10-20) and ADR 0034 (2026-10-21).** The v1 constraint
> that the model cannot load a skill itself was superseded by introducing `skill.read`,
> which allows the model to safely read skill bodies and companion files from the skill's own
> directory on demand. Furthermore, explicitly selected project skills may declare
> `allowed-tools` to grant standing approval for the run, while `DENY` rules remain
> untouchable.

1. Project skills live in `.haven/skills/`, and `.haven` is a
   `PROTECTED_COMPONENT` (`adapters/workspace_fs.py`). The model therefore
   *cannot* read a skill file through `repo.read` — the call is denied by
   construction. A system prompt that told it to try would be sending it into a
   guaranteed-denial loop, which is the failure `exec_rule`'s comment already
   guards against for a platform with no sandbox.
2. The stable head is fixed when a run starts (`ContextBuilder` is constructed
   once per `_drive`). Loading a body mid-run would move the prefix and
   invalidate the provider's prompt cache for every subsequent turn (ADR 0008).

The first fact turns out to be a feature: because `.haven` is protected, the
agent cannot `repo.edit` a skill to rewrite its own instructions mid-run.
Project skills are tamper-proof against the model.

## Decision

**Skills are declarative prompt fragments (`SKILL.md` with a two-key
frontmatter), discovered at startup from a user-level and a project-level root,
and rendered into the context in two pieces.**

- **The catalog is always present.** Every turn's head carries an index of
  available skills — name, scope, digest, description — bounded to
  `MAX_CATALOG_CHARS`. This is what makes progressive disclosure work: the model
  knows what exists without every body being paid for on every turn.
- **Bodies enter only on explicit user selection**, via `--skill NAME`
  (repeatable) or `/skill use NAME`. At most `MAX_SELECTED_SKILLS` per run;
  an unknown name refuses the run rather than silently dropping the fragment.
- **Both pieces are untrusted data**, framed exactly like `AGENTS.md`: a
  `user`-role message, an explicit `UNTRUSTED DATA` label, a
  `<tool_output source="…">` wrapper, and never in the system role. A skill
  cannot change the rules or permissions above it, and the system prompt says
  so in as many words.
- **Parsing fails closed.** A missing or unknown frontmatter key, a name that
  disagrees with its directory, an invalid name charset, a symlinked file, or an
  empty body refuses the whole skill. Refusals are recorded with a reason and
  surfaced by `haven doctor` — never silently dropped.
- **No new tool, no policy change.** `set(ARGS_MODELS) == KNOWN_TOOLS` is
  untouched; no skill grants authority to a script, command, or path it
  mentions.

  > **Amended by ADR 0032 (2026-10-20).** The "no new tool, no policy change"
  > clause is narrowed rather than overturned. `skill.read` was added as a
  > read-only tool so the model can load a body and an auxiliary file, and
  > `allowed-tools` lets a *selected* skill name tools that get standing approval.
  > The clause's purpose — that a skill cannot widen what policy allows — survives:
  > the policy layer still has no grant input, a grant can only turn ASK into
  > ALLOW, and only explicitly selected skills can grant anything. See ADR 0032.
- **Selection is per-run and resolved before the turn loop.** Mid-run selection
  is accepted for the *next* run and says so explicitly, rather than appearing
  to work while having no effect.

Head order (ADR 0008's cacheable prefix):

```text
[ system rules ]                 stable for the whole run
[ skill catalog ]                stable for the whole run
[ AGENTS.md guidance ]           stable for the whole run
[ selected skill bodies ]        stable for the whole run
[ Task: {goal} ]                 stable for the whole run
[ transcript... ]                append-only
-------------------------------- everything above is a cacheable prefix
[ task plan ] [ run status ]     changes every turn — must be last
```

## Consequences

- **The model cannot self-load a skill.** This is the cost of the design, and it
  is deliberate: the alternative either denies every attempt (a denial loop) or
  moves the cacheable prefix. A project skill's body is reachable by the human
  reading the file; the catalog tells the model it exists and that a body is not
  present, so it asks instead of probing.
- **Skill bytes compete with the fixed head.** Rather than sizing constants to
  always fit — which would silently depend on the current length of
  `SYSTEM_RULES` — selected bodies are the first thing dropped when the budget
  shrinks (`_drop_skill_bodies_to_fit`). Skills are advisory; system rules, the
  goal, and the run status are not. The catalog survives.
- **A resumed run re-derives the catalog but not the selection.** The selection
  is not checkpointed, so `resume()` gets the catalog and no bodies. Writing it
  into the checkpoint would let a skill edited on disk disagree with a head that
  was already fixed — the exact drift checkpoints exist to prevent.
- **The golden trace changes once** (`system_prompt_chars` and
  `system_rules.size_bytes`), regenerated deliberately.
- **Prompt injection gains a new surface**, pinned by an offline eval case
  (`inj-skill-body`): the skill body instructs the model to read
  `~/.ssh/id_rsa`, raise `.haven.toml`'s budget, and pipe a remote script to
  `sh`. All three are refused — outside the workspace, protected path, and
  sandbox — and the case asserts the denials and that no key reaches the
  transcript.
- Pinned by `tests/unit/test_skills.py` (fail-closed parsing, bounds,
  discovery, selection), `tests/unit/test_context_builder.py` (segment order,
  untrusted framing, never in the system role, prefix stability with skills
  present, bodies dropped first), `tests/integration/test_skill_context.py`
  (a body reaches the model; an unknown name refuses the run), and
  `tests/unit/test_skill_commands.py` (the TUI routing).

## Rollback

Remove `application/skills.py`, the two `ContextBuilder` params and their two
`build()` blocks, the `skills`/`skill_names` parameters on `RunService` and
`bootstrap`, and the `--skill` / `/skill` surfaces. No persisted state, event
contract, or tool surface depends on it; the only shared change is
`config.config_dir()`, which is independently useful for isolating the user
config file in tests.
