"""Patched-out write-through protection (v1.88.0).

The v1.86.0 alias scan protects the artifacts a repair reads —
the candidate, the spec, the report — by judging every entry of
the ``--patched-out`` destination against *their* inodes. It
does not protect anyone else's file. A destination entry on a
path the copy will actually write that is a symlink to an
unrelated file (or a directory link on the way to a copied
file), or a regular file hardlinked to an unrelated name,
passed the scan, and ``shutil.copytree`` then wrote the patched
file *through* it: the file at the other end of the link,
outside the destination tree, came back holding patched
content while the CLI reported a successful copy (both shapes
reproduced against v1.87.0). Immediately before the copy — the
first moment the patched tree, and therefore the written
paths, is known — those paths are scanned: a symlink component
on a written path, or a multiply-linked regular file at a
written path, is an output error (exit 2) and nothing is
copied. An alias on a path the copy will not write is not
judged, and a plain pre-existing file on a written path is
still overwritten, exactly as v1.86.0 pinned.
"""

import os
import shutil
from pathlib import Path

from verifygate.cli import _patched_out_write_through_conflict, main

APP = Path(__file__).resolve().parent.parent
SPEC = APP / "samples" / "spec.json"
FAILING = APP / "samples" / "failing"


def _copy_sample_tree(tmp_path):
    spec = tmp_path / "spec.json"
    shutil.copy(SPEC, spec)
    failing = tmp_path / "failing"
    shutil.copytree(FAILING, failing)
    return spec, failing


def _source_tree(tmp_path):
    source = tmp_path / "source"
    (source / "sub").mkdir(parents=True)
    (source / "stringtools.py").write_text("patched", encoding="utf-8")
    (source / "sub" / "inner.py").write_text("inner", encoding="utf-8")
    return source


# -- helper -------------------------------------------------------------------


def test_write_through_helper_clean_and_missing_destinations(tmp_path):
    source = _source_tree(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "stringtools.py").write_text("stale", encoding="utf-8")
    assert _patched_out_write_through_conflict(dest, source) is None
    assert _patched_out_write_through_conflict(
        tmp_path / "not-yet", source) is None


def test_write_through_helper_names_symlinked_file(tmp_path):
    source = _source_tree(tmp_path)
    vault = tmp_path / "vault.py"
    vault.write_text("vault", encoding="utf-8")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "stringtools.py").symlink_to(vault)
    conflict = _patched_out_write_through_conflict(dest, source)
    assert conflict is not None and "symlink" in conflict


def test_write_through_helper_names_symlinked_directory(tmp_path):
    source = _source_tree(tmp_path)
    vault_dir = tmp_path / "vault-dir"
    vault_dir.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "sub").symlink_to(vault_dir, target_is_directory=True)
    conflict = _patched_out_write_through_conflict(dest, source)
    assert conflict is not None and "symlink" in conflict


def test_write_through_helper_names_hardlinked_file(tmp_path):
    source = _source_tree(tmp_path)
    vault = tmp_path / "vault.py"
    vault.write_text("vault", encoding="utf-8")
    dest = tmp_path / "dest"
    dest.mkdir()
    os.link(vault, dest / "stringtools.py")
    conflict = _patched_out_write_through_conflict(dest, source)
    assert conflict is not None and "hardlinked" in conflict


def test_write_through_helper_ignores_unwritten_alias_paths(tmp_path):
    source = _source_tree(tmp_path)
    vault = tmp_path / "vault.txt"
    vault.write_text("vault", encoding="utf-8")
    dest = tmp_path / "dest"
    dest.mkdir()
    os.link(vault, dest / "unrelated.txt")
    (dest / "other.txt").symlink_to(vault)
    assert _patched_out_write_through_conflict(dest, source) is None


# -- CLI ------------------------------------------------------------------------


def test_repair_patched_out_symlink_to_unrelated_file_is_output_error(
        tmp_path):
    spec, failing = _copy_sample_tree(tmp_path)
    vault = tmp_path / "vault.py"
    vault.write_text("VAULT ORIGINAL", encoding="utf-8")
    dest = tmp_path / "patched"
    dest.mkdir()
    (dest / "stringtools.py").symlink_to(vault)
    before = (failing / "stringtools.py").read_bytes()
    rc = main(["repair", "--spec", str(spec), "--candidate", str(failing),
               "--mode", "judge", "--out", str(tmp_path / "r.json"),
               "--patched-out", str(dest)])
    assert rc == 2
    # The report is still delivered; only the copy is refused.
    assert (tmp_path / "r.json").exists()
    assert vault.read_text(encoding="utf-8") == "VAULT ORIGINAL"
    assert (dest / "stringtools.py").is_symlink()
    assert (failing / "stringtools.py").read_bytes() == before


def test_repair_patched_out_hardlink_to_unrelated_file_is_output_error(
        tmp_path):
    spec, failing = _copy_sample_tree(tmp_path)
    vault = tmp_path / "vault.py"
    vault.write_text("VAULT ORIGINAL", encoding="utf-8")
    dest = tmp_path / "patched"
    dest.mkdir()
    os.link(vault, dest / "stringtools.py")
    before = (failing / "stringtools.py").read_bytes()
    rc = main(["repair", "--spec", str(spec), "--candidate", str(failing),
               "--mode", "judge", "--out", str(tmp_path / "r.json"),
               "--patched-out", str(dest)])
    assert rc == 2
    assert (tmp_path / "r.json").exists()
    assert vault.read_text(encoding="utf-8") == "VAULT ORIGINAL"
    assert (dest / "stringtools.py").read_text(
        encoding="utf-8") == "VAULT ORIGINAL"
    assert (failing / "stringtools.py").read_bytes() == before


def test_repair_patched_out_with_unwritten_symlink_still_works(tmp_path):
    spec, failing = _copy_sample_tree(tmp_path)
    vault = tmp_path / "vault.txt"
    vault.write_text("keep me", encoding="utf-8")
    dest = tmp_path / "patched"
    dest.mkdir()
    (dest / "unrelated.txt").symlink_to(vault)
    rc = main(["repair", "--spec", str(spec), "--candidate", str(failing),
               "--mode", "judge", "--out", str(tmp_path / "r.json"),
               "--patched-out", str(dest)])
    assert rc == 0
    assert (dest / "stringtools.py").exists()
    assert not (dest / "stringtools.py").is_symlink()
    assert vault.read_text(encoding="utf-8") == "keep me"
