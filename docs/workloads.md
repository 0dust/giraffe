# Bounded workloads, deployment snapshots and serving telemetry

The public catalog has 17 checks. Concurrent workloads combines the former fairness, size/concurrency coverage and mixed-interference checks. Additional
workloads are opt-in and share the same request, duration, concurrency and
per-request output ceilings. Use [examples/workloads.yaml](../examples/workloads.yaml)
as a starting point, then match its settings to your server and acceptance limits.

Built-in known-answer fixtures (correctness, JSON, context, short and traffic)
request temperature zero only when neither `request_options` nor the case options supply a temperature.
Case options take precedence over run options. None-valued options are omitted from payloads.
Semantic scoring uses the final answer after stripping closed inline `think`,
`thinking`, or `reasoning` blocks anywhere in the output (case-insensitive), including outputs that start
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
nine input/output size pairs sends at least `max(samples_per_bucket, level)` requests at every
level, rounded up to a multiple of that level, using a refilling worker pool. Completed requests immediately free a slot for the next request. For levels `[1, 4, 8]` and
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

Access, serving and first output exclude capacity-stage and sustained traffic entirely. Other
stress traffic exempts timeouts and intentional cancellations, but non-timeout
HTTP and protocol failures still enter serving checks and `stop_after_errors`.
Documented context-length rejections remain coverage observations. ConnectTimeout
and ConnectError are transport failures for access; a response timeout remains a
timeout. Shared request and duration limits still apply.

Capacity sends independent arrivals and reports the highest tested rate for which
all tested rates at or below it were accepted. A higher-rate pass after a lower-rate
failure is flagged in `non_monotonic_results` and does not raise that boundary.
Answer timing ends at the last answer chunk: finish/usage messages, `[DONE]` and
connection cleanup do not extend generation time or stream gaps. A streamed
single-chunk answer with reported `completion_tokens <= 2` has no possible
inter-chunk gap and is exempt from missing-gap evidence. Longer or unknown-length
single-chunk answers still lack timing evidence (they may have been buffered).

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
completion condition qualified. Both completion paths require at least
`limits.min_samples` successful sustained requests; fewer is inconclusive. Sustained
requests already dispatched drain under their request deadlines after the dispatch
window ends. Cancelled or unfinished sustained requests remain in recovery evidence
and count as errors, including cancellation at phase end; they cannot disappear
from the denominator or qualify as successful load samples. Sustained
load uses configured concurrency, so its timeouts and errors count against recovery
`max_error_rate` (including when duration elapsed). `sustained_error_rate` includes
those failures; the legacy `sustained_non_timeout_error_rate` remains a narrower
diagnostic. Sustained timing is observational; timing limits apply to recovery
probes. Sustained outcomes stay outside serving/first-output checks. Scored
sustained answers still enter the correctness allowance. Post-load probes use the
same fixture as the pre-load baseline. `recovery_max_ratio` limits median latency
and first-output ratios. Set it even when absolute timing limits are configured
(the examples use `2`): a large slowdown can remain inside an absolute limit. An
absolute-only pass states both measured probe/baseline ratios and that no ratio
limit was configured. Without a timing limit or ratio, recovery is inconclusive.

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
| `prefix` | Shared prefix plus deterministic independent prose controls, then increasing fixed history | Order, character shape, observed input tokens, separate first/repeat timing and `usage.prompt_tokens_details.cached_tokens` when provided. No cache flushing; first use is not proof of an empty cache. Missing cache usage means reuse is unverified; independent controls must report less than the reuse threshold. Controls match characters; observed token counts disclose tokenizer-dependent differences. |
| `concurrent_workloads` | Short/medium/long inputs crossed with short/medium/long outputs at explicit levels | Each shape/level has its own sample target and maximum measurement window, overlap, correctness where applicable, timing, token distributions and finish reasons. Missing token usage stays unknown. Early EOS or a large cap alone does not exercise long output. |
| `concurrent_workloads.heavy_weights` | A bounded mixture of long-input and long-output competitors | Matched short-only controls; competitors start before short dispatch or after its first visible output. Reports show achieved overlap, heavy outcomes, short latency and stream gaps. Unobserved overlap is inconclusive. Ratios and telemetry do not identify a scheduler cause. |
| `sessions` | Fixed-history or live-response conversations, sequential turns and bounded concurrent sessions | Live history appends the completed final answer after stripping inline reasoning; fixed history uses the seeded session name at every turn. Sessions refill as each conversation finishes. Per-turn metrics, IDs, history/request hashes, requested delay and actual dispatch are saved. Failed/cancelled/truncated turns stop the session. Required history is never cropped. Conservative character admission can stop before the server context limit; actual tokens remain separate. |
| `consistency` | Identical independent requests, sequentially then at selected load levels | SHA-256 answer hashes and modal agreement fraction are computed before retention removes bodies. Equality is byte-for-byte by default; whitespace normalization is explicit. Correctness, errors and repeatability stay separate. Variation is inconclusive unless `strict: true` makes it fail; full agreement can pass. Temperature/seed support is requested, not universally verified. |
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

Mixed light requests copy a non-repetitive passage. Only correct, completed light
answers with at least 64 reported completion tokens qualify timing comparisons;
wrong light answers are reported as unqualified, rather than failing mixed traffic.
Every planned pair sends both requests, including a heavy request after a buffered
light response; those trials cannot qualify active-stream overlap. The exact count
is `mixed_pairs + 4 × mixed_pairs × sum(heavy_weights)` when concurrency is at least
two and the output cap is at least 96. Missing acceptance ratios are described as
measurements without a limit; insufficient shape/overlap reasons take precedence.
Fixed-only session plans count character-admitted turns exactly. Live plans remain
bounded because actual final-answer lengths determine later history admission.

`arrivals.max_pending` bounds only queued, undispatched arrivals. In-flight
requests use the global `concurrency` ceiling and do not occupy queue capacity.
`arrivals.max_client_wait_ms` sets the tolerated scheduled-to-dispatch delay,
defaulting to 50 ms to allow ordinary local dispatch overhead. Delays above the
threshold leave the configured rate unqualified/inconclusive. Reports retain
`client_wait_threshold_ms`, `max_client_wait_ms` and the delayed count; set a tighter
or looser threshold to match your generator and arrival requirements.

Shared-prefix repeats run back to back, followed by a separate independent-control
block so single-slot caches can retain the shared prefix. The default shared
prefix is 8,192 characters (at least roughly 1k tokens with ordinary prose).
Independent controls have different leading text. Prefix acceptance requires at
least `limits.min_samples` successful repeats after the first use, each reporting
cached tokens covering at least half the estimated shared-prefix tokens, rounded
down to a 16-token block boundary (minimum one token). The estimate scales reported
prompt tokens by the shared-prefix character fraction. Independent controls must
all stay below that same per-request reuse threshold; otherwise the result is
inconclusive ("cache reporting not discriminative"). Missing token/cache usage
is inconclusive ("cache reuse unverified"). Reports show k of n repeat misses;
only a majority of the planned repeats missing reuse fails ("no prefix reuse
observed"). At least `min_samples` reuses are required to pass. Timing improvement
alone cannot pass.

Bucket windows limit dispatch, not request duration. Requests dispatched before
the cutoff drain under their normal server timeout; the window cannot create an
error. Too few samples is unfinished/inconclusive. Genuine request timeouts still
count as errors. Each request records in-flight overlap integrated over its own
lifetime, from dispatch to completion. Level 1 always qualifies; higher levels
require a lifetime average of at least `level - 0.5`. Peak overlap remains diagnostic.
Cells report their overall time-weighted in-flight average and excluded tail IDs;
at least `min_samples` qualified requests are needed. No uncounted fillers run.
Input usage must increase short < medium < long within each level/output shape.
The long-minus-short median token span must be at least one tenth of the requested
character span. A fixed chat-template preamble cancels in that difference; no
absolute upper bound rejects a healthy template. Missing or compressed length
evidence leaves "input length not exercised" inconclusive.

Sessions seed a distinct index-derived bakery name in turn zero and score each
turn against that session's original value. This detects mixed histories even
when every session follows the same turn template. Live history preserves its own
final answers after stripping inline reasoning. Fixed history uses the seeded name
at every turn; legacy `saved_answers` fields are retained for compatibility and
do not inject contradictory names. The session worker pool refills immediately
when a conversation finishes. The session name recall instruction is added to configured
turns; saved configurations retain their fields.

Text consistency uses `prompts_per_shape: 3` distinct prompts by default, including
standalone non-contradictory, non-repetitive multi-sentence copy tasks. Temperature
defaults to zero only when neither run request options nor an explicit workload
option sets it. An explicit `consistency.temperature` takes precedence. Every
shape/prompt identity gets a level-1 modal-answer baseline under the configured
byte/whitespace equality. Reports separate within-level repeatability agreement
from agreement with that baseline at every level. With `consistency.strict: true`,
any within-level or cross-level answer difference fails. Without strict mode,
variation is diagnostic and makes the check inconclusive with “variation observed;
enable strict to enforce”; full agreement can pass. Wrong answers are reported in `correctness_observed`
and in the summary when below 1.0; they never independently fail consistency. Text repetition counts round up to full
load groups at each level, matching the request plan.

Default per-cell samples, mixed pairs, consistency repetitions and concurrent
session count are five; prefix repeats are six (five measured reuses). Planning
warns when a configured workload sample count is below `limits.min_samples`.
Budgets, missing shape evidence and inadequate overlap can still prevent a verdict.

## Concurrent workload scenarios

Select `concurrency` and configure the single `concurrent_workloads` block.
`scenarios` selects any combination of `size_matrix`, `mixed_interference`,
`streaming_fairness` and `token_stress`. Each has a separate status, summary,
measurements and request evidence under `metrics.scenarios`. A failed scenario
is not hidden by another scenario with incomplete coverage. Legacy `fairness`,
`buckets` and `mixed` selections are migrated to the corresponding scenarios;
saved historical reports remain readable.

- **Size matrix:** short/medium/long inputs crossed with output caps at each
  `levels` value. `samples_per_bucket` and `window_seconds` bound each cell.
  Actual lengths, achieved overlap, timing, errors and scoring remain per cell.
- **Mixed interference:** short-only controls and long-input/long-output pairs,
  both heavy-first and short-already-streaming. Each phase retains control/mixed
  timing, slowdown ratio, observed overlap and sample qualification.
- **Streaming fairness:** the former fairness workload, including baseline,
  latency ratio, streaming-pause maximum, overlap counts, errors and correctness.
- **Token stress:** each `stress_cases` entry runs at every configured level.
  Defaults request 10,000 input / 50 output summary tokens and a short prompt
  (at most 128 observed input tokens) / 10,000 output tokens. The cases, target
  sizes and `min_output_tokens` are configurable. `input_token_tolerance` and
  `calibration_attempts` control bounded sizing from returned usage, including
  chat-template overhead. Calibration requests consume the same global budget.

The large-token example is [concurrent-workloads.yaml](../examples/concurrent-workloads.yaml).
The server must support the requested context/output sizes. The suite never
silently lowers targets to fit global caps. Missing token usage, early output
termination, insufficient overlap or incomplete budgets leave coverage
inconclusive; request errors and incorrect scored answers remain failures.
The summary task checks that the required box-label fact survives; it does not
certify general summary quality. Long-generation content is unscored. These
examples define future runs; existing small-model reports are not new 10k-token
qualification evidence.

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
cap (0x4) and sync boost are informational. If `targets[].deployment.hardware.gpu_count`
declares more devices than observed, coverage is inconclusive.
`limits.gpu_max_temperature_c` optionally fails a device above the limit in at
least two snapshots (not twice within one snapshot); stale/invalid samples do not
count. Without a limit, `max_temperature_c` is a measurement only. Unpublished
devices cannot be discovered when no device count is declared.
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
tail requests whose lifetime average is below `level - 0.5` are listed and
excluded from probability comparisons (level 1 always qualifies). Each condition
needs at least `min_samples` qualified usable samples and comparable repeat pairs.
Each round also reports its overall time-weighted average in-flight.
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
`diagnostic` only when comparable evidence agrees within tolerance. Numeric drift
beyond `tolerance` (zero when omitted), token divergence or changed top-k membership
is inconclusive and cannot certify the consistency check. Set both `strict: true` and an explicit
nonnegative `tolerance` to enforce numeric equality within that absolute tolerance.
The summary always states the maximum cross-level delta and token divergence
count, including diagnostic runs. Strict cross-level equality requires a
[batch-invariant serving configuration](https://docs.vllm.ai/en/stable/features/batch_invariance/).
vLLM is not batch-invariant by default; strict token-divergence failures are
expected in that default mode, even at temperature zero. Enable and qualify its
`VLLM_BATCH_INVARIANT=1` mode before interpreting strict equality as an acceptance
requirement. The independent outer `consistency.strict` governs answer equality.
A known numeric violation fails even if some other samples are incomplete.

Unsupported, missing, malformed, truncated, misaligned or incomplete evidence
cannot produce a probability pass. `support: discover` leaves an explicit
logprob-field rejection or missing logprobs inconclusive; `support: required`
fails those capability expectations. Unrelated HTTP errors still use ordinary
acceptance limits; answer correctness is reported independently. Under strict logprob mode, token or length divergence between comparable
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

Inference connection establishment uses an HTTPX connect timeout of
`min(request_timeout_seconds * 0.9, 10)` seconds, strictly inside the request
deadline. Failure to connect is transport failure (blocked access if no request
succeeded); timeout waiting for an answer remains inference-timeout evidence.

## Tool-result conversations

`tool_checks.conversations` adds bounded conversations to the tool check. Run
`giraffe run --config examples/tool-result-conversation.yaml --output runs/tool-results`
against a running tool-capable endpoint. The example targets local Ollama; edit
its URL and model for your server. The Tools editor accepts the same fixture JSON,
and configuration preview, CLI export and rerun preserve it.

Each fixture declares initial `messages`, `tools`, one to four `steps`, and
`final_content` assertions. A step declares one to sixteen ordered
`expected_calls`, each with a function `name`, exact `arguments`, an inert JSON
`result`, and optional `outcome: success` or `error`. Giraffe validates the actual
call IDs, functions and arguments, appends the actual assistant message, and sends
each result with that call's actual `tool_call_id`. No tool code runs. Complete
and streaming responses are separate sessions; streaming calls are reconstructed
before any result is supplied. Unsupported assistant fields fail
explicitly rather than silently dropping history.

Include `{{observation_id}}` in a result and a final assertion. Each session gets a
fresh value that is absent from its initial question. All final assertions must
match: `exact` compares the entire answer, `contains` checks a literal substring,
and `normalization: whitespace` optionally ignores whitespace differences. Choose
assertions strong enough for your task: a substring such as `15` alone does not
prove a correct temperature. The example additionally checks the conditions and
fresh observation ID, while allowing prose formatting.

Tools remain available with `tool_choice: auto` on every request. A repeat of a
successfully supplied operation fails, even if the model changes its call ID or
JSON key order. Fingerprints include the function and parsed arguments; numeric
`1` and `1.0` match, but booleans and numbers do not. A different operation is
allowed only when declared in the next step. A declared error result permits a
retry. A step's optional `user_message` starts a new question and resets the set
of satisfied operations; this allows deliberate later reuse of a tool. Calls
within a step must match the declared order. Unplanned calls fail without being
supplied results.

Request and duration limits, cancellation, error-stop limits, response-size and
output limits still apply. Histories also use the configured context ceiling,
with a serialized UTF-8 byte admission bound for history and tool definitions.
This is a heuristic because tokenization is endpoint-specific. A timeout, truncated answer, or exhausted budget leaves a
conversation inconclusive; it never promotes a successful first call to a passed
conversation. A verified repeat remains a failure even if the response later
truncates. Protocol/API errors, unexpected calls, and final-answer mismatches have
separate reasons. A pass establishes the configured bounded conversation, not
absence of loops on arbitrary tasks.

JSON, standalone HTML and request details include session/turn IDs, actual call
IDs, operation fingerprints, repetition counts, supplied-result request IDs and
final-answer checks. Dynamic history, assistant messages, result bodies and
expanded assertions follow response retention; status and fingerprints remain.
Fixture configuration remains in reproduction exports, so use synthetic result
data rather than secrets. Up to sixteen conversations and sixteen single-response
fixtures share the 64 KiB tool configuration limit. When conversations are set and
`fixtures` is empty, the built-in weather check is skipped. Legacy forced-choice
diagnostics apply only to single-response fixtures without explicit choice rules.

The message linkage follows the [OpenAI function-calling conversation contract](https://developers.openai.com/api/docs/guides/function-calling).
