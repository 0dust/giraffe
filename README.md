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

This example uses Ollama with `llama3.2:3b` already installed. Replace the URL and model for your server.

```sh
giraffe run --url http://127.0.0.1:11434/v1 \
  --model llama3.2:3b --checks access,serving,correctness \
  --concurrency 1 --html
```

This is a basic sequential check of endpoint access, response validity and known-answer correctness. It does not test concurrent serving or load capacity.

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

Open **[localhost:8765](http://127.0.0.1:8765)**, enter your endpoint and model, choose checks, then **Launch**. Keep the terminal open; **Ctrl+C** stops the UI. Use `--port` to choose a different port.

## Inference engines

Connect through the engine’s **OpenAI-compatible chat API**. Discovery, metrics and server-side evidence vary by engine.

Download a YAML configuration from the links below into a local `examples/` directory. Each file defines the endpoint, model, checks, load settings and acceptance limits for a run.

| Engine | YAML configuration |
| --- | --- |
| Ollama | [`local.yaml`](https://github.com/0dust/giraffe/blob/main/examples/local.yaml) |
| vLLM / vLLM-Metal | [`vllm.yaml`](https://github.com/0dust/giraffe/blob/main/examples/vllm.yaml) |
| SGLang | Use your endpoint URL and model ID in the [CLI command above](#2-start-your-run) |
| llama.cpp server | [`llamacpp.yaml`](https://github.com/0dust/giraffe/blob/main/examples/llamacpp.yaml) |
| TensorRT-LLM | [`tensorrt-llm.yaml`](https://github.com/0dust/giraffe/blob/main/examples/tensorrt-llm.yaml) |
| LMDeploy | [`lmdeploy.yaml`](https://github.com/0dust/giraffe/blob/main/examples/lmdeploy.yaml) |
| Hugging Face TGI | [`tgi.yaml`](https://github.com/0dust/giraffe/blob/main/examples/tgi.yaml) |

Open the file for your engine and edit its URL, model, checks and limits. From the directory containing `examples/`, pass that file to `--config`. For example, after editing `examples/vllm.yaml`:

```sh
giraffe run --config examples/vllm.yaml \
  --output runs/before --html
```

Giraffe reads the run settings from YAML; `--output` chooses where to save the results, and `--html` adds an HTML report.

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

In the UI, open a completed run and choose **Save as reference baseline**, then **Compare a new run with this baseline**. [Baseline guide →](https://github.com/0dust/giraffe/blob/main/docs/running.md#compare-with-a-baseline)

## Tests

15 built-in checks. Select the ones that matter to your endpoint.

| Check · CLI ID | What it checks |
| --- | --- |
| Network and access · `access` | Connection, authentication and ordinary inference |
| Serving success · `serving` | Complete, valid, nonempty responses |
| First useful output · `first_output` | Time to the first useful answer |
| Generation pace · `generation` | Answer rate, response time and stream pauses |
| Load test · `load` | Correctness and timing under selected traffic patterns |
| Long-context behavior · `context` | Recall of known facts across long inputs |
| Known-answer correctness · `correctness` | Answers against expected values |
| Structured JSON · `json` | Expected fields, types and values |
| Limits and cancellation · `cancellation` | Caps, stop behavior, client cancellation and follow-up requests |
| Sustained load and recovery · `recovery` | Heavy traffic followed by matched light probes |
| GPU observations · `gpu` | Available GPU telemetry |
| Shared prefixes and cache evidence · `prefix` | Reported cache reuse against independent controls |
| Multi-turn sessions · `sessions` | Recall and timing across concurrent conversations |
| Repeated-request consistency · `consistency` | Answer agreement across repeated requests |
| Tool-calling capability · `tools` | Tool-call structure, names and expected arguments |

Results are **pass**, **fail**, **inconclusive**, **skipped** or **blocked**. Missing delivery or measurement evidence stays inconclusive. Inspect the request evidence before attributing a failure to your inference server.

## Documentation

[Documentation index](https://github.com/0dust/giraffe/blob/main/docs/README.md) — detailed guides in the same repository:

[Running & baselines](https://github.com/0dust/giraffe/blob/main/docs/running.md) · [Workloads & measurements](https://github.com/0dust/giraffe/blob/main/docs/workloads.md) · [Offline & Docker](https://github.com/0dust/giraffe/blob/main/docs/offline.md) · [Resource limits](https://github.com/0dust/giraffe/blob/main/docs/resources.md)

## Community

[Join the Giraffe Discord](https://discord.gg/GBefPbWebr) to get help with runs and reports, share feedback, and discuss ideas. Start a post in **#help-and-feedback** for a question, bug, or suggestion, and follow **#updates** for releases and improvements.

[Report an issue](https://github.com/0dust/giraffe/issues) with your Giraffe version, engine/model and reproduction steps. Remove API keys and private prompts or answers before sharing reports.

[Contributing](https://github.com/0dust/giraffe/blob/main/CONTRIBUTING.md) · [Apache 2.0 license](https://github.com/0dust/giraffe/blob/main/LICENSE)
