"""Tiny string helpers (fictional demo library for the VerifyGate sample).

This candidate is intentionally broken: slugify() neither lowercases nor
removes punctuation, so the spec's checks must catch it.
"""


def slugify(text: str) -> str:
    return text.strip().replace(" ", "-")


def word_count(text: str) -> int:
    return len([w for w in text.split() if w])
