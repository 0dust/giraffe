"""Concurrent load's browser contract, exercised through the real UI in node VM."""
from tests.test_ux_catalogue import browser_logic
from tests.test_ux_results import run_render_checks


def test_drawer_structure_and_labels():
    browser_logic(r'''
const html=renderTestCard(bootstrap.checks.find(c=>c.id==='concurrency'),config);
let previous=-1;
for(const title of ['Data profile','Load types','Load schedule','Pass criteria']){
  const index=html.indexOf('>'+title+'<');assert(index>previous,title);previous=index;
}
assert.match(html,/Input tokens · target/);assert.match(html,/Output tokens · cap/);
for(const type of ['combinations','mixed','interference','stress']){
  assert(html.includes('name="load_'+type+'_enabled"'));
  assert(html.includes('aria-controls="load-panel-'+type+'"'));
  assert(html.includes('data-load-collapse="'+type+'"'));
}
assert.equal((html.match(/name="load_combinations_selected_/g)||[]).length,9);
assert.equal((html.match(/name="load_mixed_shares_/g)||[]).length,9);
assert.doesNotMatch(html,/Streaming fairness|Context length|Samples per|Pairs per|weight|Sustained load|stage|data-profile-settings/);
assert.match(html,/name="test_mode_concurrency" value="default"/);
assert.match(html,/name="test_concurrency_request_timeout_seconds"/);
assert.doesNotMatch(html,/aria-live=/);
assert.equal((renderRunBlueprint(config).match(/aria-live="polite"/g)||[]).length,1);
assert.match(html,/At least 103 generated tokens/);
assert.match(effectiveCheckSummary(bootstrap.checks.find(c=>c.id==='concurrency'),config),/2 load types · 1, 4 concurrent/);
''')


def test_shape_selection_collapse_and_live_text():
    browser_logic(r'''
const form={elements:{}};nodes['#new-run-form']=form;
for(const shape of loadShapes)form.elements['load_combinations_selected_'+shape]={checked:false};
configChanged=()=>{};updateBounds=()=>{};
await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'load-select-all'}}:null}});assert(Object.values(form.elements).every(input=>input.checked));
await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'load-clear'}}:null}});assert(Object.values(form.elements).every(input=>!input.checked));
for(const type of Object.keys(loadTitles)){
 const panel={hidden:true},button={attributes:{},setAttribute(key,value){this.attributes[key]=value;}};
 nodes['#load-panel-'+type]=panel;nodes['[data-load-collapse="'+type+'"]']=button;
 setLoadPanel(type,true);assert.equal(panel.hidden,false);assert.equal(button.attributes['aria-expanded'],'true');
 setLoadPanel(type,false);assert.equal(panel.hidden,true);assert.equal(button.attributes['aria-expanded'],'false');
 // Enabling opens the panel; collapsing never changes selection.
 await listeners.change({target:{name:'load_'+type+'_enabled',checked:true,dataset:{loadEnable:type},matches:q=>q==='[data-load-enable]',closest:()=>null}});
 assert.equal(panel.hidden,false);
}
const settings=structuredClone(config.concurrent_load);
assert.match(concurrentDisplay(settings).coverage,/At least 103 generated tokens.*80%.*128-token cap/);
settings.profile.long.output_tokens=512;
assert.match(concurrentDisplay(settings).coverage,/At least 410 generated tokens/);
settings.mixed.shares={short_short:85};
assert.equal(concurrentDisplay(settings).total,'85% total · must equal 100%');
assert.equal(concurrentDisplay(settings).validTotal,false);
settings.mixed.shares={short_short:99.9999995};assert(concurrentDisplay(settings).validTotal);
settings.mixed.shares={short_short:100};assert.equal(concurrentDisplay(settings).total,'100% total');
settings.schedule.levels=[1,4,8];settings.schedule.duration_seconds=10;
const allocation=concurrentDisplay(settings).allocation;
assert.match(allocation,/At 4 concurrent requests: 3 short streams \+ 1 probe slot.*every 5 s; 20 s total/);
assert.match(allocation,/At 8 concurrent requests: 7 short streams/);
assert.match(allocation,/At 1 concurrent request:.*at least two/);
for(const id of ['load-share-total','load-output-coverage','load-interference-allocation','load-combination-count','load-test-point-example','load-trigger-chunks'])nodes['#'+id]={classList:{toggle(){}}};
settings.interference.trigger='after_chunks';syncConcurrentDisplay({...config,concurrent_load:settings});
assert.equal(nodes['#load-trigger-chunks'].hidden,false);assert.match(nodes['#load-output-coverage'].textContent,/410 generated tokens/);
settings.interference.trigger='first_output';syncConcurrentDisplay({...config,concurrent_load:settings});assert(nodes['#load-trigger-chunks'].hidden);
''')


def test_every_structured_error_has_inline_and_focus_destination():
    browser_logic(r'''
state.jsonDraft=state.jsonBase='synced';
const paths=['concurrent_load.schedule.levels','concurrency','max_requests','stream','max_duration_seconds',
'concurrent_load.schedule.duration_seconds','concurrent_load.interference.enabled','concurrent_load.combinations.selected',
'concurrent_load.mixed.shares','concurrent_load.stress.large_input','concurrent_load.combinations.enabled',
'concurrent_load.interference.trigger','concurrent_load.interference.trigger_chunks','concurrent_load.interference.max_slowdown',
'concurrent_load.stress.min_output_percent'];
for(const size of loadSizes)for(const field of ['input_tokens','output_tokens'])paths.push('concurrent_load.profile.'+size+'.'+field);
for(const field of ['first_output_p95_ms','response_p95_ms','max_error_percent','min_correct_percent','min_valid_responses'])paths.push('concurrent_load.pass_criteria.'+field);
for(const shape of loadShapes)paths.push('concurrent_load.mixed.shares.'+shape);
nodes['#main']={};nodes['#new-run-form']={elements:{mode:{value:'normal'}}};
syncTestControls=updateBounds=updateRestart=syncConfigView=syncPackControls=renderRequestPreview=syncSetupStep=renderModelDiscovery=()=>{};
renderNew();
const markup=nodes['#main'].innerHTML,focused=[];
revealSetupStep=step=>state.setupStep=step;revealCatalogueCheck=id=>state.openTest=id;
for(const path of paths){
 // A fresh, closed drawer and disclosure for each independent navigation.
 for(const key of Object.keys(nodes))delete nodes[key];
 const elements={},details={tagName:'DETAILS',open:false},form={elements};
 nodes['#new-run-form']=form;nodes['#load-trigger-chunks']={hidden:true};
 state.openTest=null;state.setupStep=3;state.loadPanels={};
 const dest=settingDestination(path);assert(markup.includes('id="field-'+dest.name+'"'),path);
 const input={dataset:{},parentElement:path.includes('.pass_criteria.')||!path.startsWith('concurrent_load.')?details:null,attributes:{},style:{},setAttribute(k,v){this.attributes[k]=v;},getAttribute(k){return this.attributes[k]||null;},removeAttribute(k){delete this.attributes[k];},focus(){focused.push(dest.name);},closest(){return null;},checked:true};
 elements[dest.name]=input;nodes['#field-'+dest.name]=input;nodes['#field-'+dest.name+'-error']={hidden:true,textContent:''};
 elements.load_interference_enabled ||= {checked:true};
 for(const type of Object.keys(loadTitles)){nodes['#load-panel-'+type]={hidden:true};nodes['[data-load-collapse="'+type+'"]']={setAttribute(){}};}
 const error={fields:[path],msg:'Server rejected '+path};
 state.requestPreview={status:'error',revision:state.configRevision,route:state.routeVersion,errors:[error]};
 syncInlineErrors();
 assert.equal(nodes['#field-'+dest.name+'-error'].hidden,false,path);
 assert.equal(input.attributes['aria-invalid'],'true',path);
 const issues=renderSettingIssues();assert(issues.includes('data-setting-path="'+path+'"'));assert.match(issues,/Edit /);
 await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'edit-setting'}}:q==='[data-setting-path]'?{dataset:{settingPath:path}}:null}});
 assert.equal(state.setupStep,path==='stream'?1:2,path);assert.equal(focused.at(-1),dest.name,path);
 if(path.includes('.pass_criteria.')||!path.startsWith('concurrent_load.'))assert(details.open,path);
 if(path.startsWith('concurrent_load.'))assert.equal(state.openTest,'concurrency',path);
 const type=path.split('.')[1];if(Object.hasOwn(loadTitles,type))assert.equal(nodes['#load-panel-'+type].hidden,false,path);
 if(path.endsWith('.trigger_chunks'))assert.equal(nodes['#load-trigger-chunks'].hidden,false);
}
// Suite request defaults are JSON-only; navigation still preserves their errors.
let jsonFocused=false;
nodes['#config-editor']={focus(){jsonFocused=true;}};
switchConfigView=view=>state.configView=view;
focusSetting('max_output_tokens');
assert.equal(state.configView,'json');assert(jsonFocused);
assert.match(renderSettingIssues([{fields:['max_output_tokens'],msg:'Invalid suite output default'}]),/data-setting-path="max_output_tokens"/);
// Supply a fresh schedule input for the cross-field level-1 navigation.
const schedule={parentElement:null,setAttribute(){},focus(){},closest(){return null;}};
nodes['#new-run-form'].elements.load_schedule_levels=schedule;
// The level-1 interference error opens its panel while focusing the schedule.
setLoadPanel('interference',false);focusSetting('concurrent_load.schedule.levels');
assert.equal(nodes['#load-panel-interference'].hidden,false);assert.equal(nodes['#load-trigger-chunks'].hidden,true);
assert.equal(settingDestination('concurrent_load.schedule.levels.0').name,'load_schedule_levels');
assert.equal(settingDestination('concurrent_load.combinations.selected.0').name,'load_combinations_selected_short_short');
// Current server responses own the error set; stale plans never block the new draft.
state.configRevision++;syncInlineErrors();assert.equal(currentSettingErrors().length,0);
''')


def test_plan_breakdown_and_duration_rows_use_server_numbers():
    browser_logic(r'''
config.checks=['concurrency'];config.concurrent_load.combinations.selected=[...loadShapes];
config.concurrent_load.mixed.enabled=false;config.concurrent_load.schedule={levels:[1,4,8],duration_seconds:30};
let html=renderConcurrentPlan(config);
assert.match(html,/13 min 30 s/);assert.match(html,/9 workloads × 3 levels × 30 s/);assert.match(html,/27 test points/);
const plan={configured_load_seconds:60,concurrent_load:{interference:{title:loadTitles.interference,workloads:2,levels:[2,4,8],duration_seconds:10,test_points:3,configured_load_seconds:60}},
 rows:[{id:'load_interference',title:loadTitles.interference,kind:'duration',requests_per_target:null,summary:'Duration-based',source:'configured'}],
 load_warnings:[{fields:['max_requests'],msg:'Request allowance may shorten the windows.'},{fields:['concurrent_load.schedule.duration_seconds','concurrent_load.profile.long.output_tokens'],msg:'Throughput window may be too short.'}],estimated_seconds:200,estimated_low_seconds:180,estimated_high_seconds:220};
html=renderRunBlueprint(config,plan);
assert.match(html,/Configured load time/);assert.match(html,/1 workload \(alternating short\/long probe\) × 3 levels × 20 s/);assert.match(html,/3 test points/);
assert.match(html,/Duration-based/);assert.doesNotMatch(html,/0 requests/);
assert.equal((html.match(/Estimated duration:/g)||[]).length,1);assert.match(html,/test traffic only/);assert.match(html,/Edit Stop after this many requests/);assert.match(html,/Edit duration per workload/);
nodes['#request-plan']={innerHTML:''};state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan,config};
renderRequestPlan(config);html=nodes['#request-plan'].innerHTML;
assert.match(html,/Duration-based · as many as complete/);assert.match(html,/Configured load time/);assert.doesNotMatch(html,/0 requests/);
const input={attributes:{},getAttribute(k){return this.attributes[k]||'';},setAttribute(k,v){this.attributes[k]=v;},removeAttribute(k){delete this.attributes[k];}};
nodes['#field-max_requests']=input;nodes['#field-max_requests-warning']={hidden:true};
syncInlineErrors();assert.equal(nodes['#field-max_requests-warning'].hidden,false);
assert.match(nodes['#field-max_requests-warning'].innerHTML,/Request allowance.*Edit Stop after this many requests/);
assert.equal(input.attributes['aria-invalid'],undefined);assert.match(input.attributes['aria-describedby'],/field-max_requests-warning/);
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
assert.match(renderConcurrentEditor(config),/Old samples_per_bucket=7/);
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
assert.match(html,/Mixed load · 8 concurrent requests/);assert.match(html,/Long input → Short output at 4 concurrent requests/);
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
    from tests.test_concurrent_load import perform, point

    _, check, _, events = await perform()
    finished = [event for event in events if event['event'] == 'condition_finished'
                and event['condition']['id'].startswith('load_combo_')
                and event['condition_summary'].get('load_point_result')]
    assert len(finished) == 1
    assert finished[0]['condition_summary']['load_point_result'] == point(check)
    assert finished[0]['condition_summary']['load_point_result']['reason_code'] == 'criteria_met'
