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

## Request status

| Value | Meaning | Terminal? |
| --- | --- | --- |
| `completed` | Response processing completed. | Yes |
| `failed` | Request failed. | Yes |
| `timeout` | Request timed out. | Yes |
| `cancelled` | Request was cancelled. | Yes |

Where you see it: `report.json` `requests[].status`, `evidence.jsonl`.

`valid` is true only for `completed` requests whose response was usable; `completed` with `valid: false` is a failed response.

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

Where agents see it: `report.json` `observations.stop_cause` is the raw latched cause as `{"kind", "text", "scope"}`, or null when no cause was latched; `run_stopping` and `run_finished` events include its `stop_kind` (or null).
`abort_reason` is the verdict-relevant stop text, while `stop_cause` records what actually stopped work even when a budget-only or optional memory stop leaves `abort_reason` null.
`observations.stop_cause` describes what stopped inference or workload work; `observations.judging.stop_reason` describes what stopped the later judging phase.
Callers should read both fields when judging ran to distinguish the workload stop from the judging stop.
For example, with `max_requests=1`, cancelling during judging leaves `observations.stop_cause.kind` as `request_budget` and sets `observations.judging.stop_reason` to `cancelled`.
A user cause can have `scope: "gateway"`; other causes have null scope.
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

Start conflicts keep their HTTP 409 status and detail text. `X-Giraffe-Error-Code` identifies `run_active`, `run_stopping` or `connection_test_running`.

`mark-scale-down` has its own outcomes and exit codes: `accepted` exits 0, `rejected` exits 1, and `submitted_without_acknowledgment` exits 2.

Source of truth: `src/giraffe/states.py`.
