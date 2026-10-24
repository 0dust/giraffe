# Saved reports on disk

[Documentation index](README.md) · [Running and baselines](running.md)

Automatic saved-run cleanup is off by default. The default storage limits are:

| Setting | Default | Effect |
| --- | --- | --- |
| `max_total_storage` | 5 GiB (5,368,709,120 bytes) | Refuse a new run when saved runs already exceed this total. Saved baselines are counted separately and excluded from the limit. |
| `min_free_disk` | 1 GiB (1,073,741,824 bytes) | Refuse a new run when available disk space is below this amount. |
| `max_run_storage` | 256 MiB (268,435,456 bytes) | Omit retained response bodies at export when the report JSON would exceed this amount. |

Set any limit to `0` to disable it. `giraffe web`, `giraffe run`, and `giraffe storage` accept `--max-total-storage`, `--min-free-disk`, and `--max-run-storage`, with sizes such as `500MiB` or `2GiB`. Every disk-size display uses binary units (B, KiB, MiB, GiB, TiB): 1 KiB = 1,024 B, 1 MiB = 1,048,576 B and 1 GiB = 1,073,741,824 B. The Runs page cleanup target is entered in MiB and converted to whole bytes. Legacy decimal flag suffixes remain accepted with their original meaning; displayed sizes always use binary units. See the [NIST binary prefix definitions](https://pml.nist.gov/cuu/Units/binary.html). For CLI runs, storage checks apply to the parent directory of the run output. Root aliases such as macOS `/tmp` are resolved; symlinked children are still excluded. When both start limits are disabled, startup skips storage inspection. Pass the same configured flags to `giraffe storage` to display those limits.

The per-run limit measures the compact, deduplicated JSON, including its storage notice and input table, after response retention. Export first removes response bodies from requests without failure evidence, then from the remaining requests in recorded order. Failure evidence includes incorrect answers, failed or invalid completions, evaluation errors, and requests referenced by failing, blocked, or inconclusive checks. Response text, reasoning text, tool response content, token text, and judge rationale follow the existing response retention rules. Request metadata, counts, scores, timings, check statuses, and the overall verdict remain. If metadata alone exceeds the JSON limit, it is saved in full and `manifest.storage.limit_exceeded` is true. The JSON and HTML report display explicit body omissions. Explicitly exported or legacy HTML, metadata files, markers, and temporary files also occupy disk space and count toward saved-run totals; the JSON limit is therefore a soft per-run export limit, not a hard directory quota.

`--retention all`, `failures`, and `none` control whether response text is kept: all responses, only failure evidence, or no responses. Request metadata (timings, status, scores) is always saved. Retention does not cap disk usage; storage limits do.

Use `giraffe storage --runs-dir runs` or the Runs page to inspect saved runs on disk, saved baselines, available disk space, and limits. `giraffe cleanup --runs-dir runs --older-than 30 --keep-latest 10 --max-total 2GiB` previews cleanup; add `--yes` to delete the preview's eligible runs. The criteria combine with OR: old runs, runs beyond the newest count, and oldest eligible runs needed to meet the size target. The newest count includes protected runs. A cleanup maximum must be positive; `--max-total 0` is rejected. To remove all unprotected runs, explicitly use `--older-than 0` and confirm with `--yes`. Protected runs are never deleted. Use `giraffe keep RUN_DIR` to protect a run, or `--off` to remove that marker. The active run, live running or stopping runs, saved baseline source runs, and directories containing files Giraffe did not write are also protected. Filenames alone never prove ownership: cleanup requires a `.web.json` whose `id` matches the directory name, or a bounded Giraffe report header with nonempty `run_id`, `started_at`, `finished_at` and `suite_version`, schema `2` or `3`, a Giraffe `overall` verdict and an object-valued `manifest`. Four generic string fields alone are insufficient proof. Other report-like directories (including pytest coverage reports) appear as protected with reason **not a Giraffe run**. The same proof is rechecked immediately before manual or automatic deletion. Empty or temporary-only baseline directories left by failed saves do not protect other runs; malformed actual baseline files still trigger conservative protection. Legacy `tmpXXXXXXXX` files from Giraffe's old atomic writer are recognized. Baselines are never cleaned. The UI requires a preview followed by a separate delete confirmation. Apply builds one fresh plan and revalidates each confirmed directory and its protections before deletion; it never adds unconfirmed IDs. Both the UI and CLI report the actual deleted count and bytes freed on disk, followed by each skipped ID and its reason. The Runs page keeps this result visible after the dialog closes and history refreshes, including when no runs were deleted. Storage reads prefer web metadata or cached report identity/time fields, rather than repeatedly decoding retained bodies. Storage, cleanup, keep and pre-start filesystem work use the web threadpool. A storage lookup failure leaves run history usable with an unavailable-storage notice.

Limits are checked before starting, without reserving space. Concurrent writers can consume free disk while a run executes. If JSON export fails, Giraffe marks the run as an error and tries to save a small `summary.json` with the measured overall result, per-check summaries, and request count. Check summaries are capped at 2,048 characters and visibly marked when truncated; titles are capped at 256 characters. The error message states whether that summary was saved and how many requests lost their evidence. If a CLI run explicitly requests `--html` and JSON is complete but HTML saving fails, Giraffe keeps `report.json`, marks the run completed and displays an HTML-save warning; it does not claim request evidence was lost. A failure to save final status metadata also stays a warning when the complete report exists.

Implementation references reviewed before building: Python's [compact JSON and incremental encoding](https://docs.python.org/3/library/json.html), [hard links and atomic replacement](https://docs.python.org/3/library/os.html), [disk usage and directory removal APIs](https://docs.python.org/3/library/shutil.html), including their symlink safety behavior, and FastAPI's [HTTP error handling](https://fastapi.tiangolo.com/tutorial/handling-errors/). Giraffe adds its own directory recognition and protection checks before deletion.

CLI and web writers record their process ID and host. A run is live when it is the web server's active job, its same-host owner has a different PID whose liveness check succeeds (including permission-denied checks), or its heartbeat is newer than two minutes. CLI writers refresh the state file's mtime about every 30 seconds through execution and export. Foreign hosts, the inspecting process's own PID, and legacy metadata without a host rely on that recent heartbeat rather than an unrelated PID. Crashed stale writers become cleanup candidates without needing a web restart. At web startup, stale runs recover as completed when report.json validates, otherwise interrupted. A genuine failed-export summary still takes precedence; a general metadata error alone no longer hides a complete report. Recovery remains visible even if updating metadata on disk fails.

## Report artifacts and input compatibility

Normal CLI and web runs save `report.json` and the small `.web.json` lifecycle file;
HTML is generated only on request. The existing **Download HTML report** button
uses `GET /api/runs/{id}/report.html`, rendering saved JSON with an attachment
Content-Disposition. It never reruns a check or contacts a model, and creates no
file on disk. The same route regenerates HTML for older runs, avoiding a stale
HTML file disagreeing with saved JSON. Legacy `report.html` remains recognized by
storage accounting and cleanup.

HTML and JSON downloads share a single-flight lock while generating a temporary
response file. The lock is released before sending bytes, so a stalled client
cannot block other downloads. The file is streamed in bounded chunks and removed
on completion or disconnect; it never becomes a persistent run artifact. Temporary
response files occupy disk for the lifetime of their transfers. Cancellation waits
for in-flight generation work through a shielded completion callback, with no
cancellation retry loop or repeated decoding.

JSON generation uses a bounded-cache disk-backed input lookup and streams metadata
followed by individual expanded requests into that response file. It never builds
a complete request list or expanded JSON string. The decoder scans each value once
across buffer boundaries and calls the JSON decoder once per complete value,
avoiding repeated rescans of large records. Memory scales with metadata, the
largest individual JSON value/request and the lookup cache. HTML still loads one
report to render it, with at most one HTML/JSON generation active.

Both generation paths sample RAM with `PhaseSampler`. Samples update only an
existing `.web.json`, under the same metadata lock as lifecycle writes; a download
never creates a legacy state file or invents lifecycle state. Finalization merges
samples saved during cleanup instead of overwriting them. Resources appear as
`download_html` / `download_json`. Resource-only write failures are best effort and
never create a cached lifecycle state. Temporary lookup files and response files
are closed and removed after errors or cancellation.

Use `giraffe report RUN_DIR --html [--output PATH]` for a standalone HTML file, or
`giraffe run ... --html` to write one at completion. Generated HTML links to
`report.json` for full retained evidence; keep them together when sharing files.

All newly serialized report, baseline, summary and web-state JSON is compact.
Eligible immutable schema-3 baseline sources can be hard-linked as-is. Older
sources are reserialized when saving a managed baseline so newly written format-2
evidence always carries the schema-3 signal. Default-valued fields are **not omitted**: raw consumers retain
all fields except the explicitly versioned input representation. Saved reports
have `schema_version: "3"`, `report_format: 2` and an `inputs` table mapping SHA-256 references to exact
message arrays. Nonempty `evaluation.messages` moves to that table; each request
has `input_ref` instead. Empty messages stay empty. Table entries follow retention,
so this cannot restore tool-conversation inputs stripped by `none` or `failures`.
The format fields are dedicated top-level/request fields, outside measurements.
Readers verify each table entry's SHA-256 before rehydrating and reject mismatches
rather than substituting altered input evidence. Schema 2 and schema 3 remain
measurement-compatible in the new comparator; the bump signals a changed stored
representation.

Deduplicated reports require a **schema-3 / report-format-2 reader**. Older pinned
checkouts may not understand this representation. Inspect
`schema_version`/`report_format` rather than relying on the package version alone.
Current Giraffe reads both representations. For portable exchange, `giraffe baseline save
RUN_DIR file.json` always streams expanded schema-2 JSON with input messages in
each request and no table/reference fields. Older pinned schema-2 Giraffe readers
can read that file. CLI baseline exports retain their previous `0644` mode and
have an independent inode; they intentionally trade storage sharing for portability.

`RunReport` validation, CLI loading and web loading accept both legacy reports and
this format, restoring independent message arrays for each request. API results,
including the JSON download, keep the expanded model representation with exact
inputs. Direct disk JSON readers must understand `report_format`/`inputs`/`input_ref`.
The validation suite does: its streaming decoder reads table entries individually
into a temporary SQLite lookup and rehydrates requests one at a time. The existing
64-member limit applies only to top-level report members, not input-table entries;
the per-request value bound applies to each input entry, so a large table does not
hit the metadata-value bound or require retaining the whole table in RAM.

Managed web baseline saves hard-link immutable `report.json` where possible, copying on
filesystems that cannot link. Writers publish with atomic replacement; later source
rewrites or deletion leave the baseline inode and evidence intact. Arbitrary CLI
JSON imports are compactly reserialized instead of linked because they do not have
Giraffe's immutable-file contract. A hard link shares an inode: an external
editor that writes in place would change both names. Only Giraffe's atomic
replacement contract gives snapshot independence; do not edit linked report
files in place. Managed linked baselines retain the source report's file mode
(currently `0600`), while portable CLI exports use `0644`. Saved-run baseline sources remain protected from
manual and automatic cleanup: independence removes the technical dependency, but
keeping the source still preserves its lifecycle metadata and browsing history and
honors existing cleanup behavior. Storage counts each `(st_dev, st_ino)` once.
Baseline bytes already attributed to a source run are displayed separately as
**shared with source run**, rather than added to the total twice. After source
replacement or external deletion, those baseline bytes count as independent storage.

## Optional automatic lifecycle policy

`giraffe ui` (alias `web`), `giraffe run` and `giraffe storage` accept:

- `--auto-cleanup-keep-latest N`
- `--auto-cleanup-older-than DAYS`
- `--auto-cleanup-max-total SIZE`

For example, `giraffe ui --runs-dir runs --auto-cleanup-keep-latest 20
--auto-cleanup-older-than 30 --auto-cleanup-max-total 2GiB` applies the existing
cleanup planner after each run finishes. CLI automatic cleanup requires an explicit `--runs-dir ROOT` with `--output`
directly inside that root, or output directly inside the default `runs` directory.
`--output myrun` in an arbitrary working directory is refused before running when
auto cleanup is enabled. `--runs-dir ROOT` without `--output` places the new run
inside ROOT. This keeps automatic deletion from implicitly adopting cwd or an
unrelated sibling directory as a saved-run root. Criteria combine with OR, as for manual cleanup. The
triggering run, active/live runs, kept runs, baseline sources and unrecognized
directories remain protected. Protections can prevent reaching the requested size.
There is no startup expiry, so cleanup never silently removes evidence just to
pass the pre-start disk limit. Zero count/age are explicit delete-all-eligible
policies; size must be positive.

Automatic deletion is off by default because deleting user evidence is irreversible.
The existing default 5 GiB saved-run limit already bounds accumulation by refusing
new runs once saved runs exceed it; it checks before starting and permits the run
that crosses the limit to finish. Users choose whether to clean up manually or opt
into automatic deletion. A limit is not a reservation or a hard filesystem quota.

The active policy appears in `giraffe storage` (pass the same flags as the launcher)
and `/api/storage`. CLI stdout names every automatic deletion (JSON output contains
the exact deleted-ID list). Web results and the Runs storage line show deleted IDs;
`.web.json` on the triggering run records the policy, deleted IDs, skipped runs and
bytes freed. The final run state is written before cleanup begins; cleanup is
shielded through cancellation, its log is written afterwards, and the active job
is always cleared. If a final status write first fails and a later retry succeeds,
its temporary warning and cached unsaved state are removed; other errors remain. A run interrupted by task cancellation skips automatic cleanup. The latest result remains visible after restarting the web server.
Manual cleanup retains its preview/confirmation workflow.

## Deterministic footprint measurement

Run from the checkout, in the Python environment where Giraffe is installed:

```sh
PYTHONPATH="$PWD/src" python scripts/measure_storage.py --requests 300 --body-sizes 16384
```

The fixture uses real models, fixed timestamps, 300 completed requests with 2 KiB
input text from ten repeated cases, 16 KiB response text per request and 30 wrong
answers. Counts, scores, timing samples and all required failure evidence are
preserved. The previous format is pretty JSON with repeated input arrays plus
automatically saved HTML; the new format is compact deduplicated JSON with no
persistent HTML. Both totals include a deterministic `.web.json` lifecycle file
(previous pretty: 154 B; new compact: 133 B). There are no model requests. The
per-run limit is disabled so this measures representation savings without evidence
trimming. Measurements on 2026-10-20:

| Retention | Previous JSON | Previous HTML | Previous total | New JSON | New total | Reduction |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| all | 6,333,919 B | 130,591 B | 6,464,664 B | 5,514,047 B | 5,514,180 B | 14.70% |
| failures | 1,910,244 B | 130,601 B | 2,040,999 B | 1,090,372 B | 1,090,505 B | 46.57% |
| none | 1,418,720 B | 92,673 B | 1,511,547 B | 598,848 B | 598,981 B | 60.37% |

These tables record the implementation and fixtures measured on October 20.
Re-running the current checkout can produce different artifact sizes as report
fields and fixtures evolve. Record the source revision with new measurements.
These are measured synthetic workloads, not production-size estimates. Formatting,
HTML removal and input deduplication savings overlap; no default fields or required
failure bodies were removed. Baselines normally add a link and a small metadata
file instead of another JSON-sized copy. Regression tests use a smaller fixture to
assert substantial savings and equality of the retained model, API responses,
counts, scores, timings and exact inputs, including legacy reads.


## Peak memory (RAM)

The default `max_memory_mb` is 4,096 MiB (4 GiB). Use `--max-memory 0` on a CLI run, or turn off **Maximum memory** under **Run limits → Resource protection** in the UI, to disable it. The guardrail measures growth in the tester process's RSS from `rss_at_start_bytes`, captured when this run starts, so allocations left from earlier runs and saved-report browsing are excluded from the starting budget. It does not measure the endpoint server. Allocations by other concurrent activity after the baseline can still contribute to growth. Traffic and scoring/aggregation record separate absolute RSS peaks in `observations.resources.sampled_peak_rss_bytes` and baseline-relative growth peaks in `sampled_peak_growth_bytes`. All RAM values use binary MiB/GiB (1 MiB = 1,048,576 bytes). Scoring begins after traffic ends and includes aggregation until the checks are built. Export is measured separately while `write_report` serializes JSON (plus optional HTML): the CLI prints that peak and web run metadata saves it for the run detail page. These are RAM measurements, separate from saved-run sizes on disk.

Sampling runs about once per second, with measurements at phase boundaries. Short spikes between samples can be missed. A sampled RSS increase above the limit stops unfinished execution, emits the normal stopping event, and saves the collected result with the reason. An unfinished check cannot pass; completed targets and checks keep their statuses. The overall result becomes inconclusive only if a required check was cut short and no check failed. If the limit is crossed after all traffic and scoring finished, it is recorded as a warning and no result is downgraded. The reason reports rounded-up growth and the exact limit in MiB so a small overrun remains visible (for example, “RAM grew by 4,113 MiB, over the 4,096 MiB limit”). Giraffe does not reduce concurrency or request counts to fit a limit. This is a sampling guardrail, not an operating-system allocation quota. Export can still raise RAM above the execution limit, so its measured peak is reported separately.

Linux current RSS comes from resident pages in `/proc/self/statm`; macOS uses Mach `task_info` and `resident_size`. See the [Linux statm manual](https://www.man7.org/linux/man-pages/man5/proc_pid_statm.5.html), [Apple Mach task information](https://developer.apple.com/documentation/kernel/mach_task_basic_info_data_t) and [Python resource documentation](https://docs.python.org/3/library/resource.html). Unsupported platforms or unavailable measurements report **not measured on this platform**, and the RAM-growth guardrail is inactive. Older reports and missing measurements say **not recorded** instead; if the starting RSS was unavailable, growth cannot be calculated and the guardrail stays inactive even if later RSS samples become available. `process_peak_rss_bytes` uses `getrusage` and is the process lifetime peak. In the long-lived web process it includes earlier runs; it is labelled accordingly and never triggers the guardrail.

In-memory response storage scales with requests × response size, plus request metadata, inputs, tool results, scoring data and Python object overhead. Response bodies remain available throughout execution because judging, scoring, consistency and conversation dependencies can read them later. `all` / `failures` / `none` apply at export and do not bound live RAM. The RAM-growth guardrail bounds continued execution by stopping rather than silently dropping evidence. Retaining fewer responses on disk does not remove the need for that guardrail.

Export uses shallow copies for unchanged records, copies only changed response evidence and scenario summaries, and streams the request JSON into the atomic temporary file one record at a time. The per-run disk limit counts those same UTF-8 chunks, omitting response bodies without building a full report string. Existing compact HTML previews link to `report.json` for complete retained bodies. Rehydrated model values, retention behavior and storage omission notices match the former exporter, and the caller's in-memory report is unchanged. This removes full-report copies and JSON strings; it does not stream away the original live report or make unusually large individual requests or check metrics constant-size.

### Export measurement

Run from the checkout, in the Python environment where Giraffe is installed:

```sh
PYTHONPATH="$PWD/src" python scripts/measure_memory.py
```

The script uses the real models with independent response strings, every tenth
answer incorrect, fixed timestamps and `retention=all`, with the same 2 KiB inputs
from ten cases in each child. Each case runs in a fresh subprocess and temporary
directory; no endpoint is contacted. It reports artifact bytes alongside RSS so
comparisons cannot silently change the output set. `old-json-html` versus
`new-json` describes the change in normal default behavior. Compare `old-json`
versus `new-json` to isolate JSON-only exports, and `old-json-html` versus
`new-json-html` for the same JSON+HTML artifact set. The latter represents the
new explicit `--html` path. These checks use a 5 ms sampler for short exports;
normal phase sampling remains about once per second.

Fresh measurements on 2026-10-20 (all RSS values in MiB):

These are historical measurements of that implementation and fixture set. Memory
and artifact sizes can change with the source revision, interpreter and platform;
the table is not a current-release memory guarantee.

| Requests | Body bytes | Export path | JSON bytes | HTML bytes | Holding RSS | Export sampled peak | Process peak | Seconds |
| ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | 16,384 | old-json-html | 21,099,929 | 409,966 | 98.6 | 151.1 | 151.1 | 0.057 |
| 1,000 | 16,384 | new-json-html | 18,321,527 | 409,966 | 82.5 | 84.2 | 84.2 | 0.064 |
| 1,000 | 16,384 | old-json | 21,099,929 | 0 | 77.4 | 130.7 | 130.7 | 0.052 |
| 1,000 | 16,384 | new-json | 18,321,527 | 0 | 84.3 | 84.8 | 84.9 | 0.051 |
| 10,000 | 16,384 | old-json-html | 210,949,629 | 4,003,973 | 312.8 | 795.3 | 795.3 | 0.632 |
| 10,000 | 16,384 | new-json-html | 182,990,127 | 4,003,973 | 339.2 | 356.9 | 356.9 | 0.527 |
| 10,000 | 16,384 | old-json | 210,949,629 | 0 | 343.4 | 797.3 | 797.3 | 0.479 |
| 10,000 | 16,384 | new-json | 182,990,127 | 0 | 337.2 | 338.0 | 338.0 | 0.478 |

For 10,000 × 16 KiB, the normal-default process peak changes from 795.3 MiB
(JSON+HTML) to 338.0 MiB (JSON only), about 57.5%. The like-for-like JSON-only
comparison is 797.3 to 338.0 MiB (about 57.6%); the JSON+HTML comparison is 795.3
to 356.9 MiB (about 55.1%). Holding RSS varies across fresh processes because of
allocator and interpreter behavior. The original report still occupies hundreds
of MiB; these are synthetic export measurements, not an execution quota, a
constant-memory claim for arbitrary individual requests, or a capacity prediction.
Tool data, large inputs and metadata can change the result. The default 4 GiB RAM
growth guardrail still bounds continued execution by stopping unfinished work.

Optional packet captures, traces and verifier indexes are not part of normal runs. They must stay opt-in and have separate storage budgets. Saved-run cleanup only considers recognized direct run directories, protects directories containing files Giraffe did not write, and never traverses symlinked directories or deletes unrelated files, baselines or diagnostics outside those run directories.
