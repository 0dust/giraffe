"""Per-check controls explain defaults without overwhelming the primary task."""

from tests.test_ux_catalogue import browser_logic


def test_run_limit_edits_refresh_all_quoted_limits_without_replacing_controls():
    browser_logic(r"""
config.checks=['access','correctness','generation','concurrency','sessions'];
config.test_options={correctness:{concurrency:8},generation:{max_output_tokens:64},access:{request_timeout_seconds:10}};
config.sessions={profile:'performance',concurrency:2,max_tokens:512};
const elements={};
for(const [name] of numericFields)elements[name]={name,value:String(config[name]),dataset:{},selectionStart:1,selectionEnd:2};
for(const check of bootstrap.checks){
  for(const name of check.option_fields)elements[`test_${check.id}_${name}`]={value:'',dataset:{inherited:'false'}};
  for(const name of check.limit_fields)elements[`test_${check.id}_limit_${name}`]={value:'',dataset:{inherited:'false'}};
}
for(const [name,value] of Object.entries({test_correctness_concurrency:'8',test_generation_max_output_tokens:'64',test_access_request_timeout_seconds:'10',
  load_schedule_levels:'1, 64',profile_sessions_concurrency:'2',profile_sessions_max_tokens:'512',sessions_max_tokens:'512',session_cap_0:'512'})){
  elements[name]={name,value,dataset:{inherited:'false'},selectionStart:1,selectionEnd:2};
}
const form={elements};nodes['#new-run-form']=form;
elements.test_json_concurrency={value:'4',dataset:{inherited:'true'}};
for(const id of ['selection-summary','load-guidance','run-bounds','run-bar-summary','cli-fallback','run-ceilings-summary','field-load_schedule_levels-hint','field-traffic_max_in_flight-hint'])nodes['#'+id]={};
const hints=[['correctness','concurrency'],['generation','max_output_tokens'],['access','request_timeout_seconds']].map(([checkId,settingName])=>({dataset:{checkId,settingName,settingKind:'option'}}));
groups['[data-check-field-hint]']=hints;groups['[name^="session_cap_"]']=[elements.session_cap_0];
syncSetupIdentity=updateDatasetReadiness=updateCheckRow=renderReviewScope=renderRequestPlan=renderRequestPreview=scheduleRequestPlan=()=>{};
renderNew=()=>{throw Error('Limit edits must not replace form controls');};
readForm=()=>{const current=structuredClone(config);for(const [name] of numericFields)current[name]=Number(elements[name].value);return {config:current};};
const snapshots=['load_schedule_levels','test_correctness_concurrency','test_generation_max_output_tokens','test_access_request_timeout_seconds','sessions_max_tokens','session_cap_0'].map(name=>[name,elements[name],elements[name].value]);
for(const [name,value] of [['concurrency','40'],['max_output_tokens','256'],['request_timeout_seconds','60']]){
  const input=elements[name];input.value=value;input.matches=()=>false;input.closest=q=>q==='#new-run-form'?form:null;
  listeners.input({target:input});assert.equal(form.elements[name],input);assert.equal(input.value,value);assert.equal(input.selectionStart,1);assert.equal(input.selectionEnd,2);
}
assert.match(nodes['#field-load_schedule_levels-hint'].textContent,/Maximum at once \(40\)/);
assert.match(nodes['#field-traffic_max_in_flight-hint'].textContent,/Maximum at once \(40\)/);
assert.match(nodes['#run-ceilings-summary'].textContent,/40 at once/);
assert.equal(hints[0].textContent,'Custom · run default: 40 at a time');
assert.equal(hints[1].textContent,'Custom · run default: 256 tokens');
assert.equal(hints[2].textContent,'Custom · run default: 60 seconds');
assert.equal(elements.profile_sessions_concurrency.max,'32'); // Preserve the session schema's own maximum.
for(const name of ['profile_sessions_max_tokens','sessions_max_tokens','session_cap_0'])assert.equal(elements[name].max,'256');
assert.equal(Number(elements.test_json_concurrency.value),40);
for(const [name,input,value] of snapshots){assert.equal(form.elements[name],input);assert.equal(input.value,value);assert.equal(input.selectionStart,1);assert.equal(input.selectionEnd,2);}
elements.concurrency.value='2';listeners.input({target:elements.concurrency});assert.equal(elements.profile_sessions_concurrency.max,'2');assert.equal(Number(elements.test_json_concurrency.value),2);
assert.match(nodes['#field-load_schedule_levels-hint'].textContent,/Maximum at once \(2\)/);assert.equal(elements.load_schedule_levels.value,'1, 64');
""")


def test_questions_precede_settings_and_inactive_controls_stay_preserved():
    browser_logic(r"""
for(const id of ['correctness','capacity']){
  config.checks=[id];const check=bootstrap.checks.find(x=>x.id===id);
  const markup=renderTestCard(check,config);
  assert(markup.indexOf('id="setup-dataset"')<markup.indexOf('data-test-settings="'+id+'"'));
  if(id==='capacity')assert(markup.indexOf('id="setup-dataset"')<markup.indexOf('name="traffic_rates"'));
  assert.match(markup,/>Settings<\/button>/);assert.match(markup,/aria-label="Settings for /);
}
config.checks=['correctness'];config.request_source='request_pack';config.request_pack={cases:[]};
config.test_options.correctness={samples:9,concurrency:2};
const markup=renderTestCard(bootstrap.checks.find(x=>x.id==='correctness'),config);
const hidden=markup.slice(markup.indexOf('<div hidden data-inactive-question-settings>'),markup.indexOf('class="fields check-criteria-fields"'));
assert.match(hidden,/name="test_correctness_samples"[^>]*value="9"/);
assert.match(hidden,/name="test_correctness_concurrency"[^>]*value="2"/);
assert.match(markup,/Sample count and requests-at-a-time settings are not used/);
assert.match(markup,/name="test_correctness_limit_min_correctness"/);
assert.equal(config.test_options.correctness.samples,9);
""")


def test_generic_transport_controls_are_secondary_and_pass_targets_remain_visible():
    browser_logic(r"""
for(const [id,name] of [['access','request_timeout_seconds'],['correctness','concurrency'],['generation','max_output_tokens'],['recovery','concurrency']]){
  const markup=renderTestCard(bootstrap.checks.find(x=>x.id===id),config);
  const disclosure=markup.match(/<details class="check-advanced check-run-overrides">([\s\S]*?)<\/details>/)[1];
  assert.match(disclosure,new RegExp('name="test_'+id+'_'+name+'"'));
  assert.equal((markup.match(new RegExp('name="test_'+id+'_'+name+'"','g'))||[]).length,1);
  assert.match(markup,new RegExp('id="field-check_'+id+'-help"[^>]*popover="auto"'));
  assert.doesNotMatch(markup,/Current value shown|Clear to inherit the shared default/);
}
const context=renderTestCard(bootstrap.checks.find(x=>x.id==='context'),config);
assert(context.indexOf('name="test_context_context_limit"')<context.indexOf('class="fields check-criteria-fields"'));
assert.match(context,/Minimum requests across the generated context cases/);
assert.match(context,/Calibration and retries can add requests/);
assert.doesNotMatch(context,/Minimum requests for each context-length group/);
assert.match(renderTestCard(bootstrap.checks.find(x=>x.id==='correctness'),config),/Minimum questions/);
const timing=renderTestCard(bootstrap.checks.find(x=>x.id==='first_output'),config);
assert.match(timing,/Minimum usable answers/);
assert(timing.indexOf('name="test_first_output_limit_min_samples"')<timing.indexOf('data-done-test="first_output"'));
""")


def test_hint_origins_respect_explicit_overrides_and_measure_only():
    browser_logic(r"""
const correctness=bootstrap.checks.find(x=>x.id==='correctness'),cancel=bootstrap.checks.find(x=>x.id==='cancellation');
config.samples=7;
assert.equal(checkFieldHint(correctness,config,'samples'),'Run default');
assert.equal(checkFieldHint(cancel,config,'cancel_after_ms'),'Check default');
assert.match(checkFieldHelp(bootstrap.checks.find(x=>x.id==='access'),'request_timeout_seconds'),/cannot exceed the run request deadline/);
assert.match(checkFieldHelp(bootstrap.checks.find(x=>x.id==='generation'),'max_output_tokens'),/cannot exceed the run output cap/);
config.test_options.correctness={samples:7};
assert.equal(checkFieldHint(correctness,config,'samples'),'Custom · run default: 7 requests');
const generation=bootstrap.checks.find(x=>x.id==='generation');config.limits.latency_ms=900;
config.test_options.generation={limits:{latency_ms:null}};
const hint=checkFieldHint(generation,config,'latency_ms','limit');
assert.equal(hint,'Measure only · run default: 900 ms');
assert.match(checkFieldHelp(generation,'latency_ms','limit'),/Blank means measure only/);
config.test_options.generation.limits.min_samples=null;
assert.doesNotMatch(checkFieldHint(generation,config,'min_samples','limit'),/Measure only/);
assert.doesNotMatch(checkFieldHint(cancel,config,'server_log'),/measure only/);
const node={dataset:{checkId:'correctness',settingName:'samples',settingKind:'option'}};
groups['[data-check-field-hint]']=[node];updateCheckFieldHints(config);assert.equal(node.textContent,'Custom · run default: 7 requests');
delete config.test_options.correctness;config.samples=4;
nodes['#new-run-form']={elements:{test_correctness_samples:{value:''}}};
updateCheckFieldHints(config);assert.equal(node.textContent,'Run default: 4 requests');
nodes['#new-run-form'].elements.test_correctness_samples.value='4';
updateCheckFieldHints(config);assert.equal(node.textContent,'Run default');
""")


def test_default_check_editor_keeps_only_actionable_copy_and_live_reset():
    browser_logic(r"""
const check=bootstrap.checks.find(x=>x.id==='correctness');config.checks=['correctness'];
let markup=renderTestCard(check,config);
assert.equal(testSettingsLabel(check,config),'');
assert.match(markup,/class="test-settings simple-primary-settings"/);
assert.doesNotMatch(markup,/Settings for this check|<h4>Requests<\/h4>|<h4>Pass criteria<\/h4>|Changes are kept as you edit|Shared by/);
assert.doesNotMatch(markup,/question-source-scope/);
assert.match(markup,/data-reset-test="correctness"[^>]*hidden/);
const reset={dataset:{resetTest:'correctness'},hidden:true};groups['[data-reset-test]']=[reset];
config.test_options.correctness={samples:9};updateCheckFieldHints(config);assert.equal(reset.hidden,false);
assert.equal(testSettingsLabel(check,config),'Overrides');
delete config.test_options.correctness;updateCheckFieldHints(config);assert.equal(reset.hidden,true);
config.checks.push('capacity');markup=renderTestCard(check,config);
assert.match(markup,/question-source-scope">Also used by Capacity under load/);
assert.equal((markup.match(/name="request_source"/g)||[]).length,2);
""")


def test_generated_request_summaries_are_minimums_and_profiles_do_not_claim_overrides():
    browser_logic(r"""
config.samples=3;
for(const id of ['correctness','json','context']){
  const check=bootstrap.checks.find(item=>item.id===id);
  assert.match(effectiveCheckSummary(check,config),new RegExp('At least 3 '+(id==='context'?'requests':'questions')));
}
config.sessions=bootstrap.profile_defaults.sessions;
assert.equal(testSettingsLabel(bootstrap.checks.find(item=>item.id==='sessions'),config),'');
""")


def test_question_source_keeps_examples_collapsed_and_scope_in_help():
    browser_logic(r"""
bootstrap.builtin_question_example={question:'What is 2 + 2?',expected:'4'};
const original=JSON.stringify(config),markup=renderRequestSource(config);
assert(markup.indexOf('value="builtin"')<markup.indexOf('value="request_pack"'));
assert.match(markup,/value="builtin" checked/);
assert.match(markup,/<details class="builtin-example"><summary>View an example<\/summary>/);
assert.match(markup,/What is 2 \+ 2\?/);assert.match(markup,/<code>4<\/code>/);
assert.doesNotMatch(markup,/Questions &amp; expected answers|Known-answer questions are ready|Ready to use|Add or import a dataset/);
assert.match(markup,/id="field-request_source-help"[^>]*popover="auto"/);
assert.match(markup,/Sample count and requests-at-a-time settings are not used/);
assert.equal(JSON.stringify(config),original);
""")


def test_sample_coverage_warnings_use_each_comparison_group_not_totals():
    browser_logic(r"""
config.limits.min_samples=5;
const check=id=>bootstrap.checks.find(x=>x.id===id);
config.concurrent_load=structuredClone(bootstrap.concurrent_load.defaults);
config.concurrent_load.schedule.levels=[1,8];config.concurrent_load.pass_criteria.min_valid_responses=5;
config.concurrent_load.combinations.selected=['short_short','long_short'];config.concurrent_load.mixed.enabled=true;
// Duration points have no fixed sample count; required evidence belongs to each point.
assert.equal(checkCoverageWarning(check('concurrency'),config),'');
let criteria=renderConcurrentEditor(config);
assert.match(criteria,/Minimum valid responses per test point/);
assert.match(criteria,/name="load_pass_criteria_min_valid_responses"[^>]*value="5"/);
assert.match(criteria,/Required for each workload and concurrency level/);
assert.match(criteria,/reaching this count does not stop the workload/);
config.limits.min_samples=100;config.concurrent_load.schedule.levels=[8];
config.concurrent_load.pass_criteria.min_valid_responses=2;
assert.equal(checkCoverageWarning(check('concurrency'),config),'');
criteria=renderConcurrentEditor(config);
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
const advanced=markup.match(/<details class="check-advanced">([\s\S]*?)<\/details>/)[1];
assert.match(advanced,/name="profile_sessions_max_tokens"/);
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
assert.match(effectiveCheckSummary(check,config),/8 at a time/);
config.concurrency=4;config.max_output_tokens=32;config.request_timeout_seconds=5;
config.test_options.generation={max_output_tokens:64};config.test_options.access={request_timeout_seconds:10};
const summary=effectiveCheckSummary(check,config);
assert.match(summary,/8 at a time/);assert.doesNotMatch(summary,/exceeds run limit/);
assert.doesNotMatch(summary,/ · 4 at a time/);
assert.match(checkOverrideWarning(bootstrap.checks.find(x=>x.id==='generation'),config),/64 tokens exceeds run limit 32 tokens/);
assert.match(checkOverrideWarning(bootstrap.checks.find(x=>x.id==='access'),config),/10 seconds exceeds run limit 5 seconds/);
const elements={test_correctness_concurrency:{value:'8'},test_generation_max_output_tokens:{value:'64'},
  test_access_request_timeout_seconds:{value:'10'},test_context_context_limit:{value:'2048',max:'65536'}};
nodes['#new-run-form']={elements};updateCheckFieldHints(config);
assert.equal(elements.test_correctness_concurrency.max,'4');assert.equal(elements.test_correctness_concurrency.value,'8');
assert.equal(elements.test_generation_max_output_tokens.max,'32');assert.equal(elements.test_generation_max_output_tokens.value,'64');
assert.equal(elements.test_access_request_timeout_seconds.max,'5');assert.equal(elements.test_access_request_timeout_seconds.value,'10');
assert.equal(elements.test_context_context_limit.max,'65536');
assert.equal(config.test_options.correctness.concurrency,8);
config.request_source='request_pack';config.request_pack={cases:[]};
const recovery=checkOverrideWarning(check,config,true);
assert.match(recovery,/Reset this check’s overrides or raise Maximum at once in Run limits/);
const markup=renderTestCard(check,config);
assert.match(markup,/data-check-override-warning="correctness" >/);
assert(markup.indexOf('data-check-override-warning')<markup.indexOf('data-test-editor="correctness" hidden'));
assert.equal((markup.match(/exceeds run limit/g)||[]).length,1);
const warning={dataset:{checkOverrideWarning:'correctness'}};groups['[data-check-override-warning]']=[warning];
updateCheckFieldHints(config);assert.equal(warning.hidden,false);assert.equal(warning.textContent,recovery);
config.concurrency=8;updateCheckFieldHints(config);assert.equal(elements.test_correctness_concurrency.max,'8');
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
