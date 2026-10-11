# SGLang backend cancellation

`sglang_cancellation` is an opt-in check for a **same-host SGLang endpoint**.
The existing `cancellation` check still tests output limits, stop sequences,
client disconnect/deadline behavior and follow-up inference. It does not prove
that backend generation terminated.

The new check waits for native SGLang terminal request traces. It does not modify
SGLang or infer termination from abort-dispatch counters or aggregate running
request counts.

## Setup

Install the optional receiver dependency in Giraffe:

```sh
uv sync --extra sglang-tracing
.venv/bin/giraffe sglang-traces --output /tmp/sglang-terminal.jsonl
```

Leave the receiver running. It binds only `127.0.0.1:4318`, accepts uncompressed
OTLP/HTTP protobuf at `/v1/traces`, and requires a **new output file**. It saves
only request ID, model, trace ID, terminal reason and timestamps. Prompt/response
bodies and arbitrary trace attributes are discarded. Body, record and total-file
limits are 2 MiB, 16 KiB and 16 MiB; start a fresh receiver/file when full.

In the SGLang environment, install the native tracing dependencies:

```sh
python -m pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http opentelemetry-exporter-otlp-proto-grpc
```

Restart your existing SGLang launch command with these additional settings,
retaining its model, backend and memory limits:

```sh
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf
export SGLANG_OTLP_EXPORTER_SCHEDULE_DELAY_MILLIS=200
export SGLANG_TRACE_LEVEL=1
# Add to the existing python -m sglang.launch_server command:
# --enable-trace --otlp-traces-endpoint http://127.0.0.1:4318/v1/traces
```

Save a Giraffe configuration such as `sglang-cancellation.yaml`:

```yaml
targets:
  - name: sglang
    url: http://127.0.0.1:30000/v1
    model: llama3.2-1b
    metrics_profile: sglang
checks: [sglang_cancellation]
concurrency: 1
samples: 3
max_requests: 6
max_duration_seconds: 60
request_timeout_seconds: 20
max_output_tokens: 128
test_options:
  sglang_cancellation:
    trace_file: /tmp/sglang-terminal.jsonl
    cancel_after_chunks: 5
    cleanup_seconds: 1
    trace_wait_seconds: 10
```

```sh
.venv/bin/giraffe run --config sglang-cancellation.yaml --output runs/sglang-cancellation
```

The UI lists **SGLang backend cancellation** separately. Select it and use its
custom settings to enter the absolute trace file path. Declaration can come from
`metrics_profile: sglang`, `deployment.discovery: sglang`, or
`deployment.runtime.engine: sglang`; choosing a profile alone does not enable
tracing. The server and receiver must already be running.

## Workload and verdict

For each sample, Giraffe requests numbers from 1 to 100, with a maximum of 128
output tokens (clipped to the global limit). After five nonempty answer chunks,
it closes the stream and records the client timestamp and exact server response
ID. Role-only, reasoning-only and whitespace-only chunks do not trigger cancellation.
Each probe then runs a short follow-up request to verify usable inference; its
exact wording or capitalization is not scored.

The receiver extracts `sglang::request` spans containing `gen_ai.request.id`,
`gen_ai.response.model` and `gen_ai.response.finish_reasons`. The check requires
an exact ID/model match with fresh start/end timestamps from this probe.
`termination_ms` is the native span end minus the client disconnect timestamp;
the exporter arrival time does not determine the cleanup verdict.

| Observation | Verdict |
| --- | --- |
| Matching terminal `abort` within `cleanup_seconds`, successful follow-up, every sample covered | Pass |
| Matching terminal result after the cleanup deadline, including ordinary `length`/`stop` completion | Fail |
| Follow-up inference fails | Fail |
| Missing trace, unsupported trace shape, stale/wrong ID, request finishes before cancellation, natural completion races within the deadline, or incomplete budget coverage | Inconclusive |
| Target is not declared SGLang or endpoint is not loopback | Blocked |

Defaults are five chunks, five seconds for cleanup, and ten seconds waiting for
trace export. Configure the cleanup bound for the deployment; export buffering
needs its own observation allowance. Request and duration budgets still apply.
Missing evidence never passes, and an observed failure is retained when other
samples are incomplete. Fewer samples than `limits.min_samples` are marked low
confidence. The local clock must be shared: a loopback proxy forwarding to a
different machine is outside this check's supported scope.

## Verified runtime and limitations

Validated on October 11, 2026 with SGLang source
`44bc478863052efdc455b0282b49830f68fdefb3`, Apple M1 / 8 GB, the MLX backend and
the existing Llama-3.2-1B-Instruct 4-bit model. These trace fields are a
version-specific contract; unavailable fields produce inconclusive results.

In that source, the tokenizer manager
[finishes the native request span upon processing the scheduler's terminal output](https://github.com/sgl-project/sglang/blob/44bc478863052efdc455b0282b49830f68fdefb3/python/sglang/srt/managers/tokenizer_manager.py).
The scheduler's normal decode path
[processes finished requests and releases their KV allocation before sending terminal output](https://github.com/sgl-project/sglang/blob/44bc478863052efdc455b0282b49830f68fdefb3/python/sglang/srt/managers/scheduler_components/batch_result_processor.py).
This is runtime-reported terminal evidence. It does not measure physical device
memory reclamation, require prefix-cache eviction, or certify other replicas.

The final live run passed six cancellation probes and six follow-ups. Matching
native `abort` events ended 47.8–58.6 ms after disconnect, within the one-second
cleanup bound. A temporary proxy deliberately drained the upstream response
after Giraffe disconnected: both requests continued for 2.95–3.12 seconds, ended
with `length`, and correctly failed despite successful follow-ups. Pointing
Giraffe at an empty trace file produced an inconclusive result despite a working
endpoint. The complete automated suite passed: 522 tests, including 36 new cases.
