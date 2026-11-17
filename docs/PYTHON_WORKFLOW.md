# Python engineering: from recipe discovery to reviewing the change

**English** | [中文](PYTHON_WORKFLOW.zh-CN.md)

The template that works today is an ordinary local Python 3.12+ project with a
`src/` layout, pytest, and Ruff. Project dependencies and those two check tools
must already be installed in the Python environment the recipe uses. Neither
initialisation nor diagnostics install dependencies or run project scripts.

## 1. Preview and confirm recipes

Run from the project root:

```bash
haven init
```

A pytest configuration, dependency declaration, or test directory can trigger
the existing pytest discoverer. Ruff needs `[tool.ruff]` in `pyproject.toml`
(including its subtables), or `ruff.toml` / `.ruff.toml` at the root. A project
script merely named `lint` does not trigger Ruff discovery.

For example, this project configuration:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]

[tool.ruff]
line-length = 100
```

yields two fixed-argv candidates:

```toml
[recipes.pytest]
argv = ["python", "-m", "pytest", "-q"]

[recipes.ruff]
argv = ["python", "-m", "ruff", "check", "--no-cache", "."]
```

Once the commands suit the project, save them with `haven init --accept`; or use
`haven discover` to preview and `haven discover --accept` to append candidates.
An existing recipe with the same ID keeps its definition. Discovery reads only
known configuration files and shallow directories — it never runs a check, a
project script, or an interpreter.

Then add the optional verification group for this run to `.haven.toml`:

```toml
[verification_groups]
quality = ["pytest", "ruff"]
```

## 2. Check the environment and execution scope

```bash
haven config explain
haven doctor
```

`config explain` shows the final commands with their user/project configuration
file provenance, the timeout, network permission, the extra readable paths a
recipe declares, and the uniform execution rules:

- The working directory is the run's workspace root, or the worktree root when
  isolation is enabled.
- The environment forwards only `PATH`, `HOME`, `LANG`, `LC_ALL`, `TERM`,
  `TMPDIR`, and `VIRTUAL_ENV`; the explanation shows variable names only.
- Child processes always get `CI=1`, `DEBIAN_FRONTEND=noninteractive`,
  `TERM=dumb`, and `NO_COLOR=1`, with `TMPDIR` pointing at a separate temporary
  directory outside the workspace.
- The interpreter's default readable roots are listed separately; a recipe's
  extra paths expand `~` and are resolved at execution. Network is denied by
  default, still enforced by the existing approval and the OS sandbox.

`doctor` checks executables; for `python -m pytest` / `python -m ruff`, when the
resolved interpreter belongs to Haven's own environment it also looks for the
module in that environment's install directory. A missing module exits 2 with a
prompt to prepare project dependencies first. The lookup never imports a
same-named module from the repository, nor runs module bodies.

If a recipe uses a different Python environment, diagnostics state plainly that
dependency availability is unknown. Confirm the tools and project dependencies
in that environment yourself, e.g. by running `python -m pytest --version` and
`python -m ruff --version`. A module's presence does not guarantee all project
dependencies are complete; the actual check result remains the judge.

## 3. Edit, satisfy both checks, review

In the TUI, first select the acceptance scope for the next run with
`/verify use quality`, then enter the task; use `/verify` to see both results.
Selecting runs no command automatically and grants no permission.

To keep the main workspace free for review, use the existing headless flow:

```bash
haven run "修复 add 的结果并移除无用 import" \
  --write --approval-policy all --check-group quality \
  --worktree --review --json --events /tmp/haven-python-events.jsonl
```

`--approval-policy all` explicitly authorises every policy-permitted approval
request for this run; interactive per-item approval is available in the TUI.
Checks still use registered recipes and the single execution channel. The
`run.created.workspace` event records the actual working directory. Every check
must pass with its full argv after the last write; a missing item, a failure, a
stale result, or an appended filter argument cannot satisfy the selected group.

On success, the JSON `verification` lists both states, and `review_required`
means review is still needed. Substitute the real run ID and the binding from
viewing it:

```bash
haven review RUN_ID
haven review RUN_ID --apply --binding REVIEW_BINDING
```

Viewing applies nothing; applying binds to the specific snapshot viewed. Add
`--workspace /path/to/project` to any of these commands when run from outside
the project directory. For group resume, later-turn inheritance, and clearing
rules see [verification groups](VERIFICATION_GROUPS.md).

## Offline acceptance

[A temporary Python project](../tests/integration/test_python_workflow.py)
covers initialisation preview, acceptance and repeated discovery, read-only
diagnostics, real pytest/Ruff failures and passes, worktree isolation, and
explicit review application. It uses only a scripted model while actually
executing both check tools; the test records the limits handed to the sandbox,
and OS-level limits are verified by separate security tests. Extra process
environment variables do not reach the check subprocess.

```bash
uv run pytest -q tests/integration/test_python_workflow.py
```

This fixture validates the tool flow; it does not measure real-model task
success rates.
