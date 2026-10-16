"""Prompt-snapshot Perl module coverage (v1.118.0).

The repair loop's patch prompt carries a snapshot of the candidate's
small text files (``repair._candidate_files``). Perl modules were
invisible to it: ``.pl`` is whitelisted but the snapshot matches the
full suffix, so ``Rates.pm`` matched nothing and the file to be
patched never reached the prompt. On the first live candidate whose
fix landed in a .pm module the real model named the right file from
the brief, invented its contents, and had every diff correctly
rejected (``patch_not_applicable``). These tests pin the inclusion,
the unchanged whitelist posture for unlisted/binary files, and an
end-to-end repair whose stub adapter can only build its diff from
the snapshot it is handed.
"""

import shutil

import pytest

from verifygate.evidence import verify_chain
from verifygate.llm import LLMAdapter
from verifygate.repair import (
    _TEXT_SUFFIXES,
    _candidate_files,
    run_repair,
)
from verifygate.spec import parse_spec
from verifygate.tavily import MockReferenceLookup

RATES_PM = (
    "package Rates;\n"
    "use strict;\n"
    "use warnings;\n"
    "\n"
    "# Published discount rules: >= 200 -> 20% off, >= 100 -> 10% off,\n"
    "# >= 50 -> 5% off, otherwise no discount.\n"
    "sub discount_rate {\n"
    "    my ($subtotal) = @_;\n"
    "    if ($subtotal >= 200) {\n"
    "        return 0.20;\n"
    "    } elsif ($subtotal >= 100) {\n"
    "        return 0.01;\n"
    "    } elsif ($subtotal >= 50) {\n"
    "        return 0.15;\n"
    "    } else {\n"
    "        return 0.0;\n"
    "    }\n"
    "}\n"
    "\n"
    "1;\n"
)

EXTRA_PM = "package Extra;\nuse strict;\nuse warnings;\n1;\n"

CHECKOUT_PL = (
    "#!/usr/bin/env perl\n"
    "use strict;\n"
    "use warnings;\n"
    "use FindBin qw($Bin);\n"
    "use lib $Bin;\n"
    "use Rates;\n"
    "\n"
    "my $subtotal = $ARGV[0];\n"
    "die \"usage: checkout.pl SUBTOTAL\\n\" unless defined $subtotal;\n"
    "my $rate = Rates::discount_rate($subtotal);\n"
    "printf \"%.2f\\n\", $subtotal * (1.0 - $rate);\n"
)


def test_perl_module_is_a_snapshot_suffix():
    assert ".pm" in _TEXT_SUFFIXES


def test_snapshot_includes_perl_module_files_verbatim(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "Rates.pm").write_text(RATES_PM)
    (cand / "Extra.pm").write_text(EXTRA_PM)
    (cand / "checkout.pl").write_text(CHECKOUT_PL)
    files = _candidate_files(cand)
    assert files["Rates.pm"] == RATES_PM
    assert files["Extra.pm"] == EXTRA_PM
    assert files["checkout.pl"] == CHECKOUT_PL


def test_snapshot_still_skips_unlisted_and_binary_files(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "blob.bin").write_text("plain text, unlisted suffix\n")
    (cand / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")
    (cand / "notes").write_text("extensionless, unlisted basename\n")
    (cand / "ok.py").write_text("x = 1\n")
    files = _candidate_files(cand)
    assert files == {"ok.py": "x = 1\n"}


class _SnapshotPerlStub(LLMAdapter):
    """Builds its diff from the snapshot, exactly like a real model must.

    If ``Rates.pm`` is absent from the candidate_files snapshot the
    stub has nothing truthful to diff against and returns no diff --
    the pre-v1.118.0 live failure shape.
    """

    name = "snapshot-perl-stub"

    def __init__(self):
        self.seen_snapshots = []

    def generate_patch(self, brief, failing_evidence=None):
        files = (failing_evidence or {}).get("candidate_files") or {}
        self.seen_snapshots.append(sorted(files))
        text = files.get("Rates.pm")
        if text is None:
            return "Plan only: the pm file was not shown to me."
        lines = text.splitlines()
        out = ["--- a/Rates.pm", "+++ b/Rates.pm"]
        for old, new in (
            ("        return 0.01;", "        return 0.10;"),
            ("        return 0.15;", "        return 0.05;"),
        ):
            idx = next(i for i, ln in enumerate(lines) if ln == old)
            out.append(f"@@ -{idx + 1},1 +{idx + 1},1 @@")
            out.append(f"-{old}")
            out.append(f"+{new}")
        return "\n".join(out) + "\n"

    def explain_failure(self, check_results):
        return "stub diagnosis: the pm rate module has wrong rates"


def _perl_candidate(tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "checkout.pl").write_text(CHECKOUT_PL)
    (cand / "Rates.pm").write_text(RATES_PM)
    (cand / "checkout_test.pl").write_text(
        "#!/usr/bin/env perl\n"
        "use strict;\n"
        "use warnings;\n"
        "use FindBin qw($Bin);\n"
        "\n"
        "my $fail = 0;\n"
        "\n"
        "sub check {\n"
        "    my ($expected, $label, $subtotal) = @_;\n"
        "    my $got = qx(\"$^X\" \"$Bin/checkout.pl\" $subtotal);\n"
        "    chomp $got;\n"
        "    if ($got ne $expected) {\n"
        "        print \"FAIL $label: expected $expected, got $got\\n\";\n"
        "        $fail = 1;\n"
        "    } else {\n"
        "        print \"ok $label\\n\";\n"
        "    }\n"
        "}\n"
        "\n"
        "check(\"200.00\", \"bulk\", 250);\n"
        "check(\"108.00\", \"over100\", 120);\n"
        "check(\"76.00\", \"over50\", 80);\n"
        "check(\"30.00\", \"small\", 30);\n"
        "exit($fail);\n"
    )
    (cand / "repro.pl").write_text(
        "#!/usr/bin/env perl\n"
        "use strict;\n"
        "use warnings;\n"
        "use FindBin qw($Bin);\n"
        "\n"
        "my $a = qx(\"$^X\" \"$Bin/checkout.pl\" 120);\n"
        "my $b = qx(\"$^X\" \"$Bin/checkout.pl\" 80);\n"
        "chomp($a, $b);\n"
        "if ($a ne \"108.00\" || $b ne \"76.00\") {\n"
        "    print \"discount rates are wrong\\n\";\n"
        "    exit(1);\n"
        "}\n"
        "print \"repro ok\\n\";\n"
    )
    return cand


@pytest.mark.skipif(shutil.which("perl") is None,
                    reason="perl is not available")
def test_perl_repair_end_to_end_from_snapshot(tmp_path):
    cand = _perl_candidate(tmp_path)
    spec = parse_spec({
        "name": "shipping-discount-perl-fix",
        "brief": "Fix Rates.pm so every rate matches the published rules.",
        "checks": [
            {"name": "unit_tests", "command": "perl checkout_test.pl",
             "timeout_s": 120},
            {"name": "repro", "command": "perl repro.pl",
             "timeout_s": 120},
        ],
    })
    stub = _SnapshotPerlStub()
    report = run_repair(
        spec=spec, candidate_dir=cand, mode="judge",
        llm=stub, references=MockReferenceLookup(),
        spec_label="spec.json", candidate_label=str(cand), max_rounds=3,
    )
    assert report["final_verdict"] == "PASS"
    assert report["stopped_reason"] == "passed"
    assert report["rounds_used"] == 1
    # The stub was actually handed the pm file in its snapshot.
    assert "Rates.pm" in stub.seen_snapshots[0]
    for a in report["attempts"]:
        assert verify_chain(a["evidence"]) is True
    # The original candidate on disk is never modified.
    assert (cand / "Rates.pm").read_text() == RATES_PM
