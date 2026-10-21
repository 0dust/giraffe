"""Final acceptance audit: compatibility, explained bounds and actionable copy."""

from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from giraffe import cli, runner
from giraffe.capabilities import capabilities
from giraffe.configuration_errors import configuration_errors
from giraffe.models import RunConfig
from giraffe.planning import request_plan
from tests.test_runner import FakeClient, check, pack_config
from tests.test_run_limits_phase6_ui import Markup, rendered_form
from tests.test_ux_catalogue import browser_logic
from tests.test_web import browser

TARGET = {"name": "a", "url": "http://127.0.0.1:9", "model": "m"}


def config(**settings):
    return {"config_version": 2, "targets": [TARGET], "checks": ["correctness"], **settings}


@pytest.mark.parametrize("versioned", [False, True])
def test_main_request_pack_per_check_pass_criteria_still_load(versioned):
    values = {key: value for key, value in pack_config(checks=["correctness"]).model_dump(mode="json").items()
              if key in {"targets", "checks", "request_source", "request_pack"}}
    if versioned:
        values["config_version"] = 2
    values["test_options"] = {"correctness": {"limits": {"min_correctness": .5}}}
    before = deepcopy(values)
    configured = cli.migrated_configuration(values)
    assert values == before
    assert configured.test_options["correctness"].limits.min_correctness == .5
    assert runner._check_config(configured, "correctness").limits.min_correctness == .5
    assert configured.limits.min_correctness == 1


@pytest.mark.parametrize("threshold,status", [(.5, "pass"), (1, "fail")])
async def test_pack_per_check_criteria_actually_change_verdict(monkeypatch, threshold, status):
    original = FakeClient.execute
    calls = {}

    async def half_wrong(self, spec, **kwargs):
        record = await original(self, spec, **kwargs)
        if spec.scenario == "pack_loaded":
            calls[spec.fixture_id] = calls.get(spec.fixture_id, 0) + 1
            if calls[spec.fixture_id] == 1:
                record.output = "wrong"
        return record

    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    monkeypatch.setattr(FakeClient, "execute", half_wrong)
    configured = pack_config(checks=["correctness"], concurrency=4, default_concurrency=4,
        test_options={"correctness": {"limits": {"min_correctness": threshold}}})
    result = check(await runner.run_suite(configured), "correctness")
    assert calls == {"pack:alpha": 2, "pack:beta": 2}
    assert result.status == status


def test_unsupported_pack_request_settings_name_only_actual_fields_and_error_type():
    values = pack_config(checks=["correctness"]).model_dump(mode="json")
    values["test_options"] = {"correctness": {"samples": 9, "concurrency": 1,
                                               "limits": {"min_correctness": .5}}}
    with pytest.raises(ValidationError) as caught:
        RunConfig.model_validate(values)
    error, = caught.value.errors()
    assert error["type"] == "request_pack_override"
    assert error["ctx"]["fields"] == ["test_options.correctness.concurrency", "test_options.correctness.samples"]
    assert "limits" not in error["msg"]
    assert all(field in error["msg"] for field in error["ctx"]["fields"])
    issue, = configuration_errors(caught.value.errors(), values)
    assert issue["values"] == {"test_options.correctness.concurrency": 1, "test_options.correctness.samples": 9}


CAP_CASES = [
    ("arrivals", {"arrivals": {"requests": 10001}}, "arrivals.requests", "maximum", "10,000"),
    ("arrivals", {"arrivals": {"burst_size": 1025}}, "arrivals.burst_size", "maximum", "1,024"),
    ("arrivals", {"arrivals": {"max_pending": 257}}, "arrivals.max_pending", "maximum", "256"),
    ("arrivals", {"arrivals": {"requests_per_second": 1001}}, "arrivals.requests_per_second", "maximum", "1,000"),
    ("capacity", {"traffic": {"rates": [10001]}}, "traffic.rates", "item_maximum", "10,000"),
    ("capacity", {"traffic": {"rates": list(range(1, 18))}}, "traffic.rates", "maxItems", "16"),
    ("capacity", {"traffic": {"duration_seconds": 3601}}, "traffic.duration_seconds", "maximum", "3,600"),
    ("correctness", {"concurrency": 513}, "concurrency", "maximum", "512"),
    ("correctness", {"default_concurrency": 513}, "default_concurrency", "maximum", "512"),
    ("correctness", {"test_options": {"correctness": {"concurrency": 513}}}, "test_options.correctness.concurrency", "maximum", "512"),
    ("arrivals", {"arrivals": {"max_in_flight": 513}}, "arrivals.max_in_flight", "maximum", "512"),
    ("capacity", {"traffic": {"max_in_flight": 513}}, "traffic.max_in_flight", "maximum", "512"),
    ("sessions", {"sessions": {"concurrency": 513}}, "sessions.concurrency", "maximum", "512"),
    ("sessions", {"sessions": {"sessions": 33}}, "sessions.sessions", "maximum", "32"),
    ("sessions", {"sessions": {"turns": ["a"] * 33}}, "sessions.turns", "maxItems", "32"),
    ("sessions", {"sessions": {"turns": ["a", "x" * 8193]}}, "sessions.turns.1", "maxLength", "8,192"),
    ("sessions", {"sessions": {"workloads": [{"name": f"task{i}", "turns": ["a", "b"]} for i in range(17)]}}, "sessions.workloads", "maxItems", "16"),
    ("sessions", {"sessions": {"workloads": [{"name": "x" * 65, "turns": ["a", "b"]}]}}, "sessions.workloads.0.name", "maxLength", "64"),
    ("sessions", {"sessions": {"workloads": [{"name": "task", "weight": 10001, "turns": ["a", "b"]}]}}, "sessions.workloads.0.weight", "maximum", "10,000"),
    ("sessions", {"sessions": {"workloads": [{"name": "task", "turns": ["a"] * 33}]}}, "sessions.workloads.0.turns", "maxItems", "32"),
    ("sessions", {"sessions": {"delay_seconds": 61}}, "sessions.delay_seconds", "maximum", "60"),
    ("prefix", {"prefix": {"prefix_chars": 65537}}, "prefix.prefix_chars", "maximum", "65,536"),
    ("prefix", {"prefix": {"repeats": 101}}, "prefix.repeats", "maximum", "100"),
    ("prefix", {"prefix": {"history_turns": 33}}, "prefix.history_turns", "maximum", "32"),
    ("consistency", {"consistency": {"repetitions": 101}}, "consistency.repetitions", "maximum", "100"),
    ("consistency", {"consistency": {"prompts_per_shape": 17}}, "consistency.prompts_per_shape", "maximum", "16"),
    ("consistency", {"consistency": {"levels": [513]}}, "consistency.levels.0", "maximum", "512"),
    ("concurrency", {"concurrent_load": {"schedule": {"levels": [513]}}}, "concurrent_load.schedule.levels.0", "maximum", "512"),
    ("concurrency", {"concurrent_load": {"schedule": {"levels": list(range(1, 18))}}}, "concurrent_load.schedule.levels", "maxItems", "16"),
    ("concurrency", {"concurrent_load": {"schedule": {"duration_seconds": 3601}}}, "concurrent_load.schedule.duration_seconds", "maximum", "3,600"),
    ("concurrency", {"concurrent_load": {"profile": {"short": {"input_tokens": 131073}}}}, "concurrent_load.profile.short.input_tokens", "maximum", "131,072"),
    ("concurrency", {"concurrent_load": {"profile": {"long": {"output_tokens": 65537}}}}, "concurrent_load.profile.long.output_tokens", "maximum", "65,536"),
]


@pytest.mark.parametrize("selected,settings,path,key,maximum", CAP_CASES)
async def test_cap_errors_share_field_help_values_and_api_cli_copy(tmp_path, capsys, selected, settings, path, key, maximum):
    import re

    values = config(checks=[selected], **settings)
    async with browser(tmp_path) as client:
        response = await client.post("/api/config/preview", json=values)
    assert response.status_code == 422, response.text
    issue, = response.json()["detail"]
    canonical = re.sub(r"\.\d+(?=\.|$)", "[]", path)
    policy = capabilities()["cap_explanations"][canonical]
    assert issue["msg"] == policy["errors"][key]
    assert issue["msg"] in policy["help"]
    assert maximum in issue["msg"] and "docs/limits.md" in issue["msg"]
    assert "rejects the configuration" in issue["msg"]
    assert issue["fields"] == [path]
    assert issue["values"]
    assert issue["actions"][0]["field"] == path
    saved = tmp_path / "cap.json"
    saved.write_text(json.dumps(values))
    assert cli.main(["validate", "--config", str(saved), "--output-format", "json"]) == 4
    cli_issue, = json.loads(capsys.readouterr().out)["errors"]
    assert cli_issue["message"] == issue["msg"]
    for name in ("fields", "values", "actions"):
        assert cli_issue[name] == issue[name]


def test_retained_cap_help_is_present_at_actual_rendered_fields():
    tree = Markup(rendered_form())
    paths = {
        "profile_arrivals_requests": "arrivals.requests", "profile_arrivals_burst_size": "arrivals.burst_size",
        "profile_arrivals_max_pending": "arrivals.max_pending", "profile_arrivals_requests_per_second": "arrivals.requests_per_second",
        "traffic_rates": "traffic.rates", "traffic_duration_seconds": "traffic.duration_seconds",
        "concurrency": "concurrency", "test_correctness_concurrency": "test_options.correctness.concurrency",
        "traffic_max_in_flight": "traffic.max_in_flight", "profile_arrivals_max_in_flight": "arrivals.max_in_flight",
        "sessions_count": "sessions.sessions", "profile_sessions_concurrency": "sessions.concurrency",
        "profile_prefix_repeats": "prefix.repeats", "profile_consistency_repetitions": "consistency.repetitions",
        "profile_consistency_levels": "consistency.levels", "load_schedule_levels": "concurrent_load.schedule.levels",
        "load_profile_long_input_tokens": "concurrent_load.profile.long.input_tokens",
        "load_profile_long_output_tokens": "concurrent_load.profile.long.output_tokens",
    }
    policies = capabilities()["cap_explanations"]
    for name, path in paths.items():
        help_text, = [node for node in tree.nodes if node.get("id") == f"field-{name}-help-text"]
        assert policies[path]["help"] in help_text["text"], (name, help_text["text"])


@pytest.mark.parametrize("settings,path,name", [
    ({"arrivals": {"requests_per_second": 0}}, "arrivals.requests_per_second", "profile_arrivals_requests_per_second"),
    ({"traffic": {"duration_seconds": 0}}, "traffic.duration_seconds", "traffic_duration_seconds"),
    ({"traffic": {"rates": [0]}}, "traffic.rates", "traffic_rates"),
])
def test_positive_settings_show_inline_errors_before_api_call(settings, path, name):
    browser_logic(r"""
state.jsonDraft=state.jsonBase='synced';Object.assign(config,SETTINGS);
readForm=()=>({config,mode:'normal'});api=()=>assert.fail('Invalid positive value reached the API');
const input={dataset:{},setAttribute(k,v){this[k]=v;},getAttribute(){return '';}};
nodes['#field-'+NAME]=input;nodes['#field-'+NAME+'-error']={};
await assert.rejects(refreshRequestPreview(),error=>error.errors.some(issue=>issue.fields.includes(PATH)));
assert.equal(input['aria-invalid'],'true');assert.equal(nodes['#field-'+NAME+'-error'].hidden,false);
assert.match(nodes['#field-'+NAME+'-error'].textContent,/must be greater than 0/);
assert.match(renderRunBlueprint(config),/must be greater than 0/);
""".replace("SETTINGS", json.dumps(settings)).replace("NAME", json.dumps(name)).replace("PATH", json.dumps(path)))


def test_positive_numeric_fields_keep_small_positive_values_and_have_positive_minimum():
    tree = Markup(rendered_form())
    for name in ("profile_arrivals_requests_per_second", "traffic_duration_seconds"):
        control, = tree.named(name)
        assert float(control["min"]) > 0
        assert control["step"] == "any"
    browser_logic("assert.equal(positiveBoundErrors({...config,traffic:{...config.traffic,rates:[1e-9],duration_seconds:1e-9}},bootstrap.limits,bootstrap.cap_explanations).length,0);")


def test_warning_titles_use_catalogue_names_without_changing_breakdown():
    plan = request_plan(RunConfig.model_validate(config(checks=["cancellation", "recovery"], max_requests=1,
        max_duration_seconds=.01, test_options={"recovery": {"sustained_seconds": 1}})))
    assert {row["id"]: row["title"] for row in plan["rows"]}["limits.stop_control"] == "limits.stop_control"
    assert next(row for row in plan["rows"] if row["id"] == "recovery_baseline")["title"] == "recovery_baseline"
    warnings = plan["request_warnings"] + plan["time_warnings"]
    for warning in warnings:
        assert "limits.stop_control" not in warning["msg"]
        assert "recovery_baseline" not in warning["msg"]
        assert all("limits.stop_control" not in action["label"] and "recovery_baseline" not in action["label"]
                   for action in warning.get("actions", []))
    actions = {action["label"] for warning in warnings for action in warning.get("actions", [])}
    assert "Edit Cancellation & timeouts" in actions
    assert "Edit Recovery after load" in actions


def test_selected_session_task_cap_errors_target_the_displayed_task_fields():
    browser_logic(r"""
nodes['#new-run-form']={elements:{session_workload_count:{value:3},
  session_enabled_0:{checked:false},session_enabled_1:{checked:false},session_enabled_2:{checked:true}}};
for(const [path,name] of [
  ['sessions.workloads.0.max_tokens','session_cap_2'],
  ['sessions.workloads.0.turns','session_turn_2_0'],
  ['sessions.workloads.0.turns.1','session_turn_2_1'],
  ['sessions.workloads.0.name','session_name_2'],
  ['sessions.workloads.0.weight','session_weight_2']]){
  const destination=settingDestination(path);assert.equal(destination.check,'sessions');assert.equal(destination.name,name);
}
const section=settingDestination('sessions.workloads');assert.equal(section.section,true);assert.equal(section.check,'sessions');
""")


@pytest.mark.parametrize("settings,path,name", [
    ({"arrivals": {"requests_per_second": 0}}, "arrivals.requests_per_second", "profile_arrivals_requests_per_second"),
    ({"traffic": {"duration_seconds": 0}}, "traffic.duration_seconds", "traffic_duration_seconds"),
    ({"traffic": {"rates": [0]}}, "traffic.rates", "traffic_rates"),
])
def test_json_positive_settings_show_same_inline_errors_before_api_call(settings, path, name):
    browser_logic(r"""
state.jsonDraft=JSON.stringify({...config,...SETTINGS});state.jsonBase='unchanged';
nodes['#config-error']={};renderRequestPreview=()=>{};
api=()=>assert.fail('Invalid JSON positive value reached the API');
await applyConfig();
const issue=currentSettingErrors().find(item=>item.fields.includes(PATH));
assert(issue);assert.match(issue.msg,/must be greater than 0/);
assert.match(nodes['#config-error'].textContent,/must be greater than 0/);
assert.equal(settingDestination(PATH).name,NAME);
""".replace("SETTINGS", json.dumps(settings)).replace("NAME", json.dumps(name)).replace("PATH", json.dumps(path)))
