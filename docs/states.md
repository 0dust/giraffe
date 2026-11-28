# States and lifecycles

## Run verdict and check status

| Value | Meaning | Terminal? |
| --- | --- | --- |
| `pass` | Acceptance criteria met. | Yes |
| `fail` | Acceptance criteria failed. | Yes |
| `inconclusive` | Evidence cannot establish a result. | Yes |
| `skipped` | Check was not evaluated. | Yes |
| `blocked` | A prerequisite prevented evaluation. | Yes |

Where you see it: `report.json` `overall` and `checks[].status`, CLI JSON `outcome`.

| Exit code | Meaning |
| --- | --- |
| `0` | `pass` |
| `1` | `fail` |
| `2` | `inconclusive` |
| `3` | `blocked` |
| `4` | Usage or configuration error |
| `130` | Cancelled (Ctrl+C) |

- A `skipped` overall verdict exits 2.
- Exit 3 wins over the verdict when a required check is blocked or a target had a client error, so `outcome` can be `fail` with exit code 3.
- `--fail-on` never changes exit 3 or 130.

| Check status | JUnit mapping |
| --- | --- |
| `fail` | `failure` |
| `blocked` | `error` |
| `skipped`, `inconclusive` | `skipped` |

## Baseline comparability

`report.json` exposes `baseline.targets[].comparability` and the worst target verdict
at `baseline.comparability`. These describe like-for-like evidence, independently
of regression `status` or absolute acceptance.

| Verdict | Meaning |
| --- | --- |
| `comparable` | No comparison reasons were recorded. |
| `not_comparable` | At least one reason establishes incompatible work or evidence. |
| `unknown` | Reasons only establish missing or unknown evidence. |

A target is `not_comparable` if any `reason_details[].code` maps to that verdict;
otherwise it is `unknown` if any code maps to unknown, and `comparable` with no
reasons. The report uses the worst target verdict:
`not_comparable` > `unknown` > `comparable` (empty targets are `comparable`).
In gateway-access-only comparisons, `reasons` and `comparability` cover only
access reasons, including target identity; unrelated shared-workload reasons do
not participate. `comparable` can coexist with an `inconclusive` comparison
`status`, for example when there are too few samples to conclude a regression.
`reasons` prose stays for people; agents should read `comparability` and
`reason_details[].code`. Details about one side include `side: "current"` or
`side: "baseline"`; setting and field names use `key`.
Unrecorded reasons retain the `comparison_unavailable` fallback and log a warning
in production; strict test mode raises, so new reason paths must declare a code.
`giraffe describe` exposes `baseline_comparability`, `baseline_reason_codes`, and
`load_acceptance_changes` in its `states` block.

| Code | Verdict | Meaning |
| --- | --- | --- |
| `setting_missing` | `unknown` | Recorded setting is missing. |
| `setting_unknown` | `unknown` | Recorded setting cannot be established. |
| `target_unknown` | `unknown` | Target identity key is missing from either run. |
| `config_invalid` | `unknown` | Recorded configuration is invalid. |
| `request_pack_unknown` | `unknown` | Request pack identity is missing. |
| `multi_user_policy_unknown` | `unknown` | Multi-user summary or window policy is missing. |
| `fixture_pack_unknown` | `unknown` | Fixture pack identity is missing or unknown. |
| `custom_fixtures_unhashed` | `unknown` | Custom fixture content has no recorded hash. |
| `gateway_access_missing` | `unknown` | Baseline gateway access check is missing. |
| `gateway_access_settings_unknown` | `unknown` | Gateway access settings are missing or invalid. |
| `gateway_access_workload_version_unknown` | `unknown` | Gateway access workload version is missing or invalid. |
| `load_samples_missing` | `unknown` | Request samples are absent from one load run. |
| `user_fairness_unknown` | `unknown` | Per-user fairness evidence is missing or unqualified. |
| `user_delivery_incomplete` | `unknown` | Per-user delivery or scenario evidence is incomplete. |
| `comparison_unavailable` | `unknown` | Unrecorded or undeclared comparison reason fallback. |
| `setting_differs` | `not_comparable` | Recorded setting differs. |
| `limit_constrained` | `not_comparable` | Changed run limit constrained executed work. |
| `target_differs` | `not_comparable` | Recorded target identity values differ. |
| `schema_differs` | `not_comparable` | Report schema is unsupported. |
| `timing_method_differs` | `not_comparable` | Client timing measurement methods differ. |
| `outcome_counting_differs` | `not_comparable` | Request outcome counting rules differ; rerun the baseline. |
| `run_aborted` | `not_comparable` | Run was aborted. |
| `evaluation_failed` | `not_comparable` | Answer evaluation failed. |
| `config_unsupported` | `not_comparable` | Recorded configuration version is unsupported. |
| `request_source_differs` | `not_comparable` | Effective request source differs. |
| `request_pack_differs` | `not_comparable` | Request pack identity differs. |
| `judge_incomplete` | `unknown` | Judge results are incomplete. |
| `multi_user_policy_differs` | `not_comparable` | Multi-user summary or window policy differs. |
| `cancellation_grading_differs` | `not_comparable` | Cancellation verdict definitions differ. |
| `fixture_pack_differs` | `not_comparable` | Fixture pack identity differs. |
| `workload_format_changed` | `not_comparable` | Concurrent workloads use different report formats. |
| `target_absent` | `not_comparable` | Target is absent from the baseline. |
| `gateway_access_settings_differs` | `not_comparable` | Gateway access settings differ. |
| `gateway_access_transport_differs` | `not_comparable` | Gateway access transport settings differ. |
| `gateway_access_workload_version_differs` | `not_comparable` | Gateway access workload versions differ. |
| `user_delivery_differs` | `not_comparable` | Per-user work differs or cannot be matched. |
| `user_delivery_zero_baseline` | `unknown` | Zero baseline delivery prevents relative comparison. |
| `user_slots_differs` | `not_comparable` | Changed available slots constrained scheduled offers. |
| `load_work_excluded` | `not_comparable` | Load work cannot be compared. |
| `scale_down_unsupported` | `not_comparable` | Separate scale-down events cannot be compared. |
| `user_fairness_differs` | `not_comparable` | Per-user fairness work or overlap differs or lacks evidence. |

### Load acceptance changes

Where you see it: `baseline.targets[].load_test.points[].acceptance_change`.
These preserve the existing acceptance comparison independently of comparability.

| Value | Meaning |
| --- | --- |
| `not_established` | Acceptance is not established. |
| `not_tested_in_current` | Level was not tested in the current run. |
| `not_tested_in_baseline` | Level was not tested in the baseline. |
| `work_changed` | Work changed; acceptance change is not attributable. |
| `unchanged_accepted` | Tested load remains accepted. |
| `unchanged_not_accepted` | Tested load remains not accepted. |
| `lost` | Previously accepted tested load now fails. |
| `gained` | Previously failing tested load is now accepted. |
| `accepted_baseline_not_established` | Accepted now; baseline acceptance was not established. |
| `not_established_in_current` | Preservation is unverified in the current run. |
| `not_established_in_baseline` | Baseline acceptance was not established. |

## Check phases

| Phase | Meaning | Allowed next phases |
| --- | --- | --- |
| `inference` | Check workload is pending or running. | `inference`, `judging`, `done` |
| `judging` | Inference finished; judge scoring is pending. | `judging`, `done` |
| `done` | No work remains for this check. | `done` |

Every configured check starts in `inference` for each target. When inference finishes,
`correctness`, `capacity`, and `load` enter `judging` if the request pack contains
judge-scored specs and judging is still pending. Other checks go directly to `done`.
After judging, all checks become `done`.

`unfinished` is a separate flag: a check can be `done` and still be unfinished
because it stopped early. The phase is internal runner state, not a report field.
These values are not exposed in `giraffe describe`; its `judged_checks` lists the checks that wait for judging.

## Request status

| Value | Meaning | Terminal? |
| --- | --- | --- |
| `completed` | Response processing completed. | Yes |
| `failed` | Request failed. | Yes |
| `timeout` | Request timed out. | Yes |
| `cancelled` | Request was cancelled. | Yes |

Where you see it: `report.json` `requests[].status`, `evidence.jsonl`.

`valid` is true only for `completed` requests whose response was usable. Request outcomes distinguish invalid responses from excluded cases such as reasoning-budget exhaustion.

## Request outcomes

Each record declares its derived `outcome` in `report.json` and `request_completed`
events. This is an output field, derived from the request evidence when loading old
reports too. The first matching row below wins; correctness (`score`) is a separate
axis and does not change the outcome.

| Precedence | Outcome | Meaning | Counting class |
| --- | --- | --- | --- |
| 1 | `not_dispatched` | Preparation failed before the request could be sent; explicitly marked `not_dispatched: true`. Missing dispatch timing alone does not establish this. | Excluded |
| 2 | `intentional_stop` | `intentional` is true: a designed client cancel, deadline, chunk limit, or timeout override stopped the request. | Excluded |
| 3 | `cancelled` | Status is `cancelled` without an intentional stop; `termination_reason` describes the run, user, memory, or external stop. | Excluded |
| 4 | `timeout` | Status is `timeout` without an intentional stop. | Error |
| 5 | `budget_exhausted` | The reasoning-budget predicate identifies exhaustion without a useful answer, including matching `failed` records. | Excluded |
| 6 | `failed` | Status is `failed` and no earlier outcome matched, including connection errors. | Error |
| 7 | `invalid` | Status is `completed` and `valid` is false. | Error |
| 8 | `ok` | Status is `completed` and `valid` is true. | Evaluated success |

Errors are `invalid + failed + timeout`. Evaluated requests are `ok + errors`;
error rate is `errors / evaluated`, or null when evaluated is zero. Excluded
outcomes (`intentional_stop`, `cancelled`, `budget_exhausted`, `not_dispatched`)
are reported separately and enter neither numerator nor denominator.
`giraffe describe` exposes `request_outcomes`, `request_outcome_errors`, and
`request_outcome_excluded` in its `states` block.

Preparation failures are Giraffe-side failures. They count toward the live failed
count and `stop_after_errors`, while staying outside the server error rate. A
check, load point or group, traffic stage, or multi-user window or user containing
one cannot pass. Only a passing result becomes inconclusive; all other statuses
keep their status and existing failure, blocked, execution-error, or skipped
explanations. A measurement with no acceptance criteria remains `measured` and
includes the preparation warning. The shared reason is: "N request(s) could not be prepared
locally and were not sent; fix the request inputs and rerun."

### Module rules

The shared outcome and error denominator leave these deliberate additions:

| Module | Additional rule |
| --- | --- |
| Runner | Serving failures exclude stress-scenario timeouts and context rejections. The live failed count includes preparation failures and `score is False`; probe scenarios are otherwise excluded. Designed deadline/cancel probes remain intentional stops when their request timeout fires first. |
| Multi-user | User-visible errors also include `score is False` and truncated conversation turns. Missing terminal turns remain in that error denominator. Intentional rows remain outside timing-attainment attempts. Preparation failures still stop the run at the error threshold and prevent affected users and windows from passing; existing throughput and comparison failures retain their verdicts, while measurement-only results stay measured with the warning. |
| Traffic | A non-intentional cancellation still blocks a stage pass. A preparation failure prevents a passing stage; existing failure explanations remain unchanged. Offer-ledger `dropped_*` and `not_offered` counts have no request record. Execution errors remain failed and evaluated in aggregate counts; nested `outcome_counts` partitions actual records only. |
| Load test | Offer-ledger `not_sent` stays separate. Any cancellation still makes a group inconclusive. |
| Scale down / gateway policy | Preparation failures and reasoning-budget exhaustion are not server errors and cannot establish a pass. Observed server or policy failures still take precedence. Scale down additionally requires clean stream termination, a recognized terminal finish reason, and no response-level error. Gateway HTTP 429 evidence is classified against the declared policy. |

New manifests declare `outcome_counting_version: 2`; an absent value means version
1. Different versions produce baseline reason `outcome_counting_differs`, verdict
`not_comparable`, and prose "Request outcome counting rules differ; rerun the baseline."

## Run state

| Value | Meaning | Terminal? |
| --- | --- | --- |
| `queued` | Accepted, not started (reserved; current builds go straight to running). | No |
| `running` | Run is executing. | No |
| `stopping` | Stop requested, saving partial results. | No |
| `completed` | A report was saved; the verdict may still be fail. A stopped run is `completed` with `abort_reason` set. | Yes |
| `interrupted` | The process died or was shut down. A partial report may exist; recover with the command documented in [Resource limits](resources.md#recovering-an-interrupted-run). | Until recovery |
| `error` | The run could not be saved or recovered. | Yes |

Where you see it: `.web.json` `state`, run API `state`.

CLI-owned runs write `running`, `completed`, `interrupted` or `error`. They never report `stopping`.

### Transitions

| From | To |
| --- | --- |
| `queued` | `running`, `stopping`, `completed`, `interrupted`, `error` |
| `running` | `stopping`, `completed`, `interrupted`, `error` |
| `stopping` | `completed`, `interrupted`, `error` |
| `interrupted` | `completed`, `error` |
| `completed` | `error` |
| `error` | `interrupted` |

An interrupted run can recover on the next server start to `completed` or `error`.
After the server gives up recovering a run, `giraffe report <run-dir> --html` can manually recover its retained evidence and move `error` to `interrupted`.
The API reports `error` for a completed run whose saved report is unreadable. This does not rewrite `.web.json`.
Repeated writes of the same run state are allowed.

## Stop reasons

| Value | Meaning | Terminal? |
| --- | --- | --- |
| `cancelled by user` | Explicit user stop. | Yes |
| `run cancelled` | Stopped by a timeout or forced task cancellation rather than an explicit user stop. | Yes |
| `maximum run duration reached` | Run duration limit reached. | Yes |
| `maximum request budget reached` | Request budget reached. | Yes |
| `error threshold reached` | Error threshold reached. | Yes |

Where you see it: `report.json` `abort_reason`.

Memory-limit and endpoint-abort reasons are free text.

Compare the fixed strings exactly; treat any other non-empty value as a stop.

### Stop causes

| Kind | Fixed text or free text | Soft? |
| --- | --- | --- |
| `user` | `cancelled by user` | No |
| `cancelled` | `run cancelled` | No |
| `duration` | `maximum run duration reached` | No |
| `request_budget` | `maximum request budget reached` | Yes |
| `error_threshold` | `error threshold reached` | No |
| `endpoints_unresponsive` | free text | No |
| `memory` | free text | No |

The first non-soft cause wins; a request budget is soft and can be replaced by a non-soft cause, while a soft cause never replaces an existing cause.
Polling checks user stop, duration, all endpoints unresponsive, then error threshold in that order; memory sampling resolves those pending stops before latching memory, and outer cancellation latches user stop if requested or cancellation otherwise.

After inference finishes, a user stop latches only when no cause exists: it never replaces a request budget. User-stop polling, forced cancellation, and the judging stop path follow the same phase rule. The duration checks continue to use the inference deadline during judging.
For example, with `max_requests=1`, cancelling during judging leaves `observations.stop_cause.kind` as `request_budget` and sets `observations.judging.stop_reason` to `cancelled`. With no earlier cause, a judging-only user stop latches `user`.

`report.json` `observations.stop_cause` is the raw latched cause as `{"kind", "text", "scope"}`, or null when no cause was latched. It records the cause across inference and judging. A user cause can have `scope: "gateway"`; other causes have null scope.
`abort_reason` is the verdict-relevant stop text: it may be null for a budget-only or optional memory stop. On `run_stopping`, it is raw cause text; on `run_finished`, it is verdict text. A recovered report's `abort_reason` is free text describing the interruption and recorded request count, not a fixed stop reason; read its `observations.stop_cause.kind` for the cause.

Every `run_stopping` and `run_finished` event includes both `abort_reason` and `stop_kind`, including explicit nulls. A web cancel acknowledgment carries the already-announced cause or null; it does not predict what the worker will latch. During inference, the runner's own `run_stopping` follows. The last `run_finished.stop_kind` is authoritative. If cancellation arrives during evidence-journal close, the runner has already emitted its final event; the later web terminal event carries the phase-correct latched cause and updated `abort_reason`, superseding that earlier event.

Top-level `stop_kind` is also present in run detail, run list rows, every events response (live feed, journal, and reset), final and interrupted CLI JSON output, and `.web.json` metadata for web, CLI, recovered, and `giraffe report` runs. Summary-only export failures carry it too. Live API surfaces expose the announced non-soft cause, or explicit null when none has been announced. Once the runner announces `run_finished`, its fields become the live cause on both web and CLI, including a soft request budget during export. CLI writes those finished fields to `.web.json` while the run is still `running`; before any announcement, initial CLI metadata can omit them. Cancel acknowledgments for active runs mirror the live fields; terminal cancel responses retain their state-only contract. Final surfaces use the latched cause, including a soft request budget. A saved report is authoritative over metadata. Journal causes bypass the bounded evidence queue; recovery uses the last complete cause line.
`giraffe describe` and GET `/api/capabilities` expose `stop_kinds`, `stop_soft_kinds`, and the fixed-text-to-kind map `stop_reason_kinds` in `states`.

## Condition lifecycle

| Value | Meaning | Terminal? |
| --- | --- | --- |
| `waiting` | Condition has not started. | No |
| `running` | Condition is being observed. | No |
| `finished` | Condition observation finished. | Yes |

Where you see it: `observations.targets.*.conditions[].status` before aggregation.

After aggregation, the lifecycle value moves to `lifecycle_status`; the aggregate vocabulary is documented below.

Recovered condition rows use status `interrupted`.

### Transitions

| From | To |
| --- | --- |
| `waiting` | `running`, `finished` |
| `running` | `running`, `finished` |
| `finished` | `running` |

Repeated dispatch groups can restart a finished condition.

## Scale-down phase

| Value | Meaning | Terminal? |
| --- | --- | --- |
| `baseline` | Collecting baseline samples. | No |
| `waiting_for_output` | Waiting for active long-answer output. | No |
| `armed` | Ready to accept an external removal marker. | No |
| `observing` | Observing the marked removal. | No |
| `post_removal` | Measuring after confirmed removal. | No |
| `finalizing` | Completing pending work and collecting final evidence. | No |
| `finished` | Scale-down observation finished. | Yes |

Where you see it: `.scale-down-status.json` `phase`.

### Transitions

| From | To |
| --- | --- |
| `baseline` | `waiting_for_output`, `finalizing`, `finished` |
| `waiting_for_output` | `armed`, `finalizing`, `finished` |
| `armed` | `observing`, `finalizing`, `finished` |
| `observing` | `post_removal`, `finalizing`, `finished` |
| `post_removal` | `finalizing`, `finished` |
| `finalizing` | `finished` |
| `finished` | None |

A phase write the table does not declare is still saved, and logged as a warning; the test suite fails on it.

Phases normally follow the declared order. Cleanup may skip to `finalizing`; the runner can force `finished`.

## Connection test result

| Value | Meaning |
| --- | --- |
| `connected` | A test request completed successfully. |
| `unverified` | The configured model can be tried, but model discovery could not verify it. |
| `needs_model` | A model must be selected before a test request can run. |

Where you see it: POST `/api/targets/test-connection` response `status`.

`connected` and `unverified` are usable connection results for the context-window cache gate. Errors are HTTP errors, not an additional status.

| Value | Meaning |
| --- | --- |
| `unavailable` | Context-window discovery could not obtain usable metadata. |
| `no_window` | Discovery completed without a context-window value for this model. |

Where you see it: the same response's `context_window_reason`. It is null when a context window is found.

## Token-evidence probe

| Value | Meaning | Terminal? |
| --- | --- | --- |
| `not_probed` | No compatibility probe result has been observed. | No |
| `probing` | An option was rejected; another bounded probe may try the remaining options. | No |
| `observed` | A stream completed; inspect the field states for usable evidence. | Yes |
| `unavailable` | Probing could not complete, exhausted its attempts, or had no remaining budget. | Yes |

Where you see it: `report.json` `observations.targets.*.token_evidence.status`.

| From | To |
| --- | --- |
| `not_probed` | `probing`, `observed`, `unavailable` |
| `probing` | `probing`, `observed`, `unavailable` |
| `observed` | `observed` |
| `unavailable` | `unavailable` |

The probe scheduler stops requesting probes after an observed or unavailable result; those terminal values remain unchanged.

| Value | Meaning |
| --- | --- |
| `unverified` | No completed probe establishes support. |
| `supported` | The probe returned the requested token boundaries or both usage counts. |
| `partial` | The probe returned only one of the two usage counts. |
| `not_returned` | A completed probe omitted the requested evidence. |
| `rejected` | The endpoint rejected the corresponding request options. |

Where you see it: `report.json` `observations.targets.*.token_evidence.logprobs` and `.usage`.

These are target probe field states. Request-local copies in `requests[].workload.token_evidence` can additionally qualify fields as `non_streamed`, `disabled_by_request`, or `shape_unverified`.

## Restart observation

| Value | Meaning | Terminal? |
| --- | --- | --- |
| `blocked` | Restart is not ready: the hook could not run or readiness failed. `reason` explains which. | On failure |
| `hook_completed` | The configured hook succeeded; inference readiness is still being checked. | No |
| `ready` | A readiness poll and inference request succeeded. | Yes |

Where you see it: `report.json` `observations.targets.*.restart.status`.

| From | To |
| --- | --- |
| `blocked` | `hook_completed` |
| `hook_completed` | `ready`, `blocked` |
| `ready` | None |

An observation starts `blocked`. Hook failures leave it there; only a successful hook enters readiness checking. A final `blocked` result is not retried within that observation.

## Telemetry snapshot and discovery status

| Value | Meaning | Discovery? |
| --- | --- | --- |
| `available` | Recognized metric samples were returned. | Yes |
| `stale_samples` | Only stale source samples were returned. | Yes |
| `absent_families` | Metrics format was recognized but relevant families were absent. | Yes |
| `unsupported_format` | Metrics format or runtime profile was not recognized. | Yes |
| `empty_response` | The response contained no usable statistics. | Yes |
| `connection_failure` | Collection failed or timed out. | Yes |
| `run_stopped` | Collection could not start before stop or deadline. | No |
| `authentication_denied` | The runtime metrics endpoint returned HTTP 401 or 403. | Yes |
| `endpoint_absent` | The runtime metrics endpoint returned HTTP 404. | Yes |
| `response_limit` | The response exceeded the bounded body size. | Yes |

Where you see it: `report.json` `observations.targets.*.serving_telemetry.snapshots[].status`, nested `.host.status`, and `observations.targets.*.runtime_metrics_discovery.status`.

Discovery remembers runtime scrape outcomes except `run_stopped`, which returns before discovery is recorded. Host scrapes do not update runtime discovery. `available`, `stale_samples`, and `absent_families` are usable collection outcomes and do not count toward `serving_telemetry.failures`; this does not make stale or missing measurements fresh evidence. Same-origin discovery is retried only after `available` or `stale_samples`; other remembered outcomes are reused without another attempt.

## Gateway attribution state

| Value | Meaning | Check status |
| --- | --- | --- |
| `not_selected` | Attribution was not selected. | `skipped` |
| `unavailable` | The configured evidence endpoint or credentials were unavailable. | `inconclusive` |
| `unsupported` | No supported adapter or read-only spend-log interface was available. | `inconclusive` |
| `unverified` | Evidence could not establish complete attribution. | `inconclusive` |
| `verified` | Every marked request matched its identity and final token counts. | `pass` |
| `mismatch` | Marked requests had different identity or token attribution. | `fail` |

Where you see it: `report.json` `observations.targets.*.gateway_attribution.state` and the attribution check's `metrics.state`.

Every declared state maps to the check status in the table. An undeclared state is a programming error.

## Condition aggregate status

| Value | Meaning |
| --- | --- |
| `waiting` | The condition has not started. |
| `running` | The condition is active or has requests in progress. |
| `passed` | An explicit passing verdict or all completed requests establish success. |
| `failed` | An explicit failing verdict or request failures establish failure. |
| `inconclusive` | The condition completed without a supported verdict. |
| `skipped` | The condition was not evaluated or stopped before any request started. |
| `stopped` | The condition started but ended early due to a run stop. |

Where you see it: `report.json` `conditions[].status` and condition API/event `aggregate_status`. The original lifecycle value is retained as `lifecycle_status`.

| Check verdict | Aggregate status |
| --- | --- |
| `pass` | `passed` |
| `fail` | `failed` |
| `blocked`, `inconclusive` | `inconclusive` |
| `skipped` | `skipped` |

An explicit verdict takes precedence over stop-reason classification.

## Condition stop reasons

| Value | Meaning |
| --- | --- |
| `run stopped` | The run stop signal or explicit cancellation ended the condition. |
| `time limit reached` | The run deadline ended the condition. |
| `request budget reached` | No request budget remained. |
| `check stopped early` | Another stop cause, including memory limits or an unresponsive endpoint, ended the condition. |

Where you see it: condition rows and condition API/event `reason`.

Aggregation compares these fixed values exactly. For reason text emitted by source-hashed workloads, it also retains the case-insensitive markers `run stopped`, `time limit`, `request budget`, `cancelled`, and `aborted`. A stopped condition aggregates to `stopped` if any request started, or `skipped` otherwise, unless an explicit verdict or request failure takes precedence.

## Reading states from a program

`giraffe describe` and GET `/api/capabilities` include a `states` block with vocabularies, transitions, stop reasons and exit codes.
Its `exit_codes` maps verdicts to codes and includes `cancelled`, `usage_error` and `default` keys. Stop reasons are open to free text.

Run detail, history, events, start and cancel responses include `terminal`.
It is true for `completed`, `interrupted` and `error`: no process is working on the run.

Run detail, history and events also include `recoverable`. It is true only for an `interrupted` run that still has its `evidence.jsonl` and has no valid saved report; such a run can still become `completed` or `error`. A valid report finishes recovery even if journal cleanup failed or a crash left the journal behind. To wait for a final state, poll until `terminal` is true and `recoverable` is false, with your own time limit: recovery runs when the server starts or when you run the recovery command.

The web metadata helper, CLI run writer, and storage metadata updater check run-state writes against the transition table above. A write the table does not declare is still saved, and logged as a warning; the test suite fails on it.

POST `/api/runs/{id}/cancel` includes `result`:

| Value | Meaning |
| --- | --- |
| `stopping` | This call requested a stop. |
| `already_stopping` | The active run was already stopping. |
| `already_finished` | The run is terminal. |
| `not_owned` | The run is in progress but this server does not own it. |

## Server admission

Server activities are `run_active`, `run_starting`, `run_stopping` and `connection_test_running`.
The first listed blocker wins. HTTP admission conflicts listed in this table carry `X-Giraffe-Error-Code`
with the blocking activity code on their 409 responses; other 409s are outside this contract, and existing status and detail text stay the same.
`recovery` is the background recovery pass, not an HTTP endpoint.

| Operation | Ordered blockers |
| --- | --- |
| `start_run` | `run_active`, `run_stopping`, `connection_test_running` |
| `test_connection` | `run_active`, `run_starting`, `connection_test_running` |
| `tokenizer_preview` | `run_active` |
| `compare` | `run_active` |
| `recovery` | `run_active`, `run_starting`, `run_stopping` |

Test-connection and tokenizer preview are allowed while the previous run is stopping because neither starts another measurement run.
Tokenizer preview is allowed during a connection test: the UI resolves the tokenizer automatically while the user may be testing the connection, and both only read.
Test-connection is allowed during storage cleanup and saved-report comparison because neither touches targets.

## Scale-down command outcomes

`mark-scale-down` has its own outcomes and exit codes: `accepted` exits 0, `rejected` exits 1, and `submitted_without_acknowledgment` exits 2.

Source of truth: `src/giraffe/states.py`.
