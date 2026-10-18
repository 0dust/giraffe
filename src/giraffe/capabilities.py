"""Machine-readable configuration discovery shared by the CLI and local UI."""

from __future__ import annotations

from giraffe.fixtures import SUITE_VERSION, builtin_fixtures
from giraffe.models import (
    CHECK_LIMIT_FIELDS, CHECK_NAMES, CHECK_OPTION_FIELDS, DEFAULT_CHECKS, CheckOptions, Limits,
    RunConfig, TrafficConfig, Arrival, Prefix, Sessions, Consistency, Buckets, JudgingConfig, JUDGE_PROMPT_VERSION, JUDGE_VOTING_RULE,
    MAX_REQUEST_PACK_BYTES, MAX_REQUEST_PACK_CASES, RequestPack,
)
from giraffe.request_pack import INPUT_RETENTION_NOTICE

CHECK_DESCRIPTIONS = {
    "access": "Can Giraffe connect to your server and get an answer?",
    "serving": "Does the server return complete, valid answers without errors?",
    "first_output": "How long do users wait before an answer starts?",
    "generation": "How fast do answers arrive, and how often do they pause?",
    "capacity": "How much traffic can the server handle while giving correct, timely answers?",
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
    "tools": "Does the model return the requested tool call correctly?",
}

CHECK_DETAILS = {
    "access": "Sends ordinary inference through your endpoint, network and authentication settings. Only connection/TLS errors and HTTP 401/403 are authentication or transport failures; a response timeout remains a timeout. Deliberate stress requests are judged by their own checks.",
    "serving": "Checks ordinary requests for server errors, empty answers, invalid response formats, unfinished streams and timeouts. Deliberate overload, long-context, fairness, recovery and cancellation traffic is judged by its own check.",
    "first_output": "Measures the wait for useful answer text on ordinary requests. Reports the first request separately from later requests and keeps reasoning separate. Deliberate stress requests do not determine this verdict.",
    "generation": "Measures ordinary response time, answer length, generation speed and gaps between answer chunks. Trailing finish, usage and close messages do not extend answer gaps. Token speed needs server-reported token counts.",
    "capacity": "Starts requests at each chosen rate independently of previous answers. Each stage owns its errors; intentional overload does not trigger the global error stop. Reports the highest tested rate for which every tested lower rate also passed, and flags non-monotonic results. Request and time budgets still apply.",
    "fairness": "Interleaves short-only and loaded runs of the same known-answer copy sequence. Sends long prompts with unique opening text after first output, then requires a later short-answer chunk to prove overlap. Compares first-to-last answer time and answer gaps; buffered or missing overlap evidence is inconclusive. Set a slowdown ratio to accept performance.",
    "context": "Places known facts at positions separated by at least 32 characters within each input size, then asks the model to recall them. Small budgets exercise fewer positions, recorded as reduced position_coverage in workload metadata. Known-answer fixtures request temperature zero unless you set a temperature. Use the context length configured on your server.",
    "correctness": "Scores known answers. Built-in questions cover extraction, arithmetic and classification; concurrency above one requires observed scored overlap. With your dataset, every case is scored quietly and under capacity traffic, or in a separate concurrent pass when capacity is disabled. Per-check samples/concurrency overrides are rejected in dataset mode; global samples does not choose case count. Built-ins request temperature zero unless you set a temperature. Scoring excludes leading inline reasoning; reasoning-only and nonmatching truncated answers remain unscored, and judges skip truncated answers.",
    "json": "Requests JSON answers at temperature zero unless you set a temperature, strips leading inline reasoning, then checks parsing, required fields, types and known content. Nested numbers compare by value (3 equals 3.0); booleans remain distinct. Reasoning-only and nonmatching truncated answers remain unscored. Configured concurrency above one requires observed overlap of scored requests; otherwise the result is inconclusive. Concurrency one establishes serial behavior only.",
    "cancellation": "Cancels after configured answer chunks and requires first output before cancellation. Checks stop enforcement against a full-output control and stop finish reason, output caps including length finish-reason consistency, deadlines and follow-ups. Optional logs/traces can verify backend release; client disconnect alone cannot.",
    "recovery": "Refills a worker pool with scored long-input/output requests, then compares light probes with a pre-load baseline using the same fixture. Sustained traffic gets at most half the remaining request quota while reserving fixed later work and probes. Set timing limits or recovery_max_ratio; no acceptance limit means inconclusive.",
    "gpu": "Reads existing GPU metrics per observed device label. Freshness needs recent source timestamps or changing values across scrapes. Missing families or unknown freshness are inconclusive; XID errors and active thermal/power throttle reasons fail. Unpublished devices remain unknown. This is not device health certification.",
    "arrivals": "Sends steady or burst arrivals independently of completion. max_pending bounds queued requests only; the global concurrency limit bounds in-flight requests. Reports dispatch, waiting and unissued arrivals.",
    "prefix": "Runs shared-prefix repeats back to back, then independent controls and history. A pass needs enough repeats with cached tokens covering half the estimated prefix and independent controls below that threshold. Non-discriminative cache reporting is inconclusive; only a majority of repeat misses fails. Reports k of n misses; timing alone cannot prove reuse.",
    "buckets": "Refills requests for nine input/output combinations, with counts rounded up to full load groups. Per-request lifetime-average overlap must reach level minus 0.5 (level 1 always qualifies); tail IDs are excluded. Input lengths require ordered short/medium/long tokens and a sufficient token span, allowing fixed template overhead. Cell windows stop dispatch and let active requests finish.",
    "mixed": "Compares a non-repetitive known light passage of at least 64 tokens against quiet controls during long-input/output interference. Wrong light answers are reported as unqualified. Requires heavy-shape evidence and enough overlapping samples. All planned pairs send both requests; buffered answers cannot qualify active decoding. Missing fairness_max_ratio leaves measurements inconclusive without hiding coverage gaps.",
    "sessions": "Refills a bounded conversation pool with distinct seeded names and scores recall against each session name. Live history feeds final answers after stripping inline reasoning; fixed history uses its seeded name at every turn. Fixed-only plans count character-admitted turns exactly; live history admission depends on actual answer lengths.",
    "consistency": "Repeats standalone non-repetitive copy prompts within each level and against sequential modal answers. Correctness is reported independently; strict answer differences fail. Logprobs compare level 1 with other levels using qualified lifetime overlap and report maximum cross-level delta and token divergences in the summary. Strict cross-level equality requires batch-invariant serving; default vLLM can diverge.",
    "tools": "Checks the format, tool name and arguments in the model's requested tool call. Giraffe does not execute the tool.",
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
        "buckets": {"schema": Buckets.model_json_schema(), "defaults": Buckets().model_dump(mode="json")},
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
