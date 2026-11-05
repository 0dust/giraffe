"""Sampled session traffic through the real runner and local HTTP endpoint."""

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from giraffe.local_tokenizer import needs_tokenizer
from giraffe.models import Sessions
from giraffe.planning import request_plan
from giraffe.reporting import compare_baseline, write_report
from giraffe.runner import run_suite
from giraffe.session_sampling import session_plan
from tests.fake_endpoint import FakeEndpoint
from tests.test_workloads import check, config
from tests.test_ux_catalogue import browser_logic


def settings(**updates):
    return {
        "profile": "performance", "modes": ["live"], "sessions": 4, "concurrency": 2,
        "workloads": [{"name": "chat", "turns": ["First question", "Follow up", "Summarize"]}],
        "sampling": {"seed": 42, "think_time_seconds": {"values": [.01, .03]},
                     "input_tokens": {"values": [32, 64]},
                     "output_tokens": {"values": [16, 32], "weights": [1, 3]},
                     "turn_count": {"values": [2, 3]}},
        **updates,
    }


@pytest.mark.parametrize("field,distribution", [
    ("think_time_seconds", {"values": [-1]}),
    ("think_time_seconds", {"values": [61]}),
    ("think_time_seconds", {"values": [True]}),
    ("input_tokens", {"values": [1.5]}),
    ("output_tokens", {"values": [0]}),
    ("turn_count", {"values": [4]}),
    ("input_tokens", {"values": []}),
    ("input_tokens", {"values": [32, 64], "weights": [1]}),
    ("input_tokens", {"values": [32, 64], "weights": [0, 0]}),
    ("input_tokens", {"values": [float("inf")]}),
])
def test_invalid_distributions_are_rejected(field, distribution):
    value = settings()
    value["sampling"][field] = distribution
    with pytest.raises(ValidationError):
        Sessions.model_validate(value)


def test_correctness_accepts_pause_sampling_but_preserves_recall_shape():
    assert Sessions().sampling is None
    assert Sessions().saved_answers == []
    recall = Sessions(sampling={"seed": 7, "think_time_seconds": {"values": [.01, .03], "weights": [0, 1]}})
    plans = session_plan(recall)
    assert session_plan(recall) == plans
    assert len(plans) == recall.sessions * len(recall.modes)
    for plan in plans:
        assert len(plan["turns"]) == len(recall.turns)
        assert plan["turns"][0]["think_time_seconds"] == 0
        assert all(turn["think_time_seconds"] == .03 for turn in plan["turns"][1:])
    for field, values in [("input_tokens", [32]), ("output_tokens", [16]), ("turn_count", [2])]:
        with pytest.raises(ValidationError, match="performance profile"):
            Sessions(sampling={field: {"values": values}})


def test_plans_are_reproducible_per_session_and_honor_weights_and_fallbacks():
    value = settings()
    value["sampling"]["output_tokens"]["weights"] = [0, 1]
    parsed = Sessions.model_validate(value)
    first = session_plan(parsed)
    assert session_plan(parsed) == first
    parsed.sessions = 5
    assert session_plan(parsed)[:4] == first
    assert all(turn["output_tokens"] == 32 for plan in first for turn in plan["turns"])
    assert all(plan["turns"][0]["think_time_seconds"] == 0 for plan in first)
    parsed.sampling.seed += 1
    assert session_plan(parsed)[:4] != first
    plain = Sessions.model_validate(settings(sampling=None, delay_seconds=.5))
    assert session_plan(plain)[0]["turns"][1] == {
        "think_time_seconds": .5, "input_tokens": None, "output_tokens": 32}


def test_session_sampling_ui_config_roundtrip_and_error_field_mapping():
    from giraffe.models import RunConfig

    source = RunConfig(targets=[{"name": "local", "url": "http://localhost:1", "model": "test"}],
                       checks=["sessions"], sessions=settings())
    result = browser_logic(r"""
const original=JSON.stringify(config),markup=renderSessionEditor(config);
for(const key of ['think_time_seconds','input_tokens','output_tokens','turn_count']){
  assert(markup.includes('name="session_sample_'+key+'"'));
  assert(markup.includes('name="session_weights_'+key+'"'));
}
assert(markup.includes('name="session_sampling_seed"'));
assert.equal(JSON.stringify(config),original);
const values=new FormData();
values.set('sessions_profile','performance');values.set('sessions_count','4');
values.set('sessions_delay','0');values.set('sessions_max_tokens','32');
values.set('session_workload_count','1');values.set('session_enabled_0','on');
const task=config.sessions.workloads[0];values.set('session_name_0',task.name);values.set('session_weight_0','1');
values.set('session_turn_count_0',task.turns.length);
task.turns.forEach((turn,index)=>values.set('session_turn_0_'+index,turn));
values.set('session_sampling_seed','7');values.set('session_sample_think_time_seconds','0, 0.1');
values.set('session_weights_think_time_seconds','1, 3');
values.set('session_sample_input_tokens','128, 512');values.set('session_sample_output_tokens','32, 64');
values.set('session_sample_turn_count','2, 3');
const saved=configureSessions(config,values,bootstrap.session_workloads.defaults);
assert.equal(JSON.stringify(saved.sessions.sampling.think_time_seconds),JSON.stringify({values:[0,.1],weights:[1,3]}));
assert.equal(saved.sessions.sampling.seed,7);
assert.equal(settingDestination('sessions.sampling.input_tokens.values.0').name,'session_sample_input_tokens');
assert.equal(settingDestination('sessions.sampling.output_tokens.weights').name,'session_weights_output_tokens');
assert.equal(configureSessions(saved,new FormData(),bootstrap.session_workloads.defaults).sessions.sampling.seed,7);
for(const key of ['think_time_seconds','input_tokens','output_tokens','turn_count']){
  values.set('session_sample_'+key,'');values.set('session_weights_'+key,'');
}
assert.equal(configureSessions(saved,values,bootstrap.session_workloads.defaults).sessions.sampling,null);
values.set('session_sample_input_tokens','32,,64');
assert.throws(()=>configureSessions(saved,values,bootstrap.session_workloads.defaults),/comma-separated/);
console.log(JSON.stringify(saved));
""", source)
    parsed = RunConfig.model_validate(result)
    assert parsed.sessions.sampling.seed == 7
    assert parsed.sessions.sampling.input_tokens.values == [128, 512]


def test_correctness_pause_sampling_ui_roundtrip_keeps_only_pauses():
    result = browser_logic(r"""
const values=new FormData();values.set('sessions_profile','correctness');
values.set('session_sampling_seed','7');values.set('session_sample_think_time_seconds','0, 0.02');
values.set('session_weights_think_time_seconds','1, 3');
values.set('session_sample_input_tokens','64');values.set('session_sample_turn_count','3');
const saved=configureSessions(config,values,bootstrap.session_workloads.defaults);
assert.equal(saved.sessions.profile,'correctness');
assert.equal(JSON.stringify(saved.sessions.sampling),JSON.stringify({seed:7,think_time_seconds:{values:[0,.02],weights:[1,3]}}));
assert.equal(saved.sessions.turns.length,2);
assert.equal(JSON.stringify(saved.sessions.saved_answers),'[]');
console.log(JSON.stringify(saved.sessions));
""")
    assert Sessions.model_validate(result).sampling.seed == 7


def test_pause_choice_serializes_only_active_settings():
    browser_logic(r"""
const values=new FormData();values.set('sessions_profile','correctness');
values.set('session_sampling_seed','7');values.set('session_pause_mode','dynamic');
assert.throws(()=>configureSessions(config,values,bootstrap.session_workloads.defaults),/Enter pause values/);
const draft=configureSessions(config,values,bootstrap.session_workloads.defaults,{allowInvalid:true});
assert.equal(draft.sessions.sampling.think_time_seconds.values.length,0);
values.set('session_sample_think_time_seconds','0, 1, 5');values.set('session_weights_think_time_seconds','1, 2, 3');
const dynamic=configureSessions(config,values,bootstrap.session_workloads.defaults);
assert.equal(JSON.stringify(dynamic.sessions.sampling.think_time_seconds.values),'[0,1,5]');
values.set('session_pause_mode','fixed');values.set('profile_edits_sessions','["delay_seconds"]');values.set('profile_sessions_delay_seconds','2');
const fixed=configureSessions(configureProfiles(dynamic,values,bootstrap.profile_defaults),values,bootstrap.session_workloads.defaults);
assert.equal(fixed.sessions.sampling,null);assert.equal(fixed.sessions.delay_seconds,2);
values.set('sessions_profile','performance');values.set('sessions_count','2');values.set('profile_sessions_max_tokens','32');
values.set('session_workload_count','1');values.set('session_enabled_0','on');values.set('session_name_0','chat');values.set('session_weight_0','1');
values.set('session_turn_count_0','2');values.set('session_turn_0_0','Hello');values.set('session_turn_0_1','Tell me more');
values.set('session_sample_output_tokens','16, 32');
const performance=configureSessions(fixed,values,bootstrap.session_workloads.defaults);
assert.equal(performance.sessions.delay_seconds,2);assert(!performance.sessions.sampling.think_time_seconds);
assert.equal(JSON.stringify(performance.sessions.sampling.output_tokens.values),'[16,32]');
values.set('session_pause_mode','dynamic');values.set('profile_sessions_delay_seconds','invalid hidden draft');
const again=configureSessions(configureProfiles(performance,values,bootstrap.profile_defaults),values,bootstrap.session_workloads.defaults);
assert.equal(again.sessions.delay_seconds,2);assert.equal(again.sessions.sampling.think_time_seconds.values.length,3);
""")


@pytest.mark.parametrize("modes", [["fixed"], ["live"], ["fixed", "live"]])
async def test_correctness_sampled_pauses_keep_scoring_history_and_report(modes):
    with FakeEndpoint() as endpoint:
        value = config(endpoint, checks=["sessions"], retention="all", sessions={
            "modes": modes, "sessions": 2, "concurrency": 2,
            "sampling": {"seed": 7, "think_time_seconds": {"values": [.01, .03], "weights": [0, 1]}},
        })
        plans = session_plan(value.sessions)
        preview = request_plan(value)
        assert not any("0 s timeout" in warning for warning in preview["warnings"])
        waits = [row for row in preview["unknown_time_components"] if row["id"] == "sessions_wait"]
        assert len(waits) == 1
        assert waits[0]["timeout_bound_seconds"] == pytest.approx(.03 * 2 * len(modes))
        report = await run_suite(value)
    records = [record for record in report.requests if "sessions" in record.check_ids]
    assert len(records) == len(plans) * len(value.sessions.turns)
    assert all(record.score is True for record in records)
    observation = report.observations["targets"]["local"]["sessions"]
    expected = {(plan["mode"], plan["index"]): plan["turns"] for plan in plans}
    for item in observation["sessions"]:
        assert item["sampled_turns"] == expected[(item["mode"], int(item["id"].rsplit(":", 1)[1]))]
    for record in records:
        if record.workload["turn_id"]:
            assert record.workload["configured_delay_seconds"] == .03
            assert record.workload["actual_pause_seconds"] >= .025
    assert check(report, "sessions").metrics["conversation_performance"]["sampling"]["seed"] == 7


@pytest.mark.usefixtures("local_tokenizer")
async def test_sampled_live_history_lengths_pauses_planning_and_report_roundtrip(tmp_path):
    with FakeEndpoint(first_token_delay=.02, chunk_delay=.02) as endpoint:
        events = []
        value = config(endpoint, checks=["sessions"], sessions=settings(), retention="all")
        plans = session_plan(value.sessions)
        expected_count = sum(len(plan["turns"]) for plan in plans)
        assert needs_tokenizer(value)
        preview = request_plan(value)
        row = next(row for row in preview["rows"] if row["id"] == "sessions")
        assert str(expected_count) in row["summary"]
        assert row["requests_per_target"] == expected_count
        assert not any("0 s timeout" in warning for warning in preview["warnings"])
        report = await run_suite(value, progress=events.append)
        calls = [call for call in endpoint.requests if call["messages"][0]["content"] != ""]
    records = [record for record in report.requests if "sessions" in record.check_ids]
    first_request = next(index for index, event in enumerate(events)
                         if event["event"] == "request_started" and event["scenario"].startswith("session_"))
    assert not any(event["scenario"] == "session_input" and event["event"].startswith("preparation_")
                   for event in events[first_request:])
    assert len(records) == expected_count
    observed = report.observations["targets"]["local"]["sessions"]
    assert [item["sampled_turns"] for item in observed["sessions"]] == [plan["turns"] for plan in plans]
    for record in records:
        metadata = record.workload
        prepared = metadata["input_preparation"]
        assert abs(prepared["input_tokens"] - metadata["sampled_input_tokens"]) <= max(1, metadata["sampled_input_tokens"] * .05)
        assert prepared["token_count_scope"] in {"message_content", "builtin_reference"}
        assert "messages" not in prepared
        assert record.requested_max_tokens == metadata["sampled_output_tokens"]
        if metadata["turn_id"]:
            assert metadata["actual_pause_seconds"] >= metadata["configured_delay_seconds"] - .005
    followups = [call for call in calls if len(call["messages"]) >= 3]
    assert followups
    assert all(call["messages"][1]["role"] == "assistant" and call["messages"][1]["content"] for call in followups)
    assert all(call["messages"][0]["content"].endswith("First question") for call in followups)
    measured = check(report, "sessions").metrics["conversation_performance"]
    assert measured["planned_turns"] == expected_count
    assert measured["completed_conversations"] == 4
    from giraffe import session_sampling, workloads
    from giraffe.deployment import fixture_hash

    assert report.manifest["fixture_pack"]["workload_fixture_sha256"] == fixture_hash({
        "generator_source_sha256": workloads.FIXTURE_SOURCE_SHA256,
        "settings": {"sessions": value.sessions.model_dump(mode="json")},
        "session_sampling_source_sha256": hashlib.sha256(Path(session_sampling.__file__).read_bytes()).hexdigest(),
    })
    write_report(report, tmp_path, html=True)
    saved = json.loads((tmp_path / "report.json").read_text())
    assert saved["manifest"]["config"]["sessions"]["sampling"]["seed"] == 42
    assert "Sampled pauses and lengths" in (tmp_path / "report.html").read_text()
    # A different seed changes the workload identity, even with the same prompts.
    changed = report.model_copy(deep=True)
    changed.manifest["config"]["sessions"]["sampling"]["seed"] = 99
    compared = compare_baseline(changed, report)
    assert compared.baseline["status"] == "inconclusive"


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("limitation", ["short_target", "context", "request_budget"])
async def test_incomplete_sampling_stops_without_cropping_or_hiding_planned_turns(limitation):
    value = settings()
    extra = {}
    if limitation == "short_target":
        value["sampling"]["input_tokens"] = {"values": [1]}
    elif limitation == "context":
        extra["context_limit"] = 128
    else:
        extra["max_requests"] = 2
    with FakeEndpoint() as endpoint:
        report = await run_suite(config(endpoint, checks=["sessions"], sessions=value, **extra))
    observed = report.observations["targets"]["local"]["sessions"]
    assert observed["planned_turns"] > sum(len(item["turns"]) for item in observed["sessions"])
    assert any(item["stop_reason"] for item in observed["sessions"])
    assert check(report, "sessions").status == "inconclusive"
    if limitation != "request_budget":
        assert not [record for record in report.requests if "sessions" in record.check_ids]


@pytest.mark.usefixtures("local_tokenizer")
async def test_cancellation_interrupts_sampled_think_time_and_preserves_partial_results():
    value = settings()
    value["sampling"] = {"think_time_seconds": {"values": [60]}}
    stop = asyncio.Event()

    def event(item):
        if item["event"] == "request_completed" and item.get("scenario", "").startswith("session_"):
            stop.set()

    with FakeEndpoint() as endpoint:
        report = await asyncio.wait_for(run_suite(config(endpoint, checks=["sessions"], sessions=value),
                                                 stop_event=stop, progress=event), timeout=5)
    assert report.abort_reason == "cancelled by user"
    assert len([record for record in report.requests if "sessions" in record.check_ids]) < 8
