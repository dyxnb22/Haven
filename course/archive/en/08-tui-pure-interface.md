# Module 08 — The TUI as a pure interface

**English** | [中文](../../08-tui-pure-interface.md)

> Files: `src/haven/interfaces/tui/presenter.py`,
> `src/haven/interfaces/tui/app.py`, `src/haven/interfaces/cli.py`,
> `src/haven/application/emitter.py`
> Tests: `tests/tui/test_presenter.py`, `tests/tui/test_tui_journey.py`,
> `tests/tui/test_tui_robustness.py`, `tests/golden/`
> ADR: [0001 — language and scope](../../../docs/adr/0001-language-and-scope.md)

## Learning objectives

- Keep all business logic out of the UI by making the presenter a **pure
  reducer** over the application event stream.
- Treat model and repository text as untrusted input to the *renderer*, not just
  to the model.
- Test a terminal UI offline, deterministically, including hostile input.

## Interface-only, enforced

The rule (ADR 0001, and an import-linter contract in CI): the TUI turns user
intent into service calls and renders view state from the shared event stream.
It contains **no** policy, **no** executor, **no** provider. If the TUI could
decide permissions, you would have two security models to keep in sync; there
must be one, and it is not in the UI.

## The presenter is a pure reducer

Read `presenter.py`. Its core is `reduce(state, event) -> state`: a pure function
from the previous `PresenterState` and one `ApplicationEvent` to the next state.
No I/O, no widgets, no time. `app.py` is the thin Textual shell that feeds events
in and renders whatever state comes out.

Why this shape:

- **Testable without a terminal.** `test_presenter.py` calls `reduce` directly
  with hand-built events — no Textual, no async, no screen.
- **Replay for free.** The same reducer consumes live events and replayed
  journal events, so `haven replay` reconstructs the screen. This is the
  consumer side of Module 07's projection.
- **One event stream, three consumers.** The headless CLI (`cli.py`), the TUI,
  and replay all subscribe to the same `ApplicationEvent`s produced by
  `emitter.py`. The golden trace test (`tests/golden/`) asserts TUI and headless
  produce *identical* traces — impossible unless the UI is genuinely a passive
  consumer.

## Untrusted text reaches the screen, too

Repository content and model output are rendered, so they are untrusted input to
the *renderer*, exactly as they are to the model. `presenter.py` strips ANSI and
control characters and bounds length before anything is shown.
`test_tui_robustness.py` throws an "ANSI bomb," Unicode/emoji, a 100k-line diff,
a 20×6 terminal, and key-spam at the app and asserts it neither crashes nor lets
an escape sequence through. If you build a TUI agent, write these tests: a
malicious repo should not be able to repaint the user's terminal or forge a
prompt.

## Backpressure and cancellation

`app.py` bridges the runtime to the UI through a bounded queue: transient
streaming deltas may be dropped under pressure, but authoritative events apply
backpressure and are never lost. Ctrl-C cancels the run before it quits, and the
cancellation propagates to the model request and any subprocess — a cancelled run
still ends with a named `StopReason` and a persisted `run.finished`.

## Approval is a modal bound to one action

The approval dialog shows the exact diff and the digest that authorizes it
(Module 04). Approving there resolves the one pending action; it cannot approve
anything else. The Pilot journey test (`test_tui_journey.py`) drives the whole
thing offline: submit a task, approve the edit and the check, assert success and
that the file changed.

## Exercises

1. **Reduce by hand.** Using `test_presenter.py` as a model, feed a
   `RunCreated`, a `StepStarted`, a `ToolProposed`, and a `RunFinished` into
   `reduce` and assert the resulting `PresenterState`. No Textual needed.
2. **Hostile input.** Add a presenter test that feeds model text containing
   `\x1b[2J` (clear screen) and asserts it never reaches `PresenterState`.
3. **Prove the equivalence.** Read the golden trace test. Explain, in terms of
   this module, why TUI and headless *must* produce the same trace.
4. **(Pilot)** Extend `test_tui_journey.py` with a run that gets rejected at the
   approval modal and assert the file is untouched.

## Self-check

- What does "the presenter is a pure reducer" buy you, concretely, three ways?
- Why is repository text an untrusted input to the renderer and not only to the
  model?
- How can `haven replay` rebuild the screen without calling the model?

## Further reading

- ADR 0001; `docs/ARCHITECTURE.md` "runtime event flow."
- Commit `d90f873` (`feat(tui)`) and `032f70d` (`test(golden)`).
