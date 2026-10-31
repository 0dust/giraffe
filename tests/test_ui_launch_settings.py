"""Launch controls and the closed restart row keep their existing form contract."""

from pathlib import Path

from tests.test_run_limits_phase6_ui import Markup, rendered_form
from tests.test_ux_catalogue import browser_logic


RESTART_HELP = (
    "Giraffe runs this script to restart your server before testing, so you can see how it behaves "
    "from a cold start: how long it takes to start, how slow the first requests are and, if the "
    "server reports it, how long the model takes to load. The script is yours and decides what "
    "restarting means, for example systemctl restart ollama. It runs on this machine, and only "
    "when you tick the box on the Launch page. Only this file is reviewed; anything it calls is "
    "not. Don't print secrets: output is saved in the report."
)


def test_restart_row_help_is_once_outside_the_closed_disclosure_and_keyboard_accessible():
    html = browser_logic("state.bootstrap.restart_allowed=true;console.log(JSON.stringify(renderRestartScriptRow({},config)));")
    tree = Markup(html)
    details, = [node for node in tree.nodes if node.get('id') == 'restart-script-row']
    assert 'open' not in details
    button, = [node for node in tree.nodes if node.get('id') == 'field-restart_script-info']
    assert button['tag'] == 'button' and button['type'] == 'button'
    assert 'disabled' not in button and button.get('tabindex') != '-1'
    assert not any(node['tag'] in ('summary', 'details') for node in button['parents'])
    assert button['popovertarget'] == 'field-restart_script-help'
    help_text, = [node for node in tree.nodes if node.get('id') == 'field-restart_script-help-text']
    assert help_text['text'] == RESTART_HELP
    assert html.count('Giraffe runs this script') == 1
    assert not any(node.get('aria-label') == 'About Script path' for node in tree.nodes)
    timeout, = [node for node in tree.nodes if node.get('id') == 'field-restart_script_timeout_seconds-info']
    assert details in timeout['parents']
    css = (Path(__file__).parents[1] / 'src/giraffe/static/styles.css').read_text()
    assert '#restart-script-setting' in css and '#restart-script-help' in css


def test_comparison_is_an_always_visible_equal_grid_and_saved_disclosure_has_only_two_controls():
    tree = Markup(rendered_form())
    details, = [node for node in tree.nodes if node.get('id') == 'execution-settings']
    assert 'open' not in details
    summary, = [node for node in tree.nodes if node['tag'] == 'summary' and node['parents'][-1] is details]
    assert summary['text'] == 'Saved with this run local · Failed answers only'
    assert [node['name'] for node in tree.nodes if node.get('name') and details in node['parents']] == ['retention', 'name']
    for name in ('baseline_id', 'limit_regression_percent', 'retention', 'name'):
        control, = tree.named(name)
        assert any(parent.get('id') == 'setup-panel-3' for parent in control['parents'])
    baseline, = tree.named('baseline_id')
    threshold, = tree.named('limit_regression_percent')
    grid = baseline['parents'][-2]
    assert grid is threshold['parents'][-2] and grid['class'] == 'fields'
    assert not any(parent['tag'] == 'details' or 'hidden' in parent for parent in [grid, *grid['parents']]
                   if parent.get('id') != 'setup-panel-3')
    assert [node['name'] for node in tree.nodes if node.get('name') and grid in node['parents']] == ['baseline_id', 'limit_regression_percent']
    assert not any(node.get('id') == 'review-compare' for node in tree.nodes)
    for control in (baseline, threshold):
        assert any(node.get('id') == control['id'] + '-info' for node in tree.nodes)
    retention, = tree.named('retention')
    options = [node for node in tree.nodes if node['tag'] == 'option' and retention in node['parents']]
    assert [(node['value'], node['text']) for node in options] == [('failures', 'Failed answers only'), ('all', 'All answers'), ('none', 'None')]
    label, = [node for node in tree.nodes if node.get('for') == retention['id']]
    assert label['text'] == 'Save model answers'
    name, = tree.named('name')
    assert 'required' in name and name['placeholder'] == name['value'] == 'local'
    label, = [node for node in tree.nodes if node.get('for') == name['id']]
    assert label['text'] == 'Name in reports'
    help_text, = [node for node in tree.nodes if node.get('id') == 'field-name-help-text']
    assert help_text['text'] == 'Labels this server in reports and comparisons.'
    assert not any(node.get('id') == 'field-name-hint' for node in tree.nodes)


def test_saved_summary_updates_on_name_input_and_retention_change_without_opening():
    browser_logic(r'''
const form={elements:{name:{value:'local'},retention:{value:'failures'}}};
nodes['#new-run-form']=form;
nodes['#execution-settings']={open:false};nodes['#saved-run-summary']={textContent:''};
configChanged=updateBounds=scheduleRequestPlan=()=>{};nodes['#cli-fallback']={hidden:true};
const input={id:'field-name',name:'name',value:'other <server>',dataset:{},
closest:q=>q==='#new-run-form'?form:null,matches:()=>false,setCustomValidity(){}};
form.elements.name=input;
listeners.input({target:input});
assert.equal(nodes['#saved-run-summary'].textContent,'other <server> · Failed answers only');
const retention={id:'field-retention',name:'retention',value:'all',dataset:{},
closest:q=>q==='#new-run-form'?form:null,matches:()=>false};
form.elements.retention=retention;
listeners.change({target:retention});
assert.equal(nodes['#saved-run-summary'].textContent,'other <server> · All answers');
retention.value='none';listeners.change({target:retention});
assert.equal(nodes['#saved-run-summary'].textContent,'other <server> · None');
assert.equal(nodes['#execution-settings'].open,false);
''')


def test_name_retention_validation_opens_saved_settings_and_baseline_links_focus_visible_control():
    browser_logic(r'''
state.jsonDraft=state.jsonBase='synced';state.openTest=null;
assertAppliedConfig=()=>{};revealSetupStep=step=>state.setupStep=step;
for(const [path,name] of [['targets.0.name','name'],['retention','retention']]){
 const disclosure={tagName:'DETAILS',open:false},field={parentElement:disclosure,style:{},scrollIntoView(){}};
 const input={parentElement:field,setAttribute(){},focus(){this.focused=true;},closest:()=>field};
 nodes['#new-run-form']={elements:{[name]:input}};state.setupStep=1;
 focusFirstSettingError({errors:[{fields:[path],msg:'Invalid value.'}]});
 assert.equal(state.setupStep,3,path);assert.equal(disclosure.open,true,path);assert.equal(input.focused,true,path);
}
const disclosure=nodes['#execution-settings']={open:false};
const input={setAttribute(){},focus(){this.focused=true;},scrollIntoView(){this.scrolled=true;}};
nodes['#new-run-form']={elements:{baseline_id:input}};nodes['#field-baseline_id']=input;
await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'choose-baseline'}}:null}});
assert.equal(state.setupStep,3);assert.equal(input.focused,true);assert.equal(input.scrolled,true);
assert.equal(disclosure.open,false);
input.focused=false;focusSetting('baseline_id');assert.equal(input.focused,true);assert.equal(state.setupStep,3);
''')


def test_restart_help_tracks_wrapped_phone_row_height_and_disconnects_old_observer():
    browser_logic(r'''
let height=76,right=190,resize,disconnected=false;
state.restartRowObserver={disconnect(){disconnected=true;}};
nodes['#restart-script-row > summary']={getBoundingClientRect:()=>({height,top:29})};
nodes['#restart-script-row .api-key-label']={getBoundingClientRect:()=>({right})};
nodes['#restart-script-setting']={getBoundingClientRect:()=>({left:20,top:20})};
nodes['#restart-script-help']={style:{}};
const observed=[];ResizeObserver=class{constructor(callback){resize=callback;}observe(node){observed.push(node);}};
watchRestartScriptRow();assert.equal(disconnected,true);
assert.equal(nodes['#restart-script-help'].style.height,'76px');
assert.equal(nodes['#restart-script-help'].style.left,'154px');assert.equal(nodes['#restart-script-help'].style.top,'9px');
assert.equal(observed.length,2);
height=36;right=180;resize();assert.equal(nodes['#restart-script-help'].style.height,'36px');
assert.equal(nodes['#restart-script-help'].style.left,'144px');
''')
