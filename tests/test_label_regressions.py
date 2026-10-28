"""Regression coverage for labels without changing request execution semantics."""

import threading
import asyncio
import json

import pytest

from giraffe.conditions import condition_for
from giraffe.models import RequestRecord, RequestSpec
from giraffe.runner import _Run
from giraffe.workloads import spec
from tests.test_condition_tracking import configuration
from tests.test_ux_results import run_render_checks


class RecordingClient:
    async def execute(self, request, first_output_event=None):
        return RequestRecord(id="request", target="local", fixture_id=request.fixture_id,
            scenario=request.scenario, check_ids=request.check_ids, started_at="",
            status="completed", valid=True, input_chars=request.input_chars)


async def test_plain_requests_keep_empty_workloads_and_live_record_contract():
    events = []
    config = configuration()
    run = _Run(config, events.append, None)
    requests = [spec("first", "short", "serving"), spec("second", "short", "serving")]
    records = await run.group(config.targets[0], RecordingClient(), requests, "warm", count=2)
    assert all(record.workload == {} for record in records)
    assert all(request.workload == {} for request in requests)
    expected = {"id", "target", "fixture_id", "scenario", "condition_id", "intentional", "status", "valid", "score",
                "elapsed_ms", "first_output_ms", "check_ids", "http_status", "started_at", "dispatch_ms",
                "completed_ms", "timing_start_ms", "timing_schema", "first_token_ms", "first_reasoning_ms",
                "last_output_ms", "stream", "stream_end_ms", "answer_reads", "answer_chunks", "answer_chars",
                "first_read_answer_chars", "max_stream_gap_ms", "input_tokens", "output_tokens",
                "completion_tokens", "reasoning_tokens", "cached_prompt_tokens", "finish_reason",
                "request_timeout_seconds", "termination_reason", "termination_limit_seconds"}
    for event in events:
        assert "workload" not in event
        if event["event"] == "request_completed":
            assert set(event["request"]) == expected
            assert not {"output", "reasoning", "tool_calls", "assistant_message", "evaluation",
                        "logprob_evidence", "content_reads", "reasoning_reads", "output_events"} & event["request"].keys()
            assert all(not isinstance(value, (dict, list)) for key, value in event["request"].items() if key != "check_ids")
    parameters = run.observations["local"]["conditions"][0]["condition"]["parameters"]
    assert parameters["input_chars"] == [5, 6]
    assert parameters["output_tokens"] == 24


async def test_dispatch_callback_updates_the_original_workload_seen_by_the_record():
    config = configuration()
    run = _Run(config, None, None)
    request = spec("short", "mixed_short_long_input_before_output", "mixed",
                   heavy_active_at_dispatch=True)
    original = request.workload

    def on_start():
        original["heavy_active_at_dispatch"] = False

    record = await run.request(config.targets[0], RecordingClient(), request, on_start=on_start)
    assert request.workload is original
    assert record.workload == {"heavy_active_at_dispatch": False}


@pytest.mark.parametrize("supplied", [True, False])
async def test_builder_input_size_is_preserved_and_missing_size_only_informs_conditions(supplied):
    config = configuration()
    messages = [{"role": "assistant", "content": None,
                 "tool_calls": [{"id": "call", "type": "function", "function": {
                     "name": "lookup", "arguments": '{"query":"long tool arguments"}'}}]},
                {"role": "tool", "tool_call_id": "call", "content": "result"}]
    builder_size = len(json.dumps(messages, ensure_ascii=False)) if supplied else 0
    request = RequestSpec(fixture_id="tools_lookup_stream_turn1", scenario="tools_lookup_stream_turn1",
        check_ids=["tools"], messages=messages, input_chars=builder_size, max_tokens=24)
    run = _Run(config, None, None)
    record = await run.request(config.targets[0], RecordingClient(), request)
    assert request.input_chars == record.input_chars == builder_size
    parameters = run.observations["local"]["conditions"][0]["condition"]["parameters"]
    assert parameters["input_chars"] == (builder_size if supplied else len("result"))


async def test_single_request_uses_only_the_dispatched_candidate_for_repetition():
    config = configuration()
    run = _Run(config, None, None)
    requests = [spec("first", "short", "serving"), spec("different", "short", "serving")]
    await run.group(config.targets[0], RecordingClient(), requests, "initial", count=1)
    parameters = run.observations["local"]["conditions"][0]["condition"]["parameters"]
    assert parameters["same_prompt"] is True
    assert run.observations["local"]["conditions"][0]["planned"] == 1
    run_render_checks("const p=" + json.dumps(parameters) + r""";
state.detail={live_conditions:[{target:'local',condition:{id:'initial',parameters:p},planned:1}]};
assert(renderRequestContext({target:'local',scenario:'initial'},[]).includes('Single request'));
""")


@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_interrupted_request_records_keep_the_effective_output_cap(failure):
    class InterruptedClient:
        async def execute(self, request, first_output_event=None):
            raise failure()

    config = configuration()
    run = _Run(config, None, None)
    record = await run.request(config.targets[0], InterruptedClient(),
        spec("hello", "generation", "generation", max_tokens=config.max_output_tokens * 2))
    assert record.requested_max_tokens == config.max_output_tokens * 2


def test_named_tool_selection_has_a_tool_call_label():
    assert condition_for("tools_lookup_named_complete")["label"] == "Lookup tool calls · named tool · non-streaming"


def test_per_scenario_rows_keep_partner_turn_and_prompt_distinctions():
    run_render_checks(r"""
const scenarios=[['mixed_short_long_input_before_output','Long input interference · before output','short request'],['mixed_heavy_long_input_before_output','Long input interference · before output','heavy partner'],
 ['session_live_turn1','Live sessions','turn 2'],['session_live_turn2','Live sessions','turn 3'],
 ['consistency_c4_short_p2','Short input consistency','prompt 3'],['consistency_c4_short_p3','Short input consistency','prompt 4'],
 ['tools_lookup_stream_turn0','Lookup conversation · streaming','turn 1'],['tools_lookup_stream_turn1','Lookup conversation · streaming','turn 2']];
const html=comparisonConditionTable(comparisonConditionGroups({comparisons:scenarios.map(([scenario])=>({scenario,metric:'latency_ms'}))},{}));
for(const [scenario,label,part] of scenarios)assert(html.includes(label+' · '+part),scenario);
assert(renderPhases([{event:'scenario_started',scenario:'session_live_turn1',target:'local'}]).includes('Live sessions · turn 2'));
const reportValue=report([check('serving','pass',{evidence_ids:['one','two']})],{requests:scenarios.slice(0,2).map(([scenario],i)=>({id:i?'two':'one',scenario,target:'local',status:'completed'}))});
detail(reportValue);state.selectedCheck='local:serving';renderDetail();
assert(nodes['#main'].innerHTML.includes('interference · before output · short request'));
assert(nodes['#main'].innerHTML.includes('interference · before output · heavy partner'));
""")


@pytest.mark.parametrize("messages,expected", [
    ([{"role": "user", "content": "Retained prompt text"}], "Retained prompt text"),
    ([{"role": "assistant", "content": None, "tool_calls": []}], None),
    ([], None),
])
def test_saved_dialog_does_not_duplicate_or_offer_an_empty_prompt_preview(messages, expected):
    run_render_checks("const messages=" + json.dumps(messages) + ";const expected=" + json.dumps(expected) + r""";
const record={id:'one',scenario:'mixed_heavy_long_input_active_stream',target:'local',status:'completed',evaluation:{messages,scorer:'none'}};
state.detail={report:report([],{requests:[record]})};nodes['#request-dialog']={innerHTML:'',showModal(){}};openRequest('one');
const html=nodes['#request-dialog'].innerHTML;
assert(!html.includes('Prompt text preview'));
if(expected){assert.equal(html.split(expected).length-1,1);assert(html.includes('data-action="request-input"'));}
else assert(!html.includes('data-action="request-input"'));
""")


async def test_retained_unselected_workloads_keep_plain_requests_without_sockets(monkeypatch):
    from types import SimpleNamespace
    from giraffe import runner
    from tests.test_runner import FakeClient
    from tests.test_workloads import config

    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    result = await runner.run_suite(config(SimpleNamespace(url="http://localhost:11434/v1"),
        checks=["serving"], arrivals={}, prefix={}, buckets={}, sessions={}, consistency={}, tool_calling=True))
    assert {check.id for check in result.checks if check.status != "skipped"} == {"serving"}
    assert not any(record.workload for record in result.requests)


def test_input_messages_link_scrolls_and_focuses_the_retained_input():
    run_render_checks(r"""
let scrolled=false,focused=false;
nodes['#request-input-messages']={scrollIntoView(options){assert.equal(options.block,'start');scrolled=true;},focus(options){assert(options.preventScroll);focused=true;}};
listeners.click({target:{closest:selector=>selector==='[data-action]'?{dataset:{action:'request-input'}}:null}});
assert(scrolled);assert(focused);
""")


def test_missing_record_size_uses_condition_diagnostics_without_changing_the_record():
    run_render_checks(r"""
const record={id:'one',target:'local',scenario:'generation',input_chars:0};
state.detail={live_conditions:[{target:'local',condition:{id:'generation',parameters:{input_chars:128}}}]};
assert(renderRequestContext(record,[record]).includes('128 characters'));
assert.equal(record.input_chars,0);
""")


async def test_live_api_keeps_shape_fields_separate_from_workload(tmp_path, monkeypatch, local_tokenizer):
    from tests.test_web import browser, finished, report, start

    release = threading.Event()

    async def waiting(config, *, progress, stop_event):
        shape = {"input_chars": 128, "requested_max_tokens": 24, "requested_input_tokens": 32}
        progress({"event": "request_started", "request_slot": 0, "target": "local",
                  "scenario": "warm", "fixture_id": "short", **shape})
        assert await asyncio.to_thread(release.wait, 3)
        progress({"event": "request_completed", "request_slot": 0, **shape,
                  "request": {"id": "one", "status": "completed"}})
        await stop_event.wait()
        return report(config, cancelled=True)

    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        for status in ("running", "completed"):
            for _ in range(100):
                detail = (await client.get(f"/api/runs/{identifier}")).json()
                records = detail["live_requests"]
                if records and records[0]["status"] == status:
                    break
                await asyncio.sleep(0)
            assert records[0]["status"] == status
            assert "workload" not in records[0]
            assert records[0]["input_chars"] == 128
            assert records[0]["requested_max_tokens"] == 24
            assert records[0]["requested_input_tokens"] == 32
            release.set()
        await client.post(f"/api/runs/{identifier}/cancel", json={})
        await finished(client, identifier)
