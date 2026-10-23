# ADR 0041: Speculative Git Worktrees & Evidence-Gated Atomic Reconcile

Date: 2026-10-23
Status: Accepted

## Context

In complex, multi-step agentic coding workflows, agents frequently attempt experimental refactorings, speculative code edits, or dependency reorganizations. Previously, all operations occurred directly inside the user's primary working tree. This led to serious operational risks:
1. **Workspace Pollution on Aborted Runs**: If a task failed, hit budget or step limits, or was cancelled midway by the user, partially edited files, temporary artifacts, or broken test files were left lingering in the user's repository.
2. **Review Overhead for Half-Finished Work**: Users were forced to manually run `git reset --hard` or `git checkout` to restore their previous clean working state.
3. **Dirty State Vulnerability**: If concurrent modifications occurred outside Haven while an agent was running, preimage digests could become invalidated or conflict unpredictably.

## Decision

**Introduce speculative git worktree isolation and an evidence-gated atomic reconcile mechanism (`EphemeralWorktree`):**

1. **Ephemeral Detached Worktree Allocation (`haven.adapters.worktree`)**:
   - When isolated execution is requested (via `--worktree` / `--isolated`, `[runtime] worktree = true`, or `HAVEN_WORKTREE=1`), Haven verifies that the target workspace is a valid Git repository via `git rev-parse --is-inside-work-tree`.
   - A lightweight, detached-HEAD git worktree is created under `.haven/worktrees/<run_id>` using `git worktree add --detach <path> HEAD`.
   - By creating detached worktrees, zero temporary or dangling git branches are created.
   - The `.haven/` root is already protected and ignored, ensuring worktree paths are isolated from the main workspace.

2. **Workspace Redirection**:
   - The composition root (`bootstrap.py`) points `FsWorkspace` to the isolated worktree directory (`ephemeral_wt.worktree_path`).
   - The agent executes its complete turn loop, tool invocations, syntax pre-commit gates, and verification recipes completely inside this ephemeral sandbox.
   - The user's main workspace files remain 100% pristine throughout the execution of the task.

3. **Evidence-Gated Atomic Reconcile (`reconcile_worktree_if_succeeded`)**:
   - Reconciliation back to the main workspace is strictly gated by the Evidence Gate:
     - **Only** if `outcome.status is RunStatus.SUCCEEDED` (which guarantees all diff reviews passed and verification checks succeeded after the last edit), the modified, created, and deleted files are scanned via `git status --porcelain` and atomically transferred back to the user's primary repository root using temporary atomic replacement (`tmp.replace(dst)` with permission preservation).
     - If the run fails (`RunStatus.FAILED`), stops (`RunStatus.STOPPED`), times out, or is cancelled, **zero files are transferred**.

4. **Zero-Pollution Lifecycle Guarantee**:
   - In `AppServices.close()`, `cleanup_ephemeral_worktree` executes unconditionally in a `finally` block.
   - The worktree is forcibly detached and removed via `git worktree remove --force` followed by `git worktree prune`, leaving no dirty files, metadata, or worktree references behind.

## Consequences

- Risky and exploratory coding runs operate with zero blast radius on the user's clean workspace.
- The user's main repository remains untouched until a solution is completely proven and verified by the Evidence Gate.
- If a run fails or is cancelled, cleanup is instantaneous and leaves no dirty files to revert.
- All single-channel execution invariants (`ToolPipeline`), sandbox containment, and Evidence Gate contracts are preserved.
