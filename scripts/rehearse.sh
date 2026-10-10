#!/bin/sh
# VerifyGate clean-tree rehearsal.
#
# Copies the tracked tree (git archive of HEAD, i.e. exactly what a fresh
# clone of the published repository contains — no __pycache__, no stray
# evidence files) into a fresh temporary directory and runs the whole
# Judge Mode spine there, zero keys, zero network:
#
#   1. the project's own pytest suite
#   2. `verifygate run` on the passing sample candidate  -> PASS, exit 0
#   3. `verifygate run` on the failing sample candidate  -> FAIL, exit 1
#   4. `verifygate repair` on the failing candidate      -> FAIL->PASS, exit 0
#   5. `verifygate view` on the real-mode repair report committed in
#      samples/, asserting both hash chains render as verified
#   6. every evidence record written during the rehearsal is chain-checked
#      with verifygate.evidence.verify_chain()
#
# Usage: scripts/rehearse.sh   (run from anywhere; it locates the repo root)
# Exit code: 0 when every step behaves as expected, 1 otherwise.
set -eu

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT INT TERM

echo "== exporting tracked tree (git archive HEAD) -> $WORK/tree"
mkdir -p "$WORK/tree"
git -C "$REPO_ROOT" archive HEAD | tar -x -C "$WORK/tree"
cd "$WORK/tree"

echo "== 1/6 pytest suite"
python3 -m pytest -q

echo "== 2/6 run: passing candidate (expect PASS, exit 0)"
python3 -m verifygate run --spec samples/spec.json \
    --candidate samples/passing --mode judge --out "$WORK/pass.json"

echo "== 3/6 run: failing candidate (expect FAIL, exit 1)"
set +e
python3 -m verifygate run --spec samples/spec.json \
    --candidate samples/failing --mode judge --out "$WORK/fail.json"
rc=$?
set -e
if [ "$rc" -ne 1 ]; then
    echo "ERROR: failing candidate exited $rc, expected 1" >&2
    exit 1
fi
echo "   (exit 1 as expected)"

echo "== 4/6 repair: failing candidate (expect FAIL -> PASS, exit 0)"
python3 -m verifygate repair --spec samples/spec.json \
    --candidate samples/failing --mode judge --max-rounds 3 \
    --out "$WORK/repair.json" --patched-out "$WORK/patched"

echo "== 5/6 view: real-mode repair report renders with valid chains"
python3 -m verifygate view --record samples/evidence_real_repair.json \
    --out "$WORK/view.html"
python3 - "$WORK/view.html" <<'PYEOF'
import sys
html = open(sys.argv[1], encoding="utf-8").read()
assert "Hash chain verified" in html, "chain banner missing"
assert "CHAIN INVALID" not in html, "real report chain must verify"
assert "5,367" in html or "5367" in html, "sealed token usage missing"
print("   view OK: chains verified, sealed usage shown")
PYEOF

echo "== 6/6 chain-check every record written in this rehearsal"
python3 - "$WORK/pass.json" "$WORK/fail.json" "$WORK/repair.json" <<'PYEOF'
import json, sys
sys.path.insert(0, ".")
from verifygate.evidence import verify_chain

for path in sys.argv[1:]:
    data = json.load(open(path, encoding="utf-8"))
    if "attempts" in data:
        records = [a["evidence"] for a in data["attempts"]]
    else:
        records = [data]
    for record in records:
        assert verify_chain(record), f"hash chain invalid: {path}"
    print(f"   chain OK: {path}")
PYEOF

echo "REHEARSAL PASSED — clean tree, Judge Mode spine end-to-end green."
