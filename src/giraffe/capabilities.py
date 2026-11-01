"""Machine-readable configuration discovery shared by the CLI and local UI."""

from __future__ import annotations

from . import __version__

from copy import deepcopy

from giraffe import caps
from giraffe.conditions import condition_labels
from giraffe.local_tokenizer import SETUP_TIMEOUT_SECONDS
from giraffe.fixtures import SUITE_VERSION, builtin_fixtures
from giraffe.models import (
    CHECK_LIMIT_FIELDS, CHECK_NAMES, CHECK_OPTION_FIELDS, DEFAULT_CHECKS, CheckOptions, CleanupCriteria, Limits,
    RunConfig, TrafficConfig, Arrival, Prefix, Sessions, Consistency, ConcurrentLoad, LoadTest, JudgingConfig, JUDGE_PROMPT_VERSION, JUDGE_VOTING_RULE,
    DEFAULT_CONSISTENCY_OUTPUT_TOKENS, SHAPES, RequestPack, session_workload_presets,
)
from giraffe.caps import (MAX_PLANNED_ARRIVALS, MAX_REQUEST_PACK_BYTES, MAX_REQUEST_PACK_CASES,
                          MAX_TOOL_CONFIG_BYTES, MAX_TRAFFIC_RATE, MAX_LIVE_REQUESTS, MAX_SESSION_TURN_CHARS)
from giraffe.request_pack import INPUT_RETENTION_NOTICE, pack_settings

CHECK_DESCRIPTIONS = {
    "access": "Can Giraffe connect to your server and get an answer?",
    "serving": "Does the server return complete, valid answers without errors?",
    "first_output": "How long do users wait before an answer starts?",
    "generation": "How fast do answers arrive, and how often do they pause?",
    "capacity": "How much traffic can the server handle while giving correct, timely answers?",
    "concurrency": "Sustain selected input/output combinations, random mixed traffic, active-stream interference and token stress at each concurrency level.",
    "context": "Can the model recall facts from long questions?",
    "correctness": "Does the model give correct answers?",
    "json": "Do JSON answers have the right structure and content?",
    "cancellation": "Do request limits and cancellation work, and can the server answer afterward?",
    "recovery": "Does the server recover after a period of heavy traffic?",
    "gpu": "What do the server's existing GPU measurements show?",
    "arrivals": "How does the server handle evenly spaced requests and sudden bursts?",
    "prefix": "Does the server reuse work when requests share the same opening text?",
    "sessions": "Can the model handle several conversations at once?",
    "consistency": "Does the model answer consistently across repeated requests?",
    "tools": "Does the model return the requested tool calls and required assistant text?",
}

CHECK_DETAILS = {
    "access": "Sends ordinary inference through your endpoint, network and authentication settings. Connect timeout is min(request_timeout_seconds * 0.9, 10) seconds, inside the request deadline. Connection, TLS, proxy and unsupported-protocol errors and HTTP 401/403 identify transport or authentication failures. Interrupted ordinary requests leave the check inconclusive. Capacity and sustained traffic are exempt; other stress traffic exempts timeouts/cancellation but keeps real transport failures.",
    "serving": "Checks ordinary requests for server errors, empty final answers, invalid formats, unfinished streams and timeouts. Length-finished empty output needs reasoning text, stripped inline reasoning or positive reasoning-token usage to qualify as budget exhaustion; otherwise it fails. Reasoning that exhausts the output budget without an answer is counted separately as reasoning_budget_exhausted, excluded from error_rate, and makes serving inconclusive; raise max_output_tokens. Run stops leave interrupted checks inconclusive. Capacity and sustained stages are exempt; other stress traffic exempts timeouts and intentional cancellation, while genuine HTTP/protocol failures still count.",
    "first_output": "Defaults to a 10,000 ms p95 acceptance limit; override with --first-output-ms or limits.first_output_ms. Measures the wait for final answer text on serial warm and generation requests, excluding inline and separate reasoning. Reports the cold first request separately. Requests with successful response headers that then fail or time out without output can establish a lower-bound violation. Concurrent correctness and other loaded traffic are excluded. Overlapping client scheduling delay makes affected timing inconclusive.",
    "generation": "Defaults to a 5,000 ms stream-gap limit when no generation timing limit is configured; explicit global or per-check stream_gap_ms: null disables this default. Measures serial generation requests after a separate cold request. Latency includes timed-out requests. Answer gaps and pace require distinct network reads, not SSE events; configured gap and pace limits each need min_samples measurable answers. Pace subtracts the estimated token share in the first answer read; an estimated first read above 16 tokens or later reads averaging above 16 tokens lack reliable pace evidence. Measured gaps also require a first-read estimate at most 16 tokens. Reasoning requires a positive reported reasoning token count; zero alongside reasoning text is unknown. Single-read answers of one or two completion tokens have no applicable gap, but do not count as measured gaps. Trailing whitespace, finish, usage and close messages do not extend answer timing.",
    "capacity": "Starts requests at each chosen rate independently of previous answers. Each stage owns its errors; intentional overload does not trigger the global error stop. Reports the highest tested rate for which every tested lower rate also passed, and flags non-monotonic results. Seeded Poisson stages condition on round(rate x duration) arrivals at sorted uniform offsets; scheduled_rps reports the actual finite-count rate. A robust median pairwise latency slope (at least six completions) plus excess end backlog or last-window occupancy disqualifies queue growth. Poisson stages below 80 samples with substantial p90-p10 latency spread are inconclusive because transient bursts and overload cannot be distinguished. Scheduler stalls excuse only overlapping metric violations when a stall accounts for at least half the overage. Request and time budgets still apply. Single-chunk streams with reported completion_tokens <= 2 need no inter-chunk gap; longer buffered answers still lack timing.",
    "context": "Places known facts at positions separated by at least 32 characters within each input size, then asks the model to recall them. Small budgets exercise fewer positions; reduced position_coverage is reported and prevents a pass. Every fact position at each size reaching 80% of the declared token limit must pass, independent of pooled accuracy; per-position outcomes are reported. Context rejection (400/413/422) fails only with an input estimate at most 70% of the declared limit, or server-reported input usage at most 85%; larger estimates or unknown sizes leave rejection inconclusive. Other non-timeout HTTP/protocol errors fail. Known-answer fixtures request temperature zero unless you set a temperature. Use the context length configured on your server.",
    "correctness": "Scores known answers with inherited concurrency of four. Fully scored answers conclude on accuracy when concurrency is inherited; absent server overlap is reported as an observation. Explicit default_concurrency or per-check concurrency overrides require concurrent evidence. Built-in questions cover extraction, arithmetic and classification; concurrency above one requires at least 1 ms of intersection between scored and other same-target streamed response windows, from first answer output through the stream-end read, before response cleanup, outside recorded client scheduling delays. Client counts of requests at once do not qualify; server-side concurrency needs streamed output timing. With your dataset, every case is scored quietly and under capacity traffic, or in a separate concurrent pass when capacity is disabled. Per-check request setting overrides are rejected in dataset mode; global samples does not choose case count. Built-ins request temperature zero unless you set a temperature. Scoring strips closed inline reasoning anywhere in the output, including prompt-opened reasoning. Built-in exact answers tolerate one trailing period only when the expected answer has none, and one surrounding markup or quote layer; user request packs and custom fixtures keep exact semantics. Length-finished contains answers and strict-prefix exact truncation are unscored; already-wrong exact answers fail. AI judges skip truncated answers.",
    "json": "Requests JSON answers at temperature zero unless you set a temperature, strips closed inline reasoning anywhere in the output, then checks parsing, required fields, types and known content. Nested numbers compare by value (3 equals 3.0); booleans remain distinct. One complete JSON fence is accepted with or without newlines. Reasoning-only and incomplete truncated JSON (including numeric prefixes and an unclosed opening fence) remain unscored; complete wrong JSON/schema violations fail even with a length finish. Configured concurrency above one requires at least 1 ms of intersection between scored and other same-target streamed response windows, from first answer output through the stream-end read, before response cleanup, outside recorded client scheduling delays; otherwise the result is inconclusive because server-side concurrency needs streamed output timing. Concurrency one establishes serial behavior only.",
    "cancellation": "Requires streaming (otherwise inconclusive). Cancels after configured non-empty generation chunks (answer or reasoning) and requires answer or reasoning output observed before cancellation. Checks stop enforcement against a full-output control and stop finish reason. Reasoning-only length completions judge the output cap on usage, including usage equal to the requested cap for a length finish; stop enforcement without an answer is inconclusive. Without usage, output caps need a length finish and a usable lower bound from answer chunks or fixture integers; exceeding the cap fails. Runs a same-fixture baseline before cancellation and an immediate follow-up afterward; first output above max(3 * baseline, baseline + 1000 ms) is inconclusive without backend failure evidence. client_deadline_self_test measures only Giraffe's client timer and supplies no endpoint pass evidence. Optional logs/traces can verify backend release; full-budget natural completion after cancellation fails when proven by native token counts or isolated Ollama cache growth. Client disconnect alone cannot prove server cancellation.",
    "recovery": "Refills a worker pool with scored long-input/output requests, then compares light probes with a pre-load baseline using the same fixture. Reserves fixed later work and probes; halves remaining quota only with later checks. An uncounted priming request precedes the baseline (budget permitting), after a bounded cleanup wait when cancellation ran. Both load completion paths require min_samples successful sustained requests, the configured peak and at least 90% time-weighted occupancy; consumed allowance also needs sufficient requests. Baseline and probes each need samples completions. Baseline non-timeout errors, sustained errors and timeouts count against recovery max_error_rate; deadline or stop cancellations are unfinished, not errors, and make recovery inconclusive. Reasoning-budget exhaustion is excluded from error counts and leaves recovery inconclusive. Priming timeouts remain outside core serving checks. Timing limits apply to probes. Unscored sustained or probe completions make recovery inconclusive. These outcomes stay outside serving/first-output. Set recovery_max_ratio even with absolute timing limits; absolute-only passes state measured ratios and no ratio limit configured. Without timing limits, recovery concludes from correctness and error rate; timing remains an observation.",
    "gpu": "Server observation; missing telemetry does not change the verdict. Hardware faults and configured temperature limits still fail. Reads existing GPU metrics per device UUID, falling back to Hostname, instance and GPU index. Freshness needs recent source timestamps or changing values across scrapes, plus non-stale presence in the first and last successful snapshots. Only successful scrapes with GPU families count as GPU scrapes. A device absent from the final complete GPU scrape or two consecutive complete GPU scrapes fails; a single recovered absence warns; stale boundary samples are inconclusive and do not count toward device coverage. Missing families or unknown freshness are inconclusive. Fresh XIDs are compared with fresh-only prior values, even when error labels change. A later first appearance fails only when initial absence is proven; blank sentinels, stale initial XIDs or truncation leave initial history unknown, warn and make an unclassifiable positive XID inconclusive. Historical XIDs and power caps are informational. Adverse hardware/thermal slowdown bits in at least two snapshots fail. gpu_min_observation_seconds sets the minimum window (default 30, accepts zero), independently of scrape interval. Waits only for the remaining time, bounded by run duration; a shorter window or an end scrape without fresh GPU samples is inconclusive. Reuses the bounded end scrape, preserving start/end within metrics_max_samples. Fewer observed devices than deployment.hardware.gpu_count declares is inconclusive. Optional limits.gpu_max_temperature_c fails a device exceeding it in at least two snapshots; without it max_temperature_c is measurement only. Unpublished devices remain unknown without a declared count. This is not device health certification.",
    "arrivals": "Sends steady or burst arrivals independently of completion. max_pending bounds queued requests only; arrivals.max_in_flight bounds requests at once, with the optional run limit shared across endpoints. Reports dispatch, waiting and unissued arrivals. arrivals.max_client_wait_ms is the dispatch-wait tolerance (default 50 ms); exceeding it leaves the arrival rate unqualified. Arrivals cancelled by the run deadline or a stop are incomplete and leave the rate unqualified. Without a timing limit (first_output_ms, latency_ms, stream_gap_ms or min_output_tokens_per_second) the result is inconclusive.",
    "prefix": "Server observation; never changes the verdict. Missing reuse reports Prefix caching not active. Runs shared-prefix repeats back to back with a fresh initial nonce per run and target, then uniquely nonced independent controls and history. Prefix controls and fixed history use the same admission estimate (one token per three generated ASCII characters, a 20-token nonce allowance, output and a 32-token template reserve). Requests exceeding that estimate and context-length rejections of generated prefix prompts are unfinished/inconclusive; actual token usage is reported separately. Built-in label scoring tolerates one trailing period or markup layer. Reasoning-budget replies are unscored and do not count as errors; their usage still supplies cache evidence. A pass needs enough repeats with cached tokens covering half the estimated prefix and independent controls below that threshold. Non-discriminative cache reporting is inconclusive; a majority of repeat misses reports Prefix caching not active; smaller-prefix misses remain inconclusive. Reports k of n misses; timing alone cannot prove reuse.",
    "sessions": "Refills a bounded conversation pool with distinct seeded names and scores recall against each session name, tolerating one trailing period or markup layer. Requires session concurrency of at least two and a positive intersection between streamed response windows from distinct conversations (first answer output through response completion), outside recorded client scheduling delays; server-side concurrency needs streamed output timing. Reasoning-budget replies are unscored and stop the session as inconclusive, without counting as request errors. Performance profiles run weighted coding, essay and chat conversations and retain task/turn measurements without scoring answer quality. Live history feeds final answers after stripping inline reasoning; fixed history uses its seeded name at every turn. Fixed-only plans count character-admitted turns exactly; live history admission depends on actual answer lengths.",
    "consistency": "Repeats standalone non-repetitive copy prompts within each level and against sequential modal answers. Correctness is reported independently and correctness_observed appears in summaries below 1.0; strict answer differences fail. Non-strict answer variation or probability drift beyond tolerance is inconclusive: variation observed; enable strict to enforce. Full agreement can pass. Load qualification requires average requests at once plus the requested peak of overlapping streamed response windows (first answer output through the stream-end read before cleanup; at least 1 ms of unstalled intersection), excluding intersections wholly inside recorded client scheduling delays, and per-request response overlap. Logprobs copy a passage of at least 64 words and require at least 32 compared token positions before passing, reporting maximum cross-level delta and token divergences. Complete-response logprobs cannot establish server-side concurrency without streamed output timing and leave concurrent levels inconclusive. A length finish explicitly reports exhaustion of the passage output budget. Strict cross-level equality requires batch-invariant serving; default vLLM can diverge.",
    "concurrency": "Refills individual request slots for each configured duration, then drains active requests. Measures client-delivered and server-evidenced concurrency, latency percentiles, throughput and token lengths. Minimum valid responses is evidence, never a stopping target. Interference alternates short and long probes in A/B/A/B windows. Missing token counts are not reported; insufficient evidence is inconclusive. The default levels [1, 4] do not enable interference. Pass criteria and output budgets belong to concurrent_load; its per-check request timeout inherits the run default.",
    "tools": "Checks response protocol, configured tool names and arguments, and optional assistant text requirements separately. Parallel calls match as an unordered multiset and conversation results use matched call IDs. Empty argument text means an empty object, except an empty last call cut off by the output limit remains unverified. Only final answer text satisfies content expectations; unclosed reasoning is inconclusive. Output truncation, including partial JSON numbers, is inconclusive unless complete calls independently violate the contract. Giraffe does not execute tools.",
}

CHECK_DESCRIPTIONS["load"] = "Workloads, request rates and bursts in one place."
CHECK_DETAILS["load"] = "Runs isolated combinations, seeded random mixed traffic and token stress under maintained concurrency, constant or Poisson arrivals and burst overlays. Controlled stream interference uses separate full baseline/probe windows. Each window sends for its full duration, then finishes within its allowance and cancels unfinished work. Per-response criteria, delivery evidence and minimum valid responses qualify each shape separately; percentiles are measurements only. Arrival timing includes dispatch delay. Local drops are not endpoint failures. Highest accepted values are highest tested, with burst overlays reported separately."


def configuration_limits(schema: dict) -> dict:
    """Flatten schema bounds without copying their policy into browser code.

    Array item bounds remain under ``items``; object items also expose paths with
    ``[]``. Wildcard paths describe dictionaries, with supported check aliases
    expanded below so callers need not know how CheckOptions is represented.
    """
    limits = {}
    constraint_keys = {"type", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                       "multipleOf", "minItems", "maxItems", "uniqueItems", "minLength", "maxLength"}

    def resolve(node):
        if "$ref" in node:
            node = {**schema["$defs"][node["$ref"].rsplit("/", 1)[-1]],
                    **{key: value for key, value in node.items() if key != "$ref"}}
        choices = node.get("anyOf", node.get("allOf", []))
        for choice in choices:
            if choice.get("type") != "null":
                node = {**node, **resolve(choice)}
        return node

    def bounds(node):
        node = resolve(node)
        result = {key: value for key, value in node.items() if key in constraint_keys}
        if isinstance(node.get("items"), dict):
            item_bounds = bounds(node["items"])
            if item_bounds:
                result["items"] = item_bounds
        return result

    def walk(node, path=""):
        node = resolve(node)
        value = bounds(node)
        if path and value:
            limits[path] = value
        for name, child in node.get("properties", {}).items():
            walk(child, f"{path}.{name}" if path else name)
        if isinstance(node.get("items"), dict):
            walk(node["items"], f"{path}[]")
        if isinstance(node.get("additionalProperties"), dict):
            walk(node["additionalProperties"], f"{path}.*")

    walk(schema)
    walk(CleanupCriteria.model_json_schema(), "cleanup")
    for check, fields in CHECK_OPTION_FIELDS.items():
        for field in fields:
            limits[f"test_options.{check}.{field}"] = deepcopy(limits[f"test_options.*.{field}"])
        for field in CHECK_LIMIT_FIELDS[check]:
            limits[f"test_options.{check}.limits.{field}"] = deepcopy(limits[f"limits.{field}"])

    # These validators enforce relationships and dictionary/list items beyond
    # JSON Schema. Reuse related schema bounds and named aggregate caps.
    level_items = deepcopy(limits["default_concurrency"])
    limits["concurrent_load.schedule.levels"].update(items=level_items, uniqueItems=True)
    limits["consistency.levels"].update(
        minItems=limits["concurrent_load.schedule.levels"]["minItems"],
        maxItems=limits["concurrent_load.schedule.levels"]["maxItems"],
        items=deepcopy(level_items), uniqueItems=True)
    share_bounds = deepcopy(limits["concurrent_load.pass_criteria.max_error_percent"])
    limits["concurrent_load.mixed.shares.*"] = share_bounds
    for shape in SHAPES:
        limits[f"concurrent_load.mixed.shares.{shape}"] = deepcopy(share_bounds)
    limits["load_test.patterns.concurrent.levels"].update(items=deepcopy(level_items), uniqueItems=True)
    for shape in SHAPES:
        limits[f"load_test.workloads.mixed.shares.{shape}"] = deepcopy(share_bounds)
    limits["traffic.rates"]["items"] = {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_TRAFFIC_RATE}
    limits["traffic.planned_arrivals"] = {"maximum": MAX_PLANNED_ARRIVALS}
    limits["load_test.planned_arrivals"] = {"maximum": MAX_PLANNED_ARRIVALS}
    limits["request_pack.bytes"] = {"maximum": MAX_REQUEST_PACK_BYTES}
    limits["request_pack.case_count"] = {"maximum": MAX_REQUEST_PACK_CASES}
    limits["tool_checks.bytes"] = {"maximum": MAX_TOOL_CONFIG_BYTES}
    limits["live.requests"] = {"maximum": MAX_LIVE_REQUESTS}
    # Correctness sessions retain the same turn bounds as performance workloads.
    turns = limits["sessions.workloads[].turns"]
    limits["sessions.turns"].update(minItems=turns["minItems"], maxItems=turns["maxItems"])
    for field in ("sessions.turns", "sessions.workloads[].turns"):
        limits[field]["items"].update(minLength=1, maxLength=MAX_SESSION_TURN_CHARS)
        limits[f"{field}[]"].update(minLength=1, maxLength=MAX_SESSION_TURN_CHARS)
    # Keep runtime discovery in the same flat bounds format. Defaults must not
    # masquerade as ceilings, and browser configuration paths stay unchanged.
    for name, value in vars(caps).items():
        if name.startswith("MAX_"):
            limits[f"runtime.{name}"] = {"maximum": value}
        elif name.startswith("DEFAULT_"):
            limits[f"runtime.{name}"] = {"default": value}
    return limits


def capabilities(config: RunConfig | None = None, *, restart_allowed: bool = False) -> dict:
    """Describe real model fields; semantic constraints are checked by validate."""
    configured = config or RunConfig(targets=[{
        "name": "local", "url": "http://localhost:11434/v1", "model": "placeholder",
    }])
    inherited = configured.model_copy(update={"test_options": {}})
    defaults = inherited.model_dump(mode="json")
    defaults.pop("targets")  # Targets are required inputs, never discovered credentials.
    example = builtin_fixtures(configured.model_copy(update={"context_limit": 128}))["correctness"][0]
    schema = RunConfig.model_json_schema()
    limits = configuration_limits(schema)
    return {
        "restart_allowed": restart_allowed,
        "builtin_question_example": {"question": example.messages[0]["content"], "expected": example.expected},
        "kind": "capabilities", "schema_version": "1", "suite_version": SUITE_VERSION, "version": __version__,
        "validation_note": "The schema describes configuration. Validate checks semantic constraints; neither command contacts endpoints or verifies runtime availability.",
        "defaults": defaults, "config_schema": schema, "limits": limits,
        "condition_labels": condition_labels(),
        "tokenizer_setup_timeout_seconds": SETUP_TIMEOUT_SECONDS,
        "cap_explanations": caps.cap_explanations(limits),
        "checks": [
            {"id": key, "title": title, "description": CHECK_DESCRIPTIONS[key],
             "details": CHECK_DETAILS[key],
             "optional": key not in DEFAULT_CHECKS,
             "option_fields": sorted(CHECK_OPTION_FIELDS.get(key, set())),
             "limit_fields": sorted(CHECK_LIMIT_FIELDS.get(key, set())),
             "defaults": inherited.effective_check(key),
             **({"inheritance": {"max_output_tokens": {"suite_field": "max_output_tokens",
                                                     "maximum_default": DEFAULT_CONSISTENCY_OUTPUT_TOKENS}}}
                if key == "consistency" else {}),
             **({"request_settings": pack_settings(configured)}
                if key == "correctness" and configured.request_source == "request_pack" else {})} for key, title in CHECK_NAMES.items()
        ],
        "telemetry": {"runtime": {"collection": "Automatic alongside inference, independent of GPU checks and the legacy metrics flag.",
            "discovery": "Configured metrics_url, otherwise one same-origin /metrics route; no port scanning or inference credential forwarding.",
            "profiles": "Explicit metric-name contracts; unknown profiles are detected only from published exact names."},
            "token_evidence": {"collection": "Bounded, request-budgeted setup probes negotiate streamed logprobs and usage before timed workloads.",
                "limitation": "Exact token ITL requires matching usage and distinct single-token reads; buffered events remain unavailable."},
            "host_metrics_url": {"field": "targets[].host_metrics_url",
            "label": "Server host exporter URL", "optional": True, "profile": "node_exporter",
            "metrics": ["host_cpu_percent", "host_ram_used_bytes", "host_ram_total_bytes"],
            "scope": "server-wide", "credentials": "No inference or runtime metrics credentials are forwarded.",
            "collection": "Read-only alongside the workload; configured URL enables host sampling."}},
        "profile_defaults": {name: model().model_dump(mode="json") for name, model in {
            "arrivals": Arrival, "prefix": Prefix, "sessions": Sessions,
            "consistency": Consistency,
        }.items()},
        "option_schema": CheckOptions.model_json_schema(),
        "limits_schema": Limits.model_json_schema(),
        "baseline_comparison": {"regression_percent": configured.limits.regression_percent,
                                "field": "limits.regression_percent"},
        "traffic_schema": TrafficConfig.model_json_schema(),
        "load_test": {"defaults": LoadTest().model_dump(mode="json"), "schema": LoadTest.model_json_schema()},
        "concurrent_load": {"defaults": ConcurrentLoad().model_dump(mode="json"),
                                 "schema": ConcurrentLoad.model_json_schema()},
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
