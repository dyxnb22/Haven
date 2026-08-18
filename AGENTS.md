# Haven

## Project conventions

- Python 3.12+ project managed with `uv`; source code lives under `src/`.
- Keep changes focused and preserve the existing CLI/API contracts.
- Do not scan or modify `evals/real/`, `evals/headtohead/`, or generated `eval_report/` unless the task explicitly requires it.

## Validation

- Fast project gates: `uv run python scripts/gates.py --mode fast`
- Tests: `uv run pytest -q`
- Offline behavior/evaluation checks: `uv run haven eval --offline`
- Run focused checks first, then the fast gates; report the commands and results.

## Codex workflow

- For multi-file, architectural, or unfamiliar-code work, make a plan before editing.
- For learning requests, explain the relevant flow first and do not modify code unless asked.
- For implementation requests, inspect the diff and run relevant tests before considering the task complete.
- Never claim a test or command passed without actually running it.
