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

Trailing-newline fidelity (since v1.82.0): the ``\\ No newline at
end of file`` marker is honoured on both sides of a diff and the
file's trailing-newline state changes only when a hunk actually
reaches the end of the file. Before this, the parser dropped the
marker that follows a hunk's *final* line (it arrives only once
the hunk's line counts are exhausted), and application rewrote
the trailing state from the surviving markers alone — so a patch
whose new side must end without a newline silently gained one,
and a patch that touched only the first lines of a file without
a trailing newline added one at its end too: the patched tree
was not the tree the diff specified.

Line-ending fidelity (since v1.83.0): a modified file keeps its
own line-ending style. Before this, lines were read with
``splitlines()`` and always written back joined by ``\\n``, so
patching one line of a CRLF (Windows-style) file silently
rewrote every line ending in it — and a mode-only commit on a
CRLF file did the same while changing no content at all. The
staged state now records the file's dominant line ending
(``\\r\\n`` when CRLF breaks are at least as common as lone-LF
breaks) and the commit writes that ending back; a created file
takes the patch text's own ending (CRLF when the diff itself
arrives in CRLF, LF otherwise). Mixed-ending files follow the
dominant-ending rule, documented here rather than guessed
per line.

Application is **atomic across files** (since v1.10.0): every file
patch is validated and applied to an in-memory staged copy first,
and the workspace on disk is written only after the *whole* diff has
applied cleanly in staging. Before this, files were written one by
one as their patches were processed, so a multi-file diff whose
second file failed left the first file already rewritten — a
half-applied patch, contradicting the "never partially forced in"
contract above for any caller that did not itself discard the whole
workspace (the repair loop and replay happened to, by discipline).
Sequential patches to the same file apply in order against the
staged state, exactly as they would have against disk.

A patch targeting a **binary (non-UTF-8) file** raises
:class:`PatchError` like any other inapplicable diff (since
v1.11.0): reading the target previously surfaced a raw
``UnicodeDecodeError``, which is not a ``PatchError``, so the
repair loop — which only catches ``PatchError`` — crashed on it
instead of feeding the rejection back and stopping cleanly. The
harness verifies text source files; a diff against binary content
is a clean rejection, never a crash.

Patch-input bound (since v1.37.0): the patch *text itself* is
bounded by :data:`MAX_PATCH_TEXT_CHARS`, enforced in
:func:`parse_patch` before a single line is parsed. Before this,
the repair loop *applied* a proposal of any size but *stored*
only its first 8,000 characters in the repair report — so a
valid proposal longer than the stored cap applied, passed, and
sealed a PASS report whose recorded patch was a truncated
fragment: ``verify`` reported the report INTACT (the fragment
is within the stored cap), yet ``verifygate replay`` could not
reconstruct the attempt from it (reproduced with a 9,413-char
valid diff: replay's attempt-1 tree and verdict did not match).
An applied patch must be the patch the report carries, in
full, or the record's replay promise is false. The ceiling is
therefore now the *application* ceiling as well as the stored
ceiling, and it is generous: 65,536 characters covers a full
maximum-token model response (the real adapter's patch budget
is 8,192 tokens) with headroom, and the committed real-mode
patches peak at 571 characters. A longer proposal raises
:class:`PatchError` like any other inapplicable diff — the
repair loop preserves it (truncated to the same cap) as a
rejected patch, feeds the size error back, and the model can
retry with a smaller diff within ``max_rounds``; replay only
ever applies stored patches, which are within the cap by
construction. A non-string patch text is likewise a
:class:`PatchError`, never a ``TypeError`` from ``len`` /
``splitlines``.

Patch-result bounds (since v1.25.0): the *result* of a patch is
bounded by the sandbox's candidate ceilings
(``MAX_CANDIDATE_FILES`` / ``MAX_CANDIDATE_BYTES``). Before this,
:func:`apply_patch` staged and wrote a diff of any size, so a
patch could grow the candidate past the very bounds v1.23.0 put
on submitted candidates — and in the repair loop the patched copy
then failed ``prepare_workspace``'s pre-flight on re-verification,
whose ``CandidateTooLargeError`` is not a ``PatchError``: it
escaped the loop, destroyed the report (attempt 0's already-sealed
FAIL evidence included), and surfaced at the CLI as a usage error
(exit 2, nothing written) for a run whose verdict was already
decided. The staged result is now measured against the ceilings
*before* the commit loop writes anything — counting exactly the
files the containment traversal verifies, with staged creations /
modifications / deletions folded over the on-disk baseline — and
an oversize result raises :class:`PatchError` like any other
inapplicable diff: atomicity is preserved (nothing is written),
the repair loop feeds the rejection back and stops cleanly with
its report delivered, and replay reports it as a patch that no
longer applies.

Bound-override validation (since v1.53.0): the per-call
``max_files`` / ``max_bytes`` overrides of :func:`apply_patch`
are validated through ``sandbox.validate_candidate_bound``
before anything is parsed, staged, or written — a real ``int``
between 0 and the sandbox ceiling in force, exactly as
``sandbox.check_candidate_bounds`` judges its own overrides.
Before this, a string override crashed the staged-result check
with a ``TypeError`` — not a :class:`PatchError`, so it escaped
the repair loop's rejection handling — ``True`` ran silently as
a one-file bound, a float compared as-is, and an enormous
override silently disabled the patch-result bound for that call.
A malformed override is now one ``ValueError`` up front (it is a
usage error about the *call*, not a rejection of the *patch*, so
it is deliberately not a :class:`PatchError`).

Root validation (since v1.57.0): the ``root`` a patch applies
under was the one workspace argument the applier still took on
trust — every other entry point had its directory judged
(candidate directories in v1.41.0, ``run_command``'s ``cwd``
in v1.51.0), but ``apply_patch`` coerced its root with a bare
``Path(...)``. A non-path root (``None``, an ``int``, a
``bool``) crashed with a raw ``TypeError`` — again not a
:class:`PatchError`, so it escaped the repair loop's rejection
handling for any direct caller. An empty string silently meant
the *current directory*: the staged-result bounds check then
measured the caller's whole working tree as the "candidate",
and only that tree happening to exceed the file bound prevented
a write into it. Worst, a *missing* root was silently created:
a creation diff's parent-directory creation materialised the
root itself, so the applier grew a brand-new tree wherever the
path pointed instead of patching the candidate the caller
named. The root is now validated through
``sandbox.validate_candidate_dir`` exactly as the run paths
judge theirs, before anything is parsed, staged, or written:
type / emptiness / length faults are one ``ValueError`` (a
usage error about the *call*, deliberately not a
:class:`PatchError` — the same doctrine as the bound overrides
above), and a well-formed path that does not name an existing
directory is the ``FileNotFoundError`` every other entry point
raises. The repair loop and replay only ever pass prepared
workspaces, so their behaviour is unchanged; directory
creation *inside* a validated root (a new file in a new
subdirectory) is unchanged too.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import sandbox


# The patch-proposal ceiling (v1.37.0): the largest patch text
# parse_patch / apply_patch will accept, and — because repair.py
# re-exports it as the repair report's stored cap — also the
# largest patch a report can carry. One value for both, by
# construction: an applied patch is always stored in full, so a
# report's recorded patches always reconstruct their attempts
# under replay. Generous, not a target (see the module docstring).
MAX_PATCH_TEXT_CHARS = 65_536


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
    old_no_trailing_newline: bool = False


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


def _note_no_newline_marker(hunk: Hunk) -> None:
    """Attribute a ``\\ No newline at end of file`` marker (v1.82.0).

    The marker describes the line it follows: a context line is on
    both sides of the diff, a ``+`` line only on the new side, a
    ``-`` line only on the old side.
    """
    if not hunk.lines:
        return
    kind = hunk.lines[-1][0]
    if kind in (" ", "+"):
        hunk.new_no_trailing_newline = True
    if kind in (" ", "-"):
        hunk.old_no_trailing_newline = True


def parse_patch(text: str) -> list[FilePatch]:
    """Extract file patches from (possibly prose-wrapped) unified diff text."""
    if not isinstance(text, str):
        raise PatchError(
            f"patch text must be a string, not {type(text).__name__}")
    if len(text) > MAX_PATCH_TEXT_CHARS:
        raise PatchError(
            f"patch text exceeds the maximum of {MAX_PATCH_TEXT_CHARS} "
            f"characters (got {len(text)}); a patch VerifyGate applies "
            "must fit, in full, in the repair report that records it — "
            "propose a smaller diff")
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
                        _note_no_newline_marker(hunk)
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
                if i < len(lines) and lines[i].startswith("\\"):
                    # The marker after a hunk's *final* line arrives
                    # only once the line counts are exhausted, so the
                    # loop above never sees it (v1.82.0): consume it
                    # here, or a patch that must leave the file with
                    # no trailing newline silently gains one.
                    _note_no_newline_marker(hunk)
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
    try:
        target = (root / p).resolve()
    except (OSError, RuntimeError) as exc:
        # An unresolvable path (a symlink loop, an over-long
        # component) is a diff that cannot apply, not a crash —
        # the same contract _read_lines keeps for binary targets.
        raise PatchError(f"unusable path in patch: {rel!r}") from exc
    if not target.is_relative_to(root.resolve()):
        raise PatchError(f"patch path escapes workspace: {rel!r}")
    return target


def _apply_hunks(
    current: list[str], fp: FilePatch
) -> tuple[list[str], bool | None]:
    """Apply one file patch's hunks to ``current``.

    Returns the new lines and the hunk-determined trailing-newline
    state of the file's end (v1.82.0): ``True`` when the last hunk
    that reaches the end of the file leaves it *without* a trailing
    newline (its final new-side line carries the ``\\ No newline``
    marker), ``False`` when such a hunk leaves it *with* one, and
    ``None`` when no hunk reaches the end of the file — the end is
    then untouched and the caller preserves the state it already
    had. Before this, the marker after a hunk's final line was
    dropped by the parser and every modification rewrote the
    trailing state from the markers alone, so a file without a
    trailing newline gained one whether or not the patch touched
    its end.
    """
    lines = list(current)
    offset = 0
    eof_no_newline: bool | None = None
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
        if pos + len(old_seq) == len(lines):
            # This hunk reaches the (current) end of the file, so
            # its new side decides the trailing-newline state: a
            # marker on the new side means no trailing newline; a
            # marker only on the old side (a deleted final line)
            # means the surviving last line was newline-terminated.
            eof_no_newline = hunk.new_no_trailing_newline
        lines[pos:pos + len(old_seq)] = new_seq
        offset += len(new_seq) - len(old_seq)
    return lines, eof_no_newline


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError) as exc:
        # A binary target is a diff that cannot apply, and the
        # contract is that inapplicable diffs raise PatchError —
        # a raw UnicodeDecodeError escaped the repair loop's
        # PatchError handling and crashed the run.
        raise PatchError(
            f"cannot patch {path.name}: not a UTF-8 text file") from exc


def _dominant_line_ending(raw: bytes) -> str:
    """The line ending a file predominantly uses (v1.83.0).

    CRLF wins ties: a file whose breaks are half CRLF is a CRLF
    file with some stray LF lines, and normalising it to LF would
    rewrite the endings its author chose for the other half.
    Lone CR (the classic Mac ending) counts as an ending of its
    own (v1.84.0): a CR file's breaks are neither CRLF nor LF,
    and before this it fell through to LF and was rewritten
    whole, exactly like the pre-v1.83.0 CRLF case. CR wins ties
    against LF for the same reason CRLF does.
    """
    crlf = raw.count(b"\r\n")
    lone_lf = raw.count(b"\n") - crlf
    lone_cr = raw.count(b"\r") - crlf
    if crlf and crlf >= lone_lf and crlf >= lone_cr:
        return "\r\n"
    if lone_cr and lone_cr >= lone_lf:
        return "\r"
    return "\n"


def _write_lines(
    path: Path,
    lines: list[str],
    trailing_newline: bool = True,
    line_ending: str = "\n",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = line_ending.join(lines)
    if text and trailing_newline:
        text += line_ending
    path.write_text(text, encoding="utf-8")


@dataclass
class _Staged:
    """One file's staged post-patch state (nothing on disk yet)."""

    exists: bool
    lines: list[str] = field(default_factory=list)
    trailing_newline: bool = True
    mode: int | None = None  # chmod to apply at commit; None = keep
    line_ending: str = "\n"  # the file's own ending, written back (v1.83.0)


def _staged_bytes(state: _Staged) -> int:
    """Byte size of a staged file's content as the commit writes it."""
    text = state.line_ending.join(state.lines)
    if text and state.trailing_newline:
        text += state.line_ending
    return len(text.encode("utf-8"))


def _counts_toward_bounds(rel: str) -> bool:
    """True iff the containment traversal would verify ``rel``.

    Mirrors :func:`sandbox.iter_candidate_files`: housekeeping
    names are skipped at every level and ``*.pyc`` files are
    ignored — exactly the set ``check_candidate_bounds`` counts.
    """
    parts = rel.split("/")
    return not any(p in sandbox._IGNORED_NAMES for p in parts) \
        and not parts[-1].endswith(".pyc")


def _check_result_bounds(
    root: Path,
    staged: dict[Path, _Staged],
    max_files: int,
    max_bytes: int,
) -> None:
    """Reject a staged result that outgrows the candidate ceilings.

    The on-disk baseline is the containment traversal's verified
    set; staged entries fold over it (a creation/modification sets
    the file's post-patch size, a deletion removes it). Raises
    :class:`PatchError` — before anything is written — when the
    resulting candidate would exceed either ceiling.
    """
    resulting: dict[str, int] = {}
    if root.is_dir():
        for rel, source in sandbox.iter_candidate_files(root):
            try:
                resulting[rel] = source.stat().st_size
            except OSError:
                resulting[rel] = 0
    root_resolved = root.resolve()
    for target, state in staged.items():
        try:
            rel = target.relative_to(root_resolved).as_posix()
        except ValueError:
            continue  # not under root: cannot be a verified file
        if not _counts_toward_bounds(rel):
            continue
        if state.exists:
            resulting[rel] = _staged_bytes(state)
        else:
            resulting.pop(rel, None)
    if len(resulting) > max_files:
        raise PatchError(
            f"patch would grow the candidate to {len(resulting)} "
            f"verified files, beyond the maximum of {max_files} "
            "(the candidate size bound in sandbox.py)")
    total = sum(resulting.values())
    if total > max_bytes:
        raise PatchError(
            f"patch would grow the candidate to {total} verified "
            f"bytes, beyond the maximum of {max_bytes} "
            "(the candidate size bound in sandbox.py)")


def apply_patch(
    root: str | Path,
    patch_text: str,
    *,
    max_files: int | None = None,
    max_bytes: int | None = None,
) -> list[str]:
    """Apply a unified diff under root; return the changed relative paths.

    All-or-nothing: every file patch is validated against an
    in-memory staging overlay first; disk writes happen only in the
    commit loop at the end, after the whole diff has applied cleanly
    in staging. A :class:`PatchError` therefore leaves the tree
    byte-for-byte and mode-for-mode as it was.

    The staged result must also stay within the candidate size
    bounds (``max_files`` / ``max_bytes``, defaulting to the
    sandbox ceilings at call time — see the module docstring): a
    patch that would grow the candidate past them is a
    :class:`PatchError`, raised before any write.

    The patch text itself must be a string within
    :data:`MAX_PATCH_TEXT_CHARS` (enforced in :func:`parse_patch`):
    an oversize or non-string proposal is a :class:`PatchError`
    before anything is parsed, staged, or written.

    The root must name an existing directory (validated through
    ``sandbox.validate_candidate_dir``, v1.57.0 — see the module
    docstring): a non-path, empty, or over-long root is a
    ``ValueError`` and a well-formed path that does not name a
    directory is a ``FileNotFoundError``, both before anything is
    parsed, staged, or written. The root is never created,
    and an empty string never means the current directory.
    """
    if max_files is None:
        max_files = sandbox.MAX_CANDIDATE_FILES
    else:
        max_files = sandbox.validate_candidate_bound(
            max_files, "max_files", sandbox.MAX_CANDIDATE_FILES)
    if max_bytes is None:
        max_bytes = sandbox.MAX_CANDIDATE_BYTES
    else:
        max_bytes = sandbox.validate_candidate_bound(
            max_bytes, "max_bytes", sandbox.MAX_CANDIDATE_BYTES)
    # The root is the caller's own argument, judged like every
    # other entry point's directory (v1.57.0): until now a bare
    # Path(...) coercion crashed non-path roots with a TypeError,
    # read an empty string as the current directory, and let a
    # creation diff silently materialise a missing root.
    root = sandbox.validate_candidate_dir(root, name="root")
    changed: list[str] = []
    staged: dict[Path, _Staged] = {}

    def _is_file(target: Path) -> bool:
        try:
            return target.is_file()
        except OSError:
            # A target that cannot even be stat'ed (an over-long
            # name component raises ENAMETOOLONG, not a clean
            # False) is not a file the patch can modify — the
            # caller turns that into a PatchError rejection.
            return False

    def _exists(target: Path, rel: str) -> bool:
        try:
            os.stat(target)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            # Not missing — unusable (an over-long component, a
            # path through a regular file): creating it would only
            # fail mid-commit, after other files were written, so
            # reject it here, before anything is staged.
            raise PatchError(f"cannot create {rel}: unusable path") from exc

    def load(target: Path, rel: str, what: str) -> _Staged:
        if target in staged:
            state = staged[target]
        elif _is_file(target):
            raw = target.read_bytes()
            state = _Staged(
                exists=True,
                lines=_read_lines(target),
                # Preserve the file's trailing-newline state so a
                # mode-only commit rewrites byte-identical content.
                trailing_newline=not raw or raw.endswith((b"\n", b"\r")),
                # Preserve the file's line-ending style too
                # (v1.83.0): a CRLF file stays CRLF, line for line.
                line_ending=_dominant_line_ending(raw),
            )
            staged[target] = state
        else:
            raise PatchError(f"cannot {what} {rel}: not a file")
        if not state.exists:
            raise PatchError(f"cannot {what} {rel}: not a file")
        return state

    for fp in parse_patch(patch_text):
        if fp.old_path == "/dev/null":  # file creation
            target = _safe_target(root, fp.new_path)
            existing = staged.get(target)
            if existing is not None:
                if existing.exists:
                    raise PatchError(
                        f"cannot create {fp.new_path}: already exists")
            elif _exists(target, fp.new_path):
                raise PatchError(f"cannot create {fp.new_path}: already exists")
            new_lines, eof_no_newline = _apply_hunks([], fp)
            staged[target] = _Staged(
                exists=True,
                lines=new_lines,
                trailing_newline=not eof_no_newline
                if eof_no_newline is not None else True,
                mode=fp.new_mode,
                # A created file has no on-disk style to preserve:
                # it takes the patch text's own ending (v1.83.0);
                # a diff carried entirely in CR separators gives a
                # CR file (v1.84.0).
                line_ending=("\r\n" if "\r\n" in patch_text
                             else "\r" if "\r" in patch_text
                             and "\n" not in patch_text else "\n"),
            )
            changed.append(fp.new_path)
        elif fp.new_path == "/dev/null":  # file deletion
            target = _safe_target(root, fp.old_path)
            state = load(target, fp.old_path, "delete")
            remaining, _eof = _apply_hunks(state.lines, fp)
            if remaining:
                raise PatchError(f"deletion patch for {fp.old_path} leaves content")
            state.exists = False
            state.lines = []
            state.mode = None
            changed.append(fp.old_path)
        else:  # modification (possibly mode-only: no hunks)
            target = _safe_target(root, fp.new_path)
            state = load(target, fp.new_path, "modify")
            if fp.hunks:
                state.lines, eof_no_newline = _apply_hunks(state.lines, fp)
                if eof_no_newline is not None:
                    state.trailing_newline = not eof_no_newline
                # No hunk reached the end of the file: its trailing
                # state stays whatever load() read from disk (v1.82.0).
            # Rewriting an existing file in place preserves its mode;
            # an explicit git mode line overrides it.
            if fp.new_mode is not None:
                state.mode = fp.new_mode
            changed.append(fp.new_path)

    # The staged result must stay within the candidate size bounds,
    # checked here — after the whole diff applied cleanly in staging
    # and before a single byte is written — so an oversize patch is
    # a clean, atomic rejection, never a half-grown tree that the
    # next verification's pre-flight then destroys the run over.
    _check_result_bounds(root, staged, max_files, max_bytes)

    # Commit: the whole diff validated in staging, so write it out.
    for target, state in staged.items():
        if not state.exists:
            target.unlink(missing_ok=True)
            continue
        _write_lines(
            target, state.lines, state.trailing_newline, state.line_ending)
        if state.mode is not None:
            target.chmod(state.mode & 0o777)
    return changed
