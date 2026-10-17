import {inferSetupPreset, bucketCoveragePlan, configureBuckets, convertExpectedAnswer, datasetCaseFromValues, changeDatasetScorer, configureJudging, configureProfiles, parseLogprobSettings, parseToolFixtures, effectiveProfileDefaults, configureTests, limitValue, optionDefault, optionValue, sameConfig} from './form-config.mjs?v=20261018-tools-logprobs3';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clone = (value) => structuredClone(value);
const state = {bootstrap:null, runs:[], baselines:[], detail:null, config:null, active:null, routeVersion:0, poll:null, tab:'checks', selectedCheck:null, checkNavigatorOpen:false, filter:'all', historySearch:'', comparisonFilter:null, comparisonCheck:'all', submitting:false, openTest:null, configView:'form', jsonDraft:null, jsonBase:null, configRevision:0, configBusy:false, showAvailableTests:false, formMode:'normal', formRestartTarget:null, requestPreview:null, previewSequence:0, planTimer:null, setupPreset:null, catalogueFilter:'all', catalogueCategory:'all', catalogueSearch:'', setupStep:1, returnToReview:false, modelDiscovery:null, modelDiscoverySequence:0, autoModel:null, datasetSelection:[], datasetImportSequence:0, judgeEditorOpen:null, datasetImport:null, datasetEditor:null, datasetTry:null};
const statusLabels = {pass:'Passed',fail:'Failed',inconclusive:'Inconclusive',skipped:'Not run',blocked:'Blocked',running:'Running',queued:'Queued',stopping:'Stopping',error:'Runner error',completed:'Finished',cancelled:'Cancelled',interrupted:'Interrupted'};
const statusIcons = {pass:'✓',fail:'×',inconclusive:'?',skipped:'–',blocked:'!',running:'·',queued:'·',stopping:'·',error:'×',completed:'✓',cancelled:'–',interrupted:'!'};
const order = {fail:0,blocked:1,inconclusive:2,pass:3,skipped:4};
const icon = (name) => ({plus:'<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 4v12M4 10h12"/></svg>',arrow:'<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h12m-5-5 5 5-5 5"/></svg>',repeat:'<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M15 6A6 6 0 1 0 16 12M15 2v5h-5"/></svg>',stop:'<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="5" y="5" width="10" height="10"/></svg>'}[name] || '');
const badge = (status, label) => `<span class="status ${escape(status)}"><span class="status-icon" aria-hidden="true">${statusIcons[status] || '·'}</span>${escape(label || statusLabels[status] || status)}</span>`;
const number = (value, digits=0) => value == null ? '—' : Number(value).toLocaleString(undefined,{maximumFractionDigits:digits});
const ms = (value) => value == null ? '—' : `${number(value,1)} ms`;
const date = (value) => value ? new Date(value).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '—';
const elapsed = (seconds) => seconds == null ? '—' : seconds < 60 ? `${number(seconds,1)}s` : `${Math.floor(seconds/60)}m ${Math.floor(seconds%60)}s`;
const duration = (run) => run.started_at && run.finished_at ? elapsed((new Date(run.finished_at)-new Date(run.started_at))/1000) : '—';
const human = (name) => String(name).replaceAll('_',' ').replace(/\b\w/, c=>c.toUpperCase());
const activeState = (run) => run && ['running','queued','stopping'].includes(run.state);
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
function reticulateCells(count) {
  if(mosaicGeometry.has(count))return mosaicGeometry.get(count);
  // Spots stay a readable size: a handful of requests never stretch into slabs.
  const size=Math.max(34,Math.min(84,Math.sqrt(960*300/Math.max(1,count))));
  // Keep the coat roughly 3:1 so small runs still read as spots, not slabs.
  const rows=Math.ceil(count/Math.max(1,Math.min(count,Math.round(960/size),Math.ceil(Math.sqrt(count*3))))),columns=Math.ceil(count/rows);
  const width=Math.round(columns*size),height=Math.round(Math.max(1,rows)*size*.86);
  // Inset each cell by half the channel plus the corner radius; the stroke adds the radius back as a rounded edge.
  const round=Math.max(1.6,size*.075),channel=Math.max(2.4,size*.05);
  const cells=coatGeometry(count,width,height,{columns,inset:channel/2+round}).map(cell=>cell.points);
  const geometry={cells,width,height,columns,round:Number((round*2).toFixed(2))};
  if(mosaicGeometry.size>8)mosaicGeometry.clear();
  mosaicGeometry.set(count,geometry);return geometry;
}
function orderedRequests(report) {
  return [...report.requests].sort((a,b)=>{
    if(Number.isFinite(a.dispatch_ms)&&Number.isFinite(b.dispatch_ms))return a.dispatch_ms-b.dispatch_ms;
    return (Date.parse(a.started_at)||0)-(Date.parse(b.started_at)||0);
  });
}
function renderMosaic(records,{budget=records.length,live=false,budgetUsed=records.length}={}) {
  const total=Math.max(records.length,budget || 0);
  if(!total)return '<section class="request-viz"><h2>Request mosaic</h2><p class="viz-empty">No requests recorded.</p></section>';
  const pages=Math.ceil(total/mosaicPageSize),page=Math.min(state.mosaicPage || 0,pages-1),offset=page*mosaicPageSize;
  state.mosaicPage=page;
  const count=Math.min(mosaicPageSize,total-offset),geometry=reticulateCells(count);
  const entrance=!live&&state.mosaicAnimated!==state.detail?.id;state.mosaicAnimated=state.detail?.id;
  const bySlot=new Map(records.map((r,i)=>[live?r.slot:i,r]));
  const statuses=live?['pass','fail','inconclusive','running','idle']:['pass','fail','inconclusive'];
  return `<section class="request-viz"><div class="viz-heading"><h2>${live?'Request budget':'Requests'}</h2><span class="mono hint">${number(records.length)} ${live?'started':'recorded'}</span></div><div class="mosaic-scroll"><svg class="request-mosaic${live?' is-live':''}${entrance?' is-entering':''}" viewBox="0 0 ${geometry.width} ${geometry.height}" style="--mosaic-w:${geometry.width}px;--spot-round:${geometry.round}" data-columns="${geometry.columns}" role="group" aria-label="${live?'Live request budget':'Recorded requests'}. Arrow keys move between spots.">${geometry.cells.map((points,i)=>{
    const r=bySlot.get(offset+i),status=r?requestOutcome(r):live&&offset+i<budgetUsed?'inconclusive':'idle',label=r?`Request ${offset+i+1}: ${r.fixture_id || r.scenario || ''}. ${statusLabels[status] || status}. ${r.elapsed_ms==null?'Timing pending':ms(r.elapsed_ms)}`:status==='inconclusive'?`Budget slot ${offset+i+1}: used or reserved, request evidence unavailable`:`Budget slot ${offset+i+1}: not used`;
    const action=r?(live?`data-live-slot="${r.slot}"`:`data-request="${escape(r.id)}"`):'';
    return `<g class="request-spot ${status}" style="--i:${i}" ${action} ${r?'role="button"':'role="img"'} tabindex="${i===0?0:-1}" aria-label="${escape(label)}"><polygon points="${points}"/></g>`;
  }).join('')}</svg></div><div class="viz-footer"><div class="mosaic-legend">${statuses.map(status=>`<span><i class="legend-dot ${status}"></i>${status==='idle'?'Unused':statusLabels[status]}${live?'':`<b>${number(records.filter(r=>requestOutcome(r)===status).length)}</b>`}</span>`).join('')}${infoHelp('mosaic','Request outcomes','Green means a scored, correct answer. Red means a failed request, invalid completion or incorrect answer. Unscored answers, cancellations and intentional probes stay inconclusive. Check verdicts also consider timing and aggregate criteria.')}</div>${pages>1?`<div class="mosaic-pages"><button class="text-button" data-action="mosaic-prev" ${page===0?'disabled':''} aria-label="Previous requests">←</button><span class="mono">${offset+1}–${offset+count} / ${number(total)}</span><button class="text-button" data-action="mosaic-next" ${page===pages-1?'disabled':''} aria-label="Next requests">→</button></div>`:''}</div></section>`;
}
function renderBeeswarm(records,{width=960}={}) {
  const measured=records.filter(r=>Number.isFinite(r.elapsed_ms)&&r.elapsed_ms>=0),missing=records.length-measured.length;
  if(!measured.length)return '<section class="latency-viz"><h2>Request latency</h2><p class="viz-empty">No latency measurements.</p></section>';
  // Use the mosaic's page so every rendered dot remains individually inspectable.
  const page=records.slice((state.mosaicPage || 0)*mosaicPageSize,((state.mosaicPage || 0)+1)*mosaicPageSize).filter(r=>Number.isFinite(r.elapsed_ms)&&r.elapsed_ms>=0);
  const values=measured.map(r=>r.elapsed_ms).sort((a,b)=>a-b),quantile=q=>values[Math.ceil(q*values.length)-1];
  // Tight clusters far from zero get a zoomed axis; the left label always states where it starts.
  const max=Math.max(values.at(-1),1),low=values[0]>max*.35?Math.floor(values[0]*.85):0,span=Math.max(1e-9,max-low),placed=[];
  const xOf=value=>16+(value-low)/span*(width-32);
  for(const r of [...page].sort((a,b)=>a.elapsed_ms-b.elapsed_ms)){
    const x=xOf(r.elapsed_ms);let lane=0,y=0;
    while(placed.some(p=>Math.abs(p.x-x)<10&&Math.abs(p.y-y)<10)){lane++;y=(lane%2?1:-1)*Math.ceil(lane/2)*10;}
    placed.push({r,x,y});
  }
  const extent=Math.max(25,...placed.map(p=>Math.abs(p.y)+10)),top=22,height=extent*2+10+top*2,mid=top+extent+5;
  const markers=values.length>=4?[['p50',quantile(.5),top-8],['p95',quantile(.95),height-4]].map(([label,value,y])=>{const x=xOf(value),anchor=x>width-120?'end':x<120?'start':'middle';return `<g class="swarm-marker ${label}"><line x1="${x.toFixed(2)}" x2="${x.toFixed(2)}" y1="${top-4}" y2="${height-top+4}"/><text x="${x.toFixed(2)}" y="${y}" text-anchor="${anchor}">${label} ${escape(ms(value))}</text></g>`;}).join(''):'';
  return `<section class="latency-viz"><div class="viz-heading"><h2>Latency</h2><span class="hint">${number(measured.length)} measured${missing?` · ${number(missing)} missing`:''}${records.length>mosaicPageSize?' · current page':''}</span></div><div class="mosaic-scroll"><svg class="latency-beeswarm" viewBox="0 0 ${width} ${height}" role="group" aria-label="Elapsed latency in milliseconds; one dot per measured request"><path d="M16 ${mid}H${width-16}" class="swarm-axis"/>${markers}${placed.map(({r,x,y},i)=>`<circle cx="${x.toFixed(2)}" cy="${(mid+y).toFixed(2)}" r="4.5" class="swarm-dot ${requestOutcome(r)}" style="--i:${i}" tabindex="${i===0?0:-1}" role="button" data-request="${escape(r.id)}" aria-label="${escape(r.fixture_id)}: ${ms(r.elapsed_ms)}"></circle>`).join('')}</svg></div><div class="axis-labels mono"><span>${ms(low)}</span><span>${ms(max)}</span></div></section>`;
}
function renderFlightPlan(plan) {
  if(!plan)return '';
  const rows=(plan.rows || []).filter(r=>r.requests_per_target!==0),max=Math.max(1,...rows.map(r=>r.requests_per_target || 0));
  return `<div class="flight-plan"><div class="viz-heading"><h3>Flight plan</h3><span class="hint">Per endpoint</span></div>${rows.map(r=>`<div class="flight-row"><div><span>${escape(checkGuide[r.id]?catalogTitle(r.id):r.title)}</span><span class="mono">${r.requests_per_target==null?'Adaptive':`${r.kind==='estimate'?'~':''}${number(r.requests_per_target)}`}</span></div><div class="flight-track ${r.requests_per_target==null?'adaptive':''}">${r.requests_per_target==null?'':`<span style="width:${r.requests_per_target/max*100}%"></span>`}</div></div>`).join('')}</div>`;
}
const phaseNames={initial:'First request',warm:'Warm-up',generation:'Generation speed',sustained:'Sustained load',recovery:'Recovery check',cancellation_probe:'Cancellation probes',client_cancel:'Client cancellation',backend_cancel:'Server cancellation',context:'Long-context recall',limits:'Output limits',pack_control:'Quiet baseline',readiness:'Readiness',short:'Short requests',request_pack:'Your questions',judging:'AI evaluation',connection:'Connection'};
const phaseHelp={sustained:'Steady traffic that loads the server before the recovery check.',recovery:'Checks normal answers return after the load.',warm:'Repeated short requests after the first one.',initial:'The very first request, timed separately.',pack_control:'Your questions sent with no other load, for comparison.'};
function phaseName(id) {
  if(phaseNames[id])return phaseNames[id];
  const stage=/^traffic_stage_(\d+)$/.exec(id);if(stage)return `Traffic stage ${Number(stage[1])+1}`;
  if(/^correctness/.test(id))return 'Answer correctness';
  return human(id);
}
function renderPhases(events) {
  const phases=[];
  for(const e of events){
    if(e.event==='scenario_started')phases.push({key:`${e.target}:${e.scenario}`,title:e.scenario,target:e.target,start:e.elapsed_seconds,end:null});
    if(e.event==='scenario_finished'){
      const phase=phases.findLast(p=>p.key===`${e.target}:${e.scenario}`&&p.end===null);
      if(phase)phase.end=e.elapsed_seconds;
      else phases.push({key:`${e.target}:${e.scenario}`,title:e.scenario,target:e.target,start:null,end:e.elapsed_seconds});
    }
  }
  // Back-to-back repeats of one phase (e.g. many sustained-load rounds) read as one line with a round count.
  const groups=[];
  for(const p of phases){
    const last=groups.at(-1);
    if(last&&last.key===p.key&&last.end!==null&&last.start!==null&&p.start!==null){last.rounds++;last.end=p.end;last.busy+=p.end===null?0:p.end-p.start;last.running=p.end===null;continue;}
    groups.push({...p,rounds:1,busy:p.end===null||p.start===null?0:p.end-p.start,running:p.end===null});
  }
  const row=p=>`<li class="${p.running?'running':'finished'}" ${phaseHelp[p.title]?`title="${escape(phaseHelp[p.title])}"`:''}><span class="phase-mark">${p.running?'·':'✓'}</span><div><strong>${escape(phaseName(p.title))}</strong><small>${escape(p.target)}${p.rounds>1?` · ${number(p.rounds)} rounds`:''}</small></div><span class="mono">${p.start===null?'Start unavailable':p.running?'Running':elapsed(p.rounds>1?p.end-p.start:p.end-p.start)}</span></li>`;
  const visible=groups.slice(-6),earlier=groups.slice(0,-6);
  return `<section class="phase-timeline"><h2>Phases</h2>${groups.length?`${earlier.length?`<details class="phase-earlier"><summary>${number(earlier.length)} earlier ${earlier.length===1?'phase':'phases'}</summary><ol>${earlier.map(row).join('')}</ol></details>`:''}<ol>${visible.map(row).join('')}</ol>`:'<p class="hint">Waiting for scenario events.</p>'}</section>`;
}
function openLiveRequest(slot) {
  const r=(state.detail.live_requests || []).find(r=>r.slot===slot);if(!r)return;
  const dialog=$('#request-dialog');
  dialog.innerHTML=`<div class="dialog-head"><div><div class="eyebrow">Live request</div><h2 id="request-title">${escape(r.fixture_id || r.scenario)}</h2></div><button class="button ghost" data-action="close-dialog" aria-label="Close request evidence">✕</button></div><div class="dialog-body">${badge(requestOutcome(r))}<p class="hint">Snapshot at opening. Full evidence is available when the run finishes.</p><dl class="metrics-grid">${metric('Transport',r.status)}${metric('Elapsed',ms(r.elapsed_ms))}${metric('First answer',ms(r.first_output_ms))}${metric('Answer',r.score===true?'Correct':r.score===false?'Incorrect':'Unscored')}${metric('Endpoint',r.target)}${metric('Scenario',r.scenario)}</dl></div>`;dialog.showModal();
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
  const action=[...main.querySelectorAll('.button.primary, .button.danger')].find(el=>!el.disabled&&el.getClientRects().length);
  action?.click();
}
function initReticulate() {
  let theme='dark';try{theme=localStorage.getItem('giraffe-theme') || theme;}catch{}
  setTheme(theme==='light'?'light':'dark');
  toggleRail(document.documentElement.classList.contains('rail-collapsed'));
}

async function api(path, options={}) {
  let response;
  try { response = await fetch(path,{...options,headers:{'Content-Type':'application/json',...(options.headers || {})}}); }
  catch { throw new Error('The local UI server could not be reached. Check that giraffe ui is still running.'); }
  let body;
  try { body = await response.json(); } catch { throw new Error(`The local server returned an unreadable response (${response.status}).`); }
  if (!response.ok) {
    if(response.status===404 && (path.startsWith('/api/config/')||path==='/api/datasets/preview'||path==='/api/runs/try-example')){
      const error=new Error('The running Giraffe server does not support this configuration operation. Restart giraffe ui from the updated installation, then refresh this page and try again.');
      error.status=response.status;throw error;
    }
    const detail = body.detail || body.errors || body.error || `Request failed (${response.status}).`;
    const message = Array.isArray(detail) ? detail.map(e => typeof e === 'string' ? e : `${(e.loc || []).join('.')}: ${e.msg || JSON.stringify(e)}`).join('\n') : typeof detail === 'string' ? detail : JSON.stringify(detail);
    const error = new Error(message); error.status = response.status; throw error;
  }
  return body;
}
function toast(message) { const el=$('#toast');el.textContent=message;el.hidden=false;setTimeout(()=>{el.hidden=true;},4500); }
function connectionError(message='') { if(document.body)document.body.dataset.runner=message?'unavailable':'connected';$('#connection-error').textContent=message;$('#connection-error').hidden=!message; }
function setActive(id) {
  state.active=id || null;
  const link=$('#active-run-link');link.hidden=!id;link.href=id?linkRun(id):'#runs';link.innerHTML=`${badge('running','Run in progress')}<span aria-hidden="true"> →</span>`;
}
function updateNav(section, label) {
  renderRailRuns();
  $('#nav-runs').classList.toggle('current',section==='runs');$('#nav-new').classList.toggle('current',section==='new');$('#nav-new').hidden=false;
  $('#nav-runs').setAttribute('aria-current',section==='runs'?'page':'false');$('#nav-new').setAttribute('aria-current',section==='new'?'page':'false');
  $('#breadcrumb').textContent=label;
  document.title=`${label} · Giraffe`;
}
function clearPoll() { clearTimeout(state.poll);state.poll=null; }
function schedulePoll(callback, version, delay=1800) { clearPoll();state.poll=setTimeout(()=>{if(version===state.routeVersion) callback(version);},delay); }
async function refreshRuns() {
  const data=await api('/api/runs');state.runs=data.runs || [];setActive(data.active_run_id);$('#run-count').textContent=state.runs.length || '';renderRailRuns();return data;
}
function relativeTime(value) {
  const then=Date.parse(value);if(!Number.isFinite(then))return '';
  const seconds=Math.max(0,(Date.now()-then)/1000);
  return seconds<60?'now':seconds<3600?`${Math.floor(seconds/60)}m`:seconds<86400?`${Math.floor(seconds/3600)}h`:seconds<86400*7?`${Math.floor(seconds/86400)}d`:new Date(then).toLocaleDateString(undefined,{month:'short',day:'numeric'});
}
// The rail keeps the last few runs one click away, wherever the user is.
function renderRailRuns() {
  const list=$('#rail-runs'),section=$('#rail-recent');if(!list||!section)return;
  const current=location.hash.startsWith('#runs/')?decodeURIComponent(location.hash.slice(6)):null;
  section.hidden=!state.runs.length;
  list.innerHTML=state.runs.slice(0,6).map(run=>{
    const model=(run.targets || []).map(t=>t.model).filter(Boolean).join(', ') || 'Unknown model',outcome=runOutcome(run);
    return `<li><a href="${linkRun(run.id)}" class="${run.id===current?'current':''}" ${run.id===current?'aria-current="page"':''} title="${escape(model)} · ${escape(statusLabels[outcome] || outcome)}"><span class="status-spot ${escape(outcome)}" aria-hidden="true"></span><span class="rail-run-model">${escape(model)}</span><time>${escape(relativeTime(run.started_at))}</time><span class="sr-only">${escape(statusLabels[outcome] || outcome)}</span></a></li>`;
  }).join('');
}
// Route changes glide in; re-renders within a view (polling, toggles) stay still.
function enterRoute() {
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
function showSpotTip(target,x,y) {
  const tip=$('#spot-tip'),label=target?.getAttribute('aria-label');if(!tip||!label)return;
  const outcome=['pass','fail','inconclusive','running','idle'].find(name=>target.classList.contains(name)) || 'idle';
  const [head,...rest]=label.split(/(?<=[.:])\s+/);
  tip.innerHTML=`<span class="status-spot ${outcome}"></span><span><strong>${escape(head.replace(/[.:]$/,''))}</strong>${rest.length?`<small>${escape(rest.join(' ').replace(/\.\s+/g,' · ').replace(/\.$/,''))}</small>`:''}</span>`;
  tip.hidden=false;
  const bounds=tip.getBoundingClientRect(),left=Math.min(window.innerWidth-bounds.width-12,Math.max(12,x+14)),top=y-bounds.height-14<12?y+18:y-bounds.height-14;
  tip.style.transform=`translate(${Math.round(left)}px,${Math.round(top)}px)`;
}
function hideSpotTip() {const tip=$('#spot-tip');if(tip)tip.hidden=true;}
async function route() {
  hideSpotTip();
  if($('#dataset-dialog')?.open)closeDatasetDialog();
  for(const id of ['request-dialog','baseline-dialog']){const dialog=$('#'+id);if(dialog?.open)dialog.close();}
  if ($('#new-run-form')) captureFormDraft();
  window.scrollTo?.({top:0,left:0,behavior:'instant'});
  clearPoll();const version=++state.routeVersion;const hash=location.hash;
  try {
    if(!hash){location.hash=state.active?linkRun(state.active):'#new';return;}
    if (hash==='#new') { updateNav('new','New run');state.config ||= clone(state.bootstrap.config);renderNew();enterRoute();schedulePoll(pollNew,version,3000);return; }
    if (hash.startsWith('#runs/')) {
      const id=decodeURIComponent(hash.slice(6));updateNav('runs','Run detail');
      $('#main').innerHTML='<div class="loading">Loading run…</div>';
      const detail=await api(runPath(id));if(version!==state.routeVersion)return;
      state.detail=detail;state.mosaicPage=0;state.selectedCheck=null;state.checkNavigatorOpen=false;state.tab='checks';state.comparisonFilter=null;state.comparisonCheck='all';renderDetail();enterRoute();
      if(activeState(detail))schedulePoll(pollDetail,version);return;
    }
    updateNav('runs','Runs');$('#main').innerHTML='<div class="loading">Loading runs…</div>';
    await refreshRuns();if(version!==state.routeVersion)return;
    renderRuns();enterRoute();schedulePoll(pollRuns,version,3000);
  } catch(error) { if(version===state.routeVersion) $('#main').innerHTML=`<div class="page-heading"><h1>Could not open this view</h1></div>${notice(error.message,'error')}<button class="button" data-action="retry">Try again</button>`; }
}
async function pollRuns(version) {
  try { const previous=JSON.stringify(state.runs),active=state.active;await refreshRuns();if(version!==state.routeVersion)return;connectionError();if(previous!==JSON.stringify(state.runs)||active!==state.active)renderRuns(); }
  catch(error) { if(version===state.routeVersion)connectionError(error.message+' Your saved runs have not changed.'); }
  if(version===state.routeVersion)schedulePoll(pollRuns,version,3000);
}
async function pollNew(version) {
  try { await refreshRuns();if(version!==state.routeVersion)return;connectionError();const button=$('#start-run');if(button&&!state.submitting)button.disabled=!canStartRun();const activeNotice=$('#new-active-notice');if(activeNotice){activeNotice.hidden=!state.active;$('a',activeNotice).href=state.active?linkRun(state.active):'#runs';} }
  catch(error) {if(version===state.routeVersion)connectionError(error.message);}
  if(version===state.routeVersion)schedulePoll(pollNew,version,3000);
}
async function pollDetail(version) {
  const id=state.detail.id;
  try {
    const detail=await api(runPath(id));if(version!==state.routeVersion || state.detail.id!==id)return;
    const focused=document.activeElement,slot=focused?.dataset?.liveSlot,action=focused?.dataset?.action;
    connectionError();state.detail=detail;renderDetail();
    if(slot!==undefined)$(`[data-live-slot="${slot}"]`)?.focus({preventScroll:true});
    else if(action==='stop')$('[data-action="stop"]')?.focus({preventScroll:true});
    if(activeState(detail))schedulePoll(pollDetail,version);else {if(state.active===id)setActive(null);await refreshRuns();}
  } catch(error) {if(version!==state.routeVersion)return;connectionError(error.message+' Reconnecting; the suite may still be running.');schedulePoll(pollDetail,version,3000);}
}
function suiteList() { return `<ol class="suite-list">${(state.bootstrap.checks || []).map((c,i)=>`<li><span class="index">${String(i+1).padStart(2,'0')}</span><span>${escape(c.title)}</span>${c.optional?'<small>OPT</small>':''}</li>`).join('')}</ol>`; }
function runOutcome(run) {
  return activeState(run)||['error','cancelled','interrupted'].includes(run.state)?run.state:run.overall || run.state;
}
function filteredRuns() {
  const query=state.historySearch.trim().toLowerCase();
  return state.runs.filter(run=>(state.filter==='all'||(activeState(run)?'active':runOutcome(run))===state.filter)&&
    (!query||[run.id,run.run_id,...(run.targets || []).flatMap(target=>[target.name,target.model,target.url])].join(' ').toLowerCase().includes(query)));
}
function renderRuns() {
  const focused=document.activeElement,focusId=['run-search','run-filter'].includes(focused?.id)?focused.id:null;
  const selection=focusId==='run-search'?[focused.selectionStart,focused.selectionEnd]:null;

  if(!state.runs.length&&!state.active){
    $('#main').innerHTML=`<section class="first-run"><h1>Run history</h1><p>No runs yet.</p><a class="button primary" href="#new">${icon('plus')} New run</a></section>`;
    return;
  }
  const runs=filteredRuns();
  $('#main').innerHTML=`<div class="page-heading"><h1>Runs</h1><a class="button primary" href="#new">${icon('plus')} New run</a></div>${renderOutcomeStrip()}<div class="history-chips" aria-label="Filter runs">${['all','pass','fail','inconclusive','blocked','skipped','active','cancelled','interrupted','error'].filter(status=>status==='all'||status===state.filter||state.runs.some(r=>(activeState(r)?'active':runOutcome(r))===status)).map(status=>`<button class="filter-chip" data-run-filter="${status}" aria-pressed="${state.filter===status}">${status==='all'?'All runs':status==='active'?'Running':statusLabels[status]}</button>`).join('')}</div>
  <div class="history-layout history-focused"><section class="panel"><div class="section-heading"><div class="history-tools"><label class="history-search"><span class="sr-only">Search runs</span><input id="run-search" type="search" placeholder="Search runs" value="${escape(state.historySearch)}" autocomplete="off"></label><div class="filter-group"><label class="sr-only" for="run-filter">Filter run status</label><select id="run-filter" aria-label="Filter run status"><option value="all">All outcomes</option><option value="fail">Failed</option><option value="pass">Passed</option><option value="inconclusive">Inconclusive</option><option value="blocked">Blocked</option><option value="skipped">Not run</option><option value="active">Active</option><option value="cancelled">Cancelled</option><option value="interrupted">Interrupted</option><option value="error">Runner error</option></select></div></div></div>
  ${runs.length?`<div class="history-table"><table aria-label="Saved endpoint runs"><thead><tr><th>Model</th><th>Outcome</th><th>Started</th><th><span class="sr-only">Open run</span></th></tr></thead><tbody>${runs.map(r=>{
    const targets=r.targets || [],model=targets.map(t=>t.model).filter(Boolean).join(', ') || 'Unknown model';
    const names=targets.map(t=>t.name).filter(name=>name&&name!=='local').join(', ');
    const started=`<time${r.started_at?` datetime="${escape(r.started_at)}"`:''}>${date(r.started_at)}</time>`;
    const hosts=[...new Set(targets.map(t=>{try{return new URL(t.url).host;}catch{return '';}}).filter(Boolean))].join(', ');
    return `<tr class="history-row ${escape(runOutcome(r))}"><td class="history-identity"><span class="status-spot ${escape(runOutcome(r))}" aria-hidden="true"></span><span class="history-names"><a class="run-link" href="${linkRun(r.id)}"${names?` title="${escape(names)}"`:''}>${escape(model)}</a>${hosts?`<small class="history-host">${escape(hosts)}</small>`:''}<small class="history-mobile-date">${started}</small></span></td><td class="history-result">${badge(runOutcome(r))}</td><td class="history-date">${started}</td><td class="history-open"><a class="text-button history-run-action" href="${linkRun(r.id)}" aria-label="Open run for ${escape(model)}${r.started_at?` from ${escape(date(r.started_at))}`:''}">${icon('arrow')}</a></td></tr>`;}).join('')}</tbody></table></div>`:
  `<div class="empty-state"><h2>No matching runs</h2><button class="button" data-action="clear-filter">Clear filters</button></div>`}</section></div>`;
  $('#run-filter').value=state.filter;
  if(focusId){const control=$('#'+focusId);control?.focus({preventScroll:true});if(selection&&selection[0]!=null)control?.setSelectionRange?.(...selection);}
}
const numericFields=[['context_limit','Context limit','tokens',128,1],['concurrency','Maximum in-flight requests','Requests running at the same time.',1,1],['max_requests','Total request limit','Requests allowed during the whole run.',1,1],['max_duration_seconds','Maximum duration','seconds',.1,'any'],['request_timeout_seconds','Request deadline','seconds',.1,'any'],['max_output_tokens','Output cap','tokens / request',1,1]];
const limitFields=[['first_output_ms','First output maximum','ms'],['latency_ms','Response time maximum','ms'],['stream_gap_ms','Stream pause maximum','ms'],['min_output_tokens_per_second','Minimum generation rate','tokens / second after first output (estimate)'],['fairness_max_ratio','Mixed-load slowdown','ratio, greater than 1']];
const fieldDescriptions={
  url:{hint:'Address of your LLM server.',help:'Use the address Giraffe can reach, such as http://localhost:11434/v1 for Ollama or http://localhost:8000/v1 for vLLM. Giraffe connects to an existing server; it does not start or install a model.'},
  model:{hint:'Model name used by your server.',help:'Use an exact model name from your server, including its tag or alias. For Ollama, use a name from ollama list. The model must already be installed.'},
  api_key_env:{hint:'Variable name, such as OPENAI_API_KEY. Do not paste the key.',help:'Giraffe reads this environment variable from the machine running giraffe ui. Set it before starting Giraffe. The secret key is never stored in this form.'},
  name:{hint:'A name for this endpoint in results.',help:'Choose a unique name such as local or staging. Giraffe uses it to identify this endpoint in reports and comparisons.'},
  context_limit:{help:'The server’s configured limit for the question and answer together, measured in tokens. A token is a small piece of text. Use the context length configured on your server, which can be lower than the model’s advertised maximum.'},
  concurrency:{hint:'Requests running at the same time.',help:'The most requests Giraffe can run at the same time across all servers. You set this number; Giraffe does not detect your hardware limit. Individual tests can use fewer requests. Raising this does not change Total request limit or Concurrency levels.'},
  max_requests:{help:'The most target requests Giraffe may use during the whole run, shared across all tests and endpoints. Some scheduled requests can use budget without reaching the server. The run can stop with fewer completed requests. AI evaluator calls have separate limits.'},
  max_duration_seconds:{help:'Stop starting work when the whole run reaches this many seconds, and save the results collected so far. A time limit can leave tests unfinished.'},
  request_timeout_seconds:{help:'How long a single request may take before Giraffe times it out. This covers waiting for the answer and receiving it. A shorter test-specific or remaining run deadline can stop it sooner.'},
  max_output_tokens:{help:'The most tokens Giraffe asks the model to generate for one request. A token is a small piece of text. The model can stop before reaching this number; the cap does not guarantee a long answer.'},
  buckets_levels:{help:'For 1, 4, 8, Giraffe tests one request at a time, then four together, then eight together. It repeats each load with every combination of your three question lengths and three answer caps. In the Requests at once setup, the highest number also sets the run’s simultaneous request limit.'},
  buckets_samples_per_bucket:{help:'The minimum requests for each test with short, medium or long questions and answers, repeated at every concurrency level. Giraffe sends the larger of this number and the load being tested. For example, a minimum of 4 still sends 8 requests when testing 8 at a time.'},
  buckets_window_seconds:{help:'The time limit for each test with short, medium or long questions and answers, at one concurrency level. Waiting for an available request slot uses this time too. When time runs out, Giraffe records any missing coverage.'},
  buckets_input:{help:'The target question length in characters, including repeated background text. Giraffe keeps the labeled-box instruction intact, so very small questions and the longer-answer instruction can make the actual request longer. The request plan shows the characters sent for every combination.'},
  buckets_output:{help:'The maximum answer tokens requested for this case. The first answer case asks for a box label; the other two ask for the label followed by numbers. The server can finish before reaching the cap. Every answer cap must fit within the run output cap.'},
  metrics_url:{hint:'Optional server metrics address.',help:'Use an existing Prometheus metrics URL, such as http://localhost:9400/metrics. Giraffe reads measurements from it; it does not install an exporter. Missing metrics remain unknown.'},
  mode:{help:'Use running server tests the server as it is. Restart one server first runs its configured restart command before testing. Choose this only when you intend to restart that server.'},
  restart_target:{help:'Only this endpoint’s configured restart command runs. Other endpoints keep running. A restart command must already be configured for the selected endpoint.'},
  baseline_id:{help:'Choose a previously saved run to compare with this one. Giraffe reports changes in errors, answers and timing, while still checking this run against its own acceptance settings. Leave this empty for a first run.'},
  baseline_name:{help:'Give this saved run a name you will recognize later, such as before-model-update. Saving a baseline does not change the run’s outcome.'},
  traffic_rates:{hint:'Requests per second, for example 1, 2, 4.',help:'Each number is a separate load stage, from lowest to highest. Giraffe starts new requests at that rate without waiting for previous answers. Requests it cannot start because its in-flight limit is full are counted as local drops.'},
  traffic_duration_seconds:{hint:'Seconds to spend at each rate.',help:'Keep sending new requests at each selected rate for this long. After each stage, Giraffe waits for started requests to finish, up to the completion wait limit.'},
  traffic_arrival:{hint:'Even spacing or uneven arrivals.',help:'Steady spreads requests evenly over time. Poisson varies the gaps between requests to simulate uneven traffic. The random seed lets you repeat the same arrival pattern.'},
  traffic_max_in_flight:{hint:'Leave blank to use the run limit.',help:'The most requests this traffic test may run at once. The run’s Maximum in-flight requests still applies if it is lower. When all slots are occupied, extra arrivals count as dropped rather than queued.'},
  traffic_seed:{hint:'Use the same number to repeat a pattern.',help:'Controls the repeatable arrival pattern and request mix. Keeping this number the same makes runs easier to compare; it does not force the model to produce the same answers.'},
  traffic_long_input_chars:{hint:'Question length in characters.',help:'Size of the long questions used by built-in traffic tests. Characters and model tokens are different units. This setting is not used when testing your own questions.'},
  traffic_long_output_words:{hint:'Words to request in a long answer.',help:'Built-in long-answer requests ask the model to copy this many words. The run’s output token cap still applies, and a model may stop early. This setting is not used for your own questions.'},
  traffic_drain_timeout_seconds:{hint:'Seconds to wait for remaining answers.',help:'After each traffic stage stops sending new requests, allow already-started requests this much time to finish.'},
  traffic_scheduler_lag_tolerance_ms:{hint:'Allowed delay when starting a request, in ms.',help:'If Giraffe starts a planned request too late, it records a load-generator limitation. This helps separate delays on your computer from delays in the LLM server.'},
  traffic_mix_short:{hint:'Relative share of short requests.',help:'These three weights control how often each request type is used. Weights of 2, 1, 1 mean roughly half short requests, a quarter long-question requests, and a quarter long-answer requests. They are shares, not request counts.'},
  traffic_mix_long_input:{hint:'Relative share of long questions.',help:'Increase this weight to send more long questions. The three weights are scaled to shares automatically. For example, 2, 1, 1 means about half short requests and a quarter of each long type.'},
  traffic_mix_long_output:{hint:'Relative share of long answers.',help:'Increase this weight to request more long answers. The three weights are scaled to shares automatically. The global output cap still applies to each answer.'},
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
  test_concurrency:{help:'How many requests this test asks to run at the same time. The run’s Maximum in-flight requests still applies if it is lower. Leave blank to use the run setting.'},
  test_context_limit:{help:'The context length this test should exercise, in tokens. Use the length configured on the server. Leave blank to use the run setting.'},
  test_max_output_tokens:{help:'The answer token cap for this test. The run’s output cap still applies if it is lower. Leave blank to use the run setting.'},
  test_request_timeout_seconds:{help:'The deadline for a request in this test, in seconds. The run’s request deadline still applies if it is shorter. Leave blank to use the run setting.'},
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
  judging_concurrency:{help:'The most evaluator requests Giraffe may run at once. This is separate from target-test concurrency and does not affect inference timing.'},
  judging_request_timeout_seconds:{help:'Seconds allowed for one evaluator response. An evaluator timeout leaves that vote missing; it is not treated as an incorrect target answer.'},
  judging_max_duration_seconds:{help:'Seconds allowed for the complete post-run AI evaluation. Unfinished evaluations remain inconclusive.'},
  judging_max_output_tokens:{help:'The maximum answer tokens allowed for each evaluator response. This cap applies to evaluator output, not the answers being tested.'},
  example_id:{help:'A unique name for this question, used to find it in results and comparisons. Keep the same ID when comparing later runs of the same question.'},
  example_weight:{hint:'A larger number uses this question more often.',help:'Controls how often this question appears in arrival traffic compared with other questions. A weight of 2 gives it about twice the share of a question weighted 1. It does not make the question count twice when scoring an individual answer.'},
  example_max_tokens:{hint:'Leave blank to use the run’s output cap.',help:'Maximum answer tokens to request for this question. The run’s output cap still applies if it is lower.'},
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
function renderField(name,label,control,{hint,help,helpKey=name,full=false,wrapperId='',controlId=`field-${name}`,hintId=`field-${name}-hint`}={}) {
  const copy=fieldDescription(helpKey);hint ??= copy.hint || '';help ??= copy.help || '';
  return `<div class="form-field ${full?'full':''}" ${wrapperId?`id="${escape(wrapperId)}"`:''}><div class="field-label"><label for="${escape(controlId)}">${escape(label)}</label>${infoHelp(name,label,help)}</div>${control}${hint?`<span class="hint" id="${escape(hintId)}">${escape(hint)}</span>`:''}</div>`;
}
function field(name,label,value,{type='text',hint,help,helpKey=name,min,max,step,placeholder='',required=false,full=false}={}) {
  const description=hint ?? fieldDescription(helpKey).hint;
  const control=`<input id="field-${escape(name)}" name="${escape(name)}" type="${type}" value="${escape(value ?? '')}" ${required?'required':''} ${min!==undefined?`min="${min}"`:''} ${max!==undefined?`max="${max}"`:''} ${step!==undefined?`step="${step}"`:''} placeholder="${escape(placeholder)}" autocomplete="off" ${description?`aria-describedby="field-${escape(name)}-hint"`:''}>`;
  return renderField(name,label,control,{hint,help,helpKey,full});
}
function selectField(name,label,options,{hint,help,helpKey=name,wrapperId='',controlId=`field-${name}`}={}) {
  const description=hint ?? fieldDescription(helpKey).hint;
  const control=`<select id="${escape(controlId)}" name="${escape(name)}" ${description?`aria-describedby="field-${escape(name)}-hint"`:''}>${options}</select>`;
  return renderField(name,label,control,{hint,help,helpKey,wrapperId,controlId});
}
function textField(name,label,value,{rows=4,maxLength,hint,helpKey=name,wrapperId='',hintId=`field-${name}-hint`}={}) {
  const description=hint ?? fieldDescription(helpKey).hint;
  const control=`<textarea id="field-${escape(name)}" name="${escape(name)}" rows="${rows}" ${maxLength?`maxlength="${maxLength}"`:''} ${description?`aria-describedby="${escape(hintId)}"`:''} spellcheck="false">${escape(value ?? '')}</textarea>`;
  return renderField(name,label,control,{hint,helpKey,wrapperId,hintId});
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
  return field(name,label,value,{type:'number',hint,help,helpKey,min:valueSchema.minimum ?? (valueSchema.exclusiveMinimum===undefined?undefined:valueSchema.exclusiveMinimum+(valueSchema.type==='integer'?1:.001)),max:valueSchema.maximum,step:valueSchema.type==='integer'?1:'any',required:!nullable,placeholder:nullable?placeholder:''});
}
const checkGroups=[
  {id:'answers',title:'Answers',blurb:'Is the output right?',checks:['access','serving','correctness','json','context','tools']},
  {id:'speed',title:'Speed & load',blurb:'How fast, and under how much load?',checks:['first_output','generation','capacity','buckets','arrivals']},
  {id:'reliability',title:'Reliability',blurb:'Does it hold up over time?',checks:['fairness','mixed','recovery','sessions','consistency']},
  {id:'server',title:'Diagnostics',blurb:'Evidence from the server itself.',checks:['cancellation','prefix','gpu']},
];
const checkGuide={
  access:{name:'Endpoint response',purpose:'Can this endpoint return an answer?',sends:'One short generated request, shared with timing checks.',shows:'Whether the endpoint, authentication and model produce a valid answer.'},
  serving:{name:'Invalid responses',purpose:'Look for errors, empty answers and incomplete responses.',sends:'Repeated short requests; also inspects responses from the selected checks.',shows:'Invalid responses, timeouts and server errors against your allowed error rate.'},
  correctness:{name:'Answer correctness',purpose:'Check known answers using built-in examples or your own questions.',sends:'Generated extraction, arithmetic and classification questions, or each of your saved questions.',shows:'Correct answers among scored responses. Missing answers stay visible.'},
  json:{name:'JSON correctness',purpose:'Check that JSON answers have the right fields, types and values.',sends:'Generated questions requesting a defined JSON structure.',shows:'Valid JSON, required fields, expected types and known values.'},
  context:{name:'Context recall',purpose:'Find out whether facts survive inside long questions.',sends:'Known facts placed at different positions and lengths in generated questions.',shows:'Recall at the exercised lengths. Use your server’s configured context limit.',needs:'Check the server context limit'},
  tools:{name:'Tool-call correctness',purpose:'Check tool calls and any required assistant text. Nothing is run.',sends:'Configured fixtures can require none, auto, required or a named tool. The built-in weather check uses automatic selection. Optional legacy forced-choice diagnostics run separately.',shows:'Separate protocol, choice compliance, argument schema, task and assistant text outcomes against configured expectations.',needs:'Tool-capable model and endpoint'},
  first_output:{name:'First answer',purpose:'Measure how long users wait before useful answer text arrives.',sends:'One initial request and repeated short requests, shared with response-health checks.',shows:'Initial and subsequent answer delays, separate from reasoning text.',needs:'Streaming enabled'},
  generation:{name:'Generation',purpose:'Measure completion speed and interruptions in streamed answers.',sends:'Repeated requests to reproduce a generated passage.',shows:'Response time, stream pauses and generation rate when usage data supports it.',needs:'Streaming for pause measurements'},
  capacity:{name:'Capacity under load',purpose:'See when increasing request rates causes slow, incorrect or dropped answers.',sends:'New requests at each configured rate, using generated examples or your own questions.',shows:'Correct answers, latency and drops at each tested rate. Set a time target to judge acceptable capacity for this workload.'},
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
  if(key==='tools'){
    const profile=config.tool_checks || {fixtures:[],modes:null},selected=profile.modes?.join(',') || '';
    const modes=[['','Run default'],['complete','Complete responses'],['stream','Streaming responses'],['complete,stream','Complete and streaming']];
    if(!modes.some(([value])=>value===selected))modes.push([selected,'Saved selection']);
    const fixtures=typeof profile.fixtures==='string'?profile.fixtures:JSON.stringify(profile.fixtures || [],null,2);
    const conversations=typeof profile.conversations==='string'?profile.conversations:JSON.stringify(profile.conversations || [],null,2);
    return `<input type="hidden" name="profile_edits_tools" value="[]"><div data-profile-settings="tools" class="fields">${selectField('profile_tools_modes','Response modes',modes.map(([value,label])=>`<option value="${escape(value)}" ${value===selected?'selected':''}>${label}</option>`).join(''),{hint:'Run default tests both modes when streaming is enabled, otherwise complete responses. Explicit streaming requires streaming in Run limits.'})}${selectField('profile_tools_forced_tool_diagnostic','Force tool choice',`<option value="false" ${config.forced_tool_diagnostic?'':'selected'}>No · test normal tool selection</option><option value="true" ${config.forced_tool_diagnostic?'selected':''}>Yes · include forced choice diagnostic</option>`,{hint:'Applies to fixtures without an explicit choice rule. Giraffe never executes tools.'})}${textField('profile_tools_fixtures','Tool fixtures (JSON)',fixtures,{rows:10,maxLength:65536,hint:'Use [] for the built-in weather check, or up to 16 fixture objects with fixture_id, messages, tools, expected_calls (task assertions) or choice (mode: none, auto, required, named; named also needs name), and optional expected_content. choice.support is required or discover; max_calls defaults to 16; parallel_tool_calls is optional. Exact content checks the combined assistant text. The server validates fixture schemas and the 64 KiB configuration limit.'})}${textField('profile_tools_conversations','Tool-result conversations (JSON)',conversations,{rows:10,maxLength:65536,hint:'Optional bounded conversations: fixture_id, messages, tools, steps, final_content. Each step declares expected_calls with name, arguments, result and optional outcome (success or error). Put {{observation_id}} in a result and a final_content check. Up to 4 steps; all final checks must pass. Tools stay available to detect repeated operations. With conversations configured, an empty fixture list skips the built-in check.'})}</div>`;
  }
  if(key==='mixed'){
    const settings=config.buckets || bucketFormDefaults(config);
    return `<div data-bucket-settings class="fields">${field('buckets_mixed_pairs','Short/heavy pairs',settings.mixed_pairs,{type:'number',min:1,max:100,step:1,required:true})}${field('buckets_heavy_long_input','Long-question weight',settings.heavy_weights.long_input,{type:'number',min:1,max:16,step:1,required:true,hint:'Relative share of heavy work, from 1 to 16.'})}${field('buckets_heavy_long_output','Long-answer weight',settings.heavy_weights.long_output,{type:'number',min:1,max:16,step:1,required:true,hint:'Relative share of heavy work, from 1 to 16.'})}</div><p class="hint">Question lengths and answer caps are shared with Request sizes. Configure that row to edit them; it does not need to be selected.</p>`;
  }
  const profiles={
    arrivals:{schema:'Arrival',fields:['mode','requests','requests_per_second','burst_size'],advanced:['interval_seconds','max_pending','seed']},
    prefix:{schema:'Prefix',fields:['prefix_chars','repeats','history_turns']},
    sessions:{schema:'Sessions',fields:['modes','sessions','concurrency'],advanced:['max_tokens','delay_seconds','turns','saved_answers']},
    consistency:{schema:'Consistency',fields:['repetitions','levels','shapes','equality','strict'],advanced:['temperature','seed','logprobs']},
  };
  const descriptor=profiles[key];if(!descriptor)return '';
  const values={...profileDefaults(key,config),...config[key]},schema=state.bootstrap.config_schema.$defs[descriptor.schema];
  const labels={mode:'Arrival pattern',requests:'Scheduled requests',requests_per_second:'Requests per second',burst_size:'Requests per burst',interval_seconds:'Seconds between bursts',max_pending:'Maximum pending requests',seed:'Random seed',prefix_chars:'Shared prefix (characters)',repeats:'Repeated requests',history_turns:'Conversation history turns',modes:'Conversation history',sessions:'Conversations',concurrency:'Concurrent conversations',max_tokens:'Turn cap (tokens)',delay_seconds:'Turn pause (seconds)',turns:'Conversation questions',saved_answers:'Prepared answers',repetitions:'Repetitions per case',levels:'Concurrent requests',shapes:'Question sizes',equality:'Compare answers',temperature:'Temperature',strict:'Require unanimous answers'};
  const hints={mode:'Choose even spacing or bursts of requests.',requests:'Total scheduled requests before the run limits apply.',requests_per_second:'Used only with even spacing.',burst_size:'Used only with bursts; requests scheduled together in each burst.',interval_seconds:'Used only with bursts; time between groups.',max_pending:'Maximum scheduled requests waiting for an available slot.',seed:'Keep this number to repeat the same arrival pattern.',prefix_chars:'Repeated opening text length; characters are different from tokens.',repeats:'Repeat each shared-prefix case this many times.',history_turns:'Prepared conversation turns included in the history comparison.',modes:'Prepared uses saved replies; earlier model replies uses answers from this run.',sessions:'Conversations to run for each chosen history type.',concurrency:'Conversations allowed at once, within the run request ceiling.',max_tokens:'Answer cap for each turn, within the run output cap.',delay_seconds:'Wait this long before the next turn in a conversation.',repetitions:'Answers per question-size and load combination.',levels:'For example: 1, 2, 4. Each level must fit within the run request ceiling.',shapes:'Repeat the selected question sizes at every load.',equality:'Exact text compares characters; ignore extra whitespace normalizes spacing.',temperature:'Sampling temperature sent with repeated questions.',strict:'Require agreement fails if repeated answers differ. Wrong known answers can still fail either mode.'};
  const render=name=>{
    const fieldName=`profile_${key}_${name}`,value=values[name];
    if(key==='consistency'&&name==='logprobs')return textField(fieldName,'Token logprob comparison (JSON)',typeof value==='string'?value:JSON.stringify(value ?? null,null,2),{rows:6,maxLength:4096,hint:'null disables it. {} enables complete-response diagnostics. Options: top_logprobs (0–20), strict, tolerance (logprob units; required for strict), support (discover/required), priming_rounds (0–2), compare_levels. Repetitions become corpus rounds using the existing load levels; see the request estimate. This measures token probabilities, not tokens per second.'});
    const choices=name==='modes'?[['fixed','Prepared history'],['live','Earlier model replies'],['fixed,live','Both history types']]:name==='shapes'?[['short','Short questions'],['long','Long questions'],['short,long','Short and long questions']]:name==='strict'?[['false','Report consistency'],['true','Require agreement']]:name==='equality'?[['bytes','Exact text'],['whitespace','Ignore extra whitespace']]:null;
    if(choices){const selected=Array.isArray(value)?value.join(','):String(value);if(!choices.some(([item])=>item===selected))choices.push([selected,'Saved selection']);return selectField(fieldName,labels[name],choices.map(([item,label])=>`<option value="${escape(item)}" ${item===selected?'selected':''}>${label}</option>`).join(''),{hint:hints[name]});}
    if(['turns','saved_answers'].includes(name))return textField(fieldName,labels[name],(value || []).join('\n'),{hint:'Editing replaces this list. Keep prepared answers aligned with the saved conversation questions.'});
    return schemaField(fieldName,value,schema.properties[name],{label:labels[name],hint:hints[name]});
  };
  return `<input type="hidden" name="profile_edits_${key}" value="[]"><button type="button" class="text-button reset-profile" data-action="reset-profile" data-profile="${key}">Reset this profile</button><div data-profile-settings="${key}"><fieldset class="fields">${descriptor.fields.map(render).join('')}</fieldset>${descriptor.advanced?`<details class="check-advanced"><summary>More ${escape(checkGuide[key].name.toLowerCase())} settings</summary><fieldset class="fields">${descriptor.advanced.map(render).join('')}</fieldset></details>`:''}</div>`;
}
function checkCoverageWarning(check,config) {
  const minimum=config.limits.min_samples;
  let planned;
  if(check.id==='consistency')planned=(config.consistency || profileDefaults('consistency',config)).repetitions;
  if(check.id==='mixed')planned=(config.buckets || bucketFormDefaults(config)).mixed_pairs;
  if(check.id==='buckets'){
    const buckets=config.buckets || bucketFormDefaults(config);
    planned=Math.min(...buckets.levels.map(level=>Math.max(buckets.samples_per_bucket,level)));
  }
  return planned!=null&&planned<minimum?`Too few planned samples for coverage: ${planned} in a group; ${minimum} required.`:'';
}
function checkOverrideWarning(check,config,recovery=false) {
  const messages=[];
  for(const [name,label,setting] of [['concurrency','Requested concurrency','Concurrency ceiling'],['max_output_tokens','Requested answer cap','Output cap (tokens)'],['request_timeout_seconds','Requested deadline','Request timeout (seconds)']]){
    const value=config.test_options?.[check.id]?.[name];
    if(value!=null&&value>config[name])messages.push(`${label} ${checkSettingValue(name,value)} exceeds run limit ${checkSettingValue(name,config[name])}.${recovery?` Reset this check’s overrides or raise ${setting} in Run limits.`:''}`);
  }
  return messages.join(' ');
}
function effectiveCheckSummary(check,config) {
  const parts=[];
  if(check.id==='access')parts.push('1 short request');
  else if(check.id==='capacity')parts.push(`${config.traffic.rates.join(' → ')} requests/s`,`${config.traffic.duration_seconds}s at each rate`);
  else if(check.id==='buckets')parts.push(`${(config.buckets || bucketFormDefaults(config)).levels.join(', ')} requests at a time`,'9 size combinations per level');
  else if(check.id==='gpu')parts.push(config.targets[0].metrics_url?'Metrics URL configured':'Metrics URL needed');
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
    if(check.id==='consistency'){parts.push(`${profile.repetitions} ${profile.logprobs?'corpus rounds':'repeats'}`,`${(profile.levels || []).join(', ')} requests at a time`);if(profile.logprobs)parts.push(`Logprobs · complete responses · ${profile.logprobs.strict?'strict':'diagnostic'}`);}
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
}
function sharedLimitsSummary(config) {
  return `Up to ${number(config.max_requests)} requests · ${elapsed(config.max_duration_seconds)} · ${number(config.concurrency)} at once`;
}
function updateSharedSettingsSummaries(config) {
  const ceilings=$('#run-ceilings-summary');if(ceilings)ceilings.textContent=sharedLimitsSummary(config);
  const requests=$('#request-defaults-summary');if(requests)requests.textContent=`· ${number(config.samples)} samples · ${number(config.max_output_tokens)} answer tokens · ${config.stream?'streaming':'complete answers'}`;
  const acceptance=$('#acceptance-summary');if(acceptance)acceptance.textContent=`· ${number((config.limits.min_correctness ?? 1)*100,1)}% correct · ${config.limits.latency_ms==null?'response time not set':ms(config.limits.latency_ms)}`;
}
function renderSharedRunSettings(config) {
  const primary=[
    ['max_requests','Max requests','Across all selected checks and endpoints. Reaching this budget can leave checks unfinished. AI evaluator calls have separate limits.'],
    ['max_duration_seconds','Max time (seconds)','Stop test traffic after this long. This does not include AI evaluation or saving the report.'],
    ['concurrency','Concurrency ceiling','A ceiling across target requests; individual checks may use fewer.'],
  ];
  const requestLabels={samples:'Samples per check',max_output_tokens:'Output cap (tokens)',request_timeout_seconds:'Request timeout (seconds)',context_limit:'Context window (tokens)'};
  const requestHelp={samples:'Default for checks that use a sample count; not the total run size. Explicit check overrides stay unchanged.',max_output_tokens:'Default and maximum answer length. Checks can request less; answers may finish sooner. Explicit check overrides stay unchanged.',request_timeout_seconds:'Default and maximum wait for one request. A check can use a shorter timeout. Explicit check overrides stay unchanged.',context_limit:'Use the context size configured on your server. Giraffe does not detect it.'};
  return `<section class="setup-section shared-run-settings" aria-labelledby="shared-settings-title"><details class="shared-settings-editor" id="shared-settings"><summary class="shared-settings-summary"><h2 id="shared-settings-title">Run limits</h2><span class="sr-only" id="run-ceilings-summary">${escape(sharedLimitsSummary(config))}</span><span class="shared-settings-edit">Edit</span></summary><div class="setup-body">
  <div class="fields three run-limit-fields">${primary.map(([key,label,help])=>field(key,label,config[key],{type:'number',hint:'',help,min:key==='max_duration_seconds'?.1:1,max:key==='concurrency'?256:undefined,step:key==='max_duration_seconds'?'any':1,required:true})).join('')}</div>
  <details class="setup-disclosure" id="shared-run-limits"><summary>Request settings <span class="dim" id="request-defaults-summary"></span></summary><div class="fields">${['samples','max_output_tokens','request_timeout_seconds','context_limit'].map(key=>field(key,requestLabels[key],config[key],{type:'number',min:key==='request_timeout_seconds'?.1:key==='context_limit'?128:1,step:key==='request_timeout_seconds'?'any':1,required:true,hint:'',help:requestHelp[key]})).join('')}${selectField('stream','How answers arrive',`<option value="true" ${config.stream?'selected':''}>Stream answers as they are generated</option><option value="false" ${config.stream?'':'selected'}>Wait for the complete answer</option>`,{hint:'',help:'First-answer timing and interruption checks need streaming.'})}</div></details>
  <details class="setup-disclosure" id="global-acceptance"><summary>Pass rules <span class="dim" id="acceptance-summary"></span></summary><div class="fields">${field('limit_max_error_rate','Errors allowed (%)',config.limits.max_error_rate*100,{type:'number',hint:'',help:'0% allows no request errors. Used by checks that support this criterion; a check-specific value takes priority.',min:0,max:100,step:'any'})}${field('limit_min_correctness','Correct answers (%)',config.limits.min_correctness*100,{type:'number',hint:'',help:'100% requires every scored answer to be correct. Used by checks that support this criterion. A check-specific value takes priority; diagnostics with fixed criteria still require every tested case to pass.',min:0,max:100,step:'any'})}${field('limit_min_samples','Minimum usable answers',config.limits.min_samples,{type:'number',hint:'',help:'Required per measured group where used. Too few leaves the result inconclusive.',min:state.bootstrap.limits_schema.properties.min_samples.minimum,step:1,required:true})}</div><h4>Timing, slowdown & comparison targets</h4><div class="fields">${limitFields.map(([key,label,unit])=>field(`limit_${key}`,label,config.limits[key],{type:'number',hint:unit,min:key==='fairness_max_ratio'?1.001:.001,step:'any',placeholder:'Measure only'})).join('')}${field('limit_regression_percent','Regression threshold (%)',config.limits.regression_percent,{type:'number',hint:'',help:'Used when comparing runs; percentage points for rates.',min:0,step:'any'})}</div></details></div></details><p class="checks-plan-summary sr-only" id="checks-plan-summary" role="status">Preparing request estimate…</p></section>`;
}
function renderCheckSelection(config) {
  return `<section class="setup-section check-selection" id="test-selection"><h2 class="sr-only" tabindex="-1">Check selection</h2><div class="setup-body">

  <div class="catalogue-toolbar"><div class="filter-group" aria-label="Show checks"><button type="button" class="text-button" data-action="catalogue-filter" data-catalogue-filter="selected" aria-pressed="${state.catalogueFilter==='selected'}">Selected (<span id="catalogue-selected-count">${config.checks.length}</span>)</button><button type="button" class="text-button" data-action="catalogue-filter" data-catalogue-filter="all" aria-pressed="${state.catalogueFilter!=='selected'}">All checks</button></div><div class="catalogue-bulk"><button type="button" class="text-button" data-action="select-tests">Select all</button><button type="button" class="text-button" data-action="clear-tests">Clear</button></div></div>
  <p class="sr-only" id="selection-summary" role="status">${config.checks.length} checks selected</p>
  <div id="catalogue-browse-tools" ${state.catalogueFilter==='selected'?'hidden':''}><div class="catalogue-browse-header"><label class="catalogue-search"><span class="sr-only">Find a check</span><input type="search" id="check-search" placeholder="Find a check" value="${escape(state.catalogueSearch || '')}" autocomplete="off"></label><button type="button" class="text-button" data-action="quick-checks">Reset to defaults</button></div><div class="catalogue-categories" aria-label="Check categories"><button type="button" class="catalogue-filter" data-action="catalogue-category" data-category="all" aria-pressed="${!state.catalogueCategory||state.catalogueCategory==='all'}">All categories</button>${checkGroups.map(group=>`<button type="button" class="catalogue-filter" data-action="catalogue-category" data-category="${group.id}" aria-pressed="${state.catalogueCategory===group.id}">${group.title}</button>`).join('')}</div></div>
  <div id="test-catalogue"><div class="test-cards check-catalogue" id="test-cards">${checkGroups.map(group=>{const chosen=group.checks.filter(id=>config.checks.includes(id)).length;return `<section class="catalogue-group${chosen?' has-selection':''}" data-catalogue-group="${group.id}" aria-labelledby="group-${group.id}-title"><div class="catalogue-group-heading"><div class="group-title"><h3 id="group-${group.id}-title">${group.title}</h3><p class="group-blurb">${group.blurb}</p></div><span class="group-count" data-group-count>${chosen} of ${group.checks.length}</span><button type="button" class="text-button group-toggle" data-action="select-group" data-category="${group.id}" data-group-toggle>${chosen===group.checks.length?'Clear':'Select all'}</button></div><div class="catalogue-group-cards">${group.checks.map(id=>renderTestCard(state.bootstrap.checks.find(check=>check.id===id),config)).join('')}</div></section>`;}).join('')}</div><p class="hint catalogue-empty" id="catalogue-empty" role="status" hidden></p></div>
  <p class="hint" id="no-tests-selected" ${config.checks.length?'hidden':''}>Choose at least one check before reviewing the run.</p>
  </div></section>${renderSharedRunSettings(config)}`;
}
function testSettingsLabel(check,config) {
  const custom=Object.keys(config.test_options?.[check.id] || {}).length>0||
    (check.id==='capacity'&&!sameConfig(config.traffic,state.bootstrap.config.traffic));
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
    samples:({generation:'Repeat the generated long-answer request.',fairness:'Short-only baseline samples for the competing-load comparison.',context:'Minimum requests across the generated context cases. Calibration and retries can add requests.',correctness:'Minimum generated questions; every built-in case is covered.',json:'Minimum generated JSON questions; every built-in case is covered.',cancellation:'Requests for normal controls and again for follow-up health checks.',recovery:'Short health requests after the load period.'})[check.id] || 'Requests used by this check.',
    concurrency:'Requests sent together in this check. The run ceiling still applies.',
    context_limit:'Declared server context length, used to size generated questions. This does not change the server.',
    max_output_tokens:'A lower answer cap for this check. It cannot exceed the run output cap.',
    request_timeout_seconds:'A shorter deadline for the connection request. It cannot exceed the run request deadline.',
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
  const overridden=Object.hasOwn(overrides,name)&&(kind==='limit'||overrides[name]!=null);
  const defaultValue=kind==='limit'?(Object.hasOwn(config.limits || {},name)?config.limits[name]:check.defaults.limits[name]):optionDefault(config,check,name);
  const value=kind==='limit'?limitValue(config,check,name):optionValue(config,check,name);
  const origin=(kind==='limit'&&Object.hasOwn(config.limits || {},name))||config[name]!=null?'run default':'check default';
  const optionalTarget=kind==='limit'&&state.bootstrap.limits_schema.properties[name]?.anyOf?.some(item=>item.type==='null');
  if(value==null&&optionalTarget)return overridden&&defaultValue!=null?`Measure only · ${origin}: ${checkSettingValue(name,defaultValue)}`:'Measure only';
  if(overridden)return `Custom · ${origin}: ${checkSettingValue(name,defaultValue)}`;
  return `${human(origin)}${showValue?`: ${checkSettingValue(name,value)}`:''}`;
}
function updateCheckFieldHints(config) {
  const form=$('#new-run-form');
  if(form)for(const check of state.bootstrap.checks)for(const name of ['concurrency','max_output_tokens','request_timeout_seconds']){
    const input=form.elements[`test_${check.id}_${name}`];if(!input)continue;
    if(typeof config[name]==='number'&&Number.isFinite(config[name]))input.max=String(config[name]);
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
  for(const reset of $$('[data-reset-test]'))reset.hidden=!Object.keys(config.test_options?.[reset.dataset.resetTest] || {}).length;
}
function markCheckHint(markup,check,name,kind) {
  return markup.replace('class="hint" id=',`class="hint" data-check-field-hint data-check-id="${check.id}" data-setting-name="${name}" data-setting-kind="${kind}" id=`);
}
function renderTestCard(check,config) {
  const guide=checkGuide[check.id],selected=config.checks?.includes(check.id),custom=Object.keys(config.test_options?.[check.id] || {}).length>0;
  const warningsVisible=selected||state.openTest===check.id;
  const ownQuestions=check.id==='correctness'&&config.request_source==='request_pack';
  const optionLabels={samples:'Sample requests',concurrency:'Concurrent requests',context_limit:'Context length (tokens)',max_output_tokens:'Answer cap (tokens)',request_timeout_seconds:'Connection timeout (seconds)',sustained_seconds:'Load duration (seconds)',metrics_max_age_seconds:'Metric freshness (seconds)',backend_verification:'Verify server cancellation',server_log:'Server log path',trace_file:'Trace file path',cancel_after_ms:'Cancel after (ms)',deadline_probe_ms:'Probe deadline (ms)',cancel_after_chunks:'Chunks before cancellation',cleanup_seconds:'Cleanup wait (seconds)',trace_wait_seconds:'Trace wait (seconds)'};
  const advancedOptions=new Set(['backend_verification','server_log','trace_file','cancel_after_chunks','cleanup_seconds','trace_wait_seconds']);
  const runOptions=new Set(['concurrency','max_output_tokens','request_timeout_seconds']);
  const option=name=>markCheckHint(schemaField(`test_${check.id}_${name}`,optionValue(config,check,name),state.bootstrap.option_schema.properties[name],{label:check.id==='correctness'&&name==='samples'?'Minimum questions':optionLabels[name] || human(name),helpKey:`test_${name}`,help:checkFieldHelp(check,name),hint:checkFieldHint(check,config,name),placeholder:'Use default'}),check,name,'option');
  const limitLabels={first_output_ms:'First answer (ms)',latency_ms:'Completion limit (ms)',stream_gap_ms:'Stream pause (ms)',min_output_tokens_per_second:'Minimum rate (tok/s)',fairness_max_ratio:'Maximum slowdown (ratio)',max_error_rate:'Errors allowed (%)',min_correctness:'Correct answers (%)',min_samples:'Minimum usable answers'};
  const primaryLimits=new Set(['first_output_ms','latency_ms','min_correctness','max_error_rate','fairness_max_ratio','min_samples']);
  const limit=name=>markCheckHint(['min_correctness','max_error_rate'].includes(name)?field(`test_${check.id}_limit_${name}`,limitLabels[name],limitValue(config,check,name)*100,{type:'number',min:0,max:100,step:'any',required:true,helpKey:`limit_${name}`,help:checkFieldHelp(check,name,'limit'),hint:checkFieldHint(check,config,name,'limit')}):schemaField(`test_${check.id}_limit_${name}`,limitValue(config,check,name),state.bootstrap.limits_schema.properties[name],{label:limitLabels[name] || human(name),helpKey:`limit_${name}`,help:checkFieldHelp(check,name,'limit'),placeholder:'Measure only',hint:checkFieldHint(check,config,name,'limit')}),check,name,'limit');
  const inactive=name=>ownQuestions&&['samples','concurrency'].includes(name);
  const requestFields=check.option_fields.filter(name=>!inactive(name)&&!advancedOptions.has(name)&&!runOptions.has(name));
  const requests=requestFields.map(option).join('');
  const inherited=check.option_fields.filter(name=>!inactive(name)&&runOptions.has(name)).map(option).join('');
  const diagnostics=check.option_fields.filter(name=>advancedOptions.has(name)).map(option).join('');
  const criteriaFields=check.limit_fields.filter(name=>primaryLimits.has(name));
  const criteria=criteriaFields.map(limit).join('');
  const moreCriteria=check.limit_fields.filter(name=>!primaryLimits.has(name)).map(limit).join('');
  const hostsQuestions=check.id===(config.checks.includes('correctness')?'correctness':config.checks.includes('capacity')?'capacity':null);
  const sharedQuestions=config.checks.includes('correctness')&&config.checks.includes('capacity');
  const questionSource=hostsQuestions?`<div class="catalogue-question-source">${sharedQuestions?`<p class="hint question-source-scope">Also used by ${escape(catalogTitle('capacity'))}.</p>`:''}${renderRequestSource(config)}</div>`:'';
  const inactiveControls=ownQuestions?`<div hidden data-inactive-question-settings>${check.option_fields.filter(inactive).map(option).join('')}</div>`:'';
  return `<section class="test-card catalogue-row" data-test-card="${check.id}" data-category="${checkGroups.find(group=>group.checks.includes(check.id))?.id || ''}" data-search="${escape(`${guide.name} ${guide.purpose} ${check.title} ${check.id}`.toLowerCase())}">
  <div class="test-card-heading"><div class="test-row-copy"><div class="test-selection-line"><label class="test-selection"><input type="checkbox" name="check" value="${check.id}" ${selected?'checked':''}><span>${escape(guide.name)}</span></label>${renderCheckInfo(check,config)}</div><p class="test-description">${escape(guide.purpose)}</p></div><div class="test-row-actions"><span class="test-setting-state" data-setting-state>${escape(testSettingsLabel(check,config))}</span><button type="button" class="button small configure-test" data-configure-test="${check.id}" aria-label="Settings for ${escape(guide.name)}" aria-expanded="false" aria-controls="settings-${check.id}"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 6h7M15 6h1M4 14h1M9 14h7"/><circle cx="13" cy="6" r="2"/><circle cx="7" cy="14" r="2"/></svg>Settings</button></div></div>
  <p class="hint check-row-warning" data-check-override-warning="${check.id}" ${warningsVisible&&checkOverrideWarning(check,config)?'':'hidden'}>${escape(checkOverrideWarning(check,config,true))}</p>
  <p class="hint check-row-warning" data-check-coverage="${check.id}" ${warningsVisible&&checkCoverageWarning(check,config)?'':'hidden'}>${escape(checkCoverageWarning(check,config))}</p>
  <input type="hidden" name="test_mode_${check.id}" value="${custom?'custom':'default'}">
  <div class="test-editor" id="settings-${check.id}" data-test-editor="${check.id}" hidden>
  <div class="test-editor-head"><span class="eyebrow">${escape(checkGroups.find(group=>group.checks.includes(check.id))?.title || 'Check')}</span><h3 tabindex="-1" data-editor-title>${escape(guide.name)}</h3><button type="button" class="button ghost icon-button" data-close-test="${check.id}" aria-label="Close ${escape(guide.name)} settings"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 5 10 10M15 5 5 15"/></svg></button></div>
  <p class="hint draft-notice" ${selected?'hidden':''}>Saved draft · select this check to run it.</p>
  ${guide.needs?`<p class="check-prerequisite"><strong>Needs:</strong> ${escape(guide.needs)}.</p>`:''}${questionSource}${check.id==='capacity'?renderTrafficEditor(config):''}${check.id==='gpu'?`<div class="metrics-field">${field('metrics_url','First metrics URL',config.targets[0].metrics_url,{type:'url',placeholder:'http://localhost:9400/metrics'})}</div>`:''}
  <div class="test-settings${requestFields.length===1&&criteriaFields.length===1?' simple-primary-settings':''}" data-test-settings="${check.id}">
  ${check.option_fields.length||check.limit_fields.length?`<button type="button" class="text-button reset-test" data-reset-test="${check.id}" aria-label="Reset overrides for ${escape(guide.name)}" ${custom?'':'hidden'}>Reset overrides</button>`:''}${inactiveControls}
  ${requests?`${requestFields.length>1?'<h4>Requests</h4>':''}<fieldset class="fields check-request-fields">${requests}</fieldset>`:''}
  ${criteria?`${criteriaFields.length>1?'<h4>Pass criteria</h4>':''}<fieldset class="fields check-criteria-fields">${criteria}</fieldset>`:''}
  ${inherited?`<details class="check-advanced check-run-overrides"><summary>Override run defaults</summary><fieldset class="fields">${inherited}</fieldset></details>`:''}
  ${moreCriteria?`<details class="check-advanced"><summary>More pass criteria</summary><fieldset class="fields">${moreCriteria}</fieldset></details>`:''}
  ${diagnostics?`<details class="check-advanced"><summary>Server verification & cleanup</summary><fieldset class="fields">${diagnostics}</fieldset></details>`:''}
  ${check.id==='buckets'?renderBucketEditor(config):renderProfileEditor(check,config)}</div>
  <div class="test-editor-footer"><button type="button" class="button small" data-done-test="${check.id}">Done</button></div></div></section>`;
}

function bucketFormDefaults(config) {
  const defaults=clone(state.bootstrap.buckets.defaults);
  defaults.levels=defaults.levels.filter(level=>level<=config.concurrency);
  defaults.output_tokens=Object.fromEntries(Object.entries(defaults.output_tokens).map(([name,value])=>[name,Math.min(value,config.max_output_tokens)]));
  return defaults;
}
function renderBucketSizes(settings) {
  return `<div class="bucket-sizes"><h3>Questions and answer caps</h3><p class="hint">Giraffe sends all 9 combinations below at each load. Question lengths are in characters; answer caps are in tokens (pieces of text). A cap limits an answer; it does not guarantee that length.</p><div class="fields three">${['short','medium','long'].map((size,index)=>field(`buckets_input_${size}`,`Question ${index+1} (characters)`,settings.input_chars[size],{type:'number',min:1,max:65536,step:1,required:true})).join('')}${['short','medium','long'].map((size,index)=>field(`buckets_output_${size}`,`Answer ${index+1} (tokens)`,settings.output_tokens[size],{type:'number',min:1,max:65536,step:1,required:true,hint:index===0?'Asks for the box label only.':'Asks for the label, then a list of numbers.'})).join('')}</div><p class="hint">Generated questions include “The box label is K7P4.” Longer-answer cases also ask for a list of numbers. Instructions can add characters; the request plan shows the actual length sent.</p></div>`;
}
function renderBucketEditor(config) {
  const settings=config.buckets || bucketFormDefaults(config),schema=state.bootstrap.buckets.schema;
  const basic=false;
  return `<fieldset class="fields" data-bucket-settings>${basic?'':field('buckets_levels','Concurrent requests',settings.levels.join(', '),{required:true,full:true,placeholder:'1, 4, 8',hint:'For example: 1, 4, 8 tests one request at a time, then four, then eight.'})}${schemaField('buckets_samples_per_bucket',settings.samples_per_bucket,schema.properties.samples_per_bucket,{label:'Requests per combination',hint:'Each load sends at least this many requests for each of the 9 combinations.'})}${schemaField('buckets_window_seconds',settings.window_seconds,schema.properties.window_seconds,{label:'Combination timeout (seconds)',hint:'Waiting for an available request slot uses this time too.'})}</fieldset>${basic?'':`<details class="check-advanced"><summary>Question lengths & answer caps · 9 combinations</summary>${renderBucketSizes(settings)}</details>`}<p class="hint bucket-profile-summary"></p><p class="hint" id="bucket-shared-limit">The shared simultaneous-request limit is ${config.concurrency}. Question lengths and answer caps are also used by Short-request slowdown.</p>`;
}
function renderLoadGuidance(config) {
  const messages=[];
  if(config.checks.includes('buckets')&&config.buckets){
    const highest=Math.max(...config.buckets.levels);
    if(highest>config.concurrency)messages.push(notice(`The highest tested level (${highest}) exceeds the run simultaneous request limit (${config.concurrency}). Raise the limit or lower the tested levels.`,'warning'));
    if(Object.values(config.buckets.output_tokens).some(value=>Number(value)>config.max_output_tokens))messages.push(`${notice(`A selected answer cap exceeds the run output cap of ${number(config.max_output_tokens)} tokens. Lower the answer cap or explicitly edit the run output cap before planning.`,'warning')}<button type="button" class="text-button" data-action="edit-output-cap">Edit run output cap</button>`);
  }
  return messages.join('');
}
function renderTrafficEditor(config) {
  const traffic=config.traffic || state.bootstrap.config.traffic,custom=!sameConfig(traffic,state.bootstrap.config.traffic),schema=state.bootstrap.traffic_schema;
  const labels={rates:'Rates (requests/second)',duration_seconds:'Seconds per rate',arrival:'Arrival pattern',max_in_flight:'Concurrent requests',seed:'Random seed',long_input_chars:'Long question (characters)',long_output_words:'Long answer (words)',drain_timeout_seconds:'Completion wait (seconds)',scheduler_lag_tolerance_ms:'Start delay (ms)'};
  const primary=new Set(['rates','duration_seconds','max_in_flight']);
  const control=([name,fieldSchema])=>schemaField(`traffic_${name}`,traffic[name],fieldSchema,{label:labels[name] || human(name),hint:name==='max_in_flight'?`Leave blank to use the shared limit of ${config.concurrency}. This test cannot exceed that limit.`:undefined});
  const entries=Object.entries(schema.properties).filter(([name])=>name!=='mix'),mix=schema.$defs.TrafficMix;
  return `<div class="traffic-editor"><input type="hidden" name="traffic_mode" value="${custom?'custom':'default'}"><div class="test-editor-heading"><h3>Traffic to send</h3><button class="text-button reset-test" type="button" data-action="reset-traffic">Restore traffic defaults</button></div><p class="hint">Each rate is a separate stage. If all request slots are busy, extra arrivals count as dropped.</p><div data-traffic-settings><fieldset class="fields">${entries.filter(([name])=>primary.has(name)).map(control).join('')}</fieldset><details class="check-advanced"><summary>Arrival pattern, request mix & timing</summary><fieldset class="fields">${entries.filter(([name])=>!primary.has(name)).map(control).join('')}<div class="full traffic-mix-heading"><h4>Request mix</h4><p class="hint">Higher weights mean a larger share of traffic. For example, weights 3, 1 and 1 send about 60% short requests, 20% long questions and 20% long answers.</p></div>${Object.entries(mix.properties).map(([name,s])=>schemaField(`traffic_mix_${name}`,traffic.mix[name],s,{label:({short:'Short requests',long_input:'Long questions',long_output:'Long answers'})[name]})).join('')}</fieldset></details></div></div>`;
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
    if(check.id==='gpu')form.elements.metrics_url.disabled=!open;
  }
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
  revealCatalogueCheck(config.checks.includes('correctness')?'correctness':'capacity');
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
  if(input.closest('[data-traffic-settings]'))form.elements.traffic_mode.value='custom';
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
      let message='';try{(input.name==='profile_consistency_logprobs'?parseLogprobSettings:parseToolFixtures)(input.value);}catch(error){message=error.message;}
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
    if(state.config[key])continue;
    const edits=new Set(JSON.parse(form.elements[`profile_edits_${key}`]?.value || '[]'));
    for(const [name,value] of Object.entries(profileDefaults(key,config))){
      const input=form.elements[`profile_${key}_${name}`];if(!input||edits.has(name))continue;
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
    if(packSettingInactive(input.name))continue;
    const card=input.closest('[data-test-card]');
    if(card){
      const id=card.dataset.testCard;
      if(!$('input[name="check"]',card).checked)continue;
      if(input.closest('[data-test-settings]')&&!input.closest('[data-bucket-settings]')&&!input.closest('[data-profile-settings]')&&form.elements[`test_mode_${id}`].value!=='custom')continue;
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
    for(let parent=input.parentElement;parent;parent=parent.parentElement)if(parent.tagName==='DETAILS')parent.open=true;
    const panel=input.closest('[data-setup-panel]');if(panel)state.setupStep=Number(panel.dataset.setupPanel);
    state.configView='form';syncConfigView();syncSetupStep();input.focus?.();input.reportValidity();return false;
  }
  return true;
}
function refreshInheritedFields(id,onlyInherited=false) {
  const check=state.bootstrap.checks.find(item=>item.id===id),form=$('#new-run-form');
  const config=clone(state.config);for(const [name] of numericFields)config[name]=Number(form.elements[name].value);if(form.elements.samples)config.samples=Number(form.elements.samples.value);
  config.limits={...config.limits};for(const name of check.limit_fields){const input=form.elements[`limit_${name}`];if(input)config.limits[name]=input.value===''?null:Number(input.value)/(['max_error_rate','min_correctness'].includes(name)?100:1);}
  for(const name of check.option_fields){const input=form.elements[`test_${id}_${name}`];if(!onlyInherited||input.dataset.inherited==='true'){input.value=optionDefault(config,check,name) ?? '';input.dataset.inherited='true';}}
  for(const name of check.limit_fields){const input=form.elements[`test_${id}_limit_${name}`];if(!onlyInherited||input.dataset.inherited==='true'){input.value=config.limits[name]==null?'':config.limits[name]*(['max_error_rate','min_correctness'].includes(name)?100:1);input.dataset.inherited='true';}}
}
const inputRetentionNotice='Questions and expected answers are saved with the run. Response retention controls which model answers and evaluator reasons are saved.';
function configChanged() {
  state.configRevision++;
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
  const settings={judges:[],concurrency:2,request_timeout_seconds:30,max_duration_seconds:300,max_output_tokens:256,...config.judging};
  return `<div class="judging-editor"><div class="test-editor-heading"><h3>AI evaluator</h3><button type="button" class="text-button" data-action="add-judge" ${settings.judges.length>=8?'disabled':''}>Add evaluator</button></div><p class="hint">An AI model compares each answer with your expected answer and accepts correct paraphrases.</p><input type="hidden" name="judge_count" value="${settings.judges.length}">${settings.judges.map((judge,index)=>`<fieldset class="judge-row"><legend>Evaluator ${index+1}</legend><div class="fields">${field(`judge_${index}_model`,'Model',judge.model)}${field(`judge_${index}_url`,'Endpoint URL',judge.url,{type:'url',full:true,placeholder:'http://localhost:9000/v1'})}</div><details class="evaluator-options"><summary>Credentials & evaluator name</summary><div class="fields">${field(`judge_${index}_name`,'Name',judge.name)}${field(`judge_${index}_api_key_env`,'API key environment variable (optional)',judge.api_key_env,{full:true,placeholder:'JUDGE_API_KEY'})}</div></details><button type="button" class="text-button" data-action="remove-judge" data-judge-index="${index}" aria-label="Remove evaluator ${index+1}">Remove</button></fieldset>`).join('')}<details class="judge-limits"><summary>Evaluation limits</summary><div class="fields">${field('judging_concurrency','Evaluator concurrency',settings.concurrency,{type:'number',min:1,max:256,step:1})}${field('judging_request_timeout_seconds','Evaluator timeout (seconds)',settings.request_timeout_seconds,{type:'number',min:0,step:'any'})}${field('judging_max_duration_seconds','Evaluation duration (seconds)',settings.max_duration_seconds,{type:'number',min:0,step:'any'})}${field('judging_max_output_tokens','Evaluator cap (tokens)',settings.max_output_tokens,{type:'number',min:1,step:1})}</div><p class="hint">More connection and authentication settings are available in JSON.</p></details><p class="hint judge-disclosure">Questions, expected answers and model answers are sent to your configured evaluators. With several evaluators, more than half must agree. A tie or missing vote leaves the result inconclusive.</p></div>`;
}
function changeJudge(action,index) {
  try {
    assertAppliedConfig();captureFormDraft();
    state.config.judging ||= {judges:[],concurrency:2,request_timeout_seconds:30,max_duration_seconds:300,max_output_tokens:256,ca_bundle:null,proxy:null};
    const judges=state.config.judging.judges;
    if(action==='add'){
      if(judges.length>=8)return;
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
  return `<section class="setup-section" id="setup-dataset"><div class="setup-body"><div class="request-source"><fieldset class="dataset-source"><legend>Questions ${infoHelp('request_source','Questions','Use built-in known-answer examples, or add or import your own questions and expected answers. Each saved question runs once in Answer correctness. Sample count and requests-at-a-time settings are not used for that check with saved questions. Capacity under load uses the same source when selected; other checks use generated requests. Switching sources keeps your saved questions. Try one question to inspect the answer before testing them all.')}</legend><label><input type="radio" name="request_source" value="builtin" ${custom?'':'checked'}><span>Built-in examples</span></label><label><input type="radio" name="request_source" value="request_pack" ${custom?'checked':''}><span>My questions</span></label></fieldset>${custom?`<input type="file" id="request-pack-file" accept=".csv,.json,text/csv,application/json" hidden><div class="pack-actions actions"><button type="button" class="button small" data-action="add-example">+ Add question</button><button type="button" class="button small" data-action="import-pack">Import CSV / JSON</button><button type="button" class="text-button" data-action="sample-dataset">Use sample questions</button></div>${cases.length?`<div class="dataset-table question-table"><table><thead><tr><th><input type="checkbox" id="dataset-select-all" aria-label="Select all examples" ${cases.every(item=>state.datasetSelection.includes(item.fixture_id))?'checked':''}></th><th>Question / expected answer</th><th>Answer check</th><th>Actions</th></tr></thead><tbody>${cases.map((item,index)=>`<tr><td><input type="checkbox" data-dataset-select="${escape(item.fixture_id)}" aria-label="Select ${escape(item.fixture_id)}" ${state.datasetSelection.includes(item.fixture_id)?'checked':''}></td><td><strong>${escape(item.fixture_id)}</strong><span class="dataset-excerpt">${escape(item.messages.map(message=>message.content).join(' / '))}</span><small class="dataset-excerpt">Expected: ${Object.hasOwn(item,'expected')?escape(typeof item.expected==='string'?item.expected:JSON.stringify(item.expected)):'Not set'}${item.messages.length>1?` · ${number(item.messages.length)} messages`:''}</small></td><td>${answerChecks[item.scorer]}${item.scorer==='judge'?`<small data-evaluator-needed ${judgesReady(config)?'hidden':''}>Needs evaluator</small>`:''}</td><td><div class="dataset-row-actions"><button type="button" class="text-button" data-action="try-example" data-case-index="${index}" aria-label="Try ${escape(item.fixture_id)}">Try one</button><button type="button" class="text-button" data-action="edit-example" data-case-index="${index}" aria-label="Edit ${escape(item.fixture_id)}">Edit</button><button type="button" class="text-button" data-action="remove-example" data-case-index="${index}" aria-label="Remove ${escape(item.fixture_id)}">Remove</button></div></td></tr>`).join('')}</tbody></table></div><p class="hint dataset-count">${number(cases.length)} questions</p>`:'<div class="dataset-empty"><h3>Add your first question</h3><p class="hint">Enter a question and its expected answer, or use the sample questions to get started.</p></div>'}`:` ${state.bootstrap.builtin_question_example?`<details class="builtin-example"><summary>View an example</summary><p>${escape(state.bootstrap.builtin_question_example.question)}</p><p><strong>Expected answer:</strong> <code>${escape(state.bootstrap.builtin_question_example.expected)}</code></p><p class="hint">Exact match after whitespace normalization; extra explanation does not match.</p></details>`:''}`}</div>${renderAnswerChecks(config)}${custom&&cases.length?`<details class="setup-disclosure dataset-advanced"><summary>Advanced dataset settings</summary><p class="hint">${inputRetentionNotice}</p><div id="request-pack-preview"></div></details>`:''}</div></section>`;
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
    datasetDialog(previous?'Edit question':'Add question',`<form id="dataset-example-form"><div class="dialog-body"><div id="example-messages">${renderExampleMessages(item.messages)}</div><button type="button" class="text-button" data-action="add-message">Add conversation message</button><div class="example-check">${selectField('example_scorer','Answer check',Object.entries(answerChecks).map(([key,label])=>`<option value="${key}" ${item.scorer===key?'selected':''}>${label}</option>`).join(''))}</div>${textField('example_expected','Expected answer',Object.hasOwn(item,'expected')?(item.scorer==='json'?JSON.stringify(item.expected,null,2):item.expected):'',{wrapperId:'example-expected-label',hintId:'example-expected-hint',hint:'The answer you want the model to give.'})}${textField('example_schema','JSON schema',JSON.stringify(item.schema || {},null,2),{wrapperId:'example-schema-label'})}<details class="example-advanced"><summary>Advanced question settings</summary>${textField('example_rubric','Evaluator instructions (optional)',item.rubric,{rows:2,wrapperId:'example-rubric-label'})}<div class="fields">${field('example_id','Question ID',item.fixture_id,{full:true})}${field('example_weight','Traffic weight',item.weight,{type:'number',min:0,step:'any'})}${field('example_max_tokens','Output cap (optional)',item.max_tokens,{type:'number',min:1,step:1})}</div>${textField('example_options','Extra request options · JSON',JSON.stringify(item.options || {},null,2))}</details><div id="dataset-error" class="error-message" role="alert" hidden></div></div><div class="dialog-footer"><button type="button" class="button" data-action="close-dataset">Cancel</button><button type="submit" class="button primary">Save question</button></div></form>`);
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
  $('#example-expected-hint').textContent=({json:'Enter a JSON value. Leave blank to check only the schema. Use null for a null answer.',judge:'Accepts answers with the same meaning.',exact:'Wording, capitalization and punctuation must match. Extra spaces and line breaks are ignored.',contains:'The answer must include this text. Capitalization matters.'})[scorer] || '';
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
    datasetEditingAllowed();if(file.size>1024*1024)throw new Error('Dataset exceeds 1 MiB.');
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
function openTryExample(index) {
  try{
    datasetEditingAllowed();captureFormDraft();const item=datasetCases()[index];if(!item)return;
    state.datasetTry={fixture_id:item.fixture_id,revision:state.configRevision,route:state.routeVersion};
    datasetDialog('Try one example',`<form id="dataset-try-form"><div class="dialog-body"><p><strong>${escape(item.fixture_id)}</strong></p><p class="hint">Sends this question once to the selected server, then checks the answer${item.scorer==='judge'?' with your configured AI evaluators':''}. No traffic stages or restart commands run.</p><div class="example-check">${selectField('try_target','Target',state.config.targets.map(target=>`<option value="${escape(target.name)}">${escape(target.name)} · ${escape(target.model || 'model needed')}</option>`).join(''))}</div><label class="trial-retention"><input name="save_try_output" type="checkbox" checked><span>Save this trial’s answer and judge reasons</span></label><p class="hint">Save the answer and evaluator reasons in this trial report so you can inspect them. If unchecked, only measurements and results are saved. This does not change what your main run saves.</p><div id="dataset-error" class="error-message" role="alert" hidden></div></div><div class="dialog-footer"><button type="button" class="button" data-action="close-dataset">Cancel</button><button type="submit" class="button primary" ${state.active?'disabled':''}>Send one request</button></div></form>`);
  }catch(error){configError(error);}
}
async function tryDatasetExample(event) {
  event.preventDefault();const draft=state.datasetTry;if(!draft||state.active||state.submitting)return;
  try{
    datasetEditingAllowed();if(draft.route!==state.routeVersion||draft.revision!==state.configRevision)throw new Error('Configuration changed. Reopen Try one example.');
    const config=readForm(true).config,target_name=$('#dataset-try-form').elements.try_target.value;
    config.retention=$('#dataset-try-form').elements.save_try_output.checked?'all':'none';
    state.submitting=true;
    const result=await api('/api/runs/try-example',{method:'POST',body:JSON.stringify({config,fixture_id:draft.fixture_id,target_name})});
    setActive(result.id);closeDatasetDialog();if(draft.route===state.routeVersion)location.hash=linkRun(result.id);else toast('Example run started. Open the active run to inspect it.');
  }catch(error){datasetError(error.message);}finally{state.submitting=false;}
}
function currentRequestPlan() {
  const saved=state.requestPreview;
  return !hasJsonChanges()&&saved?.status==='current'&&saved.revision===state.configRevision&&saved.route===state.routeVersion?saved.plan:null;
}
function canStartRun() {
  return Boolean(currentRequestPlan())&&!state.submitting&&!state.configBusy&&!state.active&&state.modelDiscovery?.status!=='pending';
}
function renderReviewPassCriteria(config) {
  if(!config?.limits||!state.bootstrap?.checks)return '';
  const labels={first_output_ms:'First output within',latency_ms:'Completion limit',stream_gap_ms:'Stream pause',min_output_tokens_per_second:'Minimum generation rate',fairness_max_ratio:'Maximum slowdown',max_error_rate:'Errors allowed',min_correctness:'Correct answers',min_samples:'Minimum usable answers'};
  const rows=config.checks.map(id=>{
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
function renderReviewLoad(config) {
  if(!config?.checks.includes('capacity')||!config.traffic)return '';
  const traffic=config.traffic,concurrency=Math.min(traffic.max_in_flight ?? config.concurrency,config.concurrency);
  return `<p class="hint review-load">Capacity target rates: ${escape(traffic.rates.join(' → '))} requests/s · ${escape(traffic.duration_seconds)}s per rate · up to ${number(concurrency)} at once${traffic.arrival==='poisson'?' · random arrivals':''}.</p>`;
}
function renderRequestPlan(config=null) {
  const element=$('#request-plan');if(!element)return;
  const saved=state.requestPreview,plan=currentRequestPlan();
  if(!config&&$('#run-blueprint')){try{config=readForm(true).config;}catch{}}
  config ||= (plan?saved.config:null) || state.config;
  syncRunBlueprint(config,plan);
  const start=$('#start-run');if(start)start.disabled=!canStartRun();
  const checkPlan=$('#checks-plan-summary');if(checkPlan){checkPlan.textContent=plan?`${setupPlanCount(plan)}${plan.required_request_limit>plan.request_limit?' · budget is too small for full coverage':''}`:saved?.status==='error'?`Request estimate unavailable: ${saved.error}`:'Updating request estimate…';checkPlan.classList.toggle('budget-warning',Boolean(plan&&plan.required_request_limit>plan.request_limit));}
  const dock=$('#dock-summary');if(dock&&config){const selected=config.checks?.length || 0;const baselineName=(state.baselines || []).find(item=>item.id===$('#new-run-form')?.elements?.baseline_id?.value)?.name;dock.innerHTML=state.setupStep===3?`<strong>${number(selected)}</strong> ${selected===1?'check':'checks'} <span>·</span> stops at ${number(config.max_requests)} requests or ${escape(elapsed(config.max_duration_seconds))}${baselineName?` <span>·</span> compared with <strong>${escape(baselineName)}</strong>`:''}`:`<strong>${number(selected)}</strong> ${selected===1?'check':'checks'} <span>·</span> ${plan?escape(setupPlanCount(plan)):'Planning…'}`;}
  const heading='<div class="review-section-heading"><h3>Traffic</h3><button type="button" class="text-button" data-action="edit-run-limits">Edit limits</button></div>';
  const limits=config?`<dl class="review-limits"><div><dt>Request limit</dt><dd>${number(config.max_requests)}</dd></div><div><dt>Traffic time limit</dt><dd>${elapsed(config.max_duration_seconds)}</dd></div><div><dt>Maximum at once</dt><dd>${number(config.concurrency)}</dd></div></dl>`:'';
  if(!plan){
    const status=hasJsonChanges()?'Apply or discard JSON changes to confirm the request plan.':saved?.status==='error'?`Request plan unavailable: ${saved.error}`:saved?.status==='pending'?'Updating request plan…':'Request plan stale · updating current settings…';
    element.innerHTML=`<div class="plan-hero">${heading}<p class="hint plan-status" role="status">${escape(status)}</p>${limits}${saved?.status==='error'&&!hasJsonChanges()?'<button type="button" class="button small" data-action="preview-pack">Retry request plan</button>':''}</div>`;return;
  }
  const count=setupPlanCount(plan);
  const evaluator=plan.evaluator || {},needed=plan.required_request_limit;
  const raise=Number.isInteger(needed)&&needed>plan.request_limit;
  const evaluatorScope='AI judge calls run separately from the target request budget and send cases and generated answers to each judge.';
  const warnings=[...new Set([...(evaluator.readiness_errors || []),...(plan.warnings || [])])].filter(warning=>!evaluator.active||warning!==evaluatorScope);
  const filled=Math.min(100,(plan.planned_total ?? plan.known_total ?? 0)/Math.max(1,plan.request_limit)*100);
  element.innerHTML=`<div class="plan-hero${raise?' is-over':''}">${heading}<p class="plan-total" role="status">${escape(count)}</p><div class="plan-ceiling${plan.count_kind==='exact'||plan.count_kind==='estimate'?'':' adaptive'}" aria-hidden="true"><span style="width:${raise?100:filled.toFixed(1)}%"></span></div>${limits}${renderReviewLoad(config)}${raise?`<div class="notice warning">The request budget is too small to complete the planned checks.</div><div class="actions"><button type="button" class="button small" data-action="use-request-budget" data-budget="${needed}" data-revision="${state.configRevision}" data-route="${state.routeVersion}">Use ${number(needed)} total requests</button><button type="button" class="text-button" data-action="edit-load">Reduce load or checks</button></div>`:''}${warnings.map(warning=>notice(warning,'warning')).join('')}</div><div class="plan-flight">${renderFlightPlan(plan)}${evaluator.active?`<div class="review-evaluator"><p>Up to ${number(evaluator.call_upper_bound)} evaluator calls after target traffic · ${number(evaluator.judge_count)} ${evaluator.judge_count===1?'evaluator':'evaluators'}.</p><p class="hint">Questions, expected answers and model answers go to every configured evaluator. These calls have separate limits.</p>${config?.judging?.judges?.length?`<ul class="review-evaluator-targets">${config.judging.judges.map(judge=>`<li><strong>${escape(judge.model || 'Model needed')}</strong> · ${escape(judge.url || 'Endpoint URL needed')}</li>`).join('')}</ul>`:''}</div>`:''}<details class="request-planning"><summary>Request breakdown</summary><p class="hint">${plan.planned_per_target==null?`${number(plan.known_per_target)} known requests per endpoint, plus adaptive work`:`${plan.count_kind==='estimate'?'About ':''}${number(plan.planned_per_target)} requests per endpoint`}. Time and request limits can leave checks unfinished.</p>${Object.keys(plan.target_quotas || {}).length?`<h4>Endpoint request budgets</h4><ul class="review-quotas">${Object.entries(plan.target_quotas).map(([name,quota])=>`<li>${escape(name)}: up to ${number(quota)} requests</li>`).join('')}</ul>`:''}<div class="plan-rows">${(plan.rows || []).filter(row=>row.requests_per_target!==0).map(row=>`<article><strong>${escape(checkGuide[row.id]?catalogTitle(row.id):row.title)}</strong><span>${row.requests_per_target==null?'Adaptive / bounded':`${row.kind==='estimate'?'About ':''}${number(row.requests_per_target)} per endpoint`}</span><p class="hint">${escape(row.summary)} · ${escape(row.source)}</p></article>`).join('')}</div>${plan.bucket_cases?.length?`<h4>Question and answer sizes</h4><ul class="plan-cases">${plan.bucket_cases.map(item=>`<li>${number(item.actual_input_chars)} characters sent${item.actual_input_chars!==item.input_chars?` (${number(item.input_chars)} configured, plus instructions)`:''} · answer cap ${number(item.output_tokens)} tokens</li>`).join('')}</ul>`:''}${plan.bucket_levels?.length?`<p class="hint">${plan.bucket_levels.map(level=>`${number(level.concurrency)} at once: ${number(level.requests_per_case)} requests per combination, ${number(level.requests_per_target)} per endpoint`).join(' · ')}</p>`:''}</details></div>${renderReviewPassCriteria(config)}`;
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
  renderRequestPlan();
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
    const result=await api(draft?'/api/config/draft':'/api/config/preview',{method:'POST',body:JSON.stringify(config)});
    if(version!==state.routeVersion||revision!==state.configRevision||sequence!==state.previewSequence)throw new Error('Configuration changed during preview. Refresh the current settings before running.');
    assertAppliedConfig();
    const previous=state.requestPreview;
    state.requestPreview={status:'current',revision,route:version,plan:result.request_plan,config:result.config,
      data:draft?previous?.data:result.request_pack_preview,
      packRevision:draft?(previous?.packRevision ?? -1):revision,
      packConfig:draft?previous?.packConfig:result.config};renderRequestPreview();
    return result;
  }catch(error){
    if(version===state.routeVersion&&revision===state.configRevision&&sequence===state.previewSequence){state.requestPreview={...state.requestPreview,status:'error',error:error.message};renderRequestPreview();}
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
  const titles=['Point Giraffe at an endpoint.','Choose your checks.','Ready for launch.'];
  const heading=$('#setup-title');if(heading)heading.textContent=titles[step-1];
  const lede=$('#setup-lede');if(lede)lede.textContent=['Paste your model server’s address. Giraffe sends one tiny request to check it.','Pick what to test. Each check sends a limited number of requests.','Nothing is sent until you press Launch. You can stop at any time.'][step-1];
  syncSetupIdentity();
  const blueprint=$('#run-blueprint');if(blueprint)blueprint.hidden=step!==2||state.configView==='json';
  for(const panel of $$('[data-setup-panel]'))panel.hidden=Number(panel.dataset.setupPanel)!==step;
  for(const button of $$('.setup-stepper [data-setup-step]')){
    const index=Number(button.dataset.setupStep);
    button.classList.toggle('is-current',index===step);button.classList.toggle('is-complete',index<step);
    if(index===step)button.setAttribute('aria-current','step');else button.removeAttribute('aria-current');
  }
  const json=state.configView==='json';
  const next=$('[data-action="setup-next"]'),back=$('[data-action="setup-back"]');
  const needsModel=step===1&&!$('#new-run-form')?.elements.model.value.trim();
  next.hidden=!json&&(step===3||needsModel);next.innerHTML=json?'Return to setup':`${state.returnToReview?'Return to review':step===2?'Review run':'Choose checks'} ${icon('arrow')}`;
  next.disabled=state.submitting||hasJsonChanges()||(!json&&needsModel);back.hidden=step===1||json;back.disabled=state.submitting||hasJsonChanges();
  $('#start-run').hidden=step!==3||json;$('#start-run').disabled=!canStartRun();
  const navigationHint=$('#setup-navigation-hint');navigationHint.textContent=step===3?'Press ⌘↵ to launch. You can stop at any time.':'';navigationHint.hidden=step!==3;
  const tools=$('.configuration-tools');if(tools)tools.hidden=step!==3&&!json;
  const runSummary=$('#run-bar-summary');if(runSummary)runSummary.hidden=true;
  $('.new-run-layout')?.classList.toggle('connecting',step===1||json);
  $('.new-run-layout')?.classList.toggle('configuring',step===2&&!json);
  if(focus){const target=step===3?$('#setup-title'):step===2?$('#test-selection h2'):$(`[data-setup-panel="${step}"] h2`);if(step!==2)target?.setAttribute('tabindex','-1');target?.focus({preventScroll:true});window.scrollTo?.({top:0,left:0,behavior:'instant'});}
}
function revealSetupStep(step,{focus=false}={}) {
  state.setupStep=step;if(step===3)state.returnToReview=false;
  if($('#config-panel-form')){state.configView='form';syncConfigView();}
  syncSetupStep({focus});
}
function validateSetupChecks() {
  const config=readForm(true).config;
  if(!config.checks.length){state.catalogueFilter='all';syncTestControls();$('#check-search')?.focus();throw new Error('Select at least one check before reviewing the run.');}
  readForm(false);
  if(config.request_source==='request_pack'&&config.checks.some(id=>['correctness','capacity'].includes(id))){
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
        if(index===2){revealSetupStep(2);validateSetupChecks();}
      }
    }
    captureFormDraft();revealSetupStep(step,{focus:true});updateBounds();
  }catch(problem){if(error){error.textContent=problem.message;error.hidden=false;}else configError(problem);}
}
function setupPlanCount(plan) {
  return plan.count_kind==='exact'?`${number(plan.planned_total)} ${plan.planned_total===1?'request':'requests'} planned`:plan.count_kind==='estimate'?`About ${number(plan.planned_total)} requests`:`${number(plan.known_total)} known requests + adaptive checks`;
}
function renderReviewScope(payload) {
  const config=payload.config,targets=config.targets || [],questionChecks=config.checks.filter(id=>['correctness','capacity'].includes(id)),usesQuestions=questionChecks.length>0;
  const usesPack=usesQuestions&&config.request_source==='request_pack',cases=config.request_pack?.cases || [];
  const mixedSources=usesPack&&config.checks.some(id=>!['correctness','capacity','gpu'].includes(id));
  const source=usesQuestions?(usesPack?`Your questions · ${number(cases.length)} saved ${cases.length===1?'question':'questions'}${mixedSources?` for ${questionChecks.map(catalogTitle).join(' and ')}. Other checks use generated requests.`:''}`:'Built-in examples'):'';
  const baseline=payload.baseline_id?state.baselines.find(item=>item.id===payload.baseline_id):null;
  const comparison=payload.baseline_id?`Compare with saved run: ${baseline?.name || payload.baseline_id}`:'';
  const problems=[];
  if(targets.some(target=>!target.url?.trim()||!target.model?.trim()))problems.push('Enter an endpoint URL and model for every target.');
  if(!config.checks.length)problems.push('Select at least one check.');
  if(usesPack&&!cases.length)problems.push('Add a question or choose built-in examples.');
  if(usesPack&&cases.some(item=>item.scorer==='judge')&&!judgesReady(config))problems.push('Configure the AI evaluator for questions that need it.');
  const restart=payload.mode==='cold-start'?targets.find(target=>target.name===payload.restart_target):null;
  return `<section class="review-section" id="review-endpoints"><div class="review-section-heading"><h3>${targets.length===1?'Endpoint':'Endpoints'}</h3><button type="button" class="text-button" data-review-edit="true" data-setup-step="1">Edit connection</button></div><ul class="review-targets">${targets.map(target=>`<li>${targets.length>1?`<strong>${escape(target.name)}</strong>`:''}<dl class="review-identity"><div><dt>Model</dt><dd>${escape(target.model || 'Model needed')}</dd></div><div><dt>URL</dt><dd class="review-endpoint-url">${escape(target.url || 'Endpoint URL needed')}</dd></div></dl></li>`).join('')}</ul></section><section class="review-section" id="review-checks"><div class="review-section-heading"><h3>Checks</h3><button type="button" class="text-button" data-review-edit="true" data-setup-step="2">Edit checks</button></div><ul class="review-check-names">${config.checks.length?config.checks.map(id=>`<li>${escape(catalogTitle(id))}</li>`).join(''):'<li>No checks selected</li>'}</ul>${source?`<p class="hint review-question-source">${escape(source)}</p>`:''}</section><section class="review-section review-compare" id="review-compare"><div class="review-section-heading"><h3>Compare with</h3>${state.baselines?.length?`<button type="button" class="text-button" data-action="choose-baseline">${payload.baseline_id?'Change':'Choose baseline'}</button>`:''}</div>${comparison?`<p class="hint review-baseline">${escape(comparison)}</p><p class="hint">Results will show what changed since that run.</p>`:state.baselines?.length?`<p class="hint">Not compared. Choose a saved baseline to see what changed since that run.</p>`:'<p class="hint">No baselines yet. After a run, open its results and press <strong>Save as reference baseline</strong>. You can then compare later runs against it here.</p>'}</section>${problems.map(problem=>notice(problem,'warning')).join('')}${payload.mode==='cold-start'?notice(restart?.restart_command?.length?`Restart ${restart.name} before testing. This runs its configured restart command.`:'Restart selected, but no restart command is configured for that endpoint. Use the running server or configure a restart command.','warning'):''}`;
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
    if(state.autoModel&&form.elements.model.value===state.autoModel)form.elements.model.value='';
    state.autoModel=null;invalidateModelDiscovery();captureFormDraft();configChanged();updateBounds();
    form.elements.url.focus();
  }catch(error){configError(error);}
}
function modelDiscoveryKey() {
  const form=$('#new-run-form');if(!form)return null;
  return JSON.stringify({url:form.elements.url.value.trim(),model:form.elements.model.value.trim() || null,
    api_key_env:form.elements.api_key_env?.value.trim() || null,headers_env:state.config?.targets?.[0]?.headers_env || {},
    ca_bundle:state.config?.ca_bundle || null,proxy:state.config?.proxy || null,request_options:state.config?.request_options || {}});
}
function invalidateModelDiscovery() {
  state.modelDiscoverySequence++;state.modelDiscovery=null;renderModelDiscovery();
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
    const result=await api('/api/targets/test-connection',{method:'POST',body:key});
    if(sequence!==state.modelDiscoverySequence||route!==state.routeVersion||key!==modelDiscoveryKey())return;
    if(result.status==='connected'){
      if(!JSON.parse(key).model)state.autoModel=result.model;
      form.elements.model.value=result.model;form.elements.model.setCustomValidity('');
      state.config.targets[0].model=result.model;
      configChanged();updateBounds();
    }
    state.modelDiscovery={key:modelDiscoveryKey(),route,status:result.status,models:result.models || [],message:result.status==='connected'?`Connected · ${result.model}${result.latency_ms==null?' · latency unavailable':` · ${ms(result.latency_ms)}`}`:result.message};
    renderModelDiscovery();
    if(result.status==='connected')$('#model-options').open=false;
    if(result.status==='needs_model')(result.models?.length>1?$('#connection-model-choice'):form.elements.model)?.focus();
  }catch(error){
    if(sequence!==state.modelDiscoverySequence||route!==state.routeVersion||key!==modelDiscoveryKey())return;
    state.modelDiscovery={key,route,status:'error',models:[],message:error.message};renderModelDiscovery();
  }
}
function renderRunBlueprint(config,plan=null) {
  const traffic=(plan?.rows || []).filter(row=>row.requests_per_target!==0);
  const waiting=state.requestPreview?.status==='error'?'Plan unavailable. Review your configuration.':hasJsonChanges()?'Apply JSON changes to update this plan.':'Calculating traffic…';
  return `<header class="blueprint-heading"><h2>Run plan</h2></header>
  <p class="blueprint-intro">${escape(plan?setupPlanCount(plan):waiting)}</p>
  <dl class="workload-bounds"><div><dt>Request ceiling</dt><dd>${number(config.max_requests)}</dd></div><div><dt>Time limit</dt><dd>${elapsed(config.max_duration_seconds)}</dd></div><div><dt>Maximum at once</dt><dd>${number(config.concurrency)}</dd></div></dl>
  <p class="hint workload-help">Upper limits for the whole run, not predictions.</p><button type="button" class="text-button blueprint-edit" data-action="edit-run-limits">Edit limits</button>
  ${traffic.length?`<details class="workload-breakdown"><summary>Request breakdown</summary><div>${traffic.map(row=>`<details data-plan-row="${escape(row.id)}"><summary><strong>${row.requests_per_target==null?'Adaptive':`${row.kind==='estimate'?'~ ':''}${number(row.requests_per_target)}`}</strong> ${escape(row.title)}</summary><p>${escape(row.summary)}</p><small>${escape(row.source)} · ${row.requests_per_target==null?'bounded by your limits':'per endpoint, before run limits'}</small></details>`).join('')}</div></details>`:''}
  ${plan?.evaluator?.active?'<p class="blueprint-note">AI evaluation runs afterward with separate limits.</p>':''}${plan&&plan.required_request_limit>plan.request_limit?'<p class="blueprint-note">The request limit is below the planned workload; some checks may remain incomplete.</p>':''}`;
}
function syncRunBlueprint(config,plan=null) {
  const element=$('#run-blueprint');if(!element)return;
  const open=[...element.querySelectorAll('details[open]')].map(item=>item.dataset.planRow?['planRow',item.dataset.planRow]:['planCheck',item.dataset.planCheck]);
  const focused=document.activeElement?.closest?.('[data-plan-row], [data-plan-check]');
  const focusKey=focused?(focused.dataset.planRow?['planRow',focused.dataset.planRow]:['planCheck',focused.dataset.planCheck]):null;
  element.innerHTML=renderRunBlueprint(config,plan);
  for(const node of element.querySelectorAll('details')){
    if(open.some(([key,value])=>node.dataset[key]===value))node.open=true;
    if(focusKey&&node.dataset[focusKey[0]]===focusKey[1])node.querySelector('summary')?.focus({preventScroll:true});
  }
}
function renderNew(preset=inferSetupPreset(state.config)) {
  state.setupPreset=preset;
  if(preset==='custom')state.showAvailableTests=true;
  const c=state.config,target=c.targets?.[0] || {},limits=c.limits || {};
  $('#main').innerHTML=`${renderHeroCoat()}<div class="page-heading setup-page-heading"><div><h1 id="setup-title">Test your endpoint</h1><p class="setup-lede" id="setup-lede"></p></div><button type="button" class="text-button setup-import" data-action="import-config">Load saved setup</button></div>
  <div class="notice" id="new-active-notice" ${state.active?'':'hidden'}><strong>A run is already active.</strong> <a href="${state.active?linkRun(state.active):'#runs'}">Inspect or stop it</a> before starting another.</div>
  <form id="new-run-form" novalidate>
  <ol class="setup-stepper" aria-label="Run setup">${['Connect','Checks','Launch'].map((label,index)=>`<li><button type="button" class="setup-step" data-setup-step="${index+1}" aria-controls="setup-panel-${index+1}"><span class="setup-step-index" aria-hidden="true">${index+1}</span><strong>${label}</strong></button></li>`).join('')}</ol>
<div class="notice config-draft-notice" id="config-draft-notice" hidden><div><strong>Unapplied JSON changes.</strong> Apply or discard them before editing the form, running, exporting, or copying the CLI command.</div><div class="actions"><button class="button small" type="button" data-action="apply-config">Apply changes</button><button class="text-button" type="button" data-action="discard-config">Discard changes</button></div></div><div id="config-error" class="error-message config-action-error" role="alert" hidden></div><div class="panel cli-fallback" id="cli-fallback" hidden><label for="cli-command">Copy command</label><textarea id="cli-command" readonly spellcheck="false"></textarea></div><div id="setup-context" class="setup-context" hidden></div><div class="new-run-layout"><div id="config-panel-form" role="tabpanel" aria-labelledby="config-tab-form">
  <section class="setup-step-content" id="setup-panel-1" data-setup-panel="1" aria-label="Connect">
  <div class="connection-layout"><section class="setup-section" id="setup-deployment"><h2 class="sr-only">Connection settings</h2><div class="setup-body"><div class="fields">
  <div class="endpoint-connect">${field('url','Server address',target.url,{type:'url',required:true,full:true,placeholder:'http://localhost:11434/v1',hint:''})}<button type="button" class="button primary" data-action="test-connection">Connect</button></div></div><div class="setup-connection-status" id="model-discovery-status" role="status" aria-live="polite"></div>
  <div class="start-from"><div class="start-row"><span class="start-label">Quick fill</span><div class="runtime-shortcuts">${['Ollama','vLLM','SGLang','LM Studio','llama.cpp'].map(runtime=>`<button type="button" data-action="runtime-shortcut" data-runtime="${runtime}">${runtime}</button>`).join('')}</div></div>${renderRecentEndpoints()}<div class="start-row"><span class="start-label">Saved setup</span><div><button type="button" class="button small start-file" data-action="import-config"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 13V3.5M6.5 7 10 3.5 13.5 7M4 12.5v3A1.5 1.5 0 0 0 5.5 17h9a1.5 1.5 0 0 0 1.5-1.5v-3"/></svg>Load from file…</button></div></div></div>
  <div class="connection-details" id="connection-options">
  <div class="connection-row model-options" id="model-options"><label class="row-label" for="field-model">Model</label><div class="row-control"><label for="connection-model-choice" hidden>Available models</label><select id="connection-model-choice" hidden></select><div id="manual-model-field">${field('model','Model',target.model || '',{required:true,placeholder:'Found when you connect',hint:''})}</div><p class="hint" id="model-options-label"></p></div></div>
  <details class="connection-row connection-auth" id="connection-auth" ${target.api_key_env?'open':''}><summary><span class="row-label">API key</span><span class="row-value">${target.api_key_env?`<code>${escape(target.api_key_env)}</code>`:'Not needed for most local servers'}</span><span class="row-action">${target.api_key_env?'Change':'Add'}</span></summary><div class="row-control">${field('api_key_env','API key variable',target.api_key_env,{placeholder:'OPENAI_API_KEY',hint:'Enter the variable name, not the secret key. Set it on the machine running Giraffe before starting the UI.'})}</div></details>
  </div>
  ${(c.targets || []).length>1?`<div class="notice multi-target-notice"><strong>${c.targets.length} endpoints configured.</strong> This connection test checks the first endpoint. The run checks all configured endpoints.</div>`:''}
  </div></section></div>
  </section><section class="setup-step-content" id="setup-panel-2" data-setup-panel="2" aria-label="Choose checks" hidden>
  ${renderCheckSelection(c)}</section>
  <section class="setup-step-content" id="setup-panel-3" data-setup-panel="3" aria-label="Review and run" hidden>
  <section class="setup-section" id="setup-review"><h2 class="sr-only">Run configuration</h2><div class="setup-body">
  <div class="review-main"><div id="run-bounds"></div>
  <details class="setup-disclosure" id="execution-settings"><summary>Baseline & restart options</summary><div class="details-content"><div class="fields">${field('name','Endpoint name',target.name,{required:true,placeholder:'local'})}${selectField('mode','Server startup','<option value="normal">Use the running server</option><option value="cold-start">Restart one server before testing</option>')}<div id="restart-controls" hidden>${selectField('restart_target','Server to restart',(c.targets || []).map(t=>`<option value="${escape(t.name)}">${escape(t.name)}</option>`).join(''))}<p class="hint" id="restart-hook"></p><p class="hint">Runs only the restart command already configured for this server.</p></div>${selectField('baseline_id','Compare baseline',baselineOptions(state.prefillBaseline || ''))}</div></div></details>
  </div><section id="request-plan" class="request-plan review-section" aria-live="polite"></section><div id="load-guidance" role="status" aria-live="polite"></div>
  </div></section></section></div>
<section class="panel form-section" id="config-panel-json" role="tabpanel" aria-labelledby="config-tab-json" hidden><h2>Run configuration</h2><p class="hint">Edit run settings and extra options here. Apply changes before returning to the form or starting a run.</p><p class="hint">${inputRetentionNotice}</p>${renderField('config_editor','Configuration · JSON',`<textarea class="config-editor" id="config-editor" spellcheck="false">${escape(state.jsonDraft ?? JSON.stringify(c,null,2))}</textarea>`,{controlId:'config-editor'})}<div class="editor-actions"><button class="button small" type="button" data-action="apply-config">Apply changes</button><span class="hint" id="json-status" role="status"></span></div></section><aside id="run-blueprint" class="run-blueprint" aria-label="Preview of this run" hidden></aside>

  </div>
  <details class="configuration-tools" ${state.configView==='json'||hasJsonChanges()?'open':''}><summary>Advanced options</summary><div class="config-toolbar"><div class="result-tabs config-tabs" role="tablist" aria-label="Configuration view"><button type="button" role="tab" id="config-tab-form" data-config-tab="form" aria-controls="config-panel-form">Guided setup</button><button type="button" role="tab" id="config-tab-json" data-config-tab="json" aria-controls="config-panel-json">Advanced editor</button></div><div class="actions"><button type="button" class="text-button" data-action="import-config">Load saved setup</button><button type="button" class="text-button" data-action="export-config">Save setup</button><button type="button" class="text-button" data-action="copy-cli">Copy terminal command</button></div><input type="file" id="config-file" accept=".json,application/json" hidden></div></details>
  <div class="run-action-bar setup-navigation"><div><span class="dock-summary" id="dock-summary" aria-hidden="true"></span><strong id="run-bar-summary" hidden></strong><p class="hint" id="setup-navigation-hint">No test requests are sent automatically.</p></div><div class="actions"><button class="button" type="button" data-action="setup-back" hidden>Back</button><button class="button primary" type="button" data-action="setup-next">Continue ${icon('arrow')}</button><button class="button primary" type="submit" id="start-run" ${state.active?'disabled':''} hidden>Launch <kbd>⌘ ↵</kbd></button></div><div id="form-error" class="error-message" role="alert" hidden></div></div>
  </form>`;
  for(const check of state.bootstrap.checks){
    const options=c.test_options?.[check.id] || {};
    for(const name of check.option_fields)$('#new-run-form').elements[`test_${check.id}_${name}`].dataset.inherited=String(options[name]==null);
    for(const name of check.limit_fields)$('#new-run-form').elements[`test_${check.id}_limit_${name}`].dataset.inherited=String(!Object.hasOwn(options.limits || {},name));
  }
  const form=$('#new-run-form');form.elements.mode.value=state.formMode;
  if(state.formRestartTarget&&c.targets.some(t=>t.name===state.formRestartTarget))form.elements.restart_target.value=state.formRestartTarget;
  if(state.jsonDraft===null){state.jsonDraft=JSON.stringify(portableConfig(readForm(true)),null,2);state.jsonBase=state.jsonDraft;$('#config-editor').value=state.jsonDraft;}
  syncTestControls();updateBounds();updateRestart();syncConfigView();syncPackControls();renderRequestPreview();syncSetupStep();renderModelDiscovery();
}
function readForm(allowEmpty=false) {
  const form=$('#new-run-form'),data=new FormData(form);let config=clone(state.config);
  // Disabled editors still retain their values when a test is unchecked.
  for(const input of form.querySelectorAll('[data-test-editor] input, [data-test-editor] select, [data-test-editor] textarea'))data.set(input.name,input.value);
  if(!config.targets?.length)throw new Error('Configuration must contain at least one target.');
  const first=config.targets[0];for(const key of ['url','model','name'])first[key]=String(data.get(key) || '').trim();
  if(form.elements.api_key_env)first.api_key_env=form.elements.api_key_env.value.trim() || null;
  first.metrics_url=form.elements.metrics_url.value.trim() || null;
  for(const [key] of numericFields)config[key]=data.get(key)===''?null:Number(data.get(key));
  config.limits ||= {};
  for(const key of [...limitFields.map(x=>x[0]),'max_error_rate','min_correctness','min_samples','regression_percent']){if(!data.has(`limit_${key}`))continue;const v=data.get(`limit_${key}`);config.limits[key]=v===''?null:Number(v)/(['max_error_rate','min_correctness'].includes(key)?100:1);}
  if(form.elements.samples)config.samples=Number(form.elements.samples.value);
  if(form.elements.stream)config.stream=form.elements.stream.value==='true';
  config=configureTests(config,data,state.bootstrap.checks,state.bootstrap.config.traffic,{allowEmpty,allowInvalid:allowEmpty,percentageInputs:true});
  config=configureBuckets(config,data,bucketFormDefaults(config),{allowInvalid:allowEmpty});
  config=configureJudging(config,data,{allowInvalid:allowEmpty});
  config=configureProfiles(config,data,state.bootstrap.profile_defaults || {},{allowInvalid:allowEmpty});
  config.restart_target=null;
  return {config,baseline_id:data.get('baseline_id') || null,mode:data.get('mode') || 'normal',restart_target:data.get('mode')==='cold-start'?data.get('restart_target'):null};
}
function updateBounds() {
  const selected=$$('#new-run-form input[name="check"]:checked').length;
  $('#selection-summary').textContent=`${selected} checks selected`;
  let payload;try{payload=readForm(true);}catch{return;}
  const c=payload.config;
  syncSetupIdentity();
  const trafficLimitHint=$('#field-traffic_max_in_flight-hint');if(trafficLimitHint)trafficLimitHint.textContent=`Leave blank to use the shared limit of ${c.concurrency}. This test cannot exceed that limit.`;
  const bucketLimitHint=$('#bucket-shared-limit');if(bucketLimitHint)bucketLimitHint.textContent=`The shared simultaneous-request limit is ${c.concurrency}. Question lengths and answer caps are also used by Short-request slowdown.`;refreshDefaultProfileFields(c);
  const derived=$('#derived-concurrency');if(derived){
    derived.textContent=`At most ${number(c.concurrency)} ${c.concurrency===1?'request':'requests'} at once.`;
    const ceiling=$('#new-run-form').elements.concurrency;
    if(ceiling.value!==String(c.concurrency)){ceiling.value=c.concurrency;for(const check of state.bootstrap.checks)refreshInheritedFields(check.id,true);}
  }
  $('#load-guidance').innerHTML=renderLoadGuidance(c);
  const bucketSummary=$('.bucket-profile-summary');
  if(bucketSummary){const plan=bucketCoveragePlan({...c,checks:['buckets']});bucketSummary.textContent=plan?`${number(plan.perTarget)} requests per target. Each level runs 9 tests using short, medium and long questions and answers.`:'Enter the requests to run at a time and the minimum requests per test to see the total.';}
  updateDatasetReadiness(c);
  for(const check of state.bootstrap.checks){
    const card=$(`[data-test-card="${check.id}"]`);
    updateCheckRow(check,c,card);
  }
  $('#run-bounds').innerHTML=renderReviewScope(payload);
  $('#selection-summary').textContent=`${c.checks.length} ${c.checks.length===1?'check':'checks'} selected`;
  const summary=$('#run-bar-summary');if(summary){summary.textContent='';summary.hidden=true;}
  updateSharedSettingsSummaries(c);
  updateCheckFieldHints(c);renderRequestPlan(c);scheduleRequestPlan();
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
    if(draft.request_source==='request_pack'&&draft.checks.some(id=>['correctness','capacity'].includes(id))){
      if(!draft.request_pack?.cases?.length){revealQuestionEditor(draft);$('[data-action="add-example"]').focus();throw new Error('Add a question or choose built-in examples before testing.');}
      if(draft.request_pack.cases.some(item=>item.scorer==='judge')&&!judgesReady(draft)){revealSetupStep(2);configureEvaluator();throw new Error('Set up the AI evaluator to check questions that accept paraphrased answers.');}
    }
    if(!currentRequestPlan())throw new Error('Wait for the current request plan before starting. If planning failed, use Retry request plan.');
    state.submitting=true;$('#start-run').disabled=true;$('#start-run').textContent='Validating settings…';
    const payload=await validatedSnapshot();
    await refreshRequestPreview(payload.config);
    if(payload.mode==='cold-start'&&!payload.config.targets.find(t=>t.name===payload.restart_target)?.restart_command?.length)throw new Error('Cold start requires a configured restart_command for the selected target.');
    if(version!==state.routeVersion)return;
    $('#start-run').textContent='Starting suite…';
    const result=await api('/api/runs',{method:'POST',body:JSON.stringify(payload)});state.config=clone(payload.config);
    if(draft.request_source==='request_pack'&&!draft.request_pack&&!draft.checks.some(id=>['correctness','capacity'].includes(id)))state.config.request_source=draft.request_source;
    setActive(result.id);if(version===state.routeVersion)location.hash=linkRun(result.id);
  }catch(error){if(version!==state.routeVersion){toast(`Run was not started: ${error.message}`);return;}const errorEl=$('#form-error');errorEl.textContent=error.message;errorEl.hidden=false;if($('#start-run')){$('#start-run').disabled=true;$('#start-run').innerHTML=`Launch ${icon('arrow')}`;}}finally{state.submitting=false;const button=$('#start-run');if(button)button.disabled=!canStartRun();}
}
function reportConfig(report){return report?.manifest?.config || state.detail?.config || {};}
function mainFinding(report) {
  const checks=report.checks.filter(c=>c.id!=='baseline'),failed=checks.find(c=>c.status==='fail');
  if(!checks.length)return 'No checks ran';
  if(failed){if(failed.metrics?.scored&&failed.metrics.correct<failed.metrics.scored)return `${checkGuide[failed.id]?.name || failed.title}: ${failed.metrics.correct} of ${failed.metrics.scored} correct`;return `${checkGuide[failed.id]?.name || failed.title} failed`;}
  if(report.abort_reason)return 'Run stopped with partial results';
  const uncertain=checks.find(c=>['blocked','inconclusive'].includes(c.status));
  if(uncertain)return `${checkGuide[uncertain.id]?.name || uncertain.title} is ${uncertain.status}`;
  if(report.baseline?.status==='fail')return 'A material regression was detected';
  if(report.baseline?.status==='inconclusive')return 'The baseline comparison is inconclusive';
  if(checks.some(c=>c.status==='skipped'&&!unselectedCheck(c,report)))return 'Some selected checks did not run';
  if(checks.length&&checks.every(c=>c.status==='skipped'))return 'No checks ran';
  if(report.overall==='pass')return 'Selected acceptance checks passed';
  return 'Review the recorded acceptance results';
}
function unselectedCheck(check,report) {
  if(check.status!=='skipped')return false;
  const configured=reportConfig(report).checks;
  return Array.isArray(configured)?!configured.includes(check.id):/^Not selected for this run\.?$/i.test(check.summary || '');
}
function resultGuidance(report) {
  const checks=report.checks.filter(c=>c.id!=='baseline'),problem=checks.find(c=>c.status==='fail') || checks.find(c=>['blocked','inconclusive'].includes(c.status));
  if(problem){const scored=problem.metrics?.scored,correct=problem.metrics?.correct;return scored&&correct<scored?`${scored-correct} of ${scored} scored answers did not meet the correctness requirement. Inspect the returned answers to understand the failure.`:`${problem.summary || 'This check needs a closer look.'} Inspect the recorded evidence for this check.`;}
  if(report.abort_reason)return 'The evidence collected so far is saved below. Review the stopping reason and request limits before trying again.';
  if(report.baseline?.status==='fail')return 'Open the baseline comparison to see what regressed and whether the two runs are comparable.';
  if(report.baseline?.status==='inconclusive')return 'Open the baseline comparison to see what could not be compared. No improvement or regression is established.';
  if(checks.some(c=>c.status==='skipped'&&!unselectedCheck(c,report)))return 'Open “Not run” to see why selected checks were skipped. Review their settings before trying again.';
  if(!checks.length||checks.every(c=>c.status==='skipped'))return 'No check produced a result. Review settings to choose what to test, then start a new run.';
  if(report.overall==='pass')return 'The recorded checks met their configured limits. Save a baseline to compare your next change. This result applies to the endpoint and traffic tested here.';
  return 'Review the selected checks and their recorded reasons, then adjust the settings for your next run.';
}
function findingEvidence(report) {
  const check=report.checks.find(c=>c.id!=='baseline'&&c.status==='fail') || report.checks.find(c=>c.id!=='baseline'&&['blocked','inconclusive'].includes(c.status));
  if(!check)return {check:null,record:null};
  const ids=new Set(check.evidence_ids || []),records=report.requests.filter(record=>ids.has(record.id));
  const record=records.find(record=>record.score===false) || records.find(record=>['failed','timed_out'].includes(record.status)) || records[0];
  return {check,record};
}
function inspectFinding() {
  const {check,record}=findingEvidence(state.detail.report);
  if(record){openRequest(record.id);return;}
  if(check)state.selectedCheck=`${check.target}:${check.id}`;
  state.tab='checks';renderDetail();
  const heading=$('.check-detail h2');heading?.setAttribute('tabindex','-1');heading?.focus();
}
function renderDetail() {
  const d=state.detail;if(activeState(d)){renderLive();return;}
  const report=d.report;
  if(!report){$('#main').innerHTML=`<div class="page-heading"><div><div class="eyebrow">Run ${escape(d.id)}</div><h1>Run could not finish</h1><p>No report was saved. Review the error and settings before trying again.</p></div></div>${notice(d.error || 'The runner did not produce a report.','error')}<div class="actions"><button class="button primary" data-action="rerun">Review settings</button><a class="button" href="#runs">Back to runs</a></div>`;return;}
  const config=reportConfig(report),checks=report.checks.filter(c=>c.id!=='baseline'),counts={};checks.forEach(c=>counts[c.status]=(counts[c.status] || 0)+1);
  const required=checks.filter(c=>c.required);
  const absolute=checks.some(c=>c.status==='fail')?'fail':required.some(c=>c.status==='blocked')?'blocked':report.abort_reason||!required.length||required.some(c=>c.status==='inconclusive')||checks.some(c=>c.status==='skipped'&&!unselectedCheck(c,report))?'inconclusive':required.every(c=>c.status==='skipped')?'skipped':'pass';
  const completePass=d.state==='completed'&&!d.error&&!report.abort_reason&&report.overall==='pass'&&checks.some(c=>c.status==='pass')&&checks.every(c=>c.status==='pass'||unselectedCheck(c,report));
  const finding=findingEvidence(report);
  const savedBaseline=(state.baselines || []).find(item=>item.run_id===d.id);
  const compareNext=!completePass&&!checks.some(c=>['fail','blocked','inconclusive'].includes(c.status))&&!report.abort_reason&&['fail','inconclusive'].includes(report.baseline?.status);
  const entering=state.enteredRun!==d.id;state.enteredRun=d.id;
  $('#main').innerHTML=`<div class="page-heading result-heading ${absolute}${entering?' is-entering':''}"><div><div class="eyebrow">Run <span class="mono">${escape(report.run_id)}</span></div>${badge(absolute,`Acceptance ${statusLabels[absolute]?.toLowerCase()}`)}<div class="verdict-word ${absolute}">${escape(statusLabels[absolute])}</div><h1>${escape(mainFinding(report))}</h1><p class="result-guidance">${escape(resultGuidance(report))}</p><div class="run-meta"><span><strong>${escape((config.targets || []).map(t=>t.model).join(', '))}</strong></span><span>${date(report.started_at)}</span><span>${duration(report)}</span><span>${number(report.requests.length)} requests recorded</span></div><p class="hint">${escape((config.targets || []).map(t=>t.url).filter(Boolean).join(' · '))}</p></div><div class="actions">${savedBaseline?`<p class="baseline-saved"><span aria-hidden="true">✓</span> Saved as baseline <strong>${escape(savedBaseline.name)}</strong></p>`:''}${savedBaseline&&!finding.check?`<button class="button primary" data-action="compare-run">Compare a new run</button>`:completePass?'<button class="button primary" data-action="baseline-dialog">Save baseline</button>':compareNext?'<button class="button primary" data-tab="baseline">Inspect comparison</button>':finding.check?`<button class="button primary" data-action="inspect-finding">${finding.record?.score===false?'Inspect failed answer':finding.record?'Inspect evidence':'Inspect check'}</button>`:'<button class="button primary" data-action="rerun">Review settings</button>'}${completePass||compareNext||finding.check?`<button class="button" data-action="rerun">${finding.check?'Review settings':`${icon('repeat')} Run again`}</button>`:''}</div></div>
  ${report.abort_reason?notice(`Partial run: ${report.abort_reason}`,'warning'):''}${d.error?notice(d.error,'error'):''}
  <div class="evidence-band${report.requests.length<=72?' is-compact':''}">${renderMosaic(orderedRequests(report))}${renderBeeswarm(orderedRequests(report),{width:report.requests.length<=72?500:960})}</div>
  <div class="result-summary"><div class="summary-bar" aria-hidden="true">${['fail','blocked','inconclusive','pass','skipped'].filter(k=>counts[k]).map(k=>`<span class="summary-seg ${k}" style="flex-grow:${counts[k]}"></span>`).join('')}</div><div class="summary-items">${['fail','blocked','inconclusive','pass','skipped'].filter(k=>counts[k]).map(k=>`<div class="summary-item ${k}"><strong data-count="${counts[k]}">${counts[k]}</strong>${badge(k,k==='skipped'?'Not run':undefined)}</div>`).join('')}${(config.targets || []).length>1?`<span class="hint summary-targets">${escape(config.targets.map(t=>`${t.name} · ${t.route || 'direct'}`).join(' / '))}</span>`:''}</div></div>
  <div class="result-tabs" role="tablist" aria-label="Run report"><button role="tab" id="tab-checks" aria-selected="${state.tab==='checks'}" tabindex="${state.tab==='checks'?0:-1}" aria-controls="result-content" class="${state.tab==='checks'?'selected':''}" data-tab="checks">Checks & evidence</button><button role="tab" id="tab-baseline" aria-selected="${state.tab==='baseline'}" tabindex="${state.tab==='baseline'?0:-1}" aria-controls="result-content" class="${state.tab==='baseline'?'selected':''}" data-tab="baseline">Baseline${report.baseline?` · ${escape(statusLabels[report.baseline.status] || report.baseline.status)}`:''}</button><button role="tab" id="tab-config" aria-selected="${state.tab==='config'}" tabindex="${state.tab==='config'?0:-1}" aria-controls="result-content" class="${state.tab==='config'?'selected':''}" data-tab="config">Run configuration</button></div>
  <section id="result-content" role="tabpanel" aria-labelledby="tab-${state.tab}">${state.tab==='checks'?renderChecks(report):state.tab==='baseline'?renderComparison(report):renderConfig(report)}</section>
  <div class="actions" style="margin-top:24px"><a class="button small" href="${runPath(d.id)}/report.html" download>Download HTML report</a><a class="button small" href="${runPath(d.id)}/report.json" download>Download JSON</a>${savedBaseline?`<button class="button small" data-action="compare-run">Compare a new run with this baseline</button><button class="button small" data-action="baseline-dialog">Save again under another name</button>`:!completePass&&d.state==='completed'?'<button class="button small" data-action="baseline-dialog">Save as reference baseline</button>':''}</div>`;
  syncComparisonFilters();
}
function keyMetric(check,report){if(check.status==='skipped')return report&&unselectedCheck(check,report)?'Not selected':check.summary || 'No result recorded';const m=check.metrics || {};if(check.id==='cancellation'&&m.cancellation_results)return `Server stopped: ${human(m.cancellation_results.server_stopped.status)} · Answer: ${human(m.cancellation_results.answer.status)}`;if(['context','correctness','json'].includes(check.id)&&m.scored)return `${m.correct} / ${m.scored} correct`;if(check.id==='capacity'&&Object.hasOwn(m,'highest_acceptable_rate_rps'))return m.highest_acceptable_rate_rps==null?'Arrival capacity unconfirmed':`${number(m.highest_acceptable_rate_rps,2)} requests/s ${m.request_pack?.control_status==='fail'?'loaded · quiet failed':'accepted'}`;if(check.id==='capacity'&&m.highest_tested_acceptable_concurrency!=null)return `Concurrency ${m.highest_tested_acceptable_concurrency} accepted`;if(check.id==='first_output')return `${ms(m.first_output_p95_ms)} p95 · ${m.attempted || 0} requests`;if(check.id==='generation')return `${ms(m.p95_ms)} p95 · ${m.attempted || 0} requests`;return m.attempted!=null?`${number(m.attempted)} requests · ${check.target}`:check.target;}
function renderChecks(report) {
  const checks=report.checks.filter(c=>c.id!=='baseline').sort((a,b)=>(order[a.status]??5)-(order[b.status]??5));
  if(!checks.length)return notice('This report contains no check results.');
  const selected=checks.find(c=>`${c.target}:${c.id}`===state.selectedCheck) || checks.find(c=>c.status!=='skipped');state.selectedCheck=selected?`${selected.target}:${selected.id}`:null;
  const attention=checks.filter(c=>!['pass','skipped'].includes(c.status)),passed=checks.filter(c=>c.status==='pass'),skipped=checks.filter(c=>c.status==='skipped');
  const checkButton=c=>`<button class="check-item ${c===selected?'selected':''}" data-check="${escape(`${c.target}:${c.id}`)}" aria-pressed="${c===selected}"><span><span class="check-name">${escape(checkGuide[c.id]?.name || c.title)}</span><small>${escape(keyMetric(c,report))}${(reportConfig(report).targets || []).length>1?` · ${escape(c.target)}`:''}</small></span>${badge(c.status,unselectedCheck(c,report)?'Not selected':undefined)}</button>`;
  return `<div class="result-grid"><div class="result-navigator ${state.checkNavigatorOpen?'is-expanded':''}"><button type="button" class="button check-navigator-toggle" data-action="toggle-checks" aria-expanded="${Boolean(state.checkNavigatorOpen)}" aria-controls="check-result-list">${state.checkNavigatorOpen?'Hide check list':'Browse check results'}</button><nav class="check-list" id="check-result-list" aria-label="Check results">${attention.length?`<h2 class="check-group-heading">Needs attention · ${attention.length}</h2>${attention.map(checkButton).join('')}`:''}${passed.length?`<h2 class="check-group-heading">Passed checks · ${passed.length}</h2>${passed.map(checkButton).join('')}`:''}${skipped.length?`<details class="checks-not-run" ${selected?.status==='skipped'?'open':''}><summary>Not run · ${skipped.length}</summary>${skipped.map(checkButton).join('')}</details>`:''}</nav></div><section class="panel check-detail">${selected?renderCheck(selected,report):'<div class="empty-state"><h2>No checks ran</h2><p>Open the check list to inspect recorded reasons, or review settings to choose your checks.</p><button class="button" data-action="rerun">Review settings</button></div>'}</section></div>`;
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
  const m=check.metrics || {},config=reportConfig(report),limits={...(config.limits || {}),...(config.test_options?.[check.id]?.limits || {})};
  const evidenceSet=new Set(check.evidence_ids || []),records=report.requests.filter(r=>evidenceSet.has(r.id));
  const sorted=[...records].sort((a,b)=>(a.score===false?-2:a.status==='failed'?-1:0)-(b.score===false?-2:b.status==='failed'?-1:0));
  const metrics=[];
  if(check.id==='gpu'){
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
    if(check.id==='first_output'){metrics.push(metric('Warmed first output · p95',ms(m.first_output_p95_ms),limits.first_output_ms?`Limit: ${ms(limits.first_output_ms)}`:'No acceptance limit set'));metrics.push(metric('Initial first output',ms(m.initial?.first_output_p50_ms),'Recorded separately from warmed samples'));}
    else if(check.id==='capacity'&&m.traffic_stages){metrics.push(metric(m.request_pack?.control_status==='fail'?'Highest acceptable tested loaded rate · quiet failed':'Highest acceptable tested arrival rate',m.highest_acceptable_rate_rps==null?'Unconfirmed':`${number(m.highest_acceptable_rate_rps,2)} requests/s`,m.request_pack?.control_status==='fail'?'Loaded result only; quiet checks failed. Maximum capacity unknown.':'Highest tested acceptable rate; maximum capacity unknown'));metrics.push(metric('Rate stages',number(m.traffic_stages.length),'Each stage is offered independently, then drained'));}
    else if(check.id==='capacity'){metrics.push(metric('Highest accepted load',m.highest_tested_acceptable_concurrency==null?'Unconfirmed':number(m.highest_tested_acceptable_concurrency),'Highest tested; maximum capacity unknown'));metrics.push(metric('Response time · p95',ms(m.p95_ms),`${m.completed ?? 0} valid completions`));}
    else if(check.id==='fairness'){metrics.push(metric('Mixed / short-only latency',m.latency_ratio==null?'Unmeasured':`${number(m.latency_ratio,2)}×`,limits.fairness_max_ratio?`Limit: ${limits.fairness_max_ratio}×`:'No slowdown limit set'));metrics.push(metric('Observed overlap',`${m.overlaps ?? 0} / ${m.pairs ?? 0}`,'Pairs with concurrent prefill'));}
    else {metrics.push(metric('Response time · p95',ms(m.p95_ms),limits.latency_ms?`Limit: ${ms(limits.latency_ms)}`:'No response-time limit set'));metrics.push(metric('Longest stream pause',ms(m.max_stream_gap_ms),limits.stream_gap_ms?`Limit: ${ms(limits.stream_gap_ms)}`:'No stream-pause limit set'));}
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
  ${m.request_pack?renderPackResults(m.request_pack):''}
  ${check.id==='capacity'&&m.traffic_stages?renderTrafficStages(m.traffic_stages,m.request_pack):''}
  ${check.id==='capacity'&&m.levels?`<div class="metric-table"><table><thead><tr><th>Load</th><th>Achieved</th><th>Correct</th><th>p95</th></tr></thead><tbody>${Object.entries(m.levels).map(([k,v])=>`<tr><td class="mono">${escape(k)}</td><td>${number(v.achieved_concurrency)}</td><td>${v.correct ?? 0} / ${v.scored ?? 0}</td><td class="mono">${ms(v.p95_ms)}</td></tr>`).join('')}</tbody></table></div>`:''}
  ${check.id==='gpu'&&m.snapshots?renderGpuSnapshots(m.snapshots):''}
  ${records.length?`<div class="evidence-title"><span>Requests</span><span class="hint">${records.length} recorded · wrong answers first</span></div><div class="evidence-table"><table><thead><tr><th>Fixture / scenario</th><th>Response</th><th>Answer</th><th>Time</th></tr></thead><tbody>${sorted.map(r=>`<tr><td><button class="text-button" data-request="${escape(r.id)}">${escape(r.fixture_id)}</button><small>${escape(r.scenario)}</small></td><td>${escape(r.status)}${isProbe(r)?'<small class="request-probe">Deliberate probe</small>':''}</td><td>${r.score===false?'<span class="status fail">Incorrect</span>':r.score===true?'<span class="status pass">Correct</span>':'<span class="dim">Unscored</span>'}</td><td class="mono nowrap">${ms(r.elapsed_ms)}</td></tr>`).join('')}</tbody></table></div>`:check.status==='skipped'?'':'<div class="check-observations hint">No request evidence was recorded for this check.</div>'}
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
function isProbe(r){return ['client_cancel','client_deadline','deadline_probe'].includes(r.scenario)||/intentional cancellation/i.test(r.error || '');}
function comparisonTable(rows) {
  return `<div class="comparison-table"><table><thead><tr><th>Measurement / scenario</th><th>Baseline</th><th>Current</th><th>Worsening</th><th>Result</th></tr></thead><tbody>${rows.map(row=>`<tr><td>${escape(row.metric==='output_tokens_per_second'?'End-to-end output rate':row.metric==='generation_tokens_per_second'?'Generation pace (estimate)':human(row.metric))}<small>${escape(row.scenario)}</small><small>${escape(row.reason || '')}</small></td><td class="mono">${comparisonValue(row,row.baseline)}<small>n=${number(row.baseline_samples)}</small></td><td class="mono">${comparisonValue(row,row.current)}<small>n=${number(row.current_samples)}</small></td><td class="mono">${row.worsening==null?'—':`${row.worsening>0?'+':''}${number(row.worsening,1)}${row.change_unit==='percentage_points'?' pp':'%'}`}<small>${row.threshold==null?'':`Threshold ${number(row.threshold,1)}${row.change_unit==='percentage_points'?' pp':'%'}`}</small></td><td>${badge(row.status)}</td></tr>`).join('')}</tbody></table></div>`;
}
function comparisonGroup(id,rows) {
  const status=rows.some(row=>row.status==='fail')?'fail':rows.some(row=>row.status==='inconclusive')?'inconclusive':rows.every(row=>row.status==='pass')?'pass':rows[0]?.status || 'inconclusive';
  const reasons=[...new Set(rows.map(row=>row.reason).filter(Boolean))];
  const counts=['fail','inconclusive','pass'].map(key=>[key,rows.filter(row=>row.status===key).length]).filter(([,count])=>count);
  return `<details class="comparison-check-group" data-comparison-group="${escape(id)}"><summary><span class="comparison-group-name">${escape(catalogTitle(id))}<small>${counts.map(([key,count])=>`${number(count)} ${key==='fail'?'regressed':key==='pass'?'without material regression':'inconclusive'}`).join(' · ')}</small></span>${badge(status)}<span class="disclosure-arrow" aria-hidden="true">⌄</span></summary><div class="comparison-group-body">${reasons.length?`<ul class="comparison-reason-list">${reasons.map(reason=>`<li>${escape(reason)}</li>`).join('')}</ul>`:''}${comparisonTable(rows)}</div></details>`;
}
function baselineIdentity(baseline) {
  const saved=state.baselines.find(item=>item.id===state.detail?.baseline_id || (baseline.run_id&&item.run_id===baseline.run_id));
  const run=state.runs.find(item=>baseline.run_id&&item.run_id===baseline.run_id);
  const label=saved?.name || run?.targets?.map(target=>target.model).filter(Boolean).join(', ') || 'Saved reference run';
  const when=baseline.source_started_at || run?.started_at || saved?.saved_at;
  return `<strong>${escape(label)}</strong>${when?`<span>${date(when)}</span>`:''}<details class="baseline-identifier"><summary>Reference ID</summary><code>${escape(baseline.run_id || 'Not recorded')}</code></details>`;
}
function renderComparison(report) {
  const b=report.baseline;if(!b)return `<div class="panel empty-state"><h2>No baseline selected</h2><p>Save a reference run, then select it when starting another suite to measure change. This run still evaluates its own acceptance limits.</p><button class="button primary" data-action="baseline-dialog">Save this run as a baseline</button></div>`;
  const allRows=(b.targets || []).flatMap(t=>t.comparisons || []),checkIds=[...new Set(allRows.map(row=>row.check))].sort();
  state.comparisonFilter ??= allRows.some(row=>row.status!=='pass')?'attention':'all';
  const conclusion=b.status==='fail'?'A material regression was detected':b.status==='pass'?'No material regression detected':'The comparison is inconclusive';
  const guidance=b.status==='inconclusive'?'The recorded evidence does not establish improvement or regression. Open a check to see which measurements could not resolve the comparison.':b.status==='pass'?'The comparable measurements stayed within the configured regression limits. This is separate from the run’s acceptance result.':'Open the affected checks to inspect the changes and the conditions under which they were measured.';
  const filters=allRows.length?`<div class="comparison-filters"><div><label for="comparison-outcome">Show</label><select id="comparison-outcome"><option value="attention">Needs attention</option><option value="all">All outcomes</option><option value="fail">Regressions</option><option value="inconclusive">Inconclusive</option><option value="pass">No material regression</option></select></div><div><label for="comparison-check">Check</label><select id="comparison-check"><option value="all">All checks</option>${checkIds.map(id=>`<option value="${escape(id)}">${escape(catalogTitle(id))}</option>`).join('')}</select></div><span class="hint">${number(allRows.length)} measurements, grouped by check</span></div>`:'';
  const filterRow=row=>(state.comparisonFilter==='all'||(state.comparisonFilter==='attention'?row.status!=='pass':row.status===state.comparisonFilter))&&(state.comparisonCheck==='all'||row.check===state.comparisonCheck);
  return `<header class="comparison-conclusion"><div>${badge(b.status,'Comparison '+(statusLabels[b.status] || b.status).toLowerCase())}<h2>${escape(conclusion)}</h2><p>${escape(guidance)}</p></div><div class="baseline-reference"><span>Compared with</span>${baselineIdentity(b)}</div></header>${filters}${(b.targets || []).map(target=>{
    const rows=[...(target.comparisons || [])].filter(filterRow).sort((a,c)=>(order[a.status]??5)-(order[c.status]??5));
    const groups=new Map();for(const row of rows){if(!groups.has(row.check))groups.set(row.check,[]);groups.get(row.check).push(row);}
    return `<section class="comparison-section"><div class="comparison-target-heading"><h3>${escape(target.target || target.name || 'Target')}${!(target.comparisons || []).length&&target.traffic_capacity?` <span class="hint">${target.traffic_capacity.stages?.length || 0} traffic stages compared</span>`:''}</h3>${badge(target.status)}</div>${(target.comparisons || []).length&&target.summary?`<p class="hint comparison-target-summary">${escape(target.summary)}</p>`:''}${target.traffic_capacity?renderTrafficComparison(target.traffic_capacity):''}${(target.reasons || []).length?`<ul class="comparison-reason-list">${target.reasons.map(reason=>`<li>${escape(reason)}</li>`).join('')}</ul>`:''}${[...groups].map(([id,values])=>comparisonGroup(id,values)).join('')}${!groups.size&&(target.comparisons || []).length?'<p class="comparison-reasons">No measurements match these filters.</p>':''}<details class="comparison-support"><summary>Deployment and comparison details</summary><p class="hint">${target.configuration_diff?.confounded?'Confounded experiment':'Causality unverified'}. Baseline comparisons are separate from absolute acceptance checks.</p><pre>${escape(JSON.stringify({configuration_diff:target.configuration_diff,identity_changes:target.identity_changes,telemetry:target.telemetry,consistency:target.consistency_changes},null,2))}</pre></details></section>`;
  }).join('')}<details class="comparison-support"><summary>Comparison method</summary><p class="hint">${escape(b.method || 'Method was not recorded in this report.')}</p></details>`;
}
function syncComparisonFilters(){if($('#comparison-outcome'))$('#comparison-outcome').value=state.comparisonFilter;if($('#comparison-check'))$('#comparison-check').value=state.comparisonCheck;}
function comparisonValue(row,value){if(value==null)return '—';if(row.metric.endsWith('_rate'))return `${number(value*100,1)}%`;if(row.metric.endsWith('_ms'))return ms(value);return `${number(value,2)}${row.metric.endsWith('tokens_per_second')?' tok/s':''}`;}
function renderDeployment(report){
  return `<section class="panel"><div class="section-heading"><h2>Deployment and serving observations</h2></div><p class="hint">Changes provide investigation context, not proof of causality. Source freshness and other replicas remain qualified in the evidence.</p>${Object.entries(report.observations?.targets || {}).map(([name,obs])=>`<h3>${escape(name)}</h3><details><summary>Deployment snapshot and unknown fields</summary><pre>${escape(JSON.stringify(obs.deployment,null,2))}</pre></details><details><summary>Deployment observations during the run</summary><pre>${escape(JSON.stringify(obs.deployment_history || [],null,2))}</pre></details><details><summary>Serving telemetry and phase coverage</summary><pre>${escape(JSON.stringify(obs.serving_telemetry,null,2))}</pre></details>`).join('')}<a class="button" href="${runPath(state.detail.id)}/reproduction.json" download>Export reproduction JSON</a></section>`;
}
function renderConfig(report){return `${renderDeployment(report)}${report.warnings?.length?`<div class="notice warning">${report.warnings.map(w=>`<p>${escape(w)}</p>`).join('')}</div>`:''}<section class="panel"><div class="section-heading"><h2>Recorded configuration</h2><div class="actions"><span class="hint">Suite ${escape(report.suite_version)}</span><a class="text-button" href="${runPath(state.detail.id)}/config.json" download>Download config</a></div></div><pre class="config-display">${escape(JSON.stringify(report.manifest,null,2))}</pre></section>`;}
function renderLive() {
  const d=state.detail,p=d.progress || {},c=d.config || reportConfig(d.report),events=d.events || [],last=events.at(-1) || {},progress={...last,...p};
  const finished=progress.completed || 0,started=progress.attempted || 0,active=Math.max(0,started-finished),secs=progress.elapsed_seconds || 0,stopping=d.state==='stopping';
  const judging=progress.phase==='judging'||String(progress.event || '').startsWith('judging_');
  const judgeDone=progress.judge_calls_completed || 0,judgeTotal=progress.judge_calls_total || 0;
  const title=judging?'Evaluating answers':d.state==='queued'?'Preparing run':'Mission control';
  const description=judging?'Target traffic finished. Evaluating generated answers against golden answers; target measurements are frozen.':'Requests appear as they start. Select a spot for evidence.';
  const bounds=judging?[['Evaluator concurrency',c.judging?.concurrency],['Evaluation duration',elapsed(c.judging?.max_duration_seconds)],['Judge deadline',elapsed(c.judging?.request_timeout_seconds)],['Judge output cap',`${c.judging?.max_output_tokens} tokens`]]:[['Maximum in flight',c.concurrency],['Total request limit',c.max_requests],['Maximum duration',elapsed(c.max_duration_seconds)],['Request deadline',elapsed(c.request_timeout_seconds)],['Output cap',`${c.max_output_tokens} tokens`]];
  $('#main').innerHTML=`<div class="page-heading"><div><div class="eyebrow">Live run <span class="mono">${escape(d.id)}</span></div><h1>${stopping?'Stopping your check':title}</h1><p>${stopping?'Stopping further work and saving the evidence collected so far.':description}</p></div><button class="button danger" data-action="stop" ${stopping?'disabled':''}>${icon('stop')}${stopping?'Stopping…':'Stop run'}</button></div>
  ${renderMosaic(d.live_requests || [],{budget:c.max_requests,live:true,budgetUsed:progress.budget_used ?? started})}<div class="live-layout"><div><section class="panel live-state">${badge(d.state)}<h2>${judging?'Post-run AI evaluation':escape(progress.scenario?phaseName(progress.scenario):human(progress.event || 'Preparing requests'))}</h2><p class="live-phase">${escape(progress.target || (c.targets || []).map(t=>t.name).join(', '))} · ${escape((c.targets || []).map(t=>t.model).join(', '))}</p>
  <dl class="live-numbers"><div><dt>${judging?'Judge calls completed':'Requests finished / started'}</dt><dd>${number(judging?judgeDone:finished)} <span class="dim">/ ${number(judging?judgeTotal:started)}</span></dd></div><div><dt>${judging?'Target requests finished':'Active requests'}</dt><dd>${number(judging?finished:active)}</dd></div><div><dt>Elapsed</dt><dd>${elapsed(secs)}</dd></div></dl>
  <div class="budget-track" aria-hidden="true"><span style="width:${Math.min(100,judging?judgeDone/(judgeTotal || 1)*100:started/(c.max_requests || 1)*100)}%"></span></div><div class="budget-caption"><span>${judging?`Evaluation calls · ${number(judgeDone)} of ${number(judgeTotal)}`:`Requests started · ${number(started)} / ${number(c.max_requests)} limit`}</span><span>${judging?'Inference timing is unchanged':'Request ceiling, not completion'}</span></div></section>
  <section class="panel" style="margin-top:20px"><div class="section-heading"><h2>Activity</h2><span class="hint">Updates live</span></div><div class="event-list">${[...events].reverse().slice(0,15).map(e=>`<div class="event-row"><span class="time">${elapsed(e.elapsed_seconds)}</span><span>${escape(human(e.event || 'Progress'))}${e.scenario?` · ${escape(phaseName(e.scenario))}`:''}</span><span class="phase">${String(e.event || '').startsWith('judging_')?`${number(e.judge_calls_completed || 0)} / ${number(e.judge_calls_total || 0)} judge calls`:`${escape(e.target || '')} · ${number(e.completed || 0)} finished`}</span></div>`).join('') || '<p class="hint" style="padding:20px 0">Preparing the first requests…</p>'}</div></section></div>
  <aside class="panel run-preview">${renderPhases(d.phases || events)}<h3>${judging?'Evaluation limits':'Limits'}</h3>${judging?'<p>Separate limits apply to the configured judges.</p>':''}<dl class="bound-list">${bounds.map(([k,v])=>`<div><dt>${escape(k)}</dt><dd>${escape(v)}</dd></div>`).join('')}</dl><hr><p class="hint">${judging?'Incomplete panels, evaluation failures and ties leave answers unscored.':'Finished requests include responses, failures, timeouts and cancellations. Scheduled requests can also use budget without reaching the server.'}</p></aside></div><div id="live-error" class="error-message" role="alert" hidden></div>`;
}
function renderJudgement(judgement) {
  if(!judgement)return '';
  const votes=judgement.votes || [],yes=votes.filter(vote=>vote.verdict===true).length,no=votes.filter(vote=>vote.verdict===false).length;
  const incomplete=!votes.length||votes.some(vote=>vote.status!=='completed'||typeof vote.verdict!=='boolean');
  const explanation=incomplete?'Incomplete panel · answer unscored':yes===no?'Tie · answer unscored':`${yes} accept · ${no} reject`;
  return `<section class="judge-evidence"><h3>AI evaluation ${badge(judgement.status)}</h3><p class="hint">${explanation}${yes&&no?' · Disagreement among judges':''}</p>${votes.map(vote=>`<article class="judge-vote"><div class="statline"><strong>${escape(vote.judge)}</strong>${vote.verdict===true?badge('pass','Accept'):vote.verdict===false?badge('fail','Reject'):badge('inconclusive','No verdict')}</div><p class="hint">${escape(vote.model)}${vote.observed_model&&vote.observed_model!==vote.model?` · observed ${escape(vote.observed_model)}`:''} · ${escape(vote.url)}</p>${vote.error_category?`<p class="hint">Evaluation error: ${escape(vote.error_category)}</p>`:''}${vote.reason?`<p class="judge-reason">${escape(vote.reason)}</p>`:'<p class="hint">Reason not retained or unavailable.</p>'}</article>`).join('')}</section>`;
}
function renderLogprobResults(value) {
  const cohorts=[...(value.conditions || []),...(value.cross_conditions || [])];
  return `<section class="tool-evaluation"><h3>Token probability consistency · ${escape(value.status)}</h3><p>${escape(value.interpretation)}</p><p>Tolerance: ${escape(value.tolerance ?? 'diagnostic')} logprob units · ${number(value.repetitions)} rounds · ${number(value.priming_rounds)} priming rounds</p><div class="metric-table"><table><thead><tr><th>Condition</th><th>Coverage</th><th>Maximum difference</th><th>Token divergence / top-k changes</th><th>Outcome</th></tr></thead><tbody>${cohorts.map(c=>`<tr><td>${escape(c.shape)} · ${escape(c.level ?? c.levels?.join(' vs '))}</td><td>${c.issued_requests ?? '—'} / ${c.planned_requests ?? '—'} requests<small>${number(c.compared_token_pairs)} token pairs</small></td><td>${escape(c.max_absolute_delta ?? 'unknown')}</td><td>${number(c.token_divergences)} / ${number(c.top_k_membership_changes)}</td><td>${escape(c.status)}</td></tr>`).join('')}</tbody></table></div>${cohorts.flatMap(c=>c.examples || []).map(e=>`<p>${escape(e.kind)} at position ${number(e.position)} · ${escape(e.absolute_delta ?? 'unknown')} ${e.request_ids.map(id=>`<button class="text-button" data-request="${escape(id)}">${escape(id)}</button>`).join(' ')}</p>`).join('')}</section>`;
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
    return `<h3>Input messages</h3>${(evidence.messages || []).map(message=>`<h4>${escape(message.role)}</h4><div class="body-output"><pre>${escape(message.content)}</pre></div>`).join('')}<h3>Expected assistant text</h3>${content?`<p class="hint">${escape(content.mode)} · ${content.normalization==='whitespace'?'Whitespace normalized':'No normalization'}. Compares combined assistant text.</p><div class="body-output"><pre>${escape(content.value)}</pre></div>`:'<p class="hint">No assistant text requirement. Tool-only responses are allowed.</p>'}${expected.choice?`<h3>Tool choice rule</h3><pre>${escape(JSON.stringify(expected.choice,null,2))}</pre>`:''}<h3>Expected tool calls</h3>${expected.expected_calls==null?'<p class="hint">No task-specific call expectation configured.</p>':`<p class="hint">Exact count and order. Null arguments require schema validity without a fixed value.</p><div class="body-output"><pre>${escape(JSON.stringify(expected.expected_calls,null,2))}</pre></div>`}${record.tool_request_options?`<details><summary>Sent tool options</summary><pre>${escape(JSON.stringify(record.tool_request_options,null,2))}</pre></details>`:''}<details><summary>Tool definitions</summary><pre>${escape(JSON.stringify(expected.tools || [],null,2))}</pre></details>`;
  }
  const expected=evidence.scorer==='json'?JSON.stringify(evidence.expected,null,2):String(evidence.expected ?? '');
  const schema=evidence.schema ?? evidence.schema_;
  return `<h3>Input messages</h3>${(evidence.messages || []).map(message=>`<h4>${escape(message.role)}</h4><div class="body-output"><pre>${escape(message.content)}</pre></div>`).join('')}<h3>Answer check</h3><p>${escape(answerChecks[evidence.scorer] || evidence.scorer)}</p><h3>Expected answer</h3>${evidence.expected_provided?`<div class="body-output"><pre>${escape(expected)}</pre></div>`:'<p class="hint">No expected answer configured.</p>'}${evidence.rubric?`<h3>Judge rubric</h3><div class="body-output"><pre>${escape(evidence.rubric)}</pre></div>`:''}${schema!=null?`<details><summary>JSON schema</summary><pre>${escape(JSON.stringify(schema,null,2))}</pre></details>`:''}`;
}
function openRequest(id) {
  const report=state.detail.report,r=report.requests.find(x=>x.id===id);if(!r)return;
  const dialog=$('#request-dialog'),retention=reportConfig(report).retention || 'failures';
  dialog.innerHTML=`<div class="dialog-head"><div><div class="eyebrow">Request evidence</div><h2 id="request-title">${escape(r.fixture_id)}</h2><p class="mono">${escape(r.id)}</p></div><button class="button ghost" data-action="close-dialog" aria-label="Close request evidence">✕</button></div><div class="dialog-body"><div class="statline"><span class="pill">${escape(r.target)}</span><span class="muted">${escape(r.scenario)}</span><span>Response: ${escape(r.status)}</span>${r.score===false?badge('fail','Incorrect answer'):r.score===true?badge('pass','Correct answer'):'<span class="dim">Answer unscored</span>'}</div>${isProbe(r)?'<div class="notice" style="margin-top:20px">This request is an intentional cancellation or deadline probe. Its transport status alone does not indicate a failed suite check.</div>':''}${r.error?`<div class="notice ${isProbe(r)?'':'error'}" style="margin-top:20px">${escape(r.error)}</div>`:''}${r.score_message?`<p class="hint" style="margin-top:18px">${escape(r.score_message)}</p>`:''}${renderToolEvaluation(r)}<div class="request-comparison"><section>${renderEvaluationEvidence(r,report)}</section><section><h3>Actual answer <span class="hint">${number(r.output_chars)} recorded characters</span></h3><div class="body-output">${r.output?`<pre>${escape(r.output)}</pre>`:r.output_chars>0?`<p class="body-not-retained">Body not retained (retention: ${escape(retention)}). Measurements and scoring are preserved.</p>`:'<p class="body-not-retained">No answer text was recorded for this request.</p>'}</div>${renderLogprobEvidence(r)}${renderActualToolCalls(r)}</section></div>${renderResponseEvents(r)}${renderJudgement(r.judgement)}<details class="request-timing"><summary>Timing & transport details</summary><dl class="metrics-grid" style="margin-top:24px">${r.traffic_stage!=null?metric('Arrival rate',`${number(r.traffic_rate_rps,2)} requests/s`,r.traffic_class || '')+metric('Local dispatch delay',ms(r.dispatch_lag_ms),'Measured from planned arrival; not server queue time'):''}${metric('Elapsed',ms(r.elapsed_ms))}${metric('First answer',ms(r.first_output_ms))}${metric('Last answer',ms(r.last_output_ms))}${metric('First reasoning',ms(r.first_reasoning_ms))}${metric('Longest stream gap',ms(r.max_stream_gap_ms))}${metric('Input / output tokens',`${number(r.input_tokens)} / ${number(r.output_tokens)}`,'Server-reported usage')}${metric('HTTP / finish reason',`${r.http_status ?? '—'} / ${r.finish_reason || '—'}`)}</dl></details><details class="request-reasoning"><summary>Reasoning output</summary><div class="body-output">${r.reasoning?`<pre>${escape(r.reasoning)}</pre>`:`<p class="body-not-retained">${retention==='all'?'No reasoning text was recorded.':'No reasoning body retained. This does not establish whether the model reasoned.'}</p>`}</div></details><p class="hint" style="margin-top:18px">${r.valid?'Valid non-empty completion':'Completion not validated'} · ${r.stream?'Streaming':'Non-streaming'} · ${r.stream_terminated?'Stream termination observed':'Stream termination not recorded'}</p></div><div class="dialog-footer"><button class="button" data-action="close-dialog">Close</button></div>`;
  dialog.showModal();
}
function openBaselineDialog(){
  const report=state.detail.report,dialog=$('#baseline-dialog');dialog.className='baseline-form';dialog.innerHTML=`<form id="baseline-form"><div class="dialog-head"><div><div class="eyebrow">Explicit reference run</div><h2 id="baseline-title">Save baseline</h2></div><button type="button" class="button ghost" data-action="close-dialog" aria-label="Close save baseline">✕</button></div><div class="dialog-body"><p class="hint">Save this exact report for future comparisons. Its recorded outcome is ${escape(statusLabels[report.overall] || report.overall)}.</p><div class="fields">${field('baseline_name','Baseline name','',{required:true,full:true,placeholder:'e.g. before-runtime-upgrade'})}</div><p class="hint" style="margin-top:12px">Existing baselines are preserved unless you explicitly replace one.</p><div id="baseline-error" class="error-message" role="alert" hidden></div><label class="replace-row" id="replace-row" hidden><input name="replace" type="checkbox">Replace the existing baseline with this name</label></div><div class="dialog-footer"><button type="button" class="button" data-action="close-dialog">Cancel</button><button type="submit" class="button primary" id="save-baseline">Save baseline</button></div></form>`;dialog.showModal();}
async function saveBaseline(event){event.preventDefault();const form=event.target,data=new FormData(form),button=$('#save-baseline'),error=$('#baseline-error');button.disabled=true;error.hidden=true;try{await api('/api/baselines',{method:'POST',body:JSON.stringify({run_id:state.detail.id,name:String(data.get('baseline_name')).trim(),replace:data.has('replace')})});const result=await api('/api/baselines');state.baselines=result.baselines || [];$('#baseline-dialog').close();renderDetail();toast(`Saved as baseline “${String(data.get('baseline_name')).trim()}”. Use Compare a new run to test against it.`);}catch(e){error.textContent=e.message;error.hidden=false;if(e.status===409)$('#replace-row').hidden=false;}finally{button.disabled=false;}}
async function stopRun(){const button=$('[data-action="stop"]'),id=state.detail.id,version=state.routeVersion;button.disabled=true;button.textContent='Stopping…';try{const result=await api(`${runPath(id)}/cancel`,{method:'POST',body:'{}'});if(version!==state.routeVersion||state.detail?.id!==id)return;state.detail.state=result.state || 'stopping';renderDetail();schedulePoll(pollDetail,version,500);}catch(error){if(version!==state.routeVersion||state.detail?.id!==id)return;const el=$('#live-error');if(el){el.textContent=error.message;el.hidden=false;}button.disabled=false;button.textContent='Stop run';}}
// "New run" starts setup at step 1, even when the form is already open on a later step.
function startNewRun(){
  state.setupStep=1;state.returnToReview=false;state.openTest=null;
  if(location.hash!=='#new'){location.hash='#new';return;}
  if($('#new-run-form'))captureFormDraft();
  renderNew();enterRoute();window.scrollTo?.({top:0,left:0,behavior:'instant'});
}
function rerun(){const source=state.detail.report?.manifest?.config || state.detail.config;if(!source){toast('No configuration was saved for this run.');return;}state.config=clone(source);state.autoModel=null;state.setupStep=1;invalidateModelDiscovery();state.datasetSelection=[];state.config.restart_target=null;state.formMode='normal';state.formRestartTarget=null;state.jsonDraft=null;state.jsonBase=null;state.configView='form';configChanged();state.prefillBaseline=state.detail.baseline_id || '';location.hash='#new';}
function portableConfig(payload) {return {...payload.config,restart_target:payload.mode==='cold-start'?payload.restart_target:null};}
function hasJsonChanges() {return state.jsonDraft!==null&&state.jsonDraft!==state.jsonBase;}
function assertAppliedConfig() {if(hasJsonChanges())throw new Error('Apply or discard the JSON changes first.');}
function captureFormDraft() {
  const form=$('#new-run-form');if(!form)return;
  state.prefillBaseline=form.elements.baseline_id.value;
  state.formMode=form.elements.mode.value;state.formRestartTarget=form.elements.restart_target.value;
  try{state.config=readForm(true).config;}catch{}
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
  syncSetupStep();renderModelDiscovery();
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
    const result=await api('/api/config/draft',{method:'POST',body:JSON.stringify(parsed)});
    if(version!==state.routeVersion||revision!==state.configRevision)return;
    captureFormDraft();
    state.config=result.config;state.autoModel=null;invalidateModelDiscovery();state.datasetSelection=[];
    // An explicit restart target in imported/edited JSON remains visible and intentional.
    state.formMode=result.config.restart_target?'cold-start':'normal';state.formRestartTarget=result.config.restart_target;
    state.config.restart_target=null;
    state.jsonDraft=null;state.jsonBase=null;configChanged();
    renderNew();toast(importing?'Configuration imported.':'Configuration applied.');
  }catch(error){if(version===state.routeVersion&&revision===state.configRevision)configError(error);}
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
      !payload.config.checks.some(id=>['correctness','capacity'].includes(id)))payload.config.request_source='builtin';
  if(!draft){
    const missing=payload.config.targets.findIndex(target=>!target.model);
    if(missing!==-1){
      switchConfigView(missing===0?'form':'json');
      if(missing===0){revealSetupStep(1);$('#new-run-form').elements.model.focus();}else $('#config-editor').focus();
      throw new Error(`Enter a model for target "${payload.config.targets[missing].name}" before running or copying a CLI command. You can still apply or export this draft.`);
    }
  }
  payload.config=(await api(draft?'/api/config/draft':'/api/config/validate',{method:'POST',body:JSON.stringify(payload.config)})).config;
  if(version!==state.routeVersion||revision!==state.configRevision)throw new Error('Configuration changed during validation. Try again with the current settings.');
  assertAppliedConfig();return payload;
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
  if(event.target.closest('.skip-link')){event.preventDefault();const main=$('#main');main.setAttribute('tabindex','-1');main.focus({preventScroll:true});main.scrollIntoView({block:'start'});return;}
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
  if(shellAction==='mosaic-prev'||shellAction==='mosaic-next'){state.mosaicPage=Math.max(0,(state.mosaicPage || 0)+(shellAction==='mosaic-next'?1:-1));renderDetail();$('.request-mosaic [tabindex="0"]')?.focus({preventScroll:true});return;}
  const configTab=event.target.closest('[data-config-tab]');if(configTab){switchConfigView(configTab.dataset.configTab);return;}
  const setupStep=event.target.closest('[data-setup-step]');if(setupStep){state.returnToReview=Boolean(setupStep.dataset.reviewEdit);navigateSetup(Number(setupStep.dataset.setupStep));return;}
  const tab=event.target.closest('[data-tab]');if(tab){state.tab=tab.dataset.tab;renderDetail();$(`#tab-${state.tab}`).focus();return;}
  const check=event.target.closest('[data-check]');if(check){
    // Keep the user's place: the list keeps its scroll, the clicked check stays marked, and the page only moves enough to show its details.
    const list=$('.check-list'),listScroll=list?.scrollTop || 0;
    state.selectedCheck=check.dataset.check;state.checkNavigatorOpen=false;$('#result-content').innerHTML=renderChecks(state.detail.report);
    const nextList=$('.check-list');if(nextList)nextList.scrollTop=listScroll;
    const detail=$('.check-detail'),top=detail?.getBoundingClientRect?.().top;
    if(Number.isFinite(top)&&(top<64||top>window.innerHeight*.6))window.scrollTo?.({top:window.scrollY+top-76,behavior:'smooth'});
    const heading=$('.check-detail h2');heading?.setAttribute('tabindex','-1');heading?.focus({preventScroll:true});return;}
  const request=event.target.closest('[data-request]');if(request){openRequest(request.dataset.request);return;}
  const action=event.target.closest('[data-action]')?.dataset.action;
  if(action==='inspect-finding'){inspectFinding();return;}
  if(action==='toggle-checks'){state.checkNavigatorOpen=!state.checkNavigatorOpen;$('#result-content').innerHTML=renderChecks(state.detail.report);$('[data-action="toggle-checks"]')?.focus({preventScroll:true});return;}
  if(action==='check-intent'){browseCheckIntent(event.target.closest('[data-category]').dataset.category);return;}
  if(action==='runtime-shortcut'){useRuntimeShortcut(event.target.closest('[data-runtime]').dataset.runtime);return;}
  if(action==='recent-endpoint'){const chip=event.target.closest('[data-url]');useRecentEndpoint(chip.dataset.url,chip.dataset.model);return;}
  if(action==='catalogue-category'){state.catalogueCategory=event.target.closest('[data-category]').dataset.category;syncTestControls();return;}
  if(action==='catalogue-filter'){state.catalogueFilter=event.target.closest('[data-catalogue-filter]').dataset.catalogueFilter;syncTestControls();return;}
  if(action==='reset-profile'){try{assertAppliedConfig();const key=event.target.closest('[data-profile]').dataset.profile;captureFormDraft();state.config[key]=null;configChanged();renderNew('custom');$(`[data-profile="${key}"]`)?.focus();}catch(error){configError(error);}return;}
  if(action==='quick-checks'){try{assertAppliedConfig();captureFormDraft();state.config.checks=['access','correctness'];state.config.structured_json=false;state.config.metrics=false;state.config.tool_calling=false;state.catalogueFilter='selected';state.catalogueCategory='all';state.catalogueSearch='';state.openTest=null;configChanged();renderNew('custom');$('[data-catalogue-filter="selected"]')?.focus();}catch(error){configError(error);}return;}
  if(action==='setup-next'){navigateSetup(state.returnToReview?3:state.setupStep+1);return;}
  if(action==='setup-back'){navigateSetup(state.setupStep-1);return;}
  if(action==='test-connection'){await testConnection();return;}
  const configure=event.target.closest('[data-configure-test]')?.dataset.configureTest;
  if(configure){toggleTestEditor(configure);return;}
  if(state.openTest&&event.target.id==='new-run-form'&&window.matchMedia?.('(min-width: 1180px)').matches){toggleTestEditor(state.openTest,true);return;}
  const closeTest=event.target.closest('[data-close-test]')?.dataset.closeTest;
  if(closeTest){toggleTestEditor(closeTest,true);return;}
  const done=event.target.closest('[data-done-test]')?.dataset.doneTest;
  if(done){toggleTestEditor(done,true);return;}
  const reset=event.target.closest('[data-reset-test]')?.dataset.resetTest;
  if(reset){configChanged();if(state.config.test_options)delete state.config.test_options[reset];const form=$('#new-run-form');form.elements[`test_mode_${reset}`].value='default';refreshInheritedFields(reset);syncTestControls();updateBounds();return;}
  if(action==='use-request-budget'){
    try{
      assertAppliedConfig();
      const plan=currentRequestPlan();
      if(!plan)throw new Error('Wait for the current request plan before changing the budget.');
      const budget=Number(event.target.closest('[data-budget]').dataset.budget),input=$('#new-run-form').elements.max_requests;
      if(budget!==plan.required_request_limit||Number(input.value)!==plan.request_limit)throw new Error('The request plan changed. Review the updated budget before applying it.');
      if(Number.isInteger(budget)&&budget>Number(input.value)){
        input.value=budget;configChanged();updateBounds();
        if(state.configView==='json'){
          state.jsonDraft=JSON.stringify(portableConfig(readForm(true)),null,2);state.jsonBase=state.jsonDraft;
          $('#config-editor').value=state.jsonDraft;
        }
      }
    }catch(error){configError(error);}
    return;
  }
  if(action==='edit-output-cap'){
    try{assertAppliedConfig();revealSetupStep(2);$('#shared-settings').open=true;$('#shared-run-limits').open=true;$('#new-run-form').elements.max_output_tokens.focus();}catch(error){configError(error);}
    return;
  }
  if(action==='edit-run-limits'){
    try{assertAppliedConfig();state.returnToReview=state.setupStep===3;captureFormDraft();revealSetupStep(2);updateBounds();$('#shared-settings').open=true;$('#new-run-form').elements.max_requests.focus();}catch(error){configError(error);}
    return;
  }
  if(action==='edit-load'){
    try{
      assertAppliedConfig();revealSetupStep(2);
      const checks=readForm(true).config.checks,id=checks.includes('capacity')?'capacity':checks.includes('buckets')?'buckets':null;
      if(id){revealCatalogueCheck(id);$(`[data-test-card="${id}"]`)?.scrollIntoView({block:'start'});$(`[data-configure-test="${id}"]`)?.focus();}
      else{state.catalogueFilter='selected';state.catalogueCategory='all';state.catalogueSearch='';state.openTest=null;const search=$('#check-search');if(search)search.value='';syncTestControls();$('[data-catalogue-filter="selected"]')?.focus();}
    }catch(error){configError(error);}return;
  }
  if(action==='toggle-available-tests'){state.showAvailableTests=!state.showAvailableTests;syncTestControls();return;}
  if(action==='select-group'){
    configChanged();const category=event.target.closest('[data-category]').dataset.category;
    const inputs=$$(`[data-catalogue-group="${category}"] input[name="check"]`),select=!inputs.every(input=>input.checked);
    for(const input of inputs)input.checked=select;captureFormDraft();renderNew('custom');
    $(`[data-action="select-group"][data-category="${category}"]`)?.focus({preventScroll:true});return;
  }
  if(action==='select-tests'||action==='clear-tests'){configChanged();for(const input of $$('#new-run-form input[name="check"]'))input.checked=action==='select-tests';captureFormDraft();renderNew('custom');$(`[data-action="${action}"]`)?.focus({preventScroll:true});return;}
  if(action==='reset-traffic'){configChanged();const form=$('#new-run-form');form.elements.traffic_mode.value='default';const defaults=state.bootstrap.config.traffic;for(const [key,value] of Object.entries(defaults)){if(key==='mix'){for(const [kind,weight] of Object.entries(value))form.elements[`traffic_mix_${kind}`].value=weight;}else form.elements[`traffic_${key}`].value=Array.isArray(value)?value.join(', '):value ?? '';}syncTestControls();updateBounds();return;}
  if(action==='compare-run'){const saved=(state.baselines || []).find(item=>item.run_id===state.detail?.id);rerun();if(saved)state.prefillBaseline=saved.id;return;}
  if(action==='choose-baseline'){const options=$('#execution-settings');if(options){options.open=true;const select=$('#field-baseline_id');select?.focus();select?.scrollIntoView?.({block:'center'});}return;}
  if(action==='retry')route();if(action==='clear-filter'){state.filter='all';state.historySearch='';renderRuns();}
  if(action==='stop')await stopRun();if(action==='rerun')rerun();if(action==='baseline-dialog')openBaselineDialog();
  if(action==='close-dialog')event.target.closest('dialog').close();if(action==='apply-config')await applyConfig();
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
  if(action==='try-example')openTryExample(caseIndex);
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
document.addEventListener('submit',event=>{if(event.target.id==='new-run-form')startRun(event);if(event.target.id==='baseline-form')saveBaseline(event);if(event.target.id==='dataset-example-form')saveExample(event);if(event.target.id==='dataset-import-form')useImportedDataset(event);if(event.target.id==='dataset-try-form')tryDatasetExample(event);});
document.addEventListener('input',event=>{
  if(event.target.id==='command-search'){const query=event.target.value.toLowerCase();for(const item of $$('.command-item'))item.hidden=!item.textContent.toLowerCase().includes(query);for(const group of $$('.command-group'))group.hidden=!$$('.command-item',group).some(item=>!item.hidden);return;}

  if(event.target.id==='run-search'){state.historySearch=event.target.value;renderRuns();return;}
  if(event.target.id==='check-search'){state.catalogueSearch=event.target.value;syncTestControls();return;}
  if(!event.target.closest('#new-run-form')||event.target.matches('[data-dataset-select], #dataset-select-all, #dataset-bulk-scorer'))return;
  if(['url','model','name'].includes(event.target.name))event.target.setCustomValidity?.('');
  if(['url','api_key_env'].includes(event.target.name)&&state.autoModel&&$('#new-run-form').elements.model.value===state.autoModel){$('#new-run-form').elements.model.value='';state.autoModel=null;}
  if(event.target.name==='model')state.autoModel=null;
  if(['url','model','api_key_env'].includes(event.target.name))invalidateModelDiscovery();
  if(event.target.id==='config-editor')state.jsonDraft=event.target.value;
  configChanged();$('#cli-fallback').hidden=true;
  if(event.target.id==='config-editor'){syncConfigView();return;}
  if(event.target.closest('[data-profile-settings]'))editProfileField(event.target);
  else if(event.target.closest('[data-test-settings]')||event.target.closest('[data-traffic-settings]'))editTestField(event.target);
  if(['traffic_rates','traffic_duration_seconds'].includes(event.target.name))$('#new-run-form').elements.traffic_mode.value='custom';
  if(numericFields.some(([name])=>name===event.target.name)||event.target.name==='samples'||event.target.name.startsWith('limit_'))for(const check of state.bootstrap.checks)refreshInheritedFields(check.id,true);
  updateBounds();
});
document.addEventListener('change',event=>{
  if(event.target.matches('[data-dataset-select], #dataset-select-all')){
    if(event.target.id==='dataset-select-all')state.datasetSelection=event.target.checked?datasetCases().map(item=>item.fixture_id):[];
    else {const selected=new Set(state.datasetSelection);if(event.target.checked)selected.add(event.target.dataset.datasetSelect);else selected.delete(event.target.dataset.datasetSelect);state.datasetSelection=[...selected];}
    const all=$('#dataset-select-all');if(all)all.checked=datasetCases().every(item=>state.datasetSelection.includes(item.fixture_id));
    for(const input of $$('[data-dataset-select]'))input.checked=state.datasetSelection.includes(input.dataset.datasetSelect);
    const section=$('.answer-checks');if(section){captureFormDraft();section.outerHTML=renderAnswerChecks(state.config);}return;
  }
  if(event.target.id==='connection-model-choice'){
    const input=$('#new-run-form').elements.model,value=event.target.value;
    input.value=value==='__manual__'?'':value;input.setCustomValidity('');state.autoModel=value==='__manual__'?null:value;
    // Keep the discovered choices while invalidating any earlier verification.
    const models=state.modelDiscovery?.models || [];
    state.modelDiscoverySequence++;state.modelDiscovery={key:modelDiscoveryKey(),route:state.routeVersion,status:'needs_model',models,message:'Model selected. Test the connection, or continue to choose checks.'};
    renderModelDiscovery();if(value==='__manual__'){$('#manual-model-field').hidden=false;input.focus();}
    configChanged();updateBounds();return;
  }
  if(event.target.id==='dataset-bulk-scorer')return;
  if(event.target.name==='example_scorer'){syncExampleScorer();return;}
  if(event.target.closest('#dataset-import-form')){const draft=state.datasetImport;if(draft){draft.mapping=importMappingValues();draft.sequence++;draft.loading=false;draft.mappingDirty=true;renderDatasetImport();}return;}
  if(event.target.id==='request-pack-file'){const file=event.target.files[0];event.target.value='';importRequestPack(file);return;}if(event.target.name==='request_source'){changeRequestSource(event.target.value);return;}if(event.target.id==='config-file'){const file=event.target.files[0];event.target.value='';importConfig(file);return;}if(event.target.closest('#new-run-form')){configChanged();scheduleRequestPlan();}if(event.target.closest('[data-test-editor]')&&event.target.tagName==='SELECT'){if(event.target.closest('[data-profile-settings]'))editProfileField(event.target);else editTestField(event.target);updateBounds();}if(event.target.name==='check'){const id=event.target.value;captureFormDraft();renderNew('custom');const control=$(`[name="check"][value="${id}"]`);if(control?.closest('[data-test-card]')?.hidden)$('[data-catalogue-filter="all"]')?.focus();else control?.focus();return;}if(['comparison-outcome','comparison-check'].includes(event.target.id)){const id=event.target.id;if(id==='comparison-outcome')state.comparisonFilter=event.target.value;else state.comparisonCheck=event.target.value;$('#result-content').innerHTML=renderComparison(state.detail.report);syncComparisonFilters();$('#'+id).focus();}if(event.target.id==='run-filter'){state.filter=event.target.value;renderRuns();$('#run-filter').focus();}if(['mode','restart_target'].includes(event.target.name)){updateRestart();if(state.configView==='json'&&!hasJsonChanges()){state.jsonDraft=JSON.stringify(portableConfig(readForm(true)),null,2);state.jsonBase=state.jsonDraft;$('#config-editor').value=state.jsonDraft;}}if(event.target.name==='replace')$('#save-baseline').textContent=event.target.checked?'Replace baseline':'Save baseline';});
let navigationPrefix=0;
document.addEventListener('keydown',event=>{
  if(event.isComposing)return;
  if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='k'){event.preventDefault();openCommands();return;}
  const palette=$('#command-dialog');
  if(palette?.open&&['ArrowDown','ArrowUp','Enter'].includes(event.key)){
    const items=$$('.command-item',palette).filter(el=>!el.hidden),current=items.indexOf(document.activeElement);
    event.preventDefault();if(event.key==='Enter'){(items[current] || items[0])?.click();return;}
    items[(current+(event.key==='ArrowDown'?1:-1)+items.length)%items.length]?.focus();return;
  }
  if(document.querySelector('dialog[open]'))return;
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
  const configTab=event.target.closest('[data-config-tab]');if(configTab&&['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();switchConfigView(event.key==='Home'?'form':event.key==='End'?'json':state.configView==='form'?'json':'form');return;}const tab=event.target.closest('[data-tab]');if(tab&&['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();const tabs=['checks','baseline','config'];let i=tabs.indexOf(state.tab);i=event.key==='Home'?0:event.key==='End'?2:(i+(event.key==='ArrowRight'?1:2))%3;state.tab=tabs[i];renderDetail();$(`#tab-${state.tab}`).focus();}});
document.addEventListener('toggle',event=>{
  if(event.target.matches('.judge-configuration'))state.judgeEditorOpen=event.target.open;
  if(event.target.matches('.field-help')&&event.newState==='open')positionFieldHelp(event.target);
},true);
window.addEventListener('resize',()=>$$('.field-help:popover-open').forEach(positionFieldHelp));
document.addEventListener('scroll',event=>{
  if(event.target.closest?.('.field-help'))return;
  for(const popover of $$('.field-help:popover-open'))popover.hidePopover();
},true);
document.addEventListener('cancel',event=>{if(event.target.id==='dataset-dialog')closeDatasetDialog();},true);
document.addEventListener('pointerover',event=>{const spot=event.target.closest?.('.request-spot, .swarm-dot');if(spot)showSpotTip(spot,event.clientX,event.clientY);else hideSpotTip();});
document.addEventListener('focusin',event=>{const spot=event.target.closest?.('.request-spot, .swarm-dot');if(!spot){hideSpotTip();return;}const box=spot.getBoundingClientRect();showSpotTip(spot,box.left+box.width/2,box.top);});
document.addEventListener('pointermove',event=>{
  if(!$('#spot-tip')?.hidden&&event.target.closest?.('.request-spot, .swarm-dot'))showSpotTip(event.target.closest('.request-spot, .swarm-dot'),event.clientX,event.clientY);
  const coat=document.querySelector?.('.hero-coat');if(!coat)return;
  const bounds=coat.getBoundingClientRect();
  coat.style.setProperty('--mx',`${event.clientX-bounds.left}px`);coat.style.setProperty('--my',`${event.clientY-bounds.top}px`);
},{passive:true});
window.addEventListener('hashchange',route);
async function init(){initReticulate();try{const [bootstrap,baselines]=await Promise.all([api('/api/bootstrap'),api('/api/baselines')]);state.bootstrap=bootstrap;connectionError();state.baselines=baselines.baselines || [];state.config=clone(bootstrap.config);state.config.restart_target=null;setActive(bootstrap.active_run_id);$('#suite-version').textContent=`v${bootstrap.suite_version || '0.1.0'}`;$('#runner-location').textContent=bootstrap.hostname || 'UI server connected';await refreshRuns().catch(()=>{});await route();}catch(error){if(document.body)document.body.dataset.runner='unavailable';$('#main').innerHTML=`<div class="page-heading"><h1>Local runner unavailable</h1></div>${notice(error.message,'error')}<button class="button" onclick="location.reload()">Reconnect</button>`;}}
init();
