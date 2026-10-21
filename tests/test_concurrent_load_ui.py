"""Concurrent load's browser contract, exercised through the real UI in node VM."""
from tests.test_ux_catalogue import browser_logic
from tests.test_ux_results import run_render_checks


def test_drawer_structure_and_labels():
    browser_logic(r'''
const html=renderTestCard(bootstrap.checks.find(c=>c.id==='load'),config);let previous=-1;
for(const title of ['Data profile','Workloads','Traffic patterns','Request timing &amp; delivery','Pass criteria']){
 const index=html.indexOf((title==='Pass criteria'?'<summary>':'<h3>')+title);assert(index>previous,title);previous=index;
}
for(const label of ['Input tokens · target','Output tokens · cap','Choose one or more','This check only','Bursts use this same mix. Shares describe request counts, not tokens or time.',...Object.values(loadTitles),...Object.values(loadCriteriaLabels)])assert(html.includes(label),label);
for(const type of Object.keys(loadTitles)){assert(html.includes('name="load_workloads_'+type+'_enabled"'));assert(html.includes('data-load-collapse="'+type+'"'));assert(html.includes('aria-controls="load-panel-'+type+'"'));}
assert.equal((html.match(/name="load_workloads_combinations_selected_/g)||[]).length,9);
assert.equal((html.match(/name="load_workloads_mixed_selected_/g)||[]).length,9);
assert.equal((html.match(/name="load_workloads_stress_cases_/g)||[]).length,3);
assert.equal((html.match(/placeholder="Measure only"/g)||[]).length,4);
assert.doesNotMatch(html,/Load types|Load schedule|p95|Context length|Pairs per|pending|in.flight|data-profile-settings/);
assert.equal((html.match(/aria-live="polite"/g)||[]).length,2);assert.doesNotMatch(html,/<section[^>]*aria-live/);
assert.equal((renderRunBlueprint(config).match(/aria-live="polite"/g)||[]).length,1);
assert.match(effectiveCheckSummary(bootstrap.checks.find(c=>c.id==='load'),config),/1 workload · 1, 4 concurrent/);
assert.equal(bootstrap.checks.filter(c=>['load','concurrency','capacity','arrivals'].includes(c.id)).length,1);
''')


def test_shape_selection_collapse_and_live_text():
    from tests.test_concurrent_load_ui_review import FORM
    browser_logic(FORM + r'''
configChanged=()=>{};updateBounds=()=>{};
for(const kind of ['combinations','mixed']){
 for(const shape of loadShapes)form.elements['load_workloads_'+kind+'_selected_'+shape].checked=false;
 await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'load-select-grid'}}:q==='[data-load-grid]'?{dataset:{loadGrid:kind}}:null}});
 assert(loadShapes.every(shape=>form.elements['load_workloads_'+kind+'_selected_'+shape].checked));
 selectLoadShapes(kind);assert(loadShapes.every(shape=>!form.elements['load_workloads_'+kind+'_selected_'+shape].checked));
}
for(const type of Object.keys(loadTitles)){
 const panel={hidden:true},button={attributes:{},setAttribute(k,v){this.attributes[k]=v;}};nodes['#load-panel-'+type]=panel;nodes['[data-load-collapse="'+type+'"]']=button;
 setLoadPanel(type,true);assert.equal(panel.hidden,false);assert.equal(button.attributes['aria-expanded'],'true');
 setLoadPanel(type,false);assert.equal(panel.hidden,true);assert.equal(button.attributes['aria-expanded'],'false');
 const input=form.elements['load_workloads_'+type+'_enabled'];input.checked=true;
 await listeners.change({target:{name:input.name,checked:true,dataset:{loadEnable:type},matches:q=>q==='[data-load-enable]',closest:()=>null}});assert.equal(panel.hidden,false);assert(input.checked);
 setLoadPanel(type,false);assert(input.checked);
}
const s=loadSettings(config);s.workloads.mixed.selected=['short_short','long_short','short_long'];assert.equal(loadDisplay(s).shares,'33.3% expected per selected combination; actual counts vary.');
s.workloads.mixed.share_mode='custom';s.workloads.mixed.shares={short_short:20,long_short:30,short_long:40};assert.equal(loadDisplay(s).shares,'90% total · must equal 100%.');
s.profile.long.output_tokens=512;assert.match(loadDisplay(s).coverage,/At least 410 generated tokens/);
assert.match(loadDisplay(s).slowdown,/1.5× means up to 50% longer.*at most 3 seconds/);
s.patterns.concurrent.levels=[1,4,8];s.schedule.duration_seconds=10;assert.match(loadDisplay(s).allocation,/At 4 concurrent requests: 3 short streams, with room for one added long request/);assert.match(loadDisplay(s).allocation,/Level 1 is not applicable/);
const rows=loadShapes.map(shape=>({dataset:{loadShareRow:shape}}));groups['[data-load-share-row]']=rows;nodes['#load-shares']={};nodes['#load-share-note']={};syncLoadDisplay({...config,load_test:s});assert.equal(rows.filter(row=>!row.hidden).length,3);assert.equal(nodes['#load-shares'].hidden,false);
''')


def test_every_structured_error_has_inline_and_focus_destination():
    from tests.test_concurrent_load_ui_review import FORM
    browser_logic(FORM + r'''
state.jsonDraft=state.jsonBase='synced';localSettingErrors=()=>[];const markup=mount();
const paths=['concurrency','max_requests','max_duration_seconds','load_test.workloads','load_test.patterns.concurrent.levels.0','load_test.patterns.rates.0','load_test.workloads.combinations.selected.0','load_test.workloads.mixed.selected','load_test.workloads.mixed.shares','load_test.workloads.stress.cases','load_test.workloads.interference.trigger'];
for(const input of controls)if(input.name.startsWith('load_')&&input.name!=='load_rendered')paths.push(settingPath(input.name));
for(const path of paths){
 const dest=settingDestination(path);let input=form.elements[dest.name];
 if(!input){input={style:{},dataset:{},setAttribute(){},removeAttribute(){},getAttribute(){return '';},focus(){focused.push(dest.name);}};form.elements[dest.name]=input;nodes['#field-'+dest.name]=input;}
 nodes['#field-'+dest.name+'-error']={hidden:true};state.openTest=null;
 state.requestPreview={status:'error',revision:state.configRevision,route:state.routeVersion,errors:[{fields:[path],msg:'Server rejected '+path,actions:[{field:path,label:'Fix field'}]}]};
 syncInlineErrors();assert.equal(nodes['#field-'+dest.name+'-error'].hidden,false,path);
 assert.match(renderSettingIssues(),/data-action="edit-setting"/);assert(renderSettingIssues().includes('data-setting-path="'+path+'"'));
 await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'edit-setting'}}:q==='[data-setting-path]'?{dataset:{settingPath:path}}:null}});
 assert.equal(focused.at(-1),dest.name,path);assert.equal(state.setupStep,2);if(path.startsWith('load_test'))assert.equal(state.openTest,'load');
 const type=path.split('.')[2];if(Object.hasOwn(loadTitles,type))assert.equal(nodes['#load-panel-'+type].hidden,false);
}
assert.equal(settingDestination('load_test.patterns.concurrent.levels.0').name,'load_patterns_concurrent_levels');
assert.equal(settingDestination('load_test.workloads.combinations.selected.0').name,'load_workloads_combinations_selected_short_short');
state.configRevision++;syncInlineErrors();assert.equal(currentSettingErrors().length,0);
''')


def test_plan_breakdown_and_duration_rows_use_server_numbers():
    import json
    from giraffe.models import RunConfig
    from giraffe.planning import request_plan
    config=RunConfig(targets=[{'name':'local','url':'http://unused/v1','model':'test'}],checks=['load'])
    plan=request_plan(config)
    browser_logic('const plan='+json.dumps(plan)+r''';
config.checks=['load'];const html=renderRunBlueprint(config,plan);
assert.match(html,/Load test plan/);assert.match(html,/Scheduled load tests/);assert.match(html,/12<\/dd>/);assert.match(html,/3m 0s/);assert.match(html,/6m 0s/);assert.match(html,/Unknown \+ 135/);
assert.match(html,/Expected scheduled requests/);assert.match(html,/Execution order/);
assert.equal((html.match(/Estimated duration:/g)||[]).length,1);assert.match(html,/test traffic only/);
assert.doesNotMatch(renderLoadPlan(config),/12<\/dd>|3m 0s/); // No independent planner fallback.
''')


def test_migration_notes_survive_normalization_dismiss_and_start():
    browser_logic(r'''
state.jsonDraft=state.jsonBase='synced';state.config=structuredClone(config);config.checks=['access','concurrency'];
state.setupStep=3;state.routeVersion=2;
const form={elements:{baseline_id:{value:''},mode:{value:'normal'},restart_target:{value:''}}};nodes['#new-run-form']=form;
for(const id of ['form-error','start-run','request-plan'])nodes['#'+id]={innerHTML:'',hidden:false};
readForm=()=>({config:structuredClone(config),mode:'normal'});validateRunForm=()=>true;
let resultNotes=['Old samples_per_bucket=7 was dropped.'],sent,paths=[];
api=async(path,options)=>{paths.push(path);if(path==='/api/runs'){sent=JSON.parse(options.body);return {id:'new-run'};}const normalized=JSON.parse(options.body);delete normalized.concurrent_workloads;return {config:normalized,migration_notes:resultNotes,request_plan:{rows:[],errors:[]}};};
setActive=()=>{};
await refreshRequestPreview(undefined,{draft:true});
assert.match(renderLoadEditor(config),/Old samples_per_bucket=7/);
assert.match(nodes['#request-plan'].innerHTML,/Old samples_per_bucket=7/);
resultNotes=[];await refreshRequestPreview();await validatedSnapshot();
assert.equal(state.migrationNotes.length,1);assert(paths.includes('/api/config/draft'));assert(paths.includes('/api/config/preview'));assert(paths.includes('/api/config/validate'));
await startRun({preventDefault(){},target:form});assert.deepEqual(sent.migration_notes,['Old samples_per_bucket=7 was dropped.']);
assert.equal(sent.config.migration_notes,undefined);
// Dismissal hides notes but preserves the accepted metadata across saved drafts.
saveDraft();state.migrationNotes=[];restoreDraft();assert.equal(state.migrationNotes.length,1);
await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'dismiss-migration-notes'}}:null}});
assert.equal(state.migrationNotes.length,1);assert.equal(renderMigrationNotes(),'');
restoreDraft();assert.equal(state.migrationNotes.length,1);assert.equal(renderMigrationNotes(),'');
acceptMigrationNotes(['Old samples_per_bucket=7 was dropped.']);assert.equal(state.migrationNotes.length,1);assert.equal(renderMigrationNotes(),'');
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan:{rows:[],errors:[]}};
await startRun({preventDefault(){},target:form});assert.deepEqual(sent.migration_notes,['Old samples_per_bucket=7 was dropped.']);
// Rerunning legacy config recomputes notes rather than carrying saved report metadata forever.
state.detail={report:{manifest:{config:{...config,concurrent_workloads:{levels:[1,4]}}}},baseline_id:null};
resultNotes=['Legacy rerun migrated'];await rerun();assert.deepEqual(state.migrationNotes,['Legacy rerun migrated']);assert.equal(state.config.concurrent_workloads,undefined);
''')


def test_results_for_all_load_types_null_tokens_and_old_reports():
    run_render_checks(r'''
const point={id:'load_combo_long_short_c4',type:'combinations',workload:'long_short',shape:'long_short',level:4,requested_concurrency:4,
 configured_window_seconds:10,delivered_window_seconds:3.2,drain_seconds:.5,cut_short:true,sent:12,completed:11,failed:1,timed_out:0,cancelled:0,
 valid:10,required:5,scored:10,correct:10,correct_percent:100,client_mean_inflight:3.8,client_peak_inflight:4,server_evidenced_concurrency:4,
 load_qualified:true,first_output_p95_ms:50,response_p95_ms:250,requests_per_second:20,output_tokens_per_second:null,
 token_lengths:{long_short:{input_target:2048,output_cap:32,input_tokens:{median:null,min:null,max:null},output_tokens:{median:12,min:10,max:16}}},
 status:'fail',reason:'error threshold exceeded <endpoint>',reason_code:'error_rate'};
const mixed={...point,id:'load_mixed_c4',type:'mixed',shape:null,workload:'mixed',observed_mix:{short_short:{requested_percent:60,sent:8,sent_percent:66.7,inflight_seconds:1.2,inflight_percent:25},long_short:{requested_percent:40,sent:4,sent_percent:33.3,inflight_seconds:3.6,inflight_percent:75}}};
const interference={...point,id:'load_interference_probe_c4',type:'interference',shape:null,workload:'interference',short_stream_mean_inflight:2.8,short_stream_peak_inflight:3,short_stream_evidenced_concurrency:3,interference:{
 valid_short_streams:8,buffered_short_streams:1,ambiguous_short_streams:2,
 control:{matched_count:6,decode_median_ms:100,response_median_ms:150,longest_stream_pause_ms:20},probe:{matched_count:5,decode_median_ms:120,response_median_ms:180,longest_stream_pause_ms:40},slowdown:1.2,response_time_ratio:1.2,allocation:'3 short streams + 1 probe slot',trigger_latency_ms:{median:10,min:5,max:12},probes_never_triggered:0}};
const stress={...point,id:'load_stress_output_c4',type:'stress',workload:'output',shape:'short_long',coverage:{threshold_tokens:410,covered_valid_responses:3,required:5},status:'inconclusive',reason:'insufficient coverage'};
const metrics={configured_load_seconds:80,load_types:{combinations:{title:loadTitles.combinations,status:'fail',points:[point]},mixed:{title:loadTitles.mixed,status:'fail',points:[mixed]},interference:{title:loadTitles.interference,status:'fail',points:[interference]},stress:{title:loadTitles.stress,status:'inconclusive',points:[stress]}}};
const html=renderConcurrentResults(metrics);
for(const type of Object.keys(metrics.load_types))assert(html.includes('data-load-result="'+type+'"'));
for(const label of ['Workload','requested concurrency','delivered / configured','Sent / completed','failed / timeouts','Valid / required','Correct (%)','Client-delivered concurrency','Server-evidenced concurrency','First-output p95','Response p95','Requests/s','Output tokens/s','actual (median, min–max) / target','actual (median, min–max) / cap','Status / reason','How these are measured'])assert(html.includes(label),label);
assert.match(html,/not reported \/ 2,048/);assert.match(html,/not reported/);assert.doesNotMatch(html,/0 \/ 2,048/);
assert.match(html,/3.2 \/ 10 s/);assert.match(html,/Drain 0.5 s/);assert.match(html,/error threshold exceeded &lt;endpoint&gt;/);
assert.match(html,/Observed by sent requests/);assert.match(html,/Observed by in-flight time/);assert.match(html,/66.7%/);
assert.match(html,/Matched interference groups/);assert.match(html,/Short-only control/);assert.match(html,/Slowdown: 1.2×/);
assert.match(html,/Buffered short streams: 1 \/ 8/);assert.match(html,/Ambiguous A\/B overlaps excluded from both groups: 2/);
assert.match(html,/Short-stream concurrency mean \/ peak \/ server-evidenced: 2.8 \/ 3 \/ 3/);
assert.match(html,/3 \/ 5 required valid responses generated at least 410 tokens/);
const checkHtml=renderCheck(check('concurrency','fail',{metrics}),report([]));assert.match(checkHtml,/data-load-result="mixed"/);assert.match(checkHtml,/Requests completed \/ sent/);assert.match(checkHtml,/44 \/ 48/);assert.doesNotMatch(checkHtml,/>0 \/ 0</);
const live=renderLiveLoadResults({live_conditions:[{target:'local',load_point_result:interference},{target:'local',load_point_result:interference},{target:'local',load_point_result:mixed}]});
assert.equal((live.match(/data-load-point="load_interference_probe_c4"/g)||[]).length,1);
assert.match(live,/Observed by sent requests/);
// Old saved reports retain their scenario renderer, including verdict and bucket details.
const old=renderConcurrentResults({scenarios:{size_matrix:{title:'Original size matrix',status:'pass',summary:'Saved outcome',metrics:{completed:1,attempted:1,buckets:{bucket_c1_short_short:{completed:1,attempted:1,correct:1,scored:1,actual_input_tokens:[100],actual_output_tokens:[8]}}}}}});
assert.match(old,/data-concurrent-scenario="size_matrix"/);assert.match(old,/Original size matrix/);assert.match(old,/Saved outcome/);assert.doesNotMatch(old,/data-load-result/);
''')


def test_live_conditions_group_by_load_type_and_level_with_point_verdicts():
    run_render_checks(r'''
const conditions=['load_combo_long_short_c4','load_combo_short_long_c4','load_mixed_c4','load_mixed_c8','load_interference_control_c4','load_interference_probe_c4','load_stress_output_c4'].map(id=>({target:'local',condition:{id,check:'concurrency',label:phaseName(id)},planned:null,sent:0,status:'waiting'}));
state.detail={id:'live',config:{targets:[{name:'local'}]},live_conditions:conditions};
let html=renderTests([],{conditions,live:true});
for(const type of ['combinations','mixed','interference','stress'])assert(html.includes('data-condition-load-type="'+type+'"'));
assert.match(html,/Mixed traffic · 8 concurrent requests/);assert.match(html,/Long input → Short output at 4 concurrent requests/);
assert.equal((html.match(/data-condition-load-type="mixed"/g)||[]).length,2);
conditions[0].status='finished';conditions[0].reason='criteria met';conditions[0].load_point_result={status:'pass',cut_short:false,not_run:false};
html=renderTests([],{conditions,live:true});let first=html.match(/<article[\s\S]*?<\/article>/)[0];
assert.match(first,/Finished/);assert.match(first,/class="status pass"/);assert.doesNotMatch(first,/Stopped early/);
conditions[0].load_point_result.cut_short=true;html=renderTests([],{conditions,live:true});assert.match(html.match(/<article[\s\S]*?<\/article>/)[0],/Stopped early/);
const record={scenario:'load_mixed_c4.long_short',target:'local'};
assert.equal(conditionInfo(record).id,'load_mixed_c4');assert.equal(conditionInfo(record).check,'concurrency');
const completed=report([check('concurrency','fail',{metrics:{load_types:{combinations:{points:[{id:'load_combo_long_short_c4',status:'fail'}]}}}})]);
html=renderTests([],{conditions,report:completed});
assert.match(html,/class="status fail"/);
const calibration=[{target:'local',condition:{id:'load_calibration_long_long_output',check:'concurrency',load_type:'calibration',label:phaseName('load_calibration_long_long_output')}}];
html=renderTests([],{conditions:calibration,live:true});assert.match(html,/Input token calibration/);assert.doesNotMatch(html,/undefined|— concurrent requests/);
''')


async def test_live_finished_conditions_carry_authoritative_point_measurements():
    from tests.test_load_test_phase_a import config, Client
    from giraffe.runner import _Run
    from giraffe.load_test import execute
    c=config()
    c.load_test.schedule.duration_seconds=.01
    events=[]
    run=_Run(c,lambda event:events.append(event),None)
    obs=await execute(run,c.targets[0],Client((.001,)))
    finished=[event for event in events if event['event']=='condition_finished' and event['condition_summary'].get('load_point_result')]
    assert len(finished)==1
    assert finished[0]['condition_summary']['load_point_result']=={key:value for key,value in obs['points'][0].items() if key!='offers'}
    assert finished[0]['condition_summary']['load_point_result']['groups']
