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

Path fidelity (since v1.85.0): a header path runs to the end of
the line or to a TAB (the timestamp separator), never to the
first space — a filename may contain spaces. Before this,
``_clean_path`` cut the path at its first space, so a patch for
``my file.txt`` was parsed as a patch for ``my``: a modification
was rejected against a file that was never the target, and a
*creation* silently wrote a wrong file named ``my`` into the
patched candidate — a tree the diff never specified, which the
checks then judged as if it were the model's fix. Git's C-style
quoted paths are decoded too: git quotes any path with special
or non-ASCII bytes (``"b/caf\\303\\251.txt"`` for
``b/café.txt``) in ``diff --git`` / ``---`` / ``+++`` headers,
and the quoted form was previously taken literally, so a real
``git diff`` touching such a file could never apply at all.
The ``diff --git`` line tokenises quoted paths exactly and
splits unquoted ones on the `` b/`` boundary (preferring the
boundary whose two sides name the same file); decoding happens
*before* the ``a/``/``b/`` prefix is stripped and before
``_safe_target`` judges the path, so a quoted escape that
decodes to ``..`` is rejected exactly like a literal one.

Staged path conflicts (since v1.90.0): staging judged each
file patch independently, so a diff that staged both a file
and a path *beneath* it — creating ``x`` and ``x/y``, in either
order — staged cleanly and then failed in the commit loop,
after the first target had already been written: a raw
``FileExistsError`` / ``IsADirectoryError`` (not a
:class:`PatchError`, so it escaped the repair loop's rejection
handling and lost the whole report) over a half-applied tree —
the v1.10.0 atomicity contract broken by a shape staging never
judged. Staged targets are now checked against each other
before anything is written: no staged target may be a proper
ancestor of another staged target, whatever either one's
staged state (created, modified, or deleted). Such a diff is
a clean :class:`PatchError`, the tree is left untouched, and
the repair loop feeds the rejection back like any other.
Sibling paths, and names that merely share a string prefix
(``xy`` beside ``x/y``), are not ancestors and apply as before.

File/directory replacement (since v1.91.0): the ancestor rule
above rejected two shapes a real ``git diff`` produces — a file
replaced by a directory (delete ``x``, create ``x/y``) and a
directory replaced by a file (delete every file under ``x``,
create ``x``). Both were clean rejections, but the diff was a
valid one the applier could not honour: the creation under a
still-present file died as an "unusable path", and the creation
at a still-present directory as "already exists" — in git's
canonical path order the directory-to-file creation is even
emitted *before* the deletions that make room for it. Staging
now judges a creation against the staged world plus the set of
paths this same diff deletes: a creation beneath a deleted file
stages as a creation under nothing, and a creation at a
directory stages when every entry under that directory is
deleted by this same diff (any entry left behind — an
undeleted file, a symlink, a staged creation — keeps the
"already exists" rejection). The staged-conflict check exempts
exactly the replacement pairs (a staged-deleted ancestor with
a staged creation beneath it, a staged creation whose staged
descendants are all deletions), and the commit runs in phases
— deletions first, then removal of a replaced directory's
now-empty tree, then writes — so a replacement never meets the
shape it replaced halfway. Atomicity is unchanged: every
judgement happens in staging, before a single byte is written,
and a deletion that fails to stage fails the whole diff.

Rename and copy support (since v1.93.0): git ``rename from`` /
``rename to`` and ``copy from`` / ``copy to`` headers were
ignored entirely, and a ``---``/``+++`` pair naming two
different files was applied as a modification of the *new*
path. A pure rename or copy diff (no hunks, no mode line)
produced no file patch at all and failed as "no unified diff
found"; a rename with content changes was rejected with
"cannot modify <new>: not a file", because the file still
lives at its old path; and a rename carrying a mode change
failed the same way. The patched tree could therefore never
be the tree a rename/copy diff specified, although such diffs
are ordinary ``git diff`` output (and an ordinary repair a
model may propose for a badly named file). Renames and copies
are now parsed — from the git headers, flushed as hunk-less
file patches exactly like mode-only diffs, and from a
``---``/``+++`` pair that names two different files — and
applied in staging: the source file's lines, trailing-newline
state, line-ending style, and permission bits move (rename)
or are duplicated (copy) to the destination, any hunks apply
against the *source* content, the destination must be free
(judged with the same rules as a creation, including the
file/directory replacement exemptions), and a rename stages
the source as deleted so the phased commit unlinks it before
writing the destination. Atomicity is unchanged: a rename
whose hunks do not match the source, or whose destination is
occupied, is a clean :class:`PatchError` with the tree
untouched, and the repair loop feeds it back like any other
inapplicable diff. Replay reconstructs a renamed attempt from
the recorded patch text exactly as it does any other.

Empty-file deletion (since v1.94.0): git emits no
``---``/``+++`` pair and no hunks when an *empty* file is
deleted — the whole diff block is ``diff --git`` plus a
``deleted file mode`` line and an ``index`` line. That mode
line was not parsed at all, so such a block produced no file
patch: alone it failed as "no unified diff found", and inside
a multi-file diff it was *silently dropped* while the rest
applied and reported success — the patched tree still held a
file the diff had deleted. ``deleted file mode`` is now
parsed (its value validated by :func:`_mode_value` exactly
like the other mode lines) and flushed as a hunk-less deletion
at the next boundary, exactly like a mode-only diff; a
``---``/``+++`` pair whose new side is ``/dev/null`` with no
hunks (or whose old side is ``/dev/null`` — an empty creation
carrying no mode line) likewise stands as a file patch on its
own. A hunk-less deletion of a *non-empty* file is still a
clean :class:`PatchError` at apply time: the deletion branch
requires the hunks to account for every line on disk.

Binary patch blocks (since v1.95.0): a ``GIT binary patch``
block (what ``git diff --binary`` emits for a binary change) and
the ``Binary files a/<path> and b/<path> differ`` line (what a
plain ``git diff`` emits instead) carry no ``---``/``+++`` pair
and no hunks, so the parser produced no file patch for them.
Alone, a binary diff therefore failed misleadingly as "no
unified diff found"; inside a multi-file diff it was *silently
dropped* while the text changes applied and reported success —
the patched tree was not the tree the diff specified (the same
silent-drop shape as the v1.94.0 empty-file deletion). Both
markers are now a clean :class:`PatchError` at parse time,
naming the target when the ``diff --git`` line named one, so
the whole diff fails atomically before anything is staged or
written and the repair loop feeds the rejection back like any
other inapplicable diff. VerifyGate applies text diffs only; a
binary change is a rejection, never a partial application. The
markers are matched as whole top-level lines only, so a text
patch whose *content* adds a line reading ``GIT binary patch``
(as a ``+`` line inside a hunk) applies exactly as before.

Hunk integrity (since v1.96.0): a numbered hunk header declares
exactly how many old and new lines its body must carry, and the
parser now holds the body to that minimum. Before this, the
body was read *up to* the declared counts and a shortfall was
never checked: a hunk declaring five old and five new lines
but carrying only one of each applied as if it had declared
one, and a corrupt line inside a hunk (one starting with none
of `` `` / ``-`` / ``+``) silently ended the hunk mid-body,
dropping the rest of its lines — including its additions —
while the partial change still applied. The patched tree was
not the change the diff's own header specified, yet the checks
judged it as if it were the model's fix — the
silent-partial-application shape this module has closed at
every other level. A numbered hunk whose body ends (at end of
text, at a structural boundary, or at a corrupt line) before
its declared counts are met is now a clean :class:`PatchError`
at parse time, so the whole diff fails atomically and the
repair loop feeds the rejection back like any other
inapplicable diff. Lines arriving *after* the declared counts
are met stay tolerated exactly as before, by design: real
model patches routinely carry one trailing context line beyond
their declared counts (a committed real-mode report's applied
patch does), the counted hunk is self-consistent, and replay
reconstructs the same counted hunk — so that tolerance is
load-bearing for committed evidence, while a *shortfall* can
never be legitimate: the change the header promises is simply
not in the text. Bare ``@@`` hunks are unchanged: they declare
no counts, so their counts are still derived from the body
exactly as before, and stale *start* numbers in a numbered
header are still tolerated — content is verified against
disk, never numbers (see :func:`_apply_hunks`).

Hunk over-count (since v1.136.0): the tolerance above covered
*any* line beyond the declared counts, but only a trailing
*context* line is benign. When a header under-declares — the
live case declared ``@@ -4,1 +4,1 @@`` over a body of one
context line plus the ``-``/``+`` pair that was the entire
change — the counted reader stopped at the context line and
the change lines beyond the counts were silently dropped:
the fragment applied as a no-op, the file was still reported
as changed, the patched tree was byte-identical to the
original (same tree hash), and the repair loop burned its
rounds re-verifying an unchanged candidate while the model
believed its fix had landed. Change lines beyond a numbered
hunk's declared counts are now a clean :class:`PatchError`
at parse time, like a shortfall; trailing context lines
remain tolerated — and are now consumed, where before the
first excess line ended the file's hunk loop and every
later hunk of that file was silently dropped with it — and
bare hunks are unchanged.

Context-only hunks (since v1.139.0): a hunk whose body carries
no ``-``/``+`` change line at all — every line context — can
never be a real change, yet it satisfied its declared counts
and applied as a no-op that still reported the file as changed.
The live case is a file whose own lines begin with ``-`` (an
Erlang ``-define`` header): both live models space-prefixed
every line instead of doubling the marker (``--define...``
for a removal, ``+-define...`` for an addition), so the
counted reader saw only context, the patched tree stayed
byte-identical, and the loop burned its rounds. A hunk with
no change lines is now a clean :class:`PatchError` at parse
time, fed back like any other inapplicable diff.

File-type mode validation (since v1.92.0): a git mode line was
honoured for its permission bits only — any octal value was
masked with ``& 0o777`` at commit — so a mode naming a
non-regular file was silently mangled into a regular one: a
``new file mode 120000`` (symlink) creation produced a regular
file holding the link target as text with mode ``000``, and
gitlink (``160000``) and directory (``040000``) modes did the
same; a malformed (non-octal) mode token was dropped silently
and the patch applied without its mode line at all. The
patched tree was not the tree the diff specified, yet the
checks judged it as if it were the model's fix. Mode lines
now validate at parse time (see :func:`_mode_value`): only
regular-file modes (type ``100000``) are accepted, with their
permission bits honoured exactly as before; anything else is
a clean :class:`PatchError` rejection, fed back by the repair
loop like any other inapplicable diff.

Hardlink fidelity (since v1.87.0): the commit never writes a
shared inode in place. ``_safe_target`` judges a patch path by
its resolved location, but a target file can share its inode
with a file outside the workspace (or with a second name inside
it) through a hardlink, and ``write_text`` truncates that shared
inode — patching the target rewrote the outside file too. The
commit unlinks a multiply-linked target before writing, so the
patched path gets its own inode, the other names keep their
content, and the pre-break permission bits are preserved. This
matches the workspace copy's semantics, which materialises
every verified file as its own inode.

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

Unencodable patch text (v1.97.0): two character shapes in a
diff could never be applied, yet escaped as raw exceptions —
not :class:`PatchError` — and killed the repair instead of
being fed back. A target *path* containing a NUL byte (a model
can emit one directly, or inside a git-quoted path as the
``\\000`` octal escape) reached ``os.stat`` / ``open`` during
staging and raised ``ValueError: embedded null byte``; no
filesystem path can contain a NUL, so the diff names a file
that can never exist. Diff text containing a *lone surrogate*
(a ``\\ud800`` escape decoded from a model response) reached
the applier's UTF-8 text writes and raised
``UnicodeEncodeError`` the same way. Both are now rejected at
parse time: the surrogate check runs over the whole diff text
before any header is read, and :func:`_clean_path` /
:func:`_clean_plain_path` reject a NUL in every cleaned path
(``---`` / ``+++`` lines, ``diff --git`` lines, and the
rename/copy headers alike) — before anything is staged or
written, so the repair loop feeds the rejection back like any
other inapplicable diff. NUL bytes in file *content* are
untouched: a text file may legitimately contain them, and the
checks judge the result as always.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from . import sandbox
from .jsonio import has_lone_surrogates


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
    is_rename: bool = False  # git rename headers seen (v1.93.0)
    is_copy: bool = False  # git copy headers seen (v1.93.0)


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# Real models sometimes emit a bare "@@" header with no line numbers.
# Such hunks are accepted by deriving the counts from the hunk body;
# application still verifies every context/removed line against the
# file on disk and locates the hunk by unique content search, so a
# bare header weakens nothing.
_BARE_HUNK_RE = re.compile(r"^@@\s*$|^@@\s.*@@\s*$")


_C_ESCAPES = {
    "a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11,
    "\\": 92, '"': 34,
}


def _unquote_git_path(path: str) -> str:
    """Decode a git C-style quoted path (v1.85.0).

    Git quotes paths that carry special or non-ASCII bytes in
    ``diff --git`` / ``---`` / ``+++`` headers, e.g.
    ``"b/caf\\303\\251.txt"`` for ``b/café.txt``. The quoted form
    is a byte string: octal escapes carry the raw UTF-8 bytes, so
    decoding goes through bytes, never per-character.
    """
    if len(path) < 2 or not path.startswith('"'):
        return path
    end = path.find('"', 1)
    # The caller has already cut the token at its closing quote
    # (diff --git line) or the quote runs to the end of the header
    # (---/+++ line). Anything malformed is returned as-is, so it
    # fails as an ordinary missing-file rejection downstream.
    body = path[1:end] if end != -1 else path[1:]
    if end == -1:
        return path
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in "01234567":
                j = i + 1
                value = 0
                while j < len(body) and j < i + 4 and body[j] in "01234567":
                    value = value * 8 + int(body[j])
                    j += 1
                out.append(value & 0xFF)
                i = j
                continue
            if nxt in _C_ESCAPES:
                out.append(_C_ESCAPES[nxt])
                i += 2
                continue
            out.extend(nxt.encode("utf-8"))
            i += 2
            continue
        out.extend(ch.encode("utf-8"))
        i += 1
    return bytes(out).decode("utf-8", errors="surrogateescape")


def _reject_nul_path(path: str) -> None:
    """Reject a patch target path carrying a NUL byte (v1.97.0).

    No filesystem path can contain one — paths cross the syscall
    boundary as NUL-terminated strings — so the diff names a
    file that can never exist, and every filesystem call on it
    would raise a raw ``ValueError`` outside the PatchError
    contract. The message names only the rule, never the path
    (the path is model-supplied text carrying a control byte).
    """
    if "\x00" in path:
        raise PatchError(
            "patch names a target path containing a NUL byte; "
            "no filesystem path can contain one, so the diff "
            "names a file that can never exist — re-emit the "
            "patch with a real path")


def _clean_path(raw: str) -> str:
    # A header path runs to the end of the line or to a TAB, which
    # is how both git and GNU diff separate the timestamp that may
    # follow it. A SPACE is part of the filename (v1.85.0): before
    # this the path was also cut at the first space, so a patch
    # for "my file.txt" was parsed as a patch for "my" — a
    # modification was rejected against a file that was never the
    # target, and a creation silently wrote the wrong file.
    path = raw.strip().split("\t")[0].rstrip()
    path = _unquote_git_path(path)
    _reject_nul_path(path)
    if path == "/dev/null":
        return path
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def _clean_plain_path(raw: str) -> str:
    """Clean a ``rename from`` / ``copy to`` style path (v1.93.0).

    Those git headers carry the path with no ``a/``/``b/``
    prefix, so only the TAB / end-of-line cut and the C-style
    unquoting of :func:`_clean_path` apply — never the prefix
    strip (a renamed file may legitimately live under ``a/``).
    The NUL rejection applies here too (v1.97.0).
    """
    path = raw.strip().split("\t")[0].rstrip()
    path = _unquote_git_path(path)
    _reject_nul_path(path)
    return path


def _split_git_paths(rest: str) -> tuple[str, str] | None:
    """Split the two paths of a ``diff --git`` line (v1.85.0).

    Quoted paths tokenise exactly. Unquoted paths are separated
    by the `` b/`` boundary: a rename keeps its two different
    names under the first such boundary, and when several
    boundaries are possible the one whose two sides name the
    same file (the ordinary modify / mode-only case) wins. The
    ``---``/``+++`` pair overrides these paths for content diffs
    in any case; they are load-bearing only for mode-only diffs.
    """
    rest = rest.strip()
    if not rest:
        return None
    if rest.startswith('"'):
        end = 1
        while end < len(rest):
            if rest[end] == "\\":
                end += 2
                continue
            if rest[end] == '"':
                break
            end += 1
        if end >= len(rest):
            return None
        first, remainder = rest[:end + 1], rest[end + 1:].strip()
        if not remainder:
            return None
        return _clean_path(first), _clean_path(remainder)
    candidates = []
    start = 0
    while True:
        idx = rest.find(" b/", start)
        if idx == -1:
            break
        candidates.append(idx)
        start = idx + 1
    if not candidates:
        return None
    chosen = candidates[0]
    for idx in candidates:
        left, right = rest[:idx], rest[idx + 1:]
        if left.startswith("a/") and left[2:] == right[2:]:
            chosen = idx
            break
    return _clean_path(rest[:chosen]), _clean_path(rest[chosen + 1:])


_FILE_TYPE_MASK = 0o170000
_REGULAR_FILE_TYPE = 0o100000
_MODE_KIND_NAMES = {
    0o120000: "a symlink",
    0o160000: "a gitlink (submodule)",
    0o040000: "a directory",
}


def _mode_value(line: str) -> int:
    """Parse and validate a git mode line's value (v1.92.0).

    Only regular-file modes can be honoured: the applier writes
    regular text files, and the workspace copy materialises every
    verified file as one. Before this, any octal value was taken
    at face value and its permission bits were applied with
    ``mode & 0o777`` at commit, so a ``new file mode 120000``
    (symlink) creation silently produced a *regular* file —
    containing the link target as its text, with mode ``000`` —
    instead of the symlink the diff specified, and a gitlink
    (``160000``) or directory (``040000``) mode did the same.
    The patched tree was not the tree the diff described, yet
    the checks judged it as if it were. A malformed (non-octal)
    mode token was likewise silently dropped: the mode line
    vanished and the patch applied without it. A non-regular or
    malformed mode is now a :class:`PatchError` at parse time,
    so the repair loop feeds the rejection back like any other
    inapplicable diff and nothing is staged or written.
    Regular modes keep their permission bits exactly as before
    (``100644`` / ``100755`` and any other ``100xxx`` form).
    """
    parts = line.split()
    token = parts[-1] if parts else ""
    try:
        value = int(token, 8)
    except ValueError:
        raise PatchError(
            f"malformed mode in patch: {line.strip()!r}") from None
    file_type = value & _FILE_TYPE_MASK
    if file_type != _REGULAR_FILE_TYPE:
        kind = _MODE_KIND_NAMES.get(
            file_type, "a non-regular file")
        raise PatchError(
            f"unsupported mode {value:06o} in patch: it names "
            f"{kind}, and VerifyGate patches regular text files "
            "only — a symlink, gitlink or directory cannot be "
            "created or re-typed by a patch")
    return value


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
    if has_lone_surrogates(text):
        # Unencodable text (v1.97.0): a patch is applied as UTF-8
        # text, so a diff carrying lone surrogates — reachable via
        # a \ud800 escape in a model response — could only crash
        # the applier's writes with a UnicodeEncodeError, outside
        # the PatchError contract. Rejected whole, before any
        # header is read, so nothing is staged or written.
        raise PatchError(
            "patch text contains lone surrogates — characters "
            "that are not valid Unicode and cannot be encoded as "
            "UTF-8; a patch is applied as UTF-8 text, so such a "
            "diff can never be applied — re-emit it with valid "
            "text")
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
    # Git rename/copy headers since the last file patch (v1.93.0):
    # a rename or copy with no ---/+++ pair (a pure rename, or a
    # rename carrying only a mode change) is flushed as a
    # hunk-less FilePatch at the next boundary, exactly like a
    # mode-only diff.
    pending_rename: tuple[str, str] | None = None
    pending_rename_from: str | None = None
    pending_copy: tuple[str, str] | None = None
    pending_copy_from: str | None = None
    # A `deleted file mode` line since the last file patch
    # (v1.94.0): git emits no ---/+++ pair and no hunks when an
    # empty file is deleted, so the deletion exists only in
    # this header and is flushed as a hunk-less FilePatch at
    # the next boundary, exactly like a mode-only diff.
    pending_delete = False

    def flush_mode_only() -> None:
        nonlocal git_paths, pending_mode, pending_create
        nonlocal pending_rename, pending_rename_from
        nonlocal pending_copy, pending_copy_from
        nonlocal pending_delete
        if pending_rename is not None:
            patches.append(FilePatch(
                old_path=pending_rename[0],
                new_path=pending_rename[1],
                hunks=[],
                new_mode=pending_mode,
                is_rename=True,
            ))
        elif pending_copy is not None:
            patches.append(FilePatch(
                old_path=pending_copy[0],
                new_path=pending_copy[1],
                hunks=[],
                new_mode=pending_mode,
                is_copy=True,
            ))
        elif pending_delete and git_paths is not None:
            patches.append(FilePatch(
                old_path=git_paths[0],
                new_path="/dev/null",
                hunks=[],
            ))
        elif git_paths is not None and pending_mode is not None:
            patches.append(FilePatch(
                old_path="/dev/null" if pending_create else git_paths[0],
                new_path=git_paths[1],
                hunks=[],
                new_mode=pending_mode,
            ))
        git_paths = None
        pending_mode = None
        pending_create = False
        pending_rename = None
        pending_rename_from = None
        pending_copy = None
        pending_copy_from = None
        pending_delete = False

    i = 0
    while i < len(lines):
        if lines[i] == "GIT binary patch" or (
                lines[i].startswith("Binary files ")
                and lines[i].endswith(" differ")):
            # A binary change carries no text hunks the applier
            # could honour (v1.95.0): reject the whole diff at
            # parse time instead of silently dropping this block
            # while the rest applies. Hunk content lines never
            # reach this loop (they are consumed by the hunk
            # reader), so a "+" line adding this exact text to a
            # file is unaffected.
            target = f" for {git_paths[1]!r}" if git_paths else ""
            raise PatchError(
                f"binary patch{target} cannot be applied: "
                "VerifyGate applies text (unified) diffs only; "
                "propose the change as a text diff or leave the "
                "binary file unchanged")
        if lines[i].startswith("diff --git "):
            flush_mode_only()
            git_paths = _split_git_paths(lines[i][len("diff --git "):])
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
        if lines[i].startswith("deleted file mode "):
            # Validated like every other mode line (v1.92.0):
            # a non-regular or malformed value is a parse-time
            # PatchError. The bits themselves name the file
            # being deleted and are not applied anywhere.
            _mode_value(lines[i])
            pending_delete = True
            i += 1
            continue
        if lines[i].startswith("rename from "):
            pending_rename_from = _clean_plain_path(
                lines[i][len("rename from "):])
            i += 1
            continue
        if lines[i].startswith("rename to "):
            if pending_rename_from is not None:
                pending_rename = (
                    pending_rename_from,
                    _clean_plain_path(lines[i][len("rename to "):]),
                )
            i += 1
            continue
        if lines[i].startswith("copy from "):
            pending_copy_from = _clean_plain_path(
                lines[i][len("copy from "):])
            i += 1
            continue
        if lines[i].startswith("copy to "):
            if pending_copy_from is not None:
                pending_copy = (
                    pending_copy_from,
                    _clean_plain_path(lines[i][len("copy to "):]),
                )
            i += 1
            continue
        if lines[i].startswith("--- ") and i + 1 < len(lines) \
                and lines[i + 1].startswith("+++ "):
            # A pending rename/copy whose paths are NOT this
            # header pair's is a separate, earlier file patch
            # (a pure rename block followed by a modification of
            # the new name, as a model may emit): flush it first
            # so the patches stay in emission order (v1.93.0).
            pair = (_clean_path(lines[i][4:]),
                    _clean_path(lines[i + 1][4:]))
            if (pending_rename is not None and pending_rename != pair) \
                    or (pending_copy is not None
                        and pending_copy != pair):
                flush_mode_only()
            if pending_delete and git_paths is not None and not (
                    pair[1] == "/dev/null" and pair[0] == git_paths[0]):
                # A pending empty-file deletion from an earlier
                # block whose pair this is not: flush it first so
                # it is neither lost nor attributed to this pair.
                flush_mode_only()
            pair_is_delete = pending_delete
            fp = FilePatch(
                old_path=_clean_path(lines[i][4:]),
                new_path=_clean_path(lines[i + 1][4:]),
                new_mode=pending_mode,
                is_rename=pending_rename is not None,
                is_copy=pending_copy is not None,
            )
            git_paths = None
            pending_mode = None
            pending_create = False
            pending_rename = None
            pending_rename_from = None
            pending_copy = None
            pending_copy_from = None
            pending_delete = False
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
                if not bare:
                    # Hunk over-count (v1.136.0): change lines
                    # arriving *after* the declared counts are met
                    # are not a trailing context line — they are
                    # the change itself, and the header has
                    # under-declared it. The counted reader above
                    # stops at the counts, so those lines were
                    # silently dropped while the counted fragment
                    # (context only, in the live case) applied as
                    # a no-op that still reported the file as
                    # changed: the repaired tree was byte-identical
                    # to the original, its tree hash unchanged,
                    # and the repair loop burned its rounds
                    # re-verifying an unchanged candidate. Peek at
                    # the body-like lines that follow (structural
                    # lines — the next hunk, the next file's
                    # ---/+++ pair, a diff --git header — end the
                    # peek): any '-'/'+' line among them rejects
                    # the whole diff at parse time, exactly like a
                    # shortfall. Trailing context lines carry no
                    # change and stay tolerated, as before.
                    excess_old = excess_new = 0
                    j = i
                    while j < len(lines):
                        peek = lines[j]
                        if peek.startswith(("--- ", "+++ ", "@@",
                                            "diff --git ")):
                            break
                        if peek.startswith("\\"):
                            j += 1
                            continue
                        kind = peek[0] if peek else " "
                        if kind not in (" ", "-", "+"):
                            break
                        if kind == "-":
                            excess_old += 1
                        elif kind == "+":
                            excess_new += 1
                        j += 1
                    if excess_old or excess_new:
                        raise PatchError(
                            f"{fp.new_path}: corrupt patch: hunk "
                            f"@@ -{hunk.old_start},{hunk.old_count} "
                            f"+{hunk.new_start},{hunk.new_count} @@ "
                            f"declares {hunk.old_count} old and "
                            f"{hunk.new_count} new lines, but its "
                            f"body carries {excess_old} more old "
                            f"and {excess_new} more new change "
                            "lines beyond the declared counts")
                    # Context-only excess: consume it. Before
                    # this, the unparsed excess line ended the
                    # file's hunk loop, so every later hunk of
                    # the same file was silently dropped too —
                    # the same silent-drop shape, one level up.
                    # The excess context itself is still not
                    # applied; the counted hunk above is the
                    # whole change, exactly as before.
                    i = j
                    # Hunk integrity (v1.96.0): a numbered header's
                    # declared counts are the hunk's contract. A
                    # body that ends short of them — at the end of
                    # the text, at a structural boundary, or at a
                    # corrupt line the reader stopped on — means
                    # the diff does not contain the change it
                    # would partially apply: reject the whole
                    # diff at parse time instead of silently
                    # applying the fragment that arrived. (Lines
                    # beyond the counts stay ignored, as before:
                    # see the module docstring.)
                    if need_old > 0 or need_new > 0:
                        raise PatchError(
                            f"{fp.new_path}: corrupt patch: hunk "
                            f"@@ -{hunk.old_start},{hunk.old_count} "
                            f"+{hunk.new_start},{hunk.new_count} @@ "
                            f"declares {hunk.old_count} old and "
                            f"{hunk.new_count} new lines, but its "
                            f"body carries only "
                            f"{hunk.old_count - need_old} old and "
                            f"{hunk.new_count - need_new} new "
                            "lines before it ends")
                if bare:
                    hunk.old_count = sum(
                        1 for k, _ in hunk.lines if k in (" ", "-"))
                    hunk.new_count = sum(
                        1 for k, _ in hunk.lines if k in (" ", "+"))
                    if not hunk.lines:
                        break
                # Context-only hunk (v1.139.0): a hunk carrying
                # no '-'/'+' line changes nothing, yet it used to
                # satisfy its counts and apply as a no-op that
                # reported the file as changed -- see the module
                # docstring. Reject it at parse time so the
                # repair loop can feed the rejection back.
                if hunk.lines and not any(
                        k in ("-", "+") for k, _ in hunk.lines):
                    raise PatchError(
                        f"{fp.new_path}: corrupt patch: hunk "
                        f"@@ -{hunk.old_start},{hunk.old_count} "
                        f"+{hunk.new_start},{hunk.new_count} @@ "
                        "carries no change lines (every line is "
                        "context), so it would change nothing; "
                        "lines to remove must start with '-' and "
                        "lines to add with '+' (a file line that "
                        "itself starts with '-' is removed as "
                        "'--...')")
                fp.hunks.append(hunk)
            if fp.hunks or fp.new_mode is not None or pair_is_delete \
                    or fp.old_path == "/dev/null" \
                    or fp.new_path == "/dev/null":
                # A hunk-less pair still stands on its own when it
                # names a creation or a deletion (v1.94.0): an
                # empty file has no lines for a hunk to carry.
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


def _check_staged_path_conflicts(
    root: Path,
    staged: dict[Path, _Staged],
) -> None:
    """Reject staged targets that are ancestors of other staged targets.

    Staged path conflicts (v1.90.0, see the module docstring):
    a file and a directory cannot occupy the same path, so a
    diff staging both ``x`` and ``x/y`` — as creations, or one
    as a modification and the other as a creation — can never
    be committed as staged: the commit would write one target
    and then fail on the other, half-applying the diff with a
    raw ``OSError``. Judged here, after the whole diff has
    staged and before anything is written, it is a clean
    :class:`PatchError` and the tree is left untouched. The
    comparison is by path components, never by string prefix:
    ``xy`` is not an ancestor of ``x/y``.
    """
    root_resolved = root.resolve()
    rels: dict[Path, str] = {}
    for target in staged:
        try:
            rels[target] = target.relative_to(root_resolved).as_posix()
        except ValueError:
            continue  # not under root: judged by _safe_target already
    targets = sorted(rels, key=lambda t: rels[t])
    for i, ancestor in enumerate(targets):
        ancestor_parts = ancestor.relative_to(root_resolved).parts
        for descendant in targets[i + 1:]:
            parts = descendant.relative_to(root_resolved).parts
            if len(parts) > len(ancestor_parts) \
                    and parts[:len(ancestor_parts)] == ancestor_parts:
                # File/directory replacement (v1.91.0): the pair
                # is committable exactly when one side is a staged
                # deletion — a deleted file with a creation
                # beneath it (file replaced by a directory), or
                # a created file whose descendants are all
                # deletions (a directory replaced by a file, its
                # entries staged away). The commit's phases order
                # the two so they never coexist on disk.
                if staged[ancestor].exists != staged[descendant].exists:
                    continue
                raise PatchError(
                    f"patch stages both {rels[ancestor]} and "
                    f"{rels[descendant]}: a file and a path beneath "
                    f"it cannot both be written")


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

    def _is_dir(target: Path) -> bool:
        try:
            return target.is_dir()
        except OSError:
            # Same unstatable-target rule as _is_file: an
            # over-long component raises ENAMETOOLONG out of
            # is_dir instead of returning False, and such a
            # target is not a directory the patch can replace —
            # _exists turns it into a PatchError rejection.
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

    file_patches = parse_patch(patch_text)
    # Paths this same diff deletes, resolved up front (v1.91.0):
    # a file/directory replacement's creation is judged against
    # the world the diff leaves behind, and in git's canonical
    # path order a directory-to-file creation is emitted before
    # the deletions that make room for it. A deletion named here
    # still has to stage successfully below — if it does not,
    # the whole diff fails in staging and nothing is written.
    deleted_targets: set[Path] = {
        _safe_target(root, fp.old_path)
        for fp in file_patches if fp.new_path == "/dev/null"
    }

    def _beneath_deleted_ancestor(target: Path) -> bool:
        """True iff a proper ancestor of target is deleted by this diff."""
        root_resolved = root.resolve()
        try:
            parts = target.relative_to(root_resolved).parts
        except ValueError:
            return False
        for i in range(1, len(parts)):
            ancestor = root_resolved.joinpath(*parts[:i])
            if ancestor in deleted_targets:
                return True
            state = staged.get(ancestor)
            if state is not None and not state.exists:
                return True
        return False

    def _directory_replaced(target: Path) -> bool:
        """True iff every entry under the directory target is deleted.

        Judged against the staged deletions and the paths this
        diff deletes (v1.91.0): any entry left behind — a file
        the diff does not delete, a symlink, a staged creation —
        means the directory survives and a file cannot take
        its place.
        """
        for state_target, state in staged.items():
            if state.exists and state_target != target \
                    and target in state_target.parents:
                return False
        for dirpath, dirnames, filenames in os.walk(target):
            entries = list(filenames)
            entries += [name for name in dirnames
                        if (Path(dirpath) / name).is_symlink()]
            for name in entries:
                entry = Path(dirpath) / name
                state = staged.get(entry)
                if entry in deleted_targets:
                    continue
                if state is not None and not state.exists:
                    continue
                return False
        return True

    for fp in file_patches:
        if fp.old_path == "/dev/null":  # file creation
            target = _safe_target(root, fp.new_path)
            existing = staged.get(target)
            if existing is not None:
                if existing.exists:
                    raise PatchError(
                        f"cannot create {fp.new_path}: already exists")
            elif _beneath_deleted_ancestor(target):
                pass  # the ancestor file is deleted by this diff:
                # in the staged world the path beneath it is free
            elif _is_dir(target) and not target.is_symlink():
                # A directory stands where the file must go: only
                # a full replacement (every entry deleted by this
                # same diff) makes room for it (v1.91.0).
                if not _directory_replaced(target):
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
        elif fp.old_path != fp.new_path or fp.is_rename or fp.is_copy:
            # Rename or copy (v1.93.0): the content lives at the
            # OLD path. Before this, every such patch was applied
            # as a modification of the new path and rejected with
            # "not a file", and a pure rename/copy produced no
            # file patch at all. The source is loaded, the hunks
            # (if any) apply against its content, and the result
            # is staged at the destination — which must be free,
            # judged exactly as a creation is, including the
            # file/directory replacement exemptions. A rename
            # additionally stages the source as deleted; a copy
            # leaves it untouched. The destination inherits the
            # source's trailing-newline state, line-ending style,
            # and permission bits unless a mode line overrides
            # the bits.
            source = _safe_target(root, fp.old_path)
            dest = _safe_target(root, fp.new_path)
            verb = "copy" if fp.is_copy else "rename"
            if source == dest:
                state = load(dest, fp.new_path, "modify")
                if fp.hunks:
                    state.lines, eof_no_newline = _apply_hunks(
                        state.lines, fp)
                    if eof_no_newline is not None:
                        state.trailing_newline = not eof_no_newline
                if fp.new_mode is not None:
                    state.mode = fp.new_mode
                changed.append(fp.new_path)
                continue
            source_state = load(source, fp.old_path, verb)
            existing_dest = staged.get(dest)
            if existing_dest is not None:
                if existing_dest.exists:
                    raise PatchError(
                        f"cannot {verb} to {fp.new_path}: already exists")
            elif _beneath_deleted_ancestor(dest):
                pass  # freed by a deletion in this same diff
            elif _is_dir(dest) and not dest.is_symlink():
                if not _directory_replaced(dest):
                    raise PatchError(
                        f"cannot {verb} to {fp.new_path}: already exists")
            elif _exists(dest, fp.new_path):
                raise PatchError(
                    f"cannot {verb} to {fp.new_path}: already exists")
            source_mode = source_state.mode
            if source_mode is None:
                try:
                    source_mode = stat.S_IMODE(source.lstat().st_mode)
                except OSError:
                    source_mode = None
            new_lines, eof_no_newline = _apply_hunks(
                list(source_state.lines), fp)
            staged[dest] = _Staged(
                exists=True,
                lines=new_lines,
                trailing_newline=not eof_no_newline
                if eof_no_newline is not None
                else source_state.trailing_newline,
                mode=fp.new_mode if fp.new_mode is not None
                else source_mode,
                line_ending=source_state.line_ending,
            )
            if not fp.is_copy:
                source_state.exists = False
                source_state.lines = []
                source_state.mode = None
                changed.append(fp.old_path)
            changed.append(fp.new_path)
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

    _check_staged_path_conflicts(root, staged)

    # The staged result must stay within the candidate size bounds,
    # checked here — after the whole diff applied cleanly in staging
    # and before a single byte is written — so an oversize patch is
    # a clean, atomic rejection, never a half-grown tree that the
    # next verification's pre-flight then destroys the run over.
    _check_result_bounds(root, staged, max_files, max_bytes)

    # Commit: the whole diff validated in staging, so write it out.
    # The commit runs in phases (v1.91.0) so a file/directory
    # replacement never meets the shape it replaced halfway:
    # deletions land first (a deleted file frees the path beneath
    # it), then a replaced directory's now-empty tree is removed
    # (its entries were all deleted in phase one), then the
    # writes — whose parent creation now finds no file in the way.
    for target, state in staged.items():
        if not state.exists:
            target.unlink(missing_ok=True)
    for target, state in staged.items():
        if state.exists and not target.is_symlink():
            try:
                is_dir = target.is_dir()
            except OSError:
                is_dir = False
            if is_dir:
                sandbox.remove_tree(target)
    for target, state in staged.items():
        if not state.exists:
            continue
        # Hardlink break (v1.87.0): a target whose inode is shared
        # (st_nlink > 1) must not be written in place — write_text
        # truncates the shared inode, so a file outside the root
        # hardlinked to the target (or a second name for it inside
        # the root) would be rewritten too, although _safe_target
        # judged only this path. Unlinking first gives the target
        # its own inode; the other names keep the old content,
        # exactly as the workspace copy — which always materialises
        # each verified file as its own inode — behaves. The
        # pre-break permission bits are the fallback mode, since a
        # fresh file would otherwise take the umask default instead
        # of the mode in-place writing preserves.
        fallback_mode: int | None = None
        try:
            info = target.lstat()
        except OSError:
            info = None
        if info is not None and stat.S_ISREG(info.st_mode) \
                and info.st_nlink > 1:
            fallback_mode = stat.S_IMODE(info.st_mode)
            target.unlink()
        _write_lines(
            target, state.lines, state.trailing_newline, state.line_ending)
        if state.mode is not None:
            target.chmod(state.mode & 0o777)
        elif fallback_mode is not None:
            target.chmod(fallback_mode)
    return changed
