# Known gaps

**English** | [中文](KNOWN_GAPS.zh-CN.md)

This page is the boundary of the claim "Haven's audited surface has no open
defects." Everything below is a real limitation that is **deliberately accepted**,
not an oversight: each entry says what breaks, why it is acceptable, and what
pins it so it cannot silently become an oversight.

A gap belongs here only if all three hold:

1. It is verified, not suspected — there is a probe, a measurement, or a test.
2. It is deliberate — someone chose it over the alternative, and the reason is
   written down.
3. It is pinned — a test fails if the behaviour changes without a decision.

Anything that fails a test is not a gap; it is a bug, and it goes to the backlog.
Anything unverified goes to the audit list, not here.

## Sandbox: Linux cannot grant individual device nodes

`cmd > /dev/null` works under macOS Seatbelt and fails under Linux Landlock.

`WRITABLE_DEVICES` (`ports/sandbox.py`) is the list both backends are meant to
allow. Seatbelt emits one `(allow file-write-data (literal …))` per entry.
Landlock has only `path_beneath`, which applies to file hierarchies, and the
kernel returns `EINVAL` for a device node — measured as
`landlock_add_rule failed for /dev/null`, after which **every** sandboxed
command exited 125. The only way to allow it would be to grant write access to
all of `/dev`, trading a real functional gap for a wider boundary.

Accepted because the failure is loud (exit 125, not silent) and the affected
commands are a small, well-known set. Pinned by
`tests/unit/test_sandbox_selection.py::test_landlock_cannot_express_a_per_device_grant`,
which asserts the devices stay out of the Landlock spec — the regression it
guards is the 125-exit one, not the missing feature.

## The workspace lease is local-only

The single-writer lease is scoped to one machine, keyed on the resolved
workspace path. It is not a distributed lock and does not claim to be. Two
Haven processes on different machines sharing a filesystem are not covered.

Accepted because the threat it addresses is a second local process writing
between approval and execution; a network filesystem is out of that scope, and
a real distributed lock costs far more than the risk justifies. Pinned by the
module docstring in `adapters/workspace_lease.py` and by the tests that assert
same-host behaviour.

A consequence of the same scoping: the heartbeat is only consulted on the
cross-host branch of `_is_live`, and `WorkspaceLease.refresh()` is never called,
so a cross-host holder's lease becomes breakable after `STALE_AFTER_SECONDS`.
Same-host holders are unaffected — the file-lock marker described below is
authoritative there.

## Semantic localisation: the Phase 3 gate was not met

Haven has no LSP integration. `docs/ROADMAP3.md` records the gate: proceed only
if Phases 1–2 attributed ≥5 failures to semantic-localisation limits; the
measured figure was ≈1. The plan treats "record the verdict and skip" as an
explicitly acceptable outcome, and ADR 0023 carries the decision with its
evidence.

Accepted because building an LSP adapter on one data point would be exactly the
"refused to build on vibes" failure the project names as its worst outcome.
Pinned by the ROADMAP3 header and ADR 0023.

## Bounded buffers are windows, not archives

Several display and diagnostic buffers are deliberately finite:

| Buffer | Bound | Consequence when exceeded |
|---|---|---|
| TUI timeline | 2000 entries (`MAX_TIMELINE_ENTRIES`) | older entries scroll out of view |
| TUI evidence rows | 50 | older rows scroll out of view |
| TUI process results | 10 | older results scroll out of view |
| stderr diagnostics | 50 lines | older lines are dropped |
| background task output | 1000 lines per stream | older lines are dropped |

Accepted because these are views over data that is persisted elsewhere (the
event journal, the session database, the artifacts directory); the bound exists
so one long-lived process cannot grow without limit. The event journal is the
archive, and it is complete.

## PTY is a managed byte stream

`task.start` with `terminal=true` gives a POSIX PTY with merged stdout/stderr.
It is not a full terminal emulator and not a job-control shell: no terminfo, no
window resize, no job control. Raw-terminal programs may interpret the canonical
EOF markers themselves.

Accepted because the feature exists to let an interactive program run under the
same policy and approval pipeline, not to be a terminal. Recorded in
`docs/REMAINING_CAPABILITIES.md`.

## How to close a gap

A gap leaves this page when the limitation is removed, and it leaves with the
test that pins the new behaviour. Removing one requires:

1. The measurement or probe that shows the limitation is gone.
2. A test that fails if it comes back.
3. An ADR if the decision changes a boundary (a widened sandbox, a distributed
   lease, a new tool class).

A gap does **not** leave this page because it became inconvenient.
