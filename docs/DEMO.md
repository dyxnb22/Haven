# Haven Demo Script

A 2–3 minute walkthrough that shows the guarantees, not just a happy path. All of
it runs offline except the optional live TUI segment.

## The 30-second version: record it with one command

`scripts/demo.sh` runs the whole offline tour — eval, the security-only subset,
config provenance, context inspection, and `doctor` — with pacing tuned for a
recording. It uses a temporary data directory and needs no API key.

```bash
./scripts/demo.sh                                  # watch it
asciinema rec demo.cast -c ./scripts/demo.sh       # record it
agg demo.cast docs/demo.gif                        # turn it into a GIF for the README
PACE=0 ./scripts/demo.sh                           # instant, for CI
```

Embed the result at the top of `README.md`:

```markdown
![Haven offline demo](docs/demo.gif)
```

The manual walkthrough below covers the interactive TUI, which needs a human at
the keyboard and therefore cannot be scripted.

## 0. Setup (offline)

```bash
uv sync --locked
```

## 1. Offline eval: the claims are executable (~30s)

```bash
uv run haven eval --offline
```

Point out the summary line: all cases passed, `security violations: 0` (the
current case count is in the README's generated metrics table). Open
`eval_report/report.md` and highlight the per-category table — task, robustness,
security, injection, budget, recovery — and that security is its own hard gate,
not averaged into a score.

## 2. Trace + replay: everything is recorded (~20s)

```bash
uv run haven sessions list
uv run haven replay <RUN_ID>
```

Explain that replay re-delivers the persisted event journal through the *same*
presenter reducer the TUI uses — no model, no tools — so the timeline is a
faithful reconstruction.

## 3. Config provenance and confinement (~15s)

```bash
uv run haven config explain
```

Show that every value lists its source, the API key is reported present/missing
(never printed), and the database/artifacts live outside the workspace. Then add a
project `.haven.toml` with `[budget] max_steps = 3` and re-run to show it can only
*tighten*; add a `[provider]` section to show it's rejected.

## 4. Live TUI: the full loop with approval (optional, needs a key)

```bash
export HAVEN_API_KEY=sk-...
uv run haven
```

In a small repo with a known bug and a registered check recipe, type a task like
"fix the bug in add() and verify". Narrate as it happens:

1. streaming plan and `repo.search` / `repo.read` in the timeline;
2. a `repo.edit` proposal → the **approval dialog** showing the exact diff and the
   one-time digest;
3. approve → atomic write, then `repo.diff` and `repo.check`;
4. the **Evidence** and **Diff** tabs fill in; the run only reports success after a
   passing check recorded *after* the last edit;
5. the header's budget/cost and the single stop reason.

Then demonstrate a boundary live: ask it to read `~/.ssh/id_rsa` or edit
`.git/config` and show the policy `deny` in the timeline.

## 5. Recovery: never auto-replay an ambiguous effect (~20s)

Use the recovery eval cases (`rec-crash-not-run`, `rec-crash-ambiguous`) or a
manual crash: kill a run mid-edit, then `haven resume <RUN_ID>`. Show that a
"not run" effect resumes cleanly, while an ambiguous one blocks and prints the
`haven reconcile ... --as confirmed|not_run|abandon` instruction.

## Talking points

- The model only proposes; deterministic code owns policy, execution, and success.
- Approval is bound to the exact action and consumed once; any drift fails closed.
- Success needs evidence (diff + passing check), not the model's word.
- Every run has hard budgets and exactly one stop reason.
- Ambiguous crash effects are never auto-replayed.
