# Project guidance belongs to a run snapshot

Project guidance can change during a long-lived TUI session. Re-reading it when
resuming a task would silently change the task's context, while rejecting all
file changes would make recovery unnecessarily fragile.

Freeze the bounded advisory guidance, including source and truncation metadata,
when each new run or follow-up starts. Persist it in the checkpoint and use the
saved snapshot on resume, including after a pending question is answered.
`/memory reload` explicitly replaces the session default while idle; it affects
new runs and follow-ups. An active run, pending question or pending review blocks
reload. Loading a new snapshot blocks starting a run until the load finishes.

The checkpoint field is optional: old checkpoints load without a database
migration. On their first resume, emit a warning and pin the current session
guidance, preserving their previous recovery behavior. The original serialized
payload remains the basis of checksum verification. Older Haven versions may
reject checkpoints written with this additional field.

Snapshots are advisory data, never authority. Reload only reads the same
bounded guidance sources in the active workspace (the isolated worktree when
enabled); it does not reload permissions, skills, recipes or MCP discovery.
Failure or cancellation before publication retains the previous default.

This extends [0004](0004-durable-execution-and-recovery.md) and
[0015](0015-sessions-and-multi-turn.md). Selected skill bodies and standing
grants retain their existing, separate recovery rules in
[0031](0031-skills-as-advisory-prompt-fragments.md) and
[0032](0032-skills-loadable-and-may-name-tools.md).
