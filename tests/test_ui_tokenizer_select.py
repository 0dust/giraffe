"""Native tokenizer selection keeps Custom verification and saved configuration."""

from pathlib import Path

import pytest

from giraffe.models import RunConfig
from tests.test_run_limits_phase6_ui import Markup, rendered_form
from tests.test_ui_review53_latest import TOKENIZER_FORM
from tests.test_ux_catalogue import browser_logic
from tests.test_ux_setup import run_browser_logic


TOKENIZER_HELP = (
    "Used to size prompts. Automatic uses the model's own tokenizer when it can be found, otherwise builtin. "
    "builtin uses tiktoken o200k_base, included with Giraffe; its counts are approximate. "
    "Custom loads a Hugging Face model ID or local path and needs giraffe-llm[hf-tokenizer]."
)


def test_tokenizer_help_is_one_popover_beside_the_select_label():
    tree = Markup(rendered_form())
    select, = tree.named('tokenizer')
    label, = [node for node in tree.nodes if node['tag'] == 'label' and node.get('for') == select['id']]
    field = next(parent for parent in select['parents'] if 'form-field' in parent.get('class', '').split())
    buttons = [node for node in tree.nodes if node.get('class') == 'field-info' and field in node['parents']]
    assert len(buttons) == 1
    assert label not in buttons[0]['parents']
    help_text, = [node for node in tree.nodes if node.get('id') == buttons[0]['popovertarget']]
    assert TOKENIZER_HELP in help_text['text']
    css = (Path(__file__).parents[1] / 'src/giraffe/static/styles.css').read_text()
    for selector in ['.tokenizer-combobox', '.tokenizer-dropdown', '.tokenizer-options']:
        assert selector not in css


def test_custom_choice_reveals_and_focuses_name_and_retains_it_when_switching():
    browser_logic(TOKENIZER_FORM + r'''
configChanged=updateBounds=()=>{};
const custom=form.elements.tokenizer_custom;custom.focus=()=>{custom.focused=true;};
Object.assign(form.elements.tokenizer,{dataset:{},matches:()=>false,closest:()=>null});
nodes['#tokenizer-custom-field']={hidden:true};
function choose(mode){form.elements.tokenizer.value=mode;listeners.change({target:form.elements.tokenizer});}
choose('custom');assert.equal(nodes['#tokenizer-custom-field'].hidden,false);assert.equal(custom.focused,true);
assert.equal(tokenizerChoice(),'');assert.equal(tokenizerSetupReady(),false);
assert.equal(nodes['#tokenizer-status'].textContent,'Enter a tokenizer name, or pick Automatic.');
assert.equal(nodes['#tokenizer-status'].className,'hint');
custom.value=' Qwen/Qwen2.5-7B-Instruct ';
choose('builtin');assert.equal(nodes['#tokenizer-custom-field'].hidden,true);
assert.equal(tokenizerChoice(),'builtin');assert.equal(tokenizerSetupReady(),true);
assert.equal(nodes['#tokenizer-status'].textContent,'');
custom.focused=false;choose('custom');
assert.equal(nodes['#tokenizer-custom-field'].hidden,false);assert.equal(custom.focused,true);
assert.equal(custom.value,' Qwen/Qwen2.5-7B-Instruct ');assert.equal(tokenizerChoice(),'Qwen/Qwen2.5-7B-Instruct');
assert.equal(state.tokenizerSelection.status,'pending');assert.equal(tokenizerSetupReady(),false);
choose('auto');assert.equal(nodes['#tokenizer-custom-field'].hidden,true);
assert.equal(tokenizerChoice(),'auto');assert.equal(tokenizerSetupReady(),true);
assert.equal(nodes['#tokenizer-status'].textContent,'');
''')



def test_editing_custom_name_clears_its_error_and_reverifies_the_new_name():
    browser_logic(TOKENIZER_FORM + r'''
configChanged=updateBounds=()=>{};
form.elements.tokenizer.value='custom';const custom=form.elements.tokenizer_custom;
Object.assign(custom,{value:'missing/model',dataset:{},matches:()=>false,closest:q=>q==='#new-run-form'?form:null});
state.tokenizerSelection={key:tokenizerSelectionKey(),route:state.routeVersion,status:'error',message:'Could not load tokenizer "missing/model": Invalid tokenizer.'};
renderTokenizerSelection();assert.equal(nodes['#tokenizer-status'].className,'notice error');
custom.value='working/model';listeners.input({target:custom});
assert.equal(state.tokenizerSelection.status,'pending');assert.equal(tokenizerSetupReady(),false);
assert.equal(nodes['#tokenizer-status'].className,'hint');assert.doesNotMatch(nodes['#tokenizer-status'].textContent,/Invalid tokenizer/);
let requested;api=async(path,options)=>{requested=JSON.parse(options.body);return {status:'ready',message:''};};
await resolveTokenizerSelection(state.tokenizerSelection.key,state.tokenizerSequence,state.routeVersion);
assert.equal(requested.token_id,'working/model');assert.equal(tokenizerSetupReady(),true);
assert.equal(nodes['#tokenizer-status'].textContent,'');
''')

def test_empty_custom_disables_next_and_launch_with_a_plain_hint():
    run_browser_logic(r'''
const form={elements:{model:element({value:'test-model'}),tokenizer:element({value:'custom'}),tokenizer_custom:element({value:'   '})}};
nodes['#new-run-form']=form;
for(const selector of ['#tokenizer-status','#setup-panel-1','#start-run','#setup-navigation-hint','[data-action="setup-next"]','[data-action="setup-back"]'])nodes[selector]=element();
state.jsonDraft=state.jsonBase='synced';currentRequestPlan=()=>({});currentSettingErrors=currentResourceErrors=()=>[];
renderTokenizerSelection();assert.equal(tokenizerChoice(),'');assert.equal(tokenizerSetupReady(),false);
assert.equal(nodes['#tokenizer-status'].textContent,'Enter a tokenizer name, or pick Automatic.');
assert.equal(nodes['#tokenizer-status'].className,'hint');
state.setupStep=1;syncSetupStep();assert.equal(nodes['[data-action="setup-next"]'].disabled,true);
state.setupStep=3;syncSetupStep();assert.equal(nodes['#start-run'].disabled,true);
''')


@pytest.mark.parametrize('tokenizer', [None, 'auto', 'builtin', 'Qwen/Qwen2.5-7B-Instruct', '/models/local-tokenizer'])
def test_load_run_again_and_saved_drafts_preserve_tokenizer_mapping(tokenizer):
    config = RunConfig(targets=[{'name': 'local', 'url': 'http://localhost/v1', 'model': 'test', 'tokenizer': tokenizer}])
    browser_logic(r'''
nodes['#main']={};nodes['#config-editor']={};
const elements=new Proxy({}, {get(target,name){return target[name] ||= {dataset:{},value:''};}});
nodes['#new-run-form']={elements};state.jsonDraft=state.jsonBase='synced';
syncTestControls=updateBounds=updateRestart=syncConfigView=syncPackControls=renderRequestPreview=syncSetupStep=renderModelDiscovery=()=>{};
captureFormDraft=()=>{};readForm=()=>({config:structuredClone(state.config),mode:'normal'});
configError=toast=()=>{};scheduleTokenizerSelection=()=>{};
const original=structuredClone(config);state.bootstrap.config=structuredClone(config);
function checkMapping(){
 renderNew();const html=nodes['#main'].innerHTML;
 const select=html.match(/<select\b[^>]*name="tokenizer"[^>]*>([\s\S]*?)<\/select>/)[1];
 const choice=original.targets[0].tokenizer,mode=choice==null||choice==='auto'?'auto':choice==='builtin'?'builtin':'custom';
 assert.match(select,new RegExp('<option value="'+mode+'"[^>]*selected'));
 const input=html.match(/<input\b[^>]*name="tokenizer_custom"[^>]*>/)[0];
 assert(input.includes('value="'+(mode==='custom'?choice:'')+'"'));
}
api=async(path,options)=>({config:JSON.parse(options.body)});
await importConfig({text:async()=>JSON.stringify(original)});assert.equal(state.config.targets[0].tokenizer,original.targets[0].tokenizer);checkMapping();
state.detail={config:original};await rerun();assert.equal(state.config.targets[0].tokenizer,original.targets[0].tokenizer);checkMapping();
sessionStorage.setItem('giraffe-draft-v1',JSON.stringify({bootstrap:state.bootstrap.config,config:original}));
state.config=structuredClone(original);state.config.targets[0].tokenizer='different/name';await restoreDraft();
assert.equal(state.config.targets[0].tokenizer,original.targets[0].tokenizer);checkMapping();
''', config)
