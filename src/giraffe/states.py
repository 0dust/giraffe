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
RUN_QUEUED: Final = "queued"
RUN_RUNNING: Final = "running"
RUN_STOPPING: Final = "stopping"
RUN_COMPLETED: Final = "completed"
RUN_INTERRUPTED: Final = "interrupted"
RUN_ERROR: Final = "error"
RUN_STATES: Final = (RUN_QUEUED, RUN_RUNNING, RUN_STOPPING, RUN_COMPLETED, RUN_INTERRUPTED, RUN_ERROR)
RUN_OWNED_STATES: Final = frozenset({RUN_RUNNING, RUN_STOPPING})
RUN_IN_PROGRESS_STATES: Final = frozenset({RUN_RUNNING, RUN_QUEUED, RUN_STOPPING})
RUN_TERMINAL_STATES: Final = frozenset({RUN_COMPLETED, RUN_INTERRUPTED, RUN_ERROR})
RUN_TRANSITIONS: Final = {
    RUN_QUEUED: frozenset({RUN_RUNNING, RUN_STOPPING, RUN_COMPLETED, RUN_INTERRUPTED, RUN_ERROR}),
    RUN_RUNNING: frozenset({RUN_STOPPING, RUN_COMPLETED, RUN_INTERRUPTED, RUN_ERROR}),
    RUN_STOPPING: frozenset({RUN_COMPLETED, RUN_INTERRUPTED, RUN_ERROR}),
    RUN_COMPLETED: frozenset({RUN_ERROR}),  # run_detail exposes unreadable saved reports as error.
    RUN_INTERRUPTED: frozenset({RUN_COMPLETED, RUN_ERROR}),
    RUN_ERROR: frozenset({RUN_INTERRUPTED}),  # Manual evidence recovery of a given-up run only.
}

# Run stop reasons: RunState.reason and report.json abort_reason (fixed text only).
STOP_CANCELLED_BY_USER: Final = "cancelled by user"
STOP_RUN_CANCELLED: Final = "run cancelled"
STOP_MAX_DURATION: Final = "maximum run duration reached"
STOP_REQUEST_BUDGET: Final = "maximum request budget reached"
STOP_ERROR_THRESHOLD: Final = "error threshold reached"
STOP_REASONS: Final = (STOP_CANCELLED_BY_USER, STOP_RUN_CANCELLED, STOP_MAX_DURATION,
                       STOP_REQUEST_BUDGET, STOP_ERROR_THRESHOLD)

# Run stop causes: raw latched state, including free-text reasons.
STOP_KIND_USER: Final = "user"
STOP_KIND_CANCELLED: Final = "cancelled"
STOP_KIND_DURATION: Final = "duration"
STOP_KIND_REQUEST_BUDGET: Final = "request_budget"
STOP_KIND_ERROR_THRESHOLD: Final = "error_threshold"
STOP_KIND_ENDPOINTS_UNRESPONSIVE: Final = "endpoints_unresponsive"
STOP_KIND_MEMORY: Final = "memory"
STOP_KINDS: Final = (STOP_KIND_USER, STOP_KIND_CANCELLED, STOP_KIND_DURATION,
                     STOP_KIND_REQUEST_BUDGET, STOP_KIND_ERROR_THRESHOLD,
                     STOP_KIND_ENDPOINTS_UNRESPONSIVE, STOP_KIND_MEMORY)
STOP_SOFT_KINDS: Final = frozenset({STOP_KIND_REQUEST_BUDGET})
STOP_REASON_KIND: Final = {
    STOP_CANCELLED_BY_USER: STOP_KIND_USER, STOP_RUN_CANCELLED: STOP_KIND_CANCELLED,
    STOP_MAX_DURATION: STOP_KIND_DURATION, STOP_REQUEST_BUDGET: STOP_KIND_REQUEST_BUDGET,
    STOP_ERROR_THRESHOLD: STOP_KIND_ERROR_THRESHOLD,
}

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


# Connection test: /api/targets/test-connection status.
CONNECTION_CONNECTED: Final = "connected"
CONNECTION_UNVERIFIED: Final = "unverified"
CONNECTION_NEEDS_MODEL: Final = "needs_model"
CONNECTION_TEST_STATUSES: Final = (
    CONNECTION_CONNECTED, CONNECTION_UNVERIFIED, CONNECTION_NEEDS_MODEL,
)

CONNECTION_USABLE_STATUSES: Final = frozenset({CONNECTION_CONNECTED, CONNECTION_UNVERIFIED})

# Connection context discovery: /api/targets/test-connection context_window_reason (None on success).
CONNECTION_CONTEXT_UNAVAILABLE: Final = "unavailable"
CONNECTION_CONTEXT_NO_WINDOW: Final = "no_window"
CONNECTION_CONTEXT_WINDOW_REASONS: Final = (
    CONNECTION_CONTEXT_UNAVAILABLE, CONNECTION_CONTEXT_NO_WINDOW,
)

# Token-evidence probe: observations.targets.*.token_evidence.status.
TOKEN_EVIDENCE_NOT_PROBED: Final = "not_probed"
TOKEN_EVIDENCE_PROBING: Final = "probing"
TOKEN_EVIDENCE_OBSERVED: Final = "observed"
TOKEN_EVIDENCE_UNAVAILABLE: Final = "unavailable"
TOKEN_EVIDENCE_STATUSES: Final = (
    TOKEN_EVIDENCE_NOT_PROBED, TOKEN_EVIDENCE_PROBING, TOKEN_EVIDENCE_OBSERVED, TOKEN_EVIDENCE_UNAVAILABLE,
)

# Token-evidence probe fields: observations.targets.*.token_evidence.logprobs and usage.
TOKEN_EVIDENCE_UNVERIFIED: Final = "unverified"
TOKEN_EVIDENCE_SUPPORTED: Final = "supported"
TOKEN_EVIDENCE_PARTIAL: Final = "partial"
TOKEN_EVIDENCE_NOT_RETURNED: Final = "not_returned"
TOKEN_EVIDENCE_REJECTED: Final = "rejected"
TOKEN_EVIDENCE_FIELD_STATES: Final = (
    TOKEN_EVIDENCE_UNVERIFIED, TOKEN_EVIDENCE_SUPPORTED, TOKEN_EVIDENCE_PARTIAL,
    TOKEN_EVIDENCE_NOT_RETURNED, TOKEN_EVIDENCE_REJECTED,
)

TOKEN_EVIDENCE_TRANSITIONS: Final = {
    TOKEN_EVIDENCE_NOT_PROBED: frozenset({TOKEN_EVIDENCE_PROBING, TOKEN_EVIDENCE_OBSERVED, TOKEN_EVIDENCE_UNAVAILABLE}),
    TOKEN_EVIDENCE_PROBING: frozenset({TOKEN_EVIDENCE_PROBING, TOKEN_EVIDENCE_OBSERVED, TOKEN_EVIDENCE_UNAVAILABLE}),
    TOKEN_EVIDENCE_OBSERVED: frozenset({TOKEN_EVIDENCE_OBSERVED}),
    TOKEN_EVIDENCE_UNAVAILABLE: frozenset({TOKEN_EVIDENCE_UNAVAILABLE}),
}

# Restart observation: observations.targets.*.restart.status.
RESTART_BLOCKED: Final = "blocked"
RESTART_HOOK_COMPLETED: Final = "hook_completed"
RESTART_READY: Final = "ready"
RESTART_STATUSES: Final = (
    RESTART_BLOCKED, RESTART_HOOK_COMPLETED, RESTART_READY,
)

RESTART_TRANSITIONS: Final = {
    RESTART_BLOCKED: frozenset({RESTART_HOOK_COMPLETED}),
    RESTART_HOOK_COMPLETED: frozenset({RESTART_READY, RESTART_BLOCKED}),
    RESTART_READY: frozenset(),
}

# Telemetry snapshot: observations.targets.*.serving_telemetry.snapshots[].status, including host scrapes.
TELEMETRY_AVAILABLE: Final = "available"
TELEMETRY_STALE_SAMPLES: Final = "stale_samples"
TELEMETRY_ABSENT_FAMILIES: Final = "absent_families"
TELEMETRY_UNSUPPORTED_FORMAT: Final = "unsupported_format"
TELEMETRY_EMPTY_RESPONSE: Final = "empty_response"
TELEMETRY_CONNECTION_FAILURE: Final = "connection_failure"
TELEMETRY_RUN_STOPPED: Final = "run_stopped"
TELEMETRY_AUTHENTICATION_DENIED: Final = "authentication_denied"
TELEMETRY_ENDPOINT_ABSENT: Final = "endpoint_absent"
TELEMETRY_RESPONSE_LIMIT: Final = "response_limit"
TELEMETRY_SNAPSHOT_STATUSES: Final = (
    TELEMETRY_AVAILABLE, TELEMETRY_STALE_SAMPLES, TELEMETRY_ABSENT_FAMILIES, TELEMETRY_UNSUPPORTED_FORMAT,
    TELEMETRY_EMPTY_RESPONSE, TELEMETRY_CONNECTION_FAILURE, TELEMETRY_RUN_STOPPED,
    TELEMETRY_AUTHENTICATION_DENIED, TELEMETRY_ENDPOINT_ABSENT, TELEMETRY_RESPONSE_LIMIT,
)

# Discovery remembers runtime scrape results; stopped scrapes return before recording discovery.
TELEMETRY_DISCOVERY_STATUSES: Final = tuple(
    status for status in TELEMETRY_SNAPSHOT_STATUSES if status != TELEMETRY_RUN_STOPPED)
TELEMETRY_USABLE_STATUSES: Final = frozenset({TELEMETRY_AVAILABLE, TELEMETRY_STALE_SAMPLES, TELEMETRY_ABSENT_FAMILIES})
TELEMETRY_DISCOVERY_REUSABLE_STATUSES: Final = frozenset({TELEMETRY_AVAILABLE, TELEMETRY_STALE_SAMPLES})

# Gateway attribution: observations.targets.*.gateway_attribution.state and check metrics.state.
GATEWAY_ATTRIBUTION_NOT_SELECTED: Final = "not_selected"
GATEWAY_ATTRIBUTION_UNAVAILABLE: Final = "unavailable"
GATEWAY_ATTRIBUTION_UNSUPPORTED: Final = "unsupported"
GATEWAY_ATTRIBUTION_UNVERIFIED: Final = "unverified"
GATEWAY_ATTRIBUTION_VERIFIED: Final = "verified"
GATEWAY_ATTRIBUTION_MISMATCH: Final = "mismatch"
GATEWAY_ATTRIBUTION_STATES: Final = (
    GATEWAY_ATTRIBUTION_NOT_SELECTED, GATEWAY_ATTRIBUTION_UNAVAILABLE, GATEWAY_ATTRIBUTION_UNSUPPORTED,
    GATEWAY_ATTRIBUTION_UNVERIFIED, GATEWAY_ATTRIBUTION_VERIFIED, GATEWAY_ATTRIBUTION_MISMATCH,
)

GATEWAY_ATTRIBUTION_CHECK_STATUS: Final = {
    GATEWAY_ATTRIBUTION_NOT_SELECTED: "skipped", GATEWAY_ATTRIBUTION_VERIFIED: "pass",
    GATEWAY_ATTRIBUTION_MISMATCH: "fail", GATEWAY_ATTRIBUTION_UNAVAILABLE: "inconclusive",
    GATEWAY_ATTRIBUTION_UNSUPPORTED: "inconclusive", GATEWAY_ATTRIBUTION_UNVERIFIED: "inconclusive",
}

# Condition aggregate: report conditions and condition_summary.aggregate_status.
CONDITION_AGGREGATE_WAITING: Final = "waiting"
CONDITION_AGGREGATE_RUNNING: Final = "running"
CONDITION_AGGREGATE_PASSED: Final = "passed"
CONDITION_AGGREGATE_FAILED: Final = "failed"
CONDITION_AGGREGATE_INCONCLUSIVE: Final = "inconclusive"
CONDITION_AGGREGATE_SKIPPED: Final = "skipped"
CONDITION_AGGREGATE_STOPPED: Final = "stopped"
CONDITION_AGGREGATE_STATUSES: Final = (
    CONDITION_AGGREGATE_WAITING, CONDITION_AGGREGATE_RUNNING, CONDITION_AGGREGATE_PASSED,
    CONDITION_AGGREGATE_FAILED, CONDITION_AGGREGATE_INCONCLUSIVE, CONDITION_AGGREGATE_SKIPPED,
    CONDITION_AGGREGATE_STOPPED,
)


CONDITION_VERDICT_STATUS: Final = {
    "pass": CONDITION_AGGREGATE_PASSED, "fail": CONDITION_AGGREGATE_FAILED,
    "blocked": CONDITION_AGGREGATE_INCONCLUSIVE, "inconclusive": CONDITION_AGGREGATE_INCONCLUSIVE,
    "skipped": CONDITION_AGGREGATE_SKIPPED,
}

# Condition stop causes: condition rows and condition_summary.reason; hashed workloads also use prose.
CONDITION_STOP_RUN: Final = "run stopped"
CONDITION_STOP_TIME_LIMIT: Final = "time limit reached"
CONDITION_STOP_REQUEST_BUDGET: Final = "request budget reached"
CONDITION_STOP_EARLY: Final = "check stopped early"
CONDITION_STOP_REASONS: Final = (CONDITION_STOP_RUN, CONDITION_STOP_TIME_LIMIT,
                               CONDITION_STOP_REQUEST_BUDGET, CONDITION_STOP_EARLY)
CONDITION_STOP_REASON_MARKERS: Final = ("run stopped", "time limit", "request budget", "cancelled", "aborted")


def run_transition_allowed(current: str | None, new: str) -> bool:
    return current not in RUN_TRANSITIONS or current == new or new in RUN_TRANSITIONS[current]


def check_run_transition(current: str | None, new: str | None) -> None:
    """Warn about an undeclared run-state write, or raise under STRICT_TRANSITIONS."""
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


def check_token_evidence_transition(current: str, new: str) -> None:
    if new in TOKEN_EVIDENCE_TRANSITIONS.get(current, ()):
        return
    message = f"Undeclared token evidence transition: {current!r} -> {new!r}"
    if STRICT_TRANSITIONS:
        raise AssertionError(message)
    logger.warning(message)


def run_recoverable(state: str | None, journal_present: bool, report_available: bool) -> bool:
    """An interrupted journal needs recovery until a valid report owns its evidence."""
    return state == RUN_INTERRUPTED and journal_present and not report_available


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
        "stop_kinds": list(STOP_KINDS), "stop_soft_kinds": sorted(STOP_SOFT_KINDS),
        "stop_reason_kinds": dict(STOP_REASON_KIND),
        "condition_lifecycle": list(CONDITION_LIFECYCLE),
        "condition_transitions": {state: sorted(successors) for state, successors in CONDITION_TRANSITIONS.items()},
        "connection_test_statuses": list(CONNECTION_TEST_STATUSES),
        "connection_context_window_reasons": list(CONNECTION_CONTEXT_WINDOW_REASONS),
        "token_evidence_statuses": list(TOKEN_EVIDENCE_STATUSES),
        "token_evidence_field_states": list(TOKEN_EVIDENCE_FIELD_STATES),
        "restart_statuses": list(RESTART_STATUSES),
        "telemetry_snapshot_statuses": list(TELEMETRY_SNAPSHOT_STATUSES),
        "gateway_attribution_states": list(GATEWAY_ATTRIBUTION_STATES),
        "condition_aggregate_statuses": list(CONDITION_AGGREGATE_STATUSES),
        "connection_usable_statuses": sorted(CONNECTION_USABLE_STATUSES),
        "telemetry_discovery_statuses": list(TELEMETRY_DISCOVERY_STATUSES),
        "telemetry_usable_statuses": sorted(TELEMETRY_USABLE_STATUSES),
        "telemetry_discovery_reusable_statuses": sorted(TELEMETRY_DISCOVERY_REUSABLE_STATUSES),
        "condition_stop_reasons": list(CONDITION_STOP_REASONS),
        "condition_stop_reason_markers": list(CONDITION_STOP_REASON_MARKERS),
        "token_evidence_transitions": {state: sorted(successors) for state, successors in TOKEN_EVIDENCE_TRANSITIONS.items()},
        "restart_transitions": {state: sorted(successors) for state, successors in RESTART_TRANSITIONS.items()},
        "gateway_attribution_check_status": dict(GATEWAY_ATTRIBUTION_CHECK_STATUS),
        "condition_verdict_status": dict(CONDITION_VERDICT_STATUS),
        "scale_down_phases": list(SCALE_DOWN_PHASES),
        "scale_down_transitions": {phase: sorted(successors) for phase, successors in SCALE_DOWN_TRANSITIONS.items()},
    }
