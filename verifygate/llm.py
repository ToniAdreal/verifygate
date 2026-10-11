"""LLM adapter interface.

The LLM has exactly two jobs in VerifyGate, both advisory:

  * generate_patch  — propose a candidate change from a spec brief
                      (optionally informed by prior failing evidence);
  * explain_failure — turn raw check output into a short plain-language
                      diagnosis for a human reader.

Neither job can influence a verdict. Verdicts come from validators.py.

The two jobs differ in failure handling, deliberately: an
``explain_failure`` failure is survivable — the runner catches it,
seals a deterministic fallback explanation plus a ``diagnosis_error``
note, and the record the checks decided is still delivered (v1.19.0).
A ``generate_patch`` failure is the repair loop's own failure — there
is no proposal to apply — so it propagates as an adapter error.

Adapters that talk to a real model record every call (model, purpose,
token usage) in ``self.usage_log``. The runner drains that log into the
evidence record, so real token usage ends up inside the hash chain —
usage claims in a report are as tamper-evident as the verdict itself.
The deterministic mock records nothing, which keeps Judge Mode records
byte-stable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMAdapter(ABC):
    name: str = "base"

    def __init__(self) -> None:
        self.usage_log: list[dict] = []

    @abstractmethod
    def generate_patch(self, brief: str, failing_evidence: dict | None = None) -> str:
        """Return a textual patch / candidate description for the brief."""
        raise NotImplementedError

    @abstractmethod
    def explain_failure(self, check_results: list[dict]) -> str:
        """Return a short markdown diagnosis of the failing checks."""
        raise NotImplementedError

    def _log(self) -> list[dict]:
        # Lazily created so subclasses that skip super().__init__()
        # (simple test stubs) still behave.
        if not hasattr(self, "usage_log"):
            self.usage_log = []
        return self.usage_log

    def record_call(self, entry: dict) -> None:
        """Append one real model call to the usage log."""
        self._log().append(entry)

    def drain_calls(self) -> list[dict]:
        """Return and clear the usage log (called by the runner)."""
        calls, self.usage_log = self._log(), []
        return calls

    def pending_calls(self) -> list[dict]:
        """Snapshot of the usage log without clearing it."""
        return list(self._log())
