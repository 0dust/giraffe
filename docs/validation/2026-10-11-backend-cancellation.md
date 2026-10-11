# Shared backend cancellation validation — October 11, 2026

Validated implementation commit `a09b197` on Apple M1, macOS 15.5, 8 GB RAM.
Backend verification is part of the existing **Limits and cancellation** check.
The catalog has 19 checks. Explicit target runtime metadata selects the observer;
arbitrary target names do not. See [setup and evidence boundaries](../backend-cancellation.md).

## Runtime scope

| Runtime | Version / backend | Model |
| --- | --- | --- |
| SGLang | Source `44bc478863052efdc455b0282b49830f68fdefb3`, MLX | Existing Llama 3.2 1B Instruct, 4-bit |
| vLLM | 0.30.0 V1 with vllm-metal 0.30.0 | Same existing MLX Llama 3.2 1B Instruct, 4-bit |
| Ollama | Installed 0.12.10 Go runner | Existing `llama3.2:3b`, Q4_K_M; smallest installed Ollama model |

Only one inference engine was loaded at a time. The original Ollama daemon/model
store was preserved; a dedicated debug server used port 11435 and one runner slot.
All engines used a 1,024-token context. SGLang/vLLM allowed two running requests;
Ollama admitted one at a time. vLLM enabled automatic tool choice with `llama3_json`.
The optional vLLM scheduler observer disables async scheduling; these results cover
that instrumented Metal deployment. Linux/CUDA and other runtime versions were not tested.

## Backend cancellation

Each full run required three probes, closed each stream after five useful chunks,
and required cleanup within one second plus valid follow-up inference. Every
runtime also received a separate focused rerun on the same committed source.

| Runtime | Full-run backend probes | Native observation | Delay after client close |
| --- | --- | --- | --- |
| sglang | 3/3 pass | Exact request ID/model terminal abort | 35.90, 39.84, 36.59 ms |
| vllm | 3/3 pass | Exact request ID/model terminal abort | 12.28, 0.56, 12.78 ms |
| ollama | 3/3 pass | Same exclusive runner slot reused | 98.33, 125.46, 107.88 ms |

Ollama establishes controlled capacity release, **not a native abort reason or an
exact termination timestamp**. Each uncancelled reference reached its 128-token cap;
the cancelled requests grew the native cache by 6, 5, 6 tokens before slot reuse.
An early natural-EOS race cannot be excluded by these logs. SGLang/vLLM use exact
per-request terminal evidence. None of these observations certifies physical GPU
memory deallocation.

### Negative control

The vLLM negative control backend result was **fail**. A local test gateway
deliberately continued reading the upstream stream after the downstream client
closed. The observer reported the native request ending after the one-second
cleanup deadline. This verifies that a client disconnect alone cannot produce a
passing backend result in the terminal-evidence path.

- Native terminal delay: 2975.54 ms; reason: Backend request remained active beyond the cleanup deadline.

## Complete live suite

All 19 checks were selected and completed for each engine, with no budget abort.
Run ceilings: 800 requests, 1,800 seconds, 30 seconds/request, concurrency two and
128 output tokens. The workload used six samples and three 20-second arrival
stages at 0.25, 0.5 and 1 request/second. Existing acceptance thresholds were retained.

| Runtime | Recorded requests | Pass | Fail | Inconclusive | Generator maximum lag |
| --- | ---: | ---: | ---: | ---: | ---: |
| sglang | 481 | 6 | 3 | 10 | 143.445 ms |
| vllm | 379 | 13 | 4 | 2 | 50.911 ms |
| ollama | 362 | 7 | 0 | 12 | 386.561 ms |

| Check | SGLang | vLLM | Ollama |
| --- | --- | --- | --- |
| Network and access (`access`) | pass | pass | pass |
| Serving success (`serving`) | pass | pass | pass |
| First useful output (`first_output`) | inconclusive | pass | inconclusive |
| Generation pace (`generation`) | inconclusive | pass | inconclusive |
| Arrival traffic capacity (`capacity`) | fail | fail | inconclusive |
| Mixed long/short fairness (`fairness`) | inconclusive | pass | inconclusive |
| Long-context behavior (`context`) | pass | pass | pass |
| Known-answer correctness (`correctness`) | pass | pass | pass |
| Structured JSON (`json`) | pass | pass | pass |
| Limits and cancellation (`cancellation`) | fail | fail | pass |
| Sustained load and recovery (`recovery`) | inconclusive | pass | inconclusive |
| GPU observations (`gpu`) | inconclusive | inconclusive | inconclusive |
| Scheduled arrivals (`arrivals`) | inconclusive | pass | inconclusive |
| Shared prefixes and cache evidence (`prefix`) | inconclusive | pass | inconclusive |
| Input/output and concurrency coverage (`buckets`) | inconclusive | inconclusive | inconclusive |
| Mixed workload interference (`mixed`) | inconclusive | fail | inconclusive |
| Multi-turn sessions (`sessions`) | fail | fail | inconclusive |
| Repeated-request consistency (`consistency`) | inconclusive | pass | inconclusive |
| Tool-calling capability (`tools`) | pass | pass | pass |

These are integration results, not an all-passing deployment. Backend cancellation
can pass while the parent check fails an existing output/answer requirement.

### sglang failures and coverage limits

- `capacity`: Answer checks failed: traffic.long_output.v1 (12/12).
- `cancellation`: Answer checks failed: limits.stop.v1 (1/1).
- `sessions`: Workload task checks or request errors violated configured limits; variation and telemetry are assessed separately.
- Inconclusive: `first_output`, `generation`, `fairness`, `recovery`, `gpu`, `arrivals`, `prefix`, `buckets`, `mixed`, `consistency`.
  Inspect each report summary for generator lag, sample/rate qualification, missing GPU telemetry or workload coverage; inconclusive is not a proven endpoint failure.

### vllm failures and coverage limits

- `capacity`: Answer checks failed: traffic.long_output.v1 (12/12).
- `cancellation`: Answer checks failed: limits.stop.v1 (1/1).
- `mixed`: Short-request latency exceeded the configured mixed-load ratio.
- `sessions`: Workload task checks or request errors violated configured limits; variation and telemetry are assessed separately.
- Inconclusive: `gpu`, `buckets`.
  Inspect each report summary for generator lag, sample/rate qualification, missing GPU telemetry or workload coverage; inconclusive is not a proven endpoint failure.

The qualified mixed-load comparisons measured short-request median latency at
3.54 times the short-only baseline before long-input output, and 3.03 times during
its active stream. Both exceed the configured 3.0 ratio. This is a measured
performance-limit failure for the tested instrumented deployment.

### ollama failures and coverage limits

- No failing checks.
- Inconclusive: `first_output`, `generation`, `capacity`, `fairness`, `recovery`, `gpu`, `arrivals`, `prefix`, `buckets`, `mixed`, `sessions`, `consistency`.
  Inspect each report summary for generator lag, sample/rate qualification, missing GPU telemetry or workload coverage; inconclusive is not a proven endpoint failure.

SGLang long-output traffic returned a refusal to the harmless color-pattern prompt.
The SGLang stop fixture returned `Blue ` instead of exact lowercase `blue`.
Its sessions changed `Golden Crust` to `Golden Crust Bakery` and altered punctuation
in the live answer. These are model/fixture answer mismatches; the reports do not
attribute them to failed backend cancellation. Scoring was not relaxed.

## Automated verification and cleanup

- Entire automated suite: **541 passed in 38.36 seconds**.
- Ruff, JavaScript syntax and `git diff --check`: passed.
- Real UI: one Limits and cancellation entry, with runtime/backend probe evidence inside it.
- Memory-pressure guards sampled every 250 ms. No critical-pressure sample occurred.
- Task-owned inference servers and the negative-control gateway were stopped after testing.
- Main was not changed; no push or pull request was made.

## Local evidence

Raw reports are intentionally ignored runtime artifacts in the Giraffe checkout:

- [`runs/sglang-all-checks-final/report.json`](../../runs/sglang-all-checks-final/report.json)
- [`runs/vllm-all-checks-final/report.json`](../../runs/vllm-all-checks-final/report.json)
- [`runs/ollama-all-checks-final/report.json`](../../runs/ollama-all-checks-final/report.json)
- [`runs/vllm-cancellation-negative-control/report.json`](../../runs/vllm-cancellation-negative-control/report.json)

Configs, bounded runtime launchers, native evidence, memory samples and automated
check logs are retained in `../artifacts/backend-cancellation-trial/` relative to
Giraffe. Final runtime folders are `sglang-final`, `vllm-final`, `ollama-final`
and `ollama-all-final`. These local artifacts are not added to public Git history.
