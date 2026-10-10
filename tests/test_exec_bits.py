"""Permission-bit fidelity (v1.9.0).

Before v1.9.0 the workspace copy was content-only (``shutil.copyfile``
copies no permission bits), so an executable script arrived in the
sandbox non-executable: a check invoking it directly (``./check.sh``)
failed with exit 126 inside VerifyGate while passing on disk — the
harness judged a different tree than the one submitted. Git mode
lines in patches were likewise dropped, so a repair that created a
script could never make it runnable.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from verifygate.mock_llm import MockLLMAdapter
from verifygate.patch import apply_patch, parse_patch
from verifygate.runner import run_verification
from verifygate.sandbox import prepare_workspace
from verifygate.spec import parse_spec
from verifygate.tavily import MockReferenceLookup

SCRIPT = "#!/bin/sh\necho ok\nexit 0\n"


def _executable(path: Path) -> bool:
    return bool(path.stat().st_mode & stat.S_IXUSR)


def _candidate(tmp_path: Path, *, executable: bool) -> Path:
    cand = tmp_path / "candidate"
    cand.mkdir()
    script = cand / "check.sh"
    script.write_text(SCRIPT, encoding="utf-8")
    if executable:
        script.chmod(0o755)
    return cand


def _spec():
    return parse_spec({
        "name": "exec-bit",
        "brief": "run the script directly",
        "checks": [{"name": "direct", "command": "./check.sh",
                    "timeout_s": 10}],
    })


def _run(cand: Path) -> dict:
    return run_verification(
        spec=_spec(), candidate_dir=cand, mode="judge",
        llm=MockLLMAdapter(), references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(cand),
    )


def test_workspace_copy_preserves_executable_bit(tmp_path):
    ws = prepare_workspace(_candidate(tmp_path, executable=True))
    try:
        assert _executable(ws / "check.sh")
    finally:
        import shutil
        shutil.rmtree(ws, ignore_errors=True)


def test_workspace_copy_does_not_invent_executable_bit(tmp_path):
    ws = prepare_workspace(_candidate(tmp_path, executable=False))
    try:
        assert not _executable(ws / "check.sh")
    finally:
        import shutil
        shutil.rmtree(ws, ignore_errors=True)


def test_direct_exec_check_passes_for_executable_candidate(tmp_path):
    # The v1.9.0 bug repro: this sealed FAIL (exit 126) before the fix.
    record = _run(_candidate(tmp_path, executable=True))
    assert record["verdict"] == "PASS"


def test_direct_exec_check_fails_for_non_executable_candidate(tmp_path):
    record = _run(_candidate(tmp_path, executable=False))
    assert record["verdict"] == "FAIL"
    check = [e for e in record["entries"] if e["kind"] == "check"][0]
    assert check["exit_code"] == 126


def test_patch_new_file_mode_creates_executable_file(tmp_path):
    diff = (
        "diff --git a/run.sh b/run.sh\n"
        "new file mode 100755\n"
        "--- /dev/null\n"
        "+++ b/run.sh\n"
        "@@ -0,0 +1,2 @@\n"
        "+#!/bin/sh\n"
        "+echo hi\n"
    )
    changed = apply_patch(tmp_path, diff)
    assert changed == ["run.sh"]
    assert _executable(tmp_path / "run.sh")


def test_patch_creation_without_mode_line_stays_non_executable(tmp_path):
    diff = (
        "--- /dev/null\n"
        "+++ b/plain.sh\n"
        "@@ -0,0 +1,1 @@\n"
        "+echo hi\n"
    )
    apply_patch(tmp_path, diff)
    assert not _executable(tmp_path / "plain.sh")


def test_patch_mode_only_diff_chmods_existing_file(tmp_path):
    target = tmp_path / "run.sh"
    target.write_text(SCRIPT, encoding="utf-8")
    assert not _executable(target)
    diff = (
        "diff --git a/run.sh b/run.sh\n"
        "old mode 100644\n"
        "new mode 100755\n"
    )
    changed = apply_patch(tmp_path, diff)
    assert changed == ["run.sh"]
    assert _executable(target)
    assert target.read_text(encoding="utf-8") == SCRIPT


def test_patch_modification_preserves_existing_executable_bit(tmp_path):
    target = tmp_path / "run.sh"
    target.write_text(SCRIPT, encoding="utf-8")
    target.chmod(0o755)
    diff = (
        "--- a/run.sh\n"
        "+++ b/run.sh\n"
        "@@ -2,1 +2,1 @@\n"
        "-echo ok\n"
        "+echo still ok\n"
    )
    apply_patch(tmp_path, diff)
    assert _executable(target)


def test_parse_patch_records_new_mode():
    (fp,) = parse_patch(
        "diff --git a/run.sh b/run.sh\n"
        "old mode 100644\n"
        "new mode 100755\n"
    )
    assert fp.new_mode == 0o100755
    assert fp.hunks == []


def test_repair_created_script_can_pass_direct_exec_check(tmp_path):
    # End to end through the applier + sandbox: a mode-only chmod
    # repair turns the failing candidate into a passing one.
    cand = _candidate(tmp_path, executable=False)
    assert _run(cand)["verdict"] == "FAIL"
    ws = prepare_workspace(cand)
    try:
        apply_patch(ws, (
            "diff --git a/check.sh b/check.sh\n"
            "old mode 100644\n"
            "new mode 100755\n"
        ))
        assert os.access(ws / "check.sh", os.X_OK)
    finally:
        import shutil
        shutil.rmtree(ws, ignore_errors=True)
