"""Single source of truth for lifecycle vocabularies whose values are a public contract; see docs/states.md."""

import logging
from typing import Final

logger = logging.getLogger(__name__)
# Tests turn this on so an undeclared lifecycle write fails instead of logging.
STRICT_TRANSITIONS = False


# Check / run verdict: report.json overall/checks[].status and CLI JSON outcome (models.Status).
CHECK_STATUSES: Final = ("pass", "fail", "inconclusive", "skipped", "blocked")
PROBLEM_STATUSES: Final = frozenset({"fail", "inconclusive", "blocked"})
EXIT_CODES: Final = {"pass": 0, "fail": 1, "inconclusive": 2, "blocked": 3}
EXIT_CODE_BLOCKED: Final = EXIT_CODES["blocked"]
EXIT_CODE_DEFAULT: Final = 2
EXIT_CODE_CANCELLED: Final = 130
EXIT_CODE_USAGE: Final = 4

# Request record: report.json requests[].status and evidence.jsonl (models.RequestRecord.status).
REQUEST_STATUSES: Final = ("completed", "failed", "timeout", "cancelled")

# Run lifecycle: .web.json state and run API state.
RUN_STATES: Final = ("queued", "running", "stopping", "completed", "interrupted", "error")
RUN_OWNED_STATES: Final = frozenset({"running", "stopping"})
RUN_IN_PROGRESS_STATES: Final = frozenset({"running", "queued", "stopping"})
RUN_TERMINAL_STATES: Final = frozenset({"completed", "interrupted", "error"})
RUN_TRANSITIONS: Final = {
    "queued": frozenset({"running", "stopping", "completed", "interrupted", "error"}),
    "running": frozenset({"stopping", "completed", "interrupted", "error"}),
    "stopping": frozenset({"completed", "interrupted", "error"}),
    "completed": frozenset({"error"}),  # run_detail exposes unreadable saved reports as error.
    "interrupted": frozenset({"completed", "error"}),
    "error": frozenset(),
}

# Run stop reasons: RunState.reason and report.json abort_reason (fixed text only).
STOP_CANCELLED_BY_USER: Final = "cancelled by user"
STOP_RUN_CANCELLED: Final = "run cancelled"
STOP_MAX_DURATION: Final = "maximum run duration reached"
STOP_REQUEST_BUDGET: Final = "maximum request budget reached"
STOP_ERROR_THRESHOLD: Final = "error threshold reached"
STOP_REASONS: Final = (STOP_CANCELLED_BY_USER, STOP_RUN_CANCELLED, STOP_MAX_DURATION,
                       STOP_REQUEST_BUDGET, STOP_ERROR_THRESHOLD)

# Condition lifecycle: observations.targets.*.conditions[].status before aggregation.
CONDITION_LIFECYCLE: Final = ("waiting", "running", "finished")
CONDITION_UNFINISHED: Final = frozenset({"waiting", "running"})
CONDITION_RECOVERED: Final = "interrupted"
CONDITION_TRANSITIONS: Final = {
    "waiting": frozenset({"running", "finished"}),
    "running": frozenset({"running", "finished"}),
    "finished": frozenset({"running"}),
}

# Scale-down control phase: .scale-down-status.json phase.
SCALE_DOWN_PHASES: Final = ("baseline", "waiting_for_output", "armed", "observing",
                          "post_removal", "finalizing", "finished")
SCALE_DOWN_CLOSING_PHASES: Final = frozenset({"finalizing", "finished"})
SCALE_DOWN_TRANSITIONS: Final = {
    "baseline": frozenset({"waiting_for_output", "finalizing", "finished"}),
    "waiting_for_output": frozenset({"armed", "finalizing", "finished"}),
    "armed": frozenset({"observing", "finalizing", "finished"}),
    "observing": frozenset({"post_removal", "finalizing", "finished"}),
    "post_removal": frozenset({"finalizing", "finished"}),
    "finalizing": frozenset({"finished"}),
    "finished": frozenset(),
}


def run_transition_allowed(current: str | None, new: str) -> bool:
    return current not in RUN_TRANSITIONS or current == new or new in RUN_TRANSITIONS[current]


def check_run_transition(current: str | None, new: str | None) -> None:
    """Flag a run-state write that RUN_TRANSITIONS does not declare; never blocks the write."""
    if new is None or run_transition_allowed(current, new) and new in RUN_TRANSITIONS:
        return
    message = f"Undeclared run state transition: {current!r} -> {new!r}"
    if STRICT_TRANSITIONS:
        raise AssertionError(message)
    logger.warning(message)


def check_scale_down_transition(current: str, new: str) -> None:
    """Flag a phase write that SCALE_DOWN_TRANSITIONS does not declare."""
    if new not in SCALE_DOWN_PHASES:
        raise ValueError("Unknown scale-down phase")
    if current == new or new in SCALE_DOWN_TRANSITIONS[current]:
        return
    message = f"Undeclared scale-down transition: {current!r} -> {new!r}"
    if STRICT_TRANSITIONS:
        raise AssertionError(message)
    logger.warning(message)


def run_recoverable(state: str | None, journal_present: bool, report_available: bool) -> bool:
    """An interrupted journal needs recovery until a valid report owns its evidence."""
    return state == "interrupted" and journal_present and not report_available


def describe() -> dict:
    return {
        "check_statuses": list(CHECK_STATUSES), "problem_statuses": sorted(PROBLEM_STATUSES),
        "exit_codes": {**EXIT_CODES, "cancelled": EXIT_CODE_CANCELLED,
                       "usage_error": EXIT_CODE_USAGE, "default": EXIT_CODE_DEFAULT},
        "request_statuses": list(REQUEST_STATUSES), "run_states": list(RUN_STATES),
        "run_in_progress_states": sorted(RUN_IN_PROGRESS_STATES),
        "run_terminal_states": sorted(RUN_TERMINAL_STATES),
        "run_transitions": {state: sorted(successors) for state, successors in RUN_TRANSITIONS.items()},
        "stop_reasons": list(STOP_REASONS), "stop_reasons_open": True,
        "condition_lifecycle": list(CONDITION_LIFECYCLE),
        "condition_transitions": {state: sorted(successors) for state, successors in CONDITION_TRANSITIONS.items()},
        "scale_down_phases": list(SCALE_DOWN_PHASES),
        "scale_down_transitions": {phase: sorted(successors) for phase, successors in SCALE_DOWN_TRANSITIONS.items()},
    }
