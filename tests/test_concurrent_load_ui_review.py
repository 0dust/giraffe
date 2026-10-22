"""User-visible regressions from the Concurrent load UI review."""
import pytest
import httpx
from pydantic import TypeAdapter

from giraffe.models import ConfigDraft, migrate_load_test, migrate_concurrent_load
from giraffe.web import ConfigDraftInput, create_app
from tests.test_ux_catalogue import browser_logic
from tests.test_ux_results import run_render_checks

TARGET = {"name": "local", "url": "http://localhost:1/v1", "model": "test"}


FORM = r'''
config.checks=['load'];state.config=structuredClone(config);state.setupStep=3;
state.jsonDraft=state.jsonBase=null;
const controls=[],form={elements:{},querySelectorAll:selector=>selector.includes('[data-test-editor]')?controls.filter(input=>input.name.startsWith('load_')):controls},focused=[];
const details={tagName:'DETAILS',open:false};
function mount(source=state.config){
 controls.length=0;form.elements={};
 const markup=renderTestCard(bootstrap.checks.find(check=>check.id==='load'),source);
 const decode=value=>(value||'').replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&amp;/g,'&');
 const attr=(tag,key)=>decode(tag.match(new RegExp('(?:^|\\s)'+key+'="([^"]*)"'))?.[1]);
 function add(name,value,type='text',checked=false){
  const input={name,value:String(value??''),type,checked,dataset:{},attributes:{},style:{},parentElement:name.startsWith('load_pass_criteria_')?details:null,
   setAttribute(k,v){this.attributes[k]=v;},removeAttribute(k){delete this.attributes[k];},getAttribute(k){return this.attributes[k]||'';},
   matches:()=>false,closest:q=>q==='#new-run-form'?form:q==='[data-test-settings]'?{dataset:{testSettings:'load'}}:null,
   focus(){focused.push(name);}};
  controls.push(input);form.elements[name]=input;nodes['#field-'+name]=input;return input;
 }
 for(const match of markup.matchAll(/<input\b[^>]*>|<select\b[^>]*>[\s\S]*?<\/select>/g)){
  const tag=match[0],name=attr(tag,'name');if(!name)continue;
  const options=[...tag.matchAll(/<option\b([^>]*)>/g)],option=options.find(row=>/\bselected\b/.test(row[1]))||options[0];
  add(name,tag.startsWith('<select')?attr(option?.[1]||'','value'):attr(tag,'value'),attr(tag,'type'),/\bchecked\b/.test(tag));
 }
 for(const key of ['url','model','name'])add(key,source.targets[0][key]);
 for(const [key] of numericFields)add(key,source[key]);
 for(const [key,value] of Object.entries({metrics_url:'',baseline_id:'',mode:'normal',restart_target:'',stream:'true',traffic_mode:'default'}))add(key,value);
 for(const id of source.checks)if(!controls.some(input=>input.name==='check'&&input.value===id))add('check',id,'checkbox',true);
 nodes['#new-run-form']=form;
 for(const type of Object.keys(loadTitles)){nodes['#load-panel-'+type]={hidden:true};nodes['[data-load-collapse="'+type+'"]']={setAttribute(){}};}
 return markup;
}
FormData=class{
 constructor(){this.data=new Map();for(const input of controls){if(['checkbox','radio'].includes(input.type)&&!input.checked)continue;this.data.set(input.name,[...(this.data.get(input.name)||[]),input.type==='checkbox'?input.value||'on':input.value]);}}
 set(k,v){this.data.set(k,[v]);}get(k){return this.data.get(k)?.[0]??null;}getAll(k){return this.data.get(k)||[];}has(k){return this.data.has(k);}keys(){return this.data.keys();}
};
for(const id of ['form-error','start-run','request-plan','cli-fallback','selection-summary','load-guidance','run-bounds'])nodes['#'+id]={hidden:false,innerHTML:'',textContent:''};
renderRequestPreview=()=>{};renderRequestPlan=()=>{};scheduleRequestPlan=()=>{};
revealSetupStep=step=>state.setupStep=step;revealCatalogueCheck=id=>state.openTest=id;
syncTestControls=()=>{};syncSetupIdentity=()=>{};refreshDefaultProfileFields=()=>{};
updateDatasetReadiness=()=>{};updateCheckRow=()=>{};updateSharedSettingsSummaries=()=>{};updateCheckFieldHints=()=>{};
mount();
'''


def test_untouched_drawer_and_reset_do_not_create_overrides():
    browser_logic(FORM + r'''
const check=bootstrap.checks.find(item=>item.id==='load');
let read=readForm(false).config;
assert.equal(testSettingsLabel(check,read),'');
read.load_test.profile.short.input_tokens=1;state.config=read;
captureFormDraft=()=>{};renderNew=()=>mount();state.migrationNotes=['old'];state.dismissedMigrationNotes=['old'];
await listeners.click({target:{closest:q=>q==='[data-reset-test]'?{dataset:{resetTest:'load'}}:null}});
read=readForm(false).config;assert.equal(testSettingsLabel(check,read),'');assert.equal(state.migrationNotes.length,0);assert.equal(state.dismissedMigrationNotes.length,0);
assert.deepEqual(Object.keys(loadSettings(read).workloads.mixed.shares).sort(),Object.keys(bootstrap.load_test.defaults.workloads.mixed.shares).sort());
''')


@pytest.mark.parametrize('name,value,path', [
    ('load_patterns_concurrent_levels','bad','load_test.patterns.concurrent.levels'),
    ('load_profile_short_input_tokens','0','load_test.profile.short.input_tokens'),
    ('load_profile_long_output_tokens','999999','load_test.profile.long.output_tokens'),
    ('load_profile_long_output_tokens','0','load_test.profile.long.output_tokens'),
    ('load_workloads_mixed_shares_short_short','bad','load_test.workloads.mixed.shares.short_short'),
    ('load_workloads_stress_min_output_percent','0','load_test.workloads.stress.min_output_percent'),
    ('load_pass_criteria_min_valid_responses','0','load_test.pass_criteria.min_valid_responses'),
    ('load_pass_criteria_max_first_answer_ms','0','load_test.pass_criteria.max_first_answer_ms'),
    ('load_pass_criteria_max_completion_ms','0','load_test.pass_criteria.max_completion_ms'),
    ('load_pass_criteria_max_error_percent','101','load_test.pass_criteria.max_error_percent'),
    ('load_pass_criteria_min_correct_percent','101','load_test.pass_criteria.min_correct_percent'),
])
def test_start_focuses_local_error_before_any_api_request(name, value, path):
    import json
    browser_logic(FORM + f"form.elements[{json.dumps(name)}].value={json.dumps(value)};\n" + r'''
if(form.elements.load_workloads_mixed_shares_short_short.value==='bad'){form.elements.load_workloads_mixed_enabled.checked=true;form.elements.load_workloads_mixed_share_mode.value='custom';}
if(form.elements.load_workloads_stress_min_output_percent.value==='0')form.elements.load_workloads_stress_enabled.checked=true;
validateRunForm=()=>true;canStartRun=()=>true;
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan:{rows:[],errors:[]}};
api=()=>assert.fail('local validation must precede the API');
await startRun({target:form,preventDefault(){}});
''' + f"assert.equal(focused.at(-1),{json.dumps(name)});\n" + r'''
assert.equal(state.setupStep,2);assert.equal(state.openTest,'load');assert.equal(nodes['#form-error'].hidden,false);
''' + f"const destination=settingDestination({json.dumps(path)});assert.equal(destination.name,focused.at(-1));\n" +
        ("assert(details.open);" if '.pass_criteria.' in path else ''))


def test_dormant_load_range_errors_do_not_block_form_submission():
    browser_logic(FORM + r'''
controls.find(input=>input.name==='check').value='access';
for(const [key,value] of Object.entries({load_patterns_concurrent_levels:'bad',load_profile_short_input_tokens:'0',load_workloads_mixed_shares_short_short:'wrong',load_workloads_stress_min_output_percent:'0',load_pass_criteria_min_valid_responses:'1'}))form.elements[key].value=value;
assert.equal(readForm(false).config.checks.join(','),'access');
''')


def test_concurrent_output_cap_can_exceed_suite_default():
    browser_logic(FORM + r'''
form.elements.load_profile_long_output_tokens.value='1024';
const read=readForm(false).config;
assert.equal(config.max_output_tokens,128);
assert.equal(read.max_output_tokens,128);
assert.equal(read.load_test.profile.long.output_tokens,1024);
''')


def test_actual_input_events_recalculate_ceil_coverage_without_broad_live_region():
    browser_logic(FORM + r'''
for(const id of ['load-share-note','load-output-threshold','load-interference-schedule','load-slowdown-help','load-ceiling-label','load-seed-field'])nodes['#'+id]={classList:{toggle(){}}};
const html=renderLoadEditor(state.config);assert.doesNotMatch(html,/<section[^>]*aria-live=/);assert.equal((html.match(/aria-live="polite"/g)||[]).length,2);
assert.equal((renderRunBlueprint(config).match(/aria-live="polite"/g)||[]).length,1);
form.elements.load_workloads_stress_enabled.checked=true;
const percent=form.elements.load_workloads_stress_min_output_percent,cap=form.elements.load_profile_long_output_tokens;
percent.value='80';cap.value='256';listeners.input({target:percent});
assert.match(nodes['#load-output-threshold'].textContent,/At least 205 generated tokens/);
cap.value='512';listeners.input({target:cap});assert.match(nodes['#load-output-threshold'].textContent,/At least 410 generated tokens/);
percent.value='50';listeners.input({target:percent});assert.match(nodes['#load-output-threshold'].textContent,/At least 256 generated tokens/);
form.elements.load_patterns_concurrent_levels.value='wrong';listeners.input({target:form.elements.load_patterns_concurrent_levels});
assert.match(nodes['#load-interference-schedule'].textContent,/Enter positive, increasing whole-number levels/);
form.elements.load_workloads_mixed_enabled.checked=true;form.elements.load_workloads_mixed_share_mode.value='custom';
form.elements.load_workloads_mixed_shares_short_short.value='wrong';listeners.input({target:form.elements.load_workloads_mixed_shares_short_short});
assert.match(nodes['#load-share-note'].textContent,/positive share/);
for(const node of Object.values(nodes))assert.doesNotMatch(node.textContent || '',/NaN/);
''')


def test_drawer_labels_enabled_panels_review_and_invalid_live_values():
    browser_logic(r'''
config.checks=['load'];const settings=config.load_test;
for(const type of Object.keys(loadTitles))settings.workloads[type].enabled=true;
settings.patterns.concurrent.levels=[4];settings.pass_criteria.max_first_answer_ms=123;settings.pass_criteria.max_completion_ms=456;
settings.pass_criteria.max_error_percent=2.5;settings.pass_criteria.min_correct_percent=98.25;settings.pass_criteria.min_valid_responses=1;
let html=renderLoadEditor(config);
for(const label of Object.values(loadCriteriaLabels))assert(html.includes(label),label);
for(const type of Object.keys(loadTitles))assert.match(html,new RegExp('id="load-panel-'+type+'" >'));
state.loadPanels.mixed=false;html=renderLoadEditor(config);assert.match(html,/id="load-panel-mixed" hidden/);
html=renderReviewPassCriteria(config);
for(const value of ['Load test','123','456','2.5','98.25','Minimum valid responses per result group: 1','Maximum allowed slowdown of short answers 1.5×','Minimum generated output 80%'])assert(html.includes(value),value);
settings.workloads.interference.enabled=false;settings.workloads.stress.enabled=false;html=renderReviewPassCriteria(config);assert.doesNotMatch(html,/Maximum allowed slowdown|Minimum generated output/);
settings.patterns.concurrent.levels=['wrong'];settings.workloads.mixed.share_mode='custom';settings.workloads.mixed.shares={short_short:'wrong'};
assert.doesNotMatch(renderLoadEditor(config)+renderLoadPlan(config)+JSON.stringify(loadDisplay(settings)),/NaN/);
assert.match(loadDisplay(settings).allocation,/Enter positive, increasing/);
assert.match(loadDisplay(settings).shares,/positive share/);
assert.match(effectiveCheckSummary(bootstrap.checks.find(check=>check.id==='load'),config),/Enter valid concurrency levels/);
''')


def test_old_records_group_with_their_original_check_and_concurrency():
    run_render_checks(r'''
const r={id:'old',target:'local',scenario:'bucket_c1_short_short',check_ids:['concurrency'],status:'completed',valid:true,elapsed_ms:10};
assert.equal(conditionInfo(r).check,'concurrency');
assert.match(renderTests([r]),/>1 at once</);
// Historical bucket ownership must also survive without a condition summary.
const bucket={...r,check_ids:['buckets']};
let html=renderTests([bucket]);assert.match(html,/>1 at once</);assert.match(html,/>1 test</);assert.doesNotMatch(html,/— at once|>1 tests</);
html=renderTests([bucket,{...bucket,id:'old2',scenario:'bucket_c1_long_short'}]);assert.match(html,/>2 tests</);
const generic={...r,scenario:'custom_old',check_ids:['correctness']};assert.equal(conditionInfo(generic).check,'correctness');
''')


def test_stress_workload_names_describe_the_experiment():
    run_render_checks(r'''
const base={level:4,type:'stress',status:'inconclusive',reason:'not sent',token_lengths:{},shape:'long_short',workload:'input'};
const html=renderLoadResults({load_types:{stress:{title:'Stress',points:[base,{...base,shape:'short_long',workload:'output'}]}}});
assert.match(html,/Large-input stress at 4 concurrent requests/);assert.match(html,/Large-output stress at 4 concurrent requests/);
''')


@pytest.mark.parametrize('key', ['concurrent_workloads', 'buckets'])
@pytest.mark.parametrize('checks', [['access'], ['concurrency']])
async def test_null_legacy_blocks_are_absent_without_notes(key, checks, tmp_path):
    source={'targets':[TARGET], 'checks':checks, key:None,
            'max_requests':None, 'max_duration_seconds':None, 'concurrency':None}
    migrated,notes=migrate_load_test(migrate_concurrent_load(source)[0])
    assert bool(notes) == ('concurrency' in checks)
    assert key not in migrated
    assert ConfigDraft.model_validate(migrated).checks == migrated["checks"]
    assert bool(TypeAdapter(ConfigDraftInput).validate_python(source)._migration_notes) == ('concurrency' in checks)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)),base_url='http://127.0.0.1:8787') as client:
        response=await client.post('/api/config/draft',json=source)
    assert response.status_code==200
    assert bool(response.json()['migration_notes']) == ('concurrency' in checks)
    assert key not in response.json()['config']


async def test_bootstrap_returns_migration_notes_for_legacy_launcher(tmp_path):
    app=create_app(tmp_path,defaults={'targets':[TARGET], 'checks':['buckets'], 'concurrent_workloads':{'samples_per_bucket':7}})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8787') as client:
        response=await client.get('/api/bootstrap')
    assert response.status_code==200
    body=response.json()
    assert any('samples_per_bucket=7' in note for note in body['migration_notes'])
    assert 'concurrent_workloads' not in body['config']


def test_applied_sources_replace_notes_but_same_source_validation_merges():
    browser_logic(r'''
state.config=structuredClone(config);state.setupStep=3;
const form={elements:{baseline_id:{value:''},mode:{value:'normal'},restart_target:{value:''}}};nodes['#new-run-form']=form;
for(const id of ['form-error','start-run','request-plan'])nodes['#'+id]={innerHTML:'',hidden:false};
captureFormDraft=()=>{};renderNew=()=>{};invalidateModelDiscovery=()=>{};toast=()=>{};renderRequestPreview=()=>{};setActive=()=>{};
readForm=()=>({config:structuredClone(state.config),mode:'normal'});validateRunForm=()=>true;
let sent;
api=async(path,options)=>{
 const source=JSON.parse(options.body);if(path==='/api/runs'){sent=source;return {id:'run'};}
 const normalized=structuredClone(source),notes=source.concurrent_workloads?['A migrated']:[];delete normalized.concurrent_workloads;
 return {config:normalized,migration_notes:notes,request_plan:{rows:[],errors:[]}};
};
await applyConfig(JSON.stringify({...config,concurrent_workloads:{samples_per_bucket:7}}));
assert.match(renderMigrationNotes(),/A migrated/);
state.dismissedMigrationNotes=['A migrated'];acceptMigrationNotes(['A migrated','A second note']);
assert.doesNotMatch(renderMigrationNotes(),/A migrated/);assert.match(renderMigrationNotes(),/A second note/);
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan:{rows:[],errors:[]}};
await startRun({preventDefault(){},target:form});assert.deepEqual(sent.migration_notes,['A migrated','A second note']);
await applyConfig(JSON.stringify(config));assert.equal(renderMigrationNotes(),'');assert.equal(state.dismissedMigrationNotes.length,0);
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan:{rows:[],errors:[]}};
await startRun({preventDefault(){},target:form});assert.deepEqual(sent.migration_notes,[]);
// New run starts a new source lifecycle, preserving the edited configuration.
state.migrationNotes=['old source'];state.dismissedMigrationNotes=['old source'];
enterRoute=()=>{};state.config.max_requests=33;await startNewRun();
assert.equal(state.config.max_requests,33);assert.equal(renderMigrationNotes(),'');assert.equal(state.migrationNotes.length,0);assert.equal(state.dismissedMigrationNotes.length,0);
''')


@pytest.mark.parametrize('legacy', ["{concurrent_workloads:null}", "{test_options:{concurrency:{samples:7}}}"])
def test_rerun_normalizes_null_legacy_keys_and_legacy_options(legacy):
    browser_logic(r'''
renderRequestPreview=()=>{};invalidateModelDiscovery=()=>{};let posted;
api=async(path,options)=>{assert.equal(path,'/api/config/draft');posted=JSON.parse(options.body);return {config:structuredClone(config),migration_notes:posted.test_options?.concurrency?['Legacy options dropped']:[]};};
state.detail={report:{manifest:{config:{...config,...''' + legacy + r'''}}}};
await rerun();assert(posted);assert.equal(state.config.concurrent_workloads,undefined);assert.equal(state.config.test_options?.concurrency,undefined);
assert.deepEqual(state.migrationNotes,posted.test_options?.concurrency?['Legacy options dropped']:[]);
''')


async def test_bootstrap_notes_show_and_are_sent_on_start(tmp_path):
    app=create_app(tmp_path,defaults={'targets':[TARGET], 'checks':['buckets'], 'buckets':{'samples_per_bucket':7}})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8787') as client:
        body=(await client.get('/api/bootstrap')).json()
    import json
    browser_logic(r'''
const response=''' + json.dumps(body) + r''';
api=async(path,options)=>{
 if(path==='/api/bootstrap')return response;if(path==='/api/baselines')return {baselines:[]};
 if(path==='/api/runs'&&!options)return {runs:[]};
 if(path==='/api/runs'){const sent=JSON.parse(options.body);assert.deepEqual(sent.migration_notes,response.migration_notes);return {id:'new'};}
 return {config:JSON.parse(options.body),migration_notes:[],request_plan:{rows:[],errors:[]}};
};
initReticulate=()=>{};connectionError=()=>{};setActive=()=>{};route=async()=>{};refreshRuns=async()=>{};
for(const id of ['suite-version','runner-location','form-error','start-run','request-plan'])nodes['#'+id]={innerHTML:'',hidden:false};
await init();assert.match(renderLoadEditor(state.config),/samples_per_bucket=7/);
const form={elements:{baseline_id:{value:''},mode:{value:'normal'},restart_target:{value:''}}};nodes['#new-run-form']=form;
readForm=()=>({config:structuredClone(state.config),mode:'normal'});validateRunForm=()=>true;renderRequestPreview=()=>{};
state.setupStep=3;state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan:{rows:[],errors:[]}};
await startRun({preventDefault(){},target:form});assert.equal(location.hash,'#runs/new');
''')


def test_server_interference_plan_wording_matches_one_point_per_level():
    from giraffe.planning import request_plan
    config=ConfigDraft(targets=[TARGET],checks=['concurrency'],concurrency=8,
        concurrent_load={'combinations':{'enabled':False},'mixed':{'enabled':False},
            'interference':{'enabled':True},'schedule':{'levels':[2,4,8],'duration_seconds':10}})
    plan=request_plan(config)
    row=next(row for row in plan['rows'] if row['id']=='load_interference')
    assert '3 tests, 6 sending windows; 60 s sending' in row['summary']
    assert plan['load_test']['workloads']['interference']['test_points']==3
    assert plan['configured_load_seconds']==60


async def test_bootstrap_preserves_notes_from_prevalidated_launcher(tmp_path):
    config=TypeAdapter(ConfigDraftInput).validate_python({
        'targets':[TARGET], 'checks':['buckets'], 'buckets':{'samples_per_bucket':7}})
    assert any('samples_per_bucket=7' in note for note in config._migration_notes)
    app=create_app(tmp_path,defaults=config.model_dump(mode='json'),migration_notes=config._migration_notes)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8787') as client:
        response=await client.get('/api/bootstrap')
    assert response.status_code==200
    assert response.json()['migration_notes']==config._migration_notes


def test_old_dismissed_draft_retains_notes_as_start_metadata():
    browser_logic(r'''
const note='Legacy note accepted before the UI update';
sessionStorage.setItem('giraffe-draft-v1',JSON.stringify({bootstrap:state.bootstrap.config,config,migrationNotes:[],dismissedMigrationNotes:[note]}));
restoreDraft();assert.equal(renderMigrationNotes(),'');assert.deepEqual(state.migrationNotes,[note]);
''')
