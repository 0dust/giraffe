"""A restarted UI server ends polling and explains how to restore access."""

from tests.test_ui_navigation import run_browser_script


def test_unauthorized_poll_shows_one_persistent_banner_and_stops_polling():
    run_browser_script(r"""
const page=browser();await page.run('init()');page.nodes.nativeController=AbortController;
await page.run(`
(async()=>{
  AbortController=nodes.nativeController;
  state.detail={id:'live',state:'running',seq:1};state.reportRouteActive=true;location.hash='#runs/live';
  const timers=new Map();let timerCount=0,requests=0,signal;
  setTimeout=callback=>{const id=++timerCount;timers.set(id,callback);return id;};
  clearTimeout=id=>timers.delete(id);
  schedulePoll(pollDetail,state.routeVersion);assert.equal(timers.size,1);
  const banner=nodes['#connection-error'];let text=banner.textContent,writes=0;
  Object.defineProperty(banner,'textContent',{get:()=>text,set:value=>{text=value;writes++;}});
  fetch=async(path,options)=>{requests++;signal=options.signal;return {ok:false,status:401,json:async()=>({detail:'Open the access link printed by giraffe ui'})};};
  await pollDetail(state.routeVersion);
  const message='Giraffe was restarted. Open the new link printed by giraffe ui in your terminal.';
  assert.equal(banner.textContent,message);assert.equal(banner.hidden,false);assert.equal(writes,1);
  assert.equal(state.poll,null);assert.equal(timers.size,0);assert.equal(signal.aborted,true);
  await assert.rejects(api('/api/baselines'),error=>error.status===401);
  connectionError('Trying again');connectionError();
  schedulePoll(pollDetail,state.routeVersion);listeners.visibilitychange();
  assert.equal(requests,1);assert.equal(timerCount,1);assert.equal(state.poll,null);
  assert.equal(banner.textContent,message);assert.equal(banner.hidden,false);assert.equal(writes,1);
  let reloads=0;location.reload=()=>reloads++;
  await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'reconnect'}}:null}});
  assert.equal(reloads,0);
})()
`);
""")


def test_concurrent_bootstrap_401s_show_one_banner_without_reconnect():
    run_browser_script(r"""
const page=browser();
await page.run(`
(async()=>{
  const banner=nodes['#connection-error'];let text='',writes=0;
  Object.defineProperty(banner,'textContent',{get:()=>text,set:value=>{text=value;writes++;}});
  let requests=0;
  fetch=async()=>{requests++;return {ok:false,status:401,json:async()=>{throw Error('No JSON body');}};};
  await init();
  assert.equal(requests,2);assert.equal(writes,1);assert.equal(banner.hidden,false);
  assert.equal(banner.textContent,'Giraffe was restarted. Open the new link printed by giraffe ui in your terminal.');
  assert(!nodes['#main'].innerHTML.includes('data-action="reconnect"'));
  assert(!nodes['#main'].innerHTML.includes('Local runner unavailable'));
  assert.equal(state.poll,null);
})()
`);
""")
