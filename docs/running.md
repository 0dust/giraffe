# Running Giraffe and comparing baselines

[Documentation index](README.md) · [Installation](../README.md)

Start your inference server separately. Giraffe connects to an existing
OpenAI-compatible text endpoint; it does not start the inference engine.

## Start your run

This example assumes Ollama already serves a model. Change the URL for your deployment:

```sh
giraffe run --url http://127.0.0.1:11434/v1 --html
```

The model is auto-detected when only one is served. If several are served, add
`--model MODEL_ID`; if discovery is unsupported, supply the model explicitly.
The shared CLI and UI default checks are `access, serving, correctness, first_output`:
endpoint access, serving success, known-answer correctness and time to the first
useful answer. The default p95 first-output limit is 10,000 ms; override it with
`--first-output-ms` or YAML `limits.first_output_ms` for your service.
Results appear in the terminal and in `runs/<timestamp>/report.json` and
`report.html`. The command exits when done.

For a saved configuration, edit an example's endpoint, model and limits first:

```sh
giraffe validate --config examples/local.yaml
giraffe preview --config examples/local.yaml
giraffe run --config examples/local.yaml --html
```

Validate and preview send no test traffic. Preview shows effective settings,
request estimates and limit warnings. Current examples use the current schema;
older configurations can also show migration notes.

Use `--simultaneous-limit` to set the suite's simultaneous-request ceiling and
`--default-concurrency` for default check parallelism. Check-specific settings
and Load test pattern levels can use fewer requests. See
[workload limits](workloads.md#configure-and-run).

`--json` is an alias of `--output-format json` for machine-readable CLI output.
`--structured-output` instead adds the structured-JSON check. Normal text output
shows a final verdict, selected check results and one list of unselected checks.
Use `--verbose` to include diagnostic stages and RAM measurements; JSON reports
retain the measurements regardless of verbosity.

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

Paste the endpoint in **Connect** and choose **Connect** (**Reconnect** after a
successful test). If model listing
returns one model, it is selected automatically; otherwise choose or enter a
model. An explicitly entered model skips discovery. The test sends at most one
short completion with a 32-token cap and a 15-second total timeout. It checks for
a usable response, not accuracy or load capacity, and creates no run report.
Editing the endpoint, model or authentication clears the result. A manually
specified model can be used without a successful connection test.

**Checks** initially shows the full catalog. Without `--config`, `access`,
`serving`, `correctness` and `first_output` are selected. Use **Selected** to show only
selected checks or **All checks** to browse and search the catalog. Use the **i**
button for details and **Settings** for a check's controls.
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

For an authenticated endpoint, **API key** accepts a pasted key in a
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

By default, `giraffe run` sends a bounded preflight completion request per target
before printing its request, simultaneous, duration, timeout and output settings.
Those settings appear before suite traffic. **Ctrl+C** stops new requests and
saves partial results after the suite starts. A blocking preflight failure does
not produce a run report. Ordinary runs do not restart the service; cold-start
checks require the explicit cold-start command.

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

In the UI, open a completed run. Fully passing runs offer **Save baseline**; other
completed runs offer **Save as reference baseline**. Give it a name and choose
**Save baseline**. Then choose **Compare a new run with this baseline** at the
bottom of the report, or **Compare a new run** when shown at the top. Both open a
new run using that reference and the saved setup. Review the settings before
choosing **Launch**.

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

## Run provenance

Reports and `giraffe export` preserve the launch source alongside the effective
configuration. CLI runs include the original argument list (after shell expansion,
excluding the executable); argument order and overrides are preserved. Credentials,
unknown option values and unrecognized positional content are redacted. Environment
variable names may be recorded, but Giraffe never expands them into this list or
copies stdin/configuration-file contents into it. Treat redacted arguments as
provenance, not a ready-to-execute shell command.

Runs from the local browser UI or HTTP API are identified as `web_api`, and direct
Python calls as `python_api`, with no invented command. Older reports have unknown
launch provenance. Replaying a report records the new invocation; it does not claim
to have used the original command. Server launch metadata remains separate.
