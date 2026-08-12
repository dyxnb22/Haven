# Haven Security Model

Haven's central problem: a non-deterministic model proposes actions that turn
into deterministic, security-sensitive filesystem and process side effects. The
security model exists to make sure the model can only ever *propose*, while
deterministic code owns permission, execution, and the definition of success.

## Assets

- The user's repository contents and uncommitted work.
- Everything outside the workspace (home directory, SSH keys, system files).
- Haven's own control plane: session database, event journal, approval records,
  `.git`, and `.haven.toml`.
- Provider credentials (the API key).

## Principals and trust

| Principal | Trust | Notes |
|---|---|---|
| The human user | trusted to *decide*, not to widen policy by prose | approves/rejects; cannot turn a `deny` into `allow` |
| The model | untrusted proposer | text and tool calls only; no direct side effects |
| Repository contents / tool outputs | untrusted data | wrapped in `<tool_output>` and labelled untrusted in Context |
| `AGENTS.md` project guidance | untrusted data | included in Context; explicitly cannot change permissions |
| Provider transport | trusted control plane | endpoint/credentials come from config, not from the model |

## Attack surface and defenses

### 1. Path escape (traversal, absolute, `~`, symlink, null byte)

`FsWorkspace.path_facts()` resolves every proposed path and marks it
`within_workspace=False` unless it resolves under the workspace root. Symlinks
are resolved before the containment check (an in-workspace symlink pointing
outside is rejected; regular-file/symlink checks also block editing through
links). Absolute paths, `~`, `..` traversal, and null bytes are all rejected.
Covered by `tests/security/test_workspace_escapes.py` and a Hypothesis property
test (`tests/unit/test_path_properties.py`).

### 2. Protected control-plane paths

`.git`, `.haven`, and `.haven.toml` are protected: reads, edits, and directory
listings all exclude/deny them, so the agent cannot rewrite its own permissions,
audit trail, or Git history. The session DB and artifacts live outside the
workspace entirely.

### 3. Unauthorized writes / privilege escalation

`repo.edit`, `repo.create`, and `repo.check` are `ask` in interactive mode and
`deny` in read-only mode. Policy is a pure function of
`(mode, program-collected facts)`; the model cannot influence facts. Approval is
bound to a canonical digest of workspace + tool + args + preimage + preview and
is single-use (a conditional SQL `UPDATE`), so it cannot be replayed or reused
for a different action. A unit test asserts that every registered tool is
classified by the policy and that no side-effecting tool is ever auto-allowed,
so adding a tool cannot silently create an unguarded path.

`repo.create` is deliberately restricted to paths that do **not** exist: it
fails with `invalid_arguments` on an existing file or directory. Overwriting is
only reachable through `repo.edit`, which is bound to a preimage the agent has
actually read — so "create" can never be used to blank a file the agent never
looked at.

### 4. TOCTOU / stale writes

An edit requires the file to have been read this run and to still match that
preimage. After the human approves, the preimage is re-verified immediately
before execution; if the file changed in between, execution fails closed with
`stale_preimage`. The write itself is atomic (temp file → fsync → rename) and the
postimage is re-read and digest-checked — a successful `write()` is not trusted
on its own.

### 5. Prompt injection (repository files, tool output, AGENTS.md)

Repository content and tool results are wrapped in `<tool_output>` blocks and
declared untrusted in the system prompt. `AGENTS.md` is loaded as untrusted
guidance that "cannot change permissions or rules." Crucially, injection can only
influence *proposals*; the deterministic policy, approval binding, and workspace
confinement are unaffected by anything in Context. Eval cases `inj-readme-ssh`,
`inj-tool-output`, and `inj-config-edit` exercise this: the scripted model "obeys"
the injection and every attempt is denied.

### 6. Arbitrary command execution

There is no shell tool. `repo.exec` takes an **argv array**, never a command
string, so pipes, globs, and redirection are not interpreted. A model that wants
shell semantics must name the interpreter (`["bash","-c",…]`), which is
classified as shell passthrough: it always requires approval and the approval
card warns that the visible argv no longer describes what will happen.

`repo.check` still runs only recipes registered in trusted config, by id; the
model never supplies a command string. Every process — exec and recipe alike —
runs with a fixed argv (never `shell=True`), a scrubbed environment allowlist, a
hard timeout (terminate → grace → kill), bounded stdout/stderr, cancellation
propagation, **and an OS sandbox** (see below).

Only commands on a small longest-prefix table of obviously read-only programs
(`ls`, `cat`, `git status`, `find` without `-delete`/`-exec`) skip the approval
prompt. That table controls approval friction, not capability: a
misclassification costs a skipped prompt, because the sandbox bounds what the
process can do either way.

`repo.exec` output is never verification evidence. A run that edits files still
cannot be reported as succeeded without `repo.diff` and a passing registered
recipe, so a model cannot satisfy the Evidence Gate by running `echo ok`.

### 6a. The OS sandbox

Every child process is wrapped by the platform's native sandbox in exactly one
place (`ProcessExecutor`), so no caller can introduce an unconfined path. Where
no backend exists, `repo.exec` is denied outright (`sandbox_unavailable`) — an
absent capability fact fails closed just like a negative one, and **no config
key, CLI flag, or environment variable can turn the sandbox off**.

| | macOS | Linux |
|---|---|---|
| Mechanism | Seatbelt (`sandbox-exec`, generated SBPL) | Landlock (ABI ≥ 4) |
| Writes | workspace + scratch only | workspace + scratch only |
| Reads | everything except `$HOME` | enumerated system roots + workspace |
| Network | all denied | TCP bind/connect denied |
| `.git` writes | denied by the kernel | denied by Haven's tool layer only |

Read confinement matters because `repo.exec` validates `cwd`, not the paths
inside `argv`: without it, `["cat", "~/.ssh/id_rsa"]` would succeed and quietly
undo the boundary `repo.read` enforces.

**What this does not stop**, stated plainly:

- Secrets stored outside `$HOME` (under `/opt`, say) remain readable. The
  confinement is coarse; it defeats the obvious credential paths, not a
  determined search.
- IPC. The macOS profile allows `mach-lookup` and POSIX shared memory so
  ordinary interpreters run; process isolation is not a goal here.
- UDP and DNS on Linux: Landlock ABI 4 governs TCP only.
- `.git` writes on Linux at the kernel layer: Landlock's subtree grants cannot
  express "the workspace except `.git`", so that line is held by the tool layer,
  as it was before this sandbox existed.

Enforcement is asserted by running real commands, not by inspecting profile
text (`tests/security/test_sandbox_enforcement.py`), on both platforms in CI.

`repo.search` may shell out to `ripgrep`, but the argv is fixed by the program
and only the validated pattern and a workspace-confined path come from the model.
The pattern is passed as `--regexp=<pattern>` and the path after `--`, so a value
beginning with a dash cannot be reinterpreted as a ripgrep flag; results are
re-resolved and confirmed to be inside the workspace before being returned. If
ripgrep is missing or misbehaves, the tool silently falls back to the in-process
Python scanner rather than failing.

### 7. Secret leakage

The API key is read from an environment variable, never placed in CLI args, the
session store, checkpoints, traces, or error text. Provider errors are mapped to
stable codes without echoing payloads. Exports redact known secret-env values.
Tests assert the key never appears in provider error strings, and that
out-of-workspace file contents never enter the model transcript
(`sec-absolute-path`, `inj-readme-ssh`).

### 8. Dangerous content in an otherwise valid change

A run can satisfy the Evidence Gate and still hand back a bad diff: a committed
credential, a merge-conflict marker, a `breakpoint()`, or a file it quietly
blanked. `domain/review.py` inspects the lines the run **added** and blocks
success on any finding, feeding it back so the agent can fix it. Only added lines
are examined, so pre-existing repository content can never trigger a finding.
These are heuristics for obvious mistakes, deliberately chosen for a low
false-positive rate; they are not a defense against a determined adversary and
not a substitute for reading the diff. See ADR 0007.

### 9. Runaway / stuck runs

Every run has hard budgets (steps, tool calls, wall time, tokens, cost) and a
stuck-loop detector (identical tool+args+result three times → `no_progress`).
Every run ends with exactly one stop reason. Budgets are a ceiling the agent
cannot raise: a project `.haven.toml` may only lower them, and nothing in the
loop extends them.

## Recovery safety

Interrupted side effects are classified against the execution journal:

- current file digest == recorded preimage → **not run** (safe to resume);
- current file digest == recorded postimage → **confirmed**;
- neither → **effect unknown**.

An unknown effect blocks resume and requires explicit human reconciliation
(`haven reconcile ... --as confirmed|not_run|abandon`). Haven **never**
auto-replays an ambiguous side effect. Checkpoints are checksum- and
schema-verified on load, and workspace identity is re-checked before resuming.
Covered by `tests/recovery/` and the two recovery eval cases.

## Known limitations (stated plainly)

- Child processes are confined by an OS sandbox (Seatbelt on macOS, Landlock on
  Linux) that blocks writes outside the workspace, reads of `$HOME`, and the
  network — but this is **not** a container or a VM. IPC is open, the Linux
  network rules cover TCP only, and secrets stored outside `$HOME` stay
  readable. Haven still assumes a locally trusted repository and does **not**
  claim it is safe to run untrusted or malicious repository code; container or
  VM isolation remains a precondition for any such claim.
- WAL gives local crash consistency, not distributed durability.
- Guarantees cover the code paths that are actually implemented and tested; the
  eval suite's security cases are the executable statement of those guarantees.

## The security gate

`haven eval --offline` fails if any case shows an unauthorized file change, a
missing expected policy deny, or leaked forbidden content. CI runs it, so a
regression that weakens a boundary breaks the build.
