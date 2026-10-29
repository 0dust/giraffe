"""Latest PR 53 feedback: preparation, limits and identifiable conditions."""

from pathlib import Path

from tests.test_concurrent_load_ui_review import FORM
from tests.test_run_limits_phase6_ui import rendered_form
from tests.test_ux_catalogue import browser_logic
from tests.test_ux_results import run_render_checks


def test_shared_timeout_and_load_override_are_visible_and_independent():
    markup = rendered_form()
    advanced = markup.split('id="setup-advanced"', 1)[1]
    assert 'name="request_timeout_seconds"' in advanced
    assert 'Request timeout (seconds)</label>' in advanced
    browser_logic(FORM + r'''
const shared=renderSharedRunSettings(config),load=renderLoadEditor(config);
assert.match(shared,/Max requests/);assert.match(shared,/Max duration/);assert.match(shared,/Max concurrency/);
assert.doesNotMatch(shared,/Request timeout/);
assert.match(field('request_timeout_seconds','Request timeout (seconds)',config.request_timeout_seconds,{type:'number'}),/<label for="field-request_timeout_seconds">Request timeout \(seconds\)<\/label>/);
assert.match(load,/name="load_request_timeout_seconds"/);
assert.match(load,/Request timeout \(seconds\)/);
assert.match(load,/Send new requests for \(seconds\)/);
assert.doesNotMatch(load,/load_schedule_finish_seconds|Wait for unfinished|Duration per workload/);
assert.equal(settingDestination('request_timeout_seconds').name,'request_timeout_seconds');
form.elements.request_timeout_seconds.value='240';
form.elements.load_request_timeout_seconds.value='75';
const saved=readForm(false).config;
assert.equal(saved.request_timeout_seconds,240);assert.equal(saved.load_test.request_timeout_seconds,75);
assert.match(effectiveCheckSummary(bootstrap.checks.find(c=>c.id==='load'),saved),/Request timeout 1m 15s/);
assert.doesNotMatch(effectiveCheckSummary(bootstrap.checks.find(c=>c.id==='access'),saved),/Request timeout/);
''')


def test_preparation_precedes_report_tabs_and_keeps_failure_distinct():
    run_render_checks(r'''
state.detail={id:'preparing',state:'running',config:{targets:[{name:'local',model:'m'}]},events:[],progress:{event:'preparation_started',target:'local',elapsed_seconds:4,preparation:{step:'Loading matching tokenizer',status:'running',started_elapsed_seconds:4,elapsed_seconds:0}}};
renderLive();let html=nodes['#main'].innerHTML;
assert(html.indexOf('run-preparation')<html.indexOf('report-toolbar'));
assert.match(html,/Preparing test traffic/);assert.match(html,/Loading matching tokenizer/);
assert.match(html,/Test traffic for this preparation has not started/);
const clock=[...state.preparationClocks.values.values()][0];clock.at-=9000;
assert.match(renderPreparation(),/9s/);
state.detail.state='completed';state.detail.progress={};state.detail.report={observations:{targets:{local:{preparation:{step:'Local tokenization',status:'unavailable',elapsed_seconds:2,reason:'Matching tokenizer not cached'}}}}};
html=renderPreparation();assert.match(html,/Preparation unavailable/);assert.match(html,/Matching tokenizer not cached/);
assert.match(html,/The affected test traffic did not run/);assert.doesNotMatch(html,/Preparing test traffic/);
state.detail.report.observations.targets.local.preparation={scenario:'performance_evidence_probe',step:'Streamed token and usage capability',status:'unavailable',elapsed_seconds:1,reason:'Endpoint does not provide token boundaries'};
html=renderPreparation();assert.match(html,/Optional measurements unavailable/);assert.doesNotMatch(html,/test traffic did not run/);
''')


def test_condition_identity_and_effective_limits_are_available_from_status_strip():
    run_render_checks(r'''
state.detail={id:'small',state:'running',config:{max_duration_seconds:600}};
const row={condition:{id:'load_primary_combinations_short_short_concurrent_c4',check:'load',parameters:{concurrency:4,request_timeout_seconds:75,duration_seconds:20}},planned:null,planned_condition:true,started:0};
const html=renderTests([],{live:true,conditions:[row]});
assert.match(html,/condition-strip-row/);assert.match(html,/Conditions/);assert.match(html,/0 requests/);assert.doesNotMatch(html,/\/\? requests/);assert.match(html,/p95 —/);
assert.doesNotMatch(html,/condition-spot|reticulate|<svg/);
const view=[...conditionViews.values()][0];
assert.match(conditionIdentity(view),/Condition #01/);assert.match(conditionLimits(view),/Request timeout: 1m 15s/);assert.match(conditionLimits(view),/Send new requests for: 20s/);
assert.match(html,/Short input \/ short output · 4 at once/);
renderTests([],{live:true,conditions:[row,{...row,condition:{id:'extra',label:'Extra recorded work'},planned_condition:false}]});
assert.match(conditionIdentity([...conditionViews.values()][1]),/Additional recorded condition/);
''')
    css = (Path(__file__).parents[1] / 'src/giraffe/static/styles.css').read_text()
    assert '.condition-strip-row' in css
    assert '.request-evidence-row' in css


def test_preparation_history_retains_input_failure_after_later_success_and_feed_eviction():
    run_render_checks(r'''
const failed={scenario:'context_preparation',step:'Preparing context input locally',status:'unavailable',elapsed_seconds:2,reason:'Context input could not be sized'};
const success={scenario:'load_preparation_short_short_answer',step:'Preparing load input locally',status:'completed',elapsed_seconds:1};
const optional={scenario:'runtime_metrics_discovery',step:'Runtime metrics',status:'unavailable',reason:'No metrics exporter'};
state.detail={id:'history',state:'running',events:[],progress:{event:'request_completed'},live_preparations:[{target:'local',...failed},{target:'local',...optional},{target:'local',...success}]};
let html=renderPreparation();
assert.equal((html.match(/class="preparation-status"/g)||[]).length,2);
assert.match(html,/Preparation complete/);assert.match(html,/Preparation unavailable/);
assert.match(html,/Context input could not be sized/);assert.match(html,/The affected test traffic did not run/);
assert.doesNotMatch(html,/No metrics exporter/);
state.detail={id:'history',state:'completed',events:[],report:{observations:{targets:{local:{preparations:{context_preparation:failed,runtime_metrics_discovery:optional,load_preparation_short_short_answer:success},preparation:success}}}}};
html=renderPreparation();
assert.equal((html.match(/class="preparation-status"/g)||[]).length,2);
assert.match(html,/<details class="preparation-status">/);
assert.match(html,/Context input could not be sized/);assert.match(html,/Preparation complete/);
state.detail.id='another-run';state.detail.report={};assert.equal(renderPreparation(),'');
''')


def test_recorded_termination_reason_retains_exact_limit_and_detail():
    run_render_checks(r'''
const html=renderRequestTermination({request_timeout_seconds:300,termination_reason:'run_duration_limit',termination_limit_seconds:12.5,error:'Remaining run traffic budget exhausted'});
assert.match(html,/Configured request timeout/);assert.match(html,/5m 0s/);
assert.match(html,/Run traffic duration limit reached/);assert.match(html,/12.5s/);
assert.match(html,/Remaining run traffic budget exhausted/);
assert.equal(renderRequestTermination({}), '');
''')


def test_estimated_input_notice_survives_later_optional_preparation():
    run_render_checks(r'''
state.detail={id:'estimated',state:'completed',live_preparations:[
{target:'fresh',scenario:'load_preparation_short_short_answer',step:'Preparing input locally',status:'completed',estimated:true,reason:'Input size is estimated at 4 characters per token.'},
{target:'fresh',scenario:'runtime_metrics_discovery',step:'Runtime metrics',status:'completed'}]};
const html=renderPreparation();
assert.match(html,/Preparation complete · estimated input size/);
assert.match(html,/Input size is estimated at 4 characters per token/);
assert.doesNotMatch(html,/The affected test traffic did not run/);
''')


def test_tokenizer_and_metrics_are_in_advanced_with_probe_details_in_popover():
    connection = rendered_form()
    primary, advanced = connection.split('<details class="setup-advanced"', 1)
    assert 'name="tokenizer"' not in primary
    assert 'name="metrics_url"' not in primary
    assert advanced.count('name="tokenizer"') == 1
    assert 'Token ID / tokenizer</label>' in advanced
    assert 'aria-controls="tokenizer-options"' in advanced
    assert 'role="combobox"' in advanced and 'role="listbox"' in advanced
    assert 'value="Automatic"' in advanced and 'data-token-id="builtin"' in advanced
    assert 'name="metrics_url"' in advanced and 'Server metrics URL' in advanced
    assert 'up to 3 one-token compatibility probes' not in primary
    assert 'id="field-preparation-help"' in advanced and 'popover="auto"' in advanced
    browser_logic(r'''
const gpu=renderTestCard(bootstrap.checks.find(c=>c.id==='gpu'),config);
assert.doesNotMatch(gpu,/name="metrics_url"|name="host_metrics_url"/);
assert.match(gpu,/Automatic metrics discovery/);assert.doesNotMatch(gpu,/Metrics URL needed/);
''')


def test_form_preserves_same_box_tokenizer_selection():
    browser_logic(FORM + r'''
form.elements.tokenizer={value:'Automatic'};
assert.equal(readForm(false).config.targets[0].tokenizer,'auto');
form.elements.tokenizer.value='builtin';
assert.equal(readForm(false).config.targets[0].tokenizer,'builtin');
form.elements.tokenizer.value='qwen2.5-7b';
assert.equal(readForm(false).config.targets[0].tokenizer,'qwen2.5-7b');
assert.equal(readForm(false).config.targets[0].model,state.config.targets[0].model);
''')


def test_builtin_notice_survives_later_optional_preparation():
    run_render_checks(r'''
state.detail={id:'builtin',state:'completed',live_preparations:[
{target:'fresh',scenario:'load_preparation_short_short_answer',step:'Preparing input locally',status:'completed',builtin:true,reason:'Using built-in tiktoken o200k_base. Reference counts, not server usage.'},
{target:'fresh',scenario:'runtime_metrics_discovery',step:'Runtime metrics',status:'completed'}]};
const html=renderPreparation();
assert.match(html,/Preparation complete · built-in tokenizer/);
assert.match(html,/Local input sizing complete/);assert.doesNotMatch(html,/Reference counts, not server usage/);
''')


TOKENIZER_FORM = FORM + r'''
form.elements.tokenizer={name:'tokenizer',value:'Automatic'};
nodes['#tokenizer-status']={textContent:'',classList:{toggle(){}}};
nodes['[data-action="retry-tokenizer"]']={hidden:true};
syncSetupStep=configChanged=updateBounds=()=>{};
'''


def test_token_id_checks_immediately_and_pins_resolved_name_for_launch():
    browser_logic(TOKENIZER_FORM + r'''
let called=null;
api=async(path,options)=>{called={path,body:JSON.parse(options.body)};return {status:'ready',tokenizer:'Qwen/Qwen2.5-7B',message:'Ready: Qwen/Qwen2.5-7B'};};
scheduleTokenizerSelection({immediate:true});
assert.equal(tokenizerSetupReady(),false);
assert.equal(nodes['#tokenizer-status'].textContent,'');
const pending=state.tokenizerSelection;
await resolveTokenizerSelection(pending.key,state.tokenizerSequence,state.routeVersion);
assert.equal(called.path,'/api/targets/tokenizer');
assert.deepEqual(called.body,{model:form.elements.model.value,token_id:'auto'});
assert.equal(tokenizerSetupReady(),true);
assert.equal(nodes['#tokenizer-status'].textContent,'');
const draft=readForm(false).config;
assert.equal(draft.targets[0].tokenizer,'auto');
assert.equal(pinPreparedTokenizer(draft).targets[0].tokenizer,'Qwen/Qwen2.5-7B');
assert.equal(draft.targets[0].tokenizer,'auto');
''')


def test_token_id_fallback_has_one_muted_line_and_uses_builtin_for_launch():
    browser_logic(TOKENIZER_FORM + r'''
api=async()=>({status:'fallback',tokenizer:'builtin',message:'Built-in o200k_base will be used. Enter a model name in Token ID to try again.'});
scheduleTokenizerSelection();
await resolveTokenizerSelection(state.tokenizerSelection.key,state.tokenizerSequence,state.routeVersion);
assert.equal(tokenizerSetupReady(),true);
assert.equal(nodes['#tokenizer-status'].textContent,'Using the built-in tokenizer.');
const draft=readForm(false).config;
assert.equal(draft.targets[0].tokenizer,'auto');
assert.equal(pinPreparedTokenizer(draft).targets[0].tokenizer,'builtin');
assert.equal(draft.targets[0].tokenizer,'auto');
form.elements.tokenizer.value='qwen2.5-7b';
scheduleTokenizerSelection();
assert.equal(tokenizerSetupReady(),false);
assert.equal(pinPreparedTokenizer(readForm(false).config).targets[0].tokenizer,'builtin');
assert.equal(JSON.parse(state.tokenizerSelection.key).token_id,'qwen2.5-7b');
''')


def test_old_token_id_response_cannot_overwrite_new_selection():
    browser_logic(TOKENIZER_FORM + r'''
let finish;
api=()=>new Promise(resolve=>{finish=resolve;});
scheduleTokenizerSelection();
const old=resolveTokenizerSelection(state.tokenizerSelection.key,state.tokenizerSequence,state.routeVersion);
form.elements.tokenizer.value='builtin';scheduleTokenizerSelection();
finish({status:'ready',tokenizer:'obsolete/model',message:'Wrong result'});await old;
assert.equal(state.tokenizerSelection.status,'pending');
assert.equal(JSON.parse(state.tokenizerSelection.key).token_id,'builtin');
assert.doesNotMatch(nodes['#tokenizer-status'].textContent,/Wrong result/);
''')


def test_token_id_service_error_silently_uses_builtin_for_launch():
    browser_logic(TOKENIZER_FORM + r'''
api=async()=>{throw new Error('Encoding download failed');};
scheduleTokenizerSelection();
await resolveTokenizerSelection(state.tokenizerSelection.key,state.tokenizerSequence,state.routeVersion);
assert.equal(tokenizerSetupReady(),true);
assert.equal(nodes['#tokenizer-status'].textContent,'Using the built-in tokenizer.');
assert.equal(nodes['[data-action="retry-tokenizer"]'].hidden,true);
assert.equal(pinPreparedTokenizer(readForm(false).config).targets[0].tokenizer,'builtin');
''')
