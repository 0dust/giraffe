"""Single source of truth for lifecycle vocabularies whose values are a public contract; see docs/states.md."""

import logging
from collections.abc import Collection
from typing import Final

logger = logging.getLogger(__name__)
# Tests turn this on so undeclared lifecycle states and comparison reasons fail instead of logging.
STRICT_TRANSITIONS = False


# Check / run verdict: report.json overall/checks[].status and CLI JSON outcome (models.Status).
CHECK_STATUSES: Final = ("pass", "fail", "inconclusive", "skipped", "blocked")
PROBLEM_STATUSES: Final = frozenset({"fail", "inconclusive", "blocked"})
EXIT_CODES: Final = {"pass": 0, "fail": 1, "inconclusive": 2, "blocked": 3}
EXIT_CODE_BLOCKED: Final = EXIT_CODES["blocked"]
EXIT_CODE_DEFAULT: Final = 2
EXIT_CODE_CANCELLED: Final = 130
EXIT_CODE_USAGE: Final = 4

# Baseline workload comparability is independent of regression/acceptance status.
BASELINE_COMPARABLE: Final = "comparable"
BASELINE_NOT_COMPARABLE: Final = "not_comparable"
BASELINE_UNKNOWN: Final = "unknown"
BASELINE_COMPARABILITY: Final = (BASELINE_COMPARABLE, BASELINE_NOT_COMPARABLE, BASELINE_UNKNOWN)
BASELINE_REASON_CODES: Final = {
    "setting_missing": BASELINE_UNKNOWN,
    "setting_unknown": BASELINE_UNKNOWN,
    "target_unknown": BASELINE_UNKNOWN,
    "config_invalid": BASELINE_UNKNOWN,
    "request_pack_unknown": BASELINE_UNKNOWN,
    "multi_user_policy_unknown": BASELINE_UNKNOWN,
    "fixture_pack_unknown": BASELINE_UNKNOWN,
    "custom_fixtures_unhashed": BASELINE_UNKNOWN,
    "gateway_access_missing": BASELINE_UNKNOWN,
    "gateway_access_settings_unknown": BASELINE_UNKNOWN,
    "gateway_access_workload_version_unknown": BASELINE_UNKNOWN,
    "load_samples_missing": BASELINE_UNKNOWN,
    "user_fairness_unknown": BASELINE_UNKNOWN,
    "user_delivery_incomplete": BASELINE_UNKNOWN,
    "comparison_unavailable": BASELINE_UNKNOWN,
    "setting_differs": BASELINE_NOT_COMPARABLE,
    "limit_constrained": BASELINE_NOT_COMPARABLE,
    "target_differs": BASELINE_NOT_COMPARABLE,
    "schema_differs": BASELINE_NOT_COMPARABLE,
    "timing_method_differs": BASELINE_NOT_COMPARABLE,
    "run_aborted": BASELINE_NOT_COMPARABLE,
    "evaluation_failed": BASELINE_NOT_COMPARABLE,
    "config_unsupported": BASELINE_NOT_COMPARABLE,
    "request_source_differs": BASELINE_NOT_COMPARABLE,
    "request_pack_differs": BASELINE_NOT_COMPARABLE,
    "judge_incomplete": BASELINE_UNKNOWN,
    "multi_user_policy_differs": BASELINE_NOT_COMPARABLE,
    "cancellation_grading_differs": BASELINE_NOT_COMPARABLE,
    "fixture_pack_differs": BASELINE_NOT_COMPARABLE,
    "workload_format_changed": BASELINE_NOT_COMPARABLE,
    "target_absent": BASELINE_NOT_COMPARABLE,
    "gateway_access_settings_differs": BASELINE_NOT_COMPARABLE,
    "gateway_access_transport_differs": BASELINE_NOT_COMPARABLE,
    "gateway_access_workload_version_differs": BASELINE_NOT_COMPARABLE,
    "user_delivery_differs": BASELINE_NOT_COMPARABLE,
    "user_delivery_zero_baseline": BASELINE_UNKNOWN,
    "user_fairness_differs": BASELINE_NOT_COMPARABLE,
    "user_slots_differs": BASELINE_NOT_COMPARABLE,
    "load_work_excluded": BASELINE_NOT_COMPARABLE,
    "scale_down_unsupported": BASELINE_NOT_COMPARABLE,
}

LOAD_ACCEPTANCE_NOT_ESTABLISHED: Final = "not_established"
LOAD_ACCEPTANCE_NOT_TESTED_IN_CURRENT: Final = "not_tested_in_current"
LOAD_ACCEPTANCE_NOT_TESTED_IN_BASELINE: Final = "not_tested_in_baseline"
LOAD_ACCEPTANCE_WORK_CHANGED: Final = "work_changed"
LOAD_ACCEPTANCE_UNCHANGED_ACCEPTED: Final = "unchanged_accepted"
LOAD_ACCEPTANCE_UNCHANGED_NOT_ACCEPTED: Final = "unchanged_not_accepted"
LOAD_ACCEPTANCE_LOST: Final = "lost"
LOAD_ACCEPTANCE_GAINED: Final = "gained"
LOAD_ACCEPTANCE_ACCEPTED_BASELINE_NOT_ESTABLISHED: Final = "accepted_baseline_not_established"
LOAD_ACCEPTANCE_NOT_ESTABLISHED_IN_CURRENT: Final = "not_established_in_current"
LOAD_ACCEPTANCE_NOT_ESTABLISHED_IN_BASELINE: Final = "not_established_in_baseline"
LOAD_ACCEPTANCE_CHANGES: Final = (
    LOAD_ACCEPTANCE_NOT_ESTABLISHED,
    LOAD_ACCEPTANCE_NOT_TESTED_IN_CURRENT,
    LOAD_ACCEPTANCE_NOT_TESTED_IN_BASELINE,
    LOAD_ACCEPTANCE_WORK_CHANGED,
    LOAD_ACCEPTANCE_UNCHANGED_ACCEPTED,
    LOAD_ACCEPTANCE_UNCHANGED_NOT_ACCEPTED,
    LOAD_ACCEPTANCE_LOST,
    LOAD_ACCEPTANCE_GAINED,
    LOAD_ACCEPTANCE_ACCEPTED_BASELINE_NOT_ESTABLISHED,
    LOAD_ACCEPTANCE_NOT_ESTABLISHED_IN_CURRENT,
    LOAD_ACCEPTANCE_NOT_ESTABLISHED_IN_BASELINE,
)


# Request record: report.json requests[].status and evidence.jsonl (models.RequestRecord.status).
REQUEST_STATUSES: Final = ("completed", "failed", "timeout", "cancelled")

# Per-check phase: internal runner state, not a public contract; not in describe().
CHECK_PHASE_INFERENCE: Final = "inference"
CHECK_PHASE_JUDGING: Final = "judging"
CHECK_PHASE_DONE: Final = "done"
CHECK_PHASES: Final = (CHECK_PHASE_INFERENCE, CHECK_PHASE_JUDGING, CHECK_PHASE_DONE)
CHECK_PHASE_TRANSITIONS: Final = {
    CHECK_PHASE_INFERENCE: frozenset({CHECK_PHASE_INFERENCE, CHECK_PHASE_JUDGING, CHECK_PHASE_DONE}),
    CHECK_PHASE_JUDGING: frozenset({CHECK_PHASE_JUDGING, CHECK_PHASE_DONE}),
    CHECK_PHASE_DONE: frozenset({CHECK_PHASE_DONE}),
}
# These checks wait for judging when the request pack contains judge-scored specs.
JUDGED_CHECKS: Final = frozenset({"correctness", "capacity", "load"})

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

# Server activity: admission blockers, also exposed as X-Giraffe-Error-Code.
SERVER_RUN_ACTIVE: Final = "run_active"
SERVER_RUN_STARTING: Final = "run_starting"
SERVER_RUN_STOPPING: Final = "run_stopping"
SERVER_CONNECTION_TEST: Final = "connection_test_running"
SERVER_ACTIVITIES: Final = (SERVER_RUN_ACTIVE, SERVER_RUN_STARTING, SERVER_RUN_STOPPING,
                            SERVER_CONNECTION_TEST)
ADMISSION_BLOCKERS: Final = {
    "start_run": (SERVER_RUN_ACTIVE, SERVER_RUN_STOPPING, SERVER_CONNECTION_TEST),
    "test_connection": (SERVER_RUN_ACTIVE, SERVER_RUN_STARTING, SERVER_CONNECTION_TEST),
    "tokenizer_preview": (SERVER_RUN_ACTIVE,),
    "compare": (SERVER_RUN_ACTIVE,),
    "recovery": (SERVER_RUN_ACTIVE, SERVER_RUN_STARTING, SERVER_RUN_STOPPING),
}


def admission_conflict(operation: str, activities: Collection[str]) -> str | None:
    """Return the first listed blocker; unknown operations raise KeyError."""
    return next((code for code in ADMISSION_BLOCKERS[operation] if code in activities), None)


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


def check_check_phase_transition(current: str, new: str) -> None:
    """Warn about an undeclared check-phase write, or raise under STRICT_TRANSITIONS."""
    if new in CHECK_PHASE_TRANSITIONS and (
            current not in CHECK_PHASE_TRANSITIONS or new in CHECK_PHASE_TRANSITIONS[current]):
        return
    message = f"Undeclared check phase transition: {current!r} -> {new!r}"
    if STRICT_TRANSITIONS:
        raise AssertionError(message)
    logger.warning(message)


def check_reason_code(code: str | None, *, text: str = "") -> None:
    """Reject missing/undeclared reason coverage in tests; warn in production."""
    if code in BASELINE_REASON_CODES:
        return
    message = f"Undeclared baseline reason code: {code!r} ({text})"
    if STRICT_TRANSITIONS:
        raise AssertionError(message)
    logger.warning(message)


def worst_baseline_comparability(verdicts: Collection[str]) -> str:
    """Combine target verdicts in severity order, including an empty comparison."""
    if BASELINE_NOT_COMPARABLE in verdicts:
        return BASELINE_NOT_COMPARABLE
    if BASELINE_UNKNOWN in verdicts:
        return BASELINE_UNKNOWN
    return BASELINE_COMPARABLE


def baseline_comparability(codes: Collection[str]) -> str:
    for code in codes:
        check_reason_code(code)
    return worst_baseline_comparability([
        BASELINE_REASON_CODES.get(code, BASELINE_UNKNOWN) for code in codes])


def run_recoverable(state: str | None, journal_present: bool, report_available: bool) -> bool:
    """An interrupted journal needs recovery until a valid report owns its evidence."""
    return state == RUN_INTERRUPTED and journal_present and not report_available


def describe() -> dict:
    return {
        "check_statuses": list(CHECK_STATUSES), "problem_statuses": sorted(PROBLEM_STATUSES),
        "exit_codes": {**EXIT_CODES, "cancelled": EXIT_CODE_CANCELLED,
                       "usage_error": EXIT_CODE_USAGE, "default": EXIT_CODE_DEFAULT},
        "request_statuses": list(REQUEST_STATUSES), "run_states": list(RUN_STATES),
        "judged_checks": sorted(JUDGED_CHECKS),
        "baseline_comparability": list(BASELINE_COMPARABILITY),
        "baseline_reason_codes": dict(BASELINE_REASON_CODES),
        "load_acceptance_changes": list(LOAD_ACCEPTANCE_CHANGES),
        "server_activities": list(SERVER_ACTIVITIES),
        "admission": {operation: list(blockers) for operation, blockers in ADMISSION_BLOCKERS.items()},
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
