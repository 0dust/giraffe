from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from jsonschema import ValidationError

from giraffe.fixtures import _normalized, _parsed_tool_arguments
from giraffe.models import ExpectedToolCall, RequestSpec, ToolChoiceRule, ToolExpectation
from giraffe.tool_schema import tool_call_fingerprint

SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
MARKER = "{{observation_id}}"


def _expand(value, marker):
    return json.loads(json.dumps(value, ensure_ascii=False).replace(MARKER, marker))


def _answer_matches(content, output):
    actual, expected = output, content.value
    if content.normalization == "whitespace":
        actual, expected = _normalized(actual), _normalized(expected)
    return actual == expected if content.mode == "exact" else expected in actual


async def run_conversation(run, target, client, fixture, mode):
    session_id = uuid.uuid4().hex
    marker = "obs_" + uuid.uuid4().hex
    session = {"session_id": session_id, "fixture_id": fixture.fixture_id, "mode": mode,
               "status": "inconclusive", "reason": "not_started", "request_ids": [],
               "completed_steps": 0, "repetition_count": 0}
    run.observations[target.name].setdefault("tool_conversations", []).append(session)
    messages = [message.model_dump() for message in fixture.messages]
    satisfied = set()
    final_content = [item.model_copy(update={"value": item.value.replace(MARKER, marker)})
                     for item in fixture.final_content]
    pending_results = []
    tools_bytes = len(json.dumps(fixture.tools, ensure_ascii=False).encode())
    schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in fixture.tools}

    for turn in range(len(fixture.steps) + 1):
        step = fixture.steps[turn] if turn < len(fixture.steps) else None
        if step and step.user_message is not None:
            messages = [*messages, {"role": "user", "content": step.user_message}]
            satisfied.clear()
        if not run.allowed(target.name):
            session["reason"] = "budget_exhausted"
            run.unfinished[target.name].add("tools")
            return
        serialized_history = json.dumps(messages, ensure_ascii=False)
        input_chars = len(serialized_history)
        if len(serialized_history.encode()) + tools_bytes + run.config.max_output_tokens + 32 > run.config.context_limit:
            session["reason"] = "history_limit"
            run.unfinished[target.name].add("tools")
            return
        expectation = ToolExpectation(tools=fixture.tools, choice=ToolChoiceRule(mode="auto"),
            expected_calls=[ExpectedToolCall(name=c.name, arguments=c.arguments)
                            for c in step.expected_calls] if step else None)
        scenario = f"tools_{fixture.fixture_id}_{mode}_turn{turn}"
        spec = RequestSpec(fixture_id=scenario, scenario=scenario, check_ids=["tools"],
            messages=messages, scorer="tool", stream=mode == "stream", tool_expectation=expectation,
            input_chars=input_chars, max_tokens=run.config.max_output_tokens, options=fixture.options,
            workload={"selection": "conversation", "tool_fixture_id": fixture.fixture_id,
                      "mode": mode, "session_id": session_id, "turn_id": turn,
                      "stage": "call" if step else "final"})
        def assess(record):
            nonlocal messages, pending_results
            session["request_ids"].append(record.id)
            for result in pending_results:
                result["supplied_in_request"] = record.id
            pending_results = []
            evidence = {"session_id": session_id, "turn": turn, "stage": "call" if step else "final",
                        "status": "inconclusive", "reason": "unverified", "calls": [],
                        "repetition_count": 0,
                        "final_content": [item.model_dump() for item in final_content] if not step else None}
            record.tool_conversation = evidence

            def finish(status, reason, message):
                evidence.update(status=status, reason=reason)
                session.update(status=status, reason=reason)
                record.score = {"pass": True, "fail": False, "inconclusive": None}[status]
                record.score_message = message

            for call in record.tool_calls:
                function = call["function"]
                if not call.get("id") or call.get("type") != "function" or function.get("name") not in schemas:
                    continue
                try:
                    arguments = _parsed_tool_arguments(function.get("arguments"), schemas[function["name"]])
                    fingerprint = tool_call_fingerprint(function["name"], arguments)
                except (ValueError, TypeError, RecursionError, ValidationError):
                    continue
                evidence["calls"].append({"call_id": call["id"], "function": function["name"],
                                          "fingerprint": fingerprint, "result": None,
                                          "result_outcome": None, "supplied_in_request": None})
                if fingerprint in satisfied:
                    evidence["repetition_count"] += 1
            session["repetition_count"] += evidence["repetition_count"]
            if evidence["repetition_count"]:
                finish("fail", "repeated_satisfied_call", "An already-satisfied operation was requested again.")
                return
            if not record.valid or record.status != "completed":
                reason = record.status if record.status in {"timeout", "cancelled"} else "protocol_or_api_failure"
                finish("fail" if reason == "protocol_or_api_failure" else "inconclusive", reason,
                       "Conversation response did not complete validly.")
                return
            if record.finish_reason == "length":
                finish("inconclusive", "truncated", "Conversation response was truncated; completion is unverified.")
                return
            if record.tool_evaluation.protocol.status != "pass" or record.assistant_message is None:
                finish("fail", "protocol_or_api_failure", "Conversation assistant history is not usable.")
                return
            if record.tool_evaluation.arguments.status == "inconclusive":
                finish("inconclusive", "validation_limit", "Conversation argument validation exceeded its bound.")
                return
            if record.tool_evaluation.arguments.status == "fail":
                finish("fail", "invalid_call", "Conversation call arguments or function are invalid.")
                return
            if step:
                if record.score is not True:
                    finish("fail", "unexpected_call", "Calls differ from the declared conversation step.")
                    return
                if record.input_tokens is not None and record.input_tokens + run.config.max_output_tokens > run.config.context_limit:
                    finish("inconclusive", "history_limit", "Observed conversation context exceeds the configured limit.")
                    return
                messages = [*messages, record.assistant_message]
                for call, expected, item in zip(record.tool_calls, step.expected_calls, evidence["calls"], strict=True):
                    result = _expand(expected.result, marker)
                    messages.append({"role": "tool", "tool_call_id": call["id"],
                                     "content": json.dumps(result, ensure_ascii=False)})
                    item.update(result=result, result_outcome=expected.outcome)
                    pending_results.append(item)
                    if expected.outcome == "success":
                        satisfied.add(item["fingerprint"])
                evidence.update(status="pass", reason="expected_calls")
                session["completed_steps"] += 1
            elif record.tool_calls:
                finish("fail", "unexpected_call", "The final response requested an operation outside the fixture.")
                return
            elif all(_answer_matches(item, record.output) for item in final_content):
                finish("pass", "completed", "Final answer satisfies the supplied-result expectations.")
            else:
                finish("fail", "final_answer_mismatch", "Final answer does not satisfy the supplied-result expectations.")

        record = await run.request(target, client, spec, evaluate_record=assess)
        if record is None:
            session["reason"] = "budget_exhausted"
            run.unfinished[target.name].add("tools")
            return
        if record.tool_conversation["reason"] != "expected_calls":
            return
