# Module 03 — Tools and the single execution channel

**English** | [中文](../../03-tools-and-the-execution-channel.md)

> Files: `src/haven/application/tool_pipeline.py`,
> `src/haven/application/registry.py`, `src/haven/contracts/tools.py`,
> `src/haven/domain/ticket.py`, `src/haven/domain/policy.py`
> Tests: `tests/integration/test_agent_journeys.py`,
> `tests/integration/test_tool_error_containment.py`
> ADR: [0002 — tool execution boundary](../../../docs/adr/0002-tool-execution-boundary.md)

## Learning objectives

- Build one pipeline that every model-proposed action must pass through.
- Understand each gate and what it rejects.
- See why the executor must accept a program-minted **ticket**, never raw model
  JSON.
- Make tool failures *structured results*, never exceptions that abort the run.

## The channel

There is exactly one path from a model proposal to a side effect:

```
ModelResult
  → Tool Registry        (tool exists, version pinned)
  → Schema Validation    (strict Pydantic args → stable error code, not a traceback)
  → Workspace Facts      (canonical path, preimage digest, escape/protected checks)
  → Deterministic Policy (allow / ask / deny — a pure function)
  → Exact Approval       (when policy says ask; digest-bound, single-use)
  → ExecutionTicket      (raw model JSON stops here)
  → Executor             (atomic workspace op, or fixed argv through one sandbox wrapper;
                          mandatory for repo.exec, used for checks when available)
  → ToolResult + Evidence + Trace
  → next turn's Context
```

Read `ToolPipeline.execute` in `tool_pipeline.py` top to bottom. Each stage is a
named step you can point at. The exercise for this module is essentially "explain
every line of that method," because that method *is* the security model in
motion.

## Why a ticket

The executor (`adapters/workspace_fs.py`, `adapters/process_executor.py`) never
sees the model's JSON. Instead the pipeline mints an `ExecutionTicket`
(`domain/ticket.py`) that binds the tool name/version, the canonicalized
arguments, the workspace, and the preimage digest into one value. The executor
consumes only tickets.

Why bother? Because it makes "the model asked for X but we did Y" impossible by
construction. There is no code path where an executor reads a model field. If you
ever find yourself passing `tool_call.arguments` into something that touches
disk, you have re-created the vulnerability this design removes.

## The registry pins the vocabulary

`registry.py` validates a raw arguments string against the tool's strict Pydantic
model and returns either typed args or a `ValidationFailure` with a stable code.
Two subtleties worth internalizing:

- Validation happens on the JSON *text* via `model_validate_json`, not on a
  pre-parsed dict. In strict mode, JSON mode accepts a JSON array for a tuple
  field where Python mode would reject it. Providers hand you JSON text; validate
  the thing you actually received. (This was a real bug; see the `task.plan`
  story in Module 05.)
- The tool set is compiled in. A test asserts `set(ARGS_MODELS) == KNOWN_TOOLS`
  and that no side-effecting tool is ever auto-allowed, so adding a tool without
  classifying it fails the build rather than silently creating an unguarded path.

## Errors are results, not exceptions

A tool that fails returns a `ToolResult` with a stable `error_code`
(`unknown_tool`, `invalid_arguments`, `denied`, `stale_preimage`, `timeout`, …).
It is fed back to the model so it can recover. This is not a nicety — it is an
invariant:

> A tool call never raises into the agent loop.

`tests/integration/test_tool_error_containment.py` exists because a live run
violated this: searching a nonexistent path made `ripgrep` exit 2, which was
raised as an exception, escaped the channel, and aborted an entire eval suite.
The fix restored the invariant at three layers (validate the path, degrade a
backend hiccup, wrap execution errors as results). When you build your own
channel, write this test *first*; it is the one that catches the scariest class
of bug — the one that takes down the whole run.

## Exercises

1. **Narrate the channel.** Take the `repo.edit` journey from
   `test_agent_journeys.py` and annotate each event (`tool.proposed`,
   `policy.decided`, `approval.requested`, `execution.started`, `tool.completed`)
   with the pipeline stage that emitted it.
2. **Try to smuggle.** Attempt (on paper, then in a test) to make the executor
   act on an argument the model supplied but the ticket did not bind. Explain why
   you cannot.
3. **Add a read-only tool.** Sketch a `repo.stat` tool: its args model, its
   registry entry, its policy classification, and the one test that would fail if
   you forgot to classify it.
4. **Reproduce the containment bug.** Temporarily make `repo.search` raise on a
   missing path (don't commit it) and watch a run abort; then see how the current
   code turns it into a `not_found` result.

## Self-check

- What exactly does an `ExecutionTicket` bind, and why each field?
- Why validate the JSON text rather than a parsed dict?
- Give the invariant about tool failures in one sentence, and name the test that
  guards it.

## Further reading

- ADR 0002 for the boundary; `docs/SECURITY.md` §"single execution channel."
- Commit `958d98a` (`feat(application)`) introduces the channel and loop;
  `31fde25` and `95c0e78` are the containment fixes.
