# A recipe supplies its own toolchain environment

Date: 2026-08-14

## Context

ADR 0029 gave a check recipe the ability to declare toolchain roots it may
read, so `mvn -o test` could reach `~/.m2` inside the sandbox. The first
confined run still failed, and not for a sandbox reason:

```
The JAVA_HOME environment variable is not defined correctly,
this environment variable is needed to run this program.
```

`ProcessExecutor.ENV_ALLOWLIST` is `PATH, HOME, LANG, LC_ALL, TERM, TMPDIR,
VIRTUAL_ENV`. The child's environment is rebuilt from that list precisely so a
subprocess cannot read whatever the user's shell happens to export — the
concern ADR 0026 made concrete when it found `cat /proc/<ppid>/environ` was a
silent exfiltration path. `VIRTUAL_ENV` is on the list because Python's
toolchain needs it, which is the same shape of exception `ports/sandbox.py`
already carried for the interpreter prefixes: the one toolchain Haven is
written in got its needs met, and no other did.

So a Java, Go, or Rust check hits a second obstacle after the readable root,
and the failure message points at the environment rather than at anything the
user configured.

## Decision

Do not widen `ENV_ALLOWLIST`. A recipe that needs a toolchain variable supplies
it through its own argv, which is user-authored config:

```toml
[recipes.verify]
argv = ["/bin/sh", "-c", "JAVA_HOME=/opt/homebrew/… exec mvn -o test"]
readable_roots = ["~/.m2"]
```

This grants no authority the recipe did not already have — the author could
have written any argv at all — and it keeps the allowlist a fixed, auditable
list rather than one that grows once per toolchain.

## Alternatives considered

- **Add `JAVA_HOME` to `ENV_ALLOWLIST`.** It is not a credential and the fix is
  one word. Rejected for now because it settles nothing: `GRADLE_HOME`,
  `CARGO_HOME`, `GOPATH`, `GOMODCACHE`, `MAVEN_OPTS` and `JAVA_TOOL_OPTIONS`
  all follow, the last two are arbitrary-argument injection into the JVM, and
  the list stops being reviewable. If this becomes common the right answer is
  the next bullet, not a longer list.
- **A per-recipe `env` table in `.haven.toml`.** The principled version:
  explicit, provenance-checked at config load, symmetric with
  `readable_roots`. Not built because one data point is not a design input, and
  the argv workaround costs the author one line. This is the thing to build if
  a second toolchain hits it.
- **Pass the parent environment through for recipes only.** Rejected outright.
  A check's stdout reaches the model transcript, so this is the exact
  read-plus-output-channel composition ADR 0026 exists to prevent — with the
  parent's full shell environment as the source.
- **Detect `JAVA_HOME` and inject it automatically.** Convenient and invisible,
  which is the problem: config provenance is a property this project enforces
  deliberately (`haven config explain`), and a value that appears from nowhere
  cannot be explained.

## Consequences

A non-Python toolchain costs its author one `sh -c` wrapper and one discovery
of a confusing error message. That is a papercut, and it is recorded here so
the second person to hit it finds the answer instead of rediscovering it —
`docs/EVAL_LIVE.md` (2026-08-14) has the working Maven recipe in full.

The deeper asymmetry stays on the record: Haven's sandbox and environment
defaults were both shaped around the toolchain Haven itself is written in, and
each new language finds a different edge of that assumption. ADR 0029 fixed the
filesystem half; this note is the environment half, fixed by convention rather
than by code.
