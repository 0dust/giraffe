# Sample reports

Download a ZIP, extract it, and open its `report.html` in a browser. Each report
includes the matching `report.json` and run configuration; viewing it sends no
inference requests. These are downloadable exports, not a hosted report service.

## Load comparison

[Download the load comparison](load-comparison.zip?raw=true) · [Run configuration](load-config.json)

Open `comparison/report.html`; the original runs are in `first-sweep/` and
`confirmation/`. These November 17 runs used Qwen2.5 1.5B with Ollama 0.12.10 on
a shared 8 GB M1 Mac. Concurrency 1 passed in both sweeps. All 840 scored answers
were correct; higher-load timing misses overlapped client scheduling delays, so
both complete runs remain inconclusive. The comparison is an identical repeat,
not a deployment-change experiment.

## Browser UI demo

[Download the GIF's run](browser-ui-demo.zip?raw=true)

The November 18 GIF shows a separate, brief run: 17/17 scored answers passed at
concurrency 1, 2, and 4. Its 12-second stages and three-response minimum demonstrate
the UI; they do not establish serving capacity. Playback is condensed.

## Token timing

[Download the performance run](token-timing.zip?raw=true)

The performance screenshots come from a separate November 18 run on Giraffe
`b506816`, using Qwen2.5 1.5B and Ollama 0.34.1 on the same M1 Mac. Three-minute
stages at concurrency 1, 2, and 4 passed with 307, 326, and 521 correct responses.
All 1,154 scored responses have token-boundary evidence for TPOT and ITL.
Generation pace retains the report's client-observed estimate label. One
180.64 ms client scheduling delay was recorded; its cause is unknown.

The older load comparison above remains a different experiment. Neither run
establishes general model quality or production capacity.

## Request evidence

[Complete matched context requests](context-request-evidence.json) retain the
synthetic inputs, expected values, recorded responses, and scores behind the
context-4096 and context-8192 screenshots from the Qwen2.5 7B / Colab T4 experiment.

## Provenance

[Manifest](manifest.json) records source and distributed-file hashes, run IDs,
capture revisions, and image origins. Personal hostnames and absolute local CLI
paths are replaced in the report copies. Requests, scores, verdicts, and timing
measurements are unchanged. The screenshots and GIF are unchanged captures.
