"""Exercise the browser's real serializer without a DOM or an extra JS framework."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from giraffe.models import ConcurrentLoad, LoadTest
from giraffe.models import CHECK_LIMIT_FIELDS, CHECK_NAMES, CHECK_OPTION_FIELDS, RunConfig


def test_first_run_goals_sizes_and_confirmed_live_plan():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise browser setup behavior")
    from giraffe.capabilities import capabilities

    static = Path(__file__).parents[1] / "src/giraffe/static"
    script = rf"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as formConfig from {json.dumps((static / 'form-config.mjs').as_uri())};
const nodes={{'#request-plan':{{innerHTML:''}},'#request-pack-preview':{{innerHTML:''}}}},listeners={{}},timers=[];
const context={{assert,structuredClone,...formConfig,nodes,listeners,timers,
  setTimeout(fn){{timers.push(fn);return timers.length;}},clearTimeout(){{}},
  document:{{querySelector:q=>nodes[q],querySelectorAll:()=>[],addEventListener:(kind,fn)=>listeners[kind]=fn}},window:{{addEventListener(){{}}}}}};
const source=fs.readFileSync({json.dumps(str(static / 'app.js'))},'utf8')
  .replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
context.bootstrap={json.dumps(capabilities())};
context.config={json.dumps(RunConfig(targets=[{'name':'local','url':'http://localhost/v1','model':'test'}],checks=['access','load'],concurrency=8,load_test={'patterns':{'concurrent':{'levels':[1,4,8]}}}).model_dump(mode='json'))};
await vm.runInNewContext(source+`
(async()=>{{
state.bootstrap=bootstrap;state.bootstrap.config=config;state.config=config;state.setupPreset='concurrent';state.jsonDraft=state.jsonBase='synced';
const before=JSON.stringify(config);
const bucket=state.bootstrap.checks.find(check=>check.id==='load');
let markup=renderTestCard(bucket,config);
assert.match(markup,/Concurrency levels/);
for(const size of ['short','medium','long']){{
  assert(markup.includes('name="load_profile_'+size+'_input_tokens"'));
  assert(markup.includes('name="load_profile_'+size+'_output_tokens"'));
}}
assert.equal(JSON.stringify(config),before);
for(const name of ['load_patterns_concurrent_levels','load_profile_short_input_tokens','load_profile_long_output_tokens'])assert.equal((markup.match(new RegExp('name="'+name+'"','g'))||[]).length,1);
const rate=renderTestCard(state.bootstrap.checks.find(check=>check.id==='load'),config);
for(const name of ['load_patterns_rates','load_schedule_duration_seconds'])assert.equal((rate.match(new RegExp('name="'+name+'"','g'))||[]).length,1);
state.setupPreset='concurrent';
config.load_test.profile.long.output_tokens=config.max_output_tokens+1;
assert.doesNotMatch(renderSettingIssues(loadTestIssues(config,bootstrap.load_test.defaults,bootstrap.limits)),/output cap.*run.*cap/i);
assert.doesNotMatch(renderSettingIssues(loadTestIssues(config,bootstrap.load_test.defaults,bootstrap.limits)),/data-setting-path="max_output_tokens"/);
config.load_test.profile.long.output_tokens=512;
const plan={{count_kind:'exact',planned_per_target:164,planned_total:164,known_per_target:164,known_total:164,request_limit:20,required_request_limit:164,target_quotas:{{local:20}},rows:[{{id:'initial',title:'Connection check',requests_per_target:1,kind:'exact',source:'generated examples',summary:'One generated request checks the connection.'}},{{id:'access',title:'Shared accounting',requests_per_target:0,kind:'exact',source:'generated examples',summary:'Adds no requests.'}}],bucket_cases:[{{input_chars:64,actual_input_chars:96,output_tokens:16}}],bucket_levels:[],evaluator:{{active:true,ready:true,call_upper_bound:12,judge_count:1,readiness_errors:[]}},warnings:[]}};
config.max_requests=20;
plan.request_warnings=[{{kind:'incomplete_coverage',fields:['max_requests'],msg:'The request budget leaves checks unfinished.',actions:[{{label:'Use 164 total requests',field:'max_requests',value:164}}]}}];
plan.estimated_seconds=0;plan.estimated_low_seconds=0;plan.estimated_high_seconds=0;plan.confidence='unknown';
state.requestPreview={{status:'current',revision:state.configRevision,route:state.routeVersion,plan,config}};
renderRequestPlan();
assert.match(nodes['#request-plan'].innerHTML,/164 requests/);
assert.match(nodes['#request-plan'].innerHTML,/12 evaluator calls/);
assert.match(nodes['#request-plan'].innerHTML,/data-action="use-request-budget"/);
assert.match(nodes['#request-plan'].innerHTML,/<details class="request-planning">/);
assert.match(nodes['#request-plan'].innerHTML,/96 characters sent/);assert.match(nodes['#request-plan'].innerHTML,/64 configured, plus instructions/);
assert.match(nodes['#request-plan'].innerHTML,/Connection check/);assert.doesNotMatch(nodes['#request-plan'].innerHTML,/Shared accounting/);
configChanged();assert.match(nodes['#request-plan'].innerHTML,/stale/i);
assert.doesNotMatch(nodes['#request-plan'].innerHTML,/data-action="use-request-budget"|164 requests/);
let current=structuredClone(config),resolve;
readForm=()=>({{config:structuredClone(current)}});
api=()=>new Promise(done=>resolve=done);
const older=refreshRequestPreview(undefined,{{draft:true}}),oldResolve=resolve;
current.max_requests=200;configChanged();const newer=refreshRequestPreview(undefined,{{draft:true}});
resolve({{config:current,request_plan:{{...plan,request_limit:200}}}});await newer;
oldResolve({{config,request_plan:plan}});await assert.rejects(older,/changed during preview/);
assert.equal(state.requestPreview.plan.request_limit,200);
// A failed current plan can be retried, and the retry replaces the failure state.
api=async()=>{{throw Error('temporary planning failure');}};
await assert.rejects(refreshRequestPreview(undefined,{{draft:true}}),/temporary planning failure/);
assert.match(nodes['#request-plan'].innerHTML,/Retry request plan/);
assert.doesNotMatch(nodes['#request-plan'].innerHTML,/200 requests planned/);
api=async(path,options)=>({{config:JSON.parse(options.body),request_plan:{{...plan,request_limit:200}}}});
await listeners.click({{target:{{closest(q){{return q==='[data-action]'?{{dataset:{{action:'preview-pack'}}}}:null;}}}}}});
assert.equal(state.requestPreview.status,'current');
assert.doesNotMatch(nodes['#request-plan'].innerHTML,/Retry request plan|temporary planning failure/);
nodes['#cli-fallback']={{hidden:false}};
const syncView=syncConfigView;syncConfigView=()=>{{}};
listeners.input({{target:{{id:'config-editor',value:'dirty',matches(){{return false;}},closest(){{return {{}};}}}}}});
syncConfigView=syncView;
assert.match(nodes['#request-plan'].innerHTML,/Apply or discard JSON changes/);
assert.doesNotMatch(nodes['#request-plan'].innerHTML,/164 requests|data-action="use-request-budget"/);
// Debounced live planning never sends inference, and a replaced timer cannot plan an old form.
state.jsonDraft=state.jsonBase;nodes['#new-run-form']={{}};
const paths=[];api=async(path,options)=>{{paths.push(path);return {{config:JSON.parse(options.body),request_plan:plan}};}};
scheduleRequestPlan();const replaced=timers.at(-1);configChanged();scheduleRequestPlan();
await replaced();assert.equal(paths.length,0);
await timers.at(-1)();assert.equal(paths.join(','),'/api/config/draft');
// The native change event on blur replaces the input timer rather than leaving planning stale.
const beforeBlur=timers.length;
listeners.change({{target:{{name:'max_requests',id:'',tagName:'INPUT',matches(){{return false;}},closest(q){{return q==='#new-run-form'?{{}}:null;}}}}}});
assert.equal(timers.length,beforeBlur+1);await timers.at(-1)();assert.equal(paths.join(','),'/api/config/draft,/api/config/draft');
// A full dataset preview survives live count refreshes, but remains clearly labeled previous.
current.request_source='request_pack';current.request_pack={{version:1,cases:[{{fixture_id:'saved',messages:[{{role:'user',content:'question'}}],weight:1,scorer:'exact',expected:'answer'}}]}};
state.config=structuredClone(current);
const packDetails={{active:true,consumers:['correctness'],cases:[],stages:[],warnings:[],quiet_requests:1}};
api=async(path,options)=>({{config:JSON.parse(options.body),request_plan:plan,...(path==='/api/config/preview'?{{request_pack_preview:packDetails}}:{{}})}});
await refreshRequestPreview();const packRevision=state.requestPreview.packRevision;
configChanged();await refreshRequestPreview(undefined,{{draft:true}});
assert.equal(state.requestPreview.data,packDetails);assert.equal(state.requestPreview.packRevision,packRevision);
assert.equal(state.requestPreview.status,'current');assert.match(nodes['#request-pack-preview'].innerHTML,/Preview stale/);
// Restart readiness is counted in the cloned planning input without changing execution state.
readForm=()=>({{config:structuredClone(current),mode:'cold-start',restart_target:'local'}});
let planningInput;api=async(path,options)=>{{planningInput=JSON.parse(options.body);return {{config:planningInput,request_plan:plan}};}};
await refreshRequestPreview(current,{{draft:true}});
assert.equal(planningInput.restart_target,'local');assert.equal(current.restart_target,null);assert.equal(state.config.restart_target,null);
// Leaving this route invalidates the displayed plan and its in-flight replacement.
api=()=>new Promise(done=>resolve=done);
const navigationPreview=refreshRequestPreview(undefined,{{draft:true}});
state.routeVersion++;state.requestPreview=null;renderRequestPlan();
assert.doesNotMatch(nodes['#request-plan'].innerHTML,/164 requests|data-action="use-request-budget"/);
resolve({{config:current,request_plan:plan}});
await assert.rejects(navigationPreview,/changed during preview/);
assert.equal(state.requestPreview,null);
}})()
`,context);
"""
    result = subprocess.run([node, "--input-type=module", "-"], input=script,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("capacity_limit", [700, None])
def test_setup_presets_render_one_control_per_value_and_keep_real_form_state(capacity_limit):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise browser setup behavior")
    from giraffe.capabilities import capabilities

    static = Path(__file__).parents[1] / "src/giraffe/static"
    config = RunConfig(targets=[{"name": "local", "url": "http://localhost/v1", "model": "test"}],
                       checks=["access", "correctness"], prefix={"repeats": 7},
                       request_pack={"version": 1, "cases": [{"fixture_id": "saved",
                                      "messages": [{"role": "user", "content": "Private <question>"}],
                                      "scorer": "exact", "expected": "Answer"}]},
                       limits={"latency_ms": 2000, "first_output_ms": 1000},
                       test_options={"recovery": {"sustained_seconds": 2},
                                     "capacity": {"limits": {"latency_ms": capacity_limit,
                                                              "first_output_ms": 300}}})
    script = rf"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as formConfig from {json.dumps((static / 'form-config.mjs').as_uri())};
const nodes={{}},controls=[],listeners={{}},snapshots=[];
const decode=value=>(value || '').replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
const attr=(tag,key)=>decode(tag.match(new RegExp('(?:^|\\s)'+key+'="([^"]*)"'))?.[1]);
const form={{elements:{{}},querySelectorAll:()=>controls.filter(input=>/^(test_|traffic_|load_)/.test(input.name))}};
let html='';
nodes['#main']={{get innerHTML(){{return html;}},set innerHTML(value){{
  html=value;controls.length=0;form.elements={{}};
  for(const match of value.matchAll(/<input\b[^>]*>|<select\b[^>]*>[\s\S]*?<\/select>/g)){{
    const tag=match[0],name=attr(tag,'name');if(!name)continue;
    const type=attr(tag,'type'),options=[...tag.matchAll(/<option\b([^>]*)>([^<]*)<\/option>/g)];
    const option=options.find(item=>/\bselected\b/.test(item[1])) || options[0];
    const input={{name,type,value:tag.startsWith('<select')?attr(option?.[1] || '', 'value'):attr(tag,'value'),checked:/\bchecked\b/.test(tag),dataset:{{}}}};
    controls.push(input);form.elements[name] ||= input;
  }}
}}}};
nodes['#new-run-form']=form;nodes['#config-editor']={{value:''}};
nodes['#cli-fallback']={{hidden:false}};
class FormValues {{
  constructor(){{this.values=new Map();for(const input of controls){{if(['radio','checkbox'].includes(input.type)&&!input.checked)continue;this.append(input.name,input.value);}}}}
  append(name,value){{this.values.set(name,[...(this.values.get(name)||[]),value]);}}
  set(name,value){{this.values.set(name,[value]);}}
  get(name){{return this.values.get(name)?.[0] ?? null;}}
  getAll(name){{return this.values.get(name)||[];}}
  has(name){{return this.values.has(name);}}
  keys(){{return this.values.keys();}}
}}
const context={{assert,structuredClone,...formConfig,nodes,form,controls,listeners,snapshots,FormData:FormValues,location:{{hash:'#new'}},
 document:{{querySelector:q=>nodes[q],querySelectorAll:()=>[],addEventListener:(kind,fn)=>listeners[kind]=fn}},window:{{addEventListener(){{}}}},
 setTimeout(){{}},clearTimeout(){{}},console}};
const source=fs.readFileSync({json.dumps(str(static / 'app.js'))},'utf8').replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
context.bootstrap={json.dumps(capabilities())};context.config={json.dumps(config.model_dump(mode='json'))};
await vm.runInNewContext(source+`
(async()=>{{
state.bootstrap=bootstrap;state.bootstrap.config=structuredClone(config);state.config=structuredClone(config);
state.jsonDraft=state.jsonBase='synced';
const renderBounds=updateBounds;
syncTestControls=updateBounds=updateRestart=syncConfigView=syncPackControls=renderRequestPreview=()=>{{}};
const savedPack=JSON.stringify(config.request_pack),savedProfile=JSON.stringify(config.prefix),savedOptions=JSON.stringify(config.test_options);
for(const preset of ['answers','concurrent','arrival','custom']){{
  state.config=applySetupPreset(config,preset,state.bootstrap.load_test.defaults);state.jsonDraft=state.jsonBase='synced';
  const before=JSON.stringify(state.config);renderNew(preset);
  assert.equal(JSON.stringify(state.config),before); // Merely opening never changes selection.
  const markup=nodes['#main'].innerHTML;
  assert.doesNotMatch(markup,/setup-side-summary|setup-planned-traffic|review-baseline/);
  const options=markup.slice(markup.indexOf('id="execution-settings"'),markup.indexOf('</details>',markup.indexOf('id="execution-settings"')));
  assert(options.includes('name="baseline_id"'));assert(options.includes('Compare with baseline'));
  assert(markup.indexOf('id="run-bounds"')<markup.indexOf('id="request-plan"'));
  for(const name of ['mode','restart_target','baseline_id','name'])assert(controls.some(input=>input.name===name));
  assert(markup.indexOf('id="setup-deployment"')<markup.indexOf('id="test-selection"'));assert(!markup.includes('name="setup_goal"'));
  for(const name of new Set(controls.map(input=>input.name))){{
    if(['check','setup_goal','setup_load','request_source'].includes(name))continue;
    assert.equal(controls.filter(input=>input.name===name).length,1,preset+': duplicate '+name);
  }}
  assert.equal(controls.filter(input=>input.name==='check').length,15);
  for(const name of ['max_requests','max_duration_seconds'])assert(controls.some(input=>input.name===name));
  if(preset==='concurrent'){{
    assert.equal((markup.match(/id="setup-dataset"/g)||[]).length,1);
    assert.equal(form.elements.concurrency.type,'number');
    for(const size of ['short','medium','long'])assert(form.elements['load_profile_'+size+'_input_tokens']&&form.elements['load_profile_'+size+'_output_tokens']);
    form.elements.load_patterns_concurrent_levels.value='1, 4, 8';
    for(const [index,size] of ['short','medium','long'].entries()){{form.elements['load_profile_'+size+'_input_tokens'].value=String(80+index*80);form.elements['load_profile_'+size+'_output_tokens'].value=String(8+index*8);}}
  }}
  if(preset==='arrival'){{
    const primary=renderTestCard(state.bootstrap.checks.find(check=>check.id==='load'),state.config);
    assert(primary.includes('name="load_patterns_rates"'));assert(primary.includes('name="load_pass_criteria_max_completion_ms"'));
    assert(!primary.includes('name="limit_latency_ms"'));assert.equal(form.elements.limit_latency_ms,undefined);
    assert(primary.includes('Pass criteria'));
  }}
  const result=readForm(true).config;
  assert.equal(JSON.stringify(result.request_pack),savedPack);
  assert.equal(JSON.stringify(result.prefix),savedProfile);
  assert.equal(JSON.stringify(result.test_options),savedOptions);
  assert.equal(result.max_requests,config.max_requests);
  if(preset==='concurrent'){{
    assert.equal(result.concurrency,state.config.concurrency);
    for(const [index,size] of ['short','medium','long'].entries()){{assert.equal(result.load_test.profile[size].input_tokens,80+index*80);assert.equal(result.load_test.profile[size].output_tokens,8+index*8);}}
    assert.equal(result.max_output_tokens,config.max_output_tokens);
  }}
}}
// Explicit goal changes use the form's latest draft rather than a stale applied config.
form.elements.model.value='latest-model';await listeners.click({{target:{{closest:q=>q==='[data-action]'?{{dataset:{{action:'quick-checks'}}}}:null}}}});
assert.equal(state.config.targets[0].model,'latest-model');
assert.equal(state.config.checks.join(','),'access,correctness');
assert.equal(JSON.stringify(state.config.request_pack),savedPack);
assert.equal(state.config.max_requests,config.max_requests);
// Reruns and imported advanced selections infer Custom without rewriting them.
const custom={{...structuredClone(config),checks:['access','correctness','recovery'],concurrency:16}};
state.detail={{report:{{manifest:{{config:custom}}}},baseline_id:'saved-baseline'}};rerun();
const rerunBefore=JSON.stringify(state.config);renderNew();
assert.equal(state.setupPreset,'custom');assert.equal(JSON.stringify(state.config),rerunBefore);
assert.equal(state.config.concurrency,16);assert.equal(state.prefillBaseline,'saved-baseline');
// Dedicated response criteria edit only this check; Reset restores its defaults.
state.config=applySetupPreset(config,'arrival',state.bootstrap.load_test.defaults);renderNew('arrival');
const primary=form.elements.load_pass_criteria_max_completion_ms;primary.value='900';
let current=readForm(true).config;assert.equal(current.load_test.pass_criteria.max_completion_ms,900);
assert.equal(current.limits.latency_ms,2000);assert.deepEqual(current.test_options.recovery,config.test_options.recovery);
captureFormDraft=()=>{{state.config=readForm(true).config;}};
await listeners.click({{target:{{closest:q=>q==='[data-reset-test]'?{{dataset:{{resetTest:'load'}}}}:null}}}});
assert.equal(loadSettings(readForm(true).config).pass_criteria.max_completion_ms,30000);
// My questions can remain an empty saved draft while generated-only work executes.
// Token ID readiness is covered separately; this fixture exercises source normalization.
currentTokenizerSelection=()=>({{status:'ready',tokenizer:'builtin'}});
state.config=applySetupPreset(config,'concurrent',state.bootstrap.load_test.defaults);state.config.checks=['access'];
state.config.request_source='request_pack';state.config.request_pack=null;renderNew('concurrent');
toast=()=>{{}};api=async(path,options)=>{{const body=JSON.parse(options.body);snapshots.push({{path,config:body}});return {{config:body}};}};
const editable=readForm(true).config;
const strict=await validatedSnapshot();
assert.equal(strict.config.request_source,'builtin');assert.equal(strict.config.request_pack,null);
assert.equal(editable.request_source,'request_pack');assert.equal(state.config.request_source,'request_pack');
const draft=await validatedSnapshot({{draft:true}});assert.equal(draft.config.request_source,'request_pack');
// Strict source normalization does not hide a missing dataset from its consumers.
for(const check of ['correctness','load']){{
  state.config={{...structuredClone(config),checks:[check],request_source:'request_pack',request_pack:null}};renderNew('custom');
  const consumer=await validatedSnapshot();assert.equal(consumer.config.request_source,'request_pack');
}}
// A successful generated-only run sends builtin, but retains the editable empty choice.
state.config=applySetupPreset(editable,'concurrent',state.bootstrap.load_test.defaults);state.config.checks=['access'];renderNew('custom');
nodes['#form-error']={{hidden:true}};nodes['#start-run']={{}};nodes['#new-active-notice']={{hidden:true}};
validateRunForm=()=>true;setActive=()=>{{}};refreshRequestPreview=async()=>{{}};
state.requestPreview={{status:'current',revision:state.configRevision,route:state.routeVersion,plan:{{}}}};
let runPayload;api=async(path,options)=>{{const body=JSON.parse(options.body);if(path==='/api/runs'){{runPayload=body;return {{id:'synthetic-run'}};}}return {{config:body}};}};
await startRun({{preventDefault(){{}},target:form}});
assert.equal(runPayload.config.request_source,'builtin');
assert.equal(state.config.request_source,'request_pack');assert.equal(state.config.request_pack,null);
console.log(JSON.stringify(snapshots));
}})()
`,context);
"""
    result = subprocess.run([node, "--input-type=module", "-"], input=script,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    snapshots = json.loads(result.stdout)
    RunConfig.model_validate(snapshots[0]["config"])
    from giraffe.models import ConfigDraft
    ConfigDraft.model_validate(snapshots[1]["config"])
    for snapshot in snapshots[1:]:
        with pytest.raises(ValueError, match="request_pack is required"):
            RunConfig.model_validate(snapshot["config"])


def test_field_help_preserves_labels_and_escapes_details():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute browser configuration behavior")
    from giraffe.capabilities import capabilities

    static = Path(__file__).parents[1] / "src/giraffe/static"
    script = f"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {{optionDefault,optionValue,limitValue,sameConfig,effectiveProfileDefaults,loadTestSettings,loadFieldName}} from {json.dumps((static / 'form-config.mjs').as_uri())};
const context={{structuredClone,optionDefault,optionValue,limitValue,sameConfig,effectiveProfileDefaults,loadTestSettings,loadFieldName,
  document:{{addEventListener(){{}}}},window:{{addEventListener(){{}}}}}};
const source=fs.readFileSync({json.dumps(str(static / 'app.js'))},'utf8')
  .replace(/^import .*?;\\n/,'').replace(/\\ninit\\(\\);\\s*$/,'');
vm.runInNewContext(source,context);
const html=vm.runInNewContext(`field('concurrency','Maximum in-flight requests',8)`,context);
assert.match(html,/<label for="field-concurrency">Maximum in-flight requests<\\/label>/);
assert.match(html,/<input id="field-concurrency" name="concurrency"/);
assert.match(html,/aria-describedby="field-concurrency-hint"/);
assert.match(html,/<button type="button"[^>]*popovertarget="field-concurrency-help"/);
assert.match(html,/aria-label="About Maximum in-flight requests"/);
assert.match(html,/id="field-concurrency-help"[^>]*popover="auto"/);
assert.doesNotMatch(html,/<label[^>]*>[^<]*<button/);
const unsafe=vm.runInNewContext(`field('custom','<img src=x>',1,{{help:'<script>bad</script>'}})`,context);
assert.doesNotMatch(unsafe,/<img|<script>/);
const text=vm.runInNewContext(`textField('example_expected','Expected answer','answer',{{hint:'Use a JSON value.',hintId:'answer-hint'}})`,context);
assert.match(text,/aria-describedby="answer-hint"/);
assert.match(text,/id="answer-hint"/);
assert.doesNotMatch(vm.runInNewContext(`textField('example_schema','JSON schema','{{}}')`,context),/aria-describedby="field-example_schema-hint"/);
context.bootstrap={json.dumps(capabilities())};
context.config={json.dumps(RunConfig(targets=[{'name':'local','url':'http://localhost','model':'test'}]).model_dump(mode='json'))};
vm.runInNewContext('state.bootstrap=bootstrap;state.bootstrap.config=config;',context);
for(const check of context.bootstrap.checks) {{
  context.check=check;
  const markup=vm.runInNewContext('renderTestCard(check,config)',context);
  assert.match(markup,/class="check-info-details"/);assert(markup.includes('<dt>Sends</dt>'));
  for(const name of check.option_fields) {{
    assert(markup.includes(`name="test_${{check.id}}_${{name}}"`));
    assert(markup.includes(`popovertarget="field-test_${{check.id}}_${{name}}-help"`));
  }}
  for(const name of check.limit_fields) {{
    assert(markup.includes(`popovertarget="field-test_${{check.id}}_limit_${{name}}-help"`));
  }}
}}
"""
    result = subprocess.run([node, "--input-type=module", "-"], input=script,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_load_controls_and_request_budget_guidance():
    from tests.test_ux_catalogue import browser_logic
    from tests.test_concurrent_load_ui_review import FORM
    browser_logic(FORM + r'''
const before=JSON.stringify(config),html=renderLoadEditor(config);
for(const name of ['load_patterns_concurrent_levels','load_schedule_duration_seconds','load_request_timeout_seconds','load_patterns_rates'])assert(html.includes('name="'+name+'"'));
assert(!html.includes('name="load_schedule_finish_seconds"'));
assert.doesNotMatch(html,/Samples per|Pairs per|Maximum seconds per|p95/);assert.equal(JSON.stringify(config),before);
assert.doesNotMatch(renderLoadPlan(config),/0 requests/);
for(const invalid of ['', '0','2, 2','2, 1','1.5','1025','Infinity','oops']){
 form.elements.load_patterns_concurrent_levels.value=invalid;assert.throws(()=>readForm(false),/positive, increasing/);
}
form.elements.load_patterns_concurrent_levels.value='1, oops';assert.doesNotThrow(()=>readForm(true));
form.elements.load_patterns_concurrent_levels.value='4';
assert.equal(readForm(false).config.load_test.patterns.concurrent.levels.join(','),'4');
const draft=readForm(true).config;draft.concurrency=2;
const issues=loadTestIssues(draft,bootstrap.load_test.defaults,bootstrap.limits);
assert.match(renderSettingIssues(issues),/Concurrency levels exceed the run ceiling/);assert.match(renderSettingIssues(issues),/data-setting-path="concurrency"/);
form.elements.load_patterns_concurrent_levels.value='<img src=x>';assert.doesNotMatch(renderLoadEditor(readForm(true).config),/<img src=x>/);
''')


def test_concurrent_load_and_token_targets_roundtrip_through_real_form_serializer():
    from tests.test_ux_catalogue import browser_logic
    from tests.test_concurrent_load_ui_review import FORM
    result=browser_logic(FORM + r'''
form.elements.load_workloads_combinations_enabled.checked=false;
form.elements.load_workloads_mixed_enabled.checked=true;form.elements.load_workloads_stress_enabled.checked=true;
for(const shape of loadShapes)form.elements['load_workloads_mixed_selected_'+shape].checked=['short_short','long_long'].includes(shape);
form.elements.load_workloads_mixed_share_mode.value='custom';form.elements.load_workloads_mixed_shares_short_short.value='40';form.elements.load_workloads_mixed_shares_long_long.value='60';
form.elements.load_patterns_concurrent_levels.value='2, 4';form.elements.load_schedule_duration_seconds.value='10';form.elements.load_request_timeout_seconds.value='7';
form.elements.load_patterns_poisson_enabled.checked=true;form.elements.load_patterns_poisson_seed.value='37';
form.elements.load_patterns_bursts_enabled.checked=true;form.elements.load_patterns_bursts_first_seconds.value='1';form.elements.load_patterns_bursts_size.value='2';
form.elements.load_profile_long_input_tokens.value='10000';form.elements.load_profile_long_output_tokens.value='1024';form.elements.load_workloads_stress_min_output_percent.value='80';
form.elements.load_pass_criteria_min_valid_responses.value='1';form.elements.load_pass_criteria_max_first_answer_ms.value='';
const read=readForm(false).config;state.config=structuredClone(read);mount();assert.deepEqual(readForm(false).config,read);
console.log(JSON.stringify(read));
''')
    config=RunConfig.model_validate(result)
    assert not config.load_test.workloads.combinations.enabled
    assert config.load_test.workloads.mixed.enabled and config.load_test.workloads.stress.enabled
    assert config.load_test.workloads.mixed.shares['long_long']==60
    assert config.load_test.workloads.stress.cases==['long_short','short_long','long_long']
    assert config.load_test.profile.long.input_tokens==10000
    assert config.load_test.profile.long.output_tokens==1024
    assert config.load_test.patterns.concurrent.levels==[2,4]
    assert config.load_test.patterns.poisson.seed==37 and config.load_test.patterns.bursts.enabled
    assert config.load_test.request_timeout_seconds==7
    assert config.load_test.pass_criteria.min_valid_responses==1
    assert config.load_test.pass_criteria.max_first_answer_ms is None


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
assert.equal(result.sessions.concurrency,2);assert.equal(result.sessions.workloads.length,2);
assert.deepEqual(result.sessions.modes,['live']);
assert.equal(result.sessions.workloads[0].turns[1],'Custom follow-up with <literal> characters.');
assert.equal(result.sessions.workloads[0].max_tokens,64);
assert.equal(source.sessions,undefined);
values.set('session_enabled_0','off');values.set('session_enabled_1','off');
assert.throws(()=>configureSessions(source,values,defaults),/Select at least/);
assert.equal(configureSessions(result,new FormData(),defaults).sessions.profile,'performance');
const legacy=new FormData();legacy.set('sessions_profile','correctness');
const bounded=configureSessions({{...source,concurrency:1,max_output_tokens:16}},legacy,defaults);
assert.equal(bounded.sessions,undefined); // Viewing the default profile keeps null settings implicit.
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
                       concurrent_load={"schedule": {"levels": [1]}}, sessions={"sessions": 3},
                       consistency={"repetitions": 8},
                       test_options={"correctness": {"samples": 9},
                                     "recovery": {"sustained_seconds": 2}})
    inherited = config.model_copy(update={"test_options": {}})
    context = {"source": config.model_dump(mode="json"), "checks": [
        {"id": name, "option_fields": sorted(CHECK_OPTION_FIELDS.get(name, set())),
         "limit_fields": sorted(CHECK_LIMIT_FIELDS.get(name, set())),
         "defaults": inherited.effective_check(name)} for name in CHECK_NAMES]}
    module = (Path(__file__).parents[1] / "src/giraffe/static/form-config.mjs").as_uri()
    script = f"""
import assert from 'node:assert/strict';
import {{configureTests, configureLoadTest, loadFieldName, optionValue, limitValue, sameConfig}} from {json.dumps(module)};
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
function loadValues() {{
 const s=source.load_test;values.set('load_rendered','1');
 function walk(row,path=''){{for(const [key,value] of Object.entries(row)){{const p=path?path+'.'+key:key,name=loadFieldName(p);
  if(Array.isArray(value)){{if(p.endsWith('.selected')||p.endsWith('.cases'))for(const shape of value)values.set(name+'_'+shape,'on');else if(!value.length||typeof value[0]!=='object')values.set(name,value.join(', '));}}
  else if(value&&typeof value==='object')walk(value,p);else values.set(name,typeof value==='boolean'?(value?'on':'off'):value??'');
 }}}}walk(s);
}}
const build=()=>configureLoadTest(configureTests(source,values,checks,defaults),values,{json.dumps(LoadTest().model_dump(mode="json"))});
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
    assert.deepEqual(result.traffic.rates,source.traffic.rates); // Retired traffic controls cannot edit Load test.
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
    loadValues();values.set('load_pass_criteria_max_completion_ms','');values.set('load_pass_criteria_max_first_answer_ms','500');
    const result=build();assert.equal(result.load_test.pass_criteria.max_completion_ms,null);assert.equal(result.load_test.pass_criteria.max_first_answer_ms,500);
    assert.equal(result.limits.latency_ms,1000);
    assert.deepEqual(configureLoadTest(result,values,{json.dumps(LoadTest().model_dump(mode="json"))}).load_test,result.load_test);
    break;
  }}
  case 'traffic': {{
    loadValues();values.set('load_patterns_rates','2, 5, 8');values.set('load_patterns_poisson_enabled','on');values.set('load_patterns_poisson_seed','17');
    let result=build();assert.deepEqual(result.load_test.patterns.rates,[2,5,8]);assert.equal(result.load_test.patterns.poisson.enabled,true);assert.equal(result.load_test.patterns.poisson.seed,17);
    assert.equal(result.load_test.patterns.max_in_flight,undefined);assert.equal(result.load_test.patterns.max_pending,undefined);
    assert.deepEqual(configureLoadTest(result,values,{json.dumps(LoadTest().model_dump(mode="json"))}).load_test,result.load_test);
    values.set('load_patterns_rates','2, Infinity');assert.throws(build,/positive, increasing/);
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
    for(const key of ['arrivals','prefix','concurrency','sessions','consistency'])assert.deepEqual(result[key],source[key]);
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
state.config={{samples:6,default_concurrency:3,concurrency:4,limits:{{latency_ms:1000,first_output_ms:null}}}};
state.bootstrap={{checks:[
  {{id:'correctness',option_fields:['samples','concurrency'],limit_fields:[],defaults:{{}}}},
  {{id:'capacity',option_fields:[],limit_fields:['latency_ms','first_output_ms'],defaults:{{}}}}
]}};
refreshInheritedFields('correctness',true);
refreshInheritedFields('capacity',true);
assert.equal(elements.test_correctness_samples.value,'9');
assert.equal(elements.test_correctness_concurrency.value,3);
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
assert.match(markup,/Arrival stage evidence/);
assert.doesNotMatch(markup,/0 recorded metric comparisons|0 comparable metrics|No metric rows|comparison-filters/);
target.comparisons.push({{check:'correctness',metric:'error_rate',status:'pass'}});
markup=renderComparison(report);
assert.match(markup,/<th>Metric<\\/th>/);
assert.match(markup,/Error rate/);
assert.match(markup,/no material change/);
target.configuration_diff={{confounded:true,changed_fields:['runtime.version']}};
target.telemetry={{coverage:'partial'}};
markup=renderComparison(report);
assert.match(markup,/runtime.version|confounded experiment/);
assert.match(markup,/Deployment and comparison details/);
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
  {{id:'first_output',title:'Capacity',description:'Test traffic',option_fields:[],limit_fields:['latency_ms'],defaults:{{limits:{{latency_ms:null}}}}}},
  {{id:'consistency',title:'Bucket coverage',description:'Test levels',option_fields:[],limit_fields:[],defaults:{{}}}}];
const traffic={{rates:[1,2,4],arrival:'steady',duration_seconds:5,mix:{{short:.6,long_input:.2,long_output:.2}}}};
for(const id of ['correctness','first_output','consistency']){{
  const fieldset={{disabled:true}}, selection={{checked:id==='first_output'}}, status={{textContent:''}}, draft={{hidden:true}};
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
      if(q==='[data-test-settings]'&&['test','bucket'].includes(section))return {{dataset:{{testSettings:id}}}};
      if(q==='[data-concurrent-settings]'&&section==='bucket')return {{}};
      if(q==='[data-traffic-settings]'&&section==='traffic')return {{}};
      return null;}},checkValidity(){{return this.valid;}},reportValidity(){{this.reported=true;}}}};
  elements[name]=item;return item;
}}
const samples=input('test_correctness_samples','correctness','test','6');
const latency=input('test_first_output_limit_latency_ms','first_output','test','');
const rates=input('traffic_rates','first_output','traffic','1, 2, 4');
const levels=input('test_consistency_concurrency','consistency','bucket','1, 8');
const form={{elements,querySelectorAll:()=>[samples,latency,rates,levels]}};
const document={{addEventListener:(kind,fn)=>listeners[kind]=fn,querySelectorAll:()=>[],querySelector(q){{
  if(q==='#new-run-form')return form;
  const card=q.match(/^\[data-test-card="(.*?)"\]$/);if(card)return cards[card[1]];
  const trigger=q.match(/^\[data-configure-test="(.*?)"\]$/);if(trigger)return cards[trigger[1]].trigger;
}}}};
const source=fs.readFileSync({json.dumps(str(static / 'app.js'))},'utf8')
  .replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
const context={{structuredClone,optionDefault,optionValue,limitValue,sameConfig,
  document,window:{{addEventListener(){{}}}},assert,checks,traffic,cards,elements,form,samples,latency,rates,levels,listeners}};
vm.runInNewContext(source+`
state.bootstrap={{checks,config:{{traffic}},concurrent_load:{{defaults:{json.dumps(ConcurrentLoad().model_dump())}}},option_schema:{{properties:{{samples:{{type:'integer',minimum:1}}}}}},
 limits_schema:{{properties:{{latency_ms:{{anyOf:[{{type:'number'}},{{type:'null'}}]}}}}}},
 traffic_schema:{{properties:{{rates:{{type:'array'}},arrival:{{enum:['steady','poisson']}}}},$defs:{{TrafficMix:{{properties:{{short:{{type:'number'}},long_input:{{type:'number'}},long_output:{{type:'number'}}}}}}}}}}}};
state.config={{checks:['first_output'],targets:[{{}}],samples:6,concurrency:8,max_output_tokens:128,traffic,limits:{{latency_ms:null,min_samples:5}},test_options:{{correctness:{{samples:9}}}}}};
state.catalogueFilter='all';
syncConfigView=()=>{{}};
updateBounds=()=>{{}}; // The DOM mock exercises disclosure and input behavior, not report markup.
let markup=renderTestCard(checks[0],state.config);
assert.match(markup,/aria-expanded="false"/);
assert.match(markup,/data-test-editor="correctness" hidden/);
assert.match(markup,/type="hidden" name="test_mode_correctness" value="custom"/);
assert.doesNotMatch(markup,/<select name="test_mode_/);
markup=renderTestCard(checks[1],state.config);
assert.doesNotMatch(markup,/name="traffic_rates"/);
assert(markup.includes('name="test_first_output_limit_latency_ms"'));
assert.doesNotMatch(markup,/<select name="traffic_mode"/);
syncTestControls();
assert.equal(cards.correctness.hidden,false);
assert.equal(cards.first_output.hidden,false);
state.catalogueFilter='selected';syncTestControls();assert.equal(cards.correctness.hidden,true);
state.catalogueFilter='all';syncTestControls();
assert.equal(cards.correctness.hidden,false);
assert.equal(cards.correctness.selection.checked,false);
state.catalogueFilter='all';syncTestControls();
assert.equal(cards.correctness.hidden,false);
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
toggleTestEditor('first_output');
assert.equal(cards.correctness.editor.hidden,true);
assert.equal(cards.correctness.fieldset.disabled,true);
assert.equal(samples.value,'9');
assert.equal(cards.first_output.editor.hidden,false);
rates.value='2, 5';editTestField(rates);
assert.equal(elements.traffic_mode.value,'custom');
toggleTestEditor('first_output',true);
assert.equal(cards.first_output.trigger.focused,true);
assert.equal(cards.first_output.editor.hidden,true);
assert.equal(rates.value,'2, 5');
toggleTestEditor('correctness');
assert.equal(samples.value,'9');
listeners.click({{target:{{closest(q){{return q==='[data-reset-test]'?{{dataset:{{resetTest:'correctness'}}}}:null;}}}}}});
assert.equal(elements.test_mode_correctness.value,'default');
assert.equal(samples.value,6);
assert.equal(samples.dataset.inherited,'true');
assert.equal(elements.traffic_mode.value,'custom');
// Labels reflect serialized overrides, not sticky edit flags, even after closing/reopening.
const effective=structuredClone(state.config);effective.checks=['correctness','first_output'];
effective.test_options={{}};
assert.equal(testSettingsLabel(checks[0],effective),'');
assert.equal(testSettingsLabel(checks[1],effective),'');
effective.test_options.correctness={{samples:9}};
assert.equal(testSettingsLabel(checks[0],effective),'Overrides');
effective.checks=['first_output'];assert.equal(testSettingsLabel(checks[0],effective),'Saved overrides');
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
samples.valid=true;cards.correctness.selection.checked=false;
levels.valid=false;
elements.test_mode_consistency.value='custom';
assert.equal(validateRunForm(form),true);
cards.consistency.selection.checked=true;
assert.equal(validateRunForm(form),false);
assert.equal(state.openTest,'consistency');
assert.equal(cards.consistency.fieldset.disabled,false);
assert.equal(levels.reported,true);
`,context);
"""
    result = subprocess.run([node, "--input-type=module", "-"], input=script,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_form_json_views_validate_one_snapshot_and_preserve_unapplied_drafts():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute browser configuration behavior")
    from giraffe.capabilities import capabilities

    static = Path(__file__).parents[1] / "src/giraffe/static"
    script = rf"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {{positiveBoundErrors}} from {json.dumps((static / 'form-config.mjs').as_uri())};
const nodes={{}},listeners={{}},downloads=[];
for(const id of ['config-tab-form','config-tab-json','config-panel-form','config-panel-json',
  'config-draft-notice','config-editor','json-status','config-error','cli-fallback','cli-command','execution-settings']){{
  nodes['#'+id]={{value:'',hidden:false,classList:{{toggle(){{}}}},setAttribute(k,v){{this[k]=v;}},focus(){{this.focused=true;}},select(){{this.selected=true;}}}};
}}
const form={{elements:{{baseline_id:{{value:'before'}},mode:{{value:'normal'}},restart_target:{{value:'local'}},model:{{focus(){{this.focused=true;}}}}}}}};
nodes['#new-run-form']=form;
nodes['[data-action="use-request-budget"]']={{disabled:false}};
const document={{querySelector:q=>nodes[q],querySelectorAll:()=>[],addEventListener:(kind,fn)=>listeners[kind]=fn,
  createElement:()=>({{click(){{this.clicked=true;}}}})}};
const context={{positiveBoundErrors,structuredClone,document,window:{{addEventListener(){{}}}},assert,nodes,form,listeners,
  Blob,URL:{{createObjectURL(blob){{downloads.push(blob);return 'blob:config';}},revokeObjectURL(){{}}}},
  navigator:{{clipboard:{{async writeText(){{throw new Error('denied');}}}}}},setTimeout(){{}},downloads}};
const source=fs.readFileSync({json.dumps(str(static / 'app.js'))},'utf8')
  .replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
await vm.runInNewContext(source+`
state.bootstrap={json.dumps(capabilities())};
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
assert.equal(nodes['[data-action="use-request-budget"]'].disabled,true);
switchConfigView('json');assert.equal(state.jsonDraft,'{{ invalid');
assert.throws(assertAppliedConfig,/Apply or discard/);
let calls=0;api=async()=>{{calls++;return {{config:{{}}}};}};
const before=JSON.stringify(state.config);
await applyConfig();assert.equal(calls,0);assert.equal(JSON.stringify(state.config),before);
assert.equal(state.jsonDraft,'{{ invalid');assert.equal(nodes['#config-error'].hidden,false);
await exportConfig();await copyCli();
assert.equal(calls,0);assert.equal(downloads.length,0);
discardConfig();assert.equal(state.jsonDraft,base);assert.equal(nodes['#config-panel-form'].inert,false);
assert.equal(nodes['[data-action="use-request-budget"]'].disabled,true); // Discarding JSON still requires a refreshed plan.
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
// The shared review action updates whichever configuration view is visible.
state.configView='json';state.jsonDraft=state.jsonBase;
form.elements.max_requests={{value:'1'}};
readForm=()=>({{config:{{...current.config,max_requests:Number(form.elements.max_requests.value)}},mode:'normal'}});
updateBounds=()=>{{}};
const budgetEvent={{target:{{closest(q){{
  if(q==='[data-action]')return {{dataset:{{action:'use-request-budget'}}}};
  if(q==='[data-budget]')return {{dataset:{{budget:'16'}}}};
  return null;
}}}}}};
state.requestPreview=null;await listeners.click(budgetEvent);
assert.equal(form.elements.max_requests.value,'1');assert.match(nodes['#config-error'].textContent,/current request plan/);
state.requestPreview={{status:'current',revision:state.configRevision,route:state.routeVersion,plan:{{request_limit:2,required_request_limit:16,request_warnings:[{{actions:[{{field:'max_requests',value:16}}]}}]}}}};
await listeners.click(budgetEvent);assert.equal(form.elements.max_requests.value,'1');assert.match(nodes['#config-error'].textContent,/plan changed/);
state.requestPreview.plan.request_limit=1;
await listeners.click(budgetEvent);
assert.equal(JSON.parse(nodes['#config-editor'].value).max_requests,16);
assert.equal(state.jsonDraft,state.jsonBase);
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
    from giraffe.capabilities import capabilities

    static = Path(__file__).parents[1] / "src/giraffe/static"
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {positiveBoundErrors} from MODULE_PATH;
const nodes={},listeners={};
for(const id of ['request-pack-preview','config-error','start-run','form-error','request-pack-file'])nodes['#'+id]={hidden:true,innerHTML:'',textContent:'',click(){this.clicked=true;}};
const form={elements:{request_source:{value:'builtin'}}};nodes['#new-run-form']=form;
const context={assert,positiveBoundErrors,structuredClone,nodes,form,fetch:async()=>({ok:false,status:404,json:async()=>({detail:'Not Found'})}),document:{querySelector:q=>nodes[q],querySelectorAll:()=>[],addEventListener:(kind,fn)=>listeners[kind]=fn},window:{addEventListener(){}},location:{hash:'#new'}};
const source=fs.readFileSync(APP_PATH,'utf8').replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
await vm.runInNewContext(source+`
state.bootstrap=BOOTSTRAP;
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
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan:{}};
api=async(path,options)=>{const config=JSON.parse(options.body);sent.push({path,config});if(path==='/api/runs')return {id:'done'};return {config,request_pack_preview:preview};};
await startRun({preventDefault(){},target:form});
assert.equal(sent.map(x=>x.path).join(','),'/api/config/validate,/api/config/preview,/api/runs');
assert.equal(JSON.stringify(sent[1].config),JSON.stringify(sent[2].config.config));
// Edits while the pre-run preview is pending prevent all inference calls.
state.active=null;sent.length=0;state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan:{}};api=async(path,options)=>{sent.push(path);if(path==='/api/config/preview')return new Promise(done=>resolve=done);return {config:JSON.parse(options.body)};};
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
""".replace("MODULE_PATH", json.dumps((static / "form-config.mjs").as_uri())).replace("BOOTSTRAP", json.dumps(capabilities())).replace("APP_PATH", json.dumps(str(static / "app.js")))
    result = subprocess.run([node, "--input-type=module", "-"], input=script,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_ai_judge_configuration_evidence_and_evaluation_phase():
    from giraffe.capabilities import capabilities

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
const context={assert,structuredClone,nodes,config,draft,document:{querySelector:q=>nodes[q],querySelectorAll:()=>[],addEventListener(){}},window:{addEventListener(){}},location:{hash:'#new'}};
const source=fs.readFileSync(APP_PATH,'utf8').replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
context.bootstrap=BOOTSTRAP;
vm.runInNewContext(source+`
state.bootstrap=bootstrap;
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
renderLive();markup=nodes['#main'].innerHTML;assert.match(markup,/Evaluating answers/);assert.match(markup,/Target traffic finished/);assert.match(markup,/Evaluation calls/);assert.match(markup,/7.*40/s);assert.match(markup,/data-action="stop"/);assert(!markup.includes('Measuring the configured endpoint'));
state.detail.progress={event:'request_finished',attempted:20,completed:19};renderLive();assert(nodes['#main'].innerHTML.includes('<h1>Running</h1>'));assert(nodes['#main'].innerHTML.includes('One spot per test. Select a spot for its requests and results.'));
`,context);
""".replace("APP_PATH", json.dumps(str(static / "app.js"))).replace(
        "FORM_PATH", json.dumps((static / "form-config.mjs").as_uri()))
    script=script.replace("BOOTSTRAP", json.dumps(capabilities()))
    result = subprocess.run([node, "--input-type=module", "-"], input=script,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_dataset_editor_preserves_canonical_cases_and_stages_imports():
    from giraffe.capabilities import capabilities

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
state.bootstrap=BOOTSTRAP;
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
assert(state.datasetImport.mappingDirty);assert.match(nodes['#dataset-dialog'].innerHTML,/Columns changed/);
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
openTryExample(0);markup=nodes['#dataset-dialog'].innerHTML;assert.match(markup,/Sends this question once/);assert.match(markup,/No traffic stages or restart/);assert.match(markup,/Save this trial/);assert.match(markup,/name="save_try_output" type="checkbox" checked/);assert.match(markup,/does not change what your main run saves/);
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
    script = script.replace("BOOTSTRAP", json.dumps(capabilities()))
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
const context={assert,nodes,structuredClone,convertExpectedAnswer,datasetCaseFromValues,FormData,document:{querySelector:q=>nodes[q],querySelectorAll:()=>[],addEventListener(){}},window:{addEventListener(){}}};
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
record.evaluation={messages:[],scorer:'json',expected:{purpose:'<semantic target>'},expected_provided:true};
html=renderEvaluationEvidence(record,{});assert.match(html,/Expected answer/);assert.match(html,/&lt;semantic target&gt;/);assert(!html.includes('<h3>Purpose</h3>'));assert(!html.includes('[object Object]'));
record.evaluation={messages:[],scorer:'none',expected:{purpose:'observe <sustained> answer generation'},expected_provided:true};
html=renderEvaluationEvidence(record,{});assert.match(html,/No answer check/);assert(html.includes('<h3>Purpose</h3>'));assert.match(html,/observe &lt;sustained&gt; answer generation/);assert(!html.includes('Expected answer'));assert(!html.includes('[object Object]'));assert(!html.includes('<sustained>'));
record.evaluation={messages:[],scorer:'none',expected:'unused reference',expected_provided:true};
html=renderEvaluationEvidence(record,{});assert.match(html,/No answer check/);assert.doesNotMatch(html,/Expected answer|unused reference/);
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
    from giraffe.capabilities import capabilities

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
  document:{querySelector:q=>nodes[q],querySelectorAll:()=>[],addEventListener(){}},
  window:{addEventListener(){}},location:{hash:'#new'}};
const source=fs.readFileSync(APP_PATH,'utf8').replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
context.bootstrap=BOOTSTRAP;
await vm.runInNewContext(source+`
(async()=>{
state.bootstrap=bootstrap;
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
    script=script.replace("BOOTSTRAP", json.dumps(capabilities()))
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


def test_tool_profile_serializer_preserves_custom_data_and_invalid_drafts():
    from tests.test_ux_catalogue import browser_logic

    browser_logic(r"""
config.tool_checks={fixtures:[{fixture_id:'custom',messages:[{role:'user',content:'ask'}],tools:[{custom:true}],expected_calls:[{name:'call',arguments:{x:1}}],options:{temperature:0}}],modes:['stream','complete']};
const original=JSON.stringify(config),values=new Map();
assert.equal(JSON.stringify(configureProfiles(config,values,{},{limits:bootstrap.limits})),original);
values.set('profile_edits_tools','["forced_tool_diagnostic"]');values.set('profile_tools_forced_tool_diagnostic','true');
const forced=configureProfiles(config,values,{},{limits:bootstrap.limits});assert.equal(JSON.stringify(forced.tool_checks),JSON.stringify(config.tool_checks));assert(forced.forced_tool_diagnostic);
values.set('profile_edits_tools','["modes"]');values.set('profile_tools_modes','complete');
const complete=configureProfiles(config,values,{},{limits:bootstrap.limits});assert.equal(JSON.stringify(complete.tool_checks.fixtures),JSON.stringify(config.tool_checks.fixtures));assert.equal(complete.tool_checks.modes.join(','),'complete');
values.set('profile_tools_modes','');assert.equal(configureProfiles(config,values,{},{limits:bootstrap.limits}).tool_checks.modes,null);
values.set('profile_edits_tools','["fixtures"]');values.set('profile_tools_fixtures','[{"unfinished"');
assert.throws(()=>configureProfiles(config,values,{},{limits:bootstrap.limits}),/JSON array/);
const draft=configureProfiles(config,values,{}, {allowInvalid:true});assert.equal(draft.tool_checks.fixtures,'[{"unfinished"');
assert.throws(()=>configureProfiles(draft,new Map(),{},{limits:bootstrap.limits}),/JSON array/);
for(const raw of ['null','{}','[null]','[[]]','[1]','[true]',JSON.stringify(Array(17).fill({})),' '.repeat(65537)]){
  values.set('profile_tools_fixtures',raw);assert.throws(()=>configureProfiles(config,values,{},{limits:bootstrap.limits}));
}
values.set('profile_tools_fixtures','[]');assert.equal(configureProfiles(config,values,{},{limits:bootstrap.limits}).tool_checks.fixtures.length,0);
assert.equal(JSON.stringify(config),original);
config.tool_calling=true;config.stream=false;
assert.throws(()=>configureProfiles(config,new Map(),{},{limits:bootstrap.limits}),/Enable streaming/);
config.tool_calling=false;assert.doesNotThrow(()=>configureProfiles(config,new Map(),{},{limits:bootstrap.limits}));
""")
