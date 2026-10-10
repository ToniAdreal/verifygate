from verifygate.evidence import build_record, tree_hash, verify_chain

CHECK = {
    "name": "unit_tests",
    "command": "python3 -m pytest -q",
    "exit_code": 0,
    "duration_ms": 42,
    "stdout_tail": "4 passed",
    "stderr_tail": "",
    "timed_out": False,
    "verdict": "PASS",
}


def _record():
    return build_record(
        spec_name="demo",
        spec_hash="a" * 64,
        candidate_tree_hash="b" * 64,
        mode="judge",
        check_results=[CHECK],
        verdict="PASS",
        diagnosis=None,
        replay_command="python3 -m verifygate run --spec s --candidate c",
    )


def test_record_chain_verifies():
    record = _record()
    assert record["verdict"] == "PASS"
    assert verify_chain(record) is True


def test_tampered_check_breaks_chain():
    record = _record()
    record["entries"][1]["exit_code"] = 1  # quietly rewrite history
    assert verify_chain(record) is False


def test_tampered_verdict_breaks_chain():
    record = _record()
    record["verdict"] = "FAIL"
    assert verify_chain(record) is False


def test_tree_hash_stable_and_sensitive(tmp_path):
    (tmp_path / "a.txt").write_text("one")
    h1 = tree_hash(tmp_path)
    assert h1 == tree_hash(tmp_path)
    (tmp_path / "a.txt").write_text("two")
    assert tree_hash(tmp_path) != h1
