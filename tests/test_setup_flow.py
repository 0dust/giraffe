"""Exercise setup presets through the browser's real configuration module."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from giraffe.capabilities import capabilities
from giraffe.models import CHECK_LIMIT_FIELDS, CHECK_NAMES, CHECK_OPTION_FIELDS, ConcurrentLoad, RunConfig


MODULE = (Path(__file__).parents[1] / "src/giraffe/static/form-config.mjs").as_uri()


def run_setup_script(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute browser configuration behavior")
    config = RunConfig(
        targets=[{"name": "local", "url": "http://localhost:8000/v1", "model": "test"}],
        checks=["access", "correctness"], max_requests=100,
    )
    checks = [
        {"id": name, "option_fields": sorted(CHECK_OPTION_FIELDS.get(name, set())),
         "limit_fields": sorted(CHECK_LIMIT_FIELDS.get(name, set())), "defaults": config.effective_check(name)}
        for name in CHECK_NAMES
    ]
    prelude = f"""
import assert from 'node:assert/strict';
import * as form from {json.dumps(MODULE)};
// Supply the same schema-derived bounds as the production workbench.
const limits = {json.dumps(capabilities()['limits'])};
const setup = {{...form,
  inferSetupPreset:config=>form.inferSetupPreset(config,{{limits}}),
  configureSetupLoad:(config,preset,options={{}})=>form.configureSetupLoad(config,preset,{{...options,limits}}),
  configureConcurrentLoad:(config,values,defaults,options={{}})=>form.configureConcurrentLoad(config,values,defaults,{{...options,limits}}),
}};
const source = {json.dumps(config.model_dump(mode='json'))};
const defaults = {json.dumps(ConcurrentLoad().model_dump(mode='json'))};
const checks = {json.dumps(checks)};
"""
    result = subprocess.run(
        [node, "--input-type=module", "-"], input=prelude + script,
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


def test_setup_preset_inference_preserves_imported_selection_and_ceiling():
    run_setup_script("""
assert.equal(typeof setup.inferSetupPreset, 'function');
const before = structuredClone(source);
assert.equal(setup.inferSetupPreset(source), 'answers');
assert.deepEqual(source, before);
source.checks = ['capacity', 'correctness', 'access'];
assert.equal(setup.inferSetupPreset(source), 'arrival');
source.checks = ['access', 'correctness', 'json'];
assert.equal(setup.inferSetupPreset(source), 'custom');
source.checks = ['access', 'access', 'correctness'];
assert.equal(setup.inferSetupPreset(source), 'custom');
source.checks = ['concurrency', 'access'];
source.concurrent_load = {...defaults,schedule:{...defaults.schedule,levels:[1,4,8]}};
source.concurrency = 8;
assert.equal(setup.inferSetupPreset(source), 'concurrent');
source.concurrency = 16;
const imported = structuredClone(source);
assert.equal(setup.inferSetupPreset(source), 'concurrent');
assert.deepEqual(source, imported);
source.concurrency = 4;
assert.equal(setup.inferSetupPreset(source), 'concurrent');
source.concurrency = 8;
source.test_options = {correctness:{samples:9}};
assert.equal(setup.inferSetupPreset(source), 'concurrent');
source.test_options.access = {request_timeout_seconds:12};
assert.equal(setup.inferSetupPreset(source), 'custom');
source.test_options = {};
source.concurrent_load.schedule.levels=[limits['concurrent_load.schedule.levels'].items.maximum];
assert.equal(setup.inferSetupPreset(source),'concurrent');
for (const levels of [[], ['8'], [1, 1], [0], [limits['concurrent_load.schedule.levels'].items.maximum+1], null]) {
  source.concurrent_load.schedule.levels = levels;
  assert.equal(setup.inferSetupPreset(source), 'custom');
}
""")


def test_explicit_setup_presets_preserve_dormant_data_and_safety_limits():
    run_setup_script("""
assert.equal(typeof setup.applySetupPreset, 'function');
source.checks = ['json','gpu','tools','correctness'];
source.structured_json = source.metrics = source.tool_calling = true;
source.concurrency = 16;
source.request_source = 'request_pack';
source.request_pack = {version:1,cases:[{fixture_id:'one',messages:[{role:'user',content:'literal'}],scorer:'exact',expected:'answer'}]};
source.request_options = {temperature:0.4,metadata:{team:'qa'}};
source.targets[0].headers_env = {'X-Service':'SERVICE_HEADER'};
source.targets[0].deployment = {runtime:{version:'custom'},discovery:'vllm'};
source.judging = {judges:[{name:'grader',url:'http://judge/v1',model:'grade',headers_env:{'X-Project':'PROJECT'}}],ca_bundle:'/judge/ca',proxy:'http://proxy',request_options:{temperature:0}};
source.test_options = {correctness:{samples:9},recovery:{sustained_seconds:2}};
source.prefix = {prefix_chars:512,repeats:7,history_turns:3};
source.custom_fixtures = '/fixtures/custom.json';
source.limits.min_samples = 7;
const before = structuredClone(source);
const concurrent = setup.applySetupPreset(source, 'concurrent', defaults);
assert.deepEqual(source, before);
assert.deepEqual(concurrent.checks, ['access','concurrency']);
assert.equal(concurrent.structured_json, false);
assert.equal(concurrent.metrics, false);
assert.equal(concurrent.tool_calling, false);
assert.equal(concurrent.concurrent_load.pass_criteria.min_valid_responses, 5);
assert.equal(concurrent.concurrency, 16);
assert.equal(concurrent.max_requests, 100);
assert.equal(concurrent.max_output_tokens, 128);
assert.deepEqual(defaults.schedule.levels,[1,4]);
for (const preset of ['answers','arrival','custom','concurrent']) {
  const next = setup.applySetupPreset(concurrent, preset, defaults);
  for (const key of ['targets','request_source','request_pack','request_options','judging','test_options','prefix','custom_fixtures']) {
    assert.deepEqual(next[key], source[key]);
  }
  assert.equal(next.max_requests, 100);
  assert.equal(next.max_output_tokens, 128);
}
assert.deepEqual(setup.applySetupPreset(concurrent,'answers',defaults).checks,['access','correctness']);
assert.deepEqual(setup.applySetupPreset(concurrent,'arrival',defaults).checks,['access','correctness','capacity']);
const custom = setup.applySetupPreset(source,'custom',defaults);
assert.deepEqual(custom.checks,source.checks);
assert.equal(custom.structured_json,true);
assert.equal(custom.metrics,true);
assert.equal(custom.tool_calling,true);
source.concurrent_load = {...defaults,schedule:{levels:[1,4,8],duration_seconds:3},profile:{short:{input_tokens:25,output_tokens:5},medium:{input_tokens:50,output_tokens:20},long:{input_tokens:100,output_tokens:40}}};
const existing = setup.applySetupPreset(source,'concurrent',defaults);
assert.equal(existing.concurrency,16);
assert.deepEqual(existing.concurrent_load,source.concurrent_load);
assert.equal(existing.concurrent_load.schedule.duration_seconds,3);
source.concurrent_load = null;
source.limits.min_samples = 5;
assert.equal(setup.applySetupPreset(source,'concurrent',defaults).concurrent_load.pass_criteria.min_valid_responses,5);
""")


def test_setup_load_derives_only_basic_concurrency_without_raising_budgets():
    result = run_setup_script("""
assert.equal(typeof setup.configureSetupLoad, 'function');
const preset = setup.applySetupPreset(source,'concurrent',defaults);
const values = new FormData();
values.set('load_schedule_levels','1, 4 8');
values.set('load_pass_criteria_min_valid_responses','5');
values.set('load_schedule_duration_seconds','10');
const load = setup.configureConcurrentLoad(preset,values,defaults);
const before = structuredClone(load);
const configured = setup.configureSetupLoad(load,'concurrent');
assert.deepEqual(load,before);
assert.deepEqual(configured.concurrent_load.schedule.levels,[1,4,8]);
assert.equal(configured.concurrency,null);
assert.equal(configured.max_requests,100);
assert.equal(configured.max_output_tokens,128);
for (const mode of ['answers','arrival','custom']) {
  const imported = {...configured,concurrency:16};
  assert.deepEqual(setup.configureSetupLoad(imported,mode),imported);
}
console.log(JSON.stringify(configured));
""")
    configured = RunConfig.model_validate(result)
    assert configured.concurrency is None
    assert configured.max_requests == 100


def test_setup_load_keeps_invalid_drafts_and_rejects_them_when_running():
    run_setup_script("""
const preset = setup.applySetupPreset(source,'concurrent',defaults);
for (const raw of ['', 'oops', '1, nope', '1, 1', '0', String(limits['concurrent_load.schedule.levels'].items.maximum+1), '1.5', 'Infinity']) {
  const values = new FormData();
  values.set('load_schedule_levels',raw);
  values.set('load_pass_criteria_min_valid_responses','5');
  values.set('load_schedule_duration_seconds','10');
  const draft = setup.configureConcurrentLoad(preset,values,defaults,{allowInvalid:true});
  const before = structuredClone(draft);
  assert.deepEqual(setup.configureSetupLoad(draft,'concurrent',{allowInvalid:true}),before);
  assert.deepEqual(draft,before);
  assert.throws(()=>setup.configureSetupLoad(draft,'concurrent'),/Concurrency levels/);
  assert.deepEqual(setup.configureSetupLoad(draft,'custom'),before);
}
""")


def test_load_profile_is_editable_and_suite_output_cap_is_a_default():
    result = run_setup_script("""
source.checks = ['access','concurrency'];
source.concurrency = 8;
source.concurrent_load = {...defaults,schedule:{levels:[1,4,8],duration_seconds:3},mixed:{...defaults.mixed,seed:42},input_token_tolerance:0.2,calibration_attempts:2};
const values = new FormData();
for (const [name,value] of Object.entries({
  load_profile_short_input_tokens:100,load_profile_medium_input_tokens:600,load_profile_long_input_tokens:1200,
  load_profile_short_output_tokens:12,load_profile_medium_output_tokens:36,load_profile_long_output_tokens:120,
})) values.set(name,String(value));
const before = structuredClone(source);
const configured = setup.configureConcurrentLoad(source,values,defaults);
assert.deepEqual(source,before);
assert.deepEqual(configured.concurrent_load.profile,{short:{input_tokens:100,output_tokens:12},medium:{input_tokens:600,output_tokens:36},long:{input_tokens:1200,output_tokens:120}});
assert.deepEqual(configured.concurrent_load.schedule,{levels:[1,4,8],duration_seconds:3});
assert.equal(configured.concurrent_load.mixed.seed,42);
assert.equal(configured.concurrent_load.input_token_tolerance,0.2);
assert.equal(configured.concurrent_load.calibration_attempts,2);
assert.equal(configured.max_requests,100);
assert.equal(configured.max_output_tokens,128);
values.set('load_profile_long_output_tokens','256');
const ownCap = setup.configureConcurrentLoad(source,values,defaults);
assert.equal(ownCap.concurrent_load.profile.long.output_tokens,256);
assert.equal(ownCap.max_output_tokens,128);
const draft = setup.configureConcurrentLoad(source,values,defaults,{allowInvalid:true});
assert.equal(draft.concurrent_load.profile.long.output_tokens,256);
assert.equal(draft.max_output_tokens,128);
const inactive = {...source,checks:['access','correctness']};
assert.equal(setup.configureConcurrentLoad(inactive,values,defaults).concurrent_load.profile.long.output_tokens,256);
for (const size of ['short','medium','long']) for (const field of ['input_tokens','output_tokens']) {
  const name = `load_profile_${size}_${field}`;
  const maximum = limits[`concurrent_load.profile.${size}.${field}`].maximum;
  for (const raw of ['', '0', String(maximum+1), '1.5', 'oops', 'Infinity']) {
    const invalid = new FormData(); invalid.set(name,raw);
    assert.throws(()=>setup.configureConcurrentLoad(source,invalid,defaults),/whole number/);
    const kept = setup.configureConcurrentLoad(source,invalid,defaults,{allowInvalid:true});
    const expected = raw === '' ? null : Number.isFinite(Number(raw)) ? Number(raw) : raw;
    assert.equal(kept.concurrent_load.profile[size][field],expected);
  }
}
console.log(JSON.stringify(configured));
""")
    configured = RunConfig.model_validate(result)
    assert configured.concurrent_load.profile.long.input_tokens == 1200
    assert configured.concurrent_load.profile.long.output_tokens == 120


def test_unrendered_load_fields_preserve_settings_without_activating_check():
    run_setup_script("""
source.concurrent_load = {...defaults,mixed:{...defaults.mixed,seed:9}};
const values = new FormData();
values.set('concurrent_levels','1, 2');
const result = setup.configureConcurrentLoad(source,values,defaults);
assert.deepEqual(result,source);
assert.deepEqual(setup.configureConcurrentLoad(source,new FormData(),defaults),source);
values.set('load_schedule_levels','1, 2');
const inactive = setup.configureConcurrentLoad(source,values,defaults);
assert.deepEqual(inactive.concurrent_load.schedule.levels,[1,2]);
assert.deepEqual(inactive.checks,['access','correctness']);
assert.equal(inactive.concurrent_load.mixed.seed,9);
""")


def test_setup_preset_preserves_profile_above_suite_output_default():
    run_setup_script("""
source.max_output_tokens = 32;
const configured = setup.applySetupPreset(source,'concurrent',defaults);
assert.deepEqual(configured.concurrent_load.profile,defaults.profile);
assert.equal(configured.max_output_tokens,32);
assert.equal(configured.max_requests,100);
assert.equal(defaults.profile.long.output_tokens,128);
const values = new FormData(); values.set('load_schedule_duration_seconds','10');
const ownProfile = setup.configureConcurrentLoad(configured,values,defaults);
assert.deepEqual(ownProfile.concurrent_load.profile,defaults.profile);
assert.equal(ownProfile.max_output_tokens,32);
const draft = setup.configureConcurrentLoad(configured,values,defaults,{allowInvalid:true});
assert.equal(draft.concurrent_load.profile.long.output_tokens,128);
assert.equal(draft.max_output_tokens,32);
""")


def test_load_types_and_criteria_roundtrip_checkbox_state_without_generic_overrides():
    result = run_setup_script("""
source.checks = ['concurrency'];
source.concurrent_load.mixed.seed = 123;
const values = new FormData();
values.set('load_rendered','1');
for (const name of ['combinations','mixed','interference','stress']) values.set(`load_${name}_enabled`,'on');
for (const shape of ['short_short','long_short','short_long']) values.set(`load_combinations_selected_${shape}`,'on');
for (const [shape,share] of Object.entries({short_short:40,long_short:20,short_long:40})) values.set(`load_mixed_shares_${shape}`,String(share));
values.set('load_schedule_levels','2, 4');
values.set('load_stress_large_output','on');
values.set('load_stress_min_output_percent','80');
values.set('load_interference_trigger','after_chunks');
values.set('load_interference_trigger_chunks','5');
values.set('load_interference_max_slowdown','');
values.set('load_pass_criteria_first_output_p95_ms','');
values.set('load_pass_criteria_response_p95_ms','1200');
const configured = setup.configureConcurrentLoad(source,values,defaults);
assert.deepEqual(configured.concurrent_load.combinations.selected,['short_short','long_short','short_long']);
assert.equal(configured.concurrent_load.mixed.seed,123);
assert.equal(configured.concurrent_load.mixed.shares.medium_long ?? 0,0);
assert.equal(Object.values(configured.concurrent_load.mixed.shares).reduce((sum,share)=>sum+share,0),100);
assert.equal(configured.concurrent_load.stress.large_input,false);
assert.equal(configured.concurrent_load.stress.large_output,true);
assert.equal(configured.concurrent_load.interference.max_slowdown,null);
assert.equal(configured.concurrent_load.pass_criteria.first_output_p95_ms,null);
assert.equal(configured.concurrent_load.pass_criteria.response_p95_ms,1200);
const testValues = new FormData(); testValues.append('check','concurrency');
testValues.set('test_mode_concurrency','custom'); testValues.set('test_concurrency_samples','17');
testValues.set('traffic_mode','default');
assert.deepEqual(checks.find(check=>check.id==='concurrency').option_fields,['request_timeout_seconds']);
assert.equal(setup.configureTests(configured,testValues,checks,source.traffic).test_options.concurrency,undefined);
console.log(JSON.stringify(configured));
""")
    configured = RunConfig.model_validate(result)
    assert configured.concurrent_load.mixed.seed == 123
    assert configured.concurrent_load.interference.max_slowdown is None


def test_ordinary_form_serialization_preserves_preset_overrides_and_imported_fields():
    result = run_setup_script("""
source.request_source = 'request_pack';
source.request_pack = {version:1,cases:[{fixture_id:'one',messages:[{role:'user',content:'literal'}],weight:2,scorer:'exact',expected:'answer',max_tokens:32,options:{temperature:0}}]};
source.targets[0].headers_env = {'X-Service':'SERVICE_HEADER'};
source.request_options = {temperature:0.4,metadata:{team:'qa'}};
source.judging.judges = [{name:'grader',url:'http://judge/v1',model:'grade',api_key_env:'JUDGE_KEY',headers_env:{'X-Project':'PROJECT'}}];
source.judging.ca_bundle = '/judge/ca';
source.judging.proxy = 'http://proxy';
source.test_options = {access:{request_timeout_seconds:12},correctness:{samples:9,concurrency:4},recovery:{sustained_seconds:2}};
source.prefix = {prefix_chars:512,repeats:7,history_turns:3};
source.concurrent_load = {...defaults,schedule:{levels:[1,4,8],duration_seconds:5},mixed:{...defaults.mixed,seed:42},input_token_tolerance:0.2,calibration_attempts:2};
const preset = setup.applySetupPreset(source,'concurrent',defaults);
const before = structuredClone(preset);
const values = new FormData();
for (const id of preset.checks) values.append('check',id);
for (const check of checks) {
  values.set(`test_mode_${check.id}`,preset.test_options[check.id] ? 'custom' : 'default');
  for (const name of check.option_fields) values.set(`test_${check.id}_${name}`,setup.optionValue(preset,check,name) ?? '');
  for (const name of check.limit_fields) values.set(`test_${check.id}_limit_${name}`,setup.limitValue(preset,check,name) ?? '');
}
values.set('traffic_mode','default');
values.set('load_schedule_levels','1, 4, 8');
values.set('judge_count','1');
for (const key of ['name','url','model','api_key_env']) values.set(`judge_0_${key}`,preset.judging.judges[0][key]);
for (const key of ['concurrency','request_timeout_seconds','max_duration_seconds','max_output_tokens']) {
  values.set(`judging_${key}`,String(preset.judging[key]));
}
let configured = setup.configureTests(preset,values,checks,preset.traffic);
configured = setup.configureConcurrentLoad(configured,values,defaults);
configured = setup.configureJudging(configured,values);
configured = setup.configureSetupLoad(configured,'concurrent');
assert.deepEqual(preset,before);
assert.deepEqual(configured,preset);
assert.equal(configured.request_source,'request_pack');
assert.equal(configured.max_requests,100);
for (const mode of ['answers','arrival','custom']) {
  const switched = setup.applySetupPreset(configured,mode,defaults);
  assert.deepEqual(switched.test_options,source.test_options);
  assert.deepEqual(switched.judging,source.judging);
  assert.deepEqual(switched.request_pack,source.request_pack);
  assert.deepEqual(switched.concurrent_load,source.concurrent_load);
  assert.equal(switched.concurrency,null);
}
console.log(JSON.stringify(configured));
""")
    configured = RunConfig.model_validate(result)
    assert configured.test_options["correctness"].concurrency == 4
    assert configured.request_pack.cases[0].fixture_id == "one"
    assert configured.judging.judges[0].headers_env == {"X-Project": "PROJECT"}


def test_setup_transitions_preserve_owned_overrides_when_inheritance_matches():
    result = run_setup_script("""
function valuesFor(config) {
  const values = new FormData();
  for (const id of config.checks) values.append('check',id);
  for (const check of checks) {
    values.set(`test_mode_${check.id}`,config.test_options[check.id] ? 'custom' : 'default');
    for (const name of check.option_fields) values.set(`test_${check.id}_${name}`,setup.optionValue(config,check,name) ?? '');
    for (const name of check.limit_fields) values.set(`test_${check.id}_limit_${name}`,setup.limitValue(config,check,name) ?? '');
  }
  values.set('traffic_mode','default');
  return values;
}
source.samples = 6;
source.concurrency = 16;
source.limits.min_correctness = 0.95;
source.limits.latency_ms = 1200;
source.limits.first_output_ms = null;
source.test_options = {
  correctness:{samples:6,concurrency:8,limits:{min_correctness:0.95}},
  capacity:{limits:{latency_ms:1200,first_output_ms:null}},
  recovery:{sustained_seconds:2},
};
source.concurrent_load = {...defaults,schedule:{...defaults.schedule,levels:[1,4,8]}};
const before = structuredClone(source);
const concurrent = setup.applySetupPreset(source,'concurrent',defaults);
assert.equal(concurrent.concurrency,16);
const captured = setup.configureTests(concurrent,valuesFor(concurrent),checks,source.traffic);
assert.deepEqual(captured.test_options,source.test_options);
assert.deepEqual(source,before);
const answers = setup.applySetupPreset(captured,'answers',defaults);
answers.concurrency = 16;
answers.samples = 9;
answers.limits.min_correctness = 0.9;
answers.limits.latency_ms = 2000;
answers.limits.first_output_ms = 500;
const configured = setup.configureTests(answers,valuesFor(answers),checks,source.traffic);
assert.deepEqual(configured.test_options,source.test_options);
assert.equal(setup.optionValue(configured,checks.find(check => check.id === 'correctness'),'concurrency'),8);
assert.equal(setup.limitValue(configured,checks.find(check => check.id === 'capacity'),'first_output_ms'),null);
console.log(JSON.stringify(configured));
""")
    configured = RunConfig.model_validate(result)
    assert configured.effective_check("correctness")["concurrency"] == 8
    assert configured.effective_check("correctness")["samples"] == 6
    assert configured.effective_check("correctness")["limits"]["min_correctness"] == 0.95
    assert configured.effective_check("capacity")["limits"]["latency_ms"] == 1200
    assert configured.effective_check("capacity")["limits"]["first_output_ms"] is None


def test_owned_override_preservation_keeps_reset_blank_and_sparse_semantics():
    run_setup_script("""
const values = new FormData();
for (const id of source.checks) values.append('check',id);
source.concurrency = 8;
source.limits.latency_ms = 1200;
source.test_options = {
  correctness:{samples:source.samples,concurrency:8},
  capacity:{limits:{latency_ms:1200,first_output_ms:null}},
  recovery:{sustained_seconds:2},
};
for (const check of checks) {
  values.set(`test_mode_${check.id}`,source.test_options[check.id] ? 'custom' : 'default');
  for (const name of check.option_fields) values.set(`test_${check.id}_${name}`,setup.optionValue(source,check,name) ?? '');
  for (const name of check.limit_fields) values.set(`test_${check.id}_limit_${name}`,setup.limitValue(source,check,name) ?? '');
}
values.set('traffic_mode','default');
values.set('test_correctness_samples','');
values.set('test_mode_capacity','default');
values.set('test_mode_json','custom');
const configured = setup.configureTests(source,values,checks,source.traffic);
assert.deepEqual(configured.test_options.correctness,{concurrency:8});
assert.equal(configured.test_options.capacity,undefined);
assert.equal(configured.test_options.json,undefined);
assert.deepEqual(configured.test_options.recovery,{sustained_seconds:2});
values.set('test_mode_correctness','default');
assert.equal(setup.configureTests(configured,values,checks,source.traffic).test_options.correctness,undefined);
""")


def test_load_validation_preserves_invalid_drafts_for_server_field_errors():
    run_setup_script("""
source.checks = ['concurrency'];
for (const [fields,message] of [
  [{load_mixed_shares_short_short:'59'},/must equal 100%/],
  [{load_combinations_selected_short_short:''},/Select at least one combination/],
  [{load_stress_enabled:'on',load_stress_large_input:'',load_stress_large_output:''},/Select at least one stress case/],
  [{load_combinations_enabled:'',load_mixed_enabled:''},/Enable at least one load type/],
  [{load_interference_max_slowdown:'1'},/greater than 1/],
  [{load_pass_criteria_min_valid_responses:'1'},/whole number/],
]) {
  const values = new FormData();
  for (const [key,value] of Object.entries(fields)) values.set(key,value);
  const before=structuredClone(source),originalDefaults=structuredClone(defaults);
  assert.throws(()=>setup.configureConcurrentLoad(source,values,defaults),message);
  const draft = setup.configureConcurrentLoad(source,values,defaults,{allowInvalid:true});
  assert.deepEqual(draft.checks,source.checks);
  assert.equal(draft.max_requests,source.max_requests);
  assert.equal(draft.concurrency,source.concurrency);
  assert.deepEqual(source,before);assert.deepEqual(defaults,originalDefaults);
}
const sorted = new FormData(); sorted.set('load_schedule_levels','8, 1, 4');
assert.deepEqual(setup.configureConcurrentLoad(source,sorted,defaults).concurrent_load.schedule.levels,[1,4,8]);
""")
