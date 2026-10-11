# Backend cancellation within Limits and cancellation

`cancellation` is one check for every runtime. It includes the existing output
cap, stop sequence, client disconnect, deadline and follow-up probes. Enable
backend verification in that check's custom settings; there is no separate
SGLang, vLLM or Ollama test in the catalog.

The category reports two independently scored results in JSON, HTML and the UI:

- **Cancellation — verify server stopped** uses matching native terminal evidence.
  An answer mismatch or failed follow-up cannot overwrite an observed abort.
- **Cancellation — verify answer** scores completed stop/follow-up answers.
  Intentionally cancelled partial outputs are not graded.

They are stored in `metrics.cancellation_results.server_stopped` and
`metrics.cancellation_results.answer`. The category's overall result still includes
output caps, client interruption and follow-up health. A category failure does not
mean both cancellation results failed. Backend verification disabled is explicitly
`unverified` for server termination.

```yaml
checks: [cancellation]
test_options:
  cancellation:
    backend_verification: required
    samples: 3
    cancel_after_chunks: 5
    cleanup_seconds: 1
    trace_wait_seconds: 10
    trace_file: /absolute/path/terminal-traces.jsonl
```

The UI exposes these fields under **Limits and cancellation → Custom settings**.
Backend verification defaults to `off` because the endpoint alone cannot supply
the required evidence. Reports explicitly say `unverified` when it is off. When
it is required, missing evidence makes the check inconclusive or blocked; it
cannot pass on a client disconnect alone. Existing limit or answer failures
remain failures even when backend verification passes.

The adapter uses `target.metrics_profile`, `target.deployment.discovery` and
`target.deployment.runtime.engine`. These declarations must agree. Target names
are arbitrary labels and never select the backend. This version requires a
loopback target and timestamps on the same host. Run each runtime separately on
memory-constrained machines.

## Evidence and verdicts

Each probe requests numbers 1–100, with a 128-token ceiling bounded by the run's
global output limit. Giraffe closes its streaming connection after five useful
answer chunks, then requests a short follow-up. Budgets and stop conditions apply
to every request, including references and follow-ups.

| Runtime | Evidence | What the evidence establishes |
| --- | --- | --- |
| SGLang | Native `sglang::request` terminal OTLP span | The exact response request ID and model terminated with `abort` before the cleanup deadline. |
| vLLM V1 | Optional scheduler observer after native request removal/cache release | The exact response request ID terminated with `FINISHED_ABORTED` before the deadline. |
| Ollama | Native debug cache-slot admissions, single slot, uncancelled reference | The same exclusive runner slot was reused before the deadline with less generated cache than the capped reference. Capacity release can pass, but **verify server stopped stays inconclusive** because the native abort reason and exact termination time are unavailable. |

SGLang/vLLM terminal events must have a fresh start, end after the recorded client
cancellation, and match the response ID/model. A terminal event after the cleanup
deadline fails. Natural completion within the deadline is inconclusive because
it may have raced cancellation. Missing, stale, mismatched, malformed or merely
"abort requested" evidence cannot pass. Export delay does not extend the cleanup
deadline: native finish time decides; `trace_wait_seconds` only allows collection.

Ollama additionally runs the identical deterministic request without cancellation
before each probe. The reference must finish at its token cap; references that stop naturally,
interleaved admissions, another slot, missing logs, or late slot reuse leave the
backend result inconclusive. A late follow-up admission does not by itself prove
the earlier generation continued until that time. The adapter requires a dedicated
single-slot server and an explicit `exclusive: true` declaration. Its correlation
is the exclusive probe window and native slot ID, not Ollama's synthetic OpenAI
response ID. Do not use it against a shared production server.

Every probe retains its request/follow-up IDs, outcome and allowlisted evidence
in `checks[id=cancellation].metrics.backend_cancellation`. Passing probes must
also have valid follow-up inference. Raw evidence does not contain prompts,
answers, headers or credentials. Physical GPU memory deallocation and work in
external KV connectors are not certified.

## SGLang setup

Install the optional receiver dependency in Giraffe's environment:

```sh
uv pip install --python .venv/bin/python -e '.[sglang-tracing]'
.venv/bin/giraffe sglang-traces --output /absolute/path/terminal-traces.jsonl --port 4318
```

The receiver binds loopback, accepts bounded OTLP/HTTP protobuf and creates a
fresh output file. Add tracing to your existing bounded SGLang launch:

```sh
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf
export SGLANG_OTLP_EXPORTER_SCHEDULE_DELAY_MILLIS=200
export SGLANG_TRACE_LEVEL=1
# Append to your existing SGLang command:
# --enable-trace --otlp-traces-endpoint http://127.0.0.1:4318/v1/traces
```

SGLang also needs the OpenTelemetry SDK and OTLP exporter in its own environment.
Set the target's `metrics_profile: sglang` and the `trace_file` above. No server
patch is required. The native span ends when the tokenizer manager receives the
scheduler's finished result, rather than when cancellation is merely dispatched.

## vLLM setup

The optional observer is a scheduler subclass, validated with **vLLM 0.30.0 V1**
and its Metal platform plugin. It is not imported by ordinary Giraffe runs.
Make Giraffe's `src` directory importable in the vLLM server environment, create a
fresh empty evidence file, then append the scheduler option to your bounded
launch:

```sh
export PYTHONPATH=/absolute/path/to/giraffe/src
export GIRAFFE_TRACE_FILE=/absolute/path/vllm-terminal-traces.jsonl
export GIRAFFE_TRACE_MODEL=your-served-model-name
# Create a NEW file; choose another path if this already exists.
(set -C; : > "$GIRAFFE_TRACE_FILE")
# Append to your existing vllm serve command:
# --scheduler-cls giraffe.vllm_cancellation_hooks.CancellationScheduler
```

Set `metrics_profile: vllm-v1` and point `trace_file` to that file. Keep the model
label equal to the served alias. The observer calls the native scheduler first
and writes only after the request has been removed. Delayed cleanup is not
accepted as completed cleanup. The v0.30 internal-ID suffix is checked explicitly;
unrecognized IDs produce no passing evidence. It does not change how disconnects
request cancellation.

The scheduler API is internal and version-sensitive. Revalidate the observer
before using another vLLM version. Selecting this subclass disables asynchronous
scheduling; performance results describe that instrumented deployment. Remove the
option/environment variables to return to the original scheduler.

## Ollama setup

Validated against the installed **Ollama 0.12.10 Go runner** debug format. Use a
separate local port with one slot and the existing model store:

```sh
OLLAMA_HOST=127.0.0.1:11435 OLLAMA_DEBUG=1 OLLAMA_NUM_PARALLEL=1 \
OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_CONTEXT_LENGTH=1024 \
ollama serve > /absolute/path/ollama-server.log 2>&1
```

Use these target settings, plus the shared configuration above:

```yaml
targets:
  - name: local-llama
    url: http://127.0.0.1:11435/v1
    model: llama3.2:3b
    metrics_profile: ollama
    deployment:
      runtime: {engine: ollama}
      serving: {parallelism: 1, exclusive: true}
      discovery: ollama
test_options:
  cancellation:
    backend_verification: required
    server_log: /absolute/path/ollama-server.log
    cleanup_seconds: 1
```

The adapter verifies the port and the resolved `Parallel:1` in the native
runner load-commit event, ignores
old admissions, and reads bounded incremental updates. The next native cache
admission can only use the previous slot after the runner clears the previous
sequence and releases its semaphore. Cache growth is compared with the capped
reference to reject a request that already generated the full reference length.
The cancelled answer must match the reference prefix. The observer cannot identify
an early natural-EOS race; a pass establishes bounded capacity reuse, not the
cause of termination. Unknown log
formats leave the result inconclusive; no Ollama server patch is required.

## Source boundaries

- [SGLang terminal handling](https://github.com/sgl-project/sglang/blob/44bc478863052efdc455b0282b49830f68fdefb3/python/sglang/srt/managers/tokenizer_manager.py)
- [vLLM scheduler finish and release](https://github.com/vllm-project/vllm/blob/v0.30.0/vllm/v1/core/sched/scheduler.py)
- [vLLM internal request ID assignment](https://github.com/vllm-project/vllm/blob/v0.30.0/vllm/v1/engine/input_processor.py)
- [Ollama runner sequence removal and semaphore release](https://github.com/ollama/ollama/blob/v0.12.10/runner/llamarunner/runner.go)
- [Ollama native cache admission logs](https://github.com/ollama/ollama/blob/v0.12.10/runner/llamarunner/cache.go)
