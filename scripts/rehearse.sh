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
#   5. the JavaScript sample (samples-js/): passing -> PASS, failing
#      -> FAIL, repair -> FAIL->PASS (the harness is language-agnostic;
#      these checks are Node's test runner, no Python in the candidate)
#   6. `verifygate view` on the real-mode repair report committed in
#      samples/, asserting both hash chains render as verified
#   7. dogfood: `verifygate run` on this repository itself, using the
#      committed dogfood_spec.json -> PASS, exit 0
#   8. every evidence record written during the rehearsal is verified
#      with the standalone `verifygate verify` CLI (spec binding where
#      the rehearsal's own spec applies), plus the committed real-mode
#      repair reports for the JavaScript sample (including its
#      chain-v3 report) and the chain-v2 and chain-v3 Python reports
#   9. replay: the rehearsal's own repair report and all five
#      committed real-mode repair reports are replayed in Judge Mode
#      with the standalone `verifygate replay` CLI (patched attempts
#      are reconstructed from the recorded patches; zero keys)
#  10. installed package: `pip install` the exported tree into a scratch
#      target and run the installed `verifygate` console script (not
#      `python3 -m verifygate`) from a different working directory —
#      `--version` plus a Judge Mode PASS run on the Python sample
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

echo "== 1/10 pytest suite"
python3 -m pytest -q

echo "== 2/10 run: passing candidate (expect PASS, exit 0)"
python3 -m verifygate run --spec samples/spec.json \
    --candidate samples/passing --mode judge --out "$WORK/pass.json"

echo "== 3/10 run: failing candidate (expect FAIL, exit 1)"
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

echo "== 4/10 repair: failing candidate (expect FAIL -> PASS, exit 0)"
python3 -m verifygate repair --spec samples/spec.json \
    --candidate samples/failing --mode judge --max-rounds 3 \
    --out "$WORK/repair.json" --patched-out "$WORK/patched"

echo "== 5/10 JavaScript sample: run passing + failing, repair failing"
python3 -m verifygate run --spec samples-js/spec.json \
    --candidate samples-js/passing --mode judge --out "$WORK/js-pass.json"
set +e
python3 -m verifygate run --spec samples-js/spec.json \
    --candidate samples-js/failing --mode judge --out "$WORK/js-fail.json"
rc=$?
set -e
if [ "$rc" -ne 1 ]; then
    echo "ERROR: JS failing candidate exited $rc, expected 1" >&2
    exit 1
fi
echo "   (exit 1 as expected)"
python3 -m verifygate repair --spec samples-js/spec.json \
    --candidate samples-js/failing --mode judge --max-rounds 3 \
    --out "$WORK/js-repair.json"

echo "== 6/10 view: real-mode repair report renders with valid chains"
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

echo "== 7/10 dogfood: VerifyGate verifies this repository (expect PASS, exit 0)"
python3 -m verifygate run --spec dogfood_spec.json \
    --candidate . --mode judge --out "$WORK/dogfood.json"

echo "== 8/10 verify every record written in this rehearsal (verifygate verify)"
python3 -m verifygate verify --record "$WORK/pass.json" \
    --spec samples/spec.json --candidate samples/passing
python3 -m verifygate verify --record "$WORK/fail.json" \
    --spec samples/spec.json --candidate samples/failing
python3 -m verifygate verify --record "$WORK/repair.json" \
    --spec samples/spec.json --candidate samples/failing
python3 -m verifygate verify --record "$WORK/js-pass.json" \
    --spec samples-js/spec.json --candidate samples-js/passing
python3 -m verifygate verify --record "$WORK/js-fail.json" \
    --spec samples-js/spec.json --candidate samples-js/failing
python3 -m verifygate verify --record "$WORK/js-repair.json" \
    --spec samples-js/spec.json --candidate samples-js/failing
python3 -m verifygate verify --record "$WORK/dogfood.json" \
    --spec dogfood_spec.json
python3 -m verifygate verify --record samples-js/evidence_real_repair.json \
    --spec samples-js/spec.json --candidate samples-js/failing
python3 -m verifygate verify --record samples/evidence_real_repair_v2.json \
    --spec samples/spec.json --candidate samples/failing
python3 -m verifygate verify --record samples/evidence_real_repair_v3.json \
    --spec samples/spec.json --candidate samples/failing
python3 -m verifygate verify --record samples-js/evidence_real_repair_v3.json \
    --spec samples-js/spec.json --candidate samples-js/failing

echo "== 9/10 replay: rehearsal repair report + committed real-mode reports (Judge Mode)"
python3 -m verifygate replay --record "$WORK/repair.json" \
    --spec samples/spec.json --candidate samples/failing --mode judge
python3 -m verifygate replay --record samples/evidence_real_repair.json \
    --spec samples/spec.json --candidate samples/failing --mode judge
python3 -m verifygate replay --record samples-js/evidence_real_repair.json \
    --spec samples-js/spec.json --candidate samples-js/failing --mode judge
python3 -m verifygate replay --record samples/evidence_real_repair_v2.json \
    --spec samples/spec.json --candidate samples/failing --mode judge
python3 -m verifygate replay --record samples/evidence_real_repair_v3.json \
    --spec samples/spec.json --candidate samples/failing --mode judge
python3 -m verifygate replay --record samples-js/evidence_real_repair_v3.json \
    --spec samples-js/spec.json --candidate samples-js/failing --mode judge

echo "== 10/10 installed package: pip install + console script from another directory"
# --no-build-isolation first (uses the interpreter's own setuptools,
# no downloads); fall back to a standard isolated build. Either way
# the install must succeed and the installed script must work.
python3 -m pip install --quiet --no-build-isolation \
    --target "$WORK/pkg" . \
    || python3 -m pip install --quiet --target "$WORK/pkg" .
mkdir -p "$WORK/elsewhere"
cd "$WORK/elsewhere"
PYTHONPATH="$WORK/pkg" "$WORK/pkg/bin/verifygate" --version
PYTHONPATH="$WORK/pkg" "$WORK/pkg/bin/verifygate" run \
    --spec "$WORK/tree/samples/spec.json" \
    --candidate "$WORK/tree/samples/passing" --mode judge \
    --out "$WORK/installed-pass.json"
cd "$WORK/tree"

echo "REHEARSAL PASSED — clean tree, Judge Mode spine end-to-end green."
