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
level, enough for at least one full concurrent batch. For levels `[1, 4, 8]` and
a sample minimum of 4, bucket coverage needs `9 × (4 + 4 + 8) = 144` requests per
target. The review includes other selected tests in its plan and flags
insufficient request limits as you edit, with an explicit action to raise the
limit when a complete count is known. Adaptive work remains labeled as bounded
and built-in random arrival counts as estimates. Time limits, errors and cancellation can
leave coverage incomplete. In custom configurations, changing the global in-flight
limit does not change the tested levels.

The built-in question length targets are 64, 256 and 512 characters; answer caps
are 16, 48 and 96 tokens. These are editable presets, not categories of question
difficulty. The generator pairs every question target with every answer cap,
giving three times three cases. Answer instructions can add characters to the
question, and a token cap does not guarantee an answer reaches that length.

## Workload settings and evidence

| Setting | Behavior | Evidence and limitations |
| --- | --- | --- |
| `arrivals` | Steady RPS or bursts, independent of the concurrency cap | Scheduled arrival, dispatch, completion, waiting and unissued counts; a bounded pending queue drops excess arrivals visibly. Seed is recorded; current schedule is deterministic and has no jitter. |
| `prefix` | Shared prefix plus deterministic independent prose controls, then increasing fixed history | Order, character shape, observed input tokens, separate first/repeat timing and `usage.prompt_tokens_details.cached_tokens` when provided. No cache flushing; first use is not proof of an empty cache. Missing cache usage means reuse is unverified. Controls match characters; observed token counts disclose tokenizer-dependent differences. |
| `buckets` | Short/medium/long inputs crossed with short/medium/long outputs at explicit levels | Each shape/level has its own sample target and maximum measurement window, overlap, correctness where applicable, timing, token distributions and finish reasons. Missing token usage stays unknown. Early EOS or a large cap alone does not exercise long output. |
| `buckets.heavy_weights` | A bounded mixture of long-input and long-output competitors | Matched short-only controls; competitors start before short dispatch or after its first visible output. Reports show achieved overlap, heavy outcomes, short latency and stream gaps. Unobserved overlap is inconclusive. Ratios and telemetry do not identify a scheduler cause. |
| `sessions` | Fixed-history or live-response conversations, sequential turns and bounded concurrent sessions | Live history appends the exact completed answer from that session; fixed history uses saved answers. Per-turn metrics, IDs, history/request hashes, requested delay and actual dispatch are saved. Failed/cancelled/truncated turns stop the session. Required history is never cropped. Conservative character admission can stop before the server context limit; actual tokens remain separate. |
| `consistency` | Identical independent requests, sequentially then at selected load levels | SHA-256 answer hashes and modal agreement fraction are computed before retention removes bodies. Equality is byte-for-byte by default; whitespace normalization is explicit. Correctness, errors and repeatability stay separate. Variation is diagnostic unless `strict: true`. Temperature/seed support is requested, not universally verified. |
| `tool_calling` / `checks: [tools]` | Automatic tool selection using the built-in Delhi weather request or configured `tool_checks.fixtures` | Separate response protocol, tool-call and optional assistant-text outcomes. Calls must match the expected count, order, names, argument schemas and any exact argument values. No tool is executed. |
| `tool_checks` | Custom tool fixtures and complete/stream response modes | Every declared expectation must pass. Empty fixtures preserve the built-in weather check; omitted modes inherit the run streaming setting. See the mixed text/tool example below. |
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
Mixed-load latency ratios require enough successful control/load samples and
actual overlap before they can fail the interference check.

## Assistant text alongside tool calls

Use [examples/tool-content.yaml](../examples/tool-content.yaml) to require both
assistant prose and a function call in one response:

```sh
giraffe run --config examples/tool-content.yaml --output runs/tool-content
```

Set its endpoint and model to a deployment that supports mixed assistant content
and tool calls. This example is a configurable acceptance requirement, not a claim
that a particular model or runtime has passed live qualification. Models that omit
required prose fail that requirement even when they return the right call. No
function executes: required prose is assistant content, not a tool result or an
answer from a subsequent conversation turn.

In the check catalogue, open **Tool-call correctness → Settings** to choose response
modes and edit the fixture list as JSON. `[]` restores the built-in weather fixture.
The advanced editor accepts the same configuration. Each custom fixture defines
`fixture_id`, `messages`, OpenAI-compatible function `tools`, and either
`expected_calls` or an explicit `choice` rule.
Fixture IDs are unique, use letters, digits, underscores, hyphens or dots, start
with a letter or digit, and have at most 128 characters. Each configuration has at
most 16 fixtures, each with at most 16 tools and expected calls; the entire
`tool_checks` configuration is limited to 64 KiB. `options` accepts supported
request settings, such as temperature, without overriding the tool contract.

`expected_calls` compares exact call count and order. Each call must have a call ID,
a declared function name and valid strict JSON arguments satisfying its function's
parameter schema. An `arguments` object additionally requires that exact value;
`arguments: null` checks the schema without fixing the value. Numeric values compare
by value (`1` equals `1.0`, and `-0.0` equals `0`); booleans remain distinct from
numbers, including in nested arrays and objects.

Tool parameter schemas use a bounded JSON Schema subset. Supported assertions
are `type` (including nullable type lists), `enum`, `const`, `required`, numeric
minimum/maximum and exclusive bounds, and string/array/object minimum and maximum
sizes. Structural schemas support `properties`, `additionalProperties`, `items`,
`additionalItems`, `prefixItems`, finite `anyOf`/`oneOf`/`allOf`, and acyclic local
JSON-pointer `$ref` references through `$defs` or `definitions`. Standard descriptive
annotations are allowed. Draft 4, 6, 7, 2019-09 and 2020-12 dialects are accepted;
the chosen dialect's normal keyword semantics apply.

Configuration rejects regex keywords, `format`, `uniqueItems`, `multipleOf`,
conditionals, recursive references and other unsupported keywords rather than
silently ignoring their assertions. These restrictions apply to tool schemas,
not the separate structured-JSON scorer. They prevent model output or an operator
schema from blocking the runner with regex backtracking or recursive combinators.

Each schema and argument object is limited to 64 KiB and depth 32. Schemas may
contain at most 2,048 JSON value nodes and expand to at most 256 schema nodes after
following references; argument objects may contain at most 1,024 value nodes.
The product of expanded schema nodes and argument value nodes must be at most
4,096, which also bounds the error trees built by failing combinators. Configured
expected arguments exceeding these limits are rejected. Generated arguments
exceeding them produce an **inconclusive** evaluation, never a pass or an
incorrect-answer verdict. Objects, arrays and scalar values each count as one
node; object keys do not count as separate nodes.

`expected_content` is optional. Omitting it permits tool-only responses, including
null content. When supplied, `mode: exact` compares the entire concatenated
assistant content, including text arriving before, between or after streamed tool
fragments. This detects missing or duplicated text. `mode: contains` only requires
the specified nonempty substring; it permits additional and repeated prose.
`normalization: none` preserves whitespace; `normalization: whitespace` collapses
whitespace runs and trims the result before comparison. These checks evaluate the
assembled content, not whether text preceded or followed a call on the wire.

`modes: [complete]`, `[stream]` or `[complete, stream]` selects the request modes.
Omitting modes runs both when global `stream: true`, and complete responses when
`stream: false`. Any explicit streaming mode requires global streaming to be
turned on. Every fixture/mode combination has its own sample coverage. The review
screen includes these combinations in its request budget.

Request evidence shows separate **Response protocol**, **Assistant text** and
**Tool calls** outcomes, the declared expectations, and retained actual calls.
Incomplete streams remain protocol failures; a correct call cannot hide missing
required text. Response events retain bounded metadata about arrival order without
text or argument fragments, and disclose truncation. Response text and actual
call bodies follow the run retention setting, while scoring remains available.
Older reports without these outcomes remain readable.

Complete and streaming requests are independent generations. Different outputs do
not by themselves prove that a runtime dropped text. Replayed identical outputs
can establish reconstruction correctness; live runs establish only whether the
observed responses met the declared acceptance requirements.

### Tool-choice contract checks

[examples/tool-choice.yaml](../examples/tool-choice.yaml) exercises all four choices
in complete and streaming responses. Set its endpoint and model, then run
`giraffe run --config examples/tool-choice.yaml`. No tool executes.

Add `choice` to a fixture to check the request's API rule separately from the task:

| `choice.mode` | Accepted completed response |
| --- | --- |
| `none` | Zero structured tool calls. Prose, refusal text and tool-shaped JSON text are permitted. |
| `required` | At least one declared function call with valid arguments. |
| `named` | At least one call, every call to `choice.name`, with valid arguments. |
| `auto` | Prose or declared function calls with valid arguments. |

`choice.max_calls` bounds calls at 1–16 (default 16). `none` always permits zero;
`parallel_tool_calls: false` limits every other mode to one call. Set it to true to
explicitly allow multiple calls within `max_calls`, or omit it to leave the server
setting unspecified. `max_calls` is a local acceptance bound, not an API field.
The typed rule owns `tools`, `tool_choice` and `parallel_tool_calls` for this fixture,
including overriding global request options. Sent tool options are recorded.
Legacy forced-choice diagnostics apply only to fixtures without `choice`.

`expected_calls` may be omitted or null for contract-only checks. An explicit `[]`
asserts that the task should produce no calls. A nonempty list adds exact task
expectations as described above. Contradictions with `choice` are rejected before
sending traffic. For example, `auto` can pass mode compliance and fail a task that
expected a call. Optional `expected_content` still checks assistant text separately.

`choice.support: required` is the default: API rejection fails acceptance.
`support: discover` records HTTP 400, 422 or 501 as **rejected**, leaves the verdict
**inconclusive**. Rejections still count toward the global
`stop_after_errors` bound. These statuses do not prove the option is unsupported; rejection can have another cause. Authentication, routing, throttling,
server errors and transport failures remain failures. A completed response violating
the choice rule fails even during discovery. **Observed** support means only that
this response honored the mode; it is not a guarantee about other prompts or models.

Protocol/completeness, choice compliance, argument schemas, task expectations and
assistant text have separate outcomes in JSON, HTML and request details. Giraffe
assembles streamed fragments before evaluation. Timeouts, truncation and early EOF
cannot pass `none`. Every configured fixture/transport pair needs coverage; budget
exhaustion cannot turn an incomplete matrix into a pass. Existing request, token,
duration, concurrency and retention limits apply. Stored response bodies follow
retention; configured expectations, sent tool options and verdicts remain available.

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
The example sends 40 requests. Preview shows this count before a run. Requested
concurrency, per-round client overlap, issued/completed/missing requests and
usable samples remain visible. Client overlap does not prove which requests the
server batched together. Rounds run in bounded batches; a final batch may be
smaller than the requested level. Each request records its own observed overlap.
Request, duration, cancellation, context and error limits
still apply; unfinished rounds or inadequate overlap make coverage inconclusive.

For each corpus slot, the comparator groups matching effective request hashes,
observed model/backend identities and preceding token histories. At each aligned
position, min/max ranges cover all pairs, including later-versus-later variation.
`compare_levels: true` additionally compares each pair of concurrency levels.
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
still use ordinary acceptance limits. A strict result with token divergence,
changed alternative membership or unknown probabilities remains inconclusive
unless a comparable numeric pair already proves a violation.

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
