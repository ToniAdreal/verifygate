"""Unified-diff application: strict context, safe paths, create/delete."""

import pytest

from verifygate.patch import NoPatchFound, PatchError, apply_patch, parse_patch

MODIFY_DIFF = """\
--- a/hello.py
+++ b/hello.py
@@ -1,3 +1,3 @@
 def greet():
-    return "hello"
+    return "hello, world"
"""

CREATE_DIFF = """\
--- /dev/null
+++ b/newmod.py
@@ -0,0 +1,2 @@
+VALUE = 42
+
"""

DELETE_DIFF = """\
--- a/old.py
+++ /dev/null
@@ -1,1 +0,0 @@
-VALUE = 1
"""


def test_modify_applies(tmp_path):
    (tmp_path / "hello.py").write_text('def greet():\n    return "hello"\n')
    changed = apply_patch(tmp_path, MODIFY_DIFF)
    assert changed == ["hello.py"]
    assert 'return "hello, world"' in (tmp_path / "hello.py").read_text()


def test_create_applies(tmp_path):
    changed = apply_patch(tmp_path, CREATE_DIFF)
    assert changed == ["newmod.py"]
    assert (tmp_path / "newmod.py").read_text() == "VALUE = 42\n\n"


def test_delete_applies(tmp_path):
    (tmp_path / "old.py").write_text("VALUE = 1\n")
    changed = apply_patch(tmp_path, DELETE_DIFF)
    assert changed == ["old.py"]
    assert not (tmp_path / "old.py").exists()


def test_context_mismatch_raises(tmp_path):
    (tmp_path / "hello.py").write_text('def greet():\n    return "hola"\n')
    with pytest.raises(PatchError):
        apply_patch(tmp_path, MODIFY_DIFF)


def test_prose_wrapped_diff_is_extracted(tmp_path):
    (tmp_path / "hello.py").write_text('def greet():\n    return "hello"\n')
    wrapped = "Here is the fix:\n\n```diff\n" + MODIFY_DIFF + "```\nHope it helps."
    assert apply_patch(tmp_path, wrapped) == ["hello.py"]


def test_no_diff_raises(tmp_path):
    with pytest.raises(NoPatchFound):
        apply_patch(tmp_path, "Plan: rewrite the function by hand.")


def test_path_traversal_rejected(tmp_path):
    evil = MODIFY_DIFF.replace("a/hello.py", "a/../evil.py").replace(
        "b/hello.py", "b/../evil.py")
    with pytest.raises(PatchError):
        apply_patch(tmp_path, evil)


def test_stale_line_numbers_tolerated(tmp_path):
    (tmp_path / "hello.py").write_text(
        '# one\n# two\ndef greet():\n    return "hello"\n')
    stale = MODIFY_DIFF.replace("@@ -1,3 +1,3 @@", "@@ -1,3 +1,3 @@")
    # context sits at line 3, not line 1: content search must still find it
    assert apply_patch(tmp_path, stale) == ["hello.py"]


def test_parse_counts_file_patches():
    assert len(parse_patch(MODIFY_DIFF + "\n" + CREATE_DIFF)) == 2
