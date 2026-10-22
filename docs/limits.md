# Limits and client capacity

Giraffe, measured 2026-10-21, accepts at most **512 simultaneous requests**. Backend policy applies to configuration loaded by CLI and web/API. The UI uses schema-derived bounds and shared field explanations from capabilities.

**An accepted ceiling is not demonstrated delivery capacity.** Endpoint speed, response size, streaming cadence, CPU, sockets and the selected checks determine delivered load. Timing-health stalls and local drops remain visible; increasing a setting cannot make those measurements qualify.

## Decisions and load inventory

Hard configuration bounds reject input rather than silently reducing explicit settings. Values marked configurable are user budgets or defaults, not software maxima. The machine-readable inventory is `capabilities()["limits"]`: configuration fields use flat paths such as `concurrency`, `traffic.rates` and `sessions.workloads[].turns`, resolved from `config_schema` (including nullable `$ref` fields and array item bounds). Aggregate entries such as `traffic.planned_arrivals`, `request_pack.bytes`, `tool_checks.bytes` and `live.requests` use named constants from `caps.py`. Named runtime entries use the same flat format (`runtime.MAX_LIVE_EVENTS.maximum`, for example); named runtime defaults expose `default` instead of `maximum`. `defaults` contains effective configuration defaults separately. There is no second copy of schema maxima in the capabilities implementation.

| Setting / cap | Decision | Accepted bound / default | Rationale and behavior when exceeded |
|---|---|---|---|
| Suite `concurrency`, `default_concurrency`; check-options concurrency; arrivals/traffic in-flight; concurrent-load and consistency level items | **Change** | Maximum **512** (1024 candidate rejected) | 512 delivered its peak with short, visible timing-health stalls; 1024 produced substantially longer stalls and exceeded launch tolerance. See measurements. Input above 512 is rejected. The optional suite ceiling limits the shared slot pool; conflicting explicit per-check settings reject input. Implicit settings resolve to available concurrency with notices. |
| Judge concurrency | **Change** | 256 → **512**, default **2** | Same simultaneous-request policy for the separate post-traffic HTTP client pool. This is an admission ceiling, not a demonstrated judge throughput claim; finite panels, judge budgets and endpoint latency still apply. Above it rejects input. |
| Legacy bucket concurrency levels | **Change** | 256 → **512** | Migration accepts the same simultaneous-request ceiling as current load settings. Above it rejects legacy input before migration. |
| `Arrival.requests` | **Keep** | **10,000** per arrival check/endpoint; default **8** | Candidate 100,000 must pass all memory, storage, web and lateness gates. The measured candidate fails; see arrival measurements. Above 10,000 rejects input. This is distinct from the optional whole-run request budget. |
| `Arrival.burst_size` | **Change / retain phase-1–5 implementation** | **1024**, default **2** | The final bound is 1,024 offered arrivals in one burst, independent of the 512 simultaneous-request maximum. Burst is an offered arrival cohort, not simultaneous delivery. Above it rejects input. Burst above effective in-flight settings receives the existing queue/wait warning; pending overflow is **dropped locally** and recorded as unissued. |
| `Arrival.max_pending` | **Keep** | **256**, default **4** | Bounds queued, undispatched work independently of in-flight work. Larger queues disguise dispatch delay rather than increase service capacity. Above 256 rejects input; a full configured queue drops arrivals locally. |
| Arrival steady rate | **Keep** | **1000/s**, default **1/s** | Measured at 1000/s; retain a separately bounded deterministic arrival scheduler. Higher input is rejected. Dispatch wait above `max_client_wait_ms` (default **50 ms**, configurable) leaves rate qualification inconclusive. |
| Traffic plan | **Keep** | **100,000** planned arrivals per endpoint, across rates | Bounds finite schedule/outcome/evidence allocation. Aggregate `sum(rates) × stage duration` above it rejects configuration with a structured correction. Defensive runtime overflow bounds schedule collection and leaves incomplete evidence. |
| Traffic rate | **Keep** | **10,000/s** per stage | Admission upper bound, not measured delivery capacity; the scheduler has no pending queue. No free slot → **dropped locally**. Scheduler stalls beyond `scheduler_lag_tolerance_ms` (default **100 ms**, configurable) miss arrivals visibly and make affected capacity evidence inconclusive. Higher input rejects. |
| Number of traffic rates / load levels / consistency levels | **Keep** | **16** | Bounds stage combinations and evidence growth; distinct levels and increasing traffic rates remain required. Above it rejects input. |
| Traffic / concurrent-load stage duration | **Keep** | **3600 s** | Bounds each configured sending window independently of optional suite budgets. Higher input rejects; the aggregate traffic-plan cap can reject a shorter window too. |
| Sessions count | **Keep, explicitly reviewed** | **32**, default **5** | Conversation count multiplies turns, fixed/live modes, retained histories and scoring. The simple-request concurrency experiment does not justify a larger conversation-history workload. Above it rejects input. |
| Sessions concurrency | **Change** | **512**, default **2** | Shares the user-approved simultaneous-request ceiling. Effective concurrency is still `min(sessions.concurrency, sessions.sessions)`; the separate 32-conversation count limits retained histories and actual conversation overlap. Above 512 rejects input. |
| Session turns / workloads | **Keep** | 2–**32** turns, **8192** characters/turn; **16** workloads; workload name **64** characters; weight **10,000**; delay **60 s** | Bounds history growth and profile expansion; larger inputs reject. Fixed/live modes must be distinct. Context admission stops rather than cropping required history: unfinished conversation is inconclusive. |
| Prefix workload | **Keep** | **65,536** prefix characters, **100** repeats, **32** history turns | Bounds repeated prompt/history generation; above any cap rejects. Generated prompts that fail conservative context admission remain unfinished/inconclusive; text is not silently cropped. |
| Consistency workload | **Keep** | **100** repetitions, **16** prompts/shape; top-logprobs **20**, priming rounds **2** | Bounds repeated-copy and cross-level comparison cost. Above it rejects. Text groups may round repetitions up to complete groups; logprob mode uses exact rounds. Shape choices and temperatures are semantic bounds, not throughput claims. |
| Concurrent-load token profile | **Keep** | **131,072** input tokens and **65,536** output tokens per size; **5** calibration attempts; interference trigger **1000** chunks | Bounds generated payload/calibration work, independently of declared server context. Higher input rejects; unattainable lengths/calibration or inadequate evidence stay inconclusive. Nine short/medium/long combinations are the finite choice set. |
| Traffic long-prompt profile | **Keep** | **1,000,000** input characters; **4096** output words | Bounds generated stress prompts. Above it rejects input; actual request output/timeout settings still apply. |
| Cancellation configuration | **Keep** | **128** cancel-after chunks, **60 s** cleanup/trace wait; path strings **4096** characters | Bounds evidence waits/config metadata. Above it rejects input. Missing/ambiguous backend cancellation evidence is inconclusive, not proof of release. |
| Metrics configuration | **Keep** | **4096** samples, **1024** series; interval **0.1–60 s**, timeout ≤**10 s** | Configurable below schema maxima (defaults **256**, **128**, **1 s**, **2 s**). Above maxima rejects; sample/cardinality truncation and gaps remain visible and can make coverage inconclusive. |
| Judges / judge duration | **Keep / configurable** | **8** judge endpoints; default **300 s** judging budget | Endpoint count above 8 rejects. Duration is a configurable default, not a maximum; budget expiry leaves unjudged work inconclusive. |
| Run request count / duration / memory / per-request timeout / output / context / samples | **Configurable** | Request and duration budgets default **Not set**; memory-growth default **4096 MiB**; timeout **30 s**; output **128**; context **4096**; samples **6** | These defaults are **not maxima**. Explicit budgets stop unfinished work with partial evidence. Memory is sampled RSS growth, not endpoint memory; zero disables it. Individual timeout cancels reading/unfinished request. Server output/context limits still matter. Error stop default **20** and unresponsive-target policy default **5** failures plus **45 s** silence are configurable, not immutable caps. |

### Constraint explanations

Capabilities also exposes `cap_explanations`, generated in `caps.py` from the
schema-derived bounds. The same text appears in field help and API/CLI errors;
exceeding a configuration cap rejects input rather than silently trimming work.
**Software safety bounds** limit memory, scheduling, generated payloads, histories
and evidence expansion. **Implementation constraints** cover scheduler admission
and the supported scales of pass criteria. **Product policy** covers the measured
512-request admission ceiling, pending-queue delay visibility and a 3,600-second
individual sending window. These classifications do not certify endpoint capacity.

### Comparability source identities

`_run_limits_workload_identity` in `src/giraffe/reporting.py` contains exact,
reviewed source hashes. Editing `concurrent_load.py`, `workloads.py`, `logprobs.py`
or `logprob_workload.py` changes the recorded comparability identity. Recheck the
request-level workloads, then update or extend that identity table only when
unchanged workloads justify a bridge; keep real workload changes noncomparable.
Run `tests/test_run_limits_baseline_identity.py`, whose source-table assertion
names this update when an edited generator has no reviewed entry. Never replace
pinned hashes with automatic acceptance of the current files.

## Payload and evidence inventory

All following caps are **kept** unless marked configurable. They bound reading, stored metadata or evaluation work; they do not demonstrate endpoint capacity. Schema-backed lengths and list sizes (including IDs, messages and tool fixtures) are also enumerated in the flat `capabilities()["limits"]` inventory.

| Payload / evidence cap | Bound | Rationale and effect of exceeding it |
|---|---|---|
| Inference response body | **4 MiB** per response | Stops reading and reports a failed/bounded response; limits a non-cooperative endpoint before retention policy. Applies to streaming and complete responses. |
| Response event metadata | **256** events/request | Truncates recorded event metadata, records the limit flag; transport still reads within the body bound. This is not a 256-token generation limit. |
| Answer content-read timing | **65,536** reads/request | Stops collecting read-timing detail, marks timing truncated; response processing remains bounded by body size. Incomplete timing cannot qualify fine-grained stream evidence. |
| Server request ID | **4096** characters | Drops oversized ID metadata and marks event truncation; prevents unbounded correlation evidence. |
| Tool calls / definitions / expectations | **16** per bounded list and `max_calls` (default **16**) | Config above list/choice bounds rejects; generated oversized/malformed tool-call evidence fails protocol validation. Tool fixtures/conversations each max **16**, messages **64**, steps **4**, final-content assertions **16**, follow-up user message **16,384** characters, fixture ID **128**, function name **64**, content expectation **1,000,000** characters. These are schema maxima, not defaults. |
| Tool configuration / result / JSON tree | **64 KiB** config; **64 KiB** result; **64 KiB** schema or argument JSON; depth **32** | Configured excess rejects input. Generated excessive arguments cannot be validated, producing explicit tool protocol/evidence failure. No tool execution occurs. |
| Tool schema/evaluation work | **2048** schema nodes, **256** expanded schemas, **1024** argument nodes, product ≤**4096** | Bounds local-reference expansion and combinatorial validation. Configured excess rejects; response excess cannot establish a valid tool call. |
| Request packs / dataset imports | **1 MiB**, **1000** cases; messages **64**, fixture ID **128** characters | Rejects oversized input/read after bounded file admission. Import preview keeps **5** rows as a preview, not a case-count maximum (excess preview rows are dropped locally). |
| Judge rationale | **2000** characters | Invalid/oversized judge response is unscored/inconclusive. Not silently accepted as a verdict. |
| Logprob collection | **4096** chosen tokens, **16,384** chosen+alternative entries, **512 KiB** text+token bytes per response; **16 MiB** across run | Excess collection explicitly incomplete/inconclusive; run budget stops further detailed collection. Ordinary response byte limit also applies. |
| Logprob comparison | **2,000,000** entry comparisons; **16** example differences | Work exhaustion is explicit/inconclusive; example metadata truncates at 16 without hiding total divergence counts. |
| Timing-health delay windows | **4096** windows | Extra delayed windows merge into one conservative overflow interval; delays remain visible and affected timing remains inconclusive. Sampling interval **50 ms**, delay floor **100 ms** are evidence rules, not request maxima. |
| Pace/read evidence | **16** estimated tokens/read | Larger buffered reads cannot qualify fine-grained pace/gap timing; output itself is retained normally. |
| Metrics / preflight model-list body | **1 MiB** per response | Stops reading. Metrics mark response-limit/instrumentation unavailable (coverage can be inconclusive); preflight rejects or offers manual model entry. |
| Metrics labels / deployment snapshots | **16** labels/series, **256** characters/label; **32** deployment-history snapshots | Truncates label metadata / stops appending history; does not fabricate evidence for omitted devices or times. |
| Deployment metadata file / HTTP discovery | **64 KiB** | Oversized file rejects; discovery stops reading and reports unavailable evidence. Scope/collection text **512** characters. |
| Deployment sanitization | Depth **8** (report sanitization **24**); **128** keys; key length **128**; **256** list items; strings **8192** characters | Truncates metadata or inserts depth-limit marker. Redaction applies before persistence. This is metadata sanitization, not response retention. Launch command above **256** arguments rejects. Preflight header value above **8192** characters rejects. |
| SGLang OTLP trace body / trace file / row | **2 MiB** HTTP body; **16 MiB** file; **16 KiB** row | Oversized upload rejects/stops reading; collector stops adding evidence when file fills. Tail rejects oversized/rotated/truncated files/rows, making cancellation verification inconclusive. vLLM hook uses the same file/row caps and drops excess locally so observability cannot prevent request release. Request/model IDs above **4096** characters are ignored. |
| Ollama log startup header | **512 KiB**; trace-tail file/row bounds also apply | Stops reading the startup header. Missing single-slot startup proof leaves evidence unavailable/inconclusive. |
| Web live request window | **4096** slots/job | Evicts old request metadata and counts statuses in the summary; final report retains request records. Running evictions are tracked until completion; bounded by actual in-flight work, not all arrivals. |
| Web live conditions / events / phases | **2048** conditions, **100** recent events, **1024** phases/job | New excess conditions are not added locally; old events/phases evict. Existing condition totals continue updating. This bounds the live view, not retained report evidence. |
| Web API credentials / identifiers | **64** credential entries; run/baseline ID **160** characters; baseline name **120**; migration-note list **1000** entries | Rejects oversized/invalid input. Credentials are excluded from report config. |
| HTML evidence previews / fallback summary | Text **1200** characters; JSON **6000** characters; failure title **256**, failure summary **2048** | Truncates displayed/fallback metadata explicitly. HTML request-detail target is **25 by default**, expanded to include all check evidence; it is not a hard maximum. Full retained JSON remains available when saved successfully. |
| Saved-report top-level members | **64** | Rejects oversized/duplicate-member saved input while reading; request records and deduplicated inputs are separate arrays/tables and do not count as top-level keys. |
| Context observation preview | **1 MiB** generated characters | Bounds preview reconstruction work; incompatible historical observations are ignored rather than treated as measured latency. |
| Disk usage | Defaults **256 MiB** per-run JSON, **5 GiB** total retained runs, **1 GiB** minimum free | **Configurable**, not maxima; zero disables each. Start storage guard rejects insufficient space/total budget. Report export trims response bodies first, prioritizing evidence. If metadata alone exceeds per-run limit it is preserved with `limit_exceeded=true`; the limit is not a hard file-size promise. HTML is separate and may be omitted. |

Semantic limits such as probability/error percentages (0–1 or 0–100), temperature (0–2), unique enum choices and safe JavaScript seeds are preserved, with their schema bounds exposed. They validate meaning rather than client load. Legacy workload sample/pair counts stay at 100 and legacy token-size bounds stay 65,536; migration preserves their accepted workload within the current concurrency policy.

## Concurrency measurement

Measured on macOS 26.3.1 ARM64, Python 3.12.1, HTTPX 0.28.1. Each level runs three synchronized streaming cohorts, each response 1024 characters in four chunks at 50 ms cadence. The real `_Run.request`, `LLMClient`, SSE parser, scoring, record creation and `run.monitor` execute. A barrier ensures the fake stream actually reaches the offered in-flight level. CPU includes fake endpoint SSE production. No sockets were bound. In-process HTTPX MockTransport excludes HTTP connection-pool, kernel, network and TLS costs.

Acceptance rule: all requests complete and reach the level, no timing-health delay above the existing 100 ms floor, and maximum cohort-launch lateness below the existing traffic scheduler's default 100 ms tolerance. Launch lateness is measured from common cohort release to each coroutine's start; it includes serialized task/request preparation and is **not** steady arrival lateness. Arrival dispatch uses its separate 50 ms default tolerance. CPU/request diagnoses scaling; it is not a service-throughput certification.

| Offered / delivered peak | Completed | Timing-health max lag ms | Delayed ticks >100 ms | Launch p95 / max ms | CPU ms/request | Decision |
|---|---:|---:|---:|---:|---:|---|
| 256 / 256 | 768/768 | 33.43 | 0 | 34.47 / 42.13 | 0.263 | Acceptable |
| 512 / 512 | 1536/1536 | 30.30 | 0 | 70.46 / 74.50 | 0.264 | **Largest acceptable measured level** |
| 1024 / 1024 | 3072/3072 | 131.40 | 2 | 140.67 / 170.08 | 0.291 | Reject candidate ceiling |

Fixed-cadence fake-chunk lateness is also captured in the raw evidence, separately from heartbeat and request launch. The initial exploratory run overlapped the start of an arrival experiment; it was discarded and the table uses an isolated concurrency run. The accepted value is **512** across general simultaneous-request settings; session count retains its separate 32-conversation bound. CPU/request is nearly flat from 256 to 512 after removing the request-start active-set scan.

Request starts now touch at most two peak cohorts. Requests share a union-by-rank peak node; on completion path compression reads their exact lifetime maximum. Starts are O(1), without visiting active requests. Completion uses disjoint-set amortized inverse-Ackermann lookup (bounded constant work under the retained concurrency ceiling); it does not move the old O(in-flight) scan to completion. Each request creates one node; completed unreachable cohorts are reclaimed. Regression coverage compares lifetime peaks against a scanning reference through churn and checks target isolation, cancellation and client failure cleanup.

Socket mode, measured 2026-10-21 on the maintainer's Mac (Apple Silicon, `ulimit -n 8192`). Each level starts N streaming requests together and runs 3 cohorts. Every request gets four 256-byte chunks 50 ms apart. The fake endpoint runs in a **separate process** (a threaded HTTP server with a 4,096-connection backlog), so its own CPU does not share Giraffe's event loop. All requests completed with no failures. Raw data: [external endpoint](measurements/concurrency-socket-external.json). A same-process run gave similar values: [same-process endpoint](measurements/concurrency-socket-same-process.json).

| Socket-mode level | Delivered peak | Event-loop stalls >100 ms (3 cohorts) | Max event-loop lag | Scheduler lateness p99 / max | CPU/request |
|---|---|---|---|---|---|
| 256 | 256 | 0 | 57 ms | 67 / 69 ms | 0.80 ms |
| 512 | 512 | 3 | 153 ms | 133 / 136 ms | 0.87 ms |
| 1024 candidate | 1024 | 8 | 464 ms | 377 / 390 ms | 1.06 ms |

**Decision (user-approved, 2026-10-21): 512.** The stalls come from launching every request at the same moment. At 512 they are short (about 150 ms). Giraffe's timing-health check (100 ms floor) detects them and marks the affected timing evidence as client-delayed rather than failing the endpoint. At 1024 the stalls reach about 0.5 s, so much more timing evidence would become inconclusive. An accepted ceiling is not demonstrated delivery capacity.

The socket variant serves an async HTTP/1.1 chunked endpoint in the same process (no thread per connection), including production HTTPX pooling. It needs roughly twice the level in file descriptors because both client and fake-server sockets are local. Run with `ulimit -n 4096` or greater. It is validation for the orchestrator, not a missing claim silently replaced by in-process numbers. Resource-limit behavior follows [HTTPX's documented connection-pool limits](https://www.python-httpx.org/advanced/resource-limits/); event-loop responsiveness follows [Python's event-loop scheduling model](https://docs.python.org/3/library/asyncio-eventloop.html).

## Arrival measurement

The real `workloads.arrivals` scheduler dispatches at **1000/s**, with in-flight limit 256 and pending limit 256. Four SSE chunks arrive at 5 ms cadence, with 1024 distinct reasoning characters and a short final answer; 10% of answers deliberately fail scoring. This exercises successful and failure retention. Actual traffic, records and exports are measured, not extrapolated. Report checks are a small synthetic failure summary, not full-suite aggregation; other checks and larger bodies can add memory. The measurement temporarily bypasses only the schema's candidate arrival count. RSS sampling is every 50 ms; short spikes between samples may be missed. CPU includes the fake endpoint. The process runs 10,000 followed by 100,000; each case records its own RSS baseline, so these are growth measurements in a reused process.

| Requests | Completed / local drops | Dispatch p95 / p99 / max ms | Timing-health max lag ms | CPU ms/request | Sampled peak RSS growth | Decision |
|---|---|---|---:|---:|---:|---|
| 10,000 | 10,000 / 0 | 0.20 / 6.55 / 30.25 | 14.74 | 0.365 | 123.09 MiB | All experiment gates pass |
| 100,000 | 100,000 / 0 | 0.22 / 119.48 / 271.74 | 258.18 | 0.387 | 1150.34 MiB | **Keep 10,000**: dispatch exceeds 50 ms tolerance |

The conservative experiment memory gate was growth below **1 GiB**, to reserve ample margin within the **default 4 GiB** guardrail. The candidate exceeded that experiment gate, although it remained below the actual default guardrail; it did not trigger a memory stop. The timing failure independently blocks raising the cap. This small-body workload does not establish safe memory for arbitrary 4 MiB responses or larger input histories.

| Requests | Retention | Actual report JSON bytes | Bytes/request including report metadata | Response bodies trimmed for disk limit | Fits default 256 MiB JSON budget |
|---|---|---:|---:|---:|---|
| 10,000 | all | 35,401,275 | 3540.13 | 0 | Yes |
| 10,000 | failures | 26,149,280 | 2614.93 | 0 | Yes |
| 10,000 | none | 25,071,276 | 2507.13 | 0 | Yes |
| 100,000 | all | 268,435,227 | 2684.35 | 83,298 | Yes, after substantial trimming |
| 100,000 | failures | 261,545,545 | 2615.46 | 0 | Yes |
| 100,000 | none | 250,765,541 | 2507.66 | 0 | Yes |

These are exact post-retention/post-trimming JSON file sizes, not pre-trimming size estimates. The all-policy row loses 83.3% of response bodies at 100,000 despite only 1 KiB reasoning/request; this is a material limitation for users requesting all evidence. Request metadata alone nearly consumes the budget. The per-run setting applies to JSON, so adjacent HTML and web metadata can bring directory size above that number (recorded in the raw evidence). All policies also retain structured measurements even when bodies are omitted.

| Requests | Traffic peak RSS | Export all / failures / none peak RSS | Live slots peak | Events / conditions / phases peak | Evicted slots | Live JSON bytes |
|---|---:|---|---:|---|---:|---:|
| 10,000 | 154.98 MiB | 155.22 / 189.92 / 208.17 MiB | 4096 | 100 / 1 / 0 | 5,904 | 1,848,420 |
| 100,000 | 927.17 MiB | 1260.25 / 1294.00 / 1343.19 MiB | 4096 | 100 / 1 / 0 | 95,904 | 1,853,574 |

The experiment calls production `_track_live_progress`, including request eviction and all condition/event/phase updates, without an HTTP server. One real arrival condition is created by this workload; a separate backend test feeds **100,000** distinct conditions, requests and phases to the same production callback and verifies all four bounds, eviction counts and continued updates to retained conditions. Running-eviction sets drain to zero. The runner and retained report still hold request records; the live-window bound alone does not mean all run memory is constant.

Raw evidence: [concurrency JSON](measurements/phase7-concurrency-in-process.json), [arrival JSON](measurements/phase7-arrivals-in-process.json). Both include environment/time, full timing distributions, parameters and measured peaks. Socket-mode numbers are in the table above.

## Reproduction

Use the project environment, without `-I`:

```sh
PYTHONPATH=$PWD/src /Users/0dust/Documents/giraffe/.venv/bin/python scripts/measure_concurrency.py --mode in-process --output /tmp/concurrency.json
PYTHONPATH=$PWD/src /Users/0dust/Documents/giraffe/.venv/bin/python scripts/measure_concurrency.py --mode socket --output /tmp/concurrency-sockets.json
PYTHONPATH=$PWD/src /Users/0dust/Documents/giraffe/.venv/bin/python scripts/measure_arrivals.py --requests 10000 100000 --output /tmp/arrivals.json
PYTHONPATH=$PWD/src /Users/0dust/Documents/giraffe/.venv/bin/python -m pytest tests/test_run_limits_phase7_caps.py -q
```

Run measurements serially, with no competing benchmark/test processes. The concurrency script explicitly bypasses the accepted 512 schema for the rejected 1024 candidate, solely to reproduce this comparison. Arrival count similarly bypasses the 10,000 schema for the 100,000 experiment. These bypasses are confined to measurement scripts, not API input. See adjacent checked-in measurement JSON for parameters, environment, complete distributions and exact bytes.
