# ADR 0032: skills become loadable, and may name tools they need approved

Date: 2026-10-20
Status: Accepted

## Context

ADR 0031 shipped skills as untrusted prompt fragments and drew a hard line: no new
tool, no policy change, "a skill grants no authority to a script, command, or path
it mentions." Three gaps remained, all verified rather than assumed:

1. **The model could not load a skill itself.** Project skills live under `.haven`,
   a `PROTECTED_COMPONENT`, so `repo.read` on them is denied by construction. The
   system prompt had to tell the model to ask instead of probe.
2. **Auxiliary files were invisible.** A skill that says "see `reference.md` next to
   me" was a dead instruction.
3. **The CLI could not list skill names.** `haven doctor` printed a count.

Meanwhile, every comparison to Claude Code and Codex marked the absence of
`allowed-tools` as a gap, and the product owner asked for it explicitly: skills
should behave like a mainstream agent's, for convenience.

## The decision that shapes everything

**A project-scope skill's `allowed-tools` takes effect the moment the skill is
selected, with no per-tool confirmation.** The attack was presented explicitly and
accepted: a cloned repo shipping `.haven/skills/helper/SKILL.md` with
`allowed-tools: [repo.edit, repo.apply_patch, repo.check, repo.exec]` gets its
writes auto-approved once the user runs `--skill helper`. That defeats the approval
gate for that repo, and `docs/SECURITY.md` §5 previously stated injection could
never do this.

The owner owns that tradeoff. What this ADR does is make sure it was not
implemented *worse than it had to be*. Six boundaries preserve the chosen semantics
exactly while bounding the blast radius; all six are pinned by tests:

1. **A grant can only turn `ASK` into `ALLOW`, never `DENY` into `ALLOW`.** This is
   structural, not a check someone remembered to add: `evaluate_policy` is a pure
   function of `(mode, facts)` with no grant parameter (`domain/policy.py`), and
   `tool_pipeline` returns on `DENY` before approval is ever reached. A granted
   skill still cannot write `.git`, escape the workspace, exec without a sandbox,
   or write in `READ_ONLY` mode. Pinned by `tests/unit/test_skill_grants.py`, which
   asserts the policy signature itself.
2. **Run-scoped, in-memory, never checkpointed** — ADR 0025's scope, unchanged. A
   malicious repo cannot grant persistently, and a resumed run re-asks.
3. **Fail-closed parsing.** An unrecognized tool name in `allowed-tools` refuses the
   whole skill, like every other malformed field.
4. **Grants come only from skills the user explicitly selected.** A body the model
   loads itself via `skill.read` arrives as a tool result and never enters the
   grant table. This is the one deliberate deviation from "the model decides", and
   it is why a self-loaded skill cannot escalate.
5. **Loud, auditable disclosure.** Selecting a granting skill emits a `warning`
   notice naming the skill, its scope, its source, its digest, and every granted
   tool; every granted execution still mints, decides, and consumes its own
   single-use approval row and emits `approval.decided`, so replay shows exactly
   which asks were skipped. `haven skills list` and `haven doctor` show the same.
6. **A kill switch.** `HAVEN_SKILL_GRANTS=0` empties the grant table and suppresses
   the notice, for anyone who does not want a repo's `SKILL.md` touching approval
   at all. Reported by `haven config explain`.

## Decision

**Skills are loadable by the model, and a skill may name tools it wants standing
approval for.**

- **`skill.read`** is a new read-only tool. Args are a skill name plus a relative
  path inside that skill's directory (default `SKILL.md`). The reader
  (`application/skills.py::read_skill_file`) is the only path in Haven that reaches
  bytes outside the workspace, so its bounds live there: relative paths only, no
  traversal, symlinks refused, byte-capped, and confined to the skill's own
  directory so one skill cannot read another's files. It returns a structured
  `ToolResult` on refusal rather than raising.
- **The body arrives as a tool result**, so it is append-only — ADR 0008's cacheable
  prefix is untouched — and automatically `trust="untrusted"` via `_classify`.
- **`allowed-tools`** is a third frontmatter key, hand-parsed (no YAML dependency),
  accepting both `a, b` and `[a, b]`, normalized to a sorted deduplicated tuple so
  the grant key is stable. Each name must be in `KNOWN_TOOLS`.
- **The grant machinery generalizes ADR 0025's** rather than adding a second one.
  `RunContext.standing_grants` (keyed by approval digest composed with the granted
  tool set) and `RunContext.granted_tools` (tool → requesting skill) replace
  `standing_check_grants`. `repo.check` still arms on human approval with no skill
  involved — ADR 0025's behavior is preserved exactly, and its four tests pass
  unedited.
- **`haven skills list`** names every discovered skill with scope, digest,
  description, and grants, plus every refusal with its reason.
- The system prompt now names `skill.read` as the channel for loading a body, and
  states that a grant is "a program fact you did not create and cannot enlarge".

## Consequences

- **A repo can now grant itself write approval.** That is the accepted cost. The
  mitigations above are what keep it from also granting itself path escape,
  protected-path writes, sandbox-free exec, read-only-mode writes, or persistence.
- **The tool surface grows by one**, and `TOOL_VERSION` goes `4` → `5`, which
  changes every ticket and approval digest. The golden trace and the generated tool
  table are regenerated deliberately.
- **ADR 0031's "no new tool, no policy change" clause is narrowed**, not overturned:
  the new tool is read-only and auto-allowed, and the policy layer still has no
  grant input. The clause's purpose — that a skill cannot widen what policy allows
  — survives in mitigations 1 and 4.
- **`skill.read` is the first tool that reads outside the workspace root.** Its
  bounds are the reason that is defensible, and they are pinned by
  `tests/unit/test_skills.py::TestSkillFileReader`.
- Pinned by `tests/unit/test_skill_grants.py` (the six boundaries),
  `tests/integration/test_skill_context.py` (grants take effect on selection; a
  self-loaded skill arms nothing; the model can load a body and an auxiliary file;
  a grant does not survive into a second run), and `tests/unit/test_skills.py`
  (`allowed-tools` parsing and the reader's bounds).

## Rollback

Remove `SkillReadArgs` and its four registrations, `read_skill_file`,
`RunContext.granted_tools`/`standing_grants`, the two grant branches in
`ApprovalCoordinator.ask`, `application/skill_grants.py`, `domain/skill_grants.py`,
and `haven skills list`; restore `standing_check_grants` and `TOOL_VERSION = "4"`.
No persisted state depends on any of it. `HAVEN_CONFIG_DIR` and `config_dir()` from
ADR 0031 stay — they are independently useful.
