"""Per-check controls explain defaults without overwhelming the primary task."""

from giraffe.models import RunConfig
from tests.test_run_limits_phase6_ui import Markup
from tests.test_ux_catalogue import browser_logic
from tests.test_ux_results import run_render_checks


def test_run_limit_edits_refresh_all_quoted_limits_without_replacing_controls():
    browser_logic(r"""
config.checks=['access','correctness','generation','load','sessions'];
config.test_options={correctness:{concurrency:8},generation:{max_output_tokens:64},access:{request_timeout_seconds:10}};
config.sessions={profile:'performance',concurrency:2,max_tokens:512};
const elements={};
for(const [name] of numericFields)elements[name]={name,value:String(config[name]),dataset:{},selectionStart:1,selectionEnd:2};
for(const check of bootstrap.checks){
  for(const name of check.option_fields)elements[`test_${check.id}_${name}`]={value:'',dataset:{inherited:'false'}};
  for(const name of check.limit_fields)elements[`test_${check.id}_limit_${name}`]={value:'',dataset:{inherited:'false'}};
}
for(const [name,value] of Object.entries({test_correctness_concurrency:'8',test_generation_max_output_tokens:'64',test_access_request_timeout_seconds:'10',
  load_patterns_concurrent_levels:'1, 64',profile_sessions_concurrency:'2',profile_sessions_max_tokens:'512',sessions_max_tokens:'512',session_cap_0:'512'})){
  elements[name]={name,value,dataset:{inherited:'false'},selectionStart:1,selectionEnd:2};
}
const form={elements};nodes['#new-run-form']=form;
elements.test_json_concurrency={value:'4',dataset:{inherited:'true'}};
for(const id of ['selection-summary','load-guidance','run-bounds','run-bar-summary','cli-fallback','run-ceilings-summary','field-load_patterns_concurrent_levels-hint','field-traffic_max_in_flight-hint'])nodes['#'+id]={};
const hints=[['correctness','concurrency'],['generation','max_output_tokens'],['access','request_timeout_seconds']].map(([checkId,settingName])=>({dataset:{checkId,settingName,settingKind:'option'}}));
groups['[data-check-field-hint]']=hints;groups['[name^="session_cap_"]']=[elements.session_cap_0];
syncSetupIdentity=updateDatasetReadiness=updateCheckRow=renderReviewScope=renderRequestPlan=renderRequestPreview=scheduleRequestPlan=()=>{};
renderNew=()=>{throw Error('Limit edits must not replace form controls');};
readForm=()=>{const current=structuredClone(config);for(const [name] of numericFields)current[name]=Number(elements[name].value);return {config:current};};
const snapshots=['load_patterns_concurrent_levels','test_correctness_concurrency','test_generation_max_output_tokens','test_access_request_timeout_seconds','sessions_max_tokens','session_cap_0'].map(name=>[name,elements[name],elements[name].value]);
for(const [name,value] of [['concurrency','40'],['max_output_tokens','256'],['request_timeout_seconds','60']]){
  const input=elements[name];input.value=value;input.matches=()=>false;input.closest=q=>q==='#new-run-form'?form:null;
  listeners.input({target:input});assert.equal(form.elements[name],input);assert.equal(input.value,value);assert.equal(input.selectionStart,1);assert.equal(input.selectionEnd,2);
}
assert.equal(nodes['#field-load_patterns_concurrent_levels-hint'].textContent,'Used by maintained-concurrency traffic and the long-input / short-answer test.');
nodes['#load-ceiling-label']={};syncLoadDisplay(readForm().config);assert.equal(nodes['#load-ceiling-label'].textContent,40);
assert.match(nodes['#run-ceilings-summary'].textContent,/40 at once/);
assert.equal(hints[0].textContent,'Custom: 8 at a time');
assert.equal(hints[1].textContent,'Custom · run default: 256 tokens');
assert.equal(hints[2].textContent,'Custom · run default: 60 seconds');
assert.equal(bootstrap.limits['sessions.concurrency'].maximum,512);assert.equal(elements.profile_sessions_concurrency.max,String(bootstrap.limits['sessions.concurrency'].maximum)); // Preserve the session schema's own maximum.
for(const name of ['profile_sessions_max_tokens','sessions_max_tokens','session_cap_0'])assert.equal(elements[name].max,bootstrap.limits[settingPath(name)].maximum==null?undefined:String(bootstrap.limits[settingPath(name)].maximum));
assert.equal(Number(elements.test_json_concurrency.value),4);
for(const [name,input,value] of snapshots){assert.equal(form.elements[name],input);assert.equal(input.value,value);assert.equal(input.selectionStart,1);assert.equal(input.selectionEnd,2);}
elements.concurrency.value='2';listeners.input({target:elements.concurrency});assert.equal(bootstrap.limits['sessions.concurrency'].maximum,512);assert.equal(elements.profile_sessions_concurrency.max,String(bootstrap.limits['sessions.concurrency'].maximum));assert.equal(Number(elements.test_json_concurrency.value),2);
assert.equal(nodes['#field-load_patterns_concurrent_levels-hint'].textContent,'Used by maintained-concurrency traffic and the long-input / short-answer test.');assert.equal(elements.load_patterns_concurrent_levels.value,'1, 64');
""")


def test_timeout_precedes_questions_and_other_settings_with_inactive_controls_preserved():
    browser_logic(r"""
for(const id of ['correctness','load']){
  config.checks=[id];const check=bootstrap.checks.find(x=>x.id===id);
  const markup=renderTestCard(check,config);
  const timeoutName=id==='load'?'load_request_timeout_seconds':'test_correctness_request_timeout_seconds';
  assert(markup.indexOf('name="'+timeoutName+'"')<markup.indexOf('id="setup-dataset"'));
  if(id==='correctness')assert(markup.indexOf('id="setup-dataset"')<markup.indexOf('name="test_correctness_concurrency"'));
  if(id==='load')assert(markup.indexOf('id="setup-dataset"')<markup.indexOf('name="load_patterns_rates"'));
  assert.match(markup,/>Settings<\/button>/);assert.match(markup,/aria-label="Settings for /);
}
config.checks=['correctness'];config.request_source='request_pack';config.request_pack={cases:[]};
config.test_options.correctness={samples:9,concurrency:2};
const markup=renderTestCard(bootstrap.checks.find(x=>x.id==='correctness'),config);
assert.match(markup,/data-correctness-builtin-settings hidden/);
const inputs=Object.fromEntries(['samples','concurrency','max_output_tokens','request_timeout_seconds'].map(name=>['test_correctness_'+name,{value:'saved raw value'}]));
nodes['#new-run-form']={elements:inputs};syncPackControls();
for(const input of Object.values(inputs)){assert.equal(input.disabled,true);assert.equal(input.value,'saved raw value');}
assert.doesNotMatch(markup,/Add questions to calculate their execution plan|Updating the question execution plan/);
assert.doesNotMatch(markup,/per-check request-setting overrides/i);
assert.match(markup,/name="test_correctness_limit_min_correctness"/);
assert.equal(config.test_options.correctness.samples,9);
""")


def test_timeout_is_first_and_pass_targets_remain_visible():
    browser_logic(r"""
for(const [id,name] of [['access','request_timeout_seconds'],['correctness','concurrency'],['generation','max_output_tokens'],['recovery','concurrency']]){
  const markup=renderTestCard(bootstrap.checks.find(x=>x.id===id),config);
  const fields=[...markup.matchAll(/<fieldset class="fields check-request-fields"[^>]*>([\s\S]*?)<\/fieldset>/g)].map(match=>match[1]);
  assert.match(fields[0],new RegExp('name="test_'+id+'_request_timeout_seconds"'));
  assert(fields.some(group=>new RegExp('name="test_'+id+'_'+name+'"').test(group)));
  assert.equal((markup.match(new RegExp('name="test_'+id+'_'+name+'"','g'))||[]).length,1);
  assert.match(markup,new RegExp('id="field-check_'+id+'-help"[^>]*popover="auto"'));
  assert.doesNotMatch(markup,/Current value shown|Clear to inherit the shared default/);
}
const context=renderTestCard(bootstrap.checks.find(x=>x.id==='context'),config);
assert(context.indexOf('name="test_context_context_limit"')<context.indexOf('class="fields check-criteria-fields"'));
assert.match(context,/Minimum requests across the generated context cases/);
assert.match(context,/Calibration and retries can add requests/);
assert.doesNotMatch(context,/Minimum requests for each context-length group/);
assert.match(renderTestCard(bootstrap.checks.find(x=>x.id==='correctness'),config),/Minimum requests/);
const timing=renderTestCard(bootstrap.checks.find(x=>x.id==='first_output'),config);
assert.match(timing,/Minimum usable answers/);
assert(timing.indexOf('name="test_first_output_limit_min_samples"')<timing.indexOf('data-done-test="first_output"'));
""")


def test_request_timeout_is_the_first_editable_setting_in_every_applicable_panel():
    cards = browser_logic(r"""
console.log(JSON.stringify(Object.fromEntries(bootstrap.checks.map(check=>[
  check.id,renderTestCard(check,{...config,checks:[check.id]})
]))));
""")
    expected = {
        "access", "serving", "first_output", "generation", "load", "context",
        "correctness", "json", "cancellation", "recovery", "prefix", "sessions",
        "gateway_access", "consistency", "tools",
    }
    found = set()
    for check_id, html in cards.items():
        tree = Markup(html)
        editor, = [node for node in tree.nodes if node.get("data-test-editor") == check_id]
        controls = [
            node for node in tree.nodes
            if node["tag"] in {"input", "select", "textarea"}
            and node.get("type") not in {"hidden", "file"}
            and editor in node["parents"]
        ]
        timeout = [node for node in controls if node.get("name", "").endswith("request_timeout_seconds")]
        if not timeout:
            continue
        found.add(check_id)
        assert len(timeout) == 1, check_id
        assert controls[0] is timeout[0], check_id
        if check_id not in {"load", "gateway_access"}:
            label, = [node for node in tree.nodes if node["tag"] == "label" and node.get("for") == timeout[0]["id"]]
            assert label["text"] == ("Timeout per request (seconds)" if check_id == "correctness" else "Request timeout (seconds)")
    assert found == expected


def test_timeout_edits_keep_shared_questions_and_focused_evaluator_outside_check_overrides():
    config = RunConfig(targets=[{
        "name": "local", "url": "http://localhost/v1", "model": "test",
    }], checks=["correctness"])
    run_render_checks("const config=" + config.model_dump_json() + ";" + r"""
const form=document.createElement('form');form.setAttribute('id','new-run-form');document.body.insertBefore(form,null);
const queryAll=form.querySelectorAll.bind(form);form.querySelectorAll=selector=>queryAll(selector.replace(/,\s+/g,','));
const correctnessCheck=bootstrap.checks.find(item=>item.id==='correctness');
function render(){
  state.config=config;form.innerHTML=renderTestCard(correctnessCheck,config);form.elements={};
  for(const input of form.querySelectorAll('input, select, textarea')){
    input.name=input.getAttribute('name')||'';input.value=input.getAttribute('value')||'';
    input.checked=input.hasAttribute('checked');input.checkValidity=()=>true;
    form.elements[input.name]=input;
  }
}
render();syncTestControls=()=>{};
const timeout=form.querySelector('[name="test_correctness_request_timeout_seconds"]');
const questionSource=form.querySelector('[name="request_source"]');
assert.equal(timeout.closest('[data-test-settings]').dataset.testSettings,'correctness');
assert.equal(questionSource.closest('[data-test-settings]'),null);
editTestField(questionSource);assert.equal(form.elements.test_mode_correctness.value,'default');
editTestField(timeout);assert.equal(form.elements.test_mode_correctness.value,'custom');
config.request_source='request_pack';
config.request_pack={cases:[{fixture_id:'question',messages:[{role:'user',content:'Question'}],expected:'Answer',scorer:'judge'}]};
config.judging={judges:[{name:'evaluator',url:'invalid-url',model:'test'}]};state.judgeEditorOpen=true;render();
assert.equal(form.elements.judge_0_url,undefined);
const evaluatorForm=document.createElement('form');evaluatorForm.setAttribute('id','evaluator-form');
evaluatorForm.innerHTML=renderJudging(config,{focused:true});document.body.insertBefore(evaluatorForm,null);
const evaluator=evaluatorForm.querySelector('[name="judge_0_url"]');
assert(evaluator);assert.equal(evaluator.closest('[data-test-settings]'),null);assert.equal(evaluator.closest('#new-run-form'),null);
let validated=0;evaluatorForm.reportValidity=()=>{validated++;return false;};
state.setupWorkspace={view:'evaluator'};const before=JSON.stringify(config.judging);
saveEvaluator({preventDefault(){}});assert.equal(validated,1);
assert.equal(JSON.stringify(config.judging),before);assert.equal(state.setupWorkspace.view,'evaluator');
""", dom=True)


def test_hint_origins_respect_explicit_overrides_and_measure_only():
    browser_logic(r"""
const correctness=bootstrap.checks.find(x=>x.id==='correctness'),cancel=bootstrap.checks.find(x=>x.id==='cancellation');
config.samples=7;
assert.equal(checkFieldHint(correctness,config,'samples'),'Using run default: 7 requests');
assert.equal(checkFieldHint(cancel,config,'cancel_after_ms'),'Check default');
assert.match(checkFieldHelp(bootstrap.checks.find(x=>x.id==='access'),'request_timeout_seconds'),/suite value is a default, not a ceiling/);
assert.match(checkFieldHelp(bootstrap.checks.find(x=>x.id==='generation'),'max_output_tokens'),/suite value is a default, not a ceiling/);
config.test_options.correctness={samples:7};
assert.equal(checkFieldHint(correctness,config,'samples'),'Custom: 7 requests');
const generation=bootstrap.checks.find(x=>x.id==='generation');config.limits.latency_ms=900;
config.test_options.generation={limits:{latency_ms:null}};
const hint=checkFieldHint(generation,config,'latency_ms','limit');
assert.equal(hint,'Measure only · run default: 900 ms');
assert.match(checkFieldHelp(generation,'latency_ms','limit'),/Blank means measure only/);
config.test_options.generation.limits.min_samples=null;
assert.doesNotMatch(checkFieldHint(generation,config,'min_samples','limit'),/Measure only/);
assert.doesNotMatch(checkFieldHint(cancel,config,'server_log'),/measure only/);
const node={dataset:{checkId:'correctness',settingName:'samples',settingKind:'option'}};
groups['[data-check-field-hint]']=[node];updateCheckFieldHints(config);assert.equal(node.textContent,'Custom: 7 requests');
delete config.test_options.correctness;config.samples=4;
nodes['#new-run-form']={elements:{test_correctness_samples:{value:''}}};
updateCheckFieldHints(config);assert.equal(node.textContent,'Using run default: 4 requests');
nodes['#new-run-form'].elements.test_correctness_samples.value='4';
updateCheckFieldHints(config);assert.equal(node.textContent,'Using run default: 4 requests');
""")


def test_default_check_editor_keeps_only_actionable_copy_and_live_reset():
    browser_logic(r"""
const check=bootstrap.checks.find(x=>x.id==='correctness');config.checks=['correctness'];
let markup=renderTestCard(check,config);
assert.equal(testSettingsLabel(check,config),'');
assert.match(markup,/class="test-settings"/);
assert.doesNotMatch(markup,/Settings for this check|Changes are kept as you edit|Shared by/);
assert.doesNotMatch(markup,/question-source-scope/);
assert.match(markup,/data-reset-test="correctness"[^>]*hidden/);
const reset={dataset:{resetTest:'correctness'},hidden:true};groups['[data-reset-test]']=[reset];
config.test_options.correctness={samples:9};updateCheckFieldHints(config);assert.equal(reset.hidden,false);
assert.equal(testSettingsLabel(check,config),'Overrides');
delete config.test_options.correctness;updateCheckFieldHints(config);assert.equal(reset.hidden,true);
config.checks.push('load');markup=renderTestCard(check,config);
assert.match(markup,/question-source-scope">Load test uses this question source too/);
assert.equal((markup.match(/name="request_source"/g)||[]).length,2);
""")


def test_generated_request_summaries_are_minimums_and_profiles_do_not_claim_overrides():
    browser_logic(r"""
config.samples=3;
for(const id of ['correctness','json','context']){
  const check=bootstrap.checks.find(item=>item.id===id);
  assert.match(effectiveCheckSummary(check,config),new RegExp('At least 3 '+(id==='json'?'questions':'requests')));
}
config.sessions=bootstrap.profile_defaults.sessions;
assert.equal(testSettingsLabel(bootstrap.checks.find(item=>item.id==='sessions'),config),'');
""")


def test_question_source_keeps_collection_out_of_compact_setup():
    browser_logic(r"""
bootstrap.builtin_questions=[{question:'What is 2 + 2?',expected:'4'}];
const original=JSON.stringify(config),markup=renderRequestSource(config);
assert(markup.indexOf('value="builtin"')<markup.indexOf('value="request_pack"'));
assert.match(markup,/value="builtin" checked/);
assert.doesNotMatch(markup,/class="builtin-example"|What is 2 \+ 2\?|<code>4<\/code>/);
const preview=renderBuiltinQuestions(config);assert.match(preview,/What is 2 \+ 2\?/);assert.match(preview,/<code>4<\/code>/);
assert.doesNotMatch(markup,/Questions &amp; expected answers|Known-answer questions are ready|Ready to use|Add or import a dataset/);
assert.match(markup,/data-action="view-builtin-examples">View examples/);
assert.doesNotMatch(markup,/data-action="manage-questions"|Manage questions/);
assert.match(markup,/Required correct answers/);
assert.doesNotMatch(markup,/dataset-example-form|dataset-import-form|judge_count/);
assert.doesNotMatch(markup,/Each saved question runs once/);
assert.equal(JSON.stringify(config),original);
const custom=structuredClone(config);custom.request_source='request_pack';
const customMarkup=renderRequestSource(custom);
assert.match(customMarkup,/data-action="manage-questions">Manage questions/);
assert.doesNotMatch(customMarkup,/data-action="view-builtin-examples"|View examples/);
assert.equal(JSON.stringify(config),original);
""")


def test_sample_coverage_warnings_use_each_comparison_group_not_totals():
    browser_logic(r"""
config.limits.min_samples=5;
const check=id=>bootstrap.checks.find(x=>x.id===id);
config.load_test=structuredClone(bootstrap.load_test.defaults);
config.load_test.patterns.concurrent.levels=[1,8];config.load_test.pass_criteria.min_valid_responses=5;
config.load_test.workloads.combinations.selected=['short_short','long_short'];config.load_test.workloads.mixed.enabled=true;
// Duration points have no fixed sample count; required evidence belongs to each point.
assert.equal(checkCoverageWarning(check('load'),config),'');
let criteria=renderLoadEditor(config);
assert.match(criteria,/Minimum valid responses per result group/);
assert.match(criteria,/name="load_pass_criteria_min_valid_responses"[^>]*value="5"/);
assert.match(criteria,/A result group is a request type at one traffic pattern and level/);
assert.match(criteria,/Minimum valid responses is an evidence requirement, not a stopping count/);
config.limits.min_samples=100;config.load_test.patterns.concurrent.levels=[8];
config.load_test.pass_criteria.min_valid_responses=2;
assert.equal(checkCoverageWarning(check('load'),config),'');
criteria=renderLoadEditor(config);
assert.match(criteria,/name="load_pass_criteria_min_valid_responses"[^>]*value="2"/);
// Global comparison-group coverage remains independent for consistency.
config.limits.min_samples=5;
config.consistency={...bootstrap.profile_defaults.consistency,repetitions:4,levels:[1,2],shapes:['short','long']};
assert.match(checkCoverageWarning(check('consistency'),config),/4 in a group; 5 required/);
config.consistency.repetitions=5;assert.equal(checkCoverageWarning(check('consistency'),config),'');
assert.equal(checkCoverageWarning(check('correctness'),config),'');
const warning={dataset:{checkCoverage:'consistency'}};groups['[data-check-coverage]']=[warning];
updateCheckFieldHints(config);assert.equal(warning.hidden,true);
config.checks.push('consistency');
config.limits.min_samples=6;updateCheckFieldHints(config);assert.equal(warning.hidden,false);assert.match(warning.textContent,/6 required/);
""")


def test_profile_controls_explain_the_experiment_and_keep_uncommon_values():
    browser_logic(r"""
config.sessions={...bootstrap.profile_defaults.sessions,modes:['live','fixed']};
const markup=renderProfileEditor(bootstrap.checks.find(x=>x.id==='sessions'),config);
assert.match(markup,/<option value="live,fixed" selected>Saved selection/);
const advanced=markup.match(/<details[^>]*data-session-length-sampling[^>]*>([\s\S]*?)<\/details>/)[1];
assert.doesNotMatch(advanced,/name="profile_sessions_max_tokens"/);
assert.match(advanced,/name="session_sample_input_tokens"/);
assert.match(markup,/data-profile-settings="sessions" class="fields">[\s\S]*name="profile_sessions_max_tokens"/);
assert.equal((markup.match(/name="profile_sessions_max_tokens"/g)||[]).length,1);
assert.match(markup,/Conversations to run for each chosen history type/);
const consistency=renderProfileEditor(bootstrap.checks.find(x=>x.id==='consistency'),config);
assert(consistency.indexOf('name="profile_consistency_strict"')<consistency.indexOf('<details'));
assert.match(consistency,/Wrong known answers can still fail either mode/);
assert.doesNotMatch(renderTestCard(bootstrap.checks.find(x=>x.id==='tools'),config),/no-tool control/);
""")


def test_lower_run_ceiling_preserves_requested_override_and_sets_native_limits():
    browser_logic(r"""
config.concurrency=8;config.test_options.correctness={concurrency:8};
const check=bootstrap.checks.find(x=>x.id==='correctness');
assert.match(effectiveCheckSummary(check,config),/Up to 8 in progress/);
config.concurrency=4;config.max_output_tokens=32;config.request_timeout_seconds=5;
config.test_options.generation={max_output_tokens:64};config.test_options.access={request_timeout_seconds:10};
const summary=effectiveCheckSummary(check,config);
assert.match(summary,/Up to 8 in progress/);assert.doesNotMatch(summary,/exceeds run limit/);
assert.doesNotMatch(summary,/ · Up to 4 in progress/);
assert.equal(checkOverrideWarning(bootstrap.checks.find(x=>x.id==='generation'),config),'');
assert.equal(checkOverrideWarning(bootstrap.checks.find(x=>x.id==='access'),config),'');
const elements={test_correctness_concurrency:{value:'8'},test_generation_max_output_tokens:{value:'64'},
  test_access_request_timeout_seconds:{value:'10'},test_context_context_limit:{value:'2048',max:'65536'}};
for(const input of Object.values(elements))input.removeAttribute=name=>delete input[name];
nodes['#new-run-form']={elements};updateCheckFieldHints(config);
assert.equal(elements.test_correctness_concurrency.max,String(bootstrap.limits['test_options.correctness.concurrency'].maximum));assert.equal(elements.test_correctness_concurrency.value,'8');
assert.equal(elements.test_generation_max_output_tokens.max,bootstrap.limits['test_options.generation.max_output_tokens'].maximum==null?undefined:String(bootstrap.limits['test_options.generation.max_output_tokens'].maximum));assert.equal(elements.test_generation_max_output_tokens.value,'64');
assert.equal(elements.test_access_request_timeout_seconds.max,undefined);assert.equal(elements.test_access_request_timeout_seconds.value,'10');
assert.equal(elements.test_context_context_limit.max,bootstrap.limits['test_options.context.context_limit'].maximum==null?undefined:String(bootstrap.limits['test_options.context.context_limit'].maximum));
assert.equal(config.test_options.correctness.concurrency,8);
config.request_source='request_pack';config.request_pack={cases:[]};
const markup=renderTestCard(check,config);
assert.match(markup,/data-pack-request-settings/);
assert.doesNotMatch(markup,/exceeds run limit/);
assert.match(markup,/data-correctness-builtin-settings hidden/);
syncPackControls();assert.equal(elements.test_correctness_concurrency.disabled,true);
const warning={dataset:{checkOverrideWarning:'correctness'}};groups['[data-check-override-warning]']=[warning];
updateCheckFieldHints(config);assert.equal(warning.hidden,true);assert.equal(warning.textContent,'');
config.concurrency=8;updateCheckFieldHints(config);
assert.equal(elements.test_correctness_concurrency.max,String(bootstrap.limits['concurrency'].maximum));
assert.equal(warning.hidden,true);
assert.doesNotMatch(effectiveCheckSummary(check,config),/exceeds run limit/);
""")


def test_coverage_warnings_are_visible_outside_closed_editors_and_not_in_info():
    browser_logic(r"""
config.checks=['consistency'];config.limits.min_samples=5;
config.consistency={...bootstrap.profile_defaults.consistency,repetitions:4,levels:[1,2],shapes:['short','long']};
const check=bootstrap.checks.find(item=>item.id==='consistency'),markup=renderTestCard(check,config);
assert.match(markup,/class="hint check-row-warning" data-check-coverage="consistency" >/);
assert(markup.indexOf('data-check-coverage="consistency"')<markup.indexOf('data-test-editor="consistency" hidden'));
assert.equal((markup.match(/Too few planned samples/g)||[]).length,1);
assert.doesNotMatch(renderCheckInfo(check,config),/Too few planned samples/);
const warning={dataset:{checkCoverage:'consistency'}};groups['[data-check-coverage]']=[warning];
updateCheckFieldHints(config);assert.equal(warning.hidden,false);
config.consistency.repetitions=5;updateCheckFieldHints(config);assert.equal(warning.hidden,true);
""")


def test_invalid_visible_selected_override_keeps_selected_view():
    browser_logic(r"""
config.concurrency=4;config.test_options.correctness={concurrency:8};
const panel={dataset:{setupPanel:'2'}},fieldset={disabled:true},details={tagName:'DETAILS',open:false,parentElement:null};
const card={hidden:false,dataset:{testCard:'correctness'},querySelector:()=>({checked:true})};
const input={name:'test_correctness_concurrency',value:'8',max:'4',disabled:false,parentElement:details,
  closest(selector){return selector==='[data-test-card]'?card:selector==='[data-test-settings]'?{}:selector==='[data-setup-panel]'?panel:selector==='fieldset'?fieldset:null;},
  checkValidity(){return Number(this.value)<=Number(this.max);},focus(){this.focused=true;},reportValidity(){this.reported=true;}};
const form={elements:{test_mode_correctness:{value:'custom'}},querySelectorAll:()=>[input]};
nodes['#new-run-form']=form;nodes['[data-test-card="correctness"]']=card;nodes['#check-search']={value:'saved browse query'};
state.catalogueFilter='selected';state.catalogueCategory='server';state.catalogueSearch='saved browse query';state.setupStep=3;
syncTestControls=()=>{fieldset.disabled=false;};syncConfigView=syncSetupStep=()=>{};
assert.equal(validateRunForm(form),false);
assert.equal(state.catalogueFilter,'selected');assert.equal(state.catalogueCategory,'server');
assert.equal(state.catalogueSearch,'saved browse query');assert.equal(state.openTest,'correctness');assert.equal(state.setupStep,2);
assert(details.open&&input.focused&&input.reported);assert.equal(input.value,'8');
card.hidden=true;revealCatalogueCheck('correctness');assert.equal(state.catalogueFilter,'all');
assert.equal(state.catalogueCategory,'all');assert.equal(state.catalogueSearch,'');
""")


def test_tool_fixture_editor_preserves_modes_and_escapes_json():
    browser_logic(r"""
const check=bootstrap.checks.find(x=>x.id==='tools');
config.tool_checks={modes:['stream','complete'],fixtures:[{fixture_id:'mixed',expected_content:{mode:'exact',value:'</textarea><script>bad</script>'}}]};
const original=JSON.stringify(config),markup=renderProfileEditor(check,config);
assert.match(markup,/<option value="stream,complete" selected>Saved selection/);
assert.match(markup,/name="profile_tools_fixtures"[^>]*maxlength="65536"/);
assert.match(markup,/&lt;\/textarea&gt;&lt;script&gt;bad&lt;\/script&gt;/);
assert.doesNotMatch(markup,/<script>/);
assert.match(effectiveCheckSummary(check,config),/1 tool fixture.*Streaming \+ Complete.*Assistant text and calls checked/);
assert.equal(JSON.stringify(config),original);
config.tool_checks=null;config.stream=false;
assert.match(effectiveCheckSummary(check,config),/Built-in weather check.*Complete/);
assert.doesNotMatch(effectiveCheckSummary(check,config),/Streaming/);
config.stream=true;assert.match(effectiveCheckSummary(check,config),/Complete \+ Streaming/);
config.tool_checks={modes:null,fixtures:'[{"unfinished"'};
assert.match(effectiveCheckSummary(check,config),/Invalid tool fixture draft/);
assert.match(renderProfileEditor(check,config),/\[\{&quot;unfinished&quot;/);
""")


def test_invalid_tool_fixture_editor_shows_native_error_before_closing():
    browser_logic(r"""
config.tool_checks={fixtures:[],modes:null};
const profile={dataset:{profileSettings:'tools'}},input={name:'profile_tools_fixtures',value:'[',disabled:true,
  closest(selector){return selector==='[data-profile-settings]'?profile:null;},
  setCustomValidity(message){this.message=message;},checkValidity(){return !this.message;},focus(){this.focused=true;},reportValidity(){this.reported=true;}};
const form={elements:{profile_edits_tools:{value:'[]'}},querySelectorAll:()=>[input]};
revealSetupStep=()=>{};revealCatalogueCheck=()=>{};
assert.equal(validateProfileDrafts(form,'tools'),false);assert.match(input.message,/JSON array/);
assert(input.focused&&input.reported);assert.equal(input.disabled,true);
input.value='[]';assert.equal(validateProfileDrafts(form,'tools'),true);assert.equal(input.message,'');
""")
