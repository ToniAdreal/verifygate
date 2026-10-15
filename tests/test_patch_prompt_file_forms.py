"""File-form patch prompts (v1.107.0).

The generate_patch prompt taught exactly one diff shape — a small
modification hunk — and told the model not to pull docstrings or
headers into a hunk. On the first live task whose fix was deleting
a file outright (a novel candidate whose statistics.py stub shadowed
the standard library), both Nemotron Nano and Nemotron Super failed
the same way: their whole-file deletion diffs escaped the quotes of
the docstring line (and added a trailing space), so the byte-exact
applier correctly rejected them, and Super's fallback emptied the
file instead of deleting it — still a FAIL, because an empty stub
still shadows the library and the brief required the file gone.

The prompt now teaches the create and delete file forms explicitly:
the '/dev/null' header side, the whole-file hunk for a deletion
(docstring lines included, copied verbatim, never escaped), and the
fact that emptying a file is not a deletion. These tests pin that
guidance in the captured prompt, and prove end-to-end (fake
transport, zero network) that a model following it — returning a
verbatim whole-file deletion diff — repairs the shadowing task
FAIL -> PASS while the original candidate stays byte-unchanged.
"""

import json
from pathlib import Path

from verifygate.evidence import verify_chain
from verifygate.repair import run_repair
from verifygate.spec import load_spec
from verifygate.tavily import MockReferenceLookup
from verifygate.tokenfactory import TokenFactoryAdapter

STUB = (
    '"""Leftover stub. Shadows the standard library statistics."""\n'
    "\n"
    "\n"
    "def mean(xs):\n"
    "    return max(xs)\n"
    "\n"
    "\n"
    "def median(xs):\n"
    "    return max(xs)\n"
)

SCORELIB = (
    "import statistics\n"
    "\n"
    "\n"
    "def average_score(scores):\n"
    "    return statistics.mean(scores)\n"
    "\n"
    "\n"
    "def median_score(scores):\n"
    "    return statistics.median(scores)\n"
)

TESTS = (
    "from scorelib import average_score, median_score\n"
    "\n"
    "\n"
    "def test_average():\n"
    "    assert average_score([10, 20, 30]) == 20\n"
    "\n"
    "\n"
    "def test_median():\n"
    "    assert median_score([1, 2, 9]) == 2\n"
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


def _deletion_diff() -> str:
    lines = STUB.splitlines()
    body = "".join(f"-{line}\n" for line in lines)
    return (
        "--- a/statistics.py\n"
        "+++ /dev/null\n"
        f"@@ -1,{len(lines)} +0,0 @@\n"
        + body
    )


def _capture_prompt(tmp_path) -> str:
    bodies: list[dict] = []

    def transport(body):
        bodies.append(body)
        return _response(_deletion_diff(), total=100)

    adapter = TokenFactoryAdapter(transport=transport)
    adapter.generate_patch(
        "Delete the statistics.py stub.",
        {"candidate_files": {"statistics.py": STUB}},
    )
    return bodies[0]["messages"][-1]["content"]


def test_patch_prompt_teaches_the_delete_file_form(tmp_path):
    user = _capture_prompt(tmp_path)
    assert "DELETE a file" in user
    assert "'--- a/<path>' and '+++ /dev/null'" in user
    assert "the whole file" in user
    # The exact failure of the live run: escaped docstring quotes.
    assert "Never escape characters in copied lines" in user
    assert "three double-quote characters" in user
    # The second live failure, after the escaping was fixed: both
    # models collapsed the stub's double blank lines into singles.
    assert "Count blank lines exactly" in user
    assert "two blank lines around a top-level" in user


def test_patch_prompt_teaches_the_create_file_form(tmp_path):
    user = _capture_prompt(tmp_path)
    assert "CREATE a file" in user
    assert "'--- /dev/null' and '+++ b/<path>'" in user


def test_patch_prompt_says_emptying_is_not_deleting(tmp_path):
    user = _capture_prompt(tmp_path)
    assert "Emptying a file's contents is not a" in user
    # The small-hunk guidance still stands for modifications.
    assert "Keep hunks small" in user
    assert "Reason silently" in user


def test_verbatim_deletion_diff_repairs_shadowing_task(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "scorelib.py").write_text(SCORELIB)
    (candidate / "statistics.py").write_text(STUB)
    (candidate / "test_scorelib.py").write_text(TESTS)
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps({
        "name": "score-stats-shadow-fix",
        "brief": (
            "Delete the statistics.py stub that shadows the standard "
            "library; a passing candidate must not contain it."
        ),
        "checks": [
            {"name": "unit_tests",
             "command": "python3 -m pytest -q", "timeout_s": 60},
            {"name": "shadow_stub_absent",
             "command": "python3 -c \"import pathlib; "
                        "assert not pathlib.Path('statistics.py').exists()\"",
             "timeout_s": 30},
        ],
    }))
    spec = load_spec(spec_file)

    def transport(body):
        user = body["messages"][-1]["content"]
        if "unified diff" in user:
            return _response(_deletion_diff(), total=100)
        return _response("Diagnosis: statistics.py stub shadows stdlib.",
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
    assert patch["changed_files"] == ["statistics.py"]
    for a in report["attempts"]:
        assert verify_chain(a["evidence"]) is True
    # The original candidate on disk is byte-unchanged, stub included.
    assert (candidate / "statistics.py").read_text() == STUB
    assert Path(candidate / "scorelib.py").read_text() == SCORELIB
