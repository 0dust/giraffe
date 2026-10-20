"""Tool conversations are one planned condition, including skipped follow-up turns."""

import copy
import json
import uuid

import pytest

from giraffe.models import RequestRecord, RunConfig, ToolConversationFixture
from giraffe.runner import _Run
from giraffe.tool_conversations import run_conversation
from tests.test_tool_conversations import fixture


class ConversationClient:
    def __init__(self, fail_first=False):
        self.fail_first = fail_first

    async def execute(self, spec, *, first_output_event=None):
        calls = [{"id": f"call_{index}", "type": "function", "function": {
            "name": call.name, "arguments": json.dumps(call.arguments)}}
            for index, call in enumerate(spec.tool_expectation.expected_calls or [])]
        output = " ".join(message["content"] for message in spec.messages if message["role"] == "tool")
        return RequestRecord(id=uuid.uuid4().hex, target="local", fixture_id=spec.fixture_id,
            scenario=spec.scenario, check_ids=spec.check_ids, started_at="2026-10-20T00:00:00Z",
            status="failed" if self.fail_first else "completed", valid=not self.fail_first,
            output=output, tool_calls=calls, finish_reason="tool_calls" if calls else "stop",
            assistant_message={"role": "assistant", "content": output, "tool_calls": calls})


def setup_conversation(**updates):
    events = []
    config = RunConfig.model_validate({"targets": [{"name": "local", "url": "http://localhost/v1", "model": "fake"}],
        "checks": ["tools"], "max_requests": 10, "max_duration_seconds": 10,
        "context_limit": 8192, "max_output_tokens": 128, **updates})
    return _Run(config, events.append, None), config.targets[0], events


@pytest.mark.parametrize("mode", ["complete", "stream"])
async def test_conversation_plans_one_condition_and_keeps_turns_as_request_details(mode):
    run, target, events = setup_conversation()
    value = fixture()
    value["steps"].append(copy.deepcopy(value["steps"][0]))
    value["steps"][1]["user_message"] = "Check again for this new question."
    conversation = ToolConversationFixture.model_validate(value)
    await run_conversation(run, target, ConversationClient(), conversation, mode)
    assert len(run.records) == 3
    assert len(run.observations[target.name]["conditions"]) == 1
    row = run.observations[target.name]["conditions"][0]
    assert row["condition"]["id"] == f"tools_weather-result_{mode}"
    assert {record.condition_id for record in run.records} == {row["condition"]["id"]}
    assert (row["planned"], row["sent"], row["completed"], row["not_sent"], row["reason"]) == (3, 3, 3, 0, None)
    assert row["status"] == "finished" and row["failed"] == 0
    assert [e["event"] for e in events].count("condition_started") == 1
    assert [e["event"] for e in events].count("condition_finished") == 1
    assert all(e["condition_id"] == row["condition"]["id"] for e in events if e["event"].startswith("request_"))


@pytest.mark.parametrize("cause", ["history", "budget", "failure"])
async def test_conversation_shortfall_explains_dependent_turns_skipped(cause):
    run, target, events = setup_conversation(max_requests=1) if cause == "budget" else setup_conversation()
    value = fixture()
    if cause == "history":
        value["steps"][0]["expected_calls"][0]["result"]["padding"] = "x" * 8192
    await run_conversation(run, target, ConversationClient(fail_first=cause == "failure"),
                           ToolConversationFixture.model_validate(value), "complete")
    row = run.observations[target.name]["conditions"][0]
    assert len(run.records) == row["sent"] == row["completed"] == 1
    assert row["planned"] == 2 and row["not_sent"] == 1
    assert row["reason"] == "dependent turns skipped" and row["status"] == "finished"
    assert row["failed"] == int(cause == "failure")
    assert events[-1]["event"] == "condition_finished"
    assert events[-1]["not_sent"] == 1 and events[-1]["reason"] == "dependent turns skipped"
