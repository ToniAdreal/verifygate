"""LLM adapter interface.

The LLM has exactly two jobs in VerifyGate, both advisory:

  * generate_patch  — propose a candidate change from a spec brief
                      (optionally informed by prior failing evidence);
  * explain_failure — turn raw check output into a short plain-language
                      diagnosis for a human reader.

Neither job can influence a verdict. Verdicts come from validators.py.

Both jobs' failures are survivable, in different shapes — neither
can destroy a verdict the deterministic checks already decided. An
``explain_failure`` failure is caught by the runner, which seals a
deterministic fallback explanation plus a ``diagnosis_error`` note,
and the record is still delivered (v1.19.0). A ``generate_patch``
failure ends the repair loop: there is no proposal to apply, but
attempt 0's FAIL evidence already exists, so the loop preserves it
in a repair report whose preserved ``failed_patch`` carries
``error_kind: generation_failed`` and whose ``stopped_reason`` is
``generation_failed`` — the report is delivered with final verdict
FAIL instead of the exception propagating and the report being
lost (v1.20.0).

Adapters that talk to a real model record every call (model, purpose,
token usage) in ``self.usage_log``. The runner drains that log into the
evidence record, so real token usage ends up inside the hash chain —
usage claims in a report are as tamper-evident as the verdict itself.
The deterministic mock records nothing, which keeps Judge Mode records
byte-stable.

Adapter-surface resilience (v1.66.0): an adapter is duck-typed, and
its *optional* surface — the ``name`` attribute and the usage-log
methods (``drain_calls`` / ``pending_calls``) — was read directly by
the runner and the repair loop, on the claim (in ``validate_llm`` /
``validate_references``) that it stays optional. The claim was false
in two places. A nameless adapter crashed a FAIL run on
``llm.name`` / ``references.name`` while the diagnosis entry was
being assembled — after every check had run — and crashed the
repair loop before its first run; a PASS run noticed nothing,
exactly the shape v1.42.0 described for malformed adapters. And a
usage-log method that raised (or a non-callable attribute standing
where one was expected) propagated out of the sealing path: a
verdict the checks had already decided produced no record, and a
repair in progress produced no report. The optional surface is now
read only through :func:`drain_calls_safely` /
:func:`pending_calls_safely` and ``getattr``-guarded name reads: a
missing, broken, or non-list usage log degrades to *no recorded
usage* — the calls are commentary about spend, never a verdict
input — and a missing name degrades to the caller's fallback label.
First-party adapters never degrade: their names and logs behave
exactly as before.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .spec import MAX_BRIEF_CHARS


def _bounded_repr(value) -> str:
    desc = repr(value)
    if len(desc) > 80:
        desc = desc[:77] + "..."
    return desc


def validate_brief(brief, name: str = "brief") -> str:
    """Return ``brief`` if it is a usable task brief, else raise.

    Adapter-method argument validation (v1.52.0): the entry points
    validate the adapters themselves (v1.42.0), but the two advisory
    methods took their *arguments* on trust. A non-string brief
    crashed the mock adapter with an ``AttributeError`` and was
    silently stringified by the real adapter into a paid prompt
    (``Task brief: 123`` / ``Task brief: None``); an over-long brief
    bloated that prompt without bound. A brief must now be a string
    of at most :data:`MAX_BRIEF_CHARS` — the ceiling the spec parser
    already enforces, so a brief taken from a validated spec always
    passes — and anything else is a ``ValueError`` before any model
    call is made. An empty brief stays legal: a spec's brief may be
    empty, and the mock renders it as ``(no brief)``.
    """
    if not isinstance(brief, str) or len(brief) > MAX_BRIEF_CHARS:
        raise ValueError(
            f"{name} must be a string of at most "
            f"{MAX_BRIEF_CHARS} characters, got {_bounded_repr(brief)}")
    return brief


def validate_failing_evidence(evidence, name: str = "failing_evidence"):
    """Return ``evidence`` if it is a dict or None, else raise.

    Part of the v1.52.0 adapter-method argument validation: the
    real adapter read ``failing_evidence.items()`` / ``.get()``
    unguarded, so a list (or any non-dict) crashed
    ``generate_patch`` with an ``AttributeError`` before the call.
    The repair loop always passes a dict it built itself; a direct
    caller passing anything else now gets a ``ValueError``.
    """
    if evidence is not None and not isinstance(evidence, dict):
        raise ValueError(
            f"{name} must be an object or None, "
            f"got {_bounded_repr(evidence)}")
    return evidence


def validate_check_results(check_results, name: str = "check_results"):
    """Return ``check_results`` as a list of objects, else raise.

    Part of the v1.52.0 adapter-method argument validation: both
    adapters filtered their input with ``c["verdict"]``, so a
    non-list crashed with a ``TypeError``, a string iterated into
    characters, and an entry missing ``verdict`` crashed with a
    ``KeyError``. The value must now be a list (or tuple) of
    objects; anything else is a ``ValueError`` before any work.
    A missing ``verdict`` *inside* an entry is not an error: the
    entry simply is not a failure, exactly as
    ``tavily.diagnosis_query`` has always tolerated entries with
    missing fields — the adapters now read it with ``.get`` too.
    """
    if not isinstance(check_results, (list, tuple)) or any(
            not isinstance(c, dict) for c in check_results):
        raise ValueError(
            f"{name} must be a list of check-result objects, "
            f"got {_bounded_repr(check_results)}")
    return list(check_results)


def validate_llm(llm, name: str = "llm"):
    """Validate an LLM adapter argument (v1.42.0).

    An adapter is duck-typed, exactly as the existing test stubs
    rely on: it need not subclass :class:`LLMAdapter`, and the
    optional usage-log methods (``drain_calls`` / ``pending_calls``)
    and the ``name`` attribute stay optional — the runner already
    guards and normalises those. What is *required* is the pair of
    advisory jobs themselves: a callable ``explain_failure`` and a
    callable ``generate_patch``.

    Before this, an entry point accepted ``None``, a string, or a
    bare object verbatim. On a PASS candidate the run then sealed a
    verdict with no adapter at all (the diagnosis path is skipped
    and the usage drain is ``hasattr``-guarded, so nothing noticed);
    on a FAIL candidate the guarded explanation call degraded as
    designed, but the fallback's ``llm.name`` access then raised an
    unhandled ``AttributeError`` and the already-decided record was
    lost — the same loss class v1.19.0 closed for adapter *runtime*
    failures, one shape earlier. A malformed adapter is now a
    ``ValueError`` (the CLI's exit-2 usage error) raised before any
    workspace is prepared or any check runs.
    """
    if llm is None:
        raise ValueError(f"{name} is required (an LLM adapter, got None)")
    for method in ("explain_failure", "generate_patch"):
        if not callable(getattr(llm, method, None)):
            raise ValueError(
                f"{name} must provide a callable {method}() "
                f"(got {type(llm).__name__})")
    return llm


def drain_calls_safely(llm) -> list:
    """Drain an adapter's usage log, or ``[]`` if that is impossible.

    Adapter-surface resilience (v1.66.0, see the module docstring):
    ``drain_calls`` is optional surface — an adapter may not provide
    it, may provide a non-callable attribute under its name, may
    raise, or may return a non-list. Every one of those previously
    propagated out of the runner's sealing path (or the repair
    loop's report assembly) and destroyed a verdict the checks had
    already decided. A broken usage log degrades to no recorded
    usage, never to a lost record; a genuine adapter's list is
    returned (and drained) exactly as before.
    """
    method = getattr(llm, "drain_calls", None)
    if not callable(method):
        return []
    try:
        calls = method()
    except Exception:  # a broken usage log must never lose a verdict
        return []
    return calls if isinstance(calls, list) else []


def pending_calls_safely(llm) -> list:
    """Snapshot an adapter's usage log, or ``[]`` if impossible.

    The companion to :func:`drain_calls_safely` for the repair loop,
    which lengths and slices the snapshot: a missing, non-callable,
    raising, or non-list ``pending_calls`` degrades to an empty
    snapshot, never to a lost report. A genuine adapter's list (or
    tuple) snapshot is returned as a list, exactly as before.
    """
    method = getattr(llm, "pending_calls", None)
    if not callable(method):
        return []
    try:
        calls = method()
    except Exception:  # a broken usage log must never lose a report
        return []
    return list(calls) if isinstance(calls, (list, tuple)) else []


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
