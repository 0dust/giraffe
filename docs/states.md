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
| `interrupted` | The process died or was shut down before a report was saved; recover with the command documented in [Resource limits](resources.md#recovering-an-interrupted-run). | Yes |
| `error` | The run could not be saved or recovered. | Yes |

Where you see it: `.web.json` `state`, run API `state`.

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

Phases are entered in this order; a run may skip ahead to `finalizing`.

Source of truth: `src/giraffe/states.py`.
