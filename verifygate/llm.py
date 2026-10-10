"""LLM adapter interface.

The LLM has exactly two jobs in VerifyGate, both advisory:

  * generate_patch  — propose a candidate change from a spec brief
                      (optionally informed by prior failing evidence);
  * explain_failure — turn raw check output into a short plain-language
                      diagnosis for a human reader.

Neither job can influence a verdict. Verdicts come from validators.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMAdapter(ABC):
    name: str = "base"

    @abstractmethod
    def generate_patch(self, brief: str, failing_evidence: dict | None = None) -> str:
        """Return a textual patch / candidate description for the brief."""
        raise NotImplementedError

    @abstractmethod
    def explain_failure(self, check_results: list[dict]) -> str:
        """Return a short markdown diagnosis of the failing checks."""
        raise NotImplementedError
