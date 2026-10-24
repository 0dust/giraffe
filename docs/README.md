# Giraffe documentation

[Back to the README](../README.md)

Start with a small run against a server you already have running. The guides below
describe the current configuration contract (`config_version: 2`).

| Guide | What you will find |
| --- | --- |
| [Running and baselines](running.md) | CLI commands, browser UI, credentials, results and before/after comparisons |
| [Workloads and measurements](workloads.md) | Load patterns, check settings, acceptance criteria and evidence limits |
| [Offline and container use](offline.md) | Dependency wheels, tokenizer caches and Docker commands |
| [Resource limits](resources.md) | Disk and RAM limits, response retention and cleanup |

Engine configurations live in [`examples/`](../examples/). Edit their endpoint,
model and limits for your deployment, then use `giraffe validate` and
`giraffe preview` before running them. Validation and preview send no test traffic.
The workload guide identifies examples that still exercise legacy migration.

## Historical evidence and designs

| Archive | Scope |
| --- | --- |
| [October 8 engine validation](validation/2026-10-08.md) | Results from the earlier suite on local Ollama and vLLM-Metal; not current-release qualification |
| [Concurrent load design](concurrent-load-design.md) | Superseded design retained for context; use the current Load test reference for configuration |

Live engine results apply to the recorded runtime, model and workload. A runnable
example or passing automated test does not qualify every engine or deployment.
