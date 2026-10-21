"""The form preserves sparse defaults and writes only check-owned settings."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from giraffe.capabilities import capabilities
from giraffe.models import RunConfig


def run_form(body, config=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise form serialization")
    module = (Path(__file__).parents[1] / "src/giraffe/static/form-config.mjs").as_uri()
    source = (config or RunConfig(targets=[{"name": "local", "url": "http://localhost/v1", "model": "test"}])).model_dump(mode="json")
    script = f"""
import assert from 'node:assert/strict';
import * as form from {json.dumps(module)};
const source={json.dumps(source)}, caps={json.dumps(capabilities())};
{body}
"""
    result = subprocess.run([node, "--input-type=module", "-"], input=script,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_optional_limits_null_roundtrip_and_time_units():
    run_form("""
const values=new FormData();
for(const key of ['max_requests','max_duration_seconds','concurrency','max_memory_mb'])values.set(key+'_enabled','off');
let result=form.configureRunLimits(source,values);
assert.equal(result.max_requests,null);assert.equal(result.max_duration_seconds,null);
assert.equal(result.concurrency,null);assert.equal(result.max_memory_mb,0);
assert.deepEqual(form.configureRunLimits(result,values),result);
values.set('max_duration_seconds_enabled','on');values.set('max_duration_seconds','2.5');values.set('max_duration_unit','min');
values.set('max_requests_enabled','on');values.set('max_requests','640');
values.set('concurrency_enabled','on');values.set('concurrency','40');
result=form.configureRunLimits(result,values);
assert.equal(result.max_duration_seconds,150);assert.equal(result.max_requests,640);assert.equal(result.concurrency,40);
values.set('concurrency','');
assert.throws(()=>form.configureRunLimits(result,values),error=>error.errors[0].fields[0]==='concurrency');
assert.equal(form.configureRunLimits(result,values,{allowInvalid:true}).concurrency,'');
assert.equal(source.max_requests,null);
""")


def test_all_check_options_and_pass_rules_roundtrip_without_global_clamps():
    run_form("""
const values=new FormData();for(const check of caps.checks){values.append('check',check.id);values.set('test_mode_'+check.id,'default');}
for(const id of ['correctness','sessions','consistency','tools','arrivals','prefix','concurrency']){
 const check=caps.checks.find(check=>check.id===id);values.set('test_mode_'+id,'custom');
 for(const name of check.option_fields)values.set('test_'+id+'_'+name,form.optionValue(source,check,name)??'');
 for(const name of check.limit_fields)values.set('test_'+id+'_limit_'+name,form.limitValue(source,check,name)??'');
}
values.set('test_correctness_concurrency','64');values.set('test_correctness_max_output_tokens','1024');
values.set('test_correctness_request_timeout_seconds','90');values.set('test_concurrency_request_timeout_seconds','91');
for(const id of ['sessions','consistency','tools','arrivals','prefix'])values.set('test_'+id+'_limit_latency_ms','4500');
const result=form.configureTests(source,values,caps.checks,caps.defaults.traffic);
assert.equal(result.test_options.correctness.concurrency,64);
assert.equal(result.test_options.correctness.max_output_tokens,1024);
assert.equal(result.test_options.correctness.request_timeout_seconds,90);
assert.equal(result.test_options.concurrency.request_timeout_seconds,91);
for(const id of ['sessions','consistency','tools','arrivals','prefix'])assert.equal(result.test_options[id].limits.latency_ms,4500);
assert.equal(result.max_output_tokens,source.max_output_tokens);assert.equal(result.request_timeout_seconds,source.request_timeout_seconds);
assert.deepEqual(form.configureTests(result,values,caps.checks,caps.defaults.traffic),result);
""")


def test_request_pack_correctness_request_settings_readonly_but_criteria_editable():
    run_form("""
source.request_source='request_pack';source.checks=['correctness'];
const values=new FormData();values.append('check','correctness');values.set('test_mode_correctness','custom');
const check=caps.checks.find(check=>check.id==='correctness');
for(const name of check.option_fields)values.set('test_correctness_'+name,'999');
values.set('test_correctness_limit_min_correctness','.5');
const result=form.configureTests(source,values,caps.checks,caps.defaults.traffic);
assert.deepEqual(result.test_options.correctness,{limits:{min_correctness:.5}});
// Invalid saved overrides remain visible to backend validation, never silently rewritten.
source.test_options.correctness={max_output_tokens:64};
assert.deepEqual(form.configureTests(source,values,caps.checks,caps.defaults.traffic).test_options.correctness,{max_output_tokens:64,limits:{min_correctness:.5}});
""")


def test_effective_inherited_defaults_match_backend():
    config = RunConfig(targets=[{"name": name, "url": "http://localhost/v1", "model": "test"}
                                for name in ("one", "two")],
                       checks=["correctness", "sessions", "consistency"], overlap_models=True,
                       default_concurrency=40, concurrency=64, max_output_tokens=512)
    expected = {check: config.effective_check(check) for check in ("correctness", "sessions", "consistency")}
    run_form(f"""
const expected={json.dumps(expected)};
for(const id of Object.keys(expected)){{
 const check=caps.checks.find(check=>check.id===id);
 for(const name of check.option_fields)assert.equal(form.optionValue(source,check,name),expected[id][name],id+'.'+name);
}}
const profiles=form.effectiveProfileDefaults(source,caps.profile_defaults,'sessions');
assert.equal(profiles.max_tokens,32);assert.equal(profiles.concurrency,2);
assert.deepEqual(form.effectiveProfileDefaults({{...source,concurrency:1}},caps.profile_defaults,'consistency').levels,[1]);
""", config)


def test_endpoint_response_shared_values_match_what_runs_without_rewriting_drafts():
    config = RunConfig(targets=[{"name": "local", "url": "http://localhost/v1", "model": "test"}],
                       checks=["serving", "first_output"],
                       test_options={"serving": {"request_timeout_seconds": 60, "max_output_tokens": 512},
                                     "first_output": {"request_timeout_seconds": 90, "max_output_tokens": 1024}})
    run_form("""
const values=new FormData();for(const id of source.checks)values.append('check',id);
for(const id of source.checks){
 const check=caps.checks.find(check=>check.id===id);values.set('test_mode_'+id,'custom');
 assert.equal(form.optionValue(source,check,'request_timeout_seconds'),60);
 assert.equal(form.optionValue(source,check,'max_output_tokens'),512);
 for(const name of check.option_fields)values.set('test_'+id+'_'+name,form.optionValue(source,check,name)??'');
}
assert.deepEqual(form.configureTests(source,values,caps.checks,caps.defaults.traffic),source);
""", config)


def test_sparse_load_levels_and_own_caps_are_preserved():
    run_form("""
source.checks=['access','concurrency'];source.concurrency=2;
assert.equal(form.inferSetupPreset(source),'concurrent');
assert.deepEqual(form.configureSetupLoad(source,'concurrent',{limits:caps.limits}),source);
const values=new FormData();values.set('load_schedule_levels','1');
let result=form.configureConcurrentLoad(source,values,caps.concurrent_load.defaults,{limits:caps.limits});
assert.equal(Object.hasOwn(result.concurrent_load.schedule,'levels'),false);
values.set('load_profile_long_output_tokens','256');
result=form.configureConcurrentLoad(source,values,caps.concurrent_load.defaults,{limits:caps.limits});
assert.equal(result.concurrent_load.profile.long.output_tokens,256);
const maximum=caps.limits['concurrent_load.profile.long.output_tokens'].maximum;
values.set('load_profile_long_output_tokens',String(maximum+1));
assert.throws(()=>form.configureConcurrentLoad(source,values,caps.concurrent_load.defaults,{limits:caps.limits}),/Long output cap/);
""")


def test_tool_json_limits_come_from_capabilities_and_count_bytes():
    run_form("""
assert.deepEqual(form.parseLogprobSettings(JSON.stringify({extra:'x'.repeat(5000)})).extra.length,5000);
assert.throws(()=>form.parseToolFixtures('[{},{}]',{'tool_checks.fixtures':{maxItems:1}}),/at most 1/);
assert.throws(()=>form.parseToolFixtures('[{"text":"é"}]',{'tool_checks.bytes':{maximum:14}}),/bytes/);
assert.deepEqual(form.parseToolFixtures('[{}]',caps.limits),[{}]);
""")


def test_profile_edits_do_not_turn_inherited_parallelism_into_overrides():
    run_form("""
source.arrivals=null;source.sessions=null;source.consistency=null;source.default_concurrency=40;source.concurrency=2;
const values=new FormData();
for(const [key,name,value] of [['arrivals','requests','12'],['sessions','sessions','6'],['consistency','repetitions','7']]){
 values.set('profile_edits_'+key,JSON.stringify([name]));values.set('profile_'+key+'_'+name,value);
}
const result=form.configureProfiles(source,values,caps.profile_defaults,{limits:caps.limits});
assert.equal(result.arrivals.requests,12);assert.equal(Object.hasOwn(result.arrivals,'max_in_flight'),false);
assert.equal(result.sessions.sessions,6);assert.equal(Object.hasOwn(result.sessions,'concurrency'),false);
assert.equal(result.consistency.repetitions,7);assert.equal(Object.hasOwn(result.consistency,'levels'),false);
values.set('profile_edits_arrivals','["requests","max_in_flight"]');values.set('profile_arrivals_max_in_flight','64');
assert.equal(form.configureProfiles(source,values,caps.profile_defaults,{limits:caps.limits}).arrivals.max_in_flight,64);
""")


def test_editing_capacity_rates_preserves_displayed_inherited_in_flight():
    run_form("""
source.checks=['capacity'];source.default_concurrency=40;source.concurrency=64;source.overlap_models=true;
source.targets.push({...source.targets[0],name:'other'});source.traffic.max_in_flight=null;
const values=new FormData();values.append('check','capacity');values.set('traffic_mode','custom');
values.set('traffic_rates','25');values.set('traffic_max_in_flight','32');
let result=form.configureTests(source,values,caps.checks,caps.defaults.traffic);
assert.deepEqual(result.traffic.rates,[25]);assert.equal(result.traffic.max_in_flight,null);
delete source.traffic.max_in_flight;
result=form.configureTests(source,values,caps.checks,caps.defaults.traffic);
assert.equal(Object.hasOwn(result.traffic,'max_in_flight'),false);
values.set('traffic_max_in_flight','64');
assert.equal(form.configureTests(source,values,caps.checks,caps.defaults.traffic).traffic.max_in_flight,64);
source.traffic.max_in_flight=32;values.set('traffic_max_in_flight','32');
assert.equal(form.configureTests(source,values,caps.checks,caps.defaults.traffic).traffic.max_in_flight,32);
values.set('traffic_max_in_flight','');
assert.equal(form.configureTests(source,values,caps.checks,caps.defaults.traffic).traffic.max_in_flight,null);
""")
