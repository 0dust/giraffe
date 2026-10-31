import {positiveBoundErrors, inferSetupPreset, configureLoadTest, loadTestIssues, loadTestSettings, loadFieldName, convertExpectedAnswer, datasetCaseFromValues, changeDatasetScorer, configureJudging, configureSessions, configureProfiles, parseLogprobSettings, parseToolFixtures, effectiveProfileDefaults, configureTests, configureRunLimits, limitValue, optionDefault, optionValue, sameConfig} from './form-config.mjs?v=20261028-results-merge-ci';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clone = (value) => structuredClone(value);
// Credentials live only in this tab, outside config, exports and browser storage.
let sessionAPIKey = null;
function apiKeyFor(url) {
  return sessionAPIKey && sessionAPIKey.url === url?.trim() ? sessionAPIKey.key : '';
}
function sessionAPIKeys(config) {
  const target=config.targets?.[0],key=apiKeyFor(target?.url);
  return key ? [{target:target.name,url:target.url,api_key:key}] : [];
}
function clearAPIKey() {
  sessionAPIKey=null;
  const input=$('#new-run-form')?.elements.api_key;
  if(input)input.value='';
}
function updateAPIKeySummary() {
  const form=$('#new-run-form'),summary=$('#connection-auth > summary');
  if(!form||!summary)return;
  const key=apiKeyFor(form.elements.url.value),ref=form.elements.api_key_env?.value.trim();
  $('.row-value',summary).textContent=key?'Set for this tab':ref||'Not set';
}
const state = {bootstrap:null, runs:[], storage:null, storageUnavailable:false, cleanup:null, cleanupResult:null, baselines:[], detail:null, mosaicFollow:true, config:null, active:null, diskActive:null, routeVersion:0, poll:null, tab:'checks', selectedCheck:null, checkNavigatorOpen:false, filter:'all', historySearch:'', comparisonFilter:null, comparisonCheck:'all', submitting:false, openTest:null, configView:'form', jsonDraft:null, jsonBase:null, configRevision:0, configBusy:false, showAvailableTests:false, formMode:'normal', formRestartTarget:null, requestPreview:null, previewSequence:0, planTimer:null, setupPreset:null, catalogueFilter:'all', catalogueCategory:'all', catalogueSearch:'', setupStep:1, returnToReview:false, modelDiscovery:null, modelDiscoverySequence:0, autoModel:null, datasetSelection:[], datasetImportSequence:0, judgeEditorOpen:null, datasetImport:null, datasetEditor:null, datasetTry:null, migrationNotes:[], dismissedMigrationNotes:[], loadPanels:{}, limitValues:{}, jsonValidation:null};
const statusLabels = {pass:'Passed',fail:'Failed',failed:'Failed',timeout:'Timed out',inconclusive:'Inconclusive',skipped:'Not run',idle:'Unused',blocked:'Blocked',running:'Running',queued:'Queued',reserved:'Reserved',stopping:'Stopping',error:'Runner error',completed:'Finished',stopped:'Stopped',cancelled:'Cancelled',interrupted:'Interrupted'};
const statusIcons = {pass:'✓',fail:'×',failed:'×',timeout:'◷',inconclusive:'?',skipped:'–',idle:'○',blocked:'!',running:'·',queued:'○',reserved:'○',stopping:'·',error:'×',completed:'✓',stopped:'⊘',cancelled:'⊘',interrupted:'!'};
const order = {fail:0,blocked:1,inconclusive:2,pass:3,skipped:4};
const icon = (name) => ({plus:'<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 4v12M4 10h12"/></svg>',arrow:'<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h12m-5-5 5 5-5 5"/></svg>',repeat:'<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M15 6A6 6 0 1 0 16 12M15 2v5h-5"/></svg>',stop:'<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="5" y="5" width="10" height="10"/></svg>'}[name] || '');
const badge = (status, label, run=state.detail) => {const shown=displayStatus(status,run);return `<span class="status ${escape(shown)}" data-state="${escape(shown)}"><span class="status-icon" aria-hidden="true">${statusIcons[shown] || '·'}</span>${escape(shown===status&&label || statusLabels[shown] || shown)}</span>`;};
const number = (value, digits=0) => value == null ? '—' : Number(value).toLocaleString(undefined,{maximumFractionDigits:digits});
const counted = (value, label) => `${number(value)} ${label}${Number(value)===1?'':'s'}`;
const diskSize = (value) => {const size=Number(value || 0),units=['B','KiB','MiB','GiB','TiB'],i=size>0?Math.min(4,Math.floor(Math.log(size)/Math.log(1024))):0;return `${number(size/1024**i,i?1:0)} ${units[i]}`;};
const ms = (value) => value == null ? '—' : `${number(value,1)} ms`;
const date = (value) => value ? new Date(value).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '—';
const elapsed = (seconds) => seconds == null ? '—' : seconds < 60 ? `${number(seconds,1)}s` : `${Math.floor(seconds/60)}m ${Math.floor(seconds%60)}s`;
const duration = (run) => run.started_at && run.finished_at ? elapsed((new Date(run.finished_at)-new Date(run.started_at))/1000) : '—';
const human = (name) => String(name).replaceAll('_',' ').replace(/\b\w/, c=>c.toUpperCase());
const activeState = (run) => run && ['running','queued','stopping'].includes(run.state);
function displayStatus(status,run=state.detail) {
  if(run?.state&&!activeState(run)&&['running','queued','reserved','stopping'].includes(status))return status==='queued'?'skipped':['cancelled','interrupted'].includes(run.state)?'cancelled':'inconclusive';
  return status;
}
function requestStatus(record) {
  return ['running','queued','reserved','stopping','timeout','cancelled','skipped'].includes(record.status)?displayStatus(record.status):requestOutcome(record);
}
const runPath = (id) => `/api/runs/${encodeURIComponent(id)}`;
const linkRun = (id) => `#runs/${encodeURIComponent(id)}`;
const notice = (message, kind='') => `<div class="notice ${kind}">${escape(message)}</div>`;

// Request glyphs describe request evidence, not the parent check's verdict.
function requestOutcome(record) {
  if(record.status==='running')return 'running';
  if(record.intentional||isProbe(record))return 'inconclusive';
  if(record.status==='cancelled')return 'inconclusive';
  if(['failed','timeout'].includes(record.status)||record.score===false||record.valid===false)return 'fail';
  if(record.status==='completed'&&record.valid===true&&record.score===true)return 'pass';
  return 'inconclusive';
}
const landedItems=new Set(),landingAnimations=new Set(),terminalRequests=new Map();
let landedRun=null;
const inProgress=status=>['running','queued','reserved','stopping'].includes(status);
function cancelLandings(root=null) {
  for(const animation of landingAnimations)if(!root||root.contains(animation.effect.target)){animation.cancel();landingAnimations.delete(animation);}
}
function syncLiveRun() {
  if(landedRun!==state.detail?.id){cancelLandings();landedRun=state.detail?.id;landedItems.clear();terminalRequests.clear();}
  if(!activeState(state.detail)||window.matchMedia?.('(prefers-reduced-motion: reduce)').matches)cancelLandings();
}
window.matchMedia?.('(prefers-reduced-motion: reduce)').addEventListener?.('change',event=>{if(event.matches)cancelLandings();});
function stableLiveRequests(records) {
  syncLiveRun();
  return records.map(record=>{
    if(record.slot==null)return record;
    const previous=terminalRequests.get(record.slot),reused=previous?.id&&record.id&&previous.id!==record.id;
    if(previous&&!reused&&inProgress(record.status)){
      console.warn('Ignoring regressed live request status',{run:state.detail.id,slot:record.slot,status:record.status});
      return {...record,...previous,id:record.id || previous.id};
    }
    if(reused)terminalRequests.delete(record.slot);
    if(!inProgress(record.status))terminalRequests.set(record.slot,{...record});
    return record;
  });
}
function landOnce(element,identity) {
  if(landedItems.has(identity))return;
  landedItems.add(identity);
  if(window.matchMedia?.('(prefers-reduced-motion: reduce)').matches||document.hidden||!element.isConnected||element.closest('details:not([open])')||!element.getClientRects().length||window.getComputedStyle?.(element).visibility==='hidden')return;
  const animation=element.animate([{opacity:0},{opacity:1}],{duration:400,easing:'cubic-bezier(.2,.8,.2,1)',fill:'none'});
  landingAnimations.add(animation);
  animation.finished.then(()=>{animation.cancel();landingAnimations.delete(animation);},()=>landingAnimations.delete(animation));
}
function liveNodeKey(node) {
  if(node.nodeType!==1)return '#text';
  const identity=['data-live-key','data-condition-key','data-load-point','data-load-result','data-status-slot'].map(name=>node.getAttribute(name)).find(value=>value!==null) ?? node.id;
  return `${node.tagName}:${identity || node.getAttribute('class')?.split(' ')[0] || ''}`;
}
function patchHTML(parent,html,{landing=false}={}) {
  syncLiveRun();
  if(!parent.firstChild){parent.innerHTML=html;return;}
  const template=document.createElement('template');template.innerHTML=html;
  const pendingLandings=[];
  const patch=(parent,next,path)=>{
    const available=new Map();
    for(const child of parent.childNodes){const key=liveNodeKey(child);if(!available.has(key))available.set(key,[]);available.get(key).push(child);}
    const pairs=[...next.childNodes].map(wanted=>({wanted,current:available.get(liveNodeKey(wanted))?.shift()}));
    // Remove obsolete siblings before comparing positions, so a deletion never moves its survivors.
    for(const children of available.values())for(const child of children)parent.removeChild(child);
    let cursor=parent.firstChild;
    for(const {wanted,current} of pairs){
      const identity=`${path}/${liveNodeKey(wanted)}`;
      if(!current){parent.insertBefore(wanted,cursor);continue;}
      if(current!==cursor){
        if(parent.moveBefore)parent.moveBefore(current,cursor);
        else {const focused=current.contains(document.activeElement)?document.activeElement:null;parent.insertBefore(current,cursor);focused?.focus({preventScroll:true});}
      }
      cursor=current.nextSibling;
      if(current.nodeType!==1){if(current.nodeValue!==wanted.nodeValue)current.nodeValue=wanted.nodeValue;continue;}
      if(current.isEqualNode(wanted))continue;
      if(current.getAttribute('data-request-id')!==wanted.getAttribute('data-request-id'))cancelLandings(current);
      const before=current.getAttribute('data-state'),after=wanted.getAttribute('data-state'),land=landing&&['running','queued','stopping'].includes(before)&&after&&!inProgress(after)&&after!=='idle';
      for(const attr of [...current.attributes]){
        if(current.tagName==='DETAILS'&&attr.name==='open'||attr.name==='tabindex'&&current.classList.contains('request-spot'))continue;
        if(!wanted.hasAttribute(attr.name))current.removeAttribute(attr.name);
      }
      for(const attr of wanted.attributes){
        if(current.tagName==='DETAILS'&&attr.name==='open'||attr.name==='tabindex'&&current.classList.contains('request-spot'))continue;
        if(current.getAttribute(attr.name)!==attr.value)current.setAttribute(attr.name,attr.value);
      }
      patch(current,wanted,identity);
      if(land)pendingLandings.push([current,`${identity}:${current.closest('[data-request-id]')?.getAttribute('data-request-id') || ''}`]);
    }
  };
  patch(parent,template.content,liveNodeKey(parent));
  for(const [element,identity] of pendingLandings)landOnce(element,identity);
}
function spotGlyph(points,status) {
  const vertices=points.split(' ').map(point=>point.split(',').map(Number)),x=vertices.reduce((sum,p)=>sum+p[0],0)/vertices.length,y=vertices.reduce((sum,p)=>sum+p[1],0)/vertices.length;
  return `<text class="spot-glyph" x="${x.toFixed(2)}" y="${y.toFixed(2)}" aria-hidden="true">${statusIcons[status] || '?'}</text>`;
}
const mosaicPageSize=240;
const mosaicGeometry=new Map();
// A giraffe coat is a Voronoi diagram: each site owns the plane nearest to it.
// Sites start on a staggered grid, are jittered, then relaxed once so spots are even.
function coatPolygons(sites,width,height,inset=0,reach=Infinity) {
  return sites.map(([x,y],index)=>{
    let polygon=[[inset,inset],[width-inset,inset],[width-inset,height-inset],[inset,height-inset]];
    for(let j=0;j<sites.length&&polygon.length;j++){
      if(j===index)continue;
      const [px,py]=sites[j],a=px-x,b=py-y;
      if(Math.abs(a)>reach||Math.abs(b)>reach)continue;
      const c=(px*px+py*py-x*x-y*y)/2-inset*Math.hypot(a,b),clipped=[];
      for(let k=0;k<polygon.length;k++){
        const p=polygon[k],q=polygon[(k+1)%polygon.length],dp=a*p[0]+b*p[1]-c,dq=a*q[0]+b*q[1]-c;
        if(dp<=0)clipped.push(p);
        if((dp<0&&dq>0)||(dp>0&&dq<0)){const t=dp/(dp-dq);clipped.push([p[0]+t*(q[0]-p[0]),p[1]+t*(q[1]-p[1])]);}
      }
      polygon=clipped;
    }
    return polygon;
  });
}
function coatGeometry(count,width,height,{seed=count,columns,inset=4.5,jitter=.55,relax=true,stagger=.25}={}) {
  let state32=(seed*2654435761)>>>0;
  const random=()=>{state32=(state32+0x6D2B79F5)>>>0;let t=state32;t=Math.imul(t^t>>>15,t|1);t^=t+Math.imul(t^t>>>7,t|61);return ((t^t>>>14)>>>0)/4294967296;};
  columns ||= Math.max(1,Math.round(Math.sqrt(count*width/height)));
  const rows=Math.ceil(count/columns),dx=width/columns,dy=height/rows;
  // Spread sites evenly across rows so a short final row never stretches into slabs.
  const perRow=Array.from({length:rows},(_,row)=>Math.floor(count/rows)+(row<count%rows?1:0));
  let sites=perRow.flatMap((n,row)=>Array.from({length:n},(_,column)=>{
    const step=width/n,shift=row%2?stagger:-stagger;
    return [Math.min(width-1,Math.max(1,(column+.5+shift+(random()-.5)*jitter)*step)),Math.min(height-1,Math.max(1,(row+.5+(random()-.5)*jitter)*dy))];
  }));
  // One Lloyd step: move each site to its cell centroid.
  const reach=4*Math.max(dx,dy);
  if(relax)sites=coatPolygons(sites,width,height,0,reach).map((polygon,i)=>{
    if(polygon.length<3)return sites[i];
    let area=0,cx=0,cy=0;
    for(let k=0;k<polygon.length;k++){const [x0,y0]=polygon[k],[x1,y1]=polygon[(k+1)%polygon.length],cross=x0*y1-x1*y0;area+=cross;cx+=(x0+x1)*cross;cy+=(y0+y1)*cross;}
    return area?[cx/(3*area),cy/(3*area)]:sites[i];
  });
  const outlines=coatPolygons(sites,width,height,0,reach),inner=coatPolygons(sites,width,height,inset,reach);
  return sites.map((site,i)=>({site,points:(inner[i].length>2?inner[i]:outlines[i]).map(([x,y])=>`${x.toFixed(2)},${y.toFixed(2)}`).join(' ')}));
}
// layout lets a partial last page reuse a full page's spot size and columns, so 2 leftover requests stay 2 normal spots.
function reticulateCells(count,layout=count) {
  const key=`${count}:${layout}`;
  if(mosaicGeometry.has(key))return mosaicGeometry.get(key);
  const basis=Math.max(count,layout);
  // Spots stay a readable size: a handful of requests never stretch into slabs.
  const size=Math.max(34,Math.min(84,Math.sqrt(960*300/Math.max(1,basis))));
  // Keep the coat roughly 3:1 so small runs still read as spots, not slabs.
  const basisRows=Math.ceil(basis/Math.max(1,Math.min(basis,Math.round(960/size),Math.ceil(Math.sqrt(basis*3))))),columns=Math.ceil(basis/basisRows);
  const rows=Math.max(1,Math.ceil(count/columns)),sites=basis>count?rows*columns:count;
  const width=Math.round(columns*size),height=Math.round(rows*size*.86);
  // Inset each cell by half the channel plus the corner radius; the stroke adds the radius back as a rounded edge.
  const round=Math.max(1.6,size*.075),channel=Math.max(2.4,size*.05);
  // Unused sites on a partial page only bound their neighbours' cells; they are never drawn.
  const cells=coatGeometry(sites,width,height,{columns,inset:channel/2+round}).slice(0,count).map(cell=>cell.points);
  const geometry={cells,width,height,columns,round:Number((round*2).toFixed(2))};
  if(mosaicGeometry.size>8)mosaicGeometry.clear();
  mosaicGeometry.set(key,geometry);return geometry;
}
function orderedRequests(report) {
  return [...report.requests].sort((a,b)=>{
    if(Number.isFinite(a.dispatch_ms)&&Number.isFinite(b.dispatch_ms))return a.dispatch_ms-b.dispatch_ms;
    return (Date.parse(a.started_at)||0)-(Date.parse(b.started_at)||0);
  });
}
function renderMosaic(records,{budget=records.length,live=false,budgetUsed=records.length,attempted=records.length,droppedOffers=0}={}) {
  const countOnly=live&&budget==null;
  const total=countOnly?records.reduce((total,record)=>Math.max(total,record.slot+1),0):Math.max(records.length,budget || 0);
  if(!total)return '<section class="request-viz"><h2>Request mosaic</h2><p class="viz-empty">No requests recorded.</p></section>';
  const pages=Math.ceil(total/mosaicPageSize);
  let page=Math.max(0,Math.min(state.mosaicPage || 0,pages-1)),frontier=-1;
  if(live){
    frontier=records.reduce((slot,r)=>r.status==='running'?Math.max(slot,r.slot):slot,-1);
    if(frontier<0)frontier=records.length?records.reduce((slot,r)=>Math.max(slot,r.slot),-1):budgetUsed-1;
    state.mosaicLivePage=Math.max(0,Math.min(Math.floor(frontier/mosaicPageSize),pages-1));
    const focusedSlot=document.activeElement?.closest?.('.request-mosaic')?document.activeElement.dataset?.liveSlot:undefined;
    if(state.mosaicFollow!==false&&page!==state.mosaicLivePage&&focusedSlot!==undefined&&focusedSlot!==state.mosaicAutoFocus&&Math.floor(Number(focusedSlot)/mosaicPageSize)!==state.mosaicLivePage)state.mosaicFollow=false;
    if(state.mosaicFollow!==false)page=state.mosaicLivePage;
  }
  const offset=page*mosaicPageSize,liveOffset=state.mosaicLivePage*mosaicPageSize;
  state.mosaicPage=page;
  const count=Math.min(mosaicPageSize,total-offset),geometry=reticulateCells(count,pages>1?mosaicPageSize:count);
  if(countOnly&&records.length&&!records.some(record=>record.slot>=offset&&record.slot<offset+count))return `<section class="request-viz"><h2>Requests</h2><p class="hint">Earlier request evidence was evicted from the live window. ${number(state.detail?.live_request_summary?.evicted||0)} earlier requests; ${Object.entries(state.detail?.live_request_summary?.statuses||{}).map(([status,n])=>`${number(n)} ${escape(human(status))}`).join(' · ')}.</p><div class="mosaic-pages"><button type="button" class="text-button" data-action="mosaic-prev" ${page===0?'disabled':''}>←</button><span>${offset+1}–${offset+count} / ${number(total)}</span><button type="button" class="text-button" data-action="mosaic-next" ${page===pages-1?'disabled':''}>→</button></div></section>`;
  const entrance=!live&&state.mosaicAnimated!==state.detail?.id;state.mosaicAnimated=state.detail?.id;
  const positions=requestPositions(records);
  const firstVisibleSlot=countOnly?records.reduce((slot,record)=>record.slot>=offset&&record.slot<offset+count?Math.min(slot,record.slot):slot,Infinity):offset;
  const bySlot=new Map(records.map((r,i)=>[live?r.slot:i,r]));
  // Request slots are compact dispatch IDs; unsent offers occupy the remaining budget.
  const sent=Math.max(attempted,live?records.reduce((count,r)=>Math.max(count,r.slot+1),0):records.length),droppedEnd=Math.min(budgetUsed,sent+droppedOffers);
  const statuses=['pass','fail','timeout','cancelled','skipped','inconclusive',...(live?['running','queued',...(countOnly?[]:['reserved','idle'])]:[])];
  return `<section class="request-viz"><div class="viz-heading"><h2>${live&&!countOnly?'Request budget':'Requests'}</h2><span class="mono hint">${countOnly?`${number(total)} started · ${number(records.length)} in live window`:`${number(records.length)} ${live?'started':'recorded'}`}${live&&droppedOffers?` · ${number(droppedOffers)} dropped before sending`:''}</span>${live&&pages>1?`<span class="mosaic-live-controls">${state.mosaicFollow!==false?'<span class="mosaic-following">Following live</span>':`<span class="hint">Live on requests ${liveOffset+1}–${Math.min(total,liveOffset+mosaicPageSize)}</span><button class="text-button mosaic-live" data-action="mosaic-live">Back to live</button>`}</span>`:''}</div><div class="mosaic-scroll"><svg class="request-mosaic${live?' is-live':''}${entrance?' is-entering':''}" viewBox="0 0 ${geometry.width} ${geometry.height}" style="--mosaic-w:${geometry.width}px;--spot-round:${geometry.round}" data-columns="${geometry.columns}" role="group" aria-label="${live?countOnly?'Live requests':'Live request budget':'Recorded requests'}. Arrow keys move between spots.">${geometry.cells.map((points,i)=>{
    const slot=offset+i,r=bySlot.get(slot);if(countOnly&&!r)return '';
    const dropped=!r&&slot>=sent&&slot<droppedEnd,status=r?requestStatus(r):dropped?'skipped':slot<sent?'inconclusive':slot<budgetUsed?displayStatus('reserved'):'idle',current=live&&status==='running'&&slot===frontier,label=r?requestLabel(r,records,undefined,positions):dropped?`Budget slot ${slot+1}: Not run · dropped before sending`:status!=='idle'?`Budget slot ${slot+1}: ${statusLabels[status]}, request evidence unavailable`:`Budget slot ${slot+1}: not used`;
    const action=r?(live?`data-live-slot="${r.slot}"`:`data-request="${escape(r.id)}"`):'';
    return `<g class="request-spot ${status}${current?' current':''}" data-live-key="slot:${offset+i}" data-request-id="${escape(r?.id)}" data-state="${status}" style="--i:${i}" ${action} ${r?'role="button"':'role="img"'} tabindex="${offset+i===firstVisibleSlot?0:-1}" aria-label="${escape(label)}"><polygon points="${points}"/>${spotGlyph(points,status)}</g>`;
  }).join('')}</svg></div><div class="viz-footer"><div class="mosaic-legend">${statuses.map(status=>`<span><i class="legend-dot ${status}" aria-hidden="true"></i><span aria-hidden="true">${statusIcons[status]}</span>${statusLabels[status]}${live?'':`<b>${number(records.filter(r=>requestStatus(r)===status).length)}</b>`}</span>`).join('')}${infoHelp('mosaic','Request outcomes','Green means a scored, correct answer. Red means a failed or timed-out request, invalid completion or incorrect answer. Timed-out requests use a clock glyph; cancelled and not-run requests use neutral markers. Unscored completions stay inconclusive. Check verdicts also consider probe intent, timing and aggregate criteria.')}</div>${pages>1?`<div class="mosaic-pages"><button class="text-button" data-action="mosaic-prev" ${page===0?'disabled':''} aria-label="Previous requests">←</button><span class="mono">${offset+1}–${offset+count} / ${number(total)}${live&&page!==state.mosaicLivePage?' · live on another page':''}</span><button class="text-button" data-action="mosaic-next" ${page===pages-1?'disabled':''} aria-label="Next requests">→</button></div>`:''}</div></section>`;
}
const conditionViews=new Map(),conditionLayouts=new Map();
let conditionRun=null,conditionFocus=null,conditionHover=null,conditionDialog=null,conditionDialogSequence=0;
const conditionStatusNames={passed:'Passed',failed:'Failed',inconclusive:'Inconclusive',running:'Running',waiting:'Waiting',skipped:'Not run',stopped:'Stopped',cancelled:'Cancelled',timed_out:'Timed out',not_started:'Not started',unscored:'Unscored'};
const conditionStatusGlyphs={passed:'✓',failed:'✕',inconclusive:'!',running:'·',waiting:'○',skipped:'–',stopped:'■',cancelled:'■',timed_out:'◷',not_started:'○',unscored:'!'};
const conditionStatusClass=status=>({passed:'pass',failed:'fail',waiting:'idle',stopped:'cancelled',timed_out:'timeout',not_started:'idle',unscored:'inconclusive'}[status] || status);
function conditionStatus(summary,{live=false,failed=0,started=0,completed=0,passed=0,verdict}={}) {
  const aliases={pass:'passed',fail:'failed',blocked:'inconclusive',timeout:'timed_out',completed:'inconclusive',finished:'inconclusive'};
  let status=summary?.aggregate_status;
  if(!status&&conditionStatusNames[summary?.status])status=summary.status;
  if(!status){
    if(live&&(summary?.running>0||summary?.status==='running'))status='running';
    else if(live&&summary?.status==='waiting')status='waiting';
    else if(verdict)status=aliases[verdict] || verdict;
    else if(failed)status='failed';
    else if(summary?.reason)status=started?'stopped':'skipped';
    else if(completed&&passed===completed)status='passed';
    else status=live&&!started?'waiting':'inconclusive';
  }
  if(!live&&['running','waiting'].includes(status))status=state.detail?.state==='cancelled'?'cancelled':'inconclusive';
  return aliases[status] || status;
}
function conditionProgress(view) {
  const {summary:s}=view,total=s.planned,done=s.completed||0,active=s.running||0;
  return `${number(done)}${total==null?'':` of ${s.planned_kind==='upper_bound'?'up to ':''}${number(total)}`} requests finished · ${number(active)} active${s.not_started>0?` · ${number(s.not_started)} not started`:''}`;
}
function conditionLimits(view) {
  const p=view.condition.parameters || {},config=state.detail?.config || reportConfig(state.detail?.report || {});
  const timeout=p.request_timeout_seconds ?? view.summary.request_timeout_seconds;
  const duration=p.duration_seconds ?? p.window_seconds ?? view.summary.duration_seconds;
  return [timeout!=null?`Request timeout: ${elapsed(timeout)}`:'Request timeout: not recorded',duration!=null?`Send new requests for: ${elapsed(duration)}`:'',config.max_duration_seconds!=null?`Maximum duration: ${elapsed(config.max_duration_seconds)}`:''].filter(Boolean).join(' · ');
}
function conditionIdentity(view) {
  return `Condition #${String(view.number).padStart(2,'0')}${view.summary.planned_condition===false?' · Additional recorded condition':''}`;
}
function conditionDescription(condition,summary) {
  const point=loadPointIdentity(condition.id || '');
  if(point){
    const level=condition.parameters?.concurrency ?? condition.level ?? point.level,rate=condition.parameters?.rate_rps ?? condition.rate_rps ?? point.rate;
    const pattern={constant:'Constant arrivals',poisson:'Poisson arrivals',bursts:'Bursts',interference:'Short-answer comparison'}[point.pattern];
    const delivery=rate!=null?`${number(rate,2)} requests/s`:level!=null?`${number(level)} at once`:'';
    return [pattern,delivery,point.bursts&&point.pattern!=='bursts'?'with bursts':''].filter(Boolean).join(' · ') || 'Concurrency not recorded';
  }
  if(summary.concurrency_description)return summary.concurrency_description;
  const p=condition.parameters || {},rate=p.rate_rps ?? condition.rate_rps,recordedLevel=String(condition.id||'').match(/(?:^|_)c(\d+)(?:_|\.|$)/),level=p.concurrency ?? condition.level ?? (recordedLevel?Number(recordedLevel[1]):null);
  return rate!=null?`${number(rate,2)} requests/s`:level!=null?`${number(level)} at once`:'Concurrency not recorded';
}
function isSetupCondition(condition) {
  const id=String(condition.id || ''),p=condition.parameters || {};
  return [condition.load_type,condition.kind,condition.phase,condition.cohort,p.cohort,p.phase].some(value=>/^(setup|preparation|priming|calibration|warmup|warm_up|warm-up)$/i.test(String(value || '')))
    || /^(?:warm|warmup|warm_up|warm-up|recovery_prime)$|^(?:setup|warmup|warm_up)(?:_|$)|(?:^|_)calibration(?:_|$)/.test(id);
}
function conditionDisplayName(condition,summary) {
  const point=loadPointIdentity(condition.id || ''),p=condition.parameters || {};
  if(point?.shape)return loadShapeLabel(point.shape).replace(' → ',' / ').replace(/\/ ([A-Z])/,(_,letter)=>'/ '+letter.toLowerCase());
  if(point?.type==='mixed')return 'Mixed traffic';
  if(point?.type==='stress')return {input:'Large-input stress',output:'Large-output stress',large_input:'Large-input stress',large_output:'Large-output stress'}[point.workload] || 'Token stress';
  if(point?.type==='interference')return /\.baseline(?:\.|$)/.test(condition.id)?'Short answers alone':'Short answers with a long request';
  const input=condition.input || p.input_shape,output=condition.output || p.output_shape;
  if(input&&output)return `${human(input)} input / ${String(output).toLowerCase()} output`;
  const name=summary.name || condition.label || phaseName(condition.id);
  return name.replace(/\s*[·→]?\s*(?:at )?[\d,]+ (?:at once|concurrent requests?)$/i,'').replace(/\s*·\s*$/,'');
}
function conditionSelection(view) {
  if(!view)return '<p class="hint">Waiting for the first test condition…</p>';
  const title=view.name.includes(view.description)||view.description==='Concurrency not recorded'?view.name:`${view.name} · ${view.description}`;
  return `<div><span class="eyebrow">${escape(conditionIdentity(view))} · ${escape(view.checkName)}</span><h3>${escape(title)}</h3><p class="hint">${escape(conditionProgress(view))}</p><p class="hint">${escape(conditionLimits(view))}</p></div><span class="condition-selection-status ${conditionStatusClass(view.status)}">${conditionStatusGlyphs[view.status] || '!'} ${escape(conditionStatusNames[view.status] || human(view.status))}</span>`;
}
function currentConditionView(views=[...conditionViews.values()]) {
  const key=document.activeElement?.closest?.('.condition-spot')?.dataset?.conditionKey;
  return conditionViews.get(key)||conditionViews.get(conditionHover)||views.find(view=>view.status==='running')||views.at(-1);
}
function refreshConditionSelection() {
  const view=currentConditionView(),selection=$('#condition-selection');
  for(const cell of $$('.condition-spot'))cell.classList.toggle('current',cell.dataset.conditionKey===view?.key);
  if(selection)patchHTML(selection,conditionSelection(view));
}
function updateConditionFocus(spot) {
  const key=spot?.dataset?.conditionKey,view=conditionViews.get(key);
  if(!view)return;
  conditionFocus=key;
  for(const cell of $$('.condition-spot')){cell.setAttribute('tabindex',cell===spot?'0':'-1');cell.classList.toggle('current',cell===spot);}
  const selection=$('#condition-selection');if(selection)patchHTML(selection,conditionSelection(view));
}
function conditionGeometry(keys) {
  let layout=conditionLayouts.get(conditionRun);
  if(!layout){layout={cells:new Map(),width:0,height:0,columns:null,round:4};conditionLayouts.set(conditionRun,layout);if(conditionLayouts.size>4)conditionLayouts.delete(conditionLayouts.keys().next().value);}
  const added=keys.filter(key=>!layout.cells.has(key));
  if(added.length){
    // Fill appended strips at the original spot size, including phantom sites
    // that bound a short final row. Existing polygons never need recomputing.
    layout.basis ||=added.length;
    for(let offset=0;offset<added.length;offset+=layout.basis){
      const batch=added.slice(offset,offset+layout.basis),strip=reticulateCells(batch.length,layout.basis),width=layout.width||strip.width,scale=width/strip.width,y=layout.height;
      // Assign execution order to centroid reading order, without changing the
      // original coat generator or any polygon once assigned to a condition.
      const rowHeight=strip.height/Math.ceil(batch.length/strip.columns),cells=strip.cells.map(points=>{
        const vertices=points.split(' ').map(pair=>pair.split(',').map(Number)),x=vertices.reduce((sum,p)=>sum+p[0],0)/vertices.length,cy=vertices.reduce((sum,p)=>sum+p[1],0)/vertices.length;
        return {points,x,row:Math.floor(cy/rowHeight)};
      }).sort((a,b)=>a.row-b.row||a.x-b.x);
      batch.forEach((key,i)=>layout.cells.set(key,cells[i].points.split(' ').map(pair=>{const [px,py]=pair.split(',').map(Number);return `${(px*scale).toFixed(2)},${(y+py*scale).toFixed(2)}`;}).join(' ')));
      layout.width=width;layout.height+=strip.height*scale;layout.columns ||=strip.columns;layout.round=strip.round;
    }
  }
  return layout;
}
// Only explicit per-condition results establish a condition verdict.
function conditionResults(report) {
  const results=new Map(),key=(target,id)=>JSON.stringify([target,id]),normalize=id=>String(id).replace(/^(consistency_c\d+_.+?)_p\d+$/,'$1').replace(/^mixed_(?:short|heavy)_(.+)_(before_output|active_stream)$/,'mixed_$1_$2');
  const result=(target,id,status)=>{if(!['pass','fail','blocked','inconclusive'].includes(status))return;const k=key(target,normalize(id)),previous=results.get(k);if(!previous||order[status]<order[previous])results.set(k,status);};
  const component=check=>{
    const metrics=check.metrics || {};
    for(const point of metrics.points||[]){result(check.target,point.id,point.status);if(point.type==='interference'){result(check.target,point.id+'.baseline',point.status);result(check.target,point.id+'.probe',point.status);}}
    for(const group of Object.values(metrics.load_types || {}))for(const point of group.points || []){result(check.target,point.id,point.status);if(point.type==='interference')result(check.target,point.id.replace('_probe_','_control_'),point.status);}
    for(const [scenario,row] of Object.entries(metrics.buckets || {}))result(check.target,scenario,row.status);
    for(const row of metrics.logprobs?.conditions || [])result(check.target,`consistency_c${row.level}_${row.shape}`,row.status);
    for(const [name,item] of Object.entries(metrics.cases || {}))for(const [level,row] of Object.entries(item.levels || {}))result(check.target,`stress_${name}_c${level}`,row.status);
    for(const row of metrics.conversations || [])result(check.target,`tools_${row.fixture_id}_${row.mode}`,row.status);
    for(const item of Object.values(metrics.scenarios || {}))component({...item,target:check.target});
  };
  for(const check of report?.checks || [])component(check);
  return {results,key};
}
function renderTests(records,{conditions=[],live=false,report=null}={}) {
  const run=state.detail?.id || report?.run_id || '';
  if(conditionRun!==run){conditionRun=run;conditionFocus=null;conditionHover=null;conditionViews.clear();conditionDialogSequence++;conditionDialog=null;$('#test-dialog')?.close?.();state.allRequestsOpen=false;}
  if(!conditions.length)conditions=report?.conditions || Object.entries(report?.observations?.targets || {}).flatMap(([target,obs])=>(obs.conditions || []).map(item=>({...item,target})));
  const {results,key:resultKey}=conditionResults(report),defaultTarget=reportConfig(report || {}).targets?.[0]?.name || state.detail?.config?.targets?.[0]?.name || '',groups=new Map();
  for(const summary of conditions){
    const condition=summary.condition || {id:summary.condition_id,label:summary.name,check:summary.check_id};if(!condition.id||isSetupCondition(condition))continue;
    const target=summary.target ?? defaultTarget,key=JSON.stringify([target,condition.id]);groups.set(key,{key,target,condition,summary,records:[]});
  }
  // Current reports provide aggregates; legacy reports alone need a request scan.
  if(!groups.size)for(const record of records){
    const condition=conditionInfo(record,report),target=record.target ?? defaultTarget,key=JSON.stringify([target,condition.id]);
    if(isSetupCondition(condition))continue;
    if(!groups.has(key))groups.set(key,{key,target,condition,records:[]});groups.get(key).records.push(record);
  }
  conditionViews.clear();
  for(const item of groups.values()){
    const {condition,records}=item,s=item.summary || {},started=s.started ?? s.sent ?? records.length,running=live?(s.running ?? records.filter(r=>r.status==='running').length):0,completed=s.completed ?? Math.max(0,started-running),failed=s.failed ?? records.filter(r=>requestOutcome(r)==='fail').length,planned=Object.hasOwn(s,'planned')?s.planned:null,passed=records.filter(r=>requestOutcome(r)==='pass').length;
    const summary={...s,planned,planned_kind:s.planned_kind || (planned==null?'unknown':'exact'),started,running,completed,failed,not_started:s.not_started ?? s.not_sent ?? (planned==null?null:Math.max(0,planned-started))};
    const status=conditionStatus(s,{live,started,completed,failed,passed,verdict:s.load_point_result?.status || results.get(resultKey(item.target,condition.id))});
    const check=summary.check_id || condition.check || 'serving',name=conditionDisplayName(condition,summary),description=conditionDescription(condition,summary),checkName=catalogTitle(check);
    conditionViews.set(item.key,{...item,summary,status,name,description,checkName,live});
  }
  const geometry=conditionGeometry([...conditionViews.keys()]),views=[...geometry.cells.keys()].map(key=>conditionViews.get(key)).filter(Boolean),counts=new Map();
  views.forEach((view,index)=>view.number=index+1);
  views.forEach(view=>counts.set(view.status,(counts.get(view.status)||0)+1));
  const orderedCounts=Object.keys(conditionStatusNames).filter(status=>counts.has(status)).map(status=>[status,counts.get(status)]);
  if(!conditionViews.has(conditionFocus))conditionFocus=(views.find(view=>view.status==='running') || views.at(-1))?.key || null;
  const focused=currentConditionView(views),countsLine=orderedCounts.map(([status,count])=>`${number(count)} ${(conditionStatusNames[status] || status).toLowerCase()}`).join(' · ');
  const cells=views.map((view,i)=>{
    const points=geometry.cells.get(view.key),css=conditionStatusClass(view.status),tooltip=`${conditionIdentity(view)} · ${view.checkName} · ${view.name} · ${view.description}\n${conditionProgress(view)}\n${conditionLimits(view)}`;
    return `<g class="condition-spot request-spot ${css}${view.key===focused?.key?' current':''}" data-live-key="condition:${escape(view.key)}" data-condition-key="${escape(view.key)}" data-condition-id="${escape(view.condition.id)}" data-state="${escape(css)}" role="button" tabindex="${view.key===conditionFocus?0:-1}" aria-label="${escape(tooltip)}"><title>${escape(tooltip)}</title><polygon points="${points}"/><text class="spot-glyph" x="${(points.split(' ').reduce((sum,p)=>sum+Number(p.split(',')[0]),0)/points.split(' ').length).toFixed(2)}" y="${(points.split(' ').reduce((sum,p)=>sum+Number(p.split(',')[1]),0)/points.split(' ').length).toFixed(2)}" aria-hidden="true">${conditionStatusGlyphs[view.status] || '!'}</text></g>`;
  }).join('');
  return `<section class="request-viz tests-section" aria-label="Tests" data-live-key="condition-tests"><div class="viz-heading"><h2 id="tests-heading">Tests</h2><span class="mono hint condition-summary" aria-live="polite">${escape(countsLine)}</span></div>${views.length?`<div class="mosaic-scroll"><svg class="condition-mosaic" viewBox="0 0 ${geometry.width} ${geometry.height}" style="--mosaic-w:${geometry.width}px;--spot-round:${geometry.round};--condition-display-width:${views.length<=2?geometry.width+'px':'100%'}" data-columns="${geometry.columns}" role="group" aria-label="Test conditions. Arrow keys move between cells; Enter opens details.">${cells}</svg></div>`:'<p class="viz-empty">Waiting for the first test condition…</p>'}<div class="viz-footer"><div class="mosaic-legend">${orderedCounts.map(([status])=>`<span><i class="legend-dot ${conditionStatusClass(status)}"></i>${conditionStatusGlyphs[status] || '!'} ${escape(conditionStatusNames[status] || human(status))}</span>`).join('')}</div><span class="hint">${number(views.length)} test condition${views.length===1?'':'s'}</span></div><div id="condition-selection" class="condition-selection" data-live-key="condition-selection">${conditionSelection(focused)}</div></section>`;
}
function conditionDialogCounts(view) {
  const s=view.summary,plan=s.planned==null?'—':`${s.planned_kind==='upper_bound'?'up to ':''}${number(s.planned)}`;
  const p=view.condition.parameters || {},duration=s.planned_kind==='duration'||s.plan_kind==='duration'||s.duration_seconds!=null||p.duration_seconds!=null||p.window_seconds!=null||Boolean(loadPointIdentity(view.condition.id || ''));
  const note=s.planned==null?`Unknown — ${duration?'duration-based':'plan not recorded'}`:'';
  return [['Planned',plan],['Started',number(s.started)],['Running',number(s.running)],['Completed',number(s.completed)],['Failed / timed out',number(s.failed)],['Not started',number(s.not_started)]].map(([label,value])=>`<div><dt>${label}</dt><dd>${value}</dd>${label==='Planned'&&note?`<small class="hint condition-plan-reason">${escape(note)}</small>`:''}</div>`).join('');
}
function conditionRequestContext(id) {
  const d=conditionDialog,view=conditionViews.get(d?.key),row=d?.payload?.rows?.find(row=>row.request_id===id);
  if(!view||!row)return null;
  const totals=Object.values(d.payload.totals || {}).filter(value=>Number.isFinite(value)).reduce((sum,value)=>sum+value,0);
  const s=view.summary,total=s.planned_kind==='exact'&&s.planned!=null?s.planned:Math.max(s.started || 0,totals,d.filter==='all'?d.payload.total || 0:0);
  return {number:row.number,total,label:view.name,condition_id:view.condition.id,target:view.target};
}
function renderConditionDialog() {
  const dialog=$('#test-dialog'),d=conditionDialog,view=conditionViews.get(d?.key);if(!dialog||!view)return;
  const payload=d.payload,rows=payload?.rows || [],totals=payload?.totals || {},pages=payload?.pages || 1,page=d.page,total=payload?.total || 0;
  const statuses=[...new Set(['running','passed','failed','timed_out','not_started',...Object.keys(totals).filter(status=>totals[status]>0),...(d.filter!=='all'?[d.filter]:[])])];
  const focused=document.activeElement,inside=focused&&dialog.contains?.(focused),focusId=inside?focused.id:null,focusValue=inside&&focused.tagName==='INPUT'&&!d.clamped?focused.value:null,start=focused?.selectionStart,end=focused?.selectionEnd;
  const reasons=view.summary.reasons || (view.summary.reason?[view.summary.reason]:[]),width=payload?.number_width || Math.max(4,String(view.summary.planned || view.summary.started || 0).length);
  const html=`<div class="dialog-head"><div><span class="eyebrow">${escape((conditionStatusNames[view.status] || view.status).toUpperCase())}</span><h2 id="test-title">${escape(view.name)}</h2><p class="hint">${escape(view.checkName)} · ${escape(view.description)}${view.target?` · ${escape(view.target)}`:''}</p></div><button type="button" class="button ghost" data-condition-action="close" aria-label="Close test details">×</button></div><div class="dialog-body"><dl class="metrics-grid condition-counts">${conditionDialogCounts(view)}</dl><p class="hint condition-effective-limits">${escape(conditionLimits(view))}</p><details class="condition-plan-identity"><summary>${escape(conditionIdentity(view))} · plan identity</summary><p>${escape(view.summary.name || view.condition.label || view.name)}</p><code>${escape(view.condition.id)}</code><p class="hint">Each configured workload, traffic pattern and level is a separate condition.</p></details>${reasons.length?`<p class="hint condition-shortfall">${view.summary.not_started>0?`${number(view.summary.not_started)} not sent: `:''}${escape(reasons.join(' · '))}</p>`:''}<div class="request-tools"><h3>Requests</h3><label>Status <select id="condition-filter" aria-label="Request status"><option value="all"${d.filter==='all'?' selected':''}>All statuses</option>${statuses.map(status=>`<option value="${escape(status)}"${d.filter===status?' selected':''}>${escape(conditionStatusNames[status] || human(status))}</option>`).join('')}</select></label><label>Rows <select id="condition-page-size" aria-label="Rows per page">${[10,25,50].map(size=>`<option value="${size}"${size===d.size?' selected':''}>${size}</option>`).join('')}</select></label></div>${d.error?`<p class="notice warning" role="status">${escape(d.error)}</p>`:''}<div class="request-table-scroll"><table class="request-table" aria-label="Requests in this test"><thead><tr><th>Request</th><th>Status</th><th>Result</th></tr></thead><tbody>${rows.length?rows.map(row=>`<tr data-live-key="condition-request:${row.number}"><td>${row.request_id?`<button type="button" class="text-button request-link" data-condition-request="${escape(row.request_id)}">#${String(row.number).padStart(width,'0')}</button>`:`<span class="mono">#${String(row.number).padStart(width,'0')}</span>`}</td><td><span class="condition-request-state ${conditionStatusClass(row.status)}">${conditionStatusGlyphs[row.status] || '!'} ${escape(conditionStatusNames[row.status] || human(row.status))}</span></td><td>${escape(row.result_text)}</td></tr>`).join(''):`<tr><td colspan="3" class="empty-requests">${d.error?escape(d.error):!payload?'Loading requests…':d.filter!=='all'?'No requests match this status':'No requests recorded.'}</td></tr>`}</tbody></table></div><div class="request-pagination"><span class="hint" aria-live="polite">${payload?`${number(payload.range_start)}–${number(payload.range_end)} of ${number(total)} requests`:'Loading request page…'}</span><div class="page-controls"><button type="button" class="button ghost" data-condition-action="first" aria-label="First page"${page<=1?' disabled':''}>«</button><button type="button" class="button ghost" data-condition-action="prev" aria-label="Previous page"${page<=1?' disabled':''}>‹</button><label>Page <input id="condition-page-number" type="number" min="1" max="${pages}" value="${page}" aria-label="Page number"></label><span class="hint">of ${number(pages)}</span><button type="button" class="button ghost" data-condition-action="go">Go</button><button type="button" class="button ghost" data-condition-action="next" aria-label="Next page"${page>=pages?' disabled':''}>›</button><button type="button" class="button ghost" data-condition-action="last" aria-label="Last page"${page>=pages?' disabled':''}>»</button></div></div><p id="condition-page-error" class="hint" role="status">${escape(d.validation || '')}</p></div><div class="dialog-footer"><button type="button" class="button primary" data-condition-action="close">Back to tests</button></div>`;
  patchHTML(dialog,html);
  // Native controls retain their values and caret while live patches update rows.
  const filter=$('#condition-filter'),size=$('#condition-page-size'),pageInput=$('#condition-page-number');if(filter)filter.value=d.filter;if(size)size.value=String(d.size);if(d.clamped&&pageInput)pageInput.value=String(d.page);
  if(focusId){const next=$('#'+focusId);if(next){if(focusValue!==null)next.value=focusValue;next.focus?.({preventScroll:true});if(start!=null&&next.type!=='number')next.setSelectionRange?.(start,end);}}
  d.clamped=false;
}
async function refreshConditionDialog({force=false}={}) {
  const d=conditionDialog,view=conditionViews.get(d?.key),dialog=$('#test-dialog');if(!d||!view||!dialog?.open)return;
  renderConditionDialog();
  if(d.pending){if(force)d.again=true;return;}
  if(!force&&Date.now()-(d.updatedAt || 0)<300)return;
  const query=()=>Object.entries({target:view.target,status:d.filter,page:d.page,page_size:d.size}).map(([key,value])=>`${key}=${encodeURIComponent(value)}`).join('&'),signature=query(),sequence=++conditionDialogSequence,run=state.detail?.id;
  d.pending=true;d.error=null;
  try{
    const payload=await api(`${runPath(run)}/conditions/${encodeURIComponent(view.condition.id)}/requests?${signature}`);
    if(conditionDialog!==d||conditionDialogSequence!==sequence||run!==state.detail?.id)return;
    const current=query();
    if(current!==signature){d.again=true;return;}
    d.payload=payload;d.clamped=payload.page!==d.page;d.page=payload.page;d.updatedAt=Date.now();
    if(payload.aggregate){const aggregate=payload.aggregate,currentView=conditionViews.get(d.key);if(currentView){currentView.summary={...currentView.summary,...aggregate};currentView.status=conditionStatus(aggregate,{live:currentView.live});}}
    renderConditionDialog();
  }catch(error){if(conditionDialog===d){d.error=`Could not load requests: ${error.message || error}`;renderConditionDialog();}}
  finally{d.pending=false;if(conditionDialog===d&&d.again){d.again=false;refreshConditionDialog({force:true});}}
}
function openConditionDialog(key) {
  const dialog=$('#test-dialog');if(!dialog||!conditionViews.has(key))return;
  conditionDialogSequence++;conditionDialog={key,page:1,size:10,filter:'all',payload:null,updatedAt:0};conditionFocus=key;
  renderConditionDialog();if(!dialog.open)dialog.showModal();refreshConditionDialog({force:true});
}
function conditionDialogClosed({reveal=true}={}) {
  const key=conditionDialog?.key;conditionDialogSequence++;conditionDialog=null;
  if(reveal&&state.reportRouteActive&&reportView.id===state.detail?.id&&reportView.tab==='performance')chooseReportTab('results');
  const spot=$$('.condition-spot').find(node=>node.dataset.conditionKey===key);if(reveal){const disclosure=spot?.closest('details');if(disclosure)disclosure.open=true;}spot?.focus?.({preventScroll:true});
}
function closeConditionDialog(options) {const dialog=$('#test-dialog');if(dialog?.open)dialog.close();conditionDialogClosed(options);}
function jumpConditionPage() {
  const d=conditionDialog;if(!d)return;
  const pages=d.payload?.pages || 1,value=$('#condition-page-number')?.value,page=Number(value);
  if(!value||!Number.isInteger(page)||page<1||page>pages){d.validation=`Enter a page from 1 to ${pages}.`;renderConditionDialog();return;}
  d.page=page;d.validation='';refreshConditionDialog({force:true});
}
function handleConditionClick(event) {
  const spot=event.target.closest?.('.condition-spot');if(spot){updateConditionFocus(spot);openConditionDialog(spot.dataset.conditionKey);return true;}
  const request=event.target.closest?.('[data-condition-request]');if(request){openRequest(request.dataset.conditionRequest);return true;}
  const action=event.target.closest?.('[data-condition-action]');if(!action||!conditionDialog)return false;
  const d=conditionDialog,pages=d.payload?.pages || 1,name=action.dataset.conditionAction;
  if(name==='close'){closeConditionDialog();return true;}
  if(name==='go'){jumpConditionPage();return true;}
  else d.page=name==='first'?1:name==='last'?pages:name==='prev'?Math.max(1,d.page-1):Math.min(pages,d.page+1);
  d.validation='';refreshConditionDialog({force:true});return true;
}
function handleConditionChange(event) {
  if(!conditionDialog)return false;
  if(event.target.id==='condition-filter'){conditionDialog.filter=event.target.value;conditionDialog.page=1;}
  else if(event.target.id==='condition-page-size'){const size=Number(event.target.value);if(![10,25,50].includes(size))return true;conditionDialog.size=size;conditionDialog.page=1;}
  else return false;
  const pageInput=$('#condition-page-number');if(pageInput)pageInput.value=String(conditionDialog.page);
  conditionDialog.validation='';refreshConditionDialog({force:true});return true;
}
function handleConditionKeydown(event) {
  const spot=event.target.closest?.('.condition-spot');
  if(spot){
    if(['Enter',' '].includes(event.key)){event.preventDefault();openConditionDialog(spot.dataset.conditionKey);return true;}
    const spots=$$('.condition-spot'),index=spots.indexOf(spot),columns=Number(spot.closest('svg')?.dataset.columns || 1),delta={ArrowLeft:-1,ArrowRight:1,ArrowUp:-columns,ArrowDown:columns}[event.key];
    if(delta!==undefined||['Home','End'].includes(event.key)){event.preventDefault();const next=spots[event.key==='Home'?0:event.key==='End'?spots.length-1:Math.max(0,Math.min(spots.length-1,index+delta))];updateConditionFocus(next);next?.focus?.({preventScroll:true});return true;}
  }
  if(conditionDialog&&event.key==='Escape'&&$('#test-dialog')?.open&&!$('#request-dialog')?.open){event.preventDefault();closeConditionDialog();return true;}
  if(conditionDialog&&event.key==='Enter'&&event.target.id==='condition-page-number'){event.preventDefault();jumpConditionPage();return true;}
  return false;
}
function renderAllRequests(records,options={}) {
  return `<details class="all-requests"${state.allRequestsOpen?' open':''}><summary>Individual requests · ${number(records.length)}</summary>${options.live?renderMosaic(records,options):`<div class="evidence-band${records.length<=72?' is-compact':''}">${renderMosaic(records)}${renderBeeswarm(records,{width:records.length<=72?500:960})}</div>`}</details>`;
}
function focusMosaicStart() {
  const spot=$('.request-mosaic [tabindex="0"]');state.mosaicAutoFocus=spot?.dataset?.liveSlot;spot?.focus({preventScroll:true});
}
function renderBeeswarm(records,{width=960}={}) {
  const positions=requestPositions(records),measured=records.filter(r=>Number.isFinite(r.elapsed_ms)&&r.elapsed_ms>=0),missing=records.length-measured.length;
  if(!measured.length)return '<section class="latency-viz"><h2>Request latency</h2><p class="viz-empty">No latency measurements.</p></section>';
  // Use the mosaic's page so every rendered dot remains individually inspectable.
  const page=records.slice((state.mosaicPage || 0)*mosaicPageSize,((state.mosaicPage || 0)+1)*mosaicPageSize).filter(r=>Number.isFinite(r.elapsed_ms)&&r.elapsed_ms>=0);
  const values=measured.map(r=>r.elapsed_ms).sort((a,b)=>a-b),quantile=q=>values[Math.ceil(q*values.length)-1];
  // Tight clusters far from zero get a zoomed axis; the left label always states where it starts.
  const max=Math.max(values.at(-1),1),low=values[0]>max*.35?Math.floor(values[0]*.85):0,span=Math.max(1e-9,max-low),placed=[];
  const xOf=value=>16+(value-low)/span*(width-32);
  // Dense clusters (e.g. hundreds of timeouts at the same deadline) pack tighter instead of growing the chart:
  // each dot's lane spacing shrinks with how many dots share its neighbourhood, so the swarm never exceeds maxExtent.
  const sorted=[...page].sort((a,b)=>a.elapsed_ms-b.elapsed_ms),xs=sorted.map(r=>xOf(r.elapsed_ms)),maxExtent=64;
  let from=0,to=0;
  sorted.forEach((r,i)=>{
    while(xs[i]-xs[from]>=10)from++;
    while(to+1<xs.length&&xs[to+1]-xs[i]<10)to++;
    const crowd=Math.max(to,i)-from+1,step=Math.min(10,(2*maxExtent-10)/crowd),x=xs[i];
    let lane=0,y=0;
    while(placed.some(p=>Math.abs(p.x-x)<10&&Math.abs(p.y-y)<step)&&lane<crowd*2){lane++;y=(lane%2?1:-1)*Math.ceil(lane/2)*step;}
    placed.push({r,x,y:Math.max(-(maxExtent-5),Math.min(maxExtent-5,y)),packed:step<9});
  });
  const extent=Math.min(maxExtent,Math.max(25,...placed.map(p=>Math.abs(p.y)+10))),top=22,height=extent*2+10+top*2,mid=top+extent+5;
  const markers=values.length>=4?[['p50',quantile(.5),top-8],['p95',quantile(.95),height-4]].map(([label,value,y])=>{const x=xOf(value),anchor=x>width-120?'end':x<120?'start':'middle';return `<g class="swarm-marker ${label}"><line x1="${x.toFixed(2)}" x2="${x.toFixed(2)}" y1="${top-4}" y2="${height-top+4}"/><text x="${x.toFixed(2)}" y="${y}" text-anchor="${anchor}">${label} ${escape(ms(value))}</text></g>`;}).join(''):'';
  return `<section class="latency-viz"><div class="viz-heading"><h2>Latency</h2><span class="hint">${number(measured.length)} measured${missing?` · ${number(missing)} missing`:''}${records.length>mosaicPageSize?' · current page':''}</span></div><div class="mosaic-scroll"><svg class="latency-beeswarm" viewBox="0 0 ${width} ${height}" role="group" aria-label="Elapsed latency in milliseconds; one dot per measured request"><path d="M16 ${mid}H${width-16}" class="swarm-axis"/>${markers}${placed.map(({r,x,y,packed},i)=>`<circle cx="${x.toFixed(2)}" cy="${(mid+y).toFixed(2)}" r="4.5" class="swarm-dot ${requestStatus(r)}${packed?' packed':''}" style="--i:${i}" tabindex="${i===0?0:-1}" role="button" data-request="${escape(r.id)}" aria-label="${escape(requestLabel(r,records,undefined,positions))}"></circle>`).join('')}</svg></div><div class="axis-labels mono"><span>${ms(low)}</span><span>${ms(max)}</span></div></section>`;
}
function renderFlightPlan(plan) {
  if(!plan)return '';
  const rows=(plan.rows || []).filter(r=>r.requests_per_target!==0),max=Math.max(1,...rows.map(r=>r.requests_per_target || 0));
  return `<div class="flight-plan"><div class="viz-heading"><h3>Flight plan</h3><span class="hint">Per endpoint</span></div>${rows.map(r=>`<div class="flight-row"><div><span>${escape(checkGuide[r.id]?catalogTitle(r.id):r.title)}</span><span class="mono">${r.requests_per_target==null?(r.kind==='duration'?'Duration-based':'Adaptive'):`${r.kind==='estimate'?'~':''}${number(r.requests_per_target)}`}</span></div><div class="flight-track ${r.requests_per_target==null?'adaptive':''}">${r.requests_per_target==null?'':`<span style="width:${r.requests_per_target/max*100}%"></span>`}</div></div>`).join('')}</div>`;
}
const phaseNames={"initial":"First request","readiness":"Readiness","warm":"Warm-up","short":"Short requests","long":"Long requests","generation":"Generation speed","context":"Long-context recall","correctness":"Answer correctness","json":"Structured JSON","sustained":"Sustained load","recovery":"Recovery check","recovery_baseline":"Recovery baseline","recovery_prime":"Recovery preparation","limits":"Output limits","stop_control":"Stop-sequence baseline","client_cancel":"Client cancellation","client_deadline":"Client deadline","backend_cancel":"Server cancellation","backend_cancel_control":"Server cancellation baseline","backend_cancel_followup":"Server cancellation follow-up","cancellation_probe":"Cancellation probes","cancellation_baseline":"Cancellation baseline","fairness":"Mixed long/short fairness","fairness_baseline":"Short requests alone","fairness_short":"Short requests with competing load","fairness_long":"Competing long requests","pack_control":"Quiet baseline","pack_loaded":"Your questions under load","request_pack":"Your questions","arrival":"Scheduled arrivals","mixed_control":"Short-request baseline","prefix_shared":"Shared prefix","prefix_independent":"Independent prefixes","prefix_history":"Growing conversation history","consistency":"Repeated-request consistency","judging":"AI evaluation","connection":"Connection","deadline_probe":"Client deadline"};
const phasePurposes={"concurrency":"Measures response quality, timing and evidenced concurrency for each duration-based test point.","access":"Checks that the server is reachable and can answer a request.","serving":"Checks that the server returns valid answers reliably.","generation":"Measures how quickly the server generates an answer.","context":"Checks whether the model can recall facts from a long input.","correctness":"Checks answers against the expected answers for different questions.","json":"Checks whether answers follow the required JSON structure.","recovery":"Checks whether normal answers return after sustained heavy traffic.","cancellation":"Checks request limits, cancellation, and the server's ability to keep serving afterwards.","fairness":"Checks whether competing long requests delay short answers.","mixed":"Checks whether a heavy partner delays a short request before or during its answer.","prefix":"Compares serving behavior when requests share an input prefix or use independent prefixes.","arrivals":"Checks whether the server keeps up with requests sent on a fixed schedule.","capacity":"Measures how many requests the server can handle at the configured arrival rate.","buckets":"Checks serving speed and reliability for this input size, output cap, and number of simultaneous requests.","token_stress":"Checks serving behavior with a large input or a long generated answer under load.","consistency":"Checks whether repeated copies of each prompt produce consistent answers under load.","sessions":"Checks whether answers stay correct as conversation history grows across turns.","tools":"Checks whether the model selects tools and uses their results correctly.","judging":"Asks an AI evaluator to check a recorded answer against its expected answer."};
const phaseChecks={"initial":"access","readiness":"access","warm":"serving","short":"serving","long":"serving","generation":"generation","context":"context","correctness":"correctness","json":"json","sustained":"recovery","recovery":"recovery","recovery_baseline":"recovery","recovery_prime":"recovery","limits":"cancellation","stop_control":"cancellation","client_cancel":"cancellation","client_deadline":"cancellation","backend_cancel":"cancellation","backend_cancel_control":"cancellation","backend_cancel_followup":"cancellation","cancellation_probe":"cancellation","cancellation_baseline":"cancellation","fairness":"fairness","fairness_baseline":"fairness","fairness_short":"fairness","fairness_long":"fairness","pack_control":"correctness","pack_loaded":"correctness","request_pack":"correctness","arrival":"arrivals","mixed_control":"mixed","prefix_shared":"prefix","prefix_independent":"prefix","prefix_history":"prefix","consistency":"consistency","judging":"judging","connection":"access","deadline_probe":"cancellation"};
const plainName=value=>String(value).replaceAll('_',' ').replaceAll('-',' ').toLowerCase().replace(/^./,c=>c.toUpperCase());
function phaseName(id,workload={}) {
  const comparison=/^load_(?:primary|legacy_.+?)_interference_c(\d+)\.(baseline|probe)(?:\.|$)/.exec(String(id));
  if(comparison)return `${comparison[2]==='baseline'?'Short answers alone':'Short answers with a long request'} at ${counted(Number(comparison[1]),'concurrent request')}`;
  id=String(id || 'requests').replace(/^load_interference_c(\d+)\.(short_stream|short_probe|long_probe)$/,(_,level,role)=>`load_interference_${role==='long_probe'?'probe':'control'}_c${level}`);
  id=String(id || 'requests');let m;
  if(m=/^load_combo_(short|medium|long)_(short|medium|long)_c(\d+)$/.exec(id))return `${plainName(m[1])} input → ${plainName(m[2])} output at ${counted(Number(m[3]),'concurrent request')}`;
  if(m=/^load_mixed_c(\d+)(?:\.(?:short|medium|long)_(?:short|medium|long))?$/.exec(id))return `Mixed load at ${counted(Number(m[1]),'concurrent request')}`;
  if(m=/^load_(interference|stress)_(control|probe|input|output)_c(\d+)$/.exec(id))return `${{control:'Short-only control',probe:'Long input during short streams',input:'Large-input stress',output:'Large-output stress'}[m[2]]} at ${counted(Number(m[3]),'concurrent request')}`;
  if(m=/^load_calibration_(short|medium|long)_(short_answer|long_output)$/.exec(id))return `Calibrating ${plainName(m[1])} input tokens (${m[2]==='long_output'?'long-output':'short-answer'} prompt)`;
  if(m=/^bucket_c(\d+)_(short|medium|long)_(short|medium|long)$/.exec(id))return `${plainName(m[2])} input / ${m[3]} output · ${Number(m[1])} at once`;
  if(m=/^consistency_c(\d+)_(.+?)(?:_p\d+)?$/.exec(id))return `${plainName(m[2])} input consistency · ${Number(m[1])} at once`;
  if(m=/^mixed_(?:(?:short|heavy)_)?(.+)_(before_output|active_stream)$/.exec(id))return `${plainName(m[1])} interference · ${m[2]==='before_output'?'before output':'during output'}`;
  if(m=/^session_(.+?)(?:_turn\d+)?$/.exec(id))return `${plainName(m[1])} sessions`;
  if(m=/^tools(?:_(.+))?_(auto|forced|required|none|named)_(complete|stream)$/.exec(id))return `${m[1]?plainName(m[1])+' tool calls':'Tool calls'} · ${{auto:'automatic',forced:'forced',required:'required',none:'no tools',named:'named tool'}[m[2]]} · ${m[3]==='stream'?'streaming':'non-streaming'}`;
  if(m=/^tools_(.+)_(complete|stream)(?:_turn\d+)?$/.exec(id))return `${plainName(m[1])} conversation · ${m[2]==='stream'?'streaming':'non-streaming'}`;
  if(m=/^stress_(.+)_c(\d+)$/.exec(id))return `${plainName(m[1])} · ${Number(m[2])} at once`;
  if(id.startsWith('stress_calibration_'))return `Input calibration · ${plainName(id.slice(19)).toLowerCase()}`;
  if(id==='arrival'){const rate=['requests_per_second','rate_rps','arrival_rate_rps','rate'].map(key=>workload[key]).find(value=>value!=null);if(typeof rate==='number'&&rate>0)return `Arrivals · ${rate} per second`;}
  if(/^traffic_stage_\d+$/.test(id)){const rate=workload.rate_rps ?? workload.traffic_rate_rps;return rate==null?'Capacity traffic':`${rate} requests/s`;}
  if(m=/^(correctness|json)_c(\d+)$/.exec(id))return `${phaseNames[m[1]]} · ${Number(m[2])} at once`;
  return phaseNames[id] || plainName(id);
}
function fallbackConditionId(scenario) {
  scenario=String(scenario || 'requests').replace(/^load_interference_c(\d+)\.(short_stream|short_probe|long_probe)$/,(_,level,role)=>`load_interference_${role==='long_probe'?'probe':'control'}_c${level}`);
  return String(scenario || 'requests').replace(/^(consistency_c\d+_.+?)_p\d+$/,'$1').replace(/^mixed_(?:short|heavy)_(.+)_(before_output|active_stream)$/,'mixed_$1_$2').replace(/^(load_mixed_c\d+)\..+$/,'$1').replace(/^(session_.+)_turn\d+$/,'$1').replace(/^(tools_.+_(?:complete|stream))_turn\d+$/,'$1');
}
function fallbackCheck(scenario) {
  if(scenario.startsWith('load_'))return 'concurrency';
  if(phaseChecks[scenario])return phaseChecks[scenario];
  for(const [prefix,check] of [['bucket_','buckets'],['stress_','token_stress'],['mixed_','mixed'],['session_','sessions'],['traffic_stage_','capacity']])if(scenario.startsWith(prefix))return check;
  return ['consistency','tools','prefix','context','correctness','json'].find(key=>scenario.startsWith(key+'_')) || 'serving';
}
function conditionInfo(record,report=state.detail?.report) {
  const scenario=record.scenario || record.condition_id || 'requests',config=state.detail?.config || reportConfig(report || {}),workload={...record.workload};
  if(/^traffic_stage_\d+$/.test(scenario))workload.rate_rps ??=record.traffic_rate_rps ?? config.traffic?.rates?.[Number(scenario.slice(14))];
  if(scenario==='arrival')workload.requests_per_second ??=config.arrivals?.requests_per_second;
  const id=record.condition_id || (scenario==='arrival'&&workload.requests_per_second?`arrival_r${workload.requests_per_second}`:fallbackConditionId(scenario));
  const summaries=[...(state.detail?.live_conditions || []),...Object.entries(report?.observations?.targets || {}).flatMap(([target,obs])=>(obs.conditions || []).map(row=>({...row,target})))];
  const summary=record.condition_summary || summaries.find(row=>(record.target==null||row.target===record.target)&&row.condition?.id===id);
  const loadPoint=record.load_point||loadPointIdentity(id);
  const check=record.check_ids?.[0] || (loadPoint?'load':fallbackCheck(scenario));
  return {check,...summary?.condition,id,label:(loadPoint&&/\.(baseline|probe)(?:\.|$)/.test(scenario)?phaseName(scenario,workload):loadPoint?loadPointLabel(loadPoint):summary?.condition?.label || phaseName(scenario,workload)),purpose:summary?.condition?.purpose || phasePurposes[check],parameters:summary?.condition?.parameters || {},summary};
}
function testLabel(record,report){return conditionInfo(record,report).label;}
function scenarioLabel(record,report) {
  const label=testLabel(record,report),scenario=String(record.scenario || '');let detail='';
  const mixed=/^mixed_(short|heavy)_/.exec(scenario),turn=/_turn(\d+)$/.exec(scenario),prompt=/^consistency_.+_p(\d+)$/.exec(scenario);
  if(mixed)detail=mixed[1]==='short'?'short request':'heavy partner';
  else if(turn&&/^(session|tools)_/.test(scenario))detail=`turn ${Number(turn[1])+1}`;
  else if(prompt)detail=`prompt ${Number(prompt[1])+1}`;
  return detail?`${label} · ${detail}`:label;
}
function requestPositions(records) {
  const groups=new Map(),positions=new Map();
  for(const record of records){const key=JSON.stringify([record.target,record.condition_id || fallbackConditionId(record.scenario)]);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(record);}
  for(const peers of groups.values()){
    peers.sort((a,b)=>Number.isFinite(a.slot)&&Number.isFinite(b.slot)?a.slot-b.slot:Number.isFinite(a.dispatch_ms)&&Number.isFinite(b.dispatch_ms)?a.dispatch_ms-b.dispatch_ms:(Date.parse(a.started_at)||0)-(Date.parse(b.started_at)||0));
    peers.forEach((record,index)=>positions.set(record,{index:index+1,total:peers.length}));
  }
  return positions;
}
function requestIdentity(record,records=state.detail?.live_requests || state.detail?.report?.requests || [],condition=conditionInfo(record),positions=requestPositions(records)) {
  const position=positions.get(record) || {index:1,total:1};
  return `Request ${position.index} of ${condition.summary?.planned ?? position.total}`;
}
function requestTiming(record) {return record.elapsed_ms==null?(inProgress(requestStatus(record))?'Timing pending':'No timing recorded'):ms(record.elapsed_ms);}
function renderRequestTermination(record) {
  const labels={request_timeout:'Request timeout reached',user_stop:'Stopped by user',run_duration_limit:'Run traffic duration limit reached',memory_limit:'Memory guardrail reached',error_threshold:'Error threshold reached',intentional_deadline:'Intentional deadline probe'};
  const reason=record.termination_reason,limit=record.termination_limit_seconds,timeout=record.request_timeout_seconds;
  if(reason==null&&limit==null&&timeout==null)return '';
  return `<section class="request-termination"><h3>Request limits &amp; stop reason</h3><dl class="metrics-grid">${metric('Configured request timeout',timeout==null?'Not recorded':elapsed(timeout))}${reason?metric('Stop reason',labels[reason] || human(reason)):''}${limit!=null?metric('Effective deadline',elapsed(limit),'May be shortened by the remaining run duration or an intentional probe'):''}</dl>${record.error?`<p class="hint">${escape(record.error)}</p>`:''}</section>`;
}
function requestLabel(record,records,condition=conditionInfo(record),positions) {return `${condition.label}. ${requestIdentity(record,records,condition,positions)} · ${statusLabels[requestStatus(record)] || record.status} · ${requestTiming(record)}`;}
function requestIdentityContent(record,records) {return `${escape(requestIdentity(record,records))} · <span class="mono">${escape(record.id || (inProgress(requestStatus(record))?'Request ID pending':'Request ID not recorded'))}</span> · ${escape(statusLabels[requestStatus(record)] || record.status)} · ${requestTiming(record)}`;}
function renderRequestContext(record,records) {
  const condition=conditionInfo(record),p=condition.parameters,value=v=>Array.isArray(v)?v.map(n=>number(n)).join('–'):v==null?'Not recorded':number(v),chars=record.input_chars>0?record.input_chars:(p.input_chars ?? record.input_chars),tokens=record.requested_input_tokens ?? record.workload?.requested_input_tokens ?? (typeof chars==='number'?Math.ceil(chars/4):p.input_tokens_approx),cap=record.requested_max_tokens || p.output_tokens;
  return `<p class="request-identity">${requestIdentityContent(record,records)}</p><section class="request-sent"><h3>What was sent</h3><dl class="metrics-grid">${metric('Prompt size',`${value(chars)} characters · ~${value(tokens)} tokens`,'Approximate tokens; not server-reported usage')}${metric('Output cap',`${value(cap)} tokens`)}${metric('Concurrency',p.concurrency==null?'Not recorded':`${value(p.concurrency)} at once`)}${metric('Same prompt',condition.summary?.planned===1?'Single request':p.same_prompt===true?'Yes · repeated across this test':p.same_prompt===false?(p.repetitions_per_prompt?`Different prompts · each repeated ${value(p.repetitions_per_prompt)} times`:'No · inputs vary across this test'):'Not recorded in this legacy report')}</dl>${record.evaluation?.messages?.some(message=>typeof message.content==='string'&&message.content.trim())?'<p><button type="button" class="text-button" data-action="request-input">View input messages</button></p>':''}</section><p class="hint request-internal"><small>Internal ID: <span class="mono">${escape(record.scenario || '')}${record.fixture_id?` · ${escape(record.fixture_id)}`:''}</span></small></p>`;
}
function renderPhases(events) {
  const phases=[];
  for(const e of events){
    if(e.event==='scenario_started')phases.push({key:`${e.target}:${e.scenario}`,title:e.scenario,target:e.target,record:e,start:e.elapsed_seconds,end:null});
    if(e.event==='scenario_finished'){
      const phase=phases.findLast(p=>p.key===`${e.target}:${e.scenario}`&&p.end===null);
      if(phase)phase.end=e.elapsed_seconds;
      else phases.push({key:`${e.target}:${e.scenario}`,title:e.scenario,target:e.target,record:e,start:null,end:e.elapsed_seconds});
    }
  }
  // Back-to-back repeats of one phase (e.g. many sustained-load rounds) read as one line with a round count.
  const groups=[];
  for(const p of phases){
    const last=groups.at(-1);
    if(last&&last.key===p.key&&last.end!==null&&last.start!==null&&p.start!==null){last.rounds++;last.end=p.end;last.busy+=p.end===null?0:p.end-p.start;last.running=p.end===null;continue;}
    groups.push({...p,rounds:1,busy:p.end===null||p.start===null?0:p.end-p.start,running:p.end===null});
  }
  for(const phase of groups)phase.running=phase.running&&displayStatus('running')==='running';
  const row=p=>`<li data-live-key="${escape(JSON.stringify([p.key,p.start]))}" class="${p.running?'running':'finished'}" title="${escape(conditionInfo(p.record).purpose)}"><span class="phase-mark">${p.running?'·':'✓'}</span><div><strong>${escape(scenarioLabel(p.record))}</strong><small>${escape(p.target)}${p.rounds>1?` · ${number(p.rounds)} rounds`:''}</small></div><span class="mono">${p.start===null?'Start unavailable':p.running?'Running':elapsed(p.rounds>1?p.end-p.start:p.end-p.start)}</span></li>`;
  const visible=groups.slice(-6),earlier=groups.slice(0,-6);
  return `<section class="phase-timeline"><h2>Phases</h2>${groups.length?`${earlier.length?`<details class="phase-earlier"><summary>${number(earlier.length)} earlier ${earlier.length===1?'phase':'phases'}</summary><ol>${earlier.map(row).join('')}</ol></details>`:''}<ol>${visible.map(row).join('')}</ol>`:'<p class="hint">Waiting for scenario events.</p>'}</section>`;
}
function openLiveRequest(slot) {
  const r=(state.detail.live_requests || []).find(r=>r.slot===slot);if(!r)return;
  const dialog=$('#request-dialog');
  dialog.innerHTML=`<div class="dialog-head"><div><div class="eyebrow">Live request</div><h2 id="request-title">${escape(testLabel(r))}</h2><p class="hint">${escape(conditionInfo(r).purpose)}</p></div><button class="button ghost" data-action="close-dialog" aria-label="Close request evidence">✕</button></div><div class="dialog-body">${renderRequestContext(r,state.detail.live_requests)}${renderRequestTermination(r)}<div class="live-request-status" data-status-slot="${r.slot}" data-request-id="${escape(r.id)}">${badge(requestStatus(r))}</div><p class="hint">Evidence snapshot at opening; status updates live. Full evidence is available when the run finishes.</p><dl class="metrics-grid">${metric('Transport',r.status)}${metric('Elapsed',ms(r.elapsed_ms))}${metric('First answer',ms(r.first_output_ms))}${metric('Answer',r.score===true?'Correct':r.score===false?'Incorrect':'Unscored')}${metric('Endpoint',r.target)}</dl></div>`;dialog.showModal();
}
function renderOutcomeStrip() {
  const finished=state.runs.filter(r=>!activeState(r)),tally=key=>finished.filter(r=>runOutcome(r)===key).length;
  const stats=[['Runs',number(state.runs.length),''],['Passed',number(tally('pass')),'pass'],['Failed',number(tally('fail')),'fail'],['Latest',(rel=>!rel?'—':rel==='now'||!/^\d+[mhd]$/.test(rel)?rel:`${rel} ago`)(relativeTime(state.runs[0]?.started_at)),'']];
  return `<section class="outcome-overview"><dl class="run-stats">${stats.map(([label,value,kind])=>`<div class="${kind}"><dt>${label}</dt><dd${/^\d+$/.test(value)?` data-count="${value}"`:''}>${escape(value)}</dd></div>`).join('')}</dl><div class="outcome-track"><div class="viz-heading"><h2>Run outcomes</h2><span class="hint">${number(state.runs.length)} ${state.runs.length===1?'run':'runs'} · newest first</span></div><div class="outcome-strip">${state.runs.map(r=>`<a href="${linkRun(r.id)}" class="outcome-segment ${runOutcome(r)}" aria-label="${escape((r.targets || []).map(t=>t.model).join(', '))}: ${escape(statusLabels[runOutcome(r)] || runOutcome(r))}, ${date(r.started_at)}"><span class="sr-only">${escape(statusLabels[runOutcome(r)] || runOutcome(r))}</span></a>`).join('')}</div></div></section>`;
}
let heroEntered=false;
function renderHeroCoat() {
  const width=1440,height=760,cells=coatGeometry(64,width,height,{seed:23,inset:15,jitter:.95,relax:false}),entering=!heroEntered;heroEntered=true;
  return `<div class="hero-coat${entering?' is-entering':''}" aria-hidden="true"><svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="xMidYMid slice">${cells.map(({site,points},i)=>`<polygon points="${points}" style="--i:${i};--x:${(site[0]/width).toFixed(3)};--y:${(site[1]/height).toFixed(3)}"/>`).join('')}</svg></div>`;
}
const themeIcons={dark:'<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="3.5"/><path d="M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M4.7 15.3l1.4-1.4M13.9 6.1l1.4-1.4"/></svg>',light:'<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M15.5 12.2A6.5 6.5 0 0 1 7.8 4.5a6.5 6.5 0 1 0 7.7 7.7Z"/></svg>'};
function toggleRail(force) {
  const root=document.documentElement,collapsed=force ?? !root.classList.contains('rail-collapsed');
  root.classList.toggle('rail-collapsed',collapsed);
  try{localStorage.setItem('giraffe-rail',collapsed?'collapsed':'open');}catch{}
  const button=$('[data-action="toggle-rail"]');if(button){button.setAttribute('aria-expanded',String(!collapsed));button.setAttribute('aria-label',collapsed?'Expand sidebar':'Collapse sidebar');}
}
function setTheme(theme) {
  document.documentElement.dataset.theme=theme;
  try{localStorage.setItem('giraffe-theme',theme);}catch{}
  const toggle=$('[data-action="theme"]');if(toggle){toggle.setAttribute('aria-label',`Switch to ${theme==='dark'?'light':'dark'} theme`);toggle.innerHTML=themeIcons[theme==='dark'?'dark':'light'];}
}
const commandIcons={go:'<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h12m-5-5 5 5-5 5"/></svg>',plus:'<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 4v12M4 10h12"/></svg>',history:'<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3.5 10a6.5 6.5 0 1 0 2-4.7M3.5 3.5v3h3M10 6.5V10l2.5 1.5"/></svg>',theme:'<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="6.5"/><path d="M10 3.5v13a6.5 6.5 0 0 0 0-13Z" fill="currentColor"/></svg>',bolt:'<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M11 2.5 4.5 11H10l-1 6.5L15.5 9H10z"/></svg>',keys:'<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="2.5" y="5" width="15" height="10" rx="2"/><path d="M5.5 8h1M9.5 8h1M13.5 8h1M6.5 12h7"/></svg>'};
const commandItems=[{label:'New run',key:'N',icon:'plus',group:'Navigate',action:()=>startNewRun()},{label:'Runs',key:'G R',icon:'history',group:'Navigate',action:()=>{location.hash='#runs';}},{label:'Primary action',key:'⌘ ↵',icon:'bolt',group:'Actions',action:()=>primaryAction()},{label:'Toggle sidebar',key:'[',icon:'keys',group:'Actions',action:()=>toggleRail()},{label:'Toggle theme',key:'',icon:'theme',group:'Actions',action:()=>setTheme(document.documentElement.dataset.theme==='light'?'dark':'light')},{label:'Shortcuts',key:'?',icon:'keys',group:'Actions',action:()=>openCommands(true)},{label:'Commands',key:'⌘ K',icon:'go',group:'Actions',action:()=>openCommands()}];
let commandList=commandItems;
function openCommands(shortcuts=false) {
  const dialog=$('#command-dialog');
  const recent=shortcuts?[]:state.runs.slice(0,5).map(run=>({label:(run.targets || []).map(t=>t.model).filter(Boolean).join(', ') || 'Unknown model',key:'',meta:relativeTime(run.started_at),icon:'go',group:'Recent runs',status:runOutcome(run),action:()=>{location.hash=linkRun(run.id);}}));
  commandList=shortcuts?commandItems.filter(item=>item.key):[...commandItems,...recent];
  const groups=[...new Set(commandList.map(item=>item.group))];
  dialog.innerHTML=`<div class="dialog-head command-head">${shortcuts?'<h2 id="command-title">Keyboard shortcuts</h2>':`<h2 id="command-title" class="sr-only">Go to…</h2><svg class="command-search-icon" viewBox="0 0 20 20" aria-hidden="true"><circle cx="9" cy="9" r="5.5"/><path d="m13 13 3.5 3.5"/></svg><label class="sr-only" for="command-search">Find a command</label><input id="command-search" placeholder="Type a command or search runs…" autocomplete="off">`}<button class="text-button" data-action="close-commands" aria-label="Close commands"><kbd>Esc</kbd></button></div><div class="command-list">${groups.map(group=>`<div class="command-group" role="group" aria-label="${escape(group)}"><p class="command-group-title">${escape(group)}</p>${commandList.map((item,i)=>item.group!==group?'':`<button class="command-item" data-command="${i}">${item.status?`<span class="command-glyph status-spot ${escape(item.status)}" aria-hidden="true"></span>`:`<span class="command-glyph" aria-hidden="true">${commandIcons[item.icon] || ''}</span>`}<span class="command-label">${escape(item.label)}</span>${item.meta?`<span class="command-meta">${escape(item.meta)}</span>`:''}${item.key?`<kbd>${escape(item.key)}</kbd>`:''}</button>`).join('')}</div>`).join('')}</div><p class="command-hint"><span><kbd>↑</kbd><kbd>↓</kbd> navigate</span><span><kbd>↵</kbd> select</span><span><kbd>Esc</kbd> close</span></p>`;
  if(!dialog.open)dialog.showModal();(shortcuts?$('.command-item',dialog):$('#command-search'))?.focus();
}
function primaryAction() {
  if(document.querySelector('dialog[open]'))return;
  const main=$('#main');
  const action=$$('.button.primary',main).find(el=>!el.disabled&&!el.classList.contains('danger')&&el.id!=='cleanup-confirm'&&el.getClientRects().length);
  action?.click();
}
function initReticulate() {
  let theme='dark';try{theme=localStorage.getItem('giraffe-theme') || theme;}catch{}
  setTheme(theme==='light'?'light':'dark');
  toggleRail(document.documentElement.classList.contains('rail-collapsed'));
}

function expireAccess() {
  if(!state.accessExpired){
    connectionError('Giraffe was restarted. Open the new link printed by giraffe ui in your terminal.');
    state.accessExpired=true;
    clearPoll();cancelLiveFetch();
  }
  return Object.assign(new Error('Access expired.'),{status:401});
}
async function api(path, options={}) {
  if(state.accessExpired)throw expireAccess();
  let response;
  try { response = await fetch(path,{...options,headers:{'Content-Type':'application/json',...(options.headers || {})}}); }
  catch(error) { if(error.name==='AbortError')throw error;throw new Error('The local UI server could not be reached. Check that giraffe ui is still running.'); }
  if(response.status===401)throw expireAccess();
  let body;
  try { body = await response.json(); } catch(error) { if(error.name==='AbortError')throw error;throw new Error(`The local server returned an unreadable response (${response.status}).`); }
  if (!response.ok) {
    if(response.status===404 && (path.startsWith('/api/config/')||path==='/api/datasets/preview'||path==='/api/runs/try-example')){
      const error=new Error('The running Giraffe server does not support this configuration operation. Restart giraffe ui from the updated installation, then refresh this page and try again.');
      error.status=response.status;throw error;
    }
    const detail = body.detail || body.errors || body.error || `Request failed (${response.status}).`;
    const clean = message => message.replace(/^body:\s*/,'').replace(/^Value error,\s*/,'');
    const message = Array.isArray(detail) ? detail.map(e => {
      if(typeof e==='string')return clean(e);
      const location=(e.loc || []).filter((part,i)=>i!==0||part!=='body').join('.');
      return `${location?location+': ':''}${clean(e.msg || JSON.stringify(e))}`;
    }).join('\n') : clean(typeof detail === 'string' ? detail : JSON.stringify(detail));
    const error = new Error(message);error.status=response.status;error.errors=Array.isArray(detail)?detail.filter(item=>item&&typeof item==='object').map(item=>({...item,field:item.field||(item.loc||[]).filter(part=>!['body','config'].includes(part)).join('.')||'config'})):[];throw error;
  }
  return body;
}
function toast(message) { const el=$('#toast');el.textContent=message;el.hidden=false;setTimeout(()=>{el.hidden=true;},4500); }
function connectionError(message='') { if(state.accessExpired)return;if(document.body)document.body.dataset.runner=message?'unavailable':'connected';$('#connection-error').textContent=message;$('#connection-error').hidden=!message; }
function setActive(id) {
  if(state.detail?.id===id&&!activeState(state.detail))id=null;
  state.active=id || null;
  updateTitle();
  const link=$('#active-run-link');link.hidden=!id;link.href=id?linkRun(id):'#runs';
  const html=id?`${badge('running','Run in progress',{state:'running'})}<span aria-hidden="true"> →</span>`:'';if(link.innerHTML!==html)link.innerHTML=html;
}
function updateNav(section, label) {
  renderRailRuns();
  $('#nav-runs').classList.toggle('current',section==='runs');$('#nav-new').classList.toggle('current',section==='new');$('#nav-new').hidden=false;
  $('#nav-runs').setAttribute('aria-current',section==='runs'?'page':'false');$('#nav-new').setAttribute('aria-current',section==='new'?'page':'false');
  $('#breadcrumb').textContent=label;
  state.pageLabel=label;updateTitle();
}
function updateTitle() {
  const run=(state.detail?.id===state.active?state.detail:null)||state.runs.find(run=>run.id===state.active);
  const label=run?.label||run?.targets?.map(target=>target.model).filter(Boolean).join(', ')||run?.config?.targets?.map(target=>target.model).filter(Boolean).join(', ')||state.pageLabel||'Run';
  document.title=state.active?`● In progress · ${label} · Giraffe`:`${state.pageLabel||'Giraffe'}${state.pageLabel?' · Giraffe':''}`;
}
function pageVisible() { return document.visibilityState!=='hidden'; }
function storageVisible() { return pageVisible()&&(location.hash==='#runs'||Boolean($('#storage-dialog')?.open)); }
function clearPoll() { clearTimeout(state.poll);state.poll=null; }
function cancelLiveFetch() { state.eventAbort?.abort();state.eventAbort=null; }
function schedulePoll(callback, version, delay=1800) { clearPoll();if(state.accessExpired||!pageVisible())return;state.poll=setTimeout(()=>{if(!state.accessExpired&&version===state.routeVersion&&pageVisible()) callback(version);},Math.max(300,delay,(state.eventPollAfter || 0)-Date.now())); }
async function refreshStorage() {
  if(!storageVisible())return;
  try{state.storage=await api('/api/storage');state.storageUnavailable=false;}
  catch{state.storage=null;state.storageUnavailable=true;}
}
async function refreshRuns() {
  const [runs]=await Promise.allSettled([api('/api/runs'),refreshStorage()]);
  if(runs.status==='rejected')throw runs.reason;
  const data=runs.value;state.runs=data.runs || [];state.diskActive=data.disk_active_id || null;setActive(data.active_run_id);$('#run-count').textContent=state.runs.length || '';renderRailRuns();return data;
}
async function runEvents(id, after=0) {
  const controller=typeof AbortController==='function'?new AbortController():null;
  state.eventAbort=controller;
  state.eventPollAfter=Date.now()+300;
  try{const delta=await api(`${runPath(id)}/events?after=${Number.isFinite(after)?after:0}`,controller?{signal:controller.signal}:{});state.eventPollAfter=Date.now()+(delta.reset||delta.idle?1800:300);return delta;}
  finally{if(state.eventAbort===controller)state.eventAbort=null;}
}
function mergeRunEvents(detail, delta) {
  const events=delta.events || [],keys=['state','progress','live_request_count','live_request_summary','live_preparations','phases','error','abort_reason'];
  for(const event of events)for(const key of keys)if(Object.hasOwn(event,key))detail[key]=event[key];
  for(const key of keys)if(Object.hasOwn(delta,key))detail[key]=delta[key];
  detail.seq=delta.seq;
  detail.events=[...(detail.events || []),...events].slice(-100);
  const records=new Map((detail.live_requests || []).map(record=>[record.slot,record]));
  for(const record of delta.records || [])records.set(record.slot,{...records.get(record.slot),...record});
  const through=detail.live_request_summary?.through_slot;
  detail.live_requests=[...records.values()].filter(record=>through==null||record.slot>through).sort((a,b)=>a.slot-b.slot).slice(-4096);
  const key=row=>JSON.stringify([row.target || '',row.condition?.id || row.condition_id || row.id || '']);
  const conditions=new Map((detail.live_conditions || []).map(row=>[key(row),row]));
  const checks=new Map((detail.live_checks || detail.report?.checks || []).map(row=>[key(row),row]));
  for(const row of delta.checks || []){const rows=row.condition || row.condition_id?conditions:checks;rows.set(key(row),{...rows.get(key(row)),...row});}
  detail.live_conditions=[...conditions.values()];detail.live_checks=[...checks.values()];
  if(detail.report)detail.report.checks=detail.live_checks;
  return detail;
}
function relativeTime(value) {
  const then=Date.parse(value);if(!Number.isFinite(then))return '';
  const seconds=Math.max(0,(Date.now()-then)/1000);
  return seconds<60?'now':seconds<3600?`${Math.floor(seconds/60)}m`:seconds<86400?`${Math.floor(seconds/3600)}h`:seconds<86400*7?`${Math.floor(seconds/86400)}d`:new Date(then).toLocaleDateString(undefined,{month:'short',day:'numeric'});
}
// The rail keeps the last few runs one click away, wherever the user is.
function renderRailRuns() {
  const list=$('#rail-runs'),section=$('#rail-recent');if(!list||!section)return;
  const current=location.hash.startsWith('#runs/')?decodeURIComponent(location.hash.slice(6).split('/')[0]):null;
  section.hidden=!state.runs.length;
  patchHTML(list,state.runs.slice(0,6).map(saved=>{
    const run=state.detail?.id===saved.id&&!activeState(state.detail)?{...saved,state:state.detail.state,overall:state.detail.report?.overall || saved.overall}:saved;
    const model=(run.targets || []).map(t=>t.model).filter(Boolean).join(', ') || 'Unknown model',outcome=runOutcome(run);
    return `<li data-live-key="${escape(run.id)}"><a href="${linkRun(run.id)}" class="${run.id===current?'current':''}" ${run.id===current?'aria-current="page"':''} title="${escape(model)} · ${escape(statusLabels[outcome] || outcome)}"><span class="status-spot ${escape(outcome)}" aria-hidden="true">${statusIcons[outcome] || '?'}</span><span class="rail-run-model">${escape(model)}<small>${escape(statusLabels[outcome] || outcome)} · ${escape(relativeTime(run.started_at))} · ${escape(run.started_at?new Date(run.started_at).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'}):run.id)}</small></span></a></li>`;
  }).join(''));
}
// Route changes glide in; re-renders within a view (polling, toggles) stay still.
function focusRouteHeading() {const heading=$('#main h1');heading?.setAttribute('tabindex','-1');heading?.focus({preventScroll:true});}
function enterRoute() {
  focusRouteHeading();
  const main=document.querySelector?.('#main');
  if(!main?.animate||window.matchMedia?.('(prefers-reduced-motion: reduce)').matches)return;
  main.animate([{opacity:0,transform:'translateY(8px)'},{opacity:1,transform:'none'}],{duration:420,easing:'cubic-bezier(.2,.8,.2,1)'});
  // Headline counts tick up from zero; the final text is always the real value.
  for(const element of main.querySelectorAll('[data-count]')){
    const target=Number(element.dataset.count),start=performance.now();if(!Number.isFinite(target)||target<2)continue;
    const tick=now=>{const t=Math.min(1,(now-start)/650),eased=1-Math.pow(1-t,3);element.textContent=t<1?Math.round(target*eased).toLocaleString():element.dataset.countText || target.toLocaleString();if(t<1)requestAnimationFrame(tick);};
    element.dataset.countText=element.textContent;requestAnimationFrame(tick);
  }
}
// One shared tooltip for spots and dots: instant, and it reads the same label assistive tech hears.
let spotTipTarget=null,spotTipPosition=null;
function showSpotTip(target,x,y) {
  const tip=$('#spot-tip'),label=target?.getAttribute('aria-label');if(!tip||!label)return;
  spotTipTarget=target;spotTipPosition={x,y};
  const outcome=['pass','fail','timeout','cancelled','skipped','inconclusive','running','queued','reserved','stopping','idle'].find(name=>target.classList.contains(name)) || 'idle';
  const split=label.indexOf('. Request '),head=split<0?label:label.slice(0,split),rest=split<0?[]:[label.slice(split+2)];
  tip.innerHTML=`<span class="status-icon" aria-hidden="true">${statusIcons[outcome]}</span><span><strong>${escape(head.replace(/[.:]$/,''))}</strong>${rest.length?`<small>${escape(rest.join(' ').replace(/\.\s+/g,' · ').replace(/\.$/,''))}</small>`:''}</span>`;
  tip.hidden=false;
  const bounds=tip.getBoundingClientRect(),left=Math.min(window.innerWidth-bounds.width-12,Math.max(12,x+14)),top=y-bounds.height-14<12?y+18:y-bounds.height-14;
  tip.style.transform=`translate(${Math.round(left)}px,${Math.round(top)}px)`;
}
function hideSpotTip() {const tip=$('#spot-tip');if(tip)tip.hidden=true;spotTipTarget=null;spotTipPosition=null;}
function refreshSpotTip() {if($('#spot-tip')?.hidden===false){if(spotTipTarget?.isConnected)showSpotTip(spotTipTarget,spotTipPosition.x,spotTipPosition.y);else hideSpotTip();}}
async function route() {
  const destination=reportRoute();
  pendingReportReveal=null;
  if(!destination||!state.reportRouteActive||state.detail?.id!==destination.id)cancelStopConfirmationTimer({reset:true});
  if(destination&&state.reportRouteActive&&state.detail?.id===destination.id){
    const openedPerformance=destination.tab==='performance'&&reportView.tab!=='performance';
    syncReportView();
    if(destination.tab){reportView.tab=destination.tab;reportView.explicit=true;}
    else {reportView.tab='results';reportView.explicit=false;}
    renderDetail();
    if(openedPerformance&&typeof fetch==='function')loadPerformance({force:true});
    if(destination.section)revealReportSection(destination.section);
    if(destination.requestId)await openRequest(destination.requestId);
    return;
  }
  state.reportRouteActive=false;
  detailPlan={id:null,plan:null,pending:false,done:false};
  findingEvidence={id:null,records:new Map(),pending:new Set(),errors:new Map()};
  const reportRail=$('#report-sections');if(reportRail)reportRail.hidden=true;
  if($('#test-dialog')?.open)closeConditionDialog();
  hideSpotTip();hidePerformanceTooltip();state.cleanup=null;
  if($('#dataset-dialog')?.open)closeDatasetDialog();
  closeRequestEvidence();
  for(const id of ['baseline-dialog','comparison-dialog','storage-dialog']){const dialog=$('#'+id);if(dialog?.open)dialog.close();}
  if ($('#new-run-form')) captureFormDraft();
  window.scrollTo?.({top:0,left:0,behavior:'instant'});
  clearPoll();cancelLiveFetch();const version=++state.routeVersion;const hash=location.hash;
  try {
    if(!hash){location.hash=state.active?linkRun(state.active):'#new';return;}
    if (hash==='#new') { updateNav('new','New run');state.config ||= clone(state.bootstrap.config);renderNew();enterRoute();schedulePoll(pollNew,version,state.active?3000:10000);return; }
    if (destination) {
      const id=destination.id;updateNav('runs','Run detail');
      state.detail=null;
      $('#main').innerHTML='<div class="loading">Loading run…</div>';
      const detail=await api(runPath(id));if(version!==state.routeVersion)return;
      state.detail=detail;state.failingRequestsOnly=false;state.mosaicPage=0;state.mosaicFollow=true;state.mosaicAutoFocus=null;state.selectedCheck=null;state.checkNavigatorOpen=false;state.tab='checks';state.comparisonFilter=null;state.comparisonCheck='all';reportView.id=null;renderDetail();enterRoute();if(destination?.section)revealReportSection(destination.section);
      if(reportView.tab==='performance'&&typeof fetch==='function')loadPerformance({force:true});
      if(destination?.requestId)await openRequest(destination.requestId);
      if(version!==state.routeVersion)return;
      if(activeState(detail))schedulePoll(pollDetail,version,300);else if(state.active)schedulePoll(pollRunStatus,version,3000);return;
    }
    updateNav('runs','Runs');$('#main').innerHTML='<div class="loading">Loading runs…</div>';
    await refreshRuns();if(version!==state.routeVersion)return;
    renderRuns();enterRoute();schedulePoll(pollRuns,version,3000);
  } catch(error) {
    if(version!==state.routeVersion)return;
    if(destination&&error.status===404){
      updateNav('runs','Run not found');
      $('#main').innerHTML='<div class="page-heading"><h1>Run not found</h1><p>This run is not available in the current run directory.</p></div><a class="button" href="#runs">Back to runs</a>';schedulePoll(pollRunStatus,version,3000);focusRouteHeading();return;
    }
    $('#main').innerHTML=`<div class="page-heading"><h1>Could not open this view</h1></div>${notice(error.message,'error')}<button class="button" data-action="retry">Try again</button>`;
    schedulePoll(pollRunStatus,version,3000);focusRouteHeading();
  }
}
async function pollRuns(version) {
  try {
    const previous=JSON.stringify([state.runs,state.storage,state.storageUnavailable]),active=state.active;
    if(state.active)await Promise.all([pollRunStatus(version,{schedule:false}),refreshStorage()]);else await refreshRuns();if(version!==state.routeVersion)return;
    if(!active)connectionError();if(previous!==JSON.stringify([state.runs,state.storage,state.storageUnavailable])||active!==state.active)renderRuns();
  }catch(error){if(version===state.routeVersion)connectionError(error.message+' Your saved runs have not changed.');}
  if(version===state.routeVersion)schedulePoll(pollRuns,version,3000);
}
async function pollRunStatus(version,{schedule=true}={}) {
  const id=state.active;if(!id)return;let delay=300;
  try{
    if(state.statusRun?.id!==id)state.statusRun={id,state:'running',seq:0};
    const delta=await runEvents(id,state.statusRun.seq);
    if(version!==state.routeVersion||state.active!==id)return;
    if(delta.reset||delta.idle)delay=1800;
    if(delta.reset)state.statusRun=await api(runPath(id));else mergeRunEvents(state.statusRun,delta);
    if(version!==state.routeVersion||state.active!==id)return;
    const saved=state.runs.find(run=>run.id===id);if(saved)saved.state=state.statusRun.state;
    if(!activeState(state.statusRun)){setActive(null);await refreshRuns();}
    connectionError();renderRailRuns();
  }catch(error){if(error.name==='AbortError')return;delay=3000;if(version===state.routeVersion)connectionError(error.message);}
  if(schedule&&version===state.routeVersion&&state.active)schedulePoll(pollRunStatus,version,delay);
}
async function pollNew(version) {
  if(!pageVisible())return;
  try{if(state.active)await pollRunStatus(version,{schedule:false});else await refreshRuns();}
  catch(error){if(error.name==='AbortError')return;if(version===state.routeVersion)connectionError(error.message);}
  if(version!==state.routeVersion)return;
  const button=$('#start-run');if(button&&!state.submitting)button.disabled=!canStartRun();const activeNotice=$('#new-active-notice');if(activeNotice){activeNotice.hidden=!(state.active||state.diskActive);activeNotice.innerHTML=newActiveNotice();}
  schedulePoll(pollNew,version,state.active?3000:10000);
}
let detailEpoch=0,detailSequence=0,lastDetailSequence=0;
async function pollDetail(version) {
  const id=state.detail.id,epoch=detailEpoch,sequence=++detailSequence;
  try {
    const delta=await runEvents(id,state.detail.seq);if(version!==state.routeVersion || state.detail?.id!==id || sequence<lastDetailSequence)return;
    if(!activeState(state.detail)&&activeState({state:delta.state}))return;
    const wasStopping=state.detail.state==='stopping';
    let detail=delta.reset?await api(runPath(id)):mergeRunEvents(state.detail,delta);
    if(version!==state.routeVersion || state.detail?.id!==id || sequence<lastDetailSequence)return;
    const completed=!activeState(detail);
    if(completed&&!delta.reset){detail=await api(runPath(id));if(version!==state.routeVersion||state.detail?.id!==id||sequence<lastDetailSequence)return;}
    if(wasStopping&&activeState(detail))detail.state='stopping';
    if(['running','queued'].includes(detail.state)&&(epoch!==detailEpoch||state.stopPending?.id===id)){
      schedulePoll(pollDetail,version,500);return;
    }
    if(!activeState(state.detail)&&activeState(detail))return;
    if(state.detail.state==='stopping'&&['running','queued'].includes(detail.state))detail.state='stopping';
    lastDetailSequence=sequence;
    if(state.liveError?.id===id&&sequence>state.liveError.sequence)state.liveError=null;
    const focused=document.activeElement,slot=focused?.dataset?.liveSlot,action=focused?.dataset?.action,autoFocused=slot!==undefined&&slot===state.mosaicAutoFocus,summaryFocus=focused?.tagName==='SUMMARY'&&Boolean(focused.closest?.('.all-requests'));
    connectionError();state.detail=detail;renderDetail();
    if(completed&&typeof fetch==='function')loadPerformance({force:true});
    if(slot!==undefined){const scope=$('.all-requests'),spot=scope?scope.querySelector(`[data-live-slot="${slot}"]`):$(`[data-live-slot="${slot}"]`);if(spot)spot.focus({preventScroll:true});else if(autoFocused&&activeState(detail)&&state.mosaicFollow!==false)focusMosaicStart();}
    else if(summaryFocus){const disclosure=$('.all-requests');disclosure?.querySelector('summary')?.focus({preventScroll:true});}
    else if(action==='stop')$('[data-action="stop"]')?.focus({preventScroll:true});
    if(activeState(detail))schedulePoll(pollDetail,version,delta.reset||delta.idle?1800:300);else {if(state.active===id)setActive(null);await refreshRuns();if(version===state.routeVersion&&state.active)schedulePoll(pollRunStatus,version,3000);}
  } catch(error) {if(error.name==='AbortError'||version!==state.routeVersion||state.detail?.id!==id||sequence<lastDetailSequence)return;if(epoch!==detailEpoch){schedulePoll(pollDetail,version,500);return;}connectionError(error.message+' Reconnecting; the suite may still be running.');schedulePoll(pollDetail,version,3000);}
}
function suiteList() { return `<ol class="suite-list">${(state.bootstrap.checks || []).map((c,i)=>`<li><span class="index">${String(i+1).padStart(2,'0')}</span><span>${escape(c.title)}</span>${c.optional?'<small>OPT</small>':''}</li>`).join('')}</ol>`; }
function runOutcome(run) {
  if(run.state==='cancelled'||run.state==='completed'&&(run.abort_reason||run.report?.abort_reason||run.summary?.abort_reason))return 'stopped';
  return activeState(run)||['error','failed','cancelled','interrupted'].includes(run.state)?run.state:run.overall || run.state;
}
function filteredRuns() {
  const query=state.historySearch.trim().toLowerCase();
  return state.runs.filter(run=>(state.filter==='all'||(activeState(run)?'active':runOutcome(run))===state.filter)&&
    (!query||[run.id,run.run_id,...(run.targets || []).flatMap(target=>[target.name,target.model,target.url])].join(' ').toLowerCase().includes(query)));
}
function storageLine() {
  const s=state.storage,policy=s?.auto_cleanup||{},rules=[];
  if(policy.keep_latest!=null)rules.push(`keep newest ${number(policy.keep_latest)} runs`);
  if(policy.older_than_days!=null)rules.push(`older than ${number(policy.older_than_days)} days`);
  if(policy.max_total_bytes!=null)rules.push(`oldest first to ${diskSize(policy.max_total_bytes)}`);
  return `<div class="storage-line"><p>${s?`Saved runs on disk: ${diskSize(s.runs_bytes)}${s.limits?.max_total_storage?` of ${diskSize(s.limits.max_total_storage)} limit`:' · no total limit'} · Baselines ${diskSize(s.baselines_bytes)}${s.baselines_shared_bytes?` (${diskSize(s.baselines_shared_bytes)} shared with source run)`:''} · Automatic cleanup: ${rules.length?escape(rules.join(' or ')):'off'}${s.last_auto_cleanup?.deleted?.length?` · Automatically deleted: ${s.last_auto_cleanup.deleted.map(escape).join(', ')}`:''}${s.last_auto_cleanup?.error?` · Automatic cleanup failed: ${escape(s.last_auto_cleanup.error)}`:''} · Free disk ${diskSize(s.free_bytes)}`:state.storageUnavailable?'Storage information unavailable':'Saved runs on disk: loading…'}</p><button type="button" class="button small" data-action="storage-dialog">Clean up…</button></div>`;
}
function keepButton() {return `<button type="button" class="button small" data-action="keep-run" aria-pressed="${Boolean(state.detail.kept)}">${state.detail.kept?'Kept — protected from cleanup':'Keep run'}</button>`;}
function openStorageDialog() {
  state.cleanup={revision:0,preview:null,busy:false};
  const dialog=$('#storage-dialog');
  dialog.innerHTML=`<div class="dialog-head"><h2 id="storage-title">Clean up saved runs on disk</h2><button type="button" class="button ghost" data-action="close-storage" aria-label="Close cleanup">✕</button></div><div class="dialog-body"><p>Preview which runs to delete. Runs matching any criterion are included. Protected runs and baselines stay on disk.</p><form id="cleanup-form" novalidate><div class="fields"><label for="cleanup-age">Older than (days)<input id="cleanup-age" name="older_than_days" type="number" min="${state.bootstrap.limits['cleanup.older_than_days'].minimum}" step="1" placeholder="Disabled"></label><label for="cleanup-latest">Keep newest runs (counts protected runs too)<input id="cleanup-latest" name="keep_latest" type="number" min="${state.bootstrap.limits['cleanup.keep_latest'].minimum}" step="1" placeholder="Disabled"></label><label for="cleanup-total">Maximum saved runs on disk (MiB)<input id="cleanup-total" name="max_total_mib" type="number" min="${(state.bootstrap.limits['cleanup.max_total_bytes'].minimum/1024**2).toFixed(6)}" step="0.000001" placeholder="Disabled"></label></div><button type="submit" class="button" id="cleanup-preview-button">Preview</button></form><div id="cleanup-error" class="error-message" role="alert" hidden></div><div id="cleanup-preview" aria-live="polite"></div></div><div class="dialog-footer"><button type="button" class="button" data-action="close-storage">Cancel</button><button type="button" class="button danger" id="cleanup-confirm" data-action="cleanup-confirm" disabled>Delete runs</button></div>`;
  dialog.showModal();$('#cleanup-age').focus();
}
function invalidateCleanup() {
  if(!state.cleanup)return;
  state.cleanup.revision++;state.cleanup.preview=null;$('#cleanup-confirm').disabled=true;$('#cleanup-confirm').textContent='Delete runs';$('#cleanup-preview').innerHTML='<p class="hint">Criteria changed. Preview again before deleting.</p>';
}
async function previewCleanup(event) {
  event.preventDefault();const form=$('#cleanup-form');
  const current=state.cleanup,revision=++current.revision,criteria={};current.preview=null;$('#cleanup-confirm').disabled=true;
  for(const name of ['older_than_days','keep_latest']){const value=form.elements[name].value;if(value!=='')criteria[name]=Number(value);}
  if(form.elements.max_total_mib.value!==''){
    const bytes=Math.round(Number(form.elements.max_total_mib.value)*1024**2);
    if(!Number.isFinite(bytes)||bytes<=0){$('#cleanup-error').textContent='Maximum saved runs on disk must be greater than 0. Use Older than 0 days (--older-than 0 in the CLI) to remove all unprotected runs.';$('#cleanup-error').hidden=false;return;}
    criteria.max_total_bytes=bytes;
  }
  if(!form.reportValidity())return;
  $('#cleanup-error').hidden=true;$('#cleanup-preview-button').disabled=true;
  try {
    const plan=await api('/api/storage/cleanup-preview',{method:'POST',body:JSON.stringify(criteria)});
    if(state.cleanup!==current||revision!==current.revision||!$('#storage-dialog').open)return;
    current.preview={criteria,ids:plan.candidates.map(run=>run.id)};
    const reason=run=>String(run.criterion || '').split(', ').map(key=>({older_than_days:`Older than ${criteria.older_than_days} ${criteria.older_than_days===1?'day':'days'}`,keep_latest:criteria.keep_latest===1?'Beyond the newest run':`Beyond the newest ${criteria.keep_latest} runs`,max_total_bytes:`Oldest first to reach ${diskSize(criteria.max_total_bytes)} on disk`}[key] || key)).join(' · ');
    const list=(runs,protectedRun)=>runs.map(run=>`<li><strong>${escape(run.id)}</strong> · ${diskSize(run.bytes)} on disk · ${date(run.started_at)}<small>${escape(protectedRun?run.protected_reason || run.reason:reason(run))}</small></li>`).join('');
    $('#cleanup-preview').innerHTML=`<h3>${number(plan.candidates.length)} ${plan.candidates.length===1?'run':'runs'} to delete</h3>${plan.candidates.length?`<ul class="cleanup-runs">${list(plan.candidates,false)}</ul>`:'<p>No runs match these criteria.</p>'}<p>${diskSize(plan.bytes_freed)} on disk would be freed · ${diskSize(plan.runs_bytes_after)} would remain on disk.</p><h3>${number(plan.protected.length)} protected ${plan.protected.length===1?'run':'runs'}</h3>${plan.protected.length?`<ul class="cleanup-runs">${list(plan.protected,true)}</ul>`:'<p>No protected runs.</p>'}`;
    const confirm=$('#cleanup-confirm');confirm.textContent=`Delete ${plan.candidates.length} ${plan.candidates.length===1?'run':'runs'} (${diskSize(plan.bytes_freed)})`;confirm.disabled=!plan.candidates.length;
  }catch(error){if(state.cleanup===current&&revision===current.revision){$('#cleanup-error').textContent=error.message;$('#cleanup-error').hidden=false;}}
  finally{if(state.cleanup===current)$('#cleanup-preview-button').disabled=false;}
}
async function confirmCleanup() {
  const current=state.cleanup,preview=current?.preview,routeVersion=state.routeVersion;if(!preview||current.busy)return;
  current.busy=true;$('#cleanup-confirm').disabled=true;$('#cleanup-preview-button').disabled=true;$('#cleanup-error').hidden=true;
  try {
    const result=await api('/api/storage/cleanup',{method:'POST',body:JSON.stringify({...preview.criteria,ids:preview.ids})});
    if(state.cleanup!==current)return;
    current.preview=null;state.cleanupResult=result;$('#storage-dialog').close();renderRuns();
    try{await refreshRuns();if(state.cleanup===current&&state.routeVersion===routeVersion)renderRuns();}
    catch(error){toast('Cleanup finished, but run history could not be refreshed: '+error.message);}
  }catch(error){if(state.cleanup===current){current.preview=null;$('#cleanup-error').textContent=error.message+' Preview again before deleting.';$('#cleanup-error').hidden=false;}}
  finally{current.busy=false;if(state.cleanup===current)$('#cleanup-preview-button').disabled=false;}
}
async function toggleKept() {
  const detail=state.detail,button=$('[data-action="keep-run"]');button.disabled=true;
  try{const result=await api(`${runPath(detail.id)}/keep`,{method:'POST',body:JSON.stringify({keep:!detail.kept})});if(state.detail!==detail)return;detail.kept=result.kept;renderDetail();}
  catch(error){toast(error.message);button.disabled=false;}
}
function cleanupResultNotice() {
  const result=state.cleanupResult;if(!result)return '';
  const count=result.deleted.length,skipped=result.skipped || [];
  const message=`Deleted ${count} ${count===1?'run':'runs'}, freed ${diskSize(result.bytes_freed)} on disk.${skipped.length?` Skipped ${skipped.length}: ${skipped.map(run=>`${run.id} — ${run.reason}`).join('; ')}.`:''}`;
  return `<div class="notice ${skipped.length?'warning':'info'}" role="status">${escape(message)}</div>`;
}
function renderRuns() {
  cancelStopConfirmationTimer({reset:true});
  const focused=document.activeElement,focusId=['run-search','run-filter'].includes(focused?.id)?focused.id:null;
  const selection=focusId==='run-search'?[focused.selectionStart,focused.selectionEnd]:null;

  if(!state.runs.length&&!state.active){
    $('#main').innerHTML=`<section class="first-run"><h1>Run history</h1><p>No runs yet.</p>${storageLine()}${cleanupResultNotice()}<a class="button primary" href="#new">${icon('plus')} New run</a></section>`;
    return;
  }
  const runs=filteredRuns();
  $('#main').innerHTML=`<div class="page-heading"><h1>Runs</h1><a class="button primary" href="#new">${icon('plus')} New run</a></div>${storageLine()}${cleanupResultNotice()}${renderOutcomeStrip()}<div class="history-chips" aria-label="Filter runs">${['all','pass','fail','inconclusive','blocked','skipped','active','stopped','interrupted','error'].filter(status=>status==='all'||status===state.filter||state.runs.some(r=>(activeState(r)?'active':runOutcome(r))===status)).map(status=>`<button class="filter-chip" data-run-filter="${status}" aria-pressed="${state.filter===status}">${status==='all'?'All runs':status==='active'?'Running':statusLabels[status]}</button>`).join('')}</div>
  <div class="history-layout history-focused"><section class="panel"><div class="section-heading"><div class="history-tools"><label class="history-search"><span class="sr-only">Search runs</span><input id="run-search" type="search" placeholder="Search runs" value="${escape(state.historySearch)}" autocomplete="off"></label><div class="filter-group"><label class="sr-only" for="run-filter">Filter run status</label><select id="run-filter" aria-label="Filter run status"><option value="all">All outcomes</option><option value="fail">Failed</option><option value="pass">Passed</option><option value="inconclusive">Inconclusive</option><option value="blocked">Blocked</option><option value="skipped">Not run</option><option value="active">Active</option><option value="stopped">Stopped</option><option value="interrupted">Interrupted</option><option value="error">Runner error</option></select></div></div></div>
  ${runs.length?`<div class="history-table"><table aria-label="Saved endpoint runs"><thead><tr><th>Model</th><th>Outcome</th><th>Started</th><th><span class="sr-only">Open run</span></th></tr></thead><tbody>${runs.map(r=>{
    const targets=r.targets || [],model=targets.map(t=>t.model).filter(Boolean).join(', ') || 'Unknown model';
    const names=targets.map(t=>t.name).filter(name=>name&&name!=='local').join(', ');
    const started=`<time${r.started_at?` datetime="${escape(r.started_at)}"`:''}>${date(r.started_at)}</time>`;
    const hosts=[...new Set(targets.map(t=>{try{return new URL(t.url).host;}catch{return '';}}).filter(Boolean))].join(', ');
    return `<tr class="history-row ${escape(runOutcome(r))}"><td class="history-identity"><span class="status-spot ${escape(runOutcome(r))}" aria-hidden="true"></span><span class="history-names"><a class="run-link" href="${linkRun(r.id)}"${names?` title="${escape(names)}"`:''}>${escape(model)}</a>${hosts?`<small class="history-host">${escape(hosts)}</small>`:''}<small class="history-mobile-date">${started}</small><small class="history-storage">${diskSize(r.bytes)} on disk${r.kept?' · <span class="kept-marker">Kept</span>':''}</small></span></td><td class="history-result">${badge(runOutcome(r),null,r)}</td><td class="history-date">${started}</td><td class="history-open"><a class="text-button history-run-action" href="${linkRun(r.id)}" aria-label="Open run for ${escape(model)}${r.started_at?` from ${escape(date(r.started_at))}`:''}">${icon('arrow')}</a></td></tr>`;}).join('')}</tbody></table></div>`:
  `<div class="empty-state"><h2>No matching runs</h2><button class="button" data-action="clear-filter">Clear filters</button></div>`}</section></div>`;
  $('#run-filter').value=state.filter;
  if(focusId){const control=$('#'+focusId);control?.focus({preventScroll:true});if(selection&&selection[0]!=null)control?.setSelectionRange?.(...selection);}
}
const numericFields=['context_limit','concurrency','max_requests','max_memory_mb','max_duration_seconds','request_timeout_seconds','max_output_tokens'].map(name=>[name]);
const limitFields=[['first_output_ms','First output maximum','ms'],['latency_ms','Response time maximum','ms'],['stream_gap_ms','Stream pause maximum','ms'],['min_output_tokens_per_second','Minimum generation rate','tokens / second after first output (estimate)'],['fairness_max_ratio','Mixed-load slowdown','ratio, greater than 1']];
const fieldDescriptions={
  url:{hint:'Address of your LLM server.',help:'Use the address Giraffe can reach, such as http://localhost:11434/v1 for Ollama or http://localhost:8000/v1 for vLLM. Giraffe connects to an existing server; it does not start or install a model.'},
  model:{hint:'Model name used by your server.',help:'Use an exact model name from your server, including its tag or alias. For Ollama, use a name from ollama list. The model must already be installed.'},
  api_key_env:{hint:'',help:'Name of an environment variable set on the machine running giraffe ui, set before starting Giraffe. Enter its name, not the key. Saved setups and terminal commands need this variable or a freshly pasted key.'},
  name:{hint:'A name for this endpoint in results.',help:'Choose a unique name such as local or staging. Giraffe uses it to identify this endpoint in reports and comparisons.'},
  context_limit:{help:'The server’s configured limit for the question and answer together, measured in tokens. A token is a small piece of text. Use the context length configured on your server, which can be lower than the model’s advertised maximum.'},
  concurrency:{hint:'Requests running at the same time.',help:'The most requests Giraffe can run at the same time across all servers. You set this number; Giraffe does not detect your hardware limit. Individual tests can use fewer requests. Raising this does not change Maximum requests or Concurrency levels.'},
  max_memory_mb:{hint:'MiB of RAM growth since the run started; 0 = off. Exceeding the limit stops unfinished work with partial results; unfinished required checks make the verdict inconclusive unless a check failed. Completed checks keep their results. The requested workload stays unchanged.'},
  max_requests:{help:'The most target requests Giraffe may use during the whole run, shared across all tests and endpoints. Some scheduled requests can use budget without reaching the server. The run can stop with fewer completed requests. AI evaluator calls have separate limits.'},
  max_duration_seconds:{help:'Overall time limit for test traffic, including connection, readiness and warm-up requests. When reached, Giraffe stops sending new requests and cancels all requests still in progress. Completed results are kept; interrupted checks may be inconclusive. AI evaluation and report saving happen afterward.'},
  request_timeout_seconds:{help:'How long a single request may take before Giraffe times it out. This covers waiting for the answer and receiving it. A shorter test-specific or remaining run deadline can stop it sooner.'},
  max_output_tokens:{help:'The most tokens Giraffe asks the model to generate for one request. A token is a small piece of text. The model can stop before reaching this number; the cap does not guarantee a long answer.'},
  buckets_levels:{help:'For 1, 4, 8, Giraffe tests one request at a time, then four together, then eight together. It repeats each load with every combination of your three question lengths and three answer caps. Each level must be no higher than Simultaneous limit in Run limits. Changing levels does not change that limit.'},
  buckets_samples_per_bucket:{help:'The minimum requests for each test with short, medium or long questions and answers, repeated at every concurrency level. Giraffe sends the larger of this number and the load being tested. For example, a minimum of 4 still sends 8 requests when testing 8 at a time.'},
  buckets_window_seconds:{help:'The time limit for each test with short, medium or long questions and answers, at one concurrency level. Waiting for an available request slot uses this time too. When time runs out, Giraffe records any missing coverage.'},
  buckets_input:{help:'The target question length in characters, including repeated background text. Giraffe keeps the labeled-box instruction intact, so very small questions and the longer-answer instruction can make the actual request longer. The request plan shows the characters sent for every combination.'},
  buckets_output:{help:'The maximum answer tokens requested for this case. The first answer case asks for a box label; the other two ask for the label followed by numbers. The server can finish before reaching the cap. Each case owns its answer cap; the suite value is a default.'},
  host_metrics_url:{hint:'Optional server host exporter address.',help:'An existing node_exporter Prometheus-text URL measures server-wide CPU and host RAM alongside the workload. Giraffe does not install an exporter. Inference and runtime metrics credentials are not forwarded to this URL.'},
  metrics_url:{hint:'Optional server metrics address.',help:'Use an existing Prometheus metrics URL, such as http://localhost:9400/metrics. Giraffe reads measurements from it; it does not install an exporter. Missing metrics remain unknown.'},
  mode:{help:'Use running server tests the server as it is. Restart one server first runs its configured restart command before testing. Choose this only when you intend to restart that server.'},
  restart_target:{help:'Only this endpoint’s configured restart command runs. Other endpoints keep running. A restart command must already be configured for the selected endpoint.'},
  baseline_id:{help:'Choose a previously saved run to compare with this one. Giraffe reports changes in errors, answers and timing, while still checking this run against its own acceptance settings. Leave this empty for a first run.'},
  baseline_name:{help:'Give this saved run a name you will recognize later, such as before-model-update. Saving a baseline does not change the run’s outcome.'},
  traffic_rates:{hint:'Requests per second, for example 1, 2, 4.',help:'Each number is a separate load stage, from lowest to highest. Giraffe starts new requests at that rate without waiting for previous answers. Requests it cannot start because its in-flight limit is full are counted as local drops.'},
  traffic_duration_seconds:{hint:'Seconds to spend at each rate.',help:'Keep sending new requests at each selected rate for this long. After each stage, Giraffe waits for started requests to finish, up to the completion wait limit.'},
  traffic_arrival:{hint:'Even spacing or uneven arrivals.',help:'Steady spreads requests evenly over time. Poisson varies the gaps between requests to simulate uneven traffic. The random seed lets you repeat the same arrival pattern.'},
  traffic_max_in_flight:{hint:'Leave blank to inherit this check’s default.',help:'The most unfinished requests this traffic check allows. An accepted value is not demonstrated delivery capacity. When all slots are occupied, extra arrivals count as dropped rather than queued.'},
  traffic_seed:{hint:'Use the same number to repeat a pattern.',help:'Controls the repeatable arrival pattern and request mix. Keeping this number the same makes runs easier to compare; it does not force the model to produce the same answers.'},
  traffic_long_input_chars:{hint:'Question length in characters.',help:'Size of the long questions used by built-in traffic tests. Characters and model tokens are different units. This setting is not used when testing your own questions.'},
  traffic_long_output_words:{hint:'Words to request in a long answer.',help:'Built-in long-answer requests ask the model to copy this many words. This check’s output token cap applies, and a model may stop early. This setting is not used for your own questions.'},
  traffic_drain_timeout_seconds:{hint:'Seconds to wait for remaining answers.',help:'After each traffic stage stops sending new requests, allow already-started requests this much time to finish.'},
  traffic_scheduler_lag_tolerance_ms:{hint:'Allowed delay when starting a request, in ms.',help:'If Giraffe starts a planned request too late, it records a load-generator limitation. This helps separate delays on your computer from delays in the LLM server.'},
  traffic_mix_short:{hint:'Relative share of short requests.',help:'These three weights control how often each request type is used. Weights of 2, 1, 1 mean roughly half short requests, a quarter long-question requests, and a quarter long-answer requests. They are shares, not request counts.'},
  traffic_mix_long_input:{hint:'Relative share of long questions.',help:'Increase this weight to send more long questions. The three weights are scaled to shares automatically. For example, 2, 1, 1 means about half short requests and a quarter of each long type.'},
  traffic_mix_long_output:{hint:'Relative share of long answers.',help:'Increase this weight to request more long answers. The three weights are scaled to shares automatically. This check’s output cap applies to each answer.'},
  limit_first_output_ms:{help:'The longest acceptable wait for the first useful answer text, in milliseconds. Reasoning text is measured separately. Leave blank if you do not want to require a particular first-answer time.'},
  limit_latency_ms:{help:'The longest acceptable time for a complete answer, in milliseconds. Leave blank if you do not want to require a particular response time.'},
  limit_stream_gap_ms:{help:'The longest acceptable pause between streamed answer chunks, in milliseconds. Network buffering can affect this measurement.'},
  limit_min_output_tokens_per_second:{help:'The minimum acceptable generation speed after the first answer begins, estimated in tokens per second. Single-token answers and responses with missing token usage cannot establish this speed.'},
  limit_fairness_max_ratio:{help:'How much slower a short answer may become when long requests run alongside it. A value of 2 allows up to twice the short-only response time. Use a number greater than 1.'},
  limit_max_error_rate:{help:'The largest allowed share of failed, timed-out or invalid responses. Enter a percentage from 0 to 100: 5 means 5%. Intentional cancellation probes are treated separately.'},
  limit_min_correctness:{help:'The smallest allowed share of correct answers among scored requests. Enter a percentage from 0 to 100: 95 means 95%. Missing or unscored answers can leave a test inconclusive.'},
  limit_regression_percent:{help:'How much a result may worsen compared with a saved baseline before Giraffe flags a material regression. Timing changes use percent; rate changes use percentage points. Too few comparable samples can leave the comparison inconclusive.'},
  limit_min_samples:{help:'The minimum number of usable measurements needed to judge a result. Fewer samples produce an inconclusive result rather than a pass.'},
  test_samples:{help:'How many sample requests this test should try. Some tests need extra requests to cover every case. Leave blank to use the run setting.'},
  test_concurrency:{help:'How many requests this test asks to run at the same time. An accepted value is not demonstrated delivery capacity. Leave blank to inherit this check’s default.'},
  test_context_limit:{help:'The context length this test should exercise, in tokens. Use the length configured on the server. Leave blank to use the run setting.'},
  test_max_output_tokens:{help:'The answer token cap for this test. The suite value is a default, not a ceiling. Leave blank to use the run setting.'},
  test_request_timeout_seconds:{help:'The deadline for a request in this test, in seconds. The suite value is a default, not a ceiling. Leave blank to use the run setting.'},
  test_sustained_seconds:{help:'How long to keep sending sustained traffic before checking whether the server recovers. Shared request and time limits can stop it sooner.'},
  test_metrics_max_age_seconds:{help:'How old a server metric may be before Giraffe treats it as stale. Missing or stale measurements cannot confirm healthy GPU behavior.'},
  test_backend_verification:{help:'Choose Required to check server logs or traces for proof that cancelled work stopped. It needs a supported runtime and the required logs or traces. A client disconnect alone does not prove the server stopped generation. Choose Off to skip this extra check.'},
  test_server_log:{help:'Path on the machine running Giraffe to the server log used for cancellation verification. This setting reads existing evidence; it does not turn on server logging.'},
  test_trace_file:{help:'Path on the machine running Giraffe to the request traces used for backend cancellation verification. Configure server tracing separately before requiring this check.'},
  test_cancel_after_ms:{help:'Deliberately cancel a streaming request after this many milliseconds, then check that the service can still answer new requests.'},
  test_deadline_probe_ms:{help:'Give a deliberate test request this short deadline, in milliseconds. Giraffe checks timeout behavior and whether the service recovers afterward.'},
  test_cancel_after_chunks:{help:'Cancel after receiving this many streamed answer chunks. Chunks are pieces sent by the server and can contain more than one token.'},
  test_cleanup_seconds:{help:'Seconds to allow the server to stop cancelled work before checking the result.'},
  test_trace_wait_seconds:{help:'Seconds to wait for cancellation evidence to appear in the configured logs or traces.'},
  judge_model:{help:'Exact model name for the AI evaluator. This model checks generated answers against your expected answers after target traffic finishes.'},
  judge_url:{help:'OpenAI-compatible API address of the AI evaluator. Questions, expected answers and generated answers are sent to this endpoint.'},
  judge_name:{help:'A unique name for this evaluator, used to identify its votes and errors in the results.'},
  judge_api_key_env:{hint:'Environment variable name, not the key itself.',help:'Enter the name of an environment variable containing the evaluator’s API key, such as JUDGE_API_KEY. Set it on the machine running Giraffe. Do not paste the secret key into this field.'},
  judging_concurrency:{help:'The most evaluator requests Giraffe may run at once. This is separate from target-test concurrency and does not affect inference timing. An accepted value is not demonstrated delivery capacity.'},
  judging_request_timeout_seconds:{help:'Seconds allowed for one evaluator response. An evaluator timeout leaves that vote missing; it is not treated as an incorrect target answer.'},
  judging_max_duration_seconds:{help:'Seconds allowed for the complete post-run AI evaluation. Unfinished evaluations remain inconclusive.'},
  judging_max_output_tokens:{help:'The maximum answer tokens allowed for each evaluator response. This cap applies to evaluator output, not the answers being tested.'},
  example_id:{help:'A unique name for this question, used to find it in results and comparisons. Keep the same ID when comparing later runs of the same question.'},
  example_weight:{hint:'A larger number uses this question more often.',help:'Controls how often this question appears in arrival traffic compared with other questions. A weight of 2 gives it about twice the share of a question weighted 1. It does not make the question count twice when scoring an individual answer.'},
  example_max_tokens:{hint:'Leave blank to use the run’s output cap.',help:'Maximum answer tokens to request for this question. The suite value is a default, not a ceiling.'},
  example_scorer:{help:'AI evaluation compares meaning against your expected answer. Exact match compares text, including punctuation, after normalizing whitespace. Contains checks for the expected text inside the answer. JSON checks its structure and optional reference value. No answer check sends traffic without judging correctness.'},
  example_expected:{help:'The answer you want the model to give. AI evaluation accepts answers with the same meaning. Exact match also requires matching punctuation and wording. For JSON checks, enter a JSON value; leaving it blank checks only the schema.'},
  example_schema:{help:'A JSON Schema describing the allowed answer structure, field types and required fields. The answer must parse as JSON and match this schema.'},
  example_rubric:{help:'Optional guidance for the evaluator, such as acceptable wording or how to handle missing details. The question, expected answer and generated answer are still included.'},
  example_options:{help:'Extra request settings as a JSON object, such as {"temperature": 0}. Giraffe keeps control of the model, messages and safety limits.'},
  message_content:{help:'Text sent to the model. For a conversation, add messages in order. Giraffe uses the full saved conversation when testing this question.'},
  message_role:{help:'User is a question or instruction. Assistant is a previous model reply. System is guidance for how the model should behave.'},
  input_field:{help:'Choose the column that contains each question or saved conversation. Review the import preview before using the dataset.'},
  expected_field:{help:'Choose the column that contains the expected answer for each question.'},
  import_scorer:{help:'Choose how imported answers will be checked. AI evaluation compares their meaning; exact match compares their text. You can change the check for individual questions after import.'},
  dataset_bulk_scorer:{help:'Apply the chosen check to every selected question. Choosing No answer check also removes their saved expected answers.'},
  try_target:{help:'Send this one question to the selected endpoint. Other endpoints are not used for this preview.'},
  config_editor:{help:'The same run settings as the form, with extra options for multiple endpoints, server metadata and request settings. Apply changes before running. Unapplied edits lock the form so the two views cannot silently disagree.'},
};
function fieldDescription(name) {
  const key=name.replace(/^judge_\d+_/,'judge_').replace(/^message_\d+_/,'message_').replace(/^buckets_(input|output)_(short|medium|long)$/,'buckets_$1');
  return fieldDescriptions[key] || {};
}
function infoHelp(name,label,help,details='') {
  if(!help)return '';
  const id=`field-${name}`;
  return `<button type="button" class="field-info" id="${escape(id)}-info" popovertarget="${escape(id)}-help" aria-label="About ${escape(label)}" aria-describedby="${escape(id)}-help-text"><span aria-hidden="true">i</span></button><div id="${escape(id)}-help" class="field-help" popover="auto" data-help-for="${escape(id)}-info" role="note"><strong>${escape(label)}</strong><p id="${escape(id)}-help-text">${escape(help)}</p>${details}</div>`;
}
function settingDestination(path) {
  const parts=String(path).split('.'),root=parts[0];
  if(parts[1]==='levels')parts.splice(2);
  let name=path,check=null,step=2;
  if(['checks','request_pack','judging'].includes(path))return {name:path,label:{checks:'checks',request_pack:'questions',judging:'AI evaluator'}[path],step};
  if(['samples','max_output_tokens','context_limit','default_concurrency'].includes(path))return {name:'config_editor',label:human(path).toLowerCase(),step};
  const sections={load_test:'load',concurrent_load:'concurrency',sessions:'sessions',consistency:'consistency',arrivals:'arrivals',prefix:'prefix',traffic:'capacity',tool_checks:'tools'};
  if(parts.length===1&&sections[root]||root==='test_options'&&parts.length===2)return {section:true,check:sections[root]||parts[1],step,label:root==='test_options'?`${human(parts[1]).toLowerCase()} settings`:`${human(sections[root]).toLowerCase()} settings`};
  if(root==='stream'){step=1;}
  else if(root==='retention'){step=3;}
  else if(root==='targets'){if(parts[1]!=='0')return {name:'config_editor',label:'advanced configuration'};name=parts[2]==='tokenizer'&&$('#new-run-form')?.elements.tokenizer?.value==='custom'?'tokenizer_custom':parts[2];step=1;}
  else if(root==='load_test'){
    check='load';
    if(parts[1]==='migrated_tests')return {name:'config_editor',step,label:'imported load settings'};
    if(parts[1]==='patterns'&&parts[2]==='concurrent'&&parts[3]==='levels')parts.splice(4);
    if(parts[1]==='patterns'&&parts[2]==='rates')parts.splice(3);
    if(parts[1]==='workloads'){
      if(parts.length===2)parts.push('combinations','enabled');
      if(parts.length===3)parts.push('enabled');
      if(parts[3]==='selected')parts.splice(4,parts.length-4,'short_short');
      if(parts[2]==='stress'&&parts[3]==='cases')parts.splice(4,parts.length-4,'long_short');
      if(parts[2]==='mixed'&&parts[3]==='shares'&&parts.length===4)parts.splice(3,1,'share_mode');
    }
    name=loadFieldName(parts.join('.'));
    return {name,check,step,label:loadFieldLabel(parts.slice(1).join('.'))};
  }
  else if(root==='test_options'){check=parts[1];name=check==='correctness'&&state.config?.request_source==='request_pack'&&parts[2]!=='limits'?'request_pack_correctness':`test_${check}_${parts.slice(2).join('_').replace('limits_','limit_')}`;}
  else if(root==='sessions'&&parts[1]==='workloads'&&parts.length===2)return {section:true,check:'sessions',step,label:'Conversation sessions'};
  else if(root==='sessions'&&parts[1]==='workloads'){
    check='sessions';const form=$('#new-run-form'),count=Number(form?.elements?.session_workload_count?.value),selected=[];
    for(let index=0;index<count;index++)if(form.elements[`session_enabled_${index}`]?.checked)selected.push(index);
    const index=selected[Number(parts[2])]??parts[2];
    name=parts[3]==='turns'?`session_turn_${index}_${parts[4]||0}`:`session_${parts[3]==='max_tokens'?'cap':parts[3]}_${index}`;
  }
  else if(root==='sessions'&&($('#new-run-form')?.elements?.sessions_profile?.value||state.config?.sessions?.profile)==='performance'){
    name={max_tokens:'profile_sessions_max_tokens',sessions:'sessions_count',delay_seconds:'sessions_delay',concurrency:'profile_sessions_concurrency'}[parts[1]]||`profile_sessions_${parts[1]}`;check=name==='concurrency'?null:'sessions';
  }
  else if(['arrivals','prefix','sessions','consistency'].includes(root)){check=root;name=`profile_${root}_${parts[1]}`;}
  else if(root==='traffic'){check='capacity';name=`traffic_${parts[1]}`;}
  else if(root==='tool_checks'){check='tools';name=`profile_tools_${parts[1]}`;}
  else if(root==='limits'){name=`limit_${parts[1]}`;if(parts[1]==='regression_percent')step=3;}
  else if(root==='judging')name=parts[1]==='judges'?`judge_${parts[2]}_${parts[3]}`:`judging_${parts[1]}`;
  else if(root==='config')name='config_editor';
  const labels={max_memory_mb:'Maximum memory',concurrency:'Simultaneous limit',max_duration_seconds:'Maximum duration',request_timeout_seconds:'request timeout',max_output_tokens:'output cap',max_requests:'Maximum requests',load_schedule_levels:'concurrency levels',load_schedule_duration_seconds:'send new requests for',load_combinations_enabled:'load types',load_combinations_selected_short_short:'combinations',load_mixed_shares_short_short:'mixed request shares',load_interference_enabled:'stream interference',load_stress_large_input:'stress cases',traffic_rates:'rates',traffic_duration_seconds:'seconds per rate',traffic_max_in_flight:'capacity requests at once',config_editor:'advanced configuration',sessions_max_tokens:'output cap per turn',sessions_count:'total conversations',sessions_delay:'pause between turns'};
  if(name.startsWith('session_cap_'))return {name,check,step,label:'task output cap'};
  return {name,check,step,label:labels[name]||human(name.replace(/^load_|^profile_|^test_/,'')).toLowerCase()};
}
function runLimitIssues(config) {
  const issues=[];
  for(const [name,label] of [['concurrency','Simultaneous limit'],['max_duration_seconds','Maximum duration'],['max_requests','Maximum requests']]){
    const value=config[name];if(value!=null&&(typeof value!=='number'||!Number.isFinite(value)||value<=0||(name!=='max_duration_seconds'&&!Number.isInteger(value))))issues.push({fields:[name],msg:`Enter ${name==='max_duration_seconds'?'a number':'a whole number'} greater than 0 for ${label}.`});
  }
  const saved=state.requestPreview?.config?state.requestPreview:state.bootstrap?.request_plan?{plan:state.bootstrap.request_plan,config:state.bootstrap.config}:null;
  // Reuse the server's expansion when only global limits change. A changed
  // workload waits for its new preview; no second count/time formula lives here.
  const plan=saved?.plan?.load_test;
  if(config.checks?.includes('load')&&plan&&sameConfig(loadSettings(config),loadSettings(saved.config))){
    if(config.max_duration_seconds!=null&&plan.time_with_allowances_seconds>config.max_duration_seconds)issues.push({blocking:false,fields:['max_duration_seconds'],msg:'The configured windows and allowances exceed Maximum duration. Increase it or reduce this plan.'});
    if(config.max_requests!=null&&plan.expected_scheduled_requests*(config.targets?.length||1)>config.max_requests)issues.push({blocking:false,fields:['max_requests'],msg:'Expected scheduled requests exceed the total request budget.'});
    if(config.max_requests!=null&&plan.minimum_initial_requests_per_target*(config.targets?.length||1)>config.max_requests)issues.push({fields:['max_requests'],msg:'The request budget cannot cover even the initial concurrent groups.'});
  }
  return issues;
}
function localSettingErrors(config=null) {
  if(hasJsonChanges())return [];
  try {config ||= readForm(true).config;return [...loadTestIssues(config,loadFormDefaults(),state.bootstrap.limits),...runLimitIssues(config).filter(issue=>issue.blocking!==false),...positiveBoundErrors(config,state.bootstrap.limits,state.bootstrap.cap_explanations).filter(issue=>!issue.fields?.some(path=>path.startsWith('load_test.')))];}catch{return [];}
}
function localSettingWarnings() {
  if(hasJsonChanges())return [];
  try{return runLimitIssues(readForm(true).config).filter(issue=>issue.blocking===false);}catch{return [];}
}
function currentSettingErrors() {
  if(hasJsonChanges()){const saved=state.jsonValidation;return saved?.draft===state.jsonDraft&&saved.revision===state.configRevision&&saved.route===state.routeVersion?saved.errors:[];}
  const saved=state.requestPreview,server=saved?.revision===state.configRevision&&saved.route===state.routeVersion?(saved.status==='error'?saved.errors||[]:saved.status==='current'?saved.plan?.errors||[]:[]):[];
  return [...localSettingErrors(),...server].filter((issue,i,all)=>all.findIndex(row=>row.msg===issue.msg&&JSON.stringify(row.fields)===JSON.stringify(issue.fields))===i);
}
function currentResourceErrors() {
  return (currentRequestPlan()?.resource_issues || []).filter(issue=>issue.blocking);
}
function settingActions(fields) {
  return [...new Set(fields)].map(path=>`<button type="button" class="text-button" data-action="edit-setting" data-setting-path="${escape(path)}">Edit ${escape(settingDestination(path).label)}</button>`).join(' ');
}
function concurrencySetting(check) {
  return {capacity:'traffic.max_in_flight',arrivals:'arrivals.max_in_flight',sessions:'sessions.concurrency',consistency:'consistency.levels',load:'load_test.patterns.concurrent.levels'}[check]||`test_options.${check}.concurrency`;
}
function inheritedConcurrencyNotice(issue) {
  return issue.check&&Number.isFinite(issue.effective_concurrency);
}
function concurrencyNoticeActions(issue) {
  if(issue.check==='correctness'&&state.config?.request_source==='request_pack')return `<button type="button" class="text-button" data-action="edit-setting" data-setting-path="concurrency">Raise limit</button><button type="button" class="text-button" data-action="edit-setting" data-setting-path="default_concurrency">Edit default concurrency (JSON)</button>`;
  return `<button type="button" class="text-button" data-action="edit-setting" data-setting-path="concurrency">Raise limit</button><button type="button" class="button small" data-action="use-check-concurrency" data-concurrency-check="${escape(issue.check)}" data-concurrency="${escape(issue.effective_concurrency)}">Use ${number(issue.effective_concurrency)} for ${escape(catalogTitle(issue.check))}</button>`;
}
function issueFields(issue) {
  return [...new Set([...(issue.fields||[issue.field||'config']),...(issue.check?[concurrencySetting(issue.check)]:[])])];
}
function renderSettingIssues(errors=currentSettingErrors(),kind='error') {
  return errors.map(error=>{
    const actions=inheritedConcurrencyNotice(error)?concurrencyNoticeActions(error):error.actions?.map(action=>Object.hasOwn(action,'value')&&['max_requests','max_duration_seconds'].includes(action.field)
      ?`<button type="button" class="button small" data-action="${action.field==='max_requests'?'use-request-budget':'use-time-budget'}" data-budget="${escape(action.value)}">${escape(action.label)}</button>`
      :Object.hasOwn(action,'value')&&action.field==='traffic.duration_seconds'
        ?`<button type="button" class="button small" data-action="use-setting-correction" data-setting-path="${escape(action.field)}" data-setting-value="${escape(action.value)}">${escape(action.label)}</button>`
        :action.fields?settingActions(action.fields):!Object.hasOwn(action,'value')&&action.field?.startsWith('load_test.')?settingActions([action.field]):`<button type="button" class="text-button" data-action="edit-setting" data-setting-path="${escape(action.field)}">${escape(action.label)}</button>`).join(' ') ?? settingActions(error.fields||[error.field||'config']);
    return `<div class="notice ${kind} setting-issue" ${kind==='error'?'role="alert"':''}><p>${escape(error.msg)}</p>${error.note?`<p class="hint">${escape(error.note)}</p>`:''}<div class="actions">${actions}</div></div>`;
  }).join('');
}
function syncInlineErrors() {
  const messages=new Map();
  for(const error of [...currentSettingErrors(),...currentResourceErrors()])for(const path of error.fields||[error.field||'config']){
    const {name}=state.configView==='json'?{name:'config_editor'}:settingDestination(path);messages.set(name,[...(messages.get(name)||[]),[error.msg,error.note].filter(Boolean).join(' ')]);
  }
  for(const name of new Set([...(state.inlineErrorFields||[]),...messages.keys()])){
    const input=name==='config_editor'?$('#config-editor'):$(`#field-${name}`),message=$(`#field-${name}-error`);if(!input||!message)continue;
    const text=[...new Set(messages.get(name)||[])].join(' ');message.textContent=text;message.hidden=!text;
    if(text){
      for(let details=input.closest?.('details');details;details=details.parentElement?.closest?.('details'))details.open=true;
      if(input.dataset.validationDescription===undefined)input.dataset.validationDescription=input.getAttribute?.('aria-describedby')||'';
      input.setAttribute('aria-invalid','true');input.setAttribute('aria-describedby',[input.dataset.validationDescription,`field-${name}-error`].filter(Boolean).join(' '));
    }else{
      input.removeAttribute('aria-invalid');
      if(input.dataset.validationDescription)input.setAttribute('aria-describedby',input.dataset.validationDescription);else input.removeAttribute('aria-describedby');
      delete input.dataset.validationDescription;
    }
  }
  state.inlineErrorFields=[...messages.keys()];
  const warnings=new Map(),plan=currentRequestPlan();
  for(const warning of [...localSettingWarnings(),...(plan?.load_warnings || []),...(plan?.concurrency_warnings || []),...(plan?.capacity_warnings || []),...(plan?.time_warnings || []),...(plan?.request_warnings || []),...(plan?.resource_warnings || [])])for(const path of issueFields(warning)){
    const {name}=settingDestination(path);warnings.set(name,[...(warnings.get(name) || []),warning]);
  }
  for(const name of new Set([...(state.inlineWarningFields || []),...warnings.keys()])){
    const input=name==='config_editor'?$('#config-editor'):$(`#field-${name}`),message=$(`#field-${name}-warning`);if(!input||!message)continue;
    const rows=warnings.get(name) || [],id=`field-${name}-warning`;
    message.innerHTML=rows.map(row=>`${escape(row.msg)} ${inheritedConcurrencyNotice(row)?concurrencyNoticeActions(row):settingActions(row.fields || [])}`).join(' ');message.hidden=!rows.length;
    const described=(input.getAttribute('aria-describedby') || '').split(/\s+/).filter(value=>value&&value!==id);
    if(rows.length)described.push(id);
    if(described.length)input.setAttribute('aria-describedby',described.join(' '));else input.removeAttribute('aria-describedby');
  }
  state.inlineWarningFields=[...warnings.keys()];
  for(const element of $$('[data-check-runtime-warning]')){const notices=(plan?.concurrency_warnings||[]).filter(issue=>issue.check===element.dataset.checkRuntimeWarning);element.innerHTML=renderSettingIssues(notices,'warning');element.hidden=!notices.length;}
}
function focusSetting(path) {
  if(hasJsonChanges()&&currentSettingErrors().length){const input=$('#config-editor');input?.scrollIntoView?.({block:'center'});input?.focus?.();return;}
  assertAppliedConfig();const destination=settingDestination(path),{name,check}=destination;
  state.returnToReview ||= state.setupStep===3;
  if(['checks','request_pack','judging'].includes(path)){
    revealSetupStep(2);
    if(path==='judging'){configureEvaluator();return;}
    if(path==='request_pack')revealQuestionEditor();else{state.catalogueFilter='all';syncTestControls();}
    const input=path==='request_pack'?$('[data-action="add-example"]'):$('#check-search');input?.scrollIntoView?.({block:'center'});input?.focus?.({preventScroll:true});return;
  }
  const form=$('#new-run-form');let input=form?.elements?.[name]||$(`#field-${name}`);
  if(input?.disabled&&form?.elements?.[`${name}_enabled`])input=form.elements[`${name}_enabled`];
  if(destination.section){
    revealSetupStep(destination.step);revealCatalogueCheck(check);input=$(`#settings-${check} [data-editor-title]`)||$(`#settings-${check}`);input?.setAttribute?.('tabindex','-1');
  }else if(!input||name==='config_editor'){
    switchConfigView('json');input=$('#config-editor');
  }else{
    revealSetupStep(destination.step);if(check)revealCatalogueCheck(check);else if(state.openTest)toggleTestEditor(state.openTest,true);
    if(check==='load'){
      const type=String(path).split('.')[2];if(Object.hasOwn(loadTitles,type))setLoadPanel(type,true);
      if(name==='load_patterns_concurrent_levels'&&form?.elements?.load_workloads_interference_enabled?.checked)setLoadPanel('interference',true);
    }
    for(let parent=input.parentElement;parent;parent=parent.parentElement){if(parent.tagName==='DETAILS')parent.open=true;if(parent.hidden&&parent.closest?.('[data-load-settings]'))parent.hidden=false;}
  }
  for(const field of $$('[data-setting-highlight]'))field.removeAttribute('data-setting-highlight');
  input?.setAttribute?.('data-setting-highlight','true');
  const target=input?.closest?.('.form-field')||input,footerHeight=$('.setup-navigation')?.getBoundingClientRect?.().height||0;
  if(target?.style)target.style.scrollMarginBottom=`${Math.max(160,footerHeight+32)}px`;
  target?.scrollIntoView?.({block:'center'});input?.focus?.({preventScroll:true});
}
function focusFirstSettingError(error=null) {
  if(hasJsonChanges())return;
  const errors=error?.errors?.length?error.errors:currentSettingErrors();
  if(errors.length)focusSetting(errors[0].fields?.[0]||errors[0].field||'config');
}
function renderPlanTime(plan) {
  if(!plan||!Object.hasOwn(plan,'estimated_seconds'))return '';
  const low=plan.estimated_low_seconds,high=plan.estimated_high_seconds;
  const duration=value=>value<60?`${new Intl.NumberFormat('en',{maximumSignificantDigits:2}).format(value)} s`:elapsed(value);
  const lower=low==null?null:duration(low),upper=high==null?null:duration(high);
  const estimate=lower==null||upper==null?'awaiting comparable measurements':`about ${lower}${lower===upper?'':`–${upper}`}`;
  const assumptions=[...(plan.confidence_details?.insufficient_observations||[]),...(plan.time_estimate_assumptions||[])];
  return `<div class="plan-time"><p>Estimated duration: ${escape(estimate)} · test traffic only</p><p class="hint">Confidence: ${escape(plan.confidence||'low')}. Excludes ${(plan.estimate_exclusions||['setup/connection','AI evaluation','report generation']).map(escape).join(', ')}.</p>${(plan.time_bound_reasons||[]).length?`<div class="unknown-time-components"><p class="hint">Additional work at unknown speed:</p><ul>${plan.time_bound_reasons.map(reason=>`<li>${escape(reason)}</li>`).join('')}</ul></div>`:''}${assumptions.length?`<details class="estimate-assumptions"><summary>Estimate assumptions</summary><ul>${assumptions.map(reason=>`<li>${escape(reason)}</li>`).join('')}</ul></details>`:''}${renderSettingIssues([...(plan.time_warnings||[]),...(plan.request_warnings||[]),...(plan.resource_warnings||[]),...(plan.concurrency_warnings||[]),...(plan.capacity_warnings||[])],'warning')}${renderSettingIssues((plan.resource_issues||[]).filter(issue=>issue.blocking))}</div>`;
}
function renderField(name,label,control,{hint,help,helpKey=name,hintAlways=false,full=false,wrapperId='',controlId=`field-${name}`,hintId=`field-${name}-hint`,hiddenLabel=false,hideInfo=false,helpDetails=''}={}) {
  const copy=fieldDescription(helpKey);hint ??= copy.hint || '';help ??= copy.help || '';
  help=[help,state.bootstrap?.cap_explanations?.[settingPath(name)]?.help].filter(Boolean).join(' ');
  return `<div class="form-field ${full?'full':''}" ${wrapperId?`id="${escape(wrapperId)}"`:''}><div class="field-label"><label ${hiddenLabel?'class="sr-only" ':''}for="${escape(controlId)}">${escape(label)}</label>${hideInfo?'':infoHelp(name,label,help,helpDetails)}</div>${control}${hint||hintAlways?`<span class="hint" id="${escape(hintId)}">${escape(hint)}</span>`:''}<span class="field-error" id="field-${escape(name)}-error" hidden></span><span class="hint field-warning" id="field-${escape(name)}-warning" hidden></span></div>`;
}
function loadSettingPath(name) {
  const paths=['request_timeout_seconds','patterns.concurrent.enabled','patterns.concurrent.levels','patterns.constant.enabled','patterns.poisson.enabled','patterns.poisson.seed','patterns.rates','patterns.bursts.enabled','patterns.bursts.size','patterns.bursts.first_seconds','patterns.bursts.gap_seconds','schedule.duration_seconds','schedule.finish_seconds'];
  for(const size of loadSizes)for(const key of ['input_tokens','output_tokens'])paths.push(`profile.${size}.${key}`);
  for(const type of Object.keys(loadTitles))paths.push(`workloads.${type}.enabled`);
  paths.push('workloads.mixed.share_mode','workloads.interference.trigger','workloads.interference.max_slowdown','workloads.stress.min_output_percent');
  for(const kind of ['combinations','mixed'])for(const shape of loadShapes)paths.push(`workloads.${kind}.selected.${shape}`);
  for(const shape of loadShapes)paths.push(`workloads.mixed.shares.${shape}`);
  for(const shape of ['long_short','short_long','long_long'])paths.push(`workloads.stress.cases.${shape}`);
  for(const key of Object.keys(loadCriteriaLabels))paths.push('pass_criteria.'+key);
  const path=paths.find(path=>loadFieldName(path)===name);return path?'load_test.'+path:name;
}
function settingPath(name) {
  const check=state.bootstrap?.checks?.find(check=>name.startsWith(`test_${check.id}_`));
  if(check)return `test_options.${check.id}.${name.slice(check.id.length+6).replace(/^limit_/,'limits.')}`;
  if(name.startsWith('load_'))return loadSettingPath(name);
  const profile=name.match(/^profile_(arrivals|prefix|sessions|consistency|tools)_(.+)$/);
  if(profile)return `${profile[1]==='tools'?'tool_checks':profile[1]}.${profile[2]}`;
  const aliases={sessions_count:'sessions.sessions',sessions_delay:'sessions.delay_seconds',sessions_max_tokens:'sessions.max_tokens',example_max_tokens:'request_pack.cases[].max_tokens',example_weight:'request_pack.cases[].weight'};
  if(aliases[name])return aliases[name];
  if(/^session_turn_/.test(name))return 'sessions.workloads[].turns[]';
  if(/^session_(name|weight|cap)_/.test(name))return 'sessions.workloads[].'+({name:'name',weight:'weight',cap:'max_tokens'})[name.split('_')[1]];
  if(name.startsWith('traffic_'))return 'traffic.'+name.slice(8).replace(/^mix_/,'mix.');
  if(name.startsWith('judging_'))return 'judging.'+name.slice(8);
  if(name.startsWith('limit_'))return 'limits.'+name.slice(6);
  return name;
}
function settingBounds(name) {
  const path=settingPath(name),bounds=state.bootstrap?.limits?.[path]||{};
  const scale=/^test_.*_limit_(min_correctness|max_error_rate)$/.test(name)?100:1;
  return {...bounds,minimum:bounds.minimum==null?undefined:bounds.minimum*scale,maximum:bounds.maximum==null?undefined:bounds.maximum*scale};
}
function field(name,label,value,{type='text',hint,help,helpKey=name,min,max,step,placeholder='',required=false,hintAlways=false,full=false,hiddenLabel=false,hideInfo=false}={}) {
  const bounds=settingBounds(name);
  min=bounds.minimum ?? (bounds.exclusiveMinimum==null?undefined:bounds.type==='integer'?Math.floor(bounds.exclusiveMinimum)+1:bounds.exclusiveMinimum+Math.max(Number.MIN_VALUE,Math.abs(bounds.exclusiveMinimum)*Number.EPSILON));max=bounds.maximum;

  const description=hint ?? fieldDescription(helpKey).hint;
  const control=`<input id="field-${escape(name)}" name="${escape(name)}" type="${type}" value="${escape(value ?? '')}" ${required?'required':''} ${min!==undefined?`min="${min}"`:''} ${max!==undefined?`max="${max}"`:''} ${step!==undefined?`step="${step}"`:''} placeholder="${escape(placeholder)}" autocomplete="off" ${description||hintAlways?`aria-describedby="field-${escape(name)}-hint"`:''}>`;
  return renderField(name,label,control,{hint,help,helpKey,hintAlways,full,hiddenLabel,hideInfo});
}
function selectField(name,label,options,{hint,help,helpKey=name,wrapperId='',controlId=`field-${name}`,helpDetails=''}={}) {
  const description=hint ?? fieldDescription(helpKey).hint;
  const control=`<select id="${escape(controlId)}" name="${escape(name)}" ${description?`aria-describedby="field-${escape(name)}-hint"`:''}>${options}</select>`;
  return renderField(name,label,control,{hint,help,helpKey,wrapperId,controlId,helpDetails});
}
function textField(name,label,value,{rows=4,maxLength,hint,help,helpKey=name,wrapperId='',hintId=`field-${name}-hint`}={}) {
  const description=hint ?? fieldDescription(helpKey).hint;
  const control=`<textarea id="field-${escape(name)}" name="${escape(name)}" rows="${rows}" ${maxLength?`maxlength="${maxLength}"`:''} ${description?`aria-describedby="${escape(hintId)}"`:''} spellcheck="false">${escape(value ?? '')}</textarea>`;
  return renderField(name,label,control,{hint,help,helpKey,wrapperId,hintId});
}
function positionFieldHelp(popover) {
  const trigger=document.getElementById(popover.dataset.helpFor);if(!trigger)return;
  const bounds=trigger.getBoundingClientRect(),gutter=12,width=Math.min(360,document.documentElement.clientWidth-gutter*2);
  popover.style.width=`${width}px`;
  const height=popover.getBoundingClientRect().height;
  popover.style.left=`${Math.max(gutter,Math.min(bounds.left,document.documentElement.clientWidth-width-gutter))}px`;
  popover.style.top=`${bounds.bottom+8+height<=window.innerHeight-gutter?bounds.bottom+8:Math.max(gutter,bounds.top-height-8)}px`;
}
function baselineOptions(selected='') {return `<option value="">No baseline · first or standalone run</option>${state.baselines.map(b=>`<option value="${escape(b.id)}" ${b.id===selected?'selected':''}>${escape(b.name)} · ${statusLabels[b.overall] || b.overall || 'saved'} · ${date(b.saved_at)}</option>`).join('')}`;}
function schemaField(name,value,schema,{label,hint,help,helpKey=name,placeholder='Use run setting'}={}) {
  const nullable=schema.anyOf?.some(item=>item.type==='null');
  const valueSchema=schema.anyOf?.find(item=>item.type!=='null') || schema;
  label ||= schema.title || human(name);
  if(valueSchema.enum)return selectField(name,label,valueSchema.enum.map(item=>`<option value="${escape(item)}" ${item===value?'selected':''}>${escape(item==='poisson'?'Uneven (Poisson)':item==='steady'?'Even spacing':human(item))}</option>`).join(''),{hint,help,helpKey});
  if(valueSchema.type==='array')return field(name,label,(value || []).join(', '),{hint,help,helpKey,required:true});
  if(valueSchema.type==='string')return field(name,label,value,{hint,help,helpKey,required:!nullable,placeholder});
  return field(name,label,value,{type:'number',hint,help,helpKey,step:valueSchema.type==='integer'?1:'any',required:!nullable,placeholder:nullable?placeholder:''});
}
const checkGroups=[
  {id:'answers',title:'Answers',blurb:'Is the output right?',checks:['access','serving','correctness','json','context','tools']},
  {id:'speed',title:'Speed & load',blurb:'How fast, and under how much load?',checks:['first_output','generation','load']},
  {id:'reliability',title:'Reliability',blurb:'Does it hold up over time?',checks:['recovery','sessions','consistency']},
  {id:'server',title:'Diagnostics',blurb:'Evidence from the server itself.',checks:['cancellation','prefix','gpu']},
];
const checkGuide={
  load:{name:'Load test',purpose:'Workloads, request rates and bursts in one place.',sends:'Selected workloads under maintained concurrency, arrivals and bursts.',shows:'Delivery, per-response criteria and highest tested accepted load.'},
  access:{name:'Endpoint response',purpose:'Can this endpoint return an answer?',sends:'One short generated request, shared with timing checks.',shows:'Whether the endpoint, authentication and model produce a valid answer.'},
  serving:{name:'Invalid responses',purpose:'Look for errors, empty answers and incomplete responses.',sends:'Repeated short requests; also inspects responses from the selected checks.',shows:'Invalid responses, timeouts and server errors against your allowed error rate.'},
  correctness:{name:'Answer correctness',purpose:'Check known answers using built-in examples or your own questions.',sends:'Generated extraction, arithmetic and classification questions, or each of your saved questions.',shows:'Correct answers among scored responses. Missing answers stay visible.'},
  json:{name:'JSON correctness',purpose:'Check that JSON answers have the right fields, types and values.',sends:'Generated questions requesting a defined JSON structure.',shows:'Valid JSON, required fields, expected types and known values.'},
  context:{name:'Context recall',purpose:'Find out whether facts survive inside long questions.',sends:'Known facts placed at different positions and lengths in generated questions.',shows:'Recall at the exercised lengths. Use your server’s configured context limit.',needs:'Check the server context limit'},
  tools:{name:'Tool-call correctness',purpose:'Check tool calls and any required assistant text. Nothing is run.',sends:'Configured fixtures can require none, auto, required or a named tool. The built-in weather check uses automatic selection. Optional legacy forced-choice diagnostics run separately.',shows:'Separate protocol, choice compliance, argument schema, task and assistant text outcomes against configured expectations.',needs:'Tool-capable model and endpoint'},
  first_output:{name:'First answer',purpose:'Measure how long users wait before useful answer text arrives.',sends:'One initial request and repeated short requests, shared with response-health checks.',shows:'Initial and subsequent answer delays, separate from reasoning text.',needs:'Streaming enabled'},
  generation:{name:'Generation',purpose:'Measure completion speed and interruptions in streamed answers.',sends:'Repeated requests to reproduce a generated passage.',shows:'Response time, stream pauses and generation rate when usage data supports it.',needs:'Streaming for pause measurements'},
  capacity:{name:'Capacity under load',purpose:'See when increasing request rates causes slow, incorrect or dropped answers.',sends:'New requests at each configured rate, using generated examples or your own questions.',shows:'Correct answers, latency and drops at each tested rate. Set a time target to judge acceptable capacity for this workload.'},
  concurrency:{name:'Concurrent load',purpose:'Compare request shapes and interference at sustained concurrency levels.',sends:'Selected load types replenish individual request slots for each configured sending window.',shows:'A verdict per test point, delivered and evidenced concurrency, timing, throughput and actual token lengths.'},
  buckets:{name:'Request sizes',purpose:'Compare short and long questions and answers at different loads.',sends:'Nine question-length and answer-cap combinations at every concurrency level.',shows:'Response quality and timing for each size and load. Caps do not guarantee answer length.'},
  arrivals:{name:'Arrival traffic',purpose:'Compare steady traffic with sudden bursts.',sends:'A fixed number of scheduled requests, evenly spaced or grouped into bursts.',shows:'Actual start times, queueing and requests left undispatched by limits.'},
  fairness:{name:'Stream fairness',purpose:'See if long questions interrupt a short answer already streaming.',sends:'Short responses with and without competing long questions.',shows:'Slowdown and pauses during competing work; long requests start only while the short answer streams.',needs:'Streaming and at least 2 in flight'},
  mixed:{name:'Short-request slowdown',purpose:'See if long requests slow down short requests beside them.',sends:'Short requests alongside a configurable mix of long-question and long-answer requests.',shows:'How short requests behave with and without heavy neighboring work.'},
  recovery:{name:'Recovery after load',purpose:'Check whether normal responses return after sustained traffic.',sends:'Sustained traffic followed by bounded health-check requests.',shows:'Post-load errors, correctness and response timing. Shared limits can shorten the load period.'},
  sessions:{name:'Concurrent conversations',purpose:'Check several multi-turn conversations at the same time.',sends:'Conversation turns with prepared history, earlier model replies, or both.',shows:'Completion and turn behavior across concurrent conversations.'},
  consistency:{name:'Answer consistency',purpose:'Check that repeated questions get matching answers under load.',sends:'Repeated generated questions at the chosen sizes and loads.',shows:'Whether these answers agree under exact or whitespace-normalized comparison.'},
  cancellation:{name:'Cancellation & timeouts',purpose:'Check stopped requests, output limits and service health afterward.',sends:'Output/stop controls, intentional cancellation, short deadlines and follow-up health requests.',shows:'Client behavior and recovery. Server-side proof needs supported logs or traces.'},
  prefix:{name:'Prompt cache evidence',purpose:'See if the server reuses work when prompts start the same way.',sends:'Requests with shared opening text and conversation history, plus comparisons.',shows:'Cache evidence when available. Faster responses alone do not prove cache reuse.'},
  gpu:{name:'GPU metrics',purpose:'Read GPU stats your server already publishes.',sends:'Reads your metrics URL; adds no inference requests.',shows:'Available GPU measurements and their freshness. Missing data remains unknown.',needs:'Existing metrics endpoint'},
};
function profileDefaults(key,config=state.config) {return effectiveProfileDefaults(config,state.bootstrap.profile_defaults || {},key);}
function catalogTitle(id) {return checkGuide[id]?.name || human(id);}
function renderProfileEditor(check,config) {
  const key=check.id;
  if(key==='load')return renderLoadEditor(config);
  if(key==='tools'){
    const profile=config.tool_checks || {fixtures:[],modes:null},selected=profile.modes?.join(',') || '';
    const modes=[['','Run default'],['complete','Complete responses'],['stream','Streaming responses'],['complete,stream','Complete and streaming']];
    if(!modes.some(([value])=>value===selected))modes.push([selected,'Saved selection']);
    const fixtures=typeof profile.fixtures==='string'?profile.fixtures:JSON.stringify(profile.fixtures || [],null,2);
    const conversations=typeof profile.conversations==='string'?profile.conversations:JSON.stringify(profile.conversations || [],null,2);
    return `<input type="hidden" name="profile_edits_tools" value="[]"><div data-profile-settings="tools" class="fields">${selectField('profile_tools_modes','Response modes',modes.map(([value,label])=>`<option value="${escape(value)}" ${value===selected?'selected':''}>${label}</option>`).join(''),{hint:'',help:'Run default tests both modes when streaming is enabled, otherwise complete responses. Explicit streaming requires streaming on Connect.'})}${selectField('profile_tools_forced_tool_diagnostic','Force tool choice',`<option value="false" ${config.forced_tool_diagnostic?'':'selected'}>No · test normal tool selection</option><option value="true" ${config.forced_tool_diagnostic?'selected':''}>Yes · include forced choice diagnostic</option>`,{hint:'',help:'Applies to fixtures without an explicit choice rule. Giraffe never executes tools.'})}${textField('profile_tools_fixtures','Tool fixtures (JSON)',fixtures,{rows:10,maxLength:state.bootstrap.limits['tool_checks.bytes'].maximum,hint:'Use [] for the built-in weather check.',help:`Use [] for the built-in weather check, or up to ${state.bootstrap.limits['tool_checks.fixtures'].maxItems} fixture objects with fixture_id, messages, tools, expected_calls (task assertions) or choice (mode: none, auto, required, named; named also needs name), and optional expected_content. choice.support is required or discover; max_calls defaults to 16; parallel_tool_calls is optional. Exact content checks the combined assistant text. The server validates fixture schemas and the ${diskSize(state.bootstrap.limits['tool_checks.bytes'].maximum)} configuration limit.`})}${textField('profile_tools_conversations','Tool-result conversations (JSON)',conversations,{rows:10,maxLength:state.bootstrap.limits['tool_checks.bytes'].maximum,hint:'',help:`Optional bounded conversations: fixture_id, messages, tools, steps, final_content. Each step declares expected_calls with name, arguments, result and optional outcome (success or error). Put {{observation_id}} in a result and a final_content check. Up to ${state.bootstrap.limits['tool_checks.conversations[].steps'].maxItems} steps; all final checks must pass. Tools stay available to detect repeated operations. With conversations configured, an empty fixture list skips the built-in check.`})}</div>`;
  }
  const profiles={
    prefix:{schema:'Prefix',fields:['prefix_chars','repeats','history_turns']},
    sessions:{schema:'Sessions',fields:['modes','sessions'],advanced:['delay_seconds','turns','saved_answers']},
    consistency:{schema:'Consistency',fields:['repetitions','levels','shapes','equality','strict'],advanced:['temperature','seed','logprobs']},
  };
  const descriptor=profiles[key];if(!descriptor)return '';
  const values={...profileDefaults(key,config),...config[key]},schema=state.bootstrap.config_schema.$defs[descriptor.schema];
  const labels={mode:'Arrival pattern',requests:'Arrival count',requests_per_second:'Sending rate (requests/second)',burst_size:'Burst size (requests)',interval_seconds:'Seconds between bursts',max_pending:'Pending queue (requests)',seed:'Random seed',prefix_chars:'Shared prefix (characters)',repeats:'Repeated requests',history_turns:'Conversation history turns',modes:'Conversation history',sessions:'Conversations',concurrency:'Concurrent conversations',max_tokens:'Turn cap (tokens)',delay_seconds:'Turn pause (seconds)',turns:'Conversation questions',saved_answers:'Prepared answers',repetitions:'Repetitions per case',levels:'Concurrent requests',shapes:'Question sizes',equality:'Compare answers',temperature:'Temperature',strict:'Require unanimous answers'};
  const hints={mode:'Choose even spacing or bursts of requests.',requests:'Exact number of arrivals to schedule. This is a count, not a sending rate or an in-flight limit.',requests_per_second:'Used only with even spacing.',burst_size:'Used only with bursts; requests scheduled together in each burst.',interval_seconds:'Used only with bursts; time between groups.',max_pending:'Arrivals wait in this bounded queue for an in-flight slot; overflow is dropped locally. Capacity rate-based traffic drops immediately instead of queueing.',seed:'Keep this number to repeat the same arrival pattern.',prefix_chars:'Repeated opening text length; characters are different from tokens.',repeats:'Repeat each shared-prefix case this many times.',history_turns:'Prepared conversation turns included in the history comparison.',modes:'Prepared uses saved replies; earlier model replies uses answers from this run.',sessions:'Conversations to run for each chosen history type.',concurrency:'Conversations allowed at once. An accepted value is not demonstrated delivery capacity.',max_tokens:'Answer cap for each turn; the suite output cap is a default, not a ceiling.',delay_seconds:'Wait this long before the next turn in a conversation.',repetitions:'Answers per question-size and load combination.',levels:'For example: 1, 2, 4. Each level is the check’s own parallelism. An accepted value is not demonstrated delivery capacity.',shapes:'Repeat the selected question sizes at every load.',equality:'Exact text compares characters; ignore extra whitespace normalizes spacing.',temperature:'Sampling temperature sent with repeated questions.',strict:'Require agreement fails if repeated answers differ. Wrong known answers can still fail either mode.'};
  const render=name=>{
    const fieldName=`profile_${key}_${name}`,value=key==='arrivals'&&name==='max_in_flight'?(values[name]??effectiveParallelism(config)):values[name];
    if(key==='consistency'&&name==='logprobs')return textField(fieldName,'Token logprob comparison (JSON)',typeof value==='string'?value:JSON.stringify(value ?? null,null,2),{rows:6,hint:'null turns this off.',help:`null disables it. {} enables streamed probability comparisons. response_mode: complete retains complete-response diagnostics (concurrent levels cannot qualify). Options: top_logprobs (${state.bootstrap.limits['consistency.logprobs.top_logprobs'].minimum}–${state.bootstrap.limits['consistency.logprobs.top_logprobs'].maximum}), strict, tolerance (logprob units; required for strict), support (discover/required), priming_rounds (${state.bootstrap.limits['consistency.logprobs.priming_rounds'].minimum}–${state.bootstrap.limits['consistency.logprobs.priming_rounds'].maximum}), compare_levels. Repetitions become corpus rounds using the existing load levels; see the request estimate. This measures token probabilities, not tokens per second.`});
    const helpNames=['requests','max_pending','concurrency','strict','equality','modes'];
    const copy={hint:name==='levels'?'For example: 1, 2, 4.':helpNames.includes(name)?'':hints[name],help:helpNames.includes(name)?hints[name]:name==='levels'?hints.levels.slice('For example: 1, 2, 4. '.length):undefined};
    if(key==='sessions'&&name==='modes')copy.help+=' Uses the saved fixed/live exact-repeat conversations. Their prompts, modes and concurrency can be changed below.';
    const choices=name==='modes'?[['fixed','Prepared history'],['live','Earlier model replies'],['fixed,live','Both history types']]:name==='shapes'?[['short','Short questions'],['long','Long questions'],['short,long','Short and long questions']]:name==='strict'?[['false','Report consistency'],['true','Require agreement']]:name==='equality'?[['bytes','Exact text'],['whitespace','Ignore extra whitespace']]:null;
    if(choices){const selected=Array.isArray(value)?value.join(','):String(value);if(!choices.some(([item])=>item===selected))choices.push([selected,'Saved selection']);return selectField(fieldName,labels[name],choices.map(([item,label])=>`<option value="${escape(item)}" ${item===selected?'selected':''}>${label}</option>`).join(''),copy);}
    if(['turns','saved_answers'].includes(name))return textField(fieldName,labels[name],(value || []).join('\n'),{hint:'',help:'Editing replaces this list. Keep prepared answers aligned with the saved conversation questions.'});
    return schemaField(fieldName,value,schema.properties[name],{label:labels[name] || human(name),...copy,...(name==='max_in_flight'?{hint:`Inherited default ${effectiveParallelism(config)}. An accepted value is not demonstrated delivery capacity.`}:{})});
  };
  return `${key==='sessions'?renderSessionEditor(config):''}<input type="hidden" name="profile_edits_${key}" value="[]"><button type="button" class="text-button reset-profile" data-action="reset-profile" data-profile="${key}">Reset this profile</button><div data-profile-settings="${key}" ${key==='sessions'?`data-session-fixed ${config.sessions?.profile==='performance'?'hidden':''}`:''}><fieldset class="fields">${descriptor.fields.map(render).join('')}</fieldset>${descriptor.advanced?`<details class="check-advanced"><summary>More ${escape(checkGuide[key].name.toLowerCase())} settings</summary><fieldset class="fields">${descriptor.advanced.map(render).join('')}</fieldset></details>`:''}</div>`;
}
function checkCoverageWarning(check,config) {
  if(check.id==='load')return '';
  const minimum=limitValue(config,check,'min_samples') ?? config.limits.min_samples;
  const planned=check.id==='consistency'?(config.consistency || profileDefaults('consistency',config)).repetitions:null;
  return planned!=null&&planned<minimum?`Too few planned samples for coverage: ${planned} in a group; ${minimum} required.`:'';
}
function checkOverrideWarning() {return '';}
function effectiveCheckSummary(check,config) {
  const parts=[];
  const timeout=check.id==='load'?loadSettings(config).request_timeout_seconds:check.id==='gpu'?null:optionValue(config,check,'request_timeout_seconds') ?? config.request_timeout_seconds;
  if(timeout!=null)parts.push(`Request timeout ${elapsed(timeout)}`);
  if(check.id==='access')parts.push('1 short request');
  else if(check.id==='capacity')parts.push(`${config.traffic.rates.join(' → ')} requests/s`,`${config.traffic.duration_seconds}s at each rate`);
  else if(check.id==='load'){const settings=loadSettings(config);parts.push(`${counted(Object.keys(loadTitles).filter(type=>settings.workloads[type].enabled).length,'workload')}`,validLoadLevels(settings.patterns.concurrent.levels)?`${settings.patterns.concurrent.levels.join(', ')} concurrent`:'Enter valid concurrency levels');}
  else if(check.id==='gpu')parts.push(config.targets[0].metrics_url?'Metrics URL configured':'Automatic metrics discovery');
  else if(check.id==='tools'){
    const fixtures=config.tool_checks?.fixtures,conversations=config.tool_checks?.conversations;
    parts.push(typeof fixtures==='string'?'Invalid tool fixture draft':Array.isArray(fixtures)&&fixtures.length?`${fixtures.length} tool ${fixtures.length===1?'fixture':'fixtures'}`:conversations?.length?'No single-response fixtures':'Built-in weather check');
    if(conversations?.length)parts.push(typeof conversations==='string'?'Invalid conversation draft':`${conversations.length} tool-result conversations`);
    if(Array.isArray(fixtures)){const choices=[...new Set(fixtures.map(f=>f.choice?.mode).filter(Boolean))];if(choices.length)parts.push(`Choice: ${choices.join(', ')}`);}
    parts.push((config.tool_checks?.modes || (config.stream?['complete','stream']:['complete'])).map(mode=>mode==='stream'?'Streaming':'Complete').join(' + '));
    if(Array.isArray(fixtures)&&fixtures.some(item=>item.expected_content))parts.push('Assistant text and calls checked');
    if((!fixtures?.length&&!conversations?.length)||(Array.isArray(fixtures)&&fixtures.some(item=>!item.choice)))parts.push(config.forced_tool_diagnostic?'Includes forced tool diagnostic':'Normal tool choice');
  }
  else if(check.id==='correctness'&&config.request_source==='request_pack'){
    const count=config.request_pack?.cases?.length;
    parts.push(count==null?'Each saved question once':`${count} saved ${count===1?'question':'questions'} · each once`);
  }
  else if(['arrivals','prefix','sessions','consistency'].includes(check.id)){
    const profile={...profileDefaults(check.id,config),...config[check.id]};
    if(check.id==='arrivals')parts.push(`${profile.requests} scheduled requests`,profile.mode==='burst'?`${profile.burst_size} per burst`:`${profile.requests_per_second} requests/s`);
    if(check.id==='prefix')parts.push(`${profile.repeats} repeats`,`${profile.prefix_chars} shared characters`);
    if(check.id==='sessions')parts.push(`${profile.sessions} conversations`,`${profile.concurrency} at a time`);
    if(check.id==='consistency'){parts.push(`${profile.repetitions} ${profile.logprobs?'corpus rounds':'repeats'}`,`${(profile.levels || []).join(', ')} requests at a time`);if(profile.logprobs)parts.push(`Logprobs · ${profile.logprobs.response_mode || 'stream'} responses · ${profile.logprobs.strict?'strict':'diagnostic'}`);}
  }else{
    if(check.option_fields.includes('samples')){
      const samples=optionValue(config,check,'samples');
      parts.push(['correctness','json'].includes(check.id)?`At least ${samples} questions`:check.id==='context'?`At least ${samples} requests`:`${samples} sample requests`);
    }
    if(check.option_fields.includes('concurrency'))parts.push(`${optionValue(config,check,'concurrency')} at a time`);
  }
  if(check.id==='serving')parts.push(`${number(limitValue(config,check,'max_error_rate')*100,1)}% errors allowed`);
  if(check.limit_fields.includes('min_correctness'))parts.push(`${number(limitValue(config,check,'min_correctness')*100,1)}% correct required`);
  if(check.limit_fields.includes('latency_ms')){const value=limitValue(config,check,'latency_ms');parts.push(value==null?'No response-time target':`Answer within ${ms(value)}`);}
  if(!parts.length&&check.id==='first_output'){const value=limitValue(config,check,'first_output_ms');parts.push(value==null?'Measure without a first-answer target':`First answer ${ms(value)}`);}
  return parts.join(' · ') || 'Uses this check’s defaults';
}
function renderCheckInfo(check,config) {
  const guide=checkGuide[check.id];
  const details=`<dl class="check-info-details"><div><dt>Sends</dt><dd>${escape(guide.sends)}</dd></div><div><dt>Shows</dt><dd>${escape(guide.shows)}</dd></div><div><dt>Settings</dt><dd data-effective-settings>${escape(effectiveCheckSummary(check,config))}</dd></div></dl>`;
  return infoHelp(`check_${check.id}`,guide.name,guide.purpose,details);
}
function updateCheckRow(check,config,card) {
  const badge=$('[data-setting-state]',card);if(badge)badge.textContent=testSettingsLabel(check,config);
  const effective=$('[data-effective-settings]',card);if(effective)effective.textContent=effectiveCheckSummary(check,config);
  const summary=$('[data-load-card-summary]',card);if(summary)summary.textContent=effectiveCheckSummary(check,config);
}
function sharedLimitsSummary(config) {
  return [config.max_requests!=null?`${number(config.max_requests)} requests`:'Requests not set',config.max_duration_seconds!=null?(config.max_duration_seconds>=3600?`${Math.floor(config.max_duration_seconds/3600)}h ${Math.floor(config.max_duration_seconds%3600/60)}m`:elapsed(config.max_duration_seconds)):'Duration not set',config.concurrency!=null?`${number(config.concurrency)} at once`:'Simultaneous not set'].join(' · ');
}
function updateSharedSettingsSummaries(config) {
  const summary=$('#run-ceilings-summary');if(summary)summary.textContent=sharedLimitsSummary(config);
}
function renderSharedRunSettings(config) {
  const rows=[
    ['max_duration_seconds','Maximum duration','When this time limit is reached, Giraffe stops sending new requests and cancels all requests still in progress. Tests can finish earlier. Completed results are kept; AI evaluation and report saving happen afterward. Server-side work may continue if the server does not honor cancellation.'],
    ['max_requests','Maximum requests','Shared by all selected checks and split into per-endpoint quotas. Scheduled requests may use budget without reaching the server. AI evaluator calls have separate limits.'],
    ['concurrency','Simultaneous limit','Ceiling on unfinished target requests across all endpoints. Each check keeps its own parallelism. An accepted value is not demonstrated delivery capacity.'],
  ];
  const row=(name,label,help)=>{
    const enabled=config[name]!=null;
    const unit=state.timeUnit||'s',factor=name==='max_duration_seconds'?({s:1,min:60,h:3600}[unit]):1;
    const value=enabled?config[name]/factor:null;
    const input=field(name,name==='max_duration_seconds'?'Duration':name==='max_requests'?'Request count':name==='concurrency'?'Simultaneous request count':label,value,{type:'number',step:name==='max_duration_seconds'?'any':1,required:enabled,hiddenLabel:true,hideInfo:true,help,hintAlways:true,hint:enabled?'':'Not set'}).replace('autocomplete="off"',`autocomplete="off" ${enabled?'':'disabled'}`);
    const fullHelp=[help,state.bootstrap?.cap_explanations?.[settingPath(name)]?.help].filter(Boolean).join(' ');
    return `<div class="optional-run-limit" data-run-limit="${name}"><div class="test-selection-line"><label class="run-limit-selection"><input type="checkbox" name="${name}_enabled" data-limit-toggle="${name}" aria-controls="field-${name}" aria-describedby="field-${name}-hint" ${enabled?'checked':''}><span>${label}</span></label>${infoHelp(name,label,fullHelp)}</div>${input}${name==='max_duration_seconds'?selectField('max_duration_unit','Time unit',['s','min','h'].map(value=>`<option value="${value}" ${value===unit?'selected':''}>${value}</option>`).join('')):''}</div>`;
  };
  return `<section class="setup-section shared-run-settings" aria-labelledby="shared-settings-title"><details class="shared-settings-editor" id="shared-settings"><summary class="shared-settings-summary"><h2 id="shared-settings-title">Run limits</h2><span class="run-ceilings-values" id="run-ceilings-summary">${escape(sharedLimitsSummary(config))}</span><span class="shared-settings-edit" aria-hidden="true"><span class="when-closed">Edit limits</span><span class="when-open">Close limits</span></span></summary><div class="setup-body"><div class="fields three run-limit-fields">${rows.map(args=>row(...args)).join('')}</div><div class="fields">${field('request_timeout_seconds','Request timeout (seconds)',config.request_timeout_seconds,{type:'number',step:'any',required:true,hint:''})}${field('max_memory_mb','Max memory growth (MiB)',config.max_memory_mb,{type:'number',required:true,hint:'0 turns off the memory limit.',help:fieldDescription('max_memory_mb').hint})}</div></div></details><p class="checks-plan-summary sr-only" id="checks-plan-summary">Preparing request estimate…</p></section>`;
}
function toggleRunLimit(name,enabled) {
  const form=$('#new-run-form'),input=form?.elements[name];if(!input)return;
  state.limitValues ||= {};
  if(enabled){input.value=state.limitValues[name] ?? '';input.disabled=false;input.required=true;}
  else {if(input.value!=='')state.limitValues[name]=input.value;input.value='';input.disabled=true;input.required=false;input.removeAttribute?.('aria-invalid');}
  const hint=$(`#field-${name}-hint`);if(hint)hint.textContent=enabled?'':'Not set';
}
function renderCheckSelection(config) {
  return `<section class="setup-section check-selection" id="test-selection"><h2 tabindex="-1">Choose your checks</h2><div class="setup-body">

  <div class="catalogue-toolbar"><div class="filter-group" aria-label="Show checks"><button type="button" class="text-button" data-action="catalogue-filter" data-catalogue-filter="selected" aria-pressed="${state.catalogueFilter==='selected'}">Selected (<span id="catalogue-selected-count">${config.checks.length}</span>)</button><button type="button" class="text-button" data-action="catalogue-filter" data-catalogue-filter="all" aria-pressed="${state.catalogueFilter!=='selected'}">All checks</button></div><div class="catalogue-bulk"><button type="button" class="text-button" data-action="select-tests">Select all</button><button type="button" class="text-button" data-action="clear-tests">Clear</button></div></div>
  <p class="sr-only" id="selection-summary" >${config.checks.length} checks selected</p>
  <div id="catalogue-browse-tools" ${state.catalogueFilter==='selected'?'hidden':''}><div class="catalogue-browse-header"><label class="catalogue-search"><span class="sr-only">Find a check</span><input type="search" id="check-search" placeholder="Find a check" value="${escape(state.catalogueSearch || '')}" autocomplete="off"></label><button type="button" class="text-button" data-action="quick-checks">Reset to defaults</button></div><div class="catalogue-categories" aria-label="Check categories"><button type="button" class="catalogue-filter" data-action="catalogue-category" data-category="all" aria-pressed="${!state.catalogueCategory||state.catalogueCategory==='all'}">All categories</button>${checkGroups.map(group=>`<button type="button" class="catalogue-filter" data-action="catalogue-category" data-category="${group.id}" aria-pressed="${state.catalogueCategory===group.id}">${group.title}</button>`).join('')}</div></div>
  <div id="test-catalogue"><div class="test-cards check-catalogue" id="test-cards">${checkGroups.map(group=>{const chosen=group.checks.filter(id=>config.checks.includes(id)).length;return `<section class="catalogue-group${chosen?' has-selection':''}" data-catalogue-group="${group.id}" aria-labelledby="group-${group.id}-title"><div class="catalogue-group-heading"><div class="group-title"><h3 id="group-${group.id}-title">${group.title}</h3><p class="group-blurb">${group.blurb}</p></div><span class="group-count" data-group-count>${chosen} of ${group.checks.length}</span><button type="button" class="text-button group-toggle" data-action="select-group" data-category="${group.id}" data-group-toggle>${chosen===group.checks.length?'Clear':'Select all'}</button></div><div class="catalogue-group-cards">${group.checks.map(id=>renderTestCard(state.bootstrap.checks.find(check=>check.id===id),config)).join('')}</div></section>`;}).join('')}</div><p class="hint catalogue-empty" id="catalogue-empty" role="status" hidden></p></div>
  <p class="hint" id="no-tests-selected" ${config.checks.length?'hidden':''}>Choose at least one check before reviewing the run.</p>
  </div></section>`;
}
function testSettingsLabel(check,config) {
  const custom=Object.keys(config.test_options?.[check.id] || {}).length>0||
    (check.id==='capacity'&&!sameConfig(config.traffic,state.bootstrap.config.traffic))||
    (check.id==='load'&&!sameConfig(loadSettings(config),loadSettings({...config,load_test:null})));
  return custom?(config.checks.includes(check.id)?'Overrides':'Saved overrides'):'';
}
function checkSettingValue(name,value) {
  if(value==null)return ['first_output_ms','latency_ms','stream_gap_ms','min_output_tokens_per_second','fairness_max_ratio'].includes(name)?'measure only':'not set';
  if(['min_correctness','max_error_rate'].includes(name))return `${number(value*100,1)}%`;
  const unit={samples:'requests',concurrency:'at a time',context_limit:'tokens',max_output_tokens:'tokens',request_timeout_seconds:'seconds',sustained_seconds:'seconds',metrics_max_age_seconds:'seconds',cancel_after_ms:'ms',deadline_probe_ms:'ms',cancel_after_chunks:'chunks',cleanup_seconds:'seconds',trace_wait_seconds:'seconds',first_output_ms:'ms',latency_ms:'ms',stream_gap_ms:'ms',min_output_tokens_per_second:'tokens/second',min_samples:'usable answers'}[name];
  return `${value}${unit?' '+unit:''}`;
}
function checkFieldHelp(check,name,kind='option') {
  const descriptions={
    samples:({generation:'Repeat the generated long-answer request.',concurrency:'Short-only baseline samples and paired trials for the streaming-fairness scenario. Other scenarios have their own sample counts.',context:'Minimum requests across the generated context cases. Calibration and retries can add requests.',correctness:'Minimum generated questions; every built-in case is covered.',json:'Minimum generated JSON questions; every built-in case is covered.',cancellation:'Requests for normal controls and again for follow-up health checks.',recovery:'Short health requests after the load period.'})[check.id] || 'Requests used by this check.',
    concurrency:'Requests sent together by this check. An accepted value is not demonstrated delivery capacity.',
    context_limit:'Declared server context length, used to size generated questions. This does not change the server.',
    max_output_tokens:'Answer cap for this check. The suite value is a default, not a ceiling.',
    request_timeout_seconds:'Timeout for each request in this check. The suite value is a default, not a ceiling.',
    sustained_seconds:'Send load for this long, then check recovery. The run budget can stop load earlier.',
    metrics_max_age_seconds:'Older server measurements are reported as stale.',
    backend_verification:'Required needs supported server logs or traces to prove cancelled work stopped.',
    server_log:'Path on the Giraffe machine to supported server logs.',trace_file:'Path on the Giraffe machine to supported request traces.',
    cancel_after_ms:'Cancel the streaming probe after this delay.',deadline_probe_ms:'Intentionally time out a probe after this delay.',
    cancel_after_chunks:'For server verification, wait for this many answer chunks before cancelling.',
    cleanup_seconds:'Wait for cancelled server work to finish cleaning up.',trace_wait_seconds:'Wait for cancellation evidence to appear in logs or traces.',
    min_correctness:'Required share of scored answers that match the expected answer.',max_error_rate:'Largest allowed share of failed requests.',
    first_output_ms:'Longest acceptable wait for useful answer text.',latency_ms:'Longest acceptable wait for a complete answer.',
    stream_gap_ms:'Longest acceptable pause between answer chunks.',min_output_tokens_per_second:'Minimum answer generation speed when token usage is available.',
    fairness_max_ratio:'2 allows short answers to take up to twice as long under competing load.',
    min_samples:'Fewer usable answers make the measurement inconclusive.',
  };
  const help=fieldDescription(`${kind==='limit'?'limit':'test'}_${name}`).help;
  return [descriptions[name],help,kind==='limit'&&state.bootstrap.limits_schema.properties[name]?.anyOf?'Blank means measure only.':''].filter(Boolean).join(' ');
}
function checkFieldHint(check,config,name,kind='option',showValue=false) {
  const saved=config.test_options?.[check.id] || {},overrides=kind==='limit'?(saved.limits || {}):saved;
  if(kind==='option'&&check.id==='first_output'&&config.checks.includes('serving')&&config.test_options?.serving?.[name]!=null)return `Endpoint response owns this shared request setting: ${checkSettingValue(name,optionValue(config,check,name))}. Change it in Endpoint response; the saved First answer override stays a draft.`;
  const overridden=Object.hasOwn(overrides,name)&&(kind==='limit'||overrides[name]!=null);
  const defaultValue=kind==='limit'?(Object.hasOwn(config.limits || {},name)?config.limits[name]:check.defaults.limits[name]):optionDefault(config,check,name);
  const value=kind==='limit'?limitValue(config,check,name):optionValue(config,check,name);
  const origin=(kind==='limit'&&Object.hasOwn(config.limits || {},name))||config[name]!=null?'run default':'check default';
  const optionalTarget=kind==='limit'&&state.bootstrap.limits_schema.properties[name]?.anyOf?.some(item=>item.type==='null');
  if(value==null&&optionalTarget)return overridden&&defaultValue!=null?`Measure only · ${origin}: ${checkSettingValue(name,defaultValue)}`:'Measure only';
  if(overridden)return `Custom · ${origin}: ${checkSettingValue(name,defaultValue)}`;
  return `${human(origin)}${showValue?`: ${checkSettingValue(name,value)}`:''}`;
}
function runLimitHint(name,config) {
  const explanation='An accepted value is not demonstrated delivery capacity.';
  if(name==='load_patterns_concurrent_levels')return 'Used by maintained-concurrency traffic and the long-input / short-answer test.';
  if(name==='traffic_max_in_flight')return `Own in-flight bound; inherited default ${effectiveParallelism(config)}. When slots are busy, arrivals are dropped locally. ${explanation}`;
}
function effectiveParallelism(config) {return Math.min(config.default_concurrency ?? state.bootstrap.config_schema.properties.default_concurrency.default,config.concurrency==null?Infinity:Math.max(1,Math.floor(config.concurrency/(config.overlap_models?config.targets.length:1))));}
function updateRunLimitHints(config) {
  for(const name of ['load_patterns_concurrent_levels']){const hint=$(`#field-${name}-hint`);if(hint)hint.textContent=runLimitHint(name,config);}
}
function updateCheckFieldHints(config) {
  updateRunLimitHints(config);
  const form=$('#new-run-form');
  if(form)for(const id of ['serving','first_output']){const check=state.bootstrap.checks.find(check=>check.id===id);for(const name of ['samples','max_output_tokens','request_timeout_seconds']){const input=form.elements[`test_${id}_${name}`];if(!input)continue;const owned=id==='first_output'&&config.checks.includes('serving')&&config.test_options?.serving?.[name]!=null;input.readOnly=owned;if(input.dataset?.inherited==='true'||owned)input.value=optionValue(config,check,name)??'';}}
  if(form)for(const [name,input] of Object.entries(form.elements)){
    if(!input||input.type&&input.type!=='number')continue;
    const bounds=settingBounds(input.name||name);
    if(bounds.maximum!=null)input.max=String(bounds.maximum);
    else input.removeAttribute?.('max');
  }
  for(const warning of $$('[data-check-override-warning]')){
    const check=state.bootstrap.checks.find(item=>item.id===warning.dataset.checkOverrideWarning);
    const message=check?checkOverrideWarning(check,config,true):'';
    warning.textContent=message;warning.hidden=!message||!(config.checks.includes(check.id)||state.openTest===check.id);
  }
  for(const warning of $$('[data-check-coverage]')){
    const check=state.bootstrap.checks.find(item=>item.id===warning.dataset.checkCoverage);
    const message=check?checkCoverageWarning(check,config):'';
    warning.textContent=message;warning.hidden=!message||!(config.checks.includes(check.id)||state.openTest===check.id);
  }
  for(const hint of $$('[data-check-field-hint]')){
    const check=state.bootstrap.checks.find(item=>item.id===hint.dataset.checkId);
    if(check){
      const name=hint.dataset.settingName,kind=hint.dataset.settingKind;
      const input=form?.elements[`test_${check.id}_${kind==='limit'?'limit_':''}${name}`];
      hint.textContent=checkFieldHint(check,config,name,kind,input?.value==='');
    }
  }
  for(const reset of $$('[data-reset-test]'))reset.hidden=reset.dataset.resetTest!=='load'&&!Object.keys(config.test_options?.[reset.dataset.resetTest] || {}).length;
}
function markCheckHint(markup,check,name,kind) {
  return markup.replace('class="hint" id=',`class="hint" data-check-field-hint data-check-id="${check.id}" data-setting-name="${name}" data-setting-kind="${kind}" id=`);
}
function renderTestCard(check,config) {
  if(!check)return '';
  const guide=checkGuide[check.id],selected=config.checks?.includes(check.id),custom=Object.keys(config.test_options?.[check.id] || {}).length>0;
  const warningsVisible=selected||state.openTest===check.id;
  const ownQuestions=check.id==='correctness'&&config.request_source==='request_pack';
  const optionLabels={samples:'Sample requests',concurrency:'Concurrent requests',context_limit:'Context length (tokens)',max_output_tokens:'Output cap (tokens)',request_timeout_seconds:'Request timeout (seconds)',sustained_seconds:'Load duration (seconds)',metrics_max_age_seconds:'Metric freshness (seconds)',backend_verification:'Verify server cancellation',server_log:'Server log path',trace_file:'Trace file path',cancel_after_ms:'Cancel after (ms)',deadline_probe_ms:'Probe deadline (ms)',cancel_after_chunks:'Chunks before cancellation',cleanup_seconds:'Cleanup wait (seconds)',trace_wait_seconds:'Trace wait (seconds)'};
  const advancedOptions=new Set(['backend_verification','server_log','trace_file','cancel_after_chunks','cleanup_seconds','trace_wait_seconds']);
  const runOptions=new Set();
  const option=name=>{let markup=schemaField(`test_${check.id}_${name}`,optionValue(config,check,name),state.bootstrap.option_schema.properties[name],{label:check.id==='correctness'&&name==='samples'?'Minimum questions':optionLabels[name] || human(name),helpKey:`test_${name}`,help:checkFieldHelp(check,name),hint:checkFieldHint(check,config,name),placeholder:'Use default'});if(check.id==='first_output'&&config.checks.includes('serving')&&config.test_options?.serving?.[name]!=null)markup=markup.replace('autocomplete="off"','autocomplete="off" readonly');return markCheckHint(markup,check,name,'option');};
  const limitLabels={first_output_ms:'First answer (ms)',latency_ms:'Completion limit (ms)',stream_gap_ms:'Stream pause (ms)',min_output_tokens_per_second:'Minimum rate (tok/s)',fairness_max_ratio:'Maximum slowdown (ratio)',max_error_rate:'Errors allowed (%)',min_correctness:'Correct answers (%)',min_samples:'Minimum usable answers'};
  const primaryLimits=new Set(['first_output_ms','latency_ms','min_correctness','max_error_rate','fairness_max_ratio','min_samples']);
  const limit=name=>markCheckHint(['min_correctness','max_error_rate'].includes(name)?field(`test_${check.id}_limit_${name}`,limitLabels[name],limitValue(config,check,name)*100,{type:'number',step:'any',required:true,helpKey:`limit_${name}`,help:checkFieldHelp(check,name,'limit'),hint:checkFieldHint(check,config,name,'limit')}):schemaField(`test_${check.id}_limit_${name}`,limitValue(config,check,name),state.bootstrap.limits_schema.properties[name],{label:limitLabels[name] || human(name),helpKey:`limit_${name}`,help:checkFieldHelp(check,name,'limit'),placeholder:'Measure only',hint:checkFieldHint(check,config,name,'limit')}),check,name,'limit');
  const inactive=()=>ownQuestions;
  const requestFields=check.option_fields.filter(name=>!inactive(name)&&!advancedOptions.has(name)&&!runOptions.has(name));
  const requests=requestFields.map(option).join('');
  const inherited=check.option_fields.filter(name=>!inactive(name)&&runOptions.has(name)).map(option).join('');
  const diagnostics=check.option_fields.filter(name=>advancedOptions.has(name)).map(option).join('');
  const criteriaFields=check.limit_fields.filter(name=>primaryLimits.has(name));
  const criteria=criteriaFields.map(limit).join('');
  const moreCriteria=check.limit_fields.filter(name=>!primaryLimits.has(name)).map(limit).join('');
  const hostsQuestions=check.id===(config.checks.includes('correctness')?'correctness':config.checks.includes('load')?'load':null);
  const sharedQuestions=config.checks.includes('correctness')&&config.checks.includes('load');
  const questionSource=hostsQuestions?`<div class="catalogue-question-source">${sharedQuestions?`<p class="hint question-source-scope">Also used by ${escape(catalogTitle('load'))}.</p>`:''}${renderRequestSource(config)}</div>`:'';
  const inactiveControls=ownQuestions?`<p class="hint" id="field-request_pack_correctness" tabindex="-1" data-pack-request-settings>Each saved case owns its output cap and request options. Request timeout: ${elapsed(config.request_timeout_seconds)}. Concurrency: ${effectiveParallelism(config)} (default). Per-check request-setting overrides are unavailable. Per-check pass criteria remain supported.</p><span class="field-error" id="field-request_pack_correctness-error" hidden></span><span class="hint field-warning" id="field-request_pack_correctness-warning" hidden></span>`:'';
  return `<section class="test-card catalogue-row" data-test-card="${check.id}" data-category="${checkGroups.find(group=>group.checks.includes(check.id))?.id || ''}" data-search="${escape(`${guide.name} ${guide.purpose} ${check.title} ${check.id}`.toLowerCase())}">
  <div class="test-card-heading"><div class="test-row-copy"><div class="test-selection-line"><label class="test-selection"><input type="checkbox" name="check" value="${check.id}" ${selected?'checked':''}><span>${escape(guide.name)}</span></label>${renderCheckInfo(check,config)}</div><p class="test-description">${escape(guide.purpose)}</p><p class="hint" data-load-card-summary>${escape(effectiveCheckSummary(check,config))}</p></div><div class="test-row-actions"><span class="test-setting-state" data-setting-state>${escape(testSettingsLabel(check,config))}</span><button type="button" class="button small configure-test" data-configure-test="${check.id}" aria-label="Settings for ${escape(guide.name)}" aria-expanded="false" aria-controls="settings-${check.id}"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 6h7M15 6h1M4 14h1M9 14h7"/><circle cx="13" cy="6" r="2"/><circle cx="7" cy="14" r="2"/></svg>Settings</button></div></div>
  <p class="hint check-row-warning" data-check-override-warning="${check.id}" ${warningsVisible&&checkOverrideWarning(check,config)?'':'hidden'}>${escape(checkOverrideWarning(check,config,true))}</p>
  <div data-check-runtime-warning="${check.id}" hidden></div><p class="hint check-row-warning" data-check-coverage="${check.id}" ${warningsVisible&&checkCoverageWarning(check,config)?'':'hidden'}>${escape(checkCoverageWarning(check,config))}</p>
  <input type="hidden" name="test_mode_${check.id}" value="${custom?'custom':'default'}">
  <div class="test-editor" id="settings-${check.id}" data-test-editor="${check.id}" hidden>
  <div class="test-editor-head"><span class="eyebrow">${escape(checkGroups.find(group=>group.checks.includes(check.id))?.title || 'Check')}</span><h3 tabindex="-1" data-editor-title>${escape(guide.name)}</h3><button type="button" class="button ghost icon-button" data-close-test="${check.id}" aria-label="Close ${escape(guide.name)} settings"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 5 10 10M15 5 5 15"/></svg></button></div>
  <p class="hint draft-notice" ${selected?'hidden':''}>Saved draft · select this check to run it.</p>
  ${guide.needs?`<p class="check-prerequisite"><strong>Needs:</strong> ${escape(guide.needs)}.</p>`:''}${check.id==='load'?'':questionSource}${check.id==='gpu'?'<p class="hint">Metrics are collected automatically. Add exporter addresses in Connection settings.</p>':''}
  <div class="test-settings${requestFields.length===1&&criteriaFields.length===1?' simple-primary-settings':''}" data-test-settings="${check.id}">
  ${check.option_fields.length||check.limit_fields.length||check.id==='load'?`<button type="button" class="text-button reset-test" data-reset-test="${check.id}" aria-label="${check.id==='load'?'Reset workload settings':'Reset overrides'} for ${escape(guide.name)}" ${custom||check.id==='load'?'':'hidden'}>${check.id==='load'?'Reset workload settings':'Reset overrides'}</button>`:''}${inactiveControls}
  ${requests?`${requestFields.length>1?'<h4>Requests</h4>':''}<fieldset class="fields check-request-fields">${requests}</fieldset>`:''}
  ${criteria?`${criteriaFields.length>1?'<h4>Pass criteria</h4>':''}<fieldset class="fields check-criteria-fields">${criteria}</fieldset>`:''}
  ${inherited?`<details class="check-advanced check-run-overrides"><summary>Override run defaults</summary><fieldset class="fields">${inherited}</fieldset></details>`:''}
  ${moreCriteria?`<details class="check-advanced"><summary>More pass criteria</summary><fieldset class="fields">${moreCriteria}</fieldset></details>`:''}
  ${diagnostics?`<details class="check-advanced"><summary>Server verification & cleanup</summary><fieldset class="fields">${diagnostics}</fieldset></details>`:''}
  ${renderProfileEditor(check,config)}</div>
  <div class="test-editor-footer"><button type="button" class="button small" data-done-test="${check.id}">Done</button></div></div></section>`;
}

const loadSizes=['short','medium','long'];
const loadShapes=loadSizes.flatMap(input=>loadSizes.map(output=>`${input}_${output}`));
const loadTitles={combinations:'Input → output combinations',mixed:'Mixed traffic',interference:'Long input during an active short stream',stress:'Token stress · large input and output'};
function loadShapeLabel(shape) {const [input,output]=shape.split('_');return `${human(input)} input → ${human(output)} output`;}
function loadFormDefaults() {return clone(state.bootstrap.load_test.defaults);}
function loadSettings(config) {return loadTestSettings(config,loadFormDefaults());}
function loadCheckbox(name,label,checked,{controls='',extra=''}={}) {
  return `<div class="form-field load-checkbox"><label for="field-${name}"><input type="checkbox" id="field-${name}" name="${name}" ${checked?'checked':''} ${controls?`aria-controls="${controls}"`:''} ${extra}>${label}</label><span class="field-error" id="field-${name}-error" hidden></span><span class="hint field-warning" id="field-${name}-warning" hidden></span></div>`;
}
function validLoadLevels(levels) {
  return Array.isArray(levels)&&levels.length>0&&levels.every((n,i)=>Number.isInteger(n)&&n>0&&(!i||n>levels[i-1]));
}
function loadDisplay(settings) {
  const {profile,schedule,workloads:w,patterns:p}=settings,mix=w.mixed.selected;
  const finite=n=>typeof n==='number'&&Number.isFinite(n),fmt=n=>finite(n)?String(Number(n.toFixed(2))):'—';
  const total=mix.reduce((sum,shape)=>sum+Number(w.mixed.shares[shape]),0);
  const levels=p.concurrent.levels,eligible=levels.filter(n=>Number.isInteger(n)&&n>=2),duration=schedule.duration_seconds;
  return {
    shares:w.mixed.share_mode==='equal'?`${mix.length?(100/mix.length).toFixed(1):0}% expected per selected combination; actual counts vary.`:finite(total)?`${fmt(total)}% total · must equal 100%.`:'Give each selected mix type a positive share, totaling 100%.',
    slowdown:`${fmt(w.interference.max_slowdown)}× means up to ${fmt((w.interference.max_slowdown-1)*100)}% longer. Example: if a short answer normally finishes in 2 seconds, it may take at most ${fmt(2*w.interference.max_slowdown)} seconds when the long request is added.`,
    allocation:!validLoadLevels(levels)?'Enter positive, increasing whole-number levels separated by commas.':`Uses the shared concurrency levels (${eligible.join(', ')||'none eligible'}) and ${fmt(duration)}-second duration below. ${eligible.map(n=>`At ${n} concurrent requests: ${n-1} short streams, with room for one added long request.`).join(' ')} Each run sends for the full duration; already sent requests keep their own request timeout. ${levels.includes(1)?'Level 1 is not applicable: two requests cannot overlap at that level. ':''}Constant, Poisson and bursts belong to the other selected workloads.`,
    coverage:finite(profile.long.output_tokens)&&finite(w.stress.min_output_percent)?`At least ${Math.ceil(profile.long.output_tokens*w.stress.min_output_percent/100)} generated tokens per long-output stress response (${fmt(w.stress.min_output_percent)}% of the ${profile.long.output_tokens}-token cap). Length coverage alone is not a pass.`:'Enter a valid long-output cap and output percentage.'
  };
}
function renderMigrationNotes() {
  const notes=(state.migrationNotes || []).filter(note=>!(state.dismissedMigrationNotes || []).includes(note));
  return notes.length?`<div class="notice warning migration-notes"><strong>Review imported load settings</strong><ul>${notes.map(note=>`<li>${escape(note)}</li>`).join('')}</ul><button type="button" class="text-button" data-action="dismiss-migration-notes">Dismiss notes</button></div>`:'';
}
function acceptMigrationNotes(notes=[]) {
  state.migrationNotes=[...new Set([...(state.migrationNotes || []),...notes])];
  for(const element of $$('[data-migration-notes]'))element.innerHTML=renderMigrationNotes();
}
function replaceMigrationNotes(notes=[]) {
  state.migrationNotes=[];state.dismissedMigrationNotes=[];state.loadPanels={};
  acceptMigrationNotes(notes);
}
const loadCriteriaLabels={max_first_answer_ms:'Maximum time to first answer per response (ms)',max_completion_ms:'Maximum completion time per response (ms)',max_error_percent:'Errors allowed (%)',min_correct_percent:'Correct answers required (%)',max_stream_pause_ms:'Maximum pause between streamed chunks (ms)',min_generation_tokens_per_second:'Minimum generation speed per response (tokens/sec)',min_valid_responses:'Minimum valid responses per result group'};
function loadFieldLabel(path) {
  let match;
  if(match=/^profile\.(short|medium|long)\.(input_tokens|output_tokens)$/.exec(path))return `${human(match[1])} ${match[2]==='input_tokens'?'input tokens':'output cap'}`;
  if(match=/^workloads\.(combinations|mixed)\.selected\.([a-z]+)_([a-z]+)$/.exec(path))return `${human(match[2])} → ${human(match[3])}`;
  if(match=/^workloads\.mixed\.shares\.([a-z]+)_([a-z]+)$/.exec(path))return `${human(match[1])} → ${human(match[2])} (%)`;
  if(match=/^workloads\.stress\.cases\.(.+)$/.exec(path))return loadShapeLabel(match[1]);
  if(match=/^workloads\.(.+)\.enabled$/.exec(path))return loadTitles[match[1]];
  if(path.startsWith('pass_criteria.'))return loadCriteriaLabels[path.slice(14)];
  return {'workloads.mixed.share_mode':'Share of requests','workloads.interference.trigger':'When to send the long-input request','workloads.interference.max_slowdown':'Maximum allowed slowdown of short answers (×)','workloads.stress.min_output_percent':'Minimum generated output (% of long-output cap)',
    'patterns.concurrent.enabled':'Maintain concurrent requests','patterns.constant.enabled':'Constant arrivals','patterns.poisson.enabled':'Poisson arrivals','patterns.bursts.enabled':'Bursts','patterns.concurrent.levels':'Concurrency levels','patterns.rates':'Rates (requests/second)','patterns.poisson.seed':'Random seed','patterns.bursts.size':'Requests per burst','patterns.bursts.first_seconds':'First burst after (seconds)','patterns.bursts.gap_seconds':'Time between burst starts (seconds)','schedule.duration_seconds':'Send new requests for (seconds)','request_timeout_seconds':'Request timeout (seconds)'}[path]||human(path);
}
function selectedStandardWorkloads(workloads) {
  return (workloads.combinations.enabled?workloads.combinations.selected.length:0)+(workloads.mixed.enabled?1:0)+(workloads.stress.enabled?workloads.stress.cases.length:0);
}
function renderLoadEditor(config) {
  const s=loadSettings(config),w=s.workloads,p=s.patterns,live=loadDisplay(s),standard=selectedStandardWorkloads(w);
  const numberField=(path,label,value,options={})=>field(loadFieldName(path),label,value,{type:'number',step:'any',...options});
  const checkbox=(path,label,value,options={})=>loadCheckbox(loadFieldName(path),label,value,options);
  const gridHelp={combinations:'Each selected combination runs under every selected traffic pattern and level.',mixed:'Bursts use this same mix. Shares describe request counts, not tokens or time.'};
  const grid=kind=>`<div class="load-grid-actions"><div class="field-label"><h3>${kind==='mixed'?'Include these input → output combinations':'Input → output'}</h3>${infoHelp(`load_${kind}_grid`,kind==='mixed'?'Include these input → output combinations':'Input → output',gridHelp[kind])}</div><button type="button" class="text-button" data-action="load-select-grid" data-load-grid="${kind}">Select all / clear</button></div><div class="load-shape-grid">${loadShapes.map(shape=>{const [input,output]=shape.split('_');return checkbox(`workloads.${kind}.selected.${shape}`,`<span>${human(input)} → ${human(output)}<small data-load-tokens="${shape}">${s.profile[input].input_tokens} → ${s.profile[output].output_tokens} tokens</small></span>`,w[kind].selected.includes(shape));}).join('')}</div>`;
  const cards={
    combinations:`${grid('combinations')}`,
    mixed:`${grid('mixed')}${selectField('load_workloads_mixed_share_mode','Share of requests',`<option value="equal" ${w.mixed.share_mode==='equal'?'selected':''}>Equal shares</option><option value="custom" ${w.mixed.share_mode==='custom'?'selected':''}>Custom percentages</option>`)}<div id="load-shares" class="fields load-share-grid" ${w.mixed.share_mode==='custom'?'':'hidden'}>${loadShapes.map(shape=>`<div data-load-share-row="${shape}" ${w.mixed.selected.includes(shape)?'':'hidden'}>${numberField(`workloads.mixed.shares.${shape}`,`${human(shape.split('_')[0])} → ${human(shape.split('_')[1])} (%)`,w.mixed.shares[shape]??'')}</div>`).join('')}</div><p class="hint" id="load-share-note">${escape(live.shares)}</p>`,
    interference:`${selectField('load_workloads_interference_trigger','When to send the long-input request',`<option value="streaming_started" ${w.interference.trigger==='streaming_started'?'selected':''}>After the short answers start streaming</option><option value="after_five_chunks" ${w.interference.trigger==='after_five_chunks'?'selected':''}>After five streamed chunks from each short answer</option>`,{hint:'',help:['Short requests use Short input → Short output. The added request uses Long input → Short output from the data profile. The long request is sent only while the short answers are still active. If they finish before that point, the test is inconclusive. A short answer taking longer than this relative limit fails this check. Shared response-time, error and correctness criteria still apply.',fieldDescription('load_workloads_interference_max_slowdown').help,state.bootstrap?.cap_explanations?.[settingPath('load_workloads_interference_max_slowdown')]?.help].filter(Boolean).join(' '),helpDetails:`<ol class="load-interference-steps"><li>First, measure short answers with no long request running alongside them.</li><li>Repeat the same short workload. While the short answers are still streaming, send one long-input request.</li><li>Compare how long the short answers take to finish. Report any extra pauses in their output.</li></ol><p class="hint" id="load-slowdown-help" aria-live="polite">${escape(live.slowdown)}</p><p class="hint" id="load-interference-schedule">${escape(live.allocation)}</p>`})}<p class="hint">Inconclusive if short answers finish before the long request is sent.</p><div id="load-interference-pass" ${w.interference.enabled?'':'hidden'}>${numberField('workloads.interference.max_slowdown','Maximum allowed slowdown of short answers (×)',w.interference.max_slowdown,{hideInfo:true})}</div><button type="button" class="text-button" data-action="edit-setting" data-setting-path="load_test.patterns.concurrent.levels">Edit concurrency levels and duration</button>`,
    stress:`${['long_short','short_long','long_long'].map(shape=>checkbox(`workloads.stress.cases.${shape}`,loadShapeLabel(shape),w.stress.cases.includes(shape))).join('')}<div id="load-stress-pass" ${w.stress.enabled&&w.stress.cases.some(shape=>shape.endsWith('_long'))?'':'hidden'}><span class="eyebrow">For token stress</span>${numberField('workloads.stress.min_output_percent','Minimum generated output (% of long-output cap)',w.stress.min_output_percent)}<p class="hint" id="load-output-threshold" aria-live="polite">${escape(live.coverage)}</p></div>`
  };
  const descriptions={combinations:'Test each selected size combination separately.',mixed:'Randomly mix selected sizes in the same test.',interference:'Check whether a long question makes an ongoing short answer slower.',stress:'Check whether long inputs and generated answers were actually exercised.'};
  return `<div class="load-editor" data-load-settings><input type="hidden" name="load_rendered" value="1"><p>Choose what to send, how it arrives, and what a good response looks like.</p><div data-migration-notes>${renderMigrationNotes()}</div>
  <section class="load-section"><div class="field-label"><h3>Data profile</h3>${infoHelp('load_data_profile','Data profile','Used by every selected workload below. Answers may finish before their cap; actual token lengths are reported.')}</div><span class="eyebrow">This check only</span><table class="load-profile"><thead><tr><th></th><th>Input tokens · target</th><th>Output tokens · cap</th></tr></thead><tbody>${loadSizes.map(size=>`<tr><th scope="row">${human(size)}</th>${['input_tokens','output_tokens'].map(kind=>`<td>${numberField(`profile.${size}.${kind}`,`${human(size)} ${kind==='input_tokens'?'input tokens':'output cap'}`,s.profile[size][kind],{step:1})}</td>`).join('')}</tr>`).join('')}</tbody></table>${config.checks.includes('load')&&!config.checks.includes('correctness')?renderRequestSource(config):''}</section>
  <section class="load-section"><div class="field-label"><h3>Workloads</h3>${infoHelp('load_workloads','Workloads','Select a workload to open its settings. Selected workloads run separately; mixed traffic combines request sizes within its own test.')}</div><div class="load-types">${Object.entries(loadTitles).map(([kind,title])=>{const open=state.loadPanels[kind]??w[kind].enabled;return `<section class="load-type${w[kind].enabled?' is-selected':''}" data-load-type="${kind}"><div class="load-type-heading">${checkbox(`workloads.${kind}.enabled`,`<span><strong>${title}</strong><small>${descriptions[kind]}</small></span>`,w[kind].enabled,{controls:`load-panel-${kind}`,extra:`data-load-enable="${kind}"`})}${kind==='stress'?infoHelp('load_stress_cases',title,'Each selection is a separate test using the traffic patterns below. Stress prompts ask for extended output; set minimum length coverage below.'):''}<button type="button" class="button ghost load-collapse" data-load-collapse="${kind}" aria-expanded="${open}" aria-controls="load-panel-${kind}" aria-label="${open?'Collapse':'Expand'} ${title} settings">${open?'⌃':'⌄'}</button></div><div class="load-type-panel" id="load-panel-${kind}" ${open?'':'hidden'}>${cards[kind]}</div></section>`;}).join('')}</div></section>
  <section class="load-section" id="load-traffic" ${standard||w.interference.enabled?'':'hidden'}><div class="field-label"><h3>Traffic patterns</h3>${infoHelp('load_traffic_patterns','Traffic patterns','Each selected pattern runs separately for combinations, mixed traffic and token stress.')}</div><span class="eyebrow">Choose one or more</span>${[['concurrent','Maintain concurrent requests','Replace each finished request to maintain the chosen level.'],['constant','Constant arrivals','New requests at evenly spaced intervals.'],['poisson','Poisson arrivals','Random intervals around the chosen average rate.']].map(([kind,title,description])=>checkbox(`patterns.${kind}.enabled`,`<span><strong>${title}</strong><small>${description}</small></span>`,p[kind].enabled)).join('')}<div id="load-arrival-fields" ${standard&&(p.constant.enabled||p.poisson.enabled)?'':'hidden'}>${field('load_patterns_rates','Rates (requests/second)',p.rates.join(', '))}<div id="load-seed-field" ${p.poisson.enabled?'':'hidden'}>${numberField('patterns.poisson.seed','Random seed',p.poisson.seed,{step:1})}</div></div>${checkbox('patterns.bursts.enabled','<span><strong>Bursts</strong><small>Send groups of requests on their own or alongside Constant / Poisson arrivals.</small></span>',p.bursts.enabled)}<div id="load-burst-fields" ${standard&&p.bursts.enabled?'':'hidden'}><div class="field-label"><h3>Burst schedule</h3>${infoHelp('load_burst_schedule','Burst schedule','Runs alone when neither Constant nor Poisson is selected. Otherwise, adds bursts to each selected arrival pattern. New bursts do not wait for earlier requests to finish. The concurrency ceiling still applies.')}</div><div class="fields">${numberField('patterns.bursts.size','Requests per burst',p.bursts.size,{step:1})}${numberField('patterns.bursts.first_seconds','First burst after (seconds)',p.bursts.first_seconds)}${numberField('patterns.bursts.gap_seconds','Time between burst starts (seconds)',p.bursts.gap_seconds)}</div><p id="load-burst-preview" class="hint">${escape(currentRequestPlan()?.load_test?.burst_previews?.[0]?.text||'Updating burst preview…')}</p></div><div id="load-concurrent-fields" ${p.concurrent.enabled||w.interference.enabled?'':'hidden'}>${field('load_patterns_concurrent_levels','Concurrency levels',p.concurrent.levels.join(', '),{hint:'Used by maintained-concurrency traffic and the long-input / short-answer test.'})}</div><div class="fields">${numberField('schedule.duration_seconds','Send new requests for (seconds)',s.schedule.duration_seconds,{step:1,hint:'',help:'Send new requests for this duration per workload, pattern and level. Requests already sent may finish within their request timeout. A run limit can stop traffic sooner. Minimum valid responses is an evidence requirement, not a stopping count.'})}</div></section>
  <section class="load-section"><div class="field-label"><h3>Request timing &amp; delivery</h3>${infoHelp('load_request_timing','Request timing & delivery','For arrivals and bursts, a full client slot limit means the scheduled request is not sent. Show this as incomplete delivery; do not queue it silently or count it as an endpoint failure. Actual dispatch timing and burst spread are reported. Server queue time needs server telemetry.')}</div>${numberField('request_timeout_seconds','Request timeout (seconds)',s.request_timeout_seconds)}<p>Concurrency ceiling · Run limits <strong id="load-ceiling-label">${config.concurrency??'Not set'}</strong> <button type="button" class="text-button" data-action="edit-setting" data-setting-path="concurrency">Edit limit</button></p></section>
  <details class="load-section load-pass-criteria"><summary>Pass criteria <span class="dim">Shared across this check</span></summary><div class="field-label"><h3>Pass criteria</h3>${infoHelp('load_pass_criteria','Pass criteria','Applied per response where measurable. Leave timing or speed limits blank to measure without that threshold. A result group is a request type at one traffic pattern and level. Mixed traffic reports each selected combination separately too. Too few valid responses means inconclusive. For scheduled arrivals, timing starts at planned arrival, including dispatch delay. Maintained-concurrency timing starts at dispatch. Correctness requires scorable answers; missing judgments stay unknown. Incomplete traffic delivery, missing timing evidence or missing required length coverage cannot establish a capacity pass.')}</div><p class="hint">Leave timing or speed limits blank to measure only.</p><div class="fields">${Object.entries(loadCriteriaLabels).map(([key,label])=>numberField('pass_criteria.'+key,label,s.pass_criteria[key],{step:key==='min_valid_responses'?1:'any',placeholder:['max_error_percent','min_correct_percent','min_valid_responses'].includes(key)?'':'Measure only'})).join('')}</div></details></div>`;
}
function setLoadPanel(type,open) {
  state.loadPanels ||= {};state.loadPanels[type]=open;
  const panel=$(`#load-panel-${type}`),button=$(`[data-load-collapse="${type}"]`);
  if(panel)panel.hidden=!open;
  if(button){button.setAttribute('aria-expanded',String(open));button.setAttribute('aria-label',`${open?'Collapse':'Expand'} ${loadTitles[type]} settings`);button.textContent=open?'⌃':'⌄';}
}
function selectLoadShapes(kind) {
  const form=$('#new-run-form'),on=loadShapes.some(shape=>!form?.elements[`load_workloads_${kind}_selected_${shape}`]?.checked);
  for(const shape of loadShapes){const input=form?.elements[`load_workloads_${kind}_selected_${shape}`];if(input)input.checked=on;}
  configChanged();updateBounds();
}
function syncLoadDisplay(config) {
  const settings=loadSettings(config),w=settings.workloads,p=settings.patterns,live=loadDisplay(settings);
  const levels=$('#new-run-form')?.elements?.load_patterns_concurrent_levels;if(levels?.dataset.inherited==='true')levels.value=p.concurrent.levels.join(', ');
  for(const [id,text] of Object.entries({'load-share-note':live.shares,'load-output-threshold':live.coverage,'load-slowdown-help':live.slowdown,'load-interference-schedule':live.allocation,'load-ceiling-label':config.concurrency??'Not set','load-burst-preview':currentRequestPlan()?.load_test?.burst_previews?.[0]?.text||'Updating burst preview…'})){
    const node=$(`#${id}`);if(node&&node.textContent!==String(text))node.textContent=text;
  }
  const show=(id,on)=>{const node=$('#'+id);if(node)node.hidden=!on;};
  const standard=selectedStandardWorkloads(w);show('load-traffic',standard||w.interference.enabled);
  show('load-interference-pass',w.interference.enabled);show('load-shares',w.mixed.share_mode==='custom');show('load-arrival-fields',standard&&(p.constant.enabled||p.poisson.enabled));show('load-seed-field',p.poisson.enabled);show('load-burst-fields',standard&&p.bursts.enabled);show('load-concurrent-fields',p.concurrent.enabled||w.interference.enabled);show('load-stress-pass',w.stress.enabled&&w.stress.cases.some(shape=>shape.endsWith('_long')));
  for(const node of $$('[data-load-share-row]'))node.hidden=!w.mixed.selected.includes(node.dataset.loadShareRow);
  for(const type of Object.keys(loadTitles))$(`[data-load-type="${type}"]`)?.classList?.toggle('is-selected',w[type].enabled);
  for(const node of $$('[data-load-tokens]')){const [input,output]=node.dataset.loadTokens.split('_');node.textContent=`${settings.profile[input].input_tokens} → ${settings.profile[output].output_tokens} tokens`;}
}
function loadElapsed(seconds) {return `${Math.floor(seconds/60)}m ${Math.round(seconds%60)}s`;}
function renderLoadPlan(config,plan=null) {
  if(!config?.checks?.includes('load'))return '';
  const s=loadSettings(config),p=s.patterns,standard=selectedStandardWorkloads(s.workloads);
  const patterns=[p.concurrent.enabled&&'Concurrent requests',p.constant.enabled&&('Constant'+(p.bursts.enabled?' + bursts':'')),p.poisson.enabled&&('Poisson'+(p.bursts.enabled?' + bursts':'')),p.bursts.enabled&&!p.constant.enabled&&!p.poisson.enabled&&'Bursts only'].filter(Boolean);
  const title=standard&&patterns.length?patterns.join(' · '):s.workloads.interference.enabled?'Long-input / short-answer test':'Choose workloads',value=plan?.load_test;
  return `<section class="configured-load-plan" aria-label="Load test plan"><h3>Load test plan</h3><h4>${escape(title)}</h4>${value?`<dl class="load-plan-numbers"><div><dt>Scheduled load tests</dt><dd>${number(value.scheduled_tests)}${value.comparison_checks?' + '+number(value.comparison_checks)+' short-answer checks':''}</dd></div><div><dt>Sending / maintained-load time</dt><dd>${loadElapsed(value.configured_load_seconds)}</dd></div><div><dt>Including request deadlines &amp; comparison checks</dt><dd>${loadElapsed(value.time_with_allowances_seconds)}</dd></div><div><dt>Expected scheduled requests</dt><dd>${escape(value.expected_requests_label)}</dd></div></dl><h4>Execution order</h4><ol class="load-plan-breakdown">${(value.execution_summary||value.execution_order).map(text=>`<li>${escape(text)}</li>`).join('')}</ol>${(value.execution_order_notes||[]).map(note=>`<p class="hint load-execution-note">${escape(note)}</p>`).join('')}<details data-plan-check="load-order"><summary>Each scheduled load test</summary><ol>${value.execution_order.map(text=>`<li>${escape(text)}</li>`).join('')}</ol>${(value.execution_order_notes||[]).map(note=>`<p class="hint">${escape(note)}</p>`).join('')}</details><p class="hint">${escape(value.note)}</p>`:'<p class="hint">Waiting for the current load plan…</p>'}</section>`;
}

function renderLoadResults(metrics) {
  const reported=(value,digits=1)=>value==null?'not reported':number(value,digits),rate=value=>value==null?'not reported':number(value,2),ratio=value=>value==null?'not reported':`${number(value,2)}×`;
  const distribution=values=>values?.median==null?'not reported':`${number(values.median,1)} (${reported(values.min)}–${reported(values.max)})`;
  const lengths=(point,kind)=>Object.entries(point.token_lengths || {}).map(([shape,row])=>`<div><small>${escape(loadShapeLabel(shape))}</small>${escape(distribution(row[kind]))} / ${number(row[kind==='input_tokens'?'input_target':'output_cap'])}</div>`).join('') || 'not reported';
  const pointName=point=>point.type==='stress'?`${point.workload==='input'?'Large-input stress':'Large-output stress'} at ${counted(point.level,'concurrent request')}`:point.shape?loadShapeLabel(point.shape):point.type==='mixed'?'Mixed load':'Short control / long probe';
  const extra=point=>{
    let html='';
    if(point.observed_mix)html+=`<h4>Requested vs observed mix · ${counted(point.level,'concurrent request')}</h4><div class="metric-table"><table><thead><tr><th>Shape</th><th>Requested share</th><th>Sent requests</th><th>Observed by sent requests</th><th>In-flight time</th><th>Observed by in-flight time</th></tr></thead><tbody>${Object.entries(point.observed_mix).map(([shape,row])=>`<tr><th scope="row">${escape(loadShapeLabel(shape))}</th><td>${reported(row.requested_percent)}%</td><td>${number(row.sent)}</td><td>${reported(row.sent_percent)}%</td><td>${reported(row.inflight_seconds)} s</td><td>${reported(row.inflight_percent)}%</td></tr>`).join('')}</tbody></table></div><p class="hint">Requested shares are request frequency. Observed in-flight time includes drain.</p>`;
    if(point.interference){const value=point.interference;html+=`<h4>Matched interference groups · ${counted(point.level,'concurrent request')}</h4><p>${escape(value.allocation || '')}</p><div class="metric-table"><table><thead><tr><th>Matched short streams</th><th>Matched count</th><th>Decode median</th><th>Response median</th><th>Longest stream pause</th></tr></thead><tbody>${[['control','Short-only control'],['probe','Long-input probe']].map(([key,title])=>{const row=value[key] || {};return `<tr><th scope="row">${title}</th><td>${number(row.matched_count)}</td><td>${ms(row.decode_median_ms)}</td><td>${ms(row.response_median_ms)}</td><td>${ms(row.longest_stream_pause_ms)}</td></tr>`;}).join('')}</tbody></table></div><p>Slowdown: ${ratio(value.slowdown)} · response-time ratio: ${ratio(value.response_time_ratio)}</p><p class="hint">Trigger latency median / min / max: ${escape(distribution(value.trigger_latency_ms))} ms · probes never triggered: ${number(value.probes_never_triggered)}${value.buffered_stream?' · stream is buffered; interruption cannot be measured':''}</p><p class="hint">Short-stream concurrency mean / peak / server-evidenced: ${reported(point.short_stream_mean_inflight)} / ${reported(point.short_stream_peak_inflight)} / ${reported(point.short_stream_evidenced_concurrency)}. Buffered short streams: ${reported(value.buffered_short_streams)} / ${reported(value.valid_short_streams)}; excluded from matching. Ambiguous A/B overlaps excluded from both groups: ${reported(value.ambiguous_short_streams)}.</p>`;}
    if(point.coverage)html+=`<p class="load-coverage">Large-output coverage at ${counted(point.level,'concurrent request')}: ${number(point.coverage.covered_valid_responses)} / ${number(point.coverage.required)} required valid responses generated at least ${counted(point.coverage.threshold_tokens,'token')}. Length coverage alone never establishes a pass.</p>`;
    if(point.type==='stress'&&point.workload==='input')html+=`<p class="load-coverage">Large-input coverage at ${counted(point.level,'concurrent request')}: ${point.reason_code==='input_not_verified'?'input length not verified':point.input_not_calibrated?'input length not calibrated':'Actual input lengths and targets are shown above.'}</p>`;
    return html;
  };
  const groups=Object.entries(metrics.load_types || {}),passed=groups.flatMap(([,group])=>group.points || []).filter(point=>point.status==='pass');
  return `<div class="concurrent-load-results"><p>Configured load time: ${escape(loadElapsed(metrics.configured_load_seconds || 0))} per endpoint.</p>${passed.length?`<p>Highest observed among tested settings: ${counted(Math.max(...passed.map(point=>point.level)),'concurrent request')}.</p>`:''}${metrics.migration_notes?.length?`<details><summary>Configuration migration notes</summary><ul>${metrics.migration_notes.map(note=>`<li>${escape(note)}</li>`).join('')}</ul></details>`:''}${groups.map(([type,group])=>`<section class="check-observations concurrent-result" data-load-result="${escape(type)}"><h3>${escape(group.title || loadTitles[type])} ${group.status?badge(group.status):''}</h3><div class="metric-table load-results-table" tabindex="0" role="region" aria-label="${escape(group.title || loadTitles[type])} test-point measurements"><table><thead><tr>${['Workload','Level (requested concurrency)','Sending window (delivered / configured)','Sent / completed','Errors (failed / timeouts)','Valid / required','Correct (%)','Client-delivered concurrency (mean / peak)','Server-evidenced concurrency','First-output p95','Response p95','Requests/s','Output tokens/s','Input tokens actual (median, min–max) / target','Output tokens actual (median, min–max) / cap','Status / reason'].map(title=>`<th scope="col">${title}</th>`).join('')}</tr></thead><tbody>${(group.points || []).map(point=>`<tr data-load-point="${escape(point.id)}"><th scope="row">${escape(pointName(point))}</th><td>${number(point.requested_concurrency ?? point.level)}</td><td>${reported(point.delivered_window_seconds)} / ${reported(point.configured_window_seconds)} s<small>Drain ${reported(point.drain_seconds)} s${point.cut_short?' · cut short':''}${point.not_run?' · not run':''}</small></td><td>${number(point.sent)} / ${number(point.completed)}</td><td>${number(point.failed)} / ${number(point.timed_out)}<small>Cancelled ${number(point.cancelled)} · reasoning budget exhausted ${number(point.reasoning_budget_exhausted)}</small></td><td>${number(point.valid)} / ${number(point.required)}</td><td>${point.correctness_applicable===false?'not applicable':reported(point.correct_percent)}<small>${number(point.correct)} correct / ${number(point.scored)} scored</small></td><td>${reported(point.client_mean_inflight)} / ${reported(point.client_peak_inflight)}</td><td>${reported(point.server_evidenced_concurrency)}<small>${escape(point.concurrency_reason || (point.load_qualified?'Load qualified':'Load not qualified'))}</small></td><td>${ms(point.first_output_p95_ms)}</td><td>${ms(point.response_p95_ms)}</td><td>${rate(point.requests_per_second)}</td><td>${rate(point.output_tokens_per_second)}${point.throughput_warning?`<small>${escape(point.throughput_warning)}</small>`:''}</td><td>${lengths(point,'input_tokens')}</td><td>${lengths(point,'output_tokens')}</td><td>${badge(point.status || 'running')}<small>${escape(point.reason || 'Measurements pending')}</small></td></tr>`).join('')}</tbody></table></div>${(group.points || []).map(extra).join('')}<details><summary>All test-point measurements and evidence IDs</summary><pre>${escape(JSON.stringify(group.points,null,2))}</pre></details></section>`).join('')}<details class="load-measurement-note" open><summary>How these are measured</summary><p>Each sending window replenishes individual slots until its duration ends or a run limit stops it. In-flight requests drain afterward and remain in the measured population. Client-delivered concurrency is the time-weighted mean and peak in-flight during the sending window; server-evidenced concurrency requires overlapping output. Requested concurrency is a setting, not proof of server delivery. Interference qualifies its N−1 continuous short streams plus enough matched probes in both groups; probe trigger idle time does not count against short-stream load. Throughput covers first completion to sending-end; short windows can be unreliable. With few samples, p95 is effectively the maximum. Missing token counts are not reported, never assumed to be zero. Input lengths are targets; output lengths are caps and answers can finish earlier.</p></details>${metrics.calibrations?.length?`<details><summary>Local input preparation</summary><pre>${escape(JSON.stringify(metrics.calibrations,null,2))}</pre></details>`:''}</div>`;
}
function loadPointIdentity(id) {
  const match=/^load_(primary|legacy_.+?)_(combinations|mixed|stress)_([a-z_]+?)_(concurrent|constant|poisson|bursts)(_bursts)?_(?:c([0-9]+)|r([0-9.e+-]+)|only)(?:\.|$)/.exec(id);
  if(match)return {id,source:match[1],type:match[2],workload:match[3],shape:loadShapes.includes(match[3])?match[3]:null,pattern:match[4],bursts:Boolean(match[5])||match[4]==='bursts',level:match[6]?Number(match[6]):null,rate:match[7]?Number(match[7]):null};
  const comparison=/^load_(primary|legacy_.+?)_interference_c([0-9]+)(?:\.|$)/.exec(id);
  return comparison?{id,source:comparison[1],type:'interference',workload:'interference',pattern:'interference',level:Number(comparison[2])}:null;
}
function loadPointLabel(point) {
  const traffic={concurrent:'Maintain concurrent requests',constant:'Constant',poisson:'Poisson',bursts:'Bursts only',interference:'Short-answer comparison'}[point.pattern]||human(point.pattern);
  return `${loadTitles[point.type]||human(point.type)}${point.shape?' · '+loadShapeLabel(point.shape):''} · ${traffic}${point.bursts&&point.pattern!=='bursts'?' + bursts':''}${point.level!=null?' · '+counted(point.level,'concurrent request'):point.rate!=null?' · '+point.rate+' requests/second':''}${point.source&&point.source!=='primary'?' · '+human(point.source):''}`;
}
function renderAcceptedLoad(metrics) {
  if(!Object.hasOwn(metrics,'highest_accepted'))return '';
  const key=row=>JSON.stringify([row.source||'primary',row.type,row.workload,row.pattern,Boolean(row.bursts)]),accepted=new Map((metrics.highest_accepted||[]).map(row=>[key(row),row]));
  const scopes=new Map((metrics.points||[]).filter(point=>['concurrent','constant','poisson'].includes(point.pattern)).map(point=>[key(point),point]));
  for(const row of metrics.highest_accepted||[])if(!scopes.has(key(row)))scopes.set(key(row),row);
  return [...scopes].map(([id,scope])=>{const row=accepted.get(id),label=scope.pattern==='concurrent'?'Highest accepted concurrency':'Highest accepted request rate';return `<p class="highest-accepted"><strong>${label} (tested): ${row?number(row.value,2):'Unconfirmed'}</strong> · ${escape(loadPointLabel({...scope,level:null,rate:null}))}<small>${escape(row?.qualification||'No complete, accepted result established this value; maximum capacity unknown.')}</small></p>`;}).join('');
}
function loadOutputCoverage(coverage) {
  if(!coverage)return 'not required';
  const thresholds=[...new Set(Object.values(coverage.threshold_tokens_by_request||{}))].sort((a,b)=>a-b);
  const threshold=coverage.threshold_tokens!=null?`at least ${number(coverage.threshold_tokens)} tokens`:`per-request sent-cap thresholds (${thresholds.map(n=>number(n)).join(', ')} tokens)`;
  const configured=coverage.profile_output_cap!=null&&coverage.threshold_tokens!=null?` (${number(coverage.min_output_percent)}% of Long output cap ${number(coverage.profile_output_cap)}); actual sent budgets: ${(coverage.sent_budgets||[]).map(n=>number(n)).join(', ')} tokens`:'';
  return `${number(coverage.output_covered)} / ${number(coverage.required)} ${threshold}${configured}; ${number(coverage.input_verified)} input verified`;
}
function renderLoadGroupQualification(group) {
  const q=group.input_qualification,budgets=group.saved_question_budgets||[];
  if(!q&&!budgets.length)return '';
  return `<section class="load-group-qualification" data-live-key="qualification:${escape(group.id)}"><h4>${escape(loadShapeLabel(group.shape))}</h4>${q?`<p>Input shape qualification: ${number(q.in_class)} / ${number(q.known_valid)} known-length valid responses in ${escape(human(q.size))} class; ${number(q.out_of_class)} outside; ${number(q.unknown_valid)} unknown-length valid responses.</p><p>${escape(q.reason||'Input class exercised.')}</p>`:''}${budgets.map(row=>`<p>${escape(row.note)}</p>`).join('')}</section>`;
}
function renderLoadTestResults(metrics) {
  const reported=(n,digits=1)=>n==null?'not reported':number(n,digits),distribution=row=>row?.median==null?'not reported':`${reported(row.median)} (${reported(row.min)}–${reported(row.max)})`;
  const table=(head,rows,label)=>`<div class="metric-table load-results-table" tabindex="0" role="region" aria-label="${escape(label)}"><table><thead><tr>${head.map(text=>`<th scope="col">${escape(text)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
  return `<div class="load-test-results">${renderAcceptedLoad(metrics)}${metrics.migration_notes?.length?`<details><summary>Configuration migration notes</summary><ul>${metrics.migration_notes.map(note=>`<li>${escape(note)}</li>`).join('')}</ul></details>`:''}${metrics.workload_source?`<p class="hint">Workload source: ${escape(metrics.workload_source)}. ${escape(metrics.context_note||'')}</p>`:''}${(metrics.points||[]).map(point=>`<article class="check-observations load-test-point" data-load-point="${escape(point.id)}" data-live-key="point:${escape(point.id)}"><div class="check-detail-heading"><h3>${escape(loadPointLabel(point))}</h3>${badge(point.status||'running')}</div><p>${escape(point.reason||'Measurements pending')}</p><p class="hint">${point.windows===2?'Baseline and probe sending windows':'Sending window'}: ${reported(point.delivered_window_seconds)} seconds delivered · ${reported(point.configured_window_seconds)} seconds configured per window · finish: ${reported(point.drain_seconds)} seconds · timing starts at ${escape(point.timing_clock||'not reported')}${point.cut_short?' · Stopped early':''}.</p>
  ${table(['Result group','Requested / scheduled / dispatched','Local not-sent / late / not offered','Completed / valid / required','Failures / timeouts / cancellations','Correct / scored / unknown','Client concurrency mean / peak / server-evidenced','Requested / observed request share','Status / reason'],(point.groups||[]).map(group=>`<tr data-live-key="group:${escape(group.id)}"><th scope="row">${escape(loadShapeLabel(group.shape))}</th><td>${group.requested==null?'Unknown':number(group.requested)} / ${number(group.scheduled)} / ${number(group.dispatched)}</td><td>${number(group.local_drops)} / ${number(group.late_arrivals)} / ${number(group.not_offered)}<small>${number(group.not_sent)} total not sent; excluded from endpoint errors</small></td><td>${number(group.completed)} / ${number(group.valid)} / ${number(group.required)}</td><td>${number(group.failed)} / ${number(group.timed_out)} / ${number(group.cancelled)}<small>${reported(group.error_percent)}% errors; ${escape(group.error_denominator_description||'')}</small></td><td>${number(group.correct)} / ${number(group.scored)} / ${number(group.unscored)}<small>${reported(group.correct_percent)}% correct</small></td><td>${reported(group.client_mean_inflight)} / ${reported(group.client_peak_inflight)} / ${reported(group.server_evidenced_concurrency)}</td><td>${reported(group.requested_share_percent)}% / ${reported(group.observed_request_share_percent)}%</td><td>${badge(group.status||'running')}<small>${escape(group.reason||'Measurements pending')}</small></td></tr>`),'Load test result groups')}
  ${table(['Request type','First answer p50 / p95','Completion p50 / p95','Longest streamed pause','Generation tokens/sec median (min–max)','Dispatch delay median (min–max)','Timing evidence'],(point.groups||[]).map(group=>`<tr data-live-key="timing:${escape(group.id)}"><th scope="row">${escape(loadShapeLabel(group.shape))}</th><td>${ms(group.first_answer_p50_ms)} / ${ms(group.first_answer_p95_ms)}</td><td>${ms(group.completion_p50_ms)} / ${ms(group.completion_p95_ms)}</td><td>${ms(group.longest_stream_pause_ms)}</td><td>${distribution(group.generation_tokens_per_second)}<small>${escape(group.lengths?.generation_speed_method||'Client-observed estimate after first output')}</small></td><td>${distribution(group.dispatch_delay_ms)}${group.dispatch_delay_ms?.median==null?'':' ms'}</td><td>${escape(group.timing_clock||'not reported')}<small>${escape(group.timing_quality?.status||'not reported')} · ${number(group.timing_violations?.length)} attributed timing violations · ${number(group.unattributed_timing_violations?.length)} unattributed · ${escape((group.missing_timing||[]).join(', '))}</small></td></tr>`),'Response timing and generation speed')}
  ${table(['Request type','Actual input median (min–max) / target','Actual output median (min–max) / cap','Unknown input / output lengths','Required length coverage','Completed requests/second','Aggregate output tokens/second','Correct work meeting limits','Throughput window'],(point.groups||[]).map(group=>{const lengths=group.lengths||{},coverage=group.coverage;return `<tr data-live-key="throughput:${escape(group.id)}"><th scope="row">${escape(loadShapeLabel(group.shape))}</th><td>${distribution(lengths.input_tokens)} / ${reported(lengths.input_target)}<small>${escape(lengths.input_method||'')} · shortfall tokens: ${escape((lengths.input_shortfall_tokens||[]).slice(0,6).map(n=>n==null?'unknown':number(n)).join(', ')||'not reported')}${lengths.input_shortfall_tokens?.length>6?' …':''}</small></td><td>${distribution(lengths.output_tokens)} / ${reported(lengths.output_cap)}<small>${escape(lengths.output_method||'')}</small></td><td>${number(lengths.unknown_input_count)} / ${number(lengths.unknown_output_count)}</td><td>${loadOutputCoverage(coverage)}${group.case_coverage?`<small>${escape(JSON.stringify(group.case_coverage))}</small>`:''}</td><td>${reported(group.completed_requests_per_second,2)}</td><td>${reported(group.aggregate_output_tokens_per_second,2)}</td><td>${number(group.correct_work_meeting_limits)} · ${reported(group.correct_work_per_second,2)}/s</td><td>${reported(group.throughput_window_seconds)} s observation / ${reported(group.sending_window_seconds)} s sending<small>${number(group.completed_during_finish)} completions after sending stops; ${number(group.completed_in_sending_window)} during sending. ${escape(group.throughput_description||'')}</small></td></tr>`;}),'Token coverage and throughput')}
  ${(point.groups||[]).map(renderLoadGroupQualification).join('')}
  ${point.observed_mix&&Object.keys(point.observed_mix).length?table(['Requested vs observed mix','Requested share','Dispatched','Observed share'],Object.entries(point.observed_mix).map(([shape,mix])=>`<tr data-live-key="mix:${escape(shape)}"><th scope="row">${escape(loadShapeLabel(shape))}</th><td>${reported(mix.requested_percent)}%</td><td>${number(mix.dispatched)}</td><td>${reported(mix.observed_percent)}%</td></tr>`),'Requested vs observed request mix'):''}
  ${point.interference?`<section class="load-comparison" data-live-key="comparison:${escape(point.id)}"><h4>Short-only vs long-request comparison</h4>${table(['Matched windows','Matched short answers','Completion median','Longest stream pause'],['baseline','probe'].map(phase=>{const row=point.interference[phase]||{};return `<tr><th scope="row">${phase==='baseline'?'Short answers alone':'With a long request'}</th><td>${number(row.matched_count)}</td><td>${ms(row.completion_median_ms)}</td><td>${ms(row.longest_stream_pause_ms)}</td></tr>`;}),'Interference comparison')}<p>Slowdown: ${reported(point.interference.slowdown,2)}× · extra pause: ${ms(point.interference.extra_pause_ms)} · ${number(point.interference.matched_measurement_windows?.length)} matched measurement windows · ${number(point.interference.actual_overlap_request_ids?.length)} actual overlapping requests · trigger ${point.interference.triggered?'exercised':'not exercised'}.</p><p class="hint">${escape(point.interference.comparison_metric||'')} · ${escape(point.interference.timing_quality?.status||'Timing evidence not reported')}</p></section>`:''}
  ${point.burst_delivery?.length?`<section data-live-key="bursts:${escape(point.id)}"><h4>Burst delivery and degradation</h4>${table(['Burst','Scheduled / dispatched / not sent','Dispatch spread','Completion p50'],point.burst_delivery.map(row=>`<tr><th scope="row">${number(row.index)}</th><td>${number(row.scheduled)} / ${number(row.dispatched)} / ${number(row.not_sent)}</td><td>${ms(row.spread_ms)}</td><td>${ms(row.completion_p50_ms)}</td></tr>`),'Burst delivery')}${table(['Traffic origin','Dispatched','Completion p50','Failures'],Object.entries(point.response_by_origin||{}).map(([origin,row])=>`<tr><th scope="row">${escape(origin)}</th><td>${number(row.dispatched)}</td><td>${ms(row.completion_p50_ms)}</td><td>${number(row.failed)}</td></tr>`),'Arrival and burst response degradation')}</section>`:''}
  <details data-live-key="evidence:${escape(point.id)}"><summary>All result-group measurements and evidence IDs</summary><pre>${escape(JSON.stringify({...point,offers:undefined},null,2))}</pre></details></article>`).join('')}<p class="hint">p95 is a descriptive metric. Acceptance limits apply per response. Local not-sent offers and cancellations are separate from endpoint failures. Completions after sending stops are included in both the completion count and the observation window used for throughput. Missing measurements stay unknown.</p></div>`;
}
function renderLiveLoadResults(detail) {
  const points=new Map();
  for(const row of detail.live_conditions || [])if(row.load_point_result)points.set(`${row.target}:${row.load_point_result.id}`,{target:row.target,point:row.load_point_result});
  const targets=new Map();
  for(const {target,point} of points.values()){if(!targets.has(target))targets.set(target,[]);targets.get(target).push(point);}
  return [...targets].map(([target,points])=>`<section class="panel" data-live-key="load:${escape(target)}"><h2>Load test results · ${escape(target)}</h2>${points.some(point=>point.pattern)?renderLoadTestResults({points}):renderLoadResults({load_types:Object.fromEntries(Object.entries(Object.groupBy(points,point=>point.type)).map(([type,points])=>[type,{points,title:loadTitles[type]}]))})}</section>`).join('');
}

function renderConcurrentResults(metrics) {
  if(metrics.load_types)return renderLoadResults(metrics);
  const tokenRange=values=>values?.length?`${Math.min(...values)}–${Math.max(...values)}`:'Unmeasured';
  return Object.entries(metrics.scenarios || {}).map(([name,scenario])=>{
    const m=scenario.metrics || {};
    const cells=m.buckets?`<div class="metric-table"><table><thead><tr><th>Size / concurrency</th><th>Completed / sent</th><th>Correct / scored</th><th>Observed overlap</th><th>Input / output tokens</th><th>First output p95</th><th>Response p95</th><th>Coverage</th></tr></thead><tbody>${Object.entries(m.buckets).map(([key,row])=>`<tr><td>${escape(scenarioLabel({scenario:key,workload:row.workload}))}</td><td>${number(row.completed)} / ${number(row.attempted)}</td><td>${number(row.correct)} / ${number(row.scored)}</td><td>${number(row.achieved_overlap)}</td><td>${tokenRange(row.actual_input_tokens)} / ${tokenRange(row.actual_output_tokens)}</td><td>${ms(row.first_output_p95_ms)}</td><td>${ms(row.p95_ms)}</td><td>${escape((row.coverage_gaps || []).join(' ') || 'Recorded; see scenario verdict')}</td></tr>`).join('')}</tbody></table></div>`:'';
    const pairs=m.comparisons?`<div class="metric-table"><table><thead><tr><th>Pair / starting order</th><th>Short-only p50</th><th>Mixed p50 / p95</th><th>Slowdown</th><th>Overlap / sample coverage</th></tr></thead><tbody>${m.comparisons.map(row=>`<tr><td>${escape(scenarioLabel(row))}</td><td>${ms(row.short_only?.p50_ms)}</td><td>${ms(row.mixed_short?.p50_ms)} / ${ms(row.mixed_short?.p95_ms)}</td><td>${row.latency_ratio==null?'Unmeasured':`${number(row.latency_ratio,2)}×`}</td><td>${row.overlap_observed?'Observed':'Unverified'} / ${row.samples_and_overlap_qualified?'Sufficient':'Insufficient'}</td></tr>`).join('')}</tbody></table></div>`:'';
    const cases=m.cases?Object.values(m.cases).map(item=>`<div class="check-observations"><h4>${escape(item.title)} ${badge(item.status)}</h4><p>Requested input/output: ${item.requested_input_tokens==null?'Short prompt':number(item.requested_input_tokens)} / ${number(item.requested_output_tokens)} tokens. Minimum output: ${number(item.minimum_output_tokens)}.</p><p>Observed input: ${tokenRange(item.actual_input_tokens)}; output: ${tokenRange(item.actual_output_tokens)}. ${item.calibration_requests.length} input-sizing requests.</p>${item.coverage_gaps.length?`<p class="hint">${escape(item.coverage_gaps.join(' '))}</p>`:''}<details><summary>Each concurrency level and all measurements</summary><pre>${escape(JSON.stringify(item.levels,null,2))}</pre></details></div>`).join(''):'';
    return `<section class="check-observations concurrent-result" data-concurrent-scenario="${escape(name)}"><h3>${escape(scenario.title)} ${badge(scenario.status)}</h3><p>${escape(scenario.summary)}</p><dl class="metrics-grid">${metric('Completed / sent',`${number(m.completed)} / ${number(m.attempted)}`)}${metric('Failures / timeouts',`${number(m.failed)} / ${number(m.timed_out)}`)}${metric('Correct / scored',`${number(m.correct)} / ${number(m.scored)}`)}${metric('First output p95',ms(m.first_output_p95_ms))}${metric('Response p95',ms(m.p95_ms))}${metric('Longest stream pause',ms(m.max_stream_gap_ms))}${name==='streaming_fairness'?metric('Mixed / short-only latency',m.latency_ratio==null?'Unmeasured':`${number(m.latency_ratio,2)}×`)+metric('Observed streaming overlap',`${m.overlaps ?? 0} / ${m.pairs ?? 0}`):''}</dl>${cells?`<details><summary>${name==='size_matrix'?'Request shapes and concurrency cells':'Request groups'} (${Object.keys(m.buckets).length})</summary>${cells}</details>`:''}${pairs?`<details><summary>Interference comparisons (${m.comparisons.length})</summary>${pairs}</details>`:''}${cases}<details><summary>All scenario measurements and evidence IDs</summary><pre>${escape(JSON.stringify(scenario,null,2))}</pre></details></section>`;
  }).join('');
}

function renderSessionResults(performance) {
  const rate=value=>value==null?'Unmeasured':`${number(value,1)} tok/s`;
  const columns=row=>`<td>${row.completed ?? 0} / ${row.attempted ?? 0}</td><td>${rate(row.generation_tokens_per_second_mean)}</td><td>${rate(row.generation_tokens_per_second_p50)}</td><td>${rate(row.generation_tokens_per_second_p10)}</td><td>${ms(row.first_output_p95_ms)}</td><td>${ms(row.p95_ms)}</td><td>${ms(row.max_stream_gap_ms)}</td><td>${row.unmeasured_generation_requests ?? 0}</td><td>${row.capped_responses ?? 0}</td>`;
  const header='<th>Completed / sent</th><th>Mean tok/s</th><th>Median tok/s</th><th>p10 tok/s</th><th>First output p95</th><th>Response p95</th><th>Longest pause</th><th>Unmeasured rate</th><th>Capped replies</th>';
  return `<section class="check-observations"><h3>Conversation performance</h3><dl class="metrics-grid">${metric('Average generation speed per user',rate(performance.mean_user_generation_tokens_per_second),`Equal weight for ${performance.users_with_measured_generation} measured users; excludes first-output waiting and inter-turn pauses`)}${metric('Conversation completion',`${performance.completed_conversations} / ${performance.planned_conversations}`,'All planned turns completed; output caps and answer quality are reported separately')}${metric('Concurrent requests observed',`${performance.achieved_request_overlap} / ${performance.configured_concurrency}`,'Observed client overlap / configured conversation concurrency; users may pause between turns')}${metric('Total output throughput',rate(performance.aggregate_output_tokens_per_second),'All measured users combined over the recorded workload window, including pauses')}</dl><p class="hint">Raw client-observed estimates; see client timing quality above. p10 describes slower responses. Capped replies are not quality-certified.</p>
  <h3>By task type</h3><div class="metric-table"><table><thead><tr><th>Task</th>${header}</tr></thead><tbody>${performance.tasks.map(row=>`<tr><td>${escape(row.task_type)}<small>${escape(row.mode)}</small></td>${columns(row)}</tr>`).join('')}</tbody></table></div>
  <details><summary>By conversation turn</summary><div class="metric-table"><table><thead><tr><th>Task / turn</th>${header}</tr></thead><tbody>${performance.turns.map(row=>`<tr><td>${escape(row.task_type)} · turn ${row.turn}<small>${escape(row.mode)}</small></td>${columns(row)}</tr>`).join('')}</tbody></table></div></details>
  <details><summary>Each user's results</summary><div class="metric-table"><table><thead><tr><th>User / task</th>${header}<th>Conversation</th></tr></thead><tbody>${performance.users.map(row=>`<tr><td>${escape(row.session_id)}<small>${escape(row.task_type)}</small></td>${columns(row)}<td>${row.conversation_completed?'Completed':escape(row.stop_reason || 'Incomplete')}</td></tr>`).join('')}</tbody></table></div></details></section>`;
}

function renderSessionEditor(config) {
  const defaults=state.bootstrap.session_workloads,settings={...defaults.defaults,...profileDefaults('sessions',config),...config.sessions};
  const mixed=settings.profile==='performance',saved=settings.workloads || [];
  const workloads=[...saved,...defaults.presets.filter(item=>!saved.some(row=>row.name===item.name))];
  return `<div data-session-settings>${selectField('sessions_profile','Conversation test',`<option value="correctness" ${mixed?'':'selected'}>Exact-repeat correctness</option><option value="performance" ${mixed?'selected':''}>Mixed workload performance</option>`,{hint:'',help:'Mixed performance runs use actual replies as conversation history. Concurrent users share Simultaneous limit with other target requests. Answer quality is not scored.'})}
  <div data-profile-settings="sessions" class="fields">${schemaField('profile_sessions_concurrency',settings.concurrency,state.bootstrap.config_schema.$defs.Sessions.properties.concurrency,{label:'Concurrent conversations',hint:'',help:'An accepted value is not demonstrated delivery capacity.'})}${schemaField('profile_sessions_max_tokens',settings.max_tokens,state.bootstrap.config_schema.$defs.Sessions.properties.max_tokens,{label:'Output cap per turn',hint:'',help:'Own answer cap for every turn. The suite default is not a ceiling.'})}</div>
  <div data-session-mix ${mixed?'':'hidden'}><div class="fields">${field('sessions_count','Total conversations',mixed?settings.sessions:Math.max(6,settings.sessions),{type:'number',step:1,hint:'',help:'Weights divide the total conversation budget among selected tasks. Each conversation keeps its task and history. Output caps limit length; actual input/output tokens are reported.'})}${field('sessions_delay','Pause between turns (seconds)',settings.delay_seconds,{type:'number',step:'any'})}</div>
  <input type="hidden" name="session_workload_count" value="${workloads.length}">${workloads.map((item,i)=>`<details class="setup-disclosure"><summary>${escape(human(item.name))} · ${item.turns.length} turns</summary><label class="test-selection"><input type="checkbox" name="session_enabled_${i}" ${(saved.length?saved.some(row=>row.name===item.name):true)?'checked':''}><span>Include this task</span></label><div class="fields">${field(`session_name_${i}`,'Task name',item.name,{required:true})}${field(`session_weight_${i}`,'Conversation weight',item.weight,{type:'number',step:'any',required:true})}${field(`session_cap_${i}`,'Task output cap (optional)',item.max_tokens,{type:'number',step:1,placeholder:'Use conversation output cap'})}</div><input type="hidden" name="session_turn_count_${i}" value="${item.turns.length}">${item.turns.map((turn,j)=>renderField(`session_turn_${i}_${j}`,`Turn ${j+1}`,`<textarea id="field-session_turn_${i}_${j}" name="session_turn_${i}_${j}" rows="3" maxlength="${state.bootstrap.limits['sessions.workloads[].turns']?.items?.maxLength||state.bootstrap.limits['sessions.turns']?.items?.maxLength}" required>${escape(turn)}</textarea>`)).join('')}<button type="button" class="text-button" data-action="add-session-turn" data-session-index="${i}">Add turn</button>${item.turns.length>state.bootstrap.limits['sessions.workloads[].turns'].minItems?`<button type="button" class="text-button" data-action="remove-session-turn" data-session-index="${i}">Remove last turn</button>`:''}</details>`).join('')}
  <div class="field-label"><button type="button" class="text-button" data-action="add-session-workload" ${workloads.length>=state.bootstrap.limits['sessions.workloads'].maxItems?'disabled':''}>Add custom task</button>${infoHelp('session_workload_caps','Add custom task',[state.bootstrap.cap_explanations?.['sessions.workloads']?.help,state.bootstrap.cap_explanations?.['sessions.workloads[].turns']?.help].filter(Boolean).join(' '))}</div></div></div>`;
}

function editSessionWorkload(action,index) {
  const form=$('#new-run-form'),name=form.elements[`session_name_${index}`]?.value;
  if(index!==undefined&&!form.elements[`session_enabled_${index}`]?.checked){toast('Select this task before changing its turn count.');return;}
  captureFormDraft();
  const settings=state.config.sessions;
  if(action==='add-session-workload'){
    let suffix=1;while(settings.workloads.some(item=>item.name===`custom-${suffix}`))suffix++;
    settings.workloads.push({name:`custom-${suffix}`,weight:1,max_tokens:null,turns:['Explain a topic relevant to your users in detail.','Build on your previous answer with a detailed example.']});
  }else{
    const item=settings.workloads.find(row=>row.name===name);if(!item)return;
    if(action==='add-session-turn'&&item.turns.length<state.bootstrap.limits['sessions.workloads[].turns'].maxItems)item.turns.push('Build on your previous answer and explain the next step in detail.');
    if(action==='remove-session-turn'&&item.turns.length>state.bootstrap.limits['sessions.workloads[].turns'].minItems)item.turns.pop();
  }
  configChanged();state.jsonDraft=null;state.jsonBase=null;renderNew();
  state.openTest='sessions';syncTestControls();
}


function syncTestControls() {
  const form=$('#new-run-form');if(!form)return;
  let selectedCount=0,visibleCount=0;
  const search=(state.catalogueSearch || '').trim().toLowerCase();
  for(const check of state.bootstrap.checks){
    const card=$(`[data-test-card="${check.id}"]`),selected=$('input[name="check"]',card).checked;
    if(selected)selectedCount++;
    const open=state.openTest===check.id;
    card.classList.toggle('test-disabled',!selected);card.classList.toggle('is-selected',selected);card.classList.toggle('is-editing',open);
    card.hidden=state.catalogueFilter==='selected'?!selected:(state.catalogueCategory&&state.catalogueCategory!=='all'&&state.catalogueCategory!==card.dataset.category)||Boolean(search&&!(card.dataset.search || '').includes(search));
    if(!card.hidden)visibleCount++;
    const editor=$('[data-test-editor]',card);editor.hidden=!open;
    $('[data-configure-test]',card).setAttribute('aria-expanded',String(open));
    $('.draft-notice',card).hidden=selected;
    for(const selector of ['[data-check-override-warning]','[data-check-coverage]']){
      const warning=$(selector,card);if(warning)warning.hidden=!(selected||open)||!warning.textContent;
    }
    for(const fieldset of $$('fieldset',editor))fieldset.disabled=!open;
  }
  const sessionProfile=form.elements.sessions_profile;
  if(sessionProfile){const mixed=sessionProfile.value==='performance';$('[data-session-mix]',form).hidden=!mixed;const fixed=$('[data-session-fixed]',form);if(fixed)fixed.hidden=mixed;}
  for(const group of $$('[data-catalogue-group]')){
    const cards=$$('[data-test-card]',group);group.hidden=cards.every(card=>card.hidden);
    const chosen=cards.filter(card=>card.querySelector?.('input[name="check"]')?.checked).length;
    const count=group.querySelector?.('[data-group-count]'),toggle=group.querySelector?.('[data-group-toggle]');
    if(count)count.textContent=`${chosen} of ${cards.length}`;group.classList?.toggle?.('has-selection',chosen>0);if(toggle)toggle.textContent=chosen===cards.length?'Clear':'Select all';
  }
  for(const button of $$('.check-intents [data-category]'))button.setAttribute('aria-pressed',String(state.catalogueFilter==='all'&&button.dataset.category===state.catalogueCategory));
  for(const button of $$('.catalogue-categories [data-category]'))button.setAttribute('aria-pressed',String(button.dataset.category===(state.catalogueCategory || 'all')));
  for(const button of $$('[data-catalogue-filter]'))button.setAttribute('aria-pressed',String(button.dataset.catalogueFilter===(state.catalogueFilter || 'all')));
  const count=$('#catalogue-selected-count');if(count)count.textContent=selectedCount;
  const browse=$('#catalogue-browse-tools');if(browse)browse.hidden=state.catalogueFilter==='selected';
  const empty=$('#catalogue-empty');if(empty){empty.hidden=visibleCount>0;empty.textContent=state.catalogueFilter==='selected'?'No checks selected. Browse all checks to add one.':'No matching checks. Clear the search or choose All categories.';}
  const none=$('#no-tests-selected');if(none)none.hidden=selectedCount>0;
  syncPackControls();
}
function revealCatalogueCheck(id) {
  const card=$(`[data-test-card="${id}"]`);
  if(!card||card.hidden){
    state.catalogueFilter='all';state.catalogueCategory='all';state.catalogueSearch='';
    const search=$('#check-search');if(search)search.value='';
  }
  state.openTest=id;syncTestControls();
}
function revealQuestionEditor(config=state.config) {
  revealSetupStep(2);
  revealCatalogueCheck(config.checks.includes('correctness')?'correctness':'load');
}
function editProfileField(input) {
  const profile=input.closest('[data-profile-settings]');if(!profile)return;
  const key=profile.dataset.profileSettings,name=input.name.replace(`profile_${key}_`,'');
  const marker=$('#new-run-form').elements[`profile_edits_${key}`];
  const edits=new Set(JSON.parse(marker.value || '[]'));edits.add(name);marker.value=JSON.stringify([...edits]);
}

function toggleTestEditor(id,close=false) {
  if(state.openTest&&!validateProfileDrafts($('#new-run-form'),state.openTest))return;
  state.openTest=close||state.openTest===id?null:id;
  syncTestControls();updateBounds();
  if(state.openTest&&window.matchMedia?.('(min-width: 1180px)').matches)$(`#settings-${state.openTest} [data-editor-title]`)?.focus?.({preventScroll:true});
  if(close)($(`[data-test-card="${id}"]`).hidden?$('[data-catalogue-filter="all"]'):$(`[data-configure-test="${id}"]`))?.focus();
}
function editTestField(input) {
  const form=$('#new-run-form'),settings=input.closest('[data-test-settings]');
  if(settings){input.dataset.inherited='false';form.elements[`test_mode_${settings.dataset.testSettings}`].value='custom';}
  if(input.closest('[data-traffic-settings]')){form.elements.traffic_mode.value='custom';if(input.name==='traffic_max_in_flight')input.dataset.inherited='false';}
  syncTestControls();
}
function savedProfileInput(input,form) {
  const profile=input.closest('[data-profile-settings]');if(!profile)return false;
  const key=profile.dataset.profileSettings;
  return Boolean(state.config[key==='tools'?'tool_checks':key])||JSON.parse(form.elements[`profile_edits_${key}`]?.value || '[]').length>0;
}
function validateProfileDrafts(form,id=null) {
  if(!form)return true;
  for(const input of form.querySelectorAll('input, select, textarea')){
    const profile=input.closest('[data-profile-settings]');if(!profile||id&&profile.dataset.profileSettings!==id||!savedProfileInput(input,form))continue;
    if(['profile_consistency_logprobs','profile_tools_fixtures','profile_tools_conversations'].includes(input.name)){
      let message='';try{(input.name==='profile_consistency_logprobs'?parseLogprobSettings:parseToolFixtures)(input.value,state.bootstrap.limits);}catch(error){message=error.message;}
      input.setCustomValidity?.(message);
    }
    const fieldset=input.closest('fieldset'),disabled=input.disabled,fieldsetDisabled=fieldset?.disabled;
    if(fieldset)fieldset.disabled=false;input.disabled=false;
    const valid=input.checkValidity();input.disabled=disabled;if(fieldset)fieldset.disabled=fieldsetDisabled;
    if(valid)continue;
    revealSetupStep(2);revealCatalogueCheck(profile.dataset.profileSettings);
    for(let parent=input.parentElement;parent;parent=parent.parentElement)if(parent.tagName==='DETAILS')parent.open=true;
    const error=$('#form-error');if(error){error.textContent='Fix this saved setting or restore its profile defaults.';error.hidden=false;}
    input.focus?.();input.reportValidity();return false;
  }
  return true;
}
function refreshDefaultProfileFields(config) {
  const form=$('#new-run-form');if(!form)return;
  for(const key of ['arrivals','prefix','sessions','consistency']){
    const edits=new Set(JSON.parse(form.elements[`profile_edits_${key}`]?.value || '[]'));
    for(const [name,value] of Object.entries(profileDefaults(key,config))){
      const input=form.elements[`profile_${key}_${name}`];if(!input||edits.has(name)||state.config[key]?.[name]!=null)continue;
      if(key==='consistency'&&name==='logprobs'){input.value=JSON.stringify(value ?? null);continue;}
      input.value=Array.isArray(value)?value.join(['turns','saved_answers'].includes(name)?'\n':['modes','shapes'].includes(name)?',':', '):value ?? '';
    }
  }
}
function validateRunForm(form,step=null) {
  if((step===null||step===2)&&!validateProfileDrafts(form))return false;
  // Closed editors are disabled for native validation and read explicitly by readForm.
  // Inspect selected drafts here, revealing the field before asking the browser to focus it.
  for(const input of form.querySelectorAll('input, select, textarea')){
    if(step&&Number(input.closest('[data-setup-panel]')?.dataset.setupPanel)!==step)continue;
    if(packSettingInactive(input.name)||input.disabled&&input.closest('[data-run-limit]'))continue;
    if(input.closest('[data-session-settings]')&&input.name!=='sessions_profile'){
      if(form.elements.sessions_profile.value!=='performance')continue;
      const match=input.name.match(/^session_(?:name|weight|cap|turn)_([0-9]+)_?/);
      if(match&&!form.elements[`session_enabled_${match[1]}`]?.checked)continue;
    }
    if(input.name?.startsWith('load_'))continue;
    const card=input.closest('[data-test-card]');
    if(card){
      const id=card.dataset.testCard;
      if(!$('input[name="check"]',card).checked)continue;
      if(input.closest('[data-test-settings]')&&!input.closest('[data-profile-settings]')&&form.elements[`test_mode_${id}`]?.value!=='custom')continue;
      if(input.closest('[data-traffic-settings]')&&form.elements.traffic_mode.value!=='custom')continue;
    }
    if(['url','model','name'].includes(input.name))input.setCustomValidity?.(String(input.value || '').trim()?'':`Enter ${input.name==='url'?'an endpoint URL':input.name==='model'?'a model name, or use Test connection to detect it':'a target name'}.`);
    const fieldset=input.closest('fieldset'),disabled=input.disabled,fieldsetDisabled=fieldset?.disabled;
    if(fieldset)fieldset.disabled=false;input.disabled=false;
    const valid=input.checkValidity();
    input.disabled=disabled;if(fieldset)fieldset.disabled=fieldsetDisabled;
    if(valid)continue;
    if(input.name==='model'){const manual=$('#manual-model-field');if(manual)manual.hidden=false;}
    if(card)revealCatalogueCheck(card.dataset.testCard);
    const loadType=input.closest('[data-load-type]')?.dataset.loadType;if(loadType)setLoadPanel(loadType,true);
    for(let parent=input.parentElement;parent;parent=parent.parentElement)if(parent.tagName==='DETAILS')parent.open=true;
    const panel=input.closest('[data-setup-panel]');if(panel)state.setupStep=Number(panel.dataset.setupPanel);
    state.configView='form';syncConfigView();syncSetupStep();input.focus?.();input.reportValidity();return false;
  }
  return true;
}
function refreshInheritedFields(id,onlyInherited=false) {
  const check=state.bootstrap.checks.find(item=>item.id===id),form=$('#new-run-form');
  const config=clone(state.config);for(const [name] of numericFields)if(form.elements[name])config[name]=form.elements[name].value===''?null:Number(form.elements[name].value);if(form.elements.samples)config.samples=Number(form.elements.samples.value);
  config.limits={...config.limits};for(const name of check.limit_fields){const input=form.elements[`limit_${name}`];if(input)config.limits[name]=input.value===''?null:Number(input.value)/(['max_error_rate','min_correctness'].includes(name)?100:1);}
  for(const name of check.option_fields){const input=form.elements[`test_${id}_${name}`];if(input&&(!onlyInherited||input.dataset.inherited==='true')){input.value=optionDefault(config,check,name) ?? '';input.dataset.inherited='true';}}
  for(const name of check.limit_fields){const input=form.elements[`test_${id}_limit_${name}`];if(input&&(!onlyInherited||input.dataset.inherited==='true')){input.value=config.limits[name]==null?'':config.limits[name]*(['max_error_rate','min_correctness'].includes(name)?100:1);input.dataset.inherited='true';}}
}
const inputRetentionNotice='Questions and expected answers are saved with the run. Retention all / failures / none controls whether response text and evaluator reasons are kept. Request metadata (timings, status, scores) is always saved. Retention does not cap disk usage; storage limits do.';
function configChanged() {
  state.jsonValidation=null;state.configRevision++;
  if(state.requestPreview)state.requestPreview.status='stale';
  renderRequestPreview();
}
function packSettingInactive(name) {
  return state.config?.request_source==='request_pack' &&
    (/^traffic_mix_/.test(name) || ['traffic_long_input_chars','traffic_long_output_words','test_correctness_samples','test_correctness_concurrency'].includes(name));
}
function syncPackControls() {
  const form=$('#new-run-form');if(!form || !state.config)return;
  for(const name of ['traffic_mix_short','traffic_mix_long_input','traffic_mix_long_output','traffic_long_input_chars','traffic_long_output_words','test_correctness_samples','test_correctness_concurrency']){
    const input=form.elements[name];if(input){input.disabled=packSettingInactive(name);input.title=input.disabled?'Not used for request packs; saved for built-in requests.':'';}
  }
}
function renderJudging(config) {
  if(config.request_source!=='request_pack'||!config.request_pack?.cases.some(item=>item.scorer==='judge'))return '';
  const settings={...state.bootstrap.judging.defaults,...config.judging};
  return `<div class="judging-editor"><div class="test-editor-heading"><h3>AI evaluator</h3><button type="button" class="text-button" data-action="add-judge" ${settings.judges.length>=state.bootstrap.limits['judging.judges'].maxItems?'disabled':''}>Add evaluator</button></div><p class="hint">An AI model compares each answer with your expected answer and accepts correct paraphrases.</p><input type="hidden" name="judge_count" value="${settings.judges.length}">${settings.judges.map((judge,index)=>`<fieldset class="judge-row"><legend>Evaluator ${index+1}</legend><div class="fields">${field(`judge_${index}_model`,'Model',judge.model)}${field(`judge_${index}_url`,'Endpoint URL',judge.url,{type:'url',full:true,placeholder:'http://localhost:9000/v1'})}</div><details class="evaluator-options"><summary>Credentials & evaluator name</summary><div class="fields">${field(`judge_${index}_name`,'Name',judge.name)}${field(`judge_${index}_api_key_env`,'API key environment variable (optional)',judge.api_key_env,{full:true,placeholder:'JUDGE_API_KEY'})}</div></details><button type="button" class="text-button" data-action="remove-judge" data-judge-index="${index}" aria-label="Remove evaluator ${index+1}">Remove</button></fieldset>`).join('')}<details class="judge-limits"><summary>Evaluation limits</summary><div class="fields">${field('judging_concurrency','Evaluator concurrency',settings.concurrency,{type:'number',step:1})}${field('judging_request_timeout_seconds','Evaluator timeout (seconds)',settings.request_timeout_seconds,{type:'number',step:'any'})}${field('judging_max_duration_seconds','Evaluation duration (seconds)',settings.max_duration_seconds,{type:'number',step:'any'})}${field('judging_max_output_tokens','Evaluator cap (tokens)',settings.max_output_tokens,{type:'number',step:1})}</div><p class="hint">More connection and authentication settings are available in JSON.</p></details><p class="hint judge-disclosure">Questions, expected answers and model answers are sent to your configured evaluators. With several evaluators, more than half must agree. A tie or missing vote leaves the result inconclusive.</p></div>`;
}
function changeJudge(action,index) {
  try {
    assertAppliedConfig();captureFormDraft();
    state.config.judging ||= clone(state.bootstrap.judging.defaults);
    const judges=state.config.judging.judges;
    if(action==='add'){
      if(judges.length>=state.bootstrap.limits['judging.judges'].maxItems)return;
      let suffix=1;while(judges.some(judge=>judge.name===`judge-${suffix}`))suffix++;
      judges.push({name:`judge-${suffix}`,url:'',model:'',api_key_env:null,headers_env:{}});
    } else if(Number.isInteger(index)&&index>=0&&index<judges.length)judges.splice(index,1);
    else return;
    state.judgeEditorOpen=true;configChanged();renderNew();
    const focus=action==='add'?`[name="judge_${judges.length-1}_url"]`:'[data-action="add-judge"]';
    $(focus)?.focus();
  } catch(error){configError(error);}
}
function renderJudgingPreview(judging) {
  if(!judging?.active)return '';
  if(!judging.ready)return (judging.readiness_errors?.length?judging.readiness_errors:['Configure an AI evaluator before running.']).map(error=>notice(error,'warning')).join('');
  return `<p class="hint">Evaluation: up to ${number(judging.planned_call_upper_bound)} judge calls (upper bound across all targets), after traffic finishes · ${number(judging.judge_count)} judges.</p>`;
}
const answerChecks={judge:'AI evaluator',exact:'Exact match',contains:'Contains text',json:'JSON check',none:'No answer check'};
function datasetCases(){return state.config?.request_pack?.cases || [];}
function selectedDatasetCases(){return datasetCases().filter(item=>state.datasetSelection.includes(item.fixture_id));}
function judgesReady(config) {
  const judges=config.judging?.judges || [];
  return judges.length>0&&judges.every(judge=>judge.name?.trim()&&judge.url?.trim()&&judge.model?.trim());
}
function renderAnswerChecks(config) {
  if(config.request_source!=='request_pack')return '';
  const cases=config.request_pack?.cases || [],selected=selectedDatasetCases(),judged=cases.filter(item=>item.scorer==='judge');
  return `<section class="answer-checks">${selected.length?`<div class="dataset-bulk">${selectField('dataset_bulk_scorer',`Answer check for ${number(selected.length)} selected ${selected.length===1?'question':'questions'}`,'<option value="judge">AI evaluator · accepts correct paraphrases</option><option value="exact">Exact match</option><option value="contains">Contains text</option><option value="none">No answer check · removes expected answers</option>',{controlId:'dataset-bulk-scorer'})}<button type="button" class="button small" data-action="apply-answer-check">Apply to selected</button></div>`:''}${judged.length?`<div class="evaluator-status" id="evaluator-status"><p>${judgesReady(config)?'AI evaluator ready.':`${number(judged.length)} ${judged.length===1?'question needs':'questions need'} an AI evaluator.`}</p><button type="button" class="text-button" data-action="configure-evaluator">${judgesReady(config)?'Edit evaluator':'Configure evaluator'}</button></div><details class="judge-configuration" ${state.judgeEditorOpen?'open':''}><summary>AI evaluator settings</summary>${renderJudging(config)}</details>`:''}</section>`;
}
function renderRequestSource(config) {
  const pack=config.request_pack,custom=config.request_source==='request_pack',cases=pack?.cases || [];
  return `<section class="setup-section" id="setup-dataset"><div class="setup-body"><div class="request-source"><fieldset class="dataset-source"><legend>Questions ${infoHelp('request_source','Questions','Use built-in known-answer examples, or add or import your own questions and expected answers. Each saved question runs once in Answer correctness. Sample count and requests-at-a-time settings are not used for that check with saved questions. Load test uses the same source when selected; other checks use generated requests. Switching sources keeps your saved questions. Try one question to inspect the answer before testing them all.')}</legend><label><input type="radio" name="request_source" value="builtin" ${custom?'':'checked'}><span>Built-in examples</span></label><label><input type="radio" name="request_source" value="request_pack" ${custom?'checked':''}><span>My questions</span></label></fieldset>${custom?`<input type="file" id="request-pack-file" accept=".csv,.json,text/csv,application/json" hidden><div class="pack-actions actions"><button type="button" class="button small" data-action="add-example">+ Add question</button><button type="button" class="button small" data-action="import-pack">Import CSV / JSON</button><button type="button" class="text-button" data-action="sample-dataset">Use sample questions</button></div>${cases.length?`<div class="dataset-table question-table"><table><thead><tr><th><input type="checkbox" id="dataset-select-all" aria-label="Select all examples" ${cases.every(item=>state.datasetSelection.includes(item.fixture_id))?'checked':''}></th><th>Question / expected answer</th><th>Answer check</th><th>Actions</th></tr></thead><tbody>${cases.map((item,index)=>`<tr><td><input type="checkbox" data-dataset-select="${escape(item.fixture_id)}" aria-label="Select ${escape(item.fixture_id)}" ${state.datasetSelection.includes(item.fixture_id)?'checked':''}></td><td><strong>${escape(item.fixture_id)}</strong><span class="dataset-excerpt">${escape(item.messages.map(message=>message.content).join(' / '))}</span><small class="dataset-excerpt">Expected: ${Object.hasOwn(item,'expected')?escape(typeof item.expected==='string'?item.expected:JSON.stringify(item.expected)):'Not set'}${item.messages.length>1?` · ${number(item.messages.length)} messages`:''}</small></td><td>${answerChecks[item.scorer]}${item.scorer==='judge'?`<small data-evaluator-needed ${judgesReady(config)?'hidden':''}>Needs evaluator</small>`:''}</td><td><div class="dataset-row-actions"><button type="button" class="text-button" data-action="try-example" data-case-index="${index}" aria-label="Try ${escape(item.fixture_id)}">Try one</button><button type="button" class="text-button" data-action="edit-example" data-case-index="${index}" aria-label="Edit ${escape(item.fixture_id)}">Edit</button><button type="button" class="text-button" data-action="remove-example" data-case-index="${index}" aria-label="Remove ${escape(item.fixture_id)}">Remove</button></div></td></tr>`).join('')}</tbody></table></div><p class="hint dataset-count">${number(cases.length)} questions</p>`:'<div class="dataset-empty"><h3>Add your first question</h3><p class="hint">Enter a question and its expected answer, or use the sample questions to get started.</p></div>'}`:` ${state.bootstrap.builtin_question_example?`<details class="builtin-example"><summary>View an example</summary><p>${escape(state.bootstrap.builtin_question_example.question)}</p><p><strong>Expected answer:</strong> <code>${escape(state.bootstrap.builtin_question_example.expected)}</code></p><p class="hint">Exact match after whitespace normalization; extra explanation does not match.</p></details>`:''}`}</div>${renderAnswerChecks(config)}${custom&&cases.length?`<details class="setup-disclosure dataset-advanced"><summary>Advanced dataset settings</summary><p class="hint">${inputRetentionNotice}</p><div id="request-pack-preview"></div></details>`:''}</div></section>`;
}
function datasetDialog(title,body,footer='') {
  const dialog=$('#dataset-dialog');
  dialog.innerHTML=`<div class="dialog-head"><h2 id="dataset-dialog-title">${escape(title)}</h2><button type="button" class="button ghost" data-action="close-dataset" aria-label="Close dataset editor">✕</button></div>${body}${footer}`;
  if(!dialog.open)dialog.showModal();
  return dialog;
}
function closeDatasetDialog(){state.datasetImportSequence++;state.datasetImport=null;state.datasetEditor=null;state.datasetTry=null;$('#dataset-dialog').close();}
function datasetError(message){const element=$('#dataset-error');if(element){element.textContent=message || '';element.hidden=!message;}else configError(new Error(message));}
function datasetEditingAllowed(){assertAppliedConfig();if(state.configBusy||state.submitting)throw new Error('Wait for the current operation to finish.');}
async function applyDataset(pack,{guard=()=>true,clearSelection=false}={}) {
  datasetEditingAllowed();const version=state.routeVersion,revision=state.configRevision;
  const config=readForm(true).config;config.request_source='request_pack';config.request_pack=pack;
  state.configBusy=true;
  try{
    const result=await api('/api/config/draft',{method:'POST',body:JSON.stringify(config)});
    if(!guard())return false;
    if(version!==state.routeVersion||revision!==state.configRevision)throw new Error('Configuration changed. Review the dataset and try again.');
    assertAppliedConfig();captureFormDraft();state.config=result.config;
    state.datasetSelection=clearSelection?[]:state.datasetSelection.filter(id=>datasetCases().some(item=>item.fixture_id===id));
    state.jsonDraft=null;state.jsonBase=null;configChanged();renderNew();return true;
  }finally{state.configBusy=false;}
}
function exampleEditorValues(){return new FormData($('#dataset-example-form'));}
function readExampleMessages(){const values=exampleEditorValues();return Array.from({length:Number(values.get('message_count'))},(_,index)=>({role:values.get(`message_${index}_role`),content:String(values.get(`message_${index}_content`) || '')}));}
function renderExampleMessages(messages) {
  return `<input type="hidden" name="message_count" value="${messages.length}">${messages.map((message,index)=>`<div class="example-message">${textField(`message_${index}_content`,messages.length===1&&message.role==='user'?'Question':'Message '+(index+1),message.content,{rows:3})}<details class="message-options" ${messages.length>1?'open':''}><summary>Message options</summary><div class="message-controls">${selectField(`message_${index}_role`,'Role',['system','user','assistant'].map(role=>`<option value="${role}" ${message.role===role?'selected':''}>${human(role)}</option>`).join(''))}<button type="button" class="text-button" data-action="remove-message" data-message-index="${index}" ${messages.length===1?'disabled':''}>Remove message</button></div></details></div>`).join('')}`;
}
function openExampleEditor(index=null) {
  try{
    datasetEditingAllowed();captureFormDraft();
    const previous=index===null?null:datasetCases()[index];
    let suffix=1;while(datasetCases().some(item=>item.fixture_id===`example-${suffix}`))suffix++;
    const item=clone(previous || {fixture_id:`example-${suffix}`,messages:[{role:'user',content:''}],scorer:'judge',expected:'',weight:1,options:{},max_tokens:null});
    state.datasetEditor={index,item,expectedFormat:item.scorer==='json'?'json':'text',scorer:item.scorer,revision:state.configRevision,route:state.routeVersion};
    datasetDialog(previous?'Edit question':'Add question',`<form id="dataset-example-form"><div class="dialog-body"><div id="example-messages">${renderExampleMessages(item.messages)}</div><button type="button" class="text-button" data-action="add-message">Add conversation message</button><div class="example-check">${selectField('example_scorer','Answer check',Object.entries(answerChecks).map(([key,label])=>`<option value="${key}" ${item.scorer===key?'selected':''}>${label}</option>`).join(''))}</div>${textField('example_expected','Expected answer',Object.hasOwn(item,'expected')?(item.scorer==='json'?JSON.stringify(item.expected,null,2):item.expected):'',{wrapperId:'example-expected-label',hintId:'example-expected-hint',hint:'The answer you want the model to give.'})}${textField('example_schema','JSON schema',JSON.stringify(item.schema || {},null,2),{wrapperId:'example-schema-label'})}<details class="example-advanced"><summary>Advanced question settings</summary>${textField('example_rubric','Evaluator instructions (optional)',item.rubric,{rows:2,wrapperId:'example-rubric-label'})}<div class="fields">${field('example_id','Question ID',item.fixture_id,{full:true})}${field('example_weight','Traffic weight',item.weight,{type:'number',step:'any'})}${field('example_max_tokens','Output cap (optional)',item.max_tokens,{type:'number',step:1})}</div>${textField('example_options','Extra request options · JSON',JSON.stringify(item.options || {},null,2))}</details><div id="dataset-error" class="error-message" role="alert" hidden></div></div><div class="dialog-footer"><button type="button" class="button" data-action="close-dataset">Cancel</button><button type="submit" class="button primary">Save question</button></div></form>`);
    syncExampleScorer();$('#dataset-example-form').elements.message_0_content.focus();
  }catch(error){configError(error);}
}
function syncExampleScorer(){
  const form=$('#dataset-example-form'),editor=state.datasetEditor;
  let scorer=form.elements.example_scorer.value;
  if(editor&&scorer!=='none'){
    const format=scorer==='json'?'json':'text';
    try{
      form.elements.example_expected.value=convertExpectedAnswer(form.elements.example_expected.value,editor.expectedFormat,format);
      editor.expectedFormat=format;datasetError('');
    }catch(error){scorer=editor.scorer;form.elements.example_scorer.value=scorer;datasetError(error.message);}
  }
  if(editor)editor.scorer=scorer;
  $('#example-expected-label').hidden=scorer==='none';$('#example-rubric-label').hidden=scorer!=='judge';$('#example-schema-label').hidden=scorer!=='json';
  $('#example-expected-hint').textContent=({json:'Enter a JSON value. Leave blank to check only the schema. Use null for a null answer. One complete Markdown code fence is accepted; surrounding prose is rejected.',judge:'Accepts answers with the same meaning.',exact:'Wording, capitalization and punctuation must match. Extra spaces and line breaks are ignored.',contains:'The answer must include this text. Capitalization matters.'})[scorer] || '';
}
async function saveExample(event) {
  event.preventDefault();const editor=state.datasetEditor;if(!editor||state.configBusy)return;
  try{
    if(editor.revision!==state.configRevision||editor.route!==state.routeVersion)throw new Error('Configuration changed. Reopen this example before saving.');
    const item=datasetCaseFromValues(editor.item,exampleEditorValues()),cases=clone(datasetCases());
    if(editor.index===null)cases.push(item);else cases[editor.index]=item;
    if(await applyDataset({version:1,cases},{guard:()=>state.datasetEditor===editor})){closeDatasetDialog();toast('Example saved.');}
  }catch(error){datasetError(error.message);}
}
async function removeExample(index){try{const cases=clone(datasetCases());cases.splice(index,1);await applyDataset(cases.length?{version:1,cases}:null);}catch(error){configError(error);}}
async function applyAnswerCheck(){try{const scorer=$('#dataset-bulk-scorer').value;const selected=new Set(state.datasetSelection);const cases=datasetCases().map(item=>selected.has(item.fixture_id)?changeDatasetScorer(item,scorer):clone(item));await applyDataset({version:1,cases});}catch(error){configError(error);}}
function importMappingValues(){const form=$('#dataset-import-form');return {input_field:form?.elements.input_field?.value || null,expected_field:form?.elements.expected_field?.value || null,scorer:form?.elements.import_scorer?.value || 'judge'};}
function renderDatasetImportTable(preview) {
  const cases=preview?.request_pack?.cases.slice(0,5);
  if(cases)return `<div class="dataset-table import-table"><table><thead><tr><th>Input</th><th>Expected answer</th><th>Answer check</th></tr></thead><tbody>${cases.map(item=>`<tr><td>${item.messages.map(message=>`<p>${item.messages.length>1?`<small>${escape(message.role)}</small>`:''}${escape(message.content)}</p>`).join('')}</td><td>${Object.hasOwn(item,'expected')?escape(typeof item.expected==='string'?item.expected:JSON.stringify(item.expected)):'Not set'}</td><td>${answerChecks[item.scorer]}</td></tr>`).join('')}</tbody></table></div>`;
  const columns=preview?.columns || [];
  return `<div class="dataset-table import-table"><table><thead><tr>${columns.map(column=>`<th>${escape(column)}</th>`).join('')}</tr></thead><tbody>${(preview?.preview_rows || []).map(row=>`<tr>${row===null||typeof row!=='object'||Array.isArray(row)?`<td colspan="${Math.max(1,columns.length)}">Invalid row: ${escape(JSON.stringify(row))}</td>`:columns.map(column=>`<td>${escape(typeof row[column]==='string'?row[column]:JSON.stringify(row[column]))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}
function renderDatasetImport() {
  const draft=state.datasetImport;if(!draft)return;const preview=draft.preview,columns=preview?.columns || [],mapping=draft.mapping || {};
  const options=value=>'<option value="">Choose a column</option>'+columns.map(column=>`<option value="${escape(column)}" ${value===column?'selected':''}>${escape(column)}</option>`).join('');
  const errors=(preview?.errors || []).map(error=>`<li>${error.row!=null?`Row ${number(error.row)} · `:''}${escape(error.field)}: ${escape(error.message)}</li>`).join('');
  datasetDialog('Import dataset',`<form id="dataset-import-form"><div class="dialog-body"><p class="hint">${escape(draft.name)} · up to 1 MiB / 1,000 questions. Review the preview, then choose Use dataset to import.</p>${draft.loading?'<p role="status">Reading dataset…</p>':''}${columns.length?`<div class="fields dataset-mapping">${selectField('input_field','Question column',options(mapping.input_field))}${selectField('expected_field','Expected answer column',options(mapping.expected_field))}${selectField('import_scorer','Answer check',`<option value="judge" ${mapping.scorer!=='exact'?'selected':''}>AI evaluator</option><option value="exact" ${mapping.scorer==='exact'?'selected':''}>Exact match</option>`)}</div><button type="button" class="button small" data-action="preview-dataset-mapping" ${draft.loading?'disabled':''}>Preview mapping</button><p class="hint">Choose the question and answer columns, then check the preview.</p>`:''}${errors?`<div class="notice error"><ul>${errors}</ul></div>`:''}${draft.mappingDirty?'<p class="hint">Columns changed. Check the preview again before importing.</p>':''}${preview?.request_pack&&!draft.mappingDirty?`<p class="pack-summary">${number(preview.request_pack.cases.length)} examples ready to import.</p>`:''}${preview?.preview_rows?.length?`<details class="dataset-import-preview" open><summary>Preview · first ${number(preview.preview_rows.length)} rows</summary>${renderDatasetImportTable(preview)}</details>`:''}<div id="dataset-error" class="error-message" role="alert" ${draft.error?'':'hidden'}>${escape(draft.error || '')}</div></div><div class="dialog-footer"><button type="button" class="button" data-action="close-dataset">Cancel</button><button type="submit" class="button primary" ${!preview?.request_pack||draft.loading||draft.mappingDirty?'disabled':''}>Use dataset</button></div></form>`);
}
async function previewDatasetMapping({initial=false}={}) {
  const draft=state.datasetImport;if(!draft)return;
  const sequence=++draft.sequence;if(!initial)draft.mapping=importMappingValues();
  draft.loading=true;draft.error=null;renderDatasetImport();
  try{
    const result=await api('/api/datasets/preview',{method:'POST',body:JSON.stringify({text:draft.text,format:draft.format,...(initial?{}:draft.mapping)})});
    if(state.datasetImport!==draft||sequence!==draft.sequence||draft.route!==state.routeVersion||draft.revision!==state.configRevision)return;
    draft.preview=result;draft.mappingDirty=false;
    if(initial)draft.mapping={...result.suggested_mapping,scorer:'judge'};
  }catch(error){if(state.datasetImport===draft&&sequence===draft.sequence)draft.error=error.message;}
  finally{if(state.datasetImport===draft&&sequence===draft.sequence){draft.loading=false;renderDatasetImport();}}
}
async function importRequestPack(file) {
  if(!file||state.configBusy)return;
  const version=state.routeVersion,revision=state.configRevision,sequence=++state.datasetImportSequence;
  try{
    datasetEditingAllowed();if(file.size>state.bootstrap.limits['request_pack.bytes'].maximum)throw new Error(`Dataset exceeds ${diskSize(state.bootstrap.limits['request_pack.bytes'].maximum)}.`);
    const text=await file.text();if(version!==state.routeVersion||revision!==state.configRevision||sequence!==state.datasetImportSequence)return;
    state.datasetImport={name:file.name || 'dataset.json',text,format:/\.csv$/i.test(file.name || '')?'csv':'json',route:version,revision,sequence:0,preview:null,mapping:null,mappingDirty:false};
    await previewDatasetMapping({initial:true});
  }catch(error){if(version===state.routeVersion&&revision===state.configRevision&&sequence===state.datasetImportSequence)configError(error);}
}
async function useImportedDataset(event) {
  event.preventDefault();const draft=state.datasetImport;
  if(!draft?.preview?.request_pack||draft.mappingDirty||draft.loading||state.configBusy)return;
  try{
    if(draft.route!==state.routeVersion||draft.revision!==state.configRevision)throw new Error('Configuration changed. Import the dataset again before applying it.');
    if(await applyDataset(draft.preview.request_pack,{guard:()=>state.datasetImport===draft,clearSelection:true})){closeDatasetDialog();toast('Dataset imported. Choose answer checks and try one example.');}
  }catch(error){datasetError(error.message);}
}
function sampleDataset(){
  const text=JSON.stringify({version:1,cases:[{fixture_id:'refund-window',messages:[{role:'user',content:'A store allows refunds within 30 days with a receipt. What is its refund policy?'}],scorer:'judge',expected:'Refunds are allowed within 30 days with a receipt.'},{fixture_id:'simple-count',messages:[{role:'user',content:'How many days are in one week? Reply with only the digit, with no words or punctuation.'}],scorer:'exact',expected:'7'}]});
  return importRequestPack({name:'sample-dataset.json',size:text.length,text:async()=>text});
}
function openTryExample(index,targetName=null) {
  try{
    datasetEditingAllowed();captureFormDraft();const item=datasetCases()[index];if(!item)return;
    state.datasetTry={fixture_id:item.fixture_id,revision:state.configRevision,route:state.routeVersion};
    datasetDialog('Try one example',`<form id="dataset-try-form"><div class="dialog-body"><p><strong>${escape(item.fixture_id)}</strong></p><p class="hint">Sends this question once to the selected server, then checks the answer${item.scorer==='judge'?' with your configured AI evaluators':''}. No traffic stages or restart commands run.</p><div class="example-check">${selectField('try_target','Target',state.config.targets.map(target=>`<option value="${escape(target.name)}">${escape(target.name)} · ${escape(target.model || 'model needed')}</option>`).join(''))}</div><label class="trial-retention"><input name="save_try_output" type="checkbox" checked><span>Save this trial’s answer and judge reasons</span></label><p class="hint">Save the answer and evaluator reasons in this trial report so you can inspect them. If unchecked, only measurements and results are saved. This does not change what your main run saves.</p><div id="dataset-error" class="error-message" role="alert" hidden></div></div><div class="dialog-footer"><button type="button" class="button" data-action="close-dataset">Cancel</button><button type="submit" class="button primary" ${state.active?'disabled':''}>Send one request</button></div></form>`);
    if(targetName&&state.config.targets.some(target=>target.name===targetName))$('#dataset-try-form').elements.try_target.value=targetName;
  }catch(error){configError(error);}
}
async function tryDatasetExample(event) {
  event.preventDefault();const draft=state.datasetTry;if(!draft||state.active||state.submitting)return;
  try{
    datasetEditingAllowed();if(draft.route!==state.routeVersion||draft.revision!==state.configRevision)throw new Error('Configuration changed. Reopen Try one example.');
    const config=readForm(true).config,target_name=$('#dataset-try-form').elements.try_target.value;
    config.retention=$('#dataset-try-form').elements.save_try_output.checked?'all':'none';
    state.submitting=true;
    const result=await api('/api/runs/try-example',{method:'POST',body:JSON.stringify({config,fixture_id:draft.fixture_id,target_name,api_keys:sessionAPIKeys(config)})});
    setActive(result.id);closeDatasetDialog();if(draft.route===state.routeVersion)location.hash=linkRun(result.id);else toast('Example run started. Open the active run to inspect it.');
  }catch(error){datasetError(error.message);}finally{state.submitting=false;}
}
function currentRequestPlan() {
  const saved=state.requestPreview;
  return !hasJsonChanges()&&saved?.status==='current'&&saved.revision===state.configRevision&&saved.route===state.routeVersion?saved.plan:null;
}
function canStartRun() {
  return Boolean(currentRequestPlan())&&!currentSettingErrors().length&&!currentResourceErrors().length&&!state.submitting&&!state.configBusy&&!state.active&&state.modelDiscovery?.status!=='pending'&&tokenizerSetupReady();
}
function renderReviewPassCriteria(config) {
  if(!config?.limits||!state.bootstrap?.checks)return '';
  const labels={first_output_ms:'First output within',latency_ms:'Completion limit',stream_gap_ms:'Stream pause',min_output_tokens_per_second:'Minimum generation rate',fairness_max_ratio:'Maximum slowdown',max_error_rate:'Errors allowed',min_correctness:'Correct answers',min_samples:'Minimum usable answers'};
  const rows=config.checks.map(id=>{
    if(id==='load'){
      const s=loadSettings(config),criteria=s.pass_criteria;
      const values=Object.entries(loadCriteriaLabels).map(([key,label])=>`${label}: ${criteria[key]==null?'Measure only':number(criteria[key],6)}`);
      if(s.workloads.interference.enabled)values.push(`Maximum allowed slowdown of short answers ${s.workloads.interference.max_slowdown}×`);
      if(s.workloads.stress.enabled)values.push(`Minimum generated output ${s.workloads.stress.min_output_percent}% of long-output cap`);
      return `<article><strong>Load test</strong><p class="hint">${escape(values.join(' · '))}</p></article>`;
    }
    const check=state.bootstrap.checks.find(item=>item.id===id);if(!check?.limit_fields.length)return '';
    const limits=check.limit_fields.map(name=>{
      const value=limitValue(config,check,name);if(value==null)return '';
      return `${labels[name] || human(name)} ${checkSettingValue(name,value)}`;
    }).filter(Boolean);
    const timingFields=id==='first_output'?['first_output_ms']:id==='generation'?['latency_ms','stream_gap_ms','min_output_tokens_per_second']:[];
    if(timingFields.length&&timingFields.every(name=>limitValue(config,check,name)==null))limits.unshift('Measurement only — no timing target; result is inconclusive');
    return limits.length?`<article><strong>${escape(catalogTitle(id))}</strong><p class="hint">${escape(limits.join(' · '))}</p></article>`:'';
  }).filter(Boolean);
  return rows.length?`<section class="review-criteria review-section"><div class="review-section-heading"><h3>Pass criteria</h3><button type="button" class="text-button" data-review-edit="true" data-setup-step="2">Edit criteria</button></div><div class="plan-rows">${rows.join('')}</div></section>`:'';
}
function renderCorrectnessHistory(config,plan) {
  return (plan.correctness_history || []).filter(row=>row.status==='warning').map(row=>{
    const index=(config?.request_pack?.cases || []).findIndex(item=>item.fixture_id===row.case_id);
    const links=(row.failed_request_ids || []).map((id,i)=>`<a href="${linkRun(row.run_id)}/requests/${encodeURIComponent(id)}">Failed answer ${i+1}</a>`).join(' · ');
    return `<section class="notice warning"><p>${escape(row.message)}</p><p>${links}</p>${index>=0?`<button type="button" class="button small" data-action="try-example" data-case-index="${index}" data-try-target="${escape(row.target)}">Try one</button>`:''}</section>`;
  }).join('');
}
function renderArrivalHistory(plan) {
  const rows=plan.arrival_history || [];
  if(!rows.length)return '';
  return `<details class="request-planning arrival-history"><summary>Previous arrival delivery</summary>${rows.map(row=>`<p>${escape(row.message)}${row.run_id?` <a href="${linkRun(row.run_id)}">Open source run</a>`:''}</p>`).join('')}</details>`;
}
function renderRequestPlan(config=null) {
  const element=$('#request-plan');if(!element)return;
  const saved=state.requestPreview,plan=currentRequestPlan();
  if(!config&&$('#run-blueprint')){try{config=readForm(true).config;}catch{}}
  config ||= (plan?saved.config:null) || state.config;
  const history=$('#setup-history');if(history){history.innerHTML=plan?renderArrivalHistory(plan)+renderCorrectnessHistory(config,plan):'';history.hidden=!history.innerHTML;}
  syncRunBlueprint(config,plan);
  const start=$('#start-run');if(start)start.disabled=!canStartRun();
  const checkPlan=$('#checks-plan-summary');if(checkPlan){const text=plan?`${setupPlanCount(plan)}${plan.request_limit!=null&&plan.required_request_limit>plan.request_limit?' · budget is too small for full coverage':''}`:saved?.status==='error'?`Request estimate unavailable: ${saved.error}`:'Updating request estimate…';if(checkPlan.textContent!==text)checkPlan.textContent=text;checkPlan.classList.toggle('budget-warning',Boolean(plan&&plan.request_limit!=null&&plan.required_request_limit>plan.request_limit));}
  const issues=currentSettingErrors().length,planStatus=issues?`${number(issues)} ${issues===1?'setting':'settings'} to fix`:plan?setupPlanCount(plan):saved?.status==='error'&&saved.revision===state.configRevision&&saved.route===state.routeVersion?'Plan unavailable':'Planning…';
  const dock=$('#dock-summary');if(dock&&config){const selected=config.checks?.length || 0;const baselineName=(state.baselines || []).find(item=>item.id===$('#new-run-form')?.elements?.baseline_id?.value)?.name;dock.innerHTML=state.setupStep===3?`<strong>${number(selected)}</strong> ${selected===1?'check':'checks'} <span>·</span> ${escape(sharedLimitsSummary(config))}${baselineName?` <span>·</span> compared with <strong>${escape(baselineName)}</strong>`:''}`:`<strong>${number(selected)}</strong> ${selected===1?'check':'checks'} <span>·</span> ${escape(planStatus)}`;}
  const heading='<div class="review-section-heading"><h3>Traffic</h3><button type="button" class="text-button" data-action="edit-run-limits">Edit limits</button></div>';
  const limits=config?renderPlanBounds(config,'review-limits'):'';
  if(!plan){
    const status=hasJsonChanges()?'Apply or discard JSON changes to confirm the request plan.':saved?.status==='error'?`Request plan unavailable: ${saved.error}`:saved?.status==='pending'?'Updating request plan…':'Request plan stale · updating current settings…';
    element.innerHTML=`<div class="plan-hero">${heading}<p class="hint plan-status">${escape(status)}</p>${limits}${renderLoadPlan(config)}${renderMigrationNotes()}${renderSettingIssues()}${saved?.status==='error'&&!hasJsonChanges()?'<button type="button" class="button small" data-action="preview-pack">Retry request plan</button>':''}</div>`;return;
  }
  const count=setupPlanCount(plan);
  const evaluator=plan.evaluator || {},needed=plan.required_request_limit;
  const raise=plan.request_limit!=null&&Number.isInteger(needed)&&needed>plan.request_limit;
  const evaluatorScope='AI judge calls run separately from the target request budget and send cases and generated answers to each judge.';
  const explained=new Set([...(plan.time_warnings||[]),...(plan.request_warnings||[]),...(plan.resource_warnings||[]),...(plan.concurrency_warnings||[]),...(plan.capacity_warnings||[])].map(item=>item.msg));
  for(const row of [...(plan.arrival_history||[]),...(plan.correctness_history||[])])explained.add(row.message);
  const warnings=[...new Set([...(evaluator.readiness_errors || []),...(plan.warnings || [])])].filter(warning=>(!evaluator.active||warning!==evaluatorScope)&&!explained.has(warning));
  const filled=plan.request_limit==null?0:Math.min(100,(plan.planned_total ?? plan.known_total ?? 0)/plan.request_limit*100);
  element.innerHTML=`<div class="plan-hero${raise?' is-over':''}">${heading}<p class="plan-total">${escape(count)}</p>${plan.request_limit==null?'':`<div class="plan-ceiling${plan.count_kind==='exact'||plan.count_kind==='estimate'?'':' adaptive'}" aria-hidden="true"><span style="width:${raise?100:filled.toFixed(1)}%"></span></div>`}${limits}${renderLoadPlan(config,plan)}${renderSettingIssues([...localSettingWarnings(),...(plan?.load_warnings||[])],'warning')}${renderMigrationNotes()}${renderPlanTime(plan)}${renderSettingIssues()}${warnings.map(warning=>notice(warning,'warning')).join('')}</div><div class="plan-flight">${renderFlightPlan(plan)}${evaluator.active?`<div class="review-evaluator"><p>Up to ${number(evaluator.call_upper_bound)} evaluator calls after target traffic · ${number(evaluator.judge_count)} ${evaluator.judge_count===1?'evaluator':'evaluators'}.</p><p class="hint">Questions, expected answers and model answers go to every configured evaluator. These calls have separate limits.</p>${config?.judging?.judges?.length?`<ul class="review-evaluator-targets">${config.judging.judges.map(judge=>`<li><strong>${escape(judge.model || 'Model needed')}</strong> · ${escape(judge.url || 'Endpoint URL needed')}</li>`).join('')}</ul>`:''}</div>`:''}<details class="request-planning"><summary>Request breakdown</summary><p class="hint">${plan.planned_per_target==null?`${number(plan.known_per_target)} known requests per endpoint, plus adaptive work`:`${plan.count_kind==='estimate'?'About ':''}${number(plan.planned_per_target)} requests per endpoint`}. Time and request limits can leave checks unfinished.</p>${Object.keys(plan.target_quotas || {}).length?`<h4>Endpoint request budgets</h4><ul class="review-quotas">${Object.entries(plan.target_quotas).map(([name,quota])=>`<li>${escape(name)}: up to ${number(quota)} requests</li>`).join('')}</ul>`:''}<div class="plan-rows">${(plan.rows || []).filter(row=>row.requests_per_target!==0).map(row=>`<article><strong>${escape(checkGuide[row.id]?catalogTitle(row.id):row.title)}</strong><span>${row.requests_per_target==null?(row.kind==='duration'?`Duration-based${row.estimated_requests_per_target!=null?` · ~${number(row.estimated_requests_per_target)} (estimate, ${escape(row.estimate_source||'from your last run')})`:' · as many as complete'}`:'Adaptive / bounded'):`${row.kind==='estimate'?'About ':''}${number(row.requests_per_target)} per endpoint`}</span><p class="hint">${escape(row.summary)} · ${escape(row.source)}</p></article>`).join('')}</div>${plan.bucket_cases?.length?`<h4>Question and answer sizes</h4><ul class="plan-cases">${plan.bucket_cases.map(item=>`<li>${number(item.actual_input_chars)} characters sent${item.actual_input_chars!==item.input_chars?` (${number(item.input_chars)} configured, plus instructions)`:''} · answer cap ${number(item.output_tokens)} tokens</li>`).join('')}</ul>`:''}${plan.bucket_levels?.length?`<p class="hint">${plan.bucket_levels.map(level=>`${number(level.concurrency)} at once: ${number(level.requests_per_case)} requests per combination, ${number(level.requests_per_target)} per endpoint`).join(' · ')}</p>`:''}</details></div>${renderReviewPassCriteria(config)}`;
}
function scheduleRequestPlan() {
  if(state.planTimer!==null){clearTimeout(state.planTimer);state.planTimer=null;}
  if(!$('#request-plan')||!$('#new-run-form')||hasJsonChanges()||state.submitting)return;
  const revision=state.configRevision,route=state.routeVersion;
  state.planTimer=setTimeout(async()=>{
    state.planTimer=null;
    if(revision!==state.configRevision||route!==state.routeVersion||!$('#new-run-form')||hasJsonChanges()||state.submitting)return;
    try{await refreshRequestPreview(undefined,{draft:true});}catch{}
  },300);
}
function renderRequestPreview() {
  syncInlineErrors();
  renderRequestPlan();
  try{const config=readForm(true).config;syncLoadDisplay(config);}catch{}
  const element=$('#request-pack-preview');if(!element)return;
  if(state.config?.request_source!=='request_pack'){element.innerHTML='';return;}
  const expanded=element.querySelector?.('.request-planning')?.open || false;
  const saved=state.requestPreview;
  const status=!hasJsonChanges()&&saved && saved.revision===state.configRevision && saved.route===state.routeVersion?saved.status:'stale';
  const packRevision=saved?.packRevision ?? saved?.revision;
  const packStatus=status==='current'&&packRevision!==state.configRevision?'stale':status;
  const preview=saved?.data;
  const cases=preview?.cases?.length?preview.cases:(saved?.config?.request_pack?.cases || []).map(item=>({...item,input_chars:item.messages.reduce((total,message)=>total+message.content.length,0),requested:{max_tokens:item.max_tokens,options:item.options},effective:null}));
  const labels={pending:'Updating preview…',current:'Preview current',stale:'Preview stale · refresh to see the current plan',error:'Preview failed · current plan is unconfirmed'};
  element.innerHTML=`<details class="request-planning" ${expanded?'open':''}><summary>Dataset request details <span class="dim">· ${labels[packStatus]}</span></summary><div class="pack-preview-heading"><p class="hint" role="status">${labels[packStatus]}</p><button class="text-button" type="button" data-action="preview-pack" ${status==='pending'?'disabled':''}>Refresh preview</button></div>${preview?`<p class="hint">${preview.active?`Consumers: ${preview.consumers.map(escape).join(', ')} · ${number(preview.quiet_requests)} quiet requests`:'Pack inactive. Select correctness or capacity to exercise it.'}</p>${!preview.active?'<button class="text-button" type="button" data-action="select-pack-tests">Choose tests</button>':''}<p class="hint">Pack requests per target: ${number(preview.planned_requests_per_target)} · run request cap: ${number(preview.request_cap)} · target quotas: ${Object.entries(preview.per_target_request_quota || {}).map(([key,value])=>`${escape(key)} ${number(value)}`).join(', ')}</p><p class="hint">${number(preview.unique_effective_requests)} unique effective requests · ${number(preview.repeated_requests_per_target)} repeats per target.</p>${renderJudgingPreview(preview.judging)}${(preview.warnings || []).filter(w=>w!=='AI judging runs after target traffic. Messages, golden answers and generated answers are sent to every configured judge.').map(w=>notice(w,'warning')).join('')}<details class="pack-preview-details"><summary>Effective request settings and stage allocations${packStatus!=='current'?' · previous preview':''}</summary><div class="pack-preview-scroll">${cases.map(item=>{const source=(saved.packConfig || saved.config)?.request_pack?.cases.find(c=>c.fixture_id===item.fixture_id);return `<article class="pack-case"><h3>${escape(item.fixture_id)}</h3><p class="hint">${number(item.input_chars)} input characters · weight ${number(item.weight,4)} · ${escape(item.scorer)}</p><p class="hint">Output cap: requested ${item.requested?.max_tokens==null?'inherited':number(item.requested.max_tokens)} → effective ${item.effective?`${number(item.effective.max_tokens)} tokens · ${item.effective.stream?'streaming':'non-streaming'} · deadline ${elapsed(item.effective.request_timeout_seconds)}`:'inactive; select a consumer to calculate settings'}</p><details><summary>Assertion and request options</summary><pre>${escape(JSON.stringify({expected:source?.expected,rubric:source?.rubric,schema:source?.schema,requested_options:item.requested?.options,effective_options:item.effective?.options},null,2))}</pre></details></article>`;}).join('')}${(preview.stages || []).map(stage=>`<article class="pack-case"><h3>${number(stage.rate_rps,2)} requests/s · ${number(stage.planned)} planned</h3><p class="hint">${Object.entries(stage.counts || {}).map(([key,value])=>`${escape(key)}: ${number(value)}`).join(' · ')}</p></article>`).join('')}</div></details>`:''}</details>${status==='error'?notice(saved.error,'error'):''}`;
}
async function refreshRequestPreview(snapshot,{draft=false}={}) {
  assertAppliedConfig();
  if(state.planTimer!==null){clearTimeout(state.planTimer);state.planTimer=null;}
  const version=state.routeVersion,revision=state.configRevision,sequence=++state.previewSequence;
  const payload=readForm(true),config=clone(snapshot || payload.config);
  if(payload.mode==='cold-start')config.restart_target=payload.restart_target;
  state.requestPreview={...state.requestPreview,status:'pending',revision,route:version};renderRequestPreview();
  try{
    const localErrors=localSettingErrors(config);
    if(localErrors.length)throw Object.assign(new Error(localErrors[0].msg),{errors:localErrors});
    const result=await api(draft?'/api/config/draft':'/api/config/preview',{method:'POST',body:JSON.stringify(config)});
    if(version!==state.routeVersion||revision!==state.configRevision||sequence!==state.previewSequence)throw new Error('Configuration changed during preview. Refresh the current settings before running.');
    assertAppliedConfig();
    acceptMigrationNotes(result.migration_notes);
    const previous=state.requestPreview;
    state.requestPreview={status:'current',revision,route:version,plan:result.request_plan,config:result.config,
      data:draft?previous?.data:result.request_pack_preview,
      packRevision:draft?(previous?.packRevision ?? -1):revision,
      packConfig:draft?previous?.packConfig:result.config};renderRequestPreview();
    return result;
  }catch(error){
    if(version===state.routeVersion&&revision===state.configRevision&&sequence===state.previewSequence){state.requestPreview={...state.requestPreview,status:'error',error:error.message,errors:error.errors||[]};renderRequestPreview();}
    throw error;
  }
}
function changeRequestSource(source,{remove=false}={}) {
  try{
    assertAppliedConfig();
    captureFormDraft();state.config.request_source=source;if(remove)state.config.request_pack=null;
    configChanged();state.jsonDraft=null;state.jsonBase=null;renderNew();
    $(`[name="request_source"][value="${source}"]`)?.focus();
  }catch(error){configError(error);}
}
function renderPackResults(pack) {
  return `<div class="traffic-summary"><h3>Request pack · quiet and loaded evidence</h3><p class="hint">Missing scored completions are insufficient evidence.</p>${pack.control_status?`<p>Quiet control ${badge(pack.control_status)}</p>`:''}</div><div class="metric-table"><table><thead><tr><th>Case</th><th>Quiet</th><th>Loaded · correct / scored, valid</th></tr></thead><tbody>${(pack.cases || []).map(item=>`<tr><td>${escape(item.case_id)}</td><td>${badge(item.quiet?.status || 'inconclusive')}<small>${number(item.quiet?.correct)} / ${number(item.quiet?.scored)} scored · ${number(item.quiet?.valid)} valid</small></td><td>${(item.stages || []).map(stage=>`<div>${number(stage.rate_rps,2)} req/s: ${number(stage.correct)} / ${number(stage.scored)} scored · ${number(stage.valid)} valid<small>${number(stage.offered)} / ${number(stage.planned)} offered · ${number((stage.dropped_local || 0)+(stage.dropped_late || 0))} dropped${!stage.valid||!stage.scored?' · insufficient evidence':''}</small></div>`).join('') || 'No loaded evidence'}</td></tr>`).join('')}</tbody></table></div>`;
}
function syncSetupIdentity() {
  const element=$('#setup-context'),form=$('#new-run-form');if(!element||!form)return;
  const model=form.elements.model.value.trim(),url=form.elements.url.value.trim();
  element.hidden=state.setupStep!==2||state.configView==='json';
  element.innerHTML=`<div><span class="context-label">Testing</span><strong>${escape(model || 'Choose a model')}</strong><span class="context-endpoint">${escape(url)}</span>${state.config.targets.length>1?`<span>+ ${state.config.targets.length-1} more endpoints</span>`:''}</div><button type="button" class="text-button" data-setup-step="1">Edit connection</button>`;
}
function syncSetupStep({focus=false}={}) {
  if(!$('#setup-panel-1'))return;
  const step=state.setupStep;
  const titles=['Point Giraffe at an endpoint.','Set limits and choose checks.','Ready for launch.'];
  const heading=$('#setup-title');if(heading)heading.textContent=titles[step-1];
  const lede=$('#setup-lede');if(lede)lede.textContent=['Paste your model server’s address. Giraffe sends one tiny request to check it.','Pick what to test. Each check sends a limited number of requests.','Nothing is sent until you press Launch. You can stop at any time.'][step-1];
  syncSetupIdentity();
  const blueprint=$('#run-blueprint');if(blueprint)blueprint.hidden=step!==2&&state.configView!=='json';
  for(const panel of $$('[data-setup-panel]'))panel.hidden=Number(panel.dataset.setupPanel)!==step;
  for(const button of $$('.setup-stepper [data-setup-step]')){
    const index=Number(button.dataset.setupStep);
    button.classList.toggle('is-current',index===step);button.classList.toggle('is-complete',index<step);
    if(index===step)button.setAttribute('aria-current','step');else button.removeAttribute('aria-current');
  }
  const json=state.configView==='json';
  const next=$('[data-action="setup-next"]'),back=$('[data-action="setup-back"]');
  const needsModel=step===1&&!$('#new-run-form')?.elements.model.value.trim();
  next.hidden=!json&&step===3;next.innerHTML=json?'Return to setup':`${state.returnToReview?'Return to review':step===2?'Review run':'Choose checks'} ${icon('arrow')}`;
  next.disabled=state.submitting||hasJsonChanges()||(!json&&(needsModel||!tokenizerSetupReady()));back.hidden=step===1||json;back.disabled=state.submitting||hasJsonChanges();
  $('#start-run').hidden=step!==3||json;$('#start-run').disabled=!canStartRun();
  const navigationHint=$('#setup-navigation-hint');navigationHint.textContent=step===3?'Press ⌘↵ to launch. You can stop at any time.':!json&&needsModel?'Connect to your server or enter a model name to continue.':'';navigationHint.hidden=step!==3&&(json||!needsModel);
  const tools=$('.configuration-tools');if(tools)tools.hidden=step!==3&&!json;
  const runSummary=$('#run-bar-summary');if(runSummary)runSummary.hidden=true;
  $('.new-run-layout')?.classList.toggle('connecting',step===1&&!json);
  $('.new-run-layout')?.classList.toggle('configuring',step===2||json);
  if(focus){const target=step===3?$('#setup-title'):step===2?$('#test-selection h2'):$(`[data-setup-panel="${step}"] h2`);if(step!==2)target?.setAttribute('tabindex','-1');target?.focus({preventScroll:true});window.scrollTo?.({top:0,left:0,behavior:'instant'});}
}
function revealSetupStep(step,{focus=false}={}) {
  state.setupStep=step;if(step===3)state.returnToReview=false;
  if($('#config-panel-form')){state.configView='form';syncConfigView();}
  syncSetupStep({focus});
}
function validateSetupChecks() {
  const config=readForm(true).config;
  if(currentSettingErrors().length){focusFirstSettingError();throw new Error(currentSettingErrors()[0].msg);}
  if(!config.checks.length){state.catalogueFilter='all';syncTestControls();$('#check-search')?.focus();throw new Error('Select at least one check before reviewing the run.');}
  readForm(false);
  if(config.request_source==='request_pack'&&config.checks.some(id=>['correctness','load'].includes(id))){
    if(!config.request_pack?.cases?.length){revealQuestionEditor(config);$('[data-action="add-example"]')?.focus();throw new Error('Add a question or choose built-in examples before continuing.');}
    if(config.request_pack.cases.some(item=>item.scorer==='judge')&&!judgesReady(config)){configureEvaluator();throw new Error('Set up the AI evaluator before reviewing questions that need an AI answer check.');}
  }
}
function navigateSetup(step) {
  if(state.submitting||step<1||step>3)return;
  const error=$('#form-error');if(error)error.hidden=true;
  try{
    assertAppliedConfig();
    if(state.configView==='json'){revealSetupStep(state.setupStep,{focus:true});return;}
    if(step>state.setupStep){
      for(let index=state.setupStep;index<step;index++){
        if(!validateRunForm($('#new-run-form'),index))return;
        if(index===1&&!tokenizerSetupReady()){scheduleTokenizerSelection({immediate:true});($('#new-run-form')?.elements.tokenizer_custom||$('#field-tokenizer'))?.focus();return;}
        if(index===2){
          revealSetupStep(2);
          if($('#request-plan')&&!currentRequestPlan()&&!currentSettingErrors().length){
            captureFormDraft();const revision=state.configRevision,route=state.routeVersion;
            refreshRequestPreview(undefined,{draft:true}).then(()=>{if(revision===state.configRevision&&route===state.routeVersion)navigateSetup(step);}).catch(problem=>{if(revision===state.configRevision&&route===state.routeVersion){focusFirstSettingError(problem);if(error){error.textContent=problem.message;error.hidden=false;}}});return;
          }
          validateSetupChecks();
        }
      }
    }
    captureFormDraft();
    revealSetupStep(step,{focus:true});updateBounds();
  }catch(problem){focusFirstSettingError(problem);if(error){error.textContent=problem.message;error.hidden=false;}else configError(problem);}
}
function setupPlanCount(plan) {
  if(plan.request_count_label!=null){
    const label=plan.request_count_label;
    if(plan.count_kind==='bounded'&&label!=='Unknown'){
      const scheduled=label.replace(/^Unknown \+ /,'');
      return `${scheduled} ${scheduled.startsWith('≈')?'expected':'known'} requests + adaptive checks`;
    }
    return `${label} ${plan.count_kind==='exact'&&plan.planned_total===1?'request':'requests'}${plan.count_kind==='exact'?' planned':''}`;
  }
  return plan.count_kind==='exact'?`${number(plan.planned_total)} ${plan.planned_total===1?'request':'requests'} planned`:plan.count_kind==='estimate'?`About ${number(plan.planned_total)} requests`:`${number(plan.known_total)} known requests + adaptive checks`;
}
function renderReviewScope(payload) {
  const config=payload.config,targets=config.targets || [],questionChecks=config.checks.filter(id=>['correctness','load'].includes(id)),usesQuestions=questionChecks.length>0;
  const usesPack=usesQuestions&&config.request_source==='request_pack',cases=config.request_pack?.cases || [];
  const mixedSources=usesPack&&config.checks.some(id=>!['correctness','load','gpu'].includes(id));
  const source=usesQuestions?(usesPack?`Your questions · ${number(cases.length)} saved ${cases.length===1?'question':'questions'}${mixedSources?` for ${questionChecks.map(catalogTitle).join(' and ')}. Other checks use generated requests.`:''}`:'Built-in examples'):'';
  const baseline=payload.baseline_id?state.baselines.find(item=>item.id===payload.baseline_id):null;
  const comparison=payload.baseline_id?`Compare with saved run: ${baseline?.name || payload.baseline_id}`:'';
  const problems=[];
  if(targets.some(target=>!target.url?.trim()||!target.model?.trim()))problems.push({msg:'Enter an endpoint URL and model for every target.',fields:['targets.0.url','targets.0.model']});
  if(!config.checks.length)problems.push({msg:'Select at least one check.',fields:['checks']});
  if(usesPack&&!cases.length)problems.push({msg:'Add a question or choose built-in examples.',fields:['request_pack']});
  if(usesPack&&cases.some(item=>item.scorer==='judge')&&!judgesReady(config))problems.push({msg:'Configure the AI evaluator for questions that need it.',fields:['judging']});
  const restart=payload.mode==='cold-start'?targets.find(target=>target.name===payload.restart_target):null;
  return `<section class="review-section" id="review-endpoints"><div class="review-section-heading"><h3>${targets.length===1?'Endpoint':'Endpoints'}</h3><button type="button" class="text-button" data-review-edit="true" data-setup-step="1">Edit connection</button></div><ul class="review-targets">${targets.map(target=>`<li>${targets.length>1?`<strong>${escape(target.name)}</strong>`:''}<dl class="review-identity"><div><dt>Model</dt><dd>${escape(target.model || 'Model needed')}</dd></div><div><dt>URL</dt><dd class="review-endpoint-url">${escape(target.url || 'Endpoint URL needed')}</dd></div></dl></li>`).join('')}</ul></section><section class="review-section" id="review-checks"><div class="review-section-heading"><h3>Checks</h3><button type="button" class="text-button" data-review-edit="true" data-setup-step="2">Edit checks</button></div><ul class="review-check-names">${config.checks.length?config.checks.map(id=>`<li>${escape(catalogTitle(id))}</li>`).join(''):'<li>No checks selected</li>'}</ul>${source?`<p class="hint review-question-source">${escape(source)}</p>`:''}</section><section class="review-section review-compare" id="review-compare"><div class="review-section-heading"><h3>Compare with</h3>${state.baselines?.length?`<button type="button" class="text-button" data-action="choose-baseline">${payload.baseline_id?'Change':'Choose baseline'}</button>`:''}</div>${comparison?`<p class="hint review-baseline">${escape(comparison)}</p><p class="hint">Results will show what changed since that run.</p>`:state.baselines?.length?`<p class="hint">Not compared. Choose a saved baseline to see what changed since that run.</p>`:'<p class="hint">No baselines yet. After a run, open its results and press <strong>Save as reference baseline</strong>. You can then compare later runs against it here.</p>'}</section>${renderSettingIssues(problems,'warning')}${payload.mode==='cold-start'?notice(restart?.restart_command?.length?`Restart ${restart.name} before testing. This runs its configured restart command.`:'Restart selected, but no restart command is configured for that endpoint. Use the running server or configure a restart command.','warning'):''}`;
}
function browseCheckIntent(category) {
  if(!checkGroups.some(group=>group.id===category))return;
  state.catalogueFilter='all';state.catalogueCategory=category;state.catalogueSearch='';
  const search=$('#check-search');if(search)search.value='';
  syncTestControls();
}
// Endpoints already tested from this machine, one click from being tested again.
function renderRecentEndpoints() {
  const seen=new Map();
  for(const run of state.runs || [])for(const target of run.targets || [])if(target.url&&target.model&&!seen.has(`${target.url} ${target.model}`))seen.set(`${target.url} ${target.model}`,{...target,outcome:runOutcome(run)});
  const recent=[...seen.values()].slice(0,3);
  if(!recent.length)return '';
  return `<div class="start-row recent-endpoints"><span class="start-label">Recent</span><div class="recent-list">${recent.map(target=>{let host=target.url;try{host=new URL(target.url).host;}catch{}return `<button type="button" data-action="recent-endpoint" data-url="${escape(target.url)}" data-model="${escape(target.model)}" title="${escape(target.url)}"><span class="status-spot ${escape(target.outcome)}" aria-hidden="true"></span><span class="recent-model">${escape(target.model)}</span><span class="recent-host">${escape(host)}</span></button>`;}).join('')}</div></div>`;
}
function useRecentEndpoint(url,model) {
  try {
    assertAppliedConfig();const form=$('#new-run-form');
    form.elements.url.value=url;form.elements.url.setCustomValidity('');form.elements.model.value=model;form.elements.model.setCustomValidity('');
    state.autoModel=null;invalidateModelDiscovery();captureFormDraft();configChanged();updateBounds();
    $('[data-action="test-connection"]')?.focus();
  }catch(error){configError(error);}
}
function useRuntimeShortcut(runtime) {
  const urls={Ollama:'http://localhost:11434/v1',vLLM:'http://localhost:8000/v1',SGLang:'http://localhost:30000/v1','LM Studio':'http://localhost:1234/v1','llama.cpp':'http://localhost:8080/v1'};
  if(!urls[runtime])return;
  try {
    assertAppliedConfig();const form=$('#new-run-form');
    if(form.elements.url.value.trim()===urls[runtime])return;
    form.elements.url.value=urls[runtime];form.elements.url.setCustomValidity('');
    clearAPIKey();
    if(state.autoModel&&form.elements.model.value===state.autoModel)form.elements.model.value='';
    state.autoModel=null;invalidateModelDiscovery();captureFormDraft();configChanged();updateBounds();
    form.elements.url.focus();
  }catch(error){configError(error);}
}
function tokenizerChoice() {
  const form=$('#new-run-form'),mode=form?.elements.tokenizer?.value || 'auto';
  return mode==='custom'?(form.elements.tokenizer_custom?.value || '').trim():mode;
}
function tokenizerSelectionKey() {
  const form=$('#new-run-form'),token_id=tokenizerChoice();
  return form?.elements.tokenizer?.value==='custom'&&token_id?JSON.stringify({token_id}):null;
}
function currentTokenizerSelection() {
  const key=tokenizerSelectionKey(),result=state.tokenizerSelection;
  return key&&result?.key===key&&result.route===state.routeVersion?result:null;
}
function tokenizerSetupReady() {
  return $('#new-run-form')?.elements.tokenizer?.value!=='custom'||Boolean(tokenizerChoice())&&currentTokenizerSelection()?.status==='ready';
}
function renderTokenizerSelection() {
  const node=$('#tokenizer-status');if(!node)return;
  const result=currentTokenizerSelection(),empty=$('#new-run-form')?.elements.tokenizer?.value==='custom'&&!tokenizerChoice();
  node.textContent=empty?'Enter a tokenizer name, or pick Automatic.':['pending','error'].includes(result?.status)?result.message:'';
  node.className=!empty&&result?.status==='error'?'notice error':'hint';
  node.hidden=!node.textContent;
  syncSetupStep();
}
function scheduleTokenizerSelection({immediate=false}={}) {
  const key=tokenizerSelectionKey();if(hasJsonChanges())return;
  if(key&&currentTokenizerSelection())return;
  clearTimeout(state.tokenizerTimer);
  const sequence=state.tokenizerSequence=(state.tokenizerSequence || 0)+1,route=state.routeVersion;
  state.tokenizerSelection=key?{key,route,status:'pending',message:'Resolving and loading tokenizer… Files download here if needed.'}:null;
  renderTokenizerSelection();
  if(key)state.tokenizerTimer=setTimeout(()=>resolveTokenizerSelection(key,sequence,route),immediate?0:500);
}
async function resolveTokenizerSelection(key,sequence,route) {
  if(!key||sequence!==state.tokenizerSequence||route!==state.routeVersion||key!==tokenizerSelectionKey())return;
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),state.bootstrap.tokenizer_setup_timeout_seconds*1000);
  try{
    const result=await api('/api/targets/tokenizer',{method:'POST',body:key,signal:controller.signal});
    if(sequence!==state.tokenizerSequence||route!==state.routeVersion||key!==tokenizerSelectionKey())return;
    state.tokenizerSelection={...result,key,route};
    renderTokenizerSelection();
  }catch(error){
    if(sequence!==state.tokenizerSequence||route!==state.routeVersion||key!==tokenizerSelectionKey())return;
    state.tokenizerSelection={key,route,status:'error',message:controller.signal.aborted?'Tokenizer check timed out. Edit the Token ID or pick Automatic.':error.message};renderTokenizerSelection();
  }finally{clearTimeout(timeout);}
}
function modelDiscoveryKey() {
  const form=$('#new-run-form');if(!form)return null;
  return JSON.stringify({url:form.elements.url.value.trim(),model:form.elements.model.value.trim() || null,
    api_key_env:form.elements.api_key_env?.value.trim() || null,headers_env:state.config?.targets?.[0]?.headers_env || {},
    ca_bundle:state.config?.ca_bundle || null,proxy:state.config?.proxy || null,request_options:state.config?.request_options || {}});
}
function invalidateModelDiscovery() {
  state.modelDiscoverySequence++;state.modelDiscovery=null;renderModelDiscovery();scheduleTokenizerSelection();
}
function renderModelDiscovery() {
  const status=$('#model-discovery-status'),button=$('[data-action="test-connection"]');
  if(!status||!button)return;
  if(state.modelDiscovery?.status==='pending'&&state.modelDiscovery.route!==state.routeVersion)state.modelDiscovery=null;
  const discovery=state.modelDiscovery?.key===modelDiscoveryKey()?state.modelDiscovery:null;
  const model=$('#new-run-form')?.elements.model.value.trim();
  button.disabled=discovery?.status==='pending'||hasJsonChanges();button.textContent=discovery?.status==='pending'?'Connecting…':discovery?.status==='connected'?'Reconnect':'Connect';button.classList.toggle('handshaking',discovery?.status==='pending');
  button.classList.toggle('primary',!model);button.classList.toggle('small',Boolean(model));button.title='Sends one short request (32-token limit).';
  status.textContent=discovery?.message || '';
  status.classList.toggle('error',discovery?.status==='error');status.classList.toggle('connected',discovery?.status==='connected');
  const choice=$('#connection-model-choice'),manual=$('#manual-model-field'),options=$('#model-options');
  const models=discovery?.models || [];
  if(choice){
    const value=$('#new-run-form').elements.model.value;
    choice.innerHTML='<option value="">Choose a model…</option>'+models.map(model=>`<option value="${escape(model)}" ${value===model?'selected':''}>${escape(model)}</option>`).join('')+'<option value="__manual__">Enter a different model name</option>';
    choice.hidden=models.length<2;choice.previousElementSibling.hidden=models.length<2;
    manual.hidden=models.length>1&&value!=='__manual__'&&(!value||models.includes(value));
  }
  if(discovery?.status==='needs_model'&&options)options.open=true;
  const connectionOptions=$('#connection-options');if(connectionOptions&&['needs_model','error'].includes(discovery?.status))connectionOptions.open=true;
  const label=$('#model-options-label');if(label)label.textContent=discovery?.status==='needs_model'?`Giraffe found ${models.length} models. Choose one.`:discovery?.status==='connected'?'Found on your server.':discovery?.status==='error'?'Type the exact model name to continue without a check.':discovery?.status==='pending'?'Looking for models…':model?'Typed by you. Connecting first is optional.':'Found when you connect, or type the exact name.';
  const auth=$('#connection-auth');if(auth&&discovery?.status==='error'&&/key|401|403|unauthor/i.test(discovery.message || ''))auth.open=true;
  syncSetupStep();
}
async function testConnection() {
  try{assertAppliedConfig();}catch(error){configError(error);return;}
  const form=$('#new-run-form'),url=form?.elements.url;
  url?.setCustomValidity(url.value.trim()?'':'Enter an endpoint URL.');
  if(!url?.checkValidity()){url?.reportValidity();return;}
  const key=modelDiscoveryKey(),sequence=++state.modelDiscoverySequence,route=state.routeVersion;
  state.modelDiscovery={key,route,status:'pending',models:[],message:'Checking your endpoint. A model may take a moment to load…'};renderModelDiscovery();
  try{
    const api_key=apiKeyFor(url.value);
    const result=await api('/api/targets/test-connection',{method:'POST',body:JSON.stringify({...JSON.parse(key),...(api_key?{api_key}:{})})});
    if(sequence!==state.modelDiscoverySequence||route!==state.routeVersion||key!==modelDiscoveryKey())return;
    if(result.status==='connected'){
      if(!JSON.parse(key).model)state.autoModel=result.model;
      form.elements.model.value=result.model;form.elements.model.setCustomValidity('');
      state.config.targets[0].model=result.model;
      configChanged();updateBounds();
    }
    state.modelDiscovery={key:modelDiscoveryKey(),route,status:result.status,models:result.models || [],message:result.status==='connected'?`Connected · ${result.model}${result.latency_ms==null?' · latency unavailable':` · ${ms(result.latency_ms)}`}`:result.message};
    renderModelDiscovery();scheduleTokenizerSelection({immediate:true});
    if(result.status==='connected')$('#model-options').open=false;
    if(result.status==='needs_model')(result.models?.length>1?$('#connection-model-choice'):form.elements.model)?.focus();
  }catch(error){
    if(sequence!==state.modelDiscoverySequence||route!==state.routeVersion||key!==modelDiscoveryKey())return;
    state.modelDiscovery={key,route,status:'error',models:[],message:error.message};renderModelDiscovery();
  }
}
function renderPlanBounds(config,className='workload-bounds') {
  return `<dl class="${className}"><div><dt>Maximum requests</dt><dd>${config.max_requests==null?'Not set':number(config.max_requests)}</dd></div><div><dt>Maximum duration</dt><dd>${config.max_duration_seconds==null?'Not set':elapsed(config.max_duration_seconds)}</dd></div><div><dt>Simultaneous limit</dt><dd>${config.concurrency==null?'Not set':number(config.concurrency)}</dd></div><div><dt>Maximum memory</dt><dd>${config.max_memory_mb===0?'Off':`${number(config.max_memory_mb)} MiB`}</dd></div></dl>`;
}
function planRowCount(row) {
  return row.requests_per_target==null?(row.kind==='duration'?`Duration-based${row.estimated_requests_per_target!=null?` · ~${number(row.estimated_requests_per_target)} (estimate, ${escape(row.estimate_source||'from your last run')})`:''}`:'Adaptive'):`${row.kind==='estimate'?'~ ':''}${number(row.requests_per_target)}`;
}
function planRowDescriptor(row,config) {
  if(row.id==='arrivals'&&config.arrivals?.mode==='burst')return `in ${number(Math.ceil(config.arrivals.requests/config.arrivals.burst_size))} bursts`;
  if(row.id==='capacity'&&config.traffic?.arrival==='poisson')return 'exact count; seeded random timing';
  return '';
}
function renderRunBlueprint(config,plan=null) {
  const traffic=(plan?.rows || []).filter(row=>row.requests_per_target!==0);
  const waiting=currentSettingErrors().length||state.requestPreview?.status==='error'?'Resolve the settings below to calculate traffic.':hasJsonChanges()?'Apply JSON changes to update this plan.':'Calculating traffic…';
  const split=config.max_requests!=null&&config.targets.length>1?`<p class="hint">Request budget split per endpoint: ${Object.entries(plan?.target_quotas||{}).map(([name,quota])=>`${escape(name)}: ${number(quota)}`).join(' · ')||'calculated in the current plan'}.</p>`:'';
  const status=currentSettingErrors().length||plan?.errors?.length||(plan?.resource_issues||[]).some(issue=>issue.blocking)?'error':plan?'valid':state.requestPreview?.status==='error'?'error':'pending';
  return `<header class="blueprint-heading"><h2>Run plan</h2></header><p class="blueprint-intro" data-plan-state="${status}" role="status" aria-live="polite">${escape(plan?setupPlanCount(plan):waiting)}</p>${hasJsonChanges()?'':renderPlanBounds(config)+split}
  <div class="field-label"><button type="button" class="text-button blueprint-edit" data-action="edit-run-limits" ${hasJsonChanges()?'disabled':''}>Edit limits</button>${infoHelp('run_plan_limits','Run limits','Optional limits you set. Tests also stop at their own durations, request counts and per-request timeouts.')}</div>
  ${traffic.length?`<details class="workload-breakdown"><summary>Request breakdown</summary><div>${traffic.map(row=>`<details data-plan-row="${escape(row.id)}"><summary><strong>${planRowCount(row)}</strong> ${escape(row.title)}</summary><p>${escape(row.summary)}${planRowDescriptor(row,config)?` · ${escape(planRowDescriptor(row,config))}`:''}</p><small>${escape(row.source)} · ${row.requests_per_target==null?(config.max_requests==null&&config.max_duration_seconds==null?'bounded by this test’s own settings':'bounded by your limits'):'per endpoint'}</small></details>`).join('')}</div></details>`:''}
  ${renderLoadPlan(config,plan)}${renderSettingIssues([...localSettingWarnings(),...(plan?.load_warnings||[])],'warning')}${renderMigrationNotes()}${renderSettingIssues()}${state.requestPreview?.status==='error'&&!currentSettingErrors().length?notice(state.requestPreview.error||'Plan unavailable. Retry the request plan.','error'):''}${renderPlanTime(plan)}${plan?.evaluator?.active?'<p class="blueprint-note">AI evaluation runs afterward with separate limits.</p>':''}`;
}
function syncRunBlueprint(config,plan=null) {
  const element=$('#run-blueprint');if(!element)return;
  const expansion=state.blueprintExpansion ||= {outer:false,rows:[],checks:[],assumptions:false};
  const previousOuter=element.querySelector('.workload-breakdown'),previousAssumptions=element.querySelector('.estimate-assumptions');
  if(previousOuter){expansion.outer=previousOuter.open;expansion.rows=[...element.querySelectorAll('details[data-plan-row][open]')].map(item=>item.dataset.planRow);}
  const previousChecks=[...element.querySelectorAll('details[data-plan-check]')];
  if(previousChecks.length)expansion.checks=previousChecks.filter(item=>item.open).map(item=>item.dataset.planCheck);
  if(previousAssumptions)expansion.assumptions=previousAssumptions.open;
  const openRows=new Set(expansion.rows),openChecks=new Set(expansion.checks);
  const focused=document.activeElement?.closest?.('[data-plan-row], [data-plan-check], .workload-breakdown');
  const focusRow=focused?.dataset?.planRow,focusCheck=focused?.dataset?.planCheck,focusOuter=focused?.classList?.contains('workload-breakdown');
  element.innerHTML=renderRunBlueprint(config,plan);
  const outer=element.querySelector('.workload-breakdown');if(outer)outer.open=expansion.outer;else if(plan)expansion.outer=false;
  const assumptions=element.querySelector('.estimate-assumptions');if(assumptions)assumptions.open=expansion.assumptions;else if(plan)expansion.assumptions=false;
  for(const node of element.querySelectorAll('details[data-plan-row], details[data-plan-check]')){
    node.open=node.dataset.planRow?openRows.has(node.dataset.planRow):openChecks.has(node.dataset.planCheck);
    if(node.dataset.planRow&&node.dataset.planRow===focusRow||node.dataset.planCheck&&node.dataset.planCheck===focusCheck)node.querySelector('summary')?.focus({preventScroll:true});
  }
  if(plan){const rows=new Set([...element.querySelectorAll('details[data-plan-row]')].map(node=>node.dataset.planRow));expansion.rows=expansion.rows.filter(id=>rows.has(id));const checks=new Set([...element.querySelectorAll('details[data-plan-check]')].map(node=>node.dataset.planCheck));expansion.checks=expansion.checks.filter(id=>checks.has(id));}
  if(focusOuter)outer?.querySelector('summary')?.focus({preventScroll:true});
}
function watchPlanViewport() {
  state.planViewportObserver?.disconnect();
  const plan=$('#run-blueprint'),dock=$('.setup-navigation');
  const resize=()=>{const height=dock?.getBoundingClientRect?.().height;if(plan?.style?.setProperty&&height)plan.style.setProperty('--run-dock-clearance',`${height+30}px`);};
  resize();
  if(dock&&typeof ResizeObserver!=='undefined'){state.planViewportObserver=new ResizeObserver(resize);state.planViewportObserver.observe(dock);}
}
function renderNew(preset=inferSetupPreset(state.config,{limits:state.bootstrap?.limits})) {
  cancelStopConfirmationTimer({reset:true});
  state.setupPreset=preset;
  if(preset==='custom')state.showAvailableTests=true;
  const c=state.config,target=c.targets?.[0] || {},limits=c.limits || {};
  const tokenizerMode=target.tokenizer==null||target.tokenizer==='auto'?'auto':target.tokenizer==='builtin'?'builtin':'custom';
  const tokenizerName=tokenizerMode==='custom'?target.tokenizer:$('#new-run-form')?.elements.tokenizer_custom?.value || '';
  if(sessionAPIKey && !apiKeyFor(target.url))clearAPIKey();
  $('#main').innerHTML=`${renderHeroCoat()}<div class="page-heading setup-page-heading"><div><h1 id="setup-title">Test your endpoint</h1><p class="setup-lede" id="setup-lede"></p></div><button type="button" class="text-button setup-import" data-action="import-config">Load saved setup</button></div>
  <div class="notice" id="new-active-notice" ${state.active||state.diskActive?'':'hidden'}>${newActiveNotice()}</div>
  <form id="new-run-form" novalidate>
  <ol class="setup-stepper" aria-label="Run setup">${['Connect','Checks','Launch'].map((label,index)=>`<li><button type="button" class="setup-step" data-setup-step="${index+1}" aria-label="Step ${index+1}: ${label}" aria-controls="setup-panel-${index+1}"><span class="setup-step-index" aria-hidden="true">${index+1}</span><strong>${label}</strong></button></li>`).join('')}</ol>
<div class="notice config-draft-notice" id="config-draft-notice" hidden><div><strong>Unapplied JSON changes.</strong> Apply or discard them before editing the form, running, exporting, or copying the CLI command.</div><div class="actions"><button class="button small" type="button" data-action="apply-config">Apply changes</button><button class="text-button" type="button" data-action="discard-config">Discard changes</button></div></div><div id="config-error" class="error-message config-action-error" role="alert" hidden></div><div class="panel cli-fallback" id="cli-fallback" hidden><label for="cli-command">Copy command</label><textarea id="cli-command" readonly spellcheck="false"></textarea></div><div id="setup-context" class="setup-context" hidden></div><div class="new-run-layout"><div id="config-panel-form" role="tabpanel" aria-labelledby="config-tab-form">
  <section class="setup-step-content" id="setup-panel-1" data-setup-panel="1" aria-label="Connect">
  <div class="connection-layout"><section class="setup-section" id="setup-deployment"><h2 class="sr-only">Connection settings</h2><div class="setup-body"><div class="fields">
  <div class="endpoint-connect">${field('url','Server address',target.url,{type:'url',required:true,full:true,placeholder:'http://localhost:11434/v1',hint:''})}<button type="button" class="button primary" data-action="test-connection">Connect</button></div></div><div class="setup-connection-status" id="model-discovery-status" role="status" aria-live="polite"></div>
  <div class="start-from"><div class="start-row"><span class="start-label">Quick fill</span><div class="runtime-shortcuts">${['Ollama','vLLM','SGLang','LM Studio','llama.cpp'].map(runtime=>`<button type="button" data-action="runtime-shortcut" data-runtime="${runtime}">${runtime}</button>`).join('')}</div></div>${renderRecentEndpoints()}<div class="start-row"><span class="start-label">Saved setup</span><div><button type="button" class="button small start-file" data-action="import-config"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 13V3.5M6.5 7 10 3.5 13.5 7M4 12.5v3A1.5 1.5 0 0 0 5.5 17h9a1.5 1.5 0 0 0 1.5-1.5v-3"/></svg>Load from file…</button></div></div></div>
  <div class="connection-details" id="connection-options">
  <div class="connection-row model-options" id="model-options"><label class="row-label" for="field-model">Model</label><div class="row-control"><label for="connection-model-choice" hidden>Available models</label><select id="connection-model-choice" hidden></select><div id="manual-model-field">${field('model','Model',target.model || '',{required:true,placeholder:'Found when you connect',hint:''})}</div><p class="hint" id="model-options-label"></p></div></div>
  <div class="setup-advanced"><div class="details-content">
  <details class="connection-auth" id="connection-auth"><summary class="api-key-row"><span class="api-key-label">API key (optional)</span><span class="row-value api-key-value">${apiKeyFor(target.url)?'Set for this tab':escape(target.api_key_env)||'Not set'}</span><span class="row-action api-key-action"><span class="when-closed">Edit</span><span class="when-open">Close</span></span></summary>${field('api_key','API key (optional)','',{type:'password',placeholder:'Paste your API key',hint:'',help:'Kept in memory for this tab. Never included in saved setups or reports. Paste it again after reloading. Leave blank for an open endpoint.'})}<button type="button" class="text-button" data-action="clear-api-key">Clear key</button>${field('api_key_env','API key environment variable',apiKeyFor(target.url)?'':target.api_key_env,{placeholder:'OPENAI_API_KEY',hint:''})}</details>
  ${renderField('tokenizer','Token ID / tokenizer',`<select id="field-tokenizer" name="tokenizer" aria-describedby="tokenizer-status">${[['auto','Automatic'],['builtin','builtin'],['custom','Custom']].map(([value,label])=>`<option value="${value}" ${tokenizerMode===value?'selected':''}>${label}</option>`).join('')}</select><div id="tokenizer-custom-field" ${tokenizerMode==='custom'?'':'hidden'}>${field('tokenizer_custom','Tokenizer name',tokenizerName,{placeholder:'Qwen/Qwen2.5-7B-Instruct',hint:''}).replace('autocomplete="off"','autocomplete="off" maxlength="512" aria-describedby="tokenizer-status"')}</div><p id="tokenizer-status" class="hint" role="status" aria-live="polite"></p>`,{hint:'',help:"Used to size prompts. Automatic uses the model's own tokenizer when it can be found, otherwise builtin. builtin uses tiktoken o200k_base, included with Giraffe; its counts are approximate. Custom loads a Hugging Face model ID or local path and needs giraffe-llm[hf-tokenizer]."})}
  <div class="fields">${field('metrics_url','Server metrics URL (optional)',target.metrics_url,{type:'url',hint:'',placeholder:'http://localhost:8000/metrics'})}${field('host_metrics_url','Host metrics URL (optional)',target.host_metrics_url,{type:'url',hint:'',placeholder:'http://localhost:9100/metrics'})}${selectField('stream','Streaming',`<option value="true" ${c.stream?'selected':''}>On</option><option value="false" ${c.stream?'':'selected'}>Off</option>`,{hint:''})}</div>
  </div></div></div>
  ${(c.targets || []).length>1?`<div class="notice multi-target-notice"><strong>${c.targets.length} endpoints configured.</strong> This connection test checks the first endpoint. The run checks all configured endpoints.</div>`:''}
  </div></section></div>
  </section><section class="setup-step-content" id="setup-panel-2" data-setup-panel="2" aria-label="Choose checks" hidden>
  ${renderSharedRunSettings(c)}${renderCheckSelection(c)}</section>
  <section class="setup-step-content" id="setup-panel-3" data-setup-panel="3" aria-label="Review and run" hidden>
  <section class="setup-section" id="setup-review"><h2 class="sr-only">Run configuration</h2><div class="setup-body">
  <div class="review-main"><div id="run-bounds"></div>
  <details class="setup-disclosure" id="execution-settings"><summary>Saved evidence, baseline & restart options</summary><div class="details-content"><div class="fields">${selectField('retention','Keep response text',['failures','all','none'].map(mode=>`<option value="${mode}" ${mode===(c.retention||'failures')?'selected':''}>${{failures:'Failures only',all:'All',none:'None'}[mode]}</option>`).join(''),{hint:'',help:'Questions and expected answers are always saved with the run. This setting controls whether response text and evaluator reasons are kept. Timings, status and scores are always saved. It does not cap disk use; storage limits do.'})}${field('name','Endpoint name',target.name,{required:true,placeholder:'local'})}${selectField('mode','Server startup','<option value="normal">Use the running server</option><option value="cold-start">Restart one server before testing</option>')}<div id="restart-controls" hidden>${selectField('restart_target','Server to restart',(c.targets || []).map(t=>`<option value="${escape(t.name)}">${escape(t.name)}</option>`).join(''))}<p class="hint" id="restart-hook"></p><p class="hint">Runs only the restart command already configured for this server.</p></div><div>${selectField('baseline_id','Compare with baseline',baselineOptions(state.prefillBaseline || ''))}${schemaField('limit_regression_percent',limits.regression_percent,state.bootstrap.limits_schema.properties.regression_percent,{label:'Regression threshold (%)',hint:''})}</div></div></div></details>
  </div><section id="request-plan" class="request-plan review-section"></section><div id="setup-history" hidden></div>
  </div></section></section></div>
<section class="panel form-section" id="config-panel-json" role="tabpanel" aria-labelledby="config-tab-json" hidden><h2>Run configuration</h2><p class="hint">Apply changes before returning to the form.</p>${renderField('config_editor','Configuration · JSON',`<textarea class="config-editor" id="config-editor" spellcheck="false">${escape(state.jsonDraft ?? JSON.stringify(c,null,2))}</textarea>`,{controlId:'config-editor',hint:'',help:[fieldDescription('config_editor').help,inputRetentionNotice].join(' ')})}<div class="editor-actions"><button class="button small" type="button" data-action="apply-config">Apply changes</button><span class="hint" id="json-status" role="status"></span></div></section><aside id="run-blueprint" class="run-blueprint" tabindex="0" aria-label="Run plan" hidden></aside>

  </div>
  <details class="configuration-tools" ${state.configView==='json'||hasJsonChanges()?'open':''}><summary>Edit advanced configuration, import or export</summary><div class="config-toolbar"><div class="result-tabs config-tabs" role="tablist" aria-label="Configuration view"><button type="button" role="tab" id="config-tab-form" data-config-tab="form" aria-controls="config-panel-form">Guided setup</button><button type="button" role="tab" id="config-tab-json" data-config-tab="json" aria-controls="config-panel-json">Advanced editor</button></div><div class="actions"><button type="button" class="text-button" data-action="import-config">Load saved setup</button><button type="button" class="text-button" data-action="export-config">Save setup</button><button type="button" class="text-button" data-action="copy-cli">Copy terminal command</button></div><input type="file" id="config-file" accept=".json,application/json" hidden></div></details>
  <div class="run-action-bar setup-navigation"><div><span class="dock-summary" id="dock-summary" aria-hidden="true"></span><strong id="run-bar-summary" hidden></strong><p class="hint" id="setup-navigation-hint">No test requests are sent automatically.</p></div><div class="actions"><button class="button" type="button" data-action="setup-back" hidden>Back</button><button class="button primary" type="button" data-action="setup-next">Continue ${icon('arrow')}</button><button class="button primary" type="submit" id="start-run" ${state.active?'disabled':''} hidden>Launch <kbd>⌘ ↵</kbd></button></div><div id="form-error" class="error-message" role="alert" hidden></div></div>
  </form>`;
  for(const check of state.bootstrap.checks){
    const options=c.test_options?.[check.id] || {};
    for(const name of check.option_fields){const input=$('#new-run-form').elements[`test_${check.id}_${name}`];if(input)input.dataset.inherited=String(options[name]==null);}
    for(const name of check.limit_fields){const input=$('#new-run-form').elements[`test_${check.id}_limit_${name}`];if(input)input.dataset.inherited=String(!Object.hasOwn(options.limits || {},name));}
  }
  const form=$('#new-run-form');if(form.elements.load_patterns_concurrent_levels)form.elements.load_patterns_concurrent_levels.dataset.inherited=String(c.load_test?.patterns?.concurrent?.levels===undefined);if(form.elements.traffic_max_in_flight)form.elements.traffic_max_in_flight.dataset.inherited=String(c.traffic?.max_in_flight==null);form.elements.mode.value=state.formMode;
  if(form.elements.api_key)form.elements.api_key.value=apiKeyFor(target.url);
  if(state.formRestartTarget&&c.targets.some(t=>t.name===state.formRestartTarget))form.elements.restart_target.value=state.formRestartTarget;
  if(state.jsonDraft===null){state.jsonDraft=JSON.stringify(portableConfig(readForm(true)),null,2);state.jsonBase=state.jsonDraft;$('#config-editor').value=state.jsonDraft;}
  syncTestControls();updateBounds();updateRestart();syncConfigView();syncPackControls();renderRequestPreview();syncSetupStep();renderModelDiscovery();watchPlanViewport();scheduleTokenizerSelection({immediate:true});renderTokenizerSelection();
}
function readForm(allowEmpty=false) {
  const form=$('#new-run-form'),data=new FormData(form);let config=clone(state.config);
  // Disabled editors still retain their values when a test is unchecked.
  for(const input of form.querySelectorAll('[data-test-editor] input, [data-test-editor] select, [data-test-editor] textarea'))data.set(input.name,input.type==='checkbox'?(input.checked?'on':'off'):input.value);
  if(!config.targets?.length)throw new Error('Configuration must contain at least one target.');
  const first=config.targets[0];for(const key of ['url','model','name'])first[key]=String(data.get(key) || '').trim();
  if(form.elements.api_key_env)first.api_key_env=form.elements.api_key_env.value.trim() || (apiKeyFor(first.url)?'GIRAFFE_API_KEY':null);
  if(form.elements.metrics_url)first.metrics_url=form.elements.metrics_url.value.trim() || null;
  first.tokenizer=tokenizerChoice();
  if(form.elements.host_metrics_url)first.host_metrics_url=form.elements.host_metrics_url.value.trim() || null;
  for(const [key] of numericFields)if(data.has(key))config[key]=data.get(key)===''?null:Number(data.get(key));
  for(const name of ['max_requests','max_duration_seconds','concurrency'])if(form.elements[`${name}_enabled`])data.set(`${name}_enabled`,form.elements[`${name}_enabled`].checked?'on':'off');
  config=configureRunLimits(config,data,{allowInvalid:allowEmpty});
  config.limits ||= {};
  for(const key of [...limitFields.map(x=>x[0]),'max_error_rate','min_correctness','min_samples','regression_percent']){if(!data.has(`limit_${key}`))continue;const v=data.get(`limit_${key}`);config.limits[key]=v===''?null:Number(v)/(['max_error_rate','min_correctness'].includes(key)?100:1);}
  if(form.elements.samples)config.samples=Number(form.elements.samples.value);
  if(form.elements.retention)config.retention=form.elements.retention.value;
  if(form.elements.stream)config.stream=form.elements.stream.value==='true';
  config=configureTests(config,data,state.bootstrap.checks,state.bootstrap.config.traffic,{allowEmpty,allowInvalid:allowEmpty,percentageInputs:true,limits:state.bootstrap.limits});
  if(form.elements.load_patterns_concurrent_levels?.dataset.inherited==='true')data.set('load_patterns_concurrent_levels',loadSettings(config).patterns.concurrent.levels.join(', '));
  config=configureLoadTest(config,data,loadFormDefaults(config),{allowInvalid:allowEmpty,limits:state.bootstrap.limits});
  config=configureJudging(config,data,{allowInvalid:allowEmpty});
  config=configureProfiles(config,data,state.bootstrap.profile_defaults || {},{allowInvalid:allowEmpty,limits:state.bootstrap.limits});
  config=configureSessions(config,data,state.bootstrap.session_workloads.defaults,{allowInvalid:allowEmpty});
  config.restart_target=null;
  return {config,baseline_id:data.get('baseline_id') || null,mode:data.get('mode') || 'normal',restart_target:data.get('mode')==='cold-start'?data.get('restart_target'):null};
}
function updateBounds() {
  let payload;try{payload=readForm(true);}catch{return;}
  const c=payload.config;
  syncSetupIdentity();
  refreshDefaultProfileFields(c);
  const derived=$('#derived-concurrency');if(derived){
    derived.textContent=`At most ${number(c.concurrency)} ${c.concurrency===1?'request':'requests'} at once.`;
  }
  updateDatasetReadiness(c);
  for(const check of state.bootstrap.checks){
    const card=$(`[data-test-card="${check.id}"]`);
    updateCheckRow(check,c,card);
  }
  $('#run-bounds').innerHTML=renderReviewScope(payload);
  const selectionText=`${c.checks.length} ${c.checks.length===1?'check':'checks'} selected`;
  if($('#selection-summary').textContent!==selectionText)$('#selection-summary').textContent=selectionText;
  const summary=$('#run-bar-summary');if(summary){summary.textContent='';summary.hidden=true;}
  updateSharedSettingsSummaries(c);
  syncLoadDisplay(c);syncInlineErrors();updateCheckFieldHints(c);renderRequestPlan(c);scheduleRequestPlan();
}
function updateDatasetReadiness(config) {
  const ready=judgesReady(config);
  for(const marker of $$('[data-evaluator-needed]'))marker.hidden=ready;
  const evaluator=$('#evaluator-status');if(!evaluator)return;
  const count=(config.request_pack?.cases || []).filter(item=>item.scorer==='judge').length;
  $('p',evaluator).textContent=ready?'AI evaluator ready.':`${number(count)} ${count===1?'question needs':'questions need'} an AI evaluator.`;
  $('button',evaluator).textContent=ready?'Edit evaluator':'Configure evaluator';
}
function configureEvaluator() {
  const details=$('.judge-configuration');if(!details)return;
  revealQuestionEditor();
  details.open=true;state.judgeEditorOpen=true;
  if(!state.config.judging?.judges?.length)changeJudge('add');
  else $('[name="judge_0_url"]')?.focus();
}
function updateRestart(){const form=$('#new-run-form');if(!form)return;const mode=form.elements.mode.value;$('#restart-controls').hidden=mode!=='cold-start';const name=form.elements.restart_target.value;const target=state.config.targets.find(t=>t.name===name);$('#restart-hook').textContent=target?.restart_command?.length?`Configured command: ${target.restart_command.join(' ')}`:'No restart command is configured for this target. Add restart_command in the configuration before using cold start.';updateBounds();}
async function startRun(event) {
  event.preventDefault();if(state.submitting||state.active||state.configBusy)return;
  if($('#setup-panel-1')&&state.setupStep<3){navigateSetup(state.setupStep+1);return;}
  const version=state.routeVersion;$('#form-error').hidden=true;
  try {
    assertAppliedConfig();
    if(!validateRunForm(event.target))return;
    const draft=readForm(true).config;
    if(draft.request_source==='request_pack'&&draft.checks.some(id=>['correctness','load'].includes(id))){
      if(!draft.request_pack?.cases?.length){revealQuestionEditor(draft);$('[data-action="add-example"]').focus();throw new Error('Add a question or choose built-in examples before testing.');}
      if(draft.request_pack.cases.some(item=>item.scorer==='judge')&&!judgesReady(draft)){revealSetupStep(2);configureEvaluator();throw new Error('Set up the AI evaluator to check questions that accept paraphrased answers.');}
    }
    if(currentSettingErrors().length){focusFirstSettingError();throw new Error(currentSettingErrors()[0].msg);}
    if(!currentRequestPlan())throw new Error('Wait for the current request plan before starting. If planning failed, use Retry request plan.');
    state.submitting=true;$('#start-run').disabled=true;$('#start-run').textContent='Validating settings…';
    const payload=await validatedSnapshot();
    await refreshRequestPreview(payload.config);
    if(currentResourceErrors().length)throw new Error(currentResourceErrors()[0].msg);
    if(payload.mode==='cold-start'&&!payload.config.targets.find(t=>t.name===payload.restart_target)?.restart_command?.length)throw new Error('Cold start requires a configured restart_command for the selected target.');
    if(version!==state.routeVersion)return;
    $('#start-run').textContent='Checking input preparation…';
    const result=await api('/api/runs',{method:'POST',body:JSON.stringify({...payload,migration_notes:[...(state.migrationNotes || [])],api_keys:sessionAPIKeys(payload.config)})});state.config=clone(payload.config);state.config.targets[0].tokenizer=draft.targets[0].tokenizer;
    if(draft.request_source==='request_pack'&&!draft.request_pack&&!draft.checks.some(id=>['correctness','load'].includes(id)))state.config.request_source=draft.request_source;
    setActive(result.id);if(version===state.routeVersion)location.hash=linkRun(result.id);
  }catch(error){if(version!==state.routeVersion){toast(`Run was not started: ${error.message}`);return;}focusFirstSettingError(error);const errorEl=$('#form-error');errorEl.textContent=error.message;errorEl.hidden=false;if($('#start-run')){$('#start-run').disabled=true;$('#start-run').innerHTML=`Launch ${icon('arrow')}`;}}finally{state.submitting=false;const button=$('#start-run');if(button)button.disabled=!canStartRun();}
}
function summaryReport(detail){return {checks:detail.summary?.checks||[],requests:detail.live_requests||[],abort_reason:detail.abort_reason};}
function reportConfig(report){return report?.manifest?.config || state.detail?.config || {};}
function reportSummary(report){const summary=report.observations?.summary;return typeof summary==='string'?summary.trim():'';}
function mainFinding(report) {
  if(userStopped(report))return 'Stopped by you';
  if(report.abort_reason==='run cancelled')return 'Stopped';
  const summary=reportSummary(report);if(/could not connect|no checks could run/i.test(summary))return shortReason(summary).summary;
  const checks=report.checks.filter(c=>c.id!=='baseline'),failed=checks.find(c=>c.status==='fail');
  if(!checks.length)return 'No checks ran';
  if(failed){if(failed.metrics?.scored&&failed.metrics.correct<failed.metrics.scored)return `${checkGuide[failed.id]?.name || failed.title}: ${failed.metrics.correct} of ${failed.metrics.scored} correct`;return `${checkGuide[failed.id]?.name || failed.title} failed`;}
  if(report.abort_reason)return 'Run stopped with partial results';
  const interrupted=report.observations?.resources?.memory_interrupted_checks || [];
  const uncertain=checks.find(c=>c.required!==false&&['blocked','inconclusive'].includes(c.status)&&!(c.required===false&&interrupted.some(item=>item.target===c.target&&item.id===c.id)));
  if(uncertain)return `${checkGuide[uncertain.id]?.name || uncertain.title} is ${uncertain.status}`;
  if(report.baseline?.status==='fail')return 'A material regression was detected';
  if(report.baseline?.status==='inconclusive')return 'The baseline comparison is inconclusive';
  if(checks.some(c=>c.required!==false&&c.status==='skipped'&&!unselectedCheck(c,report)))return 'Some selected checks did not run';
  if(checks.length&&checks.every(c=>c.status==='skipped'))return 'No checks ran';
  if(report.overall==='pass')return 'Selected acceptance checks passed';
  return 'Review the recorded acceptance results';
}
function unselectedCheck(check,report) {
  if(check.status!=='skipped')return false;
  const configured=reportConfig(report).checks;
  return Array.isArray(configured)?!configured.includes(check.id):/^Not selected for this run\.?$/i.test(check.summary || '');
}
function userStopped(report) {
  const detail=state.detail || {};
  return [report?.abort_reason,detail.abort_reason,detail.progress?.abort_reason].some(reason=>typeof reason==='string'&&/^cancel(?:l)?ed by user$/i.test(reason));
}
let detailPlan={id:null,plan:null,pending:false,done:false};
function runPlan(detail=state.detail) {
  const direct=detail?.request_plan || detail?.plan || detail?.report?.manifest?.request_plan || detail?.report?.manifest?.plan;
  if(direct?.planned_total!=null)return direct;
  if(detailPlan.id===detail?.id&&detailPlan.plan)return detailPlan.plan;
  const config=detail?.config || detail?.report?.manifest?.config;
  if(config&&state.requestPreview?.plan&&sameConfig(config,state.requestPreview.config))return state.requestPreview.plan;
  if(config&&state.bootstrap?.request_plan&&state.bootstrap.config&&sameConfig(config,state.bootstrap.config))return state.bootstrap.request_plan;
  return direct;
}
function plannedRequests(detail=state.detail) {
  const plan=runPlan(detail);if(Number.isFinite(plan?.planned_total))return plan.planned_total;
  const conditions=detail?.live_conditions || detail?.report?.conditions || Object.values(detail?.report?.observations?.targets || {}).flatMap(target=>target.conditions || []);
  return detail?.progress?.planned_total ?? (conditions.length&&conditions.every(item=>Number.isFinite(item.planned))?conditions.reduce((sum,item)=>sum+item.planned,0):null);
}
async function loadDetailPlan() {
  const detail=state.detail;
  if(!activeState(detail)||plannedRequests(detail)!=null||typeof fetch!=='function'||!detail.config)return;
  if(detailPlan.id!==detail.id)detailPlan={id:detail.id,plan:null,pending:false,done:false};
  const current=detailPlan,version=state.routeVersion;if(current.pending||current.done)return;
  current.pending=true;
  try{const value=await api('/api/config/plan',{method:'POST',body:JSON.stringify(detail.config)});if(state.detail?.id===detail.id&&version===state.routeVersion)current.plan=value.request_plan || null;}
  catch{ /* The request count stays absent when planning is unavailable. */ }
  finally{current.pending=false;current.done=true;if(current.plan&&state.detail?.id===detail.id&&version===state.routeVersion&&activeState(state.detail))renderDetail();}
}
function finishedRequests(report,detail=state.detail) {
  const counts=[detail?.progress?.completed,detail?.summary?.request_count,report?.request_count];
  if(report?.overall!=null&&!report.requests_compact)counts.push(report.requests?.length);
  const known=counts.filter(Number.isFinite);
  return known.length?Math.max(...known):report?.requests?.length || 0;
}

function shortReason(reason) {
  const text=String(reason || '').replace(/To conclude,\s*/gi,'').trim();
  const sentences=(text.match(/[\s\S]+?(?:[.!?]+(?=\s|$)|$)/g)||[text])
    .map(sentence=>sentence.replace(/^(\s*["'“‘(\[{]*)(\p{L})/u,(_,prefix,letter)=>prefix+letter.toUpperCase()));
  const summary=sentences.slice(0,2).join('').trim();
  return {summary,rest:sentences.slice(2).join('').trim()};
}
function renderGuidance(report,detail) {
  if(userStopped(report)){
    const finished=finishedRequests(report,detail),planned=plannedRequests(detail);
    return `<p class="result-guidance">${planned==null?`${number(finished)} requests finished`:`${number(finished)} of ${number(planned)} planned requests finished`}. Partial results below.</p>`;
  }
  const reason=report.checks.find(c=>['fail','blocked','inconclusive'].includes(c.status));
  const raw=reportSummary(report) || report.abort_reason || reason?.summary || resultGuidance(report),text=shortReason(raw);
  const extra=[text.rest,report.abort_reason&&!raw.includes(report.abort_reason)?`Run stopped: ${report.abort_reason}`:''].filter(Boolean);
  return `${text.summary?`<p class="result-guidance">${escape(text.summary)}</p>`:''}${extra.length?`<details class="finding-reason"><summary>Details</summary>${extra.map(part=>`<p>${escape(part)}</p>`).join('')}</details>`:''}`;
}
function resultGuidance(report) {
  const checks=report.checks.filter(check=>check.id!=='baseline');
  if(report.overall==='pass'&&checks.some(check=>check.status==='pass')&&checks.every(check=>check.status==='pass'||unselectedCheck(check,report)))return 'The recorded checks met their configured limits. Save a baseline to compare your next change. This result applies to the endpoint and traffic tested here.';
  return '';
}

// Report navigation is scoped to this visit. Polls never override an explicit tab choice.
let reportView={id:null,tab:null,explicit:false,wasLive:false,terminalSwitch:false,performance:null,target:null,window:null,error:'',pending:false,sequence:0,lastFetch:0};
const reportSections=[['overview','Performance overview',null],['timing','Request timing',null],['throughput','Throughput & capacity','throughput_capacity'],['delivery','Load delivery','delivery'],['outcomes','Outcomes & quality','outcomes'],['tokens','Workload & tokens','token_accounting'],['effects','Conversations & workload effects','conversations'],['server','Server observations','server'],['baseline','Baseline comparison','baseline']];
const comparisonSections=[['answers','Answers & correctness'],['timing','Response timing'],['delivery','Request completion']];
const reportResultSections=[['comparison','Baseline comparison'],['acceptance','Pass / fail','comparison'],['load-acceptance','Tested load acceptance','comparison'],...comparisonSections.map(([id,label])=>[id,label,'comparison']),['checks','Checks & evidence'],['configuration','Run configuration'],['requests','All requests'],['resources','Peak memory (RAM)']];
function reportRoute(hash=typeof location==='undefined'?'':location.hash) {
  const request=hash.match(/^#runs\/([^/]+)\/requests\/([^/]+)$/);
  if(request){try{return {id:decodeURIComponent(request[1]),tab:'results',section:null,requestId:decodeURIComponent(request[2])};}catch{return null;}}
  const match=hash.match(/^#runs\/([^/]+)(?:\/(performance|results)(?:\/([^/]+))?)?$/);
  if(!match)return null;
  const tab=match[2]||null,section=match[3]||null;
  const sections=tab==='performance'?reportSections:reportResultSections;
  if(section&&!(tab==='results'&&section==='baseline')&&!sections.some(([id])=>id===section))return null;
  try{return {id:decodeURIComponent(match[1]),tab,section};}catch{return null;}
}
function syncReportView() {
  const d=state.detail;if(!d)return;
  if(reportView.id!==d.id){
    const route=reportRoute(),tab=route?.id===d.id?route.tab:null;
    reportView={id:d.id,tab:tab||'results',explicit:!!tab,wasLive:activeState(d),terminalSwitch:false,performance:null,target:null,window:null,error:'',pending:false,sequence:reportView.sequence+1,lastFetch:0};
  }
  if(reportView.wasLive&&!activeState(d)&&!reportView.terminalSwitch){
    reportView.terminalSwitch=true;state.heroActionGuard={id:d.id,until:Date.now()+600};
    reportView.performance=null;reportView.target=null;reportView.window=null;reportView.sequence++;reportView.pending=false;reportView.lastFetch=0;
    if(!reportView.explicit){reportView.tab='results';replaceReportAnchor();}
    announceRunComplete(d);
  }
  if(d.report?.performance&&!reportView.target&&!reportView.window)reportView.performance=d.report.performance;
  updateReportSections();
}
const announcedRuns=new Set();
function announceRunComplete(detail) {
  if(announcedRuns.has(detail.id))return;announcedRuns.add(detail.id);
  let region=$('#run-announcement');
  if(!region&&document.createElement&&document.body){region=document.createElement('div');region.id='run-announcement';region.setAttribute('id','run-announcement');region.setAttribute('class','sr-only');region.setAttribute('aria-live','polite');document.body.insertBefore(region,null);}
  if(region)region.textContent=`Run complete. ${detail.report?mainFinding(detail.report):'Partial results saved.'}`;
}
function replaceReportAnchor() {
  if(typeof location==='undefined')return;
  const hash=`${linkRun(reportView.id)}/${reportView.tab}`;
  // Replacing an automatic default must not add a browser-history entry or count as a user choice.
  if(typeof history!=='undefined'&&history.replaceState)history.replaceState(null,'',hash);
}
function chooseReportTab(tab,{focus=false,anchor=true}={}) {
  if(!['performance','results'].includes(tab)||!state.detail)return;
  syncReportView();reportView.tab=tab;reportView.explicit=true;
  renderDetail();
  if(tab==='performance'&&typeof fetch==='function')loadPerformance({force:true});
  if(anchor&&typeof location!=='undefined')location.hash=`${linkRun(state.detail.id)}/${tab}`;
  if(focus)$(`#tab-${tab}`)?.focus({preventScroll:true});
}
function resultsSections() {
  const report=state.detail?.report;if(!report?.baseline)return [];
  const rows=(report.baseline.targets||[]).flatMap(t=>[...(t.comparisons||[]),...comparisonOutputs(t)]);
  return reportResultSections.filter(([id])=>{
    if(id==='load-acceptance')return report.baseline.targets?.some(target=>target.load_test||target.load_exclusions?.length);
    if(comparisonSections.some(([section])=>section===id))return rows.some(row=>comparisonCategory(row)===id);
    return true;
  });
}
function renderResultsJump() {
  const sections=resultsSections();if(!sections.length)return '';
  const selected=reportRoute()?.section||'comparison';
  return `<nav class="results-jump" aria-label="Result sections"><label for="results-section-jump">Jump to section</label><select id="results-section-jump">${sections.map(([id,label,parent])=>`<option value="${id}" ${selected===id?'selected':''}>${parent?'↳ ':''}${escape(label)}</option>`).join('')}</select></nav>`;
}
function scrollReportSection(section,tab=reportView.tab){
  const panel=$(`#report-${tab}`);if(!panel)return;
  const node=tab==='results'&&section==='comparison'?$('#report-baseline-reference',panel):$(`#${tab==='results'?'results':'performance'}-${section}`,panel);
  const disclosure=node?.closest('details')||node?.querySelector('details.report-reference')||node?.querySelector('details.run-diagnostics');if(disclosure)disclosure.open=true;
  node?.scrollIntoView?.({block:'start'});
}
function updateReportSections() {
  const rail=$('#report-sections');if(!rail)return;
  const sections=reportView.tab==='performance'?reportSections.map(([id,label])=>[id,label]):resultsSections();
  rail.hidden=!state.reportRouteActive||!state.detail||!sections.length;
  if(rail.hidden){rail.innerHTML='';return;}
  const link=([id,label])=>`<a href="${linkRun(state.detail.id)}/${reportView.tab}/${id}" data-report-section="${id}" data-report-section-tab="${reportView.tab}">${escape(label)}</a>`;
  if(!rail.hidden)patchHTML(rail,`<h2>This report</h2><nav aria-label="This report"><ul class="report-section-list">${sections.filter(([, ,parent])=>!parent).map(section=>{
    const children=sections.filter(([, ,parent])=>parent===section[0]);
    return `<li>${link(section)}${children.length?`<ul aria-label="${escape(section[1])} sections">${children.map(child=>`<li>${link(child)}</li>`).join('')}</ul>`:''}</li>`;
  }).join('')}</ul></nav>`);
}
function selectedPerformance() {
  const data=reportView.performance,target=data?.targets?.find(item=>item.target===(reportView.target||data.selected_target||data.default_target))||data?.targets?.[0];
  const windowId=reportView.window||target?.selected_window||target?.default_window;
  return {data,target,window:target?.windows?.find(item=>item.id===windowId),summary:target?.summaries?.[windowId]};
}
function measurementLabel(window) {
  const bounds=window.start_ms==null?'bounds unavailable':`${number(window.start_ms/1000,2)}–${number(window.end_ms/1000,2)} s`;
  return window.selection_label||`${window.label||'Full request window'} · ${bounds} · ${number(window.request_count)} requests`;
}
function performanceWindowOptions(target,selected) {
  const groups=new Map();
  for(const item of target?.windows||[]){const label=item.workload_label||target.cohorts?.find(cohort=>cohort.id===item.cohort_id)?.label||'Recorded workload';if(!groups.has(label))groups.set(label,[]);groups.get(label).push(item);}
  return [...groups].map(([label,windows])=>`<optgroup label="${escape(label)}">${windows.map(item=>`<option value="${escape(item.id)}" ${selected?.id===item.id?'selected':''}>${escape(measurementLabel(item))}</option>`).join('')}</optgroup>`).join('')||'<option>Full request window · not yet measured</option>';
}
function renderPreparation(detail=state.detail) {
  const snapshots=new Map(),observations=detail?.report?.observations?.targets || {};
  const remember=item=>{const key=JSON.stringify([item.target || '',item.scenario || item.step || '']);snapshots.delete(key);snapshots.set(key,{...item,key});};
  for(const [target,observation] of Object.entries(observations))for(const [scenario,item] of Object.entries(observation.preparations || {}))remember({target,scenario,...item});
  for(const event of [...(detail?.events || []),detail?.progress || {}])if(event.preparation)remember({target:event.target,scenario:event.scenario,...event.preparation});
  for(const item of detail?.live_preparations || [])remember(item);
  for(const [target,observation] of Object.entries(observations))if(observation.preparation)remember({target,...observation.preparation});
  if(!snapshots.size)return '';
  const current=new Map();for(const item of snapshots.values())current.set(item.target || '',item);
  const optional=item=>['runtime_metrics_discovery','performance_evidence_probe'].includes(item.scenario);
  const earlier=[...snapshots.values()].filter(item=>current.get(item.target || '')!==item&&!optional(item)&&(item.builtin||item.estimated||['failed','unavailable','cancelled'].includes(item.status)));
  const clocks=state.preparationClocks?.id===detail.id?state.preparationClocks.values:new Map();state.preparationClocks={id:detail.id,values:clocks};
  return [...current.values(),...earlier].map(item=>{
    const running=item.status==='running'&&activeState(detail),key=JSON.stringify([item.target,item.step,item.started_elapsed_seconds]),sample=Number(item.elapsed_seconds || 0),previous=clocks.get(key);
    if(!previous||sample!==previous.sample)clocks.set(key,{sample,at:Date.now()});
    const clock=clocks.get(key),seconds=sample+(running?(Date.now()-clock.at)/1000:0),status=running?'running':item.status==='running'?'cancelled':item.status;
    const label={running:'Preparing test traffic',completed:item.builtin?'Preparation complete · built-in tokenizer':item.estimated?'Preparation complete · estimated input size':'Preparation complete',failed:'Preparation failed',unavailable:'Preparation unavailable',cancelled:'Preparation stopped'}[status] || 'Preparation';
    const optionalEvidence=optional(item);
    const scope=item.scenario==='context_preparation'?catalogTitle('context'):item.scenario?.startsWith('load_preparation_')?catalogTitle('load'):'';
    const execution=optionalEvidence?(running?'Checking optional measurement support before continuing test traffic.':status==='completed'?'Supported measurements are available.':'Unsupported measurements stay unknown; this does not establish a workload failure.'):running?'Test traffic for this preparation has not started.':['failed','unavailable','cancelled'].includes(status)?'The affected test traffic did not run.':'';
    if(!activeState(detail)&&status==='completed'&&!item.estimated)return `<details class="preparation-status"><summary>${escape(label)}</summary><p>${escape(item.reason || human(item.step || 'Workload ready'))} · ${elapsed(seconds)}</p></details>`;
    return `<section class="run-preparation" data-live-key="preparation:${escape(item.key)}" data-status="${escape(status)}" role="status"><div><span class="eyebrow">${current.get(item.target || '')===item?'Preparation':'Earlier preparation'}${item.target?` · ${escape(item.target)}`:''}${scope?` · ${escape(scope)}`:''}</span><h2>${escape(label)}</h2><p>${escape(human(item.step || 'Preparing workload'))}</p>${item.reason?`<p>${escape(item.reason)}</p>`:''}${execution?`<p class="hint">${execution}</p>`:''}</div><span class="preparation-elapsed" aria-label="Preparation elapsed">${elapsed(seconds)}</span></section>`;
  }).join('');
}
function renderReportToolbar() {
  const {data,target,window}=selectedPerformance();
  return `${renderPreparation()}<div class="report-toolbar" data-live-key="report-toolbar"><div class="result-tabs" role="tablist" aria-label="Run report">${[['results','Results'],['performance','Performance']].map(([tab,label])=>`<button role="tab" id="tab-${tab}" aria-selected="${reportView.tab===tab}" tabindex="${reportView.tab===tab?0:-1}" aria-controls="report-${tab}" class="${reportView.tab===tab?'selected':''}" data-report-tab="${tab}">${label}</button>`).join('')}</div><div class="report-measurement" ${reportView.tab==='performance'?'':'hidden'}>${(data?.available_targets?.length||0)>1?`<label>Target <select id="performance-target">${data.available_targets.map(item=>`<option value="${escape(item.target)}" ${target?.target===item.target?'selected':''}>${escape(item.identity?.model||item.target)} · ${escape(item.target)}</option>`).join('')}</select></label>`:''}<label>Measurement <select id="performance-window" ${!target||reportView.pending?'disabled':''}>${performanceWindowOptions(target,window)}</select></label></div></div>`;
}
// Presentation uses the selected backend summary. Only scales and drawing are reduced here.
const performanceCharts=new Map(),performancePlotViews=new Map(),performanceChartWidths=new Map();
let performanceResizeObserver=null;
function observePerformanceCharts() {
  if(typeof ResizeObserver==='undefined')return;
  if(!performanceResizeObserver)performanceResizeObserver=new ResizeObserver(entries=>{
    if(!state.reportRouteActive)return;
    let changed=false;
    for(const entry of entries){const id=entry.target.dataset.performanceChart,width=Math.round(entry.contentRect.width);if(width>=240&&Math.abs((performanceChartWidths.get(id)||900)-width)>1){performanceChartWidths.set(id,width);changed=true;}}
    if(changed&&reportView.tab==='performance'&&state.detail)renderDetail();
  });
  performanceResizeObserver.disconnect();
  for(const chart of $$('[data-performance-chart]'))performanceResizeObserver.observe(chart);
}
function performanceNumber(value) {
  if(!finitePerformance(value))return '—';
  const magnitude=Math.abs(value);
  if(magnitude>0&&magnitude<.000001)return value.toExponential(1);
  const digits=magnitude>0&&magnitude<1?Math.max(2,1-Math.floor(Math.log10(magnitude))):2;
  return number(value,digits);
}
let performancePlotScope=null;
const finitePerformance=value=>typeof value==='number'&&Number.isFinite(value);
function minMaxDecimate(points,buckets=160) {
  if(points.length<=2*buckets+2)return points;
  const first=points[0],last=points.at(-1),span=last.x-first.x||1,groups=new Map();
  points.forEach((point,index)=>{
    const bucket=Math.min(buckets-1,Math.floor((point.x-first.x)/span*buckets));
    const group=groups.get(bucket)||{min:index,max:index};
    if(point.y<points[group.min].y)group.min=index;
    if(point.y>points[group.max].y)group.max=index;
    groups.set(bucket,group);
  });
  const indices=new Set([0,points.length-1]);
  for(const group of groups.values()){indices.add(group.min);indices.add(group.max);}
  return [...indices].sort((a,b)=>a-b).map(index=>points[index]);
}
// Presentation only: retain recorded fields, with bounded previews for large ledgers.
function performanceEvidenceLabel(key) {
  return ({first_output_ms:'First useful output limit',first_token_ms:'Client TTFT limit',elapsed_ms:'Elapsed time',
    local_drops:'Local drops',dropped_local:'Local drops',late_drops:'Late',dropped_late:'Late',
    not_offered:'Not offered',timing_violations:'Timing violations',missing_timing:'Missing timing'})[key]||human(key);
}
function performanceStructuredValue(metric) {
  const rows=[],limit=40;
  let truncated=false;
  const text=(value,key='')=>{
    if(key==='acceptance_ratio'&&finitePerformance(value))return performanceNumber(value*100)+'%';
    let result=value==null?'Unknown':typeof value==='boolean'?(value?'Yes':'No'):finitePerformance(value)?performanceNumber(value)+(key.endsWith('_ms')?' ms':''):(['first_output_ms','first_token_ms'].includes(value)?performanceEvidenceLabel(value):/^[a-z]+(?:_[a-z0-9]+)+$/.test(String(value))?human(String(value)):String(value));
    if(result.length>240){result=result.slice(0,240)+'…';truncated=true;}
    return result;
  };
  const visit=(value,path='',key='',depth=0)=>{
    if(rows.length>=limit){truncated=true;return;}
    if(depth>6){rows.push([path,'Nested values (preview depth limit)']);truncated=true;return;}
    if(Array.isArray(value)){
      if(value.every(item=>item==null||typeof item!=='object')){
        rows.push([path||'Values',value.length?value.slice(0,12).map(item=>text(item)).join(', '):'None']);
        if(value.length>12)truncated=true;
      }else for(let i=0;i<Math.min(value.length,limit+1);i++)visit(value[i],path?`${path} · ${i+1}`:`Measurement ${i+1}`,'',depth+1);
      if(value.length>limit+1)truncated=true;
    }else if(value&&typeof value==='object'){
      const entries=Object.entries(value);
      if(!entries.length)rows.push([path||'Values','None']);
      for(const [field,item] of entries.slice(0,limit+1))visit(item,path?`${path} · ${performanceEvidenceLabel(field)}`:performanceEvidenceLabel(field),field,depth+1);
      if(entries.length>limit+1)truncated=true;
    }else rows.push([path||'Value',text(value,key)]);
  };
  visit(metric.value);
  let summary=rows.slice(0,3).map(([key,value])=>`${key}: ${value}`).join(' · ');
  if(metric.id==='drops'){
    const values=Array.isArray(metric.value)?metric.value:[metric.value];
    summary=values.slice(0,3).map((item,index)=>{
      const counts=[['Local drops',item.local_drops??item.dropped_local],['Late',item.late_drops??item.dropped_late],['Not offered',item.not_offered]];
      return (values.length>1?`Measurement ${index+1}: `:'')+counts.map(([label,value])=>`${label} ${text(value)}`).join(' · ');
    }).join('; ')+(values.length>3?` · ${values.length} entries`:'');
  }
  return `<span class="performance-structured-summary">${escape(summary)}</span><details class="performance-evidence"><summary>Underlying values</summary><table><thead><tr><th>Recorded field</th><th>Value</th></tr></thead><tbody>${rows.map(([key,value])=>`<tr><th scope="row">${escape(key.slice(0,160))}</th><td>${escape(value)}</td></tr>`).join('')}</tbody></table>${truncated?'<p>Preview limited to 40 fields, 12 items per list and 240 characters per value. Complete values are included in the JSON export.</p>':''}</details>`;
}
function performanceDisplay(metric,{seconds=false}={}) {
  if(metric?.value==null)return '—';
  let value=metric.value,unit=metric.unit||'';
  if(finitePerformance(value)){
    if(seconds&&unit==='ms'){value/=1000;unit='s';}
    if(['ratio','fraction'].includes(unit)){value*=100;unit='%';}
    return `${performanceNumber(value)}${unit?' <small class="performance-unit">'+escape(unit)+'</small>':''}`;
  }
  if(typeof value==='object')return performanceStructuredValue(metric);
  return escape(value);
}
function performanceInfo(metric,key) {
  const id=`performance-info-${key}`;
  return `<span class="performance-info-wrap"><button type="button" class="performance-info" data-performance-info="${escape(key)}" aria-label="Definition of ${escape(metric.label)}" aria-describedby="${id}" aria-expanded="false">i</button><span id="${id}" class="performance-info-popover" role="tooltip"><b>${escape(metric.label)}</b><span>${escape(metric.source||'Origin not recorded.')}</span><span>Unit: ${escape(metric.unit||'not specified')} · ${escape(metric.display_note||`${number(metric.sample_count)} / ${number(metric.eligible_count)} eligible ${metric.sample_unit||'samples'}`)}.</span><span>${escape(human(metric.state||'not_measured'))}${metric.reason?': '+escape(metric.reason):''}</span>${metric.action?`<span>How to collect: ${escape(metric.action)}</span>`:''}<span>Window: ${escape(metric.window||'selected window')} · Target: ${escape(metric.target||'selected target')}</span></span></span>`;
}
function performanceCard(metric,key,{seconds=false,note='',emptyLabel=''}={}) {
  if(!metric)return '';
  const available=metric.value!=null,subtext=!available?[metric.reason||'No eligible observations in this scope.',metric.display_note].filter(Boolean).join(' '):note||metric.display_note||(metric.p95!=null?`Median · P95 ${performanceNumber(metric.p95*(seconds&&metric.unit==='ms'?.001:1))} ${seconds&&metric.unit==='ms'?'s':metric.unit||''}`:available?`${number(metric.sample_count)} / ${number(metric.eligible_count)} eligible ${metric.sample_unit||'observations'}`:metric.reason||'No eligible observations in this scope.');
  return `<article class="performance-kpi ${available?'':'unavailable'}" data-live-key="kpi-${escape(key)}"><div class="performance-card-label"><h3>${escape(metric.label)}</h3>${performanceInfo(metric,key)}</div><strong>${emptyLabel&&!available?escape(emptyLabel):performanceDisplay(metric,{seconds})}</strong>${metric.state==='estimated'?'<span class="performance-estimate">ESTIMATE</span>':''}<span class="metric-state ${escape(metric.state)}">${escape(human(metric.state))}</span><p>${escape(subtext)}</p>${!available&&metric.action?`<p class="performance-collect">${escape(metric.action)}</p>`:''}${metric.partial?'<p class="performance-coverage">Partial usage / measurement coverage; known observations only.</p>':''}</article>`;
}
function performanceMetricTable(rows,title='') {
  return `${title?`<h3 class="performance-table-heading">${escape(title)}</h3>`:''}<div class="metric-table performance-metric-table"><table><thead><tr><th>Metric</th><th>Value</th><th>Definition / availability</th></tr></thead><tbody>${(rows||[]).map(metric=>`<tr class="${metric.value==null?'missing':''}"><th scope="row">${escape(metric.label)}</th><td class="performance-metric-value">${performanceDisplay(metric)}${metric.state==='estimated'?'<small class="performance-estimate">ESTIMATE</small>':''}<small class="availability">${escape(human(metric.state))}</small></td><td>${escape(metric.reason||metric.source)}${metric.reason&&metric.source?`<small>${escape(metric.source)}</small>`:''}${metric.action?`<small>How to collect: ${escape(metric.action)}</small>`:''}<small>${escape(metric.display_note||`${number(metric.sample_count)} / ${number(metric.eligible_count)} eligible ${metric.sample_unit||'samples'}`)}${metric.partial?' · partial coverage':''}</small></td></tr>`).join('')}</tbody></table></div>`;
}
function performanceDistributions(summary,keys,title) {
  const rows=keys.map(key=>summary.distributions?.[key]).filter(Boolean);
  return `<details class="performance-distributions"><summary>${escape(title)}</summary><p class="hint">${escape(selectedPerformance().data?.percentile_convention||'Percentile convention was not recorded.')}</p><div class="performance-distribution-scroll"><table><thead><tr><th>Metric / unit</th><th>n</th>${['Mean','Min','Median','P90','P95','P99','Max'].map(label=>`<th>${label}</th>`).join('')}</tr></thead><tbody>${rows.map(metric=>{const value=metric.value;return `<tr><th scope="row">${escape(metric.label)}<small>${escape(metric.unit)} · ${escape(metric.source)}</small></th>${value?`<td>${number(value.n)}</td>${['mean','min','median','p90','p95','p99','max'].map(key=>`<td>${performanceNumber(value[key])}</td>`).join('')}`:`<td colspan="8">— · ${escape(human(metric.state))}. ${escape(metric.reason)} ${escape(metric.action)}</td>`}</tr>`;}).join('')||'<tr><td colspan="9">No distributions were recorded for this scope.</td></tr>'}</tbody></table></div></details>`;
}
function performanceSection(id,label,note,body) {
  const index=reportSections.findIndex(section=>section[0]===id)+1;
  return `<section id="performance-${id}" class="performance-section"><div class="performance-section-head"><h2><span class="section-number">${String(index).padStart(2,'0')}</span> ${escape(label)}</h2><p>${escape(note)}</p></div>${body}</section>`;
}
function performancePlotView(id) {
  if(!performancePlotViews.has(id))performancePlotViews.set(id,{hidden:new Set(),index:null,zoom:'all'});
  return performancePlotViews.get(id);
}
function performanceIntegerTicks(min,max,divisions=4) {
  if(min===max)return [min];
  const span=max-min,step=Math.max(1,Math.round((span+(min>0?1:0))/divisions)),ticks=[min];
  for(let value=min+step;value<max&&ticks.length<divisions;value+=step)ticks.push(value);
  if(ticks.at(-1)!==max)ticks.push(max);
  return ticks;
}
function performanceNiceTicks(min,max,divisions=4,integer=false) {
  const raw=(max-min||Math.abs(max)||1)/divisions,power=10**Math.floor(Math.log10(raw));
  const choices=integer&&raw<1?[1]:[1,2,2.5,5,10].map(value=>value*power).filter(value=>!integer||Number.isInteger(value));
  const step=choices.reduce((best,value)=>Math.abs(value-raw)<Math.abs(best-raw)?value:best);
  const first=Math.ceil(min/step-1e-10),last=Math.max(first+Number(max===min),Math.ceil(max/step-1e-10));
  const ticks=Array.from({length:last-first+1},(_,index)=>Number(((first+index)*step).toPrecision(12)));
  return {min,max:ticks.at(-1),step,ticks};
}
function performanceAxes(xmin,xmax,ymax,{xlabel='Request number · dispatch order',ylabel='seconds',width=900,integerX=false,integerY=false}={}) {
  const divisions=width<500?2:4,xaxis=integerX?{min:xmin,max:xmax,ticks:performanceIntegerTicks(xmin,xmax,divisions)}:performanceNiceTicks(xmin,xmax,divisions),yaxis=performanceNiceTicks(0,ymax,4,integerY);
  const left=58,right=width-20,bottom=238,sx=x=>xaxis.min===xaxis.max?(left+right)/2:left+(x-xaxis.min)/(xaxis.max-xaxis.min)*(right-left),sy=y=>bottom-y/yaxis.max*204;
  const ticks=xaxis.ticks,yticks=yaxis.ticks;
  const grid=yticks.map(y=>`<line x1="${left}" x2="${right}" y1="${sy(y)}" y2="${sy(y)}" class="performance-gridline"/><text data-axis-tick="y" x="${left-9}" y="${sy(y)+4}" text-anchor="end">${integerY?number(y):performanceNumber(y)}</text>`).join('');
  return {sx,sy,width,left,right,bottom,xmin:xaxis.min,xmax:xaxis.max,ymax:yaxis.max,xticks:ticks,yticks,html:`${grid}<line x1="${left}" x2="${right}" y1="238" y2="238" class="performance-axis"/>${ticks.map(x=>`<text data-axis-tick="x" x="${sx(x)}" y="257" text-anchor="middle">${integerX?number(x):performanceNumber(x)}</text>`).join('')}<text x="${(left+right)/2}" y="282" text-anchor="middle">${escape(xlabel)}</text><text x="${left}" y="17">${escape(ylabel)}</text>`};
}
function renderPerformanceLineChart(id,title,caption,rows,series,{step=false,stages=[],bounds=null,controls='',xlabel='Request number · dispatch order',ylabel='seconds'}={}) {
  const view=performancePlotView(id),prepared=series.map(item=>({...item,points:rows.map((row,index)=>({x:row.x??row.number,y:item.value?item.value(row):row.values?.[item.key],row,index})).filter(point=>finitePerformance(point.x)&&finitePerformance(point.y)).map(point=>({...point,y:point.y*(item.scale??1)}))}));
  if(!prepared.some(item=>item.points.length))return `<article class="performance-plot performance-chart-empty"><h3>${escape(title)}</h3><p class="hint">${escape(id==='cached'?'No cached prompt usage was reported in this window.':id==='size'?'No input or answer token counts were reported in this window.':'No eligible timestamped observations were recorded in this window.')}</p></article>`;
  let xmin=bounds?.[0]??Infinity,xmax=bounds?.[1]??-Infinity;
  if(!bounds)for(const item of prepared)for(const point of item.points){xmin=Math.min(xmin,point.x);xmax=Math.max(xmax,point.x);}
  const visiblePoints=points=>{
    if(!bounds)return points;
    const left=points.filter(point=>point.x<xmin).at(-1),selected=points.filter(point=>point.x>=xmin&&point.x<=xmax);
    if(left)selected.unshift({...left,x:xmin});
    if(step&&selected.length&&selected.at(-1).x<xmax)selected.push({...selected.at(-1),x:xmax});
    return selected;
  };
  let ymax=1;for(const item of prepared)for(const point of visiblePoints(item.points))ymax=Math.max(ymax,point.y);
  const axes=performanceAxes(xmin,xmax,ymax,{xlabel,ylabel,width:performanceChartWidths.get(id)||900,integerX:!step,integerY:step||['size','cached'].includes(id)});
  const visibleRows=bounds?rows.filter(row=>(row.x??row.number)>=xmin&&(row.x??row.number)<=xmax):rows;
  if(bounds&&step){
    const carry=rows.filter(row=>(row.x??row.number)<xmin).at(-1);
    if(carry&&(!visibleRows.length||(visibleRows[0].x??visibleRows[0].number)>xmin))visibleRows.unshift({...carry,x:xmin});
    if(visibleRows.length&&(visibleRows.at(-1).x??visibleRows.at(-1).number)<xmax)visibleRows.push({...visibleRows.at(-1),x:xmax});
  }
  const chart={id,rows:visibleRows,series:prepared.map(({points,...item})=>step?{...item,points}:item),xmin:axes.xmin,xmax:axes.xmax,ymax:axes.ymax,sx:axes.sx,sy:axes.sy,width:axes.width,left:axes.left,right:axes.right,kind:step?'step':'requests'};
  performanceCharts.set(id,chart);
  const paths=prepared.filter(item=>item.points.length).map((item,index)=>{
    const drawn=minMaxDecimate(visiblePoints(item.points),Math.min(160,Math.floor((axes.right-axes.left)/2))),path=drawn.map((point,i)=>`${i?(step?'H'+axes.sx(point.x).toFixed(2)+'V':'L'):'M'}${step&&i?'':axes.sx(point.x).toFixed(2)+','}${axes.sy(point.y).toFixed(2)}`).join(' ');
    const transitions=step&&item.key==='intended'?visiblePoints(item.points).filter((point,i,points)=>!i||point.y!==points[i-1].y).slice(0,40):[];
    return `<path data-live-key="series-${escape(item.key)}" data-series="${escape(item.key)}" d="${path}" class="performance-series ${item.dashed?'dashed':''}" style="--series-color:${item.color}" fill="none" ${view.hidden.has(item.key)?'hidden':''}/>${transitions.map(point=>`<circle class="performance-intended-transition" cx="${axes.sx(point.x)}" cy="${axes.sy(point.y)}" r="2.7" style="fill:${item.color}" ${view.hidden.has(item.key)?'hidden':''}/>`).join('')}${drawn.length===1&&!transitions.length?`<circle cx="${axes.sx(drawn[0].x)}" cy="${axes.sy(drawn[0].y)}" r="3" style="fill:${item.color}" ${view.hidden.has(item.key)?'hidden':''}/>`:''}`;
  }).join('');
  const visibleStages=stages.filter(stage=>stage.end_elapsed_s>xmin&&stage.elapsed_s<xmax);
  const bands=visibleStages.slice(0,40).map((stage,index)=>{const left=Math.max(xmin,stage.elapsed_s),right=Math.min(xmax,stage.end_elapsed_s);return `<g class="performance-stage-band"><rect x="${axes.sx(left)}" y="27" width="${Math.max(0,axes.sx(right)-axes.sx(left))}" height="211" opacity="${index%2?.055:.025}"/><text x="${axes.sx(left)+5}" y="42">${escape(stage.label||'Stage')}</text></g>`;}).join('');
  const description=`${title}. ${rows.length} ${step?'recorded events':'request observations'}, x ${number(xmin,2)} to ${number(xmax,2)}, y 0 to ${performanceNumber(ymax)} ${ylabel}. Series: ${prepared.filter(item=>item.points.length).map(item=>item.label).join(', ')}. Arrow keys inspect every retained observation; drawing uses min/max reduction.`;
  return `<article class="performance-plot" data-live-key="plot-${id}"><div class="performance-chart-toolbar"><div><h3>${escape(title)}</h3><p class="performance-caption">${escape(caption)}</p></div>${controls}</div><div class="performance-chart-legend">${prepared.filter(item=>item.points.length).map(item=>`<button type="button" data-live-key="legend-${item.key}" data-performance-series="${escape(item.key)}" data-chart="${id}" aria-pressed="${!view.hidden.has(item.key)}" style="--series-color:${item.color}"><i aria-hidden="true"></i>${escape(item.label)}</button>`).join('')}</div><svg class="performance-chart" data-performance-chart="${id}" viewBox="0 0 ${axes.width} 292" role="img" tabindex="0" aria-label="${escape(description)}" aria-describedby="performance-inspection-${id}"><desc>${escape(description)}</desc><defs><clipPath id="performance-clip-${id}"><rect x="${axes.left}" y="27" width="${axes.right-axes.left}" height="211"/></clipPath></defs>${axes.html}<g clip-path="url(#performance-clip-${id})">${bands}${paths}<line class="performance-crosshair" x1="${axes.left}" x2="${axes.left}" y1="27" y2="238" hidden/></g></svg><p class="performance-caption">Drawn from ${number(rows.length)} ${step?'recorded events':'requests'}; every eligible request is included in metrics. Min/max per drawing bucket preserves extrema; lines connect observations.${step?' Steps hold each recorded value until its next transition.':id==='timing'?' Timing phases are not all additive.':id==='size'?' Input and answer counts are reported separately; they describe request size, not generation rate.':' Cached prompt tokens are part of input usage; missing cache usage is unavailable, not zero.'}${visibleStages.length>40?' Showing the first 40 of '+number(visibleStages.length)+' stage bands; complete schedule evidence is retained in JSON.':''}</p><div id="performance-inspection-${id}" class="performance-inspection" role="status" aria-live="polite">Focus the chart and use ← / → to inspect ${step?'events':'every request'}, or point at the chart.</div></article>`;
}
function renderPerformanceCompletions(summary) {
  const data=summary.completion_buckets,buckets=data?.buckets||[];
  if(!buckets.length||!buckets.some(bucket=>bucket.count>0))return `<article class="performance-plot performance-chart-empty"><h3>Completed requests over time</h3><p class="hint">${buckets.length?'No completed requests in the selected window; recorded bucket counts are zero.':'No completion timestamps were recorded in this window.'}</p></article>`;
  const xmax=buckets.at(-1).end_s,ymax=Math.max(1,...buckets.map(bucket=>bucket.count)),axes=performanceAxes(0,xmax,ymax,{xlabel:'Elapsed seconds',ylabel:'completed requests',width:performanceChartWidths.get('completions')||900,integerY:true});
  const rows=buckets.map((bucket,index)=>({...bucket,x:(bucket.start_s+bucket.end_s)/2,number:index+1,values:{count:bucket.count}}));
  performanceCharts.set('completions',{id:'completions',rows,series:[{key:'count',label:'Completed requests',unit:'requests'}],xmin:axes.xmin,xmax:axes.xmax,ymax:axes.ymax,sx:axes.sx,sy:axes.sy,width:axes.width,left:axes.left,right:axes.right,kind:'buckets'});
  const last=buckets.at(-1),partial=data.final_partial_bucket?`The final bucket uses its actual duration (${performanceNumber(last.duration_s)} s).`:'Every bucket covers its full duration.';
  return `<article class="performance-plot" data-live-key="plot-completions"><h3>Completed requests over time</h3><p class="performance-caption">${performanceNumber(data.width_s)}-second buckets; ${escape(partial.charAt(0).toLowerCase()+partial.slice(1))} Completion counts include failed terminal requests.</p><svg class="performance-chart" data-performance-chart="completions" viewBox="0 0 ${axes.width} 292" role="img" tabindex="0" aria-label="Completed request counts in ${buckets.length} elapsed-time buckets, including terminal failures. ${escape(partial)}" aria-describedby="performance-inspection-completions"><desc>${escape(data.rule)} ${escape(data.boundary_rule)}</desc>${axes.html}${buckets.map(bucket=>{const span=axes.sx(bucket.end_s)-axes.sx(bucket.start_s),gap=Math.min(6,span*.2);return `<rect class="performance-completion-bar" x="${axes.sx(bucket.start_s)+gap/2}" y="${axes.sy(bucket.count)}" width="${Math.max(.1,span-gap)}" height="${238-axes.sy(bucket.count)}"><title>${performanceNumber(bucket.start_s)}–${performanceNumber(bucket.end_s)} s · ${number(bucket.count)} completed · ${number(bucket.valid_completions)} valid</title></rect>`;}).join('')}<line class="performance-crosshair" x1="${axes.left}" x2="${axes.left}" y1="27" y2="238" hidden/></svg><details class="performance-bucket-details"><summary>Bucket definitions${data.final_partial_bucket?' · partial final bucket':''}</summary><p class="performance-caption">${escape(data.rule)} ${escape(data.boundary_rule)} ${escape(data.partial_bucket_disclosure||'')} ${escape(partial)}</p></details><div id="performance-inspection-completions" class="performance-inspection" role="status" aria-live="polite">Focus or point at a bucket to inspect its count and actual duration.</div></article>`;
}
function performanceStepValue(series,x) {
  const points=series.points||[];let low=0,high=points.length;
  while(low<high){const middle=Math.floor((low+high)/2);if(points[middle].x<=x)low=middle+1;else high=middle;}
  return low?points[low-1].y:null;
}
function hidePerformanceTooltip() {const tip=$('#performance-chart-tooltip');if(tip)tip.hidden=true;}
function showPerformanceTooltip(id,x,y) {
  const tip=$('#performance-chart-tooltip'),inspection=$(`#performance-inspection-${id}`);if(!tip||!inspection)return;
  tip.textContent=inspection.textContent;tip.hidden=false;
  const box=tip.getBoundingClientRect();tip.style.left=`${Math.max(12,Math.min(x+14,window.innerWidth-box.width-12))}px`;tip.style.top=`${Math.max(12,Math.min(y+18,window.innerHeight-box.height-12))}px`;
}
function inspectPerformanceChart(id,index) {
  const chart=performanceCharts.get(id);if(!chart?.rows.length)return;
  const view=performancePlotView(id);view.index=Math.max(0,Math.min(chart.rows.length-1,index));
  const row=chart.rows[view.index],svg=$(`[data-performance-chart="${id}"]`),crosshair=svg?.querySelector('.performance-crosshair'),inspection=$(`#performance-inspection-${id}`);
  const x=Math.max(chart.xmin,Math.min(chart.xmax,row.x??row.number));
  if(crosshair){crosshair.removeAttribute('hidden');crosshair.setAttribute('x1',chart.sx(x));crosshair.setAttribute('x2',chart.sx(x));}
  let text;
  if(chart.kind==='buckets')text=`${number(row.start_s,2)}–${number(row.end_s,2)} s · ${number(row.count)} completed requests · ${number(row.valid_completions)} valid · ${performanceNumber(row.duration_s)} s actual duration${row.partial?' · partial bucket':''}`;
  else text=`${chart.kind==='requests'?`Request #${String(row.number).padStart(4,'0')} · ${row.request_id}`:`Elapsed ${performanceNumber(row.x)} s`}: ${chart.series.map(series=>{const value=chart.kind==='step'?performanceStepValue(series,row.x):series.value?series.value(row):row.values?.[series.key];return `${series.label}: ${finitePerformance(value)?performanceNumber(value*(series.scale??1))+' '+(series.unit||''):'Unavailable'}`;}).join(' · ')}`;
  if(inspection)inspection.textContent=text;
}
function handlePerformanceKeydown(event) {
  if(event.key==='Escape'){hidePerformanceTooltip();for(const button of $$('[data-performance-info][aria-expanded="true"]')){button.setAttribute('aria-expanded','false');button.closest('.performance-info-wrap')?.classList.remove('info-open');}return false;}
  const svg=event.target.closest?.('[data-performance-chart]'),chart=performanceCharts.get(svg?.dataset?.performanceChart);
  if(!chart||!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return false;
  event.preventDefault();const current=performancePlotView(chart.id).index??0;
  inspectPerformanceChart(chart.id,event.key==='Home'?0:event.key==='End'?chart.rows.length-1:current+(event.key==='ArrowRight'?1:-1));return true;
}
function handlePerformancePointer(event) {
  const svg=event.target.closest?.('[data-performance-chart]'),chart=performanceCharts.get(svg?.dataset?.performanceChart);if(!chart)return false;
  const rect=svg.getBoundingClientRect(),x=chart.xmin+Math.max(0,Math.min(1,((event.clientX-rect.left)/rect.width*chart.width-chart.left)/(chart.right-chart.left)))*(chart.xmax-chart.xmin);
  let low=0,high=chart.rows.length-1;
  while(low<high){const middle=Math.floor((low+high)/2);if((chart.rows[middle].x??chart.rows[middle].number)<x)low=middle+1;else high=middle;}
  if(low>0&&Math.abs((chart.rows[low-1].x??chart.rows[low-1].number)-x)<Math.abs((chart.rows[low].x??chart.rows[low].number)-x))low--;
  inspectPerformanceChart(chart.id,low);showPerformanceTooltip(chart.id,event.clientX,event.clientY);return true;
}
function handlePerformanceClick(event) {
  const info=event.target.closest?.('[data-performance-info]');if(info){const open=info.getAttribute('aria-expanded')!=='true';info.setAttribute('aria-expanded',String(open));info.closest('.performance-info-wrap')?.classList.toggle('info-open',open);return true;}
  const legend=event.target.closest?.('[data-performance-series]');if(!legend)return false;
  const view=performancePlotView(legend.dataset.chart),key=legend.dataset.performanceSeries;
  if(view.hidden.has(key))view.hidden.delete(key);else view.hidden.add(key);
  renderDetail();return true;
}
function handlePerformanceChange(event) {
  if(event.target.id!=='performance-concurrency-stage')return false;
  const view=performancePlotView('concurrency');view.zoom=event.target.value;view.index=null;hidePerformanceTooltip();renderDetail();return true;
}
function renderPerformance() {
  const {data,target,window,summary}=selectedPerformance(),scope=JSON.stringify([state.detail?.id,target?.target,window?.id]);
  if(performancePlotScope!==scope){performancePlotScope=scope;performancePlotViews.clear();}
  performanceCharts.clear();
  const terminal=!activeState(state.detail)?`<div class="notice report-terminal-notice">${escape(statusLabels[runOutcome(state.detail)==='stopped'?'stopped':state.detail.state]||human(state.detail.state))}. ${state.detail.report?.abort_reason?'Partial evidence: '+escape(state.detail.report.abort_reason):'Review measurement coverage and check verdicts before interpreting these results.'} <a href="${linkRun(state.detail.id)}/results" data-report-tab="results">View test results</a></div>`:'';
  let body='';
  if(summary){
    const metrics=summary.metrics||{},rows=(summary.request_row_indices||[]).map(index=>target.request_series?.[index]).filter(Boolean),timingKeys=Object.keys(summary.distributions||{}).filter(key=>!key.endsWith('_length'));
    const seconds=new Set(['response_latency','first_useful_output','answer_streaming','completion_tail','client_ttft','first_reasoning','server_ttft']);
    const kpis=(summary.kpis||[]).map(key=>performanceCard(metrics[key],key,{seconds:seconds.has(key),note:key==='output_throughput'?(metrics.completion_throughput?.semantically_identical_to_answer?'Answer and completion rates match for this usage.':'Answer tokens / matching window; completion may also include reasoning.'):key==='generation_pace'?'Median · answer streaming only':''})).join('');
    body=`<section id="performance-overview" class="performance-section performance-overview"><p class="performance-window-description"><span class="performance-target-label">${escape(target.identity?.model||target.target)}</span> · ${escape(window?.description||measurementLabel(window))}${!window?.description?` · ${elapsed(window?.duration_s)}${window?.includes_startup?' · includes first-request startup':''}${window?.includes_drain?' · includes drain':''}`:''}</p>${target.scope_note?`<p class="hint performance-scope-note">${escape(target.scope_note)}</p>`:''}<div class="performance-kpis">${kpis}</div>${performanceDistributions(summary,timingKeys,'All timing percentiles & definitions')}</section>`;
    const timingSeries=[['response_latency_ms','Response latency','var(--text-2)'],['before_first_useful_output_ms','Before first useful output','var(--unknown)'],['answer_streaming_ms','Answer streaming time','var(--accent)'],['completion_tail_ms','Completion time (tail)','var(--pass)'],['client_ttft_ms','Client TTFT','var(--chart-ttft)']].map(([key,label,color])=>({key,label,color,scale:.001,unit:'s'}));
    body+=performanceSection('timing','Request timing','Each observation is a request, ordered by dispatch.',renderPerformanceLineChart('timing','Timing across requests','X: request number · Y: seconds. First useful output and client TTFT have distinct definitions.',rows,timingSeries)+`<div class="performance-support-cards">${performanceCard(metrics.answer_streaming,'answer-streaming',{seconds:true,note:metrics.answer_streaming?.value!=null?`Median · Max ${performanceNumber(summary.distributions?.answer_streaming?.value?.max*.001)} s`:''})}${performanceCard(metrics.longest_stream_pause,'longest-pause',{note:'Maximum gap between answer reads; not token ITL.'})}</div>`+performanceDistributions(summary,['answer_streaming','completion_tail','answer_read_gap','reasoning_read_gap'],'Timing distributions'));
    const capacity=(summary.sections?.throughput_capacity||[]).filter(metric=>metric.id!=='completion_throughput'||!metric.semantically_identical_to_answer);
    body+=performanceSection('throughput','Throughput & capacity','Input, completion and normalized rates retain their populations.',`<div class="performance-throughput-layout">${renderPerformanceCompletions(summary)}<div class="performance-capacity-cards">${capacity.map(metric=>performanceCard(metric,`capacity-${metric.id}`)).join('')}</div></div>`);
    const stages=summary.schedule_intervals||[],view=performancePlotView('concurrency'),stage=stages.find((item,index)=>String(index)===view.zoom),bounds=stage?[stage.elapsed_s,stage.end_elapsed_s]:null;
    const actual=summary.actual_concurrency,actualRows=actual?.sample_count?actual.points.map(point=>({x:point.elapsed_s,values:{actual:point.in_flight}})):[],intended=(summary.intended_concurrency||[]).flatMap(item=>[{x:item.elapsed_s,values:{intended:item.intended_concurrency}},{x:item.end_elapsed_s,values:{intended:item.intended_concurrency}}]);
    const concurrencyRows=[...actualRows,...intended].sort((a,b)=>a.x-b.x),concurrencySeries=[{key:'actual',label:'Actual concurrency',color:'var(--accent)',unit:'requests'},{key:'intended',label:'Intended concurrency',color:'var(--text-2)',unit:'requests',dashed:true}];
    const controls=`<label class="performance-chart-view">Chart view <select id="performance-concurrency-stage"><option value="all" ${view.zoom==='all'?'selected':''}>Whole selected window</option>${stages.map((item,index)=>`<option value="${index}" ${String(index)===view.zoom?'selected':''}>${escape(item.label||'Stage')} · ${number(item.elapsed_s,2)}–${number(item.end_elapsed_s,2)} s</option>`).join('')}</select></label>`;
    const pauses=summary.client_pauses||[],pauseList=`<article class="performance-plot performance-pauses"><h3>When did the client pause?</h3><p class="performance-caption">Recorded scheduling delays${summary.client_pause_threshold_ms!=null?' above '+number(summary.client_pause_threshold_ms)+' ms':''}; these are client pauses, not server response times.</p>${pauses.length?`<div class="performance-pause-list"><div><span>Elapsed time</span><span>Scheduling delay</span></div>${pauses.slice(0,50).map(pause=>`<div><span>${performanceNumber(pause.elapsed_s)} s</span><strong>${number(pause.lag_ms,1)} ms</strong></div>`).join('')}</div>${pauses.length>50?`<p class="performance-caption">Showing the first 50 of ${number(pauses.length)} client pauses; complete evidence is retained in JSON.</p>`:''}`:`<p class="hint">${escape(summary.client_pause_threshold_ms==null?'Client scheduling-delay recording was not collected.':metrics.max_event_loop_delay?.reason||'No scheduling delays above the recording threshold were observed in this window.')}</p>`}</article>`;
    body+=performanceSection('delivery','Load delivery','Request lifetimes determine actual concurrency.',`<div class="performance-load-cards">${['peak_concurrency','average_concurrency','configured_ceiling'].map(key=>performanceCard(metrics[key],key,{emptyLabel:key==='configured_ceiling'?'Not set':''})).join('')}</div>`+renderPerformanceLineChart('concurrency','Actual and intended concurrency','X: elapsed seconds · Y: requests in flight. Actual lifetimes and recorded schedule transitions only.',concurrencyRows,concurrencySeries,{step:true,stages,bounds,controls,xlabel:'Elapsed seconds',ylabel:'requests in flight'})+(!intended.length?`<p class="hint">Intended concurrency: ${escape(summary.intended_reason||'No recorded schedule transitions; the configured ceiling is not intended history.')}</p>`:'')+pauseList+performanceMetricTable(summary.sections?.delivery));
    body+=performanceSection('outcomes','Outcomes & quality','Completion validity and answer correctness are distinct.',performanceMetricTable(summary.sections?.outcomes));
    const tokenSeries=[{key:'input_tokens',label:'Input tokens',color:'var(--text-2)',unit:'tokens'},{key:'answer_tokens',label:'Answer tokens',color:'var(--accent)',unit:'tokens'}];
    body+=performanceSection('tokens','Workload & tokens','Token counts and reported cache reuse; rates are shown above.',`<div class="performance-token-charts">${renderPerformanceLineChart('size','Input and answer size by request','All eligible request identities are retained; token usage comes from the endpoint.',rows,tokenSeries,{ylabel:'tokens'})}${renderPerformanceLineChart('cached','Cached prompt tokens by request','Reported cache reuse is a subset of input tokens, not a measured latency saving.',rows,[{key:'cached_prompt_tokens',label:'Cached prompt tokens',color:'var(--pass)',unit:'tokens'}],{ylabel:'cached prompt tokens'})}</div>`+performanceMetricTable(summary.sections?.token_accounting,'Token accounting · selected window')+performanceDistributions(summary,Object.keys(summary.distributions||{}).filter(key=>key.endsWith('_length')),'Token-length distributions'));
    body+=performanceSection('effects','Conversations & workload effects','Workload-specific evidence remains qualified.',performanceMetricTable(summary.sections?.conversations));
    body+=performanceSection('server','Server observations','Server-wide telemetry may include other traffic; runner memory is separate.',performanceMetricTable(summary.sections?.server));
    body+=performanceSection('baseline','Baseline comparison','Comparability and measurement coverage govern every comparison.',state.detail.report?.baseline?`<div data-comparison-scope="performance">${renderComparison(state.detail.report,{scope:'performance'})}</div>`:'<p class="hint">No baseline selected.</p>');
  }else body=`<section id="performance-overview" class="performance-section"><h2>Performance overview</h2><div class="empty-state"><h3>${reportView.pending?'Loading measurements…':'No performance measurements available'}</h3><p>Request evidence and check verdicts remain available in Test results.</p>${!reportView.pending?'<button class="button small" data-action="refresh-performance">Load measurements</button>':''}</div></section>${reportSections.slice(1).map(([id,label])=>performanceSection(id,label,'','<p class="hint">No measurements available for this selection.</p>')).join('')}`;
  if(typeof setTimeout==='function')setTimeout(observePerformanceCharts,0);
  return `<section id="report-performance" role="tabpanel" aria-labelledby="tab-performance" data-live-key="report-performance" ${reportView.tab==='performance'?'':'hidden'}>${terminal}${data?.coverage?.partial?notice(data.coverage.reason||'Live measurements cover only retained requests.','warning'):''}${reportView.error?notice(reportView.error,'warning'):''}${body}</section>`;
}

async function loadPerformance({force=false}={}) {
  if(!state.detail)return;
  if(reportView.pending){if(force)reportView.again=true;return;}
  const now=Date.now();if(!force&&(reportView.performance||reportView.lastFetch))return;
  const id=state.detail.id,version=state.routeVersion,sequence=++reportView.sequence,target=reportView.target,window=reportView.window;
  reportView.pending=true;reportView.lastFetch=now;
  const params=Object.entries({target,window}).filter(([,value])=>value!=null).map(([key,value])=>`${key}=${encodeURIComponent(value)}`).join('&');
  try{
    const value=await api(`${runPath(id)}/performance${params?'?'+params:''}`);
    if(state.detail?.id!==id||version!==state.routeVersion||reportView.sequence!==sequence)return;
    if(target!==reportView.target||window!==reportView.window){reportView.again=true;return;}
    reportView.performance=value;reportView.error='';
  }catch(error){if(state.detail?.id===id&&version===state.routeVersion&&reportView.sequence===sequence)reportView.error=error.message;}
  finally{if(state.detail?.id===id&&version===state.routeVersion&&reportView.sequence===sequence){reportView.pending=false;if(reportView.again){reportView.again=false;loadPerformance({force:true});}else if(state.reportRouteActive){renderDetail();finishPendingReportReveal();}}}
}

function resultActions(report,{completePass=false,savedBaseline=null}={}) {
  const failed=report.checks.filter(c=>c.id!=='baseline'&&['fail','blocked'].includes(c.status));
  if(savedBaseline&&completePass)return '<button class="button primary" data-action="compare-run">Compare a new run</button>';
  if(completePass)return '<button class="button primary" data-action="baseline-dialog">Save baseline</button>';
  if(failed.some(c=>checkHasFailingRequests(c,report)))return '<button class="button primary" data-action="show-failing">Show failing requests</button>';
  if(report.checks.some(c=>['inconclusive','blocked'].includes(c.status)))return '<button class="button primary" data-action="show-inconclusive">Show inconclusive checks</button>';
  if(report.baseline)return '<button class="button primary" data-action="show-comparison">Show comparison</button>';
  return '';
}
let pendingReportReveal=null;
function revealReportSection(section,{defer=true}={}) {
  if(defer&&reportView.tab==='performance'&&(!selectedPerformance().summary||reportView.pending)){
    pendingReportReveal={id:state.detail.id,version:state.routeVersion,hash:location.hash,section};return;
  }
  const panel=$(`#report-${reportView.tab}`);if(!panel||panel.hidden)return;
  if(['baseline','comparison'].includes(section)&&reportView.tab==='results'){
    state.tab='baseline';const reference=$('#report-baseline-reference',panel);if(reference){reference.open=true;reference.scrollIntoView?.({block:'start'});$('summary',reference)?.focus({preventScroll:true});}
  }else if(reportView.tab==='results'){
    scrollReportSection(section,'results');
  }else {
    const target=$(`#performance-${section}`,panel);if(!target)return;
    target.scrollIntoView?.({block:'start'});if(section==='baseline'){const heading=$('h2',target)||target;heading.setAttribute('tabindex','-1');heading.focus?.({preventScroll:true});}
  }
}
function finishPendingReportReveal() {
  const pending=pendingReportReveal;pendingReportReveal=null;
  const destination=reportRoute();
  if(pending&&pending.id===state.detail?.id&&pending.version===state.routeVersion&&pending.hash===location.hash&&destination?.section===pending.section&&destination.tab===reportView.tab)revealReportSection(pending.section,{defer:false});
}
function showFailingRequests() {
  chooseReportTab('results');
  const report=state.detail.report||summaryReport(state.detail);
  const check=report.checks.find(c=>c.id!=='baseline'&&['fail','blocked'].includes(c.status)&&checkHasFailingRequests(c,report));
  if(!check)return;
  state.selectedCheck=`${check.target}:${check.id}`;state.failingRequestsOnly=true;
  patchHTML($('#result-content'),renderChecks(report));loadFindingEvidence();
  const table=$('.check-detail .evidence-table');table?.setAttribute('tabindex','-1');table?.scrollIntoView?.({block:'start'});table?.focus({preventScroll:true});
}

function renderDetail() {
  cancelStopConfirmationTimer();
  state.reportRouteActive=true;
  const d=state.detail;
  syncReportView();
  if(state.liveError&&(state.liveError.id!==d.id||d.state==='stopping'||!activeState(d)))state.liveError=null;
  if(state.stopPending&&(state.stopPending.id!==d.id||d.state==='stopping'||!activeState(d)))state.stopPending=null;
  if(!activeState(d)){
    cancelLandings();hideSpotTip();renderRailRuns();
    if(state.liveRendered===d.id){const dialog=$('#request-dialog');if(dialog?.open){dialog.close();dialog.innerHTML='';}state.liveRendered=null;}
  }
  if(activeState(d)&&d.stoppable!==false&&state.active!==d.id)setActive(d.id);else if(state.active===d.id&&!activeState(d))setActive(null);else updateTitle();
  if(activeState(d)){renderLive();return;}
  const report=d.report;
  if(!report){
    const hadTestDialog=$('#test-dialog')?.open;if(hadTestDialog)closeConditionDialog({reveal:false});
    const summary=d.summary,records=d.live_requests||[],partial=summaryReport(d);
    patchHTML($('#main'),`<div class="page-heading"><div><div class="eyebrow">Run ${escape(d.id)}</div><h1>${userStopped(partial)?'Stopped by you':'Run could not finish'}</h1>${userStopped(partial)?renderGuidance(partial,d):`<p>${summary?'Only a summary was saved; request evidence may be unavailable.':'No report was saved. Review the error and settings before trying again.'}</p>`}</div></div>${notice(d.error || 'The runner did not produce a report.','error')}${renderReportToolbar()}${renderPerformance()}<section id="report-results" role="tabpanel" aria-labelledby="tab-results" data-live-key="report-results" ${reportView.tab==='results'?'':'hidden'}>${summary?`<section class="panel saved-summary"><h2>Saved summary</h2><p>Recorded overall: ${escape(statusLabels[summary.overall] || summary.overall)} · ${number(summary.request_count)} requests</p><section id="result-content">${renderChecks(partial)}</section></section>`:'<section class="panel check-verdict-summary"><h2>Checks & evidence</h2><p>No final check verdicts were saved.</p></section>'}${renderTests(records,{conditions:d.live_conditions||[],live:false})}${renderLiveLoadResults(d)}${renderAllRequests(records)}${renderDiagnostics(null,d)}</section><div class="actions">${keepButton()}${summary?resultActions(partial):''}<button class="button" data-action="rerun">Run again</button><a class="button" href="#runs">Back to runs</a></div>`);if(hadTestDialog)$(`#tab-${reportView.tab}`)?.focus({preventScroll:true});return;
  }
  const config=reportConfig(report),checks=report.checks.filter(c=>c.id!=='baseline');
  const required=checks.filter(c=>c.required);
  const memoryInterrupted=report.observations?.resources?.memory_interrupted_checks || [];
  const requiredMemoryInterrupted=required.some(c=>memoryInterrupted.some(item=>item.target===c.target&&item.id===c.id));
  const absolute=d.state==='error'?'error':checks.some(c=>c.status==='fail')?'fail':requiredMemoryInterrupted?'inconclusive':required.some(c=>c.status==='blocked')?'blocked':report.abort_reason||!required.length||required.some(c=>c.status==='inconclusive')||required.some(c=>c.status==='skipped'&&!unselectedCheck(c,report))?'inconclusive':required.every(c=>c.status==='skipped')?'skipped':'pass';
  const completePass=d.state==='completed'&&!report.abort_reason&&report.overall==='pass'&&checks.some(c=>c.status==='pass')&&checks.every(c=>c.status==='pass'||c.required===false&&c.status==='inconclusive'||unselectedCheck(c,report));
  const savedBaseline=(state.baselines || []).find(item=>item.run_id===d.id);
  const entering=state.enteredRun!==d.id;state.enteredRun=d.id;
  const compareButton=d.state==='completed'?`<button class="button" data-action="compare-saved">${report.baseline?'Change baseline':'Compare with baseline'}</button>`:'';
  patchHTML($('#main'),`<div class="page-heading result-heading ${absolute}${entering?' is-entering':''}"><div><div class="eyebrow">Run <span class="mono">${escape(d.id)}</span></div><h1>${escape(absolute==='error'?'Run could not finish':mainFinding(report))}</h1>${renderGuidance(report,d)}${renderBaselineSummary(report)}<div class="run-meta"><span><strong>${escape((config.targets || []).map(t=>t.model).join(', '))}</strong></span><span>${date(report.started_at)}</span><span>${duration(report)}</span><span>${number(report.requests.length)} requests recorded</span></div><p class="hint">${escape((config.targets || []).map(t=>t.url).filter(Boolean).join(' · '))}</p></div><div class="actions">${savedBaseline?`<p class="baseline-saved"><span aria-hidden="true">✓</span> Saved as baseline <strong>${escape(savedBaseline.name)}</strong></p>`:''}${resultActions(report,{completePass,savedBaseline})}<button class="button" data-action="rerun">${icon('repeat')} Run again</button>${compareButton}</div></div>
  ${d.error?notice(d.error,d.state==='error'?'error':'warning'):''}${storageOmission(report)}${d.auto_cleanup?.deleted?.length?notice(`Automatic cleanup deleted runs: ${d.auto_cleanup.deleted.map(escape).join(', ')}`):''}${renderReportToolbar()}${renderPerformance()}
  <section id="report-results" role="tabpanel" aria-labelledby="tab-results" data-live-key="report-results" ${reportView.tab==='results'?'':'hidden'}>${renderResultsJump()}
  <section id="results-checks" class="report-result-section"><section id="result-content">${renderChecks(report)}</section></section>
  <details id="report-baseline-reference" class="report-reference report-result-section comparison-container" ${report.baseline||state.tab==='baseline'?'open':''}><summary><h2>Baseline comparison</h2></summary><div id="baseline-content" data-comparison-scope="results">${renderComparison(report)}</div></details>
  <section id="results-configuration" class="report-result-section"><details id="report-config-reference" class="report-reference" ${state.tab==='config'?'open':''}><summary><h2>Run configuration</h2></summary>${renderConfig(report)}</details></section>
  <details class="report-reference condition-reference"><summary>Test conditions</summary>${renderTests(report.requests,{report})}</details>
  <section id="results-requests" class="report-result-section">${renderAllRequests(orderedRequests(report))}</section><section id="results-resources" class="report-result-section">${renderDiagnostics(report,d)}</section></section>
  <div class="actions" style="margin-top:24px">${keepButton()}<a class="button small" href="${runPath(d.id)}/report.html" download>Download HTML report</a><a class="button small" href="${runPath(d.id)}/report.json" download>Download JSON</a>${savedBaseline?`<button class="button small" data-action="compare-run">Compare a new run with this baseline</button><button class="button small" data-action="baseline-dialog">Save again under another name</button>`:!completePass&&d.state==='completed'?'<button class="button small" data-action="baseline-dialog">Save as reference baseline</button>':''}</div>`);
  syncComparisonFilters();refreshConditionDialog();loadFindingEvidence();
}
function renderDiagnostics(report,detail) {
  return `<details class="run-diagnostics"><summary>Run diagnostics</summary><p>Internal run ID: <code>${escape(report?.run_id || 'Not recorded')}</code></p>${renderResources(report,detail)}</details>`;
}
function memorySize(value,resources={}) {
  if(typeof value!=='number')return ['unavailable','not measured on this platform'].includes(resources.memory_measurement)||resources.measurement_available===false?'not measured on this platform':'not recorded';
  return value>=1024**3?`${number(value/1024**3,2)} GiB`:`${number(value/1024**2,1)} MiB`;
}
function newActiveNotice() {
  if(state.active)return `<strong>A run is already active.</strong> <a href="${linkRun(state.active)}">Inspect or stop it</a> before starting another.`;
  if(state.diskActive)return `<strong>Running from the CLI.</strong> <a href="${linkRun(state.diskActive)}">View the run</a>. You can start a web run.`;
  return '';
}
function renderResources(report,detail={}) {
  const resources=report?.observations?.resources || {},saved=detail.resources || {},peaks=resources.sampled_peak_rss_bytes || {},growth=resources.sampled_peak_growth_bytes || {},exportPeak=saved.sampled_peak_rss_bytes?.export;
  const limit=resources.memory_limit_bytes,active=resources.guardrail_active,unavailable=['unavailable','not measured on this platform'].includes(resources.memory_measurement)||resources.measurement_available===false;
  const guard=resources.memory_limit_reached?(resources.memory_limit_stopped_run?'Memory limit exceeded; unfinished work stopped with partial results. Completed checks keep their results.':'Memory limit exceeded; completed checks keep their results. No work was stopped.') :limit===0?'Memory guardrail disabled (0 = off).':active===true?`Maximum memory (RAM growth during this run): ${memorySize(limit)}. Guardrail active.`:active===false&&unavailable?'Memory guardrail inactive: RAM growth was not measured on this platform.':active===false?'Memory guardrail inactive: RAM at run start was not recorded.':'Memory guardrail status not recorded.';
  return `<section class="panel resource-summary" aria-labelledby="memory-title"><h2 id="memory-title">Peak memory (RAM)</h2><p class="hint">Sampled process RSS and RAM growth since this run started, separate from saved runs on disk. Only growth triggers the guardrail. The report helper uses a separate process; its memory is not included in these values or the guardrail.</p><dl class="metrics-grid">${metric('At run start',memorySize(resources.rss_at_start_bytes,resources))}${metric('Traffic',memorySize(peaks.traffic,resources),'Process RSS')}${metric('Scoring / aggregation',memorySize(peaks.scoring,resources),'Process RSS')}${metric('Traffic RAM growth',memorySize(growth.traffic,resources))}${metric('Scoring / aggregation RAM growth',memorySize(growth.scoring,resources))}${metric('Export',memorySize(exportPeak,saved),'Process RSS')}${metric('Process lifetime peak',memorySize(resources.process_peak_rss_bytes,resources),'Includes earlier runs in the web server; never used for the memory guardrail.')}</dl><p class="${resources.memory_limit_reached?'notice warning':'hint'}">${escape(guard)}</p><p class="hint">${escape(resources.sampling || 'Sampled about once per second; short spikes between samples can be missed.')}</p></section>`;
}
function storageOmission(report) {
  const storage=report.manifest?.storage;if(!storage||!storage.response_bodies_omitted&&!storage.limit_exceeded)return '';
  return notice(`Storage limit on disk: ${number(storage.response_bodies_omitted)} response ${storage.response_bodies_omitted===1?'body':'bodies'} omitted. ${storage.reason || 'Request metadata, scores and timing were preserved.'}${storage.limit_exceeded?' Request metadata alone exceeds the configured limit on disk.':''}`,'warning');
}
function keyMetric(check,report){if(check.status==='skipped')return report&&unselectedCheck(check,report)?'Not selected':check.summary || 'No result recorded';const m=check.metrics || {};if(check.id==='load')return counted(m.points?.length||0,'load test');if(check.id==='concurrency'&&m.load_types)return `${counted(Object.values(m.load_types).reduce((total,group)=>total+(group.points?.length || 0),0),'test point')} · ${counted(Object.keys(m.load_types).length,'load type')}`;if(check.id==='cancellation'&&m.cancellation_results)return `Server stopped: ${human(m.cancellation_results.server_stopped.status)} · Answer: ${human(m.cancellation_results.answer.status)}`;if(['context','correctness','json'].includes(check.id)&&m.scored)return `${m.correct} / ${m.scored} correct`;if(check.id==='capacity'&&Object.hasOwn(m,'highest_acceptable_rate_rps'))return m.highest_acceptable_rate_rps==null?'Arrival capacity unconfirmed':`${number(m.highest_acceptable_rate_rps,2)} requests/s ${m.request_pack?.control_status==='fail'?'loaded · quiet failed':'accepted'}`;if(check.id==='capacity'&&m.highest_tested_acceptable_concurrency!=null)return `Concurrency ${m.highest_tested_acceptable_concurrency} accepted`;if(check.id==='first_output')return `${ms(m.first_output_p95_ms)} p95 · ${m.attempted || 0} requests`;if(check.id==='generation')return `${ms(m.p95_ms)} p95 · ${m.attempted || 0} requests`;return m.attempted!=null?`${number(m.attempted)} requests · ${check.target}`:check.target;}
let findingEvidence={id:null,records:new Map(),pending:new Set(),errors:new Map()};
function checkViolationIds(check,report) {
  const ids=new Set();
  const visit=value=>{
    if(!value||typeof value!=='object')return;
    for(const [key,items] of Object.entries(value)){
      if(key==='timing_violations'){
        // Load groups partition qualified IDs from unattributed IDs before reporting.
        const qualifiedLoad=check.id==='load'&&Array.isArray(value.unattributed_timing_violations);
        const clientDelayed=value.timing_quality?.status==='client_delayed'||check.metrics?.timing_quality?.status==='client_delayed';
        if(Array.isArray(value.qualified_timing_violations)||(!qualifiedLoad&&clientDelayed))continue;
      }
      if(['violations','timing_violations','qualified_timing_violations'].includes(key)&&Array.isArray(items)){
        for(const item of items){const id=typeof item==='string'?item:item?.request_id||item?.id;if(id)ids.add(id);}
      }else if(typeof items==='object')visit(items);
    }
  };
  visit(check.metrics);
  const conditions=report.conditions||Object.values(report.observations?.targets||{}).flatMap(target=>target.conditions||[]);
  for(const condition of conditions)if((!condition.target||condition.target===check.target)&&(condition.check_id===check.id||condition.check_ids?.includes(check.id)))visit(condition);
  return ids;
}
function isFailingRequest(record,violations=null) {
  if(record.evaluation_error||record.status==='evaluation_error'||violations?.has(record.id))return true;
  return !record.intentional&&!isProbe(record)&&(record.score===false||record.valid===false||['failed','timeout','timed_out'].includes(record.status));
}
function requestPriority(record,violations=null) {return record.score===false?-2:isFailingRequest(record,violations)?-1:0;}
function findingRequests(check,report) {
  const violations=checkViolationIds(check,report),ids=new Set([...(check.evidence_ids||[]),...violations]);
  const records=report.requests.filter(r=>ids.has(r.id));
  return check.status==='pass'?records:records.sort((a,b)=>requestPriority(a,violations)-requestPriority(b,violations));
}
function checkHasFailingRequests(check,report) {
  const violations=checkViolationIds(check,report);
  return findingRequests(check,report).some(record=>isFailingRequest(record,violations));
}
function timingFailureReason(record,check,report,violations) {
  if(!violations.has(record.id))return '';
  if(check.status==='pass')return check.id==='capacity'?'Over limit (stage above accepted rate)':'Over configured limit';
  const measured=check.id==='capacity'?{...record,first_output_ms:record.arrival_first_output_ms??record.first_output_ms,elapsed_ms:record.arrival_elapsed_ms??record.elapsed_ms}:record;
  const config=reportConfig(report),limits=report.manifest?.effective_checks?.[check.id]?.limits||{...config.limits,...config.test_options?.[check.id]?.limits};
  const timings=[['first_token_ms','first_token_ms','TTFT'],['first_output_ms','first_output_ms','first output'],['elapsed_ms','latency_ms','response time'],['max_stream_gap_ms','stream_gap_ms','stream pause']];
  const reasons=timings.filter(([field,limit])=>(check.id!=='first_output'||field==='first_output_ms')&&(check.id!=='generation'||field!=='first_output_ms')&&Number.isFinite(measured[field])&&Number.isFinite(limits[limit])&&measured[field]>limits[limit])
    .map(([field,limit,label])=>`${label} ${number(measured[field]/1000,2)} s > ${number(limits[limit]/1000,2)} s`);
  if(Number.isFinite(record.generation_tokens_per_second)&&Number.isFinite(limits.min_output_tokens_per_second)&&record.generation_tokens_per_second<limits.min_output_tokens_per_second)reasons.push(`generation ${number(record.generation_tokens_per_second,2)} tok/s < ${number(limits.min_output_tokens_per_second,2)} tok/s`);
  return reasons.length?`Too slow: ${reasons.join(' · ')}`:`Timing limit exceeded: ${shortReason(check.summary).summary}`;
}
function inlineAnswerRequests(check,report) {
  if(check.status==='pass'||unselectedCheck(check,report))return [];
  return findingRequests(check,report).filter(r=>r.score===false||r.evaluation||r.evaluation_error||['correctness','json','context','tools'].includes(check.id)).slice(0,3);
}
async function loadFindingEvidence() {
  const detail=state.detail,report=detail?.report;
  if(!report?.requests_compact||reportView.tab!=='results'||typeof fetch!=='function')return;
  if(findingEvidence.id!==detail.id)findingEvidence={id:detail.id,records:new Map(),pending:new Set(),errors:new Map()};
  const evidence=findingEvidence,version=state.routeVersion,records=[...new Map(report.checks.flatMap(check=>inlineAnswerRequests(check,report)).map(r=>[r.id,r])).values()];
  const missing=records.filter(r=>!evidence.records.has(r.id)&&!evidence.pending.has(r.id)&&!evidence.errors.has(r.id));
  if(!missing.length)return;
  for(const record of missing)evidence.pending.add(record.id);
  await Promise.all(missing.map(async record=>{
    try{const value=await api(`${runPath(detail.id)}/requests/${encodeURIComponent(record.id)}`);if(state.detail?.id===detail.id&&state.routeVersion===version&&findingEvidence===evidence)evidence.records.set(record.id,value);}
    catch(error){if(findingEvidence===evidence)evidence.errors.set(record.id,error.message);}
    finally{evidence.pending.delete(record.id);}
  }));
  if(state.detail?.id===detail.id&&state.routeVersion===version&&findingEvidence===evidence)renderDetail();
}
function requestWithEvidence(record) {return findingEvidence.id===state.detail?.id?findingEvidence.records.get(record.id)||record:record;}
function requestQuestion(record,report) {
  const value=requestWithEvidence(record),config=reportConfig(report);
  const question=config.request_pack?.cases?.find(item=>value.fixture_id===`pack:${item.fixture_id}`||value.fixture_id===item.fixture_id);
  const messages=value.evaluation?.messages || value.messages || question?.messages || [];
  const content=[...messages].reverse().find(message=>message.role==='user')?.content;
  const text=(typeof content==='string'?content:Array.isArray(content)?content.filter(part=>part.type==='text').map(part=>part.text).join(' '):value.prompt || '').replace(/\s+/g,' ').trim();
  return text?text.slice(0,80)+(text.length>80?'…':''):`Open request ${value.fixture_id||value.id||''}`.trim();
}
function renderInlineAnswer(record,report,{loading=true}={}) {
  const fetched=findingEvidence.id===state.detail?.id?findingEvidence.records.get(record.id):null;
  if(!report.requests_compact||fetched)return requestAnswerDifference(fetched||record);
  const error=findingEvidence.id===state.detail?.id?findingEvidence.errors.get(record.id):null;
  return error?`<span class="hint">${escape(error)}</span>`:loading?'<span class="hint">Loading expected and received answers…</span>':`<button class="text-button" data-request="${escape(record.id)}">Open request</button>`;
}
function answerExcerpt(value) {
  const text=value===undefined?'Not recorded':typeof value==='string'?value:JSON.stringify(value);
  return text.length>130?`<details class="answer-excerpt"><summary>${escape(text.slice(0,130))}…</summary><pre>${escape(text)}</pre></details>`:escape(text);
}
function responseSavingNote(record,report) {
  const text=record.output||'',calls=record.tool_calls||[],policy=reportConfig(report).retention||'failures';
  if(text&&record.output_chars>[...text].length)return 'Response truncated';
  if(text||calls.length)return '';
  if(['failed','timeout','timed_out'].includes(record.status)&&!(record.output_chars>0))return `No response received${record.error?': '+record.error:''}`;
  if(policy==='none'&&record.valid===false&&record.output_chars===0)return '';
  if(policy==='none')return 'Response not saved (retention: none)';
  const omitted=report?.manifest?.storage?.response_bodies_omitted;
  if(omitted&&(record.output_chars>0||record.valid||record.tool_evaluation))return 'Response not saved (storage limit)';
  if(omitted&&(record.evaluation?.scorer==='tool'||record.evaluation?.tool_expectation))return 'Response evidence unavailable (storage limit)';
  if(record.output_chars>0||record.answer_chars>0||(policy==='failures'&&record.valid))return `Response not saved (retention: ${policy})`;
  return '';
}
function requestAnswerDifference(record) {
  const evaluation=record.evaluation || {},tool=evaluation.tool_expectation;
  const schema=evaluation.schema ?? evaluation.schema_,rubric=evaluation.rubric;
  const expected=tool?.expected_calls!=null?{...(tool.expected_content?{text:tool.expected_content.value}:{}),tool_calls:tool.expected_calls}:tool?.expected_content?.value ?? (evaluation.expected_provided===false?undefined:evaluation.expected);
  const hasExpected=tool?.expected_calls!=null||tool?.expected_content!=null||evaluation.expected_provided===true||(evaluation.expected_provided!==false&&evaluation.expected!==undefined);
  const requirement=hasExpected?`<strong>Expected:</strong> ${answerExcerpt(expected)}`:schema?`<strong>Schema:</strong> ${answerExcerpt(schema)}`:rubric?`<strong>Rubric:</strong> ${answerExcerpt(rubric)}`:'<strong>Expected:</strong> No fixed answer configured';
  const received=tool?.expected_calls!=null?{...(record.output?{text:record.output}:{}),tool_calls:record.tool_calls||[]}:record.tool_calls?.length&&!record.output?record.tool_calls:record.output;
  const note=responseSavingNote(record,state.detail?.report),hasBody=Boolean(record.output||record.tool_calls?.length);
  const body=hasBody?answerExcerpt(received)+(note?`<small class="hint">${escape(note)}</small>`:''):`<span class="hint">${escape(note||(record.output===undefined?'Open request for response evidence':record.status==='completed'?'Empty response':'No response recorded'))}</span>`;
  return `<span class="answer-difference"><span>${requirement}${!hasExpected&&(schema||rubric)&&(record.score_message||record.evaluation_error)?`<small>${escape(record.score_message||record.evaluation_error)}</small>`:''}</span><span><strong>Received:</strong> ${body}</span></span>`;
}
function renderChecks(report) {
  const checks=report.checks.filter(c=>c.id!=='baseline'),unselected=checks.filter(c=>unselectedCheck(c,report));
  const selected=checks.filter(c=>!unselectedCheck(c,report)).map(c=>inProgress(c.status)&&!activeState(state.detail)?{...c,status:'inconclusive'}:c).sort((a,b)=>(order[a.status]??5)-(order[b.status]??5));
  const attention=selected.filter(c=>c.status!=='pass'),passed=selected.filter(c=>c.status==='pass');
  const item=c=>{
    const key=`${c.target}:${c.id}`,excerpts=inlineAnswerRequests(c,report);
    const reason=shortReason(c.summary);
    return `<article class="panel finding-check ${escape(c.status)}" data-finding="${escape(key)}"><div class="finding-check-heading"><h2><button class="text-button" data-check="${escape(key)}" aria-pressed="${state.selectedCheck===key}">${escape(checkGuide[c.id]?.name || c.title)}${(reportConfig(report).targets||[]).length>1?` · ${escape(c.target)}`:''}</button></h2>${badge(c.status)}</div><p class="finding-check-reason">${escape(reason.summary)}</p>${reason.rest?`<details><summary>Details</summary><p>${escape(reason.rest)}</p></details>`:''}${excerpts.length?`<div class="finding-requests">${excerpts.map(r=>`<div class="finding-request"><div class="finding-question"><button class="text-button" data-request="${escape(r.id)}">${escape(requestQuestion(r,report))}</button><small>${escape(scenarioLabel(r,report))}</small></div>${renderInlineAnswer(r,report)}</div>`).join('')}</div>`:''}${state.selectedCheck===key?`<div class="finding-expanded check-detail">${renderCheck(c,report)}</div>`:''}</article>`;
  };
  return `<div class="findings" id="failing-requests" tabindex="-1"><p class="hint check-count">${number(selected.length)} checks · ${number(attention.length)} need attention · ${number(passed.length)} passed</p>${attention.map(item).join('')}${passed.length?`<details class="passed-checks" ${!attention.length||passed.some(c=>`${c.target}:${c.id}`===state.selectedCheck)?'open':''}><summary>Passed checks · ${passed.length}</summary>${passed.map(item).join('')}</details>`:''}${unselected.length?`<details class="checks-not-selected"><summary>Not selected: ${unselected.map(c=>escape(checkGuide[c.id]?.name || c.title)).join(', ')}</summary><p>No traffic was sent for these checks.</p></details>`:''}${!selected.length?'<p>No selected checks produced a result.</p>':''}</div>`;
}

function metric(label,value,detail=''){return `<div class="metric"><dt>${escape(label)}</dt><dd>${escape(value)}</dd>${detail?`<small>${escape(detail)}</small>`:''}</div>`;}
function renderCancellationResults(metrics){
  const results=metrics.cancellation_results;
  if(!results)return notice('This older report has no separate server-stop and answer verdicts. Rerun Limits and cancellation to record them.');
  return ['server_stopped','answer'].map(key=>{
    const result=results[key],count=key==='answer'?`${result.correct} / ${result.scored} answers correct`:`${result.verified} / ${result.attempted} native aborts verified`;
    return `<section class="check-detail-head" aria-label="${escape(result.title)}"><div class="check-detail-heading"><h3>${escape(result.title)}</h3>${badge(result.status)}</div><p>${escape(result.summary)}</p><p class="hint">${escape(count)}</p></section>`;
  }).join('');
}
function renderCheck(check,report) {
  const m=check.metrics || {},config=reportConfig(report),recordedLimits=report.manifest?.effective_checks?.[check.id]?.limits;
  const limits=recordedLimits || {...(config.limits || {}),...(config.test_options?.[check.id]?.limits || {})};
  const defaultLimits=report.manifest?.default_check_limits?.[check.id] || [];
  const limitDuration=value=>value>=1000?`${number(value/1000,1)} s`:ms(value);
  const timingLimit=(name,missing)=>limits[name]?`Limit: ${recordedLimits?'≤ ':''}${recordedLimits?limitDuration(limits[name]):ms(limits[name])}${recordedLimits&&defaultLimits.includes(name)?' (default)':''}`:missing;
  const positions=requestPositions(report.requests),violations=checkViolationIds(check,report),records=findingRequests(check,report);
  const sorted=records.filter(r=>!state.failingRequestsOnly||isFailingRequest(r,check.status==='pass'?null:violations));
  const metrics=[];
  if(m.timing_quality){
    const quality=m.timing_quality;
    metrics.push(metric('Client timing quality',`${quality.affected_requests} / ${quality.total_requests} requests affected`,
      quality.status==='client_delayed'?'Scheduling delay overlaps these measurements. Raw timings are retained; endpoint attribution is uncertain.':'No material client scheduling delay overlaps the assessed timing intervals.'));
  }
  if(check.id==='load'){
    metrics.push(metric('Scheduled load tests',number(m.points?.length),'Sequential workload × pattern × level/rate results'));
  }else if(check.id==='concurrency'&&m.load_types){
    const points=Object.values(m.load_types).flatMap(group=>group.points || []),sum=key=>points.reduce((total,point)=>total+(point[key] || 0),0);
    metrics.push(metric('Test points',number(points.length),counted(Object.keys(m.load_types).length,'load type')));
    metrics.push(metric('Requests completed / sent',`${number(sum('completed'))} / ${number(sum('sent'))}`,`${number(sum('failed'))} failed · ${number(sum('timed_out'))} timed out · includes drain`));
  }else if(check.id==='gpu'){
    const snapshots=m.snapshots || [],samples=snapshots.flatMap(s=>s.samples || []);
    metrics.push(metric('Telemetry snapshots',`${m.completed ?? 0} / ${m.attempted ?? 0}`,'Snapshots containing reported samples'));
    metrics.push(metric('Samples observed',number(samples.length),`${samples.filter(s=>s.stale).length} stale samples`));
    metrics.push(metric('Unavailable families',number((m.unavailable_families || []).length),'Missing telemetry does not mean healthy'));
  }else if(['context','correctness','json'].includes(check.id)){
    metrics.push(metric('Known answers correct',`${m.correct ?? 0} / ${m.scored ?? 0}`,`Required accuracy: ${number((limits.min_correctness ?? 1)*100,1)}%`));
    metrics.push(metric('Completed responses',`${m.completed ?? 0} / ${m.attempted ?? 0}`,'Completion and correctness are distinct'));
    if(check.id==='context')metrics.push(metric('Longest measured input',m.observed_max_input_tokens==null?'Not reported':`${number(m.observed_max_input_tokens)} tokens`,`Declared context: ${number(m.declared_context_limit)} tokens`));
    else metrics.push(metric('Response time · p95',ms(m.p95_ms),`${m.completed ?? 0} valid completions`));
  }else{
    metrics.push(metric('Requests completed',`${m.completed ?? 0} / ${m.attempted ?? 0}`,`${m.failed ?? 0} failed · ${m.timed_out ?? 0} timed out`));
    if(check.id==='first_output'){metrics.push(metric('Warmed first output · p95',ms(m.first_output_p95_ms),timingLimit('first_output_ms','No acceptance limit set')));metrics.push(metric('Initial first output',ms(m.initial?.first_output_p50_ms),'Recorded separately from warmed samples'));}
    else if(check.id==='capacity'&&m.traffic_stages){metrics.push(metric(m.request_pack?.control_status==='fail'?'Highest acceptable tested loaded rate · quiet failed':'Highest acceptable tested arrival rate',m.highest_acceptable_rate_rps==null?'Unconfirmed':`${number(m.highest_acceptable_rate_rps,2)} requests/s`,m.request_pack?.control_status==='fail'?'Loaded result only; quiet checks failed. Maximum capacity unknown.':'Highest tested acceptable rate; maximum capacity unknown'));metrics.push(metric('Rate stages',number(m.traffic_stages.length),'Each stage is offered independently, then drained'));}
    else if(check.id==='capacity'){metrics.push(metric('Highest accepted load',m.highest_tested_acceptable_concurrency==null?'Unconfirmed':number(m.highest_tested_acceptable_concurrency),'Highest tested; maximum capacity unknown'));metrics.push(metric('Response time · p95',ms(m.p95_ms),`${m.completed ?? 0} valid completions`));}
    else if(check.id==='fairness'){metrics.push(metric('Mixed / short-only latency',m.latency_ratio==null?'Unmeasured':`${number(m.latency_ratio,2)}×`,limits.fairness_max_ratio?`Limit: ${limits.fairness_max_ratio}×`:'No slowdown limit set'));metrics.push(metric('Observed overlap',`${m.overlaps ?? 0} / ${m.pairs ?? 0}`,'Pairs with concurrent prefill'));}
    else {metrics.push(metric('Response time · p95',ms(m.p95_ms),timingLimit('latency_ms','No response-time limit set')));metrics.push(metric('Longest stream pause',ms(m.max_stream_gap_ms),timingLimit('stream_gap_ms','No stream-pause limit set')));}
  }
  if(check.id==='cancellation' && typeof m.backend_cancellation==='object'){
    const backend=m.backend_cancellation;
    metrics.push(metric(backend.verification_scope==='capacity_release'?'Capacity release evidence':'Backend evidence',human(backend.status),`${backend.runtime || 'Unknown runtime'} · ${(backend.probes || []).filter(p=>p.status==='pass').length} / ${backend.attempted || 0} probes verified`));
    metrics.push(metric('Cleanup deadline',elapsed(backend.cleanup_seconds),backend.summary));
  }
  if(check.id==='generation'){
    const current=Object.hasOwn(m,'generation_rate_samples');
    metrics.push(metric('Generation pace · median (estimate)',m.generation_tokens_per_second_p50==null?'Unmeasured':`${number(m.generation_tokens_per_second_p50,1)} tok/s`,current?`${m.generation_rate_samples} measured samples; excludes waiting for first output`:'Older report: generation timing was not recorded'));
    if(current)metrics.push(metric('Generation pace · slowest (estimate)',m.generation_tokens_per_second_min==null?'Unmeasured':`${number(m.generation_tokens_per_second_min,1)} tok/s`,limits.min_output_tokens_per_second?`Every measurable request must reach ${limits.min_output_tokens_per_second} tok/s`:'No minimum generation rate set'));
    metrics.push(metric('End-to-end output rate · median',m.output_tokens_per_second_p50==null?'Not reported':`${number(m.output_tokens_per_second_p50,1)} tok/s`,current?'Includes waiting for first output; separate from generation pace':'Older report: the output-rate limit used this end-to-end definition'));
  }
  const extras=Object.fromEntries(Object.entries(m).filter(([k,v])=>typeof v==='object'&&v!==null || ['backend_cancellation','near_limit_coverage','tls','tls_expiry'].includes(k)));
  return `<div class="check-detail-head"><div class="check-detail-heading"><h2>${escape(checkGuide[check.id]?.name || check.title)}</h2>${badge(check.status)}</div><p>${escape(check.summary)}</p>${!check.required?'<p class="hint">Optional check</p>':''}</div>
  ${check.id==='cancellation'&&check.status!=='skipped'?renderCancellationResults(m):''}
  ${check.status==='skipped'?`<div class="empty-state" style="padding:35px 24px"><p>${unselectedCheck(check,report)?'This check was not selected. No result is inferred.':'This check was skipped. The recorded reason is shown above; no passing result is inferred.'}</p></div>`:`<dl class="metrics-grid">${metrics.join('')}</dl>`}
  ${check.id==='cancellation'?`<div class="notice" style="margin:20px 24px 0">${escape(m.backend_cancellation?.summary || 'Backend cancellation was not verified.')} Physical device memory reclamation is not measured.</div>`:''}
  ${check.id==='recovery'?`<div class="notice" style="margin:20px 24px 0">Sustained traffic was configured for ${number(config.sustained_seconds,1)} seconds. Results describe this bounded observation period.</div>`:''}
  ${m.logprobs?renderLogprobResults(m.logprobs):''}
  ${check.id==='sessions'&&m.conversation_performance?renderSessionResults(m.conversation_performance):''}${check.id==='concurrency'?renderConcurrentResults(m):check.id==='load'?renderLoadTestResults(m):''}
  ${m.request_pack?renderPackResults(m.request_pack):''}
  ${check.id==='capacity'&&m.traffic_stages?renderTrafficStages(m.traffic_stages,m.request_pack):''}
  ${check.id==='capacity'&&m.levels?`<div class="metric-table"><table><thead><tr><th>Load</th><th>Achieved</th><th>Correct</th><th>p95</th></tr></thead><tbody>${Object.entries(m.levels).map(([k,v])=>`<tr><td class="mono">${escape(k)}</td><td>${number(v.achieved_concurrency)}</td><td>${v.correct ?? 0} / ${v.scored ?? 0}</td><td class="mono">${ms(v.p95_ms)}</td></tr>`).join('')}</tbody></table></div>`:''}
  ${check.id==='gpu'&&m.snapshots?renderGpuSnapshots(m.snapshots):''}
  ${records.length?`<div class="evidence-title"><span>Requests</span><span class="hint">${state.failingRequestsOnly?`${sorted.length} failing · `:''}${records.length} recorded · incorrect and failed first</span></div><div class="evidence-table"><table><thead><tr><th>Test / request</th><th>Response</th><th>Answer</th><th>Time</th></tr></thead><tbody>${sorted.map(r=>`<tr><td><button class="text-button" data-request="${escape(r.id)}">${escape(requestQuestion(r,report))}</button><small>${escape(scenarioLabel(r,report))}</small><small>${escape(requestIdentity(r,report.requests,undefined,positions))}</small></td><td>${escape(r.status)}${violations.has(r.id)?`<small${check.status==='pass'?' class="hint"':''}>${escape(timingFailureReason(r,check,report,violations))}</small>`:''}${isProbe(r)?'<small class="request-probe">Deliberate probe</small>':''}</td><td>${r.score===false?`<span class="status fail">Incorrect</span>${renderInlineAnswer(r,report,{loading:false})}`:r.score===true?'<span class="status pass">Correct</span>':'<span class="dim">Unscored</span>'}</td><td class="mono nowrap">${ms(r.elapsed_ms)}</td></tr>`).join('')}</tbody></table></div>`:check.status==='skipped'?'':'<div class="check-observations hint">No request evidence was recorded for this check.</div>'}
  ${Object.keys(extras).length?`<details class="check-observations"><summary>More measurements</summary><pre>${escape(JSON.stringify(extras,null,2))}</pre></details>`:''}`;
}
function renderGpuSnapshots(snapshots){return snapshots.map(snapshot=>`<div class="metric-table"><h3>${escape(human(snapshot.phase))} <span class="hint">${date(snapshot.collected_at)}</span></h3>${snapshot.reason?`<p class="hint" style="margin:12px 0">${escape(snapshot.reason)}</p>`:''}${snapshot.unavailable?.length?`<p class="hint" style="margin:12px 0">Unavailable: ${snapshot.unavailable.map(human).map(escape).join(', ')}</p>`:''}${snapshot.samples?.length?`<table><thead><tr><th>Metric</th><th>Value</th><th>Freshness</th></tr></thead><tbody>${snapshot.samples.map(s=>`<tr><td>${escape(s.name)}<small>${escape(s.labels)}</small></td><td class="mono">${number(s.value,3)}</td><td>${s.stale?'Stale':s.age_seconds==null?'Scrape only':`${number(s.age_seconds,1)}s old`}</td></tr>`).join('')}</tbody></table>`:''}</div>`).join('');}
function renderTrafficStages(stages,pack=null){
  const quietFailed=pack?.control_status==='fail';
  const goodputLabel=pack?(pack.all_cases_scored?'Correct goodput':'Scored-subset correct goodput'):'Cohort goodput';
  return `<div class="traffic-summary"><h3>Continuous arrival stages</h3><p class="hint">Arrival latency includes local dispatch delay. Server queue time is not measured. ${pack?'Valid throughput counts valid completions. '+goodputLabel+' counts correct, valid completions within all configured limits.':'Cohort goodput counts correct, valid answers within all configured limits.'} Rates include completions during drain per second of the arrival window.</p></div><div class="metric-table traffic-stage-table"><table><thead><tr><th>Offered rate</th><th>Scheduled / sent</th><th>Failures / timeouts</th><th>Local drops</th><th>Arrival latency · p95</th>${pack?'<th>Valid throughput</th>':''}<th>${goodputLabel}</th><th>${quietFailed?'Loaded result · quiet failed':'Result'}</th></tr></thead><tbody>${stages.map(stage=>`<tr><td class="mono">${number(stage.rate_rps,2)} req/s<small>${number(stage.duration_seconds,1)}s arrival window</small></td><td>${number(stage.scheduled)} / ${number(stage.started)}<small>${number(stage.not_offered || 0)} not offered</small></td><td>${number(stage.failed)} / ${number(stage.timed_out)}<small>${stage.correctness==null?'Unscored':`${number(stage.correctness*100,1)}% correct`}</small></td><td>${number(stage.dropped_local || 0)} at limit<small>${number(stage.dropped_late || 0)} late · dispatch p95 ${ms(stage.dispatch_lag_p95_ms)}</small></td><td class="mono">${ms(stage.arrival_latency_p95_ms)}<small>First answer ${ms(stage.arrival_first_output_p95_ms)}</small></td>${pack?`<td class="mono">${number(stage.valid_throughput_rps,2)} req/s</td>`:''}<td class="mono">${number(pack?stage.scored_subset_correct_goodput_rps:stage.goodput_rps,2)} req/s<small>${number(stage.good)} good · ${stage.good_fraction==null?'No arrivals':`${number(stage.good_fraction*100,1)}% of arrivals`}</small></td><td>${quietFailed&&stage.status==='pass'?'<span>Loaded pass · quiet failed</span>':badge(stage.status)}<small>${escape((stage.reasons || []).join('; '))}</small></td></tr>`).join('')}</tbody></table></div>${stages.some(s=>s.windows?.length)?`<details class="check-observations"><summary>Waiting and failures through each stage</summary>${stages.map(stage=>`<div class="metric-table"><h3>${number(stage.rate_rps,2)} requests/s</h3><table><thead><tr><th>Arrival window</th><th>Pending at end</th><th>Arrival latency · p95</th><th>Failures</th><th>Local drops</th></tr></thead><tbody>${(stage.windows || []).map(w=>`<tr><td>${number(w.from_seconds,1)}–${number(w.to_seconds,1)}s</td><td>${number(w.pending_at_end)}</td><td>${ms(w.arrival_latency_p95_ms)}</td><td>${w.error_rate==null?'—':`${number(w.error_rate*100,1)}%`}</td><td>${number((w.dropped_local || 0)+(w.dropped_late || 0))}</td></tr>`).join('')}</tbody></table></div>`).join('')}</details>`:''}`;
}
function renderTrafficComparison(capacity){
  const rate=value=>value==null?'Unconfirmed':`${number(value,2)} req/s`;
  return `<div class="traffic-summary"><h3>Tested arrival capacity ${badge(capacity.status)}</h3><p class="hint">${escape(capacity.reason || '')}</p><dl class="metrics-grid"><div class="metric"><dt>Baseline accepted rate</dt><dd>${rate(capacity.baseline_highest_acceptable_rate_rps)}</dd></div><div class="metric"><dt>Current accepted rate</dt><dd>${rate(capacity.current_highest_acceptable_rate_rps)}</dd></div><div class="metric"><dt>Capacity change</dt><dd>${capacity.delta_rps==null?'Unconfirmed':`${capacity.delta_rps>0?'+':''}${number(capacity.delta_rps,2)} req/s`}</dd><small>${capacity.change_percent==null?'Matching, complete stages required':`${capacity.change_percent>0?'+':''}${number(capacity.change_percent,1)}%`}</small></div></dl></div>${capacity.stages?.length?`<div class="metric-table"><table><thead><tr><th>Rate</th><th>Baseline goodput</th><th>Current goodput</th><th>Capacity evidence</th></tr></thead><tbody>${capacity.stages.map(stage=>`<tr><td>${rate(stage.rate_rps)}</td><td>${rate(stage.baseline?.goodput_rps)}</td><td>${rate(stage.current?.goodput_rps)}</td><td>${badge(stage.status)}<small>${escape(stage.reason || '')}</small>${stage.timing_comparable===false?`<small>${escape(stage.timing_comparison_reason || '')}</small>`:''}</td></tr>`).join('')}</tbody></table></div>`:''}`;
}
function comparisonLoadLabel(point) {
  const shape=point.shape||point.workload;
  const workload=loadShapes.includes(shape)?loadShapeLabel(shape):loadTitles[point.type]||human(shape||point.type);
  const traffic=point.pattern==='concurrent'?`maintained concurrency${point.level!=null?' '+number(point.level):''}`:({constant:'constant arrivals',poisson:'Poisson arrivals',bursts:'bursts',interference:'short-answer comparison'})[point.pattern]||human(point.pattern);
  const phase=point.pattern==='interference'?(point.role==='probe'?'Long-input probe':({baseline:'Short-only control',probe:'During long-input probe'})[point.phase]):null;
  return `${workload} · ${traffic}${point.rate!=null?' · '+number(point.rate,2)+' requests/second':''}${point.bursts&&point.pattern!=='bursts'?' + bursts':''}${point.source&&point.source!=='primary'?' · '+human(point.source):''}${phase?' · '+phase:''}`;
}
function loadAcceptanceChangeLabel(change, status) {
  if(change===undefined)return `Recorded: ${human(status||'inconclusive')}`;
  return ({gained:'Newly accepted',lost:'Lost',unchanged_accepted:'Still accepted',unchanged_not_accepted:'Still not accepted',not_established_in_baseline:'Not established in baseline',accepted_baseline_not_established:'Accepted now; not established in baseline',not_established_in_current:'Not established in current',not_established:'Not established',not_tested_in_baseline:'Not tested in baseline',not_tested_in_current:'Not tested in current',work_changed:'Requested work changed'})[change]||human(change||'inconclusive');
}
function renderLoadAcceptance(targets,sectionPrefix='results') {
  const tested=targets.filter(target=>target.load_test||target.load_exclusions?.length);
  if(!tested.length)return '';
  return `<section id="${sectionPrefix}-load-acceptance" class="comparison-section load-acceptance"><h3>Tested load acceptance</h3>${tested.map(target=>{
    const load=target.load_test||{},key=point=>JSON.stringify([point.source||'primary',point.type,point.workload,point.pattern,Boolean(point.bursts)]);
    const previous=new Map((load.baseline_highest_accepted||[]).map(point=>[key(point),point])),current=new Map((load.current_highest_accepted||[]).map(point=>[key(point),point]));
    const scopes=new Map((load.points||[]).map(row=>row.current||row.baseline).filter(point=>point&&['concurrent','constant','poisson'].includes(point.pattern)).map(point=>[key(point),point]));
    for(const point of [...previous.values(),...current.values()])if(!scopes.has(key(point)))scopes.set(key(point),point);
    const highest=(points,id,point)=>points.has(id)?`${number(points.get(id).value,2)}${point.pattern==='concurrent'?'':' requests/second'}`:'Not established';
    const pointStatus=point=>{
      if(!point)return 'Not tested';
      const status=point.not_run?'Not run':point.cut_short?'Cut short':statusLabels[point.status]||human(point.status||'inconclusive');
      const samples=[point.dispatched!=null?`${number(point.dispatched)} dispatched`:null,point.required!=null?`${number(point.required)} required`:null].filter(Boolean);
      const concurrency=[point.server_evidenced_concurrency!=null?`Server concurrency ${number(point.server_evidenced_concurrency,2)}`:null,point.client_mean_inflight!=null?`Client inflight ${number(point.client_mean_inflight,2)}`:null].filter(Boolean);
      return `${escape(status)}${point.valid!=null?` · ${number(point.valid)} valid`:''}${samples.length?`<small>${escape(samples.join(' · '))}</small>`:''}${concurrency.length?`<small>${escape(concurrency.join(' · '))}</small>`:''}`;
    };
    return `<div class="load-acceptance-target"><h4>${escape(target.target||target.name||'Target')} ${load.status?badge(load.status):''}</h4>${load.reason?`<p class="hint">${escape(load.reason)}</p>`:''}${[...scopes].map(([id,point])=>`<div class="load-acceptance-highest"><h5>${escape(comparisonLoadLabel({...point,level:null,rate:null}))}</h5><p>Highest accepted ${point.pattern==='concurrent'?'concurrency':'request rate'} (tested) · Baseline: <strong>${highest(previous,id,point)}</strong> · Current: <strong>${highest(current,id,point)}</strong></p></div>`).join('')}${load.points?.length?`<div class="metric-table load-acceptance-table" tabindex="0" role="region" aria-label="${escape(target.target||target.name||'Target')} tested load acceptance"><table><thead><tr><th scope="col">Tested load</th><th scope="col">Baseline</th><th scope="col">Current</th><th scope="col">Change</th><th scope="col">Reason</th></tr></thead><tbody>${load.points.map(row=>`<tr><th scope="row">${escape(comparisonLoadLabel(row.current||row.baseline||{}))}</th><td>${pointStatus(row.baseline)}</td><td>${pointStatus(row.current)}</td><td><strong>${escape(loadAcceptanceChangeLabel(row.acceptance_change,row.status))}</strong></td><td>${escape(row.reason||'')}</td></tr>`).join('')}</tbody></table></div>`:''}${target.load_exclusions?.length?`<div class="load-comparison-exclusions"><h5>Load measurements not compared</h5><ul>${target.load_exclusions.map(row=>`<li><strong>${escape(row.load_point?comparisonLoadLabel(row.load_point):row.scenario||'Tested load')}</strong><span>${escape(row.reason||'')}</span></li>`).join('')}</ul></div>`:''}</div>`;
  }).join('')}</section>`;
}
function isProbe(r){return ['client_cancel','client_deadline','deadline_probe'].includes(r.scenario)||/intentional cancellation/i.test(r.error || '');}
function renderBaselineSummary(report) {
  const baseline=report.baseline;if(!baseline)return '';
  const saved=state.baselines.find(item=>(state.detail?.baseline_id&&item.id===state.detail.baseline_id)||item.run_id===baseline.run_id);
  const rows=(baseline.targets||[]).flatMap(target=>[...(target.comparisons||[]),...comparisonOutputs(target)]);
  const warnings=rows.filter(row=>comparisonWarnings(row).length).length;
  return `<p class="baseline-delta"><a href="${linkRun(state.detail.id)}/results/comparison" data-tab="baseline">vs baseline ${escape(saved?.name||baseline.name||baseline.run_id||'reference')}: ${number(rows.length)} measurements · ${number(warnings)} with warnings</a></p>`;
}
function comparisonOutputs(target) {
  if(target.observed_outputs)return target.observed_outputs;
  const seen=new Set(),rows=[];
  for(const row of target.comparisons||[]){
    const key=JSON.stringify([row.check,comparisonCohort(row)]),work=row.output_work;
    if(seen.has(key)||!work?.current||!work?.baseline)continue;
    seen.add(key);
    for(const [metric,valueKey,countKey] of [['output_tokens','reported_output_tokens_p50','reported_token_samples'],['output_chars','output_chars_p50','valid_completions']])rows.push({metric,check:row.check,scenario:row.scenario,load_point:row.load_point,baseline:work.baseline[valueKey],current:work.current[valueKey],baseline_samples:work.baseline[countKey],current_samples:work.current[countKey],descriptive:true,aggregation:'median'});
  }
  return rows;
}
function comparisonChange(row) {
  if(row.current==null||row.baseline==null)return 'Not measured';
  const delta=row.current-row.baseline;
  if(delta===0)return 'No change';
  if(row.metric.endsWith('_rate'))return `${delta>0?'+':''}${number(delta*100,1)} percentage points`;
  if(row.baseline===0)return `${delta>0?'+':''}${comparisonValue(row,delta)} · from zero`;
  const percent=delta*100/row.baseline;
  return `${percent>0?'+':''}${number(percent,1)}%`;
}
function comparisonMetricLabel(row) {
  return ({correctness_rate:'Correct answers',valid_completion_rate:'Completed valid responses',error_rate:'Request errors',latency_ms:'Full response time',first_output_ms:'First useful output',max_stream_gap_ms:'Longest pause per response',output_tokens_per_second:'Output rate, including initial wait',generation_tokens_per_second:'Generation pace',output_tokens:'Answer length · tokens',output_chars:'Answer length · characters'})[row.metric]||human(row.metric);
}
function comparisonWarnings(row) {
  return row.warnings?.length?row.warnings.map(w=>w.message):row.status==='inconclusive'?[row.reason||'Insufficient evidence to interpret this change.']:[];
}
function comparisonCategory(row){return ['correctness_rate','output_tokens','output_chars'].includes(row.metric)?'answers':['valid_completion_rate','error_rate'].includes(row.metric)?'delivery':'timing';}
function comparisonWarningSummary(row) {
  const notes=[];
  for(const warning of row.warnings||[]){
    if(warning.code==='output_work_changed'){
      const before=row.output_work?.baseline?.reported_output_tokens_p50,after=row.output_work?.current?.reported_output_tokens_p50;
      notes.push(before!=null&&after!=null?`Answer length changed: ${number(before)} → ${number(after)} tokens.`:'Answer lengths differ.');
    }else if(warning.code==='client_delayed'){
      for(const [name,q] of Object.entries(row.timing_quality||{}))if(q.status==='client_delayed')notes.push(q.scope==='legacy_run_maximum'?`${name} recorded a run-wide Giraffe delay; request-level overlap was not recorded.`:`Giraffe delays affected ${q.affected_requests}/${q.total_requests} ${name.toLowerCase()} requests.`);
    }else if(warning.code==='sample_uncertainty')notes.push('Sample uncertainty limits this assessment.');
    else if(warning.code==='input_work_changed')notes.push('Input workload differs.');
    else if(warning.code==='missing_timing')notes.push('Some response timings are missing.');
    else if(warning.code==='timing_version')notes.push('Timing measurement methods differ.');
    else notes.push(warning.message);
  }
  return [...new Set(notes.length?notes:comparisonWarnings(row))];
}
function comparisonTable(rows) {
  return `<div class="comparison-values">${rows.map(row=>{
    const warnings=comparisonWarnings(row),shortWarnings=comparisonWarningSummary(row);
    const label=row.descriptive?'Observed':row.status==='fail'?'Regression detected':warnings.length?'Warning':'No material regression';
    return `<article class="comparison-measurement" data-metric="${escape(row.metric)}"><div class="comparison-measurement-heading"><h4>${escape(comparisonMetricLabel(row))}</h4><small>${row.aggregation==='mean'?'Rate':'Median'} · ${escape(label)}</small></div><dl class="comparison-value-grid"><div><dt>Baseline</dt><dd>${comparisonValue(row,row.baseline)}</dd><small>n=${number(row.baseline_samples)}</small></div><div><dt>Current</dt><dd>${comparisonValue(row,row.current)}</dd><small>n=${number(row.current_samples)}</small></div><div><dt>Change</dt><dd>${comparisonChange(row)}</dd></div></dl>${warnings.length?`<div class="comparison-brief-warning">${shortWarnings.map(w=>`<p>${escape(w)}</p>`).join('')}<details class="comparison-support"><summary>Why this warning?</summary><ul>${warnings.map(w=>`<li>${escape(w)}</li>`).join('')}</ul></details></div>`:''}${row.worsening_interval_95?`<details class="comparison-statistics"><summary>Statistical details</summary><p>95% interval for worsening: ${row.worsening_interval_95.map(v=>number(v,1)).join(' to ')} ${row.change_unit==='percentage_points'?'percentage points':'%'}. Threshold: ${number(row.threshold,1)} ${row.change_unit==='percentage_points'?'percentage points':'%'}. Warnings still apply.</p></details>`:''}</article>`;
  }).join('')}</div>`;
}
function comparisonCohort(row){return row.load_point?['source','type','workload','shape','pattern','level','rate','bursts','phase','role'].map(name=>row.load_point[name]):row.scenario;}
function comparisonGroup(id,rows) {
  const row=rows[0],label=row?.load_point?comparisonLoadLabel(row.load_point):scenarioLabel(row);
  return `<div class="comparison-check-group" data-comparison-group="${escape(id)}"><p class="comparison-group-heading hint">${escape(row?.targetName||'')} · ${escape(catalogTitle(id))} · ${escape(label)}</p>${comparisonTable(rows)}</div>`;
}

function baselineIdentity(baseline) {
  const saved=state.baselines.find(item=>(state.detail?.baseline_id&&item.id===state.detail.baseline_id) || (baseline.run_id&&item.run_id===baseline.run_id));
  const run=state.runs.find(item=>baseline.run_id&&item.run_id===baseline.run_id);
  const label=baseline.saved_baseline_name || saved?.name || run?.targets?.map(target=>target.model).filter(Boolean).join(', ') || 'Saved reference run';
  const when=baseline.source_started_at || run?.started_at || saved?.saved_at;
  return `<strong>${escape(label)}</strong>${when?`<span>${date(when)}</span>`:''}<details class="baseline-identifier"><summary>Reference ID</summary><code>${escape(baseline.run_id || 'Not recorded')}</code></details>`;
}
function renderDeploymentChange(diff) {
  if(!diff)return '';
  const changed=(diff.fields||[]).filter(row=>row.change!=='no_observed_change').map(row=>row.field),intended=diff.intended_change;
  let summary=changed.length?`Deployment change: ${changed.join(', ')}`:'No observed deployment changes.';
  if(changed.length&&intended&&!diff.confounded)summary+=' (one intended change)';
  const fields=Array.isArray(intended)?intended:[intended];
  return `<p class="deployment-change">${escape(summary)}</p>${diff.confounded?`<p><strong>Confounded: multiple unrelated changes.</strong></p>${intended?`<p class="hint">Intended change: ${escape(fields.join(', '))}</p>`:''}`:''}${diff.unknown_baseline?'<p class="hint">Baseline deployment configuration was not recorded.</p>':''}${diff.interpretation?`<p class="hint">${escape(diff.interpretation)}</p>`:''}`;
}
function renderComparisonDetails(targets) {
  if(!targets.length)return '';
  return `<details class="comparison-support comparison-deployment"><summary>Deployment and comparison details</summary><p class="hint">Changes provide investigation context, not proof of causality. Missing or stale fields do not establish unchanged configuration.</p>${targets.map(target=>`<section><h4>${escape(target.target||target.name||'Target')}</h4><p>${target.configuration_diff?.confounded?'Confounded experiment: other recorded settings changed alongside the intended change.':'Causality unverified.'}</p><pre>${escape(JSON.stringify({configuration_diff:target.configuration_diff,identity_changes:target.identity_changes,telemetry:target.telemetry,consistency:target.consistency_changes},null,2))}</pre></section>`).join('')}</details>`;
}
function renderComparison(report,{scope='results'}={}) {
  const sectionPrefix=scope==='performance'?'performance-comparison':'results',filterPrefix=scope==='performance'?'performance-comparison':'comparison';
  const b=report.baseline;
  if(!b)return `<div class="panel empty-state"><h2>No baseline selected</h2><p>Choose a saved baseline to compare this completed run. No new requests will be sent. This run still evaluates its own acceptance limits.</p><button class="button" data-action="compare-saved">Compare with baseline</button><button class="button" data-action="baseline-dialog">Save this run as a baseline</button></div>`;
  const allRows=(b.targets||[]).flatMap(t=>[...(t.comparisons||[]),...comparisonOutputs(t)].map(row=>({...row,targetName:t.target||t.name||'Target'}))),checkIds=[...new Set(allRows.map(row=>row.check))].sort();
  if(!['all','with_warnings','without_warnings'].includes(state.comparisonFilter))state.comparisonFilter='all';
  const warningCount=allRows.filter(row=>comparisonWarnings(row).length).length,acceptance=b.acceptance||{};
  const acceptanceLabel=value=>value?escape(statusLabels[value]||value):'Not recorded';
  const filterRow=row=>(state.comparisonFilter==='all'||(state.comparisonFilter==='with_warnings'?comparisonWarnings(row).length>0:comparisonWarnings(row).length===0))&&(state.comparisonCheck==='all'||row.check===state.comparisonCheck);
  return `<header id="${sectionPrefix}-comparison" class="comparison-conclusion"><div><p>${number(allRows.length)} measurements · ${number(warningCount)} with warnings</p></div><div class="baseline-reference"><span>Compared with</span>${baselineIdentity(b)}${state.detail?.state==='completed'?'<button class="button small" data-action="compare-saved">Change baseline</button>':''}</div></header><section id="${sectionPrefix}-acceptance" class="comparison-acceptance"><h3>Pass / fail</h3><div><span>Baseline: <strong>${acceptanceLabel(acceptance.baseline)}</strong></span><span>Current: <strong>${acceptanceLabel(acceptance.current)}</strong></span></div><p class="hint">Against each run’s requirements.</p></section>${(b.targets||[]).filter(target=>target.configuration_diff).map(target=>`<section class="comparison-section"><h3>${escape(target.target||target.name||'Target')} · Deployment</h3>${renderDeploymentChange(target.configuration_diff)}</section>`).join('')}${renderLoadAcceptance(b.targets||[],sectionPrefix)}<div class="comparison-filters">${allRows.length?`<div><label for="${filterPrefix}-outcome">Show</label><select id="${filterPrefix}-outcome" data-comparison-filter="outcome"><option value="all">All measurements</option><option value="with_warnings">With warnings</option><option value="without_warnings">Without warnings</option></select></div>${checkIds.length>1?`<div><label for="${filterPrefix}-check">Check</label><select id="${filterPrefix}-check" data-comparison-filter="check"><option value="all">All checks</option>${checkIds.map(id=>`<option value="${escape(id)}">${escape(catalogTitle(id))}</option>`).join('')}</select></div>`:''}`:''}<details class="comparison-support comparison-method"><summary>How comparison is done</summary><p>${escape(b.method||'Not recorded.')}</p></details></div>${(b.targets||[]).flatMap(t=>(t.reasons||[]).map(reason=>`<p class="notice warning">${escape(t.target||'Target')}: ${escape(reason)}</p>`)).join('')}${(b.targets||[]).filter(t=>t.traffic_capacity).map(t=>`<section class="comparison-section"><h3>${escape(t.target||t.name||'Target')}</h3>${renderTrafficComparison(t.traffic_capacity)}</section>`).join('')}${comparisonSections.filter(([id])=>allRows.some(row=>comparisonCategory(row)===id)).map(([id,title])=>{
    const rows=allRows.filter(row=>comparisonCategory(row)===id&&filterRow(row)),groups=new Map();
    for(const row of rows){const key=JSON.stringify([row.targetName,row.check,comparisonCohort(row)]);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(row);}
    return `<section id="${sectionPrefix}-${id}" class="comparison-section"><h3>${escape(title)}</h3>${[...groups.values()].map(values=>comparisonGroup(values[0].check,values)).join('')}${!rows.length?'<p class="hint">No measurements match these filters.</p>':''}</section>`;
  }).join('')}${renderComparisonDetails(b.targets||[])}`;
}
function syncComparisonFilters(){for(const control of $$('[data-comparison-filter]'))control.value=control.dataset.comparisonFilter==='outcome'?state.comparisonFilter:state.comparisonCheck;}
function comparisonValue(row,value){if(value==null)return '—';if(typeof value!=='number')return escape(typeof value==='string'?value:JSON.stringify(value));if(row.metric==='accepted_arrival_rate')return `${number(value,2)} req/s`;if(row.metric.endsWith('_rate'))return `${number(value*100,1)}%`;if(row.metric.endsWith('_ms'))return ms(value);return `${number(value,2)}${row.metric.endsWith('tokens_per_second')?' tok/s':row.metric==='output_tokens'?' tokens':row.metric==='output_chars'?' chars':''}`;}
function renderDeployment(report){
  return `<section class="panel"><div class="section-heading"><h2>Deployment and serving observations</h2></div><p class="hint">Changes provide investigation context, not proof of causality. Source freshness and other replicas remain qualified in the evidence.</p>${Object.entries(report.observations?.targets || {}).map(([name,obs])=>`<h3>${escape(name)}</h3><details><summary>Deployment snapshot and unknown fields</summary><pre>${escape(JSON.stringify(obs.deployment,null,2))}</pre></details><details><summary>Deployment observations during the run</summary><pre>${escape(JSON.stringify(obs.deployment_history || [],null,2))}</pre></details><details><summary>Serving telemetry and phase coverage</summary><pre>${escape(JSON.stringify(obs.serving_telemetry,null,2))}</pre></details>`).join('')}<a class="button" href="${runPath(state.detail.id)}/reproduction.json" download>Export reproduction JSON</a></section>`;
}
function renderConfig(report){return `${renderDeployment(report)}${report.warnings?.length?`<div class="notice warning">${report.warnings.map(w=>`<p>${escape(w)}</p>`).join('')}</div>`:''}<section class="panel"><div class="section-heading"><h2>Recorded configuration</h2><div class="actions"><span class="hint">Suite ${escape(report.suite_version)}</span><a class="text-button" href="${runPath(state.detail.id)}/config.json" download>Download config</a></div></div><pre class="config-display">${escape(JSON.stringify(report.manifest,null,2))}</pre></section>`;}
function liveStopReasons(detail) {
  const events=detail.events||[],progress=detail.progress||{},stops=events.filter(event=>event.event==='run_stopping'),reasons=new Set();
  const add=(value,target='')=>{
    if(typeof value==='string'&&value.trim())reasons.add(target?`${target}: ${value}`:value);
    else if(Array.isArray(value))for(const item of value){if(typeof item==='string')add(item);else if(item)add(item.abort_reason||item.reason,item.target||'');}
    else if(value&&typeof value==='object')for(const [name,item] of Object.entries(value))add(typeof item==='string'?item:item?.abort_reason||item?.reason,name);
  };
  add(progress.abort_reason||events.at(-1)?.abort_reason||[...events].reverse().find(event=>event.abort_reason)?.abort_reason||detail.abort_reason);
  for(const event of stops)add(event.abort_reason);
  for(const source of [detail,progress,...stops,events.at(-1)||{}])for(const field of ['abort_reasons','target_abort_reasons','target_stop_reasons','stop_reasons','per_target_reasons'])add(source[field]);
  if(detail.id&&state.liveStopReasonState?.id===detail.id)add(state.liveStopReasonState.reasons);
  state.liveStopReasonState={id:detail.id,reasons:[...reasons]};
  return [...reasons];
}
function renderLive() {
  cancelStopConfirmationTimer();
  state.reportRouteActive=true;
  syncReportView();
  state.detail.live_requests=stableLiveRequests(state.detail.live_requests || []);
  state.enteredRun=state.detail.id;
  state.liveRendered=state.detail.id;
  state.mosaicAnimated=state.detail.id;
  const d=state.detail,p=d.progress || {},c=d.config || reportConfig(d.report),events=(d.events || []).filter(e=>!String(e.event || '').startsWith('condition_')),last=events.at(-1) || {},progress={...last,...p};
  const finished=progress.completed || 0,started=progress.attempted || 0,secs=progress.elapsed_seconds || 0,stopping=d.state==='stopping',stopPending=state.stopPending?.id===d.id;
  const judging=progress.phase==='judging'||String(progress.event || '').startsWith('judging_');
  const judgeDone=progress.judge_calls_completed || 0,judgeTotal=progress.judge_calls_total || 0;
  const planned=plannedRequests(d),confirmation=state.stopConfirmation?.id===d.id&&Date.now()<state.stopConfirmation.until;
  const stopReasons=liveStopReasons(d),liveError=state.liveError?.id===d.id?state.liveError.message:'';
  const title=d.stoppable===false?'Running from the CLI':judging?'Evaluating answers':d.state==='queued'?'Preparing run':'Running';
  const description=judging?'Target traffic finished. Evaluating generated answers against golden answers; target measurements are frozen.':'One spot per test. Select a spot for its requests and results.';
  const bounds=judging?[['Evaluator concurrency',c.judging?.concurrency],['Evaluation duration',elapsed(c.judging?.max_duration_seconds)],['Judge deadline',elapsed(c.judging?.request_timeout_seconds)],['Judge output cap',`${c.judging?.max_output_tokens} tokens`]]:[['Simultaneous limit',c.concurrency??'Not set'],['Maximum requests',c.max_requests??'Not set'],['Maximum duration',c.max_duration_seconds==null?'Not set':elapsed(c.max_duration_seconds)],['Request timeout',elapsed(c.request_timeout_seconds)],['Maximum output tokens',`${c.max_output_tokens} tokens`]];
  patchHTML($('#main'),`<div class="page-heading"><div><div class="eyebrow">Live run <span class="mono">${escape(d.id)}</span></div><h1>${p.phase==='saving_partial_report'?'Saving partial report…':stopping?'Stopping your check':title}</h1><p>${stopping?'Stopping further work and saving the evidence collected so far.':description}</p><p class="live-progress">${judging?`Evaluation calls · ${number(judgeDone)} of ${number(judgeTotal)}`:`${planned==null?`${number(finished)} requests finished`:`${number(finished)} of ~${number(planned)} planned requests`} · ${elapsed(secs)} · limit ${c.max_requests==null?'not set':number(c.max_requests)}`}</p></div>${d.stoppable===false?'':`<button class="button danger" data-action="stop" aria-label="${confirmation?'Confirm stop run':'Stop run'}" ${stopping||stopPending?'disabled':''}>${icon('stop')}${stopping||stopPending?'Stopping…':confirmation?'Confirm stop':'Stop run'}</button>`}</div>
  ${renderReportToolbar()}${renderPerformance()}<section id="report-results" role="tabpanel" aria-labelledby="tab-results" data-live-key="report-results" ${reportView.tab==='results'?'':'hidden'}><section class="panel check-verdict-summary"><h2>Checks & evidence</h2><p>Checks are still running. Finished requests are not a passing check verdict.</p></section>
  ${d.live_request_summary?.evicted?`<p class="hint live-eviction-summary">${number(d.live_request_summary.evicted)} earlier requests outside the live window · ${Object.entries(d.live_request_summary.statuses||{}).map(([status,count])=>`${number(count)} ${escape(human(status))}`).join(' · ')}</p>`:''}${stopReasons.length?`<section class="notice warning live-stop-reason" role="status"><strong>Run stop reason</strong><ul>${stopReasons.map(reason=>`<li>${escape(reason)}</li>`).join('')}</ul></section>`:''}${renderTests(d.live_requests || [],{conditions:d.live_conditions || [],live:true})}${renderLiveLoadResults(d)}<div class="live-layout"><div><section class="panel live-state">${badge(d.state)}<h2>${judging?escape(testLabel({...progress,scenario:'judging'})):escape(progress.scenario?scenarioLabel(progress):human(progress.event || 'Preparing requests'))}</h2><p class="live-phase">${escape(progress.target || (c.targets || []).map(t=>t.name).join(', '))} · ${escape((c.targets || []).map(t=>t.model).join(', '))}</p>
</section>
  <details class="panel live-activity"><summary>Activity · last 5 events</summary><div class="event-list">${[...events].reverse().slice(0,5).map(e=>`<div class="event-row"><span class="time">${elapsed(e.elapsed_seconds)}</span><span>${escape(({request_started:'Request sent',request_completed:'Response received',run_started:'Run started',run_stopping:'Stopping run',run_completed:'Run complete',judging_started:'Answer evaluation started',judging_progress:'Evaluating answers'})[e.event] || human(e.event || 'Progress'))}${e.scenario?` · ${escape(scenarioLabel(e))}`:''}</span></div>`).join('')||'<p class="hint">Preparing the first requests…</p>'}</div></details></div>
  <aside class="panel run-preview">${renderPhases(d.phases || events)}<h3>${judging?'Evaluation limits':'Limits'}</h3>${judging?'<p>Separate limits apply to the configured judges.</p>':''}<dl class="bound-list">${bounds.map(([k,v])=>`<div><dt>${escape(k)}</dt><dd>${escape(v)}</dd></div>`).join('')}</dl><hr><p class="hint">${judging?'Incomplete panels, evaluation failures and ties leave answers unscored.':'Finished requests include responses, failures, timeouts and cancellations. Scheduled requests can also use budget without reaching the server.'}</p></aside></div><div id="live-error" class="error-message" role="alert" ${liveError?'':'hidden'}>${escape(liveError)}</div>${renderAllRequests(d.live_requests || [],{budget:c.max_requests,live:true,budgetUsed:progress.budget_used ?? started,attempted:started,droppedOffers:progress.dropped_offers || 0})}${renderDiagnostics(d.report,d)}</section>`,{landing:true});
  const dialog=$('#request-dialog'),status=dialog?.open?$('.live-request-status',dialog):null,record=status&&(d.live_requests || []).find(r=>String(r.slot)===status.dataset.statusSlot);
  if(record){if(status.getAttribute('data-request-id')!==String(record.id || '')){cancelLandings(status);status.setAttribute('data-request-id',record.id || '');}patchHTML(status,badge(requestStatus(record)),{landing:true});const identity=$('.request-identity',dialog);if(identity)patchHTML(identity,identity.getAttribute('data-condition-request-context')?conditionEvidenceIdentityText(JSON.parse(identity.getAttribute('data-condition-request-context'))):requestIdentityContent(record,d.live_requests));}
  refreshSpotTip();refreshConditionDialog();loadDetailPlan();scheduleStopConfirmationReset();
}
function renderJudgement(judgement) {
  if(!judgement)return '';
  const votes=judgement.votes || [],yes=votes.filter(vote=>vote.verdict===true).length,no=votes.filter(vote=>vote.verdict===false).length;
  const incomplete=!votes.length||votes.some(vote=>vote.status!=='completed'||typeof vote.verdict!=='boolean');
  const explanation=incomplete?'Incomplete panel · answer unscored':yes===no?'Tie · answer unscored':`${yes} accept · ${no} reject`;
  return `<section class="judge-evidence"><h3>AI evaluation ${badge(judgement.status)}</h3><p class="hint">${explanation}${yes&&no?' · Disagreement among judges':''}</p>${votes.map(vote=>`<article class="judge-vote"><div class="statline"><strong>${escape(vote.judge)}</strong>${vote.verdict===true?badge('pass','Accept'):vote.verdict===false?badge('fail','Reject'):badge('inconclusive','No verdict')}</div><p class="hint">${escape(vote.model)}${vote.observed_model&&vote.observed_model!==vote.model?` · observed ${escape(vote.observed_model)}`:''} · ${escape(vote.url)}</p>${vote.error_category?`<p class="hint">Evaluation error: ${escape(vote.error_category)}</p>`:''}${vote.reason?`<p class="judge-reason">${escape(vote.reason)}</p>`:'<p class="hint">Reason not retained or unavailable.</p>'}</article>`).join('')}</section>`;
}
function renderLogprobResults(value) {
  const records=state.detail?.report?.requests || [],positions=requestPositions(records),byId=new Map(records.map(record=>[record.id,record]));
  const requestName=id=>{const record=byId.get(id);return record?`${testLabel(record)} · ${requestIdentity(record,records,undefined,positions)}`:'Request evidence';};
  const cohorts=[...(value.conditions || []),...(value.cross_conditions || [])];
  return `<section class="tool-evaluation"><h3>Token probability consistency · ${escape(value.status)}</h3><p>${escape(value.interpretation)}</p><p>Tolerance: ${escape(value.tolerance ?? 'diagnostic')} logprob units · ${number(value.repetitions)} rounds · ${number(value.priming_rounds)} priming rounds</p><div class="metric-table"><table><thead><tr><th>Condition</th><th>Coverage</th><th>Maximum difference</th><th>Token divergence / top-k changes</th><th>Outcome</th></tr></thead><tbody>${cohorts.map(c=>`<tr><td>${(c.levels || [c.level]).map(level=>escape(testLabel({scenario:`consistency_c${level}_${c.shape}`}))).join(' vs ')}</td><td>${c.issued_requests ?? '—'} / ${c.planned_requests ?? '—'} requests<small>${number(c.compared_token_pairs)} token pairs</small></td><td>${escape(c.max_absolute_delta ?? 'unknown')}</td><td>${number(c.token_divergences)} / ${number(c.top_k_membership_changes)}</td><td>${escape(c.status)}</td></tr>`).join('')}</tbody></table></div>${cohorts.flatMap(c=>c.examples || []).map(e=>`<p>${escape(e.kind)} at position ${number(e.position)} · ${escape(e.absolute_delta ?? 'unknown')} ${e.request_ids.map(id=>`<button class="text-button" data-request="${escape(id)}">${escape(requestName(id))}</button>`).join(' ')}</p>`).join('')}</section>`;
}
function renderLogprobEvidence(record) {
  return record.logprob_evidence?`<h3>Token log probabilities</h3><pre>${escape(JSON.stringify(record.logprob_evidence,null,2))}</pre>`:'';
}
function renderToolEvaluation(record) {
  const evaluation=record.tool_evaluation;if(!evaluation)return '';
  const components=[['protocol','Response protocol'],['content','Assistant text'],...(evaluation.choice?[['choice','Tool choice'],['arguments','Argument schemas'],['task','Task expectations']]:[['calls','Tool calls']])];
  return `<section class="tool-evaluation"><h3>Tool response checks</h3>${evaluation.support?`<p class="hint">Support: ${escape(evaluation.support)}</p>`:''}${components.map(([key,label])=>{
    const result=evaluation[key];if(!result)return '';
    return `<div class="statline"><strong>${label}</strong>${badge(result.status,result.status==='not_checked'?'Not checked':undefined)}</div><p class="hint">${escape(result.message || '')}</p>`;
  }).join('')}${record.tool_conversation?`<h3>Tool-result conversation</h3><pre>${escape(JSON.stringify(record.tool_conversation,null,2))}</pre>`:''}</section>`;
}
function renderActualToolCalls(record) {
  if(!record.tool_evaluation&&!record.evaluation?.tool_expectation&&!record.tool_calls?.length)return '';
  return `<h3>Actual tool calls</h3>${record.tool_calls?.length?`<div class="body-output"><pre>${escape(JSON.stringify(record.tool_calls,null,2))}</pre></div>`:'<p class="body-not-retained">No tool-call body retained or recorded. See the tool-call outcome for what was checked.</p>'}`;
}
function renderResponseEvents(record) {
  if(!record.response_events?.length&&!record.response_events_truncated)return '';
  return `<details class="response-events"><summary>Response event sequence</summary><p class="hint">Bounded protocol metadata in arrival order. Text and arguments are not included.${record.response_events_truncated?' The event list was truncated.':''}</p><pre>${escape(JSON.stringify(record.response_events || [],null,2))}</pre></details>`;
}
function renderEvaluationEvidence(record,report) {
  let evidence=record.evaluation;
  if(!evidence){
    const config=report.manifest?.config;
    const item=config?.request_source==='request_pack'?config.request_pack?.cases.find(item=>record.fixture_id===`pack:${item.fixture_id}`):null;
    if(item)evidence={...item,expected_provided:Object.hasOwn(item,'expected')};
  }
  if(!evidence)return '<p class="hint">Input and answer-check details were not saved in this report.</p>';
  if(evidence.tool_expectation){
    const expected=evidence.tool_expectation,content=expected.expected_content;
    return `<h3 id="request-input-messages" tabindex="-1">Input messages</h3>${(evidence.messages || []).map(message=>`<h4>${escape(message.role)}</h4><div class="body-output"><pre>${escape(message.content)}</pre></div>`).join('')}<h3>Expected assistant text</h3>${content?`<p class="hint">${escape(content.mode)} · ${content.normalization==='whitespace'?'Whitespace normalized':'No normalization'}. Compares combined assistant text.</p><div class="body-output"><pre>${escape(content.value)}</pre></div>`:'<p class="hint">No assistant text requirement. Tool-only responses are allowed.</p>'}${expected.choice?`<h3>Tool choice rule</h3><pre>${escape(JSON.stringify(expected.choice,null,2))}</pre>`:''}<h3>Expected tool calls</h3>${expected.expected_calls==null?'<p class="hint">No task-specific call expectation configured.</p>':`<p class="hint">Exact count and order. Null arguments require schema validity without a fixed value.</p><div class="body-output"><pre>${escape(JSON.stringify(expected.expected_calls,null,2))}</pre></div>`}${record.tool_request_options?`<details><summary>Sent tool options</summary><pre>${escape(JSON.stringify(record.tool_request_options,null,2))}</pre></details>`:''}<details><summary>Tool definitions</summary><pre>${escape(JSON.stringify(expected.tools || [],null,2))}</pre></details>`;
  }
  const expected=evidence.scorer==='json'?JSON.stringify(evidence.expected,null,2):String(evidence.expected ?? '');
  const purpose=evidence.scorer==='none'&&typeof evidence.expected?.purpose==='string'?evidence.expected.purpose:null;
  const schema=evidence.schema ?? evidence.schema_;
  return `<h3 id="request-input-messages" tabindex="-1">Input messages</h3>${(evidence.messages || []).map(message=>`<h4>${escape(message.role)}</h4><div class="body-output"><pre>${escape(message.content)}</pre></div>`).join('')}<h3>Answer check</h3><p>${escape(answerChecks[evidence.scorer] || evidence.scorer)}</p>${evidence.scorer==='json'?'<p class="hint">JSON accepts one complete Markdown code fence; surrounding prose is rejected. Expected values are compared structurally.</p>':''}${evidence.scorer==='none'?(purpose?`<h3>Purpose</h3><p>${escape(purpose)}</p>`:''):`<h3>Expected answer</h3>${evidence.expected_provided?`<div class="body-output"><pre>${escape(expected)}</pre></div>`:'<p class="hint">No expected answer configured.</p>'}`}${evidence.rubric?`<h3>Judge rubric</h3><div class="body-output"><pre>${escape(evidence.rubric)}</pre></div>`:''}${schema!=null?`<details><summary>JSON schema</summary><pre>${escape(JSON.stringify(schema,null,2))}</pre></details>`:''}`;
}
let requestEvidenceSequence=0,requestEvidenceFocus=null;
const requestEvidenceCache=new Map();
function conditionEvidenceIdentityText(context) {
  const width=Math.max(4,String(context.total||context.number).length);
  return `<strong>Request #${String(context.number).padStart(width,'0')} of ${number(context.total)} · ${escape(context.label)}</strong>`;
}
function renderConditionEvidenceIdentity(context) {return `<p class="request-identity condition-evidence-identity">${conditionEvidenceIdentityText(context)}</p>`;}
function renderRequestEvidenceContext(record,report,context) {
  const content=renderRequestContext(record,report.requests);
  return context?content.replace(/<p class="request-identity">[\s\S]*?<\/p>/,renderConditionEvidenceIdentity(context)):content;
}
function closeRequestEvidence() {
  requestEvidenceSequence++;$('#request-dialog')?.close();
  const back=$('#test-dialog')?.open?$$('[data-condition-request]',$('#test-dialog')).find(node=>node.dataset.conditionRequest===requestEvidenceFocus?.id):null;
  const previous=requestEvidenceFocus?.element,retained=previous?.isConnected===false?null:previous;
  const fallback=$('#test-dialog')?.open?$('#condition-filter')||$('[data-condition-action="close"]'):$(`#tab-${reportView.tab}`);
  (back||retained||fallback)?.focus?.({preventScroll:true});
}
async function openRequest(id) {
  const detail=state.detail,report=detail?.report,conditionContext=conditionRequestContext(id);
  if(!detail)return;
  if(!report){
    const record=detail.live_requests?.find(record=>record.id===id);
    if(record?.slot!=null){requestEvidenceFocus={id,element:document.activeElement};openLiveRequest(record.slot);if(conditionContext){const identity=$('#request-dialog .request-identity');if(identity){identity.innerHTML=conditionEvidenceIdentityText(conditionContext);identity.setAttribute('data-condition-request-context',JSON.stringify(conditionContext));}}const footer=$('#request-dialog .dialog-footer');if(footer&&$('#test-dialog')?.open)footer.innerHTML='<button class="button" data-action="back-to-test">Back to test details</button>';return;}
  }
  let r=report?.requests?.find(x=>x.id===id);
  if(!r&&!activeState(detail))return;
  const dialog=$('#request-dialog');requestEvidenceFocus={id,element:document.activeElement};
  const sequence=++requestEvidenceSequence;
  if(report?.requests_compact||!report){
    const key=JSON.stringify([detail.id,id]);
    r=requestEvidenceCache.get(key);
    if(!r){
      dialog.innerHTML='<div class="dialog-head"><h2 id="request-title">Request evidence</h2></div><div class="dialog-body"><p role="status">Loading request evidence…</p></div><div class="dialog-footer"><button class="button" data-action="close-dialog">Back</button></div>';
      if(!dialog.open)dialog.showModal();
      try{r=await api(`${runPath(detail.id)}/requests/${encodeURIComponent(id)}`);if(state.detail?.id!==detail.id||sequence!==requestEvidenceSequence||!dialog.open)return;requestEvidenceCache.set(key,r);while(requestEvidenceCache.size>3)requestEvidenceCache.delete(requestEvidenceCache.keys().next().value);}
      catch(error){if(state.detail?.id===detail.id&&sequence===requestEvidenceSequence&&dialog.open)dialog.innerHTML=`<div class="dialog-head"><h2 id="request-title">Request evidence unavailable</h2></div><div class="dialog-body">${notice(error.message,'warning')}</div><div class="dialog-footer"><button class="button" data-action="close-dialog">Back to test details</button></div>`;return;}
    }
  }
  if(!r)return;
  const evidenceReport=report||{requests:detail.live_requests||[],manifest:{config:detail.config}};
  const retention=reportConfig(evidenceReport).retention || 'failures';
  // Render a full record only after the explicit inspection action; compact rows stay compact.
  dialog.innerHTML=`<div class="dialog-head"><div><div class="eyebrow">Request evidence</div><h2 id="request-title">${escape(testLabel(r,evidenceReport))}</h2><p class="hint">${escape(conditionInfo(r,evidenceReport).purpose)}</p></div><button class="button ghost" data-action="close-dialog" aria-label="Close request evidence">✕</button></div><div class="dialog-body">${renderRequestEvidenceContext(r,evidenceReport,conditionContext)}${renderRequestTermination(r)}<div class="statline"><span class="pill">${escape(r.target)}</span><span>Response: ${escape(r.status)}</span>${r.score===false?badge('fail','Incorrect answer'):r.score===true?badge('pass','Correct answer'):'<span class="dim">Answer unscored</span>'}</div>${isProbe(r)?'<div class="notice" style="margin-top:20px">This request is an intentional cancellation or deadline probe. Its transport status alone does not indicate a failed suite check.</div>':''}${r.error?`<div class="notice ${isProbe(r)?'':'error'}" style="margin-top:20px">${escape(r.error)}</div>`:''}${r.score_message?`<p class="hint" style="margin-top:18px">${escape(r.score_message)}</p>`:''}${renderToolEvaluation(r)}<div class="request-comparison"><section>${renderEvaluationEvidence(r,evidenceReport)}</section><section><h3>Actual answer <span class="hint">${number(r.output_chars)} recorded characters</span></h3><div class="body-output">${r.output?`<pre>${escape(r.output)}</pre>`:r.output_chars>0?`<p class="body-not-retained">Body not retained (retention: ${escape(retention)}). Measurements and scoring are preserved.</p>`:'<p class="body-not-retained">No answer text was recorded for this request.</p>'}</div>${renderLogprobEvidence(r)}${renderActualToolCalls(r)}</section></div>${renderResponseEvents(r)}${renderJudgement(r.judgement)}<details class="request-timing"><summary>Timing & transport details</summary><dl class="metrics-grid" style="margin-top:24px">${r.traffic_stage!=null?metric('Arrival rate',`${number(r.traffic_rate_rps,2)} requests/s`,r.traffic_class || '')+metric('Local dispatch delay',ms(r.dispatch_lag_ms),'Measured from planned arrival; not server queue time'):''}${metric('Elapsed',ms(r.elapsed_ms))}${metric('First answer',ms(r.first_output_ms))}${metric('Last answer',ms(r.last_output_ms))}${metric('First reasoning',ms(r.first_reasoning_ms))}${metric('Longest stream gap',ms(r.max_stream_gap_ms))}${metric('Input / output tokens',`${r.load_point&&r.input_tokens==null?'not reported':number(r.input_tokens)} / ${r.load_point&&r.output_tokens==null?'not reported':number(r.output_tokens)}`,'Server-reported usage')}${metric('HTTP / finish reason',`${r.http_status ?? '—'} / ${r.finish_reason || '—'}`)}</dl></details><details class="request-reasoning"><summary>Reasoning output</summary><div class="body-output">${r.reasoning?`<pre>${escape(r.reasoning)}</pre>`:`<p class="body-not-retained">${retention==='all'?'No reasoning text was recorded.':'No reasoning body retained. This does not establish whether the model reasoned.'}</p>`}</div></details><p class="hint" style="margin-top:18px">${r.valid?'Valid non-empty completion':'Completion not validated'} · ${r.stream?'Streaming':'Non-streaming'} · ${r.stream_terminated?'Stream termination observed':'Stream termination not recorded'}</p></div><div class="dialog-footer"><button class="button" data-action="close-dialog">${$('#test-dialog')?.open?'Back to test details':'Close'}</button></div>`;
  if(!dialog.open)dialog.showModal();
}
function openBaselineDialog(){
  const report=state.detail.report,dialog=$('#baseline-dialog');dialog.className='baseline-form';dialog.innerHTML=`<form id="baseline-form"><div class="dialog-head"><div><div class="eyebrow">Explicit reference run</div><h2 id="baseline-title">Save baseline</h2></div><button type="button" class="button ghost" data-action="close-dialog" aria-label="Close save baseline">✕</button></div><div class="dialog-body"><p class="hint">Save this exact report for future comparisons. Its recorded outcome is ${escape(statusLabels[report.overall] || report.overall)}.</p><div class="fields">${field('baseline_name','Baseline name','',{required:true,full:true,placeholder:'e.g. before-runtime-upgrade'})}</div><p class="hint" style="margin-top:12px">Existing baselines are preserved unless you explicitly replace one.</p><div id="baseline-error" class="error-message" role="alert" hidden></div><label class="replace-row" id="replace-row" hidden><input name="replace" type="checkbox">Replace the existing baseline with this name</label></div><div class="dialog-footer"><button type="button" class="button" data-action="close-dialog">Cancel</button><button type="submit" class="button primary" id="save-baseline">Save baseline</button></div></form>`;dialog.showModal();}
// Closing a chooser does not cancel a comparison already being saved.
const pendingComparisons=new Set();
async function openComparisonDialog() {
  const id=state.detail?.id,version=state.routeVersion;
  if(!id||state.detail.state!=='completed')return;
  if(pendingComparisons.has(id)){toast('A comparison is still being saved for this run.');return;}
  try {
    const result=await api('/api/baselines');
    if(version!==state.routeVersion||state.detail?.id!==id||pendingComparisons.has(id))return;
    state.baselines=result.baselines||[];
    const choices=state.baselines.filter(item=>item.run_id!==id);
    const dialog=$('#comparison-dialog');
    dialog.className='baseline-form';
    dialog.innerHTML=`<form id="comparison-form" data-run-id="${escape(id)}"><div class="dialog-head"><h2 id="comparison-title">Compare with saved baseline</h2><button type="button" class="button ghost" data-action="close-dialog" aria-label="Close comparison">✕</button></div><div class="dialog-body"><p>Compare the recorded results. No new requests will be sent to your endpoint.</p>${choices.length?`<label for="saved-baseline-choice">Saved baseline</label><select id="saved-baseline-choice" name="baseline_id" required>${choices.map(item=>`<option value="${escape(item.id)}"${item.id===state.detail.baseline_id?' selected':''}>${escape(item.name)} · ${escape(date(item.saved_at))}</option>`).join('')}</select>`:'<p>No other saved baselines yet. Open a different completed run and choose “Save as reference baseline”, then return here.</p>'}<div id="comparison-error" class="error-message" role="alert" hidden></div></div><div class="dialog-footer"><button type="button" class="button" data-action="close-dialog">Cancel</button><button type="submit" class="button primary"${choices.length?'':' disabled'}>Compare saved results</button></div></form>`;
    dialog.showModal();
  }catch(error){if(version===state.routeVersion&&state.detail?.id===id)toast(error.message);}
}
async function compareSavedRun(event) {
  event.preventDefault();
  const form=event.target,id=form.dataset.runId,version=state.routeVersion;
  if(state.detail?.id!==id||pendingComparisons.has(id))return;
  const button=form.querySelector('[type="submit"]'),error=$('#comparison-error');
  pendingComparisons.add(id);
  button.disabled=true;error.hidden=true;
  try {
    const result=await api(`${runPath(id)}/compare`,{method:'POST',body:JSON.stringify({baseline_id:new FormData(form).get('baseline_id')})});
    if(version!==state.routeVersion||state.detail?.id!==id)return;
    const detail=await api(runPath(id));
    if(version!==state.routeVersion||state.detail?.id!==id)return;
    state.detail=detail;state.comparisonFilter=null;state.comparisonCheck='all';state.tab='baseline';
    reportView.performance=null;reportView.pending=false;reportView.lastFetch=0;reportView.sequence++;
    $('#comparison-dialog').close();
    chooseReportTab('results');renderDetail();
    const section=$('#report-baseline-reference');
    if(section){section.open=true;section.scrollIntoView?.({block:'start'});$('summary',section)?.focus({preventScroll:true});}
    toast(result.warning||'Comparison saved. No new requests were sent.');
  }catch(failure){if(version===state.routeVersion&&state.detail?.id===id){error.textContent=failure.message;error.hidden=false;}}
  finally{pendingComparisons.delete(id);button.disabled=false;}
}
async function saveBaseline(event){event.preventDefault();const form=event.target,data=new FormData(form),button=$('#save-baseline'),error=$('#baseline-error');button.disabled=true;error.hidden=true;try{await api('/api/baselines',{method:'POST',body:JSON.stringify({run_id:state.detail.id,name:String(data.get('baseline_name')).trim(),replace:data.has('replace')})});const result=await api('/api/baselines');state.baselines=result.baselines || [];$('#baseline-dialog').close();renderDetail();toast(`Saved as baseline “${String(data.get('baseline_name')).trim()}”. Open another completed run and choose Compare with baseline, or select it before a new run.`);}catch(e){error.textContent=e.message;error.hidden=false;if(e.status===409)$('#replace-row').hidden=false;}finally{button.disabled=false;}}
let stopConfirmationTimer=null,stopConfirmationEpoch=0;
function cancelStopConfirmationTimer({reset=false}={}) {
  if(stopConfirmationTimer!=null&&typeof clearTimeout==='function')clearTimeout(stopConfirmationTimer);
  stopConfirmationTimer=null;stopConfirmationEpoch++;
  if(reset||state.stopConfirmation?.id!==state.detail?.id||!activeState(state.detail))state.stopConfirmation=null;
}
function scheduleStopConfirmationReset() {
  const confirmation=state.stopConfirmation;if(!confirmation||typeof setTimeout!=='function')return;
  const epoch=stopConfirmationEpoch,version=state.routeVersion,id=state.detail.id,hash=typeof location==='undefined'?'':location.hash;
  stopConfirmationTimer=setTimeout(()=>{
    if(epoch!==stopConfirmationEpoch||version!==state.routeVersion||state.detail?.id!==id||state.stopConfirmation!==confirmation||!state.reportRouteActive||!activeState(state.detail)||(typeof location!=='undefined'&&location.hash!==hash))return;
    stopConfirmationTimer=null;state.stopConfirmation=null;renderDetail();
  },Math.max(0,confirmation.until-Date.now()));
}
async function confirmStop(){
  const id=state.detail?.id;if(!activeState(state.detail)||state.detail.stoppable===false||state.detail.state==='stopping')return;
  const current=state.stopConfirmation,now=Date.now();
  if(current?.id===id&&now<current.until){if(now-current.armedAt<300)return;cancelStopConfirmationTimer({reset:true});await stopRun();return;}
  state.stopConfirmation={id,armedAt:now,until:now+4000};renderDetail();
}

async function stopRun(){
  if(!activeState(state.detail)||state.detail.stoppable===false||state.detail.state==='stopping'||state.stopPending?.id===state.detail.id)return;
  const button=$('[data-action="stop"]'),id=state.detail.id,version=state.routeVersion,epoch=++detailEpoch;
  state.liveError=null;state.stopPending={id};button.disabled=true;button.textContent='Stopping…';
  try{
    const result=await api(`${runPath(id)}/cancel`,{method:'POST',body:'{}'});
    if(version!==state.routeVersion||state.detail?.id!==id||epoch!==detailEpoch)return;
    state.stopPending=null;
    if(!activeState(state.detail))return;
    if(state.detail.state!=='stopping')state.detail.state=result.state || 'stopping';
    renderDetail();schedulePoll(pollDetail,version,500);
  }catch(error){
    if(version!==state.routeVersion||state.detail?.id!==id||epoch!==detailEpoch)return;
    state.stopPending=null;
    if(state.detail.state==='stopping'||!activeState(state.detail))return;
    state.liveError={id,message:error.message,sequence:detailSequence};
    renderDetail();
  }finally{
    if(epoch===detailEpoch&&state.stopPending?.id===id)state.stopPending=null;
  }
}
// "New run" starts setup at step 1, even when the form is already open on a later step.
async function startNewRun(){
  const version=state.routeVersion,revision=state.configRevision;
  if($('#new-run-form'))captureFormDraft();
  try{
    const result=await api('/api/config/draft',{method:'POST',body:JSON.stringify(state.config || state.bootstrap.config)});
    if(version!==state.routeVersion||revision!==state.configRevision)return;
    state.config=result.config;replaceMigrationNotes(result.migration_notes);
  }catch(error){toast(error.message);return;}
  state.setupStep=1;state.returnToReview=false;state.openTest=null;configChanged();
  if(location.hash!=='#new'){location.hash='#new';return;}
  renderNew();enterRoute();window.scrollTo?.({top:0,left:0,behavior:'instant'});
}
async function rerun(){
  const version=state.routeVersion;let source=state.detail.report?.manifest?.config || state.detail.config;
  if(!source){toast('No configuration was saved for this run.');return;}
  let notes=[];
  if(source.config_version==null || 'concurrent_workloads' in source || 'buckets' in source || source.test_options?.concurrency || (source.checks || []).some(id=>['fairness','buckets','mixed','token_stress'].includes(id))){
    try{const result=await api('/api/config/draft',{method:'POST',body:JSON.stringify(source)});if(version!==state.routeVersion)return;source=result.config;notes=result.migration_notes;}
    catch(error){toast(error.message);return;}
  }
  replaceMigrationNotes(notes);state.config=clone(source);state.autoModel=null;state.setupStep=1;invalidateModelDiscovery();state.datasetSelection=[];state.config.restart_target=null;state.formMode='normal';state.formRestartTarget=null;state.jsonDraft=null;state.jsonBase=null;state.configView='form';configChanged();state.prefillBaseline=state.detail.baseline_id || '';location.hash='#new';
}
function portableConfig(payload) {return {...payload.config,restart_target:payload.mode==='cold-start'?payload.restart_target:null};}
function hasJsonChanges() {return state.jsonDraft!==null&&state.jsonDraft!==state.jsonBase;}
function assertAppliedConfig() {if(hasJsonChanges())throw new Error('Apply or discard the JSON changes first.');}
function captureFormDraft() {
  const form=$('#new-run-form');if(!form)return;
  state.prefillBaseline=form.elements.baseline_id.value;
  state.formMode=form.elements.mode.value;state.formRestartTarget=form.elements.restart_target.value;
  try{state.config=readForm(true).config;}catch{}
}
async function restoreDraft() {
  try{
    const draft=JSON.parse(sessionStorage.getItem('giraffe-draft-v1'));
    const legacy=draft?.config?.config_version==null;
    if(draft&&draft.config?.targets?.length&&(legacy||sameConfig(draft.bootstrap,state.bootstrap.config))){
      let config=draft.config,notes=[];
      if(legacy){const result=await api('/api/config/draft',{method:'POST',body:JSON.stringify(config)});config=result.config;notes=result.migration_notes || [];}
      state.config=config;state.dismissedMigrationNotes=draft.dismissedMigrationNotes || [];
      state.migrationNotes=[...new Set([...(draft.migrationNotes || []),...state.dismissedMigrationNotes,...notes])];
    }else sessionStorage.removeItem('giraffe-draft-v1');
  }catch{}
}
function saveDraft() {
  if(!state.bootstrap||!state.config)return;
  captureFormDraft();
  try{sessionStorage.setItem('giraffe-draft-v1',JSON.stringify({bootstrap:state.bootstrap.config,config:state.config,migrationNotes:state.migrationNotes,dismissedMigrationNotes:state.dismissedMigrationNotes}));}catch{}
}
function syncConfigView() {
  const dirty=hasJsonChanges();
  for(const view of ['form','json']){
    const active=state.configView===view,tab=$(`#config-tab-${view}`),panel=$(`#config-panel-${view}`);
    tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;tab.classList.toggle('selected',active);panel.hidden=!active;
  }
  $('#config-panel-form').inert=dirty;$('#execution-settings').inert=dirty;
  const budgetButton=$('[data-action="use-request-budget"]');if(budgetButton)budgetButton.disabled=dirty||!currentRequestPlan();
  $('#config-panel-form').classList.toggle('config-locked',dirty);
  $('#config-draft-notice').hidden=!dirty;
  $('#json-status').textContent=dirty?'Unapplied changes':'In sync with the form';
  syncSetupStep();renderModelDiscovery();syncInlineErrors();
}
function switchConfigView(view) {
  if(view===state.configView)return;
  if(view==='json'&&!hasJsonChanges()){
    captureFormDraft();state.jsonDraft=JSON.stringify(portableConfig(readForm(true)),null,2);state.jsonBase=state.jsonDraft;$('#config-editor').value=state.jsonDraft;
  }
  state.configView=view;if(view==='json'&&$('.configuration-tools'))$('.configuration-tools').open=true;syncConfigView();
  if(view==='json'){
    $('#config-panel-json').scrollIntoView?.({block:'start'});$('#config-editor').focus({preventScroll:true});
  }else{
    const dirty=hasJsonChanges(),panel=dirty?$('#config-draft-notice'):$(`[data-setup-panel="${state.setupStep}"]`) || $('#config-panel-form');
    panel.scrollIntoView?.({block:'start'});
    const heading=!dirty?$(`[data-setup-panel="${state.setupStep}"] h2`):null;
    if(heading){heading.setAttribute('tabindex','-1');heading.focus({preventScroll:true});}
    else $(`#config-tab-${view}`).focus({preventScroll:true});
  }
}
function configError(error) {const el=$('#config-error');if(el){el.textContent=error?.message || '';el.hidden=!error;}}
function discardConfig() {
  configChanged();state.jsonDraft=state.jsonBase;$('#config-editor').value=state.jsonDraft;configError();syncConfigView();
  scheduleRequestPlan();
}
async function applyConfig(text=state.jsonDraft,{importing=false}={}) {
  if(state.configBusy)return;
  const version=state.routeVersion,revision=state.configRevision;
  state.configBusy=true;configError();
  try{
    if(importing)assertAppliedConfig();
    const parsed=JSON.parse(text);
    const localErrors=positiveBoundErrors(parsed,state.bootstrap.limits,state.bootstrap.cap_explanations);
    if(localErrors.length)throw Object.assign(new Error(localErrors[0].msg),{errors:localErrors});
    const result=await api('/api/config/draft',{method:'POST',body:JSON.stringify(parsed)});
    if(version!==state.routeVersion||revision!==state.configRevision)return;
    captureFormDraft();
    replaceMigrationNotes(result.migration_notes);
    state.config=result.config;state.autoModel=null;invalidateModelDiscovery();state.datasetSelection=[];
    // An explicit restart target in imported/edited JSON remains visible and intentional.
    state.formMode=result.config.restart_target?'cold-start':'normal';state.formRestartTarget=result.config.restart_target;
    state.config.restart_target=null;
    state.jsonDraft=null;state.jsonBase=null;configChanged();
    renderNew();toast(importing?'Configuration imported.':'Configuration applied.');
  }catch(error){if(version===state.routeVersion&&revision===state.configRevision){configError(error);if(!importing&&text===state.jsonDraft){state.jsonValidation={draft:text,revision,route:version,errors:error.errors?.length?error.errors:[{fields:['config'],msg:error.message}]};renderRequestPreview();}}}
  finally{state.configBusy=false;}
}
async function importConfig(file) {
  if(!file)return;
  const version=state.routeVersion,revision=state.configRevision;
  try{assertAppliedConfig();const text=await file.text();if(version!==state.routeVersion||revision!==state.configRevision)return;await applyConfig(text,{importing:true});}
  catch(error){if(version===state.routeVersion)configError(error);}
}
async function validatedSnapshot({draft=false}={}) {
  assertAppliedConfig();
  const version=state.routeVersion,revision=state.configRevision,payload=clone(readForm(draft));
  if(!draft&&payload.config.request_source==='request_pack'&&!payload.config.request_pack&&
      !payload.config.checks.some(id=>['correctness','load'].includes(id)))payload.config.request_source='builtin';
  if(!draft){
    const missing=payload.config.targets.findIndex(target=>!target.model);
    if(missing!==-1){
      switchConfigView(missing===0?'form':'json');
      if(missing===0){revealSetupStep(1);$('#new-run-form').elements.model.focus();}else $('#config-editor').focus();
      throw new Error(`Enter a model for target "${payload.config.targets[missing].name}" before running or copying a CLI command. You can still apply or export this draft.`);
    }
  }
  try{const localErrors=localSettingErrors(payload.config);if(localErrors.length)throw Object.assign(new Error(localErrors[0].msg),{errors:localErrors});const result=await api(draft?'/api/config/draft':'/api/config/validate',{method:'POST',body:JSON.stringify(payload.config)});if(version===state.routeVersion&&revision===state.configRevision)acceptMigrationNotes(result.migration_notes);payload.config=result.config;}
  catch(error){if(version===state.routeVersion&&revision===state.configRevision&&error.errors?.length){state.requestPreview={status:'error',revision,route:version,error:error.message,errors:error.errors};renderRequestPreview();focusFirstSettingError(error);}throw error;}
  if(version!==state.routeVersion||revision!==state.configRevision)throw new Error('Configuration changed during validation. Try again with the current settings.');
  assertAppliedConfig();if(!draft&&!tokenizerSetupReady())throw new Error(currentTokenizerSelection()?.message || 'Wait for the Token ID check on the connection page before continuing.');
  return {...payload,migration_notes:[...(state.migrationNotes || [])]};
}
async function exportConfig() {
  configError();
  try{
    const payload=await validatedSnapshot({draft:true}),blob=new Blob([JSON.stringify(portableConfig(payload),null,2)+'\n'],{type:'application/json'});
    const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download='giraffe-config.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    toast('Configuration exported.');
  }catch(error){configError(error);}
}
async function copyCli() {
  configError();$('#cli-fallback').hidden=true;
  const version=state.routeVersion,revision=state.configRevision;
  try{
    const payload=await validatedSnapshot(),result=await api('/api/config/cli',{method:'POST',body:JSON.stringify(payload)});
    if(version!==state.routeVersion||revision!==state.configRevision)throw new Error('Configuration changed. Copy the command again.');
    try{await navigator.clipboard.writeText(result.command);toast('CLI command copied.');}
    catch{$('#cli-fallback').hidden=false;$('#cli-command').value=result.command;$('#cli-command').focus();$('#cli-command').select();}
  }catch(error){if(version===state.routeVersion)configError(error);}
}

document.addEventListener('click',async(event)=>{
  const heroAction=event.target.closest('[data-action]')?.dataset.action,guard=state.heroActionGuard;
  if(heroAction==='reconnect'){if(!state.accessExpired)location.reload();return;}
  if(guard&&guard.id===state.detail?.id&&Date.now()<guard.until&&['rerun','baseline-dialog','compare-run','show-failing','show-inconclusive','show-comparison'].includes(heroAction)){event.preventDefault?.();return;}
  if(event.target.closest('.skip-link')){event.preventDefault();const main=$('#main');main.setAttribute('tabindex','-1');main.focus({preventScroll:true});main.scrollIntoView({block:'start'});return;}
  if(handleConditionClick(event))return;
  if(handlePerformanceClick(event))return;
  const reportTab=event.target.closest('[data-report-tab]');if(reportTab){event.preventDefault();chooseReportTab(reportTab.dataset.reportTab,{focus:reportTab.getAttribute?.('role')==='tab'});return;}
  const section=event.target.closest('[data-report-section]');if(section){event.preventDefault();if(typeof location!=='undefined')location.hash=section.getAttribute('href');revealReportSection(section.dataset.reportSection);return;}

  const newRun=event.target.closest('a[href="#new"]');if(newRun&&!event.metaKey&&!event.ctrlKey&&!event.shiftKey){event.preventDefault();startNewRun();return;}
  const liveSpot=event.target.closest('[data-live-slot]');if(liveSpot){openLiveRequest(Number(liveSpot.dataset.liveSlot));return;}
  const runFilter=event.target.closest('[data-run-filter]');if(runFilter){state.filter=runFilter.dataset.runFilter;renderRuns();$(`[data-run-filter="${state.filter}"]`)?.focus();return;}
  const command=event.target.closest('[data-command]');if(command){$('#command-dialog').close();commandList[Number(command.dataset.command)]?.action();return;}
  const shellAction=event.target.closest('[data-action]')?.dataset.action;
  if(shellAction==='theme'){setTheme(document.documentElement.dataset.theme==='light'?'dark':'light');return;}
  if(shellAction==='commands'){openCommands();return;}
  if(shellAction==='shortcuts'){openCommands(true);return;}
  if(shellAction==='toggle-rail'){toggleRail();return;}
  if(shellAction==='close-commands'){$('#command-dialog').close();return;}
  if(shellAction==='mosaic-prev'||shellAction==='mosaic-next'){state.mosaicPage=Math.max(0,(state.mosaicPage || 0)+(shellAction==='mosaic-next'?1:-1));if(activeState(state.detail))state.mosaicFollow=state.mosaicPage===state.mosaicLivePage;renderDetail();focusMosaicStart();return;}
  if(shellAction==='mosaic-live'){state.mosaicFollow=true;state.mosaicPage=state.mosaicLivePage;renderDetail();focusMosaicStart();return;}
  const configTab=event.target.closest('[data-config-tab]');if(configTab){switchConfigView(configTab.dataset.configTab);return;}
  const setupStep=event.target.closest('[data-setup-step]');if(setupStep){state.returnToReview=Boolean(setupStep.dataset.reviewEdit);navigateSetup(Number(setupStep.dataset.setupStep));return;}
  const tab=event.target.closest('[data-tab]');if(tab){event.preventDefault?.();state.tab=tab.dataset.tab;chooseReportTab('results',{anchor:false});revealReportSection(state.tab);location.hash=`${linkRun(state.detail.id)}/results/${state.tab==='baseline'?'comparison':state.tab}`;return;}
  const check=event.target.closest('[data-check]');if(check){
    // Keep the user's place: the list keeps its scroll, the clicked check stays marked, and the page only moves enough to show its details.
    const list=$('.check-list'),listScroll=list?.scrollTop || 0;
    state.selectedCheck=check.dataset.check;state.failingRequestsOnly=false;state.checkNavigatorOpen=false;patchHTML($('#result-content'),renderChecks(state.detail.report||summaryReport(state.detail)));
    const disclosure=$('.check-detail')?.closest('details');if(disclosure)disclosure.open=true;
    const nextList=$('.check-list');if(nextList)nextList.scrollTop=listScroll;
    const detail=$('.check-detail'),top=detail?.getBoundingClientRect?.().top;
    if(Number.isFinite(top)&&(top<64||top>window.innerHeight*.6))window.scrollTo?.({top:window.scrollY+top-76,behavior:'smooth'});
    const heading=$('.check-detail h2');heading?.setAttribute('tabindex','-1');heading?.focus({preventScroll:true});return;}
  const request=event.target.closest('[data-request]');if(request){openRequest(request.dataset.request);return;}
  const action=event.target.closest('[data-action]')?.dataset.action;
  if(action==='refresh-performance'){loadPerformance({force:true});return;}
  if(action==='back-to-test'){closeRequestEvidence();return;}
  if(action==='storage-dialog'){openStorageDialog();return;}
  if(action==='close-storage'){$('#storage-dialog').close();state.cleanup=null;return;}
  if(action==='cleanup-confirm'){await confirmCleanup();return;}
  if(action==='keep-run'){await toggleKept();return;}
  if(action==='request-input'){const heading=$('#request-input-messages');heading?.scrollIntoView({block:'start'});heading?.focus({preventScroll:true});return;}
  if(action==='edit-setting'){try{focusSetting(event.target.closest('[data-setting-path]').dataset.settingPath);}catch(error){configError(error);}return;}
  if(action==='dismiss-migration-notes'){state.dismissedMigrationNotes=[...new Set([...(state.dismissedMigrationNotes || []),...(state.migrationNotes || [])])];acceptMigrationNotes();renderRequestPlan();saveDraft();return;}
  if(action==='load-select-grid'){selectLoadShapes(event.target.closest('[data-load-grid]').dataset.loadGrid);return;}
  const loadCollapse=event.target.closest('[data-load-collapse]');if(loadCollapse){const type=loadCollapse.dataset.loadCollapse;setLoadPanel(type,$(`#load-panel-${type}`).hidden);return;}
  if(action==='toggle-checks'){state.checkNavigatorOpen=!state.checkNavigatorOpen;$('#result-content').innerHTML=renderChecks(state.detail.report);$('[data-action="toggle-checks"]')?.focus({preventScroll:true});return;}
  if(action==='check-intent'){browseCheckIntent(event.target.closest('[data-category]').dataset.category);return;}
  if(action==='runtime-shortcut'){useRuntimeShortcut(event.target.closest('[data-runtime]').dataset.runtime);return;}
  if(action==='recent-endpoint'){const chip=event.target.closest('[data-url]');useRecentEndpoint(chip.dataset.url,chip.dataset.model);return;}
  if(action==='catalogue-category'){state.catalogueCategory=event.target.closest('[data-category]').dataset.category;syncTestControls();return;}
  if(action==='catalogue-filter'){state.catalogueFilter=event.target.closest('[data-catalogue-filter]').dataset.catalogueFilter;syncTestControls();return;}
  if(action==='reset-profile'){try{assertAppliedConfig();const key=event.target.closest('[data-profile]').dataset.profile;captureFormDraft();state.config[key]=null;configChanged();renderNew('custom');$(`[data-profile="${key}"]`)?.focus();}catch(error){configError(error);}return;}
  if(action==='quick-checks'){try{assertAppliedConfig();captureFormDraft();state.config.checks=clone(state.bootstrap.config.checks);state.config.structured_json=false;state.config.metrics=false;state.config.tool_calling=false;state.catalogueFilter='selected';state.catalogueCategory='all';state.catalogueSearch='';state.openTest=null;configChanged();renderNew('custom');$('[data-catalogue-filter="selected"]')?.focus();}catch(error){configError(error);}return;}
  if(action==='setup-next'){navigateSetup(state.configView==='json'?state.setupStep:state.returnToReview?3:state.setupStep+1);return;}
  if(action==='setup-back'){navigateSetup(state.setupStep-1);return;}
  if(action==='test-connection'){await testConnection();return;}
  if(action==='clear-api-key'){clearAPIKey();invalidateModelDiscovery();captureFormDraft();configChanged();renderNew();$('#connection-auth').open=true;$('#new-run-form').elements.api_key.focus();return;}
  const configure=event.target.closest('[data-configure-test]')?.dataset.configureTest;
  if(configure){toggleTestEditor(configure);return;}
  if(state.openTest&&event.target.id==='new-run-form'&&window.matchMedia?.('(min-width: 1180px)').matches){toggleTestEditor(state.openTest,true);return;}
  const closeTest=event.target.closest('[data-close-test]')?.dataset.closeTest;
  if(closeTest){toggleTestEditor(closeTest,true);return;}
  const done=event.target.closest('[data-done-test]')?.dataset.doneTest;
  if(done){toggleTestEditor(done,true);return;}
  const reset=event.target.closest('[data-reset-test]')?.dataset.resetTest;
  if(reset==='load'){captureFormDraft();replaceMigrationNotes();delete state.config.load_test;state.loadPanels={};delete state.config.test_options?.load;state.jsonDraft=null;state.jsonBase=null;configChanged();renderNew();return;}
  if(reset){configChanged();if(state.config.test_options)delete state.config.test_options[reset];const form=$('#new-run-form');form.elements[`test_mode_${reset}`].value='default';refreshInheritedFields(reset);syncTestControls();updateBounds();return;}
  if(action==='use-check-concurrency'){
    try{
      assertAppliedConfig();
      const button=event.target.closest('[data-concurrency-check]'),check=button?.dataset.concurrencyCheck,value=Number(button?.dataset.concurrency);
      const offered=(currentRequestPlan()?.concurrency_warnings||[]).find(issue=>inheritedConcurrencyNotice(issue)&&issue.check===check&&issue.effective_concurrency===value);
      if(!offered)throw new Error('The request plan changed. Review the updated concurrency before applying it.');
      if(check==='correctness'&&state.config.request_source==='request_pack')throw new Error('Request-pack correctness uses default_concurrency; per-check overrides are unavailable.');
      captureFormDraft();const path=concurrencySetting(check),parts=path.split('.');let target=state.config;
      const setting=check==='consistency'?profileDefaults('consistency',state.config).levels:check==='load'?loadSettings(state.config).patterns.concurrent.levels:value;
      for(const part of parts.slice(0,-1))target=target[part] ||= {};
      target[parts.at(-1)]=clone(setting);
      state.jsonDraft=null;state.jsonBase=null;configChanged();renderNew();focusSetting(path);
    }catch(error){configError(error);}return;
  }
  if(action==='use-setting-correction'){
    try{
      if(hasJsonChanges()){
        const button=event.target.closest('[data-setting-value]'),field=button.dataset.settingPath,value=Number(button.dataset.settingValue);
        const issue=currentSettingErrors().find(error=>error.actions?.some(action=>action.field===field&&action.value===value));
        if(!issue)throw new Error('Apply or discard JSON changes, or validate the current JSON before using this correction.');
        const candidate=JSON.parse(state.jsonDraft);
        if(field!=='traffic.duration_seconds'||!Number.isFinite(value)||value<=0||!issue||candidate.traffic?.duration_seconds!==issue.values?.[field]||JSON.stringify(candidate.traffic?.rates)!==JSON.stringify(issue.values?.['traffic.rates']))throw new Error('The request plan changed. Review the updated correction before applying it.');
        candidate.traffic.duration_seconds=value;state.jsonDraft=JSON.stringify(candidate,null,2);$('#config-editor').value=state.jsonDraft;
        configChanged();await applyConfig();return;
      }
      assertAppliedConfig();
      const button=event.target.closest('[data-setting-value]'),field=button.dataset.settingPath,value=Number(button.dataset.settingValue);
      const issue=currentSettingErrors().find(error=>error.actions?.some(item=>item.field===field&&item.value===value));
      const input=$('#new-run-form')?.elements?.[settingDestination(field).name];
      if(field!=='traffic.duration_seconds'||!Number.isFinite(value)||value<=0||!input||!issue||
          Number(input.value)!==Number(issue.values?.[field])||
          issue.values?.['traffic.rates']&&JSON.stringify(readForm(true).config.traffic?.rates)!==JSON.stringify(issue.values['traffic.rates']))throw new Error('The request plan changed. Review the updated correction before applying it.');
      input.value=value;
      const mode=$('#new-run-form')?.elements?.traffic_mode;if(mode)mode.value='custom';
      configChanged();updateBounds();
      if(state.configView==='json'){
        state.jsonDraft=JSON.stringify(portableConfig(readForm(true)),null,2);state.jsonBase=state.jsonDraft;
        $('#config-editor').value=state.jsonDraft;
      }
    }catch(error){configError(error);}
    return;
  }
  if(action==='use-request-budget'||action==='use-time-budget'){
    try{
      assertAppliedConfig();
      const plan=currentRequestPlan();
      if(!plan)throw new Error('Wait for the current request plan before changing the budget.');
      const field=action==='use-request-budget'?'max_requests':'max_duration_seconds';
      const budget=Number(event.target.closest('[data-budget]').dataset.budget),form=$('#new-run-form'),input=form.elements[field];
      const offered=[...(plan.request_warnings||[]),...(plan.time_warnings||[])].some(warning=>warning.actions?.some(item=>item.field===field&&item.value===budget));
      const factor=field==='max_duration_seconds'?({s:1,min:60,h:3600}[form.elements.max_duration_unit?.value||'s']):1;
      const current=Number(input.value)*factor;
      if(!offered||current!==Number(field==='max_requests'?plan.request_limit:(state.requestPreview?.config||state.config).max_duration_seconds))throw new Error('The request plan changed. Review the updated budget before applying it.');
      if(Number.isFinite(budget)&&budget>current){
        input.value=budget/factor;input.disabled=false;input.required=true;
        state.limitValues ||= {};state.limitValues[field]=input.value;
        const enabled=form.elements[`${field}_enabled`];if(enabled)enabled.checked=true;
        configChanged();updateBounds();
        if(state.configView==='json'){
          state.jsonDraft=JSON.stringify(portableConfig(readForm(true)),null,2);state.jsonBase=state.jsonDraft;
          $('#config-editor').value=state.jsonDraft;
        }
      }
    }catch(error){configError(error);}
    return;
  }
  if(action==='edit-output-cap'){
    try{assertAppliedConfig();focusSetting('max_output_tokens');}catch(error){configError(error);}
    return;
  }
  if(action==='edit-run-limits'){
    try{assertAppliedConfig();state.returnToReview=state.setupStep===3;captureFormDraft();revealSetupStep(2);updateBounds();$('#shared-settings').open=true;($('#new-run-form').elements.max_requests_enabled||$('#new-run-form').elements.max_requests).focus();}catch(error){configError(error);}
    return;
  }
  if(action==='edit-load'){
    try{
      assertAppliedConfig();revealSetupStep(2);
      const checks=readForm(true).config.checks,id=checks.includes('load')?'load':null;
      if(id){revealCatalogueCheck(id);$(`[data-test-card="${id}"]`)?.scrollIntoView({block:'start'});$(`[data-configure-test="${id}"]`)?.focus();}
      else{state.catalogueFilter='selected';state.catalogueCategory='all';state.catalogueSearch='';state.openTest=null;const search=$('#check-search');if(search)search.value='';syncTestControls();$('[data-catalogue-filter="selected"]')?.focus();}
    }catch(error){configError(error);}return;
  }
  if(['add-session-workload','add-session-turn','remove-session-turn'].includes(action)){editSessionWorkload(action,event.target.closest('[data-session-index]')?.dataset.sessionIndex);return;}
  if(action==='toggle-available-tests'){state.showAvailableTests=!state.showAvailableTests;syncTestControls();return;}
  if(action==='select-group'){
    configChanged();const category=event.target.closest('[data-category]').dataset.category;
    const inputs=$$(`[data-catalogue-group="${category}"] input[name="check"]`),select=!inputs.every(input=>input.checked);
    for(const input of inputs)input.checked=select;captureFormDraft();renderNew('custom');
    $(`[data-action="select-group"][data-category="${category}"]`)?.focus({preventScroll:true});return;
  }
  if(action==='select-tests'||action==='clear-tests'){configChanged();for(const input of $$('#new-run-form input[name="check"]'))input.checked=action==='select-tests';captureFormDraft();renderNew('custom');$(`[data-action="${action}"]`)?.focus({preventScroll:true});return;}
  if(action==='reset-traffic'){configChanged();const form=$('#new-run-form');form.elements.traffic_mode.value='default';const defaults=state.bootstrap.config.traffic;for(const [key,value] of Object.entries(defaults)){if(key==='mix'){for(const [kind,weight] of Object.entries(value))form.elements[`traffic_mix_${kind}`].value=weight;}else form.elements[`traffic_${key}`].value=Array.isArray(value)?value.join(', '):value ?? '';}syncTestControls();updateBounds();return;}
  if(action==='compare-saved'){await openComparisonDialog();return;}
  if(action==='compare-run'){const saved=(state.baselines || []).find(item=>item.run_id===state.detail?.id);await rerun();if(saved)state.prefillBaseline=saved.id;return;}
  if(action==='choose-baseline'){const options=$('#execution-settings');if(options){options.open=true;const select=$('#field-baseline_id');select?.focus();select?.scrollIntoView?.({block:'center'});}return;}
  if(action==='retry')route();if(action==='clear-filter'){state.filter='all';state.historySearch='';renderRuns();}
  if(action==='stop')await confirmStop();if(action==='show-failing')showFailingRequests();if(action==='show-inconclusive'){chooseReportTab('results',{anchor:false});const findings=$('#failing-requests');findings?.scrollIntoView?.({block:'start'});findings?.focus({preventScroll:true});}if(action==='show-comparison'){chooseReportTab('results',{anchor:false});revealReportSection('baseline');location.hash=`${linkRun(state.detail.id)}/results/baseline`;}if(action==='rerun')await rerun();if(action==='baseline-dialog')openBaselineDialog();
  if(action==='close-dialog'){if(event.target.closest('dialog')?.id==='request-dialog')closeRequestEvidence();else event.target.closest('dialog').close();}if(action==='apply-config')await applyConfig();
  if(action==='discard-config')discardConfig();
  if(action==='import-config'){try{assertAppliedConfig();$('#config-file').click();}catch(error){configError(error);}}
  if(action==='import-pack'){try{assertAppliedConfig();$('#request-pack-file').click();}catch(error){configError(error);}}
  if(action==='remove-pack')changeRequestSource('builtin',{remove:true});
  const caseIndex=Number(event.target.closest('[data-case-index]')?.dataset.caseIndex);
  if(action==='add-example')openExampleEditor();
  if(action==='edit-example')openExampleEditor(caseIndex);
  if(action==='remove-example')await removeExample(caseIndex);
  if(action==='sample-dataset')await sampleDataset();
  if(action==='apply-answer-check')await applyAnswerCheck();
  if(action==='preview-dataset-mapping')await previewDatasetMapping();
  if(action==='try-example')openTryExample(caseIndex,event.target.closest('[data-try-target]')?.dataset.tryTarget);
  if(action==='close-dataset')closeDatasetDialog();
  if(action==='add-message'||action==='remove-message'){
    const messages=readExampleMessages();
    if(action==='add-message')messages.push({role:'user',content:''});
    else if(messages.length>1)messages.splice(Number(event.target.closest('[data-message-index]').dataset.messageIndex),1);
    $('#example-messages').innerHTML=renderExampleMessages(messages);
    if(action==='add-message')$(`[name="message_${messages.length-1}_content"]`).focus();
  }
  if(action==='configure-evaluator')configureEvaluator();
  if(action==='add-judge')changeJudge('add');
  if(action==='remove-judge')changeJudge('remove',Number(event.target.closest('[data-judge-index]').dataset.judgeIndex));
  if(action==='preview-pack'){try{await refreshRequestPreview();}catch(error){configError(error);}}
  if(action==='select-pack-tests'){revealSetupStep(2);revealCatalogueCheck('correctness');$('[data-test-card="correctness"]')?.scrollIntoView({block:'start'});$('#test-selection input[value="correctness"]').focus();}
  if(action==='export-config')await exportConfig();
  if(action==='copy-cli')await copyCli();
});
document.addEventListener('submit',event=>{if(event.target.id==='cleanup-form')previewCleanup(event);if(event.target.id==='new-run-form')startRun(event);if(event.target.id==='baseline-form')saveBaseline(event);if(event.target.id==='comparison-form')compareSavedRun(event);if(event.target.id==='dataset-example-form')saveExample(event);if(event.target.id==='dataset-import-form')useImportedDataset(event);if(event.target.id==='dataset-try-form')tryDatasetExample(event);});
document.addEventListener('input',event=>{
  if(state.cleanup&&event.target.closest('#cleanup-form')){invalidateCleanup();return;}
  if(event.target.id==='command-search'){const query=event.target.value.toLowerCase();for(const item of $$('.command-item'))item.hidden=!item.textContent.toLowerCase().includes(query);for(const group of $$('.command-group'))group.hidden=!$$('.command-item',group).some(item=>!item.hidden);return;}

  if(event.target.id==='run-search'){state.historySearch=event.target.value;renderRuns();return;}
  if(event.target.id==='check-search'){state.catalogueSearch=event.target.value;syncTestControls();return;}
  if(!event.target.closest('#new-run-form')||event.target.matches('[data-dataset-select], #dataset-select-all, #dataset-bulk-scorer'))return;
  event.target.removeAttribute?.('data-setting-highlight');
  if(['url','model','name'].includes(event.target.name))event.target.setCustomValidity?.('');
  if(['url','api_key_env'].includes(event.target.name))clearAPIKey();
  if(event.target.name==='api_key'){
    const form=$('#new-run-form'),key=event.target.value.trim();
    sessionAPIKey=key?{url:form.elements.url.value.trim(),key}:null;
    if(key&&form.elements.api_key_env)form.elements.api_key_env.value='';
  }
  if(['url','api_key_env','api_key'].includes(event.target.name))updateAPIKeySummary();
  if(['url','api_key_env','api_key'].includes(event.target.name)&&state.autoModel&&$('#new-run-form').elements.model.value===state.autoModel){$('#new-run-form').elements.model.value='';state.autoModel=null;}
  if(event.target.name==='model')state.autoModel=null;
  if(['url','model','api_key_env','api_key'].includes(event.target.name))invalidateModelDiscovery();
  if(event.target.name==='tokenizer_custom')scheduleTokenizerSelection();
  if(['max_requests','max_duration_seconds','concurrency','max_memory_mb'].includes(event.target.name)){state.limitValues ||= {};state.limitValues[event.target.name]=event.target.value;}
  if(event.target.id==='config-editor')state.jsonDraft=event.target.value;
  if(event.target.name==='load_patterns_concurrent_levels')event.target.dataset.inherited='false';
  configChanged();$('#cli-fallback').hidden=true;
  if(event.target.id==='config-editor'){syncConfigView();return;}
  if(event.target.closest('[data-profile-settings]'))editProfileField(event.target);
  else if(event.target.closest('[data-test-settings]')||event.target.closest('[data-traffic-settings]'))editTestField(event.target);
  if(['traffic_rates','traffic_duration_seconds'].includes(event.target.name))$('#new-run-form').elements.traffic_mode.value='custom';
  if(numericFields.some(([name])=>name===event.target.name)||event.target.name==='samples'||event.target.name.startsWith('limit_'))for(const check of state.bootstrap.checks)refreshInheritedFields(check.id,true);
  updateBounds();
});
document.addEventListener('change',event=>{
  const comparisonFilter=event.target.dataset?.comparisonFilter;
  if(['outcome','check'].includes(comparisonFilter)){
    const container=event.target.closest('[data-comparison-scope]'),report=state.detail?.report;if(!container||!report)return;
    const scope=container.dataset.comparisonScope;
    if(comparisonFilter==='outcome')state.comparisonFilter=event.target.value;else state.comparisonCheck=event.target.value;
    for(const content of $$('[data-comparison-scope]'))content.innerHTML=renderComparison(report,{scope:content.dataset.comparisonScope});
    syncComparisonFilters();$(`[data-comparison-scope="${scope}"] [data-comparison-filter="${comparisonFilter}"]`)?.focus({preventScroll:true});return;
  }
  if(event.target.id==='results-section-jump'){
    location.hash=`${linkRun(state.detail.id)}/results/${event.target.value}`;
    scrollReportSection(event.target.value,'results');return;
  }
  if(handleConditionChange(event))return;
  if(handlePerformanceChange(event))return;
  if(event.target.id==='performance-target'||event.target.id==='performance-window'){
    if(event.target.id==='performance-target'){reportView.target=event.target.value;reportView.window=null;}else reportView.window=event.target.value;
    loadPerformance({force:true});return;
  }

  if(event.target.matches('[data-dataset-select], #dataset-select-all')){
    if(event.target.id==='dataset-select-all')state.datasetSelection=event.target.checked?datasetCases().map(item=>item.fixture_id):[];
    else {const selected=new Set(state.datasetSelection);if(event.target.checked)selected.add(event.target.dataset.datasetSelect);else selected.delete(event.target.dataset.datasetSelect);state.datasetSelection=[...selected];}
    const all=$('#dataset-select-all');if(all)all.checked=datasetCases().every(item=>state.datasetSelection.includes(item.fixture_id));
    for(const input of $$('[data-dataset-select]'))input.checked=state.datasetSelection.includes(input.dataset.datasetSelect);
    const section=$('.answer-checks');if(section){captureFormDraft();section.outerHTML=renderAnswerChecks(state.config);}return;
  }
  if(event.target.dataset?.limitToggle){toggleRunLimit(event.target.dataset.limitToggle,event.target.checked);configChanged();for(const check of state.bootstrap.checks)refreshInheritedFields(check.id,true);updateBounds();return;}
  if(event.target.name==='max_duration_unit'){state.timeUnit=event.target.value;const input=$('#new-run-form').elements.max_duration_seconds,bounds=settingBounds('max_duration_seconds'),factor=event.target.value==='min'?60:event.target.value==='h'?3600:1;if(bounds.exclusiveMinimum!=null)input.min=String(bounds.exclusiveMinimum/factor);configChanged();updateBounds();return;}
  if(event.target.id==='connection-model-choice'){
    const input=$('#new-run-form').elements.model,value=event.target.value;
    input.value=value==='__manual__'?'':value;input.setCustomValidity('');state.autoModel=value==='__manual__'?null:value;
    // Keep the discovered choices while invalidating any earlier verification.
    const models=state.modelDiscovery?.models || [];
    state.modelDiscoverySequence++;state.modelDiscovery={key:modelDiscoveryKey(),route:state.routeVersion,status:'needs_model',models,message:'Model selected. Test the connection, or continue to choose checks.'};
    renderModelDiscovery();scheduleTokenizerSelection({immediate:true});if(value==='__manual__'){$('#manual-model-field').hidden=false;input.focus();}
    configChanged();updateBounds();return;
  }
  if(event.target.name==='tokenizer'){$('#tokenizer-custom-field').hidden=event.target.value!=='custom';if(event.target.value==='custom')$('#new-run-form').elements.tokenizer_custom.focus();scheduleTokenizerSelection({immediate:true});configChanged();updateBounds();return;}
  if(event.target.id==='dataset-bulk-scorer')return;
  if(event.target.matches('[data-load-enable]')){setLoadPanel(event.target.dataset.loadEnable,event.target.checked);configChanged();updateBounds();return;}
  if(event.target.name?.startsWith('load_')){configChanged();updateBounds();return;}
  if(event.target.name==='example_scorer'){syncExampleScorer();return;}
  if(event.target.closest('#dataset-import-form')){const draft=state.datasetImport;if(draft){draft.mapping=importMappingValues();draft.sequence++;draft.loading=false;draft.mappingDirty=true;renderDatasetImport();}return;}
  if(event.target.id==='request-pack-file'){const file=event.target.files[0];event.target.value='';importRequestPack(file);return;}if(event.target.name==='request_source'){changeRequestSource(event.target.value);return;}if(event.target.id==='config-file'){const file=event.target.files[0];event.target.value='';importConfig(file);return;}if(event.target.closest('#new-run-form')){configChanged();scheduleRequestPlan();}if(event.target.closest('[data-test-editor]')&&event.target.tagName==='SELECT'){if(event.target.closest('[data-profile-settings]'))editProfileField(event.target);else editTestField(event.target);updateBounds();}if(event.target.name==='check'){const id=event.target.value;captureFormDraft();renderNew('custom');const control=$(`[name="check"][value="${id}"]`);if(control?.closest('[data-test-card]')?.hidden)$('[data-catalogue-filter="all"]')?.focus();else control?.focus();return;}if(event.target.id==='run-filter'){state.filter=event.target.value;renderRuns();$('#run-filter').focus();}if(['mode','restart_target'].includes(event.target.name)){updateRestart();if(state.configView==='json'&&!hasJsonChanges()){state.jsonDraft=JSON.stringify(portableConfig(readForm(true)),null,2);state.jsonBase=state.jsonDraft;$('#config-editor').value=state.jsonDraft;}}if(event.target.name==='replace')$('#save-baseline').textContent=event.target.checked?'Replace baseline':'Save baseline';});
let navigationPrefix=0;
document.addEventListener('keydown',event=>{
  if(event.isComposing)return;
  if(handleConditionKeydown(event))return;
  if(handlePerformanceKeydown(event))return;
  const reportTab=event.target.closest?.('[data-report-tab]');
  if(reportTab&&['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){
    event.preventDefault();chooseReportTab(event.key==='Home'?'results':event.key==='End'?'performance':reportTab.dataset.reportTab==='performance'?'results':'performance',{focus:true});return;
  }

  if(event.key==='Escape')hideSpotTip();
  if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='k'){event.preventDefault();openCommands();return;}
  const palette=$('#command-dialog');
  if(palette?.open&&['ArrowDown','ArrowUp','Enter'].includes(event.key)){
    const items=$$('.command-item',palette).filter(el=>!el.hidden),current=items.indexOf(document.activeElement);
    event.preventDefault();if(event.key==='Enter'){(items[current] || items[0])?.click();return;}
    items[(current+(event.key==='ArrowDown'?1:-1)+items.length)%items.length]?.focus();return;
  }
  if(document.querySelector('dialog[open]'))return;
  if(event.key==='Enter'&&!event.metaKey&&!event.ctrlKey&&!event.altKey&&['url','model'].includes(event.target.name)&&event.target.closest('#new-run-form')){
    event.preventDefault();const button=$('[data-action="test-connection"]');
    if(button&&!button.disabled&&!event.repeat)button.click();return;
  }
  if(event.key==='Escape'&&state.openTest&&event.target.closest?.('[data-test-editor]')&&!document.querySelector?.(':popover-open')){toggleTestEditor(state.openTest,true);return;}
  const spot=event.target.closest('.request-spot, .swarm-dot');
  if(spot&&['Enter',' ','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(event.key)){
    event.preventDefault();if(event.key==='Enter'||event.key===' '){spot.dispatchEvent(new MouseEvent('click',{bubbles:true}));return;}
    const svg=spot.closest('svg'),spots=$$('[tabindex]',svg),index=spots.indexOf(spot),cols=Number(svg.dataset.columns || 1);
    const delta=event.key==='ArrowLeft'?-1:event.key==='ArrowRight'?1:event.key==='ArrowUp'?-cols:cols;
    const next=event.key==='Home'?0:event.key==='End'?spots.length-1:Math.max(0,Math.min(spots.length-1,index+delta));
    spot.setAttribute('tabindex','-1');spots[next].setAttribute('tabindex','0');spots[next].focus();return;
  }
  if((event.metaKey||event.ctrlKey)&&event.key==='Enter'){event.preventDefault();primaryAction();return;}
  if(!event.metaKey&&!event.ctrlKey&&!event.altKey&&!event.target.closest('input,textarea,select,[contenteditable="true"]')){
    const key=event.key.toLowerCase();
    if(key==='n'){event.preventDefault();startNewRun();return;}
    if(key==='g'){navigationPrefix=Date.now();return;}
    if(key==='r'&&Date.now()-navigationPrefix<1200){event.preventDefault();navigationPrefix=0;location.hash='#runs';return;}
    if(key==='?'){event.preventDefault();openCommands(true);return;}
    if(event.key==='['){event.preventDefault();toggleRail();return;}
  }
  const configTab=event.target.closest('[data-config-tab]');if(configTab&&['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();switchConfigView(event.key==='Home'?'form':event.key==='End'?'json':state.configView==='form'?'json':'form');return;}});
document.addEventListener('toggle',event=>{
  if(event.target.matches('.all-requests'))cancelLandings(event.target);
  if(event.target.matches('.all-requests'))state.allRequestsOpen=event.target.open;
  if(event.target.matches('.judge-configuration'))state.judgeEditorOpen=event.target.open;
  if(event.target.matches('.field-help')&&event.newState==='open')positionFieldHelp(event.target);
},true);
window.addEventListener('resize',()=>$$('.field-help:popover-open').forEach(positionFieldHelp));
document.addEventListener('scroll',event=>{
  if(event.target.closest?.('.field-help'))return;
  for(const popover of $$('.field-help:popover-open'))popover.hidePopover();
},true);
document.addEventListener('close',event=>{if(event.target.id==='test-dialog'&&conditionDialog)conditionDialogClosed();},true);
document.addEventListener('cancel',event=>{if(event.target.id==='test-dialog'){event.preventDefault();closeConditionDialog();}if(event.target.id==='request-dialog'){event.preventDefault();closeRequestEvidence();}if(event.target.id==='storage-dialog')state.cleanup=null;if(event.target.id==='dataset-dialog')closeDatasetDialog();},true);
document.addEventListener('focusout',event=>{if(event.target.closest?.('.request-spot, .swarm-dot'))hideSpotTip();if(event.target.closest?.('[data-performance-chart]'))hidePerformanceTooltip();});
document.addEventListener('pointerout',event=>{if(event.target.closest?.('[data-performance-chart]')&&!event.relatedTarget?.closest?.('[data-performance-chart]'))hidePerformanceTooltip();if(event.target.closest?.('.condition-spot')&&!event.relatedTarget?.closest?.('.condition-spot')){conditionHover=null;refreshConditionSelection();}if(event.target.closest?.('.request-spot, .swarm-dot')&&!event.relatedTarget?.closest?.('.request-spot, .swarm-dot'))hideSpotTip();});
document.addEventListener('pointerover',event=>{const spot=event.target.closest?.('.request-spot, .swarm-dot');if(spot){if(spot.classList?.contains('condition-spot')){conditionHover=spot.dataset.conditionKey;updateConditionFocus(spot);}showSpotTip(spot,event.clientX,event.clientY);}else hideSpotTip();});
document.addEventListener('focusin',event=>{if(event.target.dataset?.liveSlot!==state.mosaicAutoFocus)state.mosaicAutoFocus=null;const spot=event.target.closest?.('.request-spot, .swarm-dot');if(!spot){hideSpotTip();refreshConditionSelection();const plot=event.target.closest?.('[data-performance-chart]');if(plot)inspectPerformanceChart(plot.dataset.performanceChart,performancePlotView(plot.dataset.performanceChart).index??0);return;}if(!spot.classList?.contains('condition-spot'))refreshConditionSelection();if(spot.classList?.contains('condition-spot'))updateConditionFocus(spot);const box=spot.getBoundingClientRect();showSpotTip(spot,box.left+box.width/2,box.top);});
document.addEventListener('pointermove',event=>{
  if(handlePerformancePointer(event))return;
  if(!$('#spot-tip')?.hidden&&event.target.closest?.('.request-spot, .swarm-dot'))showSpotTip(event.target.closest('.request-spot, .swarm-dot'),event.clientX,event.clientY);
  const coat=document.querySelector?.('.hero-coat');if(!coat)return;
  const bounds=coat.getBoundingClientRect();
  coat.style.setProperty('--mx',`${event.clientX-bounds.left}px`);coat.style.setProperty('--my',`${event.clientY-bounds.top}px`);
},{passive:true});
window.addEventListener('hashchange',()=>{if(state.bootstrap)return route();});
window.addEventListener('pagehide',saveDraft);
document.addEventListener('visibilitychange',()=>{
  clearPoll();cancelLiveFetch();if(!pageVisible())return;
  const version=state.routeVersion;
  if(location.hash==='#runs')schedulePoll(pollRuns,version,300);
  else if(state.reportRouteActive&&activeState(state.detail))schedulePoll(pollDetail,version,300);
  else if(location.hash==='#new')schedulePoll(pollNew,version,state.active?300:10000);
  else if(state.active)schedulePoll(pollRunStatus,version,300);
});
async function init(){initReticulate();try{const [bootstrap,baselines]=await Promise.all([api('/api/bootstrap'),api('/api/baselines')]);state.bootstrap=bootstrap;connectionError();state.baselines=baselines.baselines || [];state.config=clone(bootstrap.config);replaceMigrationNotes(bootstrap.migration_notes);await restoreDraft();state.config.restart_target=null;state.diskActive=bootstrap.disk_active_id || null;setActive(bootstrap.active_run_id);$('#suite-version').textContent=`suite ${bootstrap.suite_version || 'unknown'}`;$('#runner-location').textContent=bootstrap.hostname || 'UI server connected';const detailLink=location.hash.startsWith('#runs/');if(!detailLink)await refreshRuns().catch(()=>{});await route();if(detailLink)refreshRuns().catch(()=>{});}catch(error){if(state.accessExpired){$('#main').innerHTML='<div class="page-heading"><h1>Access expired</h1></div>';return;}if(document.body)document.body.dataset.runner='unavailable';$('#main').innerHTML=`<div class="page-heading"><h1>Local runner unavailable</h1></div>${notice(error.message,'error')}<button class="button" data-action="reconnect">Reconnect</button>`;}}
init();
