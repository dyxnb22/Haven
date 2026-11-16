# Save project notes only on explicit user input

Common project commands and conventions should survive new conversations without
editing repository guidance. Store only notes explicitly submitted through the
user interface, keyed by the canonical main workspace path in the existing user
database. An isolated worktree is an execution location, not a new project.

Notes have stable IDs and UTC creation times. Bound storage to 20 entries,
500 characters per entry and 2000 total text characters per project. Reject
overflow without evicting older entries. SQLite schema 6 adds a separate table;
deleting notes does not delete input history, runs or saved task snapshots.

Load notes into the guidance snapshot defined in
[0044](0044-project-guidance-snapshots.md), but render them as a separate
untrusted context segment. New runs and follow-ups use the current snapshot;
resume and question answers retain their saved snapshot, including deleted notes.
Older snapshots default to no notes. Notes cannot alter policy, approvals,
verification recipes, tools or the definition of success.

`/remember TEXT`, `/memory forget ID` and `/memory clear` are idle-only UI
operations. They update the loaded notes without re-reading repository files.
No model tool writes memory, and there is no automatic extraction or cross-project
sharing. A storage failure may occur after a write commits; retain the previous
loaded snapshot and direct the user to reload before retrying.
