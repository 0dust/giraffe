"""Single source of truth for lifecycle vocabularies whose values are a public contract; see docs/states.md."""

import logging
from collections.abc import Collection
from typing import Final

logger = logging.getLogger(__name__)
# Tests turn this on so undeclared lifecycle states and comparison reasons fail instead of logging.
STRICT_TRANSITIONS = False


# Tokenizer provenance, preparation observations, plan estimates, and judging evidence.
TOKENIZER_SOURCE_TARGET_MODEL: Final = "target_model"
TOKENIZER_SOURCE_REMEMBERED_MAPPING: Final = "remembered_mapping"
TOKENIZER_SOURCE_SERVER_MODEL_ROOT: Final = "server_model_root"
TOKENIZER_SOURCE_HUB_RESOLUTION: Final = "hub_resolution"
TOKENIZER_SOURCE_BUILTIN_FALLBACK: Final = "builtin_fallback"
TOKENIZER_SOURCE_CONFIGURED: Final = "configured"
TOKENIZER_SOURCE_BUILTIN: Final = "builtin"
TOKENIZER_SOURCES: Final = (
    TOKENIZER_SOURCE_TARGET_MODEL,
    TOKENIZER_SOURCE_REMEMBERED_MAPPING,
    TOKENIZER_SOURCE_SERVER_MODEL_ROOT,
    TOKENIZER_SOURCE_HUB_RESOLUTION,
    TOKENIZER_SOURCE_BUILTIN_FALLBACK,
    TOKENIZER_SOURCE_CONFIGURED,
    TOKENIZER_SOURCE_BUILTIN,
)

TOKENIZER_CALIBRATION_CALIBRATED: Final = "calibrated"
TOKENIZER_CALIBRATION_UNAVAILABLE: Final = "unavailable"
TOKENIZER_CALIBRATION_STATUSES: Final = (
    TOKENIZER_CALIBRATION_CALIBRATED,
    TOKENIZER_CALIBRATION_UNAVAILABLE,
)

TOKENIZER_CALIBRATION_REASON_TOKENIZER_UNAVAILABLE: Final = "tokenizer_unavailable"
TOKENIZER_CALIBRATION_REASON_MINIMUM_PROMPT_LENGTH: Final = "minimum_prompt_length"
TOKENIZER_CALIBRATION_REASON_INPUT_SIZING_UNAVAILABLE: Final = "input_sizing_unavailable"
TOKENIZER_CALIBRATION_REASON_CONTEXT_WINDOW_EXCEEDED: Final = "context_window_exceeded"
TOKENIZER_CALIBRATION_REASONS: Final = (
    TOKENIZER_CALIBRATION_REASON_TOKENIZER_UNAVAILABLE,
    TOKENIZER_CALIBRATION_REASON_MINIMUM_PROMPT_LENGTH,
    TOKENIZER_CALIBRATION_REASON_INPUT_SIZING_UNAVAILABLE,
    TOKENIZER_CALIBRATION_REASON_CONTEXT_WINDOW_EXCEEDED,
)

RESIDENCY_UNKNOWN: Final = "unknown"
RESIDENCY_RESIDENT: Final = "resident"
RESIDENCY_NOT_RESIDENT: Final = "not_resident"
RESIDENCY_STATES: Final = (
    RESIDENCY_UNKNOWN,
    RESIDENCY_RESIDENT,
    RESIDENCY_NOT_RESIDENT,
)

RESIDENCY_BEFORE_PREPARATION: Final = "before_preparation"
RESIDENCY_AFTER_PREPARATION: Final = "after_preparation"
RESIDENCY_PHASES: Final = (
    RESIDENCY_BEFORE_PREPARATION,
    RESIDENCY_AFTER_PREPARATION,
)

PLAN_CONFIDENCE_LOW: Final = "low"
PLAN_CONFIDENCE_MEDIUM: Final = "medium"
PLAN_CONFIDENCE_HIGH: Final = "high"
PLAN_CONFIDENCE: Final = (
    PLAN_CONFIDENCE_LOW,
    PLAN_CONFIDENCE_MEDIUM,
    PLAN_CONFIDENCE_HIGH,
)

JUDGING_STOP_CANCELLED: Final = "cancelled"
JUDGING_STOP_PHASE_DEADLINE: Final = "phase_deadline"
JUDGING_STOP_REASONS: Final = (
    JUDGING_STOP_CANCELLED,
    JUDGING_STOP_PHASE_DEADLINE,
)

JUDGE_VOTE_ERROR_CANCELLED: Final = "cancelled"
JUDGE_VOTE_ERROR_PHASE_DEADLINE: Final = "phase_deadline"
JUDGE_VOTE_ERROR_NOT_EVALUATED: Final = "not_evaluated"
JUDGE_VOTE_ERROR_TIMEOUT: Final = "timeout"
JUDGE_VOTE_ERROR_HTTP_ERROR: Final = "http_error"
JUDGE_VOTE_ERROR_REQUEST_FAILED: Final = "request_failed"
JUDGE_VOTE_ERROR_TRUNCATED_RESPONSE: Final = "truncated_response"
JUDGE_VOTE_ERROR_INVALID_COMPLETION: Final = "invalid_completion"
JUDGE_VOTE_ERROR_INVALID_VERDICT: Final = "invalid_verdict"
JUDGE_VOTE_ERROR_CLIENT_INITIALIZATION: Final = "client_initialization"
JUDGE_VOTE_ERROR_REASONS: Final = (
    JUDGE_VOTE_ERROR_CANCELLED,
    JUDGE_VOTE_ERROR_PHASE_DEADLINE,
    JUDGE_VOTE_ERROR_NOT_EVALUATED,
    JUDGE_VOTE_ERROR_TIMEOUT,
    JUDGE_VOTE_ERROR_HTTP_ERROR,
    JUDGE_VOTE_ERROR_REQUEST_FAILED,
    JUDGE_VOTE_ERROR_TRUNCATED_RESPONSE,
    JUDGE_VOTE_ERROR_INVALID_COMPLETION,
    JUDGE_VOTE_ERROR_INVALID_VERDICT,
    JUDGE_VOTE_ERROR_CLIENT_INITIALIZATION,
)

METRIC_MEASURED: Final = "measured"
METRIC_ESTIMATED: Final = "estimated"
METRIC_NOT_MEASURED: Final = "not_measured"
METRIC_NOT_OBSERVED: Final = "not_observed"
METRIC_NOT_COLLECTED: Final = "not_collected"
METRIC_NOT_APPLICABLE: Final = "not_applicable"
METRIC_INSUFFICIENT_EVIDENCE: Final = "insufficient_evidence"
METRIC_NOT_SUPPORTED: Final = "not_supported"
METRIC_NO_BASELINE: Final = "no_baseline"
METRIC_STATES: Final = (
    METRIC_MEASURED, METRIC_ESTIMATED, METRIC_NOT_MEASURED, METRIC_NOT_OBSERVED,
    METRIC_NOT_COLLECTED, METRIC_NOT_APPLICABLE, METRIC_INSUFFICIENT_EVIDENCE,
    METRIC_NOT_SUPPORTED, METRIC_NO_BASELINE,
)
METRIC_VALUE_STATES: Final = frozenset({METRIC_MEASURED, METRIC_ESTIMATED})
METRIC_STATE_LABELS: Final = {
    METRIC_MEASURED: "Measured", METRIC_ESTIMATED: "Estimated", METRIC_NOT_MEASURED: "Not measured",
    METRIC_NOT_OBSERVED: "Not observed", METRIC_NOT_COLLECTED: "Not collected",
    METRIC_NOT_APPLICABLE: "Not applicable", METRIC_INSUFFICIENT_EVIDENCE: "Insufficient evidence",
    METRIC_NOT_SUPPORTED: "Not supported", METRIC_NO_BASELINE: "No baseline",
}
METRIC_STATE_ACTIONS: Final = {
    METRIC_NOT_MEASURED: "Collect eligible observations for this scope.",
    METRIC_NOT_OBSERVED: "Collect eligible observations for this scope.",
    METRIC_NOT_COLLECTED: "Enable the corresponding collector and rerun the workload.",
    METRIC_NOT_APPLICABLE: "Select the corresponding workload or transport to collect this measure.",
    METRIC_INSUFFICIENT_EVIDENCE: "Collect eligible observations for this scope.",
    METRIC_NOT_SUPPORTED: "Collect eligible observations for this scope.",
    METRIC_NO_BASELINE: "Attach a baseline with comparable workload and measurement coverage.",
}


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
    "outcome_counting_differs": BASELINE_NOT_COMPARABLE,
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

# Derived request outcome, in first-match precedence order (request_outcome.py).
REQUEST_OUTCOMES: Final = (
    "not_dispatched", "intentional_stop", "cancelled", "timeout",
    "budget_exhausted", "failed", "invalid", "ok",
)
REQUEST_OUTCOME_ERRORS: Final = frozenset({"invalid", "failed", "timeout"})
REQUEST_OUTCOME_EXCLUDED: Final = frozenset({
    "intentional_stop", "cancelled", "budget_exhausted", "not_dispatched",
})

# Gateway policy: check metrics.gateway_policy.qualification and its check status.
GATEWAY_POLICY_UNVERIFIED: Final = "unverified"
GATEWAY_POLICY_UNAVAILABLE: Final = "unavailable"
GATEWAY_POLICY_OBSERVED_FAILURE: Final = "observed_failure"
GATEWAY_POLICY_OBSERVED: Final = "observed"
GATEWAY_POLICY_UNEXERCISED: Final = "unexercised"
GATEWAY_POLICY_QUALIFICATIONS: Final = (
    GATEWAY_POLICY_UNVERIFIED,
    GATEWAY_POLICY_UNAVAILABLE,
    GATEWAY_POLICY_OBSERVED_FAILURE,
    GATEWAY_POLICY_OBSERVED,
    GATEWAY_POLICY_UNEXERCISED,
)
GATEWAY_POLICY_STATUSES: Final = {
    GATEWAY_POLICY_UNVERIFIED: frozenset({"inconclusive"}),
    GATEWAY_POLICY_UNAVAILABLE: frozenset({"inconclusive"}),
    GATEWAY_POLICY_OBSERVED_FAILURE: frozenset({"fail"}),
    GATEWAY_POLICY_OBSERVED: frozenset({"pass"}),
    GATEWAY_POLICY_UNEXERCISED: frozenset({"inconclusive"}),
}

# Gateway access: declared policy, observed decisions, and fixed reason codes.
GATEWAY_ACCESS_ALLOW: Final = "allow"
GATEWAY_ACCESS_DENY: Final = "deny"
GATEWAY_ACCESS_UNVERIFIED: Final = "unverified"
GATEWAY_ACCESS_EXPECTED: Final = (
    GATEWAY_ACCESS_ALLOW,
    GATEWAY_ACCESS_DENY,
)
GATEWAY_ACCESS_OBSERVATIONS: Final = (
    GATEWAY_ACCESS_ALLOW,
    GATEWAY_ACCESS_DENY,
    GATEWAY_ACCESS_UNVERIFIED,
)
GATEWAY_ACCESS_REASON_NOT_RUN: Final = "not_run"
GATEWAY_ACCESS_REASON_REQUEST_CANCELLED: Final = "request_cancelled"
GATEWAY_ACCESS_REASON_IDENTITY_CHANGED: Final = "identity_changed"
GATEWAY_ACCESS_REASON_IDENTITY_UNAVAILABLE: Final = "identity_unavailable"
GATEWAY_ACCESS_REASON_FALLBACK_REPORTED: Final = "fallback_reported"
GATEWAY_ACCESS_REASON_MODEL_GROUP_MISMATCH: Final = "model_group_mismatch"
GATEWAY_ACCESS_REASON_VALID_COMPLETION: Final = "valid_completion"
GATEWAY_ACCESS_REASON_ASSISTANT_REFUSAL: Final = "assistant_refusal"
GATEWAY_ACCESS_REASON_METERED_REASONING_COMPLETION: Final = "metered_reasoning_completion"
GATEWAY_ACCESS_REASON_GATEWAY_AUTHENTICATION: Final = "gateway_authentication"
GATEWAY_ACCESS_REASON_GATEWAY_MODEL_ACCESS: Final = "gateway_model_access"
GATEWAY_ACCESS_REASON_INCOMPLETE_OR_UNSUPPORTED_RESPONSE: Final = "incomplete_or_unsupported_response"
GATEWAY_ACCESS_REASON_REQUEST_NOT_DISPATCHED: Final = "request_not_dispatched"
GATEWAY_ACCESS_REASON_AUTH_HEADER_UNVERIFIED: Final = "auth_header_unverified"
GATEWAY_ACCESS_REASON_IDENTITY_CONFLICT: Final = "identity_conflict"
GATEWAY_ACCESS_REASON_UNSUPPORTED_PROFILE: Final = "unsupported_profile"
GATEWAY_ACCESS_REASON_RUN_DEADLINE: Final = "run_deadline"
GATEWAY_ACCESS_REASON_RUN_STOPPED: Final = "run_stopped"
GATEWAY_ACCESS_REASON_REQUEST_BUDGET_EXHAUSTED: Final = "request_budget_exhausted"
GATEWAY_ACCESS_REASONS: Final = (
    GATEWAY_ACCESS_REASON_NOT_RUN,
    GATEWAY_ACCESS_REASON_REQUEST_CANCELLED,
    GATEWAY_ACCESS_REASON_IDENTITY_CHANGED,
    GATEWAY_ACCESS_REASON_IDENTITY_UNAVAILABLE,
    GATEWAY_ACCESS_REASON_FALLBACK_REPORTED,
    GATEWAY_ACCESS_REASON_MODEL_GROUP_MISMATCH,
    GATEWAY_ACCESS_REASON_VALID_COMPLETION,
    GATEWAY_ACCESS_REASON_ASSISTANT_REFUSAL,
    GATEWAY_ACCESS_REASON_METERED_REASONING_COMPLETION,
    GATEWAY_ACCESS_REASON_GATEWAY_AUTHENTICATION,
    GATEWAY_ACCESS_REASON_GATEWAY_MODEL_ACCESS,
    GATEWAY_ACCESS_REASON_INCOMPLETE_OR_UNSUPPORTED_RESPONSE,
    GATEWAY_ACCESS_REASON_REQUEST_NOT_DISPATCHED,
    GATEWAY_ACCESS_REASON_AUTH_HEADER_UNVERIFIED,
    GATEWAY_ACCESS_REASON_IDENTITY_CONFLICT,
    GATEWAY_ACCESS_REASON_UNSUPPORTED_PROFILE,
    GATEWAY_ACCESS_REASON_RUN_DEADLINE,
    GATEWAY_ACCESS_REASON_RUN_STOPPED,
    GATEWAY_ACCESS_REASON_REQUEST_BUDGET_EXHAUSTED,
)
GATEWAY_ACCESS_DENIAL_UNKNOWN: Final = "unknown"
GATEWAY_ACCESS_DENIAL_AUTHENTICATION: Final = "authentication"
GATEWAY_ACCESS_DENIAL_MODEL_ACCESS: Final = "model_access"
GATEWAY_ACCESS_DENIAL_KINDS: Final = (
    GATEWAY_ACCESS_DENIAL_UNKNOWN,
    GATEWAY_ACCESS_DENIAL_AUTHENTICATION,
    GATEWAY_ACCESS_DENIAL_MODEL_ACCESS,
)
# Observed decision -> expected decision -> check status.
GATEWAY_ACCESS_CASE_STATUSES: Final = {
    GATEWAY_ACCESS_UNVERIFIED: {GATEWAY_ACCESS_ALLOW: "inconclusive", GATEWAY_ACCESS_DENY: "inconclusive"},
    GATEWAY_ACCESS_ALLOW: {GATEWAY_ACCESS_ALLOW: "pass", GATEWAY_ACCESS_DENY: "fail"},
    GATEWAY_ACCESS_DENY: {GATEWAY_ACCESS_ALLOW: "fail", GATEWAY_ACCESS_DENY: "pass"},
}

# Offer ledgers are independent of request outcomes; see docs/states.md for counting.
OFFER_NOT_OFFERED: Final = "not_offered"
OFFER_DISPATCHING: Final = "dispatching"
OFFER_STARTED: Final = "started"
OFFER_CANCELLED: Final = "cancelled"
OFFER_DROPPED_LOCAL: Final = "dropped_local"
OFFER_DROPPED_LATE: Final = "dropped_late"
OFFER_DISPATCHED: Final = "dispatched"
OFFER_UNISSUED: Final = "unissued"
OFFER_OUTCOMES: Final = (
    OFFER_NOT_OFFERED,
    OFFER_DISPATCHING,
    OFFER_STARTED,
    OFFER_CANCELLED,
    OFFER_DROPPED_LOCAL,
    OFFER_DROPPED_LATE,
    OFFER_DISPATCHED,
    OFFER_UNISSUED,
)
TRAFFIC_OFFER_OUTCOMES: Final = (
    OFFER_NOT_OFFERED,
    OFFER_DISPATCHING,
    OFFER_STARTED,
    OFFER_CANCELLED,
    OFFER_DROPPED_LOCAL,
    OFFER_DROPPED_LATE,
)
MULTI_USER_OFFER_OUTCOMES: Final = (
    OFFER_DISPATCHED,
    OFFER_DROPPED_LOCAL,
    OFFER_DROPPED_LATE,
    OFFER_UNISSUED,
    OFFER_NOT_OFFERED,
)
# Terminal here means eligible for final accounting, not immutable during dispatch.
OFFER_TRANSIENT_OUTCOMES: Final = frozenset({OFFER_DISPATCHING})
OFFER_TERMINAL_OUTCOMES: Final = frozenset(OFFER_OUTCOMES) - OFFER_TRANSIENT_OUTCOMES
OFFER_TRANSITIONS: Final = {
    OFFER_NOT_OFFERED: frozenset({OFFER_NOT_OFFERED, OFFER_DISPATCHING, OFFER_DROPPED_LOCAL, OFFER_DROPPED_LATE}),
    OFFER_DISPATCHING: frozenset({OFFER_STARTED, OFFER_DROPPED_LATE, OFFER_NOT_OFFERED}),
    OFFER_STARTED: frozenset({OFFER_CANCELLED, OFFER_DROPPED_LATE, OFFER_NOT_OFFERED}),
    OFFER_CANCELLED: frozenset({OFFER_NOT_OFFERED}),
    OFFER_DROPPED_LOCAL: frozenset(),
    OFFER_DROPPED_LATE: frozenset(),
}


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

# Evidence recovery is independent of the run lifecycle, one state per run directory.
RECOVERY_NONE: Final = "none"
RECOVERY_PENDING: Final = "pending"
RECOVERY_RETRYING: Final = "retrying"
RECOVERY_GIVEN_UP: Final = "given_up"
RECOVERY_STATES: Final = (RECOVERY_NONE, RECOVERY_PENDING, RECOVERY_RETRYING, RECOVERY_GIVEN_UP)
RECOVERY_MAX_ATTEMPTS: Final = 5

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


def check_tokenizer_source(source: str) -> None:
    """Warn about an undeclared tokenizer source, or raise in strict mode."""
    if source in TOKENIZER_SOURCES:
        return
    message = f"Undeclared tokenizer source: {source!r}"
    if STRICT_TRANSITIONS:
        raise AssertionError(message)
    logger.warning(message)


def check_residency_state(state: str) -> None:
    """Warn about an undeclared residency state, or raise in strict mode."""
    if state in RESIDENCY_STATES:
        return
    message = f"Undeclared residency state: {state!r}"
    if STRICT_TRANSITIONS:
        raise AssertionError(message)
    logger.warning(message)


def check_plan_confidence(confidence: str) -> None:
    """Warn about an undeclared plan confidence, or raise in strict mode."""
    if confidence in PLAN_CONFIDENCE:
        return
    message = f"Undeclared plan confidence: {confidence!r}"
    if STRICT_TRANSITIONS:
        raise AssertionError(message)
    logger.warning(message)


def check_judging_stop_reason(reason: str | None) -> None:
    """Warn about an undeclared judging stop reason, or raise in strict mode."""
    if reason is None or reason in JUDGING_STOP_REASONS:
        return
    message = f"Undeclared judging stop reason: {reason!r}"
    if STRICT_TRANSITIONS:
        raise AssertionError(message)
    logger.warning(message)


class JudgeVoteError(AssertionError):
    """A strict vote-vocabulary failure, distinct from endpoint/request exceptions."""


def check_judge_vote_error_reason(reason: str) -> None:
    """Warn about an undeclared judge vote error reason, or raise in strict mode."""
    if reason in JUDGE_VOTE_ERROR_REASONS:
        return
    message = f"Undeclared judge vote error reason: {reason!r}"
    if STRICT_TRANSITIONS:
        raise JudgeVoteError(message)
    logger.warning(message)


def check_tokenizer_calibration(status: str, reason: str | None) -> None:
    """Check the saved calibration vocabulary, including success without a reason."""
    if status in TOKENIZER_CALIBRATION_STATUSES and (reason is None or reason in TOKENIZER_CALIBRATION_REASONS):
        return
    message = f"Undeclared tokenizer calibration: {status!r} / {reason!r}"
    if STRICT_TRANSITIONS:
        raise AssertionError(message)
    logger.warning(message)


def check_gateway_policy_outcome(qualification: str, status: str) -> None:
    """Flag an undeclared qualification or qualification/status pair."""
    if status in GATEWAY_POLICY_STATUSES.get(qualification, ()):
        return
    message = f"Undeclared gateway policy outcome: {qualification!r} / {status!r}"
    if STRICT_TRANSITIONS:
        raise AssertionError(message)
    logger.warning(message)


def check_gateway_access_observation(observed: str, reason: str) -> None:
    """Flag undeclared access observations or reason codes, including unrun cases."""
    if observed in GATEWAY_ACCESS_OBSERVATIONS and reason in GATEWAY_ACCESS_REASONS:
        return
    message = f"Undeclared gateway access observation: {observed!r} / {reason!r}"
    if STRICT_TRANSITIONS:
        raise AssertionError(message)
    logger.warning(message)


def gateway_access_case_status(expected: str, observed: str) -> str:
    """Apply the declared observation/expectation rule without changing unknown-value fallback."""
    status = GATEWAY_ACCESS_CASE_STATUSES.get(observed, {}).get(expected)
    if status is not None:
        return status
    message = f"Undeclared gateway access case: expected {expected!r}, observed {observed!r}"
    if STRICT_TRANSITIONS:
        raise AssertionError(message)
    logger.warning(message)
    return "inconclusive" if observed == GATEWAY_ACCESS_UNVERIFIED else "pass" if observed == expected else "fail"


class OfferTransitionError(AssertionError):
    """A strict offer-guard failure, distinct from request callback assertions."""


def check_offer_transition(old: str, new: str) -> None:
    """Flag traffic ledger writes outside the declared transition table."""
    if new in OFFER_TRANSITIONS.get(old, ()):
        return
    message = f"Undeclared offer transition: {old!r} -> {new!r}"
    if STRICT_TRANSITIONS:
        raise OfferTransitionError(message)
    logger.warning(message)


def check_multi_user_offer_outcome(outcome: str) -> None:
    """Flag a final multi-user offer value outside its own accounting vocabulary."""
    if outcome in MULTI_USER_OFFER_OUTCOMES:
        return
    message = f"Undeclared multi-user offer outcome: {outcome!r}"
    if STRICT_TRANSITIONS:
        raise AssertionError(message)
    logger.warning(message)


def check_metric_state(state: str, value: object) -> None:
    """Reject undeclared metric states or inconsistent value presence in tests; warn otherwise."""
    if state in METRIC_STATES and (value is not None) == (state in METRIC_VALUE_STATES):
        return
    message = f"Invalid metric evidence state: {state!r} (value present: {value is not None})"
    if STRICT_TRANSITIONS:
        raise AssertionError(message)
    logger.warning(message)


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


def recovery_state(state: str | None, journal_present: bool, report_available: bool,
                   summary_present: bool = False, released: bool = False, attempts: int = 0) -> str:
    """Derive recovery from plain inputs; callers normalize a dead writer to interrupted."""
    if (not journal_present or report_available or summary_present or released
            or state not in {RUN_INTERRUPTED, RUN_ERROR}):
        return RECOVERY_NONE
    if state == RUN_ERROR:
        # An error run without the maximum failed attempts is not a recovery case.
        return RECOVERY_GIVEN_UP if attempts >= RECOVERY_MAX_ATTEMPTS else RECOVERY_NONE
    return RECOVERY_RETRYING if attempts else RECOVERY_PENDING


def run_recoverable(state: str | None, journal_present: bool, report_available: bool,
                    summary_present: bool = False, released: bool = False, attempts: int = 0) -> bool:
    """Whether automatic evidence recovery remains pending."""
    return recovery_state(state, journal_present, report_available, summary_present, released, attempts) in {
        RECOVERY_PENDING, RECOVERY_RETRYING}


def describe() -> dict:
    return {
        "tokenizer_sources": list(TOKENIZER_SOURCES),
        "tokenizer_calibration_statuses": list(TOKENIZER_CALIBRATION_STATUSES),
        "tokenizer_calibration_reasons": list(TOKENIZER_CALIBRATION_REASONS),
        "residency_states": list(RESIDENCY_STATES),
        "residency_phases": list(RESIDENCY_PHASES),
        "plan_confidence": list(PLAN_CONFIDENCE),
        "judging_stop_reasons": list(JUDGING_STOP_REASONS),
        "judge_vote_error_reasons": list(JUDGE_VOTE_ERROR_REASONS),

        "gateway_policy_qualifications": sorted(GATEWAY_POLICY_QUALIFICATIONS),
        "gateway_access_expected": sorted(GATEWAY_ACCESS_EXPECTED),
        "gateway_access_observations": sorted(GATEWAY_ACCESS_OBSERVATIONS),
        "gateway_access_reasons": sorted(GATEWAY_ACCESS_REASONS),
        "gateway_access_denial_kinds": sorted(GATEWAY_ACCESS_DENIAL_KINDS),
        "offer_outcomes": sorted(OFFER_OUTCOMES),
        "traffic_offer_outcomes": sorted(TRAFFIC_OFFER_OUTCOMES),
        "multi_user_offer_outcomes": sorted(MULTI_USER_OFFER_OUTCOMES),
        "offer_terminal_outcomes": sorted(OFFER_TERMINAL_OUTCOMES),
        "offer_transient_outcomes": sorted(OFFER_TRANSIENT_OUTCOMES),
        "gateway_policy_statuses": {key: sorted(value) for key, value in GATEWAY_POLICY_STATUSES.items()},
        "gateway_access_case_statuses": {key: dict(value) for key, value in GATEWAY_ACCESS_CASE_STATUSES.items()},
        "offer_transitions": {key: sorted(value) for key, value in OFFER_TRANSITIONS.items()},
        "metric_states": list(METRIC_STATES),
        "metric_value_states": [state for state in METRIC_STATES if state in METRIC_VALUE_STATES],
        "metric_state_labels": dict(METRIC_STATE_LABELS), "metric_state_actions": dict(METRIC_STATE_ACTIONS),
        "check_statuses": list(CHECK_STATUSES), "problem_statuses": sorted(PROBLEM_STATUSES),
        "exit_codes": {**EXIT_CODES, "cancelled": EXIT_CODE_CANCELLED,
                       "usage_error": EXIT_CODE_USAGE, "default": EXIT_CODE_DEFAULT},
        "request_statuses": list(REQUEST_STATUSES), "run_states": list(RUN_STATES),
        "recovery_states": list(RECOVERY_STATES), "recovery_max_attempts": RECOVERY_MAX_ATTEMPTS,
        "request_outcomes": list(REQUEST_OUTCOMES),
        "request_outcome_errors": sorted(REQUEST_OUTCOME_ERRORS),
        "request_outcome_excluded": sorted(REQUEST_OUTCOME_EXCLUDED),
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
