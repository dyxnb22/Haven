"""Filesystem workspace adapter.

Everything the agent can see or touch on disk goes through this class. It
normalizes paths, fails closed on escapes, enforces size caps, binds edits to
preimages, applies them atomically, and tracks per-run originals so
`repo.diff` shows only what *this run* changed.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import os
import re
import shutil
import tempfile
from pathlib import Path

from haven.domain.digest import sha256_bytes, sha256_text
from haven.ports.workspace import (
    EditOutcome,
    EditPreview,
    ListEntry,
    ListResult,
    PathFacts,
    ReadResult,
    RunDiff,
    SearchMatch,
    SearchResult,
    WorkspaceError,
    WorkspaceSnapshot,
)

MAX_READ_BYTES = 128 * 1024
MAX_EDIT_FILE_BYTES = 256 * 1024
MAX_SEARCH_FILE_BYTES = 1024 * 1024
MAX_SEARCH_TOTAL_BYTES = 64 * 1024
MAX_SEARCH_LINE_CHARS = 240
MAX_DIFF_BYTES = 64 * 1024
MAX_CREATE_BYTES = 256 * 1024
RIPGREP_TIMEOUT_SECONDS = 20.0

#: Path components that tools may never touch (the agent must not be able to
#: rewrite its own configuration, git history, or audit surfaces).
PROTECTED_COMPONENTS = frozenset({".git", ".haven", ".haven.toml"})

#: Vendor and build directories that are never worth searching. Ripgrep also
#: honours `.gitignore`; this list is what keeps the pure-Python fallback from
#: walking `node_modules` on a real repository.
IGNORED_DIRS = frozenset(
    {
        ".direnv",
        ".gradle",
        ".idea",
        ".mypy_cache",
        ".next",
        ".nuxt",
        ".parcel-cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svelte-kit",
        ".terraform",
        ".tox",
        ".venv",
        ".vscode",
        "__pycache__",
        ".haven-scratch",
        "bower_components",
        "build",
        "coverage",
        "dist",
        "htmlcov",
        "node_modules",
        "target",
        "venv",
        "vendor",
    }
)


class FsWorkspace:
    """Implements WorkspacePort for a local directory."""

    def __init__(self, root: Path, *, use_ripgrep: bool = True) -> None:
        resolved = root.resolve()
        if not resolved.is_dir():
            raise WorkspaceError("not_found", f"workspace root does not exist: {root}")
        self._root = resolved
        self._workspace_digest = sha256_text(str(resolved))
        # path (normalized, relative) -> file content before this run's first
        # write. A file created by this run maps to "" so the run diff shows it
        # as a pure addition.
        self._originals: dict[str, str] = {}
        self._ripgrep = shutil.which("rg") if use_ripgrep else None

    @property
    def root(self) -> Path:
        return self._root

    @property
    def workspace_digest(self) -> str:
        return self._workspace_digest

    # -- path handling -------------------------------------------------------

    def path_facts(self, raw_path: str) -> PathFacts:
        """Normalize a model-proposed path and collect verified facts."""
        outside = PathFacts(
            raw=raw_path,
            normalized="",
            within_workspace=False,
            is_protected=False,
            exists=False,
            is_file=False,
            is_dir=False,
            size_bytes=0,
            digest=None,
        )
        if not raw_path or raw_path.startswith(("/", "~")) or "\x00" in raw_path:
            return outside

        candidate = (self._root / raw_path).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            return outside

        normalized = (
            candidate.relative_to(self._root).as_posix() if candidate != self._root else "."
        )
        is_protected = any(part in PROTECTED_COMPONENTS for part in Path(normalized).parts)

        exists = candidate.exists()
        is_file = candidate.is_file() and not candidate.is_symlink() if exists else False
        is_dir = candidate.is_dir() if exists else False
        size = candidate.stat().st_size if is_file else 0
        digest: str | None = None
        if is_file and size <= MAX_EDIT_FILE_BYTES:
            digest = sha256_bytes(candidate.read_bytes())

        return PathFacts(
            raw=raw_path,
            normalized=normalized,
            within_workspace=True,
            is_protected=is_protected,
            exists=exists,
            is_file=is_file,
            is_dir=is_dir,
            size_bytes=size,
            digest=digest,
        )

    def _require_inside(self, raw_path: str) -> tuple[Path, str]:
        facts = self.path_facts(raw_path)
        if not facts.within_workspace:
            raise WorkspaceError("denied", f"path escapes the workspace: {raw_path!r}")
        if facts.is_protected:
            raise WorkspaceError("denied", f"path is protected: {facts.normalized!r}")
        return self._root / facts.normalized, facts.normalized

    # -- read-only tools -----------------------------------------------------

    async def list_dir(self, path: str, max_entries: int) -> ListResult:
        target, normalized = self._require_inside(path)
        if not target.is_dir():
            raise WorkspaceError("not_found", f"not a directory: {normalized!r}")

        entries: list[ListEntry] = []
        truncated = False
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        for child in children:
            if child.name in PROTECTED_COMPONENTS:
                continue
            if len(entries) >= max_entries:
                truncated = True
                break
            is_dir = child.is_dir()
            size = child.stat().st_size if child.is_file() else 0
            entries.append(ListEntry(name=child.name, is_dir=is_dir, size_bytes=size))
        return ListResult(path=normalized, entries=tuple(entries), truncated=truncated)

    async def search(self, pattern: str, path: str, max_results: int) -> SearchResult:
        """Search file contents, preferring ripgrep and falling back to Python.

        Both backends skip the same vendor/build directories, cap results the
        same way, and emit the same normalized shape, so on a tree without a
        `.gitignore` they return identical matches (asserted in the tests).
        Ripgrep additionally honours `.gitignore`, which is what makes search
        usable on a real repository; the pure-Python fallback approximates that
        with the fixed `IGNORED_DIRS` list.
        """
        target, normalized = self._require_inside(path)
        try:
            re.compile(pattern)
        except re.error as exc:
            raise WorkspaceError("invalid_arguments", f"invalid regex: {exc}") from exc
        if not target.exists():
            raise WorkspaceError("not_found", f"no such path to search: {normalized!r}")

        if self._ripgrep is not None:
            return await self._search_ripgrep(self._ripgrep, pattern, target, max_results)
        return self._search_walk(pattern, target, max_results)

    async def _search_ripgrep(
        self, ripgrep: str, pattern: str, target: Path, max_results: int
    ) -> SearchResult:
        # Fixed argv, no shell. `--regexp=` and `--` keep a pattern or path that
        # begins with a dash from being parsed as a ripgrep flag.
        argv = [
            ripgrep,
            "--line-number",
            "--no-heading",
            "--with-filename",
            "--color=never",
            "--sort=path",
            # Honour .gitignore even when the workspace is not a git checkout,
            # so ignore semantics do not depend on whether .git happens to exist.
            "--no-require-git",
            f"--max-filesize={MAX_SEARCH_FILE_BYTES}",
            *(f"--glob=!{name}" for name in sorted(IGNORED_DIRS | PROTECTED_COMPONENTS)),
            f"--regexp={pattern}",
            "--",
            str(target),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self._root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
            raw, err = await asyncio.wait_for(proc.communicate(), timeout=RIPGREP_TIMEOUT_SECONDS)
        except (OSError, TimeoutError):
            # A missing or misbehaving ripgrep must never break the tool.
            return self._search_walk(pattern, target, max_results)
        # 0 = matches, 1 = no matches, 2 = partial IO error (an unreadable file
        # or a vanished path) where stdout is still valid. A search backend
        # hiccup must degrade, never abort the run.
        if proc.returncode not in (0, 1, 2):
            return self._search_walk(pattern, target, max_results)
        if proc.returncode == 2 and not raw.strip():
            return self._search_walk(pattern, target, max_results)

        matches: list[SearchMatch] = []
        seen_files: set[str] = set()
        total_bytes = 0
        truncated = False
        for line in raw.decode("utf-8", errors="replace").splitlines():
            parsed = self._parse_ripgrep_line(line)
            if parsed is None:
                continue
            rel, line_number, text = parsed
            seen_files.add(rel)
            clipped = text.strip()[:MAX_SEARCH_LINE_CHARS]
            total_bytes += len(clipped.encode("utf-8"))
            matches.append(SearchMatch(path=rel, line_number=line_number, line=clipped))
            if len(matches) >= max_results or total_bytes >= MAX_SEARCH_TOTAL_BYTES:
                truncated = True
                break
        return SearchResult(
            matches=tuple(matches), files_scanned=len(seen_files), truncated=truncated
        )

    def _parse_ripgrep_line(self, line: str) -> tuple[str, int, str] | None:
        """Parse `path:line:text`, tolerating colons inside the path and text."""
        head, sep, text = line.partition(":")
        if not sep:
            return None
        number, sep, text = text.partition(":")
        if not sep or not number.isdigit():
            return None
        try:
            rel = Path(head).resolve().relative_to(self._root).as_posix()
        except ValueError:
            return None
        return rel, int(number), text

    def _search_walk(self, pattern: str, target: Path, max_results: int) -> SearchResult:
        compiled = re.compile(pattern)
        matches: list[SearchMatch] = []
        # Files that produced at least one match, which is the only count
        # ripgrep can report. Counting files *walked* here instead would make
        # the same field mean different things depending on whether ripgrep
        # happens to be installed.
        seen_files: set[str] = set()
        total_bytes = 0
        truncated = False

        for file_path in self._iter_files(target):
            if truncated:
                break
            try:
                if file_path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
                data = file_path.read_bytes()
            except OSError:
                continue
            if b"\x00" in data:
                continue  # binary
            text = data.decode("utf-8", errors="replace")
            rel = file_path.relative_to(self._root).as_posix()
            for line_number, line in enumerate(text.splitlines(), start=1):
                if compiled.search(line):
                    clipped = line.strip()[:MAX_SEARCH_LINE_CHARS]
                    total_bytes += len(clipped.encode("utf-8"))
                    matches.append(SearchMatch(path=rel, line_number=line_number, line=clipped))
                    seen_files.add(rel)
                    if len(matches) >= max_results or total_bytes >= MAX_SEARCH_TOTAL_BYTES:
                        truncated = True
                        break
        return SearchResult(
            matches=tuple(matches), files_scanned=len(seen_files), truncated=truncated
        )

    def _iter_files(self, target: Path) -> list[Path]:
        if target.is_file():
            return [target]
        found: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames[:] = sorted(
                d for d in dirnames if d not in PROTECTED_COMPONENTS and d not in IGNORED_DIRS
            )
            for name in sorted(filenames):
                if name in PROTECTED_COMPONENTS:
                    continue
                candidate = Path(dirpath) / name
                if candidate.is_file() and not candidate.is_symlink():
                    found.append(candidate)
        return found

    async def read_file(self, path: str, start_line: int, max_lines: int) -> ReadResult:
        target, normalized = self._require_inside(path)
        if not target.is_file() or target.is_symlink():
            raise WorkspaceError("not_found", f"not a regular file: {normalized!r}")

        data = target.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError(
                "invalid_arguments", f"not a UTF-8 text file: {normalized!r}"
            ) from exc

        digest = sha256_bytes(data)
        lines = text.splitlines(keepends=True)
        total = len(lines)
        window = lines[start_line - 1 : start_line - 1 + max_lines]

        content = ""
        emitted = 0
        byte_budget = MAX_READ_BYTES
        byte_truncated = False
        for line in window:
            encoded = len(line.encode("utf-8"))
            if encoded > byte_budget:
                byte_truncated = True
                break
            content += line
            byte_budget -= encoded
            emitted += 1

        end_line = start_line - 1 + emitted
        truncated = byte_truncated or end_line < total
        return ReadResult(
            path=normalized,
            content=content,
            start_line=start_line,
            end_line=end_line,
            total_lines=total,
            truncated=truncated,
            digest=digest,
        )

    # -- edit ------------------------------------------------------------------

    async def preview_edit(
        self,
        path: str,
        old: str,
        new: str,
        *,
        occurrence: int | None = None,
        replace_all: bool = False,
    ) -> EditPreview:
        target, normalized = self._require_inside(path)
        text, preimage = self._load_editable(target, normalized)
        new_text = self._apply_replacement(
            text, old, new, normalized, occurrence=occurrence, replace_all=replace_all
        )
        return self._diff_preview(normalized, text, new_text, preimage)

    async def apply_edit(
        self,
        path: str,
        old: str,
        new: str,
        expected_preimage: str,
        *,
        occurrence: int | None = None,
        replace_all: bool = False,
    ) -> EditOutcome:
        target, normalized = self._require_inside(path)
        text, preimage = self._load_editable(target, normalized)
        if preimage != expected_preimage:
            raise WorkspaceError(
                "stale_preimage",
                f"file changed since it was read/approved: {normalized!r}",
            )
        new_text = self._apply_replacement(
            text, old, new, normalized, occurrence=occurrence, replace_all=replace_all
        )

        if normalized not in self._originals:
            self._originals[normalized] = text

        postimage = self._atomic_write(target, normalized, new_text)
        return EditOutcome(path=normalized, preimage_digest=preimage, postimage_digest=postimage)

    # -- create ----------------------------------------------------------------

    async def preview_create(self, path: str, content: str) -> EditPreview:
        normalized = self._require_creatable(path, content)
        return self._diff_preview(normalized, "", content, preimage="")

    async def apply_create(self, path: str, content: str) -> EditOutcome:
        normalized = self._require_creatable(path, content)
        target = self._root / normalized
        target.parent.mkdir(parents=True, exist_ok=True)

        if normalized not in self._originals:
            # An empty original makes the run diff show the file as an addition.
            self._originals[normalized] = ""

        postimage = self._atomic_write(target, normalized, content)
        return EditOutcome(path=normalized, preimage_digest="", postimage_digest=postimage)

    def _require_creatable(self, path: str, content: str) -> str:
        """Creation is only for genuinely new files; overwriting must go through
        repo.edit so it stays bound to a preimage."""
        facts = self.path_facts(path)
        if not facts.within_workspace:
            raise WorkspaceError("denied", f"path escapes the workspace: {path!r}")
        if facts.is_protected:
            raise WorkspaceError("denied", f"path is protected: {facts.normalized!r}")
        if facts.normalized in (".", ""):
            raise WorkspaceError("invalid_arguments", "a file path is required")
        if facts.exists:
            kind = "directory" if facts.is_dir else "file"
            raise WorkspaceError(
                "invalid_arguments",
                f"{facts.normalized!r} already exists as a {kind}; use repo.edit to change it",
            )
        if len(content.encode("utf-8")) > MAX_CREATE_BYTES:
            raise WorkspaceError(
                "invalid_arguments", f"content too large to create: {facts.normalized!r}"
            )
        return facts.normalized

    # -- shared write plumbing ---------------------------------------------------

    def _atomic_write(self, target: Path, normalized: str, new_text: str) -> str:
        """Write via temp file + fsync + rename, then re-read to confirm.

        A successful write() is not evidence; the postimage digest is.
        """
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".haven-write-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(new_text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except OSError:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

        postimage = sha256_bytes(target.read_bytes())
        if postimage != sha256_text(new_text):
            raise WorkspaceError("internal", f"postimage mismatch after write: {normalized!r}")
        return postimage

    @staticmethod
    def _diff_preview(normalized: str, before: str, after: str, preimage: str) -> EditPreview:
        diff_lines = list(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{normalized}" if before else "/dev/null",
                tofile=f"b/{normalized}",
            )
        )
        insertions = sum(
            1 for line in diff_lines if line.startswith("+") and not line.startswith("+++")
        )
        deletions = sum(
            1 for line in diff_lines if line.startswith("-") and not line.startswith("---")
        )
        return EditPreview(
            path=normalized,
            diff="".join(diff_lines),
            preimage_digest=preimage,
            postimage_digest=sha256_text(after),
            insertions=insertions,
            deletions=deletions,
        )

    def _load_editable(self, target: Path, normalized: str) -> tuple[str, str]:
        if not target.is_file() or target.is_symlink():
            raise WorkspaceError("not_found", f"not a regular file: {normalized!r}")
        data = target.read_bytes()
        if len(data) > MAX_EDIT_FILE_BYTES:
            raise WorkspaceError(
                "invalid_arguments",
                f"file too large to edit ({len(data)} bytes): {normalized!r}",
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError(
                "invalid_arguments", f"not a UTF-8 text file: {normalized!r}"
            ) from exc
        return text, sha256_bytes(data)

    @staticmethod
    def _apply_replacement(
        text: str,
        old: str,
        new: str,
        normalized: str,
        *,
        occurrence: int | None,
        replace_all: bool,
    ) -> str:
        """Replace one or all occurrences of `old`.

        Default is still "must be unique", because an accidental multi-match is
        the most common way an agent silently corrupts a file. `replace_all`
        and `occurrence` are the two explicit ways to opt out, so the intent is
        always recorded in the approved arguments.
        """
        if replace_all and occurrence is not None:
            raise WorkspaceError(
                "invalid_arguments", "set either replace_all or occurrence, not both"
            )

        count = text.count(old)
        if count == 0:
            raise WorkspaceError(
                "not_found",
                f"old_string not found in {normalized!r}; re-read the file and copy the "
                "exact text including indentation",
            )

        if replace_all:
            return text.replace(old, new)

        if occurrence is not None:
            if occurrence > count:
                raise WorkspaceError(
                    "not_found",
                    f"occurrence {occurrence} requested but old_string appears only "
                    f"{count} time(s) in {normalized!r}",
                )
            index = -1
            for _ in range(occurrence):
                index = text.index(old, index + 1)
            return text[:index] + new + text[index + len(old) :]

        if count > 1:
            raise WorkspaceError(
                "ambiguous_match",
                f"old_string occurs {count} times in {normalized!r}. Include more "
                "surrounding lines to make it unique, or pass occurrence=N (1-based) "
                "to pick one, or replace_all=true to change every match",
            )
        return text.replace(old, new, 1)

    # -- run-scoped diff -------------------------------------------------------

    async def run_diff(self) -> RunDiff:
        chunks: list[str] = []
        files: list[str] = []
        insertions = 0
        deletions = 0
        truncated = False
        total = 0

        for normalized in sorted(self._originals):
            original = self._originals[normalized]
            target = self._root / normalized
            current = ""
            if target.is_file():
                try:
                    current = target.read_bytes().decode("utf-8", errors="replace")
                except OSError:
                    current = ""
            if current == original:
                continue
            diff_lines = list(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    current.splitlines(keepends=True),
                    fromfile=f"a/{normalized}",
                    tofile=f"b/{normalized}",
                )
            )
            files.append(normalized)
            insertions += sum(
                1 for line in diff_lines if line.startswith("+") and not line.startswith("+++")
            )
            deletions += sum(
                1 for line in diff_lines if line.startswith("-") and not line.startswith("---")
            )
            chunk = "".join(diff_lines)
            if total + len(chunk) > MAX_DIFF_BYTES:
                chunk = chunk[: MAX_DIFF_BYTES - total]
                truncated = True
            total += len(chunk)
            chunks.append(chunk)
            if truncated:
                break

        return RunDiff(
            diff="".join(chunks),
            files=tuple(files),
            insertions=insertions,
            deletions=deletions,
            truncated=truncated,
        )

    def original_contents(self) -> dict[str, str]:
        return dict(self._originals)

    def restore_originals(self, originals: dict[str, str]) -> None:
        self._originals = dict(originals)

    # -- process-write attribution (ADR 0012) ----------------------------------

    def capture_snapshot(self) -> WorkspaceSnapshot:
        """Digest every regular file, and keep the text of the diffable ones.

        The digest map is gate-complete: any change, text or binary, moves a
        digest. The content map is what the run diff can render. Protected and
        ignored directories are excluded, so a process that only writes bytecode
        caches or the sandbox scratch dir records no change.
        """
        digests: dict[str, str] = {}
        contents: dict[str, str] = {}
        for file_path in self._iter_files(self._root):
            try:
                data = file_path.read_bytes()
            except OSError:
                continue
            normalized = file_path.relative_to(self._root).as_posix()
            digests[normalized] = sha256_bytes(data)
            if len(data) <= MAX_EDIT_FILE_BYTES:
                with contextlib.suppress(UnicodeDecodeError):
                    contents[normalized] = data.decode("utf-8")
        return WorkspaceSnapshot(digests=digests, contents=contents)

    def register_run_original(self, path: str, content: str) -> None:
        """Seed the run diff's original for a path a process changed, but only
        if it is not already tracked — a file edited earlier in the run must
        keep its true run-start original, not be reset to its pre-process one."""
        if path not in self._originals:
            self._originals[path] = content
