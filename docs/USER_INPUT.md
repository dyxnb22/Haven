# User input

`user.ask` lets the model request missing requirements or user choices. The run
pauses in `waiting_user` with reason `needs_input`. Waiting does not call the
model, consume steps, or charge human waiting time against the wall-clock budget.
The pending request is checkpointed before the interface is notified.

## Questions and answers

The tool accepts one to three questions. Each has a stable `id`, a `question`,
optional `options` (up to six), `required` (default true), and `allow_custom`
(default true). Empty options mean a free-text answer. Question IDs and options
must be unique. Each answer is limited to 2000 characters.

```json
{"questions":[{"id":"scope","question":"Which component should I inspect?",
"options":["Parser","CLI"],"allow_custom":false}]}
```

Answers are keyed by question ID and bound to the saved `request_id`. Unknown IDs,
missing required answers, unsupported choices, and stale requests are rejected
without consuming the question. Answers supply task data; they never change
permission or approval modes. Planning remains read-only after an answer.

## TUI

The question form supports choices and custom text. Validation errors keep it
open so the user can revise the answers. Continue resumes the same run; Cancel
task or Esc cancels without another model call. `/answer` reopens a pending form.
After restarting, `haven resume RUN_ID --workspace PATH` restores the question.

## CLI

A headless run exits with code `8`. With `--json`, it returns the usual result
fields plus `input_request`, including its request ID and questions.

```bash
haven answer RUN_ID --request REQUEST_ID --answers '{"scope":"Parser"}' --workspace . --json
haven answer RUN_ID --request REQUEST_ID --cancel --workspace .
```

To continue an originally writable task, also specify `--write` and an appropriate
`--approval-policy` (`reject`, `trusted-recipe`, or `all`). The saved permission
limit still applies; answering cannot elevate a read-only or planning run. A normal
`haven continue` cannot bypass an unanswered question.

The original budget, transcript, plan, and verification evidence are retained.
The deferred tool reply is filled with the validated answer before another model
request. Other tools proposed in the same batch as `user.ask` are not executed;
the model must propose them again after receiving the answer.

An isolated worktree is retained while input is pending. `haven answer` reopens it
only if Git still registers it under the same repository's managed worktree
directory. Successful execution follows the normal verification and reconcile
flow. Background processes and external tool servers are closed at the pause;
external tool servers are rediscovered and checked before execution resumes.

## Runtime API

```python
from haven.contracts.user_input import UserInputResponse

outcome = await runtime.start_run("Inspect a component")
if outcome.pending_input:
    # Collect the user's answer through your interface.
    outcome = await runtime.answer_run(
        outcome.run_id,
        UserInputResponse(
            request_id=outcome.pending_input.request_id,
            answers={"scope": "Parser"},
        ),
    )
```

Resuming without an answer returns the same pending question and does not run the
model. `runtime.cancel(run_id)` can cancel a waiting run. After a crash during
answer delivery, the checkpoint already contains the answer before model execution;
normal recovery resumes from that point.
