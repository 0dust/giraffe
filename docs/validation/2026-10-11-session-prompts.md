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
