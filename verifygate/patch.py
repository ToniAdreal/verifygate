"""Unified-diff parsing and application.

The repair loop needs to turn an LLM's patch *proposal* into an actual
candidate change before the deterministic checks can judge it. LLM output
is prose-wrapped (markdown fences, explanations), so the parser scans for
real diff structure and ignores everything else. Application is strict:
context and removed lines must match the file on disk exactly (a small
positional search tolerates stale line numbers, never stale content), and
paths may never escape the candidate workspace. A patch that does not
apply cleanly raises PatchError — it is never partially forced in.

Git mode lines are honoured (since v1.9.0): a ``new file mode 100755``
header makes a created file executable, an ``old mode`` / ``new mode``
pair changes an existing file's permission bits, and a mode-only diff
(no hunks at all) applies as a pure chmod. Before this, mode lines
were silently dropped, so a repair that created a script could never
make it runnable — the patched candidate failed its ``./script``
check for a reason that had nothing to do with the model's fix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


class PatchError(ValueError):
    """Raised when a patch cannot be parsed or applied cleanly."""


class NoPatchFound(PatchError):
    """Raised when the text contains no unified diff at all."""


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[tuple[str, str]] = field(default_factory=list)  # (kind, text)
    new_no_trailing_newline: bool = False


@dataclass
class FilePatch:
    old_path: str  # "/dev/null" for a file creation
    new_path: str  # "/dev/null" for a file deletion
    hunks: list[Hunk] = field(default_factory=list)
    new_mode: int | None = None  # permission bits from a git mode line


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# Real models sometimes emit a bare "@@" header with no line numbers.
# Such hunks are accepted by deriving the counts from the hunk body;
# application still verifies every context/removed line against the
# file on disk and locates the hunk by unique content search, so a
# bare header weakens nothing.
_BARE_HUNK_RE = re.compile(r"^@@\s*$|^@@\s.*@@\s*$")


def _clean_path(raw: str) -> str:
    path = raw.strip().split("\t")[0].split(" ")[0]
    if path == "/dev/null":
        return path
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def _mode_value(line: str) -> int | None:
    try:
        return int(line.split()[-1], 8)
    except ValueError:
        return None


def parse_patch(text: str) -> list[FilePatch]:
    """Extract file patches from (possibly prose-wrapped) unified diff text."""
    lines = text.splitlines()
    patches: list[FilePatch] = []
    # Git extended headers seen since the last file patch: the paths
    # from a `diff --git` line and a pending mode change. A mode line
    # whose file never reaches a ---/+++ pair is a mode-only diff and
    # is flushed as a hunk-less FilePatch at the next boundary.
    git_paths: tuple[str, str] | None = None
    pending_mode: int | None = None
    pending_create = False

    def flush_mode_only() -> None:
        nonlocal git_paths, pending_mode, pending_create
        if git_paths is not None and pending_mode is not None:
            patches.append(FilePatch(
                old_path="/dev/null" if pending_create else git_paths[0],
                new_path=git_paths[1],
                hunks=[],
                new_mode=pending_mode,
            ))
        git_paths = None
        pending_mode = None
        pending_create = False

    i = 0
    while i < len(lines):
        if lines[i].startswith("diff --git "):
            flush_mode_only()
            parts = lines[i].split()
            if len(parts) >= 4:
                git_paths = (_clean_path(parts[2]), _clean_path(parts[3]))
            i += 1
            continue
        if lines[i].startswith("new file mode "):
            pending_mode = _mode_value(lines[i])
            pending_create = True
            i += 1
            continue
        if lines[i].startswith("new mode "):
            pending_mode = _mode_value(lines[i])
            i += 1
            continue
        if lines[i].startswith("--- ") and i + 1 < len(lines) \
                and lines[i + 1].startswith("+++ "):
            fp = FilePatch(
                old_path=_clean_path(lines[i][4:]),
                new_path=_clean_path(lines[i + 1][4:]),
                new_mode=pending_mode,
            )
            git_paths = None
            pending_mode = None
            pending_create = False
            i += 2
            while i < len(lines):
                m = _HUNK_RE.match(lines[i])
                bare = False
                if m:
                    hunk = Hunk(
                        old_start=int(m.group(1)),
                        old_count=int(m.group(2) or 1),
                        new_start=int(m.group(3)),
                        new_count=int(m.group(4) or 1),
                    )
                elif _BARE_HUNK_RE.match(lines[i]):
                    bare = True
                    hunk = Hunk(old_start=1, old_count=0, new_start=1,
                                new_count=0)
                else:
                    break
                i += 1
                need_old, need_new = hunk.old_count, hunk.new_count
                while i < len(lines) and (bare or need_old > 0 or need_new > 0):
                    line = lines[i]
                    if line.startswith("\\"):  # "\ No newline at end of file"
                        if hunk.lines and hunk.lines[-1][0] in (" ", "+"):
                            hunk.new_no_trailing_newline = True
                        i += 1
                        continue
                    if bare and (
                        not line
                        or line.startswith(("--- ", "+++ "))
                    ):
                        break  # bare hunks end at blanks / next file
                    kind = line[0] if line else " "
                    content = line[1:] if line else ""
                    if kind not in (" ", "-", "+"):
                        break
                    hunk.lines.append((kind, content))
                    if kind in (" ", "-"):
                        need_old -= 1
                    if kind in (" ", "+"):
                        need_new -= 1
                    i += 1
                if bare:
                    hunk.old_count = sum(
                        1 for k, _ in hunk.lines if k in (" ", "-"))
                    hunk.new_count = sum(
                        1 for k, _ in hunk.lines if k in (" ", "+"))
                    if not hunk.lines:
                        break
                fp.hunks.append(hunk)
            if fp.hunks or fp.new_mode is not None:
                patches.append(fp)
        else:
            i += 1
    flush_mode_only()
    if not patches:
        raise NoPatchFound("no unified diff found in patch text")
    return patches


def _safe_target(root: Path, rel: str) -> Path:
    if rel == "/dev/null":
        raise PatchError("internal error: /dev/null is not a target")
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        raise PatchError(f"unsafe path in patch: {rel!r}")
    target = (root / p).resolve()
    if not target.is_relative_to(root.resolve()):
        raise PatchError(f"patch path escapes workspace: {rel!r}")
    return target


def _apply_hunks(current: list[str], fp: FilePatch) -> list[str]:
    lines = list(current)
    offset = 0
    for hunk in fp.hunks:
        old_seq = [text for kind, text in hunk.lines if kind in (" ", "-")]
        new_seq = [text for kind, text in hunk.lines if kind in (" ", "+")]
        expected = hunk.old_start - 1 + offset
        pos = None
        if 0 <= expected <= len(lines) - len(old_seq) \
                and lines[expected:expected + len(old_seq)] == old_seq:
            pos = expected
        else:  # line numbers may be stale; content may not — search for it
            matches = [j for j in range(len(lines) - len(old_seq) + 1)
                       if lines[j:j + len(old_seq)] == old_seq]
            if len(matches) == 1:
                pos = matches[0]
        if pos is None:
            raise PatchError(
                f"{fp.new_path}: hunk @@ -{hunk.old_start} @@ does not match "
                "the file on disk (context/removed lines differ)"
            )
        lines[pos:pos + len(old_seq)] = new_seq
        offset += len(new_seq) - len(old_seq)
    return lines


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _write_lines(path: Path, lines: list[str], trailing_newline: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines)
    if text and trailing_newline:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def apply_patch(root: str | Path, patch_text: str) -> list[str]:
    """Apply a unified diff under root; return the changed relative paths."""
    root = Path(root)
    changed: list[str] = []
    for fp in parse_patch(patch_text):
        if fp.old_path == "/dev/null":  # file creation
            target = _safe_target(root, fp.new_path)
            if target.exists():
                raise PatchError(f"cannot create {fp.new_path}: already exists")
            new_lines = _apply_hunks([], fp)
            trailing = not any(h.new_no_trailing_newline for h in fp.hunks)
            _write_lines(target, new_lines, trailing)
            if fp.new_mode is not None:
                target.chmod(fp.new_mode & 0o777)
            changed.append(fp.new_path)
        elif fp.new_path == "/dev/null":  # file deletion
            target = _safe_target(root, fp.old_path)
            if not target.is_file():
                raise PatchError(f"cannot delete {fp.old_path}: not a file")
            remaining = _apply_hunks(_read_lines(target), fp)
            if remaining:
                raise PatchError(f"deletion patch for {fp.old_path} leaves content")
            target.unlink()
            changed.append(fp.old_path)
        else:  # modification (possibly mode-only: no hunks)
            target = _safe_target(root, fp.new_path)
            if not target.is_file():
                raise PatchError(f"cannot modify {fp.new_path}: not a file")
            if fp.hunks:
                new_lines = _apply_hunks(_read_lines(target), fp)
                trailing = not any(h.new_no_trailing_newline for h in fp.hunks)
                _write_lines(target, new_lines, trailing)
            # Rewriting an existing file in place preserves its mode;
            # an explicit git mode line overrides it.
            if fp.new_mode is not None:
                target.chmod(fp.new_mode & 0o777)
            changed.append(fp.new_path)
    return changed
