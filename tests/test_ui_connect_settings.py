"""The owner's Connect settings block and its existing configuration contract."""

import re
from pathlib import Path

from tests.test_run_limits_phase6_ui import Markup, rendered_form


CONNECT_FIELDS = [
    ('tokenizer', 'Token ID / tokenizer'),
    ('metrics_url', 'Server metrics URL'),
    ('host_metrics_url', 'Host metrics URL'),
    ('stream', 'Streaming'),
    ('retention', 'Keep response text'),
    ('request_timeout_seconds', 'Request timeout (seconds)'),
    ('max_memory_mb', 'Max memory growth (MiB)'),
    ('limit_regression_percent', 'Regression threshold (%)'),
]


def test_connect_settings_match_reference_order_labels_and_single_controls():
    html = rendered_form()
    tree = Markup(html)
    auth = next(node for node in tree.nodes if node.get('id') == 'connection-auth')
    summary, = [node for node in tree.nodes if node['tag'] == 'summary' and node['parents'][-1] is auth]
    assert summary['class'] == 'api-key-row'
    spans = [node for node in tree.nodes if node['tag'] == 'span' and summary in node['parents']]
    assert [(node['class'], node['text']) for node in spans] == [
        ('api-key-label', 'API key'), ('row-value api-key-value', 'Not set'),
        ('row-action api-key-action', 'Edit'),
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
    assert tokenizer['parents'][-2]['parents'][-1] is details
    css = (Path(__file__).parents[1] / 'src/giraffe/static/styles.css').read_text()
    assert '#setup-deployment .fields { display: block; }' not in css
    assert '#setup-deployment > .setup-body > .fields { display: block; }' in css
    assert re.search(r'\.fields\s*\{[^}]*display:\s*grid;[^}]*grid-template-columns:\s*repeat\(2,', css)
    assert positions == sorted(positions)
    assert '0 turns off the memory limit.' in html
    connection = html.split('data-setup-panel="2"', 1)[0]
    for removed in ('Performance metrics', 'Additional exporter addresses', 'How answers arrive', 'tokenizer-help'):
        assert removed not in connection
    assert not tree.named('max_memory_mb_enabled')
    assert 'Choose your checks' in html
    assert not re.search(r'\son\w+\s*=', html, re.I)


def test_connect_info_icons_are_on_labels_and_use_csp_safe_popovers():
    tree = Markup(rendered_form())
    for name in ('metrics_url', 'host_metrics_url', 'request_timeout_seconds', 'limit_regression_percent'):
        control, = tree.named(name)
        info, = [node for node in tree.nodes if node.get('id') == control['id'] + '-info']
        label, = [node for node in tree.nodes if node['tag'] == 'label' and node.get('for') == control['id']]
        assert info['parents'][-1] is label['parents'][-1]
        assert info['tag'] == 'button' and info['type'] == 'button'
        assert info['popovertarget'] == control['id'] + '-help'
        help_node, = [node for node in tree.nodes if node.get('id') == info['popovertarget']]
        assert help_node['popover'] == 'auto' and help_node['text'].strip()
        assert not any(key.lower().startswith('on') for key in info)
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
for(const name of Object.keys(edits)){const path=['metrics_url','host_metrics_url'].includes(name)?'targets.0.'+name:name==='limit_regression_percent'?'limits.regression_percent':name;assert.equal(settingDestination(path).step,1,name);}
''')
