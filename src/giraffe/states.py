"""Single source of truth for lifecycle vocabularies whose values are a public contract; see docs/states.md.

Modules whose bytes are hashed into baseline workload identity (load_test, multi_user,
scale_down, traffic, gateway_access, gateway_policy and others) keep inline copies of
these literals: editing them would make saved baselines not comparable.
"""

from typing import Final


# Check / run verdict: report.json overall/checks[].status and CLI JSON outcome (models.Status).
CHECK_STATUSES: Final = ("pass", "fail", "inconclusive", "skipped", "blocked")
PROBLEM_STATUSES: Final = frozenset({"fail", "inconclusive", "blocked"})
EXIT_CODES: Final = {"pass": 0, "fail": 1, "inconclusive": 2, "blocked": 3}
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

# Run stop reasons: RunState.reason and report.json abort_reason (fixed text only).
STOP_CANCELLED_BY_USER: Final = "cancelled by user"
STOP_RUN_CANCELLED: Final = "run cancelled"
STOP_MAX_DURATION: Final = "maximum run duration reached"
STOP_REQUEST_BUDGET: Final = "maximum request budget reached"
STOP_ERROR_THRESHOLD: Final = "error threshold reached"

# Condition lifecycle: observations.targets.*.conditions[].status before aggregation.
CONDITION_LIFECYCLE: Final = ("waiting", "running", "finished")
CONDITION_UNFINISHED: Final = frozenset({"waiting", "running"})

# Scale-down control phase: .scale-down-status.json phase.
SCALE_DOWN_PHASES: Final = ("baseline", "waiting_for_output", "armed", "observing",
                          "post_removal", "finalizing", "finished")
SCALE_DOWN_CLOSING_PHASES: Final = frozenset({"finalizing", "finished"})
