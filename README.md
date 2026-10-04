# Giraffe

Run a built-in test suite against an existing OpenAI-compatible LLM endpoint.
See which requests failed, where output stalled, whether known answers changed,
and what regressed against a saved run. Results stay in local HTML and JSON files.

## Run

Python 3.11+ on macOS or Linux:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e .

giraffe run --url http://127.0.0.1:11434/v1 --model qwen2.5:7b-instruct \
  --output runs/first
```

Use a model already installed on your server. No test authoring, model download,
cloud account, database, or hosted judge is needed. API origins, `/v1` bases, and
complete `/chat/completions` URLs are accepted.

The CLI prints its limits before sending traffic: maximum requests, concurrency,
total duration, per-request deadline, and output tokens. Ctrl-C stops new requests
and saves partial results. Normal runs never restart the service.

Open `runs/first/report.html`. `report.json` contains the same measurements and
reproduction settings. A failed or interrupted run still produces a report.

To set your service's acceptance limits, use [examples/local.yaml](examples/local.yaml):

```sh
giraffe run --config examples/local.yaml --output runs/before
giraffe baseline save runs/before/report.json baselines/local.json

# After changing your deployment:
giraffe run --config examples/local.yaml --baseline baselines/local.json \
  --output runs/after
```

Saving a baseline is explicit. Subsequent runs never replace it; replacement
requires `baseline save ... --replace`. A baseline may contain failures: comparison
reports change, while absolute checks still evaluate the current run.

## What runs

| Check | Built-in workload / measurement |
| --- | --- |
| Access | Real inference request through the selected DNS/TLS/auth/network path |
| Serving | HTTP status, completion envelope, nonempty answer, stream finish |
| First output | First useful answer separately from reasoning and initial requests |
| Generation | End-to-end latency, visible stream gaps, actual output length/rate |
| Capacity | Bounded concurrency levels with valid completions and configured limits |
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
runs a selected subset. `--json` enables structured JSON. `--metrics` reads the
target's optional `metrics_url`; missing or stale samples cannot produce a healthy
GPU verdict. No node agents are installed.

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
network access. This MVP is a CLI and local report; it has no accounts, control
plane, inline gateway, provisioning, automatic tuning, promotion, or rollback.

## Tested compatibility

Verified locally on October 4, 2026 with Python 3.12 on macOS:

| Endpoint | Model | Verified behavior |
| --- | --- | --- |
| Ollama 0.31.1, OpenAI-compatible `/v1` | `qwen2.5:7b-instruct` | Streaming/nonstreaming, JSON, concurrency, mixed traffic, context, caps, disconnect/deadline and recovery |
| Deterministic loopback HTTP fixture | Synthetic responses | Empty/truncated HTTP 200 rejection, wrong-answer and latency regression detection, budgets, report persistence and baseline preservation |

The full Ollama run observed a known-answer failure in one near-limit context
fixture and correctly reported `fail`; compatibility does not mean the deployment
passed every check. Real CLI interruption produced an inconclusive partial report.
A wheel was built and installed offline from cached dependencies. Other runtimes,
real multi-replica deployments, GPU exporters, Linux, and the Docker image still
need live qualification; their supported paths have local automated coverage where
applicable. Browser rendering was not visually verified.
