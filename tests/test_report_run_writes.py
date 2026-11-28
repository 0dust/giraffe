"""Deferred report reads and writes share per-run ordering across visits."""

import json

import pytest

from tests.test_ux_results import REPORT_REPLY_SETUP, run_render_checks


@pytest.mark.parametrize('kept', [False, True])
def test_late_keep_reply_repaints_visible_history_and_idle_poll_preserves_focus(kept):
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 window.matchMedia=()=>({matches:true});
 const counter=document.createElement('span');counter.setAttribute('id','run-count');document.body.insertBefore(counter,null);
 const post=deferred();let serverKept=KEPT;
 state.detail.kept=serverKept;
 const row=()=>({...initial,kept:serverKept,targets:[{model:'test-model'}]});
 api=(path,options)=>options?post.promise:path==='/api/runs'?{runs:[row()]}:navigationReply(path);
 const saving=toggleKept();await navigate('R');
 assert.equal(Boolean($('.kept-marker')),KEPT);
 assert($('#run-search'),$('#main').textContent);
 const search=$('#run-search');search.value='test';state.historySearch='test';
 search.selectionStart=1;search.selectionEnd=3;
 Object.getPrototypeOf(search).setSelectionRange=function(start,end){this.selectionStart=start;this.selectionEnd=end;};
 search.focus({preventScroll:true});
 let scrolls=0;window.scrollTo=()=>scrolls++;
 serverKept=!KEPT;post.resolve({kept:serverKept});await saving;
 assert.equal(Boolean($('.kept-marker')),!KEPT,'reply must update visible badge immediately');
 assert.equal(document.activeElement,$('#run-search'));assert.equal(document.activeElement.getAttribute('value'),'test');
 assert.equal(document.activeElement.selectionStart,1);assert.equal(document.activeElement.selectionEnd,3);
 assert.equal(scrolls,0);
 const focused=document.activeElement,writes=$('#main').writes;
 await pollRuns(state.routeVersion);
 assert.equal($('#main').writes,writes,'identical idle poll must not repaint');
 assert.strictEqual(document.activeElement,focused);
})()
""".replace('KEPT', json.dumps(kept)), dom=True)


@pytest.mark.parametrize('change', ['keep', 'status', 'empty'])
def test_history_poll_compares_with_last_paint_after_out_of_band_cache_change(change):
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 window.matchMedia=()=>({matches:true});
 const counter=document.createElement('span');counter.setAttribute('id','run-count');document.body.insertBefore(counter,null);
 state.runs=[{...initial,kept:false}];state.storage={};location.hash='#runs';renderRuns();
 const writes=$('#main').writes;
 if(CHANGE==='keep')state.runs[0].kept=true;
 else if(CHANGE==='status')state.runs[0].state='error';
 else state.runs=[];
 api=path=>path==='/api/runs'?{runs:structuredClone(state.runs)}:navigationReply(path);
 await pollRuns(state.routeVersion);
 assert($('#main').writes>writes,'cache already matches server but differs from last paint');
 if(CHANGE==='keep')assert($('.kept-marker'));
 else if(CHANGE==='status')assert($('.history-row.error'));
 else assert.match($('#main').textContent,/No runs yet/);
 const painted=$('#main').writes;await pollRuns(state.routeVersion);
 assert.equal($('#main').writes,painted,'including the empty view, idle polls must not repaint');
})()
""".replace('CHANGE', json.dumps(change)), dom=True)


@pytest.mark.parametrize('newer_first', [False, True])
def test_chooser_uses_own_list_and_concurrent_save_with_newer_refresh_pending(newer_first):
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 window.matchMedia=()=>({matches:true});
 const post=deferred(),chooser=deferred(),newer=deferred();let lists=0;
 const existing={id:'existing',run_id:'pre-existing-run',name:'Existing',saved_at:'2026-10-15T09:00:00Z'};
 const saved={id:'saved',run_id:initial.id,name:'Saved on A',saved_at:'2026-10-15T10:00:00Z'};
 const self={id:'self',run_id:'other',name:'B itself'};
 api=(path,options)=>path==='/api/baselines'?(options?post.promise:++lists===1?chooser.promise:newer.promise):navigationReply(path);
 openBaselineDialog();const form=$('#baseline-dialog').querySelector('form');form.values={baseline_name:'Saved on A'};
 const saving=saveBaseline({preventDefault(){},target:form});
 await navigate('O');const opening=openComparisonDialog();assert.equal(lists,1);
 post.resolve(saved);
 for(let i=0;i<12&&lists<2;i++)await Promise.resolve();assert.equal(lists,2);
 if(NEWER_FIRST){newer.resolve({baselines:[existing,saved,self]});await saving;}
 chooser.resolve({baselines:[existing,self]});await opening;
 const options=()=>$('#comparison-dialog').querySelectorAll('option');
 assert.deepEqual(Array.from(options(),item=>item.getAttribute('value')).sort(),['existing','saved']);
 if(!NEWER_FIRST){
  assert.deepEqual(state.baselines.map(item=>item.id),['saved'],'older chooser must not overwrite cache');
  newer.resolve({baselines:[existing,saved,self]});await saving;
 }
 assert.deepEqual(state.baselines.map(item=>item.id).sort(),['existing','saved','self']);
 assert.deepEqual(Array.from(options(),item=>item.getAttribute('value')).sort(),['existing','saved']);
 assert.equal(messages.length,0);
})()
""".replace('NEWER_FIRST', json.dumps(newer_first)), dom=True)


@pytest.mark.parametrize('newer_first', [False, True])
def test_baseline_replacement_during_chooser_load_uses_newest_name_and_time(newer_first):
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 window.matchMedia=()=>({matches:true});
 const post=deferred(),chooser=deferred(),newer=deferred();let lists=0;
 const old={id:'reference',run_id:'old-run',name:'Old cached name',saved_at:'2025-01-01T00:00:00Z'};
 const saved={...old,run_id:initial.id,name:'Replaced reference',saved_at:'2026-11-28T12:00:00Z'};
 const existing={id:'existing',run_id:'pre-existing-run',name:'Another eligible reference'};
 state.baselines=[old];
 api=(path,options)=>{
  if(path!=='/api/baselines')return navigationReply(path);
  if(options){assert.equal(JSON.parse(options.body).replace,true);return post.promise;}
  return ++lists===1?chooser.promise:newer.promise;
 };
 openBaselineDialog();const form=$('#baseline-dialog').querySelector('form');form.values={baseline_name:saved.name,replace:true};
 const saving=saveBaseline({preventDefault(){},target:form});await navigate('O');
 const opening=openComparisonDialog();post.resolve(saved);
 for(let i=0;i<12&&lists<2;i++)await Promise.resolve();assert.equal(lists,2);
 if(NEWER_FIRST){newer.resolve({baselines:[saved,existing]});await saving;}
 chooser.resolve({baselines:[old,existing]});await opening;
 const options=$('#comparison-dialog').querySelectorAll('option');assert.equal(options.length,2);
 const replacement=Array.from(options).find(item=>item.getAttribute('value')===saved.id);
 assert.equal(replacement.textContent,`${saved.name} · ${date(saved.saved_at)}`);
 assert.equal(state.baselines[0].name,saved.name);
 if(!NEWER_FIRST){newer.resolve({baselines:[saved,existing]});await saving;}
 assert.equal(state.baselines[0].saved_at,saved.saved_at);
})()
""".replace('NEWER_FIRST', json.dumps(newer_first)), dom=True)


def test_older_chooser_does_not_resurrect_baseline_missing_from_newer_completed_list():
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 window.matchMedia=()=>({matches:true});
 const old=deferred(),fresh=deferred();let reads=0;
 const deleted={id:'deleted',run_id:'other',name:'Removed reference'};
 api=()=>++reads===1?old.promise:fresh.promise;
 const opening=openComparisonDialog();
 mergeBaseline(deleted);
 const refreshing=refreshBaselines();fresh.resolve({baselines:[]});await refreshing;
 old.resolve({baselines:[deleted]});await opening;
 assert.equal($('#comparison-dialog').querySelectorAll('option').length,0);
 assert.equal(state.baselines.length,0);
})()
""", dom=True)


@pytest.mark.parametrize('removed', [False, True])
def test_chooser_reconciles_newer_completed_list_during_await_handoff(removed):
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 const old=deferred(),fresh=deferred();let reads=0;
 const before={id:'reference',run_id:'other',name:'Old name',saved_at:'2025-01-01T00:00:00Z'};
 const after={...before,name:'Newest name',saved_at:'2026-11-28T12:00:00Z'};
 api=()=>++reads===1?old.promise:fresh.promise;
 const opening=openComparisonDialog(),refreshing=refreshBaselines();
 // The old refresh finishes first; the newer cache lands before the chooser resumes.
 old.resolve({baselines:[before]});fresh.resolve({baselines:REMOVED?[]:[after]});
 await opening;await refreshing;
 const options=$('#comparison-dialog').querySelectorAll('option');assert.equal(options.length,REMOVED?0:1);
 if(!REMOVED)assert.equal(options[0].textContent,`${after.name} · ${date(after.saved_at)}`);
 assert.equal(state.baselines.length,REMOVED?0:1);
})()
""".replace('REMOVED', json.dumps(removed)), dom=True)


@pytest.mark.parametrize('first', ['A', 'B'])
def test_keep_pending_belongs_to_each_run(first):
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 const a=deferred(),b=deferred();state.runs=[{...initial,id:'other',kept:true}];
 api=async(path,options)=>options?(path.includes('/other/')?b.promise:a.promise):navigationReply(path);
 const savingA=toggleKept();await navigate('O');const savingB=toggleKept();
 location.hash='#runs/saved-run';await route();
 assert($('[data-action="keep-run"]').hasAttribute('disabled'),'A must remain pending');
 if(FIRST==='A'){a.resolve({kept:true});await savingA;b.resolve({kept:false});await savingB;}
 else {b.resolve({kept:false});await savingB;a.resolve({kept:true});await savingA;}
 assert.equal(state.detail.kept,true);
 assert.equal(keepPending(initial.id),false);assert.equal(keepPending('other'),false);
 assert(!$('[data-action="keep-run"]').hasAttribute('disabled'));
 assert.match(keepButton(),/aria-pressed="true"/);
 assert.equal(messages.length,0);
 assert.equal(state.runs.find(run=>run.id==='other').kept,false);
 assert.equal(runWrites.has('other'),false);
 await navigate('O');assert.equal(Boolean(state.detail.kept),false);
 assert.match(keepButton(),/aria-pressed="false"/);
})()
""".replace('FIRST', json.dumps(first)), dom=True)


def test_latest_issued_keep_ignores_older_reply():
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 const old=deferred(),latest=deferred();let writes=0;
 api=()=>++writes===1?old.promise:latest.promise;
 const first=toggleKept();
 // Simulate a path accidentally re-enabling a pending run, as in the original bug.
 if(typeof runWrites!=='undefined')runWrites.get(initial.id).pending.clear();
 state.keepPending=null;state.detail.kept=true;
 const second=toggleKept();assert.equal(writes,2);
 latest.resolve({kept:false});await second;
 old.resolve({kept:true});await first;
 assert.equal(state.detail.kept,false,'late Keep must not replace newer Unkeep');
 assert.match(keepButton(),/aria-pressed="false"/);
})()
""", dom=True)


@pytest.mark.parametrize('post_first', [False, True])
def test_keep_confirmation_orders_return_get_and_cleans_ledger(post_first):
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 const post=deferred(),get=deferred();let returning=false;
 api=async(path,options)=>options?post.promise:returning&&path==='/api/runs/saved-run'?get.promise:navigationReply(path);
 const saving=toggleKept();await navigate('R');
 if(POST_FIRST){post.resolve({kept:true});await saving;}
 returning=true;location.hash='#runs/saved-run';const entering=route();
 if(!POST_FIRST){post.resolve({kept:true});await saving;}
 get.resolve({...initial,kept:false});await entering;
 assert.equal(state.detail.kept,!POST_FIRST);
 assert.equal(messages.length,0);
 if(!POST_FIRST){
  api=async()=>({...initial,kept:false});await fetchRunDetail(initial.id);
 }
 assert.equal(runWrites.has(initial.id),false,'post-confirmation GET retires the ledger');
})()
""".replace('POST_FIRST', json.dumps(post_first)), dom=True)


@pytest.mark.parametrize('route_first', [False, True])
def test_comparison_survives_old_return_get(route_first):
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 const post=deferred(),old=deferred();let returning=false,reads=0;
 const compared={...initial,baseline_id:'reference',report:{...initial.report,baseline:{run_id:'reference-run',name:'Reference',status:'pass',targets:[]}}};
 api=async(path,options)=>{
  if(path==='/api/baselines')return {baselines:[{id:'reference',name:'Reference'}]};
  if(options)return post.promise;
  if(returning&&path==='/api/runs/saved-run')return ++reads===1?old.promise:structuredClone(compared);
  return navigationReply(path);
 };
 await openComparisonDialog();const dialog=$('#comparison-dialog');
 const saving=compareSavedRun({preventDefault(){},target:dialog.querySelector('form')});
 await navigate('R');returning=true;location.hash='#runs/saved-run';const entering=route();
 if(ROUTE_FIRST){old.resolve(structuredClone(initial));await entering;}
 post.resolve({});await saving;
 if(!ROUTE_FIRST){old.resolve(structuredClone(initial));await entering;}
 assert.equal(state.detail.baseline_id,'reference','confirmed comparison must survive return load');
 assert($('#report-baseline-reference'),'baseline section must appear');
 assert.equal($('[data-action="compare-saved"]').textContent,'Change baseline');
 assert.doesNotMatch($('#main').textContent,/Compare with baseline/);
 assert.equal(messages.length,0);assert.equal(dialog.open,false);
 assert.equal(runWrites.has(initial.id),false);
})()
""".replace('ROUTE_FIRST', json.dumps(route_first)), dom=True)


@pytest.mark.parametrize('path', ['reset', 'complete', 'status'])
def test_poll_detail_get_overlays_keep_confirmed_after_read_started(path):
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 const get=deferred(),post=deferred();let reading=false;
 state.active=initial.id;state.statusRun={...initial};
 // Keep's repaint must not retire the active run while status awaits its reset.
 if(PATH==='status'){state.detail.state='running';state.statusRun.state='running';}
 api=async(url,options)=>{
  if(url.endsWith('/keep'))return post.promise;
  if(url.includes('/events?'))return {seq:1,state:'completed',reset:PATH!=='complete',events:[]};
  if(url==='/api/runs/saved-run'){reading=true;return get.promise;}
  return navigationReply(url);
 };
 const polling=PATH==='status'?pollRunStatus(state.routeVersion,{schedule:false}):pollDetail(state.routeVersion);
 for(let i=0;i<8&&!reading;i++)await Promise.resolve();assert(reading);
 const saving=toggleKept();post.resolve({kept:true});await saving;
 get.resolve({...initial,kept:false});await polling;
 assert.equal((PATH==='status'?state.statusRun:state.detail).kept,true);
})()
""".replace('PATH', json.dumps(path)), dom=True)


@pytest.mark.parametrize('server_kept', [False, True])
def test_old_history_read_cannot_erase_keep_after_detail_reconciliation(server_kept):
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 const counter=document.createElement('span');counter.setAttribute('id','run-count');document.body.insertBefore(counter,null);
 const list=deferred(),post=deferred();state.runs=[{...initial,kept:false}];
 api=async(url,options)=>url==='/api/runs'?list.promise:url.endsWith('/keep')?post.promise:url==='/api/runs/saved-run'?{...initial,kept:SERVER_KEPT}:navigationReply(url);
 const loading=refreshRuns(),saving=toggleKept();post.resolve({kept:true});await saving;
 assert.equal(state.runs[0].kept,true);
 await fetchRunDetail(initial.id);
 assert(runWrites.has(initial.id),'older history read still needs confirmation');
 list.resolve({runs:[{...initial,kept:false}]});await loading;
 assert.equal(state.runs[0].kept,SERVER_KEPT);renderRuns();
 assert.equal(Boolean($('.kept-marker')),SERVER_KEPT);
 assert.equal(runWrites.has(initial.id),false);
})()
""".replace('SERVER_KEPT', json.dumps(server_kept)), dom=True)


def test_comparison_refetches_if_stale_route_lands_before_fresh_detail():
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 const old=deferred(),post=deferred(),fresh=deferred();let returning=false,reads=0;
 const compared={...initial,baseline_id:'reference',report:{...initial.report,baseline:{run_id:'baseline-run',status:'pass',targets:[]}}};
 api=async(url,options)=>{
  if(url==='/api/baselines')return {baselines:[{id:'reference',name:'Reference'}]};
  if(options)return post.promise;
  if(returning&&url==='/api/runs/saved-run')return ++reads===1?old.promise:reads===2?fresh.promise:structuredClone(compared);
  return navigationReply(url);
 };
 await openComparisonDialog();const saving=compareSavedRun({preventDefault(){},target:$('#comparison-dialog').querySelector('form')});
 await navigate('R');returning=true;location.hash='#runs/saved-run';const entering=route();
 post.resolve({});for(let i=0;i<8&&reads<2;i++)await Promise.resolve();assert.equal(reads,2);
 old.resolve(structuredClone(initial));await entering;
 assert.equal(reads,3,'old route reply needs a post-comparison GET');
 assert.equal(state.detail.baseline_id,'reference');
 fresh.resolve({...initial,baseline_id:'older-fresh-read'});await saving;
 assert.equal(state.detail.baseline_id,'reference','newest fetch start wins');
 assert.equal(runWrites.has(initial.id),false);
 assert.equal(messages.length,0);
})()
""", dom=True)


def test_failed_writes_and_failed_reads_release_pending_without_losing_confirmation():
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 api=async()=>{throw Error('Keep failed');};await toggleKept();
 assert.equal(keepPending(initial.id),false);assert.equal(runWrites.has(initial.id),false);
 assert.deepEqual(messages,['Keep failed']);messages.length=0;
 const older=deferred();api=()=>older.promise;const reading=fetchRunDetail(initial.id);
 api=async()=>({kept:true});await toggleKept();
 api=async()=>{throw Error('GET failed');};await assert.rejects(fetchRunDetail(initial.id),/GET failed/);
 assert.equal(runWrites.get(initial.id).keep.value,true);
 api=async()=>({...initial,kept:false});await fetchRunDetail(initial.id);
 older.resolve({...initial,kept:true});const late=await reading;assert.equal(late.kept,false);
 assert.equal(runWrites.has(initial.id),false);
})()
""", dom=True)


def test_same_visit_comparison_closes_and_focuses_own_dialog_and_preserves_keep():
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 const get=deferred(),keep=deferred();let reading=false;
 api=async(url,options)=>{
  if(url==='/api/baselines')return {baselines:[{id:'reference',name:'Reference'}]};
  if(url.endsWith('/compare'))return {};
  if(url.endsWith('/keep'))return keep.promise;
  reading=true;return get.promise;
 };
 await openComparisonDialog();const dialog=$('#comparison-dialog');
 const saving=compareSavedRun({preventDefault(){},target:dialog.querySelector('form')});
 for(let i=0;i<8&&!reading;i++)await Promise.resolve();assert(reading);
 const keeping=toggleKept();keep.resolve({kept:true});await keeping;
 get.resolve({...initial,kept:false,baseline_id:'reference',report:{...initial.report,baseline:{run_id:'baseline-run',status:'pass',targets:[]}}});await saving;
 assert.equal(state.detail.kept,true,'compare refetch must preserve concurrent confirmed Keep');
 assert.equal(dialog.open,false);
 const section=$('#report-baseline-reference');assert(section.open);
 assert.equal(document.activeElement,section.querySelector('summary'));
 assert.equal(messages.length,1);assert.match(messages[0],/Comparison saved/);
 assert.equal(pendingComparisons.has(initial.id),false);assert.equal(keepPending(initial.id),false);
})()
""", dom=True)


def test_older_detail_reader_retains_ledger_and_uses_newer_server_truth():
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 const old=deferred();let reads=0;
 api=async(url,options)=>options?{kept:true}:++reads===1?old.promise:{...initial,kept:false};
 const abandoned=fetchRunDetail(initial.id);await toggleKept();
 const fresh=await fetchRunDetail(initial.id);assert.equal(fresh.kept,false);
 assert(runWrites.has(initial.id),'older detail reader still needs the ledger');
 old.resolve({...initial,kept:true});const late=await abandoned;
 assert.equal(late.kept,false);assert.equal(runWrites.has(initial.id),false);
})()
""", dom=True)


def test_ordinary_keep_retires_without_a_later_read():
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 let reads=0;
 api=async(url,options)=>{if(options)return {kept:true};reads++;throw Error('Unexpected GET');};
 await toggleKept();assert.equal(state.detail.kept,true);
 assert.equal(reads,0);assert.equal(runWrites.has(initial.id),false);
})()
""", dom=True)


def test_write_started_during_read_does_not_prevent_retirement():
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 const get=deferred(),post=deferred();let writes=0;
 api=async(url,options)=>options?(++writes===1?{kept:true}:post.promise):get.promise;
 await toggleKept();const reading=fetchRunDetail(initial.id);
 const saving=toggleKept();get.resolve({...initial,kept:true});await reading;
 assert(keepPending(initial.id),'pending write must survive the overlapping read');
 post.resolve({kept:false});await saving;
 assert.equal(state.detail.kept,false);assert.equal(runWrites.has(initial.id),false);
})()
""", dom=True)


@pytest.mark.parametrize('destination', ['R', 'O'])
def test_hidden_comparison_without_older_reader_skips_detail_get(destination):
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 const post=deferred();let reads=0;
 api=async(url,options)=>{
  if(url==='/api/baselines')return {baselines:[{id:'reference',name:'Reference'}]};
  if(options)return post.promise;
  if(url==='/api/runs/saved-run'){reads++;throw Error('Unnecessary comparison GET');}
  return navigationReply(url);
 };
 await openComparisonDialog();
 const saving=compareSavedRun({preventDefault(){},target:$('#comparison-dialog').querySelector('form')});
 await navigate(DESTINATION);const before=$('#main').innerHTML;
 post.resolve({});await saving;
 assert.equal(reads,0);assert.equal(runWrites.has(initial.id),false);
 assert.equal(pendingComparisons.has(initial.id),false);assert.equal(messages.length,0);
 assert.equal($('#main').innerHTML,before);
})()
""".replace('DESTINATION', json.dumps(destination)), dom=True)


def test_overlapping_detail_reads_are_deeply_isolated_from_callers_and_cache():
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 const first=deferred(),second=deferred(),third=deferred();let reads=0;
 api=()=>[first,second,third][reads++].promise;
 const oldest=fetchRunDetail(initial.id),middle=fetchRunDetail(initial.id),newest=fetchRunDetail(initial.id);
 third.resolve({...initial,state:'running'});const latest=await newest;state.detail=latest;
 assert.notStrictEqual(latest,runWrites.get(initial.id).detail,'caller must not own the cache object');
 mergeRunEvents(latest,{seq:10,state:'stopping',checks:[check('serving','fail')],events:[]});
 second.resolve({...initial,state:'completed'});const other=await middle;state.statusRun=other;
 first.resolve({...initial,state:'completed'});const old=await oldest;
 assert.notStrictEqual(other,latest);assert.notStrictEqual(old,other);assert.notStrictEqual(old,latest);
 assert.notStrictEqual(other.report,latest.report);assert.notStrictEqual(old.report,other.report);
 assert.equal(other.state,'running');assert.equal(old.state,'running');
 assert.equal(other.report.checks[0].status,'pass');assert.equal(old.report.checks[0].status,'pass');
 mergeRunEvents(other,{seq:11,state:'stopping',checks:[check('serving','fail')],events:[]});
 other.report.manifest.config.targets[0].model='changed';
 assert.equal(old.report.checks[0].status,'pass');
 assert.equal(old.report.manifest.config.targets[0].model,'test-model');
 assert.equal(runWrites.has(initial.id),false);
})()
""", dom=True)


@pytest.mark.parametrize('consumer', ['route', 'reset', 'complete', 'status', 'compare'])
def test_copied_detail_reconciles_keep_confirmed_during_await_handoff(consumer):
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 const get=deferred(),post=deferred();let reading=false;
 // Return promises directly so GET copying precedes the Keep continuation.
 api=(url,options)=>{
  if(url.endsWith('/keep'))return post.promise;
  if(url.endsWith('/compare'))return {};
  if(url==='/api/baselines')return {baselines:[{id:'reference',name:'Reference'}]};
  if(url.includes('/events?'))return {seq:1,state:'completed',reset:CONSUMER!=='complete',events:[]};
  if(url==='/api/runs/saved-run'){reading=true;return get.promise;}
  return navigationReply(url);
 };
 const keeping=toggleKept();let loading;
 if(CONSUMER==='route'){
  await navigate('R');location.hash='#runs/saved-run';loading=route();
 }else if(CONSUMER==='compare'){
  await openComparisonDialog();loading=compareSavedRun({preventDefault(){},target:$('#comparison-dialog').querySelector('form')});
 }else{
  state.active=initial.id;state.statusRun={...initial};
  // Model a live run so Keep's repaint does not invalidate the status reader.
  if(CONSUMER==='status'){state.detail.state='running';state.statusRun.state='running';}
  loading=CONSUMER==='status'?pollRunStatus(state.routeVersion,{schedule:false}):pollDetail(state.routeVersion);
 }
 for(let i=0;i<8&&!reading;i++)await Promise.resolve();assert(reading);
 get.resolve({...initial,kept:false});post.resolve({kept:true});
 await keeping;await loading;
 assert.equal((CONSUMER==='status'?state.statusRun:state.detail).kept,true);
 assert.equal(runWrites.has(initial.id),false);
})()
""".replace('CONSUMER', json.dumps(consumer)), dom=True)


def test_copied_detail_uses_newest_fetch_and_releases_handoff_metadata():
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 const first=deferred(),second=deferred();let reads=0;
 api=()=>++reads===1?first.promise:second.promise;
 const older=fetchRunDetail(initial.id,{handoff:true}),newer=fetchRunDetail(initial.id,{handoff:true});
 first.resolve({...initial,baseline_id:'old'});second.resolve({...initial,baseline_id:'new'});
 const old=await older,latest=await newer;
 assert.equal(old.baseline_id,'old');assert.equal(latest.baseline_id,'new');
 assert(runDetailReads.has(old));const current=reconcileRunDetail(old);
 assert.equal(current.baseline_id,'new');assert.notStrictEqual(current,latest);
 assert.notStrictEqual(current.report,latest.report);
 assert.equal(runDetailReads.has(old),false);
 assert.equal(reconcileRunDetail(latest),latest);assert.equal(runDetailReads.has(latest),false);
})()
""", dom=True)


@pytest.mark.parametrize('read_first', [False, True])
def test_only_list_reads_started_before_confirmation_delay_retirement(read_first):
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 const counter=document.createElement('span');counter.setAttribute('id','run-count');document.body.insertBefore(counter,null);
 const list=deferred();api=url=>url==='/api/runs'?list.promise:navigationReply(url);
 const write=beginRunWrite(initial.id,'keep');let loading;
 if(READ_FIRST)loading=refreshRuns();
 confirmRunWrite(initial.id,write,true);
 if(!READ_FIRST)loading=refreshRuns();
 finishRunWrite(initial.id,write);
 assert.equal(runWrites.has(initial.id),READ_FIRST);
 list.resolve({runs:[{...initial,kept:false}]});await loading;
 assert.equal(state.runs[0].kept,READ_FIRST);assert.equal(runWrites.has(initial.id),false);
})()
""".replace('READ_FIRST', json.dumps(read_first)), dom=True)


def test_hidden_comparison_reconciles_return_read_during_await_handoff():
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 const get=deferred(),post=deferred();let reads=0;
 const compared={...initial,baseline_id:'reference',report:{...initial.report,baseline:{run_id:'baseline-run',status:'pass',targets:[]}}};
 api=(url,options)=>{
  if(url==='/api/baselines')return {baselines:[{id:'reference',name:'Reference'}]};
  if(options)return post.promise;
  if(url==='/api/runs/saved-run')return ++reads===1?get.promise:structuredClone(compared);
  return navigationReply(url);
 };
 await openComparisonDialog();const saving=compareSavedRun({preventDefault(){},target:$('#comparison-dialog').querySelector('form')});
 await navigate('R');location.hash='#runs/saved-run';const entering=route();
 get.resolve(structuredClone(initial));post.resolve({});await saving;await entering;
 assert.equal(reads,2);assert.equal(state.detail.baseline_id,'reference');
 assert.equal($('[data-action="compare-saved"]').textContent,'Change baseline');
 assert.equal(messages.length,0);assert.equal(runWrites.has(initial.id),false);
})()
""", dom=True)


def test_uncontended_detail_returns_api_identity_without_serialization():
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 const payload=structuredClone(initial);
 Object.defineProperty(payload,'toJSON',{value(){throw Error('uncontended payload must not be serialized');},configurable:true});
 api=()=>payload;
 const result=await fetchRunDetail(initial.id,{handoff:true});
 const entry=runDetailReads.get(result).entry;
 assert.strictEqual(result,payload);assert.strictEqual(reconcileRunDetail(result),payload);
 assert.equal(entry.detail,null);assert.equal(runWrites.has(initial.id),false);
})()
""", dom=True)


def test_contended_detail_keeps_private_snapshot_and_returns_distinct_reuse():
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 const first=deferred(),second=deferred();let reads=0;
 api=()=>++reads===1?first.promise:second.promise;
 const older=fetchRunDetail(initial.id,{handoff:true}),newer=fetchRunDetail(initial.id,{handoff:true});
 const payload=structuredClone(initial);second.resolve(payload);
 const latest=await newer,entry=runWrites.get(initial.id);
 assert.strictEqual(latest,payload,'fetching consumer owns the original payload');
 assert(entry.detail,'older read needs a private snapshot');
 const snapshot=entry.detail;state.detail=latest;
 const keeping=beginRunWrite(initial.id,'keep');confirmRunWrite(initial.id,keeping,true);finishRunWrite(initial.id,keeping);
 assert.equal(entry.detail,snapshot,'Keep overlays must not mutate the private snapshot');
 mergeRunEvents(latest,{seq:3,state:'stopping',checks:[check('serving','fail')],events:[]});
 reconcileRunDetail(latest);
 first.resolve({...initial,state:'error'});const reused=await older;
 assert.notStrictEqual(reused,payload);assert.notStrictEqual(reused.report,payload.report);
 assert.equal(reused.report.checks[0].status,'pass');assert.equal(reused.state,'completed');assert.equal(reused.kept,true);
 assert.equal(entry.detail,null,'snapshot released once no older reader needs it');
 reconcileRunDetail(reused);assert.equal(entry.detail,null);assert.equal(runWrites.has(initial.id),false);
 assert.equal(entry.reads.size,0);
})()
""", dom=True, strict=True)


@pytest.mark.parametrize('blocker', ['write', 'list', 'newer-read'])
def test_detail_snapshot_is_released_while_unrelated_work_retains_ledger(blocker):
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 const first=deferred(),second=deferred(),third=deferred(),list=deferred();let reads=0;
 const counter=document.createElement('span');counter.setAttribute('id','run-count');document.body.insertBefore(counter,null);
 api=url=>url==='/api/runs'?list.promise:url==='/api/storage'?{}:[first,second,third][reads++].promise;
 const write=BLOCKER==='write'?beginRunWrite(initial.id,'keep'):null;
 const loading=BLOCKER==='list'?refreshRuns():null;
 if(loading){const marker=beginRunWrite(initial.id,'keep');confirmRunWrite(initial.id,marker,true);finishRunWrite(initial.id,marker);}
 const older=fetchRunDetail(initial.id,{handoff:true}),newer=fetchRunDetail(initial.id,{handoff:true});
 second.resolve(structuredClone(initial));const latest=await newer,entry=runWrites.get(initial.id);
 assert(entry.detail);reconcileRunDetail(latest);
 const background=BLOCKER==='newer-read'?fetchRunDetail(initial.id,{handoff:true}):null;
 first.resolve(structuredClone(initial));const old=await older;reconcileRunDetail(old);
 assert.equal(entry.detail,null,'full report must not be pinned by unrelated work');
 assert(runWrites.has(initial.id));
 if(write)finishRunWrite(initial.id,write);
 if(loading){list.resolve({runs:[]});await loading;}
 if(background){third.resolve(structuredClone(initial));reconcileRunDetail(await background);}
 assert.equal(entry.detail,null);assert.equal(runWrites.has(initial.id),false);
})()
""".replace('BLOCKER', json.dumps(blocker)), dom=True)


def test_retirement_clears_snapshot_from_retained_handoff_metadata():
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 const first=deferred(),second=deferred();let reads=0;
 api=()=>++reads===1?first.promise:second.promise;
 const older=fetchRunDetail(initial.id),newer=fetchRunDetail(initial.id);
 second.resolve(structuredClone(initial));const latest=await newer;
 const entry=runDetailReads.get(latest).entry;assert(entry.detail);
 first.resolve(structuredClone(initial));const old=await older;
 assert.equal(runWrites.has(initial.id),false);
 assert.equal(entry.detail,null,'WeakMap metadata must not pin a retired report');
 assert.equal(entry.reads.size,0,'finished requests must release reader tokens');
 assert.notStrictEqual(latest,old);
})()
""", dom=True)


def test_newer_inflight_reader_does_not_force_an_older_payload_snapshot():
    run_render_checks(REPORT_REPLY_SETUP + r"""
(async()=>{
 const first=deferred(),second=deferred();let reads=0;
 api=()=>++reads===1?first.promise:second.promise;
 const older=fetchRunDetail(initial.id,{handoff:true}),newer=fetchRunDetail(initial.id,{handoff:true});
 const payload=structuredClone(initial);
 Object.defineProperty(payload,'toJSON',{value(){throw Error('newer reader does not need this older payload');}});
 first.resolve(payload);const old=await older;const entry=runDetailReads.get(old).entry;
 assert.strictEqual(old,payload);assert.equal(entry.detail,null);reconcileRunDetail(old);
 const next=structuredClone(initial);second.resolve(next);const latest=await newer;
 assert.strictEqual(latest,next);assert.equal(entry.detail,null);reconcileRunDetail(latest);
 assert.equal(entry.detail,null);assert.equal(runWrites.has(initial.id),false);
})()
""", dom=True)
