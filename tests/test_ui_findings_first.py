"""Findings-first results, navigation and safe actions in the real UI renderer."""
from tests.test_ux_results import run_render_checks


def test_finished_reports_open_results_and_keep_performance_deep_links():
    run_render_checks(r"""
for(const overall of ['pass','fail','inconclusive']){
 reportView.id=null;location.hash='#runs/saved-run';detail(report([check('access',overall)],{overall}));
 assert.equal(reportView.tab,'results');
 assert($('#tab-results').parentNode.innerHTML.indexOf('tab-results')<$('#tab-results').parentNode.innerHTML.indexOf('tab-performance'));
}
reportView.id=null;location.hash='#runs/saved-run/performance';detail(report([check('access','pass')]));assert.equal(reportView.tab,'performance');
""", dom=True)


def test_findings_show_answer_difference_and_disclose_unselected_and_diagnostics():
    run_render_checks(r"""
const value=report([check('correctness','fail',{summary:'Wrong city.',evidence_ids:['bad']}),check('json','skipped',{summary:'Not selected for this run.'})],{overall:'fail',requests:[{id:'bad',target:'local',scenario:'correctness',status:'completed',score:false,output:'Lyon',evaluation:{expected:'Paris',expected_provided:true}}]});
const html=detail(value);
assert.match(html,/Show failing requests/);assert.match(html,/Expected:.*Paris/);assert.match(html,/Received:.*Lyon/);
assert.doesNotMatch(html,/verdict-word|Acceptance failed|check-verdicts|data-check="local:json"/);
assert.match(html,/Not selected:[^<]*JSON correctness/);assert.match(html,/<details class="run-diagnostics"/);
assert.match(html,/<p class="result-guidance">0 of 1 checks passed<\/p>/);
assert.match(html,/<p class="finding-check-reason">Wrong city\.<\/p>/);
""")


def test_stopped_by_user_overrides_backend_budget_guidance():
    run_render_checks(r"""
const value=report([check('generation','inconclusive',{summary:'Raise max_requests above 200.'})],{overall:'inconclusive',abort_reason:'Cancelled by user',requests:[{status:'completed'},{status:'cancelled'}],manifest:{request_plan:{planned_total:8},config:{checks:['generation'],targets:[]}}});
const html=detail(value);assert.match(html,/<h1>Stopped by you<\/h1>/);assert.match(html,/2 of 8 planned requests finished\. Partial results below\./);
assert.doesNotMatch(html.split('report-toolbar')[0],/Raise max_requests/);
assert.equal(shortReason('One. Two. Three.').summary,'One. Two.');
""")


def test_report_rail_retargets_links_after_run_navigation():
    run_render_checks(r"""
(async()=>{
 for(const id of ['A','B']){
  location.hash='#runs/'+id+'/performance';state.detail={id,state:'completed',report:report([check('access','pass')])};renderDetail();
  for(const link of $('#report-sections').querySelectorAll('a'))assert(link.getAttribute('href').startsWith('#runs/'+id+'/'));if($('#report-sections a'))assert.equal($('#report-sections a').getAttribute('href'),'#runs/'+id+'/performance/overview');
 }
 location.hash='#runs/B/results';await route();
 assert.equal($('#report-sections').hidden,true);assert.equal($('#report-sections').querySelectorAll('a').length,0);
 for(const link of $('#report-sections').querySelectorAll('a'))assert(link.getAttribute('href').startsWith('#runs/B/'));
})()
""", dom=True)


def test_primary_action_cannot_trigger_destructive_buttons_and_stop_needs_confirmation():
    run_render_checks(r"""
(async()=>{
 const main=$('#main');main.innerHTML='<button class="button danger" data-action="stop">Stop run</button><button class="button primary danger" id="cleanup-confirm">Delete runs</button><button class="button primary" id="safe">Continue</button>';
 let clicked='';for(const button of main.querySelectorAll('button'))button.click=()=>clicked=button.id||'stop';
 primaryAction();assert.equal(clicked,'safe');
 let now=1000;Date.now=()=>now;state.detail={id:'live',state:'running',config:{targets:[]},progress:{},events:[]};renderLive();let calls=0;api=async()=>{calls++;return {state:'stopping'};};schedulePoll=()=>{};
 await listeners.click({target:$('[data-action="stop"]')});assert.equal(calls,0);assert.match($('[data-action="stop"]').textContent,/Confirm stop/);
 now+=301;await listeners.click({target:$('[data-action="stop"]')});assert.equal(calls,1);
})()
""", dom=True)


def test_baseline_table_and_hero_summary_preserve_uncertainty():
    run_render_checks(r"""
const baseline={run_id:'old',status:'fail',targets:[{target:'local',comparisons:[
 {metric:'latency_ms',baseline:100,current:130,status:'fail',worsening:30,reason:'Slower'},
 {metric:'latency_ms',baseline:100,current:70,status:'pass',worsening:-30,threshold:20,worsening_interval_95:[-35,-25],reason:'Faster'},
 {metric:'latency_ms',baseline:100,current:90,status:'pass',worsening:-10,threshold:20,reason:'Within threshold'},
 {metric:'latency_ms',status:'inconclusive',reason:'Too few samples'}]}]};
state.baselines=[{id:'chosen',run_id:'old',name:'Before deploy'}];let html=detail(report([check('access','pass')],{baseline,overall:'fail'}));
assert.match(html,/vs baseline Before deploy: 4 measurements · 1 with warnings/);
html=renderComparison(state.detail.report);assert.equal((html.match(/data-metric=/g)||[]).length,4);for(const heading of ['Metric','Baseline','Current','Δ','Verdict'])assert(new RegExp('<th[^>]*>'+heading+'</th>').test(html));
assert.match(html,/\+30%/);assert.match(html,/-30%/);assert.match(html,/worse/);assert.match(html,/no material change/);assert.match(html,/Too few samples/);assert.match(html,/Why this warning/);assert.doesNotMatch(html,/Entry \d/);
""")


def test_live_plan_progress_collapsed_activity_and_one_completion_announcement():
    run_render_checks(r"""
state.detail={id:'live',state:'running',request_plan:{planned_total:20},config:{max_requests:100,targets:[],checks:['access']},progress:{completed:4,attempted:6,elapsed_seconds:3},events:Array.from({length:8},(_,i)=>({event:'request_completed',elapsed_seconds:i,completed:i}))};
renderDetail();assert.match($('#main').textContent,/Running/);assert.match($('#main').textContent,/4 of ~20 planned requests · 3s · limit 100/);
const log=$('.live-activity');assert(!log.open);assert.equal(log.querySelectorAll('.event-row').length,5);assert.equal($('#tab-results').textContent,'Results');
state.detail={id:'live',state:'completed',report:report([check('access','pass')])};renderDetail();const live=$('#run-announcement');assert.equal(live.getAttribute('aria-live'),'polite');assert.match(live.textContent,/Run complete/);const writes=live.writes;renderDetail();assert.equal(live.writes,writes);
""", dom=True)


def test_compact_saved_reports_fetch_inline_answer_evidence_and_reject_stale_results():
    run_render_checks(r"""
(async()=>{
 fetch=()=>{};
 const compact=report([check('correctness','fail',{evidence_ids:['bad']})],{overall:'fail',requests_compact:true,requests:[{id:'bad',target:'local',check_ids:['correctness'],score:false,status:'completed'}]});
 let resolve,calls=0;api=()=>{calls++;return new Promise(done=>resolve=done);};detail(compact);
 assert.match($('#main').textContent,/Loading expected and received answers/);assert.equal(calls,1);
 resolve({id:'bad',evaluation:{expected:'Paris',expected_provided:true},output:'Lyon'});await new Promise(done=>queueMicrotask(done));await Promise.resolve();await Promise.resolve();renderDetail();
 assert.match($('#main').textContent,/Expected: Paris/);assert.match($('#main').textContent,/Received: Lyon/);assert.equal(calls,1);
 state.detail={id:'next',state:'completed',report:compact};renderDetail();assert.equal(calls,2);
 state.detail={id:'different',state:'completed',report:report([check('access','pass')])};renderDetail();
 resolve({evaluation:{expected:'STALE'},output:'WRONG RUN'});await Promise.resolve();await Promise.resolve();
 assert.doesNotMatch($('#main').textContent,/STALE|WRONG RUN/);
})()
""".replace("queueMicrotask(done)", "Promise.resolve().then(done)"), dom=True)


def test_long_answers_expand_and_reason_details_keep_backend_text():
    run_render_checks(r"""
const value=report([check('correctness','fail',{summary:'First reason. Second reason. Third reason.',evidence_ids:['bad']})],{overall:'fail',observations:{summary:'First sentence. Second sentence. To conclude, Raise the budget. Fourth sentence.'},requests:[{id:'bad',score:false,status:'completed',output:'x'.repeat(300),evaluation:{expected:'y'.repeat(300)}}]});
const html=detail(value);assert.match(html,/<details class="answer-excerpt">/);assert.match(html,/x{300}/);assert.match(html,/y{300}/);
const guidance=html.match(/<p class="result-guidance">(.*?)<\/p>/)[1];assert.equal(guidance,'0 of 1 checks passed');assert.match(html,/First reason\. Second reason\./);assert.match(html,/Third reason\./);assert.doesNotMatch(html,/<summary>Details<\/summary>/);assert.doesNotMatch(html,/To conclude,/);
assert.equal((html.match(/<div class="finding-request">/g)||[]).length,1);
""")


def test_stop_confirmation_expires_and_cannot_carry_to_another_run():
    run_render_checks(r"""
(async()=>{
 let timer;Date.now=()=>1000;setTimeout=(fn,ms)=>{if(ms===4000)timer=fn;};api=async()=>{throw Error('must not cancel');};
 state.detail={id:'one',state:'running',config:{targets:[]},events:[],progress:{}};renderLive();await confirmStop();assert.match($('[data-action="stop"]').textContent,/Confirm stop/);
 timer();assert.equal($('[data-action="stop"]').textContent,'Stop run');
 await confirmStop();state.detail.id='two';renderLive();await confirmStop();assert.equal(state.stopConfirmation.id,'two');assert.match($('[data-action="stop"]').textContent,/Confirm stop/);
})()
""", dom=True)


def test_routes_rail_clicks_and_palette_follow_current_run_and_focus_heading():
    run_render_checks(r"""
(async()=>{
 clearPoll=schedulePoll=updateNav=()=>{};window.scrollTo=()=>{};
 const reports={A:report([check('access','pass')]),B:report([check('correctness','fail')],{overall:'fail'})};
 api=async path=>{const id=path.slice('/api/runs/'.length);return {id,state:'completed',report:reports[id]};};
 const assertCurrent=id=>{
  assert.equal(state.detail.id,id);assert.equal(document.activeElement,$('#main h1'));
  for(const link of $('#report-sections').querySelectorAll('a'))assert(link.getAttribute('href').startsWith('#runs/'+id+'/'));if($('#report-sections a'))assert.equal($('#report-sections a').getAttribute('href'),'#runs/'+id+'/performance/overview');
 };
 location.hash='#runs/A/results';await route();assertCurrent('A');
 location.hash='#runs/B/results';await route();assertCurrent('B');
 await listeners.click({target:$('#tab-performance'),preventDefault(){}});await route();
 assert.equal($('#report-sections').hidden,false);assert.equal($('#report-sections').querySelectorAll('a').length,9);
 await listeners.click({target:$('#report-sections a'),preventDefault(){}});await route();assert.equal(state.detail.id,'B');assert.equal(reportView.tab,'performance');
 const dialog=document.createElement('dialog');dialog.setAttribute('id','command-dialog');document.body.insertBefore(dialog,null);
 dialog.showModal=function(){this.open=true;$('#command-search').focus=()=>{};};
 state.runs=[{id:'A',targets:[{model:'same-model'}]},{id:'B',targets:[{model:'same-model'}]}];openCommands();
 const commandIndex=commandList.findIndex(item=>item.group==='Runs');await listeners.click({target:$('[data-command="'+commandIndex+'"]')});
 await route();assertCurrent('A');assert(!dialog.open);
})()
""", dom=True)


def test_setup_profiles_replace_steps_and_run_headers_use_url_identity():
    run_render_checks(r"""
Object.defineProperty(Object.getPrototypeOf($('#main')),'elements',{get(){return new Proxy({},{get:(_,name)=>this.querySelector('[name="'+String(name)+'"]')||{value:'',dataset:{}}});}});
state.config={...structuredClone(bootstrap.defaults),targets:[{name:'local',model:'test',url:'http://localhost/v1',tokenizer:{mode:'builtin'}}]};state.bootstrap.checks=[];state.jsonDraft=state.jsonBase='synced';
renderHeroCoat=()=>'';syncTestControls=updateBounds=updateRestart=syncConfigView=syncPackControls=renderRequestPreview=syncSetupStep=renderModelDiscovery=watchPlanViewport=scheduleTokenizerSelection=renderTokenizerSelection=()=>{};
renderNew();assert.equal($$('.setup-step').length,0);assert.equal($$('.setup-profile').length,5);assert.equal($('#start-run').getAttribute('type'),'submit');
const html=detail(report([check('access','pass')],{run_id:'abcdef012345'}),{id:'20261027T155344-8acbbc1f'});
assert.match(html.split('report-toolbar')[0],/20261027T155344-8acbbc1f/);assert.doesNotMatch(html.split('report-toolbar')[0],/abcdef012345/);assert.match($('.run-diagnostics').textContent,/abcdef012345/);
state.runs=[{id:'one',overall:'pass',state:'completed',started_at:'2026-10-27T15:00:01Z',targets:[{model:'same'}]},{id:'two',overall:'fail',state:'completed',started_at:'2026-10-27T15:00:02Z',targets:[{model:'same'}]}];renderRailRuns();const links=$$('#rail-runs a');assert.match(links[0].textContent,/Passed/);assert.match(links[1].textContent,/Failed/);assert.notEqual(links[0].textContent,links[1].textContent);
""", dom=True)


def test_sentence_truncation_keeps_decimal_measurements_and_urls():
    run_render_checks(r"""
const value='Latency is 1.25 ms at https://model.example/v1. Second sentence. Third sentence.';
assert.equal(shortReason(value).summary,'Latency is 1.25 ms at https://model.example/v1. Second sentence.');
assert.equal(shortReason(value).rest,'Third sentence.');
""")


def test_arrival_comparison_uses_request_rate_units_and_names_nested_load_points():
    run_render_checks(r"""
const point={type:'combinations',shape:'short_short',pattern:'concurrent',level:2,status:'pass'};
const value=report([check('load','fail')],{baseline:{run_id:'old',status:'fail',targets:[{target:'local',traffic_capacity:{status:'fail',baseline_highest_acceptable_rate_rps:5,current_highest_acceptable_rate_rps:4,reason:'Lost accepted rate.'},load_test:{points:[{baseline:point,current:{...point,status:'fail'},status:'fail',reason:'Lost acceptable load.'}]}}]}});
state.detail={id:'current',report:value};const html=renderComparison(value);
assert.match(html,/5 req\/s/);assert.match(html,/4 req\/s/);assert.doesNotMatch(html,/500%|400%/);
assert.match(html,/maintained concurrency 2/);assert.match(html,/Short input/);
""")


def test_tool_only_findings_show_received_tool_calls_when_text_is_empty():
    run_render_checks(r"""
const record={id:'tool',target:'local',scenario:'tool_auto',status:'completed',score:false,output:'',evaluation:{tool_expectation:{expected_calls:[{name:'expected_tool',arguments:{}}]}},tool_calls:[{function:{name:'wrong_tool',arguments:'{}'}}]};
const html=detail(report([check('tools','fail',{evidence_ids:['tool']})],{overall:'fail',requests:[record]}));
assert.match(html,/Expected:.*expected_tool/s);assert.match(html,/Received:.*wrong_tool/s);
""")


def test_returning_to_same_run_during_evidence_fetch_starts_current_visit_fetch():
    run_render_checks(r"""
(async()=>{
 fetch=()=>{};clearPoll=schedulePoll=updateNav=()=>{};
 const compact=report([check('correctness','fail',{evidence_ids:['bad']})],{overall:'fail',requests_compact:true,requests:[{id:'bad',target:'local',score:false,status:'completed'}]});
 const pending=[];
 api=async path=>path.includes('/requests/')?new Promise(resolve=>pending.push(resolve)):{id:path.slice('/api/runs/'.length),state:'completed',report:path==='/api/runs/saved-run'?compact:report([check('access','pass')])};
 detail(compact);assert.equal(pending.length,1);
 location.hash='#runs/other';await route();location.hash='#runs/saved-run';await route();
 assert.equal(pending.length,2);
 pending[1]({id:'bad',evaluation:{expected:'Paris'},output:'Lyon'});await Promise.resolve();await Promise.resolve();await Promise.resolve();renderDetail();
 assert.match($('#main').textContent,/Received: Lyon/);
 pending[0]({id:'bad',evaluation:{expected:'STALE'},output:'OLD VISIT'});await Promise.resolve();await Promise.resolve();
 assert.doesNotMatch($('#main').textContent,/STALE|OLD VISIT/);
})()
""", dom=True)


def test_saved_partial_summary_uses_findings_and_user_stop_copy():
    run_render_checks(r"""
state.detail={id:'partial',state:'interrupted',abort_reason:'cancelled by user',request_plan:{planned_total:8},config:{targets:[],checks:['access']},summary:{overall:'inconclusive',request_count:2,checks:[check('access','inconclusive'),check('json','skipped',{summary:'Not selected for this run.'})]},live_requests:[{id:'a',status:'completed'},{id:'b',status:'cancelled'}]};
renderDetail();const html=nodes['#main'].innerHTML;assert.match(html,/<h1>Stopped by you<\/h1>/);assert.match(html,/2 of 8 planned requests finished/);
assert.match(html,/Not selected:[^<]*JSON correctness/);assert.doesNotMatch(html,/data-verdict-check|data-check="local:json"/);assert.match(html,/<details class="run-diagnostics">/);
nodes['#result-content']={innerHTML:''};listeners.click({target:{closest:q=>q==='[data-check]'?{dataset:{check:'local:access'}}:null}});
assert.match(nodes['#result-content'].innerHTML,/finding-expanded/);
""")


def test_compact_answer_fetch_errors_are_escaped_and_do_not_retry_every_render():
    run_render_checks(r"""
(async()=>{
 fetch=()=>{};let calls=0;api=async()=>{calls++;throw Error('Evidence <unavailable>');};
 detail(report([check('correctness','fail',{evidence_ids:['bad']})],{overall:'fail',requests_compact:true,requests:[{id:'bad',target:'local',score:false,status:'completed'}]}));
 await Promise.resolve();await Promise.resolve();await Promise.resolve();renderDetail();
 assert.match($('#main').textContent,/Evidence <unavailable>/);assert.doesNotMatch($('#main').innerHTML,/<unavailable>/);assert.equal(calls,1);renderDetail();assert.equal(calls,1);
})()
""", dom=True)


def test_history_labels_completed_partial_reports_as_stopped():
    run_render_checks(r"""
for(const reason of ['cancelled by user','run cancelled']){
 const run={id:'stopped-run',state:'completed',overall:'inconclusive',abort_reason:reason};
 assert.equal(runOutcome(run),'stopped');
 assert.equal(statusLabels[runOutcome(run)],'Stopped');
 state.runs=[run];state.historySearch='';state.filter='stopped';
 assert.equal(filteredRuns().length,1);
 state.detail={...run,report:report([check('access','inconclusive')],{abort_reason:reason})};
 renderDetail();assert.match(nodes['#main'].innerHTML,/Stopped/);
}
state.detail={id:'live',state:'stopping',progress:{phase:'saving_partial_report',message:'Saving partial report…'},config:{targets:[],checks:[]},events:[]};
renderDetail();assert.match(nodes['#main'].innerHTML,/Saving partial report…/);
""", dom=True)
