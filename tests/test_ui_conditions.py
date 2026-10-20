"""Condition cards keep a test distinct from its individual request evidence."""

from tests.test_ux_results import run_render_checks


def test_one_condition_has_one_card_and_all_32_request_spots():
    run_render_checks(r"""
const condition={id:'bucket_c32_short_short',label:'Short input / short output · 32 at once',check:'size_matrix'};
const records=Array.from({length:32},(_,slot)=>({slot,id:'r'+slot,target:'local',scenario:condition.id,condition_id:condition.id,status:'completed',valid:true,score:true}));
const html=renderTests(records,{live:true,conditions:[{target:'local',condition,planned:32,planned_kind:'exact',started:32,completed:32,running:0,failed:0,not_sent:0,status:'finished'}]});
assert.equal((html.match(/class="test-condition"/g)||[]).length,1);
assert.match(html,/Short input \/ short output · 32 at once/);
for(const label of ['Planned','Started','Running','Completed','Failed/timed out','Not started'])assert(html.includes(label));
assert.match(html,/<dt>Planned<\/dt><dd>32<\/dd>/);
assert.match(html,/<dt>Completed<\/dt><dd>32<\/dd>/);
assert.match(html,/Finished/);
assert.equal((html.match(/class="request-spot /g)||[]).length,32);
assert.match(html,/data-live-slot="31"/);
assert.match(html,/<details class="condition-requests"[^>]*><summary>Individual requests · 32<\/summary>/);
""")


def test_partial_dispatch_and_live_totals_survive_capped_request_evidence():
    run_render_checks(r"""
const condition={id:'partial',label:'Partial cell',check:'size_matrix'};
const html=renderTests([{slot:0,target:'local',condition_id:'partial',status:'timeout'}],{live:true,conditions:[{target:'local',condition,planned:32,planned_kind:'upper_bound',started:24,completed:24,running:0,failed:3,not_sent:8,status:'finished',reason:'sending window ended'}]});
assert.match(html,/<dt>Planned<\/dt><dd>up to 32<\/dd>/);
assert.match(html,/<dt>Started<\/dt><dd>24<\/dd>/);
assert.match(html,/<dt>Failed\/timed out<\/dt><dd>3<\/dd>/);
assert.match(html,/Stopped early/);assert.match(html,/8 not sent: sending window ended/);
assert.match(html,/3 of 24 requests failed/);
""")


def test_saved_condition_keeps_group_check_verdict_separate_from_request_failures():
    run_render_checks(r"""
const condition={id:'cell',label:'Cell <32>',check:'buckets'};
const records=Array.from({length:32},(_,i)=>({id:'r'+i,target:'local',condition_id:'cell',status:i<3?'timeout':'completed',valid:true,score:true}));
const saved=report([check('concurrency','fail',{metrics:{scenarios:{size_matrix:{id:'buckets',status:'inconclusive'}}}})],{requests:records,observations:{targets:{local:{conditions:[{condition,planned:32,planned_kind:'exact',status:'finished',started:32,completed:32,running:0,failed:3,not_sent:0}]}}}});
const html=renderTests(records,{report:saved});
assert.match(html,/Cell &lt;32&gt;/);assert.match(html,/Finished/);
assert.match(html,/<div class="test-check-heading">[\s\S]*?class="status fail"/);assert.match(html,/3 of 32 requests failed/);
assert.doesNotMatch(html.match(/<article[\s\S]*?<\/article>/)[0],/class="status /);
assert.equal((html.match(/data-request=/g)||[]).length,32);
""")


def test_legacy_saved_requests_group_by_scenario_and_target():
    run_render_checks(r"""
const records=[{id:'a',target:'local',scenario:'warm',status:'completed'},
 {id:'b',target:'local',scenario:'warm',status:'timeout'},
 {id:'c',target:'other',scenario:'warm',status:'completed'}];
const html=renderTests(records,{report:report([],{requests:records})});
assert.equal((html.match(/class="test-condition"/g)||[]).length,2);
assert.equal((html.match(/Warm-up/g)||[]).length,2);
assert.match(html,/<h3 class="test-target">local<\/h3>/);assert.match(html,/<h3 class="test-target">other<\/h3>/);
assert.match(html,/1 of 2 requests failed/);
assert.equal((html.match(/class="request-spot /g)||[]).length,3);
""")


def test_individual_request_mosaic_is_secondary_closed_and_preserved_live_and_saved():
    run_render_checks(r"""
const records=[{id:'one',target:'local',scenario:'warm',status:'completed'}];
let html=detail(report([check('access','pass')],{requests:records}));
assert(html.indexOf('<h2>Tests</h2>')<html.indexOf('class="all-requests"'));
assert.match(html,/<details class="all-requests"[^>]*><summary>Individual requests · 1<\/summary>/);
assert.doesNotMatch(html,/<details class="all-requests"[^>]*\bopen\b/);
assert.match(html,/<details class="all-requests"[\s\S]*class="request-mosaic/);
state.detail={id:'live',state:'running',config:{targets:[],max_requests:32},live_requests:[{slot:0,target:'local',scenario:'warm',status:'running'}]};renderLive();html=nodes['#main'].innerHTML;
assert.match(html,/<details class="all-requests"[^>]*><summary>Individual requests · 1<\/summary>/);
assert.match(html,/request-mosaic is-live/);assert.match(html,/data-live-slot="0"/);
""")


def test_closed_condition_spots_are_cached_and_refreshed_when_opened():
    run_render_checks(r"""
state.detail={id:'cached',state:'running'};
const record={slot:0,target:'local',scenario:'warm',status:'running'};
let html=renderTests([record],{live:true});
assert.match(html,/class="request-spot running"/);
record.status='timeout';html=renderTests([record],{live:true});
assert.match(html,/1 of 1 requests failed/);assert.match(html,/class="request-spot running"/);
const key=[...conditionViews.keys()][0],content={innerHTML:''};
const disclosure={open:true,dataset:{conditionKey:key},matches:s=>s==='.condition-requests',querySelector:()=>content};
listeners.toggle({target:disclosure});assert.match(content.innerHTML,/class="request-spot fail"/);
html=renderTests([record],{live:true});assert.match(html,/<details class="condition-requests"[^>]* open>/);assert.match(html,/class="request-spot fail"/);
""")


def test_legacy_unscored_requests_and_unknown_plans_stay_qualified():
    run_render_checks(r"""
const records=[{id:'null',target:'local',scenario:'warm',status:'completed',valid:true,score:null},
 {id:'missing',target:'local',scenario:'warm',status:'completed',valid:true}];
let html=renderTests(records,{report:report([check('serving','inconclusive',{evidence_ids:['null','missing']})],{requests:records})});
assert.equal((html.match(/class="request-spot inconclusive"/g)||[]).length,2);
assert.match(html,/class="status inconclusive"/);assert.doesNotMatch(html,/requests failed/);
html=renderTests(records,{live:true,conditions:[{target:'local',condition:{id:'warm',label:'Warm-up',check:'serving'},planned:null,not_sent:null,status:'running',started:2,completed:2,failed:0,running:0}]});
assert.match(html,/<dt>Planned<\/dt><dd>—<\/dd>/);
assert.match(html,/<dt>Not started<\/dt><dd>—<\/dd>/);
""")


def test_disclosures_keep_open_state_when_run_finishes():
    run_render_checks(r"""
state.detail={id:'same-run',state:'running'};
const record={id:'r',slot:0,target:'local',scenario:'warm',status:'completed'};
renderTests([record],{live:true});const key=[...conditionViews.keys()][0];conditionOpen.add(key);state.allRequestsOpen=true;
state.detail.state='completed';
const html=renderTests([record],{report:report([],{requests:[record]})});
assert.match(html,/<details class="condition-requests"[^>]* open>/);assert.match(html,/data-request="r"/);assert.doesNotMatch(html,/data-live-slot=/);
assert.match(renderAllRequests([record]),/<details class="all-requests" open>/);
""")


def test_live_poll_preserves_condition_and_all_requests_summary_focus():
    from tests.test_ui_navigation import run_browser_script

    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(async()=>{
state.detail={id:'focus-run',state:'running',config:state.config,live_requests:[{slot:0,target:'local',scenario:'warm',status:'running'}]};
renderLive();const key=[...conditionViews.keys()][0];
let focused=0;
const condition={dataset:{conditionKey:key},querySelector:()=>({focus(options){assert(options.preventScroll);focused++;}})};
const all={querySelector:()=>({focus(options){assert(options.preventScroll);focused++;}})};
nodes['.all-requests']=all;const help={};nodes['.condition-count-help > summary']={focus(options){assert(options.preventScroll);focused++;}};
document.querySelectorAll=q=>q==='.condition-requests'?[condition]:[];
api=async()=>state.detail;
for(const selected of [condition,all,help]){
 document.activeElement={tagName:'SUMMARY',closest:q=>q==='.condition-requests'?selected===condition?condition:null:q==='.all-requests'?selected===all?all:null:q==='.condition-count-help'?selected===help?help:null:null};
 await pollDetail(state.routeVersion);
}
assert.equal(focused,3);
})()
`);
""")


def test_saved_verdict_uses_record_evidence_when_default_check_was_not_selected():
    run_render_checks(r"""
const record={id:'first',target:'local',scenario:'initial',condition_id:'initial',status:'completed'};
const saved=report([check('access','skipped'),check('serving','pass',{evidence_ids:['first']})],{requests:[record],observations:{targets:{local:{conditions:[{condition:{id:'initial',label:'First request',check:'access'},planned:1,status:'finished',started:1,completed:1,running:0,failed:0,not_sent:0}]}}}});
const html=renderTests([record],{report:saved});assert.match(html,/class="status pass"/);assert.doesNotMatch(html,/class="status skipped"/);
""")


def test_live_poll_restores_mosaic_spot_in_its_own_disclosure():
    from tests.test_ui_navigation import run_browser_script

    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(async()=>{
state.detail={id:'slots',state:'running',config:state.config,live_requests:[{slot:0,target:'local',scenario:'warm',status:'running'}]};renderLive();
let restored=0,hiddenFocused=0;
const visible={focus(){restored++;}},hidden={focus(){hiddenFocused++;}};
nodes['[data-live-slot="0"]']=hidden;
nodes['.all-requests']={querySelector:q=>q==='[data-live-slot="0"]'?visible:null};
document.activeElement={dataset:{liveSlot:'0'},closest:()=>null};api=async()=>state.detail;
await pollDetail(state.routeVersion);assert.equal(restored,1);assert.equal(hiddenFocused,0);
})()
`);
""")


def test_restored_open_disclosure_toggle_does_not_replace_focused_spots():
    run_render_checks(r"""
state.detail={id:'native-toggle',state:'running'};
const record={slot:0,target:'local',scenario:'warm',status:'running'};
renderTests([record],{live:true});const key=[...conditionViews.keys()][0];
conditionOpen.add(key);renderTests([record],{live:true});
let writes=0;
const content={get innerHTML(){return conditionSpots.get(key);},set innerHTML(value){writes++;}};
const disclosure={open:true,dataset:{conditionKey:key},matches:s=>s==='.condition-requests',querySelector:()=>content};
listeners.toggle({target:disclosure});
assert.equal(writes,0,'queued toggle on recreated open details must retain restored focused spot');
""")


def test_condition_cards_only_show_explicit_condition_results_and_group_check_verdict_once():
    run_render_checks(r"""
const conditions=['short','long'].map(shape=>({condition:{id:'bucket_c32_'+shape+'_short',label:shape,check:'buckets',level:32},planned:32,status:'finished',started:32,completed:32,failed:0,not_sent:0}));
const saved=report([check('concurrency','fail',{metrics:{scenarios:{size_matrix:{id:'buckets',status:'fail',metrics:{buckets:{bucket_c32_short_short:{status:'pass'},bucket_c32_long_short:{coverage_gaps:['Not enough overlap']}}}}}}})],{observations:{targets:{local:{conditions}}}});
const html=renderTests([],{report:saved});
assert.equal((html.match(/class="status fail"/g)||[]).length,1,'owning verdict appears on the check header once');
assert.match(html,/<div class="test-check-heading"><h3>Concurrent workloads<\/h3>[\s\S]*?class="status fail"/);
assert.match(html,/data-condition-check="concurrency"/);assert.match(html,/2 tests/);
const cards=html.match(/<article class="test-condition"[\s\S]*?<\/article>/g);
assert.match(cards[0],/class="status pass"/);assert.doesNotMatch(cards[0],/class="status fail"/);
assert.doesNotMatch(cards[1],/class="status (?:pass|fail|inconclusive)"/,'unassessed cell must not inherit check verdict');
""")


def test_explicit_consistency_stress_and_tool_results_map_to_own_cards():
    run_render_checks(r"""
const checks=[check('consistency','fail',{metrics:{logprobs:{conditions:[{shape:'short',level:4,status:'pass'},{shape:'long',level:4,status:'fail'}]}}}),check('concurrency','fail',{metrics:{scenarios:{token_stress:{id:'token_stress',status:'fail',metrics:{cases:{reasoning:{levels:{4:{status:'pass'}}}}}}}}}),check('tools','fail',{metrics:{conversations:[{fixture_id:'weather',mode:'stream',status:'pass'}]}})];
const conditions=[['consistency_c4_short','consistency'],['stress_reasoning_c4','token_stress'],['tools_weather_stream','tools']].map(([id,check])=>({condition:{id,label:id,check},planned:1,started:1,completed:1,status:'finished',failed:0,not_sent:0}));
const html=renderTests([],{report:report(checks,{observations:{targets:{local:{conditions}}}})});
const cards=html.match(/<article class="test-condition"[\s\S]*?<\/article>/g);
assert.equal(cards.length,3);for(const card of cards){assert.match(card,/class="status pass"/);assert.doesNotMatch(card,/class="status fail"/);}
""")


def test_matrix_54_waiting_cards_are_compact_grouped_by_six_levels():
    run_render_checks(r"""
state.detail={id:'planned-matrix',state:'running',config:{targets:[{name:'local'}]}};
const conditions=[1,2,4,8,16,32].flatMap(level=>['short','medium','long'].flatMap(input=>['short','medium','long'].map(output=>({target:'local',condition:{id:`bucket_c${level}_${input}_${output}`,label:`${input} / ${output}`,check:'buckets',level},planned:Math.max(8,level),planned_kind:'upper_bound',started:0,completed:0,failed:0,running:0,not_sent:Math.max(8,level),status:'waiting'}))));
const html=renderTests([],{conditions,live:true});
assert.equal((html.match(/class="test-condition"/g)||[]).length,54);
assert.equal((html.match(/class="test-check"/g)||[]).length,1);
assert.equal((html.match(/class="test-level"/g)||[]).length,6);
assert.match(html,/<h4 class="test-level">32 at once<\/h4>/);
assert.equal((html.match(/class="condition-status">Waiting/g)||[]).length,54);
assert.match(html,/Started 0\/up to 8 · Failed 0 · Not started 8/);
const card=html.match(/<article class="test-condition"[\s\S]*?<\/article>/)[0];
assert(card.indexOf('class="condition-counts"')>card.indexOf('<details class="condition-requests"'));
assert.doesNotMatch(card.split('<details')[0],/<dl/);
assert.match(html,/<details class="condition-count-help"><summary>What do these counts mean\?<\/summary>/);
assert.doesNotMatch(html,/<details class="condition-count-help" open/);
""")


def test_card_request_focus_does_not_pause_individual_request_mosaic_following():
    run_render_checks(r"""
const records=[{slot:0,status:'completed'},{slot:240,status:'running'}];
state.mosaicPage=0;state.mosaicFollow=true;state.mosaicAutoFocus=null;
document.activeElement={dataset:{liveSlot:'0'},closest:q=>q==='.condition-requests'?{}:null};
renderMosaic(records,{live:true,budget:600});
assert.equal(state.mosaicFollow,true);assert.equal(state.mosaicPage,1);
state.mosaicPage=0;state.mosaicFollow=true;
document.activeElement={dataset:{liveSlot:'0'},closest:q=>q==='.request-mosaic'?{}:null};
renderMosaic(records,{live:true,budget:600});
assert.equal(state.mosaicFollow,false);assert.equal(state.mosaicPage,0);
""")


def test_condition_events_do_not_render_in_activity_or_replace_live_phase():
    run_render_checks(r"""
state.detail={id:'events',state:'running',config:{targets:[],max_requests:32},events:[{event:'request_completed',completed:1},{event:'condition_started',condition:{id:'secret-start'}},{event:'condition_finished',condition:{id:'secret-finish'}}]};
renderLive();const html=nodes['#main'].innerHTML;
assert.match(html,/Request completed/);assert.doesNotMatch(html,/Condition started|Condition finished|secret-start|secret-finish/);
""")


def test_count_definitions_disclosure_stays_open_through_live_poll_rendering():
    run_render_checks(r"""
state.detail={id:'count-help',state:'running'};
renderTests([],{live:true});
listeners.toggle({target:{open:true,matches:s=>s==='.condition-count-help'}});
let html=renderTests([],{live:true});
assert.match(html,/<details class="condition-count-help" open>/);
state.detail.id='different-run';html=renderTests([],{live:true});
assert.doesNotMatch(html,/<details class="condition-count-help" open>/);
""")
