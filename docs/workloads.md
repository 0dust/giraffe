# Bounded workloads, deployment snapshots and serving telemetry

The ten standard endpoint checks keep their existing defaults. Additional
workloads are opt-in and share the same request, duration, concurrency and
per-request output ceilings. Use [examples/workloads.yaml](../examples/workloads.yaml)
as a starting point, then match its settings to your server and acceptance limits.

Built-in known-answer fixtures (correctness, JSON, context, short and traffic)
request temperature zero only when neither `request_options` nor the case options supply a temperature.
Case options take precedence over run options. None-valued options are omitted from payloads.
Semantic scoring uses the final answer after stripping leading inline `think`,
`thinking`, or `reasoning` blocks (case-insensitive), including outputs that start
inside reasoning opened by the chat template and end at a closing tag. Request evidence records
`reasoning_stripped`; raw outputs follow retention settings. Reasoning without a
final answer is unscored. With `finish_reason: length`, exact output is unscored only if it is a strict
normalized prefix of the expected answer; already-wrong answers fail. Incomplete
JSON is unscored, but a complete wrong value or schema violation fails. A missing
contains match under truncation is unscored; complete matching answers remain scorable. AI judges skip all truncated and reasoning-only outputs.
These missing scores leave coverage inconclusive rather than count as wrong answers.

Contains scoring normalizes whitespace and requires alphanumeric boundaries at
alphanumeric ends of the expected text (`4` cannot match `14` or `40`). Decimal
and comma separators adjacent to digits also join numbers (`.5` cannot match `1.5`). Exact and
contains remain case-sensitive. Request-pack and dataset previews warn when the
expected text already occurs verbatim in the case's messages; copy/extraction cases
remain allowed. JSON expected-value equality is structural, including nested arrays
and objects: numeric `3` equals `3.0`, while boolean `true` never equals numeric `1`.
Legacy custom fixture files allow only exact, contains, JSON and no scoring.

Context fixtures separate fact positions by at least 32 characters within each
input size. If the context budget cannot fit three positions, fewer are generated;
`workload.position_coverage` records the positions, minimum separation and reduced
coverage; reduced coverage prevents a context pass. Non-timeout HTTP/protocol
failures on context prompts fail the check; documented context-length rejections
remain incomplete coverage. Character placement and returned token counts remain separate evidence.

## Configure and run

```sh
giraffe run --config examples/workloads.yaml --output runs/expanded
giraffe baseline save runs/expanded/report.json baselines/expanded.json
giraffe run --config examples/workloads.yaml --baseline baselines/expanded.json \
  --output runs/after
giraffe inspect runs/after/report.json
giraffe export runs/after/report.json > reproduction.json
```

In the local UI, choose **Test under load → Requests at once** to edit the
request counts to test, question lengths in characters and answer caps in tokens.
The form shows the resulting size combinations directly. Sample minimums and
per-case time limits are available in advanced settings. **Choose individual
tests** keeps the full suite and its per-test controls available. Deployment
metadata, metrics profiles and separate metrics credentials remain available in
JSON. Advanced configuration accepts the same
`RunConfig` used by the CLI. Run results, standalone HTML and JSON include the
same request evidence, workload outcomes, configuration differences and telemetry.
The Configuration tab offers a reproduction export. Exporting never launches a
server, executes imported commands or recreates inaccessible server state.

`concurrency` (**Maximum in-flight requests**) caps simultaneous target requests
across the run. Choose the allowed load for the endpoint; Giraffe does not infer
server capacity from the machine running the client. `max_requests` (**Total request
limit**) caps total target requests across all selected tests and targets. It stays
independent of concurrency; increasing simultaneous load does not authorize more
total traffic. AI evaluator calls use separate limits.

`buckets.levels` selects the simultaneous request counts to test. In the simple
concurrent-load form, these counts also determine the run's concurrency ceiling;
custom configurations retain their independently configured ceiling. Each of the
nine input/output size pairs sends `max(samples_per_bucket, level)` requests at every
level, using a refilling worker pool. Completed requests immediately free a slot for the next request. For levels `[1, 4, 8]` and
a sample minimum of 4, bucket coverage needs `9 × (4 + 4 + 8) = 144` requests per
target. The review includes other selected tests in its plan and flags
insufficient request limits as you edit, with an explicit action to raise the
limit when a complete count is known. Adaptive work remains labeled as bounded
and built-in random arrival counts as estimates. Time limits, errors and cancellation can
leave coverage incomplete. In custom configurations, changing the global in-flight
limit does not change the tested levels.

The built-in question length targets are 64, 1,024 and 12,288 characters (roughly 3k tokens for the long input); answer caps
are 16, 48 and 96 tokens. Inputs are bounded by a conservative estimate of `context_limit`, output reserve and template overhead. These are editable presets, not categories of question
difficulty. The generator pairs every question target with every answer cap,
giving three times three cases. Answer instructions can add characters to the
question, and a token cap does not guarantee an answer reaches that length.

## Standard checks and load evidence

Access, serving and first output exclude capacity-stage traffic entirely. Other
stress traffic exempts timeouts and intentional cancellations, but non-timeout
HTTP and protocol failures still enter serving checks and `stop_after_errors`.
Documented context-length rejections remain coverage observations. ConnectTimeout
and ConnectError are transport failures for access; a response timeout remains a
timeout. Shared request and duration limits still apply.

Capacity sends independent arrivals and reports the highest tested rate for which
all tested rates at or below it were accepted. A higher-rate pass after a lower-rate
failure is flagged in `non_monotonic_results` and does not raise that boundary.
Answer timing ends at the last answer chunk: finish/usage messages, `[DONE]` and
connection cleanup do not extend generation time or stream gaps.

Fairness interleaves short-only and loaded samples of the same known-answer copy
sequence (48 common short English words). A long competitor starts after first output, with
a unique nonce at the beginning of its prompt to avoid identical-prefix reuse.
Overlap qualifies only when the short stream emits its last answer after the
competitor's dispatch. Buffered or insufficient overlap evidence is inconclusive
before evaluating slowdown. `fairness_max_ratio` compares median decoding time
from first to last answer, rather than total response latency; `stream_gap_ms`
checks answer pauses. Missing an acceptance ratio leaves the check inconclusive.
The fixture reserves its 48 needed word tokens plus 12 tokens of headroom; smaller
caps or observed length-finished answers leave fairness inconclusive. The word
sequence fits in 48 tokens with the Qwen2.5 and RoBERTa tokenizers.
This does not establish a scheduler cause.

Recovery first records light probes, then refills up to `concurrency` workers with
a rotating, scored mix of long-input and longer-output requests. Each worker starts
its next request as soon as its previous one completes, within the shared ceilings.
Sustained traffic reserves planned fixed requests of later checks plus post-load
probes. A half-remaining-quota cap applies only when later checks are selected.
The phase completes when its duration elapses with observed configured concurrency,
or its allowance is consumed after at least `max(min_samples, concurrency * 2)`
requests with the configured peak and at least 90% time-weighted occupancy (refill
and drain edges prevent an exact full-window average). Metrics report which
completion condition qualified. Timing and error-rate limits apply to recovery
probes; sustained timing and timeouts are observations. Genuine non-timeout
sustained server/protocol errors can still fail the error-rate limit. Scored
sustained answers still enter the correctness allowance. Post-load probes use the
same fixture as the pre-load baseline. `recovery_max_ratio` limits median latency
and first-output ratios. Without a timing limit or ratio, recovery is inconclusive.

Cancellation uses `test_options.cancellation.cancel_after_chunks` (default 5) to
interrupt an active answer stream. With streaming disabled, cancellation is
inconclusive: `client cancellation requires streaming`. The legacy `cancel_after_ms` setting is retained
for config compatibility; it no longer schedules the disconnect, and explicit values
produce a report warning. A disconnect before first output cannot establish
mid-stream cancellation. The stop test first obtains a full-output control containing
`blue [END] green`, then requests the same text with `[END]` as the stop sequence.
Enforcement needs the control to reach the marker, an answer of `blue` without the
marker or following text, and `finish_reason: stop`. Missing control/finish evidence
is inconclusive; an emitted marker or suffix fails. Output caps check reported token
usage when available, including consistency with `finish_reason: length`; a length
finish reason also provides cap evidence when usage is absent. Follow-up requests
check continued inference. Client disconnect still does not prove backend work
stopped; the optional log/trace verification supplies separate evidence.

Built-in correctness and JSON with configured concurrency above one require
observed overlap of at least two scored requests; otherwise they are inconclusive
("not exercised concurrently"). Configured concurrency one measures serial behavior.
Request-pack correctness scores each case's quiet answers and loaded capacity-stage
answers against `min_correctness`. Without capacity it runs a quiet pass and an
additional pass at global concurrency, covering all cases and sending at least
`concurrency` requests. Per-check correctness `samples` and `concurrency` overrides
are rejected in pack mode; global `samples` never selects the case count. The
planner includes this loaded pass, fairness and recovery baselines, and the
separate stop control. Adaptive traffic estimates warn when checks may be starved.
Loaded correctness still requires observed overlap when concurrency exceeds one.

Explicit cold-start runs use `readiness_timeout_seconds` (default 180 seconds)
after the restart hook, bounded by the global deadline. GET `/v1/models` polls
(falling back to the origin's `/health` on 404) back off from 0.1 to 5 seconds,
then one inference verifies readiness. Poll observations and the inference probe
are recorded separately and consume neither the shared request budget nor the
error allowance.

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

Conversation fixtures, modes, generation options, schedules and seeds contribute
to the workload fixture hash in the manifest, together with a hash of the loaded
workload generator source. Source changes conservatively prevent like-for-like
baseline comparison even when settings stay the same. Live histories may change input
work across runs: comparison checks observed input lengths and request hashes and
marks affected timing/rates inconclusive rather than claiming identical inputs.
Optional `consistency.logprobs` collects and compares content token probabilities
independently of answer hashes; see [Token probability consistency](#token-probability-consistency).

Percentiles include valid completions and show sample counts. Failures, timeouts,
cancellation, unknown lengths and finish reasons appear alongside them. Aggregate
requests/s and output tokens/s use first dispatch through last completion in that
scenario, including error time in the window. Missing output usage leaves aggregate
token throughput unknown. Per-request generation pace remains a client-observed
estimate between first/last answer chunks, separate from end-to-end throughput and
server TTFT statistics.

Timing acceptance uses the same generator scheduling-lag qualification for core
and additional workloads. If the generator cannot keep up, configured timing
limits remain inconclusive; observed task/protocol failures remain failures.
Mixed-load latency ratios require enough successful control/load samples, actual
overlap and exercised shapes before they can fail the interference check.
Without `limits.fairness_max_ratio`, mixed results are inconclusive: "effect
observed; no acceptance ratio configured". The matched light request copies a
known passage of at least 64 tokens (requires `max_output_tokens >= 96`; otherwise
no mixed requests are dispatched and the check is inconclusive). The active-stream competitor starts after
its first visible output and requires further visible light output after heavy
dispatch; buffered answers followed only by protocol trailers do not qualify.
Weighted repetitions use distinct pair identities and require the full configured
number of exercised pairs per shape/phase. Each qualifying light response must report at least
64 completion tokens; long-output competitors must report at least half their
requested cap, and long-input competitors must report at least twice the light
input tokens and at least 128 additional tokens. Missing usage is inconclusive.

`arrivals.max_pending` bounds only queued, undispatched arrivals. In-flight
requests use the global `concurrency` ceiling and do not occupy queue capacity.

Shared-prefix repeats run back to back, followed by a separate independent-control
block so single-slot caches can retain the shared prefix. The default shared
prefix is 8,192 characters (at least roughly 1k tokens with ordinary prose).
Independent controls have different leading text. Prefix acceptance requires at
least `limits.min_samples` successful repeats after the first use, each reporting
cached tokens covering at least half the estimated shared-prefix tokens, rounded
down to a 16-token block boundary (minimum one token). The estimate scales reported
prompt tokens by the shared-prefix character fraction. Missing token/cache usage
is inconclusive ("cache reuse unverified"); reported zero or insufficient cached
tokens fails ("no prefix reuse observed"). Timing improvement alone cannot pass.

Bucket windows limit dispatch, not request duration. Requests dispatched before
the cutoff drain under their normal server timeout; the window cannot create an
error. Too few samples is unfinished/inconclusive. Genuine request timeouts still
count as errors. Each cell reports time-weighted average in-flight and flags tail
samples that never overlap the requested number of requests. These samples cannot
qualify load or length coverage. Input usage must increase short < medium < long
within each level/output shape and be within a broad tokenizer allowance of the
requested size (characters / 10 through characters / 1.5 + 32 tokens); otherwise
"input length not exercised" is inconclusive.

Sessions seed a distinct index-derived bakery name in turn zero and score each
turn against that session's original value. This detects mixed histories even
when every session follows the same turn template. Live history preserves its own
actual answers. Fixed history uses the seeded first answer and configured saved
answers thereafter. The session name recall instruction is added to configured
turns; saved configurations retain their fields.

Text consistency uses `prompts_per_shape: 3` distinct prompts by default, including
known multi-sentence copy tasks, at temperature zero unless overridden. Every
shape/prompt identity gets a level-1 modal-answer baseline under the configured
byte/whitespace equality. Reports separate within-level repeatability agreement
from agreement with that baseline at every level. With `consistency.strict: true`,
any within-level or cross-level answer difference fails. Without strict mode,
variation is diagnostic and task correctness remains separate.

Default per-cell samples, mixed pairs, consistency repetitions and concurrent
session count are five; prefix repeats are six (five measured reuses). Planning
warns when a configured workload sample count is below `limits.min_samples`.
Budgets, missing shape evidence and inadequate overlap can still prevent a verdict.

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
- `ollama` / `unknown`: no serving counter mapping is assumed. Ollama native APIs
  supply deployment information, but an inference URL alone does not provide
  scheduler or KV-cache counters.

Published DCGM memory, utilization, temperature, throttle and device-error families
remain available alongside serving series. GPU checks require each observed `gpu`
label (and exporter `Hostname` when present) to have every family. A recent scrape
alone has unknown source freshness: evidence requires values changing across at
least two scrapes for that device, or explicit source timestamps within
`metrics_max_age_seconds`. Missing families, missing device labels or unknown
freshness are inconclusive. `gpu_min_observation_seconds` is an optional run setting
(default 30, minimum 0), independent of `metrics_interval_seconds`. Match it to your
exporter's refresh period, for example `gpu_min_observation_seconds: 1` for a
one-second exporter. GPU runs wait only for the remaining time between their first
GPU sample and the final `end` scrape, bounded by the run deadline; a shorter window
is inconclusive. No extra GPU `after` scrape is added: start, periodic and end
scrapes all share `metrics_max_samples`, with a slot reserved for `end`. Constant pre-existing XID values are historical warnings;
a new nonzero XID during observation fails. Hardware slowdown (0x8), software
thermal (0x20), hardware thermal (0x40) and hardware power brake (0x80) fail only
when observed in at least two snapshots. Idle, application clocks, software power
cap (0x4) and sync boost are informational. Devices absent from all scrapes remain outside coverage.
Every verdict is telemetry evidence, not device health certification. The default
window matches the [DCGM exporter default collection interval](https://docs.nvidia.com/datacenter/dcgm/latest/reference/command-line-reference/dcgm-exporter.html). Source references: [vLLM metrics](https://docs.vllm.ai/en/latest/design/metrics/)
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

## Token probability consistency

Run `giraffe run --config examples/logprob-consistency.yaml --output runs/logprobs`
after setting the example's URL and model to an existing vLLM endpoint. The
Repeated-request consistency editor exposes the same `logprobs` JSON object;
CLI YAML, API preview/export, saved runs and reruns use this configuration.
Omit it or set it to `null` to preserve ordinary answer-only consistency.

Enabling it sends `logprobs: true`, `top_logprobs` (0–20), and `stream: false`.
`response_mode: complete` is the only supported mode; `stream` is rejected during
configuration validation. The profile explicitly overrides global streaming for
these requests. A non-streaming completion is not evidence about streaming.

Each input shape uses a fixed corpus of identical independent prompts with
`max(levels)` numbered slots. Every condition replays that same corpus, first at
level 1, then at each configured level. `repetitions` is the number of measured
rounds in this mode. `priming_rounds` (0–2) adds excluded warmup rounds before each
condition. No response is appended to a later prompt. Temperature, seed, options,
model and messages stay fixed; the server's enforcement of requested generation
options remains unverified. Priming does not establish a cold cache or remove
possible ordering effects.

The request count per target is
`shapes × distinct([1, *levels]) × max(levels) × (repetitions + priming_rounds)`.
The example sends 48 requests. Preview shows this count before a run. Requested
concurrency, per-round client overlap, issued/completed/missing requests and
usable samples remain visible. Client overlap does not prove which requests the
server batched together. Rounds use a refilling worker pool. Each request records workload-local overlap;
tail requests that never reach the requested level cannot qualify coverage.
Each round also reports time-weighted average in-flight.
Request, duration, cancellation, context and error limits
still apply; unfinished rounds or inadequate overlap make coverage inconclusive.

For each corpus slot, the comparator groups matching effective request hashes,
observed model/backend identities and preceding token histories. At each aligned
position, min/max ranges cover all pairs, including later-versus-later variation.
Level 1 is always compared with every other level. `compare_levels: true`
(the default) additionally compares all pairs of concurrency levels. Within-level
comparisons alone describe run-to-run repeatability.
Token identity uses token IDs when provided, otherwise bytes, otherwise token
text. A token or length divergence is reported separately; probabilities after
diverging histories are never compared. Alternative tokens are matched by
identity rather than rank, and changed top-k membership is reported separately.
Returned alternatives are a partial distribution: an absent alternative is never
assigned zero probability. Returned lists may be shorter than requested.

Probability values use natural-log units. Non-finite or malformed values are
invalid evidence. The OpenAI `-9999.0` sentinel and absent requested alternatives
are unknown. Raw precision is retained for comparisons and exports; display
rounding does not set tolerance. With `strict: false` the probability result is
`diagnostic`, even when differences exist. Set both `strict: true` and an explicit
nonnegative `tolerance` to enforce numeric equality within that absolute tolerance.
The independent outer `consistency.strict` continues to govern answer equality.
A known numeric violation fails even if some other samples are incomplete.

Unsupported, missing, malformed, truncated, misaligned or incomplete evidence
cannot produce a probability pass. `support: discover` leaves an explicit
logprob-field rejection or missing logprobs inconclusive; `support: required`
fails those capability expectations. Unrelated HTTP errors and wrong answers
still use ordinary acceptance limits. Under strict logprob mode, token or length divergence between comparable
identities fails, even without a comparable numeric difference. Changed alternative
membership or unknown probabilities remains inconclusive unless a comparable
numeric pair already proves a violation.

Per-response evidence is bounded to 4,096 content tokens, 16,384 total chosen and
alternative entries, and 512 KiB of token text/bytes. Each run admits at most
16 MiB of serialized token evidence; each condition comparison admits two million
entries. Exceeding any bound is explicit and inconclusive, never silent sampling.
The transport's existing response-size limit also applies. Large top-k values
increase response size and work. Use smaller counts or output caps when appropriate.

JSON and HTML include separate answer and probability outcomes, extrema, coverage,
and representative request IDs and positions. Request evidence contains chosen
token bytes, positions, logprobs and returned alternatives. Existing `all`,
`failures` and `none` retention policies also govern these raw tokens; numerical
summaries survive body removal. Live progress events omit token bodies. Workload
settings and comparator source participate in baseline identity, so enabling or
changing this instrumentation prevents a like-for-like performance claim.

This check detects observed differences; it does not establish a scheduler cause,
model correctness, or a currently reproducible vLLM defect. The motivating
[vLLM report](https://github.com/vllm-project/vllm/issues/51187) describes particular
hardware, versions and concurrency. Later comments report clean runs after an
[RMSNorm fix](https://github.com/vllm-project/vllm/pull/48391). Record the actual
runtime/model/hardware and test the workload on your deployment; the original
reported concurrency threshold is not a universal trigger. The token evidence
contract follows the [Chat Completions API](https://developers.openai.com/api/reference/resources/chat).
