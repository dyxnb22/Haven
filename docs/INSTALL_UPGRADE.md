# Installation and upgrade

**English** | [中文](INSTALL_UPGRADE.zh-CN.md)

Haven's accepted platforms are macOS and Linux. Prepare Python 3.12 or newer,
Git, and `uv`. Use a Python interpreter that provides SQLite `RETURNING` and the
JSON functions. `uv sync` installs dependencies from the repository's committed
`uv.lock`. ripgrep is optional; without it, search falls back to Python.

## Clean install

Install the runtime dependencies in a source checkout and check the workspace.
`--no-sync` lets later commands reuse the same environment.

```bash
uv sync --locked --no-default-groups --no-editable
uv run --no-sync haven --version
uv run --no-sync haven doctor --workspace /path/to/repository
haven_check=$(mktemp -d "${TMPDIR:-/tmp}/haven-offline.XXXXXX")
uv run --no-sync haven eval --offline --out "$haven_check"
```

`doctor` reports which sandbox backend is available. macOS uses Seatbelt's
`sandbox-exec`; Landlock on Linux needs a kernel providing ABI 4 or newer. With
no backend available, policy denies `repo.exec`. Repository reads and the file
tools continue through the ordinary permission channel, and a registered
`repo.check` can still run under approval in locally-trusted mode. Configure a
provider key only when you need a real model. MCP, dual-track routing, and
ephemeral worktrees are all explicit opt-ins, and the offline evaluation uses a
scripted model.

## Upgrading an existing session

1. Use the currently installed `haven config explain` to locate `storage.db`,
   `storage.artifacts`, and `skills.user_dir`. The user configuration file sits
   one level above `skills.user_dir`.
2. Exit Haven. Back up the whole data directory and the user configuration
   directory. The data directory holds the database, artifacts, and recovery
   material. `HAVEN_DATA_DIR` and `HAVEN_CONFIG_DIR` can point at a backup copy
   for a rehearsal.
3. Install the candidate version and open a saved run from its original
   workspace path. `haven sessions list` or `haven sessions show RUN_ID`
   migrates a supported older store the first time it opens the data directory.
   `doctor` only inspects the environment. Once the history checks out, use
   `haven resume RUN_ID --workspace PATH` to continue an interrupted task.
   Resume still validates workspace identity, the original file artifacts, and
   any undetermined side effects.
4. To roll back, first close the candidate version. Let the older installation
   read the pre-upgrade backup copy through `HAVEN_DATA_DIR` and
   `HAVEN_CONFIG_DIR`. Workspace files are restored according to each version's
   own records.

The `v0.1.0` tag corresponds to SQLite schema 2; the current candidate uses
schema 6. Migration runs inside a single transaction. An unsupported schema is
refused before anything is written, and a failed migration closes the connection
and rolls back uncommitted schema changes. The backup preserves the original
checkpoints and artifacts in a form the older version can still read, which also
covers an older strict parser meeting new event fields.

## Maintainer walkthrough

Run the following from a local checkout that carries the `v0.1.0` tag. The
script builds wheels from the old and current sources, installs the locked
runtime dependencies into two temporary environments, and reports each version
and SHA-256.

```bash
uv run --locked python scripts/check_install.py
```

The walkthrough covers `doctor`, the offline evaluation, an old-version run
interrupted after completing a file edit, replay and continuation on the new
version, a byte-for-byte comparison of old events, and having the old version
reopen the pre-upgrade backup. Workspaces, databases, artifacts, and offline
reports all live in temporary directories.

The current walkthrough covers macOS arm64 with Seatbelt and a Linux arm64
container with Landlock. Both ends install from independent Python 3.12 wheels
and pass the offline evaluation and the schema 2 to 6 upgrade. See the
[roadmap implementation record](PERSONAL_ROADMAP.md#实施记录) for the specific
results.
