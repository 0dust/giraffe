"""Persistent-user setup and results use the same identities as the runner."""
from collections import OrderedDict
import json

from giraffe.models import RunConfig
from giraffe.web import _track_live_request
from tests.test_ux_catalogue import browser_logic


def configuration():
    return RunConfig(targets=[{'name': 'local', 'url': 'http://localhost/v1', 'model': 'test'}],
                     checks=['multi_user'], concurrency=5, multi_user={'levels': [20]})


def test_arrival_recovery_form_roundtrip():
    value = browser_logic(r'''
const markup=renderMultiUserEditor(config);
for(const name of ['scenario_arrivals','scenario_recovery','arrivals_requests_per_second','arrivals_activations','arrivals_bursts','recovery_control_seconds','recovery_sustained_seconds','recovery_lighter_seconds','recovery_max_slowdown'])assert(markup.includes('name="multi_user_'+name+'"'));
const form=new FormData();form.set('multi_user_rendered','1');form.set('multi_user_scenarios_rendered','1');
form.set('multi_user_scenario_arrivals','on');form.set('multi_user_scenario_recovery','on');
form.set('multi_user_arrivals_requests_per_second','2');form.set('multi_user_arrivals_activations','user-02@2, user-03@3');
form.set('multi_user_arrivals_bursts','user-01@4@3@0, user-02@5@2@0.1');
form.set('multi_user_recovery_control_seconds','4');form.set('multi_user_recovery_sustained_seconds','8');
form.set('multi_user_recovery_lighter_seconds','4');form.set('multi_user_recovery_max_slowdown','1.5');
assert.equal(settingPath('multi_user_arrivals_requests_per_second'),'multi_user.arrivals.requests_per_second');
assert.equal(settingDestination('multi_user.arrivals.bursts.0.requests').name,'multi_user_arrivals_bursts');
assert.equal(settingPath('multi_user_recovery_control_seconds'),'multi_user.recovery.control_seconds');
console.log(JSON.stringify(configureMultiUser(config,form,bootstrap.multi_user.defaults)));
''', configuration())
    config = RunConfig.model_validate(value)
    assert config.multi_user.scenarios == ['arrivals', 'recovery']
    assert config.multi_user.arrivals.activations == {'user-02': 2, 'user-03': 3}
    assert config.multi_user.arrivals.bursts[1].spacing_seconds == .1
    assert config.multi_user.recovery.max_slowdown == 1.5


def test_arrival_recovery_form_preserves_imported_scenario_order():
    config = configuration()
    config.multi_user.scenarios = ['recovery', 'arrivals']
    value = browser_logic(r'''
const form=new FormData();form.set('multi_user_rendered','1');form.set('multi_user_scenarios_rendered','1');
form.set('multi_user_scenario_arrivals','on');form.set('multi_user_scenario_recovery','on');
console.log(JSON.stringify(configureMultiUser(config,form,bootstrap.multi_user.defaults)));
''', config)
    assert value['multi_user']['scenarios'] == config.multi_user.scenarios


def test_recovery_results_keep_user_qualification_and_phase_evidence():
    from tests.test_multi_user_recovery import recovery_observation
    from giraffe.multi_user_metrics import summarize
    source, records = recovery_observation()
    summary = summarize(source, records)
    browser_logic('const summary='+json.dumps(summary)+r''';
const markup=renderMultiUserResults(summary);
for(const text of ['Scheduled and achieved traffic','Matched per-user recovery','user-03','Recovery slowdown','Control:','Sustained:','Lighter:','not server queue time'])assert(markup.includes(text));
assert.match(markup,/data-request="2-user-03"/);
''', configuration())


def test_multi_user_scenarios_form_roundtrip_and_field_destinations():
    value = browser_logic(r'''
const markup=renderMultiUserEditor(config);
for(const name of ['scenario_noisy_neighbors','scenario_cancellation','noisy_neighbors_heavy_user_ids','noisy_neighbors_heavy_user_count','noisy_neighbors_max_slowdown','cancellation_user_id','cancellation_cancel_after_chunks'])assert(markup.includes('name="multi_user_'+name+'"'));
const form=new FormData();form.set('multi_user_rendered','1');form.set('multi_user_scenarios_rendered','1');
form.set('multi_user_scenario_noisy_neighbors','on');form.set('multi_user_scenario_cancellation','on');
form.set('multi_user_noisy_neighbors_heavy_user_ids','user-03, user-04');
form.set('multi_user_noisy_neighbors_heavy_user_count','2');form.set('multi_user_noisy_neighbors_max_slowdown','1.5');
form.set('multi_user_noisy_neighbors_profiles_rendered','1');form.set('multi_user_noisy_neighbors_profile_short_long','on');
form.set('multi_user_noisy_neighbors_triggers_rendered','1');form.set('multi_user_noisy_neighbors_trigger_before_output','on');
form.set('multi_user_cancellation_user_id','user-02');form.set('multi_user_cancellation_cancel_after_chunks','3');
const result=configureMultiUser(config,form,bootstrap.multi_user.defaults);
assert.equal(JSON.stringify(result.multi_user.scenarios),'["noisy_neighbors","cancellation"]');
assert.equal(JSON.stringify(result.multi_user.noisy_neighbors.heavy_profiles),'["short_long"]');
assert.equal(JSON.stringify(result.multi_user.noisy_neighbors.triggers),'["before_output"]');
assert.equal(settingPath('multi_user_cancellation_user_id'),'multi_user.cancellation.user_id');
assert.equal(settingPath('multi_user_noisy_neighbors_max_slowdown'),'multi_user.noisy_neighbors.max_slowdown');
assert.equal(settingDestination('multi_user.noisy_neighbors.heavy_user_ids.0').name,'multi_user_noisy_neighbors_heavy_user_ids');
console.log(JSON.stringify(result));
''', configuration())
    resolved = RunConfig.model_validate(value)
    assert resolved.multi_user.noisy_neighbors.heavy_user_ids == ['user-03', 'user-04']
    assert resolved.multi_user.cancellation.cancel_after_chunks == 3


def test_unchanged_form_preserves_imported_scenario_order():
    config = configuration()
    config.multi_user.scenarios = ['cancellation', 'noisy_neighbors']
    saved = browser_logic(r'''
const form=new FormData();form.set('multi_user_rendered','1');form.set('multi_user_scenarios_rendered','1');
form.set('multi_user_scenario_noisy_neighbors','on');form.set('multi_user_scenario_cancellation','on');
console.log(JSON.stringify(configureMultiUser(config,form,bootstrap.multi_user.defaults)));
''', config)
    assert RunConfig.model_validate(saved).multi_user.scenarios == config.multi_user.scenarios


def test_scenario_results_show_separate_observations_and_request_links():
    from tests.test_multi_user_reporting import scenario_report
    summary = scenario_report().checks[0].metrics['multi_user']
    browser_logic('const summary='+json.dumps(summary)+r''';
const markup=renderMultiUserResults(summary);
for(const text of ['Matched noisy-neighbor comparison','Missing heavy overlap','Selected request cancellation','Peer continuation','Same-user follow-up','Backend compute and memory release: unverified'])assert(markup.includes(text));
assert.match(markup,/Heavy input \/ output medians: 640 \/ 32 tokens/);
assert.match(markup,/data-request="user-01-1"/);
assert.match(markup,/data-request="user-02-1"/);
''', configuration())


def test_imported_probe_order_survives_form_edits():
    browser_logic(r'''
config.multi_user.noisy_neighbors.heavy_profiles=['short_long','long_short'];
config.multi_user.noisy_neighbors.triggers=['during_stream','before_output'];
const form=new FormData();form.set('multi_user_rendered','1');
form.set('multi_user_noisy_neighbors_profiles_rendered','1');
form.set('multi_user_noisy_neighbors_triggers_rendered','1');
for(const name of config.multi_user.noisy_neighbors.heavy_profiles)form.set('multi_user_noisy_neighbors_profile_'+name,'on');
for(const name of config.multi_user.noisy_neighbors.triggers)form.set('multi_user_noisy_neighbors_trigger_'+name,'on');
let changed=configureMultiUser(config,form,bootstrap.multi_user.defaults);
assert.equal(JSON.stringify(changed.multi_user.noisy_neighbors),JSON.stringify(config.multi_user.noisy_neighbors));
form.set('multi_user_noisy_neighbors_profile_long_long','on');
form.delete('multi_user_noisy_neighbors_profile_long_short');
changed=configureMultiUser(config,form,bootstrap.multi_user.defaults);
assert.equal(JSON.stringify(changed.multi_user.noisy_neighbors.heavy_profiles),'["short_long","long_long"]');
assert.equal(JSON.stringify(changed.multi_user.noisy_neighbors.triggers),'["during_stream","before_output"]');
''', configuration())


def test_multi_user_form_and_drawer_roundtrip():
    result = browser_logic(r'''
const original=JSON.stringify(config),check=bootstrap.checks.find(c=>c.id==='multi_user');
const markup=renderTestCard(check,config);
assert.match(markup,/20 users/);assert.match(markup,/5 shared request slots/);
for(const name of ['levels','duration_seconds','warmup_seconds','seed','think_time_values','think_time_weights','profile_short_input_tokens','criteria_max_completion_ms'])assert(markup.includes('name="multi_user_'+name+'"'));
assert.doesNotMatch(markup,/name="test_multi_user_limit_/);
assert.equal(JSON.stringify(config),original);
const values=new FormData();values.set('multi_user_rendered','1');
values.set('multi_user_levels','1, 5, 20');values.set('multi_user_duration_seconds','2.5');
values.set('multi_user_warmup_seconds','0.2');values.set('multi_user_seed','17');
values.set('multi_user_think_time_values','0, 1');values.set('multi_user_think_time_weights','1, 3');
values.set('multi_user_shape_short_short','on');values.set('multi_user_shape_medium_long','on');
values.set('multi_user_criteria_max_completion_ms','250');values.set('multi_user_criteria_max_error_percent','');
const changed=configureMultiUser(config,values,bootstrap.multi_user.defaults);
assert.equal(changed.concurrency,5);assert.equal(JSON.stringify(changed.multi_user.levels),'[1,5,20]');
assert.equal(changed.multi_user.pass_criteria.max_error_percent,null);
assert.equal(settingDestination('multi_user.pass_criteria.max_completion_ms').name,'multi_user_criteria_max_completion_ms');
assert.equal(settingDestination('multi_user.levels.0').check,'multi_user');
assert.equal(settingPath('multi_user_profile_short_input_tokens'),'multi_user.profile.short.input_tokens');
console.log(JSON.stringify(changed));
''', configuration())
    resolved = RunConfig.model_validate(result)
    assert resolved.multi_user.duration_seconds == 2.5
    assert resolved.multi_user.think_time_seconds.weights == [1, 3]
    assert resolved.multi_user.shapes == ['short_short', 'medium_long']


def test_multi_user_results_retain_unserved_rows_and_request_drilldown():
    from tests.test_multi_user_metrics import observation, record
    from giraffe.multi_user_metrics import summarize
    records = [record('user-01')]
    summary = summarize(observation(records), records)
    browser_logic('const summary='+json.dumps(summary)+r''';
const markup=renderMultiUserResults(summary);
assert.match(markup,/Worst served user/);assert.match(markup,/user-01/);assert.match(markup,/user-02/);
assert.match(markup,/Shared-window output/);assert.match(markup,/unexercised/);
assert.match(markup,/data-request="request-user-01"/);
assert.match(markup,/0 \/ 0/);
assert.match(requestIdentityContent({id:'r',status:'completed',workload:{user_id:'user-01',population:20}},[]),/user-01/);
''', configuration())


def test_live_user_identity_survives_started_and_completed_events():
    job = {'requests': OrderedDict(), 'live_request_count': 0, 'evicted_running': set(),
           'live_request_summary': {'evicted': 0, 'through_slot': None, 'statuses': {}}}
    identity = {'user_id': 'user-01', 'population': 20, 'profile': 'short_short',
                'window_id': 'multi_user_20_measurement', 'phase': 'measurement', 'request_sequence': 2,
                'activation_seconds': .5}
    for kind in ['request_started', 'request_completed']:
        event = {'event': kind, 'request_slot': 0, 'target': 'local', 'scenario': 'multi_user_20_measurement',
                 'user': {**identity, 'prompt': 'must not reach live identity'}}
        if kind == 'request_completed':
            event['request'] = {'id': 'r', 'status': 'completed'}
        _track_live_request(job, event, 100)
        assert job['requests'][0]['workload'] == identity


async def test_population_preview_and_bound_label_are_independent_of_slots(tmp_path):
    from tests.test_web import browser
    values = configuration().model_dump(mode='json')
    async with browser(tmp_path) as client:
        preview = await client.post('/api/config/preview', json=values)
        assert preview.status_code == 200
        row = next(row for row in preview.json()['request_plan']['rows'] if row['id'] == 'multi_user_20')
        assert (row['users'], row['simultaneous_limit']) == (20, 5)
        values['multi_user']['levels'] = [33]
        response = await client.post('/api/config/validate', json=values)
        assert response.status_code == 422
        message = response.json()['detail'][0]['msg']
        assert 'User population levels' in message
        assert 'shared request slots' in message
        assert 'measured reliable client-load ceiling' not in message


def test_check_view_does_not_invent_aggregate_zero_counts():
    from tests.test_multi_user_metrics import observation, record
    from giraffe.multi_user_metrics import summarize
    records = [record('user-01')]
    summary = summarize(observation(records), records)
    browser_logic('const summary='+json.dumps(summary)+';const requests='+json.dumps([r.model_dump(mode='json') for r in records])+r''';
const check={id:'multi_user',title:'Multi-user',status:'inconclusive',required:true,summary:'Some users unexercised',metrics:{multi_user:summary},evidence_ids:requests.map(r=>r.id)};
const markup=renderCheck(check,{requests,checks:[check],manifest:{config}});
assert.match(markup,/Worst served user/);assert.doesNotMatch(markup,/<dt>Requests completed<\/dt>/);
assert.doesNotMatch(markup,/0 failed · 0 timed out/);
assert.doesNotMatch(markup,/class="evidence-table"/);
''', configuration())


def test_user_results_keep_horizontal_scroll_inside_the_table():
    from pathlib import Path
    from tests.test_ux_results import stylesheet_rules
    css = (Path(__file__).parents[1] / 'src/giraffe/static/styles.css').read_text()
    rules = {selector: declarations for selector, declarations, _ in stylesheet_rules(css)}
    assert rules['.findings'].get('grid-template-columns') == 'minmax(0,1fr)'
    assert rules['.multi-user-results .metric-table'].get('max-width') == '100%'
    assert rules['.multi-user-results .metric-table'].get('overflow-x') == 'auto'
    browser_logic(r'''
const html=renderMultiUserResults({windows:[{phase:'measurement',population:20,users:[]}]});
assert.match(html,/class="metric-table" tabindex="0" role="region" aria-label="Individual outcomes at 20 users"/);
''', configuration())


def test_shared_window_comparison_is_observed_not_a_median_or_confidence_claim():
    browser_logic(r'''
const row={metric:'shared_window_output_tokens_per_second',aggregation:'common_window',status:'pass',baseline:10,current:10,baseline_samples:5,current_samples:5,threshold:20,method:'Observed completed-output delivery over matching common windows; no cross-run confidence interval.'};
let html=comparisonTable([row]);
assert.match(html,/Common window · Observed/);assert.match(html,/no cross-run confidence interval/);
assert.doesNotMatch(html,/Median|No material regression|Statistical details/);
html=comparisonTable([{...row,current:5,status:'fail'}]);
assert.match(html,/Common window · Observed decrease/);
html=comparisonTable([{...row,current:null,status:'inconclusive',reason:'Incomplete coverage.'}]);
assert.match(html,/Common window · Warning/);assert.match(html,/Incomplete coverage/);
assert.match(comparisonTable([{...row,aggregation:'median',metric:'latency_ms'}]),/Median · No material regression/);
''', configuration())


def test_unchecked_shapes_remain_empty_while_omitted_partial_fields_are_preserved():
    result = browser_logic(r'''
const values=new FormData();values.set('multi_user_rendered','1');
assert.equal(JSON.stringify(configureMultiUser(config,values,bootstrap.multi_user.defaults).multi_user.shapes),JSON.stringify(config.multi_user.shapes));
for(const input of ['short','medium','long'])for(const output of ['short','medium','long'])values.set('multi_user_shape_'+input+'_'+output,'off');
const changed=configureMultiUser(config,values,bootstrap.multi_user.defaults);
assert.equal(JSON.stringify(changed.multi_user.shapes),'[]');console.log(JSON.stringify(changed));
''', configuration())
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match='shapes'):
        RunConfig.model_validate(result)


def test_fairness_and_conversation_settings_roundtrip():
    result = browser_logic(r'''
const markup=renderMultiUserEditor(config);
for(const name of ['criteria_max_throughput_ratio','fairness_tolerance_percent','conversation_profile','conversation_modes','conversation_turns','conversation_max_tokens','conversation_sample_turn_count'])assert(markup.includes('name="multi_user_'+name+'"'));
const values=new FormData();values.set('multi_user_rendered','1');
values.set('multi_user_criteria_max_throughput_ratio','1.5');
values.set('multi_user_fairness_tolerance_percent','5');
values.set('multi_user_conversation_profile','correctness');
values.set('multi_user_conversation_modes','live');
values.set('multi_user_conversation_turns','Remember the name.\nRepeat the name.');
values.set('multi_user_conversation_max_tokens','48');
values.set('multi_user_conversation_delay_seconds','0.1');
const changed=configureMultiUser(config,values,bootstrap.multi_user.defaults);
assert.equal(changed.multi_user.conversations.modes[0],'live');
assert.equal(changed.multi_user.conversations.turns.length,2);
assert.equal(settingDestination('multi_user.conversations.max_tokens').name,'multi_user_conversation_max_tokens');
assert.equal(settingDestination('multi_user.conversations.sampling.think_time_seconds.weights').name,'multi_user_conversation_weights_think_time_seconds');
assert.equal(settingPath('multi_user_conversation_task_0_turn_1'),'multi_user.conversations.workloads.0.turns.1');
console.log(JSON.stringify(changed));
''', configuration())
    settings = RunConfig.model_validate(result).multi_user
    assert settings.pass_criteria.max_throughput_ratio == 1.5
    assert settings.fairness_tolerance_percent == 5
    assert settings.conversations.max_tokens == 48


def test_fairness_population_and_conversation_links_render():
    browser_logic(r'''
const markup=renderMultiUserResults({windows:[{phase:'measurement',population:2,fairness:{status:'inconclusive',ratio:2,threshold:1.5,eligible_user_ids:['user-01'],compared_user_ids:['user-01','user-02'],reasons:['User 2 lacks overlap.']},users:[{user_id:'user-01',profile:'conversation:chat',conversations:[{id:'conversation-one',mode:'live',turns:['request-one'],planned_turns:2,stop_reason:'failed turn'}]}]}]});
assert.match(markup,/Observed delivery fairness/);assert.match(markup,/User 2 lacks overlap/);
assert.match(markup,/conversation-one/);assert.match(markup,/failed turn/);assert.match(markup,/data-request="request-one"/);
''', configuration())


def test_unchanged_multi_user_form_preserves_mode_order_and_dormant_tasks():
    config = RunConfig.model_validate({**configuration().model_dump(),
        'multi_user': {'levels': [2], 'conversations': {'modes': ['live', 'fixed'],
            'workloads': [{'name': 'draft', 'turns': ['Draft prompt', 'Draft follow-up']}]}}})
    result = browser_logic(r'''
const markup=renderMultiUserEditor(config);
const options=markup.match(/<select[^>]*name="multi_user_conversation_modes"[^>]*>(.*?)<\/select>/)[1];
const mode=options.match(/<option value="([^"]+)" selected>/)?.[1] || options.match(/<option value="([^"]+)"/)[1];
const values=new FormData();values.set('multi_user_rendered','1');
values.set('multi_user_conversation_profile','correctness');
values.set('multi_user_conversation_modes',mode);
console.log(JSON.stringify(configureMultiUser(config,values,bootstrap.multi_user.defaults)));
''', config)
    saved = RunConfig.model_validate(result).multi_user.conversations
    assert saved.modes == ['live', 'fixed']
    assert saved.workloads == config.multi_user.conversations.workloads


def test_first_performance_selection_uses_presets_and_deleted_tasks_stay_unselected():
    config = RunConfig.model_validate({**configuration().model_dump(),
        'multi_user': {'levels': [2], 'conversations': {'profile': 'performance', 'modes': ['live'],
            'workloads': [{'name': 'custom-1', 'turns': ['First', 'Second']}]}}})
    browser_logic(r'''
const selected=markup=>[...markup.matchAll(/name="multi_user_conversation_task_(\d+)_enabled" checked/g)].map(row=>Number(row[1]));
const fresh={...config,multi_user:{...config.multi_user,conversations:null}};
const presets=bootstrap.session_workloads.presets;
const performanceForm=()=>{
  const values=new FormData();values.set('multi_user_rendered','1');
  values.set('multi_user_conversation_profile','performance');
  values.set('multi_user_conversation_modes','fixed,live');
  values.set('multi_user_conversation_task_count',presets.length);
  return values;
};
const initial=performanceForm();
for(const i of selected(renderMultiUserConversationEditor(fresh.multi_user))){
  const key=`multi_user_conversation_task_${i}_`,task=presets[i];
  initial.set(key+'enabled','on');initial.set(key+'name',task.name);initial.set(key+'weight',task.weight);
  initial.set(key+'cap',task.max_tokens??'');initial.set(key+'turn_count',task.turns.length);
  task.turns.forEach((turn,j)=>initial.set(key+'turn_'+j,turn));
}
const first=configureMultiUser(fresh,initial,bootstrap.multi_user.defaults).multi_user.conversations;
assert(sameConfig(first.workloads,presets));
assert.equal(JSON.stringify(first.modes),'["live"]');
nodes['#new-run-form']={elements:{multi_user_conversation_task_0_name:{value:'custom-1'}}};
captureFormDraft=()=>{};
configChanged=()=>{};
let markup;
renderNew=()=>{markup=renderMultiUserEditor(state.config);};
editUserConversation('user-remove-task',0);
assert.equal(state.config.multi_user.conversations.workloads.length,0);
assert.equal(selected(markup).length,0);
const values=performanceForm();
const draft=configureMultiUser(state.config,values,bootstrap.multi_user.defaults,{allowInvalid:true});
assert.equal(draft.multi_user.conversations.workloads.length,0);
assert.throws(()=>configureMultiUser(state.config,values,bootstrap.multi_user.defaults),/Select at least one conversation task/);
''', config)


def test_unchanged_multi_user_form_preserves_multiline_recall_prompts():
    turns = ['Remember the name.\nReply with it only.', 'Repeat it.']
    config = RunConfig.model_validate({**configuration().model_dump(),
        'multi_user': {'levels': [2], 'conversations': {'turns': turns}}})
    result = browser_logic(r'''
const markup=renderMultiUserEditor(config);
const prompts=markup.match(/<textarea[^>]*name="multi_user_conversation_turns"[^>]*>([\s\S]*?)<\/textarea>/)[1];
const values=new FormData();values.set('multi_user_rendered','1');
values.set('multi_user_conversation_profile','correctness');
values.set('multi_user_conversation_turns',prompts);
console.log(JSON.stringify(configureMultiUser(config,values,bootstrap.multi_user.defaults)));
''', config)
    assert RunConfig.model_validate(result).multi_user.conversations.turns == turns


def test_multi_user_task_form_preserves_original_prompts_after_row_edits():
    prompts = ['\nFirst task', 'Second line\r\nNext']
    config = RunConfig.model_validate({**configuration().model_dump(),
        'multi_user': {'levels': [2], 'conversations': {'profile': 'performance', 'modes': ['live'],
            'workloads': [{'name': 'other-task', 'turns': ['First', 'Second']},
                          {'name': 'custom-task', 'turns': prompts}]}}})
    result = browser_logic(r'''
const markup=renderMultiUserConversationEditor(config.multi_user),values=new FormData();
const prefix='multi_user_conversation_',decode=text=>text.replace(/&quot;/g,'"');
const read=name=>markup.match(new RegExp('<input[^>]*name="'+name+'"[^>]*value="([^"]*)"'))?.[1];
values.set('multi_user_rendered','1');values.set(prefix+'profile','performance');
values.set(prefix+'task_count',read(prefix+'task_count'));
for(const index of [0,1]){
  const key=prefix+`task_${index}_`,task=config.multi_user.conversations.workloads[index];
  values.set(key+'enabled','on');values.set(key+'name',task.name);values.set(key+'weight',task.weight);
  values.set(key+'cap','');values.set(key+'turn_count',task.turns.length);
  const original=read(key+'original_turns');
  if(original!==undefined)values.set(key+'original_turns',decode(original));
  for(let turn=0;turn<task.turns.length;turn++){
    const name=key+'turn_'+turn;
    const content=markup.match(new RegExp('<textarea[^>]*name="'+name+'"[^>]*>([\\s\\S]*?)</textarea>'))[1];
    values.set(name,content.replace(/\r\n?/g,'\n').replace(/^\n/,''));
  }
}
const serialize=(source=config)=>configureMultiUser(source,values,bootstrap.multi_user.defaults);
assert(sameConfig(serialize().multi_user.conversations.workloads,config.multi_user.conversations.workloads));
values.delete(prefix+'task_0_enabled');values.set(prefix+'task_1_name','other-task');
values.set(prefix+'task_1_turn_1','Edited\nfollow-up');
console.log(JSON.stringify(serialize(serialize())));
''', config)
    saved, = RunConfig.model_validate(result).multi_user.conversations.workloads
    assert saved.name == 'other-task'
    assert saved.turns == [prompts[0], 'Edited\nfollow-up']
