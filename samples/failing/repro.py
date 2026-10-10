"""Standalone reproduction script for the slugify fix (spec check #2)."""
from stringtools import slugify

result = slugify("Hello, World!")
assert result == "hello-world", f"slugify repro failed: got {result!r}"
print("repro OK:", result)
