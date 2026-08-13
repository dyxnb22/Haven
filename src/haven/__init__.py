"""Haven: an evidence-driven, replayable, locally scoped coding agent.

One sentence: the model may only *propose* actions; deterministic code owns
permission, execution, and the definition of success.

Layer map (enforced by import-linter; see pyproject `[tool.importlinter]`)
==========================================================================

    interfaces/   CLI (Typer) and TUI (Textual). Turn user intent into
                  service calls, render events. Never import adapters.
    bootstrap.py  The composition root - the ONLY module that wires
                  adapters into application services.
    application/  Use cases. The two big orchestrators live here:
                  run_service.py (the agent loop) and tool_pipeline.py
                  (the single execution channel). Plus context_builder,
                  compaction, recovery, replay, approvals, registry.
    domain/       Pure logic, no I/O: policy, approval digests, budgets,
                  evidence gate, tickets, state transitions, review.
    ports/        Protocols the core depends on: model, workspace,
                  executor, sandbox, session store, event sink, clock.
    adapters/     Implementations of ports: OpenAI-compatible provider,
                  filesystem workspace, process executor, OS sandbox
                  launchers (Seatbelt/Landlock), SQLite session store,
                  workspace writer lease.
    contracts/    Strict Pydantic DTOs crossing every boundary: tool
                  args/results, model messages, events, checkpoints.
    evalkit/      Offline eval harness (ScriptedModel + real adapters).

The security spine (why the layering is shaped this way)
========================================================

Every model-proposed side effect passes through exactly one channel
(application/tool_pipeline.py):

    Registry -> strict schema -> workspace facts -> deterministic policy
    -> exact approval (digest-bound, single-use) -> TOCTOU re-check
    -> ExecutionTicket -> sandboxed executor -> evidence + journal

and a run can only *succeed* if the Evidence Gate (domain/evidence.py)
saw a diff plus a green registered check recorded after the last write.
Model text is never evidence. Docs: docs/SECURITY.md, docs/adr/.

Business flows and their entry points
=====================================

Interactive session   interfaces/cli.py `tui` (the default command)
                      -> bootstrap.build_services -> interfaces/tui/app.py
                      -> RunService.start_run / continue_run; approvals
                      arrive as modal cards; typing while a run is active
                      queues steering for the next turn boundary.
Headless run          interfaces/cli.py `run` (read-only by default;
                      --write + --approval-policy for unattended fixes,
                      --jsonl / --events for machine consumption).
Sessions / forensics  `sessions list|show`, `replay`, `export`,
                      `debug-context` - pure projections of the journal.
Crash recovery        `resume` -> application/recovery_service.py
                      classifies interrupted effects by digest; anything
                      unprovable blocks until `reconcile`. User-level undo
                      is `rewind` (fail-closed compensation).
Recipe discovery      `discover [--accept]` -> domain/discovery.py
                      proposes verify commands from the repo's own files;
                      `init` bundles it with an environment summary for
                      first contact with a repository.
Store maintenance     `gc` -> application/maintenance.py prunes old runs
                      and unreferenced artifacts (dry run by default).
Offline eval          `eval --offline` -> evalkit/runner.py; live suites
                      live in evals/ (see docs/EVAL_LIVE.md).

Suggested reading order for a first pass
========================================

    1. domain/policy.py + domain/approval.py   (the permission model)
    2. application/tool_pipeline.py            (the execution channel)
    3. application/run_service.py              (the agent loop)
    4. application/context_builder.py          (what the model sees)
    5. domain/evidence.py                      (what "success" means)
    6. adapters/workspace_fs.py                (how writes really land)
    7. interfaces/tui/app.py + presenter.py    (how it reaches the user)
"""

__version__ = "0.1.0"
