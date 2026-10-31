"""The owner's Connect settings block and its existing configuration contract."""

import re
from pathlib import Path

from tests.test_run_limits_phase6_ui import Markup, rendered_form


CONNECT_FIELDS = [
    ('tokenizer', 'Token ID / tokenizer'),
    ('metrics_url', 'Server metrics URL (optional)'),
    ('host_metrics_url', 'Host metrics URL (optional)'),
    ('stream', 'Streaming'),
]


def test_connect_settings_keep_tokenizer_full_width_and_metrics_stream_in_grid():
    html = rendered_form()
    tree = Markup(html)
    auth = next(node for node in tree.nodes if node.get('id') == 'connection-auth')
    summary, = [node for node in tree.nodes if node['tag'] == 'summary' and node['parents'][-1] is auth]
    assert summary['class'] == 'api-key-row'
    spans = [node for node in tree.nodes if node['tag'] == 'span' and summary in node['parents']]
    assert [(node['class'], node['text']) for node in spans] == [
        ('api-key-label', 'API key (optional)'), ('row-value api-key-value', 'Not set'),
        ('row-action api-key-action', 'EditClose'),
        ('when-closed', 'Edit'), ('when-open', 'Close'),
    ]
    positions = [html.index('id="connection-auth"')]
    grid = None
    for name, label in CONNECT_FIELDS:
        control, = tree.named(name)
        assert any(parent.get('id') == 'setup-panel-1' for parent in control['parents']), name
        field_label, = [node for node in tree.nodes if node['tag'] == 'label' and node.get('for') == control['id']]
        assert field_label['text'] == label
        positions.append(html.index('name="' + name + '"'))
        if name != 'tokenizer':
            fields = next(parent for parent in control['parents'] if parent.get('class') == 'fields')
            grid = fields if grid is None else grid
            assert fields is grid, name
    assert grid is not None
    details = grid['parents'][-1]
    assert details['class'] == 'details-content'
    assert details['parents'][-1]['class'] == 'setup-advanced'
    tokenizer, = tree.named('tokenizer')
    assert grid not in tokenizer['parents']
    assert details in tokenizer['parents']
    assert tokenizer['tag'] == 'select'
    assert [node['name'] for node in tree.nodes if node.get('name') and grid in node['parents']] == ['metrics_url', 'host_metrics_url', 'stream']
    css = (Path(__file__).parents[1] / 'src/giraffe/static/styles.css').read_text()
    assert '#setup-deployment .fields { display: block; }' not in css
    assert '#setup-deployment > .setup-body > .fields { display: block; }' in css
    assert re.search(r'\.fields\s*\{[^}]*display:\s*grid;[^}]*grid-template-columns:\s*repeat\(2,', css)
    assert positions == sorted(positions)
    assert '0 turns off the memory limit.' in html
    connection = html.split('data-setup-panel="2"', 1)[0]
    for removed in ('Performance metrics', 'Additional exporter addresses', 'How answers arrive', 'id="tokenizer-help"'):
        assert removed not in connection
    assert not tree.named('max_memory_mb_enabled')
    assert 'Choose your checks' in html
    assert not re.search(r'\son\w+\s*=', html, re.I)


def test_connect_info_icons_are_on_labels_and_use_csp_safe_popovers():
    tree = Markup(rendered_form())
    for name in ('metrics_url', 'host_metrics_url', 'request_timeout_seconds', 'max_memory_mb', 'limit_regression_percent'):
        control, = tree.named(name)
        info, = [node for node in tree.nodes if node.get('id') == control['id'] + '-info']
        label, = [node for node in tree.nodes if node['tag'] == 'label' and node.get('for') == control['id']]
        assert info['parents'][-1] is label['parents'][-1]
        assert info['tag'] == 'button' and info['type'] == 'button'
        assert info['popovertarget'] == control['id'] + '-help'
        help_node, = [node for node in tree.nodes if node.get('id') == info['popovertarget']]
        assert help_node['popover'] == 'auto' and help_node['text'].strip()
        assert not any(key.lower().startswith('on') for key in info)
    memory_help, = [node for node in tree.nodes if node.get('id') == 'field-max_memory_mb-help']
    assert 'MiB of RAM growth since the run started; 0 = off.' in memory_help['text']
    assert 'Exceeding the limit stops unfinished work with partial results;' in memory_help['text']
    assert not any(node.get('id') == 'field-preparation-info' for node in tree.nodes)


def test_connect_settings_values_round_trip_steps_rerun_and_file_import():
    from tests.test_ux_catalogue import browser_logic

    browser_logic(r'''
const controls=[],form={elements:{},querySelectorAll:()=>controls.filter(input=>/^(test_|traffic_|load_)/.test(input.name))};
const decode=value=>(value||'').replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&amp;/g,'&');
const attr=(tag,key)=>decode(tag.match(new RegExp('(?:^|\\s)'+key+'="([^"]*)"'))?.[1]);
nodes['#new-run-form']=form;nodes['#config-editor']={};
nodes['#main']={set innerHTML(html){
 controls.length=0;form.elements={};
 for(const match of html.matchAll(/<input\b[^>]*>|<select\b[^>]*>[\s\S]*?<\/select>/g)){
  const tag=match[0],name=attr(tag,'name');if(!name)continue;
  const options=[...tag.matchAll(/<option\b([^>]*)>/g)],option=options.find(item=>/\bselected\b/.test(item[1]))||options[0];
  const input={name,type:attr(tag,'type'),value:tag.startsWith('<select')?attr(option?.[1]||'','value'):attr(tag,'value'),checked:/\bchecked\b/.test(tag),dataset:{}};
  controls.push(input);form.elements[name] ||= input;
 }
}};
FormData=class{
 constructor(){this.data=new Map();for(const input of controls){if(['checkbox','radio'].includes(input.type)&&!input.checked)continue;this.data.set(input.name,[...(this.data.get(input.name)||[]),input.value]);}}
 get(name){return this.data.get(name)?.[0]??null;}getAll(name){return this.data.get(name)||[];}has(name){return this.data.has(name);}keys(){return this.data.keys();}set(name,value){this.data.set(name,[value]);}
};
syncTestControls=updateBounds=updateRestart=syncConfigView=syncPackControls=renderRequestPreview=syncSetupStep=renderModelDiscovery=()=>{};
state.jsonDraft=state.jsonBase='synced';renderNew();
const edits={metrics_url:'http://localhost:9000/metrics',host_metrics_url:'http://localhost:9100/metrics',stream:'false',retention:'all',request_timeout_seconds:'95',max_memory_mb:'512',limit_regression_percent:'19'};
for(const [name,value] of Object.entries(edits))form.elements[name].value=value;
const assertValues=()=>{
 const saved=readForm(false).config;
 assert.equal(saved.targets[0].metrics_url,edits.metrics_url);assert.equal(saved.targets[0].host_metrics_url,edits.host_metrics_url);
 assert.equal(saved.stream,false);assert.equal(saved.retention,'all');assert.equal(saved.request_timeout_seconds,95);
 assert.equal(saved.max_memory_mb,512);assert.equal(saved.limits.regression_percent,19);
 for(const [name,value] of Object.entries(edits))assert.equal(form.elements[name].value,value,name);
};
assertValues();
validateRunForm=()=>true;validateSetupChecks=()=>{};revealSetupStep=step=>state.setupStep=step;
for(const step of [2,3,1]){navigateSetup(step);assert.equal(state.setupStep,step);renderNew();assertValues();}
const saved=readForm(false).config;
state.detail={report:{manifest:{config:saved}},baseline_id:null};await rerun();renderNew();assertValues();
state.config=structuredClone(bootstrap.config);state.jsonDraft=state.jsonBase='synced';renderNew();
api=async(path,options)=>{assert.equal(path,'/api/config/draft');return {config:JSON.parse(options.body),migration_notes:[]};};
toast=()=>{};
await importConfig({text:async()=>JSON.stringify(saved)});assertValues();
for(const name of Object.keys(edits)){
 const path=['metrics_url','host_metrics_url'].includes(name)?'targets.0.'+name:name==='limit_regression_percent'?'limits.regression_percent':name;
 const step=['retention','limit_regression_percent'].includes(name)?3:['request_timeout_seconds','max_memory_mb'].includes(name)?2:1;
 assert.equal(settingDestination(path).step,step,name);
}
const limitNames=['max_requests','max_duration_seconds','concurrency'];
for(const name of limitNames)form.elements[name+'_enabled'].checked=false;
const unset=readForm(false).config;
for(const name of limitNames)assert.equal(unset[name],null,name);
// Fresh-run defaults must not fill in limits deliberately absent from a saved setup.
bootstrap.config={...bootstrap.config,max_requests:50000,max_duration_seconds:7200,concurrency:128};
const assertUnset=()=>{
 const current=readForm(false).config;
 for(const name of limitNames){assert.equal(current[name],null,name);assert.equal(form.elements[name+'_enabled'].checked,false,name);}
};
state.detail={report:{manifest:{config:unset}},baseline_id:null};await rerun();renderNew();assertUnset();
state.config=structuredClone(bootstrap.config);state.jsonDraft=state.jsonBase='synced';renderNew();
await importConfig({text:async()=>JSON.stringify(unset)});assertUnset();
''')


def test_api_key_help_is_once_per_field_without_visible_hints():
    tree = Markup(rendered_form())
    for name, placeholder, expected in [
        ('api_key', 'Paste your API key', 'Kept in memory for this tab. Never included in saved setups or reports. Paste it again after reloading. Leave blank for an open endpoint.'),
        ('api_key_env', 'OPENAI_API_KEY', 'Name of an environment variable set on the machine running giraffe ui, set before starting Giraffe. Enter its name, not the key. Saved setups and terminal commands need this variable or a freshly pasted key.'),
    ]:
        control, = tree.named(name)
        assert control['placeholder'] == placeholder
        field = control['parents'][-1]
        info, = [node for node in tree.nodes if node.get('id') == f'field-{name}-info']
        assert field in info['parents']
        assert not any(parent['tag'] in ('label', 'summary') for parent in info['parents'])
        help_node, = [node for node in tree.nodes if node.get('id') == f'field-{name}-help']
        assert help_node['popover'] == 'auto'
        assert help_node['text'].count(expected) == 1
        assert not any('hint' in node.get('class', '').split() and node['text'].strip() and field in node['parents'] for node in tree.nodes)


def test_disclosures_use_css_only_edit_close_labels_without_a_caret():
    tree = Markup(rendered_form())
    for selector, closed, opened in [('connection-auth', 'Edit', 'Close'), ('shared-settings', 'Edit limits', 'Close limits')]:
        disclosure, = [node for node in tree.nodes if node.get('id') == selector]
        assert 'open' not in disclosure
        summary, = [node for node in tree.nodes if node['tag'] == 'summary' and node['parents'][-1] is disclosure]
        for class_name, text in [('when-closed', closed), ('when-open', opened)]:
            label, = [node for node in tree.nodes if node.get('class') == class_name and summary in node['parents']]
            assert label['text'] == text
    css = (Path(__file__).parents[1] / 'src/giraffe/static/styles.css').read_text()
    assert re.search(r'details\[open\]\s*>\s*summary\s+\.when-closed,\s*details:not\(\[open\]\)\s*>\s*summary\s+\.when-open\s*\{\s*display:\s*none;', css)
    assert re.search(r'\.connection-auth\s*\{[^}]*margin:\s*0 0 20px;', css)
    assert re.search(r'\.connection-auth\[open\]\s*\{\s*padding-bottom:\s*16px;', css)
    assert re.search(r'\.connection-auth > \.api-key-row \{[^}]*grid-template-columns: 176px ', css)
    assert '  .connection-auth > .api-key-row { grid-template-columns: auto minmax(0, 1fr) auto; gap: 12px; }' in css
    assert 'shared-settings-caret' not in css
    assert not any('shared-settings-caret' in node.get('class', '') for node in tree.nodes)


def test_moved_settings_have_one_editable_control_on_their_own_step():
    tree = Markup(rendered_form())
    for name, step, disclosure in [
        ('request_timeout_seconds', 2, 'shared-settings'),
        ('max_memory_mb', 2, 'shared-settings'),
        ('retention', 3, 'execution-settings'),
        ('name', 3, 'execution-settings'),
        ('limit_regression_percent', 3, None),
    ]:
        control, = tree.named(name)
        assert any(parent.get('id') == f'setup-panel-{step}' for parent in control['parents']), name
        if disclosure:
            assert any(parent.get('id') == disclosure for parent in control['parents']), name
        else:
            assert not any(parent['tag'] == 'details' for parent in control['parents']), name
    timeout, = tree.named('request_timeout_seconds')
    memory, = tree.named('max_memory_mb')
    grid = next(parent for parent in timeout['parents'] if parent.get('class') == 'fields')
    assert grid in memory['parents']
    duration, = tree.named('max_duration_seconds')
    assert tree.nodes.index(duration) < tree.nodes.index(timeout) < tree.nodes.index(memory)
    retention, = tree.named('retention')
    baseline, = tree.named('baseline_id')
    regression, = tree.named('limit_regression_percent')
    name, = tree.named('name')
    assert tree.nodes.index(retention) < tree.nodes.index(name)
    assert tree.nodes.index(baseline) < tree.nodes.index(regression)
    assert baseline['parents'][-2] is regression['parents'][-2]
    execution_grid = next(parent for parent in baseline['parents'] if parent.get('class') == 'fields')
    execution_controls = [node['name'] for node in tree.nodes if node.get('name') and execution_grid in node['parents']]
    assert execution_controls[execution_controls.index('baseline_id') + 1] == 'limit_regression_percent'


def test_moved_setting_errors_reveal_the_new_step_and_open_their_disclosure():
    from tests.test_ux_catalogue import browser_logic

    browser_logic(r'''
state.jsonDraft=state.jsonBase='synced';state.openTest=null;
assertAppliedConfig=()=>{};revealSetupStep=step=>state.setupStep=step;
const paths=[['request_timeout_seconds','request_timeout_seconds',2],['max_memory_mb','max_memory_mb',2],['limits.regression_percent','limit_regression_percent',3],['retention','retention',3],['targets.0.name','name',3]];
for(const [path,name,step] of paths){
 const disclosure={tagName:'DETAILS',open:false},field={parentElement:disclosure,style:{},scrollIntoView(){this.scrolled=true;}};
 const input={parentElement:field,setAttribute(name,value){this[name]=value;},focus(){this.focused=true;},closest:selector=>selector==='.form-field'?field:null};
 nodes['#new-run-form']={elements:{[name]:input}};state.setupStep=1;
 focusFirstSettingError({errors:[{fields:[path],msg:'Invalid value.'}]});
 assert.equal(state.setupStep,step,path);assert.equal(disclosure.open,true,path);
 assert.equal(input.focused,true,path);assert.equal(field.scrolled,true,path);
}
''')
