"""Exercise the browser's real serializer without a DOM or an extra JS framework."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from giraffe.models import CHECK_LIMIT_FIELDS, CHECK_NAMES, CHECK_OPTION_FIELDS, RunConfig


@pytest.mark.parametrize("scenario", [
    "selection", "defaults", "nullable_limits", "traffic", "empty_selection", "round_trip",
    "optional_workloads",
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
