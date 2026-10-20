# Concurrent load: design (issue #45)

This document is the implementation contract for reorganising the **Concurrent load** check (check id `concurrency`). It covers config schema, migration, execution, scoring, planning, reporting and UI. Values marked *default* are starting defaults, not fixed product decisions.

## 0. Scope and non-goals

- The check id stays `concurrency`; its title becomes **Concurrent load**. Capacity (`capacity`) and Arrival traffic (`arrivals`) remain separate, unchanged checks. Recovery's `sustained_seconds` is unrelated and unchanged.
- The old scenario engine (`size_matrix`, `mixed_interference`, `streaming_fairness`, `token_stress` and the internal ids `fairness`, `buckets`, `mixed`, `token_stress`) is replaced by one executor. Old **saved reports** must still render (their `metrics.scenarios` shape keeps its existing renderer). Old **configs** load through an explicit, visible migration (section 2).
- `refill` / `sample_count` in `logprob_workload.py` are shared with the consistency check and stay.
- Default runs (levels `[1, 4]`) do not run the interference experiment, because it needs every level ≥ 2. The old default ran streaming fairness. This is a deliberate default change, documented in README/docs and in migration notes for migrated defaults.
- `concurrency` stays in `DEFAULT_CHECKS`. Default configured load time is (3 combinations + 1 mixed) × 2 levels × 15 s = 120 s (2 min), which fits `max_duration_seconds=300` alongside the other default checks. The default window avoids a short-window throughput warning for the default output caps.

## 1. Config schema

New `RunConfig.concurrent_load: ConcurrentLoad` (pydantic, `extra="forbid"`). The legacy keys `concurrent_workloads` and `buckets` are accepted only as migration input (section 2) and are not written back.

Sizes are `short`, `medium`, `long`. A *shape* (combination) id is `"{input}_{output}"`, e.g. `long_short` = Long input → Short output. Nine shapes exist.

```
ConcurrentLoad
  profile: DataProfile
    short:  {input_tokens: 128,  output_tokens: 32}    # default
    medium: {input_tokens: 512,  output_tokens: 64}    # default
    long:   {input_tokens: 2048, output_tokens: 128}   # default (fits default max_output_tokens=128)
    # input_tokens 1..131072 (token TARGET); output_tokens 1..65536 (CAP: model may finish earlier)
    # validation: short.input <= medium.input <= long.input and same for output (strictly increasing is not required, but non-decreasing is)
  combinations: {enabled: true, selected: ["short_short", "long_short", "short_long"]}   # default
    # selected: distinct valid shape ids; non-empty when enabled
  mixed: {enabled: true, shares: {"short_short": 60, "long_short": 20, "short_long": 20}, seed: null}  # default, percent of requests
    # seed: int 0..2^53−1 | null; null = derived from run id within the same JS-safe range; the seed used is recorded
    # each share 0..100; keys valid shape ids; shares > 0 sum to exactly 100 (tolerance 1e-6) when enabled
  interference: {enabled: false, trigger: "first_output", trigger_chunks: 5, max_slowdown: 1.5}
    # trigger: "first_output" | "after_chunks"; trigger_chunks 1..1000 (used only for after_chunks)
    # max_slowdown: float > 1, or null = measure only
  stress: {enabled: false, large_input: true, large_output: true, min_output_percent: 80}
    # at least one of large_input / large_output when enabled; min_output_percent 1..100
  schedule: {levels: [1, 4], duration_seconds: 15}   # default
    # levels: 1..16 distinct ints, each 1..256, sorted ascending on save
    # duration_seconds: int 1..3600
  pass_criteria:
    first_output_p95_ms: null   # null = measure only; else int >= 1
    response_p95_ms: null       # null = measure only; else int >= 1
    max_error_percent: 0        # 0..100
    min_correct_percent: 100    # 0..100; applies only where an expected answer exists
    min_valid_responses: 5      # int >= 2, per test point; evidence requirement, never a stopping target
  input_token_tolerance: 0.1    # advanced, not shown in the drawer
  calibration_attempts: 3       # advanced, not shown in the drawer
```

At least one load type must be enabled when the `concurrency` check is selected.

Run-wide limits that apply: `concurrency` (concurrency ceiling, "Maximum at once"), `max_requests` (total request limit), `max_duration_seconds` (total time limit), `request_timeout_seconds`, and `max_output_tokens` (run-wide output cap; `client.py` clamps every request to it, and it remains a cost/safety ceiling). Explicit profile output caps must not exceed `max_output_tokens` — a validation error, not a silent clamp. Omitted caps derive from the run-wide cap as described below. The profile is **independent of** `context_limit`: Context window is no longer used to size concurrent-load inputs.

`test_options.concurrency` no longer carries overrides for this check. `CHECK_OPTION_FIELDS` / `CHECK_LIMIT_FIELDS` entries for `concurrency` are removed. `Limits.*` global per-request thresholds do not apply to this check, because its criteria are the p95/percent criteria above.

**Defaults follow safety limits.** When `concurrent_load.schedule.levels` was omitted (including an omitted block), filter `[1, 4]` to levels no greater than the per-target ceiling (`concurrency ÷ targets` rounded down when `overlap_models`, otherwise `concurrency`), with `[1]` as the minimum. Omitted profile output caps are clipped to `max_output_tokens`; omitted neighboring caps also follow explicitly supplied caps to retain non-decreasing order. Explicit values are never clipped. Draft, validate, preview, plan and CLI all return/show the resulting effective defaults. Infeasible explicitly requested overlap allocations remain structured errors.

When `concurrency` is not selected, none of this check's cross-field rules apply, including profile ordering, enabled-type completeness and share totals. Field types, ranges, shape validity and unknown-field rejection still apply, and standalone `ConcurrentLoad` validation is strict. Other checks can therefore use `max_output_tokens=1` without a dormant load profile blocking them.

### Cross-field validation (structured errors, section 6)

| Condition | Error fields (first one is focused) |
|---|---|
| `max(levels) > concurrency` | `concurrent_load.schedule.levels`, `concurrency` |
| `overlap_models` with more than one target and `max(levels) × targets > concurrency` | `concurrent_load.schedule.levels`, `concurrency` |
| any profile output cap > `max_output_tokens` | `concurrent_load.profile.<size>.output_tokens`, `max_output_tokens` |
| interference enabled and `stream` is false | `concurrent_load.interference.enabled`, `stream` |
| interference enabled and any level < 2 | `concurrent_load.schedule.levels`, `concurrent_load.interference.enabled` |
| configured load time (section 4) × targets (sequential targets) plus the existing fixed minimum time (capacity windows, recovery sustained time) > `max_duration_seconds` — i.e. fold configured load time into `planning._time_plan` `minimum` and use the existing fixed-window error | `concurrent_load.schedule.duration_seconds`, `max_duration_seconds` |
| combinations enabled with empty `selected` | `concurrent_load.combinations.selected` |
| mixed shares not totalling 100 | `concurrent_load.mixed.shares.<shape>` (first non-zero share in grid order, or the first grid shape `short_short` when all are zero/absent) |
| stress enabled with neither case | `concurrent_load.stress.large_input` |
| profile sizes not non-decreasing / out of range | the offending `concurrent_load.profile.<size>.<field>` |
| no load type enabled | `concurrent_load.combinations.enabled` |
| known server context window < any running shape's input target + output cap | `concurrent_load.profile.<size>.input_tokens`. Error only when configured/imported deployment metadata states the server window; warning when only `RunConfig.context_limit` was explicitly set by the user (in `model_fields_set`); nothing otherwise. Windows discovered at run time are handled in 3.6. |

These are hard errors and block run start (web and CLI). They must show in the drawer beside the field and in the run plan, with "Edit …" navigation, before Review. The server returns them from the existing plan/validation endpoint with `fields` paths. The UI may also check locally for instant feedback, but the server is authoritative. Migration (section 2) must never produce a config that violates these rules — it must load cleanly and explain what it turned off instead.

Messages use visible labels and readable combinations, not internal shape ids or comparison operators. A ceiling conflict says "Concurrency level 8 exceeds Maximum at once (4)." Overlapping endpoints include the multiplier, e.g. "Concurrency level 4 × 2 endpoints exceeds Maximum at once (4)." The level-1 interference error says "Long input during an active short stream needs at least 2 concurrent requests at every level. Remove level 1 from Concurrency levels."

## 2. Migration of old configs

A `RunConfig` `mode="before"` migration converts legacy `concurrent_workloads` / `buckets` / legacy check names (`fairness`, `buckets`, `mixed`, `token_stress`) into `concurrent_load`. It also produces human-readable **migration notes**.

Notes are not a persisted `RunConfig` field, so they neither round-trip through exports forever nor vanish silently. A pure function returns `(migrated_dict, notes)`.
- **Web:** the draft/validate/preview/plan endpoints return `migration_notes` as response metadata. The UI shows them in the Concurrent load drawer and the run plan until the user dismisses them. Dismissal is display-only: accepted notes, including dismissed notes, are sent on run start and stored in `manifest["config_migration_notes"]`. Applying a new source, resetting workload settings or starting New run replaces accepted notes with that source response and clears dismissal state. Preview/validate responses may merge notes only within the current source; stale responses cannot add notes. `/api/bootstrap` also returns launcher-default migration notes.
- **CLI:** `run`, `validate`, `preview` and `--manifest` reruns print them on stderr and store them in the manifest.
- **Reruns:** rerunning from a saved report recomputes them from the saved config, which yields none for configs already in the new format.

Notes interpolate the **actual old values**. Old meanings are never silently reinterpreted.

| Legacy input | Migration |
|---|---|
| `levels` | `schedule.levels` (same meaning). |
| `size_matrix` scenario / `buckets` check | `combinations.enabled`, `selected` = all 9 (the old matrix ran all 9). Note that duration replaces sample counts. |
| `streaming_fairness` scenario / `fairness` check | Closest equivalent is `interference`. The old experiment ran at 2 concurrent requests. If every level ≥ 2 and `stream` is on, enable `interference` with a note. Otherwise leave it **off**, with a note explaining how to enable it (levels ≥ 2, streaming on). Migration never produces an invalid config. |
| `mixed_interference` scenario / `mixed` check | **Not** mapped to Mixed load, which is a different experiment. The long-input heavy partner maps to `interference` under the same rule as `streaming_fairness`. The note says that long-output heavy partners and before-output ordering have no equivalent, so that coverage is dropped. |
| `token_stress` + `stress_cases` | `stress.enabled`. `large_input` if `long_input_summary` is present, `large_output` if `short_input_long_output` is present. Case token targets and `min_output_tokens` are **not** copied; the note gives the old values. |
| `output_tokens.{size}` (token caps) | Copied to `profile.{size}.output_tokens` (same meaning), clipped to `max_output_tokens` exactly as the old migration did. The note mentions any clip. |
| `input_chars.{size}` (characters) | **Not** converted. Profile input targets use defaults. The note gives the actual old character sizes and says inputs are now token targets. |
| `samples_per_bucket`, `mixed_pairs`, `heavy_weights`, `window_seconds` | Dropped. Each gets a note naming its old value and why (sample/pair counts were stopping targets; the schedule is now duration-based; the old window was a ceiling, not a duration). |
| `input_token_tolerance`, `calibration_attempts` | Copied (same meaning). |
| `test_options.concurrency.*` | Dropped, with a note per field. |
| Non-default global `limits.*` that previously applied to concurrent workloads (`first_output_ms`, `latency_ms`, `stream_gap_ms`, `min_output_tokens_per_second`, `max_error_rate`, `min_correctness`, `min_samples`, `fairness_max_ratio`) | Not converted, because per-request limits and p95 criteria differ. One note per field, e.g. "limits.fairness_max_ratio=3 no longer gates Concurrent load; set Maximum slowdown vs short-only control". The note says explicitly that `stream_gap_ms` and output tokens/s have no replacement criterion here; they still apply to other checks as before. |
| `concurrent_workloads` block present but the check not selected | Migrate the block's settings as above but leave the check unselected. Notes still apply. If `checks` is absent, the old rule was "all legacy scenarios"; follow it. If migration must deselect concurrency, preserve normal automatic check selection for JSON, GPU, arrivals, prefix, sessions, consistency and tools. |
| Mixed load | `mixed.enabled = false` for any migrated legacy config. Note: "Mixed load is new; enable it if wanted". |

All `examples/*.yaml` move to the new schema. A legacy example is kept under `tests/fixtures/` for a migration test. Every `docs/validation/*.json` config and every legacy fixture must load without validation errors and with the expected notes (test).

Baselines: `compare_baseline` between an old-format report and a new-format report for this check is inconclusive, with the reason "baseline uses the previous Concurrent workloads format".

## 3. Execution

A new module `src/giraffe/concurrent_load.py` replaces the scenario executors for this check. It is called where today's executors run (`workloads.execute` / `_fairness`), keeping the check's position in the target sequence. Order: for each enabled load type (combinations, mixed, interference, stress), for each of its workloads, for each level ascending.

### 3.1 Test point and sending window

A **test point** is one workload at one concurrency level, e.g. "Long input → Short output at 4 concurrent requests". For each test point with level N and duration D:

1. **Sending window.** It starts when the first request of the point is sent and lasts D seconds.
2. **Slot replenishment.** N workers each keep one request outstanding. When a request finishes, its worker immediately sends the next one; requests are never sent in batches. This continues until the window ends, a run-wide limit or stop condition ends it, or the point's request allowance (3.5) is used.
3. **No stopping target.** No sample count ends a window early. `min_valid_responses` never stops anything.
4. **Drain.** Slot acquisition is bounded by the point's sending-window end once its first dispatch occurs, including workers already waiting for shared slots under `overlap_models`. Acquiring a slot never extends that window. Requests in flight when the window ends are allowed to finish, bounded by `request_timeout_seconds` and the run deadline.
5. **Measured population.** Latency, correctness, error % and evidence counts cover every request **sent** in the window, including drained ones.
6. **Record fields.** Every request records:
   - `condition_id`;
   - `load_point`, a dedicated field holding type, workload, level, shape and sub-window;
   - `requested_input_tokens` and `requested_output_tokens`;
   - timestamps.
7. **Unique prompt prefix.** Every request starts with a fixed-length unique nonce, e.g. `Request 7f3a9c21.`, so prefix caches cannot skip prefill. The nonce length is constant, so `input_chars` per shape is constant. This matters for baseline comparison.
8. **Cancellation.** Unexpected task cancellation propagates after preserving partial measurements; only a known stop/deadline for this run or target permits normal finalization. Another target's abort is not permission to continue.
9. **Cache hits.** Record `usage.prompt_tokens_details.cached_tokens` where reported.

### 3.2 Combinations

Each selected shape is one workload. Every request in the point uses that shape.

### 3.3 Mixed load

One workload per level.
- **Shape choice.** Each request's shape is drawn by **seeded pseudo-random selection** weighted by the shares. The seed is either the configured `mixed.seed` or one derived from the run id, and it is recorded. This matches the mock ("random selection"), avoids periodic slot lock-in and is reproducible.
- **Observed mix.** It is reported per shape both by requests sent and by in-flight time, next to the requested shares. Shares describe request frequency, not size.
- **Baseline comparison.** Mixed records carry `scenario = "load_mixed_c{N}.{shape}"`, so `compare_baseline`, which compares exact shape proportions per scenario, can compare each shape group.

### 3.4 Long input during an active short stream (interference)

Defined only for N ≥ 2, and requires streaming (validation errors otherwise). Per level N there is one interference workload lasting 2D:

- **Short streams.** N−1 short streams (`short_short`) run continuously for 2D, replenished individually.
- **Probe slot.** One extra slot alternates between a **short probe** (`short_short`, the control) and a **long probe** (`long_short`). The phases are A/B/A/B sub-windows of D/2 each, starting with A = short probe. Only the probe's input length differs between control and probe, and the alternation cancels drift.
- **Trigger.** Each probe is sent when the trigger fires:
  - `first_output`: every short stream in flight has received its first chunk.
  - `after_chunks`: each has received `trigger_chunks` chunks.

  The trigger waits at most `request_timeout_seconds`. Trigger latency is reported, and probes that never trigger are reported. Each probe slot is replenished individually after its probe finishes.
- **Matching.** A short stream is **matched** to a probe when its streaming interval (first chunk → last chunk) overlaps the probe's prefill interval (probe sent → probe first output). The short stream must have at least 2 chunks. Single-chunk streams are excluded from matching and counted. Only when most (>50%) valid completed short streams have fewer than 2 chunks is the point inconclusive for buffering ("stream is buffered; interruption cannot be measured"). A short stream whose decode interval overlaps both a short-probe and a long-probe prefill is excluded from **both** groups and counted as an ambiguous A/B overlap. A stream matched to several probes of one type is counted once in that group.
- **Slowdown.** Median decode time (first → last chunk) of short streams matched to a long probe, divided by the median decode time of short streams matched to a short probe. Also reported: response-time ratio, longest stream pause in each group, matched counts, the allocation ("N−1 short streams + 1 probe slot alternating short/long every D/2") and trigger latency.
- **Load qualification.** Measure the N−1 continuous short streams separately: client time-weighted in-flight ≥ 0.9 × (N−1), server-evidenced peak ≥ N−1, and at least `min_valid_responses` matched short streams in each probe group. The probe slot's trigger idle time does not reduce short-stream qualification; full-population concurrency is still reported separately.
- **Baseline comparison.** Record roles separately as `load_interference_c{N}.short_stream`, `.short_probe`, `.long_probe`; each scenario has stable input/output proportions. A dedicated `RequestSpec.condition_id` preserves A/B live condition grouping (`load_interference_control_c{N}` / `load_interference_probe_c{N}`) independently of the role scenario.
- **Configured load time.** 2D per level.

### 3.5 Run-wide limits, budget fairness and evidence

**Request allowance.** Before each test point, the point's request allowance is:

(remaining run request budget for this target − the known request counts of checks that run after this check for this target − their minimum probes) ÷ remaining test points of this check, rounded down, minimum 1.

This mirrors recovery's `known_per_target` reservation. Recovery's own reservation must also count this check as needing at least one allowance per point, so it does not starve this check.

**Cut short.** A point is **cut short** if its allowance is used, a global limit fires, or the deadline arrives before D elapses. Its delivered window (e.g. "ran 3.2 s of 10 s") and reason are recorded.

**Scoring a cut-short point.** It is scored on its evidence when all of these hold:
- it has at least `min_valid_responses` valid responses;
- its load is qualified (3.7);
- its delivered window is at least max(25% of D, 3 × its median response time).

Its result is then labelled e.g. "pass · budget-limited window 3.2 s of 10 s". Otherwise it is inconclusive.

**Not run.** Points never reached are recorded as **not run**, with the reason. Cut-short/not-run cause codes are `budget_limited` for request budgets/allowances, `time_limited` for the run deadline and `run_stopped` for user stops or other run/target stops. Request-limited inconclusive points suggest the request limit; deadline-limited points suggest Total time limit (`max_duration_seconds`); stopped points do not suggest raising limits. Supported passing points never receive "To conclude, raise …" guidance. Existing failure evidence always takes precedence over these cause codes.

**Planning.** The plan warns when Σ(level × D ÷ assumed 1 s/request) exceeds the expected allowance. The warning sits beside `max_requests` with an Edit action. Use the same per-target quota and `later_reservation(config)` as execution, including sessions/consistency/tools even though their plan rows follow the load rows; subtract preceding known requests and the calibration upper bound.

### 3.6 Token targets, calibration and context window

**Prompt construction.**
- Input prompts are built to **token targets**, reusing the label prompt idea: "The box label is K7P4" plus neutral filler, after the nonce. The model is told to write the label first.
- Long outputs are encouraged by asking the model to continue a numbered list well beyond the cap.

**Calibration.**
- Calibration happens once per (input size, output-instruction variant: short-answer vs long-output instruction), using the token-stress method: `max_tokens=1` requests read `usage.prompt_tokens`, and the filler is rescaled until it is within `input_token_tolerance`, up to `calibration_attempts`.
- Calibration requests use condition `load_calibration_{size}_{variant}` and count against the budget.
- Calibration succeeds on received `usage.prompt_tokens` even when a one-token cap produces blank answer content and the client records "no useful answer output". Calibration checks token usage, not answer usefulness; that case must not fail core serving or consume the error-stop allowance. HTTP errors, timeouts and missing responses remain calibration failures.
- When zero filler still exceeds a target, accept the minimum achievable prompt as calibrated, report its actual token length, and retain a `minimum_prompt_length` note, e.g. "Short input target 16 is below the minimum prompt length (≈52 tokens); measured at 52". The plan warns below estimated fixed prompt overhead (nonce, label, instructions and template). Large-input stress still requires measured lengths within tolerance of its long target.
- Calibration request failures (HTTP errors, timeouts) are endpoint errors: the shapes depending on them **fail** with the reason, as old token stress did.
- If the target is not reached within the attempts, the shapes' points are inconclusive ("input length not calibrated").

**Endpoints that do not report prompt tokens.**
- Inputs are sized by a 4 characters/token estimate, and the report says "estimated; endpoint did not report token counts".
- Actual token counts show as *not reported*, never 0.
- Large-input stress is inconclusive in that case, because the length cannot be verified.

**Context window.**
- If the server context window is known (deployment metadata, e.g. `max_model_len` → `serving.context_limit`, possibly discovered at run time) and a shape's input target + output cap exceeds it, that shape's points are not sent. They are recorded `blocked` with the reason and source (the known-impossibility rule in §5).
- Runtime snapshots read `deployment.fields["serving.context_limit"].reported.value` first, then `.configured.value`, then `target.deployment.serving.context_limit`, retaining the fact's provenance in the source.
- Otherwise, runtime context-length errors (HTTP 400 with a context message) are reported on that point as "context window exceeded" errors. Load timeouts and context rejections count toward the point's error %, while retaining the existing stress policy: they do not increment core serving errors or the unresponsive watchdog. Connection failures still feed the watchdog. Other HTTP failures retain the normal serving-error policy. They have a remedy that names the shape and the token sizes.

### 3.7 Measurements per test point

- **Requests:** sent, completed, failed, timed out, cancelled by run stop or deadline.
- **Responses:** valid (completed with a usable answer), scored, correct.
- **Reasoning-budget-exhausted responses:** reported separately. They are neither errors nor valid responses.
- **Requested concurrency:** N.
- **Client-delivered concurrency:** time-weighted mean in-flight and peak during the sending window.
- **Server-evidenced concurrency:** from existing overlap evidence (`overlap.py`, as used by `refill` / `load_qualified`).
- **Load qualified:** N = 1, or evidenced peak ≥ N and client time-weighted in-flight ≥ 0.9 × N. Interference instead qualifies the N−1 continuous short streams and enough matched probes in each group (3.4), reporting short-stream mean, peak and server-evidenced concurrency separately. Non-streaming points at N > 1 cannot be evidenced. Their load is reported as "client-delivered only; server overlap not evidenced" and is not qualified.
- **Latency:** first-output p50/p95, response p50/p95, longest stream pause. With few samples, p95 is effectively the maximum; "How these are measured" says so.
- **Throughput:** steady-state interval = [first completion in the point, end of sending window].
  - completed requests/second = completions inside that interval ÷ its length;
  - aggregate output tokens/second = `completion_tokens` of those completions ÷ its length, *not reported* if any count is missing.

  If D < 3 × median response time, throughput is flagged "window shorter than responses; throughput unreliable", and the plan warns when the expected response time (output cap ÷ 30 tok/s) suggests this. Warnings use readable combination names (e.g. "Short input → Long output"). The throughput warning appears beside Duration per workload and once in each plan display; it is not also attached to the profile output cap.
- **Token lengths:** actual input tokens (median / min / max) vs target; output tokens (median / min / max) vs cap; cached prompt tokens where reported.
- **Window:** delivered window seconds, drain seconds.

## 4. Planning and time

- **Configured load time** for this check = Σ over enabled load types of (workloads × levels × D), where workloads are:
  - combinations: the number of selected shapes;
  - mixed: 1;
  - interference: 2 (the 2D window);
  - stress: the number of enabled cases.
- Example: 9 combinations × 3 levels × 30 s gives 13 min 30 s for that load type.
- **Plan display.** The plan shows per-load-type rows and a check total labelled **Configured load time**, kept distinct from estimated total elapsed time. Elapsed time adds calibration, drain (up to the request timeout per point), other checks and repeats per target.
- **Fixed-window minimum.** Configured load time × sequential targets is added to `_time_plan`'s fixed-window `minimum`, so the existing fixed-window error and `time_warnings` cover the whole run.
- **Request estimate.** The plan states that duration-based points send as many requests as the endpoint completes. It shows the request warning from 3.5 and the throughput warning from 3.7.
- **`request_plan` rows for `concurrency`.** One row per load type, with count `None` (duration-bounded), plus calibration counts. The UI must never show "0 requests" for them.
- **Recovery's budget split.** It must know this check's reservation (3.5).

## 5. Scoring

Per test point, in this order. **Cut short / not run / insufficient evidence can only downgrade a would-be pass. They never mask a failure.**

1. **Blocked.** Configuration impossibility discovered at run time (e.g. a known server context exceeded), with reason.
2. **Error rate.** Error % = (failed + timeouts) ÷ sent × 100. Requests cancelled by the run stop or deadline are excluded but reported. If error % > `max_error_percent` → **fail**. This applies even when the point was cut short or the run stopped (e.g. the error-threshold stop). Calibration failures for the shape → **fail**.
3. **Output cap.** Any `completion_tokens` > requested cap → **fail**.
4. **Correctness.** Every shape uses the label scorer: the label must appear first, allowing one surrounding emphasis/code/quote layer and an optional leading `Label:` / `Box label:` prefix, following built-in `exact_matches` leniency. Additional introductions, wrong labels and nested wrappers are incorrect. Let wrong = scored − correct.
   - Correct % < `min_correct_percent` with scored ≥ `min_valid_responses` → **fail**.
   - Also **fail** early when wrong alone already exceeds the allowance at the minimum sample size: wrong > floor(`min_valid_responses` × (1 − `min_correct_percent` / 100)).
   - If nothing is scored, correctness is "not applicable".
5. **Timing.** First-output p95 > target, or response p95 > target → **fail**. If client scheduling delay explains the violation (existing qualification), the point is **inconclusive** instead.
6. **Interference.** Slowdown > `max_slowdown` with enough matched samples → **fail**.
7. **Evidence → inconclusive with reason.** Any of:
   - not run;
   - cut short without meeting the 3.5 evidence rule;
   - valid responses < `min_valid_responses`;
   - load not qualified (requested load not delivered);
   - long-input shapes with median cached prompt tokens > 50% of the prompt;
   - input length not verified (stress large input);
   - long-output coverage too low (stress large output: covered valid responses < `min_valid_responses`, where covered means output ≥ `ceil(long.output_tokens × min_output_percent / 100)`). This is never a failure on its own; an early end is not an endpoint failure;
   - interference: matched long-probe or short-probe samples < `min_valid_responses`, buffered stream, or `max_slowdown` null (measured only).
8. **Pass.** Otherwise **pass**. Length coverage alone never passes a point; the common criteria must pass too.

**Aggregation.** Load-type status = aggregate of its points (fail > blocked > inconclusive > pass). Check status = aggregate of load types. A passing check summary counts its test points and load types, with the number of budget-limited windows when present: "All 14 test points passed across 4 load types (2 ran budget-limited windows)." Other summaries name only points with the deciding status, up to three with their reasons, then "and N more". Point reasons do not repeat their status; a supported shortened pass says "Budget-limited window: ran 1.85 s of 3 s". The best result is "highest observed among tested settings", never a capacity limit; it is shown with the result measurements.

**Remedies** (via `remedy_conditions` / `_verdict_summary`, by reason code):

| Reason | Guidance |
|---|---|
| budget-limited | For an inconclusive point, uses observed requests/second × the remaining Σ(level × D) to suggest a total request limit. |
| time-limited | Raise Total time limit (`max_duration_seconds`). |
| run stopped | Report the stop cause; no raise-limit remedy. |
| load not delivered | Raise Maximum at once, or server-side concurrency, e.g. `OLLAMA_NUM_PARALLEL` / vLLM `--max-num-seqs`; the existing remedy text has examples. |
| insufficient coverage | Raise the output cap or lower Minimum output length. |
| context exceeded | Lower the input or output size, or use a server with a larger window. |
| client delay | Existing remedy text. |
| missing slowdown limit | Existing remedy text. |

Old `remedy_conditions` keys (`fairness`, `mixed`, `buckets`, `token_stress`) are removed. `_budget_guidance` must not suggest "raise max_requests above X" from known counts for duration points.


## 6. Structured errors and UI

**Validation errors** use the existing `{loc, msg, field, fields, values}` shape with the paths in section 1. `settingDestination` maps `concurrent_load.*` paths to drawer fields. `focusSetting` opens the step, the drawer, the load-type panel and any `<details>`, then focuses the field.

**Drawer** (side drawer opened from the Concurrent load card on Choose your checks), in order:

1. **Data profile.** A compact table with rows Short/Medium/Long and columns "Input tokens · target" and "Output tokens · cap". Note: "Used by the load types below. Actual token lengths are reported; answers can finish before their cap."
2. **Load types.** Four cards. Each has a checkbox that opens its panel when checked, and an independent collapse button with aria-expanded.
   - **Input → output combinations:** a 3 × 3 grid of shape checkboxes (rows = input, columns = output, each labelled with its token sizes), Select all / Clear, and a count line.
   - **Mixed load:** "Share of requests (%)" inputs per shape as a 3 × 3 grid; empty = 0. A live total shows "100% total" or "85% total · must equal 100%". Note: percentages are request frequency, not size or time; the observed mix is reported.
   - **Long input during an active short stream:** a trigger select, a chunk count (shown only for after_chunks), the allocation explanation for the current levels ("At 4 concurrent requests: 3 short streams + 1 probe slot alternating a short and a long input every 5 s; 20 s total"), and "Maximum slowdown vs short-only control" with an explanation ("1.5× allows short answers to take at most 50% longer to stream").
   - **Large-input / large-output stress:** two checkboxes and "Minimum output length (% of cap)". The live text below reads, for the defaults, "At least 103 generated tokens per long-output response (80% of the 128-token cap). This establishes length coverage; other pass criteria still apply." It uses `ceil` and recalculates when the percentage or long output cap changes.
3. **Load schedule.** "Concurrency levels" and "Duration per workload at each concurrency level (seconds)". Note on replenishment and that samples do not stop workloads.
4. **Pass criteria.** One `<details>` containing the five common fields, with the test-point example built from the current selection ("for example, Long input → Short output at 4 concurrent requests"). Blank timing = measure only.

Removed from this flow: "Streaming fairness pairs", "Context length", generic pass-criteria fields for this check, Samples per cell, Pairs per interference scenario, Long-input/Long-output weight, Maximum seconds per matrix cell, Sustained load, any data-profile override, and the word "stage".

**Plan.** The live plan in the drawer and the run-plan sidebar shows configured load time, per-load-type breakdown ("3 workloads × 2 levels × 15 s"), test points, highest level, and errors with "Edit …" buttons. Counts use singular or plural as appropriate ("1 workload", "1 level", "1 test point", "1 load type", "1 concurrent request"). The catalogue card summary reads e.g. "3 load types · 1, 4 concurrent".

**Results** (run detail, live cards, HTML report):
- Per load type, a table of test points with: workload, level, delivered/configured window, sent/completed, errors (failed / timeouts), valid / required, correct %, observed concurrency (mean / peak), first-output p95, response p95, req/s, output tok/s, input tokens actual vs target, output tokens actual vs cap, and status with reason.
- Columns distinguish requested, client-delivered and server-evidenced concurrency.
- Mixed also shows observed vs requested mix, by requests sent and by in-flight time. Interference shows control vs probe medians, matched count and slowdown. Stress shows coverage counts and the token threshold.
- A short "How these are measured" note explains the sending window, drain, observed concurrency and missing token counts.
- Live cards group conditions by load type and level, using readable condition names.
- Old reports with `metrics.scenarios` keep their current rendering.

**Conditions.** Ids `load_{type}_{workload}_c{N}`:
- `load_combo_long_short_c4`
- `load_mixed_c4`
- `load_interference_control_c4`
- `load_interference_probe_c4`
- `load_stress_input_c4`
- `load_stress_output_c4`
- `load_calibration_{size}_{variant}`

Labels such as "Long input → Short output at 4 concurrent requests", "Mixed load at 4 concurrent requests", "Short-only control at 4 concurrent requests", "Long input during short streams at 4 concurrent requests" and "Calibrating Long input tokens (long-output prompt)" must match between Python `conditions.py` and JS (parity test).

## 7. Report data shape

Phase 1 emits the following JSON contract. All times are seconds unless a field
ends in `_ms`. Unknown measurements are JSON `null`, including unknown token
counts; zero is used only for an observed zero count. Request bodies retain the
existing retention policy.

`checks[id="concurrency"].metrics` contains:

- `configured_load_seconds`: sum of all configured sending windows per target.
- `migration_notes`: the notes carried into this run (also stored at
  `manifest.config_migration_notes`).
- `mixed_seed`: the configured integer seed or an integer derived from the run id, always 0..2^53−1 (round-trips through JavaScript without precision loss).
- `calibrations[]`: one entry per `(size, variant)` actually attempted, with
  `size`, `variant` (`short_answer` or `long_output`), `target_tokens`,
  `filler_chars`, `input_tokens`, `request_ids`, `status` (`calibrated`,
  `estimated`, `inconclusive`, or `fail`), `reason`, and `reason_code`.
- `load_types`: object keyed by enabled type (`combinations`, `mixed`,
  `interference`, `stress`). Each value has `title`, aggregate `status`, and
  `points[]`, in execution order (workload, then ascending level).

Every `points[]` entry has:

| Fields | Meaning |
| --- | --- |
| `id`, `type`, `workload`, `level`, `shape` | Stable point identity. `shape` is null for mixed/interference; workload is a shape for combinations, `mixed`, `interference`, or stress `input`/`output`. Interference point id is its probe condition id. |
| `status`, `reason`, `reason_code` | Verdict and explanation. Unreached points normally use `status="inconclusive"` and `not_run=true`; independent failure evidence (including cached calibration failures) retains `fail`. Runtime context impossibility uses `status="blocked"`. |
| `configured_window_seconds`, `delivered_window_seconds`, `drain_seconds` | Requested sending time, actual sending time, time after sending ended until all requests returned. Interference configured time is 2D. |
| `sending_start_ms`, `sending_end_ms` | Optional run-relative sending boundaries; absent when no window began. |
| `request_allowance`, `reserved_later_requests` | Point allowance and reservation for checks after this one. Reservation is present when allocation was reached. |
| `cut_short`, `not_run`, `window_reason`, `request_ids` | Delivery/evidence state, underlying interruption reason, all measured requests (including drain). |
| `sent`, `completed`, `failed`, `timed_out`, `cancelled` | Request accounting. Completed includes invalid/reasoning-only completions; `failed` also includes unusable non-reasoning completions. Cancellation is reported separately and excluded from the error denominator. |
| `valid`, `required`, `scored`, `correct`, `reasoning_budget_exhausted` | Response accounting and the minimum valid evidence requirement. Reasoning exhaustion is neither an error nor valid evidence, including records whose transport status is `failed`. |
| `error_percent`, `correct_percent`, `correctness_applicable` | Percentages use 0–100. Correctness is null/not applicable when no responses were scored. |
| `requested_concurrency`, `client_mean_inflight`, `client_peak_inflight`, `server_evidenced_concurrency`, `load_qualified`, `concurrency_reason` | Requested load, time-weighted client delivery, peak server output overlap, qualification and any non-streaming limitation. |
| `first_output_p50_ms`, `first_output_p95_ms`, `response_p50_ms`, `response_p95_ms`, `longest_stream_pause_ms` | Valid-response latency measurements; longest pause is the maximum reported answer-stream gap. |
| `steady_state_seconds`, `requests_per_second`, `output_tokens_per_second`, `throughput_warning` | First-completion to sending-end interval and its throughput; token rate is null if any included completion lacks usage. A short-window warning flags unreliable throughput. |
| `token_lengths[shape]` | Per-shape `input_target`, `output_cap`, and `input_tokens`, `output_tokens`, `cached_prompt_tokens` distributions, each `{median, min, max}`. Missing distributions contain three nulls. |
| `timing_quality` | Existing scoped client scheduling-delay evidence, with affected request ids/metrics and raw samples retained. |

Conditional fields:

- Mixed: `observed_mix[shape]` contains `requested_percent`, `sent`,
  `sent_percent`, `inflight_seconds`, `inflight_percent`. In-flight time includes
  each sent request's drain. Request shares are request frequency, never time.
- Interference: `interference.control` and `.probe` contain `matched_count`,
  `request_ids`, `decode_median_ms`, `response_median_ms`,
  `longest_stream_pause_ms`. The enclosing object also has `slowdown`,
  `response_time_ratio`, `buffered_stream` (>50% valid short streams buffered),
  `valid_short_streams`, `buffered_short_streams`, `ambiguous_short_streams`,
  `ambiguous_request_ids`, `allocation`,
  `trigger_latency_ms: {median,min,max}`, `probes_never_triggered`, and four
  `sub_windows[]: {index,phase,probe_shape,configured_seconds}` entries (A/B/A/B).
  The point additionally carries `short_stream_mean_inflight`,
  `short_stream_peak_inflight`, `short_stream_evidenced_concurrency`, and
  `short_stream_load_qualified` (continuous-stream evidence alone). Overall
  `load_qualified` also requires enough matched short streams in each group.
- Output stress: `coverage` has `threshold_tokens`, `covered_valid_responses`,
  `required`; the threshold uses ceil, not rounding to nearest.
- Runtime impossibility/calibration: optional `blocked` (reason string),
  `calibration_failure` (reason string), or `input_not_calibrated` (boolean).

Reason codes currently emitted: `context_exceeded`, `calibration_failed`,
`error_rate`, `output_cap`, `correctness`, `timing`, `slowdown`, `client_delay`,
`budget_limited`, `time_limited`, `run_stopped`, `insufficient_responses`, `load_not_delivered`,
`cached_input`, `input_not_calibrated`, `input_not_verified`,
`insufficient_coverage`, `buffered_stream`, `insufficient_matches`,
`missing_slowdown_limit`, and `criteria_met`. Calibration additionally uses
`tokens_not_reported` and `minimum_prompt_length`. The status is authoritative: `budget_limited`, `time_limited`, or `run_stopped` may be a supported pass or an inconclusive result. Passing points never receive raise-limit remedies.

Each measured `RequestRecord` has dedicated `load_point`,
`requested_input_tokens`, and `requested_output_tokens` fields.
`load_point` contains `point_id`, `type`, `workload`, `level`, `shape`,
`sub_window` (0–3 for interference, otherwise null), and `role`
(`request`, `short_stream`, `probe`). Calibration uses
`{type:"calibration",input_size,variant}` instead. `condition_id` groups the
live condition; mixed `scenario` retains `load_mixed_cN.shape` for baseline
shape groups, while its `condition_id` is `load_mixed_cN`. Interference scenarios
are `load_interference_cN.short_stream`, `.short_probe`, `.long_probe` for
baseline grouping; `condition_id` retains the control/probe sub-window identity.
The optional dedicated `RequestSpec.condition_id` supplies that live grouping.
`cached_prompt_tokens` retains the existing cache-usage field. No new concurrent
load data is stored in `workload`.

The check executes at the former workload-engine position, after arrivals and
prefixes and before sessions/consistency/tools. Its result occupies the former
fairness position in the check list. Recovery reserves its point and calibration
minimum before sustained traffic. Existing reports with `metrics.scenarios`
continue to use the legacy renderer.

Live `condition_finished` events include the same scored point snapshot in
`condition_summary.load_point_result` (also exposed on the live condition row).
Interference's control and probe condition rows carry the same point id; the UI
deduplicates that id per target before rendering its results table. Pending
conditions carry no snapshot, so the UI never invents a result before scoring.
