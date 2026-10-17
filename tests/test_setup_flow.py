"""Exercise setup presets through the browser's real configuration module."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from giraffe.models import Buckets, CHECK_LIMIT_FIELDS, CHECK_NAMES, CHECK_OPTION_FIELDS, RunConfig


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
        {"id": name, "option_fields": sorted(CHECK_OPTION_FIELDS[name]),
         "limit_fields": sorted(CHECK_LIMIT_FIELDS[name]), "defaults": config.effective_check(name)}
        for name in CHECK_NAMES
    ]
    prelude = f"""
import assert from 'node:assert/strict';
import * as setup from {json.dumps(MODULE)};
const source = {json.dumps(config.model_dump(mode='json'))};
const defaults = {json.dumps(Buckets().model_dump(mode='json'))};
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
source.checks = ['buckets', 'access'];
source.buckets = {...defaults, levels:[1, 4, 8]};
source.concurrency = 8;
assert.equal(setup.inferSetupPreset(source), 'concurrent');
source.concurrency = 16;
const imported = structuredClone(source);
assert.equal(setup.inferSetupPreset(source), 'custom');
assert.deepEqual(source, imported);
source.concurrency = 4;
assert.equal(setup.inferSetupPreset(source), 'custom');
source.concurrency = 8;
source.test_options = {correctness:{samples:9}};
assert.equal(setup.inferSetupPreset(source), 'concurrent');
source.test_options.access = {request_timeout_seconds:12};
assert.equal(setup.inferSetupPreset(source), 'custom');
source.test_options = {};
for (const levels of [[], ['8'], [1, 1], [0], [257], null]) {
  source.buckets.levels = levels;
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
assert.deepEqual(concurrent.checks, ['access','buckets']);
assert.equal(concurrent.structured_json, false);
assert.equal(concurrent.metrics, false);
assert.equal(concurrent.tool_calling, false);
assert.equal(concurrent.buckets.samples_per_bucket, 7);
assert.equal(concurrent.concurrency, 2);
assert.equal(concurrent.max_requests, 100);
assert.equal(concurrent.max_output_tokens, 128);
assert.deepEqual(defaults.levels,[1,2]);
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
source.buckets = {...defaults,levels:[1,4,8],samples_per_bucket:3,input_chars:{short:25,medium:50,long:100},output_tokens:{short:5,medium:20,long:40},mixed_pairs:7,heavy_weights:{long_input:2,long_output:3}};
const existing = setup.applySetupPreset(source,'concurrent',defaults);
assert.equal(existing.concurrency,8);
assert.deepEqual(existing.buckets,source.buckets);
assert.equal(existing.buckets.samples_per_bucket,3);
source.buckets = null;
source.limits.min_samples = 5;
assert.equal(setup.applySetupPreset(source,'concurrent',defaults).buckets.samples_per_bucket,5);
""")


def test_setup_load_derives_only_basic_concurrency_without_raising_budgets():
    result = run_setup_script("""
assert.equal(typeof setup.configureSetupLoad, 'function');
const preset = setup.applySetupPreset(source,'concurrent',defaults);
const values = new FormData();
values.set('buckets_levels','1, 4 8');
values.set('buckets_samples_per_bucket','5');
values.set('buckets_window_seconds','30');
const buckets = setup.configureBuckets(preset,values,defaults);
const before = structuredClone(buckets);
const configured = setup.configureSetupLoad(buckets,'concurrent');
assert.deepEqual(buckets,before);
assert.deepEqual(configured.buckets.levels,[1,4,8]);
assert.equal(configured.concurrency,8);
assert.equal(configured.max_requests,100);
assert.equal(configured.max_output_tokens,128);
for (const mode of ['answers','arrival','custom']) {
  const imported = {...configured,concurrency:16};
  assert.deepEqual(setup.configureSetupLoad(imported,mode),imported);
}
console.log(JSON.stringify(configured));
""")
    configured = RunConfig.model_validate(result)
    assert configured.concurrency == 8
    assert configured.max_requests == 100


def test_setup_load_keeps_invalid_drafts_and_rejects_them_when_running():
    run_setup_script("""
const preset = setup.applySetupPreset(source,'concurrent',defaults);
for (const raw of ['', 'oops', '1, nope', '1, 1', '0', '257', '1.5', 'Infinity']) {
  const values = new FormData();
  values.set('buckets_levels',raw);
  values.set('buckets_samples_per_bucket','5');
  values.set('buckets_window_seconds','30');
  const draft = setup.configureBuckets(preset,values,defaults,{allowInvalid:true});
  const before = structuredClone(draft);
  assert.deepEqual(setup.configureSetupLoad(draft,'concurrent',{allowInvalid:true}),before);
  assert.deepEqual(draft,before);
  assert.throws(()=>setup.configureSetupLoad(draft,'concurrent'),/Concurrency levels/);
  assert.deepEqual(setup.configureSetupLoad(draft,'custom'),before);
}
""")


def test_bucket_sizes_are_editable_and_keep_explicit_output_safety_limit():
    result = run_setup_script("""
source.checks = ['access','buckets'];
source.concurrency = 8;
source.buckets = {...defaults,levels:[1,4,8],mixed_pairs:7,heavy_weights:{long_input:2,long_output:3}};
const values = new FormData();
for (const [name,value] of Object.entries({
  buckets_input_short:100,buckets_input_medium:600,buckets_input_long:1200,
  buckets_output_short:12,buckets_output_medium:36,buckets_output_long:120,
})) values.set(name,String(value));
const before = structuredClone(source);
const configured = setup.configureBuckets(source,values,defaults);
assert.deepEqual(source,before);
assert.deepEqual(configured.buckets.input_chars,{short:100,medium:600,long:1200});
assert.deepEqual(configured.buckets.output_tokens,{short:12,medium:36,long:120});
assert.deepEqual(configured.buckets.levels,[1,4,8]);
assert.equal(configured.buckets.samples_per_bucket,defaults.samples_per_bucket);
assert.equal(configured.buckets.window_seconds,defaults.window_seconds);
assert.equal(configured.buckets.mixed_pairs,7);
assert.deepEqual(configured.buckets.heavy_weights,source.buckets.heavy_weights);
assert.equal(configured.max_requests,100);
assert.equal(configured.max_output_tokens,128);
values.set('buckets_output_long','256');
assert.throws(()=>setup.configureBuckets(source,values,defaults),/answer cap must be 128 tokens or less/i);
const draft = setup.configureBuckets(source,values,defaults,{allowInvalid:true});
assert.equal(draft.buckets.output_tokens.long,256);
assert.equal(draft.max_output_tokens,128);
const inactive = {...source,checks:['access','correctness']};
assert.equal(setup.configureBuckets(inactive,values,defaults).buckets.output_tokens.long,256);
for (const name of ['buckets_input_short','buckets_input_medium','buckets_input_long',
                    'buckets_output_short','buckets_output_medium','buckets_output_long']) {
  for (const raw of ['', '0', '65537', '1.5', 'oops', 'Infinity']) {
    const invalid = new FormData();
    invalid.set(name,raw);
    assert.throws(()=>setup.configureBuckets(source,invalid,defaults),/whole number.*1.*65536/);
    const kept = setup.configureBuckets(source,invalid,defaults,{allowInvalid:true});
    const [group,size] = name.replace('buckets_','').split('_');
    const expected = raw === '' ? null : Number.isFinite(Number(raw)) ? Number(raw) : raw;
    assert.equal(kept.buckets[group === 'input' ? 'input_chars' : 'output_tokens'][size],expected);
  }
}
console.log(JSON.stringify(configured));
""")
    configured = RunConfig.model_validate(result)
    assert configured.buckets.input_chars["long"] == 1200
    assert configured.buckets.output_tokens["long"] == 120


def test_legacy_bucket_forms_preserve_sizes_and_do_not_activate_workloads():
    run_setup_script("""
source.buckets = {...defaults,input_chars:{short:20,medium:40,long:80},output_tokens:{short:10,medium:20,long:30},mixed_pairs:9};
const values = new FormData();
values.set('buckets_levels','1, 2');
const result = setup.configureBuckets(source,values,defaults);
assert.deepEqual(result.buckets,source.buckets);
assert.deepEqual(setup.configureBuckets(source,new FormData(),defaults),source);
source.buckets = null;
values.set('buckets_samples_per_bucket',String(defaults.samples_per_bucket));
values.set('buckets_window_seconds',String(defaults.window_seconds));
for (const size of ['short','medium','long']) {
  values.set(`buckets_input_${size}`,String(defaults.input_chars[size]));
  values.set(`buckets_output_${size}`,String(defaults.output_tokens[size]));
}
const inactive = setup.configureBuckets(source,values,defaults);
assert.equal(inactive.buckets,null);
assert.deepEqual(inactive.checks,['access','correctness']);
""")


def test_new_concurrent_defaults_fit_existing_output_limit_without_rewriting_profiles():
    result = run_setup_script("""
source.max_output_tokens = 32;
const configured = setup.applySetupPreset(source,'concurrent',defaults);
assert.deepEqual(configured.buckets.output_tokens,{short:16,medium:32,long:32});
assert.equal(configured.max_output_tokens,32);
assert.equal(configured.max_requests,100);
assert.deepEqual(defaults.output_tokens,{short:16,medium:48,long:96});
source.buckets = structuredClone(defaults);
const preserved = setup.applySetupPreset(source,'concurrent',defaults);
assert.deepEqual(preserved.buckets,defaults);
assert.equal(preserved.max_output_tokens,32);
console.log(JSON.stringify(configured));
""")
    configured = RunConfig.model_validate(result)
    assert configured.max_output_tokens == 32


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
source.buckets = {...defaults,levels:[1,4,8],samples_per_bucket:5,input_chars:{short:100,medium:600,long:1200},output_tokens:{short:12,medium:36,long:120},mixed_pairs:7,heavy_weights:{long_input:2,long_output:3}};
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
values.set('buckets_levels','1, 4, 8');
values.set('judge_count','1');
for (const key of ['name','url','model','api_key_env']) values.set(`judge_0_${key}`,preset.judging.judges[0][key]);
for (const key of ['concurrency','request_timeout_seconds','max_duration_seconds','max_output_tokens']) {
  values.set(`judging_${key}`,String(preset.judging[key]));
}
let configured = setup.configureTests(preset,values,checks,preset.traffic);
configured = setup.configureBuckets(configured,values,defaults);
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
  assert.deepEqual(switched.buckets,source.buckets);
  assert.equal(switched.concurrency,8);
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
source.buckets = {...defaults,levels:[1,4,8],samples_per_bucket:5};
const before = structuredClone(source);
const concurrent = setup.applySetupPreset(source,'concurrent',defaults);
assert.equal(concurrent.concurrency,8);
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
