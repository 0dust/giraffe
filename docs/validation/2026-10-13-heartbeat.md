# Client timing qualification — October 13, 2026

Branch: `aiswarya/fixBugs`, created directly from PR #11's tip `79e3e97`.
The SGLang/cancellation changes remain in its ancestry; this branch adds only
client timing qualification, receive-boundary changes, regression tests and docs.

## Problem and correction

Previously, one maximum heartbeat delay applied to every timing check in a run
and every baseline comparison for a target. The earlier three-runtime runs each
had one heartbeat above 100 ms, resulting in nine timing-inconclusive checks per
engine, regardless of which requests overlapped that delay.

Giraffe now retains bounded heartbeat windows and sample counts. It conservatively
uses the last responsive tick as the start of an uncertain interval, since the
exact start of a stall between ticks is unknown. Checks and baseline rows match
those windows to their own request/metric intervals. Capacity stages also include
offered arrivals and drain, including periods with no request in flight. Fairness
ratios include their quiet controls. Legacy reports without windows retain the
old conservative run-wide fallback.

First-output qualification ends at the first useful output. Generation-rate
qualification starts at dispatch, even though the rate formula uses first-to-last
answer time: a pause before the first read may buffer tokens and artificially
compress their observed spacing. Completion/gap qualification excludes hashing
performed after the inference timer ended. The final pending heartbeat interval
is sampled before the monitor is cancelled, including a final blocking callback.

All raw measurements and samples remain present. No heartbeat time is subtracted,
no delayed samples are filtered out, and acceptance thresholds are unchanged.
Independent answer, protocol and unaffected timing failures still fail.
`timing_quality` records affected request IDs/counts and metrics; the UI displays
those counts separately from the check verdict. Up to 4,096 windows are retained;
further windows become a conservative enclosing overflow interval.

Payload/history hashes are prepared before the inference timer. Streaming receive
timestamps are captured when a chunk completing an SSE event is read, before
JSON parsing. A private monotonic dispatch anchor aligns them with heartbeat
windows, excluding progress-callback work. These are client observations, not
kernel arrival timestamps or isolated server/model computation. Timing measurement
version 2 requires a new timing/traffic baseline when comparing with older
boundaries; a version difference alone does not invalidate correctness comparisons.

## Automated and controlled verification

- Full suite: **776 passed**. Ruff, JavaScript syntax and whitespace checks passed.
- Nineteen added regression cases cover unrelated phases and targets, metric
  boundaries, offered-arrival gaps, bounded retention, legacy/malformed reports,
  independent failures, receive parsing/preparation delays, baseline qualification,
  dispatch alignment and a final blocking callback.
- The post-response hashing regression failed before its interval correction.
  Slow parser/hash controls verify early timestamps without erasing completion
  processing time. A real local HTTP test verifies progress callbacks are excluded
  from the client dispatch anchor.
- Regrading frozen earlier requests using their retained external heartbeat windows
  gives Ollama 14 pass / 4 fail / 1 inconclusive, vLLM 13 / 4 / 2, and SGLang 14 / 3 / 2.
  Those are historical regrades, separate from the fresh live runs below. They
  expose genuine configured mixed-load ratio failures in Ollama/vLLM that the old
  global flag obscured; they do not simply convert every inconclusive to pass.

## Fresh live controls

All 19 checks run serially on an 8 GB Apple M1: Ollama, unload/stop, vLLM, stop,
SGLang, stop. The model, runtime settings, workload settings and acceptance limits
match the preceding [rebase validation](2026-10-13-rebase.md): the same
`mlx-community/Llama-3.2-1B-Instruct-4bit` source revision
`08231374eeacb049a0eade7922910865b8fce912`, the same MLX files for SGLang/vLLM,
and the existing Ollama F16 expansion of those quantized tensor values. This is
not a bit-identical cross-engine throughput comparison or Linux/CUDA qualification.
Temperature is zero, contexts are 1024 tokens, samples/minimums and the fixed
12 Oct template date are retained. No external AI judge is configured.

The product heartbeat monitor is used directly, without the earlier diagnostic
monitor replacement. Task-owned server groups have a 250 ms memory-pressure guard;
critical pressure would stop the owned server. Other inference engines remain
unloaded, and the original Ollama daemon/model store are preserved. Early interrupted
trials are excluded from the final request totals.

## Fresh results

All final runs completed without a budget abort or unexpected transport/protocol
errors. There were **1,118 recorded inference requests**; focused setup probes
and interrupted earlier trials are additional and excluded.

| Runtime | Requests | Pass | Fail | Inconclusive |
| --- | ---: | ---: | ---: | ---: |
| ollama | 357 | 13 | 4 | 2 |
| vllm | 354 | 13 | 4 | 2 |
| sglang | 407 | 15 | 3 | 1 |

## Remaining inconclusives and heartbeat frequency

| Runtime | Late / observed heartbeats | Frequency | Maximum lag | Timing-inconclusive checks |
| --- | ---: | ---: | ---: | --- |
| ollama | 3 / 15,544 | 0.0193% | 200.29 ms | buckets |
| vllm | 1 / 10,621 | 0.0094% | 102.94 ms | buckets |
| sglang | 1 / 6,558 | 0.0152% | 170.41 ms | None |

GPU telemetry is unavailable in all three runs and remains independently
inconclusive. Ollama/vLLM each have **2 of 108 bucket requests** with overlapping
client delay. Their bucket first-output intervals are unaffected; latency, stream
gap and generation-rate intervals are qualified. Other timing checks keep their
verdicts. SGLang's 170.41 ms delay occurred during startup, before measured
inference; it does not invalidate later request timings.

The five late heartbeats out of 32,723 observations represent **0.0153%** of this
bounded sample. Ollama's three latenesses were 109.94, 200.29 and 161.16 ms; the
first did not overlap any assessed request timing. vLLM recorded 102.94 ms. The
nearest 250 ms pressure samples for every event reported normal memory pressure
(1); overall trials also observed warning pressure (2), but no critical pressure
(4). Trial shutdown returned to normal pressure. This does not prove memory
compression or other host activity had no effect. The precise OS/CPU/client cause
of these delays is unresolved; a heartbeat is responsiveness evidence, not a
diagnosis of CPU saturation or server slowness.

## Failures retained

All three reproduce the preceding answer mismatches: 12/12 long-output colour
copy canaries refuse the harmless pattern; the stop fixture returns `Blue `
rather than lowercase `blue`; fixed histories repeat `Golden Crust Bakery`
instead of `Golden Crust`; live histories repeat the original name plus extra
names. These are fixture-answer failures, not established universal serving
engine defects. The stop fixture still mixes copying/case fidelity with stop
behaviour. Its acceptance rule was not relaxed.

Ollama/vLLM also fail the configured **3× mixed-load latency ratio**. Ollama's
short request with long generation arriving before its output slows by **21.53×**.
vLLM's long-input competitors produce **3.28× / 3.06×** ratios (before first
output / during the stream). Their control/load sample and overlap prerequisites
are satisfied and no material heartbeat delay overlaps those comparisons.
Ollama's result is consistent with its deliberately isolated single runner slot;
this experiment does not identify the complete internal scheduler cause. SGLang's
mixed-load ratios are at most **1.54×**, passing the same limit. These are bounded
deployment outcomes, not a cross-platform engine ranking.

SGLang automatic tool calls pass; its forced-tool diagnostics still fail the
required-city expectation with empty arguments. They remain separate diagnostics
under the existing tool check.

## Cancellation and cleanup

The independent server-stop verdict passes 3/3 native aborts for vLLM and SGLang.
vllm termination delays: 13.45, 13.21, 13.35 ms.
sglang termination delays: 41.44, 44.51, 63.32 ms.
Ollama's native server-stop verdict remains inconclusive: exclusive slot reuse
is capacity-release evidence, not a native abort reason or exact termination time.
This is a sub-result of the failed limits/answer category, additional to the two
top-level inconclusive checks in its table row. All three preserve the independent
answer failure; native termination does not overwrite it. Physical GPU memory
deallocation remains unverified.

All trial servers and the OTLP receiver were stopped before finishing; all three
trial server exit codes were zero. The original Ollama daemon remained unloaded.
Giraffe's lightweight UI was started afterward to inspect the reports. The UI
shows `2 / 108 requests affected` for Ollama buckets and `0 / 150` for the passing
first-output check, with no browser console errors.

## Evidence locations

The raw reports remain local under `runs/`:

- `ollama-heartbeat-fixed-20261013-complete-all/report.json`
- `vllm-heartbeat-fixed-20261013-complete-all/report.json`
- `sglang-heartbeat-fixed-20261013-complete-all/report.json`

The parent workspace's ignored `artifacts/heartbeat-fix-validation-20261013-complete/`
contains full configs, process/memory logs, native terminal traces, automated-test
output and analysis. Source hashes checked after all runs match the files checked
before startup. Raw request artifacts and local model files are not committed.
