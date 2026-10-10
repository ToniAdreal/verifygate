from pathlib import Path

from verifygate.sandbox import prepare_workspace
from verifygate.spec import parse_spec
from verifygate.validators import overall_verdict, run_checks


def _spec(command: str, timeout_s: int = 10):
    return parse_spec({
        "name": "t",
        "brief": "",
        "checks": [{"name": "c1", "command": command, "timeout_s": timeout_s}],
    })


def _empty_candidate(tmp_path: Path) -> Path:
    d = tmp_path / "cand"
    d.mkdir()
    (d / "placeholder.txt").write_text("x")
    return d


def test_passing_command(tmp_path):
    ws = prepare_workspace(_empty_candidate(tmp_path))
    results = run_checks(_spec("python3 -c \"print('ok')\""), ws)
    assert results[0].verdict == "PASS"
    assert results[0].exit_code == 0
    assert "ok" in results[0].stdout_tail
    assert overall_verdict(results) == "PASS"


def test_failing_command(tmp_path):
    ws = prepare_workspace(_empty_candidate(tmp_path))
    results = run_checks(_spec("python3 -c \"import sys; sys.exit(3)\""), ws)
    assert results[0].verdict == "FAIL"
    assert results[0].exit_code == 3
    assert overall_verdict(results) == "FAIL"


def test_timeout_is_a_fail_not_a_hang(tmp_path):
    ws = prepare_workspace(_empty_candidate(tmp_path))
    results = run_checks(
        _spec("python3 -c \"import time; time.sleep(30)\"", timeout_s=1), ws)
    assert results[0].timed_out is True
    assert results[0].exit_code is None
    assert results[0].verdict == "FAIL"


def test_workspace_is_a_copy(tmp_path):
    cand = _empty_candidate(tmp_path)
    ws = prepare_workspace(cand)
    assert ws != cand
    assert (ws / "placeholder.txt").exists()
