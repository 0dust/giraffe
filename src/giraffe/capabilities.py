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
    "access": "Sends a real request through your endpoint, network and authentication settings. A successful connection alone is not enough: the server must return an answer.",
    "serving": "Checks for server errors, empty answers, invalid response formats, unfinished streams and timeouts. Results apply to the requests made in this run.",
    "first_output": "Measures the wait for useful answer text. Reports the first request separately from later requests, and keeps reasoning text separate from the answer.",
    "generation": "Measures complete response time, pauses between streamed chunks, answer length and generation speed. Token speed needs token counts reported by the server.",
    "capacity": "Starts requests at each chosen rate without waiting for previous answers. Tracks correct answers, response times and dropped requests to estimate useful capacity. The request and time limits still apply.",
    "fairness": "Starts a short response, then sends long questions alongside it. Compares the short response with a run without competing long questions to measure slowdown.",
    "context": "Places known facts at different positions in questions of different lengths, then asks the model to recall them. Use the context length configured on your server.",
    "correctness": "Uses questions with known answers, including extraction, arithmetic and classification. Checks whether answers remain correct while multiple requests run at once. Your own dataset replaces the built-in questions when selected.",
    "json": "Requests JSON answers and checks that they parse, contain the required fields, use the expected types and match known values while requests run together.",
    "cancellation": "Tests answer caps, stop text, short deadlines and deliberate cancellation, then sends follow-up requests. Optional server logs or traces can verify that cancelled work actually stopped; a client disconnect alone cannot prove this.",
    "recovery": "Sends sustained traffic, then reduces the load and checks whether the server can answer normally again. Shared request and time limits can shorten the test.",
    "gpu": "Reads an existing metrics endpoint. Missing or stale measurements remain unknown; they do not count as healthy hardware. Giraffe does not install monitoring tools.",
    "arrivals": "Sends requests on a steady schedule or in bursts. Measures when requests actually start and how long they wait, within the configured run limits.",
    "prefix": "Repeats shared opening text and conversation history, then looks for evidence of cache reuse. Faster responses alone do not prove that the server used a cache.",
    "buckets": "Each concurrency level means requests running at the same time. Giraffe repeats each level with nine combinations of short, medium and long questions and answers. The minimum request count and time limit apply to each combination at each level.",
    "mixed": "Runs short requests alongside requests with long questions or long answers, then measures how much the short requests slow down. Request sizes and the mix can be changed in JSON.",
    "sessions": "Runs several conversations at once. Later turns use either prepared history or actual earlier answers, depending on the JSON settings.",
    "consistency": "Repeats questions with different request shapes and numbers of requests at a time, then compares the answers. It checks consistency for these examples, not every possible question.",
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
                "contains": "Case-sensitive substring after normalizing whitespace; does not validate the full answer.",
                "json": "JSON schema validation and, when supplied, equality to the complete expected JSON value.",
                "judge": "Reference-answer AI correctness after traffic; complete panel strict majority, ties/errors unscored.",
                "none": "Traffic only; never counts as correct or qualifies full-pack correct capacity.",
            },
            "input_retention_notice": INPUT_RETENTION_NOTICE,
        },
    }
