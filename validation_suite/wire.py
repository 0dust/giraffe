"""Small explicit wire corpus; expected answers never come from product scorers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

FIXTURE_VERSION = "reference-wire-v1"
FIXTURES = (
    "normal", "fragmented_utf8", "bundled", "reasoning", "tool_calls", "usage_trailer",
    "no_usage", "malformed_usage", "http_error", "stream_error_before", "stream_error_after",
    "malformed_stream", "missing_termination", "truncated", "disconnect", "hang", "incorrect",
)


@dataclass(frozen=True)
class WireEvent:
    sequence: int
    raw: bytes
    metadata: dict[str, Any]


@dataclass(frozen=True)
class WireResponse:
    status: int
    content_type: str
    events: list[WireEvent]
    expected: dict[str, Any]
    outcome: str
    terminal: bool


def auto_answer(prompt: str) -> str:
    """Reuse the existing fake endpoint's documented synthetic prompt rules.

    This is a fixture generator, not an evaluator or a model. Unknown prompts
    have the explicit default answer 42. No product implementation is imported.
    """
    for marker in ("Instead, copy this text exactly:\n",
                   "Copy the following text exactly, with no extra text:\n"):
        if marker in prompt:
            return prompt.rsplit(marker, 1)[1]
    if "JSON object" in prompt:
        return '{"label":"J6Q2","count":3,"ready":true}'
    if "17 cards" in prompt and "8 cards" in prompt:
        return "25"
    if "Label mip" in prompt:
        return "B"
    if "Copy exactly" in prompt:
        return prompt.split("\n", 1)[1] if "\n" in prompt else prompt.split(":", 1)[1].strip()
    if "numbers from 1 to 100" in prompt:
        return " ".join(map(str, range(1, 101)))
    match = re.search(r"(?:The box label is|Box label:)\s*([A-Z0-9]+)", prompt)
    return match.group(1) if match else "42"


def build_response(payload: dict[str, Any], scenario: dict[str, Any], response_id: str) -> WireResponse:
    fixture = scenario.get("model_fixtures", {}).get(payload.get("model"),
                                                     scenario.get("fixture", "normal"))
    if fixture not in FIXTURES:
        raise ValueError(f"Unknown reference fixture: {fixture}")
    if not payload.get("stream", False) and fixture in {
        "stream_error_before", "stream_error_after", "missing_termination",
    }:
        raise ValueError(f"Fixture {fixture} requires stream=true")
    prompt = "\n".join(str(m.get("content") or "") for m in payload.get("messages", []))
    output = str(scenario.get("response", "42"))
    if output == "auto":
        output = auto_answer(prompt)
    golden = output
    if fixture == "incorrect":
        output = "INCORRECT"
    stops = payload.get("stop") or []
    for marker in [stops] if isinstance(stops, str) else stops:
        output = output.split(marker, 1)[0]
        golden = golden.split(marker, 1)[0]
    pieces = re.findall(r"\S+\s*", output)
    cap = payload.get("max_completion_tokens", payload.get("max_tokens", len(pieces)))
    cap = cap if isinstance(cap, int) and cap >= 0 else len(pieces)
    finish = "length" if len(pieces) > cap else "stop"
    pieces = pieces[:cap]
    output = "".join(pieces)
    if "events" in scenario and fixture != "incorrect":
        pieces = list(scenario["events"])
        output = "".join(pieces)
        golden = output if fixture != "incorrect" else golden
    token_count = len(re.findall(r"\S+\s*", output))
    if "event_count" in scenario:
        count = int(scenario["event_count"])
        if count < 1 or count > 10000:
            raise ValueError("event_count must be between 1 and 10000")
        pieces = [output[len(output) * i // count:len(output) * (i + 1) // count]
                  for i in range(count)]
    usage: Any = {"prompt_tokens": len(prompt.split()) + 8, "completion_tokens": token_count}
    if "usage" in scenario:
        usage = scenario["usage"]
    if fixture == "malformed_usage":
        usage = {"prompt_tokens": -1, "completion_tokens": "not-an-integer"}
    expected: dict[str, Any] = {
        "content": output, "golden_content": golden, "reasoning": "",
        "outcome": "completed", "score": None if finish == "length" else output == golden,
        "finish_reason": finish,
        "input_tokens": usage.get("prompt_tokens") if isinstance(usage, dict) else None,
        "completion_tokens": usage.get("completion_tokens") if isinstance(usage, dict) else None,
        "token_count_source": "synthetic_whitespace_fixture_not_model_tokenizer",
        "fixture": fixture, "fixture_version": FIXTURE_VERSION, "response_id": response_id,
    }
    if fixture in {"no_usage", "malformed_usage"}:
        expected.update(input_tokens=None, completion_tokens=None)
    if fixture == "reasoning":
        expected["reasoning"] = str(scenario.get("reasoning", "Think first."))
    tools = [{"id": "call-1", "type": "function", "function": {
        "name": "get_weather", "arguments": '{"city":"Delhi"}'}}]
    if fixture == "tool_calls":
        expected.update(content="", tool_calls=tools, finish_reason="tool_calls", score=None)
    errors = {"http_error": "http_error", "stream_error_before": "protocol_error",
              "stream_error_after": "protocol_error", "malformed_stream": "protocol_error",
              "missing_termination": "protocol_error", "truncated": "protocol_error",
              "disconnect": "transport_error", "hang": "timeout"}
    outcome = errors.get(fixture, "completed")
    expected["outcome"] = outcome
    if outcome != "completed":
        expected["score"] = None
    events: list[WireEvent] = []

    def add(data: dict[str, Any] | str, **metadata: Any) -> None:
        if isinstance(data, dict):
            data = {"id": response_id, "model": payload.get("model", "reference"), **data}
            if scenario.get("openai_object_metadata"):
                data["object"] = "chat.completion.chunk"
            encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        else:
            encoded = data
        raw = ("data: " + encoded + "\n\n").encode()
        events.append(WireEvent(len(events), raw, {"content": "", **metadata}))

    def delta(value: dict[str, Any], reason: str | None = None, **extra: Any) -> None:
        add({"choices": [{"index": 0, "delta": value, "finish_reason": reason}], **extra},
            content=value.get("content", ""), reasoning=value.get("reasoning_content", ""),
            tool_calls=value.get("tool_calls"), finish_reason=reason, **extra)

    if fixture == "http_error":
        expected["content"] = ""
        return WireResponse(503, "application/json", [WireEvent(0,
            b'{"error":{"message":"injected unavailable"}}', {"error": "http_error"})],
            expected, outcome, True)
    if fixture in {"disconnect", "hang"}:
        expected["content"] = ""
        return WireResponse(200, "text/event-stream", [], expected, outcome, False)
    if not payload.get("stream", False):
        message = {"role": "assistant", "content": expected["content"]}
        if fixture == "reasoning":
            message["reasoning_content"] = expected["reasoning"]
        if fixture == "tool_calls":
            message["tool_calls"] = tools
        value = {"id": response_id, "object": "chat.completion", "model": payload.get("model"),
                 "choices": [{"index": 0, "message": message,
                              "finish_reason": expected["finish_reason"]}]}
        if fixture != "no_usage":
            value["usage"] = usage
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        if fixture in {"truncated", "malformed_stream"}:
            raw = raw[:max(1, len(raw) // 2)]
            expected["content"] = ""
        return WireResponse(200, "application/json", [WireEvent(0, raw,
            {"content": expected["content"], "reasoning": expected["reasoning"],
             "usage": value.get("usage"), "finish_reason": expected["finish_reason"],
             "tool_calls": message.get("tool_calls")})], expected, outcome,
            outcome == "completed")
    if fixture == "stream_error_before":
        add({"error": {"message": "injected stream failure"}}, error="injected stream failure")
        expected["content"] = ""
        return WireResponse(200, "text/event-stream", events, expected, outcome, False)
    if fixture == "reasoning":
        delta({"reasoning_content": expected["reasoning"]})
    if fixture == "tool_calls":
        delta({"tool_calls": [{"index": 0, "id": "call-1", "type": "function",
                              "function": {"name": "get_", "arguments": '{"city":'}}]})
        delta({"tool_calls": [{"index": 0,
                              "function": {"name": "weather", "arguments": '"Delhi"}'}}]})
    else:
        for piece in pieces:
            delta({"content": piece})
    if fixture == "stream_error_after":
        add({"error": {"message": "injected after partial answer"}}, error="injected stream failure")
    elif fixture == "malformed_stream":
        add("{malformed-json", error="malformed_json")
    elif fixture == "truncated":
        add('{"choices":[{"delta":{"content":"unfinished', error="truncated_event")
        last = events[-1]
        events[-1] = WireEvent(last.sequence, last.raw[:-2], last.metadata)
    else:
        trailer = {} if fixture in {"no_usage", "usage_trailer"} else {"usage": usage}
        delta({}, expected["finish_reason"], **trailer)
        if fixture == "usage_trailer":
            add({"choices": [], "usage": usage}, usage=usage)
        if fixture != "missing_termination":
            add("[DONE]", terminal=True)
    if fixture == "fragmented_utf8":
        first = events[0]
        raw = first.raw.replace(b'"choices":', b'"choices":\r\ndata: ', 1)
        raw = b": \xf0\x9f\xa6\x92\r\n" + raw.replace(b"\n\n", b"\r\n\r\n")
        events[0] = WireEvent(first.sequence, raw, first.metadata)
    return WireResponse(200, "text/event-stream", events, expected, outcome,
                        outcome == "completed")
