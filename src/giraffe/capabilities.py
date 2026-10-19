"""Machine-readable configuration discovery shared by the CLI and local UI."""

from __future__ import annotations

from giraffe.fixtures import SUITE_VERSION, builtin_fixtures
from giraffe.models import (
    CHECK_LIMIT_FIELDS, CHECK_NAMES, CHECK_OPTION_FIELDS, DEFAULT_CHECKS, CheckOptions, Limits,
    RunConfig, TrafficConfig, Arrival, Prefix, Sessions, Consistency, ConcurrentWorkloads, JudgingConfig, JUDGE_PROMPT_VERSION, JUDGE_VOTING_RULE,
    MAX_REQUEST_PACK_BYTES, MAX_REQUEST_PACK_CASES, RequestPack, session_workload_presets,
)
from giraffe.request_pack import INPUT_RETENTION_NOTICE

CHECK_DESCRIPTIONS = {
    "access": "Can Giraffe connect to your server and get an answer?",
    "serving": "Does the server return complete, valid answers without errors?",
    "first_output": "How long do users wait before an answer starts?",
    "generation": "How fast do answers arrive, and how often do they pause?",
    "capacity": "How much traffic can the server handle while giving correct, timely answers?",
    "concurrency": "Compare request sizes, mixed workloads, stream fairness and token stress at configurable loads.",
    "fairness": "Do long questions slow down a short answer already in progress?",
    "context": "Can the model recall facts from long questions?",
    "correctness": "Does the model give correct answers when requests run together?",
    "json": "Do JSON answers have the right structure and content?",
    "cancellation": "Do request limits and cancellation work, and can the server answer afterward?",
    "recovery": "Does the server recover after a period of heavy traffic?",
    "gpu": "What do the server's existing GPU measurements show?",
    "arrivals": "How does the server handle evenly spaced requests and sudden bursts?",
    "prefix": "Does the server reuse work when requests share the same opening text?",
    "buckets": "Test short, medium and long questions and answers at your chosen loads.",
    "mixed": "Do long questions or answers slow down short requests?",
    "sessions": "Can the model handle several conversations at once?",
    "consistency": "Does the model answer consistently across repeated requests?",
    "tools": "Does the model return the requested tool calls and required assistant text?",
}

CHECK_DETAILS = {
    "access": "Sends ordinary inference through your endpoint, network and authentication settings. Connect timeout is min(request_timeout_seconds * 0.9, 10) seconds, inside the request deadline. Only connection/TLS errors and HTTP 401/403 are authentication or transport failures; a response timeout remains a timeout. Capacity and sustained traffic are exempt; other stress traffic exempts timeouts/cancellation but keeps real transport failures.",
    "serving": "Checks ordinary requests for server errors, empty answers, invalid response formats, unfinished streams and timeouts. Capacity and sustained stages are exempt; recovery owns sustained errors and timeouts. Other stress traffic exempts timeouts and intentional cancellation; genuine HTTP/protocol failures still count and can trigger the error stop.",
    "first_output": "Measures the wait for useful answer text on ordinary requests. Reports the first request separately from later requests and keeps reasoning separate. Capacity/sustained traffic and other stress timeouts/cancellations are excluded; real HTTP/protocol failures remain visible.",
    "generation": "Measures ordinary response time, answer length, generation speed and gaps between answer chunks. Trailing finish, usage and close messages do not extend answer gaps. Token speed needs server-reported token counts. A streamed single-chunk answer with completion_tokens <= 2 has no applicable inter-chunk gap; longer or unknown-length single-chunk answers lack timing evidence.",
    "capacity": "Starts requests at each chosen rate independently of previous answers. Each stage owns its errors; intentional overload does not trigger the global error stop. Reports the highest tested rate for which every tested lower rate also passed, and flags non-monotonic results. Request and time budgets still apply. Single-chunk streams with reported completion_tokens <= 2 need no inter-chunk gap; longer buffered answers still lack timing.",
    "fairness": "Interleaves short-only and loaded runs of the same 48-word known-answer copy sequence with 12 tokens of budget headroom. Sends long prompts with unique opening text after first output, then requires the long request's first output strictly inside the short output window, with at least two short-answer chunks, to prove overlap. Compares first-to-last answer time and answer gaps; buffered or missing overlap evidence is inconclusive. Set a slowdown ratio to accept performance.",
    "context": "Places known facts at positions separated by at least 32 characters within each input size, then asks the model to recall them. Small budgets exercise fewer positions; reduced position_coverage is reported and prevents a pass. Non-timeout HTTP/protocol errors fail, except documented context-length rejection. Known-answer fixtures request temperature zero unless you set a temperature. Use the context length configured on your server.",
    "correctness": "Scores known answers. Built-in questions cover extraction, arithmetic and classification; concurrency above one requires a positive intersection between scored and other same-target streamed response windows, from first answer output through response completion, with some intersection outside recorded client scheduling delays. Client in-flight counts do not qualify; server-side concurrency needs streamed output timing. With your dataset, every case is scored quietly and under capacity traffic, or in a separate concurrent pass when capacity is disabled. Per-check samples/concurrency overrides are rejected in dataset mode; global samples does not choose case count. Built-ins request temperature zero unless you set a temperature. Scoring strips closed inline reasoning anywhere in the output, including prompt-opened reasoning. Only strict-prefix exact truncation is unscored; already-wrong answers fail. Judges skip truncated answers.",
    "json": "Requests JSON answers at temperature zero unless you set a temperature, strips closed inline reasoning anywhere in the output, then checks parsing, required fields, types and known content. Nested numbers compare by value (3 equals 3.0); booleans remain distinct. Reasoning-only and incomplete truncated JSON remain unscored; complete wrong JSON/schema violations fail even with a length finish. Configured concurrency above one requires a positive intersection between scored and other same-target streamed response windows, from first answer output through response completion, with some intersection outside recorded client scheduling delays; otherwise the result is inconclusive because server-side concurrency needs streamed output timing. Concurrency one establishes serial behavior only.",
    "cancellation": "Requires streaming (otherwise inconclusive). Cancels after configured answer chunks and requires first output before cancellation. Checks stop enforcement against a full-output control and stop finish reason, output caps including length finish-reason consistency, deadlines and follow-ups. Optional logs/traces can verify backend release; client disconnect alone cannot.",
    "recovery": "Refills a worker pool with scored long-input/output requests, then compares light probes with a pre-load baseline using the same fixture. Reserves fixed later work and probes; halves remaining quota only with later checks. Both load completion paths require min_samples successful sustained requests. Duration elapsed needs configured peak; consumed allowance also needs sufficient requests and at least 90% time-weighted occupancy. Sustained errors and timeouts count against recovery max_error_rate; timing limits apply to probes. These outcomes stay outside serving/first-output. Set recovery_max_ratio even with absolute timing limits; absolute-only passes state measured ratios and no ratio limit configured. No acceptance limit means inconclusive.",
    "gpu": "Reads existing GPU metrics per observed device label. Freshness needs recent source timestamps or changing values across scrapes. Missing families or unknown freshness are inconclusive; New XIDs or adverse hardware/thermal slowdown bits in at least two snapshots fail; historical XIDs and power caps are informational. gpu_min_observation_seconds sets the minimum window (default 30, accepts zero), independently of scrape interval. Waits only for the remaining time, bounded by run duration; a shorter window is inconclusive. Reuses the bounded end scrape, preserving start/end within metrics_max_samples. Fewer observed devices than deployment.hardware.gpu_count declares is inconclusive. Optional limits.gpu_max_temperature_c fails a device exceeding it in at least two snapshots; without it max_temperature_c is measurement only. Unpublished devices remain unknown without a declared count. This is not device health certification.",
    "arrivals": "Sends steady or burst arrivals independently of completion. max_pending bounds queued requests only; the global concurrency limit bounds in-flight requests. Reports dispatch, waiting and unissued arrivals. arrivals.max_client_wait_ms is the dispatch-wait tolerance (default 50 ms); exceeding it leaves the arrival rate unqualified.",
    "prefix": "Runs shared-prefix repeats back to back, then independent controls and history. A pass needs enough repeats with cached tokens covering half the estimated prefix and independent controls below that threshold. Non-discriminative cache reporting is inconclusive; only a majority of repeat misses fails. Reports k of n misses; timing alone cannot prove reuse.",
    "buckets": "Refills requests for nine input/output combinations, with counts rounded up to full load groups. Client lifetime-average in-flight count must reach level minus 0.5, the cell must reach the requested peak concurrency in overlapping streamed response windows from first answer output through completion, excluding intersections wholly inside recorded client scheduling delays, and each qualified request must overlap another response window (level 1 always qualifies); tail IDs are excluded. Server-side concurrency needs streamed output timing. Input lengths require ordered short/medium/long tokens and a sufficient token span, allowing fixed template overhead. Cell windows stop dispatch and let active requests finish.",
    "mixed": "Compares a non-repetitive known light passage of at least 64 tokens against quiet controls during long-input/output interference. Wrong light answers are reported as unqualified. Requires heavy-shape evidence and the heavy request's first output before the light request's last output, after light dispatch for before-output pairs or after its first output for active streams. Client dispatch overlap alone does not qualify. All planned pairs send both requests; buffered answers cannot qualify active decoding. Missing fairness_max_ratio leaves measurements inconclusive without hiding coverage gaps.",
    "sessions": "Refills a bounded conversation pool with distinct seeded names and scores recall against each session name. Requires session concurrency of at least two and a positive intersection between streamed response windows from distinct conversations (first answer output through response completion), outside recorded client scheduling delays; server-side concurrency needs streamed output timing. Performance profiles run weighted coding, essay and chat conversations and retain task/turn measurements without scoring answer quality. Live history feeds final answers after stripping inline reasoning; fixed history uses its seeded name at every turn. Fixed-only plans count character-admitted turns exactly; live history admission depends on actual answer lengths.",
    "consistency": "Repeats standalone non-repetitive copy prompts within each level and against sequential modal answers. Correctness is reported independently and correctness_observed appears in summaries below 1.0; strict answer differences fail. Non-strict answer variation or probability drift beyond tolerance is inconclusive: variation observed; enable strict to enforce. Full agreement can pass. Load qualification requires client lifetime-average in-flight counts plus the requested peak of overlapping streamed response windows (first answer output through response completion), excluding intersections wholly inside recorded client scheduling delays, and per-request response overlap. Logprobs copy a passage of at least 64 words and require at least 32 compared token positions before passing, reporting maximum cross-level delta and token divergences. Complete-response logprobs cannot establish server-side concurrency without streamed output timing and leave concurrent levels inconclusive. Strict cross-level equality requires batch-invariant serving; default vLLM can diverge.",
    "concurrency": "Runs selected size, interference, fairness and token-stress scenarios with separate verdicts and measurements. Size-matrix requests refill as they finish; positive intersections between streamed response windows (first answer output through response completion) and observed token lengths qualify coverage. Intersections entirely inside recorded client scheduling delays do not qualify. Client in-flight counts alone do not qualify; server-side concurrency needs streamed output timing. Insufficient limits remain inconclusive.",
    "tools": "Checks response protocol, configured tool names and arguments, and optional assistant text requirements separately. Giraffe does not execute tools.",
}


def capabilities(config: RunConfig | None = None) -> dict:
    """Describe real model fields; semantic constraints are checked by validate."""
    configured = config or RunConfig(targets=[{
        "name": "local", "url": "http://localhost:11434/v1", "model": "placeholder",
    }])
    inherited = configured.model_copy(update={"test_options": {}})
    defaults = inherited.model_dump(mode="json")
    defaults.pop("targets")  # Targets are required inputs, never discovered credentials.
    example = builtin_fixtures(configured.model_copy(update={"context_limit": 128}))["correctness"][0]
    return {
        "builtin_question_example": {"question": example.messages[0]["content"], "expected": example.expected},
        "kind": "capabilities", "schema_version": "1", "suite_version": SUITE_VERSION,
        "validation_note": "The schema describes configuration. Validate checks semantic constraints; neither command contacts endpoints or verifies runtime availability.",
        "defaults": defaults, "config_schema": RunConfig.model_json_schema(),
        "checks": [
            {"id": key, "title": title, "description": CHECK_DESCRIPTIONS[key],
             "details": CHECK_DETAILS[key],
             "optional": key not in DEFAULT_CHECKS,
             "option_fields": sorted(CHECK_OPTION_FIELDS[key]),
             "limit_fields": sorted(CHECK_LIMIT_FIELDS[key]),
             "defaults": inherited.effective_check(key)} for key, title in CHECK_NAMES.items()
        ],
        "profile_defaults": {name: model().model_dump(mode="json") for name, model in {
            "arrivals": Arrival, "prefix": Prefix, "sessions": Sessions,
            "consistency": Consistency,
        }.items()},
        "option_schema": CheckOptions.model_json_schema(),
        "limits_schema": Limits.model_json_schema(),
        "traffic_schema": TrafficConfig.model_json_schema(),
        "concurrent_workloads": {"defaults": ConcurrentWorkloads().model_dump(mode="json"),
                                 "schema": ConcurrentWorkloads.model_json_schema()},
        "session_workloads": {"defaults": Sessions().model_dump(mode="json"),
                              "presets": [item.model_dump(mode="json") for item in session_workload_presets()]},
        "judging": {"schema": JudgingConfig.model_json_schema(),
                    "defaults": JudgingConfig().model_dump(mode="json"),
                    "prompt_version": JUDGE_PROMPT_VERSION, "voting_rule": JUDGE_VOTING_RULE},
        "datasets": {
            "preview_api": "POST /api/datasets/preview",
            "preview_fields": ["text", "format", "input_field", "expected_field", "scorer"],
            "import_cli": "giraffe import-dataset --file FILE [--input-field FIELD --expected-field FIELD --scorer judge|exact]",
            "formats": ["csv", "json"], "flat_rows_require_explicit_mapping": True,
            "try_example_api": "POST /api/runs/try-example",
            "try_example_fields": ["config", "fixture_id", "target_name"],
            "try_example_cli": "giraffe try-example --config FILE --case ID --target NAME --output DIRECTORY",
            "try_example_behavior": "One selected dataset case and target; correctness only, no restart or traffic stages; configured judges run afterward.",
        },
        "request_pack": {
            "version": 1, "schema": RequestPack.model_json_schema(),
            "max_bytes": MAX_REQUEST_PACK_BYTES, "max_cases": MAX_REQUEST_PACK_CASES,
            "scorers": {
                "exact": "Case-sensitive text equality after normalizing whitespace.",
                "contains": "Case-sensitive text after normalizing whitespace, with alphanumeric boundaries; does not validate the full answer. Preview warns when expected text occurs in the case messages.",
                "json": "JSON schema validation and, when supplied, structural equality to the complete expected JSON value; numeric representations compare equally, booleans remain distinct.",
                "judge": "Reference-answer AI correctness after traffic, excluding inline reasoning, reasoning-only outputs and truncated answers; complete panel strict majority, ties/errors unscored.",
                "none": "Traffic only; never counts as correct or qualifies full-pack correct capacity.",
            },
            "input_retention_notice": INPUT_RETENTION_NOTICE,
        },
    }
