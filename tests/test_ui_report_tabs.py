"""Report navigation and measurement selection without a browser server."""

import pytest

from tests.test_ui_navigation import run_browser_script
from tests.test_ux_results import run_render_checks


PERFORMANCE = r"""
function performanceFixture(target='local',windowId='full') {
  const windows=[
    {id:'full',label:'Full request window',start_ms:0,end_ms:10000,duration_s:10,request_count:8,cohort_id:'measured',includes_startup:true},
    {id:'stage-1',label:'Stage C4',start_ms:2000,end_ms:6000,duration_s:4,request_count:3,cohort_id:'measured'}
  ];
  const metric=(label,value,unit,state,extra={})=>({label,value,unit,state,source:'Client request lifetimes',reason:'',action:'',sample_count:8,eligible_count:8,...extra});
  return {performance_schema:1,default_target:'local',selected_target:target,selected_window:windowId,
    available_targets:[{target:'local',identity:{model:'Model A'}},{target:'other',identity:{model:'Model B'}}],
    targets:[{target,identity:{model:target==='local'?'Model A':'Model B'},default_window:'full',selected_window:windowId,windows,
      summaries:{[windowId]:{kpis:['throughput','ttft','pace'],metrics:{
        throughput:metric('Request throughput',0,'req/s','measured'),
        ttft:metric('Client TTFT',null,'s','not_measured',{reason:'Old timing schema has no first-token evidence',action:'Collect timing schema 2 records'}),
        pace:metric('Generation pace',12.5,'tok/s','estimated',{source:'Generation-rate estimate'})
      },sections:{}}}}]};
}
"""


def test_saved_report_defaults_to_results_with_honest_performance_kpis_and_windows():
    run_render_checks(PERFORMANCE + r"""
detail(report([check('correctness','pass'),check('capacity','inconclusive'),check('skipped','skipped')],{performance:performanceFixture()}));
const tabs=$$('[role="tab"]');assert.equal(tabs.length,2);
assert.deepEqual(tabs.map(tab=>tab.textContent),['Results','Performance']);
assert.equal($('#tab-results').getAttribute('aria-selected'),'true');
assert.equal($('#report-performance').hidden,true);assert.equal($('#report-results').hidden,false);
assert.equal($$('.finding-check').length,2);assert.equal($('[data-finding="local:skipped"]'),null);
assert.match($('.checks-not-selected').textContent,/Not selected: skipped check/);
assert(!$('.checks-not-selected').open);assert.match($('.check-count').textContent,/2 checks/);
chooseReportTab('performance');
assert.equal($('#report-performance').getAttribute('aria-labelledby'),'tab-performance');
assert.equal($('#report-results').getAttribute('aria-labelledby'),'tab-results');
const cards=$$('.performance-kpi');assert.equal(cards.length,3);
assert.equal(cards[0].querySelector('strong').textContent,'0 req/s');
assert.equal(cards[1].querySelector('strong').textContent,'—');
assert.match(cards[1].textContent,/Not measured|Not Measured/);
assert.match(cards[1].textContent,/Old timing schema/);assert.match(cards[1].textContent,/Collect timing schema 2/);
assert.match(cards[2].textContent,/12.5 tok\/s/);assert.match(cards[2].textContent,/Estimated/);
assert.match($('#performance-window').textContent,/Full request window · 0–10 s · 8 requests/);
assert.match($('#performance-window').textContent,/Stage C4 · 2–6 s · 3 requests/);
assert.equal($('#performance-target').querySelectorAll('option').length,2);
assert.equal($('#report-sections').hidden,false);assert.equal($('#report-sections').querySelectorAll('a').length,9);
assert.match($('#report-performance').textContent,/View test results/);
chooseReportTab('results');assert.equal($('#report-sections').hidden,true);
assert.equal($('#report-performance').hidden,true);assert.equal($('#report-results').hidden,false);
""", dom=True)



def test_every_terminal_outcome_defaults_to_results_and_preserves_performance_deep_links():
    run_render_checks(r"""
for(const [index,status] of ['pass','fail','inconclusive','blocked'].entries()){
 const id='terminal-'+index;
 location.hash='#runs/'+id;
 detail(report([check('correctness',status)],{overall:status}),{id,state:index===3?'cancelled':'completed'});
 assert.equal(reportView.tab,'results');assert(!$('#report-results').hidden);
 assert($('#report-performance').hidden);
 const linked=id+'-performance';location.hash='#runs/'+linked+'/performance';
 detail(report([check('correctness',status)],{overall:status}),{id:linked});
 assert.equal(reportView.tab,'performance');assert(!$('#report-performance').hidden);
}
""", dom=True)


def test_live_and_terminal_default_to_results_unless_the_user_chose_performance():
    run_render_checks(r"""
const config={targets:[{name:'local',model:'m'}],max_requests:4,concurrency:2};
function live(id){state.detail={id,state:'running',config,progress:{},events:[],live_requests:[]};renderDetail();}
function finish(){state.detail={...state.detail,state:'completed',report:report([check('correctness','pass')])};renderDetail();}
live('automatic');assert.equal($('#tab-results').getAttribute('aria-selected'),'true');
assert.equal($('#report-sections').hidden,true);finish();
assert.equal($('#tab-results').getAttribute('aria-selected'),'true');
assert.equal($('#report-sections').hidden,true);
chooseReportTab('results');renderDetail();renderDetail();
assert.equal($('#tab-results').getAttribute('aria-selected'),'true');
live('explicit-results');chooseReportTab('results');finish();
assert.equal($('#tab-results').getAttribute('aria-selected'),'true');
live('explicit-performance');chooseReportTab('performance');finish();
assert.equal($('#tab-performance').getAttribute('aria-selected'),'true');
""", dom=True)


def test_tabs_support_roving_keyboard_focus_and_same_run_history_keeps_dialogs():
    run_render_checks(r"""
(async()=>{
detail(report([check('correctness','pass')]));
const expect=tab=>{
  assert.equal($$('[role="tab"][tabindex="0"]').length,1);
  assert.equal($('#tab-'+tab).getAttribute('aria-selected'),'true');
  assert.equal($('#report-'+tab).hidden,false);
};
const key=key=>listeners.keydown({key,target:document.activeElement,preventDefault(){}});
$('#tab-results').focus({preventScroll:true});key('ArrowRight');expect('performance');
assert.equal(document.activeElement.id,'tab-performance');assert.equal(location.hash,'#runs/saved-run/performance');
key('ArrowRight');expect('results');key('End');expect('performance');key('Home');expect('results');
const dialog=$('#request-dialog');dialog.showModal();const version=state.routeVersion;
api=async()=>{throw Error('A tab history entry must not reload the run');};
location.hash='#runs/saved-run/results';await route();expect('results');assert(dialog.open);
location.hash='#runs/saved-run/performance';await route();expect('performance');assert(dialog.open);
assert.equal(state.routeVersion,version);
location.hash='#runs/saved-run';await route();expect('results');
})()
""", dom=True)


def test_explicit_tab_hashes_and_encoded_ids_open_the_requested_run():
    run_browser_script(r"""
const page=browser(defaults,'#runs/saved%20run/results');await page.run('init()');
assert(page.paths.includes('/api/runs/saved%20run'));
assert.equal(page.run('state.detail.id'),'saved run');assert.equal(page.run('reportView.tab'),'results');
const calls=page.paths.filter(path=>path==='/api/runs/saved%20run').length,version=page.run('state.routeVersion');
page.location.hash='#runs/saved%20run/performance';await page.windowListeners.hashchange();
assert.equal(page.run('reportView.tab'),'performance');assert.equal(page.run('state.routeVersion'),version);
assert.equal(page.paths.filter(path=>path==='/api/runs/saved%20run').length,calls);
page.location.hash='#runs/saved%20run/results';await page.windowListeners.hashchange();
assert.equal(page.run('reportView.tab'),'results');
page.location.hash='#runs/other';await page.windowListeners.hashchange();
assert.equal(page.run('reportView.tab'),'results');assert(page.paths.includes('/api/runs/other'));
""")


def test_results_put_single_check_findings_before_collapsed_conditions_and_diagnostics():
    run_render_checks(r"""
const record={id:'request-1',target:'local',scenario:'initial',status:'completed',valid:true,score:true};
detail(report([check('correctness','fail',{summary:'A required answer was wrong',evidence_ids:['request-1']})],{
  requests:[record],abort_reason:'Stopped after endpoint errors',conditions:[{target:'local',condition_id:'initial',check_id:'correctness',name:'Initial request',aggregate_status:'failed',planned:1,planned_kind:'exact',started:1,running:0,completed:1,failed:1,not_started:0}]
}),{state:'completed'});
chooseReportTab('results');
const panel=$('#report-results'),html=panel.innerHTML;
const positions=['id="result-content"','condition-reference','all-requests','run-diagnostics'].map(value=>html.indexOf(value));
assert(positions.every(value=>value>=0),html);assert.deepEqual([...positions].sort((a,b)=>a-b),positions);
assert.equal(panel.querySelector('.check-verdict-summary'),null,'finished checks are listed only once');
assert.equal(panel.querySelectorAll('.finding-check').length,1);
assert.match(panel.querySelector('.finding-check').textContent,/A required answer was wrong/);
const conditions=panel.querySelector('details.condition-reference');assert(conditions);assert(!conditions.open);
assert(conditions.querySelector('.tests-section'));
const diagnostics=panel.querySelector('details.run-diagnostics');assert(diagnostics);assert(!diagnostics.open);
assert(diagnostics.querySelector('.resource-summary'));
assert.match($('#main').querySelector('h1').textContent,/Answer correctness failed/);
assert.match(panel.querySelector('.resource-summary').textContent,/Peak memory \(RAM\)/);
assert.equal($('#report-performance').querySelector('.resource-summary'),null);
assert.match($('#report-performance').textContent,/Partial evidence: Stopped after endpoint errors/);
""", dom=True)


def test_measurement_and_target_changes_fetch_selected_summary_and_reject_stale_response():
    run_render_checks(PERFORMANCE + r"""
(async()=>{
detail(report([check('correctness','pass')],{performance:performanceFixture()}));
const calls=[];let finishStage;
api=path=>{
  calls.push(path);
  if(path.endsWith('window=stage-1'))return new Promise(resolve=>finishStage=resolve);
  return Promise.resolve(performanceFixture('other','full'));
};
const change=(id,value)=>{const select=$('#'+id);select.value=value;listeners.change({target:select});};
change('performance-window','stage-1');
assert.equal(calls.length,1);assert.equal(calls[0],'/api/runs/saved-run/performance?window=stage-1');
change('performance-target','other');await Promise.resolve();await Promise.resolve();
assert.equal(calls.length,1,'rapid selections keep one request in flight');
finishStage(performanceFixture('local','stage-1'));await Promise.resolve();await Promise.resolve();await Promise.resolve();await Promise.resolve();
assert.equal(calls[1],'/api/runs/saved-run/performance?target=other');
assert.match($('#report-performance').textContent,/Model B/);
assert.equal(calls.length,2,'only the latest queued selection is loaded');
assert.equal($('#performance-window').querySelector('[selected]').getAttribute('value'),'full');
})()
""", dom=True)


def test_live_partial_measurements_disclose_retention_and_absent_data_is_not_zero():
    run_render_checks(PERFORMANCE + r"""
const config={targets:[{name:'local',model:'m'}],max_requests:1000,concurrency:32};
state.detail={id:'live-no-measurements',state:'running',config,progress:{},events:[],live_requests:[]};renderDetail();
chooseReportTab('performance');
assert.match($('#report-performance').textContent,/No performance measurements available/);
assert.equal($$('.performance-kpi').length,0);
const data=performanceFixture();data.coverage={partial:true,retained_requests:250,started_requests:1000,reason:'Only the latest 250 of 1,000 started requests are retained'};
reportView.performance=data;renderDetail();
assert.match($('#report-performance').textContent,/Only the latest 250 of 1,000 started requests are retained/);
assert.equal($('#tab-performance').getAttribute('aria-selected'),'true');
""", dom=True)


def test_compact_requests_fetch_escaped_evidence_and_return_to_the_condition_page():
    run_render_checks(r"""
(async()=>{
const compact=Array.from({length:5},(_,index)=>({id:'request-'+index,target:'local',scenario:'initial',status:'completed',output_chars:17,score:false}));
const source=report([check('correctness','fail')],{requests_compact:true,requests:compact});
const before=JSON.stringify(source);detail(source);chooseReportTab('results');
const tests=$('#test-dialog');tests.innerHTML='<button data-condition-request="request-0">#0001</button>';tests.showModal();
const requestLink=tests.querySelector('[data-condition-request]');requestLink.focus({preventScroll:true});
const calls=[];api=async path=>{
  calls.push(path);const id=decodeURIComponent(path.split('/').at(-1));
  return {id,target:'local',scenario:'initial',status:'completed',valid:true,score:false,
    output:'<actual answer>',output_chars:15,score_message:'Mismatch <expected>',
    evaluation:{scorer:'exact',messages:[{role:'user',content:'<input message>'}],expected_provided:true,expected:'<expected answer>'}};
};
await openRequest('request-0');const evidence=$('#request-dialog');
assert.equal(calls[0],'/api/runs/saved-run/requests/request-0');assert(evidence.open);assert(tests.open);
assert.match(evidence.textContent,/Incorrect answer/);assert.match(evidence.textContent,/Mismatch <expected>/);
assert.match(evidence.innerHTML,/&lt;actual answer(?:&gt;|>)/);assert.match(evidence.innerHTML,/&lt;input message(?:&gt;|>)/);
assert.match(evidence.innerHTML,/&lt;expected answer(?:&gt;|>)/);assert.doesNotMatch(evidence.innerHTML,/<actual answer>|<input message>|<expected answer>/);
assert.match(evidence.textContent,/Back to test details/);
closeRequestEvidence();assert(!evidence.open);assert(tests.open);assert.equal(document.activeElement,requestLink);
await openRequest('request-0');assert.equal(calls.length,1,'reopening retained evidence uses the small cache');closeRequestEvidence();
for(let index=1;index<5;index++){await openRequest('request-'+index);closeRequestEvidence();assert(requestEvidenceCache.size<=3);}
await openRequest('request-0');assert.equal(calls.length,6,'older evidence is evicted rather than retained for every request');
assert.equal(JSON.stringify(source),before,'inspection must leave the compact report unchanged');
})()
""", dom=True)


def test_late_request_evidence_cannot_reopen_closed_dialog_or_cross_a_run_route():
    run_render_checks(r"""
(async()=>{
detail(report([check('correctness','pass')],{requests_compact:true,requests:[{id:'slow-request',target:'local',scenario:'initial',status:'completed'}]}));
let finishRequest;
api=path=>path.endsWith('/requests/slow-request')?new Promise(resolve=>finishRequest=resolve):Promise.reject(Error('Unexpected '+path));
let pending=openRequest('slow-request');assert($('#request-dialog').open);
closeRequestEvidence();finishRequest({id:'slow-request',output:'LATE CLOSED RESPONSE'});await pending;
assert(!$('#request-dialog').open);assert.doesNotMatch($('#request-dialog').textContent,/LATE CLOSED RESPONSE/);
assert.equal(requestEvidenceCache.size,0);
pending=openRequest('slow-request');assert($('#request-dialog').open);
// Supply the navigation shell that the small renderer fixture does not include.
for(const id of ['nav-runs','nav-new','breadcrumb']){const node=document.createElement('div');node.setAttribute('id',id);document.body.insertBefore(node,null);}
enterRoute=()=>{};
const next=report([check('correctness','pass')],{run_id:'other-run'});
const requestApi=api;api=path=>path==='/api/runs/other-run'?Promise.resolve({id:'other-run',state:'completed',report:next}):requestApi(path);
location.hash='#runs/other-run/performance';await route();assert.equal(state.detail.id,'other-run');assert(!$('#request-dialog').open);
finishRequest({id:'slow-request',output:'LATE WRONG RUN RESPONSE'});await pending;
assert(!$('#request-dialog').open);assert.doesNotMatch($('#request-dialog').textContent,/LATE WRONG RUN RESPONSE/);
assert.doesNotMatch($('#main').textContent,/LATE WRONG RUN RESPONSE/);assert.equal(requestEvidenceCache.size,0);
})()
""", dom=True)


def test_returning_to_the_same_live_run_restores_its_navigation_and_polling():
    run_browser_script(r"""
const page=browser(defaults,'#runs/live/results');await page.run('init()');
await page.run(`
(async()=>{
 let scheduled; schedulePoll=fn=>scheduled=fn;
 const config=state.config;
 api=async path=>path==='/api/runs/live'?{id:'live',state:'running',config,events:[],progress:{}}:{runs:[]};
 location.hash='#runs/live/results';state.reportRouteActive=false;await route();
 assert.equal(scheduled,pollDetail);assert.equal(reportView.tab,'results');
 for(const elsewhere of ['#runs','#new']){
   location.hash=elsewhere;await route();
   assert.equal(scheduled,elsewhere==='#new'?pollNew:pollRuns);
   assert.equal(state.reportRouteActive,false);
   const before=state.routeVersion;
   location.hash='#runs/live/results';await route();
   assert(state.routeVersion>before);assert.equal(scheduled,pollDetail);
   assert.equal(state.pageLabel,'Run detail');assert.equal(reportView.tab,'results');
 }
})()
`);
""")


def test_terminal_report_rejects_a_pending_live_performance_response():
    run_render_checks(PERFORMANCE+r"""
(async()=>{
 state.detail={id:'finishing',state:'running',config:{targets:[]},live_requests:[]};renderDetail();
 chooseReportTab('performance',{anchor:false});
 let resolve;api=()=>new Promise(done=>resolve=done);
 const pending=loadPerformance({force:true});
 const final=performanceFixture();final.targets[0].summaries.full.metrics.throughput.value=88;
 state.detail={id:'finishing',state:'completed',report:report([check('access','pass')],{performance:final})};renderDetail();
 resolve(performanceFixture());await pending;
 assert.equal(selectedPerformance().summary.metrics.throughput.value,88);
 assert.equal(reportView.pending,false);assert.equal(reportView.tab,'performance');
})()
""", dom=True)


def test_inspect_comparison_opens_preserved_disclosure_on_results_tab():
    run_render_checks(r"""
(async()=>{
 const value=report([check('access','pass')],{overall:'fail',baseline:{status:'fail',rows:[]}});
 detail(value);const reference=$('#report-baseline-reference');assert(reference.open);reference.open=false;
 await listeners.click({target:$('[data-tab="baseline"]'),preventDefault(){}});
 assert.equal(reportView.tab,'results');assert.equal(reference.open,true);
 assert.equal(document.activeElement,$('summary',reference));
})()
""", dom=True)


def test_condition_dialog_closing_after_terminal_switch_reveals_its_focus_target():
    run_render_checks(r"""
(async()=>{
 const aggregate={target:'local',condition_id:'one',check_id:'correctness',name:'One condition',aggregate_status:'running',planned:2,planned_kind:'exact',started:2,running:1,completed:1,failed:0,not_started:0};
 state.detail={id:'finishing-dialog',state:'running',config:{targets:[]},live_conditions:[aggregate]};renderDetail();
 api=async()=>({aggregate,rows:[],totals:{running:1},page:1,pages:1,total:0});
 const cell=$('.condition-spot');openConditionDialog(cell.dataset.conditionKey);await Promise.resolve();await Promise.resolve();
 aggregate.aggregate_status='passed';aggregate.running=0;aggregate.completed=2;
 state.detail={id:'finishing-dialog',state:'completed',report:report([check('correctness','pass')],{conditions:[aggregate]})};renderDetail();
 assert.equal(reportView.tab,'results');assert($('#test-dialog').open);assert(!$('#report-results').hidden);
 closeConditionDialog();assert.equal(reportView.tab,'results');assert(!$('#report-results').hidden);
 assert.equal(document.activeElement,$('.condition-spot'));assert.equal(document.activeElement.dataset.conditionKey,cell.dataset.conditionKey);
 assert($('.condition-reference').open,'restored condition focus must be visible');
})()
""", dom=True)


def test_missing_terminal_report_settles_an_open_live_condition_dialog():
    run_render_checks(r"""
(async()=>{
 const aggregate={target:'local',condition_id:'one',check_id:'correctness',name:'One condition',aggregate_status:'running',planned:2,planned_kind:'exact',started:2,running:1,completed:1,failed:0,not_started:0};
 state.detail={id:'save-failure',state:'running',config:{targets:[]},live_conditions:[aggregate]};renderDetail();
 api=async()=>({aggregate,rows:[],totals:{running:1},page:1,pages:1,total:0});
 openConditionDialog($('.condition-spot').dataset.conditionKey);await Promise.resolve();await Promise.resolve();
 state.detail={...state.detail,state:'error',error:'Disk unavailable',report:null,summary:{overall:'inconclusive',request_count:2,checks:[]}};renderDetail();
 assert(!$('#test-dialog').open);assert.equal(conditionDialog,null);assert.equal(reportView.tab,'results');
 assert.equal(document.activeElement,$('#tab-results'));assert.match($('#main').textContent,/Disk unavailable|Only a summary was saved/);
 assert(!$('.condition-spot').classList.contains('running'));
})()
""", dom=True)


def test_evidence_close_keeps_focus_in_test_dialog_after_its_filtered_row_disappears():
    run_render_checks(r"""
(async()=>{
 const row={id:'r1',target:'local',scenario:'initial',status:'completed',valid:true,score:true,output:'OK'};
 detail(report([check('access','pass')],{requests:[row]}));chooseReportTab('results',{anchor:false});
 const tests=$('#test-dialog');tests.innerHTML='<label>Status <select id="condition-filter"></select></label><button data-condition-request="r1">#0001</button>';tests.showModal();
 const original=$('[data-condition-request]',tests);original.focus({preventScroll:true});await openRequest('r1');original.parentNode.removeChild(original);
 assert(!original.isConnected);closeRequestEvidence();assert(tests.open);assert.equal(document.activeElement,$('#condition-filter'));
})()
""", dom=True)


def test_performance_response_after_leaving_a_run_does_not_replace_the_next_page():
    run_browser_script(r"""
const page=browser(defaults,'#runs/saved');await page.run('init()');
await page.run(`
(async()=>{
 for(let i=0;i<8;i++)await Promise.resolve();
 let finish;api=path=>path.includes('/performance')?new Promise(resolve=>finish=resolve):Promise.resolve({runs:[]});
 const pending=loadPerformance({force:true});
 location.hash='#runs';await route();const historyMarkup=nodes['#main'].innerHTML;
 finish({targets:[],available_targets:[]});await pending;
 assert.equal(nodes['#main'].innerHTML,historyMarkup);assert.equal(state.reportRouteActive,false);
})()
`);
""")


def test_results_sections_navigate_without_reloading_and_reopen_comparison():
    run_render_checks(r"""
(async()=>{
const baseline={run_id:'reference',status:'inconclusive',targets:[{target:'local',load_test:{status:'inconclusive'},comparisons:[
  {check:'load',scenario:'same',metric:'latency_ms',baseline:10,current:20,status:'inconclusive',reason:'Timing warning'},
  {check:'load',scenario:'same',metric:'correctness_rate',baseline:.5,current:.6,status:'pass'},
  {check:'load',scenario:'same',metric:'error_rate',baseline:0,current:0,status:'pass'}]}]};
detail(report([check('load','fail')],{baseline}));chooseReportTab('results');
const links=[...$('#report-sections').querySelectorAll('a')];
assert.equal(links.length,10);
for(const link of links){
  assert.equal(link.dataset.reportSectionTab,'results');
  assert($('#results-'+link.dataset.reportSection));
  assert.deepEqual(reportRoute(link.getAttribute('href')),{id:'saved-run',tab:'results',section:link.dataset.reportSection});
}
assert.equal($('#results-section-jump').querySelectorAll('option').length,10);
assert.match($('#baseline-content').innerHTML,/data-action="compare-saved"/);
assert.equal($('#report-sections').querySelectorAll('ul ul a').length,5);
assert.equal($('#results-timing').closest('.report-result-section').id,'report-baseline-reference');
assert.equal($('#results-configuration').closest('#report-baseline-reference'),null);
assert.equal($('#results-checks').closest('#report-baseline-reference'),null);
assert.doesNotMatch($('#baseline-content').textContent,/Assessment details|load and regression assessment/);
const method=[...$('#baseline-content').querySelectorAll('details')].find(node=>node.querySelector('summary')?.textContent==='How comparison is done');
assert(method);assert.equal(method.open,false);assert(method.closest('.comparison-filters'));assert.equal(method.closest('.comparison-section'),null);
api=async()=>{throw Error('Section navigation must not reload the run');};
$('#report-baseline-reference').open=false;
const jump=$('#results-section-jump');jump.value='timing';
listeners.change({target:jump});await route();
assert.equal(location.hash,'#runs/saved-run/results/timing');
assert.equal($('#report-baseline-reference').open,true);
assert.equal(reportView.tab,'results');
assert.equal($('#results-section-jump').value,'timing');
$('#report-baseline-reference').open=false;
jump.value='configuration';listeners.change({target:jump});await route();
assert.equal($('#report-config-reference').open,true);
assert.equal($('#report-baseline-reference').open,false);
assert.equal($('.run-diagnostics').open,false);jump.value='resources';listeners.change({target:jump});await route();
assert.equal($('.run-diagnostics').open,true);assert.equal($('#report-baseline-reference').open,false);
jump.value='load-acceptance';listeners.change({target:jump});await route();
assert.equal(location.hash,'#runs/saved-run/results/load-acceptance');
assert.equal(reportView.tab,'results');assert.equal($('#report-baseline-reference').open,true);
assert.equal($('#results-section-jump').value,'load-acceptance');
$('#report-baseline-reference').open=false;
const link=$('#report-sections').querySelector('[data-report-section="load-acceptance"]');
listeners.click({target:link,preventDefault(){}});await route();
assert.equal(reportView.tab,'results');assert.equal($('#report-baseline-reference').open,true);
})()
""", dom=True)


def test_hyphenated_result_deep_links_survive_actual_hashchange_without_reloading():
    run_browser_script(r"""
const page=browser(defaults,'#runs/saved%20run/results/load-acceptance');await page.run('init()');
assert.equal(page.run('state.detail.id'),'saved run');assert.equal(page.run('reportView.tab'),'results');
const count=()=>page.paths.filter(path=>path==='/api/runs/saved%20run').length;
const calls=count(),version=page.run('state.routeVersion');
for(const hash of ['#runs/saved%20run/performance/timing','#runs/saved%20run/results/load-acceptance']){
  page.location.hash=hash;await page.windowListeners.hashchange();
}
assert.equal(page.run('reportView.tab'),'results');
assert.equal(page.run('reportRoute().section'),'load-acceptance');
assert.equal(page.run('state.routeVersion'),version);assert.equal(count(),calls);
""")


def test_invalid_report_hashes_use_runs_view_without_loading_or_throwing():
    run_browser_script(r"""
const page=browser();await page.run('init()');
for(const hash of [
  '#runs/saved/results/unknown-section','#runs/saved/results/load--acceptance',
  '#runs/saved/performance/load-acceptance','#runs/saved/results/server',
  '#runs/saved/unknown','#runs/saved/results/timing/extra','#runs/saved/results/',
  '#runs//results','#runs/%E0%A4%A/results',
]){
  const before=page.paths.length;page.location.hash=hash;
  await assert.doesNotReject(()=>page.windowListeners.hashchange(),hash);
  assert.equal(page.run('reportRoute()'),null,hash);
  assert.equal(page.run('state.reportRouteActive'),false,hash);
  assert(page.paths.slice(before).includes('/api/runs'),hash);
  assert(!page.paths.slice(before).some(path=>path.startsWith('/api/runs/')),hash);
}
""")


@pytest.mark.parametrize("tab", ["results", "performance"])
def test_comparison_filters_work_in_both_report_tabs_and_keep_visible_focus(tab):
    run_render_checks(PERFORMANCE + r"""
const baseline={run_id:'reference',status:'inconclusive',acceptance:{baseline:'pass',current:'fail'},targets:[{target:'local',comparisons:[
  {check:'load',scenario:'same',metric:'latency_ms',baseline:10,current:20,status:'inconclusive',reason:'Timing warning'},
  {check:'generation',scenario:'same',metric:'latency_ms',baseline:10,current:10,status:'pass'}]}]};
detail(report([check('load','fail')],{baseline,performance:performanceFixture()}));chooseReportTab(TAB);
const panel=()=>$('#report-'+TAB),controls=()=>panel().querySelectorAll('.comparison-filters select');
const rows=()=>panel().querySelectorAll('[data-metric]');
assert.equal(rows().length,2);assert.equal(controls().length,2);
let control=controls()[0];control.value='with_warnings';listeners.change({target:control});
assert.equal(rows().length,1);assert.match(rows()[0].textContent,/Timing warning/);
assert.equal(controls()[0].value,'with_warnings');assert.equal(document.activeElement,controls()[0]);
control=controls()[1];control.value='generation';listeners.change({target:control});
assert.equal(rows().length,0);assert.match(panel().textContent,/No measurements match/);
assert.equal(controls()[1].value,'generation');assert.equal(document.activeElement,controls()[1]);
control=controls()[0];control.value='without_warnings';listeners.change({target:control});
assert.equal(rows().length,1);assert.match(rows()[0].textContent,/No material regression/);
chooseReportTab(TAB==='results'?'performance':'results');
const other=$('#report-'+reportView.tab),otherControls=other.querySelectorAll('.comparison-filters select');
assert.equal(other.querySelectorAll('[data-metric]').length,1);
assert.equal(otherControls[0].value,'without_warnings');assert.equal(otherControls[1].value,'generation');
assert.match(other.textContent,/Baseline: Passed/);assert.match(other.textContent,/Current: Failed/);
""".replace('TAB', repr(tab)), dom=True)


def test_results_section_links_reveal_results_after_performance_comparison_is_loaded():
    run_render_checks(PERFORMANCE + r"""
(async()=>{
const baseline={run_id:'reference',status:'inconclusive',targets:[{target:'local',load_test:{status:'inconclusive'},comparisons:[
  {check:'load',scenario:'same',metric:'latency_ms',baseline:10,current:20,status:'inconclusive',reason:'Timing warning'}]}]};
detail(report([check('load','fail')],{baseline,performance:performanceFixture()}));
chooseReportTab('performance');assert.match($('#performance-baseline').textContent,/Timing warning/);
chooseReportTab('results');api=async()=>{throw Error('Section navigation must not reload the run');};
for(const section of ['timing','load-acceptance']){
  $('#report-baseline-reference').open=false;
  const link=$('#report-sections').querySelector('[data-report-section="'+section+'"]');
  await listeners.click({target:link,preventDefault(){}});await route();
  assert.equal(reportView.tab,'results');assert.equal($('#report-performance').hidden,true);
  assert.equal($('#report-baseline-reference').open,true);
  assert.equal($('#results-'+section).closest('[role="tabpanel"]').id,'report-results');
}
const ids=$$('[id]').map(node=>node.id);assert.equal(new Set(ids).size,ids.length);
})()
""", dom=True)
