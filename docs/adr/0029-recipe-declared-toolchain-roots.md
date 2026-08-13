# ADR 0029: a check recipe may declare the toolchain roots it needs to read

Date: 2026-08-14
Status: Accepted (extends ADR 0009 and ADR 0013)

## Context

Haven's sandbox hides `$HOME` (`default_private_roots()`), because that is
where credentials live. It then re-opens two things: the workspace, and the
running interpreter's prefixes. The second carve-out is in
`src/haven/ports/sandbox.py:57`, and its docstring states the reason without
hedging — the prefixes are readable *"so a virtualenv under `$HOME` can still
be executed by a check recipe."*

That single sentence has already conceded the general principle. A check recipe
may read a toolchain's files under `$HOME`, because otherwise the check cannot
run at all and the Evidence Gate (ADR 0003) has nothing to gate on. What the
code did not concede is generality: it hardcoded the one toolchain Haven
happened to be written in.

Every other toolchain keeps its dependency cache in the same place and is
therefore unusable as a check:

| Toolchain | Cache | Result under the current profile |
|---|---|---|
| Python (venv) | `sys.prefix` | works — hardcoded exception |
| Maven | `~/.m2` | offline build fails, no dependencies |
| Gradle | `~/.gradle` | same |
| Cargo | `~/.cargo` | same |
| Go | `~/go/pkg/mod` | same |

This surfaced while extending the live eval to a real 12-module Java
repository. `mvn -o test` is the only credible oracle there — the thing that
decides whether an edit is actually correct — and it cannot start with `~/.m2`
hidden. The choice is either to run Java checks unconfined, which is worse
along every axis, or to let the recipe say what it needs.

The trust argument is the one ADR 0013 already makes for recipes: a recipe's
argv comes from user-authored `.haven.toml`, never from the model. The model
can only pick an id from a registered list. Declaring `readable_roots` is
strictly less authority than declaring `argv`, which the user already has.

## Decision

`RecipeSpec` gains `readable_roots: tuple[str, ...] = ()`, parsed from
`.haven.toml` exactly as `allow_network` already is (`config.py:148`).
`ProcessExecutor.run_recipe` merges them into the recipe's `SandboxSpec`:

```toml
[recipes.mvn]
argv = ["mvn", "-o", "-pl", "big-market-types", "test"]
readable_roots = ["~/.m2"]
timeout_seconds = 300
```

Four constraints make this a narrowing of the existing carve-out rather than a
widening of the sandbox:

- **Read-only.** A declared root is added to `extra_readable_roots` and never
  to the writable set. `mvn -o` reads its cache; it does not get to rewrite it.
  A recipe that needs to *write* to a cache is out of scope for this ADR.
- **`repo.exec` is unaffected.** Only `run_recipe` consults the field. Model-
  proposed exec keeps `default_readable_roots()` unchanged, because its argv
  *is* model-authored — the exact distinction ADR 0026 had to enforce the hard
  way.
- **Resolved at config load, never at run time.** `Path(r).expanduser()
  .resolve()` happens when the `SandboxSpec` is built from a `RecipeSpec` that
  was already fixed on disk. There is no path through which a model-supplied
  string reaches this field.
- **Additive to the defaults, not a replacement.** The interpreter prefixes
  stay granted; a declaration adds one directory to an existing list.

## What this does and does not change

Changes: a `repo.check` recipe can read the directories its config names.

Does not change: the writable set, network policy, `$HOME` privacy for anything
undeclared, the approval path, or any capability of `repo.exec`. A run with no
`readable_roots` in its config produces a byte-identical sandbox profile to the
one before this ADR — which `tests/integration/test_check_sandbox.py` pins.

## Gate: metrics

- `tests/security/test_sandbox_enforcement.py::test_a_declared_root_is_readable_and_its_sibling_is_not`
  is the load-bearing one: it grants one directory under `$HOME`, then probes a
  sibling directory that was not granted. The grant must open and the sibling
  must stay shut, so the two assertions differ in exactly one variable. A
  widened boundary is only acceptable alongside a test that the unwidened case
  still fails closed.
- `tests/unit/test_config.py` pins the parse.
- The number this ADR is accountable for, stated before it was measured:
  Maven modules runnable as a confined Haven check, currently **0**. If the
  mechanism ships and that number stays 0, the premise was wrong and the
  rollback below applies. The result is recorded in `docs/EVAL_LIVE.md` under
  the 2026-08-14 session rather than here, so this document does not have to be
  edited to stay true.

## Gate: risks

- **A user can declare `/` and undo read confinement for that recipe.** Accepted.
  It is the same class of authority as `allow_network` and as the recipe argv
  itself; a recipe already runs arbitrary user-authored code with the workspace
  writable. This mechanism grants nothing the author could not already grant by
  writing a different `argv`.
- **A check's output reaches the transcript, so a wide grant is a read channel
  to the provider.** Stated plainly because this is precisely the composition
  ADR 0026 was written to close for `repo.exec`: read access plus an output path
  to the model is exfiltration, even with the network denied to the child.
  `stdout_tail`/`stderr_tail` of a check are appended to the model transcript.
  The mitigation here is not the sandbox, it is provenance — the user wrote the
  root and the argv, and neither is reachable by a prompt-injected model. A user
  who declares `readable_roots = ["~"]` has un-hidden their home directory to
  their own model provider, and should not.
- **The grant is silent at run time.** A declared root does not currently raise
  an approval card, on the same reasoning as `allow_network`: config is
  consented at authoring time. If recipes ever become shareable between users,
  that reasoning expires and this must become a prompt.

## Rollback

Delete the field from `RecipeSpec`, the `readable_roots` line in
`config.py`'s recipe parsing, and the merge in `process_executor.py`; the
recipe profile returns to `default_readable_roots()` alone. No persisted state
depends on it, and a `.haven.toml` carrying the key would then be rejected by
`StrictModel` — which is the loud failure that is wanted, not a silent
downgrade to an unconfined run.
