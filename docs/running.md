# Running Giraffe and comparing baselines

[Documentation index](README.md) · [Installation](../README.md)

Start your inference server separately. Giraffe connects to an existing
OpenAI-compatible text endpoint; it does not start the inference engine.

## Start your run

This example assumes Ollama already serves `llama3.2:3b`. Change the URL and model
for your deployment:

```sh
giraffe run --url http://127.0.0.1:11434/v1 \
  --model llama3.2:3b --checks access,serving,correctness \
  --concurrency 1 --html
```

It selects endpoint access, serving success and known-answer correctness, using
built-in prompts with one request at a time. Results appear in the terminal and
in `runs/<timestamp>/report.json` and `report.html`. The command exits when done.

For a saved configuration, edit an example's endpoint, model and limits first:

```sh
giraffe validate --config examples/local.yaml
giraffe preview --config examples/local.yaml
giraffe run --config examples/local.yaml --html
```

Validate and preview send no test traffic. Preview shows effective settings,
migration notes, request estimates and limit warnings. Examples may select more
work than the three-check command above.

`--concurrency N` sets both the suite's simultaneous-request ceiling and default
check parallelism. Use `--simultaneous-limit` and `--default-concurrency` to set
them separately. Check-specific settings and Load test pattern levels can use
fewer requests. See [workload limits](workloads.md#configure-and-run).

## Browser UI

```sh
giraffe ui
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Follow
**Connect → Checks → Launch**, review the plan, then choose **Launch**. The UI uses
the same runner and scoring as the CLI. It starts on loopback and does not begin
inference automatically. Keep the terminal open; **Ctrl+C** stops the UI server.

| Flag | Purpose |
| --- | --- |
| `--port PORT` | Choose a different port; default `8765`. |
| `--runs-dir PATH` | Report/history directory; default `runs` relative to the working directory. |
| `--config FILE` | Prefill from YAML/JSON, including the selected checks. |

For example:

```sh
giraffe ui --port 8768 --runs-dir runs/local --config examples/local.yaml
```

Paste the endpoint in **Connect** and choose **Test connection**. If model listing
returns one model, it is selected automatically; otherwise choose or enter a
model. An explicitly entered model skips discovery. The test sends at most one
short completion with a 32-token cap and a 15-second total timeout. It checks for
a usable response, not accuracy or load capacity, and creates no run report.
Editing the endpoint, model or authentication clears the result. A manually
specified model can be used without a successful connection test.

**Checks** opens on the selected checks. Without `--config`, it starts with
endpoint response and answer correctness. Use **Add checks** to search the
catalog, the **i** button for details and **Settings** for a check's controls.
The review shows planned requests, run limits and separate evaluator calls.
Plans update as settings change; **Launch** requires a current plan.

Expand **Edit advanced configuration, import or export** to open **Advanced
editor**, **Load saved setup**, **Save setup** or **Copy terminal command**.
Apply JSON changes before running, exporting or copying a command. Draft setups
may have blank models and no checks; running and copying a command require a
model for every target and at least one selected check.

All UI assets are bundled locally. One run is active at a time. Closing the
browser leaves the server and run active; use **Stop run** to stop work and retain
partial results.

## Credentials

For an authenticated endpoint, **API key (optional)** accepts a pasted key in a
masked field. The browser keeps it in this tab's memory and sends it to the local
runner for connection tests and runs. Reloading or changing the endpoint clears
it; **Clear key** removes it explicitly. A running test keeps its starting key.

Pasted keys are not written to saved configurations, reports, baselines or copied
commands. Saved setups refer to `GIRAFFE_API_KEY` or their existing `api_key_env`.
Paste the key again when reopening, or use **Use an environment variable instead**.
A copied CLI command requires that variable in its terminal environment.

The CLI accepts `--api-key-env VARIABLE_NAME`; configurations use
`targets[].api_key_env`. Set the named variable before running. Separate metrics
and evaluator endpoints use their own credentials. Open local endpoints need no key.

## Results and stopping

CLI runs print their request, simultaneous, duration, timeout and output settings
before traffic. **Ctrl+C** stops new requests and saves partial results after the
suite starts. Preflight failures do not produce a run report. Ordinary runs do not
restart the service; cold-start checks require the explicit cold-start command.

| Exit code | Meaning |
| --- | --- |
| `0` | Pass |
| `1` | Fail |
| `2` | Inconclusive |
| `3` | Blocked (including preflight failure) |
| `4` | Usage or configuration error |

Existing CLI reports under the UI's runs directory appear in run history. A
completed run can still have failed acceptance checks. Inspect the per-check and
request evidence, or generate HTML from saved results without sending traffic:

```sh
giraffe report runs/before --html
giraffe inspect runs/before/report.json
```

## Compare with a baseline

In the UI, open a completed run and choose **Save as reference baseline**. Give it
a name and choose **Save baseline**. The run then offers **Compare a new run with
this baseline**, which opens a new run using that reference and the saved setup.
Review the settings before choosing **Launch**.

From the CLI, save a completed run with `giraffe baseline save`, then pass the
saved file to `giraffe run --baseline`:

Edit `examples/local.yaml` for your endpoint and acceptance limits. Use the same
configuration for both runs:

```sh
giraffe run --config examples/local.yaml --output runs/before
giraffe baseline save runs/before baselines/local.json

# After changing your deployment:
giraffe run --config examples/local.yaml --baseline baselines/local.json \
  --output runs/after --html
```

Saving is explicit. Later runs do not replace the baseline; replacement requires
`baseline save ... --replace`. A baseline may contain failures. Absolute checks
evaluate the current run while comparison reports changes against the reference.
Incompatible workloads leave metric comparison inconclusive. See
[deployment and comparison evidence](workloads.md#deployment-metadata).


## Previous wrong answers

Planning warns about recent failed scores for matching built-in correctness fixtures
or saved questions. The UI links up to three failed answers per question and offers
**Try one**, preselecting the target that failed. Existing retention rules still apply;
a link may show scoring evidence without a retained answer body. No request is sent
until you submit the trial, and pass criteria never change automatically.

The last 30 days of compact history are matched by question, expected answer,
scorer/evaluator, request and load settings, endpoint/model and nonempty configured
deployment metadata. A newer matching run without failed scores clears an older
warning. Unknown deployment metadata, custom fixture files, missing/stale history
and old reports without summaries stay unknown. Runtime continuity is unverified,
and a prior wrong answer does not establish that the next attempt will fail.
