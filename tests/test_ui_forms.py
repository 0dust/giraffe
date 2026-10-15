"""Exercise the browser's real serializer without a DOM or an extra JS framework."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from giraffe.models import CHECK_LIMIT_FIELDS, CHECK_NAMES, CHECK_OPTION_FIELDS, RunConfig


def test_mixed_session_form_roundtrip_preserves_editable_tasks_and_shared_concurrency():
    from giraffe.models import RunConfig, Sessions, session_workload_presets

    module = (Path(__file__).parents[1] / "src/giraffe/static/form-config.mjs").as_uri()
    source = {"targets": [{"name": "local", "url": "http://localhost:1", "model": "test"}],
              "concurrency": 3, "max_output_tokens": 128, "checks": ["sessions"]}
    defaults = Sessions().model_dump(mode="json")
    tasks = [item.model_dump(mode="json") for item in session_workload_presets()]
    script = f"""
import assert from 'node:assert/strict';
import {{configureSessions}} from {json.dumps(module)};
const source={json.dumps(source)},defaults={json.dumps(defaults)},tasks={json.dumps(tasks)};
const values=new FormData();
values.set('sessions_profile','performance');values.set('sessions_count','6');
values.set('sessions_delay','1.5');values.set('sessions_max_tokens','96');
values.set('session_workload_count','3');
tasks.forEach((task,i)=>{{
  values.set(`session_enabled_${{i}}`,i===2?'off':'on');
  values.set(`session_name_${{i}}`,task.name);values.set(`session_weight_${{i}}`,i===0?'2':'1');
  values.set(`session_cap_${{i}}`,i===0?'64':'');
  values.set(`session_turn_count_${{i}}`,task.turns.length);
  task.turns.forEach((turn,j)=>values.set(`session_turn_${{i}}_${{j}}`,turn));
}});
values.set('session_turn_0_1','Custom follow-up with <literal> characters.');
const result=configureSessions(source,values,defaults);
assert.equal(result.sessions.concurrency,3);assert.equal(result.sessions.workloads.length,2);
assert.deepEqual(result.sessions.modes,['live']);
assert.equal(result.sessions.workloads[0].turns[1],'Custom follow-up with <literal> characters.');
assert.equal(result.sessions.workloads[0].max_tokens,64);
assert.equal(source.sessions,undefined);
values.set('session_enabled_0','off');values.set('session_enabled_1','off');
assert.throws(()=>configureSessions(source,values,defaults),/Select at least/);
assert.equal(configureSessions(result,new FormData(),defaults).sessions.profile,'performance');
const legacy=new FormData();legacy.set('sessions_profile','correctness');
const bounded=configureSessions({{...source,concurrency:1,max_output_tokens:16}},legacy,defaults);
assert.equal(bounded.sessions.concurrency,1);assert.equal(bounded.sessions.max_tokens,16);
console.log(JSON.stringify(result));
"""
    completed = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script],
                               capture_output=True, text=True, check=True)
    normalized = RunConfig.model_validate(json.loads(completed.stdout))
    assert normalized.sessions.workloads[0].weight == 2
    assert normalized.sessions.delay_seconds == 1.5



@pytest.mark.parametrize("scenario", [
    "selection", "defaults", "nullable_limits", "traffic", "empty_selection", "round_trip",
    "optional_workloads", "request_pack", "cancellation",
])
def test_browser_configuration_behavior(scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute browser configuration behavior")
    config = RunConfig(targets=[{"name": "a", "url": "http://localhost:8000/v1", "model": "a"},
                                {"name": "b", "url": "http://localhost:8001/v1", "model": "b"}],
                       checks=["correctness", "capacity"], limits={"latency_ms": 1000},
                       arrivals={"requests_per_second": 3}, prefix={"repeats": 7},
                       buckets={"levels": [1]}, sessions={"sessions": 3},
                       consistency={"repetitions": 8},
                       test_options={"correctness": {"samples": 9},
                                     "recovery": {"sustained_seconds": 2}})
    inherited = config.model_copy(update={"test_options": {}})
    context = {"source": config.model_dump(mode="json"), "checks": [
        {"id": name, "option_fields": sorted(CHECK_OPTION_FIELDS[name]),
         "limit_fields": sorted(CHECK_LIMIT_FIELDS[name]),
         "defaults": inherited.effective_check(name)} for name in CHECK_NAMES]}
    module = (Path(__file__).parents[1] / "src/giraffe/static/form-config.mjs").as_uri()
    script = f"""
import assert from 'node:assert/strict';
import {{configureTests, optionValue, limitValue, sameConfig}} from {json.dumps(module)};
const {{source, checks}} = {json.dumps(context)};
const defaults=structuredClone(source.traffic);
function valuesFor(config) {{
  const values=new FormData();
  for(const id of config.checks) values.append('check',id);
  for(const check of checks) {{
    values.set(`test_mode_${{check.id}}`,config.test_options?.[check.id]?'custom':'default');
    for(const name of check.option_fields) values.set(`test_${{check.id}}_${{name}}`,optionValue(config,check,name) ?? '');
    for(const name of check.limit_fields) values.set(`test_${{check.id}}_limit_${{name}}`,limitValue(config,check,name) ?? '');
  }}
  values.set('traffic_mode',sameConfig(config.traffic,defaults)?'default':'custom');
  for(const [name,value] of Object.entries(config.traffic)) {{
    if(name==='mix')for(const [kind,weight] of Object.entries(value)) values.set(`traffic_mix_${{kind}}`,weight);
    else values.set(`traffic_${{name}}`,Array.isArray(value)?value.join(', '):value ?? '');
  }}
  return values;
}}
const values=valuesFor(source);
const build=()=>configureTests(source,values,checks,defaults);
const before=JSON.stringify(source);
switch ({json.dumps(scenario)}) {{
  case 'request_pack': {{
    source.request_source='request_pack';
    source.request_pack={{version:1,cases:[{{fixture_id:'one',messages:[{{role:'user',content:'secret literal <tag>'}}],weight:1,scorer:'exact',expected:'answer'}}]}};
    source.test_options.correctness.concurrency=4;
    values.set('test_correctness_samples','100');values.set('test_correctness_concurrency','100');
    values.set('traffic_mode','custom');values.set('traffic_mix_short','9');
    values.set('traffic_long_input_chars','999');values.set('traffic_rates','3, 6');
    let result=build();
    assert.deepEqual(result.request_pack,source.request_pack);
    assert.deepEqual(result.test_options.correctness,source.test_options.correctness);
    assert.deepEqual(result.traffic.mix,source.traffic.mix);
    assert.equal(result.traffic.long_input_chars,source.traffic.long_input_chars);
    assert.deepEqual(result.traffic.rates,[3,6]);
    values.delete('check');
    result=configureTests(result,values,checks,defaults,{{allowEmpty:true}});
    assert.deepEqual(result.checks,[]);assert.deepEqual(result.request_pack,source.request_pack);
    result.request_source='builtin';
    const roundtrip=configureTests(result,valuesFor(result),checks,defaults,{{allowEmpty:true}});
    assert.equal(roundtrip.request_source,'builtin');assert.deepEqual(roundtrip.request_pack,source.request_pack);
    break;
  }}
  case 'cancellation': {{
    values.delete('check');values.append('check','cancellation');
    values.set('test_mode_cancellation','custom');
    values.set('test_cancellation_backend_verification','required');
    values.set('test_cancellation_trace_file','/tmp/sglang traces.jsonl');
    values.set('test_cancellation_cleanup_seconds','1');
    const result=build();
    assert.equal(result.test_options.cancellation.trace_file,'/tmp/sglang traces.jsonl');
    assert.equal(result.test_options.cancellation.cleanup_seconds,1);
    assert.equal(result.test_options.cancellation.backend_verification,'required');
    const again=configureTests(result,valuesFor(result),checks,defaults);
    assert.deepEqual(again.test_options.cancellation,result.test_options.cancellation);
    break;
  }}
  case 'selection': {{
    values.delete('check');values.append('check','correctness');
    const result=build();
    assert.deepEqual(result.checks,['correctness']);
    assert.equal(result.structured_json,false);assert.equal(result.metrics,false);
    assert.deepEqual(result.test_options,source.test_options);
    assert.deepEqual(result.targets,source.targets);
    assert.equal(JSON.stringify(source),before);
    break;
  }}
  case 'defaults': {{
    values.set('test_mode_correctness','default');
    const result=build();
    assert.equal(result.test_options.correctness,undefined);
    assert.deepEqual(result.test_options.recovery,{{sustained_seconds:2}});
    assert.equal(result.samples,source.samples);
    break;
  }}
  case 'nullable_limits': {{
    values.set('test_mode_capacity','custom');
    values.set('test_capacity_limit_latency_ms','');
    values.set('test_capacity_limit_first_output_ms','500');
    const result=build();
    assert.deepEqual(result.test_options.capacity,{{limits:{{latency_ms:null,first_output_ms:500}}}});
    assert.equal(result.limits.latency_ms,1000);
    const again=configureTests(result,valuesFor(result),checks,defaults);
    assert.deepEqual(again.test_options.capacity,result.test_options.capacity);
    break;
  }}
  case 'traffic': {{
    values.set('traffic_mode','custom');values.set('traffic_rates','2, 5, 8');
    values.set('traffic_arrival','poisson');values.set('traffic_seed','17');
    values.set('traffic_max_in_flight','');
    let result=build();assert.deepEqual(result.traffic.rates,[2,5,8]);
    assert.equal(result.traffic.arrival,'poisson');assert.equal(result.traffic.seed,17);
    assert.equal(result.traffic.max_in_flight,null);
    values.delete('check');values.append('check','correctness');
    assert.deepEqual(build().traffic,result.traffic);
    values.set('traffic_mode','default');assert.deepEqual(build().traffic,defaults);
    values.set('traffic_mode','custom');values.set('traffic_rates','2, Infinity');
    assert.throws(build,/finite number/);
    break;
  }}
  case 'empty_selection': {{
    values.delete('check');assert.throws(build,/Select at least one test/);
    const preview=configureTests(source,values,checks,defaults,{{allowEmpty:true}});
    assert.deepEqual(preview.checks,[]);
    assert.deepEqual(preview.test_options,source.test_options);
    break;
  }}
  case 'optional_workloads': {{
    source.targets[0].deployment={{runtime:{{version:'custom-runtime'}},discovery:'vllm'}};
    source.targets[0].metrics_profile='vllm-v1';
    source.targets[0].metrics_api_key_env='METRICS_TOKEN';
    values.delete('check');values.append('check','prefix');values.append('check','tools');
    let result=build();
    assert.deepEqual(result.checks,['prefix','tools']);assert.equal(result.tool_calling,true);
    for(const key of ['arrivals','prefix','buckets','sessions','consistency'])assert.deepEqual(result[key],source[key]);
    assert.deepEqual(result.targets,source.targets);
    const next=valuesFor(result);next.delete('check');next.append('check','correctness');
    result=configureTests(result,next,checks,defaults);
    assert.deepEqual(result.checks,['correctness']);assert.equal(result.tool_calling,false);
    assert.deepEqual(result.prefix,source.prefix);assert.deepEqual(result.targets,source.targets);
    break;
  }}
  case 'round_trip': {{
    const first=build(),second=configureTests(first,valuesFor(first),checks,defaults);
    assert.deepEqual(first,second);
    assert(sameConfig({{a:1,b:{{c:2}}}},{{b:{{c:2}},a:1}}));
    assert(!sameConfig([1,2],[2,1]));
    break;
  }}
}}
"""
    result = subprocess.run([node, "--input-type=module", "-"], input=script,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_form_inheritance_and_capacity_comparison_rendering():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute browser configuration behavior")
    static = Path(__file__).parents[1] / "src/giraffe/static"
    script = rf"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {{optionDefault}} from {json.dumps((static / 'form-config.mjs').as_uri())};
const elements={{}};
for(const [name,value] of Object.entries({{
  context_limit:4096,concurrency:8,max_requests:160,max_duration_seconds:300,
  request_timeout_seconds:30,max_output_tokens:128,limit_latency_ms:2000,
  test_correctness_samples:9,test_correctness_concurrency:4,
  test_capacity_limit_latency_ms:1000,test_capacity_limit_first_output_ms:500,
}})) elements[name]={{value:String(value),dataset:{{inherited:'true'}}}};
elements.test_correctness_samples.dataset.inherited='false';
elements.test_capacity_limit_first_output_ms.dataset.inherited='false';
const form={{elements}};
const context={{structuredClone,optionDefault,elements,assert,
  document:{{querySelector:()=>form,addEventListener:()=>{{}}}},
  window:{{addEventListener:()=>{{}}}}}};
const source=fs.readFileSync({json.dumps(str(static / 'app.js'))},'utf8')
  .replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
vm.runInNewContext(source+`
state.config={{samples:6,concurrency:4,limits:{{latency_ms:1000,first_output_ms:null}}}};
state.bootstrap={{checks:[
  {{id:'correctness',option_fields:['samples','concurrency'],limit_fields:[],defaults:{{}}}},
  {{id:'capacity',option_fields:[],limit_fields:['latency_ms','first_output_ms'],defaults:{{}}}}
]}};
refreshInheritedFields('correctness',true);
refreshInheritedFields('capacity',true);
assert.equal(elements.test_correctness_samples.value,'9');
assert.equal(elements.test_correctness_concurrency.value,8);
assert.equal(elements.test_capacity_limit_latency_ms.value,2000);
assert.equal(elements.test_capacity_limit_first_output_ms.value,'500');
refreshInheritedFields('correctness');
assert.equal(elements.test_correctness_samples.value,6);
assert.equal(elements.test_correctness_samples.dataset.inherited,'true');
// A capacity-only comparison has stage evidence, not an empty metric table.
const target={{target:'local',status:'pass',comparisons:[],
  summary:'No material regression detected in 0 comparable metrics.',
  traffic_capacity:{{status:'pass',reason:'Both rates remained acceptable.',
    stages:[{{rate_rps:10,status:'pass'}},{{rate_rps:20,status:'pass'}}]}}}};
const report={{checks:[],baseline:{{run_id:'before',status:'pass',targets:[target]}}}};
state.comparisonFilter='all';
let markup=renderComparison(report);
assert.match(markup,/2 traffic stages compared/);
assert.doesNotMatch(markup,/0 recorded metric comparisons|0 comparable metrics|No metric rows|comparison-filters/);
target.comparisons.push({{check:'correctness',metric:'error_rate',status:'pass'}});
markup=renderComparison(report);
assert.match(markup,/comparison-filters/);
assert.match(markup,/1 recorded metric comparisons/);
assert(markup.includes('1 / 1 metrics shown'));
target.configuration_diff={{confounded:true,changed_fields:['runtime.version']}};
target.telemetry={{coverage:'partial'}};
markup=renderComparison(report);
assert.match(markup,/runtime.version|confounded experiment/);
assert.match(markup,/Serving telemetry and repeatability comparison/);
assert.match(markup,/Tested arrival capacity/);
state.detail={{id:'run-one'}};
const configMarkup=renderConfig({{manifest:{{config:{{}}}},observations:{{targets:{{local:{{deployment:{{runtime:'vllm'}},serving_telemetry:{{coverage:'partial'}}}}}}}}}});
assert.match(configMarkup,/Deployment and serving observations/);
assert.match(configMarkup,/reproduction.json/);assert.match(configMarkup,/config.json/);
`,context);
"""
    result = subprocess.run([node, "--input-type=module", "-"], input=script,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_compact_test_editors_preserve_drafts_and_reveal_invalid_fields():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute browser configuration behavior")
    static = Path(__file__).parents[1] / "src/giraffe/static"
    script = rf"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {{optionDefault, optionValue, limitValue, sameConfig}} from {json.dumps((static / 'form-config.mjs').as_uri())};
const listeners={{}}, elements={{}}, cards={{}};
const checks=[{{id:'correctness',title:'Known answers',description:'Check answers',option_fields:['samples'],limit_fields:[],defaults:{{samples:6}}}},
  {{id:'capacity',title:'Capacity',description:'Test traffic',option_fields:[],limit_fields:['latency_ms'],defaults:{{limits:{{latency_ms:null}}}}}}];
const traffic={{rates:[1,2,4],arrival:'steady',duration_seconds:5,mix:{{short:.6,long_input:.2,long_output:.2}}}};
for(const id of ['correctness','capacity']){{
  const fieldset={{disabled:true}}, selection={{checked:id==='capacity'}}, status={{textContent:''}}, draft={{hidden:true}};
  const trigger={{attributes:{{}},setAttribute(k,v){{this.attributes[k]=v;}},focus(){{this.focused=true;}}}};
  const editor={{hidden:true,querySelectorAll:()=>[fieldset]}};
  const card={{dataset:{{testCard:id}},fieldset,selection,status,draft,trigger,editor,
    classList:{{toggle(){{}}}},querySelector(q){{return {{'input[name="check"]':selection,
      '[data-setting-state]':status,'[data-test-editor]':editor,'[data-configure-test]':trigger,
      '.draft-notice':draft,'.traffic-profile-summary':{{}}}}[q];}}}};
  cards[id]=card;elements[`test_mode_${{id}}`]={{value:'default'}};
}}
elements.traffic_mode={{value:'default'}};
for(const [name,value] of Object.entries({{context_limit:4096,concurrency:8,max_requests:160,
  max_duration_seconds:300,request_timeout_seconds:30,max_output_tokens:128}}))elements[name]={{value:String(value)}};
function input(name,id,section,value){{
  const item={{name,value,dataset:{{inherited:'true'}},disabled:false,valid:true,
    closest(q){{if(q==='[data-test-card]')return cards[id];if(q==='fieldset')return cards[id].fieldset;
      if(q==='[data-test-settings]'&&section==='test')return {{dataset:{{testSettings:id}}}};
      if(q==='[data-traffic-settings]'&&section==='traffic')return {{}};
      return null;}},checkValidity(){{return this.valid;}},reportValidity(){{this.reported=true;}}}};
  elements[name]=item;return item;
}}
const samples=input('test_correctness_samples','correctness','test','6');
const latency=input('test_capacity_limit_latency_ms','capacity','test','');
const rates=input('traffic_rates','capacity','traffic','1, 2, 4');
const form={{elements,querySelectorAll:()=>[samples,latency,rates]}};
const document={{addEventListener:(kind,fn)=>listeners[kind]=fn,querySelector(q){{
  if(q==='#new-run-form')return form;
  const card=q.match(/^\[data-test-card="(.*?)"\]$/);if(card)return cards[card[1]];
  const trigger=q.match(/^\[data-configure-test="(.*?)"\]$/);if(trigger)return cards[trigger[1]].trigger;
}}}};
const source=fs.readFileSync({json.dumps(str(static / 'app.js'))},'utf8')
  .replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
const context={{structuredClone,optionDefault,optionValue,limitValue,sameConfig,
  document,window:{{addEventListener(){{}}}},assert,checks,traffic,cards,elements,form,samples,latency,rates,listeners}};
vm.runInNewContext(source+`
state.bootstrap={{checks,config:{{traffic}},option_schema:{{properties:{{samples:{{type:'integer',minimum:1}}}}}},
 limits_schema:{{properties:{{latency_ms:{{anyOf:[{{type:'number'}},{{type:'null'}}]}}}}}},
 traffic_schema:{{properties:{{rates:{{type:'array'}},arrival:{{enum:['steady','poisson']}}}},$defs:{{TrafficMix:{{properties:{{short:{{type:'number'}},long_input:{{type:'number'}},long_output:{{type:'number'}}}}}}}}}}}};
state.config={{checks:['capacity'],targets:[{{}}],samples:6,traffic,limits:{{latency_ms:null}},test_options:{{correctness:{{samples:9}}}}}};
syncConfigView=()=>{{}};
updateBounds=()=>{{}}; // The DOM mock exercises disclosure and input behavior, not report markup.
let markup=renderTestCard(checks[0],state.config);
assert.match(markup,/aria-expanded="false"/);
assert.match(markup,/data-test-editor="correctness" hidden/);
assert.match(markup,/type="hidden" name="test_mode_correctness" value="custom"/);
assert.doesNotMatch(markup,/<select name="test_mode_/);
markup=renderTestCard(checks[1],state.config);
assert(markup.indexOf('Traffic profile')<markup.indexOf('Acceptance settings'));
assert.doesNotMatch(markup,/<select name="traffic_mode"/);
const workload={{id:'prefix',title:'Shared prefix',description:'Cache evidence',option_fields:[],limit_fields:[],defaults:{{}}}};
markup=renderTestCard(workload,{{...state.config,prefix:{{repeats:7}}}});
assert.match(markup,/data-config-tab="json"/);
assert.match(markup,/JSON settings saved/);
assert.doesNotMatch(markup,/<textarea|data-reset-test=/);
syncTestControls();
assert.equal(cards.correctness.hidden,true);
assert.equal(cards.capacity.hidden,false);
state.showAvailableTests=true;syncTestControls();
assert.equal(cards.correctness.hidden,false);
assert.equal(cards.correctness.selection.checked,false);
state.showAvailableTests=false;syncTestControls();
assert.equal(cards.correctness.hidden,true);
assert.equal(cards.correctness.editor.hidden,true);
const before=JSON.stringify(state.config);
toggleTestEditor('correctness');
assert.equal(elements.test_mode_correctness.value,'default');
assert.equal(cards.correctness.selection.checked,false);
assert.equal(cards.correctness.fieldset.disabled,false);
assert.equal(cards.correctness.draft.hidden,false);
assert.equal(cards.correctness.trigger.attributes['aria-expanded'],'true');
assert.equal(JSON.stringify(state.config),before);
samples.value='9';editTestField(samples);
assert.equal(elements.test_mode_correctness.value,'custom');
assert.equal(samples.dataset.inherited,'false');
toggleTestEditor('capacity');
assert.equal(cards.correctness.editor.hidden,true);
assert.equal(cards.correctness.fieldset.disabled,true);
assert.equal(samples.value,'9');
assert.equal(cards.capacity.editor.hidden,false);
rates.value='2, 5';editTestField(rates);
assert.equal(elements.traffic_mode.value,'custom');
toggleTestEditor('capacity',true);
assert.equal(cards.capacity.trigger.focused,true);
assert.equal(cards.capacity.editor.hidden,true);
assert.equal(rates.value,'2, 5');
toggleTestEditor('correctness');
assert.equal(samples.value,'9');
listeners.click({{target:{{closest(q){{return q==='[data-reset-test]'?{{dataset:{{resetTest:'correctness'}}}}:null;}}}}}});
assert.equal(elements.test_mode_correctness.value,'default');
assert.equal(samples.value,6);
assert.equal(samples.dataset.inherited,'true');
assert.equal(elements.traffic_mode.value,'custom');
// Labels reflect serialized overrides, not sticky edit flags, even after closing/reopening.
const effective=structuredClone(state.config);effective.checks=['correctness','capacity'];
effective.test_options={{}};
assert.equal(testSettingsLabel(checks[0],effective),'Defaults');
assert.equal(testSettingsLabel(checks[1],effective),'Defaults');
effective.test_options.correctness={{samples:9}};
assert.equal(testSettingsLabel(checks[0],effective),'Custom');
effective.checks=['capacity'];assert.equal(testSettingsLabel(checks[0],effective),'Custom saved');
cards.correctness.status.textContent='Defaults';elements.test_mode_correctness.value='custom';
toggleTestEditor('correctness',true);toggleTestEditor('correctness');
assert.equal(cards.correctness.status.textContent,'Defaults');
// Invalid selected drafts are reopened before browser validation; inactive drafts do not block a run.
elements.test_mode_correctness.value='custom';samples.valid=false;
toggleTestEditor('correctness',true);
assert.equal(validateRunForm(form),true);
cards.correctness.selection.checked=true;
assert.equal(validateRunForm(form),false);
assert.equal(state.openTest,'correctness');
assert.equal(cards.correctness.fieldset.disabled,false);
assert.equal(samples.reported,true);
`,context);
"""
    result = subprocess.run([node, "--input-type=module", "-"], input=script,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_form_json_views_validate_one_snapshot_and_preserve_unapplied_drafts():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute browser configuration behavior")
    static = Path(__file__).parents[1] / "src/giraffe/static"
    script = rf"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const nodes={{}},listeners={{}},downloads=[];
for(const id of ['config-tab-form','config-tab-json','config-panel-form','config-panel-json',
  'config-draft-notice','config-editor','json-status','config-error','cli-fallback','cli-command','execution-settings']){{
  nodes['#'+id]={{value:'',hidden:false,classList:{{toggle(){{}}}},setAttribute(k,v){{this[k]=v;}},focus(){{this.focused=true;}},select(){{this.selected=true;}}}};
}}
const form={{elements:{{baseline_id:{{value:'before'}},mode:{{value:'normal'}},restart_target:{{value:'local'}},model:{{focus(){{this.focused=true;}}}}}}}};
nodes['#new-run-form']=form;
const document={{querySelector:q=>nodes[q],addEventListener:(kind,fn)=>listeners[kind]=fn,
  createElement:()=>({{click(){{this.clicked=true;}}}})}};
const context={{structuredClone,document,window:{{addEventListener(){{}}}},assert,nodes,form,listeners,
  Blob,URL:{{createObjectURL(blob){{downloads.push(blob);return 'blob:config';}},revokeObjectURL(){{}}}},
  navigator:{{clipboard:{{async writeText(){{throw new Error('denied');}}}}}},setTimeout(){{}},downloads}};
const source=fs.readFileSync({json.dumps(str(static / 'app.js'))},'utf8')
  .replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
await vm.runInNewContext(source+`
(async()=>{{
let current={{config:{{targets:[{{name:'local',model:'a'}}],checks:[],concurrency:null,restart_target:null}},baseline_id:'before',mode:'normal',restart_target:null}};
readForm=()=>structuredClone(current);toast=()=>{{}};
state.config=structuredClone(current.config);state.jsonDraft=null;state.jsonBase=null;
// Form -> JSON includes the latest incomplete draft and zero selection.
switchConfigView('json');
assert.equal(JSON.parse(state.jsonDraft).concurrency,null);
assert.equal(JSON.parse(state.jsonDraft).checks.length,0);
assert.equal(nodes['#config-tab-json']['aria-selected'],'true');
assert.equal(nodes['#config-tab-form'].tabIndex,-1);
const base=state.jsonBase;
state.jsonDraft='{{ invalid';state.configRevision++;
switchConfigView('form');assert.equal(nodes['#config-panel-form'].inert,true);
assert.equal(nodes['#execution-settings'].inert,true);
switchConfigView('json');assert.equal(state.jsonDraft,'{{ invalid');
assert.throws(assertAppliedConfig,/Apply or discard/);
let calls=0;api=async()=>{{calls++;return {{config:{{}}}};}};
const before=JSON.stringify(state.config);
await applyConfig();assert.equal(calls,0);assert.equal(JSON.stringify(state.config),before);
assert.equal(state.jsonDraft,'{{ invalid');assert.equal(nodes['#config-error'].hidden,false);
await exportConfig();await copyCli();
assert.equal(calls,0);assert.equal(downloads.length,0);
discardConfig();assert.equal(state.jsonDraft,base);assert.equal(nodes['#config-panel-form'].inert,false);
// Invalid imports never replace the form, the applied snapshot, or a retained draft.
await importConfig({{text:async()=>'bad json'}});
assert.equal(JSON.stringify(state.config),before);assert.equal(state.jsonDraft,base);
api=async()=>{{throw new Error('invalid config');}};
await importConfig({{text:async()=>'{{"checks":[]}}'}});
assert.equal(JSON.stringify(state.config),before);assert.equal(state.jsonDraft,base);
// Input arriving during async validation wins over an older Apply response.
let resolve;api=()=>new Promise(done=>resolve=done);
state.jsonDraft='{{"checks":["capacity"]}}';state.configRevision++;
const pending=applyConfig();
state.jsonDraft='{{"checks":["correctness"]}}';state.configRevision++;
resolve({{config:{{checks:['capacity']}}}});await pending;
assert.equal(JSON.stringify(state.config),before);
assert.equal(state.jsonDraft,'{{"checks":["correctness"]}}');
// Successful import makes an explicit restart intent visible.
discardConfig();let renders=0;
renderNew=()=>{{renders++;state.jsonDraft=JSON.stringify({{...state.config,restart_target:state.formRestartTarget}});state.jsonBase=state.jsonDraft;}};
api=async()=>({{config:{{targets:[{{name:'local',model:'b'}}],checks:['capacity'],restart_target:'local'}}}});
await importConfig({{text:async()=>'{{"restart_target":"local"}}'}});
assert.equal(renders,1);assert.equal(state.formMode,'cold-start');assert.equal(state.formRestartTarget,'local');
assert.equal(state.config.restart_target,null);
// Export and CLI use the current form snapshot, including shared baseline/execution settings.
current={{config:{{targets:[{{name:'local',model:'current'}}],checks:['capacity'],restart_target:null}},baseline_id:'before',mode:'cold-start',restart_target:'local'}};
const requests=[];api=async(path,options)=>{{const body=JSON.parse(options.body);requests.push({{path,body}});return path==='/api/config/cli'?{{command:'giraffe cold-start --config -'}}:{{config:body}};}};
await exportConfig();assert.equal(downloads.length,1);
assert.equal(requests[0].path,'/api/config/draft');
const exported=JSON.parse(await downloads[0].text());
assert.equal(exported.targets[0].model,'current');assert.equal(exported.restart_target,'local');
await copyCli();const cli=requests.find(r=>r.path==='/api/config/cli');
assert.equal(cli.body.config.targets[0].model,'current');assert.equal(cli.body.baseline_id,'before');
assert.equal(cli.body.mode,'cold-start');assert.equal(cli.body.restart_target,'local');
assert.equal(nodes['#cli-fallback'].hidden,false);assert.equal(nodes['#cli-command'].selected,true);
// Blank models remain editable and exportable; CLI copying explains what is missing.
current.config.targets[0].model='';current.config.checks=[];
state.jsonDraft=JSON.stringify(current.config);state.configRevision++;
await applyConfig();assert.equal(requests.at(-1).path,'/api/config/draft');
assert.equal(state.config.targets[0].model,'');assert.equal(state.config.checks.length,0);
await exportConfig();assert.equal(downloads.length,2);
assert.equal(JSON.parse(await downloads[1].text()).targets[0].model,'');
current.config.checks=['capacity'];state.configView='json';
const beforeCopy=requests.length;await copyCli();
assert.equal(requests.length,beforeCopy);assert.equal(state.configView,'form');
assert.equal(form.elements.model.focused,true);
assert.match(nodes['#config-error'].textContent,/Enter a model for target "local"/);
assert.equal(nodes['#cli-fallback'].hidden,true);
current.config.targets[0].model='ready';
current.config.targets.push({{name:'second',model:''}});
await copyCli();assert.equal(state.configView,'json');
assert.equal(JSON.parse(nodes['#config-editor'].value).targets[1].name,'second');
assert.equal(nodes['#config-editor'].focused,true);
assert.match(nodes['#config-error'].textContent,/target "second"/);
current.config.targets.pop();
// A stale validation response must not export a previous version of the form.
api=()=>new Promise(done=>resolve=done);
const snapshot=validatedSnapshot();state.configRevision++;
resolve({{config:current.config}});await assert.rejects(snapshot,/changed during validation/);
// New-run keyboard tabs are scoped and never mutate the result tab selection.
state.tab='baseline';state.configView='json';state.jsonDraft=state.jsonBase;
listeners.keydown({{key:'Home',preventDefault(){{}},target:{{closest(q){{return q==='[data-config-tab]'?{{}}:null;}}}}}});
assert.equal(state.configView,'form');assert.equal(state.tab,'baseline');
}})()
`,context);
"""
    result = subprocess.run([node, "--input-type=module", "-"], input=script,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_request_pack_import_preview_revision_guards_and_run_snapshot():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute browser request pack behavior")
    static = Path(__file__).parents[1] / "src/giraffe/static"
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const nodes={},listeners={};
for(const id of ['request-pack-preview','config-error','start-run','form-error','request-pack-file'])nodes['#'+id]={hidden:true,innerHTML:'',textContent:'',click(){this.clicked=true;}};
const form={elements:{request_source:{value:'builtin'}}};nodes['#new-run-form']=form;
const context={assert,structuredClone,nodes,form,fetch:async()=>({ok:false,status:404,json:async()=>({detail:'Not Found'})}),document:{querySelector:q=>nodes[q],addEventListener:(kind,fn)=>listeners[kind]=fn},window:{addEventListener(){}},location:{hash:'#new'}};
const source=fs.readFileSync(APP_PATH,'utf8').replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
await vm.runInNewContext(source+`
(async()=>{
const initial={request_source:'builtin',request_pack:null,targets:[{name:'local',model:''}],checks:[],traffic:{rates:[1]}};
let current=structuredClone(initial),renders=0,calls=0;
state.config=structuredClone(initial);state.jsonDraft=state.jsonBase='synced';
readForm=()=>({config:structuredClone(current),mode:'normal'});
captureFormDraft=()=>{state.config=structuredClone(current);};
renderNew=()=>{renders++;current=structuredClone(state.config);state.jsonDraft=state.jsonBase='synced';renderRequestPreview();};
toast=()=>{};
const pack={version:1,cases:[{fixture_id:'<case>',messages:[{role:'user',content:'private input'}],weight:1,scorer:'exact',expected:'<answer>'}]};
const preview={active:false,source:'request_pack',consumers:[],cases:[],stages:[],warnings:['<warning>'],quiet_requests:0};
// Stale backend configuration errors remain actionable; missing runs stay ordinary 404s.
await assert.rejects(api('/api/config/preview'),/server.*configuration.*Restart.*giraffe ui.*refresh/i);
await assert.rejects(api('/api/runs/missing'),error=>error.message==='Not Found'&&error.status===404);
// My dataset is a persistent empty choice, without launching a file picker.
changeRequestSource('request_pack');assert.equal(nodes['#request-pack-file'].clicked,undefined);
assert.equal(state.config.request_source,'request_pack');assert.equal(state.config.request_pack,null);
assert.equal(calls,0);
current={...current,request_pack:pack};state.config=current;
let resolve;api=()=>new Promise(done=>resolve=done);
// Request previews may resolve out of order: only current revision is accepted.
const first=refreshRequestPreview();const oldResolve=resolve;
current.traffic.rates=[9];configChanged();
assert.match(nodes['#request-pack-preview'].innerHTML,/Preview stale/);
const second=refreshRequestPreview();const newResolve=resolve;
newResolve({config:current,request_pack_preview:{...preview,quiet_requests:9}});await second;
oldResolve({config:initial,request_pack_preview:{...preview,quiet_requests:1}});await assert.rejects(first,/changed during preview/);
assert.equal(state.requestPreview.data.quiet_requests,9);assert.equal(state.requestPreview.status,'current');
api=async()=>{throw Error('preview unavailable');};await assert.rejects(refreshRequestPreview(),/preview unavailable/);
assert.equal(state.requestPreview.status,'error');assert.match(nodes['#request-pack-preview'].innerHTML,/current plan is unconfirmed/);
assert(!nodes['#request-pack-preview'].innerHTML.includes('Preview current'));
// Switching sources preserves cases; explicit remove clears them without selecting tests.
changeRequestSource('builtin');assert.equal(state.config.request_source,'builtin');assert.equal(state.config.request_pack.cases.length,1);
changeRequestSource('request_pack');assert.equal(state.config.request_source,'request_pack');assert.equal(state.config.checks.length,0);
changeRequestSource('builtin',{remove:true});assert.equal(state.config.request_pack,null);assert.equal(state.config.checks.length,0);
// Run uses the strict validated snapshot, then previews that same snapshot before sending.
current={...initial,targets:[{name:'local',model:'model'}],checks:['capacity'],request_source:'request_pack',request_pack:pack};state.config=current;
validateRunForm=()=>true;setActive=()=>{};const sent=[];
api=async(path,options)=>{const config=JSON.parse(options.body);sent.push({path,config});if(path==='/api/runs')return {id:'done'};return {config,request_pack_preview:preview};};
await startRun({preventDefault(){},target:form});
assert.equal(sent.map(x=>x.path).join(','),'/api/config/validate,/api/config/preview,/api/runs');
assert.equal(JSON.stringify(sent[1].config),JSON.stringify(sent[2].config.config));
// Edits while the pre-run preview is pending prevent all inference calls.
state.active=null;sent.length=0;api=async(path,options)=>{sent.push(path);if(path==='/api/config/preview')return new Promise(done=>resolve=done);return {config:JSON.parse(options.body)};};
const running=startRun({preventDefault(){},target:form});await Promise.resolve();await Promise.resolve();
configChanged();resolve({config:current,request_pack_preview:preview});await running;
assert(!sent.includes('/api/runs'));assert.match(nodes['#form-error'].textContent,/changed during preview/);
// Case evidence and assertions are escaped, including long operator-provided identifiers.
const rendered=renderPackResults({control_status:'fail',cases:[{case_id:'<unsafe>',quiet:{status:'fail',correct:0,scored:1,valid:1},stages:[{rate_rps:2,correct:0,scored:0,valid:0}]}]});
assert.match(rendered,/&lt;unsafe&gt;/);assert.match(rendered,/insufficient evidence/);assert.match(rendered,/Quiet control/);

// Service throughput and scored-subset goodput remain separate human-facing claims.
const stage={rate_rps:2,status:'inconclusive',valid_throughput_rps:1.8,scored_subset_correct_goodput_rps:.7,goodput_rps:.7,all_cases_scored:false};
const mixedPack={control_status:'inconclusive',all_cases_scored:false,cases:[]};
let stageMarkup=renderTrafficStages([stage],mixedPack);
assert.match(stageMarkup,/Valid throughput/);assert.match(stageMarkup,/Scored-subset correct goodput/);
assert(stageMarkup.includes('1.8 req/s'));assert(stageMarkup.includes('0.7 req/s'));
assert.doesNotMatch(stageMarkup,/Cohort goodput/);
const failedQuiet={control_status:'fail',all_cases_scored:true,cases:[]};
const capacity={id:'capacity',title:'Capacity',target:'local',status:'fail',required:true,metrics:{highest_acceptable_rate_rps:2,request_pack:failedQuiet,traffic_stages:[{...stage,status:'pass',all_cases_scored:true}]}};
assert(keyMetric(capacity).includes('2 requests/s loaded · quiet failed'));
const detail=renderCheck(capacity,{requests:[],manifest:{config:{}}});
assert.match(detail,/Highest acceptable tested loaded rate · quiet failed/);
assert.match(detail,/Loaded result · quiet failed/);
assert(!detail.includes('Highest accepted arrival rate'));assert(!detail.includes('requests/s accepted'));
assert(detail.includes('<th>Correct goodput</th>'));
assert.doesNotMatch(detail,/class="status pass"/);
const builtin=renderTrafficStages([{...stage,status:'pass'}]);
assert.match(builtin,/Cohort goodput/);assert.doesNotMatch(builtin,/Scored-subset correct goodput/);
// Preview has one warming explanation and names only the pack subtotal.
state.config=current;state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,config:current,data:{...preview,active:true,consumers:['capacity'],planned_requests_per_target:12,warnings:['Repeated, warmed workload: quiet controls and traffic repeat the same requests and may benefit caches.','Other selected checks consume additional shared request and time budget.']}};
renderRequestPreview();const previewMarkup=nodes['#request-pack-preview'].innerHTML;
assert.match(previewMarkup,/Pack requests per target: 12/);
assert.doesNotMatch(previewMarkup,/Known selected-check subtotal/);
assert.equal((previewMarkup.match(/warmed workload/g)||[]).length,1);
assert.match(previewMarkup,/Other selected checks/);

})()
`,context);
""".replace("APP_PATH", json.dumps(str(static / "app.js")))
    result = subprocess.run([node, "--input-type=module", "-"], input=script,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_ai_judge_configuration_evidence_and_evaluation_phase():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute browser judge behavior")
    static = Path(__file__).parents[1] / "src/giraffe/static"
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {configureJudging} from FORM_PATH;
const config={request_source:'request_pack',request_pack:{version:1,cases:[{fixture_id:'refund',scorer:'judge',expected:'30 days',rubric:'Preserve eligibility',messages:[{role:'user',content:'Refund?'}]}]},judging:{judges:[{name:'reviewer',url:'http://localhost:9000/v1',model:'judge-model',api_key_env:null,headers_env:{'X-Auth':'JUDGE_HEADER'}}],concurrency:2,request_timeout_seconds:30,max_duration_seconds:300,max_output_tokens:256,proxy:'http://localhost:3128',ca_bundle:'/cert.pem'}};
const values=new FormData();
values.set('judge_count','1');values.set('judge_0_name',' reviewer ');values.set('judge_0_url','http://localhost:9000/v1');values.set('judge_0_model','');values.set('judge_0_api_key_env','JUDGE_API_KEY');
for(const [key,value] of Object.entries(config.judging))if(typeof value==='number')values.set('judging_'+key,String(value));
values.set('judging_concurrency','3');
const draft=configureJudging(config,values,{allowInvalid:true});
assert.equal(draft.judging.judges[0].model,'');assert.equal(draft.judging.judges[0].name,'reviewer');
assert.equal(draft.judging.judges[0].api_key_env,'JUDGE_API_KEY');assert.equal(draft.judging.concurrency,3);
assert.deepEqual(draft.judging.judges[0].headers_env,config.judging.judges[0].headers_env);
assert.equal(draft.judging.ca_bundle,'/cert.pem');assert.equal(draft.judging.proxy,config.judging.proxy);
assert.deepEqual(draft.request_pack,config.request_pack);assert.equal(config.judging.judges[0].model,'judge-model');
assert.deepEqual(configureJudging({...draft,request_source:'builtin'},new FormData()),{...draft,request_source:'builtin'});
values.set('judging_concurrency','bad');assert.throws(()=>configureJudging(config,values),/finite number/);
assert.equal(configureJudging(config,values,{allowInvalid:true}).judging.concurrency,'bad');
const nodes={'#main':{innerHTML:''},'#request-pack-preview':{innerHTML:''},'#config-error':{hidden:true},'#request-dialog':{innerHTML:'',showModal(){this.shown=true;}}};
const context={assert,structuredClone,nodes,config,draft,document:{querySelector:q=>nodes[q],addEventListener(){}},window:{addEventListener(){}},location:{hash:'#new'}};
const source=fs.readFileSync(APP_PATH,'utf8').replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
vm.runInNewContext(source+`
state.config=draft;state.jsonDraft=state.jsonBase='synced';
let captured=0,rendered=0;
captureFormDraft=()=>{captured++;};renderNew=()=>{rendered++;};
let markup=renderJudging(draft);assert.match(markup,/AI evaluator/);assert.match(markup,/judge_0_model/);assert.match(markup,/Evaluation limits/);assert.match(markup,/configured evaluators/);
assert.equal(renderJudging({...draft,request_source:'builtin'}),'');
assert.equal(renderJudging({...draft,request_pack:{cases:[{scorer:'exact'}]}}),'');
changeJudge('add');assert.equal(state.config.judging.judges.length,2);assert.equal(captured,1);assert.equal(rendered,1);
assert.equal(state.config.judging.judges[1].model,'');assert.equal(state.requestPreview,null);
changeJudge('remove',0);assert.equal(state.config.judging.judges.length,1);assert.equal(state.config.judging.judges[0].name,'judge-1');
state.jsonDraft='unapplied';changeJudge('add');assert.equal(state.config.judging.judges.length,1);assert.match(nodes['#config-error'].textContent,/Apply or discard/);
state.jsonDraft=state.jsonBase;
state.config=structuredClone(config);
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,config:state.config,data:{active:true,consumers:['correctness'],cases:[],stages:[],warnings:['AI judging runs after target traffic. Messages, golden answers and generated answers are sent to every configured judge.','Other warning'],judging:{active:true,ready:false,readiness_errors:['Missing <model>'],planned_call_upper_bound:12,judge_count:1}}};
renderRequestPreview();markup=nodes['#request-pack-preview'].innerHTML;assert.match(markup,/Missing &lt;model&gt;/);assert(!markup.includes("Missing <model>"));assert.doesNotMatch(markup,/judge calls/);assert.doesNotMatch(markup,/AI judging runs after target traffic/);assert.match(markup,/Other warning/);assert.equal(state.requestPreview.data.warnings.length,2);
const votes=[{judge:'<judge>',model:'<model>',url:'http://judge',status:'completed',verdict:true,reason:'<script>alert(1)</script>'},{judge:'second',status:'completed',verdict:false,reason:'Wrong quantity'}];
markup=renderJudgement({status:'inconclusive',votes});assert.match(markup,/Tie/);assert.match(markup,/Disagreement/);assert.match(markup,/&lt;script&gt;/);assert(!markup.includes('<script>'));assert.match(markup,/&lt;judge&gt;/);
markup=renderJudgement({status:'pass',votes:[...votes,{judge:'third',status:'completed',verdict:true}]});assert.match(markup,/Disagreement/);assert.match(markup,/Passed/);
markup=renderJudgement({status:'inconclusive',votes:[votes[0],{judge:'second',status:'error',verdict:null,error_category:'timeout'}]});assert.match(markup,/Incomplete panel/);assert.match(markup,/timeout/);assert.match(markup,/Reason not retained/);
state.detail={report:{requests:[{id:'r',fixture_id:'refund',judgement:{status:'inconclusive',votes},score:null}],manifest:{config:{retention:'all'}}}};
openRequest('r');assert(nodes['#request-dialog'].shown);assert.match(nodes['#request-dialog'].innerHTML,/AI evaluation/);assert.match(nodes['#request-dialog'].innerHTML,/Wrong quantity/);
state.detail={id:'run',state:'running',config:{targets:[{name:'target',model:'m'}],judging:config.judging,max_requests:50},progress:{event:'judging_progress',attempted:20,completed:20,judge_calls_total:40,judge_calls_completed:7,elapsed_seconds:10},events:[]};
renderLive();markup=nodes['#main'].innerHTML;assert.match(markup,/Evaluating golden answers/);assert.match(markup,/Target traffic finished/);assert.match(markup,/Judge calls completed/);assert.match(markup,/7.*40/s);assert.match(markup,/data-action="stop"/);assert(!markup.includes('Measuring the configured endpoint'));
state.detail.progress={event:'request_finished',attempted:20,completed:19};renderLive();assert.match(nodes['#main'].innerHTML,/Measuring the configured endpoint/);
`,context);
""".replace("APP_PATH", json.dumps(str(static / "app.js"))).replace(
        "FORM_PATH", json.dumps((static / "form-config.mjs").as_uri()))
    result = subprocess.run([node, "--input-type=module", "-"], input=script,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_dataset_editor_preserves_canonical_cases_and_stages_imports():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute dataset editing behavior")
    static = Path(__file__).parents[1] / "src/giraffe/static"
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {datasetCaseFromValues,changeDatasetScorer} from FORM_PATH;
const original={fixture_id:'conversation',messages:[{role:'system',content:'Return JSON'},{role:'user',content:'Nothing'}],scorer:'json',schema:{type:'null'},expected:null,weight:2,max_tokens:null,options:{temperature:0,response_format:{type:'json_object'}}};
const values=new FormData();
values.set('example_id','conversation');values.set('message_count','2');
original.messages.forEach((message,index)=>{values.set(`message_${index}_role`,message.role);values.set(`message_${index}_content`,message.content);});
values.set('example_scorer','json');values.set('example_expected','null');values.set('example_schema','{"type":"null"}');values.set('example_weight','2');values.set('example_max_tokens','');values.set('example_options',JSON.stringify(original.options));
assert.deepEqual(datasetCaseFromValues(original,values),original);
values.set('example_expected','');assert(!Object.hasOwn(datasetCaseFromValues(original,values),'expected'));
assert.throws(()=>changeDatasetScorer(original,'judge'),/Edit the expected answer.*as text first/);
assert.throws(()=>changeDatasetScorer({...original,expected:{a:1}},'exact'),/as text first/);
assert.deepEqual(changeDatasetScorer(original,'json'),original);
const judged={...original,scorer:'judge',expected:'Nothing',rubric:'No invented values'};delete judged.schema;
const exact=changeDatasetScorer(judged,'exact');assert.equal(exact.expected,'Nothing');assert(!Object.hasOwn(exact,'rubric'));assert.deepEqual(exact.messages,original.messages);assert.deepEqual(exact.options,original.options);
const unscored=changeDatasetScorer(judged,'none');assert(!Object.hasOwn(unscored,'expected'));assert(!Object.hasOwn(unscored,'rubric'));
values.set('example_scorer','judge');values.set('example_expected','No value');values.set('example_rubric',' Require absence ');
const edited=datasetCaseFromValues(original,values);assert.equal(edited.expected,'No value');assert.equal(edited.rubric,'Require absence');assert(!Object.hasOwn(edited,'schema'));assert.deepEqual(edited.messages,original.messages);assert.equal(original.expected,null);
const nodes={},listeners={};
for(const id of ['main','config-error','dataset-error','request-pack-preview','request-pack-file'])nodes['#'+id]={innerHTML:'',textContent:'',hidden:true,click(){this.clicked=true;}};
nodes['#dataset-dialog']={innerHTML:'',open:false,showModal(){this.open=true;},close(){this.open=false;}};
nodes['#new-run-form']={elements:{request_source:{value:'builtin'}}};
nodes['#dataset-import-form']={elements:{input_field:{value:'question'},expected_field:{value:'answer'},import_scorer:{value:'judge'}}};
nodes['#dataset-try-form']={elements:{try_target:{value:'secondary'},save_try_output:{checked:true}}};
const context={assert,structuredClone,nodes,listeners,datasetCaseFromValues,changeDatasetScorer,document:{querySelector:q=>nodes[q],querySelectorAll:()=>[],addEventListener:(kind,fn)=>listeners[kind]=fn},window:{addEventListener(){}},location:{hash:'#new'}};
const source=fs.readFileSync(APP_PATH,'utf8').replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
await vm.runInNewContext(source+`
(async()=>{
const initial={request_source:'builtin',request_pack:null,targets:[{name:'local',model:''},{name:'secondary',model:'m'}],checks:[],retention:'failures',max_requests:99,traffic:{rates:[1]}};
state.config=structuredClone(initial);state.jsonDraft=state.jsonBase='synced';
readForm=()=>({config:structuredClone(state.config),mode:'normal'});captureFormDraft=()=>{};
renderNew=()=>{state.jsonDraft=state.jsonBase='synced';};toast=()=>{};setActive=id=>{state.active=id;};
const pack={version:1,cases:[{fixture_id:'one',messages:[{role:'user',content:'<question>'}],scorer:'judge',expected:'<answer>',weight:1}]};
changeRequestSource('request_pack');assert.equal(state.config.request_source,'request_pack');assert.equal(state.config.request_pack,null);assert(!nodes['#request-pack-file'].clicked);
let markup=renderRequestSource(state.config);assert.match(markup,/My questions/);assert.match(markup,/Add your first question/);assert(!markup.includes('dataset-bulk-scorer'));
const calls=[];api=async(path,options)=>{calls.push({path,body:JSON.parse(options.body)});if(path==='/api/datasets/preview')return {columns:[],preview_rows:pack.cases,request_pack:pack,errors:[],needs_mapping:false};if(path==='/api/config/draft')return {config:JSON.parse(options.body)};throw Error('Unexpected endpoint');};
await importRequestPack({name:'examples.json',size:20,text:async()=>JSON.stringify(pack)});
assert.equal(state.config.request_pack,null);assert.equal(calls.length,1);assert.equal(calls[0].path,'/api/datasets/preview');assert(nodes['#dataset-dialog'].open);
markup=nodes['#dataset-dialog'].innerHTML;assert.match(markup,/Use dataset/);assert(markup.includes('<th>Input</th>'));assert.match(markup,/&lt;question&gt;/);assert(!markup.includes('<pre>'));
await useImportedDataset({preventDefault(){}});assert.equal(state.config.request_pack.cases[0].fixture_id,'one');assert.equal(state.config.targets[0].model,'');assert.equal(calls[1].path,'/api/config/draft');assert(!nodes['#dataset-dialog'].open);
const accepted=JSON.stringify(state.config);
// Sample loading uses the same staged preview: cancelling preserves custom questions.
const importApi=api;const sampleCalls=[];
api=async(path,options)=>{sampleCalls.push(path);const body=JSON.parse(options.body);if(path==='/api/config/draft')return {config:body};const sample=JSON.parse(body.text);return {columns:[],preview_rows:sample.cases,request_pack:sample,errors:[],needs_mapping:false};};
await sampleDataset();assert.equal(JSON.stringify(state.config),accepted);assert(nodes['#dataset-dialog'].open);
assert.deepEqual(sampleCalls,['/api/datasets/preview']);assert.equal(state.datasetImport.preview.request_pack.cases[0].fixture_id,'refund-window');
closeDatasetDialog();assert.equal(JSON.stringify(state.config),accepted);
await sampleDataset();await useImportedDataset({preventDefault(){}});
assert.equal(sampleCalls.at(-1),'/api/config/draft');assert.equal(state.config.request_pack.cases[0].fixture_id,'refund-window');assert(!nodes['#dataset-dialog'].open);
state.config=JSON.parse(accepted);api=importApi;
const invalidPreview=renderDatasetImportTable({columns:['question','answer'],preview_rows:[{question:'q',answer:'a'},null,42]});
assert.match(invalidPreview,/Invalid row: null/);assert.match(invalidPreview,/Invalid row: 42/);
// A slower old file read cannot replace a newer staged import.
let finishRead;const slowRead=importRequestPack({name:'old.json',size:20,text:()=>new Promise(done=>finishRead=done)});
await importRequestPack({name:'new.json',size:20,text:async()=>JSON.stringify(pack)});
finishRead(JSON.stringify(pack));await slowRead;assert.equal(state.datasetImport.name,'new.json');assert.equal(JSON.stringify(state.config),accepted);closeDatasetDialog();
// Flat import requires a confirmed mapping preview and explicit commit.
api=async(path,options)=>{const body=JSON.parse(options.body);calls.push({path,body});if(path==='/api/config/draft')return {config:body};return body.input_field?{columns:['question','answer'],preview_rows:[{question:'<question>',answer:'<answer>'}],request_pack:pack,errors:[],needs_mapping:false}:{columns:['question','answer'],preview_rows:[{question:'<question>',answer:'<answer>'}],suggested_mapping:{input_field:'question',expected_field:'answer'},request_pack:null,errors:[],needs_mapping:true};};
await importRequestPack({name:'examples.csv',size:20,text:async()=>'question,answer'});
assert.equal(JSON.stringify(state.config),accepted);assert.equal(state.datasetImport.preview.request_pack,null);
const before=calls.length;await useImportedDataset({preventDefault(){}});assert.equal(calls.length,before);
await previewDatasetMapping();assert.equal(calls.at(-1).body.input_field,'question');assert.equal(JSON.stringify(state.config),accepted);assert(state.datasetImport.preview.request_pack);
// Changing a mapping invalidates the preview; late earlier responses cannot restore it.
let resolve;api=()=>new Promise(done=>resolve=done);const older=previewDatasetMapping();const oldResolve=resolve;
listeners.change({target:{matches:()=>false,closest:selector=>selector==='#dataset-import-form',name:'input_field'}});
assert(state.datasetImport.mappingDirty);assert.match(nodes['#dataset-dialog'].innerHTML,/Mapping changed/);
const newer=previewDatasetMapping();resolve({columns:['question','answer'],preview_rows:[],request_pack:pack,errors:[]});await newer;
oldResolve({columns:[],preview_rows:[],request_pack:null,errors:[{field:'old',message:'stale'}]});await older;
assert.equal(state.datasetImport.preview.request_pack.cases[0].fixture_id,'one');assert(!state.datasetImport.mappingDirty);assert.equal(JSON.stringify(state.config),accepted);
// Cancelling while the explicit commit validates cannot apply it afterward.
const applying=useImportedDataset({preventDefault(){}});const commitResolve=resolve;
assert(state.configBusy);closeDatasetDialog();commitResolve({config:{...state.config,request_pack:{version:1,cases:[]}}});await applying;
assert.equal(JSON.stringify(state.config),accepted);assert.equal(state.configBusy,false);
state.datasetImport={name:'again.json',text:'{}',format:'json',revision:state.configRevision,route:state.routeVersion,sequence:0,mapping:{},preview:null};
// Leaving/cancelling an import while parsing keeps the active dataset unchanged.
const pending=previewDatasetMapping();const pendingResolve=resolve;closeDatasetDialog();pendingResolve({request_pack:pack});await pending;assert.equal(state.datasetImport,null);assert.equal(JSON.stringify(state.config),accepted);
// Edits to main configuration while import validation is in flight win.
const importing=importRequestPack({name:'late.json',size:20,text:async()=>JSON.stringify(pack)});await Promise.resolve();const staleResolve=resolve;configChanged();staleResolve({request_pack:pack,preview_rows:pack.cases});await importing;assert.equal(state.datasetImport.preview,null);assert.equal(JSON.stringify(state.config),accepted);closeDatasetDialog();
// Dirty JSON and oversized files are rejected before reading.
state.jsonDraft='unapplied';await importRequestPack({size:20,text:async()=>{throw Error('must not read');}});assert.match(nodes['#config-error'].textContent,/Apply or discard/);state.jsonDraft=state.jsonBase;
await importRequestPack({size:1048577,text:async()=>{throw Error('must not read');}});assert.match(nodes['#config-error'].textContent,/1 MiB/);
// Trial only submits one named example/target to its own bounded endpoint.
openTryExample(0);markup=nodes['#dataset-dialog'].innerHTML;assert.match(markup,/one inference request/);assert.match(markup,/No traffic stages or restart/);assert.match(markup,/Save this trial/);assert.match(markup,/name="save_try_output" type="checkbox" checked/);assert.match(markup,/retention setting stays unchanged/);
const trial=[];api=async(path,options)=>{trial.push({path,body:JSON.parse(options.body)});return {id:'trial',state:'queued'};};
await tryDatasetExample({preventDefault(){}});assert.equal(trial[0].path,'/api/runs/try-example');assert.equal(trial[0].body.fixture_id,'one');assert.equal(trial[0].body.target_name,'secondary');assert.equal(trial[0].body.config.retention,'all');assert.equal(state.config.retention,'failures');assert.equal(state.config.max_requests,99);assert.equal(trial[0].body.config.checks.length,0);assert.equal(location.hash,'#runs/trial');
// Unchecking trial retention overrides even an all-retention main config, without changing it.
state.active=null;state.config.retention='all';nodes['#dataset-try-form'].elements.save_try_output.checked=false;
openTryExample(0);await tryDatasetExample({preventDefault(){}});
assert.equal(trial[1].body.config.retention,'none');assert.equal(state.config.retention,'all');
// Removing the last row preserves My dataset as an empty draft.
state.active=null;api=async(path,options)=>({config:JSON.parse(options.body)});await removeExample(0);assert.equal(state.config.request_source,'request_pack');assert.equal(state.config.request_pack,null);assert.equal(state.config.targets[0].model,'');
})()
`,context);
""".replace("APP_PATH", json.dumps(str(static / "app.js"))).replace(
        "FORM_PATH", json.dumps((static / "form-config.mjs").as_uri()))
    result = subprocess.run([node, "--input-type=module", "-"], input=script,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_scorer_conversion_sample_and_request_evidence():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute UI behavior")
    static = Path(__file__).parents[1] / "src/giraffe/static"
    script = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {convertExpectedAnswer,datasetCaseFromValues} from FORM_PATH;
assert.equal(convertExpectedAnswer('"Paris"','json','text'),'Paris');
assert.equal(convertExpectedAnswer('Paris','text','json'),'"Paris"');
assert.equal(convertExpectedAnswer('"Paris"','text','text'),'"Paris"');
for(const text of ['"Paris"','line\nbreak','C:\\path','null','123']) {
  assert.equal(convertExpectedAnswer(convertExpectedAnswer(text,'text','json'),'json','text'),text);
}
for(const value of ['null','{}','[]','42','true'])assert.throws(()=>convertExpectedAnswer(value,'json','text'),/JSON string reference/);
assert.throws(()=>convertExpectedAnswer('{broken','json','text'),/Fix the JSON reference/);
const nodes={};
for(const id of ['example-expected-label','example-rubric-label','example-schema-label','example-expected-hint','dataset-error'])nodes['#'+id]={hidden:false,textContent:''};
nodes['#dataset-example-form']={elements:{example_scorer:{value:'json'},example_expected:{value:'"Paris"'}}};
const context={assert,nodes,structuredClone,convertExpectedAnswer,datasetCaseFromValues,FormData,document:{querySelector:q=>nodes[q],addEventListener(){}},window:{addEventListener(){}}};
const source=fs.readFileSync(APP_PATH,'utf8').replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
await vm.runInNewContext(source+`
(async()=>{
const form=nodes['#dataset-example-form'];
state.datasetEditor={expectedFormat:'json',scorer:'json'};
for(const scorer of ['exact','contains','judge']){
 form.elements.example_scorer.value=scorer;syncExampleScorer();assert.equal(form.elements.example_expected.value,'Paris');
 const values=new FormData();for(const [key,value] of Object.entries({example_id:'city',message_count:'1',message_0_role:'user',message_0_content:'Capital?',example_scorer:scorer,example_expected:form.elements.example_expected.value,example_weight:'1',example_options:'{}'}))values.set(key,value);
 assert.equal(datasetCaseFromValues(null,values).expected,'Paris');
 form.elements.example_scorer.value='json';syncExampleScorer();assert.equal(form.elements.example_expected.value,'"Paris"');
}
form.elements.example_scorer.value='none';syncExampleScorer();assert(nodes['#example-expected-label'].hidden);
form.elements.example_scorer.value='exact';syncExampleScorer();assert.equal(form.elements.example_expected.value,'Paris');
form.elements.example_scorer.value='json';syncExampleScorer();form.elements.example_expected.value='null';
form.elements.example_scorer.value='exact';syncExampleScorer();assert.equal(form.elements.example_scorer.value,'json');assert.equal(form.elements.example_expected.value,'null');assert.match(nodes['#dataset-error'].textContent,/JSON string reference/);
let sample;importRequestPack=async(file)=>{sample=JSON.parse(await file.text());assert.equal(file.name,'sample-dataset.json');};applyDataset=()=>{throw Error('sample must wait for explicit application');};await sampleDataset();
const exact=sample.cases.find(item=>item.scorer==='exact');assert.equal(exact.expected,'7');assert.match(exact.messages[0].content,/only the digit/);
const record={evaluation:{messages:[{role:'user',content:'<question>'}],scorer:'judge',expected:'<Paris>',expected_provided:true,rubric:'No <invention>'}};
let html=renderEvaluationEvidence(record,{});assert.match(html,/&lt;question&gt;/);assert.match(html,/&lt;Paris&gt;/);assert.match(html,/No &lt;invention&gt;/);assert.match(html,/AI evaluator/);assert(!html.includes('<question>'));
record.evaluation={messages:[],scorer:'json',expected:null,expected_provided:true,schema:{type:'null'}};
html=renderEvaluationEvidence(record,{});assert(html.includes("<pre>null</pre>"));assert.match(html,/JSON schema/);
const old={manifest:{config:{request_source:'request_pack',request_pack:{cases:[{fixture_id:'city',messages:[{role:'user',content:'Capital?'}],scorer:'exact',expected:'Paris'}]}}}};
assert.match(renderEvaluationEvidence({fixture_id:'pack:city'},old),/Paris/);
assert.match(renderEvaluationEvidence({fixture_id:'missing'},old),/not saved/);
})()
`,context);
'''.replace("FORM_PATH", json.dumps((static / "form-config.mjs").as_uri())).replace(
        "APP_PATH", json.dumps(str(static / "app.js")))
    result = subprocess.run([node, "--input-type=module", "-"], input=script,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_dataset_readiness_and_contextual_controls():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise browser setup behavior")
    app = Path(__file__).parents[1] / "src/giraffe/static/app.js"
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const nodes={
  '#form-error':{hidden:true},'#start-run':{},
  '[data-action="add-example"]':{focus(){this.focused=true;}},
  '.judge-configuration':{open:false},
};
const context={assert,nodes,structuredClone,
  document:{querySelector:q=>nodes[q],addEventListener(){}},
  window:{addEventListener(){}},location:{hash:'#new'}};
const source=fs.readFileSync(APP_PATH,'utf8').replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
await vm.runInNewContext(source+`
(async()=>{
state.config={request_source:'request_pack',checks:['correctness','recovery'],request_pack:null};
state.jsonDraft=state.jsonBase='synced';
syncTestControls=()=>{};updateBounds=()=>{};renderRequestPreview=()=>{};
// Missing questions and evaluator setup are caught before any HTTP request.
let calls=0;api=async()=>{calls++;throw Error('should not send');};
validateRunForm=()=>true;readForm=()=>({config:structuredClone(state.config)});
await startRun({preventDefault(){},target:{}});
assert.equal(calls,0);assert(nodes['[data-action="add-example"]'].focused);
assert.match(nodes['#form-error'].textContent,/Add a question/);
state.config.request_pack={cases:[{fixture_id:'one',scorer:'judge'}]};
let additions=0;changeJudge=action=>{assert.equal(action,'add');additions++;};
await startRun({preventDefault(){},target:{}});
assert.equal(calls,0);assert.equal(additions,1);assert(nodes['.judge-configuration'].open);
assert.match(nodes['#form-error'].textContent,/Set up the AI evaluator/);
assert(!judgesReady(state.config));
state.config.judging={judges:[{name:'evaluator',url:'http://localhost/v1',model:'m'}]};
assert(judgesReady(state.config));state.config.judging.judges[0].model=' ';assert(!judgesReady(state.config));
// Bulk controls are contextual; exact matches never require an AI evaluator.
state.judgeEditorOpen=false;state.datasetSelection=[];
let markup=renderAnswerChecks(state.config);
assert(!markup.includes('dataset-bulk-scorer'));assert(markup.includes('Configure evaluator'));
assert(!markup.includes('class="judge-configuration" open'));
state.datasetSelection=['one'];markup=renderAnswerChecks(state.config);
assert(markup.includes('dataset-bulk-scorer'));
state.config.request_pack.cases[0].scorer='exact';markup=renderAnswerChecks(state.config);
assert(!markup.includes('judge_count'));assert(!markup.includes('Configure evaluator'));
})()
`,context);
""".replace("APP_PATH", json.dumps(str(app)))
    result = subprocess.run([node, "--input-type=module", "-"], input=script,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("server_status,answer_status", [
    ("pass", "fail"), ("fail", "pass"), ("inconclusive", "pass"),
])
def test_cancellation_ui_renders_independent_verdicts(server_status, answer_status):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute browser rendering")
    app = Path(__file__).parents[1] / "src/giraffe/static/app.js"
    outcomes = {
        "server_stopped": {"title": "Cancellation — verify server stopped", "status": server_status,
                           "summary": "Native evidence only", "verified": 3, "attempted": 3},
        "answer": {"title": "Cancellation — verify answer", "status": answer_status,
                   "summary": "Completed answers only", "correct": 2, "scored": 3},
    }
    script = rf"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync({json.dumps(str(app))},'utf8')
  .replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
vm.runInNewContext(source+`
const outcomes={json.dumps(outcomes)};
const check={{id:'cancellation',title:'Limits and cancellation',status:'fail',required:true,
  target:'local',metrics:{{cancellation_results:outcomes}},evidence_ids:[]}};
const report={{manifest:{{config:{{}}}},requests:[]}};
const markup=renderCheck(check,report);
const sections=[...markup.matchAll(/<section class="check-detail-head"[\\s\\S]*?<\\/section>/g)].map(x=>x[0]);
assert.equal(sections.length,2);
assert.ok(sections[0].includes(outcomes.server_stopped.title));
assert.ok(sections[0].includes('class="status '+outcomes.server_stopped.status+'"'));
assert.ok(sections[1].includes(outcomes.answer.title));
assert.ok(sections[1].includes('class="status '+outcomes.answer.status+'"'));
assert.ok(keyMetric(check).includes('Server stopped:'));
assert.ok(keyMetric(check).includes('Answer:'));
assert.match(renderCancellationResults({{}}),/older report/);
`,{{assert,structuredClone,document:{{addEventListener(){{}}}},window:{{addEventListener(){{}}}}}});
"""
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
