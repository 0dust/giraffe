"""Evidence remains complete and explains nonpassing verdicts in the results UI."""

from tests.test_ux_results import run_render_checks


def test_live_condition_pages_and_filters_include_evicted_requests():
    run_render_checks(r'''
(async()=>{
const records=Array.from({length:5000},(_,i)=>({id:`r${i}`,target:'local',scenario:'correctness',condition_id:'correctness',status:'completed',valid:true,score:i!==0,elapsed_ms:i+1,question:`Question ${i}`}));
const retained=records.slice(-4096),aggregate={target:'local',condition:{id:'correctness',check:'correctness'},started:5000,completed:5000,failed:1};
state.detail={id:'run',state:'running',live_requests:retained};
renderTests(retained,{live:true,conditions:[aggregate]});
const key=[...conditionViews.keys()][0],view=conditionViews.get(key);
conditionDialog={key,page:1,filter:'all',table:createRequestTableState(),payload:null};
assert.equal(conditionTableRecords(view,conditionDialog).length,4096);
assert.equal(conditionDialog.localEvidence,false,'retained records do not establish completeness');
const calls=[];api=async path=>{
 calls.push(path);const page=Number(path.match(/page=(\d+)/)[1]),status=path.match(/status=([^&]+)/)[1];
 const selected=status==='failed'?[records[0]]:records,offset=(page-1)*50;
 return {page,pages:Math.ceil(selected.length/50),total:selected.length,aggregate,totals:{passed:4999,failed:1},rows:selected.slice(offset,offset+50).map(r=>({request_id:r.id,number:Number(r.id.slice(1))+1,status:r.score?'passed':'failed'}))};
};
$('#test-dialog').open=true;await refreshConditionDialog({force:true});
assert.equal(conditionDialog.localEvidence,false);
assert.equal($$('#test-dialog .request-evidence-row').length,200);
assert.equal($('#test-dialog .request-evidence-row').dataset.request,'r0');
assert.match($('#test-dialog').textContent,/Page 1 of 25/);
assert.match($('#test-dialog').textContent,/Search and sorting apply to this page/);
assert.doesNotMatch($('#test-dialog').textContent,/requests available/);
assert.equal(requestStatus(conditionTableRecords(view,conditionDialog)[0]),'fail');
conditionDialog.page=25;conditionDialog.table.page=25;await refreshConditionDialog({force:true});
assert.match($('#test-dialog').textContent,/Page 25 of 25/);
assert.equal($('#test-dialog .request-evidence-row').dataset.request,'r4800');
assert.match($('#test-dialog').textContent,/Question 4800/,'retained bodies hydrate API rows');
conditionDialog.page=1;conditionDialog.table.page=1;conditionDialog.filter='failed';conditionDialog.table.status='fail';
await refreshConditionDialog({force:true});
assert.equal($$('#test-dialog .request-evidence-row').length,1);
assert.equal($('#test-dialog .request-evidence-row').dataset.request,'r0');
assert.equal(conditionRequestContext('r0').number,1);
assert(calls.some(path=>path.includes('status=failed')));
})()
''', dom=True)


def test_complete_live_condition_keeps_local_sort_search_and_status_filtering():
    run_render_checks(r'''
const records=[{id:'a',target:'local',scenario:'correctness',condition_id:'correctness',status:'completed',valid:true,score:true,elapsed_ms:20,question:'First'},
 {id:'b',target:'local',scenario:'correctness',condition_id:'correctness',status:'completed',valid:true,score:false,elapsed_ms:10,question:'Needle'}];
state.detail={id:'run',state:'running',live_requests:records};
const view={target:'local',condition:{id:'correctness'},summary:{started:2},records};
const d={filter:'all',table:createRequestTableState(),payload:{total:2,totals:{passed:1,failed:1},rows:[]}};
const local=conditionTableRecords(view,d);assert.equal(d.localEvidence,true);
updateRequestTableState(d.table,'sort','elapsed_ms');assert.equal(requestTablePage(local,d.table,{status:requestStatus}).rows[0].id,'b');
updateRequestTableState(d.table,'search','Needle');updateRequestTableState(d.table,'status','fail');
assert.equal(requestTablePage(local,d.table,{status:requestStatus}).rows.length,1);
''')


def test_partial_compact_condition_waits_for_complete_hydration():
    run_render_checks(r'''
const record=id=>({id,target:'local',scenario:'correctness',condition_id:'correctness',status:'completed',valid:true,score:true});
const value=report([check('correctness','pass')],{requests:[record('late')],requests_compact:true,request_count:2});
state.detail={id:'saved',state:'completed',report:value};
const view={target:'local',condition:{id:'correctness'},summary:{started:2},records:value.requests};
const d={filter:'all',payload:{total:2,totals:{passed:2},rows:[{request_id:'early',status:'passed'},{request_id:'late',status:'passed'}]}};
state.requestEvidencePending='saved';state.requestEvidenceRun='other';state.requestEvidenceRecords=new Map([['early',record('early')]]);
let rows=conditionTableRecords(view,d);assert.equal(d.localEvidence,false);assert.equal(rows.length,2);assert.equal(rows[0]._sourceEvidence,false);
state.requestEvidenceRun='saved';state.requestEvidenceRecords=new Map([['early',record('early')],['late',record('late')]]);
rows=conditionTableRecords(view,d);assert.equal(d.localEvidence,true);assert.equal(rows.length,2);
assert(rows.some(row=>row.id==='early'&&row.valid===true));
''')


def test_unknown_live_counts_and_planned_placeholders_use_api_rows():
    run_render_checks(r'''
const r={id:'r',target:'local',scenario:'correctness',condition_id:'correctness',status:'completed',valid:true,score:true};
state.detail={id:'run',state:'running',live_requests:[r]};
const view={target:'local',condition:{id:'correctness'},summary:{},records:[r]},d={filter:'all',payload:{total:2,totals:{passed:1,not_started:1},rows:[{request_id:'r',status:'passed'},{request_id:null,status:'not_started',result_text:'Not sent: stopped'}]}};
let rows=conditionTableRecords(view,d);assert.equal(d.localEvidence,false);assert.equal(rows.length,2);assert.equal(rows[1].error,'Not sent: stopped');
state.detail={id:'run',state:'completed',report:report([],{requests:[r]})};view.summary.started=1;
rows=conditionTableRecords(view,d);assert.equal(d.localEvidence,false);assert.equal(requestStatus(rows[1]),'not_started');
''')


def test_derived_condition_counts_do_not_prove_live_cache_completeness():
    run_render_checks(r'''
const r={id:'late',target:'local',scenario:'correctness',condition_id:'correctness',status:'completed',valid:true,score:true};
state.detail={id:'run',state:'running',live_request_count:5000,live_requests:[r]};
renderTests([r],{live:true});const view=[...conditionViews.values()][0];
assert.equal(view.summary.started,1,'display fallback is based on retained records');
const d={filter:'all',payload:null};conditionTableRecords(view,d);assert.equal(d.localEvidence,false);
d.payload={total:2,rows:[{request_id:'early',status:'failed'},{request_id:'late',status:'passed'}]};
const rows=conditionTableRecords(view,d);assert.equal(d.localEvidence,false);assert.equal(rows[0].id,'early');
''')


def test_scored_nonpassing_checks_keep_reason_and_counts_in_both_views():
    run_render_checks(r'''
for(const id of ['correctness','json','context','tools'])for(const status of ['inconclusive','fail','blocked']){
 const finding='Concurrent answers were not observed <recorded evidence>.';
 const c=check(id,status,{summary:'Fallback reason',finding_summary:finding,metrics:{correct:5,scored:5}}),value=report([c]);
 state.detail={id:'run',state:'completed',report:value};state.selectedCheck=`local:${id}`;
 assert.equal(measuredCheckReason(c,value),finding);
 const html=renderChecks(value);assert.equal((html.match(/Concurrent answers were not observed &lt;recorded evidence&gt;/g)||[]).length,2);
 assert.equal((html.match(/5 of 5 answers matched the expected answer/g)||[]).length,2);
 assert.doesNotMatch(html,/<recorded evidence>/);
}
const passing=check('correctness','pass',{summary:'Legacy summary',metrics:{correct:5,scored:5}});
assert.equal(measuredCheckReason(passing,report([passing])),'5 of 5 answers matched the expected answer.');
assert.equal(renderScoredCheckEvidence(passing),'','passing count is already the primary explanation');
''')


def test_missing_concurrent_answers_has_actionable_remediation():
    run_render_checks(r'''
const c=check('correctness','inconclusive',{finding_summary:'Answers were correct, but concurrent answers were not observed.',metrics:{correct:5,scored:5},remediation:{action:'review_check',check:'correctness',code:'missing_concurrent_answers'}});
const html=detail(report([c]));
assert.match(html,/concurrent answers were not observed/);
assert.match(html,/5 of 5 answers matched/);
assert.match(html,/Enable parallel serving on the server and rerun with streaming/);
assert.match(html,/set Max concurrency to 1 in Limits/);
assert.match(html,/data-check-id="correctness"/);
''')
