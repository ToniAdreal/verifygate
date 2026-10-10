"""Hosted-demo + publication assets: static, self-contained, honest.

The demo directory is the hosted face of the project (Oct 22-24
milestone). It must stay deployable to any static host: no JavaScript,
no external resources, and the evidence page it links to must be the
rendered real-mode repair report with its hash chains verified.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"


def test_demo_index_is_self_contained():
    html = (DEMO / "index.html").read_text(encoding="utf-8")
    assert "<script" not in html
    assert 'src="http' not in html and "src='http" not in html
    assert 'href="http' not in html and "href='http" not in html
    assert "Judge Mode" in html
    assert "evidence_real_repair.html" in html


def test_demo_evidence_page_chains_verify():
    html = (DEMO / "evidence_real_repair.html").read_text(encoding="utf-8")
    assert "Hash chain verified" in html
    assert "CHAIN INVALID" not in html
    assert "<script" not in html


def test_license_present_and_mit():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License")
    assert "Permission is hereby granted" in text
