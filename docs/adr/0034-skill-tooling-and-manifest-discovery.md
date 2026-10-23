# ADR 0034: skill lifecycle tooling and companion manifest discovery

Date: 2026-10-22
Status: Accepted

## Context

ADR 0031 shipped skills as declarative prompt fragments in a stable catalog prefix, and ADR 0032 made skills loadable by the model via `skill.read` and allowed them to request standing approval for tools via `allowed-tools`.

While these two ADRs established Haven's core safety invariants (tamper-proof `.haven` component, fail-closed parsing, untrusted framing, model self-loading strictly unable to escalate privileges), real developer workflows revealed two practical friction points:

1. **No developer lifecycle tooling**: Creating a skill required manually creating directories and handwriting YAML frontmatter. Because parsing fails closed on any unquoted colon, unregistered tool name, or directory-name mismatch, authoring mistakes were difficult to diagnose without inspecting debug traces. There was no `init`, `info`, or `validate` CLI command.
2. **Invisible companion files**: ADR 0032 allowed `skill.read` to read auxiliary files (e.g. `reference.md`), but the model had to guess which files existed unless `SKILL.md` explicitly hardcoded every path in its prose.

## Decision

**Add developer lifecycle tooling (`haven skills init`, `info`, `validate`) and companion file manifest discovery.**

1. **`haven skills init <name> [--scope project|user] [--tools ...]`**:
   - Scaffolds a new skill directory with a standard, syntactically valid `SKILL.md` template;
   - Validates the skill name against `_NAME_PATTERN` (`[a-z0-9][a-z0-9-]{0,63}`);
   - Verifies that any requested `allowed-tools` exist in `KNOWN_TOOLS`;
   - Refuses to overwrite an existing skill directory.

2. **`haven skills info <name>`**:
   - Discovers skills and presents formatted metadata for a named skill: name, scope, source path, SHA-256 digest, description, granted tools, companion files list, and body preview;
   - If a skill was refused, explains the exact refusal reason.

3. **`haven skills validate` (or `check`)**:
   - Performs a static audit of both user and project skill directories;
   - Evaluates all files against parsing contracts and symlink safety;
   - Returns a non-zero exit code (`EXIT_POLICY = 3`) if any refusal exists, making it suitable for CI gates.

4. **Companion file manifest discovery**:
   - `list_skill_files(catalog, skill)` safely enumerates normal companion files within the skill's own directory, excluding `SKILL.md`, hidden files/directories, and symlinks;
   - `execute_skill_read` automatically includes `companion_files: [...]` in the `ToolResult` payload, enabling the model to immediately discover auxiliary documentation and reference materials without blind guessing.

5. **TUI visibility**:
   - `PresenterState` includes `selected_skills` in the header bar (`skills: [...]`), providing real-time visual feedback on active skills.

## Consequences

- Skills remain strictly advisory context and do not widen what the deterministic policy layer permits.
- The tool count remains 11 (`TOOL_VERSION = "5"` is untouched), and no execution path bypasses the OS sandbox or evidence gate.
- Developers gain convenient first-class CLI commands for authoring, auditing, and inspecting skills.
