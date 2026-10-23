# Giraffe

Run a built-in test suite against an existing OpenAI-compatible LLM endpoint.
See which requests failed, where output stalled, whether known answers changed,
and what regressed against a saved run. Results stay in local HTML and JSON files.

Licensed under [Apache-2.0](LICENSE). See [CONTRIBUTING.md](CONTRIBUTING.md)
for the branch and approval policy.

## Try it in 30 seconds

Use Python 3.11+ on macOS or Linux, with Ollama already running and
`llama3.2:3b` installed:

```sh
git clone https://github.com/0dust/giraffe.git && cd giraffe
python -m venv .venv
. .venv/bin/activate
pip install -e .
giraffe run --url http://127.0.0.1:11434/v1 --model llama3.2:3b --checks access,serving,correctness --concurrency 1
```

This command was measured against local Ollama with `llama3.2:3b`: **PASS,
13 suite requests, exit 0**. Runtime and results depend on your model and server.
It checks access, serving and known answers at concurrency 1. With `--concurrency 2`
or higher on default Ollama, correctness is **INCONCLUSIVE** because Ollama serves
one request at a time by default and the check requires observed scored overlap.
To test concurrent serving, configure `OLLAMA_NUM_PARALLEL` on the Ollama server
(or your engine's equivalent), restart it, and rerun with the desired concurrency.
Parallel serving needs additional memory; see the
[Ollama concurrency guidance](https://docs.ollama.com/faq#how-does-ollama-handle-concurrent-requests).

Use a model already installed on your server. No test authoring, model download,
cloud account, database, or hosted judge is needed. API origins, `/v1` bases, and
complete `/chat/completions` URLs are accepted. For a non-editable installation
from this checkout, use `pip install .`; the command is still `giraffe`.

## What your first run looks like

The CLI sends one bounded connection/model probe per target, then prints planning
warnings before starting the suite. Each probe allows at most 32 output tokens
and 15 seconds, uses your connection settings, and is
separate from the suite request count and budget. `--skip-preflight` skips these
probes. A preflight failure prints a remedy and exits as blocked without creating
a run directory or report.

The quick run above leaves the request-count and traffic-duration limits unset;
add `--max-requests N` and `--max-duration SECONDS` to bound them. It prints limits
and live progress, followed by output shaped like this (summaries and paths vary):

```text
Giraffe: 1 target(s), Stop after this many requests: Not set, Limit simultaneous requests: 1, Stop test traffic after: Not set, Request defaults: 300s timeout, 128 output tokens. Check and saved-request overrides apply. Ctrl-C saves a partial report.
Per-check request timeouts: access=300s, serving=300s, correctness=300s. Saved requests may override these.
...
  local / access: pass — ...
  local / serving: pass — ...
  local / correctness: pass — ...
JSON: /your/checkout/giraffe/runs/<timestamp>/report.json
Export HTML: giraffe report /your/checkout/giraffe/runs/<timestamp> --html
PASS: ... requests
```

**PASS** means the selected required checks have sufficient evidence and meet their
acceptance limits. **FAIL** means a check observed a failure. **INCONCLUSIVE** means
evidence is insufficient to decide, for example too few samples or no concurrent
overlap. Inconclusive check lines include a remedy describing what to change or
collect before rerunning. **BLOCKED** means the run or check could not proceed,
for example because preflight could not connect to the model.

| Exit code | Meaning |
| --- | --- |
| `0` | Pass |
| `1` | Fail |
| `2` | Inconclusive |
| `3` | Blocked (including preflight failure) |
| `4` | Usage or configuration error |

Runs save `runs/<timestamp>/report.json` relative to the working directory, or
in the directory supplied with `--output`. JSON includes measurements and
reproduction settings. Download HTML from the web results page, or generate it
without rerunning the suite with `giraffe report runs/<timestamp> --html`
(optionally `--output PATH`). Add `--html` to `giraffe run` to save HTML at completion.
Once the suite starts, failed or interrupted runs also save reports. Ctrl-C stops
new requests and saves partial results.

## Preparation, timeouts and automatic measurements

The first setup page has one editable **Token ID** box, defaulting to **Automatic**.
It resolves and loads the tokenizer on that page before allowing continuation to
Checks. If resolution fails, the page says that built-in `o200k_base` will be used;
enter a model name in the same box to retry, or choose `builtin` from its dropdown.
The fallback uses the same established encoding as AIPerf's `builtin` option,
not a character-count approximation. Its reference counts can differ from
the served model and exclude chat formatting. Preparation and request evidence
identify the tokenizer; only server-reported usage establishes actual input-size
coverage and measured token counts. Missing server usage remains unknown.

Automatic tries a cached tokenizer or Hugging Face exact/repository-suffix name
resolution. The first-page check sends no requests to the inference endpoint.
CLI automatic resolution can also use matching server metadata. For
example, `qwen2.5-7b` can resolve to `Qwen/Qwen2.5-7B`. Like AIPerf, multiple exact
suffix matches prefer the most downloaded repository; partial/fuzzy matches are
not guessed. If lookup or loading fails, Giraffe uses the built-in tokenizer.
Discovery has a five-second total budget; each tokenizer load/download has a
30-second deadline. Built-in fallback loading has its own 30-second deadline.

Encoding/tokenizer files download automatically **on the machine running Giraffe**
and are reused locally. First use needs internet access; offline runs need cached
files. The built-in cache defaults to `~/.cache/giraffe/tiktoken` (respects
`XDG_CACHE_HOME` and `TIKTOKEN_CACHE_DIR`); model-specific tokenizers use the normal
Hugging Face cache (`HF_HOME` is supported). Model weights and remote Python code
are never loaded. Hugging Face offline settings also disable built-in downloads.

For CLI configurations, `targets[].tokenizer` accepts `null` or `auto` (default),
`builtin`, or an optional explicit model ID/local directory. Explicit overrides that
cannot load produce a setup error; select `builtin` to use the built-in tokenizer. Successfully loaded
identities are remembered per endpoint and served model for automatic mode.

Prompt sizing then runs locally and offline, with a 10-second deadline per
preparation and a 20-second total per target. No inference requests are sent to
calibrate input length. When the tokenizer has a chat template, the local count
includes that template. Otherwise it counts message text and explicitly excludes
chat formatting. Actual server usage still determines measured input lengths.
Saved request packs and custom load fixtures do not need synthetic prompt sizing.
Preparation progress and elapsed time appear above the results.

New runs default to a **300-second request timeout**. Checks can override it;
load uses `load_test.request_timeout_seconds`, and saved timeout values remain
unchanged. **Send new requests for** controls the sending window. Already sent
requests retain their own deadlines after that window ends; there is no separate
finishing wait limit. User stop and run limits can terminate them sooner, with
the exact reason and effective deadline retained in request evidence.

Timing, token usage and supported runtime telemetry are collected automatically.
Preparation can send up to three one-token compatibility probes sharing one
`request_timeout_seconds` budget, bounded by the remaining run duration and
using only spare request budget. Only explicit optional-field rejections trigger
another probe; timeouts do not retry. These setup calls are separate from
the planned test requests; local input sizing still sends no inference calls.
Unsupported token boundaries or usage remain unavailable. Runtime metric discovery uses
supported endpoints; `metrics_url` and `host_metrics_url` can supply additional
exporters. These measurements do not require selecting the optional GPU health
check, which separately evaluates fresh GPU evidence against its criteria.

## Ollama

Connect to a running Ollama server using a model you already have installed.
Copy [examples/local.yaml](examples/local.yaml) and edit these settings:

| Setting | What to enter |
| --- | --- |
| `targets[0].name` | Required unique target label, such as `local`; used in results and comparisons. |
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

Saved run configurations use `config_version: 2`. The request, traffic-duration and simultaneous-request limits are optional; `null` means **Not set**. Older files with omitted limits load without those limits and show migration notes. `default_concurrency` keeps check parallelism separate from the simultaneous-request ceiling. The legacy `--concurrency N` flag sets both; use `--simultaneous-limit N` to set only the ceiling. Version-2 exports are rejected by older Giraffe versions because their configuration schema forbids unknown fields.

This broader example uses concurrency 2; configure server parallelism before
expecting concurrent checks to qualify. For a sequential first run, use the
three-check command above. The example selects the nine default checks plus JSON,
with a 400-request limit and a 300-second traffic limit. Load test uses explicit
concurrency levels `[1, 2]`; duration-based and adaptive work can exhaust the limits
and leave checks unfinished. Preview the plan before running. The context and
ordinary-request output caps do not resize Load test's token profile; review its
input/output targets against your server. Use
[concurrent-workloads.yaml](examples/concurrent-workloads.yaml) to customize those
workloads.

## vLLM

Start your vLLM server separately, then connect Giraffe to its
[Chat Completions API](https://docs.vllm.ai/en/latest/serving/online_serving/).
Use a text-generation model with a working chat template.

Copy [examples/vllm.yaml](examples/vllm.yaml) and edit these settings:

| Setting | What to enter |
| --- | --- |
| `targets[0].name` | Required unique target label, such as `vllm`; used in results and comparisons. |
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

The example enables the nine default checks; JSON and GPU checks remain optional.
It sets a 400-request limit, a 900-second traffic limit and explicit Load test
concurrency levels `[1, 2]`. Duration-based and adaptive work can exhaust those
limits. Preview the plan and review Load test's independent token profile against
your server; use [concurrent-workloads.yaml](examples/concurrent-workloads.yaml)
to customize those workloads.
A wrong answer can fail a load check even when requests complete successfully.
Inspect the failed responses before treating the result as a vLLM server fault.

## llama.cpp, TensorRT-LLM, LMDeploy and TGI

Start the engine separately and connect to its OpenAI-compatible **chat** API.
Use a text-generation model with a working chat template. All 15 checks can be
selected for any engine; a supported connection does not guarantee that its model
passes JSON, tool calling, exact-answer, timing, or concurrency checks.

| Engine | Example | Default local API base | Optional discovery |
| --- | --- | --- | --- |
| [llama.cpp server](https://github.com/ggml-org/llama.cpp/tree/master/tools/server) | [llamacpp.yaml](examples/llamacpp.yaml) | `http://127.0.0.1:8080/v1` | `llamacpp`: `/props`, build, context per slot, parallel slots, chat-template hash |
| [TensorRT-LLM](https://nvidia.github.io/TensorRT-LLM/commands/trtllm-serve.html) | [tensorrt-llm.yaml](examples/tensorrt-llm.yaml) | `http://127.0.0.1:8000/v1` | `tensorrt-llm`: `/version`; use `trtllm-serve`, not a raw Triton inference URL |
| [LMDeploy](https://lmdeploy.readthedocs.io/en/latest/llm/api_server.html) | [lmdeploy.yaml](examples/lmdeploy.yaml) | `http://127.0.0.1:23333/v1` | `lmdeploy`: matching `/v1/models` entry; version and resolved settings remain unknown |
| [Hugging Face TGI](https://huggingface.co/docs/text-generation-inference/reference/api_reference) | [tgi.yaml](examples/tgi.yaml) | `http://127.0.0.1:8080/v1` | `tgi`: `/info`, runtime/model revision and configured input/total-token limits |

Edit the example's model ID, context length, credentials and acceptance limits,
then run `giraffe run --config examples/llamacpp.yaml --output runs/llamacpp-first`,
or load that configuration with `giraffe ui --config examples/llamacpp.yaml`.
Authentication uses environment-variable references, as with the other engines.
Discovery is optional; an unavailable metadata endpoint does not disable chat tests.

The `llamacpp`, `lmdeploy`, and `tgi` metrics profiles recognize their documented
Prometheus names. llama.cpp needs `--metrics`; LMDeploy needs metrics enabled for
its chosen backend. Missing counters, cache evidence, and GPU exporter observations
stay unknown. TensorRT-LLM's native `/metrics` iteration JSON is not parsed as
Prometheus; configure a separate exporter for GPU observations. Native per-request
server cancellation verification is unavailable for these four engines: output
caps, stop sequences, client cancellation and follow-up probes still run, but
`backend_verification: required` remains inconclusive without a native adapter.

GPU-only engines need a compatible Linux host.

## Run and compare

Start the local UI with `giraffe ui`, open **http://127.0.0.1:8765** and select
**New run**. Follow **Connect → Checks → Launch**, then select **Launch**.
The header shows the suite version. The web interface uses the same
runner and scores as the CLI. It lets
you follow live activity, stop and keep partial results, inspect failed checks and
individual responses, save a named baseline, and compare a later run. Existing
CLI reports in `runs/` appear in the history. A finished run can still fail its
acceptance checks; those are displayed separately.

`giraffe ui` starts on loopback only and never starts inference automatically.
Its flags are:

| Flag | Purpose |
| --- | --- |
| `--port PORT` | Loopback HTTP port; default `8765`. |
| `--runs-dir PATH` | Report/history directory; default `runs` relative to the working directory. Created on the first run. |
| `--config FILE` | Prefill the form from YAML/JSON, including the file's selected checks. |

If the port is in use, Giraffe exits with code 4 and asks you to choose another
with `--port`; it does not switch ports automatically. For example:

```sh
giraffe ui --port 8766 --runs-dir runs/local --config my-ollama.yaml
```

Paste your endpoint in **Connect** and select **Test connection**.
Giraffe discovers available models and selects one automatically if only one is
listed. If several are available, choose one; if listing is unsupported, enter its
name. An explicitly entered model skips discovery. The test sends at most one
short completion (32 output tokens, 15 seconds total), separately from a full run.
It checks that a usable reply returns, not answer accuracy or load capacity. No run
report is created. Editing the endpoint, model or authentication clears that result.
You can continue with a manually specified model without a successful probe.

**API key (optional)** accepts a pasted key in a masked field. No terminal setup
or UI restart is needed. Giraffe keeps it in this browser tab's memory and sends it
to the local runner only for connection tests and runs. Reloading the page or
changing the endpoint clears the key; **Clear key** removes it explicitly. An
already running test keeps the key it started with. Open local endpoints need no key.
The **Use an environment variable instead** option preserves the existing
`api_key_env` workflow for automation and shared configurations.

Pasted keys are separate from configuration and are not written into saved setups,
reports, baselines, or copied terminal commands. A saved setup uses the reference
`GIRAFFE_API_KEY` (or its existing `api_key_env`). When reopening it, paste the key
again or set that environment variable. A copied CLI command requires the variable.
Keys are used only for the selected inference endpoint, not separate metrics or
evaluator endpoints. Additional targets and evaluators can still use their own
environment-variable references in the advanced configuration.

`POST /api/targets/test-connection` accepts `url`, optional `model`, `api_key`,
`api_key_env`, `headers_env`, `ca_bundle`, `proxy` and `request_options`; it returns
`connected` or `needs_model`. `POST /api/targets/models` accepts the same credentials
for a metadata-only lookup. `POST /api/runs` and `POST /api/runs/try-example` accept
an optional `api_keys` array alongside `config`, with entries shaped as
`{"target": "local", "url": "https://endpoint.example/v1", "api_key": "..."}`.
Each entry must match a configured target name and URL. It applies only to that
request/run and is never added to the server's environment. A pasted key takes
precedence over `api_key_env`; combining it with a custom Authorization header is
rejected. TLS trust, proxy settings and other configured header variables still apply.

**Checks** opens on your selected checks. Use **Add checks** to search
the full catalog by purpose. Without `--config`, the initial selection is endpoint
response and answer correctness. With `giraffe ui --config FILE`, the initial
selection is the file's checks. Each check has an **i**
button for details and a **Settings** button for its controls. Load settings belong to their load
check; answer checks and rate tests can use your own questions. Shared request
and time limits apply across the run. The final review shows planned requests,
stopping limits and separate evaluator calls before you start. Plans update as
you edit; adaptive tests are labeled as bounded and built-in random arrival counts
as estimates. Increasing the total request limit always requires an explicit
action. **Launch** becomes available only after the current request plan is ready.

The optional `--config` prefills the form and preserves the supplied test selection.
In **Launch**, expand **Save this setup or use advanced options** to save
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

After preflight, the CLI prints its suite limits: maximum requests, concurrency,
total duration, per-request deadline, and output tokens. Ctrl-C stops new requests
and saves partial results. Normal runs never restart the service.

Read results in the web UI, or run `giraffe report RUN_DIR --html` and open the
generated `report.html`. The saved `report.json` contains measurements and
reproduction settings. A failed or interrupted run still produces
a report once the suite has started; preflight failures do not.

To compare runs, substitute your edited Ollama or vLLM configuration for
`my-config.yaml` below:

```sh
giraffe run --config my-config.yaml --output runs/before
giraffe baseline save runs/before baselines/local.json

# After changing your deployment:
giraffe run --config my-config.yaml --baseline baselines/local.json \
  --output runs/after
```

Saving a baseline is explicit. Subsequent runs never replace it; replacement
requires `baseline save ... --replace`. A baseline may contain failures: comparison
reports change, while absolute checks still evaluate the current run.

## Additional workloads and deployment evidence

Concurrent load, opt-in arrivals, shared prefixes, fixed/live
conversations, repeated-request consistency and parsed tool-calling checks use the
same global budgets. Deployment snapshots and bounded serving-metrics collection
keep configuration changes, provenance, missing data and performance evidence
alongside an explicit baseline. The CLI, local UI, HTML and JSON expose the same
configuration and evidence. `arrivals.max_client_wait_ms` sets the dispatch-wait
tolerance (default 50 ms); exceeding it leaves the scheduled arrival rate unqualified.
Prefix acceptance requires substantial cached-token reuse on enough repeats and
independent controls below the reuse threshold; a majority of repeat misses fails.
Concurrent load qualifies time-weighted client delivery and server output overlap;
its sending windows drain requests already sent. Text and optional logprob
consistency compare concurrent levels with sequential controls; strict mode fails
answer or comparable token divergence. Non-strict variation or logprob drift beyond
tolerance is inconclusive (not certified); full agreement can pass. Correctness is
reported independently, including `correctness_observed` in summaries below 1.0.
Logprob summaries show the maximum cross-level delta and token divergence count.
Strict cross-level equality requires batch-invariant serving; default vLLM serving
can diverge. Sessions refill a conversation pool and recall unique seeded facts,
feeding final answers without inline reasoning into live history. Concurrent load
scores the box label on every shape. Interference needs matched control/probe
short streams and an explicit slowdown limit. Missing evidence is inconclusive; see
[workload evidence](docs/workloads.md).


Read [the workload and telemetry guide](docs/workloads.md) and use
[the expanded example](examples/workloads.yaml). Export a sanitized reproduction
snapshot with `giraffe export runs/after`, or inspect deployment,
telemetry and comparison evidence with `giraffe inspect runs/after`.
New workloads stay disabled unless configured; missing internal telemetry stays
unverified.

## What runs

| # | Check | Built-in workload / measurement |
| --- | --- | --- |
| 1 | Network and access | Ordinary inference through the selected DNS/TLS/auth/network path |
| 2 | Serving success | HTTP status, completion envelope, nonempty answer and stream finish |
| 3 | First useful output | Client-observed first answer, separately from reasoning and initial requests |
| 4 | Generation pace | Generation rate after first output, response time and streaming pauses |
| 5 | Load test | Input/output combinations, mixed requests, maintained concurrency, scheduled arrivals, active-stream interference and token stress |
| 6 | Long-context behavior | Known facts at different positions, calibrated from actual input-token usage |
| 7 | Known-answer correctness | Known answers with observed scored overlap required when concurrency exceeds one |
| 8 | Structured JSON | Required fields, types and expected values under concurrency |
| 9 | Limits and cancellation | Output caps, stop sequences, deadlines, client interruption, backend evidence and follow-ups; separate sub-results are retained |
| 10 | Sustained load and recovery | Refilled heavy worker pool, then matched pre/post light probes with configured acceptance limits |
| 11 | GPU observations | Existing telemetry; absent data leaves observations unverified |
| 12 | Shared prefixes and cache evidence | Matched independent controls, repeated history and reported cache evidence |
| 13 | Multi-turn sessions | Fixed/live history and concurrent conversation measurements |
| 14 | Repeated-request consistency | Answer agreement within and across sequential/concurrent levels; correctness reported separately |
| 15 | Tool-calling capability | Parsed tool-call structure and expected arguments |

**Load test** uses `load_test`: a token data profile, selected input → output
combinations, request-frequency shares for mixed traffic, optional active-stream
interference and optional token stress. Each selected workload runs separately
under each selected traffic pattern and level. Maintained concurrency replenishes
individual slots as requests finish; scheduled arrivals send independently of
completions. Every test sends for its configured duration, then drains in-flight
requests. Minimum valid responses is an evidence requirement, not a stopping count.
Output lengths are caps, so early endings can leave coverage inconclusive.

Defaults select three combinations, maintained concurrency at levels `[1, 4]`,
and constant arrivals at `[1, 2]` requests/second. At 15 seconds per workload,
pattern and level, that is 12 tests and 3 minutes of sending time, plus finish and
setup allowances. A simultaneous-request ceiling can reduce inherited levels;
explicit levels must fit the ceiling. Mixed traffic, interference, Poisson arrivals,
bursts and token stress are off by default. Interference requires streaming and
levels of at least 2, alternating short and long probes against replenished streams.
Use [the token-stress example](examples/concurrent-workloads.yaml) for 10,000-token
input and output targets. Inputs calibrate against server token usage independently
of `context_limit`; ordinary-request output caps do not resize this profile.

Legacy concurrency, capacity and arrival checks migrate into separate sequential
Load tests with CLI and web migration notes. Old sample counts, weights and
per-request limits are not silently reinterpreted. Saved reports still render;
baseline comparisons across incompatible formats are inconclusive. Results show
requested/client/server concurrency, latency, throughput, token lengths and coverage.
The best result is the highest observed among tested settings. See
[workload settings](docs/workloads.md) and
[the implementation contract](docs/concurrent-load-design.md) for more detail.

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
prove backend work was reclaimed. Buffered responses can prevent a valid interference
measurement. The report preserves these limits.

First-content timing starts before the HTTP request and includes connection setup
and connection-pool waiting. Streamed request timing ends after processing `[DONE]`;
bounded transport cleanup happens afterward so completed HTTP/1.1 connections can
be reused. Tools that start their clock at HTTP header transmission exclude some
of this waiting, so their first-content values are not directly interchangeable.

Generation pace is a client-observed estimate: `(output tokens - 1) / seconds from
first useful answer chunk to last answer chunk`. Prompt waiting and trailing
usage/finish messages are excluded. For a streamed single-chunk answer with reported `completion_tokens <= 2`, an
inter-chunk gap is not applicable. Longer or unknown-length single-chunk answers
keep missing timing evidence. Stream gaps likewise measure only intervals
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

Each target requires a unique `name`, a `url` and a `model`; other settings have
defaults. Multiple targets share one global request/time/concurrency budget. Targets normally run sequentially;
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
max_requests: 600
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
```

Set `limits.recovery_max_ratio` (the shipped examples use `2`) to catch post-load
slowdowns even when probes stay within absolute latency limits. An absolute-only
recovery pass reports the measured probe/baseline ratios and states that no ratio
limit was configured. Sustained timeouts and errors count against recovery
`max_error_rate`, and load qualification requires at least `min_samples` successful
sustained requests. These load outcomes stay outside serving/first-output checks.

These are example limits, not recommendations for a model or GPU. Declared runtime,
image, topology, and quantization are recorded separately from observed identity.
Give each model/route a distinct target name. Include the service URL when supplying
replica targets; unreachable replicas remain visible in the report.

Use environment variables for credentials. `headers_env` maps header names to
environment-variable names. HTTPX honors `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY`;
`ca_bundle` supplies a private CA file. Nothing disables TLS verification.

`--nonstream` exercises nonstreaming Chat Completions. `--checks serving,correctness`
runs a selected subset. **All checks** in the UI exposes all 15 checks
with a selection checkbox and **Settings** action. Specialist settings stay in
advanced sections. Settings start collapsed, including saved custom settings.
Opening settings does not change them. Edits are kept automatically, and only one
editor is open at a time. Defaults inherit the run configuration;
custom settings change only the selected test. Deselecting a test does not run its
workload. Shared probes needed for a selected timing check remain visible
in the recorded request scenarios.

An explicit `checks` list is authoritative: selecting `json` or `gpu` enables it
without a second flag. Without a list, the nine default checks are selected, and
legacy `structured_json` / `metrics` flags opt into their additional checks.
`--structured-output` and `--metrics` extend a saved selection unless `--checks`
is also given. `--json` remains a hidden alias for `--structured-output`.
When reusing an older manifest whose `checks` list included disabled optional
tests, remove `json` or `gpu` from that list to keep them deselected. Saved custom
fixtures are used only when correctness is selected.
`--metrics` reads the
target's optional `metrics_url`. GPU evidence requires fresh source timestamps or
values changing across scrapes, plus every required family per observed device.
Fewer observed devices than `targets[].deployment.hardware.gpu_count` declares
leaves GPU coverage inconclusive. Set `limits.gpu_max_temperature_c` to fail when
any device exceeds that temperature in at least two snapshots. Without that limit,
`max_temperature_c` is a measurement only. New XID values or repeated
hardware/thermal slowdown bits fail; historical XIDs
and software power caps are informational. `gpu_min_observation_seconds` sets the
minimum GPU observation window (default 30, accepts zero), independently of
`metrics_interval_seconds`. Set it to your exporter's refresh period; for example,
`gpu_min_observation_seconds: 1` for a one-second exporter. GPU runs wait only for
the remaining window, bounded by the run deadline. A shorter observation window is
inconclusive. The final `end` scrape also closes the GPU window; all scrapes share
`metrics_max_samples`, preserving the first `start` and last `end`. Missing evidence is
inconclusive; this is not device health certification. No node agents are installed.

Connection establishment uses an HTTPX connect timeout of
`min(request_timeout_seconds * 0.9, 10)` seconds, inside the total request deadline.
A connect timeout is a transport failure; a response deadline remains a timeout.

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
giraffe run --manifest runs/pack-before --baseline baselines/pack.json \
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

Semantic scoring and judging strip closed `<think>`, `<thinking>`, or `<reasoning>`
blocks anywhere in the output, case-insensitively, including reasoning opened in the chat template
(output starts mid-reasoning before a closing tag), and save `reasoning_stripped` on the request record.
Raw response evidence follows the configured retention policy. An unclosed block
or a block with no final answer remains unscored: `reasoning without final answer`.
When `finish_reason` is `length`, an exact answer is unscored only if it is a strict
normalized prefix of the expected value. A complete wrong JSON value or schema
violation fails; JSON cut off during parsing remains unscored. Missing contains
matches remain unscored under truncation. A full matching
exact/contains answer or complete valid matching JSON can still be scored.
Judges never receive truncated answers. Missing scores leave correctness coverage
inconclusive rather than counting incomplete answers as wrong.

Built-in known-answer correctness, JSON, context, short and traffic fixtures
request `temperature: 0` only when neither run nor case options supply one.
Case temperature takes precedence; None-valued payload options are omitted.
This requests deterministic decoding; server support is not independently verified.
Context fact positions are separated by at least 32 characters within each input
size. Small context budgets produce fewer positions; per-request workload metadata
`position_coverage` lists the exercised positions and marks reduced coverage,
which prevents a pass. Non-timeout HTTP/protocol failures on context prompts fail.

Fairness copies 48 common short words, reserving 12 extra output tokens, and needs
visible overlap; insufficient caps or observed truncation leave it inconclusive.
Recovery reserves its baseline, probes and later fixed work. A consumed load
allowance can qualify with enough requests and at least 90% time-weighted occupancy
at the configured peak concurrency. Metrics report the completion condition.
Recovery timing/error limits apply to probes; heavy-load timeouts are observations,
while genuine server/protocol errors can fail. Capacity stages are exempt from
serving/access/first-output checks; other stress traffic exempts only timeouts and
intentional cancellation, keeping real HTTP/protocol errors visible. Setup counts
include quiet baselines, the pack loaded pass, and stop controls; adaptive estimates
warn about likely budget starvation. Nonstreaming cancellation is inconclusive:
`client cancellation requires streaming`.

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

Load test sends arrivals continuously during each rate stage, independently
of previous completions. `steady` spaces arrivals evenly; `poisson` produces seeded
uneven arrivals. Each arrival samples the configured mix of short answers, long
input retrieval, and longer exact-copy output. Every built-in traffic request has
a known-answer scorer. The defaults are a small smoke workload; choose rates,
lengths, duration and service limits that represent your deployment.

The legacy example below migrates `capacity` into Load test; new version-2 files
use `load_test`. Preview its migration notes and explicit token targets before
running.

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

The capacity plan row explains that scheduled requests consume the run limit even
when all request slots are busy and the generator cannot send them. This accounting
note remains visible in the plan without warning on an otherwise valid budget.

The result is the **highest acceptable tested arrival rate** for which every
tested rate at or below it was accepted, not a discovered maximum. A failure at a
lower rate followed by a pass at a higher rate is flagged as non-monotonic and
does not raise this boundary. Baseline comparison matches the planned profile, seed, tests and acceptance
criteria, and reports lost previously acceptable rates. Dropped or failed requests
remain outcomes, even if they change the observed request mix. Incomplete or
generator-limited evidence cannot establish an exact capacity decrease. Older
reports remain readable; the suite-version change prevents comparing the former
concurrency sweep with this arrival-rate definition.

Response retention is `failures` by default. `all` keeps response text for every
request, `failures` keeps failure evidence, and `none` discards response text after
scoring. Request metadata (timings, status, scores) is always saved. Retention does
not cap disk usage; the storage limits do. Prompts are deterministic built-ins; optional custom
fixtures are a local JSON list of `RequestSpec` objects. Legacy `--fixtures` accepts
only `exact`, `contains`, `json`, and `none` scorers; AI judges require a request
pack, and tool scoring is reserved for built-in tool checks. Custom fixture files must
remain available to reproduce a run.

```sh
giraffe run --manifest runs/before --output runs/reproduced
```

Saved runs on disk default to a 5 GiB total limit, a 1 GiB minimum free disk
reserve, and a 256 MiB retained JSON limit per run. Configure `--max-total-storage`,
`--min-free-disk`, and `--max-run-storage` on `giraffe run` or `giraffe web` (`ui`
also works); each accepts sizes such as `500MiB` or `2GiB`, and `0` disables it.
Disk sizes are displayed in binary units (B, KiB, MiB, GiB, TiB); the Runs page
cleanup size target is entered in MiB.
Runs refuse to start when the saved total exceeds its limit or free disk falls
below the reserve. Nothing is deleted automatically. Export removes response
bodies to meet the per-run limit, preserving failure evidence as long as possible;
metadata is always kept even if it exceeds the limit. Reports disclose omissions.
Standalone HTML adds disk space beyond the JSON limit.

```sh
giraffe storage --runs-dir runs
# Preview eligible runs without deleting them.
giraffe cleanup --runs-dir runs --older-than 30 --keep-latest 10
giraffe cleanup --runs-dir runs --older-than 30 --keep-latest 10 --yes
# Protect a run.
giraffe keep runs/before
# Remove that protection.
giraffe keep runs/before --off
```

Cleanup criteria combine with OR: old runs, runs beyond the newest N, or oldest
eligible runs until the requested total is reached. The newest count includes
protected runs. Cleanup `--max-total` must be positive; use `--older-than 0` to
explicitly remove all unprotected runs. Active and live running/stopping runs,
kept runs and baseline-source runs are protected. Baselines are counted separately and
never cleaned. Directories containing files Giraffe did not write are protected.
The Runs page offers the same preview and a separate deletion confirmation.
Cleanup reports the runs deleted, space freed on disk, and every skipped run
with its reason in both the UI and CLI.
If JSON export fails, Giraffe records an error and tries to save a small summary;
request evidence may be lost. If only HTML fails, the complete JSON stays saved
and the run completes with a warning. CLI writers refresh a heartbeat so crashed
runs can eventually become cleanable. Use the same storage flags with
`giraffe storage` to display your configured limits. See [disk measurements and limits](docs/resources.md).

Peak memory (RAM) is measured separately from saved data on disk. The default
**Memory limit (RAM growth during this run)** (`max_memory_mb: 4096`) guardrail
samples current process RSS about once per second and compares growth with RSS at
the run start. Earlier web-server allocations do not count toward the growth
budget. RAM uses binary MiB/GiB.
Use `giraffe run --max-memory 2048` for a 2 GiB limit, or `--max-memory 0` to disable
it. The web run-limits section offers the same setting. Exceeding it stops
unfinished work with partial evidence; the verdict is inconclusive if a required
check was cut short and no check failed. Completed checks keep their statuses, and
a limit crossing after completion is a warning only. It never lowers the requested
concurrency or request count. If RSS at run start is unavailable, the guardrail is
inactive. Short spikes between samples can be missed.

Reports record starting RSS, traffic and scoring absolute RSS peaks, and RAM-growth
peaks; the CLI and run detail also show export RAM. Process lifetime peak is
labelled separately because a web server can include earlier runs. Response bodies stay in RAM until their dependent
scoring and checks finish, so usage grows with request count and response size;
response retention controls saved output, not that working set. Export streams
JSON and copies only retained data that changes. See the measured before/after
results and limitations in [resources](docs/resources.md).

Cold-start testing is a separate, explicit command. Configure an argv list on the
specific target, such as `restart_command: [docker, restart, my-test-service]`, then:

```sh
giraffe cold-start --config my-test-service.yaml --restart service --output runs/cold
```

The command invokes only that hook. It does not provision deployments or reboot
nodes. A missing hook blocks the cold-start run. After the hook completes, readiness
retries use `readiness_timeout_seconds` (default 180 seconds), bounded by the run
deadline. GET `/v1/models` polls (or `/health` on 404) back off from 0.1 to 5
seconds, then one inference verifies readiness. These probes are recorded separately
and excluded from `max_requests` and `stop_after_errors`. Use an isolated test service when
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

## Agent and automation use

Discover supported tests and the complete configuration schema, validate a file
without sending traffic, then execute it with structured output. Plain
`giraffe validate --config FILE` prints a short text summary and planning warnings;
add `--show-config` to also print the normalized configuration.
`giraffe preview --config FILE` returns the same JSON preview as the UI, including
`config`, `request_pack_preview` and `request_plan`. Planning warnings are in
`request_plan.warnings`; add `--output-format text` for a short summary and
`Warning: ...` lines. Both commands work without sending traffic:

```sh
giraffe describe
giraffe validate --config deployment.json --output-format json
giraffe preview --config deployment.json --output-format text
giraffe run --config deployment.json --output-format json
```

`--config -` reads JSON or YAML from standard input. `--output-format json` emits
one JSON object on stdout; progress remains on stderr. Run results include
`outcome`, `exit_code`, `run_id`, and an absolute `report_paths.json` path.
`report_paths.html` is `null` unless `--html` was requested, in which case it is
an absolute path. Generate HTML later without traffic with
`giraffe report RUN_DIR --html` (optionally `--output PATH`). A preflight failure returns `outcome: "blocked"`, exit code 3,
and a message without report paths. Errors include field locations and messages.
`--structured-output` selects the structured JSON test; `--json` remains a hidden
compatibility alias. Use `--output-format json` for the CLI's output format.
`cold-start` and `baseline save` also support structured output.

`inspect`, `export`, `baseline save`, `run --baseline` and `run --manifest` accept
either a run directory containing `report.json` or the report file itself.

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

When verifying a separate worktree with an environment installed from another
checkout, use `PYTHONPATH="$PWD/src" python -m pytest -q`. Python's `-I` ignores
`PYTHONPATH`, so omit it for this invocation. Confirm the source with
`PYTHONPATH="$PWD/src" python -c 'import giraffe.runner; print(giraffe.runner.__file__)'`
before running an external verifier against worktree changes.

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

The model runs below predate the arrival-rate capacity test and the stricter
reliability audit; their pass counts do not certify the current suite. The new scheduler,
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
