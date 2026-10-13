# Bounded workloads, deployment snapshots and serving telemetry

The ten standard endpoint checks keep their existing defaults. Additional
workloads are opt-in and share the same request, duration, concurrency and
per-request output ceilings. Use [examples/workloads.yaml](../examples/workloads.yaml)
as a starting point, then match its settings to your server and acceptance limits.

## Configure and run

```sh
giraffe run --config examples/workloads.yaml --output runs/expanded
giraffe baseline save runs/expanded/report.json baselines/expanded.json
giraffe run --config examples/workloads.yaml --baseline baselines/expanded.json \
  --output runs/after
giraffe inspect runs/after/report.json
giraffe export runs/after/report.json > reproduction.json
```

The local UI exposes workload JSON, deployment metadata, metrics profile and
separate metrics credentials. Advanced configuration accepts the complete same
`RunConfig` used by the CLI. Run results, standalone HTML and JSON include the
same request evidence, workload outcomes, configuration differences and telemetry.
The Configuration tab offers a reproduction export. Exporting never launches a
server, executes imported commands or recreates inaccessible server state.

## Workload settings and evidence

| Setting | Behavior | Evidence and limitations |
| --- | --- | --- |
| `arrivals` | Steady RPS or bursts, independent of the concurrency cap | Scheduled arrival, dispatch, completion, waiting and unissued counts; a bounded pending queue drops excess arrivals visibly. Seed is recorded; current schedule is deterministic and has no jitter. |
| `prefix` | Shared prefix plus deterministic independent prose controls, then increasing fixed history | Order, character shape, observed input tokens, separate first/repeat timing and `usage.prompt_tokens_details.cached_tokens` when provided. No cache flushing; first use is not proof of an empty cache. Missing cache usage means reuse is unverified. Controls match characters; observed token counts disclose tokenizer-dependent differences. |
| `buckets` | Short/medium/long inputs crossed with short/medium/long outputs at explicit levels | Each shape/level has its own sample target and maximum measurement window, overlap, correctness where applicable, timing, token distributions and finish reasons. Missing token usage stays unknown. Early EOS or a large cap alone does not exercise long output. |
| `buckets.heavy_weights` | A bounded mixture of long-input and long-output competitors | Matched short-only controls; competitors start before short dispatch or after its first visible output. Reports show achieved overlap, heavy outcomes, short latency and stream gaps. Unobserved overlap is inconclusive. Ratios and telemetry do not identify a scheduler cause. |
| `sessions` | Fixed-history or live-response conversations, sequential turns and bounded concurrent sessions | Live history appends the exact completed answer from that session; fixed history uses saved answers. Per-turn metrics, IDs, history/request hashes, requested delay and actual dispatch are saved. Failed/cancelled/truncated turns stop the session. Required history is never cropped. Conservative character admission can stop before the server context limit; actual tokens remain separate. |
| `consistency` | Identical independent requests, sequentially then at selected load levels | SHA-256 answer hashes and modal agreement fraction are computed before retention removes bodies. Equality is byte-for-byte by default; whitespace normalization is explicit. Correctness, errors and repeatability stay separate. Variation is diagnostic unless `strict: true`. Temperature/seed support is requested, not universally verified. |
| `tool_calling` | A real automatic `get_weather` function definition for Delhi | Complete and reconstructed streamed calls must have one function call, a call ID, the expected name and strict JSON arguments `{"city":"Delhi"}`. Rejections, missing calls, malformed JSON, wrong functions, schema errors and incomplete streams remain distinct request evidence. No generated tool is executed. |
| `forced_tool_diagnostic` | Optional forced-selection probe when tool calling is selected | Diagnostic results appear separately. Forced success cannot establish automatic selection; forced failures do not independently fail automatic capability. |

The default conversation asks: "Suggest a name for a bakery. Reply with only the
name, without quotation marks or a trailing full stop." Its next turn is "Repeat
exactly the above answer." The repeat answer is scored against the assistant
message supplied in that session's history: a saved answer in fixed mode, or the
actual preceding output in live mode. Exact scoring normalizes whitespace only;
it does not remove punctuation or quotation marks. Saved configurations using the
older bakery-specific repeat prompt remain scored.

Conversation fixtures, modes, generation options, schedules and seeds contribute
to the workload fixture hash in the manifest, together with a hash of the loaded
workload generator source. Source changes conservatively prevent like-for-like
baseline comparison even when settings stay the same. Live histories may change input
work across runs: comparison checks observed input lengths and request hashes and
marks affected timing/rates inconclusive rather than claiming identical inputs.
Log-probability requests can be supplied through request options, but Giraffe does
not currently persist or compare log-probabilities.

Percentiles include valid completions and show sample counts. Failures, timeouts,
cancellation, unknown lengths and finish reasons appear alongside them. Aggregate
requests/s and output tokens/s use first dispatch through last completion in that
scenario, including error time in the window. Missing output usage leaves aggregate
token throughput unknown. Per-request generation pace remains a client-observed
estimate between first/last answer chunks, separate from end-to-end throughput and
server TTFT statistics.

Timing acceptance uses the same client scheduling-delay qualification for core
and additional workloads. A 50 ms heartbeat observes event-loop responsiveness;
it cannot distinguish CPU overload from OS wake-up delays or other causes.
Delays above the existing threshold (the greater of 100 ms and 10% of warmed
median first-output time) qualify only overlapping request/metric intervals.
First-output qualification intervals end at the first useful output. Generation
pace is calculated between the first and last answer, but its qualification
interval starts at dispatch: a pause before the first read may buffer tokens and
artificially compress observed gaps. Completion and stream-gap qualification
intervals likewise start at dispatch and include the protocol trailer. Arrival
latency also includes scheduled-to-dispatch time.
Capacity stages include offered arrival windows and drain, including intervals
with no request in flight. Fairness ratios include their short-only controls.

Reports retain every raw timing sample and percentile. They record delay windows,
heartbeat sample/delay counts in `observations.client_timing_health`, and affected
request IDs/counts and metrics under each check's `timing_quality`. The UI shows
affected request counts. Overlap qualifies endpoint attribution; it does not
erase the client-observed elapsed time, prove a server defect or subtract a delay
from latency. An independently violated timing limit or task/protocol failure
still fails even when another sample was delayed.

The start of a stall between heartbeats is unknown, so its conservative window
starts at the last responsive tick. Up to 4,096 windows are retained; later
windows are represented by an enclosing overflow window, which can qualify extra
measurements conservatively. Legacy reports containing only a run-wide maximum
retain the old conservative qualification because overlap cannot be reconstructed.

Payload/history hashing occurs before the inference timer. Streaming timestamps
are captured when the client reads the chunk completing an SSE event, before JSON
parsing; non-streaming first output is observed with the full response body. These
are client observation times, not network-interface arrival or server TTFT.
Reports record `timing_measurement_version: 2`; timing/traffic comparisons against
older boundaries require a new baseline. Timing-version differences alone do not
invalidate correctness comparisons.

Configured timing limits remain inconclusive where relevant client delay prevents
qualification; observed task/protocol failures remain failures.
Mixed-load latency ratios require enough successful control/load samples and
actual overlap before they can fail the interference check.

## Deployment metadata

Each target accepts `deployment` with `model`, `runtime`, `serving`, `backend`,
`hardware`, `software` and a bounded `extension`. Common fields include requested
and served model, revision/digest, quantization, tokenizer/template identity,
engine/version/commit, image/digest, launch arguments, context and batching,
scheduler/KV/prefix settings, attention/kernel/compilation backend, GPU layout,
parallelism, driver/CUDA/ROCm and libraries. Missing fields stay unknown.

Supply structured JSON through `giraffe run --deployment-file metadata.json`,
`deployment.metadata_file`, or the UI. Imports are at most 64 KiB and allowlisted;
flat serving-export keys such as `max_model_len`, `max_num_seqs`,
`enable_prefix_caching` and `attention_backend` map to the structured fields.
`launch_command` accepts a string or ordered argv and records sanitized intent.
Arguments are never executed by discovery. Preserve precedence-sensitive ordering.
Arbitrary environment dumps and unknown top-level configuration fields are excluded.

Discovery is optional and read-only: `discovery: ollama` reads `/api/version` and
`/api/show`, with optional `/api/tags` and `/api/ps` for matching model digests and
active context length; `discovery: vllm` reads `/version`. Served model identifiers also come
from inference responses. A separately configured `collector_url` can return an
allowlisted JSON export with `scope` and `collected_at`. A collector never inherits
inference credentials. Use an accessible read-only export, not a privileged agent
or production debug mode. No field is inferred from this client's hardware.

Every captured field keeps user-supplied or runtime/collector provenance, collection
time and scope. Conflicting intended/reported values are preserved together.
Old user snapshots can be marked stale; untimestamped source data has unknown
underlying freshness. Start/end discovery and bounded periodic collector snapshots
can detect changes. No observed change is qualified by between-sample gaps; without
discovery, continuity remains unverified. One backend snapshot never implies that
all service replicas share its configuration.

The baseline stores its source report, outcome, timestamps, schema/suite/fixture
identity, measurements, acceptance limits and effective configuration. Saving a
failed baseline does not approve it. Baselines are never silently replaced; `--replace`
is explicit. Legacy snapshots are readable, with missing deployment fields unknown.
Workload incompatibility can block metric comparison while the deployment diff
still shows added, removed and changed fields. Runtime/image/backend changes alone
do not invalidate an otherwise comparable workload. `intended_change` names a
field (for example `serving.scheduler`); other observed changes flag the declared
one-setting experiment as confounded. Differences are context, not causality.

## Serving statistics

Enable `metrics: true` and configure a target's separate `metrics_url`. Credentials
use `metrics_api_key_env` and `metrics_headers_env`; inference credentials are never
copied to statistics endpoints, even on the same origin. Collection failure does
not prevent endpoint tests or change their correctness/performance outcomes.

Explicit profiles map exact metric names and units:

- `vllm-v1`: vLLM 0.30.0 V1 metric names for KV-cache fraction, running/waiting
  requests, preemption count, prefix hits/queries and server TTFT sum/count.
- `vllm-legacy`: an explicit older-name contract adding GPU/CPU cache fractions;
  qualify the recorded runtime/version before interpreting it as supported.
- `sglang`: running/queued requests, KV pool usage fraction, retraction counter,
  current retraction gauge, prefix-hit ratio, cached/prompt token counters and
  server TTFT sum/count. The old retraction gauge is never treated as a counter.
  The mapping follows [SGLang's collector at the tested commit](https://github.com/sgl-project/sglang/blob/44bc478863052efdc455b0282b49830f68fdefb3/python/sglang/srt/observability/metrics_collector.py).
  `deployment.discovery: sglang` reads `/get_server_info` for version, context,
  scheduler, cache/instrumentation settings and reported parallel sizes. Unknown
  settings and credentials are excluded; an MPS device does not establish MLX use.
- `ollama` / `unknown`: no serving counter mapping is assumed. Ollama native APIs
  supply deployment information, but an inference URL alone does not provide
  scheduler or KV-cache counters.

Published DCGM memory, utilization, temperature, throttle and device-error families
remain available alongside serving series. Missing or stale GPU data is not device
health certification. Source references: [vLLM metrics](https://docs.vllm.ai/en/latest/design/metrics/)
and [Ollama API](https://github.com/ollama/ollama/blob/main/docs/api.md).

Start/end and periodic scrapes preserve source timestamps where present, scrape
time, phase, units and labels. Bounds are explicit: `metrics_interval_seconds`,
`metrics_timeout_seconds`, `metrics_max_samples`, `metrics_max_series`; every scrape
also has a 1 MiB response limit and the global run deadline. Collection stops on
cancellation. Sample/cardinality limits and collection gaps remain visible.

Unavailable states distinguish not enabled, no endpoint, transport/timeout,
authentication denied, endpoint absent, empty success, unsupported format, absent
families and stale samples. Empty HTTP 200/`[]` establishes neither health nor
unsupported/disabled instrumentation. Instrumentation claims keep their provenance;
configured-collector evidence is separate from a user's unverified declaration.

Gauges report observed range/peak. Counter changes are per label series and become
unknown on reset or insufficient samples. Different replica labels are never joined.
Published statistics may include unrelated traffic. A recent scrape alone cannot
prove the underlying data is fresh. Cache occupancy exceeding
`cache_pressure_threshold` is reported as observed, not exercised or unverified;
peaks between samples can be missed. Cache pressure and preemption are diagnostic
context and never independently fail an endpoint acceptance check.

## Retention and secrets

Response bodies are transient for live history, scoring and consistency hashes.
Stored response and tool-argument bodies follow `retention: all|failures|none`.
Deployment imports sanitize supported arguments, nested credential fields,
credential-bearing URLs and secret environment expressions before persistence.
Reports, UI job metadata, baseline diffs and reproduction exports keep secret
reference names, not resolved secret values. Redactions are visible. Supply omitted
credentials/settings manually when reproducing a run.
