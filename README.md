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

Open **http://127.0.0.1:8765**, select **New run**, review the settings, and select
**Run suite**. The web interface uses the same runner and scores as the CLI. It lets
you follow live activity, stop and keep partial results, inspect failed checks and
individual responses, save a named baseline, and compare a later run. Existing
CLI reports in `runs/` appear in the history. A finished run can still fail its
acceptance checks; those are displayed separately.

`giraffe ui` starts on loopback only and never starts inference automatically.
Use `--port 8766` to select another port or `--runs-dir /path/to/runs` for another
report directory. The optional `--config` prefills the form. **Form** and **JSON** at
the top of **New run** are two views of the same configuration. The JSON view
supports the same multi-model/replica settings as the CLI. **Apply changes** validates
JSON before updating the form; unapplied edits are retained and must be applied or
discarded before running, exporting, or copying a command. **Import config** loads
a local JSON file, and **Export config** downloads the current configuration draft.
Applying, importing, and exporting allow blank models and no tests selected while
you are setting up a run. Running and copying a CLI command require a model for
every target and at least one selected test.
**Copy CLI command** carries the configuration and selected baseline into a command
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

Read [the workload and telemetry guide](docs/workloads.md) and use
[the expanded example](examples/workloads.yaml). Export a sanitized reproduction
snapshot with `giraffe export runs/after/report.json`, or inspect deployment,
telemetry and comparison evidence with `giraffe inspect runs/after/report.json`.
New workloads stay disabled unless configured; missing internal telemetry stays
unverified.

## What runs

| Check | Built-in workload / measurement |
| --- | --- |
| Access | Real inference request through the selected DNS/TLS/auth/network path |
| Serving | HTTP status, completion envelope, nonempty answer, stream finish |
| First output | First useful answer separately from reasoning and initial requests |
| Generation | Longer-output samples, generation pace after first output, response time and stream gaps |
| Capacity | Independently arriving mixed requests at chosen rates; correct, timely answers and highest acceptable tested rate |
| Fairness | Long prompts added while a short response is streaming |
| Context | Injected known facts at different lengths and positions |
| Correctness | Extraction, arithmetic, and classification, including under load |
| JSON (opt-in) | Required fields, types, enums, expected content under concurrency |
| Limits/cancellation | Output cap, stop sequence, deadline/disconnect, recovery probes |
| Recovery | Repeated bounded traffic followed by light-load probes |
| GPU (opt-in) | Existing Prometheus telemetry when available |

Each result is `pass`, `fail`, `inconclusive`, `skipped`, or `blocked`. A required
inconclusive check prevents an overall pass. Missing acceptance limits produce
measurements without certifying performance. Small or incomparable samples cannot
establish a regression. Latency percentiles always accompany request/error counts.

The suite checks a small, versioned set of observable serving behaviors. It is not
a general model-quality benchmark. Context lengths depend on the server tokenizer;
the report distinguishes measured token usage from character counts. A service URL
does not establish which replicas or GPUs were reached. Client disconnect does not
prove backend work was reclaimed. Buffered responses can prevent a valid fairness
measurement. The report preserves these limits.

Generation pace is a client-observed estimate: `(output tokens - 1) / seconds from
first useful answer chunk to last answer chunk`. Prompt waiting and trailing
usage/finish messages are excluded. This follows the first-token exclusion used
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
runs a selected subset. The UI exposes every check with a selection checkbox and
**Configure** action. Settings start collapsed, including saved custom settings.
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
target's optional `metrics_url`; missing or stale samples cannot produce a healthy
GPU verdict. No node agents are installed.

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
delay, recovery duration, telemetry freshness, or acceptance limits. **Reset to defaults**
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

The result is the **highest acceptable tested arrival rate**, not a discovered
maximum. Baseline comparison matches the planned profile, seed, tests and acceptance
criteria, and reports lost previously acceptable rates. Dropped or failed requests
remain outcomes, even if they change the observed request mix. Incomplete or
generator-limited evidence cannot establish an exact capacity decrease. Older
reports remain readable; the suite-version change prevents comparing the former
concurrency sweep with this arrival-rate definition.

Response retention is `failures` by default. Use `all` to keep every response, or
`none` to discard all output/reasoning text after scoring. Aggregate metrics and
request statuses remain. Prompts are deterministic built-ins; optional custom
fixtures are a local JSON list of `RequestSpec` objects. Custom fixture files must
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
nodes. A missing hook blocks the cold-start run. Use an isolated test service when
measuring restarts or load.

## Offline and container use

After installing dependencies, the suite needs only your configured inference and
optional metrics endpoints. It never downloads fixtures or calls an external judge.
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
