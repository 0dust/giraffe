"""Small, versioned serving smoke tests with local, deterministic scoring.

These tasks test selected observable failures, not general model quality. Prompt
lengths are measured in characters. Without the target's tokenizer and chat
template they cannot prove that a token context boundary was exercised.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from jsonschema import SchemaError, ValidationError
from pydantic import ValidationError as ModelValidationError
from referencing.exceptions import Unresolvable

from giraffe.models import CHECK_NAMES, RequestRecord, RequestSpec, RunConfig, EvaluationEvidence, validated_json_schema

FIXTURE_VERSION = "0.2.0"
SUITE_VERSION = "0.2.0"
_RESERVED_OPTIONS = {"model", "messages", "stream", "max_tokens", "max_completion_tokens", "n"}
_CONTEXT_POSITION_SEPARATION = 32
REASONING_WITHOUT_ANSWER = "reasoning without final answer"
TRUNCATED_ANSWER = "truncated before a complete answer"


def deterministic_options(config: RunConfig, options: dict | None = None) -> dict:
    """Default known-answer sampling without overriding user or case settings."""
    options = dict(options or {})
    if "temperature" not in config.request_options and "temperature" not in options:
        options["temperature"] = 0
    return options


def _request(
    fixture_id: str,
    scenario: str,
    check_ids: list[str],
    prompt: str,
    *,
    scorer: str = "exact",
    expected: Any = None,
    max_tokens: int = 24,
    version: int = 1,
    **kwargs: Any,
) -> RequestSpec:
    return RequestSpec(
        fixture_id=f"{fixture_id}.v{version}",
        scenario=scenario,
        check_ids=check_ids,
        messages=[{"role": "user", "content": prompt}],
        scorer=scorer,
        expected=expected,
        max_tokens=max_tokens,
        input_chars=len(prompt),
        **kwargs,
    )


def _context_request(config: RunConfig, size: str, position: str) -> RequestSpec:
    """Reserve output/template space and bound ASCII input conservatively.

    A character budget deliberately avoids claiming an exact token budget. Even
    an ASCII prompt's token usage depends on the model and its chat template.
    """
    output_budget = min(24, config.max_output_tokens)
    character_budget = config.context_limit - output_budget - 32
    # Keep the expected labels stable across prompt wording revisions.
    label = hashlib.sha256(f"context-v1-{size}-{position}".encode()).hexdigest()[:6].upper()
    prefix, suffix = "Notes:\n", "\nReply with the box label only."
    fact = f"The box label is {label}."
    minimum = len(prefix + fact + suffix)
    minimum_filler = min(2, max(0, character_budget - minimum) // _CONTEXT_POSITION_SEPARATION) * _CONTEXT_POSITION_SEPARATION
    desired = character_budget if size == "long" else max(minimum + minimum_filler, character_budget // 3)
    filler_chars = max(0, desired - minimum)
    positions = (["beginning", "middle", "end"] if filler_chars >= 2 * _CONTEXT_POSITION_SEPARATION
                 else ["beginning", "end"] if filler_chars >= _CONTEXT_POSITION_SEPARATION else ["beginning"])
    filler = (" The box is gray." * (filler_chars // 17 + 1))[:filler_chars]
    insertion = {"beginning": 0, "middle": len(filler) // 2, "end": len(filler)}[position]
    # Whitespace around the injected fact prevents joining it to a filler word.
    body = filler[:insertion] + fact + filler[insertion:]
    if insertion and not body[insertion - 1].isspace():
        body = body[: insertion - 1] + " " + body[insertion:]
    after = insertion + len(fact)
    if after < len(body) and not body[after].isspace():
        body = body[:after] + " " + body[after + 1 :]
    return _request(
        f"context.{size}.{position}",
        "context",
        ["context", "serving"],
        prefix + body + suffix,
        expected=label,
        max_tokens=output_budget,
        version=2,
        context_position=position,
        workload={"position_coverage": {"positions": positions, "reduced": len(positions) < 3,
                                        "minimum_separation_chars": _CONTEXT_POSITION_SEPARATION}},
    )


def builtin_fixtures(config: RunConfig) -> dict[str, list[RequestSpec]]:
    """Return reproducible fixtures; the runner reuses them across load levels.

    The runner must enforce the shared request/time/concurrency budget. Fixtures
    do not schedule traffic, prove backend cancellation, or infer token counts.
    JSON is opt-in. Output-cap and cancellation requests intentionally have no
    semantic scorer; their observed protocol and timing behavior is evaluated
    by the runner. Exact scorers intentionally reject explanatory extra text.
    Identifiers are box labels so prompts do not imply access credentials.
    """
    short = [
        _request(
            "short.code", "short", ["serving", "first_output", "generation", "capacity"],
            "The box label is K7P4. Reply with the box label only.", expected="K7P4",
            version=2,
        ),
        _request(
            "short.copy", "short", ["serving", "first_output", "generation", "capacity"],
            "Copy exactly, with no extra text:\n"
            "The blue box holds seven red cards. The label on each card is K7P4.",
            expected="The blue box holds seven red cards. The label on each card is K7P4.",
            max_tokens=64, version=2,
        ),
    ]
    context = [
        _context_request(config, size, position)
        for size in ("short", "long")
        for position in ("beginning", "middle", "end")
    ]
    context = [spec for spec in context if spec.context_position in spec.workload["position_coverage"]["positions"]]
    long = [
        next((spec for spec in context if spec.fixture_id == "context.long.middle.v2"), context[-1]).model_copy(update={
            "fixture_id": "long.prefill.v2",
            "scenario": "long",
            "check_ids": ["serving", "fairness", "capacity"],
        })
    ]
    correctness = [
        _request(
            "correctness.extraction", "correctness", ["correctness", "serving"],
            "Name: Mira. Box label: T8R2. Color: blue. Return the box label only.",
            expected="T8R2", version=2,
        ),
        _request(
            "correctness.arithmetic", "correctness", ["correctness", "serving"],
            "A box has 17 cards. Add 8 cards. How many cards? Return only the number.",
            expected="25",
        ),
        _request(
            "correctness.classification", "correctness", ["correctness", "serving"],
            "Rule: dax means A; mip means B. Label mip. Reply with A or B only.", expected="B",
        ),
    ]
    json_fixtures: list[RequestSpec] = []
    if config.structured_json:
        expected = {"label": "J6Q2", "count": 3, "ready": True}
        schema = {
            "type": "object",
            "properties": {
                "label": {"type": "string", "enum": ["J6Q2"]},
                "count": {"type": "integer", "enum": [3]},
                "ready": {"type": "boolean"},
            },
            "required": ["label", "count", "ready"],
            "additionalProperties": False,
        }
        json_fixtures.append(_request(
            "json.record", "json", ["json", "serving"],
            "Convert this box record to JSON.\nlabel: J6Q2\ncount: 3\nready: true\n"
            "Output only the JSON object, with no explanation or markdown.",
            scorer="json", expected=expected, schema=schema, max_tokens=64, version=2,
        ))
    cap = min(8, config.max_output_tokens)
    limits = [
        _request(
            "limits.output_cap", "limits", ["cancellation"],
            "Write the numbers from 1 to 100 separated by spaces. No other text.",
            scorer="none", expected={"maximum_output_tokens": cap}, max_tokens=cap,
        ),
        _request(
            "limits.stop", "limits", ["cancellation"],
            "Copy exactly, with no extra text: blue [END] green",
            expected="blue", options={"stop": ["[END]"]},
        ),
        _request(
            "limits.client_cancel", "client_cancel", ["cancellation"],
            "Write the numbers from 1 to 100 separated by spaces. No other text.",
            scorer="none", expected={"client_disconnect_after_ms": 100},
            max_tokens=config.max_output_tokens, cancel_after_ms=100,
        ),
    ]
    sustained = [
        _request(
            "sustained.code", "sustained", ["recovery", "serving"],
            "The box label is V5N3. Reply with the box label only.", expected="V5N3",
            version=2,
        ),
    ]
    generation = [
        _request(
            "generation.copy", "generation", ["generation", "serving"],
            "Copy exactly, with no extra text:\n"
            "The small boat crossed the lake early in the morning. The water was calm, "
            "and the trees along the shore were reflected on its surface. A family watched "
            "from the wooden pier as the boat approached. They carried a basket of bread, "
            "apples, and fresh water for their picnic. After tying the boat to the pier, "
            "the captain helped everyone aboard and checked that each person had a life jacket.",
            scorer="none", expected={"purpose": "observe sustained answer generation"},
            max_tokens=config.max_output_tokens,
        ),
    ]
    fixtures = {
        "short": short, "long": long, "context": context, "correctness": correctness,
        "json": json_fixtures, "limits": limits, "sustained": sustained, "generation": generation,
    }
    if "fairness" in config.checks:
        # Verified as single tokens with cached Qwen2.5 and RoBERTa BPE
        # vocabularies/merges; no numeric separator tokens are needed.
        sequence = " ".join("red blue green white black brown gold pink gray dark light sky sun moon star rain snow wind tree leaf grass seed bird fish cat dog horse cow sheep pig fox bear wolf lion mouse house door room wall floor roof road boat ship car train book pen".split())
        fixtures["generation"].append(_request(
            "fairness.copy", "fairness", ["fairness"],
            "Copy exactly, with no extra text:\n" + sequence,
            expected=sequence, max_tokens=config.max_output_tokens,
            options=deterministic_options(config),
        ))
    if "cancellation" in config.checks:
        fixtures["limits"].append(_request(
            "limits.stop_control", "stop_control", ["cancellation"],
            "Copy exactly, with no extra text: blue [END] green",
            scorer="none", expected={"text": "blue [END] green"},
            options=deterministic_options(config, {"stop": None}),
        ))
    if "cancellation" in config.checks and config.effective_check("cancellation")["backend_verification"] == "required":
        chunks = config.effective_check("cancellation")["cancel_after_chunks"]
        fixtures["backend_cancellation"] = [
            _request(
                "limits.backend_cancel", "backend_cancel", ["cancellation"],
                "Write the numbers from 1 to 100, one per line.", scorer="none",
                max_tokens=128, options=deterministic_options(config), cancel_after_chunks=chunks,
            ),
            _request(
                "limits.backend_cancel.followup", "backend_cancel_followup", ["cancellation"],
                "Reply with just OK.", scorer="none", max_tokens=8, options=deterministic_options(config),
            ),
        ]
    for group in fixtures.values():
        for spec in group:
            if spec.scorer in {"exact", "contains", "json"}:
                spec.options = deterministic_options(config, spec.options)
            spec.max_tokens = min(spec.max_tokens or config.max_output_tokens, config.max_output_tokens)
            spec.stream = config.stream
    return fixtures


def traffic_fixtures(config: RunConfig) -> dict[str, list[RequestSpec]]:
    """Three reproducible request shapes with complete local answer scoring.

    Lengths describe characters and words, not tokenizer-dependent token counts.
    Repeated fixtures intentionally exercise a reproducible canary workload;
    production cache locality and workload coverage must be assessed separately.
    """
    traffic = config.traffic
    prefix = "Notes:\nThe box label is L8N2.\n"
    suffix = "\nReply with the box label only."
    filler_size = max(0, traffic.long_input_chars - len(prefix + suffix))
    filler = ("The box is gray. " * (filler_size // 17 + 1))[:filler_size]
    word_bank = "blue red green yellow orange silver violet brown white black".split()
    answer = " ".join(word_bank[index % len(word_bank)]
                      for index in range(traffic.long_output_words))
    groups = {
        "short": builtin_fixtures(config)["short"],
        "long_input": [_request("traffic.long_input", "capacity", ["capacity"],
                                prefix + filler + suffix, expected="L8N2")],
        "long_output": [_request("traffic.long_output", "capacity", ["capacity"],
                                 "Copy exactly, with no extra text:\n" + answer,
                                 expected=answer, max_tokens=config.max_output_tokens)],
    }
    for specs in groups.values():
        for spec in specs:
            spec.options = deterministic_options(config, spec.options)
            spec.max_tokens = min(spec.max_tokens, config.max_output_tokens)
            spec.stream = config.stream
    return groups


def _normalized(text: str) -> str:
    return " ".join(text.split())


def final_answer(output: str) -> tuple[str, bool, bool]:
    """Return final text, whether reasoning was stripped, and whether it was unclosed."""
    stripped = False
    closings = list(re.finditer(r"</(?:think|thinking|reasoning)>", output, re.IGNORECASE))
    if closings and not re.search(r"<(?:think|thinking|reasoning)>", output[:closings[0].start()], re.IGNORECASE):
        output, stripped = output[closings[-1].end():].lstrip(), True
    while match := re.match(r"\s*<(think|thinking|reasoning)>", output, re.IGNORECASE):
        stripped = True
        closing = re.search(rf"</{match[1]}>", output[match.end():], re.IGNORECASE)
        if closing is None:
            return "", stripped, True
        output = output[match.end() + closing.end():].lstrip()
    return output, stripped, False


def _contains(actual: str, expected: str) -> bool:
    start = actual.find(expected)
    while start >= 0:
        end = start + len(expected)
        left = expected and expected[0].isalnum() and start and actual[start - 1].isalnum()
        right = expected and expected[-1].isalnum() and end < len(actual) and actual[end].isalnum()
        # Decimal/group separators join adjacent digits into one numeric token.
        if expected and (expected[0].isdigit() or expected[0] in ".,"):
            left |= bool(start and actual[start - 1].isdigit())
            left |= bool(start >= 2 and actual[start - 1] in ".," and actual[start - 2].isdigit())
        if expected and (expected[-1].isdigit() or expected[-1] in ".,"):
            right |= bool(end < len(actual) and actual[end].isdigit())
            right |= bool(end + 1 < len(actual) and actual[end] in ".," and actual[end + 1].isdigit())
        if not left and not right:
            return True
        start = actual.find(expected, start + 1)
    return False


def _json_equal(actual: Any, expected: Any) -> bool:
    if type(actual) in {int, float} and type(expected) in {int, float}:
        return actual == expected
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, dict):
        return actual.keys() == expected.keys() and all(_json_equal(value, expected[key]) for key, value in actual.items())
    if isinstance(actual, list):
        return len(actual) == len(expected) and all(_json_equal(a, e) for a, e in zip(actual, expected))
    return actual == expected


def _json_answer(output: str, *, allow_fence: bool = True) -> Any:
    answer = output.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n\s*```", answer, re.DOTALL | re.IGNORECASE)
    if fenced and allow_fence:
        answer = fenced[1]

    def reject_constant(value: str) -> None:
        raise ValueError(f"{value} is not a JSON value")

    def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON property: {key}")
            result[key] = value
        return result

    return json.loads(answer, parse_constant=reject_constant, object_pairs_hook=unique_keys)


def score_response(spec: RequestSpec, record: RequestRecord) -> RequestRecord:
    """Score only a valid, completed answer; return a new record.

    Exact checks compare the complete case-sensitive answer after whitespace
    normalization. ``contains`` is an explicit custom boundary-aware check, never
    used by built-ins. JSON permits one complete markdown fence, but not prose
    surrounding an object. Reasoning content never substitutes for the answer.
    """
    record = record.model_copy(update={"evaluation": EvaluationEvidence(
        messages=spec.messages, scorer=spec.scorer, expected=spec.expected,
        expected_provided=spec.expected_provided or spec.expected is not None,
        rubric=spec.rubric, schema=spec.schema_,
    ).model_copy(deep=True)})
    answer, stripped, unclosed = final_answer(record.output)
    if stripped:
        record = record.model_copy(update={"reasoning_stripped": True})
    if spec.scorer == "none":
        return record.model_copy(update={"score": None, "score_message": "No semantic scorer."})
    if record.status != "completed" or not record.valid:
        return record.model_copy(update={
            "score": None, "score_message": "Answer not scored: request did not complete validly.",
        })
    if unclosed or (stripped and not answer.strip() and spec.scorer != "tool"):
        return record.model_copy(update={"score": None, "score_message": REASONING_WITHOUT_ANSWER})
    if spec.scorer == "judge":
        return record.model_copy(update={"score": None, "score_message": TRUNCATED_ANSWER
                                         if record.finish_reason == "length" else "AI judgment pending."})
    if spec.scorer == "tool":
        calls = record.tool_calls
        message = "Parsed tool call matches the fixture."
        score = False
        if record.finish_reason == "length":
            return record.model_copy(update={"score": None, "score_message": TRUNCATED_ANSWER})
        elif not calls:
            message = "Missing parsed tool call; JSON answer text is not a tool call."
        elif len(calls) != 1:
            message = "Wrong tool-call count: expected exactly one."
        elif calls[0].get("function", {}).get("name") != "get_weather":
            message = "Wrong tool function: expected get_weather."
        else:
            try:
                value = _json_answer(calls[0]["function"]["arguments"], allow_fence=False)
                if not isinstance(value, dict) or set(value) != {"city"} or not isinstance(value["city"], str):
                    message = "Tool argument schema mismatch: exactly one string city required."
                elif value != {"city": "Delhi"}:
                    message = "Tool arguments differ from expected city Delhi (case sensitive)."
                else:
                    score = True
            except (ValueError, TypeError, KeyError):
                message = "Malformed tool argument JSON."
        return record.model_copy(update={"score": score, "score_message": message})
    incomplete = False
    if spec.scorer in {"exact", "contains"}:
        expected = _normalized(str(spec.expected))
        actual = _normalized(answer)
        score = actual == expected if spec.scorer == "exact" else _contains(actual, expected)
        incomplete = expected.startswith(actual) and actual != expected if spec.scorer == "exact" else not score
        message = "Answer matches the fixture." if score else "Answer differs from the fixture."
        if spec.scorer == "contains" and score:
            message = "Expected text found with alphanumeric boundaries; this check does not validate the full answer."
    else:
        try:
            if spec.schema_ is None:
                raise ValueError("JSON scorer requires a schema")
            validator = validated_json_schema(spec.schema_)
            value = _json_answer(answer)
            validator.validate(value)
            if (spec.expected_provided or spec.expected is not None) and not _json_equal(value, spec.expected):
                score, message = False, "JSON is valid but differs from the expected answer."
            else:
                score, message = True, "JSON matches the required schema and expected values."
        except RecursionError:
            return record.model_copy(update={
                "score": None, "evaluation_error": "schema_recursion",
                "score_message": "Answer not scored: JSON schema evaluation exceeded the recursion limit. Review the schema; this is not an incorrect-answer verdict.",
            })
        except (ValueError, TypeError, SchemaError, ValidationError, Unresolvable) as error:
            score = False
            # Do not include raw answers in scoring messages; retention is handled elsewhere.
            if isinstance(error, ValidationError):
                path = ".".join(str(part) for part in error.absolute_path) or "<root>"
                message = f"JSON schema violation at {path} ({error.validator})."
            elif isinstance(error, json.JSONDecodeError):
                tail = answer.strip()[error.pos:]
                incomplete = (error.pos >= len(answer.strip()) or error.msg.startswith("Unterminated string")
                              or bool(tail) and any(word.startswith(tail) for word in ("true", "false", "null")))
                message = f"Invalid JSON at line {error.lineno}, column {error.colno}."
            else:
                message = f"Invalid JSON or schema: {error}"
    if not score and incomplete and record.finish_reason == "length":
        return record.model_copy(update={"score": None, "score_message": TRUNCATED_ANSWER})
    return record.model_copy(update={"score": score, "score_message": message})


def load_custom_fixtures(path: str) -> list[RequestSpec]:
    """Read a local JSON list of RequestSpec objects with convenient defaults.

    Required fields are fixture_id, messages and scorer. Exact/contains scorers
    require a non-empty string expected answer; JSON requires an inline schema.
    Scenario defaults to custom and check_ids to correctness. Request options
    cannot override target, messages, streaming, token budgets or response count.
    The transport still caps each explicit max_tokens at the run's ceiling.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read custom fixtures {path!r}: {error}") from error
    if not isinstance(data, list) or not data:
        raise ValueError("Custom fixtures must be a non-empty JSON list")
    fixtures: list[RequestSpec] = []
    ids: set[str] = set()
    for index, entry in enumerate(data, start=1):
        try:
            if not isinstance(entry, dict):
                raise ValueError("each fixture must be an object")
            if "scorer" not in entry:
                raise ValueError("scorer is required (exact, contains, json or none)")
            if entry["scorer"] not in ("exact", "contains", "json", "none"):
                raise ValueError("Allowed scorers: exact, contains, json, none. "
                                 "Use a request pack and configured AI judges for judge scoring; tool scoring is built-in only.")
            values = {"scenario": "custom", "check_ids": ["correctness"], **entry}
            spec = RequestSpec.model_validate(values)
            if not spec.fixture_id.strip() or spec.fixture_id in ids:
                raise ValueError("fixture_id must be non-empty and unique")
            if not spec.scenario.strip():
                raise ValueError("scenario must be non-empty")
            if not spec.check_ids or set(spec.check_ids) - set(CHECK_NAMES):
                raise ValueError("check_ids must contain supported check names")
            if not spec.messages or any(
                message.get("role") not in {"system", "user", "assistant"}
                or not isinstance(message.get("content"), str)
                or not message["content"].strip()
                for message in spec.messages
            ):
                raise ValueError("messages must contain non-empty text and system/user/assistant roles")
            if _RESERVED_OPTIONS.intersection(spec.options):
                raise ValueError("options cannot override model/messages/stream/token budget/n")
            if spec.scorer in {"exact", "contains"} and (
                not isinstance(spec.expected, str) or not spec.expected.strip()
            ):
                raise ValueError("exact/contains scorer requires a non-empty string expected answer")
            if spec.scorer == "json" and spec.schema_ is None:
                raise ValueError("json scorer requires a schema")
            if spec.schema_ is not None:
                validated_json_schema(spec.schema_)
            spec.input_chars = sum(len(message["content"]) for message in spec.messages)
            ids.add(spec.fixture_id)
            fixtures.append(spec)
        except (ValueError, TypeError, SchemaError, ModelValidationError) as error:
            raise ValueError(f"Custom fixture {index}: {error}") from error
    return fixtures
