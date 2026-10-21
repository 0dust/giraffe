# Load test (issue #45, final version): implementation brief

**Authoritative sources**
- Final issue text: `scratchpad/issue45-now.md`.
- Final interactive mock: `scratchpad/mocks45/combined-inner.html`. Its logic is extracted in `scratchpad/mocks45/combined-app.js`, and its text and controls in `scratchpad/mocks45/combined-text.txt`.
- Where this brief and the issue disagree, the issue wins. Where both are silent, follow the mock's markup and logic.

**Branch context**
- This branch already contains the merged #48 **Concurrent load** check (`concurrent_load`), which this work replaces and extends.
- It also contains #26: optional run limits (`concurrency`/`max_requests`/`max_duration_seconds` may be `None`), `default_concurrency`, per-check request fields, a Run plan duration estimate, an open-file guard and caps in `caps.py`. Keep all of #26 working.

## 1. One check replaces three
- **New check id `load`, titled "Load test".** It replaces the picker entries `concurrency` (Concurrent load), `capacity` (Capacity under load / Incoming request rate) and `arrivals` (Arrival traffic / Bursts and queueing).
- **Unrelated checks** remain unchanged.
- **Old check ids** stay readable in saved reports (old renderers), and are migrated on config input (§7).
- **Picker copy:** "Load test" with the hint "Workloads, request rates and bursts in one place."

## 2. Config: `RunConfig.load_test`

```
load_test:
  profile: {short|medium|long: {input_tokens, output_tokens}}   # reuse the existing LoadSize/DataProfile semantics and validation (increasing sizes)
  workloads:
    combinations: {enabled: bool, selected: [shape,...]}         # 3×3 grid; non-empty when enabled
    mixed: {enabled: bool, selected: [shape,...] (≥2), share_mode: "equal"|"custom", shares: {shape: percent}}   # custom: each selected share > 0 and the shares total exactly 100
    interference: {enabled: bool, trigger: "streaming_started"|"after_five_chunks", max_slowdown: float > 1}
    stress: {enabled: bool, cases: ["long_short","short_long","long_long"] (≥1), min_output_percent: 1..100}
  patterns:
    concurrent: {enabled: bool, levels: [int increasing, 1..512]}   # also used by interference (levels ≥ 2 only)
    constant: {enabled: bool}
    poisson: {enabled: bool, seed: int ≥ 0}
    rates: [float increasing > 0]                                   # shared by constant and poisson
    bursts: {enabled: bool, size: int ≥ 1, first_seconds: ≥ 0, gap_seconds: > 0}
  schedule: {duration_seconds: int ≥ 1, finish_seconds: ≥ 0}        # "Duration per workload, pattern and level"; "Wait for unfinished requests after each test"
  request_timeout_seconds: > 0
  pass_criteria: {max_first_answer_ms: null|>0, max_completion_ms: null|>0, max_error_percent: 0..100, min_correct_percent: 0..100,
                  max_stream_pause_ms: null|>0, min_generation_tokens_per_second: null|>0, min_valid_responses: int ≥ 1}
```

**Defaults**
- Copy the mock's defaults wherever they fit the default run. For a default suite that fits the #26 defaults, choose:
  - combinations: short_short, long_short, short_long;
  - patterns: concurrent levels [1, 4], plus constant at rates [1, 2];
  - duration 15 s, finish allowance 15 s.
- Document every default.

**Validation** (structured errors with `fields`/`actions`, following the #26 conventions). Mirror the mock's issues in `update()`:
- token sizes positive and increasing;
- at least one combination;
- mixed needs at least two types; custom shares positive and totalling 100;
- at least one stress case;
- at least one workload;
- a pattern is required when standard workloads are selected;
- levels and rates positive and increasing;
- duration/finish/seed/burst values valid;
- the first burst must start before the window ends;
- a single burst larger than the ceiling → error naming the ceiling;
- levels above the ceiling → error;
- interference needs a level ≥ 2, otherwise an actionable error linking to levels;
- percentages 0–100;
- slowdown > 1;
- output percent 1–100;
- budget conflicts:
  - expected scheduled requests above the budget → warning or error, as #26 policy applies;
  - the budget cannot cover the initial concurrent groups → error, as in the mock;
- configured windows plus allowances above the total duration → the #26 time-budget policy.

**Context limit:** do not use Context length for sizing; validate against a known endpoint context window.

## 3. Expansion and execution
- **Standard workloads:** each selected combination is its own workload. Mixed is one workload. Each stress case is its own workload.
- **Pattern points for each standard workload:**
  - one point per concurrency level (Maintain concurrent requests);
  - Constant: one point per rate, with bursts added when Bursts is on;
  - Poisson: one point per rate, with bursts added when Bursts is on;
  - **Bursts only**: one point, when Bursts is on and neither Constant nor Poisson is selected.
- **Both arrival patterns with bursts:** two separate overlaid tests (Constant + bursts and Poisson + bursts). Never a blended pattern, and never an extra burst-only test.
- **Interference:** separate, at levels ≥ 2 only. Each level runs a baseline window, then a probe window. Each window gets the full duration plus the finish allowance. At level N: N−1 short streams (short_short) alone, then the same with room for one added long_short request, sent per the trigger, only while the required short streams are active. If it can't be exercised → inconclusive.
  - Reuse #48's matching and overlap evidence, but the comparison metric is **short-answer completion time**, plus extra pauses reported.
  - Constant/Poisson/bursts never apply to interference.
- **Order:** an explicit, documented execution order. Points run sequentially. Each point is a sending window of `duration_seconds`, then up to `finish_seconds` for outstanding requests, then the rest is cancelled and accounted as cancelled.
- **Maintained concurrency:** individual replenishment (reuse #48's executor), no batch barrier. A sample count or minimum-valid threshold never ends a window early.
- **Arrivals**
  - Reuse the existing `traffic.py` scheduler: constant is evenly spaced; Poisson uses the seed (same seed and config → same planned schedule).
  - Mixed traffic picks request shapes by seeded random selection from the shares; bursts use the same mix.
  - Bursts are scheduled at `first + k·gap` for all times strictly before the window end, with `size` requests each, sent without waiting for earlier requests.
  - Overlays add the burst offers to that arrival test.
  - **Admission:** when all client slots are occupied, the offer is recorded as **not sent (local drop)**. It is never queued silently, never reshapes the schedule, and is never counted as an endpoint failure. Record dispatch delay and actual burst spread.
  - **Slot limit for arrival and burst points:** the run ceiling when it is set. When it is Not set, use `min(512 cap, ceil(max_rate × request_timeout) + burst size)`.
  - The drawer shows no second local cap. It shows "Concurrency ceiling · Run limits: N" with an Edit limit link, or "Not set (admission up to N)".
- **Timing clocks**
  - Arrival and burst timing starts at the **planned arrival**, including dispatch delay.
  - Maintained-concurrency timing starts at dispatch.
  - Results explain which clock applies.
- **Run limits:**
  - **Cutting work short:** the run-wide ceiling, budget and duration may cut work short, and a truncated point must be labelled as such.
  - **Stop handling:** keep the endpoint-unresponsive stop and the explicit stop reasons (#36).

## 4. Pass criteria: per response, not p95
- Replace #48's p95 criteria with the seven per-response criteria in §2. Reuse the existing per-request violation machinery: `_timing_violations` / `qualified_violations` and the client-delay qualification that turns a violation into inconclusive.
- Per **result group**: a request type at one traffic pattern and level/rate. In mixed workloads, each shape is a separate group.
  - **Fail** when a response violates a set limit, when the error % (with its denominator) exceeds the allowance, or when correct % over scorable answers is below the requirement (missing judgements stay unknown).
  - **Inconclusive** when valid responses < `min_valid_responses`, timing evidence is missing, delivery is incomplete (local drops or late arrivals), or required length coverage is missing.
  - Percentiles remain report metrics only. Blank timing or speed means measure only. Do not add a "Dropped arrivals allowed (%)" criterion.
- **Kept inside their workloads:**
  - interference: max slowdown — exceeding it fails the comparison, and the common criteria still apply;
  - stress: minimum generated output % with `ceil(cap × pct / 100)` — coverage alone never passes.
- **Capacity claims** need a first-answer or completion limit. Without one, report measure-only and never "Highest accepted".

## 5. Planning (Run plan + the drawer plan, as in the mock)
Report:
- scheduled load tests (standard workloads × points), plus the short-answer checks (comparison levels);
- sending / maintained-load time;
- the total with finish and comparison allowances, as a configured upper allowance excluding setup and other checks;
- expected scheduled requests:
  - exact for constant and bursts;
  - approximate for Poisson;
  - "Unknown + N" when concurrent or interference work is selected;
- the execution order list, matching the mock's wording;
- the burst preview: "5 bursts per test: at 10, 20, 30, 40, 50 seconds. 160 additional requests per arrival-pattern test".

Worked examples that must hold (tests):
- 60 s window, first burst at 10 s, gap 10 s, size 32 → bursts at 10/20/30/40/50 s = 160 requests.
- Poisson 10/s plus those bursts ≈ 760.
- Burst-only = 160.
- Constant + Poisson with bursts = two tests, ≈ 1,520 expected.
- Each comparison level adds 2 windows.

Integrate with #26:
- the `request_plan` rows (keep the Request breakdown component);
- the time plan and estimate: fixed windows are now load windows plus finish allowances;
- budget warnings, the open-file guard (`required_concurrency` includes the arrival/burst admission limit) and structured errors.

## 6. Reporting (UI results + HTML report)
- **Separate results** by workload/request shape × pattern × level/rate × burst settings.
- **Report per group:**
  - requested, scheduled and dispatched counts;
  - observed concurrency and observed request mix;
  - valid completions, failures, timeouts, cancellations, local drops and late arrivals;
  - actual input/output lengths and coverage (unknown stays unknown);
  - first-answer and completion latency, stream pauses and generation speed;
  - completed requests/second and aggregate output tokens/second, with explicit windows and how finish-allowance completions are handled;
  - "correct work meeting limits", separate from raw throughput;
  - the short-only vs long-request comparison: actual overlap, matched windows, slowdown.
- **Highest accepted values:** "Highest accepted concurrency" and "Highest accepted request rate", qualified as highest **tested**, per workload and pattern.
- **Burst overlays** stay distinct from steady-arrival capacity, with burst delivery and degradation shown separately. Never imply a proven absolute capacity.
- **Old reports** (`concurrency` with #48 `load_types`, old `capacity` stages, old `arrivals`) keep their existing renderers.

## 7. Migration (explicit notes; never silently blend)
- **From #48 `concurrent_load`:**
  - The profile is copied.
  - combinations/mixed/interference/stress map directly. Stress gains `long_long` as off. Mixed shares carry over with `share_mode: custom` and `selected` set to the shapes with share > 0.
  - Schedule levels map to the concurrent pattern levels; the duration is copied; finish = the old drain bound (request timeout), with a note.
  - p95 criteria become per-response limits **only** with a note saying the semantics changed: p95 first-output → max first answer per response. Or don't convert and use defaults with a note. Pick the honest option and state it.
- **From legacy `capacity` (`traffic`):**
  - rates → constant or poisson per `traffic.arrival`, with the seed;
  - duration per stage → `duration_seconds`;
  - the legacy fixed traffic mix → a mixed workload of the equivalent shapes, with a note, plus the old long_input/long_output sizes noted as not converted, since they were characters/words;
  - `max_in_flight` → dropped, with a note saying the ceiling now governs admission.
- **From legacy `arrivals`:**
  - steady → constant at that rate;
  - burst → bursts-only (or an overlay) with size/interval;
  - `requests` count → the duration derived and noted, or a note that count-based arrivals became duration-based;
  - `max_pending` → dropped, with a note that there is no queue now; full slots are local drops.
- Selecting any of the three old checks selects `load`. Never blend two old schedules into one test: each old check's traffic becomes separate pattern points.
- Notes go through the existing migration-notes channel. Old reports stay readable.

## 8. UI: follow the mock
- Side drawer for Load test, opened from the Choose your checks row, using the existing drawer and components.
- **Sections, in order:**
  1. **Data profile**: compact table, "Input tokens · target" / "Output tokens · cap", with the note "Used by every selected workload below. Answers may finish before their cap; actual token lengths are reported."
  2. **Workloads**: "Select a workload to open its settings. Selected workloads run separately; mixed traffic combines request sizes within its own test." There are four cards with checkbox, description and an independent collapse; checking a card opens it. Each card's content follows the mock:
     - **Combinations:** 3×3 grid plus Select all / clear.
     - **Mixed traffic:** its own 3×3 include grid, an "Equal shares / Custom percentages" select, custom % rows only for the included shapes, and a live note ("33.3% expected per selected combination; actual counts vary." or "N% total · must equal 100%.").
     - **Interference:** the 3 numbered steps text, the trigger select, the slowdown field with the live explanation from the issue, a live schedule text and an "Edit concurrency levels and duration" link.
     - **Token stress:** 3 checkboxes and min output % with the live `ceil` text.
  3. **Traffic patterns** ("Choose one or more"):
     - checkboxes Maintain concurrent requests / Constant arrivals / Poisson arrivals / Bursts;
     - Rates (shown when Constant or Poisson is on);
     - Random seed (Poisson);
     - burst fields plus the live burst preview (when Bursts is on);
     - Concurrency levels ("Used by maintained-concurrency traffic and the long-input / short-answer test");
     - Duration per workload, pattern and level (seconds);
     - Wait for unfinished requests after each test (seconds);
     - the note about duration and minimum valid responses.
  4. **Request timing & delivery**: Request timeout, "Concurrency ceiling · Run limits N" with an Edit limit link that focuses the Run limits field, and the delivery note from the mock.
  5. **Pass criteria**: one `<details>` with the seven fields, their placeholders ("Measure only"), the result-group explanation and the timing-clock notes, exactly as in the mock.
- **Plan:** the Run plan shows the load-test plan (title from the selected patterns, test count, sending time, time with allowances, expected requests, execution order, issues), plus the #26 bounds and estimate.
- **Problems:** every problem appears inline beside its field and in the plan as a button that opens and focuses the exact field (including Run limits), before Review.
- **Accessibility:** real labels, `aria-expanded` on collapses, `aria-live` only on the live summaries.
- **Results UI** per §6. Live cards group by workload × pattern × level/rate.
- **Remove** the separate Capacity and Arrivals check panels and their per-check in-flight/pending fields. Also remove the #48 drawer sections that no longer exist (Load types / Load schedule / p95 criteria).
