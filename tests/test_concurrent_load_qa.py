"""Final Load test QA: validation destinations, plans and honest result copy."""
import json

import pytest
from pydantic import ValidationError

from giraffe.capabilities import capabilities
from giraffe.load_test import result
from giraffe.models import RunConfig
from giraffe.planning import request_plan
from giraffe.reporting import _load_test_html
from giraffe.web import _configuration_errors
from tests.test_concurrent_load import TARGET, configuration, perform


pytestmark = pytest.mark.usefixtures("local_tokenizer")


def test_final_default_window_and_bootstrap_match():
    config = RunConfig(targets=[TARGET], checks=["load"])
    plan = request_plan(config)
    assert config.load_test.schedule.duration_seconds == 15
    assert plan['load_test']['scheduled_tests'] == 12
    assert plan['configured_load_seconds'] == 180
    assert capabilities()['load_test']['defaults'] == config.load_test.model_dump(mode='json')


@pytest.mark.parametrize('levels,overlap', [([8], False), ([4], True)])
def test_final_allocation_error_keeps_both_edit_destinations(levels, overlap):
    values = configuration().model_dump()
    values.update(concurrency=4, overlap_models=overlap)
    values['load_test']['patterns']['concurrent']['levels'] = levels
    if overlap:
        values['targets'].append({**TARGET, 'name': 'other'})
    with pytest.raises(ValidationError) as error:
        RunConfig.model_validate(values)
    message = _configuration_errors(error.value.errors(), values)[0]
    assert 'ceiling' in message['msg']
    assert message['fields'] == ['load_test.patterns.concurrent.levels', 'concurrency']
    assert {a['field'] for a in message['actions']} == set(message['fields'])


def test_final_interference_level_one_has_actionable_shared_controls():
    values = configuration().model_dump()
    values['load_test']['workloads']['interference']['enabled'] = True
    with pytest.raises(ValidationError) as error:
        RunConfig.model_validate(values)
    message = _configuration_errors(error.value.errors(), values)[0]
    assert '2' in message['msg']
    assert 'load_test.patterns.concurrent.levels' in message['fields']
    values['load_test']['patterns']['concurrent']['levels'] = [1, 2]
    assert RunConfig.model_validate(values).load_test.workloads.interference.enabled


@pytest.mark.parametrize('path,value,label', [
    ('profile.medium.input_tokens', 1, 'Medium'),
    ('profile.long.output_tokens', 0, 'Long'),
    ('patterns.concurrent.levels', [2, 2], 'levels'),
    ('workloads.combinations.selected', ['wrong_shape'], 'combination'),
])
def test_final_validation_labels_and_exact_field(path, value, label):
    values = configuration().model_dump()
    node = values['load_test']
    fields = path.split('.')
    for field in fields[:-1]:
        node = node[field]
    node[fields[-1]] = value
    with pytest.raises(ValidationError) as error:
        RunConfig.model_validate(values)
    message = _configuration_errors(error.value.errors(), values)[0]
    assert label.lower() in message['msg'].lower()
    assert any(field.startswith('load_test.' + path) for field in message['fields'])


async def test_final_summary_counts_sequential_tests_without_duplicate_status():
    _, check, _, _ = await perform(configuration(load={
        'combinations': {'selected': ['short_short', 'long_short']},
        'pass_criteria': {'max_completion_ms': 1000}}))
    assert check.summary == '2 of 2 load conditions passed.'
    assert check.summary.count('passed') == 1
    assert 'pass · pass' not in _load_test_html(check.metrics)


async def test_truncated_window_never_claims_highest_accepted():
    _, check, _, _ = await perform(configuration(max_requests=5,
        load={'pass_criteria': {'max_completion_ms': 1000}}))
    assert any(p['cut_short'] for p in check.metrics['points'])
    assert check.status == 'inconclusive'
    assert check.metrics['highest_accepted'] == []
    html = _load_test_html(check.metrics)
    assert 'absolute capacity limits' in html


def test_cli_preview_preserves_final_plan_and_warnings(tmp_path, capsys):
    from giraffe.cli import main
    source = tmp_path / 'load.json'
    source.write_text(configuration(max_duration_seconds=.1).model_dump_json())
    assert main(['preview', '--config', str(source)]) == 0
    output = json.loads(capsys.readouterr().out)
    row = next(r for r in output['request_plan']['rows'] if r['id'] == 'load_combinations')
    assert row['requests_per_target'] is None
    assert output['request_plan']['load_test']['unknown_requests']
    assert output['request_plan']['time_warnings']


async def test_final_html_reports_per_response_clock_and_finish_denominator():
    _, check, _, _ = await perform(configuration(load={'pass_criteria': {'max_completion_ms': 1000}}))
    html = _load_test_html(check.metrics)
    for text in ['Timing clock:', 'Actual input tokens', 'unknown input/output',
                 'Error denominator:', 'completions after sending stopped',
                 'Correct work meeting limits', 'highest tested']:
        assert text in html


@pytest.mark.parametrize('status', ['fail', 'inconclusive', 'blocked'])
def test_nonpassing_final_summary_names_only_deciding_points_and_limits_them(status, monkeypatch):
    from giraffe import load_test
    from giraffe.runner import _Run
    configured = configuration()
    run = _Run(configured, None, None)
    points = [{"id": f"point-{i}", "source": "primary", "type": "combinations", "workload": "short_short",
               "shape": "short_short", "pattern": "concurrent", "level": i + 1, "rate": None,
               "bursts": None, "status": status if i < 5 else "pass", "reason": f"Deciding reason {i}"}
              for i in range(6)]
    run.observations['local']['load_test'] = {'points': points}
    monkeypatch.setattr(load_test, 'assess_point', lambda *_: None)
    monkeypatch.setattr(load_test, 'publish_point', lambda *_: None)
    summary = result(run, configured.targets[0]).summary
    assert summary.startswith('5 of 6 load conditions ')
    assert len(summary.split('. ')) == 2
    assert all(f'Deciding reason {i}' not in summary for i in range(6))
    assert [point['reason'] for point in points] == [f'Deciding reason {i}' for i in range(6)]


def test_singular_plural_final_drawer_plan_and_results():
    from tests.test_ux_catalogue import browser_logic
    configured = configuration()
    browser_logic('const plan=' + json.dumps(request_plan(configured)) + r''';
config.checks=['load'];
let html=renderLoadEditor(config)+renderLoadPlan(config,plan);
assert.match(html,/Scheduled load tests/);assert.match(html,/Send new requests for/);
const point={id:'one',source:'primary',type:'combinations',workload:'short_short',shape:'short_short',pattern:'concurrent',level:1,groups:[]};
html+=renderLoadTestResults({points:[point]});assert.match(html,/1 concurrent request/);assert.doesNotMatch(html,/1 concurrent requests/);
point.level=2;assert.match(renderLoadTestResults({points:[point]}),/2 concurrent requests/);
config.load_test.profile.long.output_tokens=1;config.load_test.workloads.stress.enabled=true;config.load_test.workloads.stress.min_output_percent=100;
assert.match(renderLoadEditor(config),/At least 1 generated token/);
''', config=configured)


@pytest.mark.parametrize('width', [375, 1440])
def test_final_profile_grids_and_result_tables_fit_viewport(width):
    from pathlib import Path
    from tests.test_ux_catalogue import browser_logic
    playwright = pytest.importorskip('playwright.sync_api', reason='Browser layout checks require Playwright and Chromium')
    html = browser_logic(r'''
config.load_test=loadSettings(config);config.load_test.workloads.mixed.enabled=true;config.load_test.workloads.mixed.share_mode='custom';config.load_test.workloads.mixed.selected=[...loadShapes];
config.checks=['load'];state.loadPanels={combinations:true,mixed:true,interference:true,stress:true};
console.log(JSON.stringify('<form id="new-run-form">'+renderTestCard(bootstrap.checks.find(c=>c.id==='load'),config)+'</form><section class="check-detail">'+renderLoadTestResults({points:[{id:'one',type:'combinations',shape:'short_short',pattern:'concurrent',level:1,groups:[{id:'group',shape:'short_short'}]}]})+'</section>'));
''')
    styles = (Path(__file__).parents[1] / 'src/giraffe/static/styles.css').read_text()
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={'width': width, 'height': 900}, offline=True)
            page.emulate_media(reduced_motion='reduce')
            page.set_content('<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><style>' + styles + '</style>' + html)
            editor = page.locator('#settings-load')
            editor.evaluate("node => {node.hidden=false;node.closest('.test-card').classList.add('is-editing');}")
            assert editor.is_visible()
            assert page.locator('#load-shares').is_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            headers = page.locator('.load-profile thead th:not(:empty)')
            assert headers.count() == 2
            assert headers.evaluate_all('''cells => cells.every(cell => {
                const text=document.createRange();text.selectNodeContents(cell);
                const rect=text.getBoundingClientRect(),box=cell.getBoundingClientRect();
                return rect.right<=box.right+1 && rect.left>=box.left-1;
            })''')
            for kind in ['input', 'output']:
                bounds = page.locator(f'.load-profile input[name$="_{kind}_tokens"]').evaluate_all(
                    'inputs=>inputs.map(input=>{const r=input.getBoundingClientRect();return [r.x,r.width];})')
                assert len(bounds) == 3 and all(row == bounds[0] for row in bounds)
            for selector, count in [('.load-shape-grid', 2), ('.load-share-grid', 1)]:
                grids = page.locator(selector + ':visible')
                assert grids.count() == count
                assert grids.evaluate_all('''grids => grids.every(grid => {
                    const cells=[...grid.children].map(cell=>cell.getBoundingClientRect()),r=grid.getBoundingClientRect();
                    return r.width>0 && cells.length===9 && cells.every(cell=>cell.width>0 && cell.height>0
                        && cell.left>=r.left-1 && cell.right<=r.right+1)
                        && cells[0].left<cells[1].left && cells[1].left<cells[2].left
                        && Math.abs(cells[3].left-cells[0].left)<1;
                })''')
            assert page.locator('.load-results-table').evaluate_all(
                'tables=>tables.every(node=>node.scrollWidth>node.clientWidth && getComputedStyle(node).overflowX==="auto")')
        finally:
            browser.close()


# Fake inference clients in this module use the matching local tokenizer.
