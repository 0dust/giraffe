"""Regressions from browser QA of the complete Concurrent load flow."""
import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from giraffe.capabilities import capabilities
from giraffe.concurrent_load import result, score_point, test_points as load_points
from giraffe.models import RunConfig
from giraffe.planning import request_plan
from giraffe.runner import _Run
from giraffe.web import _configuration_errors
from tests.test_concurrent_load import TARGET, configuration, perform, point
from tests.test_ux_catalogue import browser_logic


def test_default_window_has_no_throughput_warning_and_bootstrap_matches():
    config = RunConfig(targets=[TARGET])
    plan = request_plan(config)
    assert config.concurrent_load.schedule.duration_seconds == 15
    assert plan['configured_load_seconds'] == 120
    assert not any('throughput' in warning['msg'] for warning in plan['load_warnings'])
    assert capabilities()['concurrent_load']['defaults']['schedule']['duration_seconds'] == 15


def test_short_window_warning_uses_readable_shape_and_one_responsible_field():
    config = configuration(load={'combinations': {'selected': ['short_long']}})
    warnings = [row for row in request_plan(config)['load_warnings'] if 'throughput' in row['msg']]
    assert len(warnings) == 1
    assert warnings[0]['msg'].startswith('Short input → Long output:')
    assert warnings[0]['fields'] == ['concurrent_load.schedule.duration_seconds']
    browser_logic(r'''
config.checks=['concurrency'];state.jsonDraft=state.jsonBase='synced';
const warning=WARNING;
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan:{load_warnings:[warning]}};
for(const name of ['load_schedule_duration_seconds','load_profile_long_output_tokens']){
 const attributes={};nodes['#field-'+name]={getAttribute:key=>attributes[key]||'',setAttribute:(key,value)=>attributes[key]=value,removeAttribute:key=>delete attributes[key]};
 nodes['#field-'+name+'-warning']={hidden:true,innerHTML:''};
}
syncInlineErrors();
assert.equal(nodes['#field-load_profile_long_output_tokens-warning'].hidden,true);
assert.equal(nodes['#field-load_schedule_duration_seconds-warning'].hidden,false);
assert.equal(nodes['#field-load_schedule_duration_seconds-warning'].innerHTML.split(warning.msg).length-1,1);
assert.equal(renderConcurrentPlan(config,currentRequestPlan()).split(warning.msg).length-1,1);
'''.replace('WARNING', json.dumps(warnings[0])))


@pytest.mark.parametrize('levels,overlap,expected', [
    ([8], False, 'Concurrency level 8 exceeds Maximum at once (4).'),
    ([4], True, 'Concurrency level 4 × 2 endpoints exceeds Maximum at once (4).'),
])
def test_allocation_errors_are_readable_and_keep_edit_destinations(levels, overlap, expected):
    values = {'targets': [TARGET], 'checks': ['concurrency'], 'concurrency': 4,
              'concurrent_load': {'schedule': {'levels': levels}}}
    if overlap:
        values.update(overlap_models=True, targets=[TARGET, {**TARGET, 'name': 'other'}])
    with pytest.raises(ValidationError) as error:
        RunConfig.model_validate(values)
    message = _configuration_errors(error.value.errors(), values)[0]
    assert message['msg'] == expected
    assert message['fields'] == ['concurrent_load.schedule.levels', 'concurrency']


def test_interference_error_explains_how_to_fix_level_one():
    values = configuration().model_dump()
    values['concurrent_load']['interference']['enabled'] = True
    with pytest.raises(ValidationError) as error:
        RunConfig.model_validate(values)
    message = _configuration_errors(error.value.errors(), values)[0]
    assert message['msg'] == ('Long input during an active short stream needs at least 2 concurrent requests at every level. '
                              'Remove level 1 from Concurrency levels.')
    assert message['fields'] == ['concurrent_load.schedule.levels', 'concurrent_load.interference.enabled']


@pytest.mark.parametrize('path,value,fragment', [
    ('profile.medium.input_tokens', 1, 'Medium input target'),
    ('profile.long.output_tokens', 256, 'Long output cap'),
    ('schedule.levels', [2, 2], 'whole numbers from 1 to 256'),
    ('interference.trigger', 'bad', 'Introduce the long input'),
    ('interference.trigger_chunks', 0, 'Answer chunks per short stream'),
    ('mixed.shares', {'wrong_shape': 100}, 'Share of requests'),
    ('combinations.selected', ['wrong_shape'], 'input/output combination'),
])
def test_validation_messages_use_labels_instead_of_internal_names(path, value, fragment):
    values = configuration().model_dump()
    destination = values['concurrent_load']
    parts = path.split('.')
    for name in parts[:-1]:
        destination = destination[name]
    destination[parts[-1]] = value
    with pytest.raises(ValidationError) as error:
        RunConfig.model_validate(values)
    message = _configuration_errors(error.value.errors(), values)[0]['msg']
    assert fragment in message
    assert not any(text in message for text in ['>=', 'concurrent_load', 'input_tokens', 'output_tokens', 'wrong_shape', 'first_output', 'after_chunks'])


def all_type_run():
    config = configuration(load={'combinations': {'selected': ['short_short', 'long_short', 'short_long']},
        'mixed': {'enabled': True}, 'interference': {'enabled': True}, 'stress': {'enabled': True},
        'schedule': {'levels': [2, 4]}})
    run = _Run(config, None, None)
    groups = {}
    for p in load_points(config.concurrent_load):
        p.update(status='pass', reason='Observed evidence met the configured criteria', reason_code='criteria_met')
        groups.setdefault(p['type'], {'title': p['type'], 'points': []})['points'].append(p)
    run.observations['local']['concurrent_load'] = {'load_types': groups}
    return run, [p for group in groups.values() for p in group['points']]


def test_passing_summary_counts_points_types_and_budget_limited_windows():
    run, points = all_type_run()
    for p in points[:2]:
        p.update(cut_short=True, reason_code='budget_limited', reason='Budget-limited window: ran 1.85 s of 3 s')
    check = result(run, run.config.targets[0])
    assert check.summary == 'All 14 test points passed across 4 load types (2 ran budget-limited windows).'
    assert check.status == 'pass'
    assert 'pass ·' not in check.summary


@pytest.mark.parametrize('status', ['fail', 'inconclusive', 'blocked'])
def test_nonpassing_summary_only_names_deciding_points_and_limits_them(status):
    run, points = all_type_run()
    for index, p in enumerate(points[:5]):
        p.update(status=status, reason=f'Deciding reason {index + 1}')
    summary = result(run, run.config.targets[0]).summary
    assert 'and 2 more' in summary
    for p in points[:3]:
        assert p['reason'] in summary
    for p in points[3:]:
        assert p['reason'] not in summary
    assert 'short_short' not in summary


async def test_budget_limited_pass_reason_has_no_status_prefix():
    run, check, _, _ = await perform()
    p = copy.deepcopy(point(check))
    p.update(cut_short=True, window_reason='request allowance', configured_window_seconds=3, delivered_window_seconds=1.85)
    score_point(run.config.concurrent_load, p, run.records)
    assert p['status'] == 'pass'
    assert p['reason'] == 'Budget-limited window: ran 1.85 s of 3 s'


def test_singular_and_plural_load_counts_across_drawer_review_and_results():
    config = configuration()
    row = next(row for row in request_plan(config)['rows'] if row['id'] == 'load_combinations')
    assert row['summary'].startswith('1 workload × 1 level × 1 s')
    browser_logic(r'''
config.checks=['concurrency'];
let html=renderConcurrentEditor(config)+renderRunBlueprint(config);
assert.match(html,/1 workload × 1 level × 1 s/);
assert.match(html,/1 combination × 1 level/);
assert.match(html,/1 test point · highest level 1 concurrent request/);
assert.match(effectiveCheckSummary(bootstrap.checks.find(c=>c.id==='concurrency'),config),/1 load type/);
const point={id:'load_stress_output_c1',type:'stress',workload:'output',level:1,status:'pass',token_lengths:{}};
const metrics={load_types:{stress:{points:[point]}}};
html+=renderLoadResults(metrics)+keyMetric({id:'concurrency',metrics})+phaseName('load_combo_short_short_c1')+phaseName('load_mixed_c1')+phaseName('load_stress_output_c1');
assert.match(html,/1 test point · 1 load type/);
assert.match(html,/Large-output stress at 1 concurrent request/);
assert.doesNotMatch(html,/\b1 (?:workloads|levels|combinations|test points|load types|concurrent requests)\b/);
config.concurrent_load.schedule.levels=[1,4];config.concurrent_load.combinations.selected=['short_short','long_short'];
html=renderConcurrentPlan(config);assert.match(html,/2 workloads × 2 levels/);assert.match(html,/4 test points/);
for(const size of loadSizes)config.concurrent_load.profile[size]={input_tokens:1,output_tokens:1};
html=renderConcurrentEditor(config);
assert.match(html,/At least 1 generated token per long-output response/);
assert.match(html,/1 input token · 1 output token/);
assert.doesNotMatch(html,/\b1 (?:generated tokens|output tokens|input tokens)\b/);
''', config=config)


def test_cli_preview_preserves_readable_warnings_and_singular_plan(tmp_path, capsys):
    from giraffe.cli import main
    source = tmp_path / 'load.json'
    source.write_text(configuration(load={'combinations': {'selected': ['short_long']}}).model_dump_json())
    assert main(['preview', '--config', str(source)]) == 0
    output = json.loads(capsys.readouterr().out)
    row = next(row for row in output['request_plan']['rows'] if row['id'] == 'load_combinations')
    assert row['summary'].startswith('1 workload × 1 level × 1 s')
    assert any(warning.startswith('Short input → Long output:') for warning in output['request_plan']['warnings'])


async def test_html_report_has_singular_conditions_and_no_duplicate_status_prefix():
    from giraffe.reporting import _concurrent_load_html
    _, check, _, _ = await perform()
    p = point(check)
    p['reason'] = 'Budget-limited window: ran 1.85 s of 3 s'
    html = _concurrent_load_html(check.metrics)
    assert 'at 1 concurrent request' in html
    assert 'Highest observed among tested settings: 1 concurrent request.' in html
    assert 'pass · Budget-limited window: ran 1.85 s of 3 s' in html
    assert 'pass · pass' not in html and '1 concurrent requests' not in html


@pytest.mark.parametrize('width', [375, 1440])
def test_profile_grids_and_result_tables_fit_viewport(width):
    playwright = pytest.importorskip('playwright.sync_api', reason='Browser layout checks require Playwright and Chromium')
    run, _ = all_type_run()
    metrics = run.observations['local']['concurrent_load']
    html = browser_logic(r'''
config.checks=['concurrency'];state.openTest='concurrency';
state.loadPanels={combinations:true,mixed:true,interference:true,stress:true};
console.log(JSON.stringify('<form id="new-run-form">'+renderTestCard(bootstrap.checks.find(c=>c.id==='concurrency'),config)+'</form><section class="check-detail">'+renderLoadResults(METRICS)+'</section>'));
'''.replace('METRICS', json.dumps(metrics)))
    styles = (Path(__file__).parents[1] / 'src/giraffe/static/styles.css').read_text()
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={'width': width, 'height': 900}, offline=True)
            page.emulate_media(reduced_motion='reduce')
            page.set_content('<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
                             '<style>' + styles + '</style>' + html)
            page.evaluate('Promise.all(document.getAnimations().map(animation=>animation.finished))')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            assert page.locator('.load-profile thead th').evaluate_all('''cells => cells.every(cell => {
                const text=document.createRange();text.selectNodeContents(cell);
                const rect=text.getBoundingClientRect(),box=cell.getBoundingClientRect();
                return rect.right<=box.right+1 && rect.left>=box.left-1;
            })''')
            for kind in ['input', 'output']:
                bounds = page.locator(f'.load-profile input[name$="_{kind}_tokens"]').evaluate_all(
                    'inputs=>inputs.map(input=>{const r=input.getBoundingClientRect();return [r.x,r.width];})')
                assert len(bounds) == 3 and all(row == bounds[0] for row in bounds)
            for selector in ['.load-shape-grid', '.load-share-grid']:
                assert page.locator(selector).evaluate('''grid => {
                    const cells=[...grid.children].map(cell=>cell.getBoundingClientRect()),r=grid.getBoundingClientRect();
                    return cells.length===9 && cells.every(cell=>cell.left>=r.left-1 && cell.right<=r.right+1)
                        && cells[0].left<cells[1].left && cells[1].left<cells[2].left
                        && Math.abs(cells[3].left-cells[0].left)<1;
                }''')
            assert page.locator('.load-results-table').count() == 4
            assert page.locator('.load-results-table').evaluate_all('''tables => tables.every(table => {
                table.scrollLeft=100;
                return table.scrollWidth>table.clientWidth && table.scrollLeft>0;
            })''')
        finally:
            browser.close()
