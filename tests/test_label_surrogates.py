"""Labels must be encodable: lone surrogates in a label (v1.99.0).

v1.40.0 validated the spec/candidate labels for type, emptiness,
and length — but not for encodability, although a label is
*sealed*: it is interpolated into the evidence record's
``replay`` string, which the hash chain encodes as UTF-8. A
label carrying lone surrogates could therefore never be sealed
at all, and such a label is exactly how a non-UTF-8 path arrives
on POSIX: ``sys.argv`` (the CLI passes ``args.spec`` /
``args.candidate`` verbatim as the labels) and ``os.environ``
decode bytes with surrogateescape, so a path whose filename
holds non-UTF-8 bytes is a string of legal length carrying
unpaired surrogates.

Before v1.99.0 that label passed entry validation, was
shlex-quoted by ``quote_label``, and:

* the runner and the repair loop ran every check, then
  ``build_record`` refused to seal — the decided verdict's
  record/report was lost, and the error blamed the "record
  content", not the label;
* replay let the same ``ValueError`` escape its per-record
  path (which catches only ``OSError`` / ``RuntimeError``)
  mid-replay, after records had already been re-run;
* replay's derived candidate label — ``str(candidate_dir)`` —
  was never judged at all: ``validate_candidate_dir``'s
  guarantees cover the string form's type, emptiness, and
  length, but a directory whose name holds non-UTF-8 bytes is
  a working candidate (surrogateescape round-trips it through
  the filesystem) whose string form still cannot be sealed.

``validate_label`` now rejects a surrogate-bearing label with
the one ``ValueError`` at entry, before any work, and
``replay_document`` judges the derived candidate label through
it up front as well. Valid labels are byte-unchanged — in
particular a control-character label stays valid here (the
``quote_label`` placeholder contract), and non-ASCII text is
ordinary UTF-8.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from verifygate.evidence import quote_label, validate_label
from verifygate.mock_llm import MockLLMAdapter
from verifygate.repair import run_repair
from verifygate.replay import replay_document, replay_ok
from verifygate.runner import run_verification
from verifygate.spec import load_spec, parse_spec
from verifygate.tavily import MockReferenceLookup

APP = Path(__file__).resolve().parents[1]
SPEC = load_spec(APP / "samples" / "spec.json")
PASSING = APP / "samples" / "passing"
SURR = "\ud800"  # a lone high surrogate: not valid Unicode
SURR_LABEL = f"bad {SURR} label"


def _marker_spec(marker: Path):
    return parse_spec({
        "name": "marker",
        "brief": "touch a marker file",
        "checks": [{
            "name": "mark",
            "command": f"touch {marker} && true",
            "timeout_s": 5,
        }],
    })


# -- the helper itself ---------------------------------------------------


@pytest.mark.parametrize("field", ["spec_label", "candidate_label"])
def test_validate_label_rejects_surrogate_label(field):
    with pytest.raises(ValueError) as excinfo:
        validate_label(SURR_LABEL, field)
    message = str(excinfo.value)
    assert field in message
    assert "encodable as UTF-8" in message
    # The label itself is never echoed into the error.
    assert SURR not in message


@pytest.mark.parametrize("label", [
    "samples/spec.json", "my spec.json", "候选", "a'b",
    "bad\nlabel",  # control chars: placeholder contract, still valid
])
def test_validate_label_valid_labels_unchanged(label):
    assert validate_label(label) == label


def test_quote_label_rejects_surrogate_label_as_value_error():
    # The backstop: a direct caller's surrogate label is the one
    # clean ValueError, not a shlex-quoted unsealable string.
    with pytest.raises(ValueError):
        quote_label(SURR_LABEL)


# -- the runner and the repair loop: rejected before any work ------------


@pytest.mark.parametrize("field", ["spec_label", "candidate_label"])
def test_runner_rejects_surrogate_label_before_any_check(tmp_path, field):
    marker = tmp_path / "ran.txt"
    labels = {"spec_label": "spec.json", "candidate_label": "cand"}
    labels[field] = SURR_LABEL
    with pytest.raises(ValueError) as excinfo:
        run_verification(
            spec=_marker_spec(marker), candidate_dir=PASSING,
            mode="judge", llm=MockLLMAdapter(),
            references=MockReferenceLookup(), **labels)
    assert field in str(excinfo.value)
    # Before the fix the check ran and build_record refused to
    # seal afterwards; now no check has run at all.
    assert not marker.exists()


def test_repair_rejects_surrogate_label_before_any_check(tmp_path):
    marker = tmp_path / "ran.txt"
    with pytest.raises(ValueError) as excinfo:
        run_repair(
            spec=_marker_spec(marker), candidate_dir=PASSING,
            mode="judge", llm=MockLLMAdapter(),
            references=MockReferenceLookup(),
            spec_label="spec.json", candidate_label=SURR_LABEL,
            max_rounds=2)
    assert "candidate_label" in str(excinfo.value)
    assert not marker.exists()


# -- replay: judged up front, on both label routes ------------------------


def _passing_record():
    return json.loads((APP / "samples" / "evidence_passing.json").read_text())


def test_replay_rejects_surrogate_spec_label_up_front():
    with pytest.raises(ValueError) as excinfo:
        replay_document(
            _passing_record(), spec=SPEC, candidate_dir=PASSING,
            mode="judge", llm=MockLLMAdapter(),
            references=MockReferenceLookup(), spec_label=SURR_LABEL)
    assert "spec_label" in str(excinfo.value)


def test_replay_rejects_non_utf8_candidate_dir_name_up_front(tmp_path):
    # A real directory whose name holds non-UTF-8 bytes: a working
    # candidate directory, an unsealable derived label.
    raw_dir = os.fsencode(tmp_path) + b"/cand-\xff"
    shutil.copytree(os.fsencode(PASSING), raw_dir)
    candidate = Path(os.fsdecode(raw_dir))
    assert candidate.is_dir()
    with pytest.raises(ValueError) as excinfo:
        replay_document(
            _passing_record(), spec=SPEC, candidate_dir=candidate,
            mode="judge", llm=MockLLMAdapter(),
            references=MockReferenceLookup(), spec_label="spec.json")
    assert "candidate_label" in str(excinfo.value)


def test_replay_with_ordinary_labels_still_matches():
    results = replay_document(
        _passing_record(), spec=SPEC, candidate_dir=PASSING,
        mode="judge", llm=MockLLMAdapter(),
        references=MockReferenceLookup(), spec_label="samples/spec.json")
    assert replay_ok(results)


# -- the CLI: a non-UTF-8 candidate path is a usage error, exit 2 ---------


def test_cli_run_with_non_utf8_candidate_path_exits_2(tmp_path):
    raw_dir = os.fsencode(tmp_path) + b"/cand-\xff"
    shutil.copytree(os.fsencode(PASSING), raw_dir)
    candidate = os.fsdecode(raw_dir)  # surrogateescape form, as in argv
    out = tmp_path / "record.json"
    proc = subprocess.run(
        [sys.executable, "-m", "verifygate", "run",
         "--spec", "samples/spec.json", "--candidate", candidate,
         "--mode", "judge", "--out", str(out)],
        capture_output=True, text=True, cwd=APP, timeout=120)
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr
    assert "candidate_label" in proc.stderr
    assert not out.exists()
