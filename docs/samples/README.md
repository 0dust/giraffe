# Sample reports

Download a ZIP, extract it, and open its `report.html` in a browser. Each report
includes the matching `report.json` and run configuration; viewing it sends no
inference requests.

## Order extraction under load

[Download both full reports](order-extraction.zip?raw=true) ·
[Six complete prompts and expected answers](order-extraction-questions.json) ·
[Verified counts and late-request IDs](order-extraction-evidence.json)

An order-extraction app needs the correct `order_id`, `status`, and integer
`quantity` in its JSON response, within its response-time limit. This recorded
local demo checks both requirements while maintaining one and then four requests
in flight. Its six authored sample orders and eight-second deadline illustrate
an application requirement; they are not customer production traffic or a
customer service-level agreement.

The November 1, 2026 runs used Llama 3.2 1B Q8_0 with Ollama 0.12.10 on an 8 GB
M1 MacBook Air. The only recorded configuration difference was
`OLLAMA_NUM_PARALLEL=1` versus `4`. The model digest, complete prompts, JSON
schema, expected field values, temperature, seed, context, output cap, 90-second
load stages, and acceptance limits match. All six cases appear at each tested
load in both runs.

| Measured result | Before: one Ollama slot | After: four Ollama slots |
| --- | ---: | ---: |
| At one request in flight: correct and within eight seconds | 49 / 49 | 47 / 47 |
| At four requests in flight: correct JSON | 51 / 51 | 100 / 100 |
| At four requests in flight: responses over eight seconds | 9 / 51 | 0 / 100 |
| At four requests in flight: slowest completion | 10.21 s | 6.68 s |
| At four requests in flight: p95 completion | 9.28 s | 5.79 s |
| Four-request acceptance | Failed | Passed |

Every measured response must finish within eight seconds; p95 is a descriptive
measurement, not the acceptance rule. The duration-bounded stages complete
different numbers of requests. Both four-request groups have qualified timing
evidence. The after run's one-request group has a client-delay timing warning;
its acceptance still passed. Optional statistical comparisons retain their
warnings. These observations do not establish maximum capacity or a universal
effect of this setting.

Giraffe identified the failing requirement, retained the request evidence, and
compared the repeat against the saved baseline. The operator changed and
restarted Ollama; Giraffe did not tune the server. The saved tested-load comparison
records concurrency one still accepted and concurrency four newly accepted.

### Original product screenshots

| Before: load test failed | After: selected checks passed |
| --- | --- |
| ![Original Giraffe screen showing the order-extraction load failure](../assets/readme/order-extraction-before.png) | ![Original Giraffe screen showing the same checks passing after the change](../assets/readme/order-extraction-after.png) |

These are frames at 2.0 seconds in the original before/after screen recordings.
Only the browser chrome above the application was cropped. No UI text, results,
colors, or other product content was redrawn or altered. They show Giraffe
`6ea042908ac4cc0770c353fe6f1c0faf864ba5e0`, the revision used for these runs.

Open `before/report.html` and `after/report.html` from the ZIP. The full recorded
JSON, resolved configurations, and request pack are included. The HTML was
rendered from those saved reports with the PR review branch; the screenshots
retain the original UI. Only the personal hostname and JSON whitespace were
changed in the distributed JSON. The manifest records both original and
distributed hashes, recording hashes, frame times, and run IDs.

## Ollama context comparison

[All 45 matched recall cases](context-recall-evidence.json) ·
[Complete input and response for the illustrated request](context-request-evidence.json)

These November 17 runs used the same Qwen2.5 7B Q4_K_M model with Ollama 0.40.16
on a Colab T4. The server reported context 4096 without an override; the second
configuration set `OLLAMA_CONTEXT_LENGTH=8192`. All 45 quiet-phase recall requests
have matching prompt hashes across the two runs.

| Quiet-phase recall answers | Context 4096 | Context 8192 |
| --- | ---: | ---: |
| Correct | 26 / 45 | 44 / 45 |
| Wrong | 19 / 45 | 1 / 45 |

The same 18 medium/long cases changed from wrong to correct. One short recall
case failed at both settings. These are synthetic questions from one pass per
configuration, not an estimate of general model accuracy. Other checks in both
complete runs failed. The later loaded phase repeated these prompts and is not
an independent replication; it is excluded from the headline counts.

These records are retained as a synthetic test example. They are not a
production-workload comparison and are not used as the README's hero.

### Inspect one answer

The question asks for Entry 9239's badge code, `ZM-5744`. At context 4096 the
recorded answer was `NR-9239`, despite HTTP 200 and a normal stop. At context 8192
the same request returned `ZM-5744`.

**Context 4096 — wrong answer.**

![Expected ZM-5744; recorded NR-9239](../assets/readme/ollama-default-recall-medium-d0-0-ui.jpg)

**Context 8192 — correct answer.**

![Expected and recorded ZM-5744](../assets/readme/ollama-ctx8k-recall-medium-d0-0-ui.jpg)

These original UI captures show an excerpt of the input. The linked matched
request retains the full synthetic register. This corrected answer also took
longer; this example does not claim a speed improvement.

## Browser UI demo

[Download the GIF's run](browser-ui-demo.zip?raw=true)

The November 18 GIF shows three test conditions in a separate, brief run:
17/17 scored answers passed at concurrency 1, 2, and 4. Its 12-second stages and
three-response minimum demonstrate the UI; they do not establish serving
capacity. Playback is condensed to 10.8 seconds, with the live excerpt at 2.5x
speed and the completed report held for 3.5 seconds.

## Tool-call mismatch

[Recorded request and expected call](tool-call-evidence.json)

The prompt requests cancelled orders for customer `C-41442`. The model returns a
valid `search_orders` call with `C-41422`. The response protocol passed; the
expected-argument check failed. There was no assistant-text requirement.

This is one streamed request from the November 17 Qwen2.5 7B Q8_0 / llama.cpp /
Colab T4 experiment, not a general conclusion about Q8.
Giraffe checked the tool call without executing it.

## Token timing

[Download the performance run](token-timing.zip?raw=true)

The performance views come from a separate November 18 run on Giraffe `b506816`,
using Qwen2.5 1.5B and Ollama 0.34.1 on a shared 8 GB M1 Mac. Three-minute stages
at concurrency 1, 2, and 4 passed with 307, 326, and 521 correct responses.
All 1,154 scored responses have token-boundary evidence for TPOT and ITL.

![Performance measurements from the November 18 Ollama 0.34.1 run](../assets/readme/fresh-load-kpis.jpg)

This is the original screenshot. The downloadable report has updated measurement
notes; its JSON evidence and measurements are unchanged. Generation pace retains
the report's client-observed estimate label. One 180.64 ms client scheduling delay
was recorded; its cause is unknown.

![Actual and intended concurrency in the same November 18 run](../assets/readme/fresh-load-concurrency.jpg)

This repeated JSON extraction task is a local serving example. It does not
establish general model quality or production capacity.

## Load comparison

[Download the load comparison](load-comparison.zip?raw=true) · [Run configuration](load-config.json)

Open `comparison/report.html`; the original runs are in `first-sweep/` and
`confirmation/`. These November 17 runs used Qwen2.5 1.5B with Ollama 0.12.10 on
a shared 8 GB M1 Mac. Concurrency 1 passed in both sweeps. All 840 scored answers
were correct; higher-load timing misses overlapped client scheduling delays, so
both complete runs remain inconclusive. The comparison is an identical repeat,
not a deployment-change experiment.

![The November 17 comparison retains its inconclusive timing outcomes](../assets/readme/fresh-load-results.jpg)

This is a different experiment from the November 18 performance views above.

## Provenance

[Manifest](manifest.json) records source and distributed-file hashes, run IDs,
capture revisions, and image origins. Personal hostnames and absolute local CLI
paths are replaced in the report copies. Requests, scores, verdicts, and timing
measurements are unchanged.

The context screenshots are JPEG conversions at 1280 pixels wide. The original
GIF and performance screenshots retain their recorded content.
The token-timing ZIP has only a presentation update to
`report.html`; the manifest records its previous and current HTML hashes.
