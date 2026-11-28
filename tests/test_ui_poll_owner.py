"""One timer and one cancellable events request across polling mode changes."""

import pytest

from tests.test_ui_navigation import run_browser_script


POLL_BROWSER = r"""
const page=browser();await page.run('init()');page.nodes.nativeController=AbortController;page.nodes.nativeDOMException=DOMException;
await page.run(`
AbortController=nodes.nativeController;
const timers=new Map(),events=[],reads=[],renders=[];
let timerId=0,inFlight=0,maxInFlight=0,holdAbort=false;
Date.now=()=>1000;
setTimeout=(callback,delay)=>{const id=++timerId;timers.set(id,{callback,delay});return id;};
clearTimeout=id=>timers.delete(id);
const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
const fire=async()=>{assert.equal(timers.size,1);const [id,timer]=[...timers][0];timers.delete(id);timer.callback();await flush();};
const delay=()=>{assert.equal(timers.size,1);return [...timers.values()][0].delay;};
const response=body=>({ok:true,status:200,json:async()=>body});
let readRun=async id=>({id,state:'running',seq:1,config:state.config});
let runList={runs:[],active_run_id:null};
fetch=async(path,options={})=>{
  reads.push(path);
  if(path.includes('/events?'))return await new Promise((resolve,reject)=>{
    inFlight++;maxInFlight=Math.max(maxInFlight,inFlight);
    let active=true;
    const ended=()=>{if(active){active=false;inFlight--;}};
    const request={signal:options.signal,
      deliver:body=>{ended();resolve(response(body));},
      fail:error=>{ended();reject(error);}};
    events.push(request);
    options.signal.addEventListener('abort',()=>{ended();if(!holdAbort)reject(options.signal.reason);},{once:true});
  });
  if(path==='/api/runs')return response(runList);
  if(path==='/api/storage')return response({});
  if(path.endsWith('/cancel'))return response({state:'stopping'});
  return response(await readRun(decodeURIComponent(path.slice('/api/runs/'.length))));
};
renderDetail=()=>{state.reportRouteActive=true;renders.push(state.detail.state);};
renderRuns=()=>renders.push('runs');loadPerformance=async()=>{};
nodes['[data-action="stop"]']={disabled:false,textContent:''};
state.detail={id:'live',state:'running',seq:1,config:state.config};
state.reportRouteActive=true;setActive('live');location.hash='#runs/live';
`);
"""


def run_poll_script(script):
    run_browser_script(POLL_BROWSER + "\nawait page.run(`\n" + script + "\n`);\n")


def test_stop_aborts_long_poll_and_leaves_one_timer_after_both_settle():
    run_poll_script(r"""
(async()=>{
  const old=pollDetail(state.routeVersion);await flush();assert.equal(inFlight,1);
  await stopRun();
  assert.equal(events[0].signal.aborted,true,'stop must abort the superseded long-poll');
  assert.equal(delay(),500);await fire();assert.equal(events.length,2);
  assert.equal(maxInFlight,1);
  events[1].deliver({seq:3,state:'stopping',events:[],records:[],checks:[]});
  await old;await flush();assert.equal(inFlight,0);assert.equal(delay(),300);
  assert.equal(nodes['#connection-error'].textContent,'');
})()
""")


@pytest.mark.parametrize("another_active", [False, True])
def test_completion_handoff_cannot_be_replaced_by_stale_detail(another_active):
    run_poll_script(r"""
(async()=>{
  let finishRuns;
  const originalFetch=fetch;
  fetch=(path,options)=>path==='/api/runs'?new Promise(resolve=>{finishRuns=resolve;}):originalFetch(path,options);
  readRun=async()=>({id:'live',state:'completed',seq:4});
  const old=pollDetail(state.routeVersion);await flush();
  events[0].deliver({seq:4,state:'completed',events:[],records:[],checks:[]});await flush();
  assert.equal(state.detail.state,'completed');assert(finishRuns);
  state.active=ANOTHER?'other':null;
  document.visibilityState='hidden';listeners.visibilitychange();
  document.visibilityState='visible';listeners.visibilitychange();
  assert.equal(timers.size,ANOTHER?1:0);if(ANOTHER)assert.equal(delay(),300);
  const renderCount=renders.length;
  finishRuns(response({runs:[],active_run_id:ANOTHER?'other':'obsolete'}));await old;await flush();
  assert.equal(renders.length,renderCount,'stale completion must not render');
  assert.equal(state.active,ANOTHER?'other':null,'stale completion must not publish its active run');
  assert.equal(timers.size,ANOTHER?1:0,'stale detail must not reclaim the timer');
  if(ANOTHER){assert.equal(delay(),300);await fire();assert(reads.at(-1).startsWith('/api/runs/other/events?'));}
  else assert.equal(inFlight,0);
})()
""".replace("ANOTHER", str(another_active).lower()))


def test_hide_aborts_and_stale_success_cannot_render_or_replace_resume_timer():
    run_poll_script(r"""
(async()=>{
  // Model a response already queued when abort is delivered.
  holdAbort=true;
  const old=pollDetail(state.routeVersion);await flush();
  document.visibilityState='hidden';listeners.visibilitychange();
  assert.equal(events[0].signal.aborted,true);assert.equal(inFlight,0);assert.equal(timers.size,0);
  document.visibilityState='visible';listeners.visibilitychange();assert.equal(delay(),300);
  events[0].deliver({seq:2,state:'running',idle:true,events:[],records:[],checks:[]});
  await old;await flush();assert.equal(renders.length,0);assert.equal(delay(),300);
  assert.equal(state.eventPollAfter,1300,'stale idle delta must not postpone the current poll');
})()
""")


@pytest.mark.parametrize(("view", "active", "resume_delay"), [
    ("#runs", True, 300), ("#runs/live", True, 300),
    ("#new", True, 300), ("#new", False, 10000),
    ("#runs/saved", True, 300),
])
def test_route_hide_and_resume_keep_exactly_one_poll_with_existing_delay(view, active, resume_delay):
    run_poll_script(r"""
(async()=>{
  const old=pollDetail(state.routeVersion);await flush();
  location.hash='#runs/saved';readRun=async()=>({id:'saved',state:'completed',report:null});
  await route();await old;assert.equal(events[0].signal.aborted,true);assert.equal(inFlight,0);
  assert.equal(delay(),3000);
  document.visibilityState='hidden';listeners.visibilitychange();assert.equal(timers.size,0);
  location.hash=VIEW;state.active=ACTIVE?'live':null;
  state.detail=VIEW==='#runs/live'?{id:'live',state:'running',seq:1}:{id:'saved',state:'completed'};
  state.reportRouteActive=VIEW.startsWith('#runs/');
  document.visibilityState='visible';listeners.visibilitychange();assert.equal(delay(),DELAY);
  await fire();
  if(ACTIVE){
    assert.equal(inFlight,1);assert.equal(timers.size,0);
    document.visibilityState='hidden';listeners.visibilitychange();await flush();
    assert.equal(events.at(-1).signal.aborted,true);assert.equal(inFlight,0);assert.equal(timers.size,0);
  }else assert.equal(delay(),10000);
})()
""".replace("VIEW", repr(view)).replace("ACTIVE", str(active).lower()).replace("DELAY", str(resume_delay)))


def test_expire_access_aborts_events_and_invalidates_a_queued_success():
    run_poll_script(r"""
(async()=>{
  holdAbort=true;
  const old=pollDetail(state.routeVersion);await flush();
  expireAccess();assert.equal(events[0].signal.aborted,true);assert.equal(inFlight,0);assert.equal(timers.size,0);
  events[0].deliver({seq:2,state:'running',events:[],records:[],checks:[]});await old;
  listeners.visibilitychange();assert.equal(timers.size,0);assert.equal(renders.length,0);
  assert.match(nodes['#connection-error'].textContent,/Giraffe was restarted/);
})()
""")


@pytest.mark.parametrize("view", ["#new", "#runs"])
def test_resumed_discovery_owns_timer_when_old_discovery_finishes(view):
    run_poll_script(r"""
(async()=>{
  location.hash=VIEW;state.active=null;state.reportRouteActive=false;
  let finishOld;
  const originalFetch=fetch;
  fetch=(path,options)=>path==='/api/runs'?new Promise(resolve=>{finishOld=resolve;}):originalFetch(path,options);
  const old=VIEW==='#new'?pollNew(state.routeVersion):pollRuns(state.routeVersion);await flush();
  document.visibilityState='hidden';listeners.visibilitychange();
  document.visibilityState='visible';listeners.visibilitychange();const resumedDelay=delay();
  const renderCount=renders.length;
  finishOld(response({runs:[],active_run_id:'external'}));await old;await flush();
  assert.equal(renders.length,renderCount,'superseded discovery must not render');
  assert.equal(state.active,null,'superseded discovery must not publish a discovered active run');
  assert.equal(delay(),resumedDelay,'superseded discovery must not replace resumed timer');
})()
""".replace("VIEW", repr(view)))


@pytest.mark.parametrize("another_active", [False, True])
def test_completion_continues_only_status_for_another_active_run(another_active):
    run_poll_script(r"""
(async()=>{
  readRun=async()=>({id:'live',state:'completed',seq:4});
  runList={runs:[],active_run_id:ANOTHER?'other':null};
  const pending=pollDetail(state.routeVersion);await flush();
  events[0].deliver({seq:4,state:'completed',events:[],records:[],checks:[]});await pending;
  assert.equal(state.detail.state,'completed');assert.equal(timers.size,ANOTHER?1:0);
  if(ANOTHER){assert.equal(delay(),3000);await fire();assert.equal(events.length,2);assert(reads.at(-1).startsWith('/api/runs/other/events?'));}
  else assert.equal(inFlight,0);
})()
""".replace("ANOTHER", str(another_active).lower()))


def test_stale_reset_snapshot_cannot_replace_resumed_detail_timer_after_stop():
    run_poll_script(r"""
(async()=>{
  let finishSnapshot;
  readRun=()=>new Promise(resolve=>{finishSnapshot=resolve;});
  const old=pollDetail(state.routeVersion);await flush();
  events[0].deliver({seq:2,reset:true});await flush();assert(finishSnapshot);
  await stopRun();assert.equal(delay(),1800);
  document.visibilityState='hidden';listeners.visibilitychange();
  // Let the reset minimum interval elapse before resuming.
  Date.now=()=>3000;
  document.visibilityState='visible';listeners.visibilitychange();assert.equal(delay(),300);
  const renderCount=renders.length;
  finishSnapshot({id:'live',state:'running',seq:2});await old;
  assert.equal(renders.length,renderCount);assert.equal(state.detail.state,'stopping');
  assert.equal(delay(),300,'superseded snapshot must not replace resume with a 500ms stop retry');
})()
""")


@pytest.mark.parametrize("mode", ["detail", "status"])
@pytest.mark.parametrize("delta_flag", ["reset", "idle", "normal"])
def test_event_modes_keep_exact_cadence_and_resume_minimum(mode, delta_flag):
    run_poll_script(r"""
(async()=>{
  location.hash=MODE==='detail'?'#runs/live':'#runs/saved';
  if(MODE==='status')state.detail={id:'saved',state:'completed'};
  const pending=MODE==='detail'?pollDetail(state.routeVersion):pollRunStatus(state.routeVersion);await flush();
  const delta={seq:2,state:'running',events:[],records:[],checks:[]};
  if(FLAG!=='normal')delta[FLAG]=true;
  events[0].deliver(delta);await pending;
  const expected=FLAG==='normal'?300:1800;
  assert.equal(delay(),expected);
  document.visibilityState='hidden';listeners.visibilitychange();assert.equal(timers.size,0);
  document.visibilityState='visible';listeners.visibilitychange();assert.equal(delay(),expected);
})()
""".replace("MODE", repr(mode)).replace("FLAG", repr(delta_flag)))


def test_starting_events_aborts_previous_controller_and_keeps_new_one_owned():
    run_poll_script(r"""
(async()=>{
  const old=runEvents('live',1).catch(error=>{assert.equal(error.name,'AbortError');});await flush();
  const current=runEvents('live',2);await flush();
  assert.equal(events[0].signal.aborted,true);assert.equal(events[1].signal.aborted,false);
  assert.equal(maxInFlight,1);await old;
  document.visibilityState='hidden';listeners.visibilitychange();
  await assert.rejects(current,error=>error.name==='AbortError');
  assert.equal(events[1].signal.aborted,true);assert.equal(inFlight,0);assert.equal(timers.size,0);
})()
""")


def test_runs_route_failure_keeps_status_only_requests_and_error_view():
    run_poll_script(r"""
(async()=>{
  const originalFetch=fetch;
  fetch=(path,options)=>path==='/api/runs'?Promise.reject(Error('offline')):originalFetch(path,options);
  location.hash='#runs';await route();assert.equal(delay(),3000);
  const markup=nodes['#main'].innerHTML;assert(markup.includes('Could not open this view'));
  const before=reads.length;await fire();
  assert.deepEqual(reads.slice(before),['/api/runs/live/events?after=0']);
  events.at(-1).deliver({seq:2,state:'running',events:[],records:[],checks:[]});await flush();
  assert.equal(nodes['#main'].innerHTML,markup);assert.equal(delay(),300);
})()
""")


@pytest.mark.parametrize("active", [False, True])
def test_fallback_route_preserves_initial_runs_and_resumed_status_cadence(active):
    run_poll_script(r"""
(async()=>{
  runList={runs:[],active_run_id:ACTIVE?'live':null};
  location.hash='#unknown';await route();assert.equal(delay(),3000);
  document.visibilityState='hidden';listeners.visibilitychange();assert.equal(timers.size,0);
  document.visibilityState='visible';listeners.visibilitychange();assert.equal(timers.size,ACTIVE?1:0);
  if(ACTIVE){
    assert.equal(delay(),300);const before=reads.length;await fire();
    assert.deepEqual(reads.slice(before),['/api/runs/live/events?after=0']);
    events.at(-1).deliver({seq:2,state:'running',events:[],records:[],checks:[]});await flush();
    assert.equal(delay(),300);
  }
})()
""".replace("ACTIVE", str(active).lower()))


@pytest.mark.parametrize("loop", ["detail", "status", "events"])
@pytest.mark.parametrize("dom_exception", [False, True])
def test_abort_without_reason_is_a_silent_abort_error(loop, dom_exception):
    run_poll_script(r"""
(async()=>{
  AbortController=class {
    constructor(){this.signal={aborted:false,addEventListener(kind,listener){this.onabort=listener;}};}
    abort(){this.signal.aborted=true;this.signal.onabort?.();}
  };
  holdAbort=true;DOMException=DOM_EXCEPTION?nodes.nativeDOMException:undefined;
  location.hash=LOOP==='status'?'#runs/saved':'#runs/live';
  if(LOOP==='status')state.detail={id:'saved',state:'completed'};
  const pending=LOOP==='events'?runEvents('live',1).then(
    ()=>assert.fail('aborted events must reject'),error=>assert.equal(error?.name,'AbortError')):
    LOOP==='detail'?pollDetail(state.routeVersion):pollRunStatus(state.routeVersion);
  await flush();cancelLiveFetch();assert.equal(events[0].signal.aborted,true);
  assert.equal(events[0].signal.reason,undefined);
  events[0].deliver({seq:2,state:'running',events:[],records:[],checks:[]});
  await pending;assert.equal(timers.size,0);assert.equal(inFlight,0);
  assert.equal(nodes['#connection-error'].textContent,'');assert.equal(renders.length,0);
})()
""".replace("LOOP", repr(loop)).replace("DOM_EXCEPTION", str(dom_exception).lower()))


@pytest.mark.parametrize("phase", ["events", "reset"])
@pytest.mark.parametrize(("mode", "next_delay"), [("status", 300), ("runs", 3000), ("new", 3000)])
def test_active_run_change_midflight_keeps_current_mode_polling_new_run(phase, mode, next_delay):
    run_poll_script(r"""
(async()=>{
  location.hash=MODE==='status'?'#runs/saved':MODE==='runs'?'#runs':'#new';
  state.detail={id:'saved',state:'completed'};
  state.runs=[{id:'live',state:'running'}];
  const pending=MODE==='status'?pollRunStatus(state.routeVersion):
    MODE==='runs'?pollRuns(state.routeVersion):pollNew(state.routeVersion);
  await flush();assert.equal(events.length,1);
  let finishSnapshot;
  if(PHASE==='reset'){
    readRun=()=>new Promise(resolve=>{finishSnapshot=resolve;});
    events[0].deliver({seq:2,reset:true});await flush();assert(finishSnapshot);
  }
  state.active='other';Date.now=()=>3000;
  if(PHASE==='reset')finishSnapshot({id:'live',state:'completed',seq:2});
  else events[0].deliver({seq:2,state:'completed',events:[],records:[],checks:[]});
  await pending;
  assert.equal(state.active,'other');assert.equal(state.statusRun.id,'live');
  assert.equal(state.statusRun.state,'running');assert.equal(state.runs[0].state,'running');
  assert.equal(delay(),DELAY);assert.equal(nodes['#connection-error'].textContent,'');
  await fire();assert.equal(events.length,2);
  assert(reads.includes('/api/runs/other/events?after=0'));assert.equal(maxInFlight,1);
  document.visibilityState='hidden';listeners.visibilitychange();await flush();
  assert.equal(inFlight,0);assert.equal(timers.size,0);
})()
""".replace("PHASE", repr(phase)).replace("MODE", repr(mode)).replace("DELAY", str(next_delay)))


@pytest.mark.parametrize("current", [False, True])
def test_detail_non_abort_error_reconnects_and_retries_only_when_current(current):
    run_poll_script(r"""
(async()=>{
  holdAbort=true;
  const pending=pollDetail(state.routeVersion);await flush();
  if(!CURRENT)applyPollMode(500);
  events[0].fail(Error('offline'));await pending;
  assert.equal(delay(),CURRENT?3000:500);assert.equal(inFlight,0);assert.equal(renders.length,0);
  const banner=nodes['#connection-error'];
  if(CURRENT){assert.match(banner.textContent,/Reconnecting; the suite may still be running\./);assert.equal(banner.hidden,false);}
  else assert.equal(banner.textContent,'');
})()
""".replace("CURRENT", str(current).lower()))


@pytest.mark.parametrize("result", ["success", "error"])
def test_detail_epoch_change_retries_once_at_500_without_reconnecting(result):
    run_poll_script(r"""
(async()=>{
  const pending=pollDetail(state.routeVersion);await flush();
  ++detailEpoch;assert.equal(state.stopPending,undefined);
  if(RESULT==='error')events[0].fail(Error('offline'));
  else events[0].deliver({seq:2,state:'running',events:[],records:[],checks:[]});
  await pending;
  assert.equal(delay(),500);assert.equal(inFlight,0);assert.equal(renders.length,0);
  assert.equal(nodes['#connection-error'].textContent,'');
})()
""".replace("RESULT", repr(result)))


def test_consecutive_mode_applications_keep_one_timer_and_one_events_request():
    run_poll_script(r"""
(async()=>{
  const pending=pollDetail(state.routeVersion);await flush();assert.equal(inFlight,1);
  applyPollMode();applyPollMode();await pending;
  assert.equal(events[0].signal.aborted,true);assert.equal(inFlight,0);assert.equal(delay(),300);
  await fire();assert.equal(inFlight,1);assert.equal(timers.size,0);assert.equal(events.length,2);
  applyPollMode();applyPollMode();await flush();
  assert.equal(events[1].signal.aborted,true);assert.equal(inFlight,0);assert.equal(delay(),300);
  assert.equal(maxInFlight,1);assert.equal(nodes['#connection-error'].textContent,'');
})()
""")


@pytest.mark.parametrize("mode", ["detail", "status"])
def test_superseded_reset_releases_reconciliation_reader_without_publishing(mode):
    run_poll_script(r"""
(async()=>{
  location.hash=MODE==='detail'?'#runs/live':'#runs/saved';
  if(MODE==='status')state.detail={id:'saved',state:'completed'};
  let finishSnapshot;
  readRun=()=>new Promise(resolve=>{finishSnapshot=resolve;});
  const pending=MODE==='detail'?pollDetail(state.routeVersion):pollRunStatus(state.routeVersion);
  await flush();events[0].deliver({seq:2,reset:true});await flush();assert(finishSnapshot);
  assert.equal(runWrites.get('live').readers,1);
  document.visibilityState='hidden';listeners.visibilitychange();
  document.visibilityState='visible';listeners.visibilitychange();assert.equal(delay(),1800);
  const renderCount=renders.length;
  finishSnapshot({id:'live',state:'completed',seq:2,kept:false});await pending;
  assert.equal((MODE==='detail'?state.detail:state.statusRun).state,'running');
  assert.equal(state.active,'live');assert.equal(renders.length,renderCount);
  assert.equal(runWrites.size,0,'stale reset must release main reconciliation metadata');
  assert.equal(delay(),1800,'stale reset must not replace the resumed timer');
})()
""".replace("MODE", repr(mode)))
