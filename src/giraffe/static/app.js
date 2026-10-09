import {configureTests, limitValue, optionDefault, optionValue, sameConfig} from './form-config.mjs?v=20261010-integrated-config';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clone = (value) => structuredClone(value);
const state = {bootstrap:null, runs:[], baselines:[], detail:null, config:null, active:null, routeVersion:0, poll:null, tab:'checks', selectedCheck:null, filter:'all', comparisonFilter:null, comparisonCheck:'all', submitting:false, openTest:null, configView:'form', jsonDraft:null, jsonBase:null, configRevision:0, configBusy:false, formMode:'normal', formRestartTarget:null};
const statusLabels = {pass:'Passed',fail:'Failed',inconclusive:'Inconclusive',skipped:'Skipped',blocked:'Blocked',running:'Running',queued:'Queued',stopping:'Stopping',error:'Runner error',completed:'Finished',cancelled:'Cancelled',interrupted:'Interrupted'};
const statusIcons = {pass:'✓',fail:'×',inconclusive:'!',skipped:'–',blocked:'!',running:'·',queued:'·',stopping:'·',error:'×',completed:'✓',cancelled:'–',interrupted:'!'};
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

async function api(path, options={}) {
  let response;
  try { response = await fetch(path,{...options,headers:{'Content-Type':'application/json',...(options.headers || {})}}); }
  catch { throw new Error('The local UI server could not be reached. Check that giraffe ui is still running.'); }
  let body;
  try { body = await response.json(); } catch { throw new Error(`The local server returned an unreadable response (${response.status}).`); }
  if (!response.ok) {
    const detail = body.detail || body.errors || body.error || `Request failed (${response.status}).`;
    const message = Array.isArray(detail) ? detail.map(e => typeof e === 'string' ? e : `${(e.loc || []).join('.')}: ${e.msg || JSON.stringify(e)}`).join('\n') : typeof detail === 'string' ? detail : JSON.stringify(detail);
    const error = new Error(message); error.status = response.status; throw error;
  }
  return body;
}
function toast(message) { const el=$('#toast');el.textContent=message;el.hidden=false;setTimeout(()=>{el.hidden=true;},4500); }
function connectionError(message='') { $('#connection-error').textContent=message;$('#connection-error').hidden=!message; }
function setActive(id) {
  state.active=id || null;
  const link=$('#active-run-link');link.hidden=!id;link.href=id?linkRun(id):'#runs';link.innerHTML=`${badge('running','A suite is active')}<br><small>Open live run →</small>`;
}
function updateNav(section, label) {
  $('#nav-runs').classList.toggle('current',section==='runs');$('#nav-new').classList.toggle('current',section==='new');
  $('#nav-runs').setAttribute('aria-current',section==='runs'?'page':'false');$('#nav-new').setAttribute('aria-current',section==='new'?'page':'false');
  $('#breadcrumb').innerHTML=`<a href="#runs">Workspace</a><span>/</span>${escape(label)}`;
  document.title=`${label} · Giraffe`;
}
function clearPoll() { clearTimeout(state.poll);state.poll=null; }
function schedulePoll(callback, version, delay=1800) { clearPoll();state.poll=setTimeout(()=>{if(version===state.routeVersion) callback(version);},delay); }
async function refreshRuns() {
  const data=await api('/api/runs');state.runs=data.runs || [];setActive(data.active_run_id);$('#run-count').textContent=state.runs.length || '';return data;
}
async function route() {
  if ($('#new-run-form')) captureFormDraft();
  clearPoll();const version=++state.routeVersion;const hash=location.hash || '#runs';
  try {
    if (hash==='#new') { updateNav('new','New run');state.config ||= clone(state.bootstrap.config);renderNew();schedulePoll(pollNew,version,3000);return; }
    if (hash.startsWith('#runs/')) {
      const id=decodeURIComponent(hash.slice(6));updateNav('runs','Run detail');
      $('#main').innerHTML='<div class="loading">Loading run…</div>';
      const detail=await api(runPath(id));if(version!==state.routeVersion)return;
      state.detail=detail;state.selectedCheck=null;state.tab='checks';state.comparisonFilter=null;state.comparisonCheck='all';renderDetail();
      if(activeState(detail))schedulePoll(pollDetail,version);return;
    }
    updateNav('runs','Runs');$('#main').innerHTML='<div class="loading">Loading runs…</div>';
    await refreshRuns();if(version!==state.routeVersion)return;renderRuns();schedulePoll(pollRuns,version,3000);
  } catch(error) { if(version===state.routeVersion) $('#main').innerHTML=`<div class="page-heading"><h1>Could not open this view</h1></div>${notice(error.message,'error')}<button class="button" data-action="retry">Try again</button>`; }
}
async function pollRuns(version) {
  try { const previous=JSON.stringify(state.runs),active=state.active;await refreshRuns();if(version!==state.routeVersion)return;connectionError();if(previous!==JSON.stringify(state.runs)||active!==state.active)renderRuns(); }
  catch(error) { if(version===state.routeVersion)connectionError(error.message+' Your saved runs have not changed.'); }
  if(version===state.routeVersion)schedulePoll(pollRuns,version,3000);
}
async function pollNew(version) {
  try { await refreshRuns();if(version!==state.routeVersion)return;connectionError();const button=$('#start-run');if(button&&!state.submitting)button.disabled=Boolean(state.active);const activeNotice=$('#new-active-notice');if(activeNotice){activeNotice.hidden=!state.active;$('a',activeNotice).href=state.active?linkRun(state.active):'#runs';} }
  catch(error) {if(version===state.routeVersion)connectionError(error.message);}
  if(version===state.routeVersion)schedulePoll(pollNew,version,3000);
}
async function pollDetail(version) {
  const id=state.detail.id;
  try {
    const detail=await api(runPath(id));if(version!==state.routeVersion || state.detail.id!==id)return;
    connectionError();state.detail=detail;renderDetail();
    if(activeState(detail))schedulePoll(pollDetail,version);else {if(state.active===id)setActive(null);await refreshRuns();}
  } catch(error) {if(version!==state.routeVersion)return;connectionError(error.message+' Reconnecting; the suite may still be running.');schedulePoll(pollDetail,version,3000);}
}
function suiteList() { return `<ol class="suite-list">${(state.bootstrap.checks || []).map((c,i)=>`<li><span class="index">${String(i+1).padStart(2,'0')}</span><span>${escape(c.title)}</span>${c.optional?'<small>OPT</small>':''}</li>`).join('')}</ol>`; }
function renderRuns() {
  const runs=state.runs.filter(r=>state.filter==='all'||(activeState(r)?'active':r.overall || r.state)===state.filter);
  $('#main').innerHTML=`<div class="page-heading"><div><div class="eyebrow">Deployment acceptance</div><h1>Runs</h1><p>Your deployment, put to the test. Inspect results and see what changed.</p></div><a class="button primary" href="#new">${icon('plus')} New run</a></div>
  ${state.active?`<div class="notice"><strong>A suite is running.</strong> Traffic remains bounded by its configured limits. <a href="${linkRun(state.active)}">Open live run →</a></div>`:''}
  <div class="history-layout"><section class="panel"><div class="section-heading"><h2>Run history <span class="dim mono">${state.runs.length}</span></h2><div class="filter-group"><label class="sr-only" for="run-filter">Filter run status</label><select id="run-filter" aria-label="Filter run status"><option value="all">All outcomes</option><option value="fail">Failed</option><option value="pass">Passed</option><option value="inconclusive">Inconclusive</option><option value="blocked">Blocked</option><option value="active">Active</option><option value="error">Runner error</option></select></div></div>
  ${runs.length?`<div class="history-table"><table><thead><tr><th>Target / model</th><th>Outcome</th><th>Checks</th><th>Started</th><th>Duration</th><th></th></tr></thead><tbody>${runs.map(r=>{
    const targets=r.targets || [],counts=r.check_counts || {},outcome=activeState(r)?r.state:r.overall || r.state;
    return `<tr><td><a class="run-link" href="${linkRun(r.id)}">${escape(targets.map(t=>t.name).join(', ') || 'Unnamed run')}</a><small class="mono">${escape(targets.map(t=>t.model).join(', ') || 'No model recorded')}</small></td><td>${badge(outcome)}${r.baseline_id?'<small>Baseline compared</small>':''}</td><td>${activeState(r)?'<span class="muted">In progress</span>':`<span>${number(counts.pass || 0)} passed</span>${counts.fail?`<small style="color:var(--fail)">${number(counts.fail)} failed</small>`:counts.inconclusive?`<small>${number(counts.inconclusive)} inconclusive</small>`:''}`}</td><td class="nowrap">${date(r.started_at)}<small class="run-id">${escape((r.run_id || r.id).slice(0,14))}</small></td><td class="mono">${duration(r)}</td><td><a class="text-button" href="${linkRun(r.id)}" aria-label="Open run ${escape(r.id)}">↗</a></td></tr>`;}).join('')}</tbody></table></div><div class="table-note">Local results · newest first · a failed check is a saved result</div>`:
  `<div class="empty-state"><svg viewBox="0 0 40 40" aria-hidden="true"><rect x="8" y="5" width="24" height="30" rx="3"/><path d="M14 13h12M14 20h12M14 27h7"/></svg><h2>${state.runs.length?'No runs match this filter':'Start with your endpoint'}</h2><p>${state.runs.length?'Choose another outcome to inspect your saved runs.':'Run the built-in suite against an OpenAI-compatible endpoint. Get a clear result, with the requests behind it.'}</p>${state.runs.length?'<button class="button" data-action="clear-filter">Show all runs</button>':'<a class="button primary" href="#new">Configure first run →</a>'}</div>`}</section>
  <aside class="context-panel"><div class="eyebrow">One suite. Real evidence.</div><h2>What gets checked</h2><p>Built-in fixtures test serving behavior and known answers. No test authoring required.</p>${suiteList()}<p>Results apply to the targets, traffic and limits recorded in each run.</p></aside></div>`;
  $('#run-filter').value=state.filter;
}
const numericFields=[['context_limit','Context limit','tokens',128,1],['concurrency','Concurrency ceiling','requests',1,1],['max_requests','Request budget','requests',1,1],['max_duration_seconds','Maximum duration','seconds',.1,'any'],['request_timeout_seconds','Request deadline','seconds',.1,'any'],['max_output_tokens','Output cap','tokens / request',1,1]];
const limitFields=[['first_output_ms','First output maximum','ms'],['latency_ms','Response time maximum','ms'],['stream_gap_ms','Stream pause maximum','ms'],['min_output_tokens_per_second','Minimum generation rate','tokens / second after first output (estimate)'],['fairness_max_ratio','Mixed-load slowdown','ratio, greater than 1']];
function field(name,label,value,{type='text',hint='',min,max,step,placeholder='',required=false,full=false}={}) {
  return `<label class="${full?'full':''}">${escape(label)}<input name="${name}" type="${type}" value="${escape(value ?? '')}" ${required?'required':''} ${min!==undefined?`min="${min}"`:''} ${max!==undefined?`max="${max}"`:''} ${step!==undefined?`step="${step}"`:''} placeholder="${escape(placeholder)}" autocomplete="off">${hint?`<span class="hint">${escape(hint)}</span>`:''}</label>`;
}
function baselineOptions(selected='') {return `<option value="">No baseline · first or standalone run</option>${state.baselines.map(b=>`<option value="${escape(b.id)}" ${b.id===selected?'selected':''}>${escape(b.name)} · ${statusLabels[b.overall] || b.overall || 'saved'} · ${date(b.saved_at)}</option>`).join('')}`;}
function schemaField(name,value,schema,{label,hint='',placeholder='Inherit run setting'}={}) {
  const nullable=schema.anyOf?.some(item=>item.type==='null');
  const valueSchema=schema.anyOf?.find(item=>item.type!=='null') || schema;
  label ||= schema.title || human(name);
  if(valueSchema.enum)return `<label>${escape(label)}<select name="${escape(name)}">${valueSchema.enum.map(item=>`<option value="${escape(item)}" ${item===value?'selected':''}>${escape(item==='poisson'?'Poisson · uneven arrivals':item==='steady'?'Steady · evenly spaced':human(item))}</option>`).join('')}</select>${hint?`<span class="hint">${escape(hint)}</span>`:''}</label>`;
  if(valueSchema.type==='array')return field(name,label,(value || []).join(', '),{hint,required:true});
  return field(name,label,value,{type:'number',hint,min:valueSchema.minimum,max:valueSchema.maximum,step:valueSchema.type==='integer'?1:'any',required:!nullable,placeholder:nullable?placeholder:''});
}
function testSettingsLabel(check,config) {
  if(!check.option_fields.length&&!check.limit_fields.length){
    const key={mixed:'buckets',tools:'forced_tool_diagnostic'}[check.id] || check.id;
    if(config[key])return config.checks.includes(check.id)?'JSON settings':'JSON settings saved';
  }
  const custom=Object.keys(config.test_options?.[check.id] || {}).length>0||(check.id==='capacity'&&!sameConfig(config.traffic,state.bootstrap.config.traffic));
  return config.checks.includes(check.id)?(custom?'Custom':'Defaults'):(custom?'Custom saved':'Not selected');
}
function renderTestCard(check,config) {
  const selected=config.checks?.includes(check.id),custom=Object.keys(config.test_options?.[check.id] || {}).length>0;
  const fields=check.option_fields.map(name=>schemaField(`test_${check.id}_${name}`,optionValue(config,check,name),state.bootstrap.option_schema.properties[name],{label:human(name),hint:'Blank inherits the run setting.'}));
  const profileKey={mixed:'buckets',tools:'tool_calling'}[check.id] || check.id;
  const advanced=!check.option_fields.length&&!check.limit_fields.length;
  const limits=check.limit_fields.map(name=>schemaField(`test_${check.id}_limit_${name}`,limitValue(config,check,name),state.bootstrap.limits_schema.properties[name],{label:human(name),placeholder:'No limit',hint:state.bootstrap.limits_schema.properties[name].anyOf?'Blank removes this limit for this test.':''}));
  return `<section class="test-card" data-test-card="${check.id}"><div class="test-card-heading"><div class="test-row-copy"><label class="test-selection"><input type="checkbox" name="check" value="${check.id}" ${selected?'checked':''}><span>${escape(check.title)}</span></label><p class="hint test-description">${escape(check.description)}</p>${check.id==='capacity'?'<p class="hint traffic-profile-summary"></p>':''}</div><div class="test-row-actions"><span class="test-setting-state" data-setting-state>${testSettingsLabel(check,config)}</span><button type="button" class="text-button configure-test" data-configure-test="${check.id}" aria-label="Configure ${escape(check.title)}" aria-expanded="false" aria-controls="settings-${check.id}">Configure</button></div></div><input type="hidden" name="test_mode_${check.id}" value="${custom?'custom':'default'}"><div class="test-editor" id="settings-${check.id}" data-test-editor="${check.id}" hidden><p class="hint draft-notice" ${selected?'hidden':''}>Not selected. These settings will be used if you select this test.</p>${check.id==='capacity'?renderTrafficEditor(config):''}<div class="test-settings" data-test-settings="${check.id}"><div class="test-editor-heading"><h3>${check.id==='capacity'?'Acceptance settings':'Test settings'}</h3>${advanced?'':`<button type="button" class="text-button reset-test" data-reset-test="${check.id}">${check.id==='capacity'?'Reset acceptance settings':'Reset to defaults'}</button>`}</div><fieldset class="fields">${fields.join('')}${limits.join('')}</fieldset>${advanced?`<p class="hint">Uses the shared run bounds. Customize <code>${escape(profileKey)}</code>${check.id==='tools'?' and forced_tool_diagnostic':''} in the JSON configuration.</p><button type="button" class="text-button" data-config-tab="json">Edit in JSON</button>`:''}</div>${check.id==='gpu'?`<div class="metrics-field">${field('metrics_url','Metrics URL for first target',config.targets[0].metrics_url,{type:'url',placeholder:'http://localhost:9400/metrics'})}</div>`:''}<div class="test-editor-footer"><span class="hint">Changes are kept as you edit.</span><button type="button" class="button small" data-done-test="${check.id}">Done</button></div></div></section>`;
}
function renderTrafficEditor(config) {
  const traffic=config.traffic || state.bootstrap.config.traffic,custom=!sameConfig(traffic,state.bootstrap.config.traffic),schema=state.bootstrap.traffic_schema;
  const descriptions={rates:'Requests per second, in increasing order. Use commas between stages.',duration_seconds:'Each rate runs for this many seconds, then drains.',max_in_flight:'Blank inherits the global concurrency ceiling.',seed:'Repeat the arrival pattern and request mix.',long_input_chars:'Characters of input for long-input requests.',long_output_words:'Words copied by long-output requests. The global output cap still applies.',drain_timeout_seconds:'Wait for admitted requests after each arrival window.',scheduler_lag_tolerance_ms:'Late arrivals are counted as generator limitations.'};
  const controls=Object.entries(schema.properties).filter(([name])=>name!=='mix').map(([name,fieldSchema])=>schemaField(`traffic_${name}`,traffic[name],fieldSchema,{label:name==='rates'?'Arrival rates · requests/s':human(name),hint:descriptions[name] || ''}));
  const mix=schema.$defs.TrafficMix;
  return `<div class="traffic-editor"><input type="hidden" name="traffic_mode" value="${custom?'custom':'default'}"><div class="test-editor-heading"><h3>Traffic profile</h3><button class="text-button reset-test" type="button" data-action="reset-traffic">Reset traffic profile</button></div><p class="hint">Requests arrive continuously without waiting for a batch to finish. A full in-flight limit counts as a local drop.</p><div data-traffic-settings><fieldset class="fields">${controls.join('')}<div class="full traffic-mix-heading"><h4>Request mix</h4><p class="hint">Relative weights are normalized. All three request classes have known-answer checks.</p></div>${Object.entries(mix.properties).map(([name,s])=>schemaField(`traffic_mix_${name}`,traffic.mix[name],s,{label:`${human(name)} weight`})).join('')}</fieldset></div></div>`;
}
function syncTestControls() {
  const form=$('#new-run-form');if(!form)return;
  for(const check of state.bootstrap.checks){
    const card=$(`[data-test-card="${check.id}"]`),selected=$('input[name="check"]',card).checked;
    const open=state.openTest===check.id;
    card.classList.toggle('test-disabled',!selected);
    const editor=$('[data-test-editor]',card);editor.hidden=!open;
    $('[data-configure-test]',card).setAttribute('aria-expanded',String(open));
    $('.draft-notice',card).hidden=selected;
    for(const fieldset of $$('fieldset',editor))fieldset.disabled=!open;
    if(check.id==='capacity')$('.traffic-profile-summary',card).hidden=!selected;
    if(check.id==='gpu')form.elements.metrics_url.disabled=!open;
  }
}
function toggleTestEditor(id,close=false) {
  state.openTest=close||state.openTest===id?null:id;
  syncTestControls();updateBounds();
  if(close)$(`[data-configure-test="${id}"]`).focus();
}
function editTestField(input) {
  const form=$('#new-run-form'),settings=input.closest('[data-test-settings]');
  if(settings){input.dataset.inherited='false';form.elements[`test_mode_${settings.dataset.testSettings}`].value='custom';}
  if(input.closest('[data-traffic-settings]'))form.elements.traffic_mode.value='custom';
  syncTestControls();
}
function validateRunForm(form) {
  // Closed editors are disabled for native validation and read explicitly by readForm.
  // Inspect selected drafts here, revealing the field before asking the browser to focus it.
  for(const input of form.querySelectorAll('input, select, textarea')){
    const card=input.closest('[data-test-card]');
    if(card){
      const id=card.dataset.testCard;
      if(!$('input[name="check"]',card).checked)continue;
      if(input.closest('[data-test-settings]')&&form.elements[`test_mode_${id}`].value!=='custom')continue;
      if(input.closest('[data-traffic-settings]')&&form.elements.traffic_mode.value!=='custom')continue;
    }
    const fieldset=input.closest('fieldset'),disabled=input.disabled,fieldsetDisabled=fieldset?.disabled;
    if(fieldset)fieldset.disabled=false;input.disabled=false;
    const valid=input.checkValidity();
    input.disabled=disabled;if(fieldset)fieldset.disabled=fieldsetDisabled;
    if(valid)continue;
    if(card){state.openTest=card.dataset.testCard;syncTestControls();}
    for(let parent=input.parentElement;parent;parent=parent.parentElement)if(parent.tagName==='DETAILS')parent.open=true;
    state.configView='form';syncConfigView();input.reportValidity();return false;
  }
  return true;
}
function refreshInheritedFields(id,onlyInherited=false) {
  const check=state.bootstrap.checks.find(item=>item.id===id),form=$('#new-run-form');
  const config=clone(state.config);for(const [name] of numericFields)config[name]=Number(form.elements[name].value);
  config.limits={...config.limits};for(const name of check.limit_fields){const input=form.elements[`limit_${name}`];if(input)config.limits[name]=input.value===''?null:Number(input.value);}
  for(const name of check.option_fields){const input=form.elements[`test_${id}_${name}`];if(!onlyInherited||input.dataset.inherited==='true'){input.value=optionDefault(config,check,name) ?? '';input.dataset.inherited='true';}}
  for(const name of check.limit_fields){const input=form.elements[`test_${id}_limit_${name}`];if(!onlyInherited||input.dataset.inherited==='true'){input.value=config.limits[name] ?? '';input.dataset.inherited='true';}}
}
function renderNew() {
  state.openTest=null;
  const c=state.config,target=c.targets?.[0] || {},limits=c.limits || {};
  $('#main').innerHTML=`<div class="page-heading"><div><div class="eyebrow">Built-in acceptance suite</div><h1>New run</h1><p>Connect an existing endpoint. Set the bounds. Let the suite do the work.</p></div><a class="button ghost" href="#runs">Back to runs</a></div>
  <div class="notice" id="new-active-notice" ${state.active?'':'hidden'}><strong>A run is already active.</strong> <a href="${state.active?linkRun(state.active):'#runs'}">Inspect or stop it</a> before starting another.</div>
  <form id="new-run-form" novalidate><div class="config-toolbar"><div class="result-tabs config-tabs" role="tablist" aria-label="Configuration view"><button type="button" role="tab" id="config-tab-form" data-config-tab="form" aria-controls="config-panel-form">Form</button><button type="button" role="tab" id="config-tab-json" data-config-tab="json" aria-controls="config-panel-json">JSON</button></div><div class="actions"><button type="button" class="text-button" data-action="import-config">Import config</button><button type="button" class="text-button" data-action="export-config">Export config</button><button type="button" class="text-button" data-action="copy-cli">Copy CLI</button></div><input type="file" id="config-file" accept=".json,application/json" hidden></div><div class="notice config-draft-notice" id="config-draft-notice" hidden><div><strong>Unapplied JSON changes.</strong> Apply or discard them before editing the form, running, exporting, or copying the CLI command.</div><div class="actions"><button class="button small" type="button" data-action="apply-config">Apply changes</button><button class="text-button" type="button" data-action="discard-config">Discard changes</button></div></div><div id="config-error" class="error-message config-action-error" role="alert" hidden></div><div class="panel cli-fallback" id="cli-fallback" hidden><label for="cli-command">Clipboard unavailable. Copy this command:</label><textarea id="cli-command" readonly spellcheck="false"></textarea></div><div class="form-layout"><div><div id="config-panel-form" role="tabpanel" aria-labelledby="config-tab-form"><section class="panel form-section"><h2>Endpoint</h2><p class="hint">Requests are sent by this local runner to your OpenAI-compatible chat endpoint.</p><div class="fields">
  ${field('url','Endpoint URL',target.url,{type:'url',required:true,full:true,placeholder:'http://localhost:11434/v1'})}
  ${field('model','Model',target.model,{required:true,placeholder:'e.g. qwen2.5:7b-instruct'})}${field('name','Target name',target.name,{required:true,placeholder:'local'})}
  </div>${(c.targets || []).length>1?`<div class="notice multi-target-notice"><strong>${c.targets.length} targets configured.</strong> These fields edit the first target. All remaining models, routes and replicas are preserved; edit them in JSON.</div>`:''}</section>
  <section class="panel form-section"><h2>Traffic bounds</h2><p class="hint">Hard ceilings for the whole run, shared across all configured targets.</p><div class="fields three">${numericFields.map(([key,label,unit,min,step])=>field(key,label,c[key],{type:'number',hint:unit,min,step,required:true})).join('')}</div></section>
  <section class="panel form-section"><div class="test-suite-heading"><div><h2>Choose tests</h2><p class="hint" id="selection-summary"></p></div><div class="actions"><button class="text-button" type="button" data-action="select-tests">Select all</button><button class="text-button" type="button" data-action="clear-tests">Clear</button></div></div><p class="hint">Select tests to run with defaults. Configure only what you need to change.</p><div class="test-cards">${(state.bootstrap.checks || []).map(check=>renderTestCard(check,c)).join('')}</div></section>

  <details class="panel details-box"><summary>Acceptance limits <span class="dim">· optional performance targets</span></summary><div class="details-content"><p class="hint">A missing performance limit means an observation, or an inconclusive acceptance check. It does not certify a response-time requirement.</p><div class="fields">${limitFields.map(([key,label,unit])=>field(`limit_${key}`,label,limits[key],{type:'number',hint:unit,min:key==='fairness_max_ratio'?1.001:.001,step:'any',placeholder:'Not set'})).join('')}${field('limit_max_error_rate','Maximum error rate',limits.max_error_rate,{type:'number',hint:'Fraction: 0 to 1',min:0,step:'any'})}${field('limit_min_correctness','Minimum correctness',limits.min_correctness,{type:'number',hint:'Fraction: 0 to 1',min:0,step:'any'})}${field('limit_regression_percent','Material regression threshold',limits.regression_percent,{type:'number',hint:'Percent; percentage points for rates',min:0,step:'any'})}</div></div></details>
  </div><section class="panel form-section" id="config-panel-json" role="tabpanel" aria-labelledby="config-tab-json" hidden><h2>Run configuration</h2><p class="hint">Edit the same configuration as the form, including additional workload profiles, deployment metadata, serving telemetry, multiple targets, credentials via environment-variable names, and request options. Apply changes to validate them.</p><label for="config-editor">Configuration · JSON</label><textarea class="config-editor" id="config-editor" spellcheck="false">${escape(state.jsonDraft ?? JSON.stringify(c,null,2))}</textarea><div class="editor-actions"><button class="button small" type="button" data-action="apply-config">Apply changes</button><span class="hint" id="json-status" role="status"></span></div></section>
  <section class="panel form-section"><h2>Baseline</h2><p class="hint">Compare to a run you explicitly saved. Running never replaces a baseline.</p><label>Compare against<select name="baseline_id">${baselineOptions(state.prefillBaseline || '')}</select></label></section>
  <section class="panel form-section" id="execution-settings"><h2>Execution</h2><label>Execution mode<select name="mode"><option value="normal">Normal · use the existing service</option><option value="cold-start">Cold start · explicitly restart one target</option></select></label><div id="restart-controls" hidden style="margin-top:16px"><label>Target to restart<select name="restart_target">${(c.targets || []).map(t=>`<option value="${escape(t.name)}">${escape(t.name)}</option>`).join('')}</select></label><p class="hint" id="restart-hook" style="margin-top:12px"></p><p class="hint" style="margin-top:8px">Only this named target's configured restart command will run.</p></div></section>
  </div><aside class="panel run-preview sticky-panel"><h3>Ready to test</h3><p>Built-in fixtures. Bounded traffic.<br>A report with every finding.</p><dl class="bound-list" id="run-bounds"></dl><button class="button primary" type="submit" id="start-run" ${state.active?'disabled':''}>Run suite ${icon('arrow')}</button><p class="hint">This sends inference requests to your configured endpoint${c.targets?.length>1?'s':''}. You can stop the run at any time.</p><div id="form-error" class="error-message" role="alert" hidden></div></aside></div></form>`;
  for(const check of state.bootstrap.checks){
    const options=c.test_options?.[check.id] || {};
    for(const name of check.option_fields)$('#new-run-form').elements[`test_${check.id}_${name}`].dataset.inherited=String(options[name]==null);
    for(const name of check.limit_fields)$('#new-run-form').elements[`test_${check.id}_limit_${name}`].dataset.inherited=String(!Object.hasOwn(options.limits || {},name));
  }
  const form=$('#new-run-form');form.elements.mode.value=state.formMode;
  if(state.formRestartTarget&&c.targets.some(t=>t.name===state.formRestartTarget))form.elements.restart_target.value=state.formRestartTarget;
  if(state.jsonDraft===null){state.jsonDraft=JSON.stringify(portableConfig(readForm(true)),null,2);state.jsonBase=state.jsonDraft;$('#config-editor').value=state.jsonDraft;}
  syncTestControls();updateBounds();updateRestart();syncConfigView();
}
function readForm(allowEmpty=false) {
  const form=$('#new-run-form'),data=new FormData(form);let config=clone(state.config);
  // Disabled editors still retain their values when a test is unchecked.
  for(const input of form.querySelectorAll('[data-test-settings] input, [data-traffic-settings] input, [data-traffic-settings] select'))data.set(input.name,input.value);
  if(!config.targets?.length)throw new Error('Configuration must contain at least one target.');
  const first=config.targets[0];for(const key of ['url','model','name'])first[key]=String(data.get(key) || '').trim();
  first.metrics_url=form.elements.metrics_url.value.trim() || null;
  for(const [key] of numericFields)config[key]=data.get(key)===''?null:Number(data.get(key));
  config.limits ||= {};
  for(const key of [...limitFields.map(x=>x[0]),'max_error_rate','min_correctness','regression_percent']){const v=data.get(`limit_${key}`);config.limits[key]=v===''?null:Number(v);}
  config=configureTests(config,data,state.bootstrap.checks,state.bootstrap.config.traffic,{allowEmpty,allowInvalid:allowEmpty});
  config.restart_target=null;
  return {config,baseline_id:data.get('baseline_id') || null,mode:data.get('mode') || 'normal',restart_target:data.get('mode')==='cold-start'?data.get('restart_target'):null};
}
function updateBounds() {
  const selected=$$('#new-run-form input[name="check"]:checked').length;
  $('#selection-summary').textContent=`${selected} of ${state.bootstrap.checks.length} tests selected`;
  let payload;try{payload=readForm(true);}catch{return;}
  const c=payload.config;
  for(const check of state.bootstrap.checks){
    const card=$(`[data-test-card="${check.id}"]`);
    $('[data-setting-state]',card).textContent=testSettingsLabel(check,c);
  }
  $('#run-bounds').innerHTML=[['Targets',number(c.targets.length)],['Concurrency',`≤ ${number(c.concurrency)}`],['Request budget',`≤ ${number(c.max_requests)}`],['Run duration',`≤ ${elapsed(c.max_duration_seconds)}`],['Request deadline',elapsed(c.request_timeout_seconds)],['Output cap',`${number(c.max_output_tokens)} tokens`],['Mode',payload.mode==='normal'?'Existing service':'Explicit restart']].map(([k,v])=>`<div><dt>${escape(k)}</dt><dd>${escape(v)}</dd></div>`).join('');
  $('#selection-summary').textContent=`${c.checks.length} of ${state.bootstrap.checks.length} tests selected`;
  $('#run-bounds').insertAdjacentHTML('afterbegin',`<div><dt>Selected tests</dt><dd>${c.checks.length}</dd></div>`);
  const traffic=c.traffic,profile=$('.traffic-profile-summary');
  if(profile)profile.textContent=`${traffic.rates.join(' → ')} requests/s · ${traffic.duration_seconds}s per stage · ${traffic.arrival==='poisson'?'uneven':'steady'} arrivals · up to ${traffic.max_in_flight ?? c.concurrency} in flight`;
}
function updateRestart(){const form=$('#new-run-form');if(!form)return;const mode=form.elements.mode.value;$('#restart-controls').hidden=mode!=='cold-start';const name=form.elements.restart_target.value;const target=state.config.targets.find(t=>t.name===name);$('#restart-hook').textContent=target?.restart_command?.length?`Configured command: ${target.restart_command.join(' ')}`:'No restart command is configured for this target. Add restart_command in the configuration before using cold start.';updateBounds();}
async function startRun(event) {
  event.preventDefault();if(state.submitting||state.active||state.configBusy)return;
  const version=state.routeVersion,errorEl=$('#form-error');errorEl.hidden=true;
  try {
    assertAppliedConfig();
    if(!validateRunForm(event.target))return;
    state.submitting=true;$('#start-run').disabled=true;$('#start-run').textContent='Validating settings…';
    const payload=await validatedSnapshot();
    if(payload.mode==='cold-start'&&!payload.config.targets.find(t=>t.name===payload.restart_target)?.restart_command?.length)throw new Error('Cold start requires a configured restart_command for the selected target.');
    if(version!==state.routeVersion)return;
    $('#start-run').textContent='Starting suite…';
    const result=await api('/api/runs',{method:'POST',body:JSON.stringify(payload)});state.config=clone(payload.config);setActive(result.id);if(version===state.routeVersion)location.hash=linkRun(result.id);
  }catch(error){if(version!==state.routeVersion){toast(`Run was not started: ${error.message}`);return;}errorEl.textContent=error.message;errorEl.hidden=false;if($('#start-run')){$('#start-run').disabled=Boolean(state.active);$('#start-run').innerHTML=`Run suite ${icon('arrow')}`;}}finally{state.submitting=false;}
}
function reportConfig(report){return report?.manifest?.config || state.detail?.config || {};}
function mainFinding(report) {
  const checks=report.checks.filter(c=>c.id!=='baseline'),failed=checks.find(c=>c.status==='fail');
  if(failed){if(failed.metrics?.scored&&failed.metrics.correct<failed.metrics.scored)return `${failed.title}: ${failed.metrics.correct} of ${failed.metrics.scored} correct`;return `${failed.title} failed`;}
  if(report.abort_reason)return 'Run stopped with partial results';
  const uncertain=checks.find(c=>['blocked','inconclusive'].includes(c.status)&&c.required);
  if(uncertain)return `${uncertain.title} is ${uncertain.status}`;
  if(report.overall==='pass')return 'Selected acceptance checks passed';
  if(report.baseline?.status==='fail')return 'A material regression was detected';
  return 'Review the recorded acceptance results';
}
function renderDetail() {
  const d=state.detail;if(activeState(d)){renderLive();return;}
  const report=d.report;
  if(!report){$('#main').innerHTML=`<div class="page-heading"><div><div class="eyebrow">Run ${escape(d.id)}</div><h1>Run could not finish</h1></div><a class="button" href="#new">New run</a></div>${notice(d.error || 'The runner did not produce a report.','error')}<button class="button" data-action="rerun">Review settings</button>`;return;}
  const config=reportConfig(report),checks=report.checks.filter(c=>c.id!=='baseline'),counts={};checks.forEach(c=>counts[c.status]=(counts[c.status] || 0)+1);
  const required=checks.filter(c=>c.required);
  const absolute=checks.some(c=>c.status==='fail')?'fail':required.some(c=>c.status==='blocked')?'blocked':report.abort_reason||!required.length||required.some(c=>c.status==='inconclusive')?'inconclusive':required.every(c=>c.status==='skipped')?'skipped':'pass';
  $('#main').innerHTML=`<div class="page-heading result-heading"><div><div class="eyebrow">Run <span class="mono">${escape(report.run_id)}</span></div>${badge(absolute,`Acceptance ${statusLabels[absolute]?.toLowerCase()}`)}<h1>${escape(mainFinding(report))}</h1><div class="run-meta"><span><strong>${escape((config.targets || []).map(t=>t.model).join(', '))}</strong></span><span>${date(report.started_at)}</span><span>${duration(report)}</span><span>${number(report.requests.length)} requests recorded</span></div></div><div class="actions"><button class="button" data-action="rerun">${icon('repeat')} Run again</button><button class="button primary" data-action="baseline-dialog">Save baseline</button></div></div>
  ${report.abort_reason?notice(`Partial run: ${report.abort_reason}`,'warning'):''}${d.error?notice(d.error,'error'):''}
  <div class="result-summary">${['pass','fail','inconclusive','blocked','skipped'].filter(k=>counts[k]).map(k=>`<div class="summary-item"><strong>${counts[k]}</strong>${badge(k)}</div>`).join('')}<span style="margin-left:auto" class="hint">${escape((config.targets || []).map(t=>`${t.name} · ${t.route || 'direct'}`).join(' / '))}</span></div>
  <div class="result-tabs" role="tablist" aria-label="Run report"><button role="tab" id="tab-checks" aria-selected="${state.tab==='checks'}" aria-controls="result-content" class="${state.tab==='checks'?'selected':''}" data-tab="checks">Checks & evidence</button><button role="tab" id="tab-baseline" aria-selected="${state.tab==='baseline'}" aria-controls="result-content" class="${state.tab==='baseline'?'selected':''}" data-tab="baseline">Baseline${report.baseline?` · ${escape(statusLabels[report.baseline.status] || report.baseline.status)}`:''}</button><button role="tab" id="tab-config" aria-selected="${state.tab==='config'}" aria-controls="result-content" class="${state.tab==='config'?'selected':''}" data-tab="config">Run configuration</button></div>
  <section id="result-content" role="tabpanel" aria-labelledby="tab-${state.tab}">${state.tab==='checks'?renderChecks(report):state.tab==='baseline'?renderComparison(report):renderConfig(report)}</section>
  <div class="actions" style="margin-top:24px"><a class="button small" href="${runPath(d.id)}/report.html" download>Download HTML report</a><a class="button small" href="${runPath(d.id)}/report.json" download>Download JSON</a></div>`;
  syncComparisonFilters();
}
function keyMetric(check){if(check.status==='skipped')return 'Not selected';const m=check.metrics || {};if(['context','correctness','json'].includes(check.id)&&m.scored)return `${m.correct} / ${m.scored} correct`;if(check.id==='capacity'&&Object.hasOwn(m,'highest_acceptable_rate_rps'))return m.highest_acceptable_rate_rps==null?'Arrival capacity unconfirmed':`${number(m.highest_acceptable_rate_rps,2)} requests/s accepted`;if(check.id==='capacity'&&m.highest_tested_acceptable_concurrency!=null)return `Concurrency ${m.highest_tested_acceptable_concurrency} accepted`;if(check.id==='first_output')return `${ms(m.first_output_p95_ms)} p95 · ${m.attempted || 0} requests`;if(check.id==='generation')return `${ms(m.p95_ms)} p95 · ${m.attempted || 0} requests`;if(check.status==='skipped')return 'Not selected';return m.attempted!=null?`${number(m.attempted)} requests · ${check.target}`:check.target;}
function renderChecks(report) {
  const checks=report.checks.filter(c=>c.id!=='baseline').sort((a,b)=>(order[a.status]??5)-(order[b.status]??5));
  if(!checks.length)return notice('This report contains no check results.');
  let selected=checks.find(c=>`${c.target}:${c.id}`===state.selectedCheck) || checks[0];state.selectedCheck=`${selected.target}:${selected.id}`;
  return `<div class="result-grid"><div class="panel check-list" aria-label="Suite checks">${checks.map(c=>`<button class="check-item ${c===selected?'selected':''}" data-check="${escape(`${c.target}:${c.id}`)}" aria-pressed="${c===selected}"><span><span class="check-name">${escape(c.title)}</span><small>${escape(keyMetric(c))}${(reportConfig(report).targets || []).length>1?` · ${escape(c.target)}`:''}</small></span>${badge(c.status)}</button>`).join('')}</div><section class="panel check-detail">${renderCheck(selected,report)}</section></div>`;
}
function metric(label,value,detail=''){return `<div class="metric"><dt>${escape(label)}</dt><dd>${escape(value)}</dd>${detail?`<small>${escape(detail)}</small>`:''}</div>`;}
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
    else if(check.id==='capacity'&&m.traffic_stages){metrics.push(metric('Highest accepted arrival rate',m.highest_acceptable_rate_rps==null?'Unconfirmed':`${number(m.highest_acceptable_rate_rps,2)} requests/s`,'Highest tested acceptable rate; maximum capacity unknown'));metrics.push(metric('Rate stages',number(m.traffic_stages.length),'Each stage is offered independently, then drained'));}
    else if(check.id==='capacity'){metrics.push(metric('Highest accepted load',m.highest_tested_acceptable_concurrency==null?'Unconfirmed':number(m.highest_tested_acceptable_concurrency),'Highest tested; maximum capacity unknown'));metrics.push(metric('Response time · p95',ms(m.p95_ms),`${m.completed ?? 0} valid completions`));}
    else if(check.id==='fairness'){metrics.push(metric('Mixed / short-only latency',m.latency_ratio==null?'Unmeasured':`${number(m.latency_ratio,2)}×`,limits.fairness_max_ratio?`Limit: ${limits.fairness_max_ratio}×`:'No slowdown limit set'));metrics.push(metric('Observed overlap',`${m.overlaps ?? 0} / ${m.pairs ?? 0}`,'Pairs with concurrent prefill'));}
    else {metrics.push(metric('Response time · p95',ms(m.p95_ms),limits.latency_ms?`Limit: ${ms(limits.latency_ms)}`:'No response-time limit set'));metrics.push(metric('Longest stream pause',ms(m.max_stream_gap_ms),limits.stream_gap_ms?`Limit: ${ms(limits.stream_gap_ms)}`:'No stream-pause limit set'));}
  }
  if(check.id==='generation'){
    const current=Object.hasOwn(m,'generation_rate_samples');
    metrics.push(metric('Generation pace · median (estimate)',m.generation_tokens_per_second_p50==null?'Unmeasured':`${number(m.generation_tokens_per_second_p50,1)} tok/s`,current?`${m.generation_rate_samples} measured samples; excludes waiting for first output`:'Older report: generation timing was not recorded'));
    if(current)metrics.push(metric('Generation pace · slowest (estimate)',m.generation_tokens_per_second_min==null?'Unmeasured':`${number(m.generation_tokens_per_second_min,1)} tok/s`,limits.min_output_tokens_per_second?`Every measurable request must reach ${limits.min_output_tokens_per_second} tok/s`:'No minimum generation rate set'));
    metrics.push(metric('End-to-end output rate · median',m.output_tokens_per_second_p50==null?'Not reported':`${number(m.output_tokens_per_second_p50,1)} tok/s`,current?'Includes waiting for first output; separate from generation pace':'Older report: the output-rate limit used this end-to-end definition'));
  }
  const extras=Object.fromEntries(Object.entries(m).filter(([k,v])=>typeof v==='object'&&v!==null || ['backend_cancellation','near_limit_coverage','tls','tls_expiry'].includes(k)));
  return `<div class="check-detail-head"><div class="check-detail-heading"><h2>${escape(check.title)}</h2>${badge(check.status)}</div><p>${escape(check.summary)}</p>${!check.required?'<p class="hint">Optional check</p>':''}</div>
  ${check.status==='skipped'?'<div class="empty-state" style="padding:35px 24px"><p>This optional check was not selected for the run. No result is inferred.</p></div>':`<dl class="metrics-grid">${metrics.join('')}</dl>`}
  ${check.id==='cancellation'?'<div class="notice" style="margin:20px 24px 0">Cancellation and deadline probes deliberately interrupt requests. Backend resource reclamation remains unverified.</div>':''}
  ${check.id==='recovery'?`<div class="notice" style="margin:20px 24px 0">Sustained traffic was configured for ${number(config.sustained_seconds,1)} seconds. Results describe this bounded observation period.</div>`:''}
  ${check.id==='capacity'&&m.traffic_stages?renderTrafficStages(m.traffic_stages):''}
  ${check.id==='capacity'&&m.levels?`<div class="metric-table"><table><thead><tr><th>Load</th><th>Achieved</th><th>Correct</th><th>p95</th></tr></thead><tbody>${Object.entries(m.levels).map(([k,v])=>`<tr><td class="mono">${escape(k)}</td><td>${number(v.achieved_concurrency)}</td><td>${v.correct ?? 0} / ${v.scored ?? 0}</td><td class="mono">${ms(v.p95_ms)}</td></tr>`).join('')}</tbody></table></div>`:''}
  ${check.id==='gpu'&&m.snapshots?renderGpuSnapshots(m.snapshots):''}
  ${records.length?`<div class="evidence-title"><span>Request evidence</span><span class="hint">${records.length} recorded · answer failures first</span></div><div class="evidence-table"><table><thead><tr><th>Fixture / scenario</th><th>Response</th><th>Answer</th><th>Time</th></tr></thead><tbody>${sorted.map(r=>`<tr><td><button class="text-button" data-request="${escape(r.id)}">${escape(r.fixture_id)}</button><small>${escape(r.scenario)}</small></td><td>${escape(r.status)}${isProbe(r)?'<small class="request-probe">Deliberate probe</small>':''}</td><td>${r.score===false?'<span class="status fail">Incorrect</span>':r.score===true?'<span class="status pass">Correct</span>':'<span class="dim">Unscored</span>'}</td><td class="mono nowrap">${ms(r.elapsed_ms)}</td></tr>`).join('')}</tbody></table></div>`:check.status==='skipped'?'':'<div class="check-observations hint">No request evidence was recorded for this check.</div>'}
  ${Object.keys(extras).length?`<details class="check-observations"><summary>Additional measurements and coverage</summary><pre>${escape(JSON.stringify(extras,null,2))}</pre></details>`:''}`;
}
function renderGpuSnapshots(snapshots){return snapshots.map(snapshot=>`<div class="metric-table"><h3>${escape(human(snapshot.phase))} <span class="hint">${date(snapshot.collected_at)}</span></h3>${snapshot.reason?`<p class="hint" style="margin:12px 0">${escape(snapshot.reason)}</p>`:''}${snapshot.unavailable?.length?`<p class="hint" style="margin:12px 0">Unavailable: ${snapshot.unavailable.map(human).map(escape).join(', ')}</p>`:''}${snapshot.samples?.length?`<table><thead><tr><th>Metric</th><th>Value</th><th>Freshness</th></tr></thead><tbody>${snapshot.samples.map(s=>`<tr><td>${escape(s.name)}<small>${escape(s.labels)}</small></td><td class="mono">${number(s.value,3)}</td><td>${s.stale?'Stale':s.age_seconds==null?'Scrape only':`${number(s.age_seconds,1)}s old`}</td></tr>`).join('')}</tbody></table>`:''}</div>`).join('');}
function renderTrafficStages(stages){
  return `<div class="traffic-summary"><h3>Continuous arrival stages</h3><p class="hint">Arrival latency includes local dispatch delay. Server queue time is not measured. Cohort goodput counts correct, valid answers within all configured limits, including completions during drain, per second of the arrival window.</p></div><div class="metric-table traffic-stage-table"><table><thead><tr><th>Offered rate</th><th>Scheduled / sent</th><th>Failures / timeouts</th><th>Local drops</th><th>Arrival latency · p95</th><th>Cohort goodput</th><th>Result</th></tr></thead><tbody>${stages.map(stage=>`<tr><td class="mono">${number(stage.rate_rps,2)} req/s<small>${number(stage.duration_seconds,1)}s arrival window</small></td><td>${number(stage.scheduled)} / ${number(stage.started)}<small>${number(stage.not_offered || 0)} not offered</small></td><td>${number(stage.failed)} / ${number(stage.timed_out)}<small>${stage.correctness==null?'Unscored':`${number(stage.correctness*100,1)}% correct`}</small></td><td>${number(stage.dropped_local || 0)} at limit<small>${number(stage.dropped_late || 0)} late · dispatch p95 ${ms(stage.dispatch_lag_p95_ms)}</small></td><td class="mono">${ms(stage.arrival_latency_p95_ms)}<small>First answer ${ms(stage.arrival_first_output_p95_ms)}</small></td><td class="mono">${number(stage.goodput_rps,2)} req/s<small>${number(stage.good)} good · ${stage.good_fraction==null?'No arrivals':`${number(stage.good_fraction*100,1)}% of arrivals`}</small></td><td>${badge(stage.status)}<small>${escape((stage.reasons || []).join('; '))}</small></td></tr>`).join('')}</tbody></table></div>${stages.some(s=>s.windows?.length)?`<details class="check-observations"><summary>Waiting and failures through each stage</summary>${stages.map(stage=>`<div class="metric-table"><h3>${number(stage.rate_rps,2)} requests/s</h3><table><thead><tr><th>Arrival window</th><th>Pending at end</th><th>Arrival latency · p95</th><th>Failures</th><th>Local drops</th></tr></thead><tbody>${(stage.windows || []).map(w=>`<tr><td>${number(w.from_seconds,1)}–${number(w.to_seconds,1)}s</td><td>${number(w.pending_at_end)}</td><td>${ms(w.arrival_latency_p95_ms)}</td><td>${w.error_rate==null?'—':`${number(w.error_rate*100,1)}%`}</td><td>${number((w.dropped_local || 0)+(w.dropped_late || 0))}</td></tr>`).join('')}</tbody></table></div>`).join('')}</details>`:''}`;
}
function renderTrafficComparison(capacity){
  const rate=value=>value==null?'Unconfirmed':`${number(value,2)} req/s`;
  return `<div class="traffic-summary"><h3>Tested arrival capacity ${badge(capacity.status)}</h3><p class="hint">${escape(capacity.reason || '')}</p><dl class="metrics-grid"><div class="metric"><dt>Baseline accepted rate</dt><dd>${rate(capacity.baseline_highest_acceptable_rate_rps)}</dd></div><div class="metric"><dt>Current accepted rate</dt><dd>${rate(capacity.current_highest_acceptable_rate_rps)}</dd></div><div class="metric"><dt>Capacity change</dt><dd>${capacity.delta_rps==null?'Unconfirmed':`${capacity.delta_rps>0?'+':''}${number(capacity.delta_rps,2)} req/s`}</dd><small>${capacity.change_percent==null?'Matching, complete stages required':`${capacity.change_percent>0?'+':''}${number(capacity.change_percent,1)}%`}</small></div></dl></div>${capacity.stages?.length?`<div class="metric-table"><table><thead><tr><th>Rate</th><th>Baseline goodput</th><th>Current goodput</th><th>Capacity evidence</th></tr></thead><tbody>${capacity.stages.map(stage=>`<tr><td>${rate(stage.rate_rps)}</td><td>${rate(stage.baseline?.goodput_rps)}</td><td>${rate(stage.current?.goodput_rps)}</td><td>${badge(stage.status)}<small>${escape(stage.reason || '')}</small>${stage.timing_comparable===false?`<small>${escape(stage.timing_comparison_reason || '')}</small>`:''}</td></tr>`).join('')}</tbody></table></div>`:''}`;
}
function isProbe(r){return ['client_cancel','client_deadline','deadline_probe'].includes(r.scenario)||/intentional cancellation/i.test(r.error || '');}
function renderComparison(report) {
  const b=report.baseline;if(!b)return `<div class="panel empty-state"><h2>No baseline selected</h2><p>This run still evaluates its configured acceptance limits. Save a reference run, then select it when starting another suite to measure change.</p><button class="button primary" data-action="baseline-dialog">Save this run as a baseline</button></div>`;
  const allRows=(b.targets || []).flatMap(t=>t.comparisons || []),checkIds=[...new Set(allRows.map(r=>r.check))].sort();
  state.comparisonFilter ??= allRows.some(r=>r.status!=='pass')?'attention':'all';
  const trafficOnly=!allRows.length&&(b.targets || []).some(target=>target.traffic_capacity);
  const filters=trafficOnly?'':`<div class="comparison-filters"><div><label for="comparison-outcome">Metric outcome</label><select id="comparison-outcome"><option value="attention">Needs attention</option><option value="all">All outcomes</option><option value="fail">Regressions</option><option value="inconclusive">Inconclusive</option><option value="pass">No material regression</option></select></div><div><label for="comparison-check">Check</label><select id="comparison-check"><option value="all">All checks</option>${checkIds.map(id=>`<option value="${escape(id)}">${escape(state.bootstrap.checks.find(c=>c.id===id)?.title || human(id))}</option>`).join('')}</select></div><span class="hint">${number(allRows.length)} recorded metric comparisons</span></div>`;
  const filterRow=row=>(state.comparisonFilter==='all'||(state.comparisonFilter==='attention'?row.status!=='pass':row.status===state.comparisonFilter))&&(state.comparisonCheck==='all'||row.check===state.comparisonCheck);
  return `${filters}<div class="notice"><strong>${escape(statusLabels[b.status] || b.status)} comparison</strong> against <span class="mono">${escape(b.run_id)}</span>. Baseline results are separate from absolute acceptance checks.</div>${(b.targets || []).map(t=>{
    const rows=[...(t.comparisons || [])].filter(filterRow).sort((a,c)=>(order[a.status]??5)-(order[c.status]??5));
    const onlyTraffic=!(t.comparisons || []).length&&Boolean(t.traffic_capacity);
    const count=onlyTraffic?`${t.traffic_capacity.stages?.length || 0} traffic stages compared`:`${rows.length} / ${(t.comparisons || []).length} metrics shown`;
    const summary=onlyTraffic?t.traffic_capacity.reason:t.summary || report.checks.find(c=>c.id==='baseline'&&c.target===t.target)?.summary || '';
    return `<section class="panel comparison-section"><div class="section-heading"><div><h2>${escape(t.target || t.name || 'Target')} <span class="hint">${escape(count)}</span></h2><p>${escape(summary)}</p></div>${badge(t.status)}</div>${t.traffic_capacity?renderTrafficComparison(t.traffic_capacity):''}<details class="check-observations"><summary>Deployment changes · ${t.configuration_diff?.confounded?'confounded experiment':'causality unverified'}</summary><pre>${escape(JSON.stringify(t.configuration_diff || {unknown_baseline:true},null,2))}</pre></details><details class="check-observations"><summary>Serving telemetry and repeatability comparison</summary><pre>${escape(JSON.stringify({telemetry:t.telemetry,consistency:t.consistency_changes},null,2))}</pre></details>${(t.reasons || []).length?`<div class="comparison-reasons">${t.reasons.map(x=>`<p>${escape(x)}</p>`).join('')}</div>`:''}${rows.length?`<div class="comparison-table"><table><thead><tr><th>Metric / scenario</th><th>Baseline</th><th>Current</th><th>Worsening</th><th>Result</th></tr></thead><tbody>${rows.map(row=>`<tr><td>${escape(row.metric==='output_tokens_per_second'?'End-to-end output rate':row.metric==='generation_tokens_per_second'?'Generation pace (estimate)':human(row.metric))}<small>${escape(row.check)} · ${escape(row.scenario)}</small><small>${escape(row.reason || '')}</small></td><td class="mono">${comparisonValue(row,row.baseline)}<small>n=${number(row.baseline_samples)}</small></td><td class="mono">${comparisonValue(row,row.current)}<small>n=${number(row.current_samples)}</small></td><td class="mono">${row.worsening==null?'—':`${row.worsening>0?'+':''}${number(row.worsening,1)}${row.change_unit==='percentage_points'?' pp':'%'}`}<small>${row.threshold==null?'':`Threshold ${number(row.threshold,1)}${row.change_unit==='percentage_points'?' pp':'%'}`}</small></td><td>${badge(row.status)}</td></tr>`).join('')}</tbody></table></div>`:onlyTraffic?'':'<div class="comparison-reasons">No metric rows match these filters.</div>'}${Object.keys(t.identity_changes || {}).length?`<details class="check-observations"><summary>Declared deployment identity changes</summary><pre>${escape(JSON.stringify(t.identity_changes,null,2))}</pre></details>`:''}</section>`;
  }).join('')}<details class="panel details-box"><summary>Comparison method</summary><div class="details-content"><p class="hint">${escape(b.method || '')}</p></div></details>`;
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
  $('#main').innerHTML=`<div class="page-heading"><div><div class="eyebrow">Live run <span class="mono">${escape(d.id)}</span></div><h1>${stopping?'Stopping the suite':'Suite in progress'}</h1><p>${stopping?'No more traffic is being scheduled. Waiting for the partial report.':'Measuring the configured endpoint. Results are assessed when the suite finishes.'}</p></div><button class="button danger" data-action="stop" ${stopping?'disabled':''}>${icon('stop')}${stopping?'Stopping…':'Stop run'}</button></div><div class="live-layout"><div><section class="panel live-state">${badge(d.state)}<h2>${escape(human(progress.scenario || progress.event || 'Preparing fixtures'))}</h2><p class="live-phase">${escape(progress.target || (c.targets || []).map(t=>t.name).join(', '))} · ${escape((c.targets || []).map(t=>t.model).join(', '))}</p><dl class="live-numbers"><div><dt>Finished / started</dt><dd>${number(finished)} <span class="dim">/ ${number(started)}</span></dd></div><div><dt>Active requests</dt><dd>${number(active)}</dd></div><div><dt>Elapsed</dt><dd>${elapsed(secs)}</dd></div></dl><div class="budget-track" aria-hidden="true"><span style="width:${Math.min(100,started/(c.max_requests || 1)*100)}%"></span></div><div class="budget-caption"><span>Request budget used · ${number(started)} of ${number(c.max_requests)}</span><span>Budget ceiling, not completion</span></div></section><section class="panel" style="margin-top:20px"><div class="section-heading"><h2>Recent activity</h2><span class="hint">Refreshes automatically</span></div><div class="event-list">${[...events].reverse().slice(0,15).map(e=>`<div class="event-row"><span class="time">${elapsed(e.elapsed_seconds)}</span><span>${escape(human(e.scenario || e.event || 'Progress'))}</span><span class="phase">${escape(e.target || '')} · ${number(e.completed || 0)} finished</span></div>`).join('') || '<p class="hint" style="padding:20px 0">Waiting for the first runner event…</p>'}</div></section></div><aside class="panel run-preview"><h3>Run bounds</h3><p>The current suite shares these ceilings across all targets.</p><dl class="bound-list">${[['Concurrency',c.concurrency],['Request budget',c.max_requests],['Maximum duration',elapsed(c.max_duration_seconds)],['Request deadline',elapsed(c.request_timeout_seconds)],['Output cap',`${c.max_output_tokens} tokens`]].map(([k,v])=>`<div><dt>${escape(k)}</dt><dd>${escape(v)}</dd></div>`).join('')}</dl><hr><p class="hint">Finished requests include responses, failures, timeouts and cancellations. Some interruptions are deliberate suite probes.</p></aside></div><div id="live-error" class="error-message" role="alert" hidden></div>`;
}
function openRequest(id) {
  const report=state.detail.report,r=report.requests.find(x=>x.id===id);if(!r)return;
  const dialog=$('#request-dialog'),retention=reportConfig(report).retention || 'failures';
  dialog.innerHTML=`<div class="dialog-head"><div><div class="eyebrow">Request evidence</div><h2 id="request-title">${escape(r.fixture_id)}</h2><p class="mono">${escape(r.id)}</p></div><button class="button ghost" data-action="close-dialog" aria-label="Close request evidence">✕</button></div><div class="dialog-body"><div class="statline"><span class="pill">${escape(r.target)}</span><span class="muted">${escape(r.scenario)}</span><span>Response: ${escape(r.status)}</span>${r.score===false?badge('fail','Incorrect answer'):r.score===true?badge('pass','Correct answer'):'<span class="dim">Answer unscored</span>'}</div>${isProbe(r)?'<div class="notice" style="margin-top:20px">This request is an intentional cancellation or deadline probe. Its transport status alone does not indicate a failed suite check.</div>':''}${r.error?`<div class="notice ${isProbe(r)?'':'error'}" style="margin-top:20px">${escape(r.error)}</div>`:''}<dl class="metrics-grid" style="margin-top:24px">${r.traffic_stage!=null?metric('Arrival rate',`${number(r.traffic_rate_rps,2)} requests/s`,r.traffic_class || '')+metric('Local dispatch delay',ms(r.dispatch_lag_ms),'Measured from planned arrival; not server queue time'):''}${metric('Elapsed',ms(r.elapsed_ms))}${metric('First answer',ms(r.first_output_ms))}${metric('Last answer',ms(r.last_output_ms))}${metric('First reasoning',ms(r.first_reasoning_ms))}${metric('Longest stream gap',ms(r.max_stream_gap_ms))}${metric('Input / output tokens',`${number(r.input_tokens)} / ${number(r.output_tokens)}`,'Server-reported usage')}${metric('HTTP / finish reason',`${r.http_status ?? '—'} / ${r.finish_reason || '—'}`)}</dl>${r.score_message?`<p class="hint" style="margin-top:18px">${escape(r.score_message)}</p>`:''}<h3>Answer output <span class="hint">${number(r.output_chars)} recorded characters</span></h3><div class="body-output">${r.output?`<pre>${escape(r.output)}</pre>`:r.output_chars>0?`<p class="body-not-retained">Body not retained (retention: ${escape(retention)}). Measurements and scoring are preserved.</p>`:'<p class="body-not-retained">No answer text was recorded for this request.</p>'}</div><h3>Reasoning output</h3><div class="body-output">${r.reasoning?`<pre>${escape(r.reasoning)}</pre>`:`<p class="body-not-retained">${retention==='all'?'No reasoning text was recorded.':'No reasoning body retained. This does not establish whether the model reasoned.'}</p>`}</div><p class="hint" style="margin-top:18px">${r.valid?'Valid non-empty completion':'Completion not validated'} · ${r.stream?'Streaming':'Non-streaming'} · ${r.stream_terminated?'Stream termination observed':'Stream termination not recorded'}</p></div><div class="dialog-footer"><button class="button" data-action="close-dialog">Close</button></div>`;
  dialog.showModal();
}
function openBaselineDialog(){
  const report=state.detail.report,dialog=$('#baseline-dialog');dialog.className='baseline-form';dialog.innerHTML=`<form id="baseline-form"><div class="dialog-head"><div><div class="eyebrow">Explicit reference run</div><h2 id="baseline-title">Save baseline</h2></div><button type="button" class="button ghost" data-action="close-dialog" aria-label="Close save baseline">✕</button></div><div class="dialog-body"><p class="hint">Save this exact report for future comparisons. Its recorded outcome is ${escape(statusLabels[report.overall] || report.overall)}.</p><div class="fields">${field('baseline_name','Baseline name','',{required:true,full:true,placeholder:'e.g. before-runtime-upgrade'})}</div><p class="hint" style="margin-top:12px">Existing baselines are preserved unless you explicitly replace one.</p><div id="baseline-error" class="error-message" role="alert" hidden></div><label class="replace-row" id="replace-row" hidden><input name="replace" type="checkbox">Replace the existing baseline with this name</label></div><div class="dialog-footer"><button type="button" class="button" data-action="close-dialog">Cancel</button><button type="submit" class="button primary" id="save-baseline">Save baseline</button></div></form>`;dialog.showModal();}
async function saveBaseline(event){event.preventDefault();const form=event.target,data=new FormData(form),button=$('#save-baseline'),error=$('#baseline-error');button.disabled=true;error.hidden=true;try{await api('/api/baselines',{method:'POST',body:JSON.stringify({run_id:state.detail.id,name:String(data.get('baseline_name')).trim(),replace:data.has('replace')})});const result=await api('/api/baselines');state.baselines=result.baselines || [];$('#baseline-dialog').close();toast('Baseline saved. Select it when configuring your next run.');}catch(e){error.textContent=e.message;error.hidden=false;if(e.status===409)$('#replace-row').hidden=false;}finally{button.disabled=false;}}
async function stopRun(){const button=$('[data-action="stop"]'),id=state.detail.id,version=state.routeVersion;button.disabled=true;button.textContent='Stopping…';try{const result=await api(`${runPath(id)}/cancel`,{method:'POST',body:'{}'});if(version!==state.routeVersion||state.detail?.id!==id)return;state.detail.state=result.state || 'stopping';renderDetail();schedulePoll(pollDetail,version,500);}catch(error){if(version!==state.routeVersion||state.detail?.id!==id)return;const el=$('#live-error');if(el){el.textContent=error.message;el.hidden=false;}button.disabled=false;button.textContent='Stop run';}}
function rerun(){const source=state.detail.report?.manifest?.config || state.detail.config;if(!source){toast('No configuration was saved for this run.');return;}state.config=clone(source);state.config.restart_target=null;state.formMode='normal';state.formRestartTarget=null;state.jsonDraft=null;state.jsonBase=null;state.configView='form';state.configRevision++;state.prefillBaseline=state.detail.baseline_id || '';location.hash='#new';}
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
  $('#config-panel-form').classList.toggle('config-locked',dirty);
  $('#config-draft-notice').hidden=!dirty;
  $('#json-status').textContent=dirty?'Unapplied changes':'In sync with the form';
}
function switchConfigView(view) {
  if(view===state.configView)return;
  if(view==='json'&&!hasJsonChanges()){
    captureFormDraft();state.jsonDraft=JSON.stringify(portableConfig(readForm(true)),null,2);state.jsonBase=state.jsonDraft;$('#config-editor').value=state.jsonDraft;
  }
  state.configView=view;syncConfigView();$(`#config-tab-${view}`).focus();
}
function configError(error) {const el=$('#config-error');if(el){el.textContent=error?.message || '';el.hidden=!error;}}
function discardConfig() {
  state.configRevision++;state.jsonDraft=state.jsonBase;$('#config-editor').value=state.jsonDraft;configError();syncConfigView();
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
    state.config=result.config;
    // An explicit restart target in imported/edited JSON remains visible and intentional.
    state.formMode=result.config.restart_target?'cold-start':'normal';state.formRestartTarget=result.config.restart_target;
    state.config.restart_target=null;
    state.jsonDraft=null;state.jsonBase=null;state.configRevision++;
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
  const version=state.routeVersion,revision=state.configRevision,payload=readForm(draft);
  if(!draft){
    const missing=payload.config.targets.findIndex(target=>!target.model);
    if(missing!==-1){
      switchConfigView(missing===0?'form':'json');
      if(missing===0)$('#new-run-form').elements.model.focus();else $('#config-editor').focus();
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
  const configTab=event.target.closest('[data-config-tab]');if(configTab){switchConfigView(configTab.dataset.configTab);return;}
  const tab=event.target.closest('[data-tab]');if(tab){state.tab=tab.dataset.tab;renderDetail();$(`#tab-${state.tab}`).focus();return;}
  const check=event.target.closest('[data-check]');if(check){state.selectedCheck=check.dataset.check;const selectedKey=state.selectedCheck;$('#result-content').innerHTML=renderChecks(state.detail.report);[...document.querySelectorAll('[data-check]')].find(e=>e.dataset.check===selectedKey)?.focus();return;}
  const request=event.target.closest('[data-request]');if(request){openRequest(request.dataset.request);return;}
  const action=event.target.closest('[data-action]')?.dataset.action;
  const configure=event.target.closest('[data-configure-test]')?.dataset.configureTest;
  if(configure){toggleTestEditor(configure);return;}
  const done=event.target.closest('[data-done-test]')?.dataset.doneTest;
  if(done){toggleTestEditor(done,true);return;}
  const reset=event.target.closest('[data-reset-test]')?.dataset.resetTest;
  if(reset){state.configRevision++;const form=$('#new-run-form');form.elements[`test_mode_${reset}`].value='default';refreshInheritedFields(reset);syncTestControls();updateBounds();return;}
  if(action==='select-tests'||action==='clear-tests'){state.configRevision++;for(const input of $$('#new-run-form input[name="check"]'))input.checked=action==='select-tests';syncTestControls();updateBounds();return;}
  if(action==='reset-traffic'){state.configRevision++;const form=$('#new-run-form');form.elements.traffic_mode.value='default';const defaults=state.bootstrap.config.traffic;for(const [key,value] of Object.entries(defaults)){if(key==='mix'){for(const [kind,weight] of Object.entries(value))form.elements[`traffic_mix_${kind}`].value=weight;}else form.elements[`traffic_${key}`].value=Array.isArray(value)?value.join(', '):value ?? '';}syncTestControls();updateBounds();return;}
  if(action==='retry')route();if(action==='clear-filter'){state.filter='all';renderRuns();}
  if(action==='stop')await stopRun();if(action==='rerun')rerun();if(action==='baseline-dialog')openBaselineDialog();
  if(action==='close-dialog')event.target.closest('dialog').close();if(action==='apply-config')await applyConfig();
  if(action==='discard-config')discardConfig();
  if(action==='import-config'){try{assertAppliedConfig();$('#config-file').click();}catch(error){configError(error);}}
  if(action==='export-config')await exportConfig();
  if(action==='copy-cli')await copyCli();
});
document.addEventListener('submit',event=>{if(event.target.id==='new-run-form')startRun(event);if(event.target.id==='baseline-form')saveBaseline(event);});
document.addEventListener('input',event=>{
  if(!event.target.closest('#new-run-form'))return;
  state.configRevision++;$('#cli-fallback').hidden=true;
  if(event.target.id==='config-editor'){state.jsonDraft=event.target.value;syncConfigView();return;}
  if(event.target.closest('[data-test-editor]'))editTestField(event.target);
  if(numericFields.some(([name])=>name===event.target.name)||event.target.name.startsWith('limit_'))for(const check of state.bootstrap.checks)refreshInheritedFields(check.id,true);
  updateBounds();
});
document.addEventListener('change',event=>{if(event.target.id==='config-file'){const file=event.target.files[0];event.target.value='';importConfig(file);return;}if(event.target.closest('#new-run-form'))state.configRevision++;if(event.target.closest('[data-test-editor]')&&event.target.tagName==='SELECT'){editTestField(event.target);updateBounds();}if(event.target.name==='check'){syncTestControls();updateBounds();}if(['comparison-outcome','comparison-check'].includes(event.target.id)){const id=event.target.id;if(id==='comparison-outcome')state.comparisonFilter=event.target.value;else state.comparisonCheck=event.target.value;$('#result-content').innerHTML=renderComparison(state.detail.report);syncComparisonFilters();$('#'+id).focus();}if(event.target.id==='run-filter'){state.filter=event.target.value;renderRuns();$('#run-filter').focus();}if(['mode','restart_target'].includes(event.target.name)){updateRestart();if(state.configView==='json'&&!hasJsonChanges()){state.jsonDraft=JSON.stringify(portableConfig(readForm(true)),null,2);state.jsonBase=state.jsonDraft;$('#config-editor').value=state.jsonDraft;}}if(event.target.name==='replace')$('#save-baseline').textContent=event.target.checked?'Replace baseline':'Save baseline';});
document.addEventListener('keydown',event=>{const configTab=event.target.closest('[data-config-tab]');if(configTab&&['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();switchConfigView(event.key==='Home'?'form':event.key==='End'?'json':state.configView==='form'?'json':'form');return;}const tab=event.target.closest('[data-tab]');if(tab&&['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();const tabs=['checks','baseline','config'];let i=tabs.indexOf(state.tab);i=event.key==='Home'?0:event.key==='End'?2:(i+(event.key==='ArrowRight'?1:2))%3;state.tab=tabs[i];renderDetail();$(`#tab-${state.tab}`).focus();}});
window.addEventListener('hashchange',route);
async function init(){try{const [bootstrap,baselines]=await Promise.all([api('/api/bootstrap'),api('/api/baselines')]);state.bootstrap=bootstrap;state.baselines=baselines.baselines || [];state.config=clone(bootstrap.config);state.config.restart_target=null;setActive(bootstrap.active_run_id);$('#suite-version').textContent=`v${bootstrap.suite_version || '0.1.0'}`;$('#runner-location').textContent=bootstrap.hostname || 'UI server connected';await route();}catch(error){$('#main').innerHTML=`<div class="page-heading"><h1>Local runner unavailable</h1></div>${notice(error.message,'error')}<button class="button" onclick="location.reload()">Reconnect</button>`;}}
init();
