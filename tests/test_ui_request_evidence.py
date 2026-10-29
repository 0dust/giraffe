"""Request evidence remains inspectable without a server or decorative mosaics."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_ux_results import run_render_checks


APP = Path(__file__).parents[1] / "src/giraffe/static/app.js"


def run_script(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise request evidence")
    helpers = APP.read_text().split("// Request evidence helpers.", 1)[1].split("// End request evidence helpers.", 1)[0]
    helpers += "\nconst evidence={renderConditionStrip,createRequestTableState,renderRequestTable,requestTablePage,updateRequestTableState,handleRequestTableKeydown};\n"
    source = "import assert from 'node:assert/strict';\nimport vm from 'node:vm';\n"
    source += f"vm.runInNewContext({json.dumps(helpers + script)}, {{assert}});"
    result = subprocess.run([node, "--input-type=module", "-"], input=source,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_request_table_sorts_filters_and_caps_pages_without_changing_records():
    run_script(r"""
const state=evidence.createRequestTableState();
const records=Array.from({length:405},(_,i)=>({id:`request-${i}`,condition_id:i%2?'odd':'even',status:'completed',valid:true,score:i!==1,elapsed_ms:405-i,first_token_ms:i===2?null:i,output_tokens:i,evaluation:{messages:[{role:'user',content:`Question ${i}`}],expected:'Paris'},output:i===1?'Lyon':'Paris'}));
const original=JSON.stringify(records);
let page=evidence.requestTablePage(records,state);
assert.equal(page.rows.length,200);assert.equal(page.pages,3);assert.equal(page.rows[0].id,'request-0');
evidence.updateRequestTableState(state,'page',3);page=evidence.requestTablePage(records,state);
assert.equal(page.rows.length,5);assert.equal(page.rows[0].id,'request-400');
evidence.updateRequestTableState(state,'sort','elapsed_ms');assert.equal(state.page,1);
page=evidence.requestTablePage(records,state);assert.equal(page.rows[0].id,'request-404');
evidence.updateRequestTableState(state,'sort','elapsed_ms');page=evidence.requestTablePage(records,state);assert.equal(page.rows[0].id,'request-0');
evidence.updateRequestTableState(state,'status','fail');page=evidence.requestTablePage(records,state);assert.deepEqual(page.rows.map(row=>row.id),['request-1']);
evidence.updateRequestTableState(state,'condition','even');assert.equal(evidence.requestTablePage(records,state).matched,0);
evidence.updateRequestTableState(state,'status','all');evidence.updateRequestTableState(state,'search','Question 404');
assert.deepEqual(evidence.requestTablePage(records,state).rows.map(row=>row.id),['request-404']);
evidence.updateRequestTableState(state,'condition','all');evidence.updateRequestTableState(state,'search','lyon');
assert.equal(evidence.requestTablePage(records,state).rows[0].id,'request-1');
assert.equal(JSON.stringify(records),original);
const missing=evidence.createRequestTableState();evidence.updateRequestTableState(missing,'sort','first_token_ms');evidence.updateRequestTableState(missing,'sort','first_token_ms');evidence.updateRequestTableState(missing,'page',3);
assert.equal(evidence.requestTablePage(records,missing).rows.at(-1).id,'request-2');
""")


def test_table_evidence_is_escaped_has_real_ttft_and_shows_capped_counts():
    run_script(r"""
const state=evidence.createRequestTableState();
const records=Array.from({length:201},(_,i)=>({id:`r${i}`,status:'completed',valid:true,score:false,first_output_ms:14,first_token_ms:i?7:null,elapsed_ms:20,output_tokens:3,evaluation:{messages:[{role:'user',content:'<question>'}],expected:'<expected>',expected_provided:true},output:'<received>',error:'<error>'}));
let html=evidence.renderRequestTable(records,state,{conditionName:()=>'<condition>',total:300});
const headings=[...html.matchAll(/data-evidence-sort="[^"]+">([^<]+)/g)].map(match=>match[1]);
assert.deepEqual(headings,['Status','Condition','Latency','TTFT','Tokens','Question','Expected / received','Error']);
assert(html.includes('200 of 201 requests'));assert(html.includes('201 of 300 requests available'));
for(const word of ['question','expected','received','error','condition'])assert(html.includes(`&lt;${word}&gt;`));
assert(!html.includes('<question>'));assert(!html.includes('14 ms'));assert(html.includes('7 ms'));
assert.equal((html.match(/class="request-evidence-row"/g)||[]).length,200);
assert(html.includes('data-live-key="request:r0"'));assert(html.includes('data-request="r0"'));assert(html.includes('tabindex="0"'));
assert.doesNotMatch(html,/reticulate|mosaic|coat|idle|polygon/);
evidence.updateRequestTableState(state,'search','missing');html=evidence.renderRequestTable(records,state);
assert(html.includes('No requests match these filters.'));assert(html.includes('0 of 0 requests'));
const jsonNull={id:'null',status:'completed',valid:true,score:true,evaluation:{expected:null,expected_provided:true},output:'null'};
const scored=evidence.renderRequestTable([jsonNull],evidence.createRequestTableState());assert(scored.includes('Expected: null'));assert(scored.includes('✓ match'));assert(scored.includes('Received: null'));
const unretained=evidence.renderRequestTable([{id:'absent',status:'completed',valid:true,score:null}],evidence.createRequestTableState());assert(unretained.includes('Not saved'));
const long='Long retained answer '+ 'x'.repeat(200),full=evidence.renderRequestTable([{...jsonNull,evaluation:{expected:long,expected_provided:true},output:long}],evidence.createRequestTableState());assert(full.includes('title="Expected: '+long+' · Received: '+long+'"'));
""")


def test_condition_strip_uses_canonical_names_counts_status_and_measured_p95():
    run_script(r"""
const views=[{key:'["local","json_c2"]',condition:{id:'json_c2',check:'json'},name:'Small input',description:'2 at once',status:'passed',summary:{completed:2,planned:4},records:[{status:'completed',elapsed_ms:10},{status:'completed',elapsed_ms:30},{status:'running',elapsed_ms:1000}]}];
const html=evidence.renderConditionStrip(views,{checkName:id=>({json:'JSON correctness'}[id])});
assert(html.includes('JSON correctness'));assert(html.includes('Small input · 2 at once'));assert(html.includes('2/4 requests'));assert(html.includes('p95 29 ms'));assert(html.includes('Passed'));
assert(html.includes('data-action="open-condition"'));assert(html.includes('data-condition-key="[&quot;local&quot;,&quot;json_c2&quot;]"'));
assert.doesNotMatch(html,/mosaic|reticulate|coat|polygon|idle/);
views[0].summary.planned=null;const unknownTotal=evidence.renderConditionStrip(views);assert(unknownTotal.includes('2 requests'));assert.doesNotMatch(unknownTotal,/\/\? requests/);
assert(evidence.renderConditionStrip([]).includes('Waiting for the first condition'));
""")


def test_live_append_preserves_table_state_and_stable_identity():
    run_script(r"""
const state=evidence.createRequestTableState();
evidence.updateRequestTableState(state,'sort','elapsed_ms');evidence.updateRequestTableState(state,'sort','elapsed_ms');
const records=[{id:'old',condition_id:'same',status:'completed',valid:true,score:true,elapsed_ms:50}];
const before=evidence.renderRequestTable(records,state),snapshot=JSON.stringify(state);
records.push({id:'new',condition_id:'same',status:'running',elapsed_ms:10});
const after=evidence.renderRequestTable(records,state);
assert.equal(JSON.stringify(state),snapshot);assert(before.includes('data-live-key="request:old"'));assert(after.includes('data-live-key="request:old"'));assert(after.indexOf('request:old')<after.indexOf('request:new'));
assert(before.includes('data-live-key="request-table-scroll"'));assert(after.includes('data-live-key="request-table-scroll"'));
const pending=[{slot:2,status:'running'}];
assert(evidence.renderRequestTable(pending,state,{live:true}).includes('data-live-key="request:slot-2"'));
pending[0]={...pending[0],id:'finished',status:'completed'};
const finished=evidence.renderRequestTable(pending,state,{live:true});
assert(finished.includes('data-live-key="request:slot-2"'));assert(finished.includes('data-live-slot="2"'));
evidence.updateRequestTableState(state,'status','running');evidence.updateRequestTableState(state,'condition','evicted');
const empty=evidence.renderRequestTable(pending,state,{live:true});
assert(empty.includes('value="running" selected'));assert(empty.includes('value="evicted" selected'));
""")


def test_request_rows_support_arrow_navigation_and_enter_detail():
    run_script(r"""
const opened=[];let prevented=0,focused=null,scrolled=null;
const rows=Array.from({length:3},(_,i)=>({dataset:{request:`r${i}`},focus(options){assert.equal(options.preventScroll,true);focused=i;},scrollIntoView(options){assert.equal(options.block,'nearest');scrolled=i;}}));
const tbody={querySelectorAll:()=>rows};
for(const row of rows)row.closest=selector=>selector==='tbody'?tbody:row;
const event=(i,key)=>({key,target:rows[i],preventDefault(){prevented++;}});
assert(evidence.handleRequestTableKeydown(event(0,'ArrowDown'),id=>opened.push(id)));assert.equal(focused,1);assert.equal(scrolled,1);
evidence.handleRequestTableKeydown(event(0,'ArrowUp'),()=>{});assert.equal(focused,0);
evidence.handleRequestTableKeydown(event(0,'End'),()=>{});assert.equal(focused,2);
evidence.handleRequestTableKeydown(event(2,'Home'),()=>{});assert.equal(focused,0);
evidence.handleRequestTableKeydown(event(1,'Enter'),id=>opened.push(id));assert.deepEqual(opened,['r1']);
assert(!evidence.handleRequestTableKeydown(event(0,'Escape'),()=>{}));assert.equal(prevented,5);
assert(!evidence.handleRequestTableKeydown({target:{closest:()=>null},key:'ArrowDown'},()=>{}));
rows[1].dataset.liveSlot='11';const live=[];
evidence.handleRequestTableKeydown(event(1,'Enter'),()=>assert.fail('must use live request dialog'),slot=>live.push(slot));assert.deepEqual(live,[11]);
""")


def test_archived_request_evidence_loads_once_when_disclosure_opens():
    run_render_checks(r"""
(async()=>{
globalThis.fetch=()=>assert.fail('Requests must use api()');
const compact={id:'answer',target:'local',condition_id:'correctness',scenario:'correctness',status:'completed',valid:true,score:false,elapsed_ms:45,first_output_ms:29,output_chars:4};
const value=report([check('correctness','fail')],{requests_compact:true,request_count:1,requests:[compact]});
detail(value);const calls=[];let resolve;
api=path=>{calls.push(path);return new Promise(done=>resolve=done);};
await loadRequestTableEvidence();assert.equal(calls.length,0,'closed disclosure does not fetch bodies');
const disclosure=$('.all-requests');disclosure.open=true;listeners.toggle({target:disclosure});
assert.deepEqual(calls,['/api/runs/saved-run/report.json']);
await loadRequestTableEvidence();listeners.toggle({target:disclosure});assert.equal(calls.length,1,'pending fetch is deduplicated');
resolve({requests:[{...compact,first_token_ms:11,evaluation:{messages:[{role:'user',content:'Capital of France?'}],expected:'Paris',expected_provided:true},output:'Lyon'}]});
for(let i=0;i<10;i++)await Promise.resolve();
const row=$('.request-evidence-row');assert.match(row.textContent,/Capital of France\?/);assert.match(row.textContent,/Expected: Paris/);assert.match(row.textContent,/Received: Lyon/);
assert.equal($$('td',row)[3].textContent,'11 ms','TTFT uses first_token_ms rather than first_output_ms');
assert.equal(state.requestEvidenceRun,'saved-run');assert.equal(state.requestEvidencePending,null);
$('.all-requests').open=false;listeners.toggle({target:$('.all-requests')});$('.all-requests').open=true;listeners.toggle({target:$('.all-requests')});await loadRequestTableEvidence();assert.equal(calls.length,1,'reopening uses loaded evidence');
})()
""", dom=True)


def test_stale_request_evidence_response_cannot_replace_next_runs_rows():
    run_render_checks(r"""
(async()=>{
globalThis.fetch=()=>assert.fail('Requests must use api()');
const compact=id=>({id,target:'local',condition_id:'correctness',scenario:'correctness',status:'completed',valid:true,score:false,elapsed_ms:10,output_chars:6});
const value=id=>report([check('correctness','fail')],{run_id:id,requests_compact:true,requests:[compact('shared-id')]});
state.detail={id:'first-run',state:'completed',report:value('first-run')};renderDetail();
const pending=new Map(),paths=[];api=path=>{paths.push(path);return new Promise(resolve=>pending.set(path,resolve));};
$('.all-requests').open=true;listeners.toggle({target:$('.all-requests')});
state.detail={id:'next-run',state:'completed',report:value('next-run')};renderDetail();$('.all-requests').open=true;listeners.toggle({target:$('.all-requests')});
assert.deepEqual(paths,['/api/runs/first-run/report.json','/api/runs/next-run/report.json']);
pending.get(paths[0])({requests:[{...compact('shared-id'),output:'STALE',evaluation:{expected:'STALE',expected_provided:true}}]});
for(let i=0;i<8;i++)await Promise.resolve();
assert.equal(state.detail.id,'next-run');assert.notEqual(state.requestEvidenceRun,'first-run');assert.equal(state.requestEvidencePending,'next-run');assert.doesNotMatch($('.request-evidence').textContent,/STALE/);
pending.get(paths[1])({requests:[{...compact('shared-id'),output:'CURRENT',first_token_ms:6,evaluation:{messages:[{role:'user',content:'Current question'}],expected:'CURRENT',expected_provided:true}}]});
for(let i=0;i<10;i++)await Promise.resolve();
assert.equal(state.requestEvidenceRun,'next-run');assert.match($('.request-evidence').textContent,/Current question/);assert.match($('.request-evidence').textContent,/Received: CURRENT/);assert.doesNotMatch($('.request-evidence').textContent,/STALE/);
})()
""", dom=True)


def test_real_sort_filter_search_handlers_keep_controls_and_scroll():
    run_render_checks(r"""
(async()=>{
const record=(id,elapsed_ms,score,question)=>({id,target:'local',scenario:'correctness',condition_id:'correctness',status:'completed',valid:true,score,elapsed_ms,evaluation:{messages:[{role:'user',content:question}],expected:'Paris',expected_provided:true},output:score?'Paris':'Lyon'});
detail(report([check('correctness','fail')],{requests:[record('slow',90,true,'Slow question'),record('fast',10,false,'Fast question')]}));
const disclosure=$('.all-requests');disclosure.open=true;listeners.toggle({target:disclosure});
const scroll=$('.request-evidence .request-table-scroll'),sort=$('[data-evidence-sort="elapsed_ms"]');scroll.scrollTop=120;scroll.scrollLeft=45;sort.focus({preventScroll:true});
await listeners.click({target:sort});assert.deepEqual($$('.request-evidence-row').map(row=>row.dataset.request),['fast','slow']);
assert.equal(document.activeElement,sort);assert.equal($('.request-evidence .request-table-scroll'),scroll);assert.equal(scroll.scrollTop,120);assert.equal(scroll.scrollLeft,45);
await listeners.click({target:sort});assert.deepEqual($$('.request-evidence-row').map(row=>row.dataset.request),['slow','fast']);assert.equal(evidenceTableState().direction,'desc');
const filter=$('[data-evidence-filter="status"]');filter.value='fail';filter.focus({preventScroll:true});listeners.change({target:filter});
assert.deepEqual($$('.request-evidence-row').map(row=>row.dataset.request),['fast']);assert.equal(document.activeElement,filter);assert.equal(filter.value,'fail');
filter.value='all';listeners.change({target:filter});
const search=$('[data-evidence-search]');search.value='Slow question';search.focus({preventScroll:true});listeners.input({target:search});
assert.deepEqual($$('.request-evidence-row').map(row=>row.dataset.request),['slow']);assert.equal(document.activeElement,search);assert.equal(search.value,'Slow question');
assert.equal(evidenceTableState().sort,'elapsed_ms');assert.equal(evidenceTableState().direction,'desc');assert.equal(scroll.scrollTop,120);assert.equal(scroll.scrollLeft,45);
})()
""", dom=True)


def test_live_completion_hydrates_requests_that_are_already_open():
    from tests.test_ux_results import run_render_checks
    run_render_checks(r"""
(async()=>{
 fetch=()=>{};loadDetailPlan=()=>{};const paths=[];let resolve;
 api=path=>{paths.push(path);return new Promise(done=>resolve=done);};
 state.detail={id:'finishing-table',state:'running',config:{targets:[]},live_requests:[{slot:0,id:'r',status:'completed',valid:true,score:true}]};renderDetail();
 state.allRequestsOpen=true;$('.all-requests').open=true;
 state.detail={id:'finishing-table',state:'completed',report:report([check('access','pass')],{requests_compact:true,requests:[{id:'r',status:'completed',valid:true,score:true}]})};renderDetail();
 assert.deepEqual(paths,['/api/runs/finishing-table/report.json']);assert($('.all-requests').open);
 renderDetail();assert.equal(paths.length,1);
 resolve({requests:[{id:'r',status:'completed',valid:true,score:true,first_token_ms:123,evaluation:{messages:[{role:'user',content:'Completed question'}],expected:'Expected answer'},output:'Expected answer'}]});
 await Promise.resolve();await Promise.resolve();await Promise.resolve();
 assert.match($('.request-table').textContent,/Completed question/);assert.match($('.request-table').textContent,/123 ms/);assert.equal(paths.length,1);
})()
""",dom=True)


def test_scored_schema_tool_and_rubric_evidence_ignores_default_null_expected():
    run_script(r"""
const base={expected:null,expected_provided:false,messages:[]};
for(const [extra,expected] of [[{schema:{type:'object',required:['city']}},'city'],[{tool_expectation:{expected_calls:[{name:'weather'}]}},'weather'],[{rubric:'Explain the tradeoff'},'Explain the tradeoff']]){
 const html=evidence.renderRequestTable([{id:'scored',score:false,status:'completed',evaluation:{...base,...extra},output:'Wrong answer'}],evidence.createRequestTableState());
 assert(html.includes(expected));assert(!html.includes('Expected: null'));
}
const html=evidence.renderRequestTable([{id:'null',score:true,status:'completed',evaluation:{...base,expected_provided:true},output:'null'}],evidence.createRequestTableState());
assert(html.includes('Expected: null'));
""")


def test_card_view_has_a_labelled_sort_control_with_both_directions():
    run_script(r'''
const state=evidence.createRequestTableState();const html=evidence.renderRequestTable([],state);
assert.match(html,/<label class="request-sort-control">Sort <select[^>]*data-evidence-sort-order/);
assert.match(html,/<option value="elapsed_ms:asc"/);assert.match(html,/<option value="elapsed_ms:desc"/);
evidence.updateRequestTableState(state,'sort-order','elapsed_ms:desc');assert.equal(state.sort,'elapsed_ms');assert.equal(state.direction,'desc');assert.equal(state.page,1);
const before=JSON.stringify(state);evidence.updateRequestTableState(state,'sort-order','injected:desc');assert.equal(JSON.stringify(state),before);
''')


def test_card_sort_control_changes_rows_through_the_real_change_handler():
    run_render_checks(r'''
const records=[{id:'slow',condition_id:'correctness',scenario:'correctness',status:'completed',valid:true,score:true,elapsed_ms:20},{id:'fast',condition_id:'correctness',scenario:'correctness',status:'completed',valid:true,score:true,elapsed_ms:4}];
state.detail={id:'cards',state:'completed',report:report([check('correctness','pass')],{requests:records})};
$('#main').innerHTML=renderMosaic(records);const select=$('[data-evidence-sort-order]');select.value='elapsed_ms:asc';listeners.change({target:select});
assert.equal($('.request-evidence-row').dataset.request,'fast');assert.equal(evidenceTableState().direction,'asc');
const retained=$('[data-evidence-sort-order]');retained.value='elapsed_ms:desc';listeners.change({target:retained});
assert.equal($('.request-evidence-row').dataset.request,'slow');assert.equal(evidenceTableState().direction,'desc');
''',dom=True)
