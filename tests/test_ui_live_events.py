"""Long-poll fetching, bounded delta merging, and visibility without sockets."""

from tests.test_ui_navigation import run_browser_script


def test_live_deltas_keep_detail_identity_cursor_windows_and_condition_updates():
    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(async()=>{
  schedulePoll=()=>{};renderDetail=()=>{};connectionError=()=>{};
  const condition={target:'local',condition:{id:'warm'},status:'running'};
  const detail=state.detail={id:'live',state:'running',seq:7,config:state.config,
    live_requests:[{slot:0,status:'completed'},{slot:1,status:'running'}],live_conditions:[condition],events:[{seq:7,event:'request_started'}]};
  const paths=[];api=async path=>{paths.push(path);return {seq:9,reset:false,state:'running',progress:{completed:2},
    live_request_count:3,live_request_summary:{through_slot:0,evicted:1},
    events:[{seq:8,event:'request_completed'},{seq:9,event:'condition_finished'}],
    records:[{slot:1,id:'done',status:'completed'},{slot:2,id:'next',status:'running'}],
    checks:[{...condition,status:'completed',completed:2}]};};
  await pollDetail(state.routeVersion);
  assert.equal(paths[0],'/api/runs/live/events?after=7');assert.equal(paths.length,1);
  assert.equal(state.detail,detail);assert.equal(detail.seq,9);assert.equal(detail.progress.completed,2);
  assert.deepEqual(detail.live_requests.map(row=>row.slot),[1,2]);assert.equal(detail.live_requests[0].id,'done');
  assert.equal(detail.live_conditions.length,1);assert.equal(detail.live_conditions[0].status,'completed');
  api=async path=>{paths.push(path);return {seq:9,reset:false,events:[],records:[],checks:[]};};
  await pollDetail(state.routeVersion);assert.equal(paths[1],'/api/runs/live/events?after=9');
  const bounded={events:[],live_requests:[]};
  mergeRunEvents(bounded,{seq:5000,events:Array.from({length:200},(_,seq)=>({seq})),records:Array.from({length:5000},(_,slot)=>({slot})),checks:[]});
  assert.equal(bounded.events.length,100);assert.equal(bounded.live_requests.length,4096);assert.equal(bounded.live_requests[0].slot,904);
})()
`);
""")


def test_completion_fetches_final_detail_and_performance_once_then_stops():
    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(async()=>{
  const paths=[],scheduled=[];schedulePoll=(...args)=>scheduled.push(args);
  const config=state.config,final={id:'live',state:'completed',seq:3,report:{run_id:'live',overall:'pass',checks:[],requests:[],manifest:{config}}};
  let detailReads=0,eventReads=0;
  api=async path=>{paths.push(path);
    if(path==='/api/runs/live')return ++detailReads===1?{id:'live',state:'running',seq:1,config,events:[]}:final;
    if(path.includes('/events?'))return {seq:++eventReads+1,reset:false,state:eventReads===1?'running':'completed',events:[],records:[],checks:[]};
    if(path.endsWith('/performance'))return {targets:[]};
    if(path==='/api/runs')return {runs:[final]};
    throw Error('Unexpected request '+path);
  };
  location.hash='#runs/live';await route();assert.equal(detailReads,1);
  scheduled.length=0;await pollDetail(state.routeVersion);assert.equal(detailReads,1);assert.equal(paths.filter(path=>path.endsWith('/performance')).length,0);
  scheduled.length=0;await pollDetail(state.routeVersion);
  for(let i=0;i<6;i++)await Promise.resolve();
  assert.equal(detailReads,2);assert.equal(state.detail.state,'completed');assert.equal(state.active,null);assert.equal(scheduled.length,0);
  assert.equal(paths.filter(path=>path.endsWith('/performance')).length,1);
  assert.equal(paths.filter(path=>path==='/api/storage').length,0);
  renderDetail();renderDetail();for(let i=0;i<3;i++)await Promise.resolve();
  assert.equal(paths.filter(path=>path.endsWith('/performance')).length,1);
})()
`);
""")


def test_reset_refetches_full_detail_and_uses_new_sequence_for_next_delta():
    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(async()=>{
  schedulePoll=()=>{};renderDetail=()=>{};connectionError=()=>{};
  state.detail={id:'live',state:'running',seq:2,config:state.config,live_requests:[{slot:0}]};
  const paths=[];api=async path=>{paths.push(path);return path.includes('/events?')?{seq:99,reset:true,events:[],records:[],checks:[]}:{id:'live',state:'running',seq:100,config:state.config,live_requests:[{slot:99,status:'completed'}]};};
  await pollDetail(state.routeVersion);assert.deepEqual(paths,['/api/runs/live/events?after=2','/api/runs/live']);
  assert.equal(state.detail.seq,100);assert.deepEqual(state.detail.live_requests.map(row=>row.slot),[99]);
  api=async path=>{paths.push(path);return {seq:100,reset:false,events:[],records:[],checks:[]};};
  await pollDetail(state.routeVersion);assert.equal(paths.at(-1),'/api/runs/live/events?after=100');
})()
`);
""")


def test_storage_and_live_timers_stop_when_hidden_and_resume_without_full_refetch():
    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(async()=>{
  const paths=[];api=async path=>{paths.push(path);return path==='/api/runs'?{runs:[]}:{runs_bytes:10};};
  document.visibilityState='visible';location.hash='#new';await refreshRuns();assert.deepEqual(paths,['/api/runs']);
  location.hash='#runs';await refreshStorage();assert.equal(paths.at(-1),'/api/storage');
  document.visibilityState='hidden';await refreshStorage();assert.equal(paths.length,2);
  let aborted=0;state.eventAbort={abort(){aborted++;}};state.poll=10;listeners.visibilitychange();assert.equal(state.poll,null);assert.equal(aborted,1);
  const scheduled=[];schedulePoll=(...args)=>scheduled.push(args);document.visibilityState='visible';listeners.visibilitychange();
  assert.equal(scheduled.at(-1)[0],pollRuns);
  location.hash='#runs/live';state.reportRouteActive=true;state.detail={id:'live',state:'running',seq:5};listeners.visibilitychange();assert.equal(scheduled.at(-1)[0],pollDetail);
  assert.equal(paths.length,2);
})()
`);
""")


def test_performance_is_fetched_on_tab_open_and_never_from_live_rendering():
    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(async()=>{
  const paths=[];api=async path=>{paths.push(path);return {targets:[]};};
  state.detail={id:'live',state:'running',seq:0,config:state.config,events:[]};renderDetail();
  chooseReportTab('performance');for(let i=0;i<6;i++)await Promise.resolve();
  assert.equal(paths.filter(path=>path.endsWith('/performance')).length,1);
  renderDetail();renderDetail();for(let i=0;i<3;i++)await Promise.resolve();
  assert.equal(paths.filter(path=>path.endsWith('/performance')).length,1);
  await route();for(let i=0;i<3;i++)await Promise.resolve();
  assert.equal(paths.filter(path=>path.endsWith('/performance')).length,1);
  chooseReportTab('results');chooseReportTab('performance');for(let i=0;i<6;i++)await Promise.resolve();
  assert.equal(paths.filter(path=>path.endsWith('/performance')).length,2);
})()
`);
""")


def test_visible_runs_discovers_new_runs_then_watches_events_without_reloading_list():
    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(async()=>{
  schedulePoll=()=>{};renderRuns=()=>{};location.hash='#runs';
  const paths=[];api=async path=>{paths.push(path);
    if(path==='/api/runs')return {runs:[{id:'live',state:'running'}],active_run_id:'live'};
    if(path==='/api/storage')return {};
    if(path.includes('/events?'))return {seq:3,state:'running',events:[],records:[],checks:[]};
    throw Error('Unexpected request '+path);
  };
  await pollRuns(state.routeVersion);assert.equal(state.active,'live');
  paths.length=0;await pollRuns(state.routeVersion);
  assert.deepEqual([...paths].sort(),['/api/runs/live/events?after=0','/api/storage']);
})()
`);
""")


def test_stale_active_delta_cannot_regress_completion_while_final_detail_is_loading():
    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(async()=>{
  schedulePoll=()=>{};renderDetail=()=>{};connectionError=()=>{};refreshRuns=async()=>{};loadPerformance=async()=>{};
  state.detail={id:'live',state:'running',seq:1};
  const replies=[];let finishDetail;
  api=path=>new Promise(resolve=>{if(path.includes('/events?'))replies.push(resolve);else finishDetail=resolve;});
  const completing=pollDetail(state.routeVersion),stale=pollDetail(state.routeVersion);
  replies[0]({seq:3,state:'completed',events:[],records:[],checks:[]});
  for(let i=0;i<6;i++)await Promise.resolve();assert.equal(state.detail.state,'completed');assert(finishDetail);
  replies[1]({seq:2,state:'running',events:[],records:[],checks:[]});await stale;
  assert.equal(state.detail.state,'completed');
  finishDetail({id:'live',state:'completed',seq:3});await completing;assert.equal(state.detail.state,'completed');
})()
`);
""")


def test_delta_resets_idle_and_normal_updates_have_bounded_poll_cadence():
    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(async()=>{
  renderDetail=()=>{};connectionError=()=>{};
  const scheduled=[];schedulePoll=(...args)=>scheduled.push(args);
  state.detail={id:'live',state:'running',seq:1};setActive('live');
  for(const flag of ['reset','idle',null]){
    api=async path=>path.includes('/events?')?{seq:2,state:'running',events:[],records:[],checks:[],...(flag?{[flag]:true}:{})}:{id:'live',state:'running',seq:2};
    await pollDetail(state.routeVersion);
    assert.equal(scheduled.at(-1)[0],pollDetail);assert(scheduled.at(-1)[2]>=(flag?1800:300));
    await pollRunStatus(state.routeVersion);
    assert.equal(scheduled.at(-1)[0],pollRunStatus);assert(scheduled.at(-1)[2]>=(flag?1800:300));
  }
})()
`);
""")


def test_check_result_deltas_are_separate_from_condition_rows_and_update_in_place():
    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(()=>{
  const detail={live_conditions:[{target:'a',condition:{id:'warm'},status:'running'}],
    live_checks:[{target:'a',id:'access',status:'running',title:'Access'}]};
  mergeRunEvents(detail,{seq:3,checks:[{target:'a',condition:{id:'warm'},status:'finished'},
    {target:'a',id:'access',status:'pass',summary:'Available'}, {target:'b',id:'access',status:'fail'}]});
  assert.equal(detail.live_conditions.length,1);assert.equal(detail.live_conditions[0].status,'finished');
  assert.equal(detail.live_checks.length,2);assert.equal(detail.live_checks[0].status,'pass');assert.equal(detail.live_checks[0].title,'Access');
  detail.report={checks:detail.live_checks};
  mergeRunEvents(detail,{seq:4,checks:[{target:'b',id:'access',status:'pass'}]});
  assert.equal(detail.report.checks[1].status,'pass');
})()
`);
""")


def test_hidden_abort_does_not_show_reconnecting_or_replace_resumed_poll():
    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(async()=>{
  state.detail={id:'live',state:'running',seq:1};state.reportRouteActive=true;location.hash='#runs/live';
  const scheduled=[],errors=[];schedulePoll=(...args)=>scheduled.push(args);connectionError=message=>errors.push(message);
  let reject;api=()=>new Promise((resolve,fail)=>{reject=fail;});
  const pending=pollDetail(state.routeVersion);
  state.eventAbort={abort(){reject(Object.assign(new Error('hidden'),{name:'AbortError'}));}};
  document.visibilityState='hidden';listeners.visibilitychange();
  document.visibilityState='visible';listeners.visibilitychange();
  await pending;
  assert.equal(errors.length,0);assert.equal(scheduled.length,1);assert.equal(scheduled[0][0],pollDetail);assert(scheduled[0][2]>=300);
  api=async()=>{throw Object.assign(new Error('hidden'),{name:'AbortError'});};state.active='live';
  await pollRunStatus(state.routeVersion);assert.equal(errors.length,0);assert.equal(scheduled.length,1);
})()
`);
""")


def test_new_page_idle_discovery_is_slow_visible_only_and_notices_external_runs():
    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(async()=>{
  const originalSchedule=schedulePoll,scheduled=[],paths=[];schedulePoll=(...args)=>scheduled.push(args);
  location.hash='#new';state.active=null;document.visibilityState='visible';
  api=async path=>{paths.push(path);return {runs:[]};};
  await pollNew(state.routeVersion);assert.deepEqual(paths,['/api/runs']);assert.equal(scheduled.at(-1)[0],pollNew);assert(scheduled.at(-1)[2]>=10000);
  listeners.visibilitychange();assert.equal(scheduled.at(-1)[0],pollNew);assert(scheduled.at(-1)[2]>=10000);
  schedulePoll=originalSchedule;let timerCalls=0;setTimeout=()=>{timerCalls++;};document.visibilityState='hidden';listeners.visibilitychange();await pollNew(state.routeVersion);assert.equal(timerCalls,0);assert.equal(paths.length,1);
  document.visibilityState='visible';schedulePoll=(...args)=>scheduled.push(args);
  api=async path=>{paths.push(path);return {active_run_id:'external',runs:[{id:'external',state:'running'}]};};
  await pollNew(state.routeVersion);assert.equal(state.active,'external');assert.equal(scheduled.at(-1)[2],3000);
})()
`);
""")


def test_native_fetch_abort_survives_api_wrapper_and_visibility_resume():
    run_browser_script(r"""
const page=browser();await page.run('init()');page.nodes.nativeController=AbortController;
await page.run(`
(async()=>{
  AbortController=nodes.nativeController;
  state.detail={id:'live',state:'running',seq:1};state.reportRouteActive=true;location.hash='#runs/live';
  const errors=[],scheduled=[];connectionError=message=>errors.push(message);schedulePoll=(...args)=>scheduled.push(args);
  for(const phase of ['fetch','body']){
    fetch=async(path,{signal})=>{
      const aborted=new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(signal.reason),{once:true}));
      if(phase==='fetch')return await aborted;
      return {ok:true,json:()=>aborted};
    };
    const pending=pollDetail(state.routeVersion);
    for(let i=0;i<3;i++)await Promise.resolve();
    document.visibilityState='hidden';listeners.visibilitychange();
    document.visibilityState='visible';listeners.visibilitychange();
    await pending;
    assert.equal(errors.length,0);assert.equal(scheduled.length,phase==='fetch'?1:2);
    assert.equal(scheduled.at(-1)[0],pollDetail);
  }
})()
`);
""")


def test_poll_scheduler_enforces_minimum_interval_including_resume():
    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`
(()=>{
  document.visibilityState='visible';const delays=[];setTimeout=(callback,delay)=>{delays.push(delay);};
  for(const callback of [pollDetail,pollRunStatus,pollRuns,pollNew]){schedulePoll(callback,state.routeVersion,0);assert(delays.at(-1)>=300);}
  Date.now=()=>1000;state.eventPollAfter=2800;state.reportRouteActive=true;state.detail={id:'live',state:'running'};location.hash='#runs/live';
  listeners.visibilitychange();assert.equal(delays.at(-1),1800);
})()
`);
""")
