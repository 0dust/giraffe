"""Derive request outcomes and their shared error denominator without model imports."""

from collections.abc import Iterable
from typing import Any

from .states import REQUEST_OUTCOMES, REQUEST_OUTCOME_ERRORS


def request_outcome(record: Any) -> str:
    """Return the first matching outcome; correctness is a separate axis."""
    # fixtures imports models, whose computed field calls this helper. Import only
    # when deriving an outcome so importing any of these modules remains acyclic.
    from .fixtures import reasoning_budget

    if record.not_dispatched:
        return "not_dispatched"
    if record.intentional:
        return "intentional_stop"
    if record.status == "cancelled":
        return "cancelled"
    if record.status == "timeout":
        return "timeout"
    if reasoning_budget(record):
        return "budget_exhausted"
    if record.status == "failed":
        return "failed"
    return "ok" if record.valid else "invalid"


def count_outcomes(records: Iterable[Any]) -> dict:
    """Count every outcome, errors, evaluated requests and their error rate."""
    counts = dict.fromkeys(REQUEST_OUTCOMES, 0)
    for record in records:
        counts[request_outcome(record)] += 1
    errors = sum(counts[outcome] for outcome in REQUEST_OUTCOME_ERRORS)
    evaluated = counts["ok"] + errors
    return {**counts, "errors": errors, "evaluated": evaluated,
            "error_rate": errors / evaluated if evaluated else None}


def preparation_failure_reason(records: Iterable[Any]) -> str | None:
    """Explain why local preparation failures prevent a passing assessment."""
    count = sum(request_outcome(record) == "not_dispatched" for record in records)
    if count:
        return (f"{count} request(s) could not be prepared locally and were not sent; "
                "fix the request inputs and rerun.")
    return None
