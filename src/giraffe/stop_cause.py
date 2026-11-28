"""Stop-cause values and projections shared by run surfaces."""

from __future__ import annotations

from dataclasses import dataclass

from . import states


@dataclass(frozen=True)
class StopCause:
    kind: str
    text: str
    scope: str | None = None


def latch_cause(current: StopCause | None, kind: str, text: str | None = None,
                scope: str | None = None, *, replace_soft: bool) -> StopCause:
    """Keep the first cause, allowing an explicit upgrade of a soft budget."""
    if current is not None and not (current.kind in states.STOP_SOFT_KINDS
            and replace_soft and kind not in states.STOP_SOFT_KINDS):
        return current
    if text is None:
        text = next(reason for reason, reason_kind in states.STOP_REASON_KIND.items() if reason_kind == kind)
    return StopCause(kind, text, scope)


def stopping_fields(cause: StopCause | None) -> dict:
    hard = cause is not None and cause.kind not in states.STOP_SOFT_KINDS
    return {"abort_reason": cause.text if hard else None, "stop_kind": cause.kind if hard else None}


def finished_fields(cause: StopCause | None, verdict_reason: str | None) -> dict:
    return {"abort_reason": verdict_reason, "stop_kind": cause.kind if cause else None}


def cause_of(report_or_observations) -> StopCause | None:
    value = getattr(report_or_observations, "observations", report_or_observations) or {}
    if not isinstance(value, dict):
        return None
    if "observations" in value:
        value = value["observations"]
    if not isinstance(value, dict):
        return None
    cause = value.get("stop_cause")
    if not isinstance(cause, dict):
        return None
    kind, text, scope = cause.get("kind"), cause.get("text"), cause.get("scope")
    if not isinstance(kind, str) or kind not in states.STOP_KINDS or not isinstance(text, str):
        return None
    return StopCause(kind, text, scope if isinstance(scope, str) else None)


def stop_kind_of(report_or_observations) -> str | None:
    """Read the raw cause, independently of verdict or recovery prose."""
    cause = cause_of(report_or_observations)
    return cause.kind if cause else None

