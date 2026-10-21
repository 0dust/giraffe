"""Final #45 drawer, planner parity and #50 browser interaction contracts."""
import json
import re
from pathlib import Path

import httpx
import pytest

from giraffe.models import RunConfig
from giraffe.planning import request_plan
from giraffe.web import create_app
from tests.test_concurrent_load_ui_review import FORM
from tests.test_ux_catalogue import browser_logic
from tests.test_ux_results import run_render_checks

STATIC=Path(__file__).parents[1]/'src/giraffe/static'

# Each tuple is a real form edit, its exact field destination, and the mock copy.
ISSUES=[
    ("form.elements.load_profile_short_input_tokens.value='0'",'load_test.profile.short.input_tokens','Token sizes must be positive whole numbers.'),
    ("form.elements.load_profile_medium_output_tokens.value='1'",'load_test.profile.medium.output_tokens','Use increasing Short, Medium and Long token sizes.'),
    ("for(const shape of loadShapes)form.elements['load_workloads_combinations_selected_'+shape].checked=false",'load_test.workloads.combinations.selected','Select at least one size combination.'),
    ("form.elements.load_workloads_mixed_enabled.checked=true;for(const shape of loadShapes)form.elements['load_workloads_mixed_selected_'+shape].checked=shape==='short_short'",'load_test.workloads.mixed.selected','Select at least two request types for mixed traffic.'),
    ("form.elements.load_workloads_mixed_enabled.checked=true;form.elements.load_workloads_mixed_share_mode.value='custom';form.elements.load_workloads_mixed_shares_short_short.value='10'",'load_test.workloads.mixed.share_mode','Give each selected mix type a positive share, totaling 100%.'),
    ("form.elements.load_workloads_stress_enabled.checked=true;for(const shape of ['long_short','short_long','long_long'])form.elements['load_workloads_stress_cases_'+shape].checked=false",'load_test.workloads.stress.cases','Select at least one token-stress case.'),
    ("for(const type of Object.keys(loadTitles))form.elements['load_workloads_'+type+'_enabled'].checked=false",'load_test.workloads.combinations.enabled','Select at least one workload.'),
    ("for(const type of ['concurrent','constant','poisson','bursts'])form.elements['load_patterns_'+type+'_enabled'].checked=false",'load_test.patterns.poisson.enabled','Select a traffic pattern for the selected workloads.'),
    ("form.elements.load_patterns_concurrent_levels.value='2, 1'",'load_test.patterns.concurrent.levels','Enter positive, increasing whole-number levels separated by commas.'),
    ("form.elements.load_patterns_rates.value='0'",'load_test.patterns.rates','Enter positive, increasing rates separated by commas.'),
    ("form.elements.load_schedule_duration_seconds.value='0'",'load_test.schedule.duration_seconds','Check Duration per workload, pattern and level (seconds).'),
    ("form.elements.load_schedule_finish_seconds.value='-1'",'load_test.schedule.finish_seconds','Check Wait for unfinished requests after each test (seconds).'),
    ("form.elements.load_patterns_poisson_enabled.checked=true;form.elements.load_patterns_poisson_seed.value='-1'",'load_test.patterns.poisson.seed','Check Random seed.'),
    ("form.elements.load_patterns_bursts_enabled.checked=true;form.elements.load_patterns_bursts_size.value='0'",'load_test.patterns.bursts.size','Check Requests per burst.'),
    ("form.elements.load_patterns_bursts_enabled.checked=true;form.elements.load_patterns_bursts_first_seconds.value='-1'",'load_test.patterns.bursts.first_seconds','Check First burst after (seconds).'),
    ("form.elements.load_patterns_bursts_enabled.checked=true;form.elements.load_patterns_bursts_gap_seconds.value='0'",'load_test.patterns.bursts.gap_seconds','Check Time between burst starts (seconds).'),
    ("form.elements.load_patterns_bursts_enabled.checked=true;form.elements.load_patterns_bursts_first_seconds.value='15'",'load_test.patterns.bursts.first_seconds','The first burst must start before the sending window ends.'),
    ("form.elements.concurrency.value='4';form.elements.load_patterns_concurrent_levels.value='8'",'concurrency','Concurrency levels exceed the run ceiling. Update Run limits or lower the levels.'),
    ("form.elements.load_patterns_bursts_enabled.checked=true;form.elements.concurrency.value='4';form.elements.load_patterns_bursts_size.value='8'",'concurrency','One burst exceeds the concurrency ceiling and cannot be dispatched in full.'),
    ("form.elements.load_workloads_interference_enabled.checked=true;form.elements.load_patterns_concurrent_levels.value='1'",'load_test.patterns.concurrent.levels','The long-input / short-answer test needs a concurrency level of at least 2.'),
    ("form.elements.load_workloads_interference_enabled.checked=true;form.elements.load_workloads_interference_max_slowdown.value='1'",'load_test.workloads.interference.max_slowdown','Check Maximum allowed slowdown of short answers (×).'),
    ("form.elements.load_workloads_stress_enabled.checked=true;form.elements.load_workloads_stress_min_output_percent.value='0'",'load_test.workloads.stress.min_output_percent','Check Minimum generated output (% of long-output cap).'),
    ("form.elements.load_workloads_stress_enabled.checked=true;form.elements.load_workloads_stress_min_output_percent.value='101'",'load_test.workloads.stress.min_output_percent','Output coverage cannot exceed 100% of the cap.'),
    ("form.elements.load_request_timeout_seconds.value='0'",'load_test.request_timeout_seconds','Check Request timeout (seconds).'),
    ("form.elements.load_pass_criteria_min_valid_responses.value='0'",'load_test.pass_criteria.min_valid_responses','Check Minimum valid responses per result group.'),
    ("form.elements.load_pass_criteria_max_error_percent.value='-1'",'load_test.pass_criteria.max_error_percent','Check Errors allowed (%).'),
    ("form.elements.load_pass_criteria_min_correct_percent.value='101'",'load_test.pass_criteria.min_correct_percent','Percentages must be from 0 to 100.'),
    *[(f"form.elements.load_pass_criteria_{key}.value='0'",'load_test.pass_criteria.'+key,'Check '+label+'.') for key,label in [
        ('max_first_answer_ms','Maximum time to first answer per response (ms)'),('max_completion_ms','Maximum completion time per response (ms)'),('max_stream_pause_ms','Maximum pause between streamed chunks (ms)'),('min_generation_tokens_per_second','Minimum generation speed per response (tokens/sec)')]],
    ("form.elements.concurrency.value='0'",'concurrency','Check Concurrency ceiling.'),
    ("form.elements.max_requests.value='0'",'max_requests','Check Total request budget.'),
    ("form.elements.max_duration_seconds.value='0'",'max_duration_seconds','Check Total duration.'),
]


@pytest.mark.parametrize('edit,path,message',ISSUES,ids=[row[1]+':'+str(i) for i,row in enumerate(ISSUES)])
def test_every_local_issue_is_inline_in_plan_and_focusable_before_api(edit,path,message):
    browser_logic(FORM+'\n'+edit+';\nconst path='+json.dumps(path)+',message='+json.dumps(message)+r''';
const errors=currentSettingErrors(),error=errors.find(issue=>issue.fields.includes(path)&&issue.msg===message);assert(error,JSON.stringify(errors));
const dest=settingDestination(path);nodes['#field-'+dest.name+'-error']={};syncInlineErrors();
assert(nodes['#field-'+dest.name+'-error'].textContent.includes(message));assert.equal(nodes['#field-'+dest.name+'-error'].hidden,false);
const plan=renderRunBlueprint(readForm(true).config);assert(plan.includes(escape(message)));assert(plan.includes('data-setting-path="'+path+'"'));
await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'edit-setting'}}:q==='[data-setting-path]'?{dataset:{settingPath:path}}:null}});
assert.equal(focused.at(-1),dest.name);assert.equal(state.setupStep,2);if(path.startsWith('load_test.'))assert.equal(state.openTest,'load');
api=()=>assert.fail('invalid local field reached API');await assert.rejects(refreshRequestPreview(undefined,{draft:true}));
''')


@pytest.mark.parametrize('constant,poisson,expected,tests,label',[
    (False,False,160,1,'160'),(False,True,760,1,'≈ 760'),(True,True,1520,2,'≈ 1,520')])
async def test_worked_plan_examples_use_preview_numbers(tmp_path,constant,poisson,expected,tests,label):
    config=RunConfig(targets=[{'name':'local','url':'http://unused.test/v1','model':'test'}],checks=['load'],concurrency=128,max_requests=20000,max_duration_seconds=1800,
        load_test={'workloads':{'combinations':{'selected':['short_short']}},'patterns':{'concurrent':{'enabled':False},'constant':{'enabled':constant},'poisson':{'enabled':poisson},'bursts':{'enabled':True,'first_seconds':10,'gap_seconds':10,'size':32},'rates':[10]},'schedule':{'duration_seconds':60,'finish_seconds':30}})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)),base_url='http://test') as client:
        body=(await client.post('/api/config/preview',json=config.model_dump(mode='json'))).json()
    plan=body['request_plan']
    assert plan['load_test']==request_plan(config)['load_test']
    assert plan['load_test']['expected_scheduled_requests']==expected
    assert plan['load_test']['scheduled_tests']==tests
    browser_logic('const plan='+json.dumps(plan)+',label='+json.dumps(label)+r''';
const html=renderLoadPlan(config,plan);assert(html.includes(label));assert(html.includes(loadElapsed(plan.load_test.configured_load_seconds)));assert(html.includes(loadElapsed(plan.load_test.time_with_allowances_seconds)));
for(const text of plan.load_test.execution_summary)assert(html.includes(escape(text)));
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan,config};nodes['#load-burst-preview']={};syncLoadDisplay(config);
const text=nodes['#load-burst-preview'].textContent;assert.match(text,/5 bursts per test: at 10, 20, 30, 40, 50 seconds. 160/);
if(config.load_test.patterns.poisson.enabled)assert.match(text,/160 additional requests per arrival-pattern test/);else assert.match(text,/160 scheduled requests in this burst-only test/);
''',config)


def test_cached_backend_plan_validates_limit_changes_without_recounting():
    config=RunConfig(targets=[{'name':'local','url':'http://unused.test/v1','model':'test'}],checks=['load'])
    browser_logic('const backend='+json.dumps(request_plan(config))+r''';
state.requestPreview={plan:backend,config:structuredClone(config)};
const draft=structuredClone(config);draft.max_requests=1;draft.max_duration_seconds=1;
const issues=runLimitIssues(draft);assert(issues.some(issue=>issue.msg.includes('initial concurrent groups')));assert(issues.some(issue=>issue.msg.includes('Expected scheduled requests')));assert(issues.some(issue=>issue.fields.includes('max_duration_seconds')));
backend.load_test.expected_scheduled_requests=0;backend.load_test.minimum_initial_requests_per_target=0;backend.load_test.time_with_allowances_seconds=0;
assert.equal(runLimitIssues(draft).length,0); // All three numbers are server owned.
''',config)


def test_run_limits_click_targets_and_scrollable_plan_css():
    css=(STATIC/'styles.css').read_text()
    selectors=[match.group(1).strip() for match in re.finditer(r'([^{}]+)\{[^{}]*\}',css) if '.test-selection::after' in match.group(1)]
    assert selectors and all('.test-card > .test-card-heading ' in selector for selector in selectors)
    assert re.search(r'\.test-card-heading\s*\{[^}]*position:\s*relative',css)
    desktop=re.search(r'\.run-blueprint\s*\{([^}]+)',css)[1]
    for rule in ['max-height: calc(100dvh - 76px - var(--run-dock-clearance, 112px) - env(safe-area-inset-bottom, 0px))','overflow-y: auto','overscroll-behavior: contain']:
        assert rule in desktop
    assert re.search(r'@media\s*\(max-width:\s*\d+px\)[\s\S]*?\.run-blueprint\s*\{[^}]*position:\s*static;[^}]*max-height:\s*none;[^}]*overflow-y:\s*visible',css)
    browser_logic(r'''
const html=renderSharedRunSettings(config);assert.equal((html.match(/class="run-limit-selection"/g)||[]).length,4);assert.doesNotMatch(html,/class="test-selection"/);
for(const label of html.matchAll(/<label\b[^>]*>[\s\S]*?<\/label>/g))assert.doesNotMatch(label[0],/class="field-info"/);
for(const field of ['max_duration_seconds','max_requests','concurrency','max_memory_mb'])assert(html.includes('popovertarget="field-'+field+'-help"'));
assert.match(renderNew.toString(),/class="run-blueprint" tabindex="0" aria-label="Run plan"/);
''')


def test_new_load_results_keep_keyed_nodes_and_disclosures_during_updates():
    run_render_checks(r'''
const point={id:'load_primary_mixed_mixed_poisson_bursts_r10',type:'mixed',workload:'mixed',pattern:'poisson',rate:10,bursts:{size:32},groups:[{id:'shape1',shape:'short_short',status:'inconclusive',reason:'No timing',dispatched:2,scheduled:3,local_drops:1,not_sent:1,valid:2,required:5,lengths:{input_target:128,output_cap:64}}],status:'inconclusive',reason:'Incomplete delivery'};
const conditions=[{target:'local',condition:{id:point.id,check:'load'},load_point_result:point}];
const parent=$('#main');patchHTML(parent,renderLiveLoadResults({live_conditions:conditions}));
const article=$('[data-load-point="'+point.id+'"]'),group=$('[data-live-key="group:shape1"]'),details=$('details',article);details.open=true;
point.groups[0].dispatched=3;patchHTML(parent,renderLiveLoadResults({live_conditions:[...conditions,...conditions]}));
assert.equal($('[data-load-point="'+point.id+'"]'),article);assert.equal($('[data-live-key="group:shape1"]'),group);assert.equal($('details',article),details);assert(details.open);assert.equal(article.animationCalls.length,0);assert.equal($$('[data-load-point]').length,1);
const html=parent.innerHTML;for(const label of ['Requested / scheduled / dispatched','Local not-sent / late / not offered','Correct work meeting limits','First answer p50 / p95','Generation tokens/sec','Throughput window','Unknown','not reported','Poisson + bursts'])assert(html.includes(label),label);
''',dom=True)


def test_dormant_settings_keep_valid_edits_and_omit_invalid_drafts():
    browser_logic(FORM+r'''
form.elements.load_workloads_interference_max_slowdown.value='2';
form.elements.load_patterns_poisson_seed.value='57';
let read=readForm(false).config;assert.equal(read.load_test.workloads.interference.max_slowdown,2);assert.equal(read.load_test.patterns.poisson.seed,57);
form.elements.load_workloads_interference_max_slowdown.value='0';form.elements.load_patterns_poisson_seed.value='-1';
read=readForm(false).config;assert.equal(read.load_test.workloads.interference.max_slowdown,config.load_test.workloads.interference.max_slowdown);assert.equal(read.load_test.patterns.poisson.seed,42);
// Valid settings of an unselected check remain an editable saved draft.
controls.find(input=>input.name==='check').value='access';form.elements.load_profile_long_output_tokens.value='1024';
assert.equal(readForm(false).config.load_test.profile.long.output_tokens,1024);
configChanged=()=>{};updateBounds=()=>{};
const panel=nodes['#load-panel-combinations'];panel.hidden=false;
await listeners.change({target:{name:'load_workloads_combinations_enabled',checked:false,dataset:{loadEnable:'combinations'},matches:q=>q==='[data-load-enable]',closest:()=>null}});assert(panel.hidden);
''')


def test_plan_viewport_tracks_the_actual_bottom_dock():
    browser_logic(r'''
let height=82,disconnects=0,observed,callback;
nodes['#run-blueprint']={style:{setProperty(name,value){this[name]=value;}}};nodes['.setup-navigation']={getBoundingClientRect:()=>({height})};
ResizeObserver=class{constructor(fn){callback=fn;}observe(node){observed=node;}disconnect(){disconnects++;}};
watchPlanViewport();assert.equal(observed,nodes['.setup-navigation']);assert.equal(nodes['#run-blueprint'].style['--run-dock-clearance'],'112px');
height=140;callback();assert.equal(nodes['#run-blueprint'].style['--run-dock-clearance'],'170px');watchPlanViewport();assert.equal(disconnects,1);
''')


def test_result_comparisons_burst_evidence_and_accepted_values_keep_their_meaning():
    run_render_checks(r'''
const group={id:'long',shape:'long_short',status:'inconclusive',requested:null,scheduled:2,dispatched:2,lengths:{input_target:2048,output_cap:64,input_tokens:{median:null},output_tokens:{median:32,min:30,max:34},unknown_input_count:2,input_shortfall_tokens:[null,null]},coverage:{threshold_tokens:410,output_covered:1,input_verified:0,required:5},completed:2,valid:2,required:5,cancelled:1};
const point={id:'comparison',type:'interference',workload:'interference',pattern:'interference',level:4,windows:2,groups:[group],status:'inconclusive',delivered_window_seconds:20,configured_window_seconds:10,interference:{baseline:{matched_count:5,completion_median_ms:20,longest_stream_pause_ms:1},probe:{matched_count:5,completion_median_ms:30,longest_stream_pause_ms:3},slowdown:1.5,matched_measurement_windows:[{},{}],actual_overlap_request_ids:['a','b'],extra_pause_ms:2,triggered:1}};
let html=renderLoadTestResults({points:[point],highest_accepted:[]});assert.match(html,/2 matched measurement windows/);assert.match(html,/2 actual overlapping requests/);assert.match(html,/Slowdown: 1.5×/);assert.match(html,/20 seconds delivered · 10 seconds configured per window/);assert.doesNotMatch(html,/NaN/);assert.match(html,/not reported \/ 2,048/);
const rate={...point,id:'arrival',source:'primary',type:'combinations',workload:'long_short',shape:'long_short',pattern:'poisson',level:null,rate:10,bursts:{size:32},interference:null,burst_delivery:[{index:0,scheduled:32,dispatched:24,not_sent:8,spread_ms:5,completion_p50_ms:40}],response_by_origin:{arrival:{dispatched:10,completion_p50_ms:20,failed:0},burst:{dispatched:24,completion_p50_ms:40,failed:1}}};
html=renderLoadTestResults({points:[rate],highest_accepted:[{source:'primary',type:'combinations',workload:'long_short',pattern:'poisson',bursts:true,label:'Highest accepted request rate',value:10,qualification:'highest tested, not an absolute capacity limit'}]});
for(const text of ['Highest accepted request rate (tested): 10','Long input → Short output','Poisson + bursts','Burst delivery and degradation','Dispatch spread','Arrival and burst response degradation','highest tested, not an absolute capacity limit'])assert(html.includes(text),text);
assert.match(renderLoadTestResults({points:[rate],highest_accepted:[]}),/Highest accepted request rate \(tested\): Unconfirmed/);
const conditions=[{target:'local',condition:{id:'load_primary_combinations_long_short_poisson_bursts_r10',check:'load'}},{target:'local',condition:{id:'load_primary_combinations_long_short_constant_r10',check:'load'}}];
html=renderTests([],{conditions,live:true});assert.match(html,/Poisson \+ bursts · 10 requests\/second/);assert.match(html,/Constant · 10 requests\/second/);assert.equal((html.match(/data-condition-load-type="combinations"/g)||[]).length,2);
''')
