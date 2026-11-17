# Check results and process output

**English** | [中文](PROCESS_OUTPUT.zh-CN.md)

The Evidence page keeps a per-item status for the selected verification group
and concentrates the exit code, timeout, tool errors, diagnostic locations, and
log-review commands of the last 10 `repo.check` / `repo.exec` calls.

Each result shows at most 3 diagnostics; the rest are available from the log.
The evidence history retains the last 50 rows. A group's passed, failed, not
run, stale, and mismatched states are still computed by the existing ledger
rules. Opening a historical run takes the ledger from its checkpoint; later
process logs are observations that can be reviewed, not evidence.

## Viewing and exporting

In the TUI, `/output` opens the Evidence page, then use the call ID shown there:

```text
/output CALL_ID
```

The window pages at 16,000 characters per page with Previous/Next, and Esc
closes. Logs are read-only text: file and line numbers are for locating, and
they never open a path or execute anything in it.

The command-line entry likewise reads local storage only and creates no model
service:

```bash
haven output RUN_ID
haven output RUN_ID CALL_ID
haven output RUN_ID CALL_ID --start-line 201 --max-lines 200
haven output RUN_ID CALL_ID --save /tmp/check-output.log
```

Without a call ID it shows the acceptance status and recent results. Viewing a
log defaults to at most 200 lines and 20,000 characters, with `--max-lines`
capped at 1,000; content beyond the displayed range is reported as the next
line, and an over-long single line is reported with a pointer to save the raw
text. `--save` exports the complete retained log and refuses to overwrite an
existing file. Screen output strips terminal control characters and the TUI
escapes Rich markup; the exported file keeps the log text. A read position is
parsed only from the completion event of the given run/call — arbitrary file or
artifact addresses are not accepted.

## Retention and summary bounds

The standard executor stores stdout and stderr text separately: up to 2 MiB per
stream (counted in pre-decode bytes). Within range the full output is kept;
beyond it the head and tail are kept with the omitted byte count inserted
between. Both the log header and the result overview state truncation
explicitly. Invalid UTF-8 is decoded with replacement characters.

For pipes, the final stdout segment and each stderr segment are saved, and
merged stderr shares the same limit. A timeout keeps the output read before the
process was killed and records both the timeout and exit code 124. The model's
original tail observation stays bounded; complete logs never enter model
context automatically.

Diagnostics are extracted from this fuller captured text, which can locate early
errors outside the original tail range. Supported formats include pytest, Ruff
(compact and multi-line), Mypy, Python tracebacks, and existing compiler
formats. At most 25 diagnostics, each message at most 500 characters, with a
formatted summary of at most 4,000 characters. When nothing is recognised, a
bounded error-line or tail fallback is used and the log is kept for manual
inspection.

Logs are stored as local artifacts carrying a SHA-256 checksum; the completion
event saves only the content address and bounded display information. Events
referencing a live run protect its logs, and `haven gc` cleans unreferenced
artifacts only after deleting the owning run. Artifact sweeping is deferred
while a run is active, so a log just written but not yet followed by a
completion event is not deleted. Sweep candidates are limited to artifacts that
already existed before the sweep began; logs carry their owning run and call ID,
so a new run's log cannot fall into an older sweep set by reusing old output
content. Old runs, or cancelled calls without a completion event, may have no
log, and review reports that explicitly; a compatible older executor that
returned only a tail is saved with its truncation flag, and already-discarded
content cannot be recovered. A missing or corrupted artifact is reported as an
error; the original command is never re-run.

Process reports and logs take no part in permission or the Evidence Gate:
ordinary exec produces no check evidence even with exit code zero and output
claiming everything passed. For verification-scope rules see
[verification groups](VERIFICATION_GROUPS.md); for the persistence boundary see
[ADR 0047](adr/0047-retained-process-output.md).
