"""Completed Results can restore saved checks directly into setup's Checks step."""

import json

import pytest

from giraffe.models import RunConfig

from tests.test_ui_navigation import run_browser_script
from tests.test_ux_catalogue import browser_logic
from tests.test_ux_results import run_render_checks


@pytest.mark.parametrize('verdict', ['pass', 'fail', 'inconclusive', 'blocked', 'skipped'])
@pytest.mark.parametrize('custom', [False, True])
def test_completed_results_offer_one_button_beside_closed_disclosure(verdict, custom):
    run_render_checks(r'''
const saved=report([check('generation','skipped',{summary:'Not selected for this run.'})]);
saved.overall=VERDICT;
saved.manifest.config.checks=CUSTOM?['json']:DEFAULT_CHECKS;
const html=detail(saved);
const buttons=document.querySelectorAll('[data-action="choose-next-checks"]');
assert.equal(buttons.length,1);
const button=buttons[0],row=button.closest('.checks-not-selected-row');
assert(row);assert.equal(button.parentNode,row);
assert.equal(button.tagName,'BUTTON');assert.equal(button.getAttribute('type'),'button');
assert.equal(button.getAttribute('class'),'button');assert.equal(button.textContent,'Choose checks for next run');
assert.equal(button.closest('summary'),null);assert.equal(button.closest('details'),null);
const disclosure=row.querySelector('.checks-not-selected');
assert(disclosure);assert.equal(disclosure.hasAttribute('open'),false);
assert.match(html,/<details class="checks-not-selected">/);
assert.equal(disclosure.querySelector('p').textContent,'No traffic was sent for these checks.');
'''.replace('VERDICT', repr(verdict)).replace('CUSTOM', str(custom).lower()).replace('DEFAULT_CHECKS', json.dumps(RunConfig(targets=[{'name': 'local', 'url': 'http://localhost/v1', 'model': 'test'}]).checks)), dom=True)


def test_button_requires_available_checks_and_completed_saved_report():
    run_render_checks(r'''
const saved=report([check('access','pass'),check('generation','skipped',{summary:'Not selected for this run.'})]);
for(const status of ['running','stopping','queued','error']){
 detail(saved,{state:status});assert.equal(document.querySelector('[data-action="choose-next-checks"]'),null);
}
const all=checkGroups.flatMap(group=>group.checks).filter(id=>bootstrap.checks.some(c=>c.id===id));
detail(report(all.map(id=>check(id,'pass'))));
assert.equal(document.querySelector('[data-action="choose-next-checks"]'),null);
detail(null,{config:saved.manifest.config});
assert.equal(document.querySelector('[data-action="choose-next-checks"]'),null);
detail(saved,{abort_reason:'cancelled by user'});
assert.equal(document.querySelectorAll('[data-action="choose-next-checks"]').length,1);
''', dom=True)


def test_click_restores_saved_configuration_and_resets_browse_state():
    browser_logic(r'''
const saved=structuredClone(config);saved.targets[0].url='http://saved.example/v1';saved.targets[0].model='saved-model';
saved.checks=['generation','json'];saved.restart_target='local';
state.detail={state:'completed',report:{manifest:{config:saved}},config:config,baseline_id:'saved-baseline'};
state.catalogueFilter='selected';state.catalogueCategory='performance';state.catalogueSearch='missing';state.returnToReview=true;
state.jsonDraft='dirty';state.formRestartTarget='local';state.modelDiscovery={models:['old']};
await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'choose-next-checks'}}:null}});
assert.equal(state.setupStep,2);assert.equal(state.returnToReview,false);assert.equal(state.focusSetupOnEntry,true);
assert.equal(state.catalogueFilter,'all');assert.equal(state.catalogueCategory,'all');assert.equal(state.catalogueSearch,'');
assert.equal(JSON.stringify(state.config.checks),JSON.stringify(saved.checks));assert.equal(state.config.targets[0].url,saved.targets[0].url);
assert.equal(state.config.targets[0].model,saved.targets[0].model);assert.equal(state.prefillBaseline,'saved-baseline');
assert.equal(state.config.restart_target,null);assert.equal(state.formRestartTarget,null);assert.equal(state.jsonDraft,null);assert.equal(state.modelDiscovery,null);
assert.equal(location.hash,'#new');
nodes['#main']={innerHTML:''};
nodes['#new-run-form']={elements:new Proxy({}, {get(target,name){return target[name] ||= {dataset:{},value:''};}})};
state.jsonDraft=state.jsonBase='synced';
syncTestControls=updateBounds=updateRestart=syncConfigView=syncPackControls=renderRequestPreview=syncSetupStep=renderModelDiscovery=()=>{};
renderNew();const html=nodes['#main'].innerHTML;
assert.match(html,/id="check-search"[^>]*value=""/);
for(const id of saved.checks)assert.match(html,new RegExp('name="check"[^>]*value="'+id+'"[^>]*checked'));
await rerun();assert.equal(state.setupStep,1);assert.equal(state.focusSetupOnEntry,false);
await rerun({step:2});assert.equal(state.focusSetupOnEntry,true);
await rerun();assert.equal(state.focusSetupOnEntry,false);
await rerun({step:2});assert.equal(state.focusSetupOnEntry,true);
await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'compare-run'}}:null}});
assert.equal(state.setupStep,1);assert.equal(state.focusSetupOnEntry,false);
saved.targets[0].model='  ';await rerun({step:2});assert.equal(state.setupStep,1);assert.equal(state.focusSetupOnEntry,false);
''')


def test_legacy_migration_and_route_guard_are_preserved():
    browser_logic(r'''
const saved=structuredClone(config);delete saved.config_version;
state.detail={config:saved,baseline_id:'baseline'};
api=async(path,options)=>{assert.equal(path,'/api/config/draft');assert.equal(options.body,JSON.stringify(saved));return {config, migration_notes:[]};};
await rerun({step:2});assert.equal(state.setupStep,2);assert.equal(state.prefillBaseline,'baseline');
const before=state.config;let resolve;api=()=>new Promise(done=>resolve=done);
const pending=rerun({step:2});state.routeVersion++;state.setupStep=3;
resolve({config:saved,migration_notes:[]});await pending;
assert.equal(state.config,before);assert.equal(state.setupStep,3);
''')


def test_new_route_focuses_checks_heading_once_after_rendering():
    run_browser_script(r'''
const page=browser();await page.run('init()');
await page.run(`
(async()=>{
 const saved=structuredClone(state.config);saved.targets[0].model='saved-model';
 state.detail={report:{manifest:{config:saved}}};
 await rerun({step:2});
 let rendered=false,focused=0,scrolled=0;
 renderNew=()=>{assert.equal(state.focusSetupOnEntry,false);rendered=true;};
 const originalSync=syncSetupStep;
 syncSetupStep=options=>{if(options?.focus){assert(rendered);focused++;assert.equal(state.setupStep,2);}};
 window.scrollTo=()=>{scrolled++;};
 await route();assert.equal(focused,1);assert(scrolled>0);
 await route();assert.equal(focused,1);
 syncSetupStep=originalSync;
})()
`);
''')



def test_step_two_requires_a_model_on_every_target():
    browser_logic(r"""
const saved=structuredClone(config);
saved.targets[0].model='first-model';
saved.targets.push({...saved.targets[0],name:'second',model:''});
state.detail={report:{manifest:{config:saved}}};
await rerun({step:2});assert.equal(state.setupStep,1);assert.equal(state.focusSetupOnEntry,false);
saved.targets[1].model='  ';
await rerun({step:2});assert.equal(state.setupStep,1);assert.equal(state.focusSetupOnEntry,false);
saved.targets[1].model='second-model';
await rerun({step:2});assert.equal(state.setupStep,2);assert.equal(state.focusSetupOnEntry,true);
""")


def test_post_run_guard_ignores_choose_next_checks_until_it_expires():
    run_render_checks(r"""
(async()=>{
 let now=1000;Date.now=()=>now;let reruns=0;
 rerun=async options=>{assert.equal(options.step,2);reruns++;};schedulePoll=()=>{};
 state.detail={id:'live',state:'running',config:{targets:[]},progress:{},events:[]};renderDetail();
 state.detail={id:'live',state:'completed',report:report([
  check('access','pass'),check('generation','skipped',{summary:'Not selected for this run.'})
 ])};renderDetail();
 const configBefore=state.config,stepBefore=state.setupStep,hashBefore=location.hash;
 await listeners.click({target:$('[data-action="choose-next-checks"]')});assert.equal(reruns,0);
 assert.equal(state.config,configBefore);assert.equal(state.setupStep,stepBefore);assert.equal(location.hash,hashBefore);
 now+=601;await listeners.click({target:$('[data-action="choose-next-checks"]')});assert.equal(reruns,1);
})()
""", dom=True)



def test_omitted_unselected_rows_still_offer_next_run_checks():
    run_render_checks(r"""
const ids=['access','serving','first_output','generation','load','context','correctness','json','cancellation','recovery','gpu'];
for(const id of checkGroups.flatMap(group=>group.checks))assert(bootstrap.checks.some(c=>c.id===id));
const saved=report(ids.map((id,index)=>check(id,index<4?'fail':'pass')),{overall:'fail'});
const html=detail(saved);
assert.match(document.querySelector('.check-count').textContent,/11 checks · 4 need attention · 7 passed/);
const buttons=document.querySelectorAll('[data-action="choose-next-checks"]');
assert.equal(buttons.length,1);
const row=buttons[0].closest('.checks-not-selected-row');assert(row);assert.equal(row.childNodes.length,1);
assert.equal(buttons[0].textContent,'Choose checks for next run');
assert.equal(document.querySelector('.checks-not-selected'),null);
assert.doesNotMatch(html,/<details class="checks-not-selected">/);
""", dom=True)


def test_available_checks_match_setup_catalogue_and_missing_bootstrap_fallback():
    run_render_checks(r"""
const saved=report([check('access','pass')]);
state.bootstrap={...bootstrap,checks:bootstrap.checks.filter(c=>c.id==='access')};
state.bootstrap.checks.push({id:'baseline'});
detail(saved);assert.equal(document.querySelector('[data-action="choose-next-checks"]'),null);
state.bootstrap.checks.push(bootstrap.checks.find(c=>c.id==='multi_user'));
detail(saved);assert.equal(document.querySelectorAll('[data-action="choose-next-checks"]').length,1);
state.bootstrap=null;
state.detail={state:'completed',report:saved};
assert.match(renderChecks(saved),/data-action="choose-next-checks"/);
saved.manifest.config.checks=checkGroups.flatMap(group=>group.checks);
assert.doesNotMatch(renderChecks(saved),/data-action="choose-next-checks"/);
""", dom=True)


def test_legacy_selection_falls_back_to_report_rows():
    run_render_checks(r"""
const saved=report([check('access','pass')]);delete saved.manifest.config.checks;
detail(saved);assert.equal(document.querySelector('[data-action="choose-next-checks"]'),null);
saved.checks.push(check('multi_user','skipped',{summary:'Not selected for this run.'}));
detail(saved);
assert.equal(document.querySelectorAll('[data-action="choose-next-checks"]').length,1);
const disclosure=document.querySelector('.checks-not-selected');
assert.equal(disclosure.hasAttribute('open'),false);
assert(disclosure.closest('.checks-not-selected-row'));
assert.equal(disclosure.querySelector('summary').textContent,'Not selected: Multi-user');
assert.equal(disclosure.querySelector('p').textContent,'No traffic was sent for these checks.');
detail(saved,{state:'error'});
assert.equal(document.querySelector('[data-action="choose-next-checks"]'),null);
assert.equal(document.querySelector('.checks-not-selected-row'),null);
assert(document.querySelector('.checks-not-selected'));
""", dom=True)


def test_lone_button_precedes_no_selected_results_message():
    run_render_checks(r"""
const html=detail(report([]));
assert.equal(document.querySelectorAll('[data-action="choose-next-checks"]').length,1);
assert.equal(document.querySelector('.checks-not-selected'),null);
assert(html.indexOf('data-action="choose-next-checks"')<html.indexOf('No selected checks produced a result.'));
""", dom=True)
