<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/0dust/giraffe/main/docs/assets/giraffe-dark.svg">
    <img alt="Giraffe" src="https://raw.githubusercontent.com/0dust/giraffe/main/docs/assets/giraffe-light.svg" width="360">
  </picture>
</p>

<h3 align="center">Check whether your self-hosted LLM performs as expected.</h3>

<p align="center">
  <a href="https://pypi.org/project/giraffe-llm/"><img alt="PyPI version" src="https://img.shields.io/pypi/v/giraffe-llm"></a>
  <a href="https://github.com/0dust/giraffe/actions/workflows/checks.yml"><img alt="CI checks" src="https://github.com/0dust/giraffe/actions/workflows/checks.yml/badge.svg?branch=main&amp;event=push"></a>
  <a href="https://github.com/0dust/giraffe/blob/main/docs/README.md"><img alt="Documentation" src="https://raw.githubusercontent.com/0dust/giraffe/main/docs/assets/badges/docs.svg"></a>
  <a href="https://discord.gg/GBefPbWebr"><img alt="Join the Giraffe Discord" src="https://raw.githubusercontent.com/0dust/giraffe/main/docs/assets/badges/discord.svg"></a>
  <a href="https://github.com/0dust/giraffe/blob/main/pyproject.toml"><img alt="Python 3.11+" src="https://raw.githubusercontent.com/0dust/giraffe/main/docs/assets/badges/python.svg"></a>
  <a href="https://github.com/0dust/giraffe/blob/main/LICENSE"><img alt="License Apache 2.0" src="https://raw.githubusercontent.com/0dust/giraffe/main/docs/assets/badges/license.svg"></a>
</p>

---

Giraffe is a test suite for engineers running self-hosted LLMs. Point it at an
existing **OpenAI-compatible chat API**, such as Ollama or vLLM, to check answers
against expected results, measure response times, and test behavior under load.
Run it from the command line or a local web UI, inspect failed requests, and
compare results before and after a deployment change.

Use it to answer questions like:

- Do answers that pass individually still pass when requests run concurrently?
- Which tested traffic rates meet our correctness and response-time limits?
- After a server upgrade, which checks or measurements got worse?

Start with built-in checks, or supply your own questions and expected answers.
You choose the checks, traffic and acceptance limits. A passing run applies to
those tests and settings; it does not establish general model quality or certify
production capacity.

[Quick start](#quick-start) · [Inference engines](#inference-engines) · [Baselines](#save-a-baseline-and-compare) · [Tests](#tests) · [Docs](#documentation) · [Discord](https://discord.gg/GBefPbWebr)

## Quick start

Start your inference server separately and have a model ready to use.

### 1. Install

Install [`giraffe-llm` from PyPI](https://pypi.org/project/giraffe-llm/) in a virtual environment. The command is `giraffe`:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install "giraffe-llm==0.1.0a1"
```

### 2. Start your run

This example uses Ollama with a model already installed. Replace the URL for your server.

```sh
giraffe run --url http://127.0.0.1:11434/v1 --html
```

The model is auto-detected when only one is served; otherwise add `--model MODEL_ID`.
The CLI and UI select the same four checks by default:
`access, serving, correctness, first_output`. These check endpoint access, response
validity, answer correctness and p95 time to the first answer.
The default first-output limit is **10,000 ms**, a generous laptop sanity bound;
override it with `--first-output-ms` or YAML `limits.first_output_ms`.
Default check concurrency is four. Fully scored answers can conclude on accuracy when
concurrency is inherited; missing server overlap is reported as an observation.
Set `--default-concurrency` or a per-check concurrency override above one to require concurrent evidence.
The 10,000 ms sanity bound applies only to `first_output`.

**Outputs:** results in the terminal, plus JSON and HTML reports in `runs/<timestamp>/`. The command exits when finished. Open `report.html` to inspect each check and its requests.

A **pass** means the selected required checks have enough evidence and meet their
limits. A **fail** means a check observed a failure. **Inconclusive** means there
is not enough evidence to decide; **blocked** means the run or check could not
proceed. See the [results guide](https://github.com/0dust/giraffe/blob/main/docs/running.md#results-and-stopping)
for exit codes and partial runs.

### Prefer the browser UI?

Run this in your terminal:

```sh
giraffe ui
```

Open the access link printed in the terminal (it contains a per-launch token), enter your endpoint and model, choose checks, then **Launch**. Keep the terminal open; **Ctrl+C** stops the UI. Use `--port` to choose a different port.

## Inference engines

Connect through the engine’s **OpenAI-compatible chat API**. Discovery, metrics and server-side evidence vary by engine.

Generate a starter configuration from your installed package:

```sh
giraffe init --engine vllm --url http://127.0.0.1:8000/v1
giraffe validate --config giraffe.yaml
giraffe run --config giraffe.yaml --html
```

`init` discovers the model when exactly one is served; otherwise edit the model
placeholder. It refuses to overwrite `giraffe.yaml` without `--force`.
Supported engines are `ollama`, `vllm`, `sglang`, `llamacpp`, `tgi`, `lmdeploy`
and `tensorrt-llm`. The repository also has detailed examples:

| Engine | YAML configuration |
| --- | --- |
| Ollama | [`local.yaml`](https://github.com/0dust/giraffe/blob/main/examples/local.yaml) |
| vLLM / vLLM-Metal | [`vllm.yaml`](https://github.com/0dust/giraffe/blob/main/examples/vllm.yaml) |
| SGLang | [`sglang.yaml`](https://github.com/0dust/giraffe/blob/main/examples/sglang.yaml) |
| llama.cpp server | [`llamacpp.yaml`](https://github.com/0dust/giraffe/blob/main/examples/llamacpp.yaml) |
| TensorRT-LLM | [`tensorrt-llm.yaml`](https://github.com/0dust/giraffe/blob/main/examples/tensorrt-llm.yaml) |
| LMDeploy | [`lmdeploy.yaml`](https://github.com/0dust/giraffe/blob/main/examples/lmdeploy.yaml) |
| Hugging Face TGI | [`tgi.yaml`](https://github.com/0dust/giraffe/blob/main/examples/tgi.yaml) |

Edit the generated `giraffe.yaml` for your endpoint, model, checks and limits:

```sh
giraffe run --config giraffe.yaml \
  --output runs/before --html
```

Giraffe reads the run settings from YAML; `--output` chooses where to save the results, and `--html` adds an HTML report.

## CI and GitHub Actions

Use `--quiet --junit junit.xml --summary-md summary.md` for CI output. Saved
runs support `giraffe report runs/before --junit junit.xml --summary-md summary.md`.
`--fail-on inconclusive` is the default; `--fail-on fail` accepts an inconclusive
verdict with a stderr note. `--fail-on never` accepts failed and inconclusive
verdicts, including baseline regressions, with a stderr note. Blocked runs and
errors keep their exit codes.

The composite Action installs the selected package release, adds a job summary
and uploads run artifacts, including when checks fail. Choose a package version
containing these CLI options:

```yaml
steps:
  - uses: actions/checkout@v4
  - uses: 0dust/giraffe@main
    with:
      url: http://127.0.0.1:8000/v1
      model: your-model
      checks: access,serving,correctness,first_output
      fail-on: inconclusive
      python-version: '3.12'
      version: latest
```

`run --help` shows common options; `run --help-all` includes advanced settings.
Manage saved runs with `giraffe runs list`, `runs cleanup` and `runs keep`.

The default install includes the CLI, UI and built-in tiktoken sizing. To use a
model's Hugging Face tokenizer, install `giraffe-llm[hf-tokenizer]`.

For Docker, build with `docker build -t giraffe .`, then run:

```sh
docker run --rm -p 127.0.0.1:8765:8765 giraffe ui --host 0.0.0.0
```

Open the printed token link (wildcard binds use `localhost` in the link). The
published port is accessible only from the Docker host. To expose it beyond
localhost, change the published address deliberately and use a trusted network.
Keep the access link private; it permits run control and access to saved reports.

## Test behavior under load

Select **Load test** to repeat requests at different concurrency levels or arrival
rates. Set the traffic and correctness, response-time and error limits for your
service, then inspect which tested settings pass and which fail. The highest
passing setting is a tested result, not a discovered maximum capacity.

Built-in workloads cover different input and output lengths. You can also supply
your own questions and expected answers to test the work your application needs.
See the [Load test guide](https://github.com/0dust/giraffe/blob/main/docs/workloads.md#load-test)
for traffic patterns, answer checks and configuration examples.

## Save a baseline and compare

Save the completed `runs/before` run as a reference. After a deployment change,
such as a server upgrade, run the same configuration with `--baseline`. Keep the
endpoint URL, model ID, workload and acceptance settings the same across runs:

```sh
giraffe baseline save runs/before baselines/before.json

# After changing your deployment:
giraffe run --config examples/vllm.yaml \
  --baseline baselines/before.json --output runs/after --html
```

The new report shows acceptance results and changes against the saved baseline. Keep the workload comparable; incompatible measurements stay inconclusive.

In the UI, open a completed run and choose **Save baseline** (fully passing runs) or **Save as reference baseline** (other completed runs). Name and save it, then choose **Compare a new run with this baseline** at the bottom of the report, or **Compare a new run** when shown at the top. [Baseline guide →](https://github.com/0dust/giraffe/blob/main/docs/running.md#compare-with-a-baseline)

## Tests

15 built-in checks. The four defaults are `access, serving, correctness, first_output`; select additional checks for your endpoint.

| Check · CLI ID | What it checks |
| --- | --- |
| Endpoint access · `access` | Connection, authentication and ordinary inference |
| Response validity · `serving` | Complete, valid, nonempty responses |
| Time to first answer · `first_output` | Time to the first useful answer |
| Generation speed · `generation` | Answer rate, response time and stream pauses |
| Load test · `load` | Correctness and timing under selected traffic patterns |
| Long-context recall · `context` | Recall of known facts across long inputs |
| Answer correctness · `correctness` | Answers against expected values |
| JSON correctness · `json` | Expected fields, types and values |
| Cancellation & limits · `cancellation` | Caps, stop behavior, client cancellation and follow-up requests |
| Recovery after load · `recovery` | Heavy traffic followed by matched light probes |
| GPU metrics · `gpu` | Available GPU telemetry |
| Prefix cache · `prefix` | Reported cache reuse against independent controls |
| Multi-turn sessions · `sessions` | Recall and timing across concurrent conversations |
| Answer consistency · `consistency` | Answer agreement across repeated requests |
| Tool calling · `tools` | Tool-call structure, names and expected arguments |

The catalog has 13 verdict checks and two server observations: **GPU metrics** and
**Prefix cache**. Both remain in the selected checks and baseline configuration.
Missing GPU telemetry and missing or inactive prefix caching do not change the
verdict. GPU hardware faults and an exceeded configured temperature limit can
still fail the run. Prefix cache never fails the run.

When selected, generation uses a **5,000 ms stream-gap limit** if no generation
limit is supplied; this catches stalls without imposing a token speed. Set
`--stream-gap-ms` or YAML `limits.stream_gap_ms` to override it; an explicit YAML
`stream_gap_ms: null` disables this fallback. Recovery can pass
on correctness and error rate without a timing limit; timing remains an observation.

Use `--json` (an alias of `--output-format json`) for machine-readable CLI output.
Use `--structured-output` to add the structured-JSON check, and `--verbose` to show
diagnostic stages and RAM measurements. JSON reports retain those measurements.

Results are **pass**, **fail**, **inconclusive**, **skipped** or **blocked**. Missing delivery or measurement evidence stays inconclusive. Inspect the request evidence before attributing a failure to your inference server.

## Documentation

[Documentation index](https://github.com/0dust/giraffe/blob/main/docs/README.md) — detailed guides in the same repository:

[Running & baselines](https://github.com/0dust/giraffe/blob/main/docs/running.md) · [Workloads & measurements](https://github.com/0dust/giraffe/blob/main/docs/workloads.md) · [Offline & Docker](https://github.com/0dust/giraffe/blob/main/docs/offline.md) · [Resource limits](https://github.com/0dust/giraffe/blob/main/docs/resources.md)

## Community

[Join the Giraffe Discord](https://discord.gg/GBefPbWebr) to get help with runs and reports, share feedback, and discuss ideas. Start a post in **#help-and-feedback** for a question, bug, or suggestion, and follow **#updates** for releases and improvements.

[Report an issue](https://github.com/0dust/giraffe/issues) with your Giraffe version, engine/model and reproduction steps. Remove API keys and private prompts or answers before sharing reports.

[Contributing](https://github.com/0dust/giraffe/blob/main/CONTRIBUTING.md) · [Apache 2.0 license](https://github.com/0dust/giraffe/blob/main/LICENSE)
