"""RAM measurements and guardrails remain distinct from saved disk storage."""

from tests.test_ux_results import run_render_checks


def test_ram_limit_field_is_required_nonnegative_and_keeps_zero_and_blank_values():
    run_render_checks(r"""
const config={targets:[{name:'local',model:'test',url:'http://localhost/v1'}],checks:[],limits:{max_error_rate:0,min_correctness:1,min_samples:1},max_requests:10,max_duration_seconds:30,concurrency:2,max_memory_mb:0};
const html=field('max_memory_mb','Max memory growth (MiB)',0,{type:'number',required:true,hint:'0 turns off the memory limit.'});
assert.match(html,/name="max_memory_mb" type="number" value="0"[^>]*required[^>]*min="0"/);
assert.doesNotMatch(renderSharedRunSettings(config),/name="max_memory_mb"/);
assert.equal(settingDestination('max_memory_mb').name,'max_memory_mb');
assert.equal(settingDestination('max_memory_mb').label,'Maximum memory');
state.config=config;state.bootstrap.config={traffic:{}};
const values=new Map([['max_memory_mb_enabled','off'],['url','http://localhost/v1'],['model','test'],['name','local'],['max_memory_mb','0']]);
FormData=class {constructor(){} get(key){return values.get(key) ?? null;} has(key){return values.has(key);} set(key,value){values.set(key,value);}};
nodes['#new-run-form']={elements:{metrics_url:{value:''}},querySelectorAll:()=>[]};
configureTests=(c)=>c;configureConcurrentLoad=(c)=>c;configureJudging=(c)=>c;configureProfiles=(c)=>c;configureSessions=(c)=>c;concurrentFormDefaults=()=>({});
let payload=readForm();assert.equal(payload.config.max_memory_mb,0);assert.equal(portableConfig(payload).max_memory_mb,0);
values.set('max_memory_mb_enabled','on');values.set('max_memory_mb','512');payload=readForm();assert.equal(payload.config.max_memory_mb,512);
values.set('max_memory_mb','');assert.throws(()=>readForm(),/Enter a value or turn this limit off/);payload=readForm(true);assert.equal(payload.config.max_memory_mb,'');
assert.equal(state.config.max_memory_mb,0);
""")


def test_ram_phase_breakdown_and_lifetime_peak_have_distinct_sources():
    run_render_checks(r"""
const resources={rss_at_start_bytes:100*1024**2,sampled_peak_growth_bytes:{traffic:100*1024**2,scoring:150*1024**2},sampled_peak_rss_bytes:{traffic:200*1024**2,scoring:250*1024**2,export:999999999},process_peak_rss_bytes:400*1024**2,memory_limit_bytes:4096*1024**2,memory_limit_reached:false,guardrail_active:true,sampling:'About once per second; short spikes between samples can be missed.'};
const value=report([check('access','pass')],{observations:{resources}});
const html=detail(value,{resources:{sampled_peak_rss_bytes:{export:300*1024**2}}});
assert.match(html,/aria-labelledby="memory-title"/);assert.match(html,/<h2 id="memory-title">Peak memory \(RAM\)<\/h2>/);
assert.match(html,/<dt>Traffic<\/dt><dd>200 MiB/);assert.match(html,/<dt>Scoring \/ aggregation<\/dt><dd>250 MiB/);assert.match(html,/<dt>Export<\/dt><dd>300 MiB/);
assert.doesNotMatch(html,/1000 MB/);
assert.match(html,/<dt>Process lifetime peak<\/dt><dd>400 MiB/);
assert.match(html,/Includes earlier runs in the web server; never used for the memory guardrail/);
assert.match(html,/Maximum memory \(RAM growth during this run\): 4 GiB. Guardrail active/);assert.match(html,/short spikes between samples can be missed/);
assert.match(html,/separate from runs on disk/);
state.storage={runs_bytes:2*1024**2,baselines_bytes:0,free_bytes:9*1024**3,limits:{max_total_storage:5*1024**3}};
assert.match(storageLine(),/Runs: 2 MiB/);assert.doesNotMatch(storageLine(),/Peak memory|RAM/);
""")


def test_unavailable_ram_is_not_zero_and_guardrail_states_are_explicit():
    run_render_checks(r"""
let resources={memory_measurement:'unavailable',sampled_peak_rss_bytes:{traffic:null,scoring:null},process_peak_rss_bytes:null,memory_limit_bytes:4096000000,guardrail_active:false,memory_limit_reached:false};
let html=renderResources({observations:{resources}},{resources:{memory_measurement:'unavailable',sampled_peak_rss_bytes:{export:null}}});
assert.equal((html.match(/not measured on this platform/g)||[]).length,8);assert.doesNotMatch(html,/<dd>0 B/);
assert.match(html,/Memory guardrail inactive/);
resources.memory_limit_bytes=0;html=renderResources({observations:{resources}});assert.match(html,/Memory guardrail disabled \(0 = off\)/);
resources.memory_limit_reached=true;resources.memory_limit_stopped_run=true;html=renderResources({observations:{resources}});assert.match(html,/notice warning/);assert.match(html,/Memory limit exceeded; unfinished work stopped with partial results/);
html=detail(null,{state:'error',error:'Export failed',resources:{sampled_peak_rss_bytes:{export:120*1024**2}},summary:{overall:'pass',request_count:1,checks:[]}});
assert.match(html,/Run could not finish/);assert.match(html,/Export failed/);assert.match(html,/<dt>Export<\/dt><dd>120 MiB/);assert.doesNotMatch(html,/Acceptance passed/);
""")


def test_memory_stop_verdict_is_inconclusive_without_erasing_blocked_checks():
    run_render_checks(r"""
const value=report([check('access','blocked')],{overall:'inconclusive',abort_reason:'memory limit reached',observations:{resources:{memory_limit_reached:true,memory_limit_stopped_run:true,memory_interrupted_checks:[{target:'local',id:'access'}]}}});
let html=detail(value);
assert.match(html,/result-heading inconclusive/);assert.match(html,/<h1>Run stopped with partial results<\/h1>/);assert.doesNotMatch(html,/result-heading blocked/);
assert.match(html,/Recorded blocked reason/);
value.checks.push(check('correctness','fail'));value.overall='fail';html=detail(value);
assert.match(html,/result-heading fail/);assert.match(html,/<h1>[^<]*failed<\/h1>/);
""")


def test_ram_growth_warning_preserves_completed_results_and_binary_units():
    run_render_checks(r"""
const resources={memory_limit_reached:true,memory_limit_stopped_run:false,memory_interrupted_checks:[],memory_limit_bytes:4096*1024**2,rss_at_start_bytes:8*1024**3,sampled_peak_rss_bytes:{traffic:12*1024**3},sampled_peak_growth_bytes:{traffic:4*1024**3}};
let html=detail(report([check('access','pass')],{observations:{resources}}));
assert.match(html,/result-heading pass/);assert.match(html,/<h1>1 check passed<\/h1>/);assert.doesNotMatch(html,/result-heading inconclusive|stopped with partial results/);
assert.match(html,/Memory limit exceeded; completed checks keep their results. No work was stopped/);
assert.match(html,/<dt>At run start<\/dt><dd>8 GiB/);assert.match(html,/<dt>Traffic RAM growth<\/dt><dd>4 GiB/);
assert.equal(memorySize(0),'0 MiB');assert.equal(memorySize(1024**2),'1 MiB');assert.equal(memorySize(1024**3),'1 GiB');
assert.doesNotMatch(renderResources({observations:{resources}}),/ GB| MB/);
""")


def test_missing_ram_is_not_recorded_until_explicitly_unavailable():
    run_render_checks(r"""
let html=renderResources(report([]));
assert.match(html,/not recorded/);assert.doesNotMatch(html,/not measured on this platform/);
html=renderResources({observations:{resources:{sampled_peak_rss_bytes:{traffic:null},guardrail_active:false}}});
assert.match(html,/not recorded/);assert.doesNotMatch(html,/not measured on this platform/);
html=renderResources({observations:{resources:{memory_measurement:'unavailable',guardrail_active:false}}});
assert.match(html,/not measured on this platform/);assert.match(html,/Memory guardrail inactive/);
""")


def test_only_interrupted_required_checks_make_ram_verdict_inconclusive():
    run_render_checks(r"""
const resources={memory_limit_reached:true,memory_limit_stopped_run:true,memory_interrupted_checks:[{target:'second',id:'access'}]};
const checks=[check('access','pass'),check('access','inconclusive',{target:'second',required:false})];
let html=detail(report(checks,{observations:{resources}}));
assert.match(html,/result-heading pass/);assert.match(html,/<h1>1 check passed<\/h1>/);assert.match(html,/unfinished work stopped with partial results/);
checks[1].required=true;html=detail(report(checks,{overall:'inconclusive',observations:{resources}}));
assert.match(html,/result-heading inconclusive/);assert.match(html,/<article class="panel finding-check pass" data-finding="local:access"/);
""")
