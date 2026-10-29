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

Generate a saved configuration, then review its endpoint, model and limits:

```sh
giraffe init --engine ollama --url http://127.0.0.1:11434/v1
giraffe validate --config giraffe.yaml
giraffe preview --config giraffe.yaml
giraffe run --config giraffe.yaml --html
```

Validate and preview send no test traffic. Preview shows effective settings,
request estimates and limit warnings. Current examples use the current schema;
older configurations can also show migration notes.

Use `--simultaneous-limit` to set the suite's simultaneous-request ceiling and
`--default-concurrency` for default check parallelism. Check-specific settings
and Load test pattern levels can use fewer requests. See
[workload limits](workloads.md#configure-and-run).

`--json` is an alias of `--output-format json` for machine-readable CLI output.
`--checks json` selects the structured-JSON check. Normal text output
shows a final verdict, selected check results and one list of unselected checks.
Use `--verbose` to include diagnostic stages and RAM measurements; JSON reports
retain the measurements regardless of verbosity.

## Browser UI

```sh
giraffe ui
```

Open the token link printed by `giraffe ui`. Follow
**Connect → Checks → Launch**, review the plan, then choose **Launch**. The UI uses
the same runner and scoring as the CLI. It starts on loopback and does not begin
inference automatically. Keep the terminal open; **Ctrl+C** stops the UI server.

| Flag | Purpose |
| --- | --- |
| `--host HOST` | Bind address; default `127.0.0.1`. Non-loopback binding prints a warning. |
| `--no-browser` | Print the access link; no browser is opened automatically. |
| `--allow-restart-command` | Permit cold-start execution. Configurations may retain unused restart commands without it. |
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
**Save baseline**.

To compare a run that already finished, open that run and choose **Compare with
baseline**. Select the saved reference and choose **Compare saved results**.
Giraffe uses the recorded measurements without contacting the endpoint. The
comparison stays with the report after reload and appears in JSON/HTML exports.
Use **Change baseline** to compare the same measurements against another saved
reference. Saved baselines and request evidence remain unchanged; incompatible
workloads or insufficient evidence still produce inconclusive results.

The local API provides the same operation: `POST /api/runs/{run_id}/compare`
with `{"baseline_id": "<saved-baseline-id>"}`. List references with
`GET /api/baselines`. Only completed reports can be compared.

You can also choose **Compare a new run with this baseline** to open a new run
using that reference and the saved setup. Review settings before **Launch**.

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

## CI output and exit policy

```sh
giraffe run --config giraffe.yaml --quiet --fail-on inconclusive \
  --junit results/junit.xml --summary-md results/summary.md --output runs/ci
giraffe report runs/ci --junit results/junit.xml --summary-md results/summary.md
```

JUnit has one testcase per selected check and target, with failed assertions,
errors for blocked checks and skipped reasons for unfinished evidence. Each
target's suite properties contain its model, credential-redacted endpoint and
run ID; testcase output contains the display title. Markdown includes the
verdict, check table, baseline delta and report path. Blocked/preflight exits also
write the requested CI files, including the blocking reason. HTML export remains
optional.

The default `--fail-on inconclusive` preserves exit codes 0/1/2 for
pass/fail/inconclusive. `--fail-on fail` accepts an inconclusive **run verdict**
with exit 0. `--fail-on never` accepts both failed and inconclusive run verdicts,
including baseline regressions, with exit 0. Each accepted verdict prints a
stderr note. Model selection errors remain 2; blocked/preflight failures remain
3; usage, configuration, export and Giraffe errors remain errors. Cancellation retains exit 130. The verdict saved in
reports and CI exports is unchanged by the exit policy.

`--quiet` prints only the final verdict block (or final JSON object) on stdout.
It suppresses progress on stderr; warnings and deprecation notices remain.

## Common and advanced CLI options

`giraffe run --help` shows common options. `giraffe run --help-all` also shows the
advanced group. Every existing flag remains accepted; no setting moved to
config-only. These options moved from ordinary help into the advanced group:

- `--manifest`, `--no-simultaneous-limit`, `--no-max-requests`, `--no-max-duration`,
  `--max-memory`, `--context-limit`, `--samples`, `--sustained-seconds`,
  `--stop-after-errors`, `--unresponsive-after-failures`, `--unresponsive-for`.
- `--deployment-file`, `--metrics-url`, `--host-metrics-url`,
  `--metrics-api-key-env`, `--metrics-profile`, `--overlap-models`, `--fixtures`,
  `--request-pack`, `--retention`, `--ca-bundle`, `--proxy`.
- `--first-output-ms`, `--latency-ms`, `--stream-gap-ms`,
  `--min-output-tokens-per-second`, `--max-error-rate`, `--min-correctness`,
  `--regression-percent`, `--save-config`.
- `--max-total-storage`, `--min-free-disk`, `--max-run-storage`,
  `--auto-cleanup-keep-latest`, `--auto-cleanup-older-than`,
  `--auto-cleanup-max-total`, `--runs-dir`.

`--max-concurrency` and `--simultaneous-limit` are aliases for the suite request
ceiling. `--default-concurrency` sets per-check parallelism. The hidden legacy
`--concurrency` still sets both and prints a migration note. Hidden
`--structured-output`, `--tool-calling` and `--metrics` still opt into JSON, tool
and GPU checks; use `--checks json,tools,gpu` for explicit selection.
`--nonstream` remains an alias with a deprecation notice; use `--no-stream`.
`--json` remains an alias for `--output-format json`.

Manage reports with `giraffe runs list`, `giraffe runs cleanup` and
`giraffe runs keep RUN_DIR`. The old `storage`, `cleanup` and `keep` commands
remain hidden aliases and print a deprecation notice. Cleanup is still a preview
until `--yes` is supplied.

## Local UI access and containers

Each UI launch generates a new access token. Opening `/?token=...` exchanges it
for an HttpOnly, SameSite=Strict cookie named `giraffe_token_<port>` and redirects
to `/`. Distinct names let two UI instances on different ports stay signed in.
All `/api/` routes,
including bootstrap, event polling and downloads, require that cookie or an
`X-Giraffe-Token` header. A plain report download link works after opening the
access link. For curl, send `-H 'X-Giraffe-Token: YOUR_TOKEN'`. Static assets are
public; opening `/` without authentication shows instructions to open the access
link. Keep that link private. Cold-start execution and requests setting a restart
target require `giraffe ui --allow-restart-command`. Unused restart commands may
remain in configurations for ordinary runs, editing, preview and copied CLI
commands. Ordinary CLI runs retain their existing explicit cold-start flow.

Cookies are scoped to the host, not the port: other local HTTP servers on the
same host receive these cookies when you visit them. Port-specific names prevent
overwriting another UI instance's cookie; they do not isolate local servers from
each other. On a shared machine, avoid visiting untrusted servers on the same
host while signed in, or use a separate browser session or header-only API
access. See [cookie origin rules](https://developer.mozilla.org/en-US/docs/Web/Security/Defenses/Same-origin_policy).

The UI sends a Content-Security-Policy restricting scripts and connections to its
own origin. Web configurations retain environment-variable credential references;
there is no environment-variable name allowlist.

The container runs as a non-root user. Build and expose the UI with:

```sh
docker build -t giraffe .
docker run --rm -p 127.0.0.1:8765:8765 giraffe ui --host 0.0.0.0
```

Wildcard binds (`0.0.0.0` or `::`) print a token link using `localhost`. The Docker
example publishes the port only on the host's loopback interface. To expose it
beyond localhost, deliberately change the published address and open the token
link using your host's address; use a trusted network. Non-loopback binding
prints a warning. Anonymous visits to `/` show access instructions; API requests
and report data still require authentication. Mount a writable directory at
`/work` to persist runs across containers.

## Optional Hugging Face tokenizer

The core install uses built-in tiktoken sizing when automatic model-tokenizer
resolution is unavailable. It includes FastAPI and Uvicorn for the UI, without
transformers or torch. Install `giraffe-llm[hf-tokenizer]` to use a model's own
tokenizer. Explicitly configuring an HF tokenizer without the extra fails with
an install hint; automatic resolution silently falls back to built-in sizing.
