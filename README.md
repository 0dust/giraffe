# Giraffe

Run a built-in test suite against an existing OpenAI-compatible LLM endpoint.
See which requests failed, where output stalled, whether known answers changed,
and what regressed against a saved run. Results stay in local HTML and JSON files.

Licensed under [Apache-2.0](LICENSE). See [CONTRIBUTING.md](CONTRIBUTING.md)
for the branch and approval policy.

## Install

Python 3.11+ on macOS or Linux:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Use a model already installed on your server. No test authoring, model download,
cloud account, database, or hosted judge is needed. API origins, `/v1` bases, and
complete `/chat/completions` URLs are accepted.

## Ollama

Connect to a running Ollama server using a model you already have installed.
Copy [examples/local.yaml](examples/local.yaml) and edit these settings:

| Setting | What to enter |
| --- | --- |
| `targets[0].url` | `http://127.0.0.1:11434/v1` for a local server; use its reachable address if remote. |
| `targets[0].model` | A model name from `ollama list`, including its tag, such as `llama3.2:3b`. |
| `context_limit` | The context length configured in Ollama for this model. |

```sh
cp examples/local.yaml my-ollama.yaml
# Edit my-ollama.yaml, then open the prefilled form:
giraffe ui --config my-ollama.yaml

# Or run the same configuration from the CLI:
giraffe run --config my-ollama.yaml --output runs/ollama-first
```

Review the traffic budgets, acceptance limits and enabled checks before running.

## vLLM

Start your vLLM server separately, then connect Giraffe to its
[Chat Completions API](https://docs.vllm.ai/en/latest/serving/online_serving/).
Use a text-generation model with a working chat template.

Copy [examples/vllm.yaml](examples/vllm.yaml) and edit these settings:

| Setting | What to enter |
| --- | --- |
| `targets[0].url` | `http://127.0.0.1:8000/v1` for a local server; use the reachable host/port for a remote server. |
| `targets[0].model` | An exact model ID from the server's `/v1/models` response, including any `--served-model-name` alias. |
| `context_limit` | The context length configured on the server with `--max-model-len`. |
| `targets[0].api_key_env` | If authentication is enabled, the name of an environment variable containing the API key. |
| Traffic and `limits` | Review the request/time budgets and set acceptance limits for your service. The example values are not hardware recommendations. |

```sh
cp examples/vllm.yaml my-vllm.yaml
# Edit my-vllm.yaml, then open the prefilled form:
giraffe ui --config my-vllm.yaml

# Or run the same configuration from the CLI:
giraffe run --config my-vllm.yaml --output runs/vllm-first
```

The example enables the ten standard checks; JSON and GPU checks remain optional.
A wrong answer can fail a load check even when requests complete successfully.
Inspect the failed responses before treating the result as a vLLM server fault.

## Run and compare

Open **http://127.0.0.1:8765** and select **New run**. Follow **Connect → Choose
checks → Review & run**, then select **Start run**. The web interface uses the same
runner and scores as the CLI. It lets
you follow live activity, stop and keep partial results, inspect failed checks and
individual responses, save a named baseline, and compare a later run. Existing
CLI reports in `runs/` appear in the history. A finished run can still fail its
acceptance checks; those are displayed separately.

`giraffe ui` starts on loopback only and never starts inference automatically.
Use `--port 8766` to select another port or `--runs-dir /path/to/runs` for another
report directory. Paste your endpoint in **Connect** and select **Test connection**.
Giraffe discovers available models and selects one automatically if only one is
listed. If several are available, choose one; if listing is unsupported, enter its
name. An explicitly entered model skips discovery. The test sends at most one
short completion (32 output tokens, 15 seconds total), separately from a full run.
It checks that a usable reply returns, not answer accuracy or load capacity. No run
report is created. Editing the endpoint, model or authentication clears that result.
You can continue with a manually specified model without a successful probe.

**API key (optional)** accepts the name of an environment variable on the machine
running Giraffe, not the secret key. Open local endpoints need no key. The connection
API also honors configured header variables, TLS trust and proxy settings.
`POST /api/targets/test-connection` accepts `url`, optional `model`, `api_key_env`,
`headers_env`, `ca_bundle`, `proxy` and `request_options`; it returns `connected`
or `needs_model`. `POST /api/targets/models` remains a metadata-only lookup.

**Choose checks** opens on your selected checks. Use **Add checks** to search
the full catalog by purpose. The initial selection is endpoint response and
answer correctness. Each check has an **i**
button for details and a **Settings** button for its controls. Load settings belong to their load
check; answer checks and rate tests can use your own questions. Shared request
and time limits apply across the run. The final review shows planned requests,
stopping limits and separate evaluator calls before you start. Plans update as
you edit; adaptive tests are labeled as bounded and built-in random arrival counts
as estimates. Increasing the total request limit always requires an explicit
action. **Start run** becomes available only after the current request plan is ready.

The optional `--config` prefills the form and preserves the supplied test selection.
In **Review & run**, expand **Save this setup or use advanced options** to save
a setup, copy a terminal command, or open the **Advanced editor**. Its JSON view
supports the same multi-model/replica settings as the CLI. **Apply changes** validates
JSON before updating the form; unapplied edits are retained and must be applied or
discarded before running, exporting, or copying a command. **Load saved setup** loads
a local JSON file, and **Save setup to file** downloads the current configuration draft.
Applying, importing, and exporting allow blank models and no tests selected while
you are setting up a run. Running and copying a CLI command require a model for
every target and at least one selected test.
**Copy terminal command** carries the configuration and selected baseline into a command
you can run from a terminal. It does not start traffic. All UI assets are bundled
locally, with no CDN or frontend build step. Closing the browser does not stop a run;
use **Stop run**. One run is active at a time to avoid competing benchmark traffic.

The CLI prints its limits before sending traffic: maximum requests, concurrency,
total duration, per-request deadline, and output tokens. Ctrl-C stops new requests
and saves partial results. Normal runs never restart the service.

Open `report.html` in your run's output directory. `report.json` contains the same
measurements and reproduction settings. A failed or interrupted run still produces
a report.

To compare runs, substitute your edited Ollama or vLLM configuration for
`my-config.yaml` below:

```sh
giraffe run --config my-config.yaml --output runs/before
giraffe baseline save runs/before/report.json baselines/local.json

# After changing your deployment:
giraffe run --config my-config.yaml --baseline baselines/local.json \
  --output runs/after
```

Saving a baseline is explicit. Subsequent runs never replace it; replacement
requires `baseline save ... --replace`. A baseline may contain failures: comparison
reports change, while absolute checks still evaluate the current run.

## Additional workloads and deployment evidence

Opt-in arrivals, shared prefixes, input/output buckets, mixed traffic, fixed/live
conversations, repeated-request consistency and parsed tool-calling checks use the
same global budgets. Deployment snapshots and bounded serving-metrics collection
keep configuration changes, provenance, missing data and performance evidence
alongside an explicit baseline. The CLI, local UI, HTML and JSON expose the same
configuration and evidence.
Prefix acceptance requires substantial reported cached-token reuse. Size cells use
refilling concurrency pools and qualify actual token lengths and per-sample overlap;
cell windows stop dispatch without cancelling requests. Text and optional logprob
consistency compare concurrent levels with sequential controls; strict mode fails
cross-level answer or comparable token divergence. Sessions recall unique seeded
facts. Mixed-load acceptance requires exercised long competitors, a known light
passage of at least 64 tokens, and an explicit slowdown ratio. Missing evidence
leaves these checks inconclusive; see [workload evidence](docs/workloads.md).


Read [the workload and telemetry guide](docs/workloads.md) and use
[the expanded example](examples/workloads.yaml). Export a sanitized reproduction
snapshot with `giraffe export runs/after/report.json`, or inspect deployment,
telemetry and comparison evidence with `giraffe inspect runs/after/report.json`.
New workloads stay disabled unless configured; missing internal telemetry stays
unverified.

## What runs

| Check | Built-in workload / measurement |
| --- | --- |
| Access | Ordinary inference through the selected DNS/TLS/auth/network path |
| Serving | Ordinary traffic: HTTP status, completion envelope, nonempty answer, stream finish |
| First output | Ordinary traffic: first useful answer separately from reasoning and initial requests |
| Generation | Longer-output samples, generation pace after first output, response time and stream gaps |
| Capacity | Independent mixed arrivals; highest tested rate with every lower tested rate accepted |
| Fairness | Interleaved matched short streams; verified long-prompt overlap, decoding slowdown and answer gaps |
| Context | Injected known facts at different lengths and positions |
| Correctness | Known answers, with observed concurrent overlap required when concurrency exceeds one |
| JSON (opt-in) | Required fields, types, enums and expected content; observed overlap qualifies concurrency |
| Limits/cancellation | Output cap, controlled server stop enforcement, mid-stream cancellation, deadlines and follow-ups |
| Recovery | Refilled heavy worker pool, then matched pre/post light probes with configured acceptance limits |
| GPU (opt-in) | Per-observed-device metric coverage, freshness, XID and thermal/power throttle evidence |

Each result is `pass`, `fail`, `inconclusive`, `skipped`, or `blocked`. A required
inconclusive check prevents an overall pass. Missing acceptance limits produce
measurements without certifying performance. Deliberate stress traffic is judged by its
own checks, independently of ordinary access, serving, first-output and generation
verdicts. Capacity and other deliberate overload errors do not consume the global
`stop_after_errors` allowance. Connection/TLS failures and HTTP 401/403 establish
access failures; a pre-header timeout remains a timeout. Small or incomparable samples cannot
establish a regression. Latency percentiles always accompany request/error counts.

The suite checks a small, versioned set of observable serving behaviors. It is not
a general model-quality benchmark. Context lengths depend on the server tokenizer;
the report distinguishes measured token usage from character counts. A service URL
does not establish which replicas or GPUs were reached. Client disconnect does not
prove backend work was reclaimed. Buffered responses can prevent a valid fairness
measurement. The report preserves these limits.

Generation pace is a client-observed estimate: `(output tokens - 1) / seconds from
first useful answer chunk to last answer chunk`. Prompt waiting and trailing
usage/finish messages are excluded. Stream gaps likewise measure only intervals
between answer chunks, excluding the wait after the final answer. This follows the first-token exclusion used
by [vLLM's per-request TPOT](https://docs.vllm.ai/en/latest/design/metrics/), using
the last observed answer instead of protocol cleanup as the endpoint. A chunk may
contain multiple tokens, and network buffering can distort arrival times; this
is not an exact server decoding measurement. Longer-output fixtures reduce the
influence of small answers. Their purpose is timing, not semantic scoring.

`limits.min_output_tokens_per_second` sets the minimum **generation** rate for
measurable requests. Single-token answers have no remaining generation interval;
they do not establish a rate. Non-streamed, single-answer-chunk, incomplete or
missing-usage responses also cannot establish it. A rate requirement without
sufficient measurable evidence stays inconclusive. First-output and total-time
limits still apply independently.

End-to-end output rate (`output tokens / total request seconds`) is retained as a
separate measurement. Schema 2 reports store the additional answer timing;
schema 1 reports remain readable and are not compared against the new definition.

## Configure targets and traffic

All settings are optional except at least one target URL and model. Multiple targets
share one global request/time/concurrency budget. Targets normally run sequentially;
`overlap_models: true` tests competing models within that same budget.

```yaml
targets:
  - name: service
    url: https://inference.example.internal/v1
    model: my-model
    route: gateway
    api_key_env: INFERENCE_API_KEY
    identity:
      runtime: vllm
      image: candidate-build
      quantization: AWQ
  - name: replica-a
    url: http://replica-a.internal:8000/v1
    model: my-model
    route: replica
    parent: service

concurrency: 4
max_requests: 160
max_duration_seconds: 300
request_timeout_seconds: 30
max_output_tokens: 128
context_limit: 4096
samples: 6
sustained_seconds: 5
request_options: {temperature: 0}
structured_json: true
retention: failures
limits:
  first_output_ms: 1000
  latency_ms: 10000
  stream_gap_ms: 1000
  fairness_max_ratio: 2
```

These are example limits, not recommendations for a model or GPU. Declared runtime,
image, topology, and quantization are recorded separately from observed identity.
Give each model/route a distinct target name. Include the service URL when supplying
replica targets; unreachable replicas remain visible in the report.

Use environment variables for credentials. `headers_env` maps header names to
environment-variable names. HTTPX honors `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY`;
`ca_bundle` supplies a private CA file. Nothing disables TLS verification.

`--nonstream` exercises nonstreaming Chat Completions. `--checks serving,correctness`
runs a selected subset. **Add checks** in the UI exposes all 19 checks
with a selection checkbox and **Settings** action. Specialist settings stay in
advanced sections. Settings start collapsed, including saved custom settings.
Opening settings does not change them. Edits are kept automatically, and only one
editor is open at a time. Defaults inherit the run configuration;
custom settings change only the selected test. Deselecting a test does not run its
workload. Shared probes needed for a selected timing/fairness check remain visible
in the recorded request scenarios.

An explicit `checks` list is authoritative: selecting `json` or `gpu` enables it
without a second flag. Without a list, the ten standard checks are selected, and
legacy `structured_json` / `metrics` flags opt into their additional checks.
`--json` and `--metrics` extend a saved selection unless `--checks` is also given.
When reusing an older manifest whose `checks` list included disabled optional
tests, remove `json` or `gpu` from that list to keep them deselected. Saved custom
fixtures are used only when correctness is selected.
`--metrics` reads the
target's optional `metrics_url`. GPU evidence requires fresh source timestamps or
values changing across scrapes, plus every required family per observed device.
XID errors and active thermal/power throttle reasons fail. Missing evidence is
inconclusive; this is not device health certification. No node agents are installed.

### Use your own requests under traffic

In **New run → Add questions & expected answers**, choose **My questions**. Add examples in the editor,
try the sample, or import CSV/JSON. For tabular files, select the input and expected
answer columns, preview the mapping, then choose **Use dataset**. Invalid rows are
shown with their row numbers; the current dataset stays unchanged until you apply
a valid import. Canonical request-pack JSON preserves conversations and mixed checks.

Edit any example's messages, expected answer and answer check. Select rows to change
their checks together using the controls below the table. Select correctness, capacity,
or both in **Tests & traffic**. **Advanced dataset settings → Request planning details**
shows effective requests, case allocation at each rate and budget gaps. **Try one** sends one selected example to one target using its effective request
settings and configured judges, without traffic stages or restart commands. Its
optional answer-retention checkbox affects only that trial's saved report.

The same operations are available to agents:

```sh
# Inspect columns without sending traffic; add mappings to produce request_pack.
giraffe import-dataset --file examples.csv
giraffe import-dataset --file examples.csv --input-field question \
  --expected-field answer --scorer judge
# Run one case from a complete exported configuration.
giraffe try-example --config my-config.json --case example-1 --target local \
  --output runs/one-example --output-format json
```

`import-dataset` returns JSON with `request_pack`, preview rows, mapping suggestions
and structured errors. Put the returned pack in your configuration's `request_pack`
field and set `request_source` to `request_pack`. The API equivalents are
`POST /api/datasets/preview` and `POST /api/runs/try-example`; `giraffe describe`
lists their fields. A successful single-example trial checks setup and that example,
not deployment capacity.

Start with [examples/request-pack.json](examples/request-pack.json):

```sh
giraffe run --config my-config.yaml --request-pack examples/request-pack.json \
  --checks correctness,capacity --latency-ms 1000 --output runs/pack-before
giraffe baseline save runs/pack-before/report.json baselines/pack.json
# Saved configuration embeds the pack; the original file is no longer needed.
giraffe preview --config exported-config.json
giraffe run --manifest runs/pack-before/report.json --baseline baselines/pack.json \
  --output runs/pack-after
```

Each case has a unique `fixture_id`, text `messages`, a positive `weight` (default 1),
and an explicit `scorer`: `exact`, `contains`, `json`, `judge`, or `none`. Exact and contains
need an `expected` string; JSON needs a local `schema` and can include expected JSON.
Exact compares the whole answer after normalizing whitespace. Contains checks for
case-sensitive text with alphanumeric boundaries: expected `4` matches `(4)` but
not `14` or `40`. It does not validate the whole answer. Pack and dataset previews
warn when an exact/contains expected value appears verbatim in that case's own
messages: copying that value can pass without demonstrating the intended task.
This is a warning; extraction and copy tasks can intentionally include the answer.
JSON validates the schema and compares supplied expected values structurally,
treating `3` and `3.0` as equal while keeping booleans distinct from numbers,
including within arrays and objects. Schema validation alone checks structure.
None of these establishes general chatbot quality.

Semantic scoring and judging strip leading `<think>`, `<thinking>`, or `<reasoning>`
blocks, case-insensitively, and save `reasoning_stripped` on the request record.
Raw response evidence follows the configured retention policy. An unclosed block
or a block with no final answer remains unscored: `reasoning without final answer`.
When `finish_reason` is `length`, a deterministic answer that does not satisfy its
scorer remains unscored: `truncated before a complete answer`. A full matching
exact/contains answer or complete valid matching JSON can still be scored.
Judges never receive truncated answers. Missing scores leave correctness coverage
inconclusive rather than counting incomplete answers as wrong.

Built-in known-answer correctness, JSON, context, short and traffic fixtures
request `temperature: 0` unless `request_options` explicitly supplies a temperature.
This requests deterministic decoding; server support is not independently verified.
Context fact positions are separated by at least 32 characters within each input
size. Small context budgets produce fewer positions; per-request workload metadata
`position_coverage` lists the exercised positions and marks reduced coverage.

Giraffe sends each case once quietly. With capacity selected, the same weighted
pack supplies continuous arrivals; every case's loaded answers also enter its
correctness verdict against `min_correctness`. Each finite stage is allocated before
dispatch; dropped requests are not replaced. Without capacity, correctness runs a
second pass at global concurrency, sending at least that many requests and covering
every case. Concurrency above one requires observed overlap for a loaded verdict.
Per-check correctness `samples` or `concurrency` overrides are rejected in pack mode;
global `samples` does not choose case count. Legacy request previews omit the extra
concurrent pass when capacity is disabled, so allow for its additional request budget.
Every scored case needs quiet and loaded coverage for a full-pack rate claim. An
unscored case prevents that claim, though throughput and latency remain available.
A quiet failure never removes a case from load. Requests repeat and may benefit
caches, so this measures a warmed workload.

**Requests and expected answers are saved with the run.** Response retention only
controls generated outputs. Imports are limited to 1 MiB and 1,000 cases. Export,
Copy CLI, stdin configuration, and `POST /api/config/preview` use the same inline
`request_source: "request_pack"` / `request_pack` contract. Legacy `--fixtures`
continues to add correctness cases only and cannot be combined with a selected pack.

### Evaluate golden answers with AI judges

Use `scorer: "judge"` with an `expected` reference answer when correct answers may
use different words. An optional `rubric` adds case-specific requirements. The
default policy accepts paraphrases but rejects wrong facts, contradictions and
missing essential details. See [examples/request-pack-judged.json](examples/request-pack-judged.json).

In **New run → Add questions & expected answers**, add or import examples with expected answers. Choose
**AI evaluator** in an example's editor or apply it to selected rows,
then choose **Configure evaluator** to enter the evaluator endpoint and model.
For CLI/API runs, add the same settings to your run configuration:

```json
{
  "judging": {
    "judges": [
      {
        "name": "reviewer",
        "url": "http://localhost:9000/v1",
        "model": "your-judge-model",
        "api_key_env": "JUDGE_API_KEY"
      }
    ],
    "concurrency": 2,
    "request_timeout_seconds": 30,
    "max_duration_seconds": 300,
    "max_output_tokens": 256
  }
}
```

Set the actual served model ID and omit `api_key_env` for an endpoint without
authentication. Add further named endpoints to `judges` for a panel, up to eight.
One judge decides directly; a complete panel uses strict majority. Two disagreeing
judges tie. A tie, timeout, malformed verdict or unavailable judge leaves the answer
unscored and prevents accepted capacity; missing votes never reduce the panel size.
Each judge's vote and short explanation appear in request evidence.

Giraffe judges every valid completed final answer from a judged case, excluding
truncated and reasoning-only outputs and including repeated
quiet and loaded answers, **after all measured traffic and telemetry finish**.
Judge calls use separate limits and never enter deployment latency or traffic counts.
Preview shows a call upper bound; no model is contacted during preview. There are no
retries, sampling or cached verdicts. Stopping the run stops evaluation as well.
The configured judges receive the original messages, reference answer and generated
answer. Judge explanations follow the same response-retention policy as outputs.

Run with `giraffe run --config my-config.json --request-pack examples/request-pack-judged.json`.
Export, Copy CLI and stdin carry the complete configuration. Changing active references,
rubrics or judge settings makes an upgrade comparison inconclusive. Pin judge model
versions where possible: a mutable model alias can change without a configuration change.

Reference-based judging is an established [evaluation method](https://docs.langchain.com/langsmith/evaluation-types),
but judges can be wrong. Check your chosen judge against a small human-reviewed set of
correct paraphrases and deliberately wrong answers before relying on it. Panel agreement
does not prove correctness or general model quality.

### Arrival traffic and per-test settings

The capacity test sends arrivals continuously during each rate stage, independently
of previous completions. `steady` spaces arrivals evenly; `poisson` produces seeded
uneven arrivals. Each arrival samples the configured mix of short answers, long
input retrieval, and longer exact-copy output. Every built-in traffic request has
a known-answer scorer. The defaults are a small smoke workload; choose rates,
lengths, duration and service limits that represent your deployment.

```yaml
checks: [capacity, correctness]
concurrency: 32           # Global in-flight ceiling, shared by all targets
max_requests: 600         # Includes traffic arrivals dropped by the generator
max_duration_seconds: 120
traffic:
  rates: [1, 2, 4]        # Requests/second; one rate also works
  duration_seconds: 20    # Each stage, followed by bounded drain
  arrival: poisson       # steady or poisson
  seed: 42
  max_in_flight: 32       # Optional; inherits global concurrency when omitted
  mix: {short: 0.6, long_input: 0.2, long_output: 0.2}
  long_input_chars: 4096  # Characters, not a claim about tokenizer counts
  long_output_words: 32
  drain_timeout_seconds: 30
  scheduler_lag_tolerance_ms: 100
test_options:
  capacity:
    limits: {first_output_ms: 1500, latency_ms: 10000, min_correctness: 1}
  correctness:
    samples: 12
    concurrency: 4
```

The traffic editor exposes this profile before the run. Each other test exposes
only applicable settings, such as context length, sample count, cancellation
answer-chunk count, recovery duration, telemetry freshness, or acceptance limits. **Reset to defaults**
removes that test's overrides. The JSON view, saved manifests and the CLI use the
same validated configuration. Per-test concurrency, output and deadline settings
remain bounded by the run's global ceilings. Overrides above those ceilings are
rejected for selected tests; disabled customization drafts are retained.
Unsupported options are rejected.

Reports show the planned and sent counts, failures, local drops, latency, dispatch
delay, and time-window trends for each rate. Giraffe does not queue traffic locally:
if an arrival finds the in-flight ceiling occupied, it records a local drop.
Excessively late scheduler arrivals are also counted instead of sent in a catch-up
burst. Neither case establishes the deployment's capacity. Client dispatch delay
and time until the first answer are observable; server-internal queue time is not
inferred from them.

**Goodput** counts valid, correct answers meeting every configured per-request
timing limit, divided by the stage's arrival duration. This is cohort goodput:
answers finishing during bounded drain belong to their original arrival stage.
The good fraction uses all planned arrivals, including unsent ones. A stage needs
sufficient samples and a first-output or total-response latency limit to qualify;
stream-gap or generation-speed limits alone do not bound user waiting time.

The result is the **highest acceptable tested arrival rate** for which every
tested rate at or below it was accepted, not a discovered maximum. A failure at a
lower rate followed by a pass at a higher rate is flagged as non-monotonic and
does not raise this boundary. Baseline comparison matches the planned profile, seed, tests and acceptance
criteria, and reports lost previously acceptable rates. Dropped or failed requests
remain outcomes, even if they change the observed request mix. Incomplete or
generator-limited evidence cannot establish an exact capacity decrease. Older
reports remain readable; the suite-version change prevents comparing the former
concurrency sweep with this arrival-rate definition.

Response retention is `failures` by default. Use `all` to keep every response, or
`none` to discard all output/reasoning text after scoring. Aggregate metrics and
request statuses remain. Prompts are deterministic built-ins; optional custom
fixtures are a local JSON list of `RequestSpec` objects. Legacy `--fixtures` accepts
only `exact`, `contains`, `json`, and `none` scorers; AI judges require a request
pack, and tool scoring is reserved for built-in tool checks. Custom fixture files must
remain available to reproduce a run.

```sh
giraffe run --manifest runs/before/report.json --output runs/reproduced
```

Cold-start testing is a separate, explicit command. Configure an argv list on the
specific target, such as `restart_command: [docker, restart, my-test-service]`, then:

```sh
giraffe cold-start --config my-test-service.yaml --restart service --output runs/cold
```

The command invokes only that hook. It does not provision deployments or reboot
nodes. A missing hook blocks the cold-start run. After the hook completes, readiness
retries use `readiness_timeout_seconds` (default 180 seconds), bounded by the run
deadline and request budget. Retries back off from 0.1 to 2 seconds; readiness
failures do not consume `stop_after_errors`. Use an isolated test service when
measuring restarts or load.

## Offline and container use

After installing dependencies, the suite needs only your configured inference,
optional metrics, and optional AI judge endpoints. It never downloads fixtures or models.
For an offline machine, prepare wheels on a compatible online machine:

```sh
pip wheel . --wheel-dir wheelhouse
# Transfer wheelhouse, then on the offline machine:
pip install --no-index --find-links wheelhouse giraffe-check
```

```sh
docker build -t giraffe .
docker run --rm -v "$PWD:/work" giraffe run \
  --url http://host.docker.internal:11434/v1 --model qwen2.5:7b-instruct \
  --output runs/container
```

`host.docker.internal` is provided by Docker Desktop. On Linux, supply a reachable
service address or the appropriate Docker host mapping. Pass credentials using
`docker run -e INFERENCE_API_KEY ...` when configured.

Exit codes: `0` pass, `1` observed failure, `2` inconclusive/blocked/skipped or invalid
configuration. Use the JSON for per-check decisions.

## Agent and automation use

Discover supported tests and the complete configuration schema, validate a file
without sending traffic, then execute it with structured output:

```sh
giraffe describe
giraffe validate --config deployment.json --output-format json
giraffe run --config deployment.json --output-format json
```

`--config -` reads JSON or YAML from standard input. `--output-format json` emits
one JSON object on stdout; progress remains on stderr. Run results include
`outcome`, `exit_code`, `run_id`, and absolute `report_paths.json` and
`report_paths.html`. Errors include field locations and messages. The existing
`--json` flag still selects the structured-output test; it does not control the
CLI's output format. `cold-start` and `baseline save` also support structured output.

The local HTTP API provides the same configuration and runner contracts:

| Request | Purpose |
|---|---|
| `GET /api/capabilities` | Supported tests, defaults and full configuration schema |
| `POST /api/config/validate` | Validate and normalize a run configuration without traffic |
| `POST /api/config/draft` | Normalize an editable draft, allowing blank models and no selected tests; never starts traffic |
| `POST /api/targets/test-connection` | Discover or choose a model and send one bounded completion without saving a run |
| `POST /api/targets/models` | List advertised model IDs without inference |
| `POST /api/config/preview` | Preview the selected tests, request counts, sources and evaluator calls without sending traffic |
| `POST /api/runs` | Start a run with `config`, optional `baseline_id`, and explicit execution mode |
| `GET /api/runs/{id}` | Inspect state, progress and the completed report |
| `POST /api/runs/{id}/cancel` | Stop traffic and retain partial results |
| `GET /api/runs/{id}/config.json` | Download the completed run's recorded configuration |
| `GET /api/runs/{id}/report.json` | Retrieve structured measurements and acceptance results |
| `GET /api/baselines`, `POST /api/baselines` | List references or save an explicitly chosen run |
| `POST /api/config/cli` | Generate the UI's equivalent shell command without running it |

Send JSON request bodies with `Content-Type: application/json`. The API schema is
available at `/openapi.json`. Configuration validation checks values and their
relationships; it does not establish endpoint reachability, credential validity or
deployment health. Those require a run. Generated commands target POSIX shells and
carry the selected baseline when present. Credential environment variables and any
custom fixture or certificate files must exist on the machine executing the command.

## Development

For the recorded dependency versions, use `uv sync --extra dev --frozen` with the
included `uv.lock`. A normal pip installation works without uv.

```sh
pip install -e '.[dev]'
pytest -q
ruff check src tests
```

Integration tests use a real local HTTP server to inject empty/truncated successful
responses, wrong answers, timeouts, and timing regressions. They require loopback
network access. This MVP is a CLI, local web interface and local reports; it has no
accounts, control plane, inline gateway, provisioning, automatic tuning, promotion,
or rollback.

## Tested compatibility

The expanded workloads and deployment/telemetry capture have a separate
[October 8 validation record](docs/validation/2026-10-08.md), with runnable
configurations, real Ollama and vLLM-Metal results, endpoint failures and coverage gaps.

Verified locally on October 4, 2026 with Python 3.12 on macOS:

The model runs below predate the arrival-rate capacity test. The new scheduler,
selectable controls and upgrade comparison have automated loopback HTTP coverage;
those results do not constitute live GPU qualification of the new workload.

| Endpoint | Model | Verified behavior |
| --- | --- | --- |
| Ollama 0.31.1, OpenAI-compatible `/v1` | `qwen2.5:7b-instruct` | Streaming/nonstreaming, JSON, concurrency, mixed traffic, context, caps, disconnect/deadline and recovery |
| Ollama, OpenAI-compatible `/v1` | `llama3.2:3b` | All ten standard checks passed: 141 requests, concurrency 2, 4,096-token context. JSON/GPU checks were not selected. |
| vLLM 0.30.0 + vLLM-Metal 0.30.0 on Apple M1 | `mlx-community/Llama-3.2-1B-Instruct-4bit` | All ten standard checks exercised: 4 passed, 4 failed on answer checks, 2 timing checks inconclusive. 159 requests, concurrency 2, 1,024-token context. |
| Deterministic loopback HTTP fixture | Synthetic responses | Empty/truncated HTTP 200 rejection, wrong-answer and latency regression detection, budgets, report persistence and baseline preservation |

The vLLM-Metal run's copy refusal and incorrect stop-test answer also reproduced
with the same checkpoint directly through MLX, without Giraffe or vLLM. The two
timing checks were inconclusive because Giraffe recorded a scheduling pause.
These results do not isolate an engine difference from Ollama: the models differ.
Linux/CUDA vLLM has not been tested live here.

The earlier Qwen/Ollama run observed a known-answer failure in one near-limit context
fixture and correctly reported `fail`; compatibility does not mean the deployment
passed every check. Real CLI interruption produced an inconclusive partial report.
The web UI was checked at desktop, narrow-window and mobile widths. A UI-started
Ollama run saved all 119 requests with the same context failure and a separate,
inconclusive baseline comparison. Stopping another run saved 15 requests as a
partial report. Refresh continuity, form preservation, baseline saving and
comparison filtering were verified in the browser.

A wheel was built and installed offline from cached dependencies. Other runtimes,
real multi-replica deployments, GPU exporters, Linux, and the Docker image still
need live qualification; their supported paths have local automated coverage where
applicable. Exported HTML report rendering was not separately visually verified.

For optional token probability comparisons under concurrency, see the
[logprob consistency guide](docs/workloads.md#token-probability-consistency) and
[runnable vLLM configuration](examples/logprob-consistency.yaml).
