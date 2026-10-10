"""Tiny string helpers (fictional demo library for the VerifyGate sample)."""

import re


def slugify(text: str) -> str:
    """Lowercase, drop punctuation, join words with single hyphens."""
    lowered = text.strip().lower()
    hyphenated = re.sub(r"[^a-z0-9]+", "-", lowered)
    return hyphenated.strip("-")


def word_count(text: str) -> int:
    return len([w for w in text.split() if w])
