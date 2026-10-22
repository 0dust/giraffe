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
    values = RunConfig(targets=[TARGET], checks=["correctness"], request_source="request_pack",
        request_pack={"version": 1, "cases": [{"fixture_id": "one", "scorer": "none",
            "messages": [{"role": "user", "content": "Question"}]}]}).model_dump(mode="json")
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


CAP_CASES = [('load',
  {'load_test': {'patterns': {'bursts': {'enabled': True, 'size': 1025, 'first_seconds': 0}}}},
  'load_test.patterns.bursts.size',
  'maximum',
  '1,024'),
 ('load',
  {'load_test': {'patterns': {'rates': [10001]}}},
  'load_test.patterns.rates.0',
  'item_maximum',
  '10,000'),
 ('load',
  {'load_test': {'patterns': {'rates': [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]}}},
  'load_test.patterns.rates',
  'maxItems',
  '16'),
 ('load',
  {'load_test': {'schedule': {'duration_seconds': 3601}}},
  'load_test.schedule.duration_seconds',
  'maximum',
  '3,600'),
 ('correctness', {'concurrency': 513}, 'concurrency', 'maximum', '512'),
 ('correctness', {'default_concurrency': 513}, 'default_concurrency', 'maximum', '512'),
 ('correctness',
  {'test_options': {'correctness': {'concurrency': 513}}},
  'test_options.correctness.concurrency',
  'maximum',
  '512'),
 ('sessions', {'sessions': {'concurrency': 513}}, 'sessions.concurrency', 'maximum', '512'),
 ('sessions', {'sessions': {'sessions': 33}}, 'sessions.sessions', 'maximum', '32'),
 ('sessions',
  {'sessions': {'turns': ['a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a',
                          'a']}},
  'sessions.turns',
  'maxItems',
  '32'),
 ('sessions',
  {'sessions': {'turns': ['a',
                          'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx']}},
  'sessions.turns.1',
  'maxLength',
  '8,192'),
 ('sessions',
  {'sessions': {'workloads': [{'name': 'task0', 'turns': ['a', 'b']},
                              {'name': 'task1', 'turns': ['a', 'b']},
                              {'name': 'task2', 'turns': ['a', 'b']},
                              {'name': 'task3', 'turns': ['a', 'b']},
                              {'name': 'task4', 'turns': ['a', 'b']},
                              {'name': 'task5', 'turns': ['a', 'b']},
                              {'name': 'task6', 'turns': ['a', 'b']},
                              {'name': 'task7', 'turns': ['a', 'b']},
                              {'name': 'task8', 'turns': ['a', 'b']},
                              {'name': 'task9', 'turns': ['a', 'b']},
                              {'name': 'task10', 'turns': ['a', 'b']},
                              {'name': 'task11', 'turns': ['a', 'b']},
                              {'name': 'task12', 'turns': ['a', 'b']},
                              {'name': 'task13', 'turns': ['a', 'b']},
                              {'name': 'task14', 'turns': ['a', 'b']},
                              {'name': 'task15', 'turns': ['a', 'b']},
                              {'name': 'task16', 'turns': ['a', 'b']}]}},
  'sessions.workloads',
  'maxItems',
  '16'),
 ('sessions',
  {'sessions': {'workloads': [{'name': 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
                               'turns': ['a', 'b']}]}},
  'sessions.workloads.0.name',
  'maxLength',
  '64'),
 ('sessions',
  {'sessions': {'workloads': [{'name': 'task', 'weight': 10001, 'turns': ['a', 'b']}]}},
  'sessions.workloads.0.weight',
  'maximum',
  '10,000'),
 ('sessions',
  {'sessions': {'workloads': [{'name': 'task',
                               'turns': ['a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a',
                                         'a']}]}},
  'sessions.workloads.0.turns',
  'maxItems',
  '32'),
 ('sessions', {'sessions': {'delay_seconds': 61}}, 'sessions.delay_seconds', 'maximum', '60'),
 ('prefix', {'prefix': {'prefix_chars': 65537}}, 'prefix.prefix_chars', 'maximum', '65,536'),
 ('prefix', {'prefix': {'repeats': 101}}, 'prefix.repeats', 'maximum', '100'),
 ('prefix', {'prefix': {'history_turns': 33}}, 'prefix.history_turns', 'maximum', '32'),
 ('consistency', {'consistency': {'repetitions': 101}}, 'consistency.repetitions', 'maximum', '100'),
 ('consistency',
  {'consistency': {'prompts_per_shape': 17}},
  'consistency.prompts_per_shape',
  'maximum',
  '16'),
 ('consistency', {'consistency': {'levels': [513]}}, 'consistency.levels.0', 'item_maximum', '512'),
 ('load',
  {'load_test': {'patterns': {'concurrent': {'levels': [513]}}}},
  'load_test.patterns.concurrent.levels.0',
  'item_maximum',
  '512'),
 ('load',
  {'load_test': {'patterns': {'concurrent': {'levels': [1,
                                                        2,
                                                        3,
                                                        4,
                                                        5,
                                                        6,
                                                        7,
                                                        8,
                                                        9,
                                                        10,
                                                        11,
                                                        12,
                                                        13,
                                                        14,
                                                        15,
                                                        16,
                                                        17]}}}},
  'load_test.patterns.concurrent.levels',
  'maxItems',
  '16'),
 ('load',
  {'load_test': {'schedule': {'duration_seconds': 3601}}},
  'load_test.schedule.duration_seconds',
  'maximum',
  '3,600'),
 ('load',
  {'load_test': {'profile': {'short': {'input_tokens': 131073}}}},
  'load_test.profile.short.input_tokens',
  'maximum',
  '131,072'),
 ('load',
  {'load_test': {'profile': {'long': {'output_tokens': 65537}}}},
  'load_test.profile.long.output_tokens',
  'maximum',
  '65,536')]


@pytest.mark.parametrize("selected,settings,path,key,maximum", CAP_CASES)
async def test_cap_errors_share_field_help_values_and_api_cli_copy(tmp_path, capsys, selected, settings, path, key, maximum):
    import re

    values = config(checks=[selected], **settings)
    async with browser(tmp_path) as client:
        response = await client.post("/api/config/preview", json=values)
    assert response.status_code == 422, response.text
    issue, = response.json()["detail"]
    canonical = path.rsplit(".", 1)[0] if key.startswith("item_") else re.sub(r"\.\d+(?=\.|$)", "[]", path)
    policy = capabilities()["cap_explanations"][canonical]
    assert issue["msg"] == policy["errors"][key]
    assert issue["msg"] in policy["help"]
    assert maximum in issue["msg"] and "docs/limits.md" not in issue["msg"]
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
        "concurrency": "concurrency", "test_correctness_concurrency": "test_options.correctness.concurrency",
        "sessions_count": "sessions.sessions", "profile_sessions_concurrency": "sessions.concurrency",
        "profile_prefix_repeats": "prefix.repeats", "profile_consistency_repetitions": "consistency.repetitions",
        "profile_consistency_levels": "consistency.levels",
        "load_patterns_rates": "load_test.patterns.rates",
        "load_schedule_duration_seconds": "load_test.schedule.duration_seconds",
        "load_patterns_concurrent_levels": "load_test.patterns.concurrent.levels",
        "load_patterns_bursts_size": "load_test.patterns.bursts.size",
        "load_profile_long_input_tokens": "load_test.profile.long.input_tokens",
        "load_profile_long_output_tokens": "load_test.profile.long.output_tokens",
    }
    policies = capabilities()["cap_explanations"]
    for name, path in paths.items():
        node_id = f"field-{name}-help" if name.startswith('load_') else f"field-{name}-help-text"
        help_text, = [node for node in tree.nodes if node.get("id") == node_id]
        assert policies[path]["help"] in help_text["text"], (name, help_text["text"])


@pytest.mark.parametrize("settings,path,name", [
    ({"load_test": {"patterns": {"constant": {"enabled": True}, "rates": [0]}}}, "load_test.patterns.rates", "load_patterns_rates"),
    ({"load_test": {"patterns": {"bursts": {"enabled": True, "first_seconds": 0, "gap_seconds": 0}}}}, "load_test.patterns.bursts.gap_seconds", "load_patterns_bursts_gap_seconds"),
    ({"load_test": {"patterns": {"constant": {"enabled": True}, "rates": [-1]}}}, "load_test.patterns.rates", "load_patterns_rates"),
])
def test_positive_settings_show_inline_errors_before_api_call(settings, path, name):
    browser_logic(r"""
state.jsonDraft=state.jsonBase='synced';config.checks=['load'];Object.assign(config,SETTINGS);
readForm=()=>({config,mode:'normal'});api=()=>assert.fail('Invalid positive value reached the API');
const input={dataset:{},setAttribute(k,v){this[k]=v;},getAttribute(){return '';}};
nodes['#field-'+NAME]=input;nodes['#field-'+NAME+'-error']={};
await assert.rejects(refreshRequestPreview(),error=>error.errors.some(issue=>issue.fields.includes(PATH)));
assert.equal(input['aria-invalid'],'true');assert.equal(nodes['#field-'+NAME+'-error'].hidden,false);
const expected=PATH.endsWith('rates')?'Enter positive, increasing rates separated by commas.':'Enter a number greater than 0 for Time between burst starts (seconds).';
assert.equal(nodes['#field-'+NAME+'-error'].textContent,expected);
assert(renderRunBlueprint(config).includes(expected));
""".replace("SETTINGS", json.dumps(settings)).replace("NAME", json.dumps(name)).replace("PATH", json.dumps(path)))


def test_positive_numeric_fields_keep_small_positive_values_and_have_positive_minimum():
    tree = Markup(rendered_form())
    duration, = tree.named("load_schedule_duration_seconds")
    assert duration["min"] == "1" and duration["step"] == "1"
    gap, = tree.named("load_patterns_bursts_gap_seconds")
    assert float(gap["min"]) > 0 and gap["step"] == "any"
    configured = RunConfig.model_validate(config(checks=['load'], load_test={
        'patterns': {'rates': [1e-9], 'bursts': {'gap_seconds': 1e-9}},
        'schedule': {'duration_seconds': 1}}))
    assert configured.load_test.patterns.rates == [1e-9]
    assert configured.load_test.patterns.bursts.gap_seconds == 1e-9


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
    ({"load_test": {"patterns": {"constant": {"enabled": True}, "rates": [0]}}}, "load_test.patterns.rates", "load_patterns_rates"),
    ({"load_test": {"patterns": {"bursts": {"enabled": True, "first_seconds": 0, "gap_seconds": 0}}}}, "load_test.patterns.bursts.gap_seconds", "load_patterns_bursts_gap_seconds"),
    ({"load_test": {"patterns": {"constant": {"enabled": True}, "rates": [-1]}}}, "load_test.patterns.rates", "load_patterns_rates"),
])
def test_json_positive_settings_show_same_inline_errors_before_api_call(settings, path, name):
    browser_logic(r"""
config.checks=['load'];state.jsonDraft=JSON.stringify({...config,...SETTINGS});state.jsonBase='unchanged';
nodes['#config-error']={};renderRequestPreview=()=>{};
api=()=>assert.fail('Invalid JSON positive value reached the API');
await applyConfig();
const issue=currentSettingErrors().find(item=>item.fields.includes(PATH));
assert(issue);assert.match(issue.msg,/must be greater than 0/);
assert.match(nodes['#config-error'].textContent,/must be greater than 0/);
assert.equal(settingDestination(PATH).name,NAME);
""".replace("SETTINGS", json.dumps(settings)).replace("NAME", json.dumps(name)).replace("PATH", json.dumps(path)))


@pytest.mark.parametrize('check,section,field,value,maximum', [
    ('arrivals', 'arrivals', 'requests', 10001, 10000),
    ('arrivals', 'arrivals', 'max_pending', 257, 256),
    ('arrivals', 'arrivals', 'requests_per_second', 1001, 1000),
    ('arrivals', 'arrivals', 'max_in_flight', 513, 512),
    ('capacity', 'traffic', 'max_in_flight', 513, 512),
])
async def test_legacy_load_cap_errors_keep_the_original_field_and_safe_evidence(tmp_path, capsys, check, section, field, value, maximum):
    values = config(checks=[check], **{section: {field: value}})
    path = section + '.' + field
    async with browser(tmp_path) as client:
        response = await client.post('/api/config/preview', json=values)
    assert response.status_code == 422, response.text
    issue, = response.json()['detail']
    assert issue['fields'] == [path]
    assert issue['values'] == {path: value}
    assert str(maximum) in issue['msg'].replace(',', '')
    assert issue['actions'][0]['field'] == path
    saved = tmp_path / 'legacy-cap.json'
    saved.write_text(json.dumps(values))
    assert cli.main(['validate', '--config', str(saved), '--output-format', 'json']) == 4
    cli_issue, = json.loads(capsys.readouterr().out)['errors']
    assert cli_issue['message'] == issue['msg']
    for key in ('fields', 'values', 'actions'):
        assert cli_issue[key] == issue[key]
