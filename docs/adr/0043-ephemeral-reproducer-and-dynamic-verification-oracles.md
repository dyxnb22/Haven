# ADR 0043: Ephemeral Reproducer Synthesis and Dynamic Verification Oracles

Date: 2026-10-24
Status: Accepted (extends ADR 0003 and ADR 0009; preserves ADR 0025 and ADR 0033)

## Context

Haven's core thesis is that the model may only propose operations, while deterministic code owns permissions, execution, and the definition of success via the Evidence Gate (ADR 0003).

When a run performs code modifications, the Evidence Gate mandates a passing `repo.check` recipe recorded after the last write. However, historically, check recipes were strictly static:
1. They were loaded only from `.haven.toml` at startup or inferred via static project discovery (e.g. `pytest`, `cargo test`).
2. When operating on repositories without a pre-configured test suite, or when diagnosing subtle bugs where existing tests do not assert the failure, modifying code immediately resulted in a terminal failure: `verification_unavailable`.
3. The model was instructed that changes in unverified workspaces were refused, turning lack of test configuration into a hard blocker for valid refactors and bug fixes.

To achieve autonomy on arbitrary repositories without lowering verification standards, Haven needs a way for the agent to synthesize an ephemeral test/reproducer script, register it dynamically as a sandboxed recipe, and prove failure (Red) before fix and success (Green) after fix.

## Decision

**Introduce dynamic ephemeral verification recipe proposal (`recipe.propose`) within the single execution pipeline:**

1. **Tool Interface (`recipe.propose`)**:
   - `recipe_id: str`: Alphanumeric identifier (e.g., `repro-calc-bug`).
   - `argv: tuple[str, ...]`: Program and arguments to execute inside the OS sandbox.
   - `timeout_seconds: float = 60.0`: Bounded execution timeout.
   - `description: str = ""`: Human-readable purpose of the verification reproducer.

2. **Policy & Permission Enforcement**:
   - `recipe.propose` is classified as `RiskLevel.HIGH` in `EFFECT_TOOLS`:
     - Under `PermissionMode.READ_ONLY`: hard `DENY` (`read_only_mode`).
     - Under `ApprovalMode.ASK` and `ApprovalMode.SMART`: returns `PolicyDecision.ASK`, halting execution for explicit human review with an `ApprovalRequested` card. Proposing an executable command is never silently auto-approved under `smart` mode.
     - Under `ApprovalMode.FULL`: automatically approved if path validation succeeds.
   - Command constraints:
     - `allow_network = False` is unconditionally enforced. Ephemeral recipes cannot make network calls.
     - Argv items are path-checked; traversal outside the workspace or referencing protected directories (`.git`, `.haven`) yields hard `DENY`.
     - Execution occurs within the OS sandbox (Seatbelt / Landlock), with filesystem write protections identical to `repo.check`.

3. **Lifecycle & Dynamic Evidence Integration**:
   - Registration dynamically updates `RunService._recipes`, instantly flipping `verification_available` to `True`.
   - `ContextBuilder.register_recipe` updates active recipe visibility across subsequent turns.
   - The recipe is marked `ephemeral=True` (run-scoped, in-memory only, discarded on session close without polluting `.haven.toml`).

4. **TDD Verification Loop**:
   - Step 1: Agent writes a reproducer test script via `repo.create` or `repo.edit`.
   - Step 2: Agent registers the script via `recipe.propose`.
   - Step 3: Agent runs `repo.check` to confirm failure (Red).
   - Step 4: Agent edits source code to fix the problem.
   - Step 5: Agent re-runs `repo.check` to confirm success (Green).
   - Step 6: Evidence Gate verifies the passing check post-write, satisfying success criteria.

## Consequences

- Removes the fatal dependency on pre-existing test suites; Haven can now autonomously verify tasks in arbitrary repositories.
- Preserves the Evidence Gate and Single Execution Channel: success is still determined by exit codes and diffs, not LLM self-reports.
- Ephemeral recipes are strictly sandboxed, non-networked, and auditable in the event journal.
