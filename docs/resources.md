# Saved reports on disk

Giraffe never deletes saved runs automatically. The default storage limits are:

| Setting | Default | Effect |
| --- | --- | --- |
| `max_total_storage` | 5 GiB (5,368,709,120 bytes) | Refuse a new run when saved runs already exceed this total. Saved baselines are counted separately and excluded from the limit. |
| `min_free_disk` | 1 GiB (1,073,741,824 bytes) | Refuse a new run when available disk space is below this amount. |
| `max_run_storage` | 256 MiB (268,435,456 bytes) | Omit retained response bodies at export when the report JSON would exceed this amount. |

Set any limit to `0` to disable it. `giraffe web`, `giraffe run`, and `giraffe storage` accept `--max-total-storage`, `--min-free-disk`, and `--max-run-storage`, with sizes such as `500MiB` or `2GiB`. Every disk-size display uses binary units (B, KiB, MiB, GiB, TiB): 1 KiB = 1,024 B, 1 MiB = 1,048,576 B and 1 GiB = 1,073,741,824 B. The Runs page cleanup target is entered in MiB and converted to whole bytes. Legacy decimal flag suffixes remain accepted with their original meaning; displayed sizes always use binary units. See the [NIST binary prefix definitions](https://pml.nist.gov/cuu/Units/binary.html). For CLI runs, storage checks apply to the parent directory of the run output. Root aliases such as macOS `/tmp` are resolved; symlinked children are still excluded. When both start limits are disabled, startup skips storage inspection. Pass the same configured flags to `giraffe storage` to display those limits.

The per-run limit applies to JSON, including its storage notice. Export first removes response bodies from requests without failure evidence, then from the remaining requests in recorded order. Failure evidence includes incorrect answers, failed or invalid completions, evaluation errors, and requests referenced by failing, blocked, or inconclusive checks. Response text, reasoning text, tool response content, token text, and judge rationale follow the existing response retention rules. Request metadata, counts, scores, timings, check statuses, and the overall verdict remain. If metadata alone exceeds the JSON limit, it is saved in full and `manifest.storage.limit_exceeded` is true. The JSON and HTML report display explicit body omissions. HTML, metadata files, markers, and temporary files also occupy disk space and count toward saved-run totals; the JSON limit is therefore a soft per-run export limit, not a hard directory quota.

`--retention all`, `failures`, and `none` control whether response text is kept: all responses, only failure evidence, or no responses. Request metadata (timings, status, scores) is always saved. Retention does not cap disk usage; storage limits do.

Use `giraffe storage --runs-dir runs` or the Runs page to inspect saved runs on disk, saved baselines, available disk space, and limits. `giraffe cleanup --runs-dir runs --older-than 30 --keep-latest 10 --max-total 2GiB` previews cleanup; add `--yes` to delete the preview's eligible runs. The criteria combine with OR: old runs, runs beyond the newest count, and oldest eligible runs needed to meet the size target. The newest count includes protected runs. A cleanup maximum must be positive; `--max-total 0` is rejected. To remove all unprotected runs, explicitly use `--older-than 0` and confirm with `--yes`. Protected runs are never deleted. Use `giraffe keep RUN_DIR` to protect a run, or `--off` to remove that marker. The active run, live running or stopping runs, saved baseline source runs, and directories containing files Giraffe did not write are also protected. Empty or temporary-only baseline directories left by failed saves do not protect other runs; malformed actual baseline files still trigger conservative protection. Legacy `tmpXXXXXXXX` files from Giraffe's old atomic writer are recognized. Baselines are never cleaned. The UI requires a preview followed by a separate delete confirmation. Apply builds one fresh plan and revalidates each confirmed directory and its protections before deletion; it never adds unconfirmed IDs. Both the UI and CLI report the actual deleted count and bytes freed on disk, followed by each skipped ID and its reason. The Runs page keeps this result visible after the dialog closes and history refreshes, including when no runs were deleted. Storage reads prefer web metadata or cached report identity/time fields, rather than repeatedly decoding retained bodies. Storage, cleanup, keep and pre-start filesystem work use the web threadpool. A storage lookup failure leaves run history usable with an unavailable-storage notice.

Limits are checked before starting, without reserving space. Concurrent writers can consume free disk while a run executes. If JSON export fails, Giraffe marks the run as an error and tries to save a small `summary.json` with the measured overall result, per-check summaries, and request count. Check summaries are capped at 2,048 characters and visibly marked when truncated; titles are capped at 256 characters. The error message states whether that summary was saved and how many requests lost their evidence. If JSON is complete but HTML saving fails, Giraffe keeps `report.json`, marks the run completed and displays an HTML-save warning; it does not claim request evidence was lost. A failure to save final status metadata also stays a warning when the complete report exists.

Implementation references reviewed before building: Python's [disk usage and directory removal APIs](https://docs.python.org/3/library/shutil.html), including their symlink safety behavior, and FastAPI's [HTTP error handling](https://fastapi.tiangolo.com/tutorial/handling-errors/). Giraffe adds its own directory recognition and protection checks before deletion.

CLI and web writers record their process ID and host. A run is live when it is the web server's active job, its same-host owner has a different PID whose liveness check succeeds (including permission-denied checks), or its heartbeat is newer than two minutes. CLI writers refresh the state file's mtime about every 30 seconds through execution and export. Foreign hosts, the inspecting process's own PID, and legacy metadata without a host rely on that recent heartbeat rather than an unrelated PID. Crashed stale writers become cleanup candidates without needing a web restart. At web startup, stale runs recover as completed when report.json validates, otherwise interrupted. A genuine failed-export summary still takes precedence; a general metadata error alone no longer hides a complete report. Recovery remains visible even if updating metadata on disk fails.

## Deterministic measurement

Run from the checkout:

```sh
PYTHONPATH=$PWD/src /Users/0dust/Documents/giraffe/.venv/bin/python scripts/measure_storage.py --requests 100 --body-sizes 0 1024 16384
```

The script constructs real `RunConfig`, `RequestRecord`, `CheckResult`, and `RunReport` models, then exports JSON and HTML with `write_report`. It uses fixed timestamps, ASCII response bodies, and every tenth answer incorrect. It does not contact an endpoint. The per-run limit is disabled to measure retention itself; temporary exports are removed. Measurements on 2026-10-20:

| Requests | Response bytes each | Retention | JSON bytes | HTML bytes | Total bytes | Total bytes/request |
| ---: | ---: | --- | ---: | ---: | ---: | ---: |
| 100 | 0 | all | 241,759 | 36,079 | 277,838 | 2,778.38 |
| 100 | 0 | failures | 241,764 | 36,089 | 277,853 | 2,778.53 |
| 100 | 0 | none | 241,760 | 36,081 | 277,841 | 2,778.41 |
| 100 | 1,024 | all | 344,659 | 62,283 | 406,942 | 4,069.42 |
| 100 | 1,024 | failures | 252,504 | 47,158 | 299,662 | 2,996.62 |
| 100 | 1,024 | none | 242,260 | 37,060 | 279,320 | 2,793.20 |
| 100 | 16,384 | all | 1,880,859 | 68,734 | 1,949,593 | 19,495.93 |
| 100 | 16,384 | failures | 406,304 | 49,784 | 456,088 | 4,560.88 |
| 100 | 16,384 | none | 242,460 | 37,136 | 279,596 | 2,795.96 |

The bodyless rows show the metadata cost that retention cannot remove. These are synthetic measurements, not upper bounds: input evidence, tool calls, judges, and check metrics can add more data. The 256 MiB JSON default leaves room for roughly 14,000 requests at the measured 16 KiB `all` JSON cost while bounding retained response growth in large runs. A 5 GiB saved-run allowance accommodates about 2,750 complete exports of the measured 100-request, 16 KiB `all` case. The 1 GiB free-disk threshold leaves headroom for exports and other processes; it does not guarantee an export will fit. These defaults leave ordinary small acceptance runs unaffected while making repeated accumulation and unusually large response retention explicit and configurable.


## Peak memory (RAM)

The default `max_memory_mb` is 4,096 MiB (4 GiB); set `giraffe run --max-memory 0` or the web run's **Memory limit (RAM growth during this run)** to `0` to disable it. The guardrail measures growth in the tester process's RSS from `rss_at_start_bytes`, captured when this run starts, so allocations left from earlier runs and saved-report browsing are excluded from the starting budget. It does not measure the endpoint server. Allocations by other concurrent activity after the baseline can still contribute to growth. Traffic and scoring/aggregation record separate absolute RSS peaks in `observations.resources.sampled_peak_rss_bytes` and baseline-relative growth peaks in `sampled_peak_growth_bytes`. All RAM values use binary MiB/GiB (1 MiB = 1,048,576 bytes). Scoring begins after traffic ends and includes aggregation until the checks are built. Export is measured separately while `write_report` serializes the artifacts: the CLI prints that peak and web run metadata saves it for the run detail page. These are RAM measurements, separate from saved-run sizes on disk.

Sampling runs about once per second, with measurements at phase boundaries. Short spikes between samples can be missed. A sampled RSS increase above the limit stops unfinished execution, emits the normal stopping event, and saves the collected result with the reason. An unfinished check cannot pass; completed targets and checks keep their statuses. The overall result becomes inconclusive only if a required check was cut short and no check failed. If the limit is crossed after all traffic and scoring finished, it is recorded as a warning and no result is downgraded. The reason reports rounded-up growth and the exact limit in MiB so a small overrun remains visible (for example, “RAM grew by 4,113 MiB, over the 4,096 MiB limit”). Giraffe does not reduce concurrency or request counts to fit a limit. This is a sampling guardrail, not an operating-system allocation quota. Export can still raise RAM above the execution limit, so its measured peak is reported separately.

Linux current RSS comes from resident pages in `/proc/self/statm`; macOS uses Mach `task_info` and `resident_size`. See the [Linux statm manual](https://www.man7.org/linux/man-pages/man5/proc_pid_statm.5.html), [Apple Mach task information](https://developer.apple.com/documentation/kernel/mach_task_basic_info_data_t) and [Python resource documentation](https://docs.python.org/3/library/resource.html). Unsupported platforms or unavailable measurements report **not measured on this platform**, and the RAM-growth guardrail is inactive. Older reports and missing measurements say **not recorded** instead; if the starting RSS was unavailable, growth cannot be calculated and the guardrail stays inactive even if later RSS samples become available. `process_peak_rss_bytes` uses `getrusage` and is the process lifetime peak. In the long-lived web process it includes earlier runs; it is labelled accordingly and never triggers the guardrail.

In-memory response storage scales with requests × response size, plus request metadata, inputs, tool results, scoring data and Python object overhead. Response bodies remain available throughout execution because judging, scoring, consistency and conversation dependencies can read them later. `all` / `failures` / `none` apply at export and do not bound live RAM. The RAM-growth guardrail bounds continued execution by stopping rather than silently dropping evidence. Retaining fewer responses on disk does not remove the need for that guardrail.

Export uses shallow copies for unchanged records, copies only changed response evidence and scenario summaries, and streams the request JSON into the atomic temporary file one record at a time. The per-run disk limit counts those same UTF-8 chunks, omitting response bodies without building a full report string. Existing compact HTML previews link to `report.json` for complete retained bodies. Parsed JSON, retention behavior and storage omission notices match the former exporter, and the caller's in-memory report is unchanged. This removes full-report copies and JSON strings; it does not stream away the original live report or make unusually large individual requests or check metrics constant-size.

### Export measurement

Run from the checkout:

```sh
PYTHONPATH=$PWD/src /Users/0dust/Documents/giraffe/.venv/bin/python scripts/measure_memory.py
```

The script uses the real models with independent response strings, every tenth answer incorrect, fixed timestamps and `retention=all`. Each old/new case runs in a fresh subprocess and writes into a temporary directory removed afterwards; no endpoint is contacted. The script alone preserves the previous deep-copy/full-JSON exporter for comparison. Its optional `--requests` / `--body-sizes` accept larger cases, including 50,000 requests. The default modest cases below finished in under five seconds on macOS on 2026-10-20. For these short exports only, the benchmark samples current RSS every 5 ms and also records the process lifetime peak; normal runs keep the roughly one-second sampler.

| Requests | Body bytes each | Exporter | Holding report RSS (MiB) | Holding process peak (MiB) | Sampled export peak (MiB) | Process peak after export (MiB) | Export seconds |
| ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1,000 | 1,024 | Old | 62.1 | 62.7 | 79.1 | 79.1 | 0.036 |
| 1,000 | 1,024 | Streamed | 63.8 | 64.3 | 65.2 | 65.2 | 0.013 |
| 1,000 | 16,384 | Old | 85.3 | 85.9 | 141.2 | 141.2 | 0.049 |
| 1,000 | 16,384 | Streamed | 80.6 | 81.1 | 82.3 | 82.3 | 0.029 |
| 10,000 | 1,024 | Old | 117.1 | 117.7 | 235.1 | 235.1 | 0.323 |
| 10,000 | 1,024 | Streamed | 112.5 | 113.0 | 122.0 | 122.0 | 0.131 |
| 10,000 | 16,384 | Old | 267.2 | 267.8 | 688.6 | 688.6 | 0.414 |
| 10,000 | 16,384 | Streamed | 264.0 | 264.5 | 276.5 | 276.5 | 0.255 |

At 10,000 × 16 KiB, process peak fell from 688.6 MiB to 276.5 MiB (about 60%); incremental peak over holding the report fell from about 421 MiB to 12.5 MiB. Holding RSS varies slightly between fresh processes because interpreter and allocator behavior varies. These synthetic results establish an export improvement for this workload, not a universal bound or a capacity prediction. Tool data, large individual inputs, scenario metrics and HTML evidence summaries can change the result. The report itself still occupies about 264 MiB in the larger streamed case. These benchmark RSS columns measure the absolute process footprint, not the run-growth budget. The default 4 GiB RAM-growth limit leaves ordinary acceptance runs substantial additional headroom while stopping sustained accumulation in larger runs; choose a lower limit on constrained machines or a higher one for intentionally large runs.

Optional packet captures, traces and verifier indexes are not part of normal runs. They must stay opt-in and have separate storage budgets. Saved-run cleanup only considers recognized direct run directories, protects directories containing files Giraffe did not write, and never traverses symlinked directories or deletes unrelated files, baselines or diagnostics outside those run directories.
