"""Bounded recorded scoring and run-stop evidence for availability explanations."""
from dataclasses import dataclass


def run_stop_cause(report, target=None):
    """Classify recorded stop metadata without copying free-form messages."""
    from . import states
    observations = report.observations
    if report.abort_reason in {states.STOP_CANCELLED_BY_USER, states.STOP_RUN_CANCELLED} or observations.get("user_stop_scope"):
        return "user_stop", "The run was stopped before this window finished."
    if observations.get("resources", {}).get("memory_limit_reached"):
        return "memory", "The run reached its memory limit before this window finished."
    if observations.get("target_abort_reasons", {}).get(target):
        return "endpoint_unresponsive", "The endpoint stopped responding, so the run stopped early."
    if "max_duration_seconds" in report.limits_reached or report.abort_reason == states.STOP_MAX_DURATION:
        return "max_duration", "The run reached its maximum duration (--max-duration) before this window finished."
    if (report.abort_reason == states.STOP_REQUEST_BUDGET or observations.get("request_limit_scopes")
            or "max_requests" in report.limits_reached):
        return "request_budget", "The run used its request budget (--max-requests) before this window finished."
    if report.abort_reason == states.STOP_ERROR_THRESHOLD:
        return "error_threshold", "The run stopped after reaching its error threshold (--stop-after-errors)."
    return None


def run_stop_action(code):
    return {
        "user_stop": "Rerun to let it finish.",
        "max_duration": "Raise Maximum duration (--max-duration) or lower the request count, then rerun.",
        "request_budget": "Raise Maximum requests (--max-requests), then rerun.",
        "error_threshold": "Fix the request errors, or raise --stop-after-errors, then rerun.",
        "endpoint_unresponsive": "Check the endpoint is reachable, then rerun.",
        "memory": "Raise the memory limit or reduce concurrency, then rerun.",
    }[code]


# Inference budgets and target health do not shorten the post-request judge phase.
# The runner only skips/cancels judging for its stop event or memory limit.
STOP_PHASES = {
    "user_stop": frozenset({"requests", "judging"}),
    "memory": frozenset({"requests", "judging"}),
    "max_duration": frozenset({"requests"}),
    "request_budget": frozenset({"requests"}),
    "error_threshold": frozenset({"requests"}),
    "endpoint_unresponsive": frozenset({"requests"}),
}


def stop_ends_phase(stop, phase):
    return bool(stop and phase in STOP_PHASES[stop[0]])


def cancelled_by_stop(record):
    # The runner's recorded flag is canonical, including imported probe records
    # whose termination reason was not retained. Traffic shares this vocabulary.
    from .traffic import AUTHORITATIVE_TERMINATION_REASONS
    if record.intentional:
        return False
    return (record.status in {"cancelled", "timeout"}
            and record.termination_reason in AUTHORITATIVE_TERMINATION_REASONS)


def request_phase_end_ms(report, target):
    """Use recorded monotonic evidence before the wall-clock report finish."""
    ends = [r.completed_ms for r in report.requests if r.target == target
            and cancelled_by_stop(r) and r.completed_ms is not None]
    if ends:
        return min(ends)
    duration = report.observations.get("inference_duration_seconds")
    if isinstance(duration, (int, float)):
        return duration * 1000
    from datetime import datetime
    try:
        return (datetime.fromisoformat(report.finished_at.replace("Z", "+00:00"))
                - datetime.fromisoformat(report.started_at.replace("Z", "+00:00"))).total_seconds() * 1000
    except ValueError:
        return None


@dataclass(frozen=True)
class RunEvidence:
    stop: tuple | None
    scoring: tuple

    @classmethod
    def from_report(cls, report, target, good):
        from .scoring_facts import scoring_facts
        return cls(run_stop_cause(report, target), tuple(
            scoring_facts(record) for record in good))
