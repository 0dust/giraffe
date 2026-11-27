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
| `error` | None |

An interrupted run can recover on the next server start to `completed` or `error`.
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

## Condition lifecycle

| Value | Meaning | Terminal? |
| --- | --- | --- |
| `waiting` | Condition has not started. | No |
| `running` | Condition is being observed. | No |
| `finished` | Condition observation finished. | Yes |

Where you see it: `observations.targets.*.conditions[].status` before aggregation.

After a run ends, each condition's `status` is an aggregate (`passed`, `failed`, `inconclusive`, `skipped`, `stopped`) and the lifecycle value moves to `lifecycle_status`.

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

## Reading states from a program

`giraffe describe` and GET `/api/capabilities` include a `states` block with vocabularies, transitions, stop reasons and exit codes.
Its `exit_codes` maps verdicts to codes and includes `cancelled`, `usage_error` and `default` keys. Stop reasons are open to free text.

Run detail, history, events, start and cancel responses include `terminal`.
It is true for `completed`, `interrupted` and `error`: no process is working on the run.

Run detail, history and events also include `recoverable`. It is true only for an `interrupted` run that still has its `evidence.jsonl` and has no valid saved report; such a run can still become `completed` or `error`. A valid report finishes recovery even if journal cleanup failed or a crash left the journal behind. To wait for a final state, poll until `terminal` is true and `recoverable` is false, with your own time limit: recovery runs when the server starts or when you run the recovery command.

The server checks every run-state write against the transition table above. A write the table does not declare is still saved, and logged as a warning; the test suite fails on it.

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
