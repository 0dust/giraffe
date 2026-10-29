"""Single-screen setup and all-run command search contracts."""
from tests.test_ux_results import run_render_checks


def test_setup_profiles_use_bootstrap_checks_and_render_advanced_fields():
    run_render_checks(r"""
state.bootstrap.config={...structuredClone(bootstrap.defaults),checks:['access','serving','first_output']};
state.config={...structuredClone(bootstrap.defaults),checks:['access','serving','first_output'],targets:[{name:'local',url:'http://localhost/v1',model:'model',tokenizer:'auto'}]};
assert.deepEqual(Array.from(profileChecks('quick')),['access','serving','first_output']);
assert.deepEqual(Array.from(profileChecks('latency')),['access','serving','first_output','generation']);
assert.deepEqual(Array.from(profileChecks('load')),['access','serving','first_output','load']);
assert.deepEqual(Array.from(profileChecks('correctness')),['access','serving','first_output','json','context']);
Object.defineProperty(Object.getPrototypeOf($('#main')),'elements',{get(){return new Proxy({},{get:(_,name)=>this.querySelector('[name="'+String(name)+'"]')||{value:'',dataset:{}}});}});
state.bootstrap.checks=[];state.jsonDraft=state.jsonBase='synced';
syncTestControls=updateBounds=updateRestart=syncConfigView=syncPackControls=renderRequestPreview=syncSetupStep=renderModelDiscovery=watchPlanViewport=scheduleTokenizerSelection=renderTokenizerSelection=()=>{};
renderNew();assert.equal($$('.setup-step').length,0);assert.equal($$('.setup-profile').length,5);
const advanced=$('#setup-advanced');
for(const name of ['name','api_key','tokenizer','metrics_url','host_metrics_url','stream','retention','mode','request_timeout_seconds'])assert(advanced.querySelector('[name="'+name+'"]'),name);
assert.equal($('#execution-settings').hidden,true);
assert(!advanced.querySelector('[name="baseline_id"]'));assert($('[name="baseline_id"]'));
assert.equal($('#start-run').getAttribute('type'),'submit');
assert.match($('#setup-duration').textContent,/Duration unknown until first requests/);
assert.doesNotMatch(nodes['#main'].innerHTML,/hero-coat|setup-stepper|Confidence:|Preparation may send/);
""", dom=True)


def test_profile_selection_changes_checks_and_custom_keeps_selection():
    run_render_checks(r"""
state.bootstrap.config={checks:['access','serving','first_output']};state.config={checks:['access']};
captureFormDraft=configChanged=renderNew=()=>{};
chooseSetupProfile('latency');assert.deepEqual(Array.from(state.config.checks),['access','serving','first_output','generation']);
chooseSetupProfile('correctness');assert.deepEqual(Array.from(state.config.checks),['access','serving','first_output','json','context']);
chooseSetupProfile('custom');assert.deepEqual(Array.from(state.config.checks),['access','serving','first_output','json','context']);
assert.equal(renderPlanTime({estimated_seconds:125}),'<p class="hint plan-time">~2m 5s</p>');
""")


def test_commands_show_all_runs_and_fetch_lazily_without_losing_input_focus():
    run_render_checks(r"""
(async()=>{
const dialog=document.createElement('dialog');dialog.setAttribute('id','command-dialog');document.body.insertBefore(dialog,null);
dialog.showModal=function(){this.open=true;};let paths=[];
state.runs=Array.from({length:8},(_,index)=>({id:'run-'+index,targets:[{model:'model-'+index}]}));
api=async path=>{paths.push(path);return {runs:[...state.runs,{id:'older',targets:[{model:'older-model'}]}]};};
openCommands();assert.equal(paths[0],'/api/runs');assert.equal(commandList.filter(item=>item.group==='Runs').length,8);
$('#command-search').value='older';await Promise.resolve();await Promise.resolve();
assert.equal($('#command-search').value,'older');assert(commandList.some(item=>item.label==='older-model'));
assert.equal($('#command-search').getAttribute('role'),'combobox');assert.equal($('#command-options').getAttribute('role'),'listbox');
const input=$('#command-search');input.focus({preventScroll:true});listeners.keydown({target:input,key:'ArrowDown',preventDefault(){}});
assert.equal(document.activeElement,input);assert(input.getAttribute('aria-activedescendant'));
})()
""", dom=True)


def test_saved_titles_never_replace_catalogue_names_or_observation_group():
    run_render_checks(r"""
const value=report([check('access','pass',{title:'Obsolete network label'}),check('prefix','inconclusive',{title:'Obsolete cache label',required:false})]);
state.detail={id:'saved-run',state:'completed',report:value};
const markup=renderChecks(value);
assert.match(markup,/Endpoint access/);assert.match(markup,/Server observations/);assert.match(markup,/Prefix cache/);
assert.doesNotMatch(markup,/Obsolete network label|Obsolete cache label/);
assert.equal(mainFinding(value),'1 check passed');
state.baselines=[{id:'baseline',run_id:'saved-run'}];assert.doesNotMatch(resultGuidance(value),/Save a baseline/);
""")


def test_tokenizer_fallback_is_muted_and_check_timeout_only_shows_overrides():
    run_render_checks(r"""
nodes['#tokenizer-status']={textContent:'',classList:{toggle(name,value){assert.equal(value,false);}}};
currentTokenizerSelection=()=>({status:'fallback',message:'Could not resolve or load tokenizer for llama3.1'});syncSetupStep=()=>{};
renderTokenizerSelection();assert.equal(nodes['#tokenizer-status'].textContent,'Using the built-in tokenizer.');
const config={...structuredClone(bootstrap.defaults),targets:[{name:'local'}],checks:['access'],test_options:{}};
const access=bootstrap.checks.find(item=>item.id==='access');
assert.doesNotMatch(effectiveCheckSummary(access,config),/Request timeout/);
config.test_options.access={request_timeout_seconds:17};assert.match(effectiveCheckSummary(access,config),/Request timeout 17s/);
""")


def test_unselected_observations_use_the_same_canonical_catalog():
    run_render_checks(r"""
const value=report([check('access','pass'),check('gpu','skipped',{title:'GPU observations',required:false}),check('prefix','skipped',{title:'Shared prefixes and cache evidence',required:false})]);
const html=detail(value);assert.match(html,/Not selected:[^<]*GPU metrics, Prefix cache/);for(const meta of bootstrap.checks.filter(item=>item.id!=='access'))assert(html.includes(escape(catalogTitle(meta.id))),meta.id);assert.doesNotMatch(html,/GPU observations|Shared prefixes and cache evidence/);
""")
