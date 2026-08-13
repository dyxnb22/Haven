# Testing Patterns

## Core Sections (Required)

### 1) Test Stack and Commands

- Primary test framework: pytest (asyncio_mode=auto via pytest-asyncio),
  pytest-timeout for TUI journeys; hypothesis for property tests.
- Assertion/mocking tools: plain pytest asserts; respx for provider HTTP;
  dependency-injected fakes for everything else.
- Commands:

```bash
uv run pytest                       # the whole suite (count in README's metrics table)
uv run pytest tests/unit -q        # one category
uv run coverage run -m pytest && uv run coverage report --include="src/*"
uv run haven eval --offline         # 38 scripted eval cases (security gate)
HAVEN_UPDATE_GOLDEN=1 uv run pytest tests/golden -q   # regenerate golden trace
```

### 2) Test Layout

- Placement: central `tests/` tree by category: `unit/`, `integration/`,
  `contract/`, `security/`, `recovery/`, `tui/`, `golden/`, `eval/`.
- Naming: `test_*.py`, descriptive test names in sentence style
  (e.g. `test_rejected_approval_cannot_be_consumed`).
- Setup: shared integration harness `tests/integration/harness.py`
  (ScriptedModel + real adapters on a temp repo); TUI uses Textual Pilot.

### 3) Test Scope Matrix

| Scope | Covered? | Typical target | Notes |
|-------|----------|----------------|-------|
| Unit | yes | domain logic, compaction, policy, maintenance, lease | pure, fast |
| Integration | yes | full pipeline journeys (edit/patch/exec/steering/standing approvals) via scripted model | real adapters, temp repos |
| Contract | yes | both session stores kept equal; provider wire behavior (respx) | `tests/contract/` |
| Security | yes | sandbox enforcement by running real commands; policy escapes | skips per-platform where no backend |
| E2E (TUI) | yes | Pilot journeys: approval modal, /rewind, steering, fork | `tests/tui/test_tui_journey.py` |
| Golden | yes | full event-trace byte comparison, TUI==headless | `tests/golden/` |
| Offline eval | yes | 38 JSON cases run the real stack with only the model scripted; unauthorized changes / transcript leaks fail the build | `evals/cases/`, `src/haven/evalkit/runner.py` |

### 4) Mocking and Isolation Strategy

- Main approach: dependency injection at ports — `ScriptedModel`,
  `MemorySessionStore`, `RecordingLauncher`; no global monkeypatching norm.
- Isolation: every test gets a temp repo/store; eval cases run in disposable
  fixture copies; `HAVEN_DATA_DIR` redirects user data in CLI tests.
- Common failure mode: TUI Pilot timing (mitigated with `_settle`/timeouts).
  The one other flake seen — standing approvals, 1 in 8 runs — was root-caused
  rather than retried away: the test issued three *consecutive* identical
  checks, which is exactly the no-progress condition, and only passed because
  check results carry `duration_ms` and the milliseconds usually differed. It
  now interleaves `repo.diff` like a real fix/verify loop. Worth repeating as
  a rule: a test that passes on timing jitter is testing the clock.

### 5) Coverage and Quality Signals

- Coverage tool + threshold: coverage.py in CI; **no fail_under enforced**.
- Current reported coverage: ~88% of `src/` (generated metrics table in
  README).
- Known gaps: TUI worker paths partially covered; `evals/` harness scripts are
  not unit-tested (exercised by live runs, and by rebuilding fixtures and
  diffing when they are refactored). No flaky tests are currently known.

### 6) Evidence

- `pyproject.toml` (`[tool.pytest.ini_options]`)
- `tests/integration/harness.py`
- `.github/workflows/ci.yml`
