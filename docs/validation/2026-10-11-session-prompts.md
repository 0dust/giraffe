# Clearer multi-turn repeat prompts

The default first turn now asks: "Suggest a name for a bakery. Reply with only
the name, without quotation marks or a trailing full stop." The follow-up is
"Repeat exactly the above answer."

Both fixed-history and live-history modes still run. Repeat answers retain exact
scoring with whitespace normalization only. The old follow-up remains scored for
saved configurations. Prompt settings and generator source hashes prevent treating
the changed workload as identical to an earlier baseline.

## Verification

All **557 automated tests passed in 38.74 seconds**. Local HTTP integration tests
cover both prompt versions, concurrent fixed/live histories and stopping after an
incorrect repeat. The new-prompt regressions failed before implementation and
passed afterward. Ruff and `git diff --check` passed.

Focused live runs used the existing bounded runtime setups, one loaded engine at
a time, with two fixed and two live conversations per runtime:

| Runtime and model | Repeat answers correct | Session result |
| --- | --- | --- |
| SGLang, Llama 3.2 1B Instruct, MLX 4-bit | 0/4 | fail |
| vLLM-Metal, same 1B model | 0/4 | fail |
| Ollama, Llama 3.2 3B, Q4_K_M | 4/4 | pass |

All eight session requests completed per runtime, without request errors or
timeouts. First answers had no quotation marks or trailing full stop. SGLang and
vLLM produced the same observed mismatches: fixed history `Golden Crust` became
`Golden Crust Bakery`; live history `Sweet Delights Bakery` was followed by that
name plus four additional names. Ollama repeated both supplied histories correctly.
These runs use different model sizes across Ollama and the other engines and do
not isolate an engine-specific cause.

The reports are local ignored artifacts:

- [`runs/sglang-session-prompts/report.json`](../../runs/sglang-session-prompts/report.json)
- [`runs/vllm-session-prompts/report.json`](../../runs/vllm-session-prompts/report.json)
- [`runs/ollama-session-prompts/report.json`](../../runs/ollama-session-prompts/report.json)

Fresh launchers, configurations, native logs and memory samples are retained in
`../artifacts/session-prompts-verified/`. No critical memory-pressure sample
occurred. All task-owned servers and the trace receiver stopped; the original
Ollama daemon remained running with no model loaded. SGLang required the bounded
supervisor's forced stop after graceful shutdown exceeded 15 seconds.

Simplification and source review ran sequentially in the main thread. No further
changes were needed. Dedicated code review was skipped: no Tier 1 review tool was
available, and this small fixture change did not meet Tier 2 escalation criteria.
Work remains on `aiswarya/addSGLangSupport`; no push or PR was made.

## Follow-up: Ollama with the same 1B source checkpoint

The earlier Ollama result used a different 3B model. This follow-up reused the
actual MLX checkpoint loaded by SGLang and vLLM:

- Repository: `mlx-community/Llama-3.2-1B-Instruct-4bit`.
- Revision: `08231374eeacb049a0eade7922910865b8fce912`.
- Original `model.safetensors` SHA-256:
  `35e396644bca888eec399f9c0f843ec7fa78b8f8c5e06841661be62b4edf96dd`.

Ollama cannot use this MLX-packed checkpoint directly. Its 146 weight tensors
were expanded in 128-row chunks with native `mlx.core.dequantize`, saved as F16
Safetensors, then imported with [Ollama's documented import workflow](https://docs.ollama.com/import).
There was no second quantization and no independently downloaded model.
Expansion retains the quantized checkpoint's values; it does not recover the
original model's lost precision. Ollama reported 1,235,814,432 parameters and F16.
The final model's GGUF weight layer SHA-256 was
`02a53ad2f722fe9ab7e0c94152a9b011c370843037b47a263e14d8c76bb9dba5`.

The [Ollama chat template](https://docs.ollama.com/modelfile#template) was configured
with the checkpoint's Llama headers, system metadata and beginning-of-sequence
token. Temperature was zero, repetition penalty one, context 1024 and per-turn
output cap 64. Fixed/live histories and prompts matched the earlier runs.

| Same-source 1B runtime | Fixed repeats | Live repeats | Overall sessions |
| --- | --- | --- | --- |
| SGLang, original MLX 4-bit | 0/2 correct | 0/2 correct | fail |
| vLLM-Metal, original MLX 4-bit | 0/2 correct | 0/2 correct | fail |
| Ollama, expanded F16, complete chat template | 0/2 correct | 0/2 correct | fail |

All eight session requests completed without errors or timeouts. All eight
response bodies, input-token counts and local scores matched the earlier
SGLang/vLLM runs. Fixed repeats returned `Golden Crust Bakery` instead of
`Golden Crust`. Live repeats returned `Sweet Delights Bakery` followed by
`Bakehouse Bliss Bakery`, `Flour & Fancy Bakery`, `The Sweet Spot Bakery` and
`Cupcake Cove Bakery` instead of only the preceding answer.

This independently reproduces the observed answer failures through Ollama and
strongly supports shared model/prompt behavior rather than an engine-specific
explanation. It is a bounded reproduction, not proof that the engines have no
defects or that every configuration of this model will fail.

### Controls and cleanup

The raw import initially selected `{{ .Prompt }}`, which does not provide the
required Llama conversation format. That preliminary run is excluded from the
comparison. A second template omitted BOS because the importer was assumed to
add it; actual metadata showed `tokenizer.ggml.add_bos_token: false`. That run
passed 2/4 repeats and reported one fewer input token. Explicit BOS corrected the
format, matched input counts (58/77/78 by turn/history) and reproduced all failures.
This control demonstrates that formatting can change results for the same weights.

The initial whole-model conversion hit critical memory pressure and was stopped
by the guard. Chunked conversion completed without critical pressure, and the
final live run remained at normal pressure. Task-owned Ollama was unloaded and
stopped; the original daemon and its existing models were preserved, with no model
loaded. The imported 1B models are retained in the isolated trial model store.

Final report:
[`runs/ollama-same-1b-bos-template-sessions/report.json`](../../runs/ollama-same-1b-bos-template-sessions/report.json).
Configs, conversion provenance, scripts, model metadata, preliminary controls and
pressure logs are local ignored artifacts in
`../artifacts/ollama-1b-checkpoint-trial/`. No product code changed in this follow-up;
the earlier 557-test automated result remains the last code verification.
