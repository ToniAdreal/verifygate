"""Mode-form patch prompts (v1.108.0).

The generate_patch prompt taught the modification, create, and
delete file forms (v1.107.0), but never the mode-only form. On the
first live task whose fix was a permission change alone — a sale
announcement script whose content was already correct but which
lacked the executable bit, so direct invocation failed with exit
126 — Nemotron Nano spent three rounds proposing content edits:
appending a chmod line inside the script (which can never run,
because the missing permission is what prevents the script from
running at all) and duplicating its own diff; every proposal was
correctly rejected and the run ended FAIL, max_rounds, 22,697
tokens, the original candidate byte- and mode-unchanged.

The prompt now teaches the mode-only form explicitly: a
'diff --git' line plus 'old mode'/'new mode', no '---'/'+++'
lines and no hunks, and the fact that a content edit never changes
a file's mode. These tests pin that guidance in the captured
prompt, and prove end-to-end (fake transport, zero network) that
a model following it — returning a verbatim mode-only diff —
repairs the announcement task FAIL -> PASS while the original
candidate stays byte- and mode-unchanged.
"""

import json
import os
from pathlib import Path

from verifygate.evidence import verify_chain
from verifygate.repair import run_repair
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.tokenfactory import TokenFactoryAdapter

SCRIPT = (
    "#!/bin/sh\n"
    'echo "SALE: everything 20 percent off today"\n'
)

REPRO = (
    "import os, subprocess\n"
    "p = os.path.join(os.path.dirname(os.path.abspath(__file__)),"
    " 'announce.sh')\n"
    "assert os.access(p, os.X_OK), 'announce.sh is not executable'\n"
    "out = subprocess.run(['./announce.sh'],"
    " cwd=os.path.dirname(p), capture_output=True, text=True)\n"
    "assert out.returncode == 0, out.stderr\n"
    "assert out.stdout.strip() =="
    " 'SALE: everything 20 percent off today', out.stdout\n"
    "print('repro ok')\n"
)

MODE_DIFF = (
    "diff --git a/announce.sh b/announce.sh\n"
    "old mode 100644\n"
    "new mode 100755\n"
)


def _response(content: str, total: int = 100) -> dict:
    return {
        "model": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
        "choices": [{
            "message": {"content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": total - 40,
            "completion_tokens": 40,
            "total_tokens": total,
        },
    }


def _capture_prompt() -> str:
    bodies: list[dict] = []

    def transport(body):
        bodies.append(body)
        return _response(MODE_DIFF, total=100)

    adapter = TokenFactoryAdapter(transport=transport)
    adapter.generate_patch(
        "Make announce.sh executable; its content must not change.",
        {"candidate_files": {"announce.sh": SCRIPT}},
    )
    return bodies[0]["messages"][-1]["content"]


def test_patch_prompt_teaches_the_mode_only_form():
    user = _capture_prompt()
    assert "change ONLY a file's permissions" in user
    assert "'diff --git a/<path> b/<path>'" in user
    assert "'old mode 100644'" in user
    assert "'new mode 100755'" in user
    assert "NO '---'/'+++' lines and NO" in user


def test_patch_prompt_says_a_content_edit_never_changes_the_mode():
    user = _capture_prompt()
    assert "a content edit never changes a file's mode" in user
    assert "prevents the script from running at all" in user
    # The earlier forms and the small-hunk guidance still stand.
    assert "CREATE a file" in user
    assert "DELETE a file" in user
    assert "Keep hunks small" in user
    assert "Reason silently" in user


def test_mode_only_diff_repairs_announcement_task(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    script = candidate / "announce.sh"
    script.write_text(SCRIPT)
    os.chmod(script, 0o644)
    (candidate / "repro.py").write_text(REPRO)
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps({
        "name": "announce-exec-bit-fix",
        "brief": (
            "announce.sh must be executable directly as "
            "./announce.sh; its content is already correct and "
            "must not change."
        ),
        "checks": [
            {"name": "direct_run",
             "command": "./announce.sh", "timeout_s": 30},
            {"name": "repro_script",
             "command": "python3 repro.py", "timeout_s": 30},
        ],
    }))
    spec = load_spec(spec_file)

    def transport(body):
        user = body["messages"][-1]["content"]
        if "unified diff" in user:
            return _response(MODE_DIFF, total=100)
        return _response(
            "Diagnosis: announce.sh lacks the executable bit.",
            total=100)

    adapter = TokenFactoryAdapter(transport=transport)
    report = run_repair(
        spec=spec, candidate_dir=candidate, mode="real",
        llm=adapter, references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label="candidate",
        max_rounds=3,
    )
    assert [a["verdict"] for a in report["attempts"]] == ["FAIL", "PASS"]
    assert report["final_verdict"] == "PASS"
    patch = report["attempts"][1]["patch_from_previous"]
    assert patch["applied"] is True
    assert patch["changed_files"] == ["announce.sh"]
    for a in report["attempts"]:
        assert verify_chain(a["evidence"]) is True
    # The patched copy is executable; the original candidate is
    # byte- and mode-unchanged.
    patched = Path(report["attempts"][1]["candidate_dir"])
    assert os.access(patched / "announce.sh", os.X_OK)
    assert script.read_text() == SCRIPT
    assert not os.access(script, os.X_OK)
