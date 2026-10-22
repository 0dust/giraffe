"""Exercise setup presets through the browser's real configuration module."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from giraffe.capabilities import capabilities
from giraffe.models import CHECK_LIMIT_FIELDS, CHECK_NAMES, CHECK_OPTION_FIELDS, LoadTest, RunConfig


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
  configureLoadTest:(config,values,defaults,options={{}})=>form.configureLoadTest(config,values,defaults,{{...options,limits}}),
}};
const source = {json.dumps(config.model_dump(mode='json'))};
const defaults = {json.dumps(LoadTest().model_dump(mode='json'))};
const checks = {json.dumps(checks)};
function loadValues(config=source) {{
 const values=new FormData(),settings=form.loadTestSettings(config,defaults);
 values.set('load_rendered','1');
 for(const [kind,row] of Object.entries(settings.workloads)) {{
  if(row.enabled)values.set(`load_workloads_${{kind}}_enabled`,'on');
  for(const shape of row.selected||[])values.set(`load_workloads_${{kind}}_selected_${{shape}}`,'on');
  for(const shape of row.cases||[])values.set(`load_workloads_stress_cases_${{shape}}`,'on');
 }}
 for(const kind of ['concurrent','constant','poisson','bursts'])if(settings.patterns[kind].enabled)values.set(`load_patterns_${{kind}}_enabled`,'on');
 return values;
}}
"""
    result = subprocess.run(
        [node, "--input-type=module", "-"], input=prelude + script,
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


def test_setup_preset_inference_preserves_imported_selection_and_ceiling():
    run_setup_script(r"""
const before=structuredClone(source);assert.equal(setup.inferSetupPreset(source),'answers');assert.deepEqual(source,before);
for(const [checks,preset] of [[['access','load'],'concurrent'],[['access','correctness','load'],'arrival'],[['access','json'],'custom'],[['access','access','correctness'],'custom']]){
 source.checks=checks;source.concurrency=4;source.test_options={access:{request_timeout_seconds:12}};
 const imported=structuredClone(source);assert.equal(setup.inferSetupPreset(source),preset);assert.deepEqual(source,imported);
}
""")


def test_explicit_setup_presets_preserve_dormant_data_and_safety_limits():
    run_setup_script(r"""
source.checks=['json','tools','correctness'];source.structured_json=source.tool_calling=true;source.concurrency=16;
source.request_source='request_pack';source.request_pack={version:1,cases:[{fixture_id:'one',messages:[{role:'user',content:'literal'}],scorer:'exact',expected:'answer'}]};
source.request_options={temperature:0.4,metadata:{team:'qa'}};source.targets[0].headers_env={'X-Service':'SERVICE_HEADER'};source.test_options={correctness:{samples:9},recovery:{sustained_seconds:2}};
source.load_test=structuredClone(defaults);source.load_test.profile.long.output_tokens=1024;
const before=structuredClone(source);
for(const [preset,selection] of [['answers',['access','correctness']],['arrival',['access','correctness','load']],['concurrent',['access','load']],['custom',source.checks]]){
 const next=setup.applySetupPreset(source,preset,defaults);assert.deepEqual(next.checks,selection);
 for(const key of ['targets','request_source','request_pack','request_options','test_options','load_test','concurrency','max_requests','max_output_tokens'])assert.deepEqual(next[key],source[key]);
 assert.equal(next.structured_json,selection.includes('json'));assert.equal(next.tool_calling,selection.includes('tools'));
}assert.deepEqual(source,before);
""")


def test_setup_load_derives_only_basic_concurrency_without_raising_budgets():
    run_setup_script(r"""
source.concurrency=16;const preset=setup.applySetupPreset(source,'concurrent',defaults),values=loadValues(preset);
values.set('load_patterns_concurrent_levels','1, 4, 8');values.set('load_schedule_duration_seconds','10');
const configured=setup.configureLoadTest(preset,values,defaults),before=structuredClone(configured);
assert.deepEqual(setup.configureSetupLoad(configured,'concurrent'),before);assert.deepEqual(configured.load_test.patterns.concurrent.levels,[1,4,8]);
assert.equal(configured.concurrency,16);assert.equal(configured.max_requests,100);assert.equal(configured.max_output_tokens,128);
""")


def test_setup_load_keeps_invalid_drafts_and_rejects_them_when_running():
    run_setup_script(r"""
const preset=setup.applySetupPreset(source,'concurrent',defaults);
for(const raw of ['', 'oops', '1, nope', '1, 1', '0',String(limits['load_test.patterns.concurrent.levels'].items.maximum+1),'1.5','Infinity']){
 const values=loadValues(preset);values.set('load_patterns_concurrent_levels',raw);
 const before=structuredClone(preset),draft=setup.configureLoadTest(preset,values,defaults,{allowInvalid:true});
 assert.throws(()=>setup.configureLoadTest(preset,values,defaults),/positive, increasing whole-number levels/);
 assert.deepEqual(preset,before);assert.equal(draft.max_requests,preset.max_requests);assert.deepEqual(draft.checks,preset.checks);
}
""")


def test_load_profile_is_editable_and_suite_output_cap_is_a_default():
    run_setup_script(r"""
source.checks=['load'];source.load_test=structuredClone(defaults);source.load_test.patterns.poisson.seed=57;
const values=loadValues();for(const [size,input,output] of [['short',100,12],['medium',600,36],['long',1200,256]]){values.set(`load_profile_${size}_input_tokens`,String(input));values.set(`load_profile_${size}_output_tokens`,String(output));}
const before=structuredClone(source),configured=setup.configureLoadTest(source,values,defaults);assert.deepEqual(source,before);
assert.deepEqual(configured.load_test.profile,{short:{input_tokens:100,output_tokens:12},medium:{input_tokens:600,output_tokens:36},long:{input_tokens:1200,output_tokens:256}});
assert.equal(configured.load_test.patterns.poisson.seed,57);assert.equal(configured.max_output_tokens,128);assert.equal(configured.max_requests,100);
for(const size of ['short','medium','long'])for(const kind of ['input_tokens','output_tokens']){
 const maximum=limits[`load_test.profile.${size}.${kind}`].maximum;
 for(const raw of ['', '0','1.5','oops','Infinity',String(maximum+1)]){
  const invalid=loadValues();invalid.set(`load_profile_${size}_${kind}`,raw);
  assert.throws(()=>setup.configureLoadTest(source,invalid,defaults),/Token sizes/);
  const draft=setup.configureLoadTest(source,invalid,defaults,{allowInvalid:true});
  assert.equal(draft.load_test.profile[size][kind],raw===''?'':Number.isFinite(Number(raw))?Number(raw):raw);
 }
}
""")


def test_unrendered_load_fields_preserve_settings_without_activating_check():
    run_setup_script(r"""
source.load_test=structuredClone(defaults);source.load_test.patterns.poisson.seed=9;
const values=new FormData();values.set('load_patterns_concurrent_levels','1, 2');assert.deepEqual(setup.configureLoadTest(source,values,defaults),source);
const rendered=loadValues();rendered.set('load_profile_long_output_tokens','1024');const inactive=setup.configureLoadTest(source,rendered,defaults);
assert.equal(inactive.load_test.profile.long.output_tokens,1024);assert.deepEqual(inactive.checks,['access','correctness']);assert.equal(inactive.load_test.patterns.poisson.seed,9);
""")


def test_setup_preset_preserves_profile_above_suite_output_default():
    run_setup_script(r"""
source.max_output_tokens=32;source.load_test=structuredClone(defaults);source.load_test.profile.long.output_tokens=1024;
for(const mode of ['answers','arrival','custom','concurrent']){const configured=setup.applySetupPreset(source,mode,defaults);assert.deepEqual(configured.load_test,source.load_test);assert.equal(configured.max_output_tokens,32);assert.equal(configured.max_requests,100);}
""")


def test_load_types_and_criteria_roundtrip_checkbox_state_without_generic_overrides():
    run_setup_script(r"""
source.checks=['load'];source.load_test=structuredClone(defaults);const values=loadValues();
for(const key of [...values.keys()])if(key.startsWith('load_workloads_mixed_selected_')||key.startsWith('load_workloads_stress_cases_'))values.delete(key);
for(const kind of ['combinations','mixed','interference','stress'])values.set(`load_workloads_${kind}_enabled`,'on');
for(const shape of ['short_short','long_short','short_long'])values.set(`load_workloads_mixed_selected_${shape}`,'on');
values.set('load_workloads_mixed_share_mode','custom');for(const [shape,share]of Object.entries({short_short:40,long_short:20,short_long:40}))values.set(`load_workloads_mixed_shares_${shape}`,String(share));
values.set('load_patterns_concurrent_levels','2, 4');values.set('load_workloads_stress_cases_short_long','on');values.set('load_workloads_stress_min_output_percent','80');values.set('load_workloads_interference_trigger','after_five_chunks');values.set('load_workloads_interference_max_slowdown','1.5');values.set('load_pass_criteria_max_first_answer_ms','');values.set('load_pass_criteria_max_completion_ms','1200');
const configured=setup.configureLoadTest(source,values,defaults);assert.deepEqual(configured.load_test.workloads.mixed.selected,['short_short','short_long','long_short']);assert.equal(configured.load_test.workloads.interference.trigger,'after_five_chunks');assert.equal(configured.load_test.workloads.interference.max_slowdown,1.5);assert.equal(configured.load_test.pass_criteria.max_first_answer_ms,null);assert.equal(configured.load_test.pass_criteria.max_completion_ms,1200);
assert.deepEqual(configured.load_test.workloads.stress.cases,['short_long']);
assert.deepEqual(Object.fromEntries(configured.load_test.workloads.mixed.selected.map(shape=>[shape,configured.load_test.workloads.mixed.shares[shape]])),{short_short:40,short_long:40,long_short:20});
const testValues=new FormData();testValues.append('check','load');testValues.set('test_mode_load','custom');testValues.set('test_load_samples','17');assert.deepEqual(checks.find(check=>check.id==='load').option_fields,[]);assert.equal(setup.configureTests(configured,testValues,checks,{}).test_options.load,undefined);
""")


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
source.load_test = structuredClone(defaults);source.load_test.patterns.concurrent.levels=[1,4,8];source.load_test.schedule.duration_seconds=5;
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
configured = setup.configureLoadTest(configured,values,defaults);
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
  assert.deepEqual(switched.load_test,source.load_test);
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
  generation:{limits:{latency_ms:1200,stream_gap_ms:null}},
  recovery:{sustained_seconds:2},
};
source.load_test = structuredClone(defaults);source.load_test.patterns.concurrent.levels=[1,4,8];
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
assert.equal(setup.limitValue(configured,checks.find(check => check.id === 'generation'),'stream_gap_ms'),null);
console.log(JSON.stringify(configured));
""")
    configured = RunConfig.model_validate(result)
    assert configured.effective_check("correctness")["concurrency"] == 8
    assert configured.effective_check("correctness")["samples"] == 6
    assert configured.effective_check("correctness")["limits"]["min_correctness"] == 0.95
    assert configured.effective_check("generation")["limits"]["latency_ms"] == 1200
    assert configured.effective_check("generation")["limits"]["stream_gap_ms"] is None


def test_owned_override_preservation_keeps_reset_blank_and_sparse_semantics():
    run_setup_script("""
const values = new FormData();
for (const id of source.checks) values.append('check',id);
source.concurrency = 8;
source.limits.latency_ms = 1200;
source.test_options = {
  correctness:{samples:source.samples,concurrency:8},
  generation:{limits:{latency_ms:1200,stream_gap_ms:null}},
  recovery:{sustained_seconds:2},
};
for (const check of checks) {
  values.set(`test_mode_${check.id}`,source.test_options[check.id] ? 'custom' : 'default');
  for (const name of check.option_fields) values.set(`test_${check.id}_${name}`,setup.optionValue(source,check,name) ?? '');
  for (const name of check.limit_fields) values.set(`test_${check.id}_limit_${name}`,setup.limitValue(source,check,name) ?? '');
}
values.set('traffic_mode','default');
values.set('test_correctness_samples','');
values.set('test_mode_generation','default');
values.set('test_mode_json','custom');
const configured = setup.configureTests(source,values,checks,source.traffic);
assert.deepEqual(configured.test_options.correctness,{concurrency:8});
assert.equal(configured.test_options.generation,undefined);
assert.equal(configured.test_options.json,undefined);
assert.deepEqual(configured.test_options.recovery,{sustained_seconds:2});
values.set('test_mode_correctness','default');
assert.equal(setup.configureTests(configured,values,checks,source.traffic).test_options.correctness,undefined);
""")


def test_load_validation_preserves_invalid_drafts_for_server_field_errors():
    run_setup_script(r"""
source.checks=['load'];source.load_test=structuredClone(defaults);
for(const [field,raw,message]of [['load_workloads_interference_max_slowdown','1',/greater than 1×/],['load_pass_criteria_min_valid_responses','0',/whole number of at least 1/],['load_profile_short_input_tokens','0',/positive whole numbers/]]){
 const values=loadValues();values.set('load_workloads_interference_enabled','on');values.set(field,raw);const before=structuredClone(source);assert.throws(()=>setup.configureLoadTest(source,values,defaults),message);const draft=setup.configureLoadTest(source,values,defaults,{allowInvalid:true});assert.deepEqual(draft.checks,source.checks);assert.equal(draft.max_requests,source.max_requests);assert.deepEqual(source,before);
}
const values=loadValues();values.set('load_patterns_concurrent_levels','8, 1, 4');assert.throws(()=>setup.configureLoadTest(source,values,defaults),/increasing/);assert.deepEqual(setup.configureLoadTest(source,values,defaults,{allowInvalid:true}).load_test.patterns.concurrent.levels,[8,1,4]);
""")
