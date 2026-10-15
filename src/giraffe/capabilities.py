"""Machine-readable configuration discovery shared by the CLI and local UI."""

from __future__ import annotations

from giraffe.fixtures import SUITE_VERSION
from giraffe.models import (
    CHECK_LIMIT_FIELDS, CHECK_NAMES, CHECK_OPTION_FIELDS, DEFAULT_CHECKS, CheckOptions, Limits,
    RunConfig, TrafficConfig, JudgingConfig, JUDGE_PROMPT_VERSION, JUDGE_VOTING_RULE,
    MAX_REQUEST_PACK_BYTES, MAX_REQUEST_PACK_CASES, RequestPack, Sessions, session_workload_presets,
)
from giraffe.request_pack import INPUT_RETENTION_NOTICE

CHECK_DESCRIPTIONS = {
    "access": "Real inference through your network, TLS, credentials, and endpoint route.",
    "serving": "Errors, empty answers, malformed responses, incomplete streams, and timeouts.",
    "first_output": "Time until useful answer text, separating initial and warmed requests.",
    "generation": "Response time, stream pauses, output length, and reported token rate.",
    "capacity": "Continuous mixed arrivals at chosen rates; correctness, waiting and useful capacity.",
    "fairness": "Whether long prompts disrupt an already streaming short response.",
    "context": "Recall of known facts at different positions and input lengths.",
    "correctness": "Known-answer extraction, arithmetic, and classification under load.",
    "json": "JSON parsing, fields, types, and expected content under concurrency.",
    "cancellation": "Output caps, stop sequences, deadlines, cancellation, and follow-up probes.",
    "recovery": "Sustained traffic followed by light-load recovery probes.",
    "gpu": "Existing GPU telemetry; missing observations never imply healthy hardware.",
    "arrivals": "Bounded steady or burst schedules with actual dispatch and waiting measurements.",
    "prefix": "Shared prompt prefixes, repeated history, and observed cache reuse evidence.",
    "buckets": "Input and output sizes across concurrency levels with coverage measurements.",
    "mixed": "Whether long input and output requests interfere with short requests.",
    "sessions": "Concurrent multi-turn conversations using fixed or live response history.",
    "consistency": "Repeated answers across request shapes and concurrency levels.",
    "tools": "Parsed tool-call format and expected arguments; tools are never executed.",
}


def capabilities(config: RunConfig | None = None) -> dict:
    """Describe real model fields; semantic constraints are checked by validate."""
    configured = config or RunConfig(targets=[{
        "name": "local", "url": "http://localhost:11434/v1", "model": "placeholder",
    }])
    inherited = configured.model_copy(update={"test_options": {}})
    defaults = inherited.model_dump(mode="json")
    defaults.pop("targets")  # Targets are required inputs, never discovered credentials.
    return {
        "kind": "capabilities", "schema_version": "1", "suite_version": SUITE_VERSION,
        "validation_note": "The schema describes configuration. Validate checks semantic constraints; neither command contacts endpoints or verifies runtime availability.",
        "defaults": defaults, "config_schema": RunConfig.model_json_schema(),
        "checks": [
            {"id": key, "title": title, "description": CHECK_DESCRIPTIONS[key],
             "optional": key not in DEFAULT_CHECKS,
             "option_fields": sorted(CHECK_OPTION_FIELDS[key]),
             "limit_fields": sorted(CHECK_LIMIT_FIELDS[key]),
             "defaults": inherited.effective_check(key)} for key, title in CHECK_NAMES.items()
        ],
        "option_schema": CheckOptions.model_json_schema(),
        "limits_schema": Limits.model_json_schema(),
        "traffic_schema": TrafficConfig.model_json_schema(),
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
                "contains": "Case-sensitive substring after normalizing whitespace; does not validate the full answer.",
                "json": "JSON schema validation and, when supplied, equality to the complete expected JSON value.",
                "judge": "Reference-answer AI correctness after traffic; complete panel strict majority, ties/errors unscored.",
                "none": "Traffic only; never counts as correct or qualifies full-pack correct capacity.",
            },
            "input_retention_notice": INPUT_RETENTION_NOTICE,
        },
    }
