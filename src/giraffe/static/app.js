import {bucketCoveragePlan, configureBuckets, convertExpectedAnswer, datasetCaseFromValues, changeDatasetScorer, configureJudging, configureTests, limitValue, optionDefault, optionValue, sameConfig} from './form-config.mjs?v=20261014-concurrency';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clone = (value) => structuredClone(value);
const state = {bootstrap:null, runs:[], baselines:[], detail:null, config:null, active:null, routeVersion:0, poll:null, tab:'checks', selectedCheck:null, filter:'all', comparisonFilter:null, comparisonCheck:'all', submitting:false, openTest:null, configView:'form', jsonDraft:null, jsonBase:null, configRevision:0, configBusy:false, showAvailableTests:false, formMode:'normal', formRestartTarget:null, requestPreview:null, previewSequence:0, datasetSelection:[], datasetImportSequence:0, judgeEditorOpen:null, datasetImport:null, datasetEditor:null, datasetTry:null};
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
  if($('#dataset-dialog')?.open)closeDatasetDialog();
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
const numericFields=[['context_limit','Context limit','tokens',128,1],['concurrency','Maximum in-flight requests','Requests running at the same time.',1,1],['max_requests','Total request limit','Requests allowed during the whole run.',1,1],['max_duration_seconds','Maximum duration','seconds',.1,'any'],['request_timeout_seconds','Request deadline','seconds',.1,'any'],['max_output_tokens','Output cap','tokens / request',1,1]];
const limitFields=[['first_output_ms','First output maximum','ms'],['latency_ms','Response time maximum','ms'],['stream_gap_ms','Stream pause maximum','ms'],['min_output_tokens_per_second','Minimum generation rate','tokens / second after first output (estimate)'],['fairness_max_ratio','Mixed-load slowdown','ratio, greater than 1']];
const fieldDescriptions={
  url:{hint:'Address of your LLM server.',help:'Use the address Giraffe can reach, such as http://localhost:11434/v1 for Ollama or http://localhost:8000/v1 for vLLM. Giraffe connects to an existing server; it does not start or install a model.'},
  model:{hint:'Model name used by your server.',help:'Use an exact model name from your server, including its tag or alias. For Ollama, use a name from ollama list. The model must already be installed.'},
  name:{hint:'A name for this endpoint in results.',help:'Choose a unique name such as local or staging. Giraffe uses it to identify this endpoint in reports and comparisons.'},
  context_limit:{help:'The server’s configured limit for the question and answer together, measured in tokens. A token is a small piece of text. Use the context length configured on your server, which can be lower than the model’s advertised maximum.'},
  concurrency:{hint:'Requests running at the same time.',help:'The most requests Giraffe can run at the same time across all servers. You set this number; Giraffe does not detect your hardware limit. Individual tests can use fewer requests. Raising this does not change Total request limit or Concurrency levels.'},
  max_requests:{help:'The most target requests Giraffe may use during the whole run, shared across all tests and endpoints. Some scheduled requests can use budget without reaching the server. The run can stop with fewer completed requests. AI evaluator calls have separate limits.'},
  max_duration_seconds:{help:'Stop starting work when the whole run reaches this many seconds, and save the results collected so far. A time limit can leave tests unfinished.'},
  request_timeout_seconds:{help:'How long a single request may take before Giraffe times it out. This covers waiting for the answer and receiving it. A shorter test-specific or remaining run deadline can stop it sooner.'},
  max_output_tokens:{help:'The most tokens Giraffe asks the model to generate for one request. A token is a small piece of text. The model can stop before reaching this number; the cap does not guarantee a long answer.'},
  buckets_levels:{help:'Choose how many requests to run at the same time. For 1, 4, 8, Giraffe tests one request at a time, then four together, then eight together. It repeats those loads with nine combinations of short, medium and long questions and answers. Every number must fit within Maximum in-flight requests.'},
  buckets_samples_per_bucket:{help:'The minimum requests for each test with short, medium or long questions and answers, repeated at every concurrency level. Giraffe sends the larger of this number and the load being tested. For example, a minimum of 4 still sends 8 requests when testing 8 at a time.'},
  buckets_window_seconds:{help:'The time limit for each test with short, medium or long questions and answers, at one concurrency level. Waiting for an available request slot uses this time too. When time runs out, Giraffe records any missing coverage.'},
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
  limit_max_error_rate:{help:'The largest allowed share of failed, timed-out or invalid responses. Enter a fraction from 0 to 1: 0.05 means 5%. Intentional cancellation probes are treated separately.'},
  limit_min_correctness:{help:'The smallest allowed share of correct answers among scored requests. Enter a fraction from 0 to 1: 0.95 means 95%. Missing or unscored answers can leave a test inconclusive.'},
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
  const key=name.replace(/^judge_\d+_/,'judge_').replace(/^message_\d+_/,'message_');
  return fieldDescriptions[key] || {};
}
function infoHelp(name,label,help) {
  if(!help)return '';
  const id=`field-${name}`;
  return `<button type="button" class="field-info" id="${escape(id)}-info" popovertarget="${escape(id)}-help" aria-label="About ${escape(label)}" aria-describedby="${escape(id)}-help-text"><span aria-hidden="true">i</span></button><div id="${escape(id)}-help" class="field-help" popover="auto" data-help-for="${escape(id)}-info" role="note"><strong>${escape(label)}</strong><p id="${escape(id)}-help-text">${escape(help)}</p></div>`;
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
function textField(name,label,value,{rows=4,hint,helpKey=name,wrapperId='',hintId=`field-${name}-hint`}={}) {
  const description=hint ?? fieldDescription(helpKey).hint;
  const control=`<textarea id="field-${escape(name)}" name="${escape(name)}" rows="${rows}" ${description?`aria-describedby="${escape(hintId)}"`:''} spellcheck="false">${escape(value ?? '')}</textarea>`;
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
function schemaField(name,value,schema,{label,hint,helpKey=name,placeholder='Use run setting'}={}) {
  const nullable=schema.anyOf?.some(item=>item.type==='null');
  const valueSchema=schema.anyOf?.find(item=>item.type!=='null') || schema;
  label ||= schema.title || human(name);
  if(valueSchema.enum)return selectField(name,label,valueSchema.enum.map(item=>`<option value="${escape(item)}" ${item===value?'selected':''}>${escape(item==='poisson'?'Uneven (Poisson)':item==='steady'?'Even spacing':human(item))}</option>`).join(''),{hint,helpKey});
  if(valueSchema.type==='array')return field(name,label,(value || []).join(', '),{hint,helpKey,required:true});
  if(valueSchema.type==='string')return field(name,label,value,{hint,helpKey,required:!nullable,placeholder});
  return field(name,label,value,{type:'number',hint,helpKey,min:valueSchema.minimum,max:valueSchema.maximum,step:valueSchema.type==='integer'?1:'any',required:!nullable,placeholder:nullable?placeholder:''});
}
function testSettingsLabel(check,config) {
  if(check.id==='buckets')return `${config.checks.includes('buckets')?'Levels':'Saved levels'}: ${(config.buckets || bucketFormDefaults(config)).levels.join(', ')}`;
  if(!check.option_fields.length&&!check.limit_fields.length){
    const key={mixed:'buckets',tools:'forced_tool_diagnostic'}[check.id] || check.id;
    if(config[key])return config.checks.includes(check.id)?'JSON settings':'JSON settings saved';
  }
  const custom=Object.keys(config.test_options?.[check.id] || {}).length>0||(check.id==='capacity'&&!sameConfig(config.traffic,state.bootstrap.config.traffic));
  return config.checks.includes(check.id)?(custom?'Custom':'Defaults'):(custom?'Custom saved':'Not selected');
}
function renderTestCard(check,config) {
  const selected=config.checks?.includes(check.id),custom=Object.keys(config.test_options?.[check.id] || {}).length>0;
  const optionLabels={samples:'Sample requests',concurrency:'Requests at a time',context_limit:'Context length (tokens)',max_output_tokens:'Answer cap (tokens)',request_timeout_seconds:'Request time limit (seconds)',sustained_seconds:'Time under load (seconds)',metrics_max_age_seconds:'Maximum metric age (seconds)',backend_verification:'Verify cancelled work on server',server_log:'Server log path',trace_file:'Trace file path',cancel_after_ms:'Cancel after (ms)',deadline_probe_ms:'Test request deadline (ms)',cancel_after_chunks:'Cancel after this many answer chunks',cleanup_seconds:'Cleanup wait (seconds)',trace_wait_seconds:'Trace wait (seconds)'};
  const fields=check.option_fields.map(name=>schemaField(`test_${check.id}_${name}`,optionValue(config,check,name),state.bootstrap.option_schema.properties[name],{label:optionLabels[name] || human(name),helpKey:`test_${name}`,hint:name==='backend_verification'?'Uses the run setting until you change it.':'Leave blank to use the run setting.'}));
  const profileKey={mixed:'buckets',tools:'tool_calling'}[check.id] || check.id;
  const advanced=!check.option_fields.length&&!check.limit_fields.length;
  const limitLabels={first_output_ms:'First output maximum (ms)',latency_ms:'Response time maximum (ms)',stream_gap_ms:'Stream pause maximum (ms)',min_output_tokens_per_second:'Minimum generation rate (tokens/second)',fairness_max_ratio:'Mixed-load slowdown (ratio)',max_error_rate:'Maximum error rate',min_correctness:'Minimum correctness',min_samples:'Minimum usable samples'};
  const limits=check.limit_fields.map(name=>schemaField(`test_${check.id}_limit_${name}`,limitValue(config,check,name),state.bootstrap.limits_schema.properties[name],{label:limitLabels[name] || human(name),helpKey:`limit_${name}`,placeholder:'No limit',hint:({max_error_rate:'Fraction: 0.05 means 5%.',min_correctness:'Fraction: 0.95 means 95%.'})[name] || (state.bootstrap.limits_schema.properties[name].anyOf?'Leave blank for no limit on this test.':'')}));
  return `<section class="test-card" data-test-card="${check.id}"><div class="test-card-heading"><div class="test-row-copy"><div class="test-title"><label class="test-selection"><input type="checkbox" name="check" value="${check.id}" ${selected?'checked':''}><span>${escape(check.title)}</span></label>${infoHelp(`check-${check.id}`,check.title,check.details || check.description)}</div><p class="hint test-description">${escape(check.description)}</p>${check.id==='capacity'?'<p class="hint traffic-profile-summary"></p>':''}</div><div class="test-row-actions"><span class="test-setting-state" data-setting-state>${escape(testSettingsLabel(check,config))}</span><button type="button" class="button small configure-test" data-configure-test="${check.id}" aria-label="Configure ${escape(check.title)}" aria-expanded="false" aria-controls="settings-${check.id}">Configure</button></div></div><input type="hidden" name="test_mode_${check.id}" value="${custom?'custom':'default'}"><div class="test-editor" id="settings-${check.id}" data-test-editor="${check.id}" hidden><p class="hint draft-notice" ${selected?'hidden':''}>Not selected. These settings will be used if you select this test.</p>${check.id==='capacity'?renderTrafficEditor(config):''}<div class="test-settings" data-test-settings="${check.id}"><div class="test-editor-heading"><h3>${check.id==='capacity'?'Acceptance settings':'Test settings'}</h3>${advanced?'':`<button type="button" class="text-button reset-test" data-reset-test="${check.id}">${check.id==='capacity'?'Reset acceptance settings':'Reset to defaults'}</button>`}</div><fieldset class="fields">${fields.join('')}${limits.join('')}</fieldset>${check.id==='buckets'?renderBucketEditor(config):advanced?`<p class="hint">Uses the shared run limits. Edit <code>${escape(profileKey)}</code>${check.id==='tools'?' and forced_tool_diagnostic':''} in JSON for more options.</p><button type="button" class="text-button" data-config-tab="json">Edit in JSON</button>`:''}</div>${check.id==='gpu'?`<div class="metrics-field">${field('metrics_url','Metrics URL for first target',config.targets[0].metrics_url,{type:'url',placeholder:'http://localhost:9400/metrics'})}</div>`:''}<div class="test-editor-footer"><span class="hint">Changes are kept as you edit.</span><button type="button" class="button small" data-done-test="${check.id}">Done</button></div></div></section>`;
}
function bucketFormDefaults(config) {
  const defaults=clone(state.bootstrap.buckets.defaults);
  defaults.levels=defaults.levels.filter(level=>level<=config.concurrency);
  defaults.output_tokens=Object.fromEntries(Object.entries(defaults.output_tokens).map(([name,value])=>[name,Math.min(value,config.max_output_tokens)]));
  return defaults;
}
function renderBucketEditor(config) {
  const settings=config.buckets || bucketFormDefaults(config),schema=state.bootstrap.buckets.schema;
  return `<fieldset class="fields" data-bucket-settings>${field('buckets_levels','Concurrency levels',settings.levels.join(', '),{required:true,full:true,placeholder:'1, 2, 4, 8',hint:'For example: 1, 4, 8 tests one request at a time, then four, then eight.'})}${schemaField('buckets_samples_per_bucket',settings.samples_per_bucket,schema.properties.samples_per_bucket,{label:'Minimum requests per test',hint:'Testing 8 at a time always sends at least 8 requests.'})}${schemaField('buckets_window_seconds',settings.window_seconds,schema.properties.window_seconds,{label:'Time limit per test (seconds)',hint:'Each short, medium or long test has its own time limit.'})}</fieldset><p class="hint bucket-profile-summary"></p><p class="hint">Input/output sizes and mixed-workload settings are available in JSON under <code>buckets</code>.</p><button type="button" class="text-button" data-config-tab="json">Edit sizes in JSON</button>`;
}
function renderLoadGuidance(config) {
  const plan=bucketCoveragePlan(config),messages=[];
  const batchBudget=config.concurrency*config.targets.length;
  let budgetNeeded=batchBudget;
  if(plan){
    messages.push(`<p class="hint">Test ${escape(plan.levels.join(' → '))} requests at a time: ${number(plan.perTarget)} requests per target, ${number(plan.total)} total. Each level runs 9 tests using short, medium and long questions and answers. Time limits can stop a test early.</p>`);
    budgetNeeded=Math.max(budgetNeeded,plan.total);
    if(plan.peak>config.concurrency)messages.push(notice(`The highest tested level (${plan.peak}) exceeds Maximum in-flight requests (${config.concurrency}). Raise the maximum or lower the tested levels.`,'warning'));
    if(config.max_requests<plan.total)messages.push(notice(`This test needs ${number(plan.total)} requests, but your total request limit is ${number(config.max_requests)}. Increase the limit to run all selected levels.`,'warning'));
    else if(config.checks.length>1)messages.push('<p class="hint">Other selected tests share the total request limit and may leave fewer requests for bucket coverage.</p>');
  }
  if(config.max_requests<batchBudget)messages.push(notice(`The total request limit gives at least one target fewer than ${number(config.concurrency)} requests. That target cannot reach the maximum in-flight setting.`,'warning'));
  if(config.max_requests<budgetNeeded)messages.push(`<button type="button" class="button small" data-action="use-request-budget" data-budget="${budgetNeeded}">Use ${number(budgetNeeded)} total requests</button><p class="hint">${plan?'Enough for this test; other selected tests may need more requests.':'Allows one full batch per target at the maximum in-flight setting; individual tests may use lower levels.'} This changes your total traffic limit.</p>`);
  return messages.join('');
}
function renderTrafficEditor(config) {
  const traffic=config.traffic || state.bootstrap.config.traffic,custom=!sameConfig(traffic,state.bootstrap.config.traffic),schema=state.bootstrap.traffic_schema;
  const labels={rates:'Request rates (per second)',duration_seconds:'Seconds per rate',arrival:'Arrival pattern',max_in_flight:'Requests at a time',seed:'Random seed',long_input_chars:'Long question size (characters)',long_output_words:'Long answer size (words)',drain_timeout_seconds:'Completion wait (seconds)',scheduler_lag_tolerance_ms:'Allowed start delay (ms)'};
  const controls=Object.entries(schema.properties).filter(([name])=>name!=='mix').map(([name,fieldSchema])=>schemaField(`traffic_${name}`,traffic[name],fieldSchema,{label:labels[name] || human(name)}));
  const mix=schema.$defs.TrafficMix;
  return `<div class="traffic-editor"><input type="hidden" name="traffic_mode" value="${custom?'custom':'default'}"><div class="test-editor-heading"><h3>Traffic profile</h3><button class="text-button reset-test" type="button" data-action="reset-traffic">Reset traffic profile</button></div><p class="hint">New requests start at the chosen rate. If all request slots are busy, extra arrivals count as dropped.</p><div data-traffic-settings><fieldset class="fields">${controls.join('')}<div class="full traffic-mix-heading"><h4>Request mix</h4><p class="hint">Choose how often to use each request type. Higher weights mean a larger share of traffic.</p></div>${Object.entries(mix.properties).map(([name,s])=>schemaField(`traffic_mix_${name}`,traffic.mix[name],s,{label:({short:'Short requests',long_input:'Long questions',long_output:'Long answers'})[name]})).join('')}</fieldset></div></div>`;
}
function syncTestControls() {
  syncPackControls();
  const form=$('#new-run-form');if(!form)return;
  let selectedCount=0;
  for(const check of state.bootstrap.checks){
    const card=$(`[data-test-card="${check.id}"]`),selected=$('input[name="check"]',card).checked;
    if(selected)selectedCount++;
    const open=state.openTest===check.id;
    card.classList.toggle('test-disabled',!selected);
    card.hidden=!selected&&!state.showAvailableTests&&!open;
    const editor=$('[data-test-editor]',card);editor.hidden=!open;
    $('[data-configure-test]',card).setAttribute('aria-expanded',String(open));
    $('.draft-notice',card).hidden=selected;
    for(const fieldset of $$('fieldset',editor))fieldset.disabled=!open;
    if(check.id==='capacity')$('.traffic-profile-summary',card).hidden=!selected;
    if(check.id==='gpu')form.elements.metrics_url.disabled=!open;
  }
  const toggle=$('#add-tests');
  if(toggle){toggle.textContent=state.showAvailableTests?'Hide unselected tests':`Add tests (${state.bootstrap.checks.length-selectedCount})`;toggle.setAttribute('aria-expanded',String(state.showAvailableTests));}
  const empty=$('#no-tests-selected');if(empty)empty.hidden=selectedCount>0;
}
function toggleTestEditor(id,close=false) {
  state.openTest=close||state.openTest===id?null:id;
  syncTestControls();updateBounds();
  if(close)($(`[data-test-card="${id}"]`).hidden?$('#add-tests'):$(`[data-configure-test="${id}"]`))?.focus();
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
    if(packSettingInactive(input.name))continue;
    const card=input.closest('[data-test-card]');
    if(card){
      const id=card.dataset.testCard;
      if(!$('input[name="check"]',card).checked)continue;
      if(input.closest('[data-test-settings]')&&!input.closest('[data-bucket-settings]')&&form.elements[`test_mode_${id}`].value!=='custom')continue;
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
  return `<div class="judging-editor"><div class="test-editor-heading"><h3>AI evaluator</h3><button type="button" class="text-button" data-action="add-judge" ${settings.judges.length>=8?'disabled':''}>Add evaluator</button></div><p class="hint">An AI model compares each answer with your expected answer and accepts correct paraphrases.</p><input type="hidden" name="judge_count" value="${settings.judges.length}">${settings.judges.map((judge,index)=>`<fieldset class="judge-row"><legend>Evaluator ${index+1}</legend><div class="fields">${field(`judge_${index}_model`,'Model',judge.model)}${field(`judge_${index}_url`,'Endpoint URL',judge.url,{type:'url',full:true,placeholder:'http://localhost:9000/v1'})}</div><details class="evaluator-options"><summary>Credentials & evaluator name</summary><div class="fields">${field(`judge_${index}_name`,'Name',judge.name)}${field(`judge_${index}_api_key_env`,'API key environment variable (optional)',judge.api_key_env,{full:true,placeholder:'JUDGE_API_KEY'})}</div></details><button type="button" class="text-button" data-action="remove-judge" data-judge-index="${index}" aria-label="Remove evaluator ${index+1}">Remove</button></fieldset>`).join('')}<details class="judge-limits"><summary>Evaluation limits</summary><div class="fields">${field('judging_concurrency','Evaluator requests at a time',settings.concurrency,{type:'number',min:1,max:256,step:1})}${field('judging_request_timeout_seconds','Evaluator request limit (seconds)',settings.request_timeout_seconds,{type:'number',min:0,step:'any'})}${field('judging_max_duration_seconds','Total evaluation time (seconds)',settings.max_duration_seconds,{type:'number',min:0,step:'any'})}${field('judging_max_output_tokens','Evaluator answer cap (tokens)',settings.max_output_tokens,{type:'number',min:1,step:1})}</div><p class="hint">More connection and authentication settings are available in JSON.</p></details><p class="hint judge-disclosure">Questions, expected answers and model answers are sent to your configured evaluators. With several evaluators, more than half must agree. A tie or missing vote leaves the result inconclusive.</p></div>`;
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
  return `<section class="setup-section" id="setup-dataset">${setupSectionHeader(2,'Add questions & expected answers','Try one question to inspect the answer before testing them all.')}<div class="setup-body"><div class="request-source"><fieldset class="dataset-source"><legend class="sr-only">Questions to test</legend><label><input type="radio" name="request_source" value="request_pack" ${custom?'checked':''}><span>My questions</span></label><label><input type="radio" name="request_source" value="builtin" ${custom?'':'checked'}><span>Built-in examples</span></label></fieldset>${custom?`<input type="file" id="request-pack-file" accept=".csv,.json,text/csv,application/json" hidden><div class="pack-actions actions"><button type="button" class="button small" data-action="add-example">+ Add question</button><button type="button" class="button small" data-action="import-pack">Import CSV / JSON</button><button type="button" class="text-button" data-action="sample-dataset">Use sample questions</button></div>${cases.length?`<div class="dataset-table question-table"><table><thead><tr><th><input type="checkbox" id="dataset-select-all" aria-label="Select all examples" ${cases.every(item=>state.datasetSelection.includes(item.fixture_id))?'checked':''}></th><th>Question / expected answer</th><th>Answer check</th><th>Actions</th></tr></thead><tbody>${cases.map((item,index)=>`<tr><td><input type="checkbox" data-dataset-select="${escape(item.fixture_id)}" aria-label="Select ${escape(item.fixture_id)}" ${state.datasetSelection.includes(item.fixture_id)?'checked':''}></td><td><strong>${escape(item.fixture_id)}</strong><span class="dataset-excerpt">${escape(item.messages.map(message=>message.content).join(' / '))}</span><small class="dataset-excerpt">Expected: ${Object.hasOwn(item,'expected')?escape(typeof item.expected==='string'?item.expected:JSON.stringify(item.expected)):'Not set'}${item.messages.length>1?` · ${number(item.messages.length)} messages`:''}</small></td><td>${answerChecks[item.scorer]}${item.scorer==='judge'?`<small data-evaluator-needed ${judgesReady(config)?'hidden':''}>Needs evaluator</small>`:''}</td><td><div class="dataset-row-actions"><button type="button" class="text-button" data-action="try-example" data-case-index="${index}" aria-label="Try ${escape(item.fixture_id)}">Try one</button><button type="button" class="text-button" data-action="edit-example" data-case-index="${index}" aria-label="Edit ${escape(item.fixture_id)}">Edit</button><button type="button" class="text-button" data-action="remove-example" data-case-index="${index}" aria-label="Remove ${escape(item.fixture_id)}">Remove</button></div></td></tr>`).join('')}</tbody></table></div><p class="hint dataset-count">${number(cases.length)} questions</p>`:'<div class="dataset-empty"><h3>Add your first question</h3><p class="hint">Enter a question and its expected answer, or use the sample questions to get started.</p></div>'}`:`<p class="hint">Known-answer questions are ready to test.${pack?` Your ${number(cases.length)} questions are saved for later.`:''}</p>`}</div>${renderAnswerChecks(config)}${custom&&cases.length?`<details class="setup-disclosure dataset-advanced"><summary>Advanced dataset settings</summary><p class="hint">${inputRetentionNotice}</p><div id="request-pack-preview"></div></details>`:''}</div></section>`;
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
function renderRequestPreview() {
  const element=$('#request-pack-preview');if(!element)return;
  if(state.config?.request_source!=='request_pack'){element.innerHTML='';return;}
  const expanded=element.querySelector?.('.request-planning')?.open || false;
  const saved=state.requestPreview;
  const status=saved && saved.revision===state.configRevision && saved.route===state.routeVersion?saved.status:'stale';
  const preview=saved?.data;
  const cases=preview?.cases?.length?preview.cases:(saved?.config?.request_pack?.cases || []).map(item=>({...item,input_chars:item.messages.reduce((total,message)=>total+message.content.length,0),requested:{max_tokens:item.max_tokens,options:item.options},effective:null}));
  const labels={pending:'Updating preview…',current:'Preview current',stale:'Preview stale · refresh to see the current plan',error:'Preview failed · current plan is unconfirmed'};
  element.innerHTML=`<details class="request-planning" ${expanded?'open':''}><summary>Request planning details <span class="dim">· ${labels[status]}</span></summary><div class="pack-preview-heading"><p class="hint" role="status">${labels[status]}</p><button class="text-button" type="button" data-action="preview-pack" ${status==='pending'?'disabled':''}>Refresh preview</button></div>${preview?`<p class="hint">${preview.active?`Consumers: ${preview.consumers.map(escape).join(', ')} · ${number(preview.quiet_requests)} quiet requests`:'Pack inactive. Select correctness or capacity to exercise it.'}</p>${!preview.active?'<button class="text-button" type="button" data-action="select-pack-tests">Choose tests</button>':''}<p class="hint">Pack requests per target: ${number(preview.planned_requests_per_target)} · run request cap: ${number(preview.request_cap)} · target quotas: ${Object.entries(preview.per_target_request_quota || {}).map(([key,value])=>`${escape(key)} ${number(value)}`).join(', ')}</p><p class="hint">${number(preview.unique_effective_requests)} unique effective requests · ${number(preview.repeated_requests_per_target)} repeats per target.</p>${renderJudgingPreview(preview.judging)}${(preview.warnings || []).filter(w=>w!=='AI judging runs after target traffic. Messages, golden answers and generated answers are sent to every configured judge.').map(w=>notice(w,'warning')).join('')}<details class="pack-preview-details"><summary>Effective request settings and stage allocations${status!=='current'?' · previous preview':''}</summary><div class="pack-preview-scroll">${cases.map(item=>{const source=saved.config?.request_pack?.cases.find(c=>c.fixture_id===item.fixture_id);return `<article class="pack-case"><h3>${escape(item.fixture_id)}</h3><p class="hint">${number(item.input_chars)} input characters · weight ${number(item.weight,4)} · ${escape(item.scorer)}</p><p class="hint">Output cap: requested ${item.requested?.max_tokens==null?'inherited':number(item.requested.max_tokens)} → effective ${item.effective?`${number(item.effective.max_tokens)} tokens · ${item.effective.stream?'streaming':'non-streaming'} · deadline ${elapsed(item.effective.request_timeout_seconds)}`:'inactive; select a consumer to calculate settings'}</p><details><summary>Assertion and request options</summary><pre>${escape(JSON.stringify({expected:source?.expected,rubric:source?.rubric,schema:source?.schema,requested_options:item.requested?.options,effective_options:item.effective?.options},null,2))}</pre></details></article>`;}).join('')}${(preview.stages || []).map(stage=>`<article class="pack-case"><h3>${number(stage.rate_rps,2)} requests/s · ${number(stage.planned)} planned</h3><p class="hint">${Object.entries(stage.counts || {}).map(([key,value])=>`${escape(key)}: ${number(value)}`).join(' · ')}</p></article>`).join('')}</div></details>`:''}</details>${status==='error'?notice(saved.error,'error'):''}`;
}
async function refreshRequestPreview(snapshot) {
  assertAppliedConfig();
  const version=state.routeVersion,revision=state.configRevision,sequence=++state.previewSequence;
  const config=clone(snapshot || readForm(true).config);
  state.requestPreview={...state.requestPreview,status:'pending',revision,route:version};renderRequestPreview();
  try{
    const result=await api('/api/config/preview',{method:'POST',body:JSON.stringify(config)});
    if(version!==state.routeVersion||revision!==state.configRevision||sequence!==state.previewSequence)throw new Error('Configuration changed during preview. Refresh the current settings before running.');
    assertAppliedConfig();
    state.requestPreview={status:'current',revision,route:version,data:result.request_pack_preview,config:result.config};renderRequestPreview();
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
function setupSectionHeader(number,title,description) {
  return `<header class="setup-heading"><span class="setup-number" aria-hidden="true">${number}</span><div><h2>${escape(title)}</h2><p>${escape(description)}</p></div></header>`;
}
function renderNew() {
  state.openTest=null;
  const c=state.config,target=c.targets?.[0] || {},limits=c.limits || {};
  $('#main').innerHTML=`<div class="page-heading"><div><div class="eyebrow">Deployment acceptance</div><h1>New run</h1><p>Connect a deployment, choose your tests, then review and run.</p></div><a class="button ghost" href="#runs">Back to runs</a></div>
  <div class="notice" id="new-active-notice" ${state.active?'':'hidden'}><strong>A run is already active.</strong> <a href="${state.active?linkRun(state.active):'#runs'}">Inspect or stop it</a> before starting another.</div>
  <form id="new-run-form" novalidate><div class="config-toolbar"><div class="result-tabs config-tabs" role="tablist" aria-label="Configuration view"><button type="button" role="tab" id="config-tab-form" data-config-tab="form" aria-controls="config-panel-form">Form</button><button type="button" role="tab" id="config-tab-json" data-config-tab="json" aria-controls="config-panel-json">JSON</button></div><div class="actions"><button type="button" class="text-button" data-action="import-config">Import run config</button><button type="button" class="text-button" data-action="export-config">Export config</button><button type="button" class="text-button" data-action="copy-cli">Copy CLI</button></div><input type="file" id="config-file" accept=".json,application/json" hidden></div><div class="notice config-draft-notice" id="config-draft-notice" hidden><div><strong>Unapplied JSON changes.</strong> Apply or discard them before editing the form, running, exporting, or copying the CLI command.</div><div class="actions"><button class="button small" type="button" data-action="apply-config">Apply changes</button><button class="text-button" type="button" data-action="discard-config">Discard changes</button></div></div><div id="config-error" class="error-message config-action-error" role="alert" hidden></div><div class="panel cli-fallback" id="cli-fallback" hidden><label for="cli-command">Clipboard unavailable. Copy this command:</label><textarea id="cli-command" readonly spellcheck="false"></textarea></div><div class="new-run-layout"><div id="config-panel-form" role="tabpanel" aria-labelledby="config-tab-form">
  <section class="setup-section" id="setup-deployment">${setupSectionHeader(1,'Deployment','Where should Giraffe send requests?')}<div class="setup-body"><div class="fields">
  ${field('url','Endpoint URL',target.url,{type:'url',required:true,placeholder:'http://localhost:11434/v1'})}
  ${field('model','Model',target.model,{required:true,placeholder:'e.g. qwen2.5:7b-instruct'})}</div>
  ${(c.targets || []).length>1?`<div class="notice multi-target-notice"><strong>${c.targets.length} targets configured.</strong> These fields edit the first target. Other targets are preserved and editable in JSON.</div>`:''}
  <details class="setup-disclosure" id="execution-settings"><summary>Target and execution options</summary><div class="details-content"><div class="fields">${field('name','Target name',target.name,{required:true,placeholder:'local'})}${selectField('mode','Execution mode','<option value="normal">Use running server</option><option value="cold-start">Restart one server first</option>')}<div id="restart-controls" hidden style="margin-top:16px">${selectField('restart_target','Target to restart',(c.targets || []).map(t=>`<option value="${escape(t.name)}">${escape(t.name)}</option>`).join(''))}<p class="hint" id="restart-hook" style="margin-top:12px"></p><p class="hint" style="margin-top:8px">Only this named target's configured restart command will run.</p></div></div></div></details></div></section>
  ${renderRequestSource(c)}
  <section class="setup-section" id="test-selection">${setupSectionHeader(3,'Tests & traffic','Choose what to measure and the limits this deployment must meet.')}<div class="setup-body">
  <div class="test-suite-heading"><div><h3>Selected tests</h3><p class="hint" id="selection-summary"></p></div><div class="actions"><button class="button small" id="add-tests" type="button" data-action="toggle-available-tests" aria-expanded="false" aria-controls="test-cards">Add tests</button><button class="button small ghost" type="button" data-action="select-tests">Select all</button><button class="button small ghost" type="button" data-action="clear-tests">Clear</button></div></div>
  <p class="hint" id="no-tests-selected" hidden>No tests selected. Use Add tests to choose what to run.</p>
  <div class="test-cards" id="test-cards">${[...(state.bootstrap.checks || [])].sort((a,b)=>Number(c.checks.includes(b.id))-Number(c.checks.includes(a.id))).map(check=>renderTestCard(check,c)).join('')}</div>
  <details class="setup-disclosure" open><summary>Shared run limits <span class="dim" id="run-ceilings-summary"></span></summary><div class="details-content"><p class="hint">Maximum in-flight requests controls how many requests run together. Total request limit controls how many requests the whole run can send. Changing one does not change the other.</p><div class="fields three">${numericFields.map(([key,label,unit,min,step])=>field(key,label,c[key],{type:'number',hint:unit,min,max:key==='concurrency'?256:undefined,step,required:true})).join('')}</div></div></details>
  <details class="setup-disclosure"><summary>Acceptance targets <span class="dim" id="acceptance-summary"></span></summary><div class="details-content"><p class="hint">Set what counts as a pass. Leave a performance limit blank to measure it without requiring a particular result.</p><div class="fields">${limitFields.map(([key,label,unit])=>field(`limit_${key}`,label,limits[key],{type:'number',hint:unit,min:key==='fairness_max_ratio'?1.001:.001,step:'any',placeholder:'Not set'})).join('')}${field('limit_max_error_rate','Maximum error rate',limits.max_error_rate,{type:'number',hint:'Fraction: 0 to 1',min:0,step:'any'})}${field('limit_min_correctness','Minimum correctness',limits.min_correctness,{type:'number',hint:'Fraction: 0 to 1',min:0,step:'any'})}${field('limit_regression_percent','Material regression threshold',limits.regression_percent,{type:'number',hint:'Percent; percentage points for rates',min:0,step:'any'})}</div></div></details>
</div></section></div>
<section class="panel form-section" id="config-panel-json" role="tabpanel" aria-labelledby="config-tab-json" hidden><h2>Run configuration</h2><p class="hint">Edit run settings and extra options here. Apply changes before returning to the form or starting a run.</p><p class="hint">${inputRetentionNotice}</p>${renderField('config_editor','Configuration · JSON',`<textarea class="config-editor" id="config-editor" spellcheck="false">${escape(state.jsonDraft ?? JSON.stringify(c,null,2))}</textarea>`,{controlId:'config-editor'})}<div class="editor-actions"><button class="button small" type="button" data-action="apply-config">Apply changes</button><span class="hint" id="json-status" role="status"></span></div></section>

  <section class="setup-section" id="setup-review">${setupSectionHeader(4,'Review & run','Check the scope and any missing settings before sending traffic.')}<div class="setup-body">
  <div class="review-baseline">${selectField('baseline_id','Compare against',baselineOptions(state.prefillBaseline || ''))}<p class="hint">Compare results with a saved run to see what got better or worse.</p></div>
  <dl class="bound-list review-bounds" id="run-bounds"></dl><div id="load-guidance" role="status" aria-live="polite"></div></div></section>
  <div class="run-action-bar"><div><strong id="run-bar-summary"></strong><p class="hint">Sends requests to your configured deployment. You can stop at any time.</p></div><button class="button primary" type="submit" id="start-run" ${state.active?'disabled':''}>Run suite ${icon('arrow')}</button><div id="form-error" class="error-message" role="alert" hidden></div></div>
  </div></form>`;
  for(const check of state.bootstrap.checks){
    const options=c.test_options?.[check.id] || {};
    for(const name of check.option_fields)$('#new-run-form').elements[`test_${check.id}_${name}`].dataset.inherited=String(options[name]==null);
    for(const name of check.limit_fields)$('#new-run-form').elements[`test_${check.id}_limit_${name}`].dataset.inherited=String(!Object.hasOwn(options.limits || {},name));
  }
  const form=$('#new-run-form');form.elements.mode.value=state.formMode;
  if(state.formRestartTarget&&c.targets.some(t=>t.name===state.formRestartTarget))form.elements.restart_target.value=state.formRestartTarget;
  if(state.jsonDraft===null){state.jsonDraft=JSON.stringify(portableConfig(readForm(true)),null,2);state.jsonBase=state.jsonDraft;$('#config-editor').value=state.jsonDraft;}
  syncTestControls();updateBounds();updateRestart();syncConfigView();syncPackControls();renderRequestPreview();
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
  config=configureBuckets(config,data,bucketFormDefaults(config),{allowInvalid:allowEmpty});
  config=configureJudging(config,data,{allowInvalid:allowEmpty});
  config.restart_target=null;
  return {config,baseline_id:data.get('baseline_id') || null,mode:data.get('mode') || 'normal',restart_target:data.get('mode')==='cold-start'?data.get('restart_target'):null};
}
function updateBounds() {
  const selected=$$('#new-run-form input[name="check"]:checked').length;
  $('#selection-summary').textContent=`${selected} of ${state.bootstrap.checks.length} tests selected`;
  let payload;try{payload=readForm(true);}catch{return;}
  const c=payload.config;
  $('#load-guidance').innerHTML=renderLoadGuidance(c);
  const bucketSummary=$('.bucket-profile-summary');
  if(bucketSummary){const plan=bucketCoveragePlan({...c,checks:['buckets']});bucketSummary.textContent=plan?`${number(plan.perTarget)} requests per target. Each level runs 9 tests using short, medium and long questions and answers.`:'Enter the requests to run at a time and the minimum requests per test to see the total.';}
  updateDatasetReadiness(c);
  for(const check of state.bootstrap.checks){
    const card=$(`[data-test-card="${check.id}"]`);
    $('[data-setting-state]',card).textContent=testSettingsLabel(check,c);
  }
  $('#run-bounds').innerHTML=[['Targets',number(c.targets.length)],['Maximum in flight',`≤ ${number(c.concurrency)}`],['Total request limit',`≤ ${number(c.max_requests)}`],['Run duration',`≤ ${elapsed(c.max_duration_seconds)}`],['Request deadline',elapsed(c.request_timeout_seconds)],['Output cap',`${number(c.max_output_tokens)} tokens`],['Mode',payload.mode==='normal'?'Existing service':'Explicit restart']].map(([k,v])=>`<div><dt>${escape(k)}</dt><dd>${escape(v)}</dd></div>`).join('');
  $('#selection-summary').textContent=`${c.checks.length} of ${state.bootstrap.checks.length} tests selected`;
  $('#run-bounds').insertAdjacentHTML('afterbegin',`<div><dt>Selected tests</dt><dd>${c.checks.length}</dd></div>`);
  const missing=[];
  if(c.targets.some(target=>!target.model?.trim()))missing.push('Enter target model');
  if(c.targets.some(target=>!target.url?.trim()))missing.push('Enter endpoint URL');
  if(!c.checks.length)missing.push('Select at least one test');
  if(c.request_source==='request_pack'){
    const cases=c.request_pack?.cases || [],active=c.checks.some(id=>['capacity','correctness'].includes(id));
    const limits={...c.limits,...c.test_options?.[c.checks.includes('capacity')?'capacity':'correctness']?.limits};
    if(!cases.length)missing.push('Add dataset examples');
    if(!active)missing.push('Select correctness or capacity');
    const judged=active&&cases.some(item=>item.scorer==='judge'),judges=c.judging?.judges || [];
    if(judged&&!judgesReady(c))missing.push('Configure AI evaluator');
    const rows=[['Dataset',`${cases.length} examples`],['Required correct',`${number((limits.min_correctness ?? 1)*100,1)}%`],['Response limit',limits.latency_ms==null?'Not set':ms(limits.latency_ms)]];
    if(c.checks.includes('capacity'))rows.push(['Arrival rates',`${c.traffic.rates.join(' → ')} req/s`]);
    if(judged&&judgesReady(c))rows.push(['Evaluator calls',`≤ ${number(c.max_requests*judges.length)} (budget bound)`]);
    $('#run-bounds').insertAdjacentHTML('beforeend',rows.map(([label,value])=>`<div><dt>${escape(label)}</dt><dd>${escape(value)}</dd></div>`).join(''));
  }
  if(missing.length)$('#run-bounds').insertAdjacentHTML('beforeend',`<div class="setup-needed"><dt>Setup needed</dt><dd>${escape(missing.join(' · '))}</dd></div>`);
  const summary=$('#run-bar-summary');if(summary)summary.textContent=`${c.checks.length} tests · ${c.targets.length} target${c.targets.length===1?'':'s'} · up to ${number(c.max_requests)} requests`;
  const ceilings=$('#run-ceilings-summary');if(ceilings)ceilings.textContent=`· ${number(c.max_requests)} requests / ${elapsed(c.max_duration_seconds)}`;
  const acceptance=$('#acceptance-summary');if(acceptance)acceptance.textContent=`· ${number((c.limits.min_correctness ?? 1)*100,1)}% correct · ${c.limits.latency_ms==null?'response time not set':ms(c.limits.latency_ms)}`;
  const traffic=c.traffic,profile=$('.traffic-profile-summary');
  if(profile)profile.textContent=`${traffic.rates.join(' → ')} requests/s · ${traffic.duration_seconds}s per stage · ${traffic.arrival==='poisson'?'uneven':'steady'} arrivals · up to ${traffic.max_in_flight ?? c.concurrency} in flight`;
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
  details.open=true;state.judgeEditorOpen=true;
  if(!state.config.judging?.judges?.length)changeJudge('add');
  else $('[name="judge_0_url"]')?.focus();
}
function updateRestart(){const form=$('#new-run-form');if(!form)return;const mode=form.elements.mode.value;$('#restart-controls').hidden=mode!=='cold-start';const name=form.elements.restart_target.value;const target=state.config.targets.find(t=>t.name===name);$('#restart-hook').textContent=target?.restart_command?.length?`Configured command: ${target.restart_command.join(' ')}`:'No restart command is configured for this target. Add restart_command in the configuration before using cold start.';updateBounds();}
async function startRun(event) {
  event.preventDefault();if(state.submitting||state.active||state.configBusy)return;
  const version=state.routeVersion;$('#form-error').hidden=true;
  try {
    assertAppliedConfig();
    if(!validateRunForm(event.target))return;
    const draft=readForm(true).config;
    if(draft.request_source==='request_pack'&&draft.checks.some(id=>['correctness','capacity'].includes(id))){
      if(!draft.request_pack?.cases?.length){$('[data-action="add-example"]').focus();throw new Error('Add a question or choose built-in examples before testing.');}
      if(draft.request_pack.cases.some(item=>item.scorer==='judge')&&!judgesReady(draft)){configureEvaluator();throw new Error('Set up the AI evaluator to check questions that accept paraphrased answers.');}
    }
    state.submitting=true;$('#start-run').disabled=true;$('#start-run').textContent='Validating settings…';
    const payload=await validatedSnapshot();
    await refreshRequestPreview(payload.config);
    if(payload.mode==='cold-start'&&!payload.config.targets.find(t=>t.name===payload.restart_target)?.restart_command?.length)throw new Error('Cold start requires a configured restart_command for the selected target.');
    if(version!==state.routeVersion)return;
    $('#start-run').textContent='Starting suite…';
    const result=await api('/api/runs',{method:'POST',body:JSON.stringify(payload)});state.config=clone(payload.config);setActive(result.id);if(version===state.routeVersion)location.hash=linkRun(result.id);
  }catch(error){if(version!==state.routeVersion){toast(`Run was not started: ${error.message}`);return;}const errorEl=$('#form-error');errorEl.textContent=error.message;errorEl.hidden=false;if($('#start-run')){$('#start-run').disabled=Boolean(state.active);$('#start-run').innerHTML=`Run suite ${icon('arrow')}`;}}finally{state.submitting=false;}
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
function keyMetric(check){if(check.status==='skipped')return 'Not selected';const m=check.metrics || {};if(check.id==='cancellation'&&m.cancellation_results)return `Server stopped: ${human(m.cancellation_results.server_stopped.status)} · Answer: ${human(m.cancellation_results.answer.status)}`;if(['context','correctness','json'].includes(check.id)&&m.scored)return `${m.correct} / ${m.scored} correct`;if(check.id==='capacity'&&Object.hasOwn(m,'highest_acceptable_rate_rps'))return m.highest_acceptable_rate_rps==null?'Arrival capacity unconfirmed':`${number(m.highest_acceptable_rate_rps,2)} requests/s ${m.request_pack?.control_status==='fail'?'loaded · quiet failed':'accepted'}`;if(check.id==='capacity'&&m.highest_tested_acceptable_concurrency!=null)return `Concurrency ${m.highest_tested_acceptable_concurrency} accepted`;if(check.id==='first_output')return `${ms(m.first_output_p95_ms)} p95 · ${m.attempted || 0} requests`;if(check.id==='generation')return `${ms(m.p95_ms)} p95 · ${m.attempted || 0} requests`;if(check.status==='skipped')return 'Not selected';return m.attempted!=null?`${number(m.attempted)} requests · ${check.target}`:check.target;}
function renderChecks(report) {
  const checks=report.checks.filter(c=>c.id!=='baseline').sort((a,b)=>(order[a.status]??5)-(order[b.status]??5));
  if(!checks.length)return notice('This report contains no check results.');
  let selected=checks.find(c=>`${c.target}:${c.id}`===state.selectedCheck) || checks[0];state.selectedCheck=`${selected.target}:${selected.id}`;
  return `<div class="result-grid"><div class="panel check-list" aria-label="Suite checks">${checks.map(c=>`<button class="check-item ${c===selected?'selected':''}" data-check="${escape(`${c.target}:${c.id}`)}" aria-pressed="${c===selected}"><span><span class="check-name">${escape(c.title)}</span><small>${escape(keyMetric(c))}${(reportConfig(report).targets || []).length>1?` · ${escape(c.target)}`:''}</small></span>${badge(c.status)}</button>`).join('')}</div><section class="panel check-detail">${renderCheck(selected,report)}</section></div>`;
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
  return `<div class="check-detail-head"><div class="check-detail-heading"><h2>${escape(check.title)}</h2>${badge(check.status)}</div><p>${escape(check.summary)}</p>${!check.required?'<p class="hint">Optional check</p>':''}</div>
  ${check.id==='cancellation'&&check.status!=='skipped'?renderCancellationResults(m):''}
  ${check.status==='skipped'?'<div class="empty-state" style="padding:35px 24px"><p>This optional check was not selected for the run. No result is inferred.</p></div>':`<dl class="metrics-grid">${metrics.join('')}</dl>`}
  ${check.id==='cancellation'?`<div class="notice" style="margin:20px 24px 0">${escape(m.backend_cancellation?.summary || 'Backend cancellation was not verified.')} Physical device memory reclamation is not measured.</div>`:''}
  ${check.id==='recovery'?`<div class="notice" style="margin:20px 24px 0">Sustained traffic was configured for ${number(config.sustained_seconds,1)} seconds. Results describe this bounded observation period.</div>`:''}
  ${m.request_pack?renderPackResults(m.request_pack):''}
  ${check.id==='capacity'&&m.traffic_stages?renderTrafficStages(m.traffic_stages,m.request_pack):''}
  ${check.id==='capacity'&&m.levels?`<div class="metric-table"><table><thead><tr><th>Load</th><th>Achieved</th><th>Correct</th><th>p95</th></tr></thead><tbody>${Object.entries(m.levels).map(([k,v])=>`<tr><td class="mono">${escape(k)}</td><td>${number(v.achieved_concurrency)}</td><td>${v.correct ?? 0} / ${v.scored ?? 0}</td><td class="mono">${ms(v.p95_ms)}</td></tr>`).join('')}</tbody></table></div>`:''}
  ${check.id==='gpu'&&m.snapshots?renderGpuSnapshots(m.snapshots):''}
  ${records.length?`<div class="evidence-title"><span>Request evidence</span><span class="hint">${records.length} recorded · answer failures first</span></div><div class="evidence-table"><table><thead><tr><th>Fixture / scenario</th><th>Response</th><th>Answer</th><th>Time</th></tr></thead><tbody>${sorted.map(r=>`<tr><td><button class="text-button" data-request="${escape(r.id)}">${escape(r.fixture_id)}</button><small>${escape(r.scenario)}</small></td><td>${escape(r.status)}${isProbe(r)?'<small class="request-probe">Deliberate probe</small>':''}</td><td>${r.score===false?'<span class="status fail">Incorrect</span>':r.score===true?'<span class="status pass">Correct</span>':'<span class="dim">Unscored</span>'}</td><td class="mono nowrap">${ms(r.elapsed_ms)}</td></tr>`).join('')}</tbody></table></div>`:check.status==='skipped'?'':'<div class="check-observations hint">No request evidence was recorded for this check.</div>'}
  ${Object.keys(extras).length?`<details class="check-observations"><summary>Additional measurements and coverage</summary><pre>${escape(JSON.stringify(extras,null,2))}</pre></details>`:''}`;
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
  const judging=progress.phase==='judging'||String(progress.event || '').startsWith('judging_');
  const judgeDone=progress.judge_calls_completed || 0,judgeTotal=progress.judge_calls_total || 0;
  const title=judging?'Evaluating golden answers':'Suite in progress';
  const description=judging?'Target traffic finished. Evaluating generated answers against golden answers; target measurements are frozen.':'Measuring the configured endpoint. Results are assessed when the suite finishes.';
  const bounds=judging?[['Evaluator requests at a time',c.judging?.concurrency],['Evaluation duration',elapsed(c.judging?.max_duration_seconds)],['Judge deadline',elapsed(c.judging?.request_timeout_seconds)],['Judge output cap',`${c.judging?.max_output_tokens} tokens`]]:[['Maximum in flight',c.concurrency],['Total request limit',c.max_requests],['Maximum duration',elapsed(c.max_duration_seconds)],['Request deadline',elapsed(c.request_timeout_seconds)],['Output cap',`${c.max_output_tokens} tokens`]];
  $('#main').innerHTML=`<div class="page-heading"><div><div class="eyebrow">Live run <span class="mono">${escape(d.id)}</span></div><h1>${stopping?'Stopping the suite':title}</h1><p>${stopping?'Stopping further work and saving partial evidence.':description}</p></div><button class="button danger" data-action="stop" ${stopping?'disabled':''}>${icon('stop')}${stopping?'Stopping…':'Stop run'}</button></div><div class="live-layout"><div><section class="panel live-state">${badge(d.state)}<h2>${judging?'Post-run AI evaluation':escape(human(progress.scenario || progress.event || 'Preparing fixtures'))}</h2><p class="live-phase">${escape(progress.target || (c.targets || []).map(t=>t.name).join(', '))} · ${escape((c.targets || []).map(t=>t.model).join(', '))}</p><dl class="live-numbers"><div><dt>${judging?'Judge calls completed':'Finished / started'}</dt><dd>${number(judging?judgeDone:finished)} <span class="dim">/ ${number(judging?judgeTotal:started)}</span></dd></div><div><dt>${judging?'Target requests finished':'Active requests'}</dt><dd>${number(judging?finished:active)}</dd></div><div><dt>Elapsed</dt><dd>${elapsed(secs)}</dd></div></dl><div class="budget-track" aria-hidden="true"><span style="width:${Math.min(100,judging?judgeDone/(judgeTotal || 1)*100:started/(c.max_requests || 1)*100)}%"></span></div><div class="budget-caption"><span>${judging?`Evaluation calls · ${number(judgeDone)} of ${number(judgeTotal)}`:`Request budget used · ${number(started)} of ${number(c.max_requests)}`}</span><span>${judging?'Inference timing is unchanged':'Budget ceiling, not completion'}</span></div></section><section class="panel" style="margin-top:20px"><div class="section-heading"><h2>Recent activity</h2><span class="hint">Refreshes automatically</span></div><div class="event-list">${[...events].reverse().slice(0,15).map(e=>`<div class="event-row"><span class="time">${elapsed(e.elapsed_seconds)}</span><span>${escape(human(e.scenario || e.event || 'Progress'))}</span><span class="phase">${String(e.event || '').startsWith('judging_')?`${number(e.judge_calls_completed || 0)} / ${number(e.judge_calls_total || 0)} judge calls`:`${escape(e.target || '')} · ${number(e.completed || 0)} finished`}</span></div>`).join('') || '<p class="hint" style="padding:20px 0">Waiting for the first runner event…</p>'}</div></section></div><aside class="panel run-preview"><h3>${judging?'Evaluation bounds':'Run bounds'}</h3><p>${judging?'Separate limits apply to the configured judges.':'The current suite shares these ceilings across all targets.'}</p><dl class="bound-list">${bounds.map(([k,v])=>`<div><dt>${escape(k)}</dt><dd>${escape(v)}</dd></div>`).join('')}</dl><hr><p class="hint">${judging?'Incomplete panels, evaluation failures and ties leave answers unscored.':'Finished requests include responses, failures, timeouts and cancellations. Some interruptions are deliberate suite probes.'}</p></aside></div><div id="live-error" class="error-message" role="alert" hidden></div>`;
}
function renderJudgement(judgement) {
  if(!judgement)return '';
  const votes=judgement.votes || [],yes=votes.filter(vote=>vote.verdict===true).length,no=votes.filter(vote=>vote.verdict===false).length;
  const incomplete=!votes.length||votes.some(vote=>vote.status!=='completed'||typeof vote.verdict!=='boolean');
  const explanation=incomplete?'Incomplete panel · answer unscored':yes===no?'Tie · answer unscored':`${yes} accept · ${no} reject`;
  return `<section class="judge-evidence"><h3>AI evaluation ${badge(judgement.status)}</h3><p class="hint">${explanation}${yes&&no?' · Disagreement among judges':''}</p>${votes.map(vote=>`<article class="judge-vote"><div class="statline"><strong>${escape(vote.judge)}</strong>${vote.verdict===true?badge('pass','Accept'):vote.verdict===false?badge('fail','Reject'):badge('inconclusive','No verdict')}</div><p class="hint">${escape(vote.model)}${vote.observed_model&&vote.observed_model!==vote.model?` · observed ${escape(vote.observed_model)}`:''} · ${escape(vote.url)}</p>${vote.error_category?`<p class="hint">Evaluation error: ${escape(vote.error_category)}</p>`:''}${vote.reason?`<p class="judge-reason">${escape(vote.reason)}</p>`:'<p class="hint">Reason not retained or unavailable.</p>'}</article>`).join('')}</section>`;
}
function renderEvaluationEvidence(record,report) {
  let evidence=record.evaluation;
  if(!evidence){
    const config=report.manifest?.config;
    const item=config?.request_source==='request_pack'?config.request_pack?.cases.find(item=>record.fixture_id===`pack:${item.fixture_id}`):null;
    if(item)evidence={...item,expected_provided:Object.hasOwn(item,'expected')};
  }
  if(!evidence)return '<p class="hint">Input and answer-check details were not saved in this report.</p>';
  const expected=evidence.scorer==='json'?JSON.stringify(evidence.expected,null,2):String(evidence.expected ?? '');
  const schema=evidence.schema ?? evidence.schema_;
  return `<h3>Input messages</h3>${(evidence.messages || []).map(message=>`<h4>${escape(message.role)}</h4><div class="body-output"><pre>${escape(message.content)}</pre></div>`).join('')}<h3>Answer check</h3><p>${escape(answerChecks[evidence.scorer] || evidence.scorer)}</p><h3>Expected answer</h3>${evidence.expected_provided?`<div class="body-output"><pre>${escape(expected)}</pre></div>`:'<p class="hint">No expected answer configured.</p>'}${evidence.rubric?`<h3>Judge rubric</h3><div class="body-output"><pre>${escape(evidence.rubric)}</pre></div>`:''}${schema!=null?`<details><summary>JSON schema</summary><pre>${escape(JSON.stringify(schema,null,2))}</pre></details>`:''}`;
}
function openRequest(id) {
  const report=state.detail.report,r=report.requests.find(x=>x.id===id);if(!r)return;
  const dialog=$('#request-dialog'),retention=reportConfig(report).retention || 'failures';
  dialog.innerHTML=`<div class="dialog-head"><div><div class="eyebrow">Request evidence</div><h2 id="request-title">${escape(r.fixture_id)}</h2><p class="mono">${escape(r.id)}</p></div><button class="button ghost" data-action="close-dialog" aria-label="Close request evidence">✕</button></div><div class="dialog-body"><div class="statline"><span class="pill">${escape(r.target)}</span><span class="muted">${escape(r.scenario)}</span><span>Response: ${escape(r.status)}</span>${r.score===false?badge('fail','Incorrect answer'):r.score===true?badge('pass','Correct answer'):'<span class="dim">Answer unscored</span>'}</div>${isProbe(r)?'<div class="notice" style="margin-top:20px">This request is an intentional cancellation or deadline probe. Its transport status alone does not indicate a failed suite check.</div>':''}${r.error?`<div class="notice ${isProbe(r)?'':'error'}" style="margin-top:20px">${escape(r.error)}</div>`:''}<dl class="metrics-grid" style="margin-top:24px">${r.traffic_stage!=null?metric('Arrival rate',`${number(r.traffic_rate_rps,2)} requests/s`,r.traffic_class || '')+metric('Local dispatch delay',ms(r.dispatch_lag_ms),'Measured from planned arrival; not server queue time'):''}${metric('Elapsed',ms(r.elapsed_ms))}${metric('First answer',ms(r.first_output_ms))}${metric('Last answer',ms(r.last_output_ms))}${metric('First reasoning',ms(r.first_reasoning_ms))}${metric('Longest stream gap',ms(r.max_stream_gap_ms))}${metric('Input / output tokens',`${number(r.input_tokens)} / ${number(r.output_tokens)}`,'Server-reported usage')}${metric('HTTP / finish reason',`${r.http_status ?? '—'} / ${r.finish_reason || '—'}`)}</dl>${r.score_message?`<p class="hint" style="margin-top:18px">${escape(r.score_message)}</p>`:''}<div class="request-comparison"><section>${renderEvaluationEvidence(r,report)}</section><section><h3>Actual answer <span class="hint">${number(r.output_chars)} recorded characters</span></h3><div class="body-output">${r.output?`<pre>${escape(r.output)}</pre>`:r.output_chars>0?`<p class="body-not-retained">Body not retained (retention: ${escape(retention)}). Measurements and scoring are preserved.</p>`:'<p class="body-not-retained">No answer text was recorded for this request.</p>'}</div></section></div>${renderJudgement(r.judgement)}<h3>Reasoning output</h3><div class="body-output">${r.reasoning?`<pre>${escape(r.reasoning)}</pre>`:`<p class="body-not-retained">${retention==='all'?'No reasoning text was recorded.':'No reasoning body retained. This does not establish whether the model reasoned.'}</p>`}</div><p class="hint" style="margin-top:18px">${r.valid?'Valid non-empty completion':'Completion not validated'} · ${r.stream?'Streaming':'Non-streaming'} · ${r.stream_terminated?'Stream termination observed':'Stream termination not recorded'}</p></div><div class="dialog-footer"><button class="button" data-action="close-dialog">Close</button></div>`;
  dialog.showModal();
}
function openBaselineDialog(){
  const report=state.detail.report,dialog=$('#baseline-dialog');dialog.className='baseline-form';dialog.innerHTML=`<form id="baseline-form"><div class="dialog-head"><div><div class="eyebrow">Explicit reference run</div><h2 id="baseline-title">Save baseline</h2></div><button type="button" class="button ghost" data-action="close-dialog" aria-label="Close save baseline">✕</button></div><div class="dialog-body"><p class="hint">Save this exact report for future comparisons. Its recorded outcome is ${escape(statusLabels[report.overall] || report.overall)}.</p><div class="fields">${field('baseline_name','Baseline name','',{required:true,full:true,placeholder:'e.g. before-runtime-upgrade'})}</div><p class="hint" style="margin-top:12px">Existing baselines are preserved unless you explicitly replace one.</p><div id="baseline-error" class="error-message" role="alert" hidden></div><label class="replace-row" id="replace-row" hidden><input name="replace" type="checkbox">Replace the existing baseline with this name</label></div><div class="dialog-footer"><button type="button" class="button" data-action="close-dialog">Cancel</button><button type="submit" class="button primary" id="save-baseline">Save baseline</button></div></form>`;dialog.showModal();}
async function saveBaseline(event){event.preventDefault();const form=event.target,data=new FormData(form),button=$('#save-baseline'),error=$('#baseline-error');button.disabled=true;error.hidden=true;try{await api('/api/baselines',{method:'POST',body:JSON.stringify({run_id:state.detail.id,name:String(data.get('baseline_name')).trim(),replace:data.has('replace')})});const result=await api('/api/baselines');state.baselines=result.baselines || [];$('#baseline-dialog').close();toast('Baseline saved. Select it when configuring your next run.');}catch(e){error.textContent=e.message;error.hidden=false;if(e.status===409)$('#replace-row').hidden=false;}finally{button.disabled=false;}}
async function stopRun(){const button=$('[data-action="stop"]'),id=state.detail.id,version=state.routeVersion;button.disabled=true;button.textContent='Stopping…';try{const result=await api(`${runPath(id)}/cancel`,{method:'POST',body:'{}'});if(version!==state.routeVersion||state.detail?.id!==id)return;state.detail.state=result.state || 'stopping';renderDetail();schedulePoll(pollDetail,version,500);}catch(error){if(version!==state.routeVersion||state.detail?.id!==id)return;const el=$('#live-error');if(el){el.textContent=error.message;el.hidden=false;}button.disabled=false;button.textContent='Stop run';}}
function rerun(){const source=state.detail.report?.manifest?.config || state.detail.config;if(!source){toast('No configuration was saved for this run.');return;}state.config=clone(source);state.datasetSelection=[];state.config.restart_target=null;state.formMode='normal';state.formRestartTarget=null;state.jsonDraft=null;state.jsonBase=null;state.configView='form';configChanged();state.prefillBaseline=state.detail.baseline_id || '';location.hash='#new';}
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
  const budgetButton=$('[data-action="use-request-budget"]');if(budgetButton)budgetButton.disabled=dirty;
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
  configChanged();state.jsonDraft=state.jsonBase;$('#config-editor').value=state.jsonDraft;configError();syncConfigView();
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
    state.config=result.config;state.datasetSelection=[];
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
  if(reset){configChanged();const form=$('#new-run-form');form.elements[`test_mode_${reset}`].value='default';refreshInheritedFields(reset);syncTestControls();updateBounds();return;}
  if(action==='use-request-budget'){
    try{
      assertAppliedConfig();
      const budget=Number(event.target.closest('[data-budget]').dataset.budget),input=$('#new-run-form').elements.max_requests;
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
  if(action==='toggle-available-tests'){state.showAvailableTests=!state.showAvailableTests;syncTestControls();return;}
  if(action==='select-tests'||action==='clear-tests'){configChanged();for(const input of $$('#new-run-form input[name="check"]'))input.checked=action==='select-tests';syncTestControls();updateBounds();return;}
  if(action==='reset-traffic'){configChanged();const form=$('#new-run-form');form.elements.traffic_mode.value='default';const defaults=state.bootstrap.config.traffic;for(const [key,value] of Object.entries(defaults)){if(key==='mix'){for(const [kind,weight] of Object.entries(value))form.elements[`traffic_mix_${kind}`].value=weight;}else form.elements[`traffic_${key}`].value=Array.isArray(value)?value.join(', '):value ?? '';}syncTestControls();updateBounds();return;}
  if(action==='retry')route();if(action==='clear-filter'){state.filter='all';renderRuns();}
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
  if(action==='select-pack-tests'){state.showAvailableTests=true;syncTestControls();$('#test-selection').scrollIntoView({block:'start'});$('#test-selection input[value="correctness"]').focus();}
  if(action==='export-config')await exportConfig();
  if(action==='copy-cli')await copyCli();
});
document.addEventListener('submit',event=>{if(event.target.id==='new-run-form')startRun(event);if(event.target.id==='baseline-form')saveBaseline(event);if(event.target.id==='dataset-example-form')saveExample(event);if(event.target.id==='dataset-import-form')useImportedDataset(event);if(event.target.id==='dataset-try-form')tryDatasetExample(event);});
document.addEventListener('input',event=>{
  if(!event.target.closest('#new-run-form')||event.target.matches('[data-dataset-select], #dataset-select-all, #dataset-bulk-scorer'))return;
  configChanged();$('#cli-fallback').hidden=true;
  if(event.target.id==='config-editor'){state.jsonDraft=event.target.value;syncConfigView();return;}
  if(event.target.closest('[data-test-editor]'))editTestField(event.target);
  if(numericFields.some(([name])=>name===event.target.name)||event.target.name.startsWith('limit_'))for(const check of state.bootstrap.checks)refreshInheritedFields(check.id,true);
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
  if(event.target.id==='dataset-bulk-scorer')return;
  if(event.target.name==='example_scorer'){syncExampleScorer();return;}
  if(event.target.closest('#dataset-import-form')){const draft=state.datasetImport;if(draft){draft.mapping=importMappingValues();draft.sequence++;draft.loading=false;draft.mappingDirty=true;renderDatasetImport();}return;}
  if(event.target.id==='request-pack-file'){const file=event.target.files[0];event.target.value='';importRequestPack(file);return;}if(event.target.name==='request_source'){changeRequestSource(event.target.value);return;}if(event.target.id==='config-file'){const file=event.target.files[0];event.target.value='';importConfig(file);return;}if(event.target.closest('#new-run-form'))configChanged();if(event.target.closest('[data-test-editor]')&&event.target.tagName==='SELECT'){editTestField(event.target);updateBounds();}if(event.target.name==='check'){syncTestControls();updateBounds();if(event.target.closest('[data-test-card]')?.hidden)$('#add-tests')?.focus();}if(['comparison-outcome','comparison-check'].includes(event.target.id)){const id=event.target.id;if(id==='comparison-outcome')state.comparisonFilter=event.target.value;else state.comparisonCheck=event.target.value;$('#result-content').innerHTML=renderComparison(state.detail.report);syncComparisonFilters();$('#'+id).focus();}if(event.target.id==='run-filter'){state.filter=event.target.value;renderRuns();$('#run-filter').focus();}if(['mode','restart_target'].includes(event.target.name)){updateRestart();if(state.configView==='json'&&!hasJsonChanges()){state.jsonDraft=JSON.stringify(portableConfig(readForm(true)),null,2);state.jsonBase=state.jsonDraft;$('#config-editor').value=state.jsonDraft;}}if(event.target.name==='replace')$('#save-baseline').textContent=event.target.checked?'Replace baseline':'Save baseline';});
document.addEventListener('keydown',event=>{const configTab=event.target.closest('[data-config-tab]');if(configTab&&['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();switchConfigView(event.key==='Home'?'form':event.key==='End'?'json':state.configView==='form'?'json':'form');return;}const tab=event.target.closest('[data-tab]');if(tab&&['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();const tabs=['checks','baseline','config'];let i=tabs.indexOf(state.tab);i=event.key==='Home'?0:event.key==='End'?2:(i+(event.key==='ArrowRight'?1:2))%3;state.tab=tabs[i];renderDetail();$(`#tab-${state.tab}`).focus();}});
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
window.addEventListener('hashchange',route);
async function init(){try{const [bootstrap,baselines]=await Promise.all([api('/api/bootstrap'),api('/api/baselines')]);state.bootstrap=bootstrap;state.baselines=baselines.baselines || [];state.config=clone(bootstrap.config);state.config.restart_target=null;setActive(bootstrap.active_run_id);$('#suite-version').textContent=`v${bootstrap.suite_version || '0.1.0'}`;$('#runner-location').textContent=bootstrap.hostname || 'UI server connected';await route();}catch(error){$('#main').innerHTML=`<div class="page-heading"><h1>Local runner unavailable</h1></div>${notice(error.message,'error')}<button class="button" onclick="location.reload()">Reconnect</button>`;}}
init();
