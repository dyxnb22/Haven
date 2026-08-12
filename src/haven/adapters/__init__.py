"""Concrete adapters for providers, filesystem, processes, and persistence.

Each module implements one port from `haven.ports`; only `bootstrap.py`
(the composition root) may wire them into services:

    providers/            ModelPort: OpenAI-compatible streaming adapter +
                          the deterministic ScriptedModel used by tests/eval
    workspace_fs.py       WorkspacePort on the real filesystem: normalized
                          paths, preimage digests, atomic writes, patch
                          transactions, run-scoped originals for diff/rewind
    process_executor.py   ExecutorPort: fixed-argv recipes and sandboxed
                          commands, scrubbed env, bounded output
    sandbox/              SandboxLauncher backends: Seatbelt (macOS) and
                          Landlock (Linux), selected at bootstrap
    sqlite_session.py     SessionStorePort on SQLite/aiosqlite (WAL), with
                          the append-only event journal and schema migrations
    memory_session.py     the same contract in memory, for tests and eval
    git_baseline.py       records the repo's git state at run start
    workspace_lease.py    advisory single-writer lease across processes
"""
