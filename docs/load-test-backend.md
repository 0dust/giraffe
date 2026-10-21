# Load test backend (issue 45, Phase A)

`load` replaces the `concurrency`, `capacity` and `arrivals` execution paths.
Its dedicated configuration is `RunConfig.load_test`; generic `test_options.load`
request or criteria overrides are rejected. Old reports retain their renderers.
The final issue and `load-test-design.md` define the complete product; the drawer
and live result UI are Phase B.

## Defaults

| Setting | Default |
| --- | --- |
| Short input target / output cap | 128 / 64 tokens |
| Medium input target / output cap | 512 / 256 tokens |
| Long input target / output cap | 2,048 / 512 tokens |
| Isolated combinations | Enabled: short_short, long_short, short_long |
| Mixed traffic | Disabled; all nine shapes selected, equal shares |
| Inactive custom shares | First eight grid cells 11%, long_long 12% |
| Interference | Disabled; streaming_started trigger, maximum slowdown 1.5× |
| Token stress | Disabled; all three cases selected, minimum output 80% |
| Maintained concurrency | Enabled, levels 1 and 4 (inherited levels respect run ceiling) |
| Constant arrivals | Enabled, 1 and 2 requests/second |
| Poisson arrivals | Disabled; shared schedule/mix seed 42 |
| Bursts | Disabled; 32 requests, first at 10 s, gap 10 s |
| Sending / finish allowance | 15 / 15 seconds per window |
| Request timeout | 60 seconds |
| First-answer / completion limits | 2,000 / 30,000 ms per response |
| Errors / correct answers | 0% / 100% |
| Stream pause / generation speed limits | Blank (measure only) |
| Minimum valid responses | 5 per shape at each workload/traffic point |

The brief's smaller default schedule replaces the mock's exhaustive selection;
other sizes and response controls follow the mock. Default mixed, interference
and stress experiments are off to keep the default schedule bounded. Sparse
local profile dimensions inherit these token defaults, never Context length or
the suite output cap. A long-output stress response needs ceil(512 × 80%) = 410
answer tokens. Unknown token lengths remain unknown. Known endpoint context
windows constrain targets; client Context length never establishes server limits.

## Execution and evidence

Order: isolated combinations, one mixed workload, stress cases, interference.
For each standard workload: increasing concurrency levels, increasing Constant
rates, increasing Poisson rates, then bursts-only if neither arrival pattern is
selected. Additional migrated schedules follow the primary schedule separately.
Constant + bursts and Poisson + bursts are independent tests. Bursts never apply
to maintained concurrency or interference. Each interference level ≥ 2 has a
full baseline window followed by a full probe window, each with its own finish
allowance. The probe slot introduces one long request at a time only while all
N−1 required short streams have reached the trigger. Matching compares short
completion times over actual long-prefill overlap and corresponding baseline
intervals. Unexercised triggers, insufficient overlap or short-stream occupancy
leave the comparison inconclusive.

The shared `traffic.run_stage` owns immediate admission, drop accounting,
late-arrival handling and cleanup. Load traffic uses exponential Poisson intervals;
legacy traffic scheduler callers retain their historical conditioned schedule.
Arrival and shape RNGs are independent. The seed reproduces offers and the request
mix, including bursts; it does not reproduce generated model output.

A full client slot pool drops an offer without queuing. Scheduler lateness over
100 ms uses the existing scheduler's discard policy and is an evidence problem,
not an endpoint failure or a configurable drops allowance. The run request budget
retains #26's scheduled-offer accounting, including local drops; dispatched
requests are reported separately. Without a run ceiling, admission is
min(512, ceil(max selected rate × request timeout) + burst size). Bursts start
strictly before the sending-window end. At 60 s, first 10 s, gap 10 s, size 32,
there are five bursts / 160 requests. Poisson 10/s plus bursts expects about 760;
both arrival patterns plus bursts expect about 1,520 across two tests.

Response criteria use each response, never p95. Arrival timing includes planned
arrival-to-dispatch delay; maintained concurrency uses dispatch timing. Qualified
client scheduling delay makes affected timing violations inconclusive. Independent
endpoint errors or incorrect answers still fail. Error percentage divides endpoint
failures plus timeouts by dispatched responses excluding cancellations; local
not-sent offers are excluded. Missing scoring, timing, delivery or length coverage
cannot establish accepted capacity. Without a first-answer or completion limit,
standard points are measure-only. Highest accepted values are highest tested,
per workload and pattern; burst overlays remain distinct.

Throughput includes completions during finishing and divides by sending plus
actual finishing time. Sending-window completion counts are also retained.
Aggregate output tokens/sec and correct work meeting limits are separate from
per-response generation speed. Saved question text, case scoring, weights and
output budgets remain authoritative; token targets and shortfalls are reported
rather than replacing user questions with calibration prompts.

## Planning and migration

`request_plan.load_test` contains expansion, execution order, exact Constant/burst
counts, approximate Poisson counts, unknown completion-driven counts, and separate
sending/finish allowances. Request breakdown rows remain available. #26 budget
warnings, time policy, observation-based request estimates and the open-file guard
use the same points. A budget too small even for initial concurrent groups is an
error. Estimates fitting a budget never guarantee Poisson or concurrent completion.

#48 profiles and combinations copy over. Positive mixed shares become an explicit
custom mix. Stress long_long starts off. Old drain bounds become finish allowances.
P95 limits are not converted: notes explicitly announce the new per-response
2,000 / 30,000 ms defaults. Legacy profiles that violate strictly increasing sizes
require correction through the structured profile errors rather than silent resizing.

Legacy capacity maps its traffic mix to shapes and its rate/seed/duration to an
independent arrival test. Character/word sizes are not relabeled as tokens.
Legacy arrivals become duration-based Constant or bursts-only traffic (fractional
durations round up); the queue and local admission controls are dropped with notes.
Multiple selected legacy checks are persisted as typed
`load_test.migrated_tests: [{source, settings}]`, each carrying its own full settings.
They are never blended into an accidental overlay. Migration notes use the existing
CLI/web channel. New and legacy executions are not interchangeable baseline evidence.
