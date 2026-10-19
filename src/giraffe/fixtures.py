"""Small, versioned serving smoke tests with local, deterministic scoring.

These tasks test selected observable failures, not general model quality. Prompt
lengths are measured in characters. Without the target's tokenizer and chat
template they cannot prove that a token context boundary was exercised.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from jsonschema import SchemaError, ValidationError
from pydantic import ValidationError as ModelValidationError
from referencing.exceptions import Unresolvable

from giraffe.models import (CHECK_NAMES, EvaluationEvidence, RequestRecord, RequestSpec, RunConfig,
                           ToolCheckOutcome, ToolEvaluation, ToolExpectation, finite_tool_json,
                           validated_json_schema)
from giraffe.tool_schema import (MAX_TOOL_JSON_BYTES, ToolSchemaBudgetExceeded, bounded_json_tree,
                                bounded_tool_schema)

FIXTURE_VERSION = "0.3.0"
SUITE_VERSION = "0.3.0"
_RESERVED_OPTIONS = {"model", "messages", "stream", "max_tokens", "max_completion_tokens", "n"}
_CONTEXT_POSITION_SEPARATION = 32
REASONING_WITHOUT_ANSWER = "reasoning without final answer"
TRUNCATED_ANSWER = "truncated before a complete answer"
# Built-in exact fixtures tolerate one trailing period and one layer of emphasis/code/quote
# wrapping. User request packs and custom fixtures (marked by request_source) stay strict.
STRICT_EXACT_SOURCES = {"request_pack", "custom_fixtures"}
_WRAPPERS = (("**", "**"), ("`", "`"), ('"', '"'), ("'", "'"), ("\u201c", "\u201d"))


def deterministic_options(config: RunConfig, options: dict | None = None) -> dict:
    """Default known-answer sampling without overriding user or case settings."""
    options = dict(options or {})
    if "temperature" not in config.request_options and "temperature" not in options:
        options["temperature"] = 0
    return options


GENERATION_PASSAGE = (
    "The small boat crossed the lake early in the morning. The water was calm, "
    "and the trees along the shore were reflected on its surface. A family watched "
    "from the wooden pier as the boat approached. They carried a basket of bread, "
    "apples, and fresh water for their picnic. After tying the boat to the pier, "
    "the captain helped everyone aboard and checked that each person had a life jacket."
)


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
            "sustained.copy", "sustained", ["recovery", "serving"],
            "Copy exactly, with no extra text:\n" + GENERATION_PASSAGE,
            expected=GENERATION_PASSAGE, max_tokens=config.max_output_tokens, version=3,
        ),
    ]
    generation = [
        _request(
            "generation.copy", "generation", ["generation", "serving"],
            "Copy exactly, with no extra text:\n" + GENERATION_PASSAGE,
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
    """Return final text, whether reasoning was stripped, and whether it was unclosed.

    Tags are matched by name and depth, so nested blocks strip completely. A closing tag at
    depth zero ends prompt-opened reasoning and discards everything before it.
    """
    tags = list(re.finditer(r"<(/?)(think|thinking|reasoning)>", output, re.IGNORECASE))
    if not tags:
        return output, False, False
    parts: list[str] = []
    stack: list[str] = []
    cursor = 0
    for tag in tags:
        name = tag[2].lower()
        if not tag[1]:
            if not stack:
                parts.append(output[cursor:tag.start()])
            stack.append(name)
        elif stack:
            # A mismatched closing tag stays inside the open block.
            if stack[-1] == name:
                stack.pop()
                cursor = cursor if stack else tag.end()
        else:
            parts, cursor = [], tag.end()
    if stack:
        return "", True, True
    before = "".join(parts)
    return (before + output[cursor:]).lstrip() if not before.strip() else before + output[cursor:], True, False


def lenient_exact(text: str, expected: str = "") -> str:
    """Remove one trailing period and one layer of surrounding emphasis, code or quotes."""
    text = text.strip()
    period = not expected.endswith(".") and text.endswith(".")
    text = text[:-1].rstrip() if period else text
    for opening, closing in _WRAPPERS:
        if len(text) > len(opening) + len(closing) and text.startswith(opening) and text.endswith(closing):
            text = text[len(opening):-len(closing)].strip()
            break
    return text[:-1].rstrip() if not expected.endswith(".") and not period and text.endswith(".") else text


def exact_matches(actual: str, expected: str, workload: dict | None = None) -> tuple[bool, bool]:
    """Return (matches, matched only after lenient built-in normalization)."""
    actual, expected = _normalized(actual), _normalized(expected)
    if actual == expected:
        return True, False
    lenient = (workload or {}).get("request_source") not in STRICT_EXACT_SOURCES
    return (True, True) if lenient and lenient_exact(actual, expected) == expected else (False, False)


def _contains(actual: str, expected: str) -> bool:
    start = actual.find(expected)
    while start >= 0:
        end = start + len(expected)
        left = expected and expected[0].isalnum() and start and actual[start - 1].isalnum()
        right = expected and expected[-1].isalnum() and end < len(actual) and actual[end].isalnum()
        # Decimal/group separators join adjacent digits into one numeric token.
        if expected and (expected[0].isdigit() or expected[0] in ".,"):
            left |= bool(start and actual[start - 1].isdigit())
            left |= bool(start and actual[start - 1] in "+-−–—"
                         and (start < 2 or not actual[start - 2].isalnum()))
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
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", answer, re.DOTALL | re.IGNORECASE)
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


def _incomplete_json(answer: str) -> bool:
    """Whether unparseable answer text is a prefix of JSON, inside an unclosed opening fence or not."""
    text = answer.strip()
    if text.startswith("```") and not re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE):
        text = re.sub(r"\A```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        try:
            json.loads(text)
            return True  # Complete JSON whose closing fence was cut off.
        except (ValueError, RecursionError):
            pass
    try:
        json.loads(text)
        return False
    except json.JSONDecodeError as error:
        tail = text[error.pos:]
        number = re.search(r'(?:^|[\s\[:,])(-?(?:0|[1-9][0-9]*)?(?:\.[0-9]*)?(?:[eE][+-]?[0-9]*)?)$', text)
        if number and number[1]:
            # Complete just the final numeric token, then let the decoder prove
            # that no earlier syntax error was hidden by the truncated tail.
            token = number[1]
            if (token == "-" or re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.|(?:\.[0-9]+)?[eE][+-]?)', token)):
                completed = text + "0"
                try:
                    json.loads(completed)
                    return True
                except json.JSONDecodeError as partial:
                    if partial.pos == len(completed):
                        return True
        return (error.pos >= len(text) or error.msg.startswith("Unterminated string")
                or bool(tail) and any(word.startswith(tail) for word in ("true", "false", "null")))
    except (ValueError, RecursionError):
        return False


def _equal_json_values(left: Any, right: Any) -> bool:
    """JSON has one numeric category; booleans are a distinct category."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_equal_json_values(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(_equal_json_values(a, b) for a, b in zip(left, right, strict=True))
    return left == right


def _parsed_tool_arguments(arguments: Any, schema: dict) -> dict:
    if not isinstance(arguments, str):
        raise ValueError("arguments must be JSON text")
    if not arguments.strip():
        # Some servers send "" for a no-argument call; treat it as the empty object.
        arguments = "{}"
    if len(arguments.encode("utf-8")) > MAX_TOOL_JSON_BYTES:
        raise ToolSchemaBudgetExceeded
    value = _json_answer(arguments, allow_fence=False)
    finite_tool_json(value)
    bounded_json_tree(value)
    if not isinstance(value, dict):
        raise ValueError("tool arguments must be an object")
    bounded_tool_schema(schema, instance=value).validate(value)
    return value


def reasoning_budget(record: RequestRecord) -> bool:
    if record.valid or record.finish_reason != "length":
        return False
    answer, stripped, _ = final_answer(record.output)
    return (not answer.strip()
        and (bool(record.reasoning.strip()) or stripped or record.reasoning_stripped or (record.reasoning_tokens or 0) > 0) and (
        record.status == "completed" and record.error == "reasoning exhausted the output budget before an answer"
        or record.status == "failed" and record.error == "response contained no useful answer output"))


def matched_calls(expected: list, names: list, values: list) -> list[int] | None:
    """Map each expected call to a distinct returned call, or None.

    Parallel calls are unordered: calls match as a multiset by name and canonical
    arguments; expectations without arguments match any remaining call by name.
    """
    if len(expected) != len(names):
        return None
    free, assignment = list(range(len(names))), [0] * len(expected)
    for wildcard in (False, True):
        for index, item in enumerate(expected):
            if (item.arguments is None) is not wildcard:
                continue
            match = next((j for j in free if names[j] == item.name and (
                wildcard or _equal_json_values(values[j], item.arguments))), None)
            if match is None:
                return None
            free.remove(match)
            assignment[index] = match
    return assignment


def _tool_calls_outcome(expectation: ToolExpectation, record: RequestRecord) -> ToolCheckOutcome:
    calls, expected_calls = record.tool_calls, expectation.expected_calls
    truncated = record.finish_reason == "length"
    unverified = ToolCheckOutcome(status="inconclusive", message="Truncated tool-call completion cannot establish complete capability.")
    if not calls:
        return unverified if truncated else ToolCheckOutcome(status="fail", message="Missing parsed tool call; JSON answer text is not a tool call.")
    if len(calls) > len(expected_calls) or len(calls) < len(expected_calls) and not truncated:
        return ToolCheckOutcome(status="fail", message="Wrong tool-call count: differs from the fixture.")
    schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in expectation.tools}
    names = [call["function"].get("name") if isinstance(call.get("function"), dict) else None for call in calls]
    remaining = Counter(item.name for item in expected_calls)
    remaining.subtract(names)
    if any(count < 0 for count in remaining.values()):
        return ToolCheckOutcome(status="fail", message="Wrong tool function: differs from the fixture's calls.")
    values, pending = [], list(expected_calls)
    for index, (call, name) in enumerate(zip(calls, names, strict=True)):
        if call.get("type", "function") != "function":
            return ToolCheckOutcome(status="fail", message="Wrong tool-call type: expected a function.")
        raw = call["function"].get("arguments")
        if truncated and index == len(calls) - 1 and isinstance(raw, str) and not raw.strip():
            return unverified
        try:
            values.append(_parsed_tool_arguments(raw, schemas[name]))
        except ToolSchemaBudgetExceeded:
            return ToolCheckOutcome(status="inconclusive", message="Tool argument validation exceeded bounded evaluation limits.")
        except RecursionError:
            return ToolCheckOutcome(status="inconclusive", message="Tool schema evaluation exceeded the recursion limit.")
        except ValidationError:
            return ToolCheckOutcome(status="fail", message="Tool argument schema mismatch.")
        except (ValueError, TypeError, KeyError, Unresolvable, SchemaError):
            if truncated and index == len(calls) - 1 and isinstance(call["function"].get("arguments"), str) and _incomplete_json(call["function"]["arguments"]):
                return unverified  # The output cap may have cut the last call's arguments.
            return ToolCheckOutcome(status="fail", message="Malformed tool argument JSON.")
        if truncated:
            value = values[-1]
            item = next((item for item in pending if item.name == name and item.arguments is not None
                         and _equal_json_values(value, item.arguments)), None) or next(
                (item for item in pending if item.name == name and item.arguments is None), None)
            if item is None:
                return ToolCheckOutcome(status="fail", message="Tool arguments differ from the fixture's expected values.")
            pending.remove(item)
    if truncated:
        return unverified
    if matched_calls(expected_calls, names, values) is None:
        return ToolCheckOutcome(status="fail", message="Tool arguments differ from the fixture's expected values.")
    return ToolCheckOutcome(status="pass", message="Parsed tool call matches the fixture.")


def _tool_choice_outcomes(expectation: ToolExpectation, record: RequestRecord):
    rule, calls = expectation.choice, record.tool_calls
    truncated = record.finish_reason == "length"
    names = [call.get("function", {}).get("name") for call in calls]
    schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in expectation.tools}
    compliant = (len(calls) <= rule.call_limit
                 and (rule.mode not in {"required", "named"} or bool(calls) or truncated)
                 and (rule.mode != "named" or all(name == rule.name for name in names)))
    choice = ToolCheckOutcome(status="inconclusive" if compliant and truncated else "pass" if compliant else "fail",
        message="Truncated output cannot establish complete tool choice compliance." if compliant and truncated else
                "Response honors the tool choice rule." if compliant else "Response violates the tool choice name/count rule.")
    arguments = ToolCheckOutcome(status="not_checked", message="No calls returned; no arguments to validate.")
    parsed = []
    for index, (call, name) in enumerate(zip(calls, names, strict=True)):
        if name not in schemas or call.get("type") != "function":
            arguments = ToolCheckOutcome(status="fail", message="Call does not reference a declared function.")
            break
        raw = call["function"].get("arguments")
        if truncated and index == len(calls) - 1 and isinstance(raw, str) and not raw.strip():
            arguments = ToolCheckOutcome(status="inconclusive", message="Tool arguments were truncated.")
            break
        try:
            parsed.append(_parsed_tool_arguments(raw, schemas[name]))
        except (ToolSchemaBudgetExceeded, RecursionError):
            arguments = ToolCheckOutcome(status="inconclusive", message="Tool argument validation exceeded bounded evaluation limits.")
            break
        except (ValueError, TypeError, KeyError, Unresolvable, SchemaError, ValidationError) as error:
            partial = (truncated and index == len(calls) - 1 and isinstance(error, json.JSONDecodeError)
                       and _incomplete_json(raw))
            arguments = ToolCheckOutcome(status="inconclusive" if partial else "fail", message=
                "Tool arguments were truncated." if partial else "Tool arguments are malformed or do not satisfy the declared schema.")
            break
        arguments = ToolCheckOutcome(status="pass", message="Returned tool arguments satisfy their declared schemas.")
    expected = expectation.expected_calls
    task = ToolCheckOutcome(status="not_checked", message="No task-specific call expectation configured.")
    if expected is not None:
        if truncated:
            task = _tool_calls_outcome(expectation, record)
        elif Counter(e.name for e in expected) != Counter(names):
            task = ToolCheckOutcome(status="fail", message="Returned call names/count differ from the task expectation.")
        elif arguments.status in {"fail", "inconclusive"}:
            task = ToolCheckOutcome(status="inconclusive", message="Task arguments cannot be compared before schema validation succeeds.")
        else:
            matches = matched_calls(expected, names, parsed) is not None
            task = ToolCheckOutcome(status="pass" if matches else "fail", message=
                "Calls match the task expectation." if matches else "Tool arguments differ from the task expectation.")
    return choice, arguments, task


def _score_tool_response(expectation: ToolExpectation, record: RequestRecord) -> RequestRecord:
    expected_content = expectation.expected_content
    answer, _, unclosed = final_answer(record.output)
    content = ToolCheckOutcome(status="not_checked", message="No assistant content expectation configured.")
    if (record.status != "completed" or not record.valid) and not reasoning_budget(record):
        if expectation.choice:
            rejected = record.http_status in {400, 422, 501}
            required = expectation.choice.support == "required"
            pending = ToolCheckOutcome(status="inconclusive", message="Response did not complete validly; no semantic verdict.")
            choice = ToolCheckOutcome(status="fail" if rejected and required else "inconclusive",
                message="API rejected this request; tool-choice support is unverified." if rejected
                        else "Tool choice cannot be assessed without a completed response.")
            return record.model_copy(update={"score": False if rejected and required else None,
                "score_message": choice.message, "tool_evaluation": ToolEvaluation(
                    protocol=ToolCheckOutcome(status="fail", message="Request did not complete validly."),
                    calls=pending, content=pending if expected_content is not None else content,
                    choice=choice, arguments=pending, task=pending,
                    support="rejected" if rejected else "unverified")})
        pending = ToolCheckOutcome(status="inconclusive", message="Request did not complete validly; partial output cannot establish correctness.")
        evaluation = ToolEvaluation(
            protocol=ToolCheckOutcome(status="fail", message="Request did not complete validly."),
            calls=pending, content=pending if expected_content is not None else content,
        )
        return record.model_copy(update={
            "score": None, "score_message": "Answer not scored: request did not complete validly.",
            "tool_evaluation": evaluation,
        })
    protocol = ToolCheckOutcome(status="pass", message="Request completed validly.")
    truncated = record.finish_reason == "length"
    if truncated:
        # Giraffe's own output cap can stop a reasoning model before any call: unverified, not failed.
        protocol = ToolCheckOutcome(status="inconclusive", message="Completion was truncated by the output limit.")
    elif expectation.choice and record.finish_reason == "tool_calls" and not record.tool_calls:
        protocol = ToolCheckOutcome(status="fail", message="Tool-call finish reason returned without any structured calls.")
    choice = arguments = task = None
    if expectation.choice:
        if protocol.status == "fail":
            choice = arguments = task = ToolCheckOutcome(status="inconclusive", message="Incomplete or contradictory output cannot establish tool choice compliance.")
        else:
            choice, arguments, task = _tool_choice_outcomes(expectation, record)
        call_outcomes = (arguments, task)
        calls = next((outcome for status in ("fail", "inconclusive", "pass")
                      for outcome in call_outcomes if outcome.status == status),
                     ToolCheckOutcome(status="not_checked", message="No call arguments or task assertions to check."))
    else:
        calls = _tool_calls_outcome(expectation, record)
    if expected_content is not None:
        actual, expected = answer, expected_content.value
        if expected_content.normalization == "whitespace":
            actual, expected = _normalized(actual), _normalized(expected)
        matches = actual == expected if expected_content.mode == "exact" else expected in actual
        content = ToolCheckOutcome(
            status="pass" if matches else "fail",
            message=("Assistant content matches the fixture." if matches else "Assistant content differs from the fixture."),
        )
        if matches and expected_content.mode == "contains":
            content.message = "Expected assistant substring found; this does not validate full content or duplication."
        if truncated and not matches:
            content = ToolCheckOutcome(status="inconclusive", message="Assistant content was truncated before it could be compared.")
    if unclosed:
        content = ToolCheckOutcome(status="inconclusive", message=REASONING_WITHOUT_ANSWER)
    evaluation = ToolEvaluation(protocol=protocol, calls=calls, content=content,
        choice=choice, arguments=arguments, task=task,
        support=("observed" if choice.status == "pass" else "unverified") if choice else None)
    outcomes = (protocol, calls, content, *([choice] if choice else []))
    failed = any(outcome.status == "fail" for outcome in outcomes)
    inconclusive = any(outcome.status == "inconclusive" for outcome in outcomes)
    score = False if failed else None if inconclusive else True
    message = (next((outcome.message for outcome in outcomes if outcome.status in {"fail", "inconclusive"}),
                    choice.message) if choice else calls.message)
    if content.status == "fail":
        message = content.message if calls.status == "pass" else f"{calls.message} {content.message}"
    elif content.status == "inconclusive" and not failed:
        message = content.message
    elif score is True and expected_content is not None:
        message = f"{calls.message} {content.message}"
    return record.model_copy(update={
        "score": score, "score_message": message, "tool_evaluation": evaluation,
        **({"evaluation_error": "schema_recursion" if "recursion" in calls.message else "tool_evaluation_budget"}
           if calls.status == "inconclusive" and protocol.status == "pass" else {}),
    })


def score_response(spec: RequestSpec, record: RequestRecord) -> RequestRecord:
    """Score only a valid, completed answer; return a new record.

    Exact checks compare the complete case-sensitive answer after whitespace
    normalization. ``contains`` is an explicit custom boundary-aware check, never
    used by ordinary smoke fixtures. Token-stress summaries check a required fact,
    without claiming full summary quality. JSON permits one complete markdown fence, but not prose
    surrounding an object. Reasoning content never substitutes for the answer.
    """
    expectation = spec.tool_expectation
    if spec.scorer == "tool" and expectation is None:
        from giraffe.tool_checks import default_tool_fixture
        fixture = default_tool_fixture()
        expectation = ToolExpectation(tools=fixture.tools, expected_calls=fixture.expected_calls)
    record = record.model_copy(update={"evaluation": EvaluationEvidence(
        messages=spec.messages, scorer=spec.scorer, expected=spec.expected,
        expected_provided=spec.expected_provided or spec.expected is not None,
        rubric=spec.rubric, schema=spec.schema_, tool_expectation=expectation,
    ).model_copy(deep=True)})
    answer, stripped, unclosed = final_answer(record.output)
    if stripped:
        record = record.model_copy(update={"reasoning_stripped": True})
    if spec.scorer == "none":
        return record.model_copy(update={"score": None, "score_message": "No semantic scorer."})
    if spec.scorer == "tool":
        return _score_tool_response(expectation, record)
    if record.status != "completed" or not record.valid:
        return record.model_copy(update={
            "score": None, "score_message": "Answer not scored: request did not complete validly.",
        })
    if unclosed or (stripped and not answer.strip() and spec.scorer != "tool"):
        return record.model_copy(update={"score": None, "score_message": REASONING_WITHOUT_ANSWER})
    if spec.scorer == "judge":
        return record.model_copy(update={"score": None, "score_message": TRUNCATED_ANSWER
                                         if record.finish_reason == "length" else "AI judgment pending."})
    incomplete = False
    if spec.scorer == "contains" and record.finish_reason == "length":
        # Truncated text may be tag-less (prompt-opened) reasoning; a found substring proves nothing.
        return record.model_copy(update={"score": None, "score_message": TRUNCATED_ANSWER})
    if spec.scorer in {"exact", "contains"}:
        expected = _normalized(str(spec.expected))
        actual = _normalized(answer)
        lenient = False
        if spec.scorer == "exact":
            score, lenient = exact_matches(actual, expected, spec.workload)
        else:
            score = _contains(actual, expected)
        incomplete = expected.startswith(actual) and actual != expected if spec.scorer == "exact" else not score
        message = "Answer matches the fixture." if score else "Answer differs from the fixture."
        if lenient:
            message = "Answer matches the fixture after removing a trailing period or surrounding markup."
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
                incomplete = _incomplete_json(answer)
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
            spec.workload = {**spec.workload, "request_source": "custom_fixtures"}
            ids.add(spec.fixture_id)
            fixtures.append(spec)
        except (ValueError, TypeError, SchemaError, ModelValidationError) as error:
            raise ValueError(f"Custom fixture {index}: {error}") from error
    return fixtures
