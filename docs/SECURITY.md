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

The side-effecting tools — `repo.edit`, `repo.create`, `repo.delete`,
`repo.move`, `repo.apply_patch`, and `repo.check` — are `ask` in interactive
mode and `deny` in read-only mode. Headless runs (`haven run`) are read-only
by default; `--write` moves to interactive mode where an automated approval
policy (`reject` / `trusted-recipe` / `all`, ADR 0021) supplies the decision,
and `all` is reachable only with an explicit `--write` so unattended mutation
is never the default. Policy is a pure function of `(mode, program-collected facts)`;
the model cannot influence facts. Approval is bound to a canonical digest of
workspace + tool + args + preimage + preview and is single-use (a conditional
SQL `UPDATE`), so it cannot be replayed or reused for a different action. Every
tool that pins a file's content at approval — edit, delete, and the source of a
move — re-verifies that content on disk before executing, so a change between
approval and execution fails closed with `stale_preimage`.

One narrow, disclosed exception to per-invocation asking (ADR 0025): after a
human approves a `repo.check`, byte-identical re-runs of that exact check
(same digest, so same recipe, argv, workspace, and tool version) are covered
for the remainder of that run — the card says so at consent time, a
rejection arms nothing, the grant is memory-only (a resumed run asks again),
and every skipped ask still mints and consumes its own single-use approval
row so the journal stays one-to-one. Writes, `repo.exec`, and patches always
re-ask. A unit test asserts that every registered tool is
classified by the policy and that no side-effecting tool is ever auto-allowed,
so adding a tool cannot silently create an unguarded path.

`repo.create` is deliberately restricted to paths that do **not** exist: it
fails with `invalid_arguments` on an existing file or directory. Overwriting is
only reachable through `repo.edit`, which is bound to a preimage the agent has
actually read — so "create" can never be used to blank a file the agent never
looked at.

`repo.apply_patch` (ADR 0019) bundles several such operations into one
transaction: a single approval bound to an aggregate digest of every touched
file's preimage, the whole diff as the preview, all-or-nothing application
with a journaled rollback, and per-file journal entries so recovery can prove
each effect independently. A protected path anywhere in the patch denies the
entire patch; every per-file rule above (read-before-edit, create-only-new,
move-never-overwrites) applies inside the patch exactly as it does outside.

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

Child processes are wrapped by the platform's native sandbox in exactly one
place (`ProcessExecutor`), so no caller can introduce an unconfined path where a
backend exists. The two process tools are treated differently on purpose
(ADR 0013):

- `repo.exec` runs **model-proposed** argv, so the sandbox is the only thing
  between the model and the machine. It is **mandatory**: with no backend, exec
  is denied outright (`sandbox_unavailable`), an absent capability fact failing
  closed exactly like a negative one. **No config key, CLI flag, or environment
  variable can turn it off.** Its profile is **workspace-read-only** (ADR 0017):
  only the scratch directory is writable. Source changes must go through the
  audited `edit`/`create`/`delete`/`move` tools — a command that tries to write
  the workspace fails with a permission error on both platforms, which is also
  what closes the Landlock gap where `.git` could not be carved out of a
  writable workspace.
- `repo.check` runs a **user-authored recipe id** against a repository the user
  has already chosen to trust, so the sandbox is defense-in-depth and the
  workspace stays **writable** (tests write caches and artifacts). It is applied
  whenever a backend exists, but a platform without one still runs registered
  checks, under the same locally-trusted-repo assumption the whole tool holds.
  Files a check changes are attributed to the evidence ledger from before/after
  snapshots (ADR 0012), and a change to a protected path (`.git`, `.haven`,
  `.haven.toml`) — which Landlock cannot prevent for a writable workspace — is
  detected the same way, surfaced as an **error** in the event stream, and
  **fails the check call** with `protected_path_tampered`: no check evidence is
  recorded, so a tampering check can never satisfy the Evidence Gate (ADR 0018).

The practical consequence: on a platform with no supported backend (Windows, or
a Linux kernel below Landlock ABI 4), `repo.exec` is unavailable and only
registered `repo.check` recipes run processes.

| | macOS | Linux |
|---|---|---|
| Mechanism | Seatbelt (`sandbox-exec`, generated SBPL) | Landlock (ABI ≥ 4) |
| Writes (`repo.exec`) | scratch only | scratch only |
| Writes (`repo.check`) | workspace + scratch | workspace + scratch |
| Reads | everything except `$HOME` | enumerated system roots + workspace |
| Network | all denied | TCP bind/connect denied |
| `.git` writes during a check | denied by the kernel | allowed by the kernel; detected by snapshot, fails the call (`protected_path_tampered`, ADR 0018) |

Read confinement matters because `repo.exec` validates `cwd`, not the paths
inside `argv`: without it, `["cat", "~/.ssh/id_rsa"]` would succeed and quietly
undo the boundary `repo.read` enforces.

Because that confinement is coarse, approval friction is calibrated to the
**operands**, not only the program (ADR 0026). A command from the read-only
table is auto-allowed while every operand stays inside the workspace; an
operand that is absolute, `~`-rooted, `..`-traversing, or hidden behind
`--flag=/path` re-classifies the call as ordinary exec and it goes to
approval. Without that rule an auto-allowed `cat` of an absolute path would
read a file the human never approved and return it into the transcript — and
therefore to the model provider. On Linux it also closes the specific hole
where `/proc/<parent-pid>/environ` reaches the **parent** process's
environment, around the child's `ENV_ALLOWLIST` scrub.

**What this does not stop**, stated plainly:

- Secrets stored outside `$HOME` (under `/opt`, say) remain readable *once the
  human approves the call*. The confinement is coarse; what the operand rule
  above adds is that such a read is never silent.
- IPC. The macOS profile allows `mach-lookup` and POSIX shared memory so
  ordinary interpreters run; process isolation is not a goal here.
- UDP and DNS on Linux: Landlock ABI 4 governs TCP only.
- A trusted `repo.check` recipe on Linux writing `.git` at the kernel layer:
  Landlock's subtree grants cannot express "the workspace except `.git`", so
  the write itself is not *prevented*. It is detected by the before/after
  snapshot, and the check call fails with `protected_path_tampered` recording
  no evidence (ADR 0018) — so the tamper cannot be laundered into a passing
  verification, though the on-disk write already happened. The
  locally-trusted-repo assumption is what holds beyond that.

Enforcement is asserted by running real commands, not by inspecting profile
text (`tests/security/test_sandbox_enforcement.py`), on both platforms in CI.

`repo.search` may shell out to `ripgrep`, but the argv is fixed by the program
and only the validated pattern and a workspace-confined path come from the model.
The pattern is passed as `--regexp=<pattern>` and the path after `--`, so a value
beginning with a dash cannot be reinterpreted as a ripgrep flag; results are
re-resolved and confirmed to be inside the workspace before being returned. If
ripgrep is missing or misbehaves, the tool silently falls back to the in-process
Python scanner rather than failing.

That fallback carries a wall-clock deadline, because the model's pattern is
validated only for syntax and Python's backtracking engine has no timeout of
its own: a pattern like `(a+)+b` costs exponential time per subject. The
deadline bounds the walk — many files, many lines — and reports a truncated
result. It cannot interrupt a single `re.search` already running: the regex
engine holds the GIL (measured: a 0.68s match let the event loop run zero
times), which is also why moving it to a thread does not help and was not
done. Bounding one pathological subject would need a killable subprocess;
ripgrep, the default backend, uses a linear engine and is unaffected.

### 7. Secret leakage

The API key is read from an environment variable, never placed in CLI args, the
session store, checkpoints, traces, or error text. Provider errors are mapped to
stable codes without echoing payloads. Tests assert the key never appears in
provider error strings, and that out-of-workspace file contents never enter the
model transcript (`sec-absolute-path`, `inj-readme-ssh`); reads that would leave
the workspace are additionally forced through approval (ADR 0026).

Exports redact in two passes, because they fail differently. The first masks the
*values* of environment variables whose names end in `_API_KEY`/`_KEY`/`_TOKEN`/
`_SECRET`/`_PASSWORD` — exact, no false positives, but blind to any secret this
process never held. The second masks well-known credential *shapes* (`sk-`,
`sk-ant-`, `AKIA…`, `ghp_`/`github_pat_`, `xox…`, `AIza…`, `SG.`, PEM blocks), so
a key pasted into a goal or read out of a file by a tool is caught too. It is
deliberately shape-based rather than an entropy heuristic: redacting every
high-entropy string would eat digests and diffs, and an export nobody trusts
gets read past. Neither pass is complete, and redaction of an artifact is not a
reason to put secrets in front of the agent.

### 7a. Untrusted text reaching a rendering surface

Everything the model writes, and everything a tool read out of the repository,
eventually gets displayed. Both are untrusted, so both are neutralized at the
point of display rather than trusted to be plain:

- **Control/ANSI sequences** are stripped in the TUI (`presenter.sanitize`) and
  in the headless `ConsoleSink`. Without this, model output can clear the
  screen, recolour, or forge a plausible prompt line in a terminal or a CI log.
- **Rich console markup** is escaped by `sanitize`. The chat, diff, evidence,
  and trace panels are `Static` widgets with markup enabled, so unescaped
  `[red]`, `[/]`, or `[link=…]` in model output would be *rendered* — enough to
  restyle or hide transcript text and fake a "succeeded" line. Escaped, the
  tags stay visible as the literal characters they are. (The timeline
  `RichLog` already runs with markup disabled.)
- Every rendered field is length-bounded, so an oversized payload cannot push
  the rest of the screen out of view.

This is display integrity, not a sandbox: it stops the transcript from lying
about itself. What the model may *do* is bounded by the policy and the OS
sandbox, not by anything here.

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

User-level undo (`haven rewind RUN_ID`, ADR 0020) is the human-initiated
counterpart to crash recovery: it restores a finished run's files to their
pre-run content, but only where the file on disk still matches what that run
left behind — anything changed since blocks rather than being overwritten, so
rewind is fail-closed compensation, never a blind replay. A single-writer
workspace lease makes concurrent Haven processes on one workspace explicit
(the second runs read-only), closing the cross-process approve-then-execute
window.

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
