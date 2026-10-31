"""Catalogue behavior, honest summaries and lossless specialist settings."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from giraffe.capabilities import capabilities
from giraffe.fixtures import builtin_fixtures
from giraffe.models import RunConfig


STATIC = Path(__file__).parents[1] / "src/giraffe/static"


def browser_logic(body, config=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise catalogue behavior")
    config = config or RunConfig(targets=[{
        "name": "local", "url": "http://localhost/v1", "model": "test",
    }], checks=["access", "correctness"])
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as formConfig from MODULE_PATH;
const nodes={},groups={},listeners={},storage=new Map();
const context={assert,structuredClone,FormData,AbortController,...formConfig,nodes,groups,listeners,console,
  sessionStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
  document:{querySelector:q=>nodes[q],querySelectorAll:q=>groups[q] || [],addEventListener:(event,fn)=>listeners[event]=fn},
  window:{addEventListener(){}},location:{hash:'#new'},setTimeout(){},clearTimeout(){}};
context.bootstrap=BOOTSTRAP;context.config=CONFIG;
const source=fs.readFileSync(APP_PATH,'utf8').replace(/^import .*?;\n/,'').replace(/\ninit\(\);\s*$/,'');
await vm.runInNewContext(source+'\nstate.bootstrap=bootstrap;state.bootstrap.config=config;state.config=config;\n(async()=>{'+BODY+'\n})()',context);
"""
    substitutions = {
        "MODULE_PATH": json.dumps((STATIC / "form-config.mjs").as_uri()),
        "APP_PATH": json.dumps(str(STATIC / "app.js")),
        "BOOTSTRAP": json.dumps(capabilities()),
        "CONFIG": json.dumps(config.model_dump(mode="json")),
        "BODY": json.dumps(body),
    }
    # Replace template tokens once; injected constants can contain CONFIG/BODY.
    script = re.sub(r"\b(?:MODULE_PATH|APP_PATH|BOOTSTRAP|CONFIG|BODY)\b",
                    lambda match: substitutions[match[0]], script)
    result = subprocess.run([node, "--input-type=module", "-"], input=script,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


def test_all_checks_have_local_settings_and_one_question_source():
    browser_logic(r"""
const original=JSON.stringify(config),markup=renderCheckSelection(config);
assert.equal((markup.match(/data-test-card="/g)||[]).length,15);
for(const check of bootstrap.checks){
  assert.equal((markup.match(new RegExp('data-test-card="'+check.id+'"','g'))||[]).length,1);
  const row=renderTestCard(check,config);assert.match(row,/<dt>Sends<\/dt>/);assert.match(row,/<dt>Shows<\/dt>/);
}
for(const group of checkGroups)assert(markup.includes('data-action="catalogue-category" data-category="'+group.id+'"'));
assert.equal((markup.match(/id="setup-dataset"/g)||[]).length,1);
assert.match(renderTestCard(bootstrap.checks.find(x=>x.id==='correctness'),config),/id="setup-dataset"/);
const capacity={...config,checks:['load']};
assert.match(renderTestCard(bootstrap.checks.find(x=>x.id==='load'),capacity),/id="setup-dataset"/);
assert.doesNotMatch(renderCheckSelection({...config,checks:['access']}),/id="setup-dataset"/);
for(const name of ['load_patterns_rates','load_schedule_duration_seconds','load_patterns_concurrent_levels','load_profile_short_input_tokens','profile_sessions_concurrency']){
  assert.equal((markup.match(new RegExp('name="'+name+'"','g'))||[]).length,1,name);
}
assert.doesNotMatch(markup,/name="setup_goal"|name="setup_load"/);
assert.match(markup,/Correct answers \(%\)/);assert.match(markup,/<h4>Requests<\/h4>/);assert.match(markup,/<h4>Pass criteria<\/h4>/);
assert.equal(JSON.stringify(config),original);
""")


def test_saved_questions_summary_does_not_claim_inactive_sample_settings():
    browser_logic(r"""
const check=bootstrap.checks.find(x=>x.id==='correctness');
config.request_source='request_pack';config.request_pack={cases:Array.from({length:12},()=>({}))};
config.test_options.correctness={samples:4,concurrency:2,limits:{min_correctness:.95}};
const summary=effectiveCheckSummary(check,config);
assert.match(summary,/12 saved questions · each once/);assert.match(summary,/95% correct required/);
assert.doesNotMatch(summary,/sample requests|at a time/);
config.request_pack.cases.length=1;assert.match(effectiveCheckSummary(check,config),/1 saved question · each once/);
""")


def test_check_rows_keep_details_in_accessible_info_and_update_live_settings():
    browser_logic(r"""
for(const check of bootstrap.checks){
  const row=renderTestCard(check,config),helpId='field-check_'+check.id+'-help';
  assert(row.includes('<p class="test-description">'+escape(checkGuide[check.id].purpose)+'</p>'));
  assert.doesNotMatch(row,/test-effective-summary|traffic-profile-summary|check-explanation-disclosure/);
  assert.match(row,new RegExp('</label><button type="button" class="field-info"[^>]*popovertarget="'+helpId+'"'));
  assert(row.includes('aria-label="About '+escape(catalogTitle(check.id))+'"'));
  assert(row.includes('id="'+helpId+'" class="field-help" popover="auto"'));
  assert.match(row,/<dt>Sends<\/dt>/);assert.match(row,/<dt>Shows<\/dt>/);
  assert.match(row,/<dt>Settings<\/dt><dd data-effective-settings>/);
  assert.equal((row.match(/data-effective-settings/g)||[]).length,1);
}
const check=bootstrap.checks.find(item=>item.id==='correctness');
const status={},effective={};const card={querySelector:selector=>({'[data-setting-state]':status,'[data-effective-settings]':effective})[selector]};
updateCheckRow(check,config,card);assert.match(effective.textContent,/At least 6 questions/);
config.test_options.correctness={samples:9,limits:{min_correctness:.95}};
updateCheckRow(check,config,card);assert.match(effective.textContent,/At least 9 questions/);assert.match(effective.textContent,/95% correct required/);
assert.equal(status.textContent,'Overrides');
config.checks=[];updateCheckRow(check,config,card);
assert.equal(status.textContent,'Saved overrides');assert.match(effective.textContent,/At least 9 questions/);
assert.equal(effective.hidden,undefined); // Info stays available for checks not selected.
assert.equal(catalogTitle('access'),'Endpoint response');
assert.equal(catalogTitle('prefix'),'Prompt cache evidence');
""")


def test_catalogue_filters_keep_selection_and_settings():
    browser_logic(r"""
const original=JSON.stringify(config),cards=[];
nodes['#new-run-form']={elements:{metrics_url:{}}};nodes['#catalogue-empty']={};nodes['#check-search']={value:''};
syncPackControls=()=>{};
for(const check of bootstrap.checks){
  const controls={'input[name="check"]':{checked:config.checks.includes(check.id)},'[data-configure-test]':{setAttribute(){}},
    '.draft-notice':{},'[data-effective-settings]':{},'[data-test-editor]':{querySelectorAll:()=>[]}};
  const card={id:check.id,hidden:false,classList:{toggle(){}},querySelector:q=>controls[q],
    dataset:{category:checkGroups.find(x=>x.checks.includes(check.id)).id,search:(checkGuide[check.id].name+' '+checkGuide[check.id].purpose).toLowerCase()}};
  nodes['[data-test-card="'+check.id+'"]']=card;cards.push(card);
}
syncTestControls();assert.equal(cards.filter(x=>!x.hidden).length,15);
state.catalogueFilter='all';syncTestControls();assert.equal(cards.filter(x=>!x.hidden).length,15);
state.catalogueCategory='server';syncTestControls();assert.equal(cards.filter(x=>!x.hidden).map(x=>x.id).sort().join(','),'cancellation,gpu,prefix');
state.catalogueCategory='all';state.catalogueFilter='selected';syncTestControls();assert.equal(cards.filter(x=>!x.hidden).length,2);
// Browse filters must not hide selected checks when returning to the run.
state.catalogueCategory='server';state.catalogueSearch='gpu';syncTestControls();assert.equal(cards.filter(x=>!x.hidden).length,2);
state.catalogueCategory='all';
state.catalogueFilter='all';state.catalogueSearch='concurrent conversations';syncTestControls();assert.equal(cards.filter(x=>!x.hidden)[0].id,'sessions');
revealCatalogueCheck('prefix');assert.equal(state.openTest,'prefix');assert.equal(cards.filter(x=>!x.hidden).length,15);
assert.equal(JSON.stringify(config),original);
""")


def test_dataset_validation_and_evaluator_reveal_the_hosting_check():
    browser_logic(r"""
nodes['#check-search']={value:'gpu'};nodes['[data-action="add-example"]']={focus(){this.focused=true;}};
const details={open:false};nodes['.judge-configuration']=details;
nodes['[name="judge_0_url"]']={focus(){this.focused=true;}};
syncTestControls=()=>{};revealSetupStep=step=>state.setupStep=step;
readForm=()=>({config:state.config});state.config.request_source='request_pack';state.config.request_pack={cases:[]};
state.catalogueCategory='server';state.catalogueFilter='selected';state.catalogueSearch='gpu';
assert.throws(()=>validateSetupChecks(),/Add a question/);
assert.equal(state.openTest,'correctness');assert.equal(state.setupStep,2);assert.equal(state.catalogueCategory,'all');
assert(nodes['[data-action="add-example"]'].focused);
state.config.checks=['load'];state.config.judging={judges:[{}]};state.openTest=null;state.setupStep=3;
configureEvaluator();assert.equal(state.openTest,'load');assert.equal(state.setupStep,2);assert(details.open);
assert(nodes['[name="judge_0_url"]'].focused);
state.config.judging=null;let added=false;changeJudge=action=>{added=action==='add';assert.equal(state.openTest,'load');};
state.openTest=null;configureEvaluator();assert(added);
""")


def test_browse_warnings_follow_selection_and_open_draft_without_losing_values():
    browser_logic(r"""
config.limits.min_samples=5;config.max_output_tokens=32;
config.load_test=structuredClone(bootstrap.load_test.defaults);config.load_test.patterns.concurrent.levels=[1];
config.consistency={...bootstrap.profile_defaults.consistency,repetitions:4,levels:[1,2],shapes:['short','long']};
config.test_options.generation={max_output_tokens:64};
state.catalogueFilter='all';state.openTest=null;
const warnings={},selections={};
groups['[data-check-override-warning]']=[];groups['[data-check-coverage]']=[];
nodes['#new-run-form']={elements:{metrics_url:{}}};syncPackControls=()=>{};
for(const check of bootstrap.checks){
  const override={dataset:{checkOverrideWarning:check.id},textContent:checkOverrideWarning(check,config,true)};
  const coverage={dataset:{checkCoverage:check.id},textContent:checkCoverageWarning(check,config)};
  const selection={checked:config.checks.includes(check.id)};
  groups['[data-check-override-warning]'].push(override);groups['[data-check-coverage]'].push(coverage);
  warnings[check.id]={override,coverage};selections[check.id]=selection;
  const controls={'input[name="check"]':selection,'[data-configure-test]':{setAttribute(){}},'.draft-notice':{},
    '[data-test-editor]':{querySelectorAll:()=>[]},'[data-check-override-warning]':override,'[data-check-coverage]':coverage};
  nodes['[data-test-card="'+check.id+'"]']={dataset:{category:'all',search:''},classList:{toggle(){}},querySelector:selector=>controls[selector]};
}
const original=JSON.stringify(config);
for(const id of ['consistency']){
  const check=bootstrap.checks.find(item=>item.id===id),markup=renderTestCard(check,config);
  assert.match(markup,new RegExp('data-check-coverage="'+id+'" hidden'));
  assert(warnings[id].coverage.textContent.length>0);
}
syncTestControls();updateCheckFieldHints(config);
for(const id of ['load','consistency'])assert.equal(warnings[id].coverage.hidden,true);
assert.equal(warnings.generation.override.hidden,true);
// An unselected draft gets feedback only while the user is editing it.
state.openTest='consistency';syncTestControls();updateCheckFieldHints(config);
assert.equal(warnings.consistency.coverage.hidden,false);
state.openTest='generation';syncTestControls();updateCheckFieldHints(config);
assert.equal(warnings.consistency.coverage.hidden,true);assert.equal(warnings.generation.override.hidden,true); // Suite output is a default, not a ceiling.
assert.equal(checkOverrideWarning(bootstrap.checks.find(x=>x.id==='generation'),config,true),'');
state.openTest=null;syncTestControls();updateCheckFieldHints(config);
assert.equal(warnings.generation.override.hidden,true);
assert.equal(JSON.stringify(config),original);
// Selected checks keep warnings visible with their editor closed.
config.checks.push('load');selections.load.checked=true;syncTestControls();updateCheckFieldHints(config);
assert.equal(warnings.load.coverage.hidden,true); // Duration windows have no planned sample target.
config.checks=config.checks.filter(id=>id!=='load');selections.load.checked=false;
state.openTest='load';syncTestControls();assert.equal(warnings.load.coverage.hidden,true); // Duration windows have no planned sample target.
state.openTest=null;syncTestControls();updateCheckFieldHints(config);
assert.equal(warnings.load.coverage.hidden,true);
assert.equal(config.test_options.generation.max_output_tokens,64);
""")


def test_specialist_edits_preserve_profiles_and_use_backend_bounded_defaults():
    config = RunConfig(targets=[{"name": "local", "url": "http://localhost/v1", "model": "test"}],
                       checks=["access", "sessions", "consistency"], concurrency=1,
                       max_output_tokens=16)
    payload = browser_logic(r"""
const values=new Map();const original=JSON.stringify(config);
assert.equal(JSON.stringify(configureProfiles(config,values,bootstrap.profile_defaults)),original);
const saved={...config,prefix:{...bootstrap.profile_defaults.prefix,repeats:9,extra_future_setting:{keep:true}}};
values.set('profile_edits_prefix','["repeats"]');values.set('profile_prefix_repeats','10');
const patched=configureProfiles(saved,values,bootstrap.profile_defaults);
assert.equal(patched.prefix.repeats,10);assert.equal(patched.prefix.extra_future_setting.keep,true);
assert.equal(saved.prefix.repeats,9);
values.clear();values.set('profile_edits_sessions','["sessions"]');values.set('profile_sessions_sessions','3');
values.set('profile_edits_consistency','["repetitions"]');values.set('profile_consistency_repetitions','3');
const bounded=configureProfiles(config,values,bootstrap.profile_defaults);
assert.equal(effectiveProfileDefaults(bounded,bootstrap.profile_defaults,'sessions').concurrency,1);assert.equal(effectiveProfileDefaults(bounded,bootstrap.profile_defaults,'sessions').max_tokens,16);
assert.equal(effectiveProfileDefaults(bounded,bootstrap.profile_defaults,'consistency').levels.join(','),'1');assert.equal(bounded.sessions.sessions,3);
assert.equal(JSON.stringify(config),original);console.log(JSON.stringify(bounded));
""", config)
    valid = RunConfig.model_validate(payload)
    assert valid.sessions.concurrency == 1
    assert valid.sessions.max_tokens == 16
    assert valid.consistency.levels == [1]


def test_untouched_profile_fields_follow_global_limits_without_overwriting_edits():
    browser_logic(r"""
const elements={profile_edits_sessions:{value:'["sessions"]'},profile_sessions_sessions:{value:'7'},
  profile_sessions_concurrency:{value:'2'},profile_sessions_max_tokens:{value:'32'},profile_sessions_modes:{value:'fixed,live'},
  profile_edits_consistency:{value:'[]'},profile_consistency_levels:{value:'1, 2'},profile_consistency_shapes:{value:'short,long'}};
nodes['#new-run-form']={elements};
refreshDefaultProfileFields({...config,concurrency:1,max_output_tokens:16});
assert.equal(elements.profile_sessions_sessions.value,'7');
assert.equal(elements.profile_sessions_concurrency.value,1);assert.equal(elements.profile_sessions_max_tokens.value,16);
assert.equal(elements.profile_consistency_levels.value,'1');
assert.equal(elements.profile_sessions_modes.value,'fixed,live');assert.equal(elements.profile_consistency_shapes.value,'short,long');
state.config.sessions={...bootstrap.profile_defaults.sessions,concurrency:3};
elements.profile_sessions_concurrency.value='3';refreshDefaultProfileFields({...config,concurrency:1});
assert.equal(elements.profile_sessions_concurrency.value,'3');
""")


def test_percentage_inputs_and_inheritance_keep_backend_ratios():
    browser_logic(r"""
const check=bootstrap.checks.find(x=>x.id==='correctness'),values=new Map();
values.getAll=key=>key==='check'?['correctness','serving']:[];values.set('traffic_mode','default');values.set('test_mode_correctness','custom');values.set('test_mode_serving','custom');
for(const name of check.option_fields)values.set('test_correctness_'+name,String(optionDefault(config,check,name)));
for(const name of check.limit_fields)values.set('test_correctness_limit_'+name,config.limits[name]==null?'':String(config.limits[name]));
values.set('test_correctness_limit_min_correctness','95');values.set('test_serving_limit_max_error_rate','5');
const saved=configureTests(config,values,bootstrap.checks,config.traffic,{percentageInputs:true});
assert.equal(saved.test_options.correctness.limits.min_correctness,.95);
assert.equal(saved.test_options.serving.limits.max_error_rate,.05);
const elements={samples:{value:'3'}};
for(const [name] of numericFields)elements[name]={value:String(config[name])};
for(const name of check.option_fields)elements['test_correctness_'+name]={value:'old',dataset:{inherited:'true'}};
for(const name of check.limit_fields){
  elements['limit_'+name]={value:name==='min_correctness'?'98':name==='max_error_rate'?'2':config.limits[name]??''};
  elements['test_correctness_limit_'+name]={value:'old',dataset:{inherited:'true'}};
}
nodes['#new-run-form']={elements};refreshInheritedFields('correctness',true);
assert.equal(elements.test_correctness_limit_min_correctness.value,98);
assert.equal(elements.test_correctness_samples.value,3);
elements.test_correctness_limit_min_correctness.dataset.inherited='false';elements.test_correctness_limit_min_correctness.value=95;
refreshInheritedFields('correctness',true);assert.equal(elements.test_correctness_limit_min_correctness.value,95);
refreshInheritedFields('correctness');assert.equal(elements.test_correctness_limit_min_correctness.value,98);
""")


@pytest.mark.parametrize("materialized", [False, True])
def test_invalid_unselected_saved_profile_reveals_field_on_done_and_review(materialized):
    browser_logic(r"""
const fieldset={disabled:true},details={tagName:'DETAILS',open:false,parentElement:null};
const profile={dataset:{profileSettings:'prefix'}};
const input={name:'profile_prefix_prefix_chars',value:'',disabled:true,parentElement:details,
  closest(selector){return selector==='[data-profile-settings]'?profile:selector==='fieldset'?fieldset:null;},
  checkValidity(){assert.equal(this.disabled,false);assert.equal(fieldset.disabled,false);return this.value!=='';},
  focus(){this.focused=true;},reportValidity(){this.reported=true;}};
const form={elements:{profile_edits_prefix:{value:MATERIALIZED?'[]':'["prefix_chars"]'}},querySelectorAll:()=>[input]};
nodes['#new-run-form']=form;nodes['#form-error']={};nodes['#check-search']={value:'hidden row'};
state.config.checks=['access'];state.config.prefix=MATERIALIZED?{...bootstrap.profile_defaults.prefix,prefix_chars:null}:null;
state.openTest='prefix';state.catalogueFilter='selected';state.catalogueCategory='answers';state.catalogueSearch='hidden row';
syncTestControls=()=>{fieldset.disabled=false;input.disabled=false;};updateBounds=()=>{};
revealSetupStep=step=>state.setupStep=step;
toggleTestEditor('prefix',true);
assert.equal(state.openTest,'prefix');assert.equal(state.setupStep,2);assert.equal(state.catalogueFilter,'all');
assert.equal(state.catalogueCategory,'all');assert.equal(state.catalogueSearch,'');assert(details.open&&input.focused&&input.reported);
assert.match(nodes['#form-error'].textContent,/Fix this saved setting/);
state.setupStep=3;state.openTest=null;assert.equal(validateRunForm(form),false);assert.equal(state.setupStep,2);
assert.equal(input.value,''); // Recovery must not silently replace the saved draft.
input.value='1024';assert.equal(validateProfileDrafts(form),true);
""".replace("MATERIALIZED", json.dumps(materialized)))


def test_catalogue_example_is_an_actual_scored_builtin_request():
    config = RunConfig(targets=[{"name": "local", "url": "http://localhost/v1", "model": "test"}],
                       context_limit=128)
    example = builtin_fixtures(config)["correctness"][0]
    assert capabilities()["builtin_question_example"] == {
        "question": example.messages[0]["content"], "expected": example.expected,
    }


def test_run_limits_are_collapsed_and_keep_controls_without_repeating_workload():
    browser_logic(r"""
config.max_requests=160;config.max_duration_seconds=300;config.concurrency=4;
const before=JSON.stringify(config),markup=renderSharedRunSettings(config);
assert.match(markup,/<details class="shared-settings-editor" id="shared-settings">/);
assert.match(markup,/<summary class="shared-settings-summary"><h2 id="shared-settings-title">Run limits<\/h2>/);
assert.match(markup,/160 requests · 5m 0s · 4 at once/);
assert.match(markup,/<span class="shared-settings-edit" aria-hidden="true"><span class="when-closed">Edit limits<\/span><span class="when-open">Close limits<\/span><\/span>/);
assert.equal(sharedLimitsSummary({...config,max_requests:null,max_duration_seconds:null,concurrency:null}),'Requests not set · Duration not set · Simultaneous not set');
assert.equal(sharedLimitsSummary({...config,max_duration_seconds:null}),'160 requests · Duration not set · 4 at once');
assert.equal(sharedLimitsSummary({...config,max_requests:50000,max_duration_seconds:7200,concurrency:128}),'50,000 requests · 2h 0m · 128 at once');
assert.doesNotMatch(markup,/Up to|No suite limits set/);
assert(markup.lastIndexOf('</details>')<markup.indexOf('id="checks-plan-summary"'));
assert.match(markup,/class="run-ceilings-values" id="run-ceilings-summary"/);
assert.match(markup,/class="checks-plan-summary sr-only" id="checks-plan-summary"/);
assert.doesNotMatch(markup,/aria-live=/);
const expected=['max_requests','max_duration_seconds','concurrency','max_requests_enabled','max_duration_seconds_enabled','concurrency_enabled','max_duration_unit'];
for(const name of expected)assert.equal((markup.match(new RegExp('name="'+name+'"','g'))||[]).length,1,name);
let depth=0,maximumDepth=0;
for(const tag of markup.matchAll(/<(\/?)details\b[^>]*>/g)){depth+=tag[1]?-1:1;maximumDepth=Math.max(maximumDepth,depth);}
assert.equal(depth,0);assert.equal(maximumDepth,1); // One disclosure; request settings and pass criteria live in check panels.
assert.doesNotMatch(markup,/Set the limits once|run-limit-note/);
assert.doesNotMatch(markup,/name="max_memory_mb"|name="request_timeout_seconds"|Request default|Resource protection/);
assert.match(markup,/Giraffe stops sending new requests and cancels all requests still in progress/);
for(const name of ['max_requests','max_duration_seconds','concurrency'])assert.match(markup,new RegExp('id="field-'+name+'-hint"></span>')); // Empty enabled-state hint receives Not set when unchecked.
assert.match(markup,/AI evaluator calls have separate limits/); // Available through the field help.
assert.equal(JSON.stringify(config),before);
for(const id of ['run-ceilings-summary','request-defaults-summary','acceptance-summary'])nodes['#'+id]={};
config.max_requests=80;config.max_duration_seconds=12.5;config.concurrency=2;updateSharedSettingsSummaries(config);
assert.equal(nodes['#run-ceilings-summary'].textContent,'80 requests · 12.5s · 2 at once');
state.catalogueFilter='selected';const catalogue=renderCheckSelection(config);
assert(!catalogue.includes('id="shared-settings"')); // Run limits is a preceding sibling in renderNew.
assert.doesNotMatch(markup,/name="request_timeout_seconds"/);
assert.doesNotMatch(markup,/name="samples"|name="max_output_tokens"|name="context_limit"|name="stream"|name="limit_/);
assert.match(catalogue,/<div id="catalogue-browse-tools" hidden>/);
assert(catalogue.indexOf('id="catalogue-browse-tools"')<catalogue.indexOf('data-action="quick-checks"'));
assert.doesNotMatch(catalogue,/catalogue-view-hint/);
""")


def test_displayed_concurrency_defaults_are_not_saved_as_user_overrides():
    browser_logic(r"""
assert.equal(Object.hasOwn(config,'default_concurrency'),false);
assert.equal(effectiveParallelism(config),4);
const check=bootstrap.checks.find(item=>item.id==='correctness');
assert.equal(optionValue(config,check,'concurrency'),4);
const values=new FormData();values.append('check','correctness');
values.set('test_mode_correctness','custom');values.set('test_correctness_concurrency','4');
const saved=configureTests(config,values,bootstrap.checks,bootstrap.load_test.defaults);
assert.equal(Object.hasOwn(saved,'default_concurrency'),false);
assert.equal(Object.hasOwn(saved.test_options.correctness || {},'concurrency'),false);
const explicit={...config,default_concurrency:4};
assert.equal(configureTests(explicit,values,bootstrap.checks,bootstrap.load_test.defaults).default_concurrency,4);
""", RunConfig(targets=[{'name': 'local', 'url': 'http://local', 'model': 'm'}], checks=['correctness']))
