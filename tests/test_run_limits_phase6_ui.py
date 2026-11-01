"""Run limits, plan, check settings and the budget-free live view use one config."""

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from giraffe.capabilities import capabilities
from giraffe.models import RunConfig
from giraffe.planning import request_plan
from tests.test_ux_catalogue import browser_logic
from tests.test_web import browser


STATIC = Path(__file__).parents[1] / 'src/giraffe/static'


class Markup(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.nodes = []
        self.stack = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        node = {'tag': tag, **dict(attrs), 'parents': self.stack[:], 'text': ''}
        self.nodes.append(node)
        if tag not in {'input', 'br', 'hr', 'img', 'meta', 'link', 'path', 'rect', 'circle'}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]['tag'] == tag:
                self.stack = self.stack[:index]
                break

    def handle_data(self, data):
        for node in self.stack:
            node['text'] += data

    def named(self, name):
        return [node for node in self.nodes if node.get('name') == name]


def rendered_form(config=None):
    return browser_logic(r"""
nodes['#main']={};nodes['#config-editor']={};
const elements=new Proxy({}, {get(target,name){return target[name] ||= {dataset:{},value:''};}});
nodes['#new-run-form']={elements};
state.jsonDraft=state.jsonBase='synced';
syncTestControls=updateBounds=updateRestart=syncConfigView=syncPackControls=renderRequestPreview=syncSetupStep=renderModelDiscovery=()=>{};
renderNew();console.log(JSON.stringify(nodes['#main'].innerHTML));
""", config)


def test_run_limit_value_controls_have_distinct_accessible_labels():
    tree = Markup(rendered_form())
    for name, label, hidden in [('max_requests', 'Request count', True),
                                ('max_duration_seconds', 'Duration', True),
                                ('concurrency', 'Simultaneous request count', True)]:
        control, = tree.named(name)
        value_label, = [node for node in tree.nodes if node['tag'] == 'label' and node.get('for') == control['id']]
        assert value_label['text'] == label
        assert ('sr-only' in value_label.get('class', '').split()) == hidden
        toggle, = tree.named(name + '_enabled')
        toggle_label = next(node for node in toggle['parents'] if node['tag'] == 'label')
        assert toggle_label['text'] == ('Maximum requests' if name == 'max_requests'
                                        else 'Maximum duration' if name == 'max_duration_seconds' else 'Simultaneous limit')
        assert toggle['aria-controls'] == control['id']
        assert control['aria-describedby'] == control['id'] + '-hint'


def test_a1_a3_limits_dom_order_visible_headings_and_unique_check_owned_fields():
    markup = rendered_form()
    tree = Markup(markup)
    assert markup.index('id="shared-settings"') < markup.index('id="test-selection"')
    headings = {node['text'].strip(): node for node in tree.nodes if node['tag'] == 'h2'}
    assert headings['Run limits'].get('class') != 'sr-only'
    assert headings['Choose your checks'].get('class') != 'sr-only'
    assert 'order: -1' not in (STATIC / 'styles.css').read_text()
    limits = next(node for node in tree.nodes if node.get('id') == 'shared-settings')
    controls = [node['name'] for node in tree.nodes if node.get('name') and limits in node['parents']]
    assert set(controls) == {'max_requests', 'max_requests_enabled', 'concurrency', 'concurrency_enabled',
                             'max_duration_seconds', 'max_duration_seconds_enabled', 'max_duration_unit',
                             'request_timeout_seconds', 'max_memory_mb'}
    for check in capabilities()['checks']:
        for name in check['option_fields'] + ['limit_' + name for name in check['limit_fields']]:
            fields = tree.named(f'test_{check["id"]}_{name}')
            assert len(fields) == 1, (check['id'], name)
            assert any(parent.get('data-test-editor') == check['id'] for parent in fields[0]['parents'])
    for name in ['profile_sessions_concurrency', 'profile_sessions_max_tokens', 'profile_consistency_levels',
                 'load_request_timeout_seconds']:
        assert len(tree.named(name)) == 1
    assert not tree.named('sessions_max_tokens')
    stream, = tree.named('stream')
    assert any(parent.get('id') == 'setup-panel-1' for parent in stream['parents'])
    regression, = tree.named('limit_regression_percent')
    assert any(parent.get('id') == 'setup-panel-3' for parent in regression['parents'])
    assert len(tree.named('request_timeout_seconds')) == 1
    for name in ['samples', 'max_output_tokens', 'context_limit']:
        assert not tree.named(name)
    browser_logic("nodes['#setup-panel-1']={};nodes['#setup-title']={};state.setupStep=2;"
                  "syncSetupIdentity=()=>{};syncSetupStep=syncSetupStep;"
                  "assert.match(syncSetupStep.toString(),/Set limits and choose checks/);")


def test_a2_exact_open_rows_and_outer_state_survive_repeated_refreshes():
    browser_logic(r"""
const rows=['one','two','three'];let generation=[];
function node(id,open=false){return {dataset:id?{planRow:id}:{},open,querySelector:()=>({focus(){}})};}
const element={outer:node(null,false),assumptions:node(null,false),rows:rows.map(id=>node(id,id!=='two')),
 querySelector(q){return q==='.workload-breakdown'?this.outer:q==='.estimate-assumptions'?this.assumptions:null;},
 querySelectorAll(q){return q==='details[data-plan-row], details[data-plan-check]'?this.rows:q.includes('[data-plan-check]')?[]:q==='details[data-plan-row][open]'?this.rows.filter(row=>row.open):this.rows;},
 set innerHTML(value){this.outer=value.includes('workload-breakdown')?node(null):null;this.assumptions=value.includes('estimate-assumptions')?node(null):null;this.rows=value.includes('data-plan-row')?rows.map(id=>node(id)):[];generation.push(value);}};
nodes['#run-blueprint']=element;
const plan={estimated_seconds:1,estimated_low_seconds:0,estimated_high_seconds:2,time_estimate_assumptions:['Observed batch time.'],rows:rows.map(id=>({id,title:id,requests_per_target:1,summary:'kept',source:'built-in'}))};
syncRunBlueprint(config,plan);
assert.equal(element.outer.open,false);assert.equal(element.rows.filter(row=>row.open).map(row=>row.dataset.planRow).join(','),'one,three');
element.outer.open=true;element.rows[0].open=false;element.rows[1].open=true;element.assumptions.open=true;
syncRunBlueprint(config,null);assert.equal(element.outer,null);assert.equal(element.rows.length,0);
syncRunBlueprint(config,plan);
assert.equal(element.outer.open,true);assert.equal(element.assumptions.open,true);
assert.equal(element.rows.filter(row=>row.open).map(row=>row.dataset.planRow).join(','),'two,three');
assert.equal(generation.length,3);assert.doesNotMatch(generation[2],/planCheck=undefined/);
""")


def test_a4_each_nullable_limit_restores_only_session_value_and_time_units():
    browser_logic(r"""
state.jsonDraft=state.jsonBase='synced';const elements={};
const enabledMarkup=renderSharedRunSettings({...config,max_requests:7,max_duration_seconds:7,concurrency:7});
for(const name of ['max_requests','max_duration_seconds','concurrency']){assert(enabledMarkup.includes('id="field-'+name+'-hint"></span>'));assert(enabledMarkup.includes('aria-describedby="field-'+name+'-hint"'));}
for(const name of ['max_requests','max_duration_seconds','concurrency']){
 elements[name]={value:'',disabled:true,required:false,removeAttribute(){}};
 elements[name+'_enabled']={checked:false};nodes['#field-'+name+'-hint']={};
}
nodes['#new-run-form']={elements};configChanged=updateBounds=()=>{};
for(const name of ['max_requests','max_duration_seconds','concurrency']){
 const toggle={name:name+'_enabled',checked:true,dataset:{limitToggle:name},matches:()=>false};
 await listeners.change({target:toggle});assert.equal(elements[name].value,'');assert.equal(elements[name].required,true);
 elements[name].value='7';toggle.checked=false;
 await listeners.change({target:toggle});assert.equal(elements[name].value,'');assert.equal(elements[name].disabled,true);
 assert.equal(nodes['#field-'+name+'-hint'].textContent,'Not set');
 toggle.checked=true;await listeners.change({target:toggle});assert.equal(elements[name].value,'7');
 const values=new Map([[name+'_enabled','off']]);const disabled=configureRunLimits(config,values);
 assert.equal(disabled[name],null);
 assert.equal(JSON.parse(JSON.stringify(portableConfig({config:disabled,mode:'normal'})))[name],null);
}
const values=new Map([['max_duration_seconds_enabled','on'],['max_duration_seconds','2'],['max_duration_unit','h']]);
assert.equal(configureRunLimits(config,values).max_duration_seconds,7200);
assert.equal(sharedLimitsSummary({...config,max_requests:null,max_duration_seconds:null,concurrency:null}),'Requests not set · Duration not set · Simultaneous not set');
""")


async def test_a4_a17_null_limits_draft_export_and_json_form_derived_parity(tmp_path):
    cfg = RunConfig(targets=[{'name': 'local', 'url': 'http://localhost/v1', 'model': 'test'}],
                    checks=['correctness'], test_options={'correctness': {'max_output_tokens': 512}})
    values = cfg.model_dump(mode='json')
    async with browser(tmp_path) as client:
        responses = []
        for path in ['/api/config/draft', '/api/config/validate', '/api/config/preview']:
            response = await client.post(path, json=json.loads(json.dumps(values)))
            assert response.status_code == 200, response.text
            responses.append(response.json())
    for result in responses:
        assert all(result['config'][name] is None for name in ['max_requests', 'max_duration_seconds', 'concurrency'])
        assert result['config']['config_version'] == 2
        assert result['request_plan']['request_settings']['correctness']['max_output_tokens'] == 512
    for key in ['rows', 'request_settings', 'required_concurrency', 'planned_total']:
        assert responses[0]['request_plan'][key] == responses[1]['request_plan'][key] == responses[2]['request_plan'][key]


def test_a7_a16_plan_copy_observed_counts_bursts_and_distinct_controls():
    cfg=RunConfig(targets=[{'name':'local','url':'http://localhost/v1','model':'test'}],checks=['load'],load_test={'workloads':{'combinations':{'selected':['short_short']}},'patterns':{'concurrent':{'enabled':False},'constant':{'enabled':False},'poisson':{'enabled':True},'bursts':{'enabled':True,'first_seconds':0,'gap_seconds':1,'size':4},'rates':[2]},'schedule':{'duration_seconds':3}})
    browser_logic('const plan='+json.dumps(request_plan(cfg))+r''';
plan.estimated_seconds=3;plan.estimated_low_seconds=2;plan.estimated_high_seconds=4;plan.time_estimate_assumptions=['Finite batches use comparable observations.'];plan.confidence_details={insufficient_observations:['At least 150 comparable samples are required.']};plan.time_bound_reasons=['+ 10 requests at unknown speed'];
const markup=renderRunBlueprint(config,plan),limits=renderSharedRunSettings(config);assert.equal((markup.match(/Estimated duration:/g)||[]).length,1);assert.equal(((markup+limits).match(/aria-live="polite"/g)||[]).length,1);
for(const text of ['Confidence: low','Additional work at unknown speed','At least 150 comparable samples are required','Estimate assumptions','setup/connection, AI evaluation, report generation','Poisson + bursts','≈ 18'])assert(markup.includes(text),text);
assert.equal((markup.match(/Not set/g)||[]).length,3);assert.match(markup,/Maximum memory/);
const duration={id:'load_combinations',kind:'duration',requests_per_target:null};assert.equal(planRowCount(duration),'Duration-based');duration.estimated_requests_per_target=12;assert.equal(planRowCount(duration),'Duration-based · ~12 (estimate, from your last run)');
const editor=renderLoadEditor(config);for(const label of ['Requests per burst','Rates (requests/second)','First burst after (seconds)','Time between burst starts (seconds)','Concurrency ceiling · Run limits'])assert(editor.includes(label));assert.doesNotMatch(editor,/Pending queue|traffic_max_in_flight|profile_arrivals_max_in_flight/);
''',cfg)


def test_a25_budget_free_mosaic_uses_slots_not_unused_budget_and_eviction_summary():
    browser_logic(r"""
const records=[{slot:500,status:'running',scenario:'warm',target:'local'}];
state.detail={id:'live',live_request_summary:{evicted:500,statuses:{completed:499,running:1}}};
state.mosaicPage=0;state.mosaicFollow=false;
let markup=renderMosaic(records,{budget:null,live:true,budgetUsed:501});
assert.match(markup,/Earlier request evidence was evicted/);assert.match(markup,/500 earlier requests/);
assert.doesNotMatch(markup,/request-spot|Unused/);
state.mosaicPage=2;markup=renderMosaic(records,{budget:null,live:true,budgetUsed:999});
assert.match(markup,/501 started · 1 in live window/);
assert.equal((markup.match(/class="request-spot /g)||[]).length,1);
assert.match(markup,/data-live-slot="500"[^>]*tabindex="0"/);
assert.match(markup,/481–501 \/ 501/);assert.doesNotMatch(markup,/Unused|Budget slot|Request budget/);
nodes['#main']={};state.detail={id:'live',state:'running',config,live_requests:records,progress:{attempted:501,completed:500},events:[],live_conditions:[],live_request_summary:{evicted:500,statuses:{completed:500}}};
renderTests=renderAllRequests=renderLiveLoadResults=renderPhases=()=>'';renderLive();
assert.match(nodes['#main'].innerHTML,/500 requests finished · 0s · limit not set/);assert.match(nodes['#main'].innerHTML,/500 earlier requests outside the live window/);
assert.doesNotMatch(nodes['#main'].innerHTML,/budget-track|NaN|Infinity|undefined/);
""")


def test_every_native_bound_is_capability_sourced_including_first_output():
    browser_logic(r"""
bootstrap.limits['test_options.first_output.request_timeout_seconds'].maximum=777;
bootstrap.limits['load_test.profile.long.output_tokens'].maximum=987;
bootstrap.limits['sessions.concurrency'].maximum=23;
assert.match(renderTestCard(bootstrap.checks.find(check=>check.id==='first_output'),config),/name="test_first_output_request_timeout_seconds"[^>]*max="777"/);
assert.match(renderLoadEditor(config),/name="load_profile_long_output_tokens"[^>]*max="987"/);
assert.match(renderProfileEditor(bootstrap.checks.find(check=>check.id==='sessions'),config),/name="profile_sessions_concurrency"[^>]*max="23"/);
""")
    source = (STATIC / 'app.js').read_text() + (STATIC / 'form-config.mjs').read_text()
    source = source.replace('{s:1,min:60,h:3600}', '')  # Time-unit conversion is not a cap.
    assert not re.search(r'\b(?:min|max|maxLength):\s*\d', source)


def test_concurrent_load_timeout_uses_real_card_edit_and_read_form_path():
    from tests.test_concurrent_load_ui_review import FORM
    browser_logic(FORM + r'''
const input=form.elements.load_request_timeout_seconds;assert(input);input.value='90';listeners.input({target:input});
const result=readForm(false).config;assert.equal(result.load_test.request_timeout_seconds,90);assert.equal(result.test_options.load,undefined);assert.equal(result.request_timeout_seconds,config.request_timeout_seconds);
''')


def test_shared_request_defaults_and_sparse_sessions_show_effective_values():
    browser_logic(r"""
config.checks=['serving','first_output','sessions'];config.test_options.serving={request_timeout_seconds:60};
config.test_options.first_output={request_timeout_seconds:90};
config.sessions={profile:'performance',max_tokens:128};
const markup=renderSessionEditor(config);
assert.match(markup,/name="profile_sessions_concurrency"[^>]*value="2"/);
const first=bootstrap.checks.find(check=>check.id==='first_output');
assert.match(renderTestCard(first,config),/name="test_first_output_request_timeout_seconds"[^>]*value="60"[^>]*readonly/);
assert.match(renderTestCard(first,config),/Endpoint response owns this shared request setting/);
const input={name:'test_first_output_request_timeout_seconds',value:'30',dataset:{inherited:'true'}};
nodes['#new-run-form']={elements:{test_first_output_request_timeout_seconds:input}};
updateCheckFieldHints(config);assert.equal(input.value,60);assert.equal(input.readOnly,true);
config.test_options.serving.request_timeout_seconds=75;updateCheckFieldHints(config);assert.equal(input.value,75);
assert.equal(config.test_options.first_output.request_timeout_seconds,90);
""")


def test_ceiling_toggle_refreshes_inherited_check_values_and_reset_traffic_prefills():
    from tests.test_concurrent_load_ui_review import FORM
    browser_logic(FORM + r'''
const levels=form.elements.load_patterns_concurrent_levels;levels.dataset.inherited='true';
form.elements.concurrency.value='2';let read=readForm(false).config;syncLoadDisplay(read);assert.equal(levels.value,'1');assert.equal(Object.hasOwn(read.load_test.patterns.concurrent,'levels'),false);
form.elements.concurrency.value='64';read=readForm(false).config;syncLoadDisplay(read);assert.equal(levels.value,'1, 4');assert.equal(Object.hasOwn(read.load_test.patterns.concurrent,'levels'),false);
levels.value='8';listeners.input({target:levels});assert.equal(levels.dataset.inherited,'false');form.elements.concurrency.value='32';read=readForm(false).config;assert.equal(read.load_test.patterns.concurrent.levels.join(','),'8');
''')


def test_inherited_notice_has_check_inline_plan_and_explicit_use_action():
    cfg = RunConfig(targets=[{'name': 'local', 'url': 'http://localhost/v1', 'model': 'test'}],
                    checks=['correctness'], concurrency=2)
    plan = request_plan(cfg)
    browser_logic(r"""
const plan=PLAN;state.jsonDraft=state.jsonBase='synced';
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan};
const warning={hidden:true,dataset:{checkRuntimeWarning:'correctness'}};groups['[data-check-runtime-warning]']=[warning];
nodes['#field-test_correctness_concurrency']={getAttribute:()=>'',setAttribute(){}};
nodes['#field-test_correctness_concurrency-warning']={hidden:true};syncInlineErrors();
assert.equal(warning.hidden,false);assert.match(warning.innerHTML,/Raise limit/);assert.match(warning.innerHTML,/Use 2 for Answer correctness/);
assert.match(nodes['#field-test_correctness_concurrency-warning'].innerHTML,/inherits 4 at once/);
assert.match(renderRunBlueprint(config,plan),/Use 2 for Answer correctness/);
captureFormDraft=()=>{};let rendered=0;renderNew=()=>rendered++;configChanged=()=>{};focusSetting=path=>assert.equal(path,'test_options.correctness.concurrency');
await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'use-check-concurrency'}}:q==='[data-concurrency-check]'?{dataset:{concurrencyCheck:'correctness',concurrency:'2'}}:null}});
assert.equal(state.config.test_options.correctness.concurrency,2);assert.equal(state.config.concurrency,2);assert.equal(rendered,1);
""".replace('PLAN', json.dumps(plan)), cfg)


@pytest.mark.parametrize('check,settings,path', [
    ('correctness', {'test_options': {'correctness': {'concurrency': 64}}}, 'test_options.correctness.concurrency'),
    ('load', {'load_test': {'patterns':{'bursts':{'enabled':True,'size':64,'first_seconds':0}}}}, 'load_test.patterns.bursts.size'),
    ('load', {'load_test': {'patterns':{'concurrent':{'levels':[1,64]}}}}, 'load_test.patterns.concurrent.levels'),
    ('sessions', {'sessions': {'sessions': 32, 'concurrency': 32}}, 'sessions.concurrency'),
    ('consistency', {'consistency': {'levels': [1, 64]}}, 'consistency.levels'),
    ('load', {'load_test': {'workloads':{'interference':{'enabled':True}},'patterns':{'concurrent':{'levels':[1,64]}}}}, 'load_test.patterns.concurrent.levels'),
])
async def test_a8_a9_actual_conflicts_have_inline_plan_and_focus_destination(tmp_path, check, settings, path):
    ceiling = 4 if check == 'sessions' else 40
    values = {'config_version': 2, 'targets': [{'name': 'local', 'url': 'http://localhost/v1', 'model': 'test'}],
              'checks': [check], 'concurrency': ceiling, **settings}
    async with browser(tmp_path) as client:
        response = await client.post('/api/config/preview', json=values)
    assert response.status_code == 422, response.text
    errors = response.json()['detail']
    assert any(path in error['fields'] and 'concurrency' in error['fields'] for error in errors)
    if path == 'load_test.patterns.concurrent.levels':
        error, = errors
        assert error['msg'] == 'Concurrency levels exceed the run ceiling: 64 > 40. Update Run limits or lower the levels.'
        assert error['values'] == {path: [1, 64], 'concurrency': 40}
    base = RunConfig(targets=values['targets'], checks=[check], concurrency=ceiling)
    browser_logic(r"""
const errors=ERRORS,path=PATH;state.jsonDraft=state.jsonBase='synced';
state.requestPreview={status:'error',revision:state.configRevision,route:state.routeVersion,errors};
const markup=renderSharedRunSettings(config)+renderCheckSelection(config),destination=settingDestination(path);
assert(markup.includes('name="'+destination.name+'"'));
const inputs={};
for(const field of new Set(errors.flatMap(error=>error.fields))){
 const {name}=settingDestination(field),details={open:false,parentElement:null};
 const input={details,dataset:{},attributes:{},closest:()=>details,getAttribute(name){return this.attributes[name]||'';},setAttribute(name,value){this.attributes[name]=value;},removeAttribute(name){delete this.attributes[name];},focus(){this.focused=true;}};
 assert.equal((markup.match(new RegExp('id="field-'+name+'-error"','g'))||[]).length,1);
 inputs[name]=input;nodes['#field-'+name]=input;nodes['#field-'+name+'-error']={};
}
nodes['#new-run-form']={elements:inputs};syncInlineErrors();
for(const [name,input] of Object.entries(inputs)){
 assert.equal(input.attributes['aria-invalid'],'true');
 assert(input.attributes['aria-describedby'].includes('field-'+name+'-error'));
 assert.equal(nodes['#field-'+name+'-error'].hidden,false);
 assert.match(nodes['#field-'+name+'-error'].textContent,/above Simultaneous limit|Concurrency levels exceed the run ceiling|One burst exceeds the concurrency ceiling/);
 assert.equal(input.details.open,true);
}
const input=inputs[destination.name];
assert.match(renderRunBlueprint(config),/above Simultaneous limit|Concurrency levels exceed the run ceiling|One burst exceeds the concurrency ceiling/);
assert(renderRunBlueprint(config).includes('data-setting-path="'+path+'"'));
revealSetupStep=step=>assert.equal(step,2);revealCatalogueCheck=id=>assert.equal(id,destination.check);
focusSetting(path);assert(input.focused);
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan:{}};syncInlineErrors();
for(const [name,input] of Object.entries(inputs)){
 assert.equal(input.attributes['aria-invalid'],undefined);
 assert.equal(nodes['#field-'+name+'-error'].hidden,true);
 assert.equal(nodes['#field-'+name+'-error'].textContent,'');
}

""".replace('ERRORS', json.dumps(errors)).replace('PATH', json.dumps(path)), base)


def test_a15_multi_endpoint_budget_actions_in_sidebar_and_review_enable_checkbox():
    cfg = RunConfig(targets=[{'name': name, 'url': 'http://localhost/v1', 'model': 'test'} for name in ['one', 'two']],
                    checks=['access', 'correctness'], max_requests=2)
    plan = request_plan(cfg)
    browser_logic(r"""
const plan=PLAN;state.jsonDraft=state.jsonBase='synced';nodes['#request-plan']={};
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan,config};
renderRequestPlan();const sidebar=renderRunBlueprint(config,plan),review=nodes['#request-plan'].innerHTML;
for(const markup of [sidebar,review]){assert.match(markup,/Use \d+ total requests/);assert.match(markup,/incomplete coverage/);}
assert.match(sidebar,/Request budget split per endpoint: one: 1 · two: 1/);
const value=plan.request_warnings.flatMap(warning=>warning.actions||[]).find(action=>action.field==='max_requests').value;
const elements={max_requests:{value:'2',disabled:true,required:false},max_requests_enabled:{checked:false}};
nodes['#new-run-form']={elements};nodes['#config-error']={};
let refreshed=0;configChanged=()=>{};updateBounds=()=>refreshed++;
const original=JSON.stringify(config);
await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'use-request-budget'}}:q==='[data-budget]'?{dataset:{budget:String(value)}}:null}});
assert.equal(elements.max_requests.value,value);assert.equal(elements.max_requests.disabled,false);assert.equal(elements.max_requests.required,true);assert.equal(elements.max_requests_enabled.checked,true);
assert.equal(refreshed,1);assert.equal(JSON.stringify(config),original);
""".replace('PLAN', json.dumps(plan)), cfg)


def test_time_budget_action_preserves_minutes_and_enables_limit():
    browser_logic(r"""
state.config.max_duration_seconds=180;state.jsonDraft=state.jsonBase='synced';
const plan={time_warnings:[{actions:[{field:'max_duration_seconds',value:600}]}]};
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan,config:state.config};
const elements={max_duration_seconds:{value:'3',disabled:true,required:false},max_duration_seconds_enabled:{checked:false},max_duration_unit:{value:'min'}};
nodes['#new-run-form']={elements};nodes['#config-error']={};configChanged=updateBounds=()=>{};
await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'use-time-budget'}}:q==='[data-budget]'?{dataset:{budget:'600'}}:null}});
assert.equal(elements.max_duration_seconds.value,10);assert.equal(elements.max_duration_unit.value,'min');
assert.equal(elements.max_duration_seconds_enabled.checked,true);assert.equal(elements.max_duration_seconds.disabled,false);assert.equal(elements.max_duration_seconds.required,true);
""")


async def test_a8_stream_conflict_focuses_connect_and_retains_both_inline_destinations(tmp_path):
    values = {'config_version': 2, 'targets': [{'name': 'local', 'url': 'http://localhost/v1', 'model': 'test'}],
              'checks': ['tools'], 'stream': False, 'tool_checks': {'modes': ['stream']}}
    async with browser(tmp_path) as client:
        response = await client.post('/api/config/preview', json=values)
    assert response.status_code == 422
    browser_logic(r"""
state.config.stream=false;state.config.checks=['tools'];state.jsonDraft=state.jsonBase='synced';
const errors=ERRORS;state.requestPreview={status:'error',revision:state.configRevision,route:state.routeVersion,errors};
const elements={};for(const name of ['stream','profile_tools_modes']){
 const input={dataset:{},attributes:{},getAttribute(name){return this.attributes[name]||'';},setAttribute(name,value){this.attributes[name]=value;},focus(){this.focused=true;}};
 elements[name]=input;nodes['#field-'+name]=input;nodes['#field-'+name+'-error']={};
}
nodes['#new-run-form']={elements};syncInlineErrors();
assert.equal(elements.stream.attributes['aria-invalid'],'true');assert.equal(elements.profile_tools_modes.attributes['aria-invalid'],'true');
const steps=[];revealSetupStep=step=>steps.push(step);revealCatalogueCheck=check=>assert.equal(check,'tools');
focusSetting('stream');assert.equal(steps.at(-1),1);assert(elements.stream.focused);
focusSetting('tool_checks.modes');assert.equal(steps.at(-1),2);assert(elements.profile_tools_modes.focused);
assert.match(renderRunBlueprint(state.config),/Enable streaming on Connect/);
""".replace('ERRORS', json.dumps(response.json()['detail'])))


def test_sessions_reset_clears_per_check_overrides_without_changing_own_workload():
    browser_logic(r"""
state.config.test_options.sessions={request_timeout_seconds:90,limits:{latency_ms:10}};
state.config.sessions={...bootstrap.profile_defaults.sessions,sessions:7,max_tokens:512};state.jsonDraft=state.jsonBase='synced';
const original=JSON.stringify(state.config.sessions),mode={value:'custom'};
nodes['#new-run-form']={elements:{test_mode_sessions:mode}};
configChanged=syncTestControls=updateBounds=refreshInheritedFields=()=>{};
await listeners.click({target:{closest:q=>q==='[data-reset-test]'?{dataset:{resetTest:'sessions'}}:null}});
assert.equal(state.config.test_options.sessions,undefined);assert.equal(mode.value,'default');
assert.equal(JSON.stringify(state.config.sessions),original);
""")


def test_request_pack_override_error_is_inline_at_readonly_explanation_and_focusable():
    browser_logic(r"""
config.request_source='request_pack';config.request_pack={version:1,cases:[]};
config.test_options.correctness={samples:9};state.jsonDraft=state.jsonBase='synced';
const check=bootstrap.checks.find(check=>check.id==='correctness'),markup=renderTestCard(check,config);
assert.doesNotMatch(markup,/name="test_correctness_(samples|concurrency|max_output_tokens|request_timeout_seconds)/);
assert.match(markup,/id="field-request_pack_correctness" tabindex="-1"/);
assert.match(markup,/id="field-request_pack_correctness-error"/);
const input={dataset:{},setAttribute(){},focus(){this.focused=true;}};
nodes['#field-request_pack_correctness']=input;nodes['#field-request_pack_correctness-error']={};nodes['#new-run-form']={elements:{}};
state.requestPreview={status:'error',revision:state.configRevision,route:state.routeVersion,errors:[{fields:['test_options.correctness.samples'],msg:'Request-pack correctness uses its cases and default_concurrency; per-check overrides are not supported.'}]};
syncInlineErrors();assert.match(nodes['#field-request_pack_correctness-error'].textContent,/per-check overrides are not supported/);
revealSetupStep=step=>assert.equal(step,2);revealCatalogueCheck=id=>assert.equal(id,'correctness');focusSetting('test_options.correctness.samples');assert(input.focused);
assert.match(renderRunBlueprint(config),/per-check overrides are not supported/);
""")


def test_request_pack_inherited_notice_never_offers_a_forbidden_override():
    cfg = RunConfig(targets=[{'name': 'local', 'url': 'http://localhost/v1', 'model': 'test'}],
                    checks=['correctness'], concurrency=2, request_source='request_pack',
                    request_pack={'version': 1, 'cases': [{'fixture_id': 'one', 'scorer': 'exact', 'expected': 'ok',
                                                          'messages': [{'role': 'user', 'content': 'Say ok'}]}]})
    plan = request_plan(cfg)
    browser_logic(r"""
const plan=PLAN,markup=renderRunBlueprint(config,plan);
assert.match(markup,/inherits 4 at once/);assert.match(markup,/Raise limit/);assert.match(markup,/Edit default concurrency \(JSON\)/);
assert.doesNotMatch(markup,/data-action="use-check-concurrency"/);
assert.match(renderTestCard(bootstrap.checks.find(check=>check.id==='correctness'),config),/Concurrency: 2 \(default\)/);
assert.equal(Object.keys(config.test_options).length,0);
""".replace('PLAN', json.dumps(plan)), cfg)


def test_a17_plan_summary_announces_pending_error_valid_and_never_starts_stale():
    browser_logic(r"""
state.jsonDraft=state.jsonBase='synced';
state.requestPreview={status:'pending',revision:state.configRevision,route:state.routeVersion};
assert.match(renderRunBlueprint(config),/data-plan-state="pending" role="status" aria-live="polite"/);assert.equal(canStartRun(),false);
const plan={rows:[],errors:[{fields:['concurrency'],msg:'Concurrency conflict'}]};
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan};
assert.match(renderRunBlueprint(config,plan),/data-plan-state="error"/);assert.equal(canStartRun(),false);
plan.errors=[];assert.match(renderRunBlueprint(config,plan),/data-plan-state="valid"/);assert.equal(canStartRun(),true);
state.configRevision++;assert.equal(canStartRun(),false);assert.equal(currentRequestPlan(),null);
state.requestPreview.revision=state.configRevision;state.jsonDraft='unapplied';assert.equal(canStartRun(),false);assert.equal(currentRequestPlan(),null);
""")


async def test_a12_a17_json_conflict_and_duration_correction_share_form_api_data(tmp_path):
    cfg=RunConfig(targets=[{'name':'local','url':'http://localhost/v1','model':'test'}],checks=['load'],load_test={'workloads':{'combinations':{'selected':['short_short']}},'patterns':{'concurrent':{'enabled':False},'constant':{'enabled':True},'rates':[250]},'schedule':{'duration_seconds':400}})
    invalid=cfg.model_dump(mode='json')
    invalid['load_test']['schedule']['duration_seconds']=3600
    async with browser(tmp_path) as client:
        rejected=await client.post('/api/config/draft',json=invalid)
        accepted=await client.post('/api/config/draft',json=cfg.model_dump(mode='json'))
    assert rejected.status_code==422 and accepted.status_code==200
    error, = rejected.json()['detail']
    assert error['values']['planned_arrivals'] == 900000
    assert error['values']['maximum_planned_arrivals'] == 100000
    assert error['values']['longest_stage_seconds'] == 400
    assert error['actions'] == [
        {'label': 'Use 400 seconds', 'field': 'load_test.schedule.duration_seconds', 'value': 400},
        {'label': 'Edit Rates (requests/second)', 'field': 'load_test.patterns.rates'},
    ]
    browser_logic('const errors='+json.dumps(rejected.json()['detail'])+',valid='+json.dumps(accepted.json())+',invalid='+json.dumps(invalid)+r''';
state.configView='json';state.jsonBase=JSON.stringify(config);state.jsonDraft=JSON.stringify(invalid);
const editor={dataset:{},attributes:{},getAttribute(k){return this.attributes[k]||'';},setAttribute(k,v){this.attributes[k]=v;},removeAttribute(k){delete this.attributes[k];},focus(){this.focused=true;}};nodes['#config-editor']=editor;nodes['#field-config_editor-error']={};nodes['#request-plan']={};nodes['#config-error']={};
readForm=()=>({config:structuredClone(state.config)});captureFormDraft=invalidateModelDiscovery=toast=()=>{};
renderNew=()=>{state.jsonDraft=state.jsonBase=JSON.stringify(state.config);editor.value=state.jsonDraft;};
let sent=0;api=async(path,options)=>{sent++;const body=JSON.parse(options.body);if(body.load_test.schedule.duration_seconds===3600)throw Object.assign(Error(errors[0].msg),{errors});return valid;};
await applyConfig();assert.equal(sent,1);assert.equal(hasJsonChanges(),true);assert.equal(canStartRun(),false);assert.equal(editor.attributes['aria-invalid'],'true');assert.equal(nodes['#field-config_editor-error'].textContent,errors[0].msg);assert.match(nodes['#field-config_editor-error'].textContent,/900,000 planned arrivals.*at most 100,000.*longest stage is 400 seconds/);
focusSetting('load_test.patterns.rates');assert(editor.focused);assert.equal(state.configView,'json');
state.jsonDraft=JSON.stringify(valid.config);await applyConfig();assert.equal(sent,2);assert.equal(state.config.load_test.schedule.duration_seconds,400);assert.equal(hasJsonChanges(),false);assert.equal(currentRequestPlan(),null);
await refreshRequestPreview(undefined,{draft:true});assert.equal(canStartRun(),true);assert.equal(currentSettingErrors().length,0);assert.equal(editor.attributes['aria-invalid'],undefined);
state.jsonBase='old';state.jsonDraft='new';state.jsonValidation={draft:'new',revision:state.configRevision,route:state.routeVersion,errors};assert.equal(currentSettingErrors().length,1);configChanged();assert.equal(currentSettingErrors().length,0);assert.equal(canStartRun(),false);
''',cfg)
