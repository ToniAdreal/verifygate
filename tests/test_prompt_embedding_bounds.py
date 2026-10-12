"""Prompt-embedding bounds (v1.67.0).

``TokenFactoryAdapter.generate_patch`` embedded two parts of
``failing_evidence`` verbatim, at any size: the candidate_files
snapshot and the patch_error feedback. The repair loop caps both
at the source, but a direct caller bypassed every cap — a probe
carrying a 500,000-char file and a 500,000-char patch error
produced a 1,001,449-char prompt on a paid call. The embedding
now mirrors the loop's caps at the method: string pairs only,
per-file cap (skip whole, never truncate), total cap, and the
patch error truncated. Loop-shaped evidence is byte-identical.
"""

from verifygate.tokenfactory import (
    MAX_EMBED_FILE_CHARS,
    MAX_EMBED_PATCH_ERROR_CHARS,
    MAX_EMBED_TOTAL_CHARS,
    TokenFactoryAdapter,
)

GOOD_DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"


class CapturingTransport:
    def __init__(self):
        self.bodies = []

    def __call__(self, body):
        self.bodies.append(body)
        return {
            "model": "m",
            "choices": [{
                "message": {"content": GOOD_DIFF},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2},
        }

    @property
    def user(self):
        return self.bodies[-1]["messages"][-1]["content"]


def _adapter():
    transport = CapturingTransport()
    return TokenFactoryAdapter(model="m", transport=transport), transport


def test_caps_mirror_the_repair_snapshot():
    from verifygate.repair import (
        MAX_PATCH_ERROR_CHARS, _MAX_FILE_CHARS, _MAX_TOTAL_CHARS)
    assert MAX_EMBED_FILE_CHARS == _MAX_FILE_CHARS == 4000
    assert MAX_EMBED_TOTAL_CHARS == _MAX_TOTAL_CHARS == 12000
    assert MAX_EMBED_PATCH_ERROR_CHARS == MAX_PATCH_ERROR_CHARS == 2000


def test_small_file_embedded_verbatim():
    adapter, transport = _adapter()
    adapter.generate_patch("brief", {
        "candidate_files": {"a.py": "y = 1\n"}})
    assert "Current contents of a.py:\n```\ny = 1\n\n```" in transport.user


def test_file_at_cap_embedded_over_cap_skipped_whole():
    adapter, transport = _adapter()
    at_cap = "a" * MAX_EMBED_FILE_CHARS
    over_cap = "b" * (MAX_EMBED_FILE_CHARS + 1)
    adapter.generate_patch("brief", {
        "candidate_files": {"at.py": at_cap, "over.py": over_cap}})
    assert at_cap in transport.user
    # Skipped whole: not even a truncated prefix is embedded, so no
    # diff can be proposed against contents that do not exist.
    assert "b" * 100 not in transport.user
    assert "over.py" not in transport.user


def test_total_cap_stops_embedding():
    adapter, transport = _adapter()
    chunk = "c" * 4000
    files = {f"f{i}.py": chunk for i in range(10)}  # 40,000 chars offered
    adapter.generate_patch("brief", {"candidate_files": files})
    # Exactly three 4,000-char files fit the 12,000 total, in order.
    assert transport.user.count("Current contents of") == 3
    assert "f2.py" in transport.user
    assert "f3.py" not in transport.user
    assert len(transport.user) < 20000


def test_non_string_path_or_content_skipped():
    adapter, transport = _adapter()
    adapter.generate_patch("brief", {
        "candidate_files": {123: "x = 1\n", "bad.py": 123,
                            "none.py": None, "ok.py": "z = 2\n"}})
    assert "Current contents of ok.py" in transport.user
    assert transport.user.count("Current contents of") == 1


def test_huge_file_and_huge_patch_error_bounded():
    # The pre-fix probe: 500k file + 500k patch error -> 1,001,449
    # chars of prompt. Post-fix both are bounded at the method.
    adapter, transport = _adapter()
    adapter.generate_patch("brief", {
        "candidate_files": {"big.py": "x" * 500000},
        "patch_error": "E" * 500000})
    assert "x" * 1000 not in transport.user
    assert "E" * (MAX_EMBED_PATCH_ERROR_CHARS + 1) not in transport.user
    assert "E" * MAX_EMBED_PATCH_ERROR_CHARS in transport.user
    assert len(transport.user) < 10000


def test_patch_error_at_cap_embedded_verbatim():
    adapter, transport = _adapter()
    err = "E" * MAX_EMBED_PATCH_ERROR_CHARS
    adapter.generate_patch("brief", {"patch_error": err})
    assert err in transport.user


def test_non_string_patch_error_stringified_and_bounded():
    adapter, transport = _adapter()
    adapter.generate_patch("brief", {"patch_error": 12345})
    assert "12345" in transport.user


def test_loop_shaped_evidence_prompt_unchanged():
    # Evidence in exactly the shape the repair loop builds (small
    # text files, a short rejection) embeds everything, in order.
    adapter, transport = _adapter()
    files = {"stringtools.py": "def slugify(t):\n    return t\n",
             "cart.js": "function total() {}\n"}
    adapter.generate_patch("fix slugify", {
        "checks": [{"name": "unit_tests", "verdict": "FAIL"}],
        "candidate_files": files,
        "patch_error": "hunk does not match"})
    for path, content in files.items():
        assert f"Current contents of {path}:\n```\n{content}\n```" \
            in transport.user
    assert "hunk does not match" in transport.user


def test_generate_patch_still_returns_the_completion():
    adapter, _transport = _adapter()
    out = adapter.generate_patch("brief", {
        "candidate_files": {"big.py": "x" * 500000}})
    assert out == GOOD_DIFF
    calls = adapter.pending_calls()
    assert len(calls) == 1 and calls[0]["purpose"] == "generate_patch"
