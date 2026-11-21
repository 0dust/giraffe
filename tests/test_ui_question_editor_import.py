"""Question editing and imports explain their contracts without changing them."""

import json

import pytest

from giraffe.dataset_import import preview_dataset
from giraffe.models import MAX_REQUEST_PACK_BYTES
from tests.test_web import browser
from tests.test_ui_question_navigation import navigation_browser
from tests.test_run_limits_phase6_ui import Markup
from tests.test_ux_catalogue import browser_logic


def test_question_editor_keeps_names_and_conversation_controls_in_their_places():
    result = browser_logic(r'''
config.request_source='request_pack';
config.request_pack={version:1,cases:[{fixture_id:'kept-name',messages:[{role:'user',content:'<question>'}],scorer:'json',expected:null,schema:{type:'null'},weight:2},{fixture_id:'example-1',messages:[{role:'user',content:'existing'}],scorer:'exact',expected:'answer',weight:1}]};
const before=JSON.stringify(config);
datasetEditingAllowed=()=>{};captureFormDraft=()=>{};syncExampleScorer=()=>{};
let markup='';openSetupWorkspace=view=>{state.setupWorkspace={view};};renderSetupWorkspace=(title,body)=>{markup=body;};focusSetupWorkspace=()=>{};
nodes['#dataset-example-form']={elements:{message_0_content:{focus(){}}}};
openExampleEditor(0);const saved={markup,item:state.datasetEditor.item};
openExampleEditor();
assert.equal(state.datasetEditor.item.fixture_id,'example-2');
assert.equal(JSON.stringify(config),before);
console.log(JSON.stringify({saved,added:markup}));
''')
    tree = Markup(result['saved']['markup'])
    for name, label in (
        ('example_id', 'Test name'),
        ('example_weight', 'Relative frequency in Load test'),
    ):
        control, = tree.named(name)
        assert any('example-advanced' in node.get('class', '').split()
                   for node in control['parents'])
        assert label in result['saved']['markup']
    assert result['saved']['item']['fixture_id'] == 'kept-name'
    assert result['saved']['item']['expected'] is None
    assert result['saved']['item']['schema'] == {'type': 'null'}
    assert '&lt;question&gt;' in result['saved']['markup']
    assert '<question>' not in result['saved']['markup']
    assert 'name="example_id" type="text" value="example-2"' in result['added']
    assert result['added'].count('These messages form one saved conversation.') == 1
    assert 'Add message to this conversation' in result['added']
    assert 'Message speaker' in result['added']
    for role in ('Application instructions', 'User message', 'Saved model reply'):
        assert role in result['added']
    assert 'A weight of 2 selects this question about twice as often as weight 1' in result['added']
    assert 'standalone Answer correctness' in result['added']
    assert 'Maximum generated tokens for this question. Leave blank to inherit the effective output limit.' in result['added']
    assert 'The suite value is a default, not a ceiling.' not in result['added']
    scorer, = Markup(result['added']).named('example_scorer')
    assert scorer['aria-describedby'] == 'field-example_scorer-hint'


def test_selected_answer_check_hint_changes_without_losing_json_null_or_text():
    browser_logic(r'''
const expected={value:'"Paris"'},selector={value:'json'},hint={textContent:''};
nodes['#dataset-example-form']={elements:{example_scorer:selector,example_expected:expected}};
for(const id of ['example-expected-label','example-rubric-label','example-schema-label','example-expected-hint','dataset-error'])nodes['#'+id]={hidden:false,textContent:''};
nodes['#field-example_scorer-hint']=hint;
state.datasetEditor={expectedFormat:'json',scorer:'json'};
selector.value='exact';syncExampleScorer();
assert.equal(expected.value,'Paris');
assert.equal(hint.textContent,'Exact match: Capitalization and punctuation must match. Extra whitespace is ignored.');
assert.equal(nodes['#example-expected-hint'].textContent,'');
selector.value='judge';syncExampleScorer();
assert.match(hint.textContent,/Configure an evaluator model and endpoint/);
assert(!nodes['#example-rubric-label'].hidden);
selector.value='contains';syncExampleScorer();
assert.match(hint.textContent,/word or numeric boundaries/);
assert.doesNotMatch(hint.textContent,/AI evaluator|Exact match/);
selector.value='none';syncExampleScorer();
assert.match(hint.textContent,/unscored.*inconclusive/);
assert(nodes['#example-expected-label'].hidden);
selector.value='json';syncExampleScorer();assert.equal(expected.value,'"Paris"');
assert.match(nodes['#example-expected-hint'].textContent,/Use null for a null answer/);
expected.value='null';selector.value='exact';syncExampleScorer();
assert.equal(selector.value,'json');assert.equal(expected.value,'null');
assert.match(hint.textContent,/JSON check:/);
assert.match(nodes['#dataset-error'].textContent,/JSON string reference/);
''')


def test_import_area_only_shows_downloadable_valid_samples():
    result = browser_logic(r'''
const before=JSON.stringify(config);
const markup=renderDatasetImportHelp(config),csv=datasetImportSample('csv'),json=datasetImportSample('json');
assert.equal(JSON.stringify(config),before);assert.equal(datasetImportSample('unknown'),null);
console.log(JSON.stringify({markup,csv,json}));
''')
    markup = result['markup']
    for text in ('One question and expected answer per row.', 'Limit: 1 MiB / 1,000 questions.',
                 'View format example', 'Advanced JSON format'):
        assert text not in markup
    for label in ('Download sample CSV', 'Download sample JSON'):
        assert label in markup
    for format in ('csv', 'json'):
        parsed = preview_dataset(result[format]['text'], format,
                                 input_field='question', expected_field='answer', scorer='exact')
        assert not parsed['errors']
        assert [case['expected'] for case in parsed['request_pack']['cases']] == ['4', 'Paris']


def test_sample_downloads_use_only_fixed_content_and_release_the_object_url():
    browser_logic(r'''
const before=JSON.stringify(config),downloads=[],released=[];
Blob=class {constructor(parts,options){this.parts=parts;this.type=options.type;}};
URL={createObjectURL:blob=>{downloads.push({blob});return 'blob:sample';},revokeObjectURL:url=>released.push(url)};
document.createElement=tag=>{assert.equal(tag,'a');return {click(){Object.assign(downloads.at(-1),{name:this.download,url:this.href});}};};setTimeout=fn=>fn();
downloadDatasetSample('csv');downloadDatasetSample('json');downloadDatasetSample('unknown');
assert.equal(downloads.length,2);assert.equal(released.length,2);
assert.equal(downloads[0].name,'giraffe-sample-questions.csv');assert.equal(downloads[0].blob.type,'text/csv');
assert.equal(downloads[1].name,'giraffe-sample-questions.json');assert.equal(downloads[1].blob.type,'application/json');
assert.equal(downloads[0].blob.parts[0],datasetImportSample('csv').text);
assert.equal(downloads[1].blob.parts[0],datasetImportSample('json').text);
assert.equal(JSON.stringify(config),before);
''')


def test_import_replacement_count_preserves_mapping_errors_preview_and_disabled_state():
    result = browser_logic(r'''
const cases=Array.from({length:20},(_,i)=>({fixture_id:'example-'+i,messages:[{role:'user',content:'<question>'}],scorer:'exact',expected:'<answer>'}));
const preview={columns:['question','answer'],preview_rows:[{question:'<question>',answer:'<answer>'}],request_pack:{version:1,cases},errors:[{row:2,field:'answer',message:'<invalid>'}]};
state.datasetImport={name:'<questions>.csv',preview,mapping:{input_field:'question',expected_field:'answer',scorer:'exact'},mappingDirty:false,loading:false};
state.setupWorkspace={view:'question-import'};let markup;renderSetupWorkspace=(title,body)=>{markup=body;};renderDatasetImport();const ready=markup;
state.datasetImport.mappingDirty=true;renderDatasetImport();const changed=markup;
state.datasetImport.mappingDirty=false;state.datasetImport.loading=true;renderDatasetImport();
const loading=markup;state.datasetImport.mapping.scorer='judge';renderDatasetImport();
console.log(JSON.stringify({ready,changed,loading,judge:markup}));
''')
    assert 'Replace current questions with these 20 questions' in result['ready']
    assert 'Row 2' in result['ready'] and '&lt;invalid&gt;' in result['ready']
    assert '&lt;question&gt;' in result['ready'] and '<question>' not in result['ready']
    assert 'Question column' in result['ready'] and 'Expected answer column' in result['ready']
    assert 'dataset-import-preview' in result['ready']
    exact_hint, = [node for node in Markup(result['ready']).nodes
                   if node.get('id') == 'field-import_scorer-hint']
    judge_hint, = [node for node in Markup(result['judge']).nodes
                   if node.get('id') == 'field-import_scorer-hint']
    assert exact_hint['text'].startswith('Exact match:')
    assert 'Configure an evaluator model and endpoint' in judge_hint['text']
    for state in ('changed', 'loading'):
        assert '<button type="submit" class="button primary" disabled>' in result[state]


async def test_near_limit_import_and_edits_keep_model_bounds_and_json_null(tmp_path, monkeypatch):
    monkeypatch.setattr('giraffe.client.LLMClient.__aenter__',
                        lambda *args: pytest.fail('Question editing must not send model requests'))
    pack = {'version': 1, 'cases': [{'fixture_id': 'large', 'messages': [
        {'role': 'user', 'content': 'x'}], 'scorer': 'exact', 'expected': 'a'}]}
    overhead = len(json.dumps(pack, separators=(',', ':')).encode()) - 1
    pack['cases'][0]['messages'][0]['content'] = 'x' * (MAX_REQUEST_PACK_BYTES - overhead - 40)
    raw = json.dumps(pack, separators=(',', ':'))
    preview = preview_dataset(raw, 'json')
    assert preview['request_pack'] and not preview['errors']
    assert len(raw.encode()) < MAX_REQUEST_PACK_BYTES
    assert len(json.dumps(preview['request_pack'], separators=(',', ':')).encode()) > MAX_REQUEST_PACK_BYTES

    result = browser_logic('const normalized=' + json.dumps(preview['request_pack']) + r'''
config.max_requests=-17;config.targets[0].url='unfinished endpoint';
const before=JSON.stringify(config);
configChanged=()=>{state.configRevision++;};refreshQuestionSetupSummary=()=>{};
openQuestionManager=()=>{state.datasetImport=null;};
state.datasetImport={preview:{request_pack:normalized},revision:state.configRevision,route:state.routeVersion,loading:false,mappingDirty:false};
api=()=>assert.fail('Applying the validated preview must not count its inflated JSON as a new raw import');
await useImportedDataset({preventDefault(){}});
assert.equal(config.request_pack.cases[0].messages[0].content.length,normalized.cases[0].messages[0].content.length);
assert.equal(config.max_requests,-17);assert.equal(config.targets[0].url,'unfinished endpoint');
let candidate;
api=async(path,options)=>{assert.equal(path,'/api/config/draft');candidate=JSON.parse(options.body);return {config:candidate};};
assert(await applyDataset(normalized));
assert.equal(config.max_requests,-17);assert.equal(config.targets[0].url,'unfinished endpoint');
assert.equal(candidate.config_version,2);assert.equal(candidate.targets.length,1);assert.equal(candidate.targets[0].url,'http://localhost/v1');
assert.deepEqual(candidate.checks,[]);assert.equal(candidate.request_source,'request_pack');
assert(!Object.hasOwn(candidate,'max_requests'),'Unrelated invalid run settings must not enter question validation');
console.log(JSON.stringify(candidate));
''')
    async with browser(tmp_path) as client:
        accepted = await client.post('/api/config/draft', json=result)
        assert accepted.status_code == 200
        assert accepted.json()['config']['request_pack']['cases'][0]['expected'] == 'a'
        oversized = json.loads(json.dumps(result))
        oversized['request_pack']['cases'][0]['messages'][0]['content'] = 'x' * MAX_REQUEST_PACK_BYTES
        rejected = await client.post('/api/config/draft', json=oversized)
        assert rejected.status_code == 422
        assert '1 MiB' in rejected.text
        null_pack = {'version': 1, 'cases': [
            {'fixture_id': 'null', 'messages': [{'role': 'user', 'content': 'Null?'}],
             'scorer': 'json', 'schema': {'type': 'null'}, 'expected': None},
            {'fixture_id': 'schema-only', 'messages': [{'role': 'user', 'content': 'Null?'}],
             'scorer': 'json', 'schema': {'type': 'null'}},
        ]}
        accepted_null = await client.post('/api/config/draft', json={**result, 'request_pack': null_pack})
        assert accepted_null.status_code == 200
        null_case, schema_only = accepted_null.json()['config']['request_pack']['cases']
        assert null_case['expected'] is None and 'expected' not in schema_only



def test_return_to_questions_restores_the_surviving_action_by_question_identity():
    navigation_browser(r"""
renderNew();await click('manage-questions');
type(visible('[name="question_search"]'),'alpha');
change(visible('[name="question_scorer_filter"]'),'exact');
const selected=visible('[data-dataset-select="alpha"]');selected.checked=true;change(selected);
window.scrollY=503.5;visible('[data-action="edit-example"]').focus({preventScroll:true});
await click('edit-example');
let editor=document.querySelector('#dataset-example-form');type(editor.elements.example_expected,'Updated alpha');
await submit(editor);await until(()=>state.setupWorkspace?.view==='questions');
const assertReturn=(action,fixtureId=null)=>{
 const target=document.activeElement;assert.equal(target.dataset.action,action);
 if(fixtureId)assert.equal(datasetCases()[Number(target.dataset.caseIndex)].fixture_id,fixtureId);
 assert.equal(window.scrollY,503.5);
 assert.equal(visible('[name="question_search"]').value,'alpha');
 assert.equal(visible('[name="question_scorer_filter"]').value,'exact');
 assert.deepEqual(state.datasetSelection,['alpha']);
};
assertReturn('edit-example','alpha');
await click('edit-example');state.config.request_pack.cases.reverse();
await click('cancel-question-edit');assertReturn('edit-example','alpha');
assert.equal(document.activeElement.dataset.caseIndex,'1','The stable question can move to a different index');
await click('import-pack');await click('cancel-question-import');assertReturn('import-pack');
await click('edit-example');editor=document.querySelector('#dataset-example-form');
change(editor.elements.example_scorer,'judge');await submit(editor);await until(()=>state.setupWorkspace?.view==='questions');
assert.equal(document.activeElement.name,'question_search','A filtered-out action falls back to Search questions');
assert.equal(window.scrollY,503.5);
""")
