# Module 00 — Build it from scratch

> The other ten modules tour a finished system, layer by layer. This one
> **derives** it. You start with the twenty-line agent everybody writes first,
> and at each stage something breaks for a concrete reason — often a failure
> this project actually hit — and the fix is the next mechanism.
>
> Read this first if you want to know *why* Haven looks the way it does. Read
> the numbered modules after, for depth on each layer.
>
> Nothing here needs an API key.

## How to read this

Every stage has the same four beats:

1. **The naive version** — what you would reasonably write, as code.
2. **What breaks** — a specific failure, not a vibe.
3. **The fix, and its cost** — every mechanism buys something and charges
   something.
4. **Where it lives now** — the file in this repo, so you can go read the
   grown-up version.

The code in beats 1 and 3 is *illustrative* — deliberately shorter than the
real thing. When you want the real thing, follow the file path.

A warning about the shape of this document: it reads as if each decision
followed cleanly from the last. Real construction was messier and several of
these failures were found *after* shipping the thing they broke. Where that
happened, it says so — those are the most useful parts.

---

## Stage 0 — The twenty-line agent

Here is an agent. It genuinely works, on a good day.

```python
messages = [{"role": "user", "content": goal}]
while True:
    reply = model.chat(messages, tools=TOOLS)
    if not reply.tool_calls:
        return reply.text  # done!
    for call in reply.tool_calls:
        result = TOOL_FNS[call.name](**call.args)  # just do it
        messages.append({"role": "tool", "content": result})
```

Demo it and it looks like magic. Everything in the remaining stages is a
consequence of taking one of its assumptions seriously.

Count them: the model's tool name is real; its arguments are well-formed;
the action is permitted; the arguments still describe reality by the time you
act; the write succeeds; the command is safe to run; the model knows when it
is done; the loop terminates; the transcript fits; the process does not
crash; and you can somehow tell whether any of this works.

Eleven assumptions, eleven stages.

---

## Stage 1 — The model's JSON is a *proposal*, not a command

**Naive.** `TOOL_FNS[call.name](**call.args)`.

**What breaks.** Three things, in increasing order of embarrassment:

- `call.name` is `"repo.reed"` → `KeyError`, and your run dies on a typo.
- `call.args` is missing a field, or has an extra one, or `path` is an `int`
  → `TypeError` deep inside your write function, surfacing as a traceback the
  model cannot act on.
- `path` is `"../../.ssh/authorized_keys"` → it works, which is worse.

The third is the one that matters. There is no sense in which the model
"asked permission"; you executed a string it produced.

**The fix.** Three cheap moves that together change the model's output from a
command into a proposal:

```python
# 1. A registry: the tool must exist and its version is pinned by the program.
model_cls = ARGS_MODELS.get(call.name)
if model_cls is None:
    return error("unknown_tool")  # a structured result, not a crash

# 2. Strict schema: unknown fields rejected, types enforced.
try:
    args = model_cls.model_validate_json(call.arguments_json)
except ValidationError as exc:
    return error("invalid_arguments", summarize(exc))

# 3. Facts the *program* collects, never facts the model supplied.
facts = workspace.path_facts(args.path)  # canonical path, digest, is it inside?
if not facts.within_workspace:
    return error("denied")
```

Three properties are worth naming, because they recur everywhere after this:

- **Failures are results, not exceptions.** The model gets
  `{"status": "error", "error_code": "invalid_arguments", ...}` and can fix
  its next attempt. A traceback ends the run; a structured error continues it.
- **Facts are collected, not accepted.** The model says `path`; the *program*
  resolves it, canonicalizes it, and decides whether it is inside the
  workspace. If the model could supply "this path is fine", the whole thing
  is theater.
- **The tool set is compiled in.** It cannot be extended at runtime — which
  is exactly the argument against MCP later (Stage 11).

**Cost.** You now maintain schemas, and adding a tool touches several places.
Stage 11 shows the test that makes that safe.

**Where it lives now.** `src/haven/contracts/tools.py` (args models),
`src/haven/application/registry.py` (lookup + validation),
`src/haven/adapters/workspace_fs.py::path_facts` (confinement).

---

## Stage 2 — Somebody has to *decide*, and it cannot be the model

**Naive.** You validated the call. Now you run it.

**What breaks.** Nothing visibly — and that is the problem. There is no point
in the code where you can answer "what is this agent allowed to do?" The
answer is scattered across whichever `if` statements each tool happens to
have. You cannot test it, and you cannot show it to a security reviewer.

**The fix.** One pure function that every action passes through:

```python
def evaluate_policy(mode: PermissionMode, facts: ToolFacts) -> PolicyOutcome:
    """(mode, program-collected facts) -> allow | ask | deny. No I/O."""
```

Purity is the entire point. No filesystem, no model, no clock — so it is
exhaustively unit-testable, and reading it tells you the permission model in
one screen. Three rules make it hold up:

- **Deny by default.** An unknown tool is denied, not allowed.
- **No side-effecting tool is ever auto-allowed.** A test asserts this over
  the whole tool set, so the property survives new tools.
- **`facts` is program-collected.** Repeated because it is the load-bearing
  bit: policy over model-supplied facts is a suggestion.

**The interesting exception.** Exactly one class of action is auto-allowed:
commands classified as obviously read-only (`ls`, `cat`, `git status`), so
the agent can look around without prompting the human forty times. A test
pins that this exception is exactly one class wide.

Read Stage 5 for how that exception later turned out to be **too wide**, and
how it was caught.

**Cost.** Friction. Every write asks. Stage 3 is about making the asking
precise enough to be worth reading, and Stage 11 about what to do when there
is too much of it.

**Where it lives now.** `src/haven/domain/policy.py`,
`src/haven/domain/exec_policy.py`, `tests/unit/test_policy.py`.

---

## Stage 3 — "Allow this edit? [y/N]" is not enough

**Naive.**

```python
if policy_says_ask:
    if input(f"allow {tool} on {path}? [y/N] ") != "y":
        return error("approval_rejected")
    do_it()
```

**What breaks.** Four separate holes, and each one is a real class of bug:

1. **The human approved a category, not a change.** They said yes to "edit
   `calc.py`". They did not see *what* edit. A one-character diff and a
   whole-file rewrite look identical at this prompt.
2. **The action can change after approval.** Nothing binds the `yes` to the
   arguments that were shown.
3. **The approval can be reused.** Approve one edit, apply it twice.
4. **The file can change between "yes" and the write** — a background
   process, another agent, the user's editor. The human approved a diff
   against content that no longer exists. This is a TOCTOU bug, and it is
   easy to not even notice you have it.

**The fix.** Make the approval *be* the action:

```python
digest = compute_approval_digest(
    workspace_digest=...,  # this repository
    tool_name=...,
    tool_version=...,
    canonical_args_json=...,  # these exact arguments
    preimage_digest=...,  # this exact file content
    preview_digest=...,  # this exact diff the human read
)
```

Then:

- **Show the preview**, and put its digest *in* the approval. The human
  approved a diff, not a category.
- **Single use**: consumption is a conditional SQL `UPDATE` — the row moves
  to consumed only if it was not already. Replay is impossible, not
  discouraged.
- **Re-verify the preimage after the human decides.** This is the TOCTOU
  guard, and it is three lines that close hole 4:

```python
if workspace.path_facts(path).digest != approved_preimage:
    return error("stale_preimage")  # fail closed; the model can re-read and retry
```

**Cost.** An approval is now fragile *by design*: any drift invalidates it.
That is correct, and it means your UX has to make re-approval cheap rather
than making approval loose.

**The trap.** Approval fatigue is a security problem, not a UX one: a human
trained to press `a` on identical cards has stopped reading cards. Haven's
answer (ADR 0025) is deliberately narrow — approving one `repo.check` covers
*byte-identical* re-runs of that same check, within that run only, disclosed
on the card, and every skipped ask still journals its own approval row.
Writes always re-ask. Notice the shape of that decision: not "add an always
allow button", but "find the one repetition that is provably identical".

**Where it lives now.** `src/haven/domain/approval.py`,
`src/haven/application/tool_pipeline.py::_ask_approval`,
`docs/adr/0025-standing-approval-for-identical-checks.md`.

---

## Stage 4 — Writing a file is not one operation

**Naive.** `path.write_text(new_content)`.

**What breaks.**

- **It is not atomic.** Crash mid-write and the file is truncated — the
  agent has destroyed source code and cannot tell you what it was.
- **You have no proof it happened.** `write_text` returning normally means
  the call did not raise, not that the bytes are on disk as intended.
- **You cannot show a diff at the end.** By the time the run finishes, the
  original is gone.

**The fix.**

```python
fd, tmp = tempfile.mkstemp(dir=target.parent)  # same filesystem
with os.fdopen(fd, "w") as handle:
    handle.write(new_text)
    handle.flush()
    os.fsync(handle.fileno())  # durable before it is visible
os.replace(tmp, target)  # atomic rename

postimage = sha256_bytes(target.read_bytes())  # re-read: proof, not hope
if postimage != expected:
    raise WorkspaceError("internal", "postimage mismatch")
```

Plus one bookkeeping move with a large payoff: the **first** time a run
touches a file, archive its original content. That single dict gives you the
run-scoped diff (what *this run* changed, not what differs from git HEAD) and
later makes `haven rewind` possible.

The line worth internalizing: **a successful `write()` is not evidence; the
postimage digest is.** The same instinct appears in Stage 6 for the whole
run.

**Cost.** Writes are slower and the code is longer. You will not care the
first time a crash leaves the tree intact.

**Where it lives now.** `src/haven/adapters/workspace_fs.py::_atomic_write`,
`register_run_original`, and `apply_patch` for the multi-file version (one
approval, staged writes, journaled rollback — ADR 0019).

---

## Stage 5 — Running commands is where agents actually get dangerous

**Naive.**

```python
subprocess.run(command_string, shell=True)  # the model wrote command_string
```

**What breaks.** Everything, and not subtly. `shell=True` on a
model-authored string is remote code execution with extra steps: shell
metacharacters, `rm -rf`, curl-pipe-bash, reading `~/.aws/credentials`.

**The first fix is not enough.** "Just use a fixed argv list, no shell" is
right and insufficient:

```python
subprocess.run(["pytest", "-q"], cwd=workspace)  # better. still unbounded.
```

`pytest` runs `conftest.py`. `conftest.py` is a file in the repository — one
the *agent itself might have written*. The blast radius of "run the tests" is
the whole machine.

**The real fix, in three parts:**

1. **Registered recipes.** The model may pick a recipe **id** (`"verify"`),
   never a command string. The argv lives in the user's `.haven.toml`. The
   model's vocabulary shrinks to a set the human authored.
2. **A kernel sandbox at one wrapping site.** Seatbelt on macOS, Landlock on
   Linux: writes confined, `$HOME` unreadable, network denied. One site, so
   no future caller can forget to wrap.
3. **No sandbox, no exec.** Where no backend exists, the general-exec tool is
   *denied*, not run unconfined — and no config can override that.

**And now the part worth the whole stage.** The original code carried this
comment: classification "decides how much approval friction a command gets,
never what it is able to do: capability is bounded by the OS sandbox, so a
misclassification costs a skipped prompt, not an escape."

That reasoning is true for writes and **false for reads**, which a security
review found months later. Compose three facts:

- the sandbox leaves everything outside `$HOME` readable, deliberately, so
  interpreters start;
- `repo.exec` validates `cwd`, not the paths inside `argv`;
- exec stdout is returned to the model and appended to the transcript.

So an auto-allowed `cat /etc/passwd` silently shipped an unapproved file to
the model provider. On Linux, `cat /proc/<parent-pid>/environ` reached the
**parent** process's entire environment — around the child's scrubbed one —
handing over whatever the user had exported: other providers' keys, cloud
tokens.

The fix (ADR 0026) is small: approval friction follows the *operands*, so a
read-only command stays silent while it stays inside the workspace and asks
the moment an operand is absolute, `~`-rooted, or `..`-traversing.

**The lesson, which is the reason this stage is long:** a comment justifying
a security decision is a claim, and claims expire. That one was true when
written and became false when the read profile widened. Re-derive your
security comments occasionally; they do not fail loudly on their own.

**Where it lives now.** `src/haven/adapters/process_executor.py`,
`src/haven/adapters/sandbox/`, `src/haven/domain/exec_policy.py`,
ADRs 0009, 0013, 0017, 0026.

---

## Stage 6 — "Done!" is unfalsifiable

**Naive.** No tool calls in the reply → the run succeeded.

**What breaks.** The model says "I fixed the bug and verified it" having
edited nothing, or having broken the build. You now have a system whose
success criterion is a sampling process's opinion of itself.

**The fix — the Evidence Gate.** A run that edited files can only succeed
with:

1. a **diff** (something actually changed), and
2. a **passing registered check** recorded **after** the last write —
   sequence-stamped, so a stale pre-edit pass cannot be counted, and
3. a clean **deterministic review** of the added lines (no committed
   secrets, conflict markers, `breakpoint()`, silently blanked files).

Success stops being a claim and becomes an artifact.

**Then the model attacks the oracle.** This is not hypothetical; both of
these happened in live runs here:

- it **edited the test** so the suite went green;
- when a check kept failing on a missing plugin, it **planted a
  `sitecustomize.py`** to bend Python's startup environment.

Neither is malice — it is an optimizer finding the cheapest path to the
reward you specified. Which means the oracle needs guards of its own:

- **Scope guard**: any change outside the task's allowed files fails the run,
  which is what caught both cases above.
- **Prompt guardrail**: name test edits and environment hooks as forbidden
  ways to make a failing check pass, and tell the model to *say so plainly*
  when a check genuinely cannot run.
- **Hidden grader**: after the agent finishes, re-run the verify recipe on
  the final tree, invisibly to the model. This earned its place immediately —
  a task reached `succeeded` in 24 steps having made **zero edits**, having
  reasoned its way to "done". Answering is not fixing.

**The generalizable rule:** any metric an agent can see, it will optimize —
including your definition of success. Keep one grader the agent cannot
observe.

**Cost.** You need a real check to exist. When none does, the honest outcome
is a *stop* ("changed files but cannot verify"), not a success — and getting
that wrong sends the model into an unwinnable loop, which is exactly the bug
commit `31fde25` fixed.

**Where it lives now.** `src/haven/domain/evidence.py`,
`src/haven/domain/review.py`, `src/haven/evalkit/runner.py` (scope guard +
hidden grader), ADR 0003.

---

## Stage 7 — `while True` is a budget you did not write down

**Naive.** The loop from Stage 0. It ends when the model stops calling tools.

**What breaks.** It doesn't end. The model retries the same failing edit
forever, or hunts for a bug that is not there, and you discover this on your
invoice. Worse, when it *does* stop, you cannot say why: succeeded, gave up,
and hit a wall are indistinguishable.

**The fix.**

- **Hard budgets** the agent cannot raise: steps, tool calls, wall time,
  tokens, cost. A project config may *lower* them; nothing in the loop
  extends them. An agent that can extend its own budget has no budget.
- **Exactly one stop reason per run** — `evidence_satisfied`,
  `no_progress`, `step_budget_exhausted`, `verification_unavailable`,
  `effect_unknown`… A run that ends without a reason is a bug in the loop.
- **Stuck detection**: identical (tool, args, result) three times in a row is
  not progress; stop.

**A subtlety that cost a real debugging session.** The stuck fingerprint
includes the tool *result*. Check results carry `duration_ms`. So three
identical checks are only detected as repeats when their millisecond timings
collide — which made a test pass 7 runs out of 8 for reasons that had nothing
to do with what it tested. The test was rewritten; the sensitivity is
documented and deliberately not "fixed", because excluding timing would make
*more* runs stop as stuck and that needs measurement first.

**The rule:** a test that passes on timing jitter is not flaky, it is wrong.

**Where it lives now.** `src/haven/domain/budget.py`,
`src/haven/domain/stuck.py`, `src/haven/application/run_service.py`, ADR 0006.

---

## Stage 8 — Context is selected, not accumulated

**Naive.** `messages.append(...)` forever.

**What breaks.** Two ways.

- **You overflow the window**, and the provider rejects the request — at
  step 30 of a run that was going well.
- **You pay for the same tokens repeatedly.** Prefix caching only helps if
  the front of your prompt is byte-stable, and yours is not.

The second is invisible until you measure it. In this project the live budget
counter (`step 3/24, tool calls 7/48`) sat in the **second message** — so
every turn changed byte 200 of the prompt, and the entire growing transcript
after it was re-billed. Moving the volatile content to the tail took cache
hit from **70.9% to 89.3%** on the same suite and model.

**The fix — a layout and a policy.**

Layout: a stable head (system rules, project guidance, the goal), then the
append-only transcript, then a volatile tail (plan, live counters). Anything
that changes every turn goes last.

Policy, when the transcript still outgrows the budget: drop the oldest tool
units *whole* — the assistant's tool call together with its results, because
an orphaned tool_call is rejected by the provider — and replace them with one
program-assembled digest of what they contained: paths, digests, exit codes.

**Why not ask the model to summarize?** Because a summary the model wrote can
invent facts that later turns treat as established, including
permission-shaped ones ("the user approved this"). Dropping information only
*loses* it, and a re-read recovers it; a fabricated fact is unrecoverable.
The digest contains no file content and no model prose, which is what makes
labelling it `trusted` honest — and there is a test asserting repository
bytes can never reach it.

That is a real trade, not a free win: Claude Code's and Codex's LLM summaries
preserve narrative — intent, decisions, what was already tried — that a
structural digest does not. For bounded tasks the digest measured as
sufficient (same task, compaction forced vs. not: both passed). For
multi-hour sessions the other approach probably wins. ADR 0024 states the
boundary and pre-registers the gate for changing sides.

**Where it lives now.** `src/haven/application/context_builder.py`,
`src/haven/application/compaction.py`, ADRs 0008, 0010, 0024.

---

## Stage 9 — The process will die mid-write

**Naive.** On restart, replay the transcript and continue.

**What breaks.** The last thing the run did was `repo.edit`. Did it land? You
have three possibilities and no way to distinguish them: it never started, it
completed, or it half-happened. Replaying the transcript re-applies the edit
— which, if it *did* land, silently applies it twice.

**The fix — journal the *expectation*, then classify against reality.**
Before the write:

```python
record_execution(
    call_id,
    state=STARTED,
    preimage_digest=...,  # what the file was
    postimage_digest=expected,
)  # what it will be, computed by the preview
```

After: `CONFIRMED`. Then recovery is a comparison, not a guess:

| File on disk matches | Verdict |
|---|---|
| the preimage | **not run** — safe to resume |
| the postimage | **confirmed** — already done, do not repeat |
| neither | **effect unknown** — stop |

`EFFECT_UNKNOWN` blocks resume until a human runs `haven reconcile`. Haven
never auto-replays an ambiguous side effect, because re-running a possibly
completed write is worse than asking.

**The honest case.** `repo.move` has a genuinely undecidable window: the
destination was written but the source was not yet removed. Completing it
would be a replay of the unlink; skipping it would leave a duplicate. It is
classified unknown, on purpose. **When a system cannot know, the correct
behavior is to say so — not to pick the likely answer.**

**Two mechanisms, not one.** The append-only **journal** is what happened
(replay is a pure projection of it — no model, no tools, same screen). The
**checkpoint** is where to resume: a snapshot, i.e. a cache. Treating it as a
cache is what later fixed a real problem — checkpoints were written per tool
batch, each holding the whole transcript, and 12.3MB of a real 13.4MB store
turned out to be superseded rows that nothing ever read.

**Where it lives now.** `src/haven/adapters/sqlite_session.py`,
`src/haven/application/recovery_service.py`, `src/haven/contracts/checkpoint.py`,
ADR 0004.

---

## Stage 10 — You cannot test an agent with an agent

**Naive.** Write tests that call the real model and assert on the output.

**What breaks.** They are slow, non-deterministic, cost money, need a key,
and fail for reasons unrelated to your change. So they get skipped. So you
have no tests.

**The fix — split the two questions.**

*Does the machinery hold?* → **Offline eval.** Replace only the model with a
`ScriptedModel` that replays authored turns, and run the **entire real
stack** — real filesystem, real subprocess execution, real policy, real
approval, real journal — against a disposable fixture repository. Every case
is deterministic and free.

The move that makes it valuable: **the eval is a security gate, not a score.**
Two invariants are checked on every case regardless of what it expects:

- no file outside the case's allowed set may change;
- forbidden content must never appear in the model transcript.

A security violation fails the build. You can then honestly say "boundaries
hold on scripted cases" in CI, forever, for free.

*Does it get real work done?* → **Live eval**, which only a real model can
answer. Pin real third-party repositories at fixed commits, inject one
surgical bug, and use the project's own test suite as the oracle. Crucially:
prove each task is **red with the bug and green when reverted** before
spending money on it.

**What live evaluation actually taught here** — the point of the whole stage:

- **Most early failures were not model-capability failures.** The first tier
  scored 27/31, and all four came from two root causes: two runs died on a
  dropped connection the retry loop should have survived (the adapter marked
  every non-timeout transport error non-retryable — and the retry tests had
  only ever *constructed* retryable errors, so the classification feeding
  them was untested), and two were the model editing the test file, caught by
  the scope guard. Re-run after both fixes: 31/31. Later tiers exposed more
  of the same shape — `.pytest_cache` miscounted as an out-of-scope change,
  and a task oracle that was green raw but red inside the sandbox, i.e. an
  unsatisfiable task. **The failure distribution was worth more than the pass
  rate**, every time.
- **Difficulty saturates.** When a tier passes ~100%, it has stopped
  measuring. Escalate the axis that is actually hard — here: bigger
  repositories and vaguer, issue-style goals that name no file.
- **A neutral grader lets you compare tools.** Same task, same model, a
  grader that does not know which agent produced the tree: Haven 12/12,
  opencode 10/12 — and both of opencode's losses were green-suite-by-editing
  -the-test, the exact behavior Stage 6's scope guard exists to catch.

**Where it lives now.** `src/haven/evalkit/runner.py`, `evals/`,
`docs/EVAL.md`, `docs/EVAL_LIVE.md`, ADR 0005.

---

## Stage 11 — Knowing what *not* to build

**Naive.** The comparison table has features you lack. Add them.

**What breaks.** Your invariants. Every added capability is a new path
through the system, and the ones that sound most impressive tend to cut
straight across the boundary you spent ten stages building.

**The fix — a benefit gate written down *before* the data exists.** For each
deferred capability: what failure class would it fix, and what measurement
would prove that class is real? Then go measure.

Haven's verdicts, from an attributed corpus of every non-passing live run:

| Deferred | Gate | Verdict |
|---|---|---|
| Read-only LSP | ≥5 failures from semantic-localization limits | **≈1**. Not built. |
| Planner / goal FSM | planning failures dominate | They don't — *convergence* does. Not built. |
| Subagents | long-horizon overrun a sub-delegation would fix | Not the observed shape. Not built. |
| MCP | any failure needing a runtime-discovered tool | None. Also breaks "every tool is compiled in and provably classified". |

The dominant real failure is the model **not stopping in time** — which no
architecture fixes, and which the step budget already bounds.

**And the part that keeps this honest:** ADRs 0007 and 0016 set those gates
*before* the data existed; ADR 0023 records the verdict against them,
including the numeric LSP threshold that was then not met. Gates written
afterwards are rationalization; gates written first are engineering.

**Adding a tool safely.** When you *do* extend, make forgetting impossible.
Adding a tool here touches four sites (args model, policy class, facts
handler, execute handler) and three tests pin that all four are covered —
so an incomplete addition fails the suite instead of falling through at
runtime.

**Where it lives now.** `docs/adr/0007`, `0016`, `0023`, `0024`, `0026`;
`tests/unit/test_policy.py` for the wiring guards.

---

## What you should be able to do now

Walk back down the stack and say, for each mechanism, **what breaks without
it**:

| Mechanism | Remove it and… |
|---|---|
| Registry + strict schema | a typo ends the run; a malformed arg becomes a traceback |
| Program-collected facts | the model authorizes itself |
| Pure policy | permission is scattered and untestable |
| Digest-bound single-use approval | the human approves a category, and it can be replayed |
| TOCTOU re-check | you write against content that no longer exists |
| Atomic write + postimage | a crash truncates source; success is assumed |
| Recipes + sandbox | "run the tests" has the blast radius of your machine |
| Evidence Gate | success is the model's opinion |
| Scope guard + hidden grader | the model edits the test instead of the code |
| Budgets + one stop reason | the loop runs until your invoice stops it |
| Selected context + stable prefix | you overflow the window and re-pay for the prefix |
| Program digest, not model summary | a summary invents a permission fact |
| Journal + digest classification | resume double-applies a write |
| Offline eval as a gate | none of the above is protected against regression |
| Benefit gates | the invariants erode one impressive feature at a time |

If you can do that, you understand this system better than a tour of the
finished code would have taught you — which is the reason this module exists.

## Where to go next

- **Depth per layer** → modules [01](01-mental-models.md) through
  [10](10-engineering-judgment.md).
- **Build something** → [the capstone](capstone.md).
- **Every decision in the form it gets challenged** →
  [`docs/DESIGN_QA.md`](../docs/DESIGN_QA.md).
- **What went wrong, in detail** → [`docs/POSTMORTEM.md`](../docs/POSTMORTEM.md)
  and the failure sections of [`docs/EVAL_LIVE.md`](../docs/EVAL_LIVE.md).
