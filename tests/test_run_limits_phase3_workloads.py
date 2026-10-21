"""Per-check settings on extended workloads, without opening target sockets."""

import json
import time
from types import SimpleNamespace

import pytest

from giraffe.fixtures import builtin_fixtures
from giraffe.models import RequestRecord, RunConfig
from giraffe.runner import _Run, _missing_timing, _stats, _timing_violations
from giraffe.tool_checks import default_tool_fixture
from giraffe.workloads import arrivals, consistency, prefixes, results, sessions, tools


TARGET = {"name": "local", "url": "http://unused.test/v1", "model": "test"}


def configuration(check, **values):
    return RunConfig.model_validate({
        "targets": [TARGET], "checks": [check], "samples": 2,
        "max_output_tokens": 32, "request_timeout_seconds": .01,
        "context_limit": 8192, "limits": {"min_samples": 2, "latency_ms": 50},
        "arrivals": {"requests": 2, "mode": "burst", "burst_size": 2},
        "prefix": {"repeats": 3, "prefix_chars": 256, "history_turns": 2},
        "sessions": {"sessions": 2, "concurrency": 2, "modes": ["fixed"],
                     "turns": ["Remember the name.", "Repeat the name."], "delay_seconds": 0},
        "consistency": {"levels": [1], "repetitions": 2, "shapes": ["short"]},
        **values,
    })


class Client:
    def __init__(self):
        self.specs = []

    async def execute(self, spec, first_output_event=None):
        self.specs.append(spec.model_copy(deep=True))
        output = str(spec.expected or "K7P4")
        if spec.workload.get("selection") == "conversation" and spec.workload.get("turn_id"):
            output = spec.messages[-1]["content"]
        calls = []
        if spec.tool_expectation and spec.tool_expectation.expected_calls:
            calls = [{"id": f"call_{index}", "type": "function", "function": {
                "name": call.name, "arguments": json.dumps(call.arguments)}}
                for index, call in enumerate(spec.tool_expectation.expected_calls)]
        return RequestRecord(id=f"r{len(self.specs)}", target="local", fixture_id=spec.fixture_id,
            scenario=spec.scenario, check_ids=spec.check_ids, started_at="now", status="completed",
            valid=True, output=output, requested_max_tokens=spec.max_tokens,
            first_output_ms=1, elapsed_ms=10, finish_reason="stop", completion_tokens=1,
            input_tokens=1, tool_calls=calls, assistant_message={"role": "assistant", "content": output,
                                                                "tool_calls": calls})


@pytest.mark.parametrize("check,callback,output", [
    ("arrivals", arrivals, 96), ("prefix", prefixes, 24),
    ("sessions", sessions, 48), ("consistency", consistency, 96), ("tools", tools, 96),
])
async def test_extended_specs_resolve_local_settings_without_mutating_suite(check, callback, output):
    options = {"request_timeout_seconds": .5}
    if check in {"arrivals", "consistency", "tools"}:
        options["max_output_tokens"] = 96
    extra = {"test_options": {check: options}}
    if check == "sessions":
        extra["sessions"] = {"sessions": 2, "concurrency": 2, "modes": ["fixed"],
                             "turns": ["Remember the name.", "Repeat the name."], "max_tokens": 48, "delay_seconds": 0}
    config = configuration(check, **extra)
    run, client = _Run(config, None, None), Client()
    args = [run, config.targets[0], client]
    if check in {"arrivals", "consistency"}:
        args.append(builtin_fixtures(config))
    await callback(*args)
    assert client.specs
    assert {spec.request_timeout_seconds for spec in client.specs} == {.5}
    assert {spec.max_tokens for spec in client.specs} == {output}
    assert config.max_output_tokens == 32
    assert config.request_timeout_seconds == .01
    assert run.config is config


@pytest.mark.parametrize("check", ["arrivals", "prefix", "sessions", "consistency", "tools"])
def test_extended_check_pass_criteria_change_verdict_and_remain_isolated(check):
    config = configuration(check)
    fixture = default_tool_fixture()
    records = [RequestRecord(
        id=f"r{index}", target="local", fixture_id="test", scenario=(
            "consistency_c1_short_p0" if check == "consistency" else check),
        check_ids=[check], started_at="now", status="completed", valid=True,
        output="K7P4", score=True, answer_hash="same", first_output_ms=1, elapsed_ms=10,
        finish_reason="stop", requested_max_tokens=24, completion_tokens=1,
        workload={"level": 1, "input_bucket": "short", "prompt_index": 0, "load_qualified": True,
                  "selection": "automatic", "tool_fixture_id": fixture.fixture_id,
                  "mode": "complete" if index == 0 else "stream"},
    ) for index in range(2)]
    run = SimpleNamespace(config=config, records=records, max_lag_ms=0, pack_specs=[],
        start=time.monotonic(), observations={"local": {}}, unfinished={"local": set()})

    def outcome():
        return results(run, config.targets[0], _stats, _timing_violations, _missing_timing)[0]

    before = outcome()
    assert before.status != "fail"
    overridden = config.model_copy(deep=True)
    # Validate the actual public override shape instead of injecting a resolved Limits.
    overridden = RunConfig.model_validate(overridden.model_dump() | {
        "test_options": {check: {"limits": {"latency_ms": 5}}}})
    run.config = overridden
    after = outcome()
    assert after.status == "fail"
    assert after.metrics["timing_violations"]
    assert overridden.limits.latency_ms == 50
    run.config = config
    assert outcome().status == before.status


async def test_tool_conversation_uses_effective_settings_on_every_turn():
    fixture = {
        "fixture_id": "weather", "messages": [{"role": "user", "content": "Check weather."}],
        "tools": [{"type": "function", "function": {"name": "weather", "parameters": {
            "type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}],
        "steps": [{"expected_calls": [{"name": "weather", "arguments": {"city": "Warsaw"},
                                        "result": {"weather": "sunny", "observation_id": "{{observation_id}}"}}]}],
        "final_content": [{"mode": "contains", "value": "sunny"},
                          {"mode": "contains", "value": "{{observation_id}}"}],
    }
    config = configuration("tools", context_limit=128,
        tool_checks={"conversations": [fixture], "modes": ["complete"]},
        test_options={"tools": {"max_output_tokens": 96, "request_timeout_seconds": .5,
                                "context_limit": 8192}})
    run, client = _Run(config, None, None), Client()
    await tools(run, config.targets[0], client)
    conversation_specs = [spec for spec in client.specs if spec.workload["selection"] == "conversation"]
    assert len(conversation_specs) == 2
    assert {spec.request_timeout_seconds for spec in conversation_specs} == {.5}
    assert {spec.max_tokens for spec in conversation_specs} == {96}
    assert conversation_specs[1].messages[-1]["role"] == "tool"
    assert run.observations["local"]["tool_conversations"][0]["status"] == "pass"
    assert config.context_limit == 128


@pytest.mark.parametrize("check,callback", [("prefix", prefixes), ("sessions", sessions)])
async def test_history_admission_uses_local_context_window(check, callback):
    config = configuration(check, context_limit=128,
        test_options={check: {"context_limit": 8192, "request_timeout_seconds": .5}})
    run, client = _Run(config, None, None), Client()
    await callback(run, config.targets[0], client)
    assert len(client.specs) >= 2
    assert "history" not in str(run.observations["local"].get("prefix_stop", ""))
    assert config.context_limit == 128


async def test_logprob_consistency_uses_its_effective_output_timeout_and_context():
    config = configuration("consistency", context_limit=128,
        consistency={"levels": [1], "repetitions": 2, "shapes": ["long"],
                     "logprobs": {"priming_rounds": 0}},
        test_options={"consistency": {"max_output_tokens": 256, "request_timeout_seconds": .5,
                                      "context_limit": 2048}})
    run, client = _Run(config, None, None), Client()
    await consistency(run, config.targets[0], client, builtin_fixtures(config))
    assert len(client.specs) == 2
    assert {spec.max_tokens for spec in client.specs} == {256}
    assert {spec.request_timeout_seconds for spec in client.specs} == {.5}
    assert all(spec.input_chars > 1000 and spec.options["logprobs"] for spec in client.specs)
    assert config.context_limit == 128


async def test_concurrent_load_profile_caps_and_timeout_are_own_settings():
    from giraffe.concurrent_load import execute

    config = configuration("concurrency", concurrent_load={
        "profile": {"short": {"input_tokens": 64, "output_tokens": 96},
                    "medium": {"input_tokens": 96, "output_tokens": 128},
                    "long": {"input_tokens": 128, "output_tokens": 256}},
        "schedule": {"levels": [1], "duration_seconds": 1},
        "combinations": {"selected": ["short_short"]}, "mixed": {"enabled": False}},
        test_options={"concurrency": {"request_timeout_seconds": .5}})
    run, client = _Run(config, None, None), Client()
    run.deadline = time.monotonic() + .03
    # Let the finite sending window progress without a large zero-latency request loop.
    original = client.execute

    async def delayed(spec, first_output_event=None):
        import asyncio
        await asyncio.sleep(.002)
        record = await original(spec, first_output_event)
        record.input_tokens = spec.requested_input_tokens
        return record

    client.execute = delayed
    await execute(run, config.targets[0], client)
    assert client.specs
    assert {spec.request_timeout_seconds for spec in client.specs} == {.5}
    assert {spec.max_tokens for spec in client.specs if not spec.scenario.startswith("load_calibration_")} == {96}
    assert config.max_output_tokens == 32


@pytest.mark.parametrize("suite_cap", [16, 512])
@pytest.mark.parametrize("check,callback", [
    ("arrivals", arrivals), ("prefix", prefixes), ("sessions", sessions),
    ("consistency", consistency), ("tools", tools),
])
async def test_inherited_extended_request_budgets_preserve_fixture_defaults(suite_cap, check, callback):
    config = configuration(check, max_output_tokens=suite_cap, request_timeout_seconds=.5)
    run, client = _Run(config, None, None), Client()
    args = [run, config.targets[0], client]
    if check in {"arrivals", "consistency"}:
        args.append(builtin_fixtures(config))
    await callback(*args)
    expected = {"arrivals": min(24, suite_cap), "prefix": min(24, suite_cap),
                "sessions": 32, "consistency": min(192, suite_cap),
                "tools": suite_cap}[check]
    assert client.specs
    assert {spec.max_tokens for spec in client.specs} == {expected}
    assert {spec.request_timeout_seconds for spec in client.specs} == {.5}


async def test_session_workload_owns_output_budget_above_suite_and_session_defaults():
    config = configuration("sessions", sessions={
        "profile": "performance", "modes": ["live"], "sessions": 2, "concurrency": 2,
        "max_tokens": 48, "delay_seconds": 0,
        "workloads": [{"name": "essay", "turns": ["Write a sentence.", "Expand it."],
                       "max_tokens": 96}]},
        test_options={"sessions": {"request_timeout_seconds": .5}})
    run, client = _Run(config, None, None), Client()
    await sessions(run, config.targets[0], client)
    assert len(client.specs) == 4
    assert {spec.max_tokens for spec in client.specs} == {96}
    assert {spec.request_timeout_seconds for spec in client.specs} == {.5}
    assert config.max_output_tokens == 32
    assert config.sessions.max_tokens == 48


async def test_auto_selected_sessions_retain_suite_derived_default_output_budget():
    config = configuration("sessions", sessions=None, max_output_tokens=16, request_timeout_seconds=.5)
    run, client = _Run(config, None, None), Client()
    await sessions(run, config.targets[0], client)
    assert client.specs
    assert {spec.max_tokens for spec in client.specs} == {16}


async def test_standalone_tool_context_override_preserves_requests_without_cropping():
    config = configuration("tools", context_limit=128,
        test_options={"tools": {"request_timeout_seconds": .5}})
    rejected_run, rejected_client = _Run(config, None, None), Client()
    await tools(rejected_run, config.targets[0], rejected_client)
    assert len(rejected_client.specs) == 2
    assert not rejected_run.unfinished["local"]
    assert "tool_context_rejections" not in rejected_run.observations["local"]
    raised = RunConfig.model_validate(config.model_dump() | {
        "test_options": {"tools": {"request_timeout_seconds": .5, "context_limit": 8192}}})
    admitted_run, admitted_client = _Run(raised, None, None), Client()
    await tools(admitted_run, raised.targets[0], admitted_client)
    assert len(admitted_client.specs) == 2
    assert not admitted_run.unfinished["local"]
    assert "tool_context_rejections" not in admitted_run.observations["local"]
    expected_messages = [message.model_dump() for message in default_tool_fixture().messages]
    assert all(spec.messages == expected_messages for spec in admitted_client.specs)
    assert [spec.model_dump() for spec in rejected_client.specs] == [spec.model_dump() for spec in admitted_client.specs]
    assert config.context_limit == raised.context_limit == 128


async def test_standalone_tool_fixtures_all_send_without_silent_context_rejection():
    template = default_tool_fixture().model_dump()
    config = configuration("tools", context_limit=128, tool_checks={"fixtures": [
        template | {"fixture_id": f"weather-{index}"} for index in range(9)]})
    run, client = _Run(config, None, None), Client()
    await tools(run, config.targets[0], client)
    assert len(client.specs) == 18
    assert {spec.workload["tool_fixture_id"] for spec in client.specs} == {f"weather-{index}" for index in range(9)}
    assert all(spec.messages == template["messages"] for spec in client.specs)
    assert "tool_context_rejections" not in run.observations["local"]
    assert not run.unfinished["local"]


def test_default_and_example_tool_payloads_still_fit_context_admission():
    from pathlib import Path

    import yaml

    from giraffe.runner import _check_config
    from giraffe.tool_checks import resolved_tool_fixtures

    configs = [RunConfig.model_validate({"targets": [TARGET], "checks": ["tools"]})]
    configs.extend(RunConfig.model_validate(yaml.safe_load(path.read_text()))
                   for path in sorted(Path("examples").glob("*.yaml")))
    seen = 0
    for config in configs:
        if "tools" not in config.checks:
            continue
        effective = _check_config(config, "tools")
        for fixture in resolved_tool_fixtures(effective):
            history_bytes = len(json.dumps([message.model_dump() for message in fixture.messages],
                                          ensure_ascii=False).encode())
            tools_bytes = len(json.dumps(fixture.tools, ensure_ascii=False).encode())
            assert history_bytes + tools_bytes + effective.max_output_tokens + 32 <= effective.context_limit
            seen += 1
    assert seen >= 10
