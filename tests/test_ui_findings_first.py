"""Findings-first results, navigation and safe actions in the real UI renderer."""
import json

import pytest

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
assert.match(html,/Not selected: JSON correctness/);assert.match(html,/<details class="run-diagnostics"/);
assert.match(html,/<p class="result-guidance">Wrong city\.<\/p>/);
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
  for(const link of $('#report-sections').querySelectorAll('a'))assert(link.getAttribute('href').startsWith('#runs/'+id+'/performance/'));
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
html=renderComparison(state.detail.report);assert.equal((html.match(/data-metric=/g)||[]).length,4);for(const heading of ['Baseline','Current','Change'])assert(html.includes('<dt>'+heading+'</dt>'));
assert.match(html,/\+30%/);assert.match(html,/-30%/);assert.match(html,/Regression detected/);assert.match(html,/No material regression/);assert.match(html,/Too few samples/);assert.match(html,/Why this warning/);assert.doesNotMatch(html,/Entry \d/);
""")


@pytest.mark.parametrize("comparison_status", ["pass", "fail", "inconclusive"])
def test_headline_comparison_summary_keeps_current_failure_and_recorded_verdict(comparison_status):
    from giraffe.web import _api_report
    from tests.test_reporting import make_report

    value = make_report(count=6)
    value.checks[0].status = "fail"
    value.checks[0].metrics = {"correct": 4, "scored": 6}
    value.overall = "fail"
    value.baseline = {
        "run_id": "before-upgrade", "saved_baseline_name": "Reference <upgrade>",
        "source_started_at": "2026-10-24T10:00:00Z", "status": comparison_status,
        "acceptance": {"baseline": "fail", "current": "fail"},
        "targets": [{"target": "local", "comparisons": [{
            "check": "correctness", "scenario": "warm", "metric": "latency_ms",
            "baseline": 222.6, "current": 288.4, "baseline_samples": 6, "current_samples": 6,
            "observed_delta": 65.8, "observed_change": 29.5, "change_unit": "percent",
            "status": comparison_status, "reason": "Sample variation crosses the threshold.",
        }]}],
    }
    run_render_checks("const value=" + json.dumps(_api_report(value)) + ";" + r"""
detail(value);
const summary=$('.comparison-summary');assert(summary,'comparison summary must be visible near the headline');
assert.match(summary.textContent,/Current acceptance:.*Failed/);
assert.match(summary.textContent,/Baseline comparison:/);
assert.match(summary.textContent,new RegExp({pass:'No material regression',fail:'Regression detected',inconclusive:'Inconclusive'}[value.baseline.status]));
assert.match(summary.textContent,/Reference <upgrade>/);assert.equal(summary.querySelector('upgrade'),null);
assert.match(summary.textContent,/before-upgrade/);assert.match(summary.textContent,/2026/);
assert.match(summary.textContent,/Reference acceptance:.*Failed/);
assert.match(summary.textContent,/Saving.*does not approve/i);
assert.match(summary.textContent,/222\.6 ms/);assert.match(summary.textContent,/288\.4 ms/);assert.match(summary.textContent,/\+29\.5%/);
assert.match(summary.textContent,/Baseline n=6.*Current n=6/);
assert.match(summary.textContent,/Sample variation crosses the threshold/);
assert(!summary.closest('[role="tabpanel"]'),'summary must be shared by both tabs');
chooseReportTab('performance');assert.equal($('.comparison-summary').textContent,summary.textContent);
assert.equal($('#report-performance').hidden,false);
""", dom=True)


def test_comparison_summary_evidence_links_clear_filters_and_reveal_the_metric_section():
    from giraffe.web import _api_report
    from tests.test_reporting import make_report

    value = make_report()
    value.baseline = {"run_id": "reference", "status": "inconclusive", "targets": [{
        "target": "local", "comparisons": [
            {"check": "correctness", "scenario": "short", "metric": "latency_ms",
             "baseline": 10, "current": 10, "status": "pass", "reason": "Stable response time."},
            {"check": "correctness", "scenario": "warm", "metric": "latency_ms",
             "baseline": 222.6, "current": 288.4, "status": "inconclusive",
             "reason": "Too few samples."},
        ]}]}
    run_render_checks("const value=" + json.dumps(_api_report(value)) + ";" + r"""
(async()=>{
detail(value);chooseReportTab('performance');state.comparisonFilter='without_warnings';state.comparisonCheck='other';renderDetail();
const evidence=$('.comparison-summary [data-comparison-summary-section="timing"]');assert(evidence);
assert.equal(evidence.getAttribute('href'),'#runs/saved-run/results/timing');
await listeners.click({target:evidence,preventDefault(){}});
await route();
assert.equal(state.comparisonFilter,'all');assert.equal(state.comparisonCheck,'all');
assert.equal(reportView.tab,'results');assert($('#report-baseline-reference').open);
assert.match($('#results-timing').textContent,/Too few samples/);
assert.equal(document.activeElement,$('#results-comparison-metric-0-1'));
assert.equal(document.activeElement.getAttribute('data-metric'),'latency_ms');
const full=$('.comparison-summary [data-comparison-summary-section="comparison"]');assert(full);
await listeners.click({target:full,preventDefault(){}});
await route();
assert.equal(document.activeElement,$('#report-baseline-reference summary'));
})()
""", dom=True)


@pytest.mark.parametrize("evidence", ["traffic", "load", "load_no_points"])
def test_comparison_summary_opens_exact_target_for_traffic_and_load_evidence(evidence):
    from giraffe.web import _api_report
    from tests.test_reporting import make_report
    from tests.test_ui_report_tabs import PERFORMANCE

    value = make_report()
    target = {"target": "loaded"}
    if evidence == "traffic":
        target["traffic_capacity"] = {
            "status": "fail", "reason": "Loaded target lost accepted arrival rate.",
            "baseline_highest_acceptable_rate_rps": 5, "current_highest_acceptable_rate_rps": 2,
            "delta_rps": -3,
        }
        expected_id = "results-comparison-traffic-1"
    else:
        point = {"type": "combinations", "shape": "short_short", "pattern": "concurrent", "bursts": False}
        target["traffic_capacity"] = {}
        target["comparisons"] = [{"check": "correctness", "scenario": "warm", "metric": "latency_ms",
            "baseline": 10, "current": 10, "status": "pass",
            "output_work": {"current": {}, "baseline": {"valid_completions": 8}}}]
        target["load_test"] = {"status": "fail", "points": [
            {"status": "pass", "acceptance_change": "unchanged_accepted",
             "baseline": {**point, "level": 1, "status": "pass"},
             "current": {**point, "level": 1, "status": "pass"}},
            {"status": "fail", "acceptance_change": "lost", "reason": "Loaded target lost concurrency 2.",
             "baseline": {**point, "level": 2, "status": "pass"},
             "current": {**point, "level": 2, "status": "fail"}},
        ]}
        expected_id = "results-comparison-load-1-1"
        if evidence == "load_no_points":
            target["load_test"] = {"status": "inconclusive", "reason": "Loaded target lost measurement coverage."}
            expected_id = "results-comparison-load-1"
    value.baseline = {"run_id": "reference", "status": "fail", "targets": [
        {"target": "other", "comparisons": [{"check": "correctness", "metric": "correctness_rate",
            "baseline": 1, "current": 1, "status": "pass"}]}, target,
    ]}
    run_render_checks(PERFORMANCE + "const value=" + json.dumps(_api_report(value)) + ";const expectedId=" +
                      json.dumps(expected_id) + ";value.performance=performanceFixture();" + r"""
(async()=>{
detail(value);chooseReportTab('performance');
const action=$('.comparison-summary [data-comparison-summary-evidence="'+expectedId.slice('results-'.length)+'"]');assert(action);
await listeners.click({target:action,preventDefault(){}});await route();
const destination=$('#'+expectedId);assert(destination);
assert.equal(document.activeElement,destination);assert($('#report-baseline-reference').open);
assert.match(destination.textContent,/Loaded target lost/);
assert.equal(destination.closest('[hidden]'),null);
const ids=$$('[id]').map(node=>node.id);assert.equal(new Set(ids).size,ids.length,'evidence IDs must be unique across report tabs');
})()
""", dom=True)


@pytest.mark.parametrize("empty_side", ["baseline", "current"])
def test_comparison_summary_output_evidence_skips_empty_work_before_a_valid_cohort(empty_side):
    from giraffe.web import _api_report
    from tests.test_reporting import make_report

    value = make_report()
    previous = {"reported_output_tokens_p50": 10, "reported_token_samples": 8,
                "output_chars_p50": 15, "valid_completions": 8}
    current = {**previous, "reported_output_tokens_p50": 20, "output_chars_p50": 30}
    empty_work = {"baseline": previous, "current": current, empty_side: {}}
    value.baseline = {"status": "pass", "run_id": "reference", "targets": [{"target": "local", "comparisons": [
        {"check": "correctness", "scenario": "empty-work", "metric": "latency_ms", "status": "pass",
         "baseline": 10, "current": 10, "output_work": empty_work},
        {"check": "correctness", "scenario": "valid-work", "metric": "latency_ms", "status": "pass",
         "baseline": 10, "current": 10, "output_work": {"baseline": previous, "current": current}},
    ]}]}
    run_render_checks("const value=" + json.dumps(_api_report(value)) + ";" + r"""
(async()=>{
detail(value);const action=$('[data-comparison-summary-evidence="comparison-output-0-0"]');assert(action);
await listeners.click({target:action,preventDefault(){}});await route();
const evidence=$('#results-comparison-output-0-0');assert.equal(document.activeElement,evidence);
assert.match(evidence.textContent,/10 tokens/);assert.match(evidence.textContent,/20 tokens/);
assert.match(evidence.closest('[data-comparison-group]').textContent,/Valid work/);
assert.doesNotMatch(evidence.closest('[data-comparison-group]').textContent,/Empty work/);
})()
""", dom=True)


@pytest.mark.parametrize("baseline", [None, {"run_id": "old", "status": "inconclusive", "targets": [{
    "target": "local", "status": "inconclusive", "reasons": ["Requested workload differs."]}]}])
def test_comparison_summary_does_not_invent_missing_measurements(baseline):
    from giraffe.web import _api_report
    from tests.test_reporting import make_report

    value = make_report()
    value.baseline = baseline
    run_render_checks("const value=" + json.dumps(_api_report(value)) + ";" + r"""
detail(value);const summary=$('.comparison-summary');assert(summary);
assert.match(summary.textContent,value.baseline?/Requested workload differs/:/No baseline selected/);
assert.doesNotMatch(summary.textContent,/0 ms|0%|n=0|No material regression/);
""", dom=True)


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
const guidance=html.match(/<p class="result-guidance">(.*?)<\/p>/)[1];assert.equal(guidance,'First sentence. Second sentence.');assert.match(html,/Raise the budget/);assert.doesNotMatch(html,/To conclude,/);
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
  for(const link of $('#report-sections').querySelectorAll('a'))assert(link.getAttribute('href').startsWith('#runs/'+id+'/performance/'));
 };
 location.hash='#runs/A/results';await route();assertCurrent('A');
 location.hash='#runs/B/results';await route();assertCurrent('B');
 await listeners.click({target:$('#tab-performance'),preventDefault(){}});await route();
 assert.equal($('#report-sections').hidden,false);assert.equal($('#report-sections').querySelectorAll('a').length,9);
 await listeners.click({target:$('#report-sections a'),preventDefault(){}});await route();assert.equal(state.detail.id,'B');assert.equal(reportView.tab,'performance');
 const dialog=document.createElement('dialog');dialog.setAttribute('id','command-dialog');document.body.insertBefore(dialog,null);
 dialog.showModal=function(){this.open=true;$('#command-search').focus=()=>{};};
 state.runs=[{id:'A',targets:[{model:'same-model'}]},{id:'B',targets:[{model:'same-model'}]}];openCommands();
 const commandIndex=commandList.findIndex(item=>item.group==='Recent runs');await listeners.click({target:$('[data-command="'+commandIndex+'"]')});
 await route();assertCurrent('A');assert(!dialog.open);
})()
""", dom=True)


def test_setup_step_buttons_have_names_and_run_headers_use_url_identity():
    run_render_checks(r"""
Object.defineProperty(Object.getPrototypeOf($('#main')),'elements',{get(){return new Proxy({},{get:(_,name)=>this.querySelector('[name="'+String(name)+'"]')||{value:'',dataset:{}}});}});
state.config={...structuredClone(bootstrap.defaults),targets:[{name:'local',model:'test',url:'http://localhost/v1',tokenizer:{mode:'builtin'}}]};state.bootstrap.checks=[];state.jsonDraft=state.jsonBase='synced';
renderHeroCoat=()=>'';syncTestControls=updateBounds=updateRestart=syncConfigView=syncPackControls=renderRequestPreview=syncSetupStep=renderModelDiscovery=watchPlanViewport=scheduleTokenizerSelection=renderTokenizerSelection=()=>{};
renderNew();const steps=$$('.setup-step');assert.equal(steps.length,3);
for(const [index,label] of ['Connect','Checks','Launch'].entries())assert.equal(steps[index].getAttribute('aria-label'),'Step '+(index+1)+': '+label);
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
assert.match(html,/Not selected: JSON correctness/);assert.doesNotMatch(html,/data-verdict-check|data-check="local:json"/);assert.match(html,/<details class="run-diagnostics">/);
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
