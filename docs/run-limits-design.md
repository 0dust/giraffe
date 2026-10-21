# Run limits, Run plan and workload caps — design (issue #26), revision 2

This is the implementation contract for issue #26, reconciled with #45 (merged as **Concurrent load**). The user decided D1–D4 on 2026-10-20.

Revision 2 folds in an adversarial review and a feasibility review of revision 1. Line references point to `main` at 94818d6.

## 0. User decisions (do not relitigate)

- **D1 — Memory guardrail.** `max_memory_mb` is not one of the three suite limits. It appears in the Run limits section as a separate **Resource protection** row with its own checkbox: on by default, 4096 MiB of RAM growth. It also stays in the Run plan bounds.
- **D2 — Saved configs that omit a limit.** The omitted limit is now **Not set**, and the user sees a migration note. Explicit saved values stay enabled.
- **D3 — Request settings.** Request settings leave Run limits. Each check's panel shows the request settings that check actually uses, pre-filled with the effective default. Suite-wide defaults remain in JSON/CLI only.
- **D4 — The 256 cap on simultaneous requests.** The schema maximum is 512 (user approved 512 on 2026-10-21 after socket measurements in docs/limits.md). Before a run starts, the simultaneous requests the run can open are compared with the measured OS open-file limit, and start is blocked with an explanation if they do not fit.

## 1. Separate "ceiling" from "default parallelism" (the core change)

Today `concurrency` means two things at once:

- the global slot-pool ceiling (`runner.py:170`, `client.py:211`);
- the default parallelism of sample-based checks and arrivals (`effective_check` `models.py:1491`, `effective_traffic` 1505, the arrival pool `conditions.py:213`, Sessions/Consistency auto-defaults 1373/1377, pack correctness `runner.py:1943`, #45 default levels 1307-1311, `form-config.mjs:276`).

Issue #26 needs these split apart.

### 1.1 Fields

| Field | Type / default | Meaning |
|---|---|---|
| `concurrency` | `int \| None = None`, 1..512 | **Limit simultaneous requests**: an optional suite ceiling on unfinished target requests. It is never a default parallelism. |
| `default_concurrency` | `int = 4`, 1..512 (JSON/CLI only) | Default for every check that runs more than one request at a time when its own setting is unset. |
| `max_requests` | `int \| None = None`, ≥1 | **Stop after this many requests**: suite target-request budget, split into per-endpoint quotas as today. AI evaluator calls keep their separate budget. |
| `max_duration_seconds` | `float \| None = None`, >0 | **Stop test traffic after**: suite traffic time budget. |
| `arrivals.max_in_flight` | `int \| None = None`, 1..512 | New. Arrivals' own in-flight bound. `None` means `default_concurrency`. |
| `traffic.max_in_flight` | existing, `None` means `default_concurrency` | It no longer defaults to the ceiling. |

### 1.2 Peak simultaneous requests

`required_concurrency(config) -> {per_target_peak, peak, by_check}` is a pure, conservative upper bound.

Per-check peak:

| Check | Peak |
|---|---|
| initial, warm, generation, context, cancellation, prefix, tools | 1 |
| correctness / json / pack_loaded | effective concurrency |
| capacity | resolved `traffic.max_in_flight` |
| arrivals | resolved `arrivals.max_in_flight` |
| recovery | sustained workers (`runner.py:1038`) |
| sessions | `min(sessions.concurrency, sessions.sessions)` |
| consistency | `max({1, *levels})` |
| Concurrent load | `max(levels)` (interference is level−1 + 1) |

- `per_target_peak` is the maximum over the selected checks.
- `peak` is `per_target_peak × targets` when `overlap_models`, otherwise `per_target_peak`.
- There is no "+1 for readiness": nothing overlaps within a target. Telemetry and deployment scrapes use their own clients, so they count toward the file estimate (§6), not the slot pool.

**Slot pool size**

- Ceiling Not set: `peak`. Never unbounded.
- Ceiling set: `min(peak, ceiling)`.
- With a single target, the conflicts in §1.3 guarantee `peak ≤ ceiling`. With `overlap_models`, contention is allowed and reported (§1.3).

**Clients and sockets**

- `LLMClient` takes an explicit `max_connections` equal to the per-target pool share.
- `max_keepalive_connections` equals `max_connections`, so idle sockets never exceed in-flight capacity per client.
- Judging clients use `judging.concurrency`.

### 1.3 Conflicts instead of silent clamps

Remove every silent clamp:

- `effective_check` `min(...)` (`models.py:1494-1495`) and `effective_traffic` (1505).
- Sessions/Consistency default clamps (1372-1377).
- #45 default load levels filtered by ceiling (1306-1311).
- #45 default profile output caps clamped to `max_output_tokens` (1315-1334), plus the validator at 1431-1433.
- `form-config.mjs` 47-48, 62-82, 270-276, 340-349.
- `updateBounds` concurrency rewrite (`app.js:2075-2079`).
- The concurrent preset setting `config.concurrency`.
- The runtime clamps in §2.2.

Replacement rules, with the ceiling set:

- **Explicit per-check value above the ceiling (single target).** This is a **blocking error** naming both values and both fields, for example: "Correctness runs 64 at once, above Limit simultaneous requests (40)." It covers per-check `concurrency`, `traffic.max_in_flight`, `arrivals.max_in_flight`, `sessions.concurrency`, consistency levels and Concurrent load levels.
- **Explicit per-check value with overlap.**
  - Concurrent load keeps its existing error (`max(levels) × targets > ceiling`).
  - Other explicit values × targets above the ceiling are a **non-blocking warning**: "Shared across 2 endpoints: up to 8 requests may wait for the 4-request limit." This matches today's shared-pool behaviour, so no saved overlap config becomes invalid.
- **Inherited value above the ceiling.** The effective value is `min(default, ceiling // (targets if overlap else 1))`, at least 1. A **visible non-blocking notice** appears at the check row/panel and in the Run plan, offering **Raise limit** and **Use N for <check>**. This covers `default_concurrency`, the Sessions/Consistency auto-defaults, and #45 default levels.

Burst warning: if `arrivals.burst_size > arrivals in-flight + max_pending`, warn that the excess will be dropped locally, and show the arithmetic.

## 2. Request settings per check (D3) and runtime wiring

### 2.1 Inventory and ownership

The authoritative inventory comes from the runner today. Rule: **no second field when a check-local one exists.**

| Check panel | Fields shown (each pre-filled with its effective value, marked "default" while inherited) | Runtime consumer to wire |
|---|---|---|
| Endpoint response (`serving`; owns `initial` + `warm`) | samples (warm-up), request timeout, output cap | `runner.py:817-821, 856`; `planning.py:202` |
| First answer | request timeout, output cap | warm/initial specs |
| Generation | samples, output cap, request timeout | existing per-check, plus timeout |
| Context recall | samples, output cap, request timeout, context window (`test_options.context.context_limit`) | rebuilt fixtures |
| Answer correctness / JSON correctness | samples, concurrency, output cap, request timeout | fixtures built per check (`runner.py:1964`) |
| Request-pack correctness | per-check overrides stay forbidden (`runner.py:1943-1945`). Concurrency comes from `default_concurrency`, shown read-only with an explanation. | `request_pack.py:53-67` |
| Cancellation | samples, output cap, request timeout | `runner.py:989` |
| Recovery | samples, concurrency, sustained seconds, output cap, request timeout | sustained/traffic fixtures |
| Capacity | in-flight (`traffic.max_in_flight`), request timeout, output cap | `traffic.py` |
| Arrivals | in-flight (`arrivals.max_in_flight`), request timeout, output cap, pass criteria (§2.3) | `workloads.py:126-161` |
| Prefix | request timeout, context window | `workloads.py:276,290` |
| Sessions | existing `sessions.concurrency` and `sessions.max_tokens` (no duplicates), request timeout, context window | `workloads.py:416-429` |
| Consistency | existing levels, request timeout, output cap (replacing `min(192, global)`), context window | `workloads.py:457-461` |
| Tools | request timeout, output cap, context window | `workloads.py:510`, `tool_conversations.py:59,69,137` |
| Concurrent load | unchanged (#45 profile, levels, pass criteria) plus request timeout | `concurrent_load.py:297,305` |

**Endpoint properties.** `stream` ("How answers arrive") moves to the Connect step: it is single and run-wide. `focusSetting` crosses steps for stream-related conflicts (interference, tool stream mode). `context_limit` does **not** move to Connect: it stays a suite default (JSON/CLI), with the per-check context-window fields listed above and the existing per-target `deployment.serving.context_limit`.

### 2.2 Runtime: effective values travel on the request

Every spec builder resolves the check's effective `max_tokens` and `request_timeout_seconds` into the `RequestSpec`, building fixtures per check config. Then remove the global clamps:

- `runner.py:454` (output cap)
- `runner.py:497-500` (timeout)
- `client.py:250`
- `client.py:456-457`
- `fixtures.py:256,287`

**Suite defaults are defaults, not ceilings.** A per-check value may exceed the suite default. A Concurrent load profile cap is its own value.

**Verdict and analysis sites read effective per-check concurrency, never the ceiling:**

- `runner.py:1200-1202` (pack loaded `configured_concurrency`)
- `runner.py:1584` (recovery windows)
- `runner.py:1440, 1451, 1471, 1663`
- `conditions.py:211-213`
- `planning.py:53, 93, 96, 104, 120, 133`
- `cli.py:656`

`_check_config` (`runner.py:153-158`) keeps copying the *effective* value into the per-check config's `.concurrency`, and must never copy the ceiling.

### 2.3 Pass criteria per check

Extend `CHECK_LIMIT_FIELDS` for arrivals, prefix, sessions, consistency and tools. Today they are empty (`models.py:757-763`) and read global `config.limits` (`workloads.py:566-571, 681, 846-910`). Route `workloads.results` through `_check_config`, and give each panel its applicable criteria.

`limits.regression_percent` moves next to **Compare with baseline** on Review.

Global `limits.*` remain suite defaults, JSON/CLI only.

## 3. Optional suite limits at runtime

- **Deadline.** Internally `deadline = math.inf` when Not set. Every `min`, `timeout_at` and `wait_for` site works unchanged. The planner, CLI and UI compare with `None` explicitly. Never serialise `inf` (`cli.py:354, 463` use `allow_nan=False`).
- **Budget.** `None` budget, no quotas, with a `run.remaining(target) -> int | None` helper. Never use `inf` here: `inf // n` is `nan`.
  - `allowed()` ignores the budget.
  - Concurrent load `allowance = None` (records `request_allowance: null`).
  - Recovery `allowance = None`: `sustained_seconds` ends the phase. The prime gate always passes, so the planner shows `recovery_prime` as +1.
  - `_budget_guidance` and Concurrent load budget remedies do not fire.
- **Budget-free paths.** Each must handle `None`:
  - `request_pack.py:174-175, 189, 194, 222, 244`
  - `planning.py:137-160, 181-183, 252, 297-301, 347-351, 448-468`
  - `web.py:630`
  - `cli.py:190-191, 359, 448, 469, 655-656`
  - `reporting.py:30`
  - the UI sites listed in the feasibility review
- **Live request slots (`web.py:630`, `app.js:125-148, 2352-2356`).**
  - With no budget the mosaic is count-only: pages run up to `max(slot)+1` and there is no unused-budget fill.
  - The server keeps the most recent 4096 slots in an `OrderedDict`; older pages render as a summary.
  - A completion for an evicted slot updates the summary and is never re-inserted.
  - The budget bar shows "No request limit set".

## 4. Migration and versioning (D2)

- **`config_version`.** `RunConfig.config_version: int = 2`. UI exports, CLI `--save-config` and the bootstrap config write it.
- **Where migration runs.** Only at I/O boundaries:
  - CLI config file / `--manifest` load
  - web request bodies (draft/validate/preview/plan/start)
  - sessionStorage drafts
  - baseline and report config normalisation

  Python-constructed configs (tests, `judging.py:139`, `preflight.py:72`, `capabilities.py:56`, scripts) and flag-only CLI runs are v2 and produce no notes.
- **Order.** The D2 migration runs **before** the #45 migration. The #45 migration must treat `None`/omitted `max_duration_seconds` as unlimited, so it never shortens the load to fit 300 s (`models.py:440-453`). It must also stop assuming `concurrency` 4 and `max_output_tokens` 128 caps (344, 369).
- **Legacy (no `config_version`) rules:**
  - An omitted `max_requests`, `max_duration_seconds` or `concurrency` becomes Not set, with one note each, for example: "Earlier Giraffe versions stopped after 300 requests by default; this configuration did not set a request limit, so none applies now. Enable Stop after this many requests to keep that bound." Use the matching text for 300 s and 4 at once.
  - An explicit value stays enabled. An explicit `null` means Not set, with no note.
  - An explicit legacy `concurrency: N` also sets `default_concurrency = N`, so sample-based checks, arrivals and capacity keep running N at once. Note: "concurrency: 40 now limits simultaneous requests; checks keep running 40 at once via default_concurrency."
  - An omitted legacy `concurrency` gives `default_concurrency = 4`, which preserves today's parallelism.
  - Legacy `traffic.max_in_flight` omitted resolves to `default_concurrency`, which equals the old effective value.
- **Notes channel.** Same as #45: response metadata, sent on start, `manifest["config_migration_notes"]`, CLI stderr.
- **Compatibility.**
  - All `examples/*.yaml` and `docs/validation/*.json` must load.
  - Update examples to `config_version: 2` with the limits they intend.
  - Keep a legacy fixture.
  - Do **not** add `config_version` to `validation_suite`: it drives unchanged CLI checkouts and already sets explicit limits.
  - Document that v2 exports are rejected by older Giraffe (`extra="forbid"`).
- **CLI flags.**
  - `--concurrency N` keeps its legacy meaning: it sets both the ceiling and `default_concurrency`, with a stderr note.
  - New `--simultaneous-limit N` sets only the ceiling.
  - New `--default-concurrency N`.
  - New `--no-max-requests`, `--no-max-duration` and `--no-simultaneous-limit` force Not set after the override loop (`None` already means "flag absent" at `cli.py:295-298`).
  - Help text drops "default 300".
  - The banner prints "Not set".
- **Baselines and compare.**
  - Normalise baseline configs through the same migration before matching.
  - Match workloads on `effective_checks` / `effective_traffic`, which already capture resolved per-check concurrency.
  - Treat the ceiling and budgets as non-workload settings, unless the report records that a budget actually stopped work. Add `limits_reached` to the report.
  - Normalise new optional fields such as `arrivals.max_in_flight` and `default_concurrency` to their defaults, so old baselines do not become "comparability unknown".

## 5. Run plan (right column; existing components)

Keep `renderRunBlueprint`, the bounds `dl`, and the Request breakdown `details` with its `data-plan-row` rows, count/Adaptive/Duration-based markers, titles, summaries, sources and scopes. Do not rename planner titles.

**Bounds rows**

- The three limits show their value, or **Not set**.
- "Memory guardrail" shows its value, or Off.
- With a request budget and more than one endpoint, a note shows the per-endpoint split.
- Replace copy that becomes false when limits are Not set:
  - "Upper limits for the whole run, not predictions." → "Optional limits you set. Tests also stop at their own durations, request counts and per-request timeouts."
  - Row scope "bounded by your limits" → "bounded by this test's own settings" when no suite limit applies.
  - "per endpoint, before run limits" → "per endpoint".

**Request breakdown**

- Collapsed by default.
- **Fix the open-row bug** (`app.js:1992-2002`): the outer `details` is recorded as `planCheck=undefined`. Preserve exactly the open row ids and the outer element's open state.
- Counts reflect the whole effective suite.
- Arrivals keeps `n` requests as its count, with "in ceil(n/burst) bursts" as a descriptor.
- Constant-rate and Poisson traffic say "exact count; seeded random timing" for Poisson.
- With no budget, `recovery_prime` shows +1.
- Duration-based rows stay Duration-based. They show an observation-based "~N (estimate, from your last run)" only when comparable observations exist (§5.1).

### 5.1 Estimated duration: evolve the existing estimate

The existing estimate is `_time_plan` / `renderPlanTime` / `.plan-time` ("Usually about…"). **Replace it in place; do not add a second duration statement.**

- **Remove** the 1 s/request `typical` path (`planning.py:30-165`) and the "assumed 1 s/request" text (planning.py:349, `time_estimate_assumptions`).
- **Headline.** "Estimated duration: about 3–5 min · test traffic only". It is a range built from known components only.
- **Unknown components.** Shown separately, never folded into the range: "+ 120 requests at unknown speed (up to 1 h if every request hits its 30 s timeout)".
- **Excluded from the estimate.** Listed explicitly: setup/connection, AI evaluation, report generation.
- **Components:**
  - **Fixed windows.** Capacity stages, Concurrent load configured load time, arrival schedule span, recovery sustained time and waits, cancellation waits.
  - **Finite groups.** `group()` runs `gather` batches (`runner.py:617-625`), so each batch lasts as long as its slowest request. Batch time uses the expected maximum of the requests from comparable recorded empirical distributions (`F(max ≤ x) = ∏ F_i(x)`). Sum expected batch times and combine batch variances across sequential groups in a nominal 99% prediction interval, including finite-sample mean uncertainty. Keep the normal lower bound; extend the upper bound with the first Cornish–Fisher correction from measured positive third moments, because small gather batches are skewed. Uncertainty grows with the square root of batch count, rather than summing per-batch worst cases. Optional queued-arrival drain stays an upper-bound component, with zero lower contribution when the queue might be empty. No fixed per-batch padding or percentage multiplier is added.
  - **Drain.**
    - Low end: Concurrent load windows have zero lower drain; steady Capacity stages use `max(0, p50 − 1/rate)`. Arrivals end at the last arrival, so their lower drain is the minimum observed latency, without subtracting a rate gap. Quantile-only legacy summaries have no measured minimum and contribute zero lower drain.
    - High end: up to the request timeout per window.
  - **Targets.** Sequential or overlapping, using effective per-target concurrency, never the ceiling.
- **Observations source.**
  - At `write_report` time, persist a compact `latency_summary` into the run's existing `.web.json`. This is written for both CLI and web runs, so it adds no new file and keeps the footprint small.
  - Keys: (check family via `conditions`, concurrency/level, stream, output cap, input size bucket). Values: n, p50, p90, p99, max, batch timing coverage (the fraction of a finite group's traffic span explained by actual batch maxima), 21 quantiles of `elapsed_ms`, minimum latency, mean inter-batch overhead, and a mergeable sparse fixed log2 histogram (32 buckets per factor of two; each bucket width is at most 2.2%) for completed valid requests. Raw histogram counts merge at the same fixed boundaries without rebinning error; pooling applies run weights to those counts. Quantiles are derived after merging, never averaged. Existing 21/101-point summaries without histograms remain usable individually but cannot establish pooled confidence. Version 3 measurements retain the 21 points for backward readability; measured storage is recorded in COMMIT_MSG.txt.
  - Exclude initial, `recovery_prime`, readiness, calibration, timeouts, errors, cancellations, and cold-start-mode runs.
  - The web server serves summaries for the same endpoint URL (normalised via `completion_url`) + model, with the same deployment fingerprint when present, from runs in the last 30 days. Results are cached by (path, mtime). Error/cancelled web states are excluded. The 300 ms preview never parses `report.json`. All eligible observations are sorted newest first and pooled with equal total weight per run, so a large older run cannot outweigh a smaller recent run. Effective sample size is `(sum weights)^2 / sum(weights^2)`, and this effective count controls prediction uncertainty and the sample thresholds; raw recorded counts are retained. Each contributing run must cover every fixture/level of a component. Calibrated context inputs additionally require the same reconstruction profile. A drift boundary in any setting ends the common block for that whole component; do not skip a shifted run and resume pooling older ones.
  - The CLI uses `--runs-dir` when given.
  - `request_plan` stays observation-free when called from model validation and from recursive calls (`models.py:1455-1459`, `planning.py:295`, `concurrent_load.py:569`). Observations are an optional argument.
- **Comparable.** An observation counts only with the same check family, the same concurrency/level, the same stream mode, an output cap within ±25%, and the same input size bucket. Otherwise that component is unknown.
- **Drift and provenance.** Compare medians in log space against a two-sided 99.9% median-sampling interval estimated from IQR, with a 10% minimum shift (above histogram error). Small variable samples use a conservative 2.5× shift floor below 10 samples and a log-spread floor of 0.4 below 20 samples, to avoid diagnosing drift from a few chance draws. Use only the newest consistent block and state “based on 3 earlier runs of this endpoint”; if a boundary was detected, say that older observations were excluded for latency drift.
  Every older run is compared with the newest run alone, never with an accumulating pooled reference. Bound the pooled median's log-distance from the newest median by their 99.9% sampling interval, with a 20% floor and a hard 2.5× maximum; this accommodates small stable cohorts without allowing an unbounded moving reference. Also check the latest five run medians (and each proposed pooled block) for a run-order trend: absolute Spearman rank correlation ≥0.99 and at least 25% end-to-end change. Average tied ranks; five untied medians require perfect order (two-sided random-order probability 1/60). This is a conservative stability gate, not a statistical significance claim. A detected trend stops further pooling and forces low confidence with a “latency is trending” assumption, even if the truncated block has enough samples. Three runs alone are insufficient trend evidence (random medians are monotonic one third of the time); stable quality suites can still reach medium after three comparable runs.
  After a detected shift, small pooled cohorts require five consistent runs (or 150 comparable samples per setting) before medium confidence resumes; three small post-shift runs did not meet 90% containment in the seeded drift review. Unshifted stable suites retain the three-run threshold.
- **Timing coverage.** Compare actual batch maxima with the span of contiguous batches of the same setting. Time spent sending other levels/input buckets is not local overhead. Retain the strictest coverage across pooled runs so an older fast run cannot hide new bookkeeping delay. After at least 3 runs, small measured gaps may be modeled explicitly: each run must have at least 95% raw batch timing coverage and measured mean inter-batch overhead. Add that overhead to expected batch time and its across-run variance to prediction uncertainty. The resulting accounted coverage must still reach 99%. Missing or larger overhead remains unmodelled/low; do not substitute guessed overhead padding.
- **Confidence:**
  - **high**: no unknown or insufficiently sampled finite components, at least 80% of the midpoint comes from fixed windows **and** drain uncertainty is at most 20% of the midpoint;
  - **medium**: every finite component has a comparable recorded distribution with either at least 150 samples or at least 15 samples per setting from 3 consistent runs, no unknown components, and at least 99% accounted timing coverage (raw batches, or raw batches plus the repeated-run small-overhead model). Single-run components still require their normal prediction half-width to be at most 40% of expected time. The combined asymmetric range must have half-width at most 50% of its midpoint. With 150 independent samples the probability of missing a 2% slow tail is below 5% (`0.98^150`); the pooled route instead uses repeated-run stability, measured moments and finite-sample prediction uncertainty, and is checked against independent seeded runs. This is an empirical model, not a guarantee about unseen tails. Sparse, shifted, incomplete, overhead-dominated or wider cohorts remain low, with actual sample/run counts and reasons;
  - **low**: otherwise, with the unknowns listed. Repeated default suites can remain low because adaptive context, load calibration and intentional cancellation have unknown duration; a large configured drain timeout can keep a load-style suite low even after pooling. Explain those remaining causes in assumptions, rather than implying that more samples alone will fix them.
- **Accuracy test.** An in-process run against a fake endpoint with fixed latency must finish inside the stated range for high and medium cases.

### 5.2 Budget comparisons

- **Time budget** (when set):
  - Fixed sending windows that exceed it are a **warning** ("incomplete coverage"). The warning names the windows that will be cut and offers **Use N s** / **Edit <window>**. This follows the issue's "may end the run before all selected tests finish" and matches the request-budget treatment.
  - It is a **block** only when no selected traffic work can start before the budget. Earlier finite work counts as traffic work. In current schedules this is effectively unreachable: limited runs warn because the time budget may end a run before all selected checks finish.
  - Cut detection uses estimated high durations and unknown lower bounds, not the unknown timeout bounds. Estimate risk (including unknown timeout bounds) is a separate, softer warning. Completion remedies use the conservative runtime-ordered sum of component highs (including the setup allowance), even when the headline combines independent variances into a tighter range. Offer Use N s only when N is greater than the current budget; without full observations label it as covering fixed windows only.
  - **This changes today's blocking rule (#36/#45). Update those tests and say so in the PR.**
- **Request budget** (when set):
  - An exact plan above the budget is a warning naming the checks that will be cut. It offers **Use N total requests** in the sidebar and on Review; the handler also ticks the checkbox (`app.js:2623`). It also offers Edit for checks.
  - Duration or adaptive work gives a risk warning.
  - Never change the budget or the workload silently.

## 6. Open-file guard (D4)

1. **Requirement**
   - `max(target socket peak, judging.concurrency)`.
   - Target socket peak = the slot pool size. Keepalive is bounded per client (§1.2).
   - Plus currently open descriptors, measured from `/dev/fd` on macOS or `/proc/self/fd` on Linux.
   - Plus per-target metrics and deployment clients when enabled.
   - Plus a margin of 32, which covers the report stream, trace files and SSE/browser sockets.
2. **Raise first.** `setrlimit(RLIMIT_NOFILE, (min(hard, need), hard))`. On macOS, cap at `min(hard, kern.maxfilesperproc / OPEN_MAX)`; a soft limit of `RLIM_INFINITY` fails. Record before/after values in the manifest.
3. **Block** only if the raised soft limit is still below the requirement. The message names:
   - the numbers;
   - the largest contributors, with Edit links;
   - the remedy: run `ulimit -n 4096` in the shell, and for the web UI restart `giraffe web` from that shell.
4. **Where it runs.** `start_run` (web) and CLI `run` only. Never `prepare_run`: Copy CLI (`web.py:854, 881-887`) must not be blocked by the web process's limit.
5. **During configuration.** The live preview runs the same computation without raising. It shows a warning when the requirement exceeds the *hard* limit (blocking) or the current soft limit (it will be raised automatically).
6. **Unsupported platforms** (no `resource`): skip the guard and say so in the plan.

## 7. Conflicts at the setting that causes them

Every conflict appears beside the offending field(s) **and** in the Run plan, with Edit actions. The actions reuse `settingDestination` / `focusSetting`, open the right panel, `<details>` or step, and focus the field.

CLI JSON validation errors retain `loc`, `message` and `type`, and also expose `field`, `fields`, safe `values` and correction `actions`, matching the API diagnostics. They never echo raw configuration inputs.

- **Concurrency.** Covered by §1.3.
- **Traffic plan cap.**
  - It becomes a `PydanticCustomError` with ctx `fields: ["traffic.rates", "traffic.duration_seconds"]` and values. `_configuration_errors` generalises its field-prefix and safe-values handling (`web.py:105-107, 158-160`).
  - Message: "250 requests/second × 3,600 seconds = 900,000 planned arrivals. This traffic plan allows at most 100,000 per endpoint. At this rate, the longest stage is 400 seconds (6 minutes 40 seconds)."
  - Multi-stage: "(50 + 100 + 250) requests/second × 300 seconds = 120,000 planned arrivals… the longest stage that fits is 250 seconds."
  - Actions:
    - **Use 400 seconds** sets the duration only and refreshes the whole preview.
    - **Edit sending rate**.
  - Note that this covers scheduled traffic only. Never offer to raise a budget for a hard cap.
- **Capacity risk** (optional expected response time in the capacity editor): "≈ rate × response time = 500 unfinished requests needed; in-flight limit 256 → the rest are dropped locally." Labelled as an estimate. Never blocking.
- **Budgets and the open-file guard:** §5.2 and §6.
- **States.** Pending, error and valid states are accessible: `aria-live` only on the plan summary, `aria-invalid` on fields. A stale preview cannot enable Start. Form and JSON views give identical derived values and errors.

## 8. Run limits section UI

- **Placement.** Above the checks in **DOM order** as well as visually. Remove the `order:-1` trick (`styles.css:1107`) and update the DOM-order tests.
- **Headings.**
  - Rename the existing `<summary><h2>Edit run limits</h2>` in place to **Run limits**, keeping its disclosure behaviour.
  - Rename the sr-only `<h2>Check selection</h2>` (`app.js:1014`) to a visible **Choose your checks**.
  - Change the step-2 `<h1>` (`app.js:1808`) from "Choose your checks." to "Set limits and choose checks.", so the heading text is not duplicated.
- **Three optional suite limits**, each a checkbox plus a value field using the existing field components:
  - "Stop test traffic after", with a unit select (s / min / h);
  - "Stop after this many requests", with a hint on the per-endpoint split;
  - "Limit simultaneous requests".
- **Checkbox behaviour.**
  - Unchecked disables and visually empties the value, and shows "Not set".
  - Re-checking restores the last value entered in this session; otherwise the field is empty and required.
  - It never fills in 300 / 300 s / 4.
- **Resource protection row.** Memory guardrail checkbox, default on, plus a MiB value. Unchecked writes `max_memory_mb: 0`.
- **Removed from Run limits.** Samples, output cap, request timeout, context window, stream and every pass rule. They now live in the check panels (§2) or on Connect (stream). Nothing is duplicated.
- **Summary text.** "Up to 400 requests · 5m · 40 at once", or "No suite limits set".
- **Help text.**
  - Every simultaneous-request field says: "An accepted value is not demonstrated delivery capacity."
  - Arrival-count, burst, pending-queue and rate-based traffic help explains the differences (queued vs dropped locally).

## 9. Caps review (`docs/limits.md`, single source)

Expose every retained cap through `capabilities()["limits"]`, generated from the schema plus named constants. The UI reads min/max from it; remove the hard-coded duplicates in `app.js`/`form-config.mjs`. Record **keep / change / configurable** with rationale and **measured evidence**.

- **Simultaneous requests: 512.**
  - Phase 7 replaces the O(in-flight²) scan with lifetime peak bookkeeping.
  - Socket measurements at 256 / 512 / 1024 recorded timing health, launch lateness and CPU. The accepted ceiling is 512; the 1024 candidate failed timing-health and launch-tolerance checks. See `docs/limits.md`.
  - Sessions concurrency shares the 512 bound; its effective value is capped by the separate 32-conversation count.
- **`Arrival.requests` 10,000 → 100,000** only if all of these pass:
  - in-process run RSS growth stays under the default 4 GiB guardrail with margin;
  - report bytes under each retention mode fit the per-run storage limit, or the trimming is acceptable;
  - web job memory (`job["requests"]`, conditions) is bounded;
  - scheduler lateness at 1,000/s is within the existing tolerance.

  Otherwise keep 10,000 and document why. Do not cite the local benchmark.
- **Burst size 256 → 1024**, with the §1.3 burst warning.
- **Kept, with rationale and the effect of exceeding each:**
  - pending queue 256;
  - traffic plan 100,000;
  - traffic rate 10,000/s;
  - arrival steady rate 1,000/s;
  - stage duration 3,600 s;
  - at most 16 rates;
  - sessions / prefix / consistency caps (decide each);
  - payload and evidence bounds.

## 10. Implementation phases

Each phase is reviewed and tested before the next.

1. **Behaviour-preserving split.**
   - Add `default_concurrency`, `arrivals.max_in_flight` and the `traffic.max_in_flight` default rule.
   - Route every default-parallelism read through them.
   - Add `required_concurrency`, slot pool = peak, and an explicit `LLMClient` `max_connections`/keepalive.
   - Replace clamps with conflicts and notices (§1.3).
   - Verdict sites read the effective value.
   - Tests still pass with all three limits set.
2. **Nullable limits.**
   - `inf` deadline and `None` budgets with helpers.
   - Recovery and load allowances, live slots.
   - Migration and `config_version` with notes; CLI flags and prints; bootstrap emits null.
   - Baseline normalisation.
   - Update tests that relied on the 300 default.
3. **Per-check request settings and pass criteria** (§2): per-spec effective values, removal of the global runtime clamps, extended `CHECK_OPTION_FIELDS`/`CHECK_LIMIT_FIELDS`.
4. **Planner.** None-aware budget comparisons (§5.2), `latency_summary` + observation lookup + the estimate (§5.1), open-file guard (§6).
5. **Structured errors** (§7) and the `_configuration_errors` generalisation.
6. **UI** (§§5, 8): Run limits checkboxes, Connect `stream`, per-check fields, plan rows and estimate, the open-row fix, the mosaic with no budget, capabilities-sourced caps.
7. **Caps.** Measurements and `docs/limits.md` (§9).

## 11. Acceptance matrix

Every row must have evidence before the PR. Rows A1–A19 are the issue's criteria and examples; A20+ come from the reviews.

| # | Criterion | Evidence |
|---|---|---|
| A1 | Run limits above checks (DOM and visual), both h2 headings visible, Run plan on the right with existing components and styles | DOM-order test; real-browser screenshots at 1400 px and 375 px |
| A2 | Breakdown markup, styling, expand/collapse, summaries, sources, scopes and adaptive labels preserved; open rows preserved on refresh | UI test (open 2 rows, refresh, exactly those stay open); markup comparison with main |
| A3 | Request/data settings and pass criteria live in each check (including arrivals, prefix, sessions, consistency, tools); none duplicated in Run limits | UI test of field placement and uniqueness |
| A4 | Each limit toggles independently; unchecked persists as null through save, load, export, draft, CLI and run; no revert to 300 / 300 s / 4 | Round-trip tests per channel; an in-process run with all three Not set completes |
| A5 | Retained software limits explained; API, CLI and UI enforce one policy from `capabilities.limits` | `docs/limits.md`; grep test for hard-coded maxima in app.js/form-config.mjs; identical API and CLI errors |
| A6 | Planned counts reflect the whole suite (controls, warm-ups, endpoints, adaptive, `recovery_prime` +1 without a budget) | Planner tests against in-process runs: default suite, issue example suite, #45 Concurrent load |
| A7 | Estimated duration with scope, assumptions and uncertainty; one duration statement; no generator row | UI test scoped to the Run limits / Run plan DOM ("Automatic" and "Load generation" absent there); confidence-class tests; accuracy test |
| A8 | Conflicts appear during configuration; links open and focus the settings (including cross-step for stream) | UI tests per conflict class |
| A9 | 64-vs-40 concurrency conflict and insufficient time/request budgets covered by focused UI/API tests | Named tests |
| A10 | Examples and #45 produce correct breakdowns; no workload silently changed | Behavioural tests: each removed clamp now errors or notifies; example breakdown snapshots |
| A11 | Cap decisions recorded with measured evidence | `docs/limits.md` with measurement tables |
| A12 | 250/s × 3600 s blocks with 900,000 and a correction; Use 400 s refreshes without changing the rate | API + UI tests |
| A13 | 25/s × 3600 s passes the cap; a shorter time budget or too-small request budget is explained separately | Tests |
| A14 | `[50, 100, 250]` × 300 s reports 120,000 and the aggregate conflict | Test |
| A15 | Budget-only overrun shows the budget action/warning with multiple endpoints and other checks | Test |
| A16 | Arrival count, burst, pending queue and rate-based traffic controls are clearly distinct | Labels/help test |
| A17 | Form and JSON give identical values and errors; a stale preview cannot enable Start; accessible states | UI tests |
| A18 | Open-file guard: soft limit raised automatically; blocks only when the hard limit is insufficient; judging-dominated case; Copy CLI not blocked; preview warns; skipped where unsupported | Tests with monkeypatched `getrlimit`/`setrlimit`/fd count |
| A19 | Migration: omitted limits become Not set with notes; explicit values kept; legacy `concurrency: N` preserves workloads via `default_concurrency`; arrivals/capacity in-flight unchanged; #45 load duration not shortened when the time limit is omitted | Tests per case, including a legacy overlap fixture |
| A20 | Slot pool equals planner peak; observed peak stays within the pool in mixed-check overlap runs | In-process test |
| A21 | A per-check timeout or output cap above the suite default is actually applied by the runner and client | Test |
| A22 | Verdict sites use effective concurrency (pack loaded at effective 1 with ceiling Not set; recovery windows with ceiling ≠ effective) | Tests |
| A23 | Old baseline vs new run with the same workload: comparable | Test |
| A24 | CLI `--concurrency` alone keeps legacy semantics with a note; `--simultaneous-limit` sets only the ceiling | Test |
| A25 | Live view with no request budget: count-only mosaic, eviction summary, no budget bar division by zero | UI test |
