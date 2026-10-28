"""Display names and optional server observations share the public contract."""
from types import SimpleNamespace

import pytest

from giraffe.capabilities import capabilities
from giraffe.models import (CHECK_NAMES, CATALOGUE_NAMES, INTERNAL_CHECK_NAMES,
                            CheckResult, RequestRecord, RunConfig, overall_status)
from giraffe.runner import _checks

NAMES = {
    'access': 'Endpoint access', 'serving': 'Response validity',
    'first_output': 'Time to first answer', 'generation': 'Generation speed',
    'load': 'Load test', 'context': 'Long-context recall',
    'correctness': 'Answer correctness', 'json': 'JSON correctness',
    'cancellation': 'Cancellation & limits', 'recovery': 'Recovery after load',
    'gpu': 'GPU metrics', 'prefix': 'Prefix cache',
    'sessions': 'Multi-turn sessions', 'consistency': 'Answer consistency',
    'tools': 'Tool calling',
}


def test_one_display_name_catalogue():
    assert CHECK_NAMES == NAMES
    assert CATALOGUE_NAMES is CHECK_NAMES
    assert INTERNAL_CHECK_NAMES is CHECK_NAMES
    assert {row['id']: row['title'] for row in capabilities()['checks']} == NAMES


def observed_run(*, cached=0, correct=True, latency_limit=None):
    config = RunConfig(targets=[{'name': 'local', 'url': 'http://localhost:1', 'model': 'm'}],
                       checks=['prefix', 'gpu'], prefix={'repeats': 3},
                       limits={'min_samples': 2, 'latency_ms': latency_limit})
    records = [RequestRecord(
        id=f'{scenario}-{i}', target='local', fixture_id='prefix', scenario=scenario,
        check_ids=['prefix'], started_at='2026-10-29T00:00:00Z', status='completed',
        valid=True, score=correct, input_chars=4096, input_tokens=1024,
        cached_prompt_tokens=cached if scenario == 'prefix_shared' else 0,
        elapsed_ms=100, first_output_ms=20, workload={'shared_prefix_chars': 4096},
    ) for scenario in ['prefix_shared', 'prefix_independent'] for i in range(3)]
    return SimpleNamespace(config=config, records=records, max_lag_ms=0, pack_specs=[],
                           observations={'local': {'restart': None, 'metrics': []}},
                           unfinished={'local': set()})


@pytest.mark.parametrize('cached,correct,latency_limit', [(0, True, None), (None, True, None),
                                                        (1024, False, None), (1024, True, 1)])
def test_prefix_never_fails_or_changes_verdict(cached, correct, latency_limit):
    run = observed_run(cached=cached, correct=correct, latency_limit=latency_limit)
    checks = _checks(run, run.config.targets[0])
    prefix = next(c for c in checks if c.id == 'prefix')
    gpu = next(c for c in checks if c.id == 'gpu')
    assert not prefix.required and not gpu.required
    assert prefix.status != 'fail'
    if cached == 0 and correct and latency_limit is None:
        assert prefix.status == 'inconclusive'
        assert prefix.summary.startswith('Prefix caching not active')
    passing = CheckResult(id='access', target='local', title=NAMES['access'], status='pass', summary='Connected')
    assert overall_status([passing, prefix, gpu]) == 'pass'
    fault = gpu.model_copy(update={'status': 'fail', 'summary': 'Fresh XID error'})
    assert overall_status([passing, prefix, fault]) == 'fail'


def test_user_facing_vocabulary_has_no_retired_phrases():
    """CLI flags/config keys and deferred documentation prose are outside this check."""
    import re
    from pathlib import Path
    root = Path(__file__).parents[1]
    forbidden = re.compile(r'\b(?:reference baseline|AI evaluator|golden answers|Run limits|suite limits|Test results|simultaneous|parallelism|in[ -]flight|Flight plan|First output within|Connection check|Repeated connection checks|Saved runs on disk)\b', re.I)
    from giraffe.capabilities import CHECK_DETAILS
    assert not forbidden.search('\n'.join(CHECK_DETAILS.values()))
    for name in ['src/giraffe/static/app.js', 'src/giraffe/static/index.html',
                 'src/giraffe/reporting.py', 'src/giraffe/planning.py', 'README.md']:
        source = (root / name).read_text()
        # Display-only normalization keeps the delegated HTML renderer untouched.
        if name == 'src/giraffe/reporting.py':
            source = re.sub(r'\.replace\("[^"]*",\s*"[^"]*"\)', '', source)
        # README flag tables and commands document immutable CLI names.
        if name == 'README.md':
            source = '\n'.join(line for line in source.splitlines() if not line.startswith('| `--'))
        assert not forbidden.search(source), name
        assert not re.search(r'[\"\']Invalid responses[\"\']', source), name


def test_html_displays_current_names_without_rewriting_saved_titles():
    from giraffe.models import RunReport
    from giraffe.reporting import _render
    check = CheckResult(id='access', target='local', title='Network and access',
                        status='pass', summary='Connected')
    report = RunReport(run_id='saved', started_at='2026-10-29T00:00:00Z',
                       finished_at='2026-10-29T00:00:01Z', overall='pass',
                       checks=[check], manifest={'config': {'checks': ['access']}})
    html = _render(report)
    assert 'Endpoint access' in html
    assert 'Network and access' not in html
    assert report.checks[0].title == 'Network and access'


def test_generated_prefix_defaults_are_observational_without_rewriting_archived_flags():
    generated = CheckResult(id='prefix', target='local', title='Prefix cache', status='fail',
                            summary='no prefix reuse observed on a majority of repeats; 2 of 2 repeat misses.')
    assert generated.required is False
    assert generated.status == 'inconclusive'
    assert generated.summary.startswith('Prefix caching not active')
    archived = CheckResult(id='prefix', target='local', title='Old prefix title', status='fail',
                           required=True, summary='Historical failure')
    assert archived.required is True and archived.status == 'fail'
    assert archived.title == 'Old prefix title'


def test_request_plan_uses_check_names_for_initial_and_repeated_requests():
    from giraffe.planning import request_plan
    config = RunConfig(targets=[{'name': 'local', 'url': 'http://localhost:1', 'model': 'm'}])
    titles = {row['id']: row['title'] for row in request_plan(config)['rows']}
    assert titles['initial'] == CHECK_NAMES['access']
    assert titles['warm'] == CHECK_NAMES['serving']


def test_exported_html_uses_giraffe_version_and_requests_navigation():
    from giraffe.models import RunReport
    from giraffe.reporting import _render
    report = RunReport(run_id='copy', suite_version='0.1.0a1', started_at='2026-10-29T00:00:00Z',
                       finished_at='2026-10-29T00:00:01Z', overall='pass', checks=[],
                       manifest={'config': {'checks': []}})
    html = _render(report)
    assert 'Giraffe 0.1.0a1' in html
    assert 'suite 0.1.0a1' not in html
    assert '<a href="#requests">Requests</a>' in html
    assert '<h2>Requests</h2>' in html
    assert 'Requests in flight' not in html


def test_rendered_setup_and_html_have_no_remaining_vocabulary_drift():
    """Check visible text, excluding immutable keys, flags and HTML attributes."""
    from tests.test_ux_results import run_render_checks
    run_render_checks(r'''
const cfg=structuredClone(bootstrap.defaults);cfg.request_source='request_pack';cfg.request_pack={cases:[{fixture_id:'q',scorer:'judge',messages:[{role:'user',content:'Question'}],expected:'Answer'}]};state.config=cfg;
const html=renderJudging(cfg)+renderRequestSource(cfg)+renderSharedRunSettings(cfg);
const visible=html.replace(/<[^>]*>/g,' ');
assert.doesNotMatch(visible,/\b(?:evaluators?|flight plan|saved runs?|invalid responses|first output within|parallel|in flight)\b/i);
''')
    import inspect
    import re
    from giraffe import reporting
    from giraffe.models import RunReport
    html = reporting._render(RunReport(
        run_id='vocabulary', suite_version='0.1.0a1',
        started_at='2026-10-29T00:00:00Z', finished_at='2026-10-29T00:00:01Z',
        overall='pass', checks=[], manifest={'config': {'checks': []}}))
    visible = re.sub(r'<[^>]*>', ' ', html)
    assert not re.search(r'\b(?:evaluators?|flight plan|saved runs?|invalid responses|first output within|parallel|in flight|suite\s+\d)\b', visible, re.I)
    # Labels in the renderer itself are covered even when this fixture has no requests.
    renderer = re.sub(r'\.replace\("[^"]*",\s*"[^"]*"\)', '', inspect.getsource(reporting._render))
    assert '<h2>Request evidence</h2>' not in renderer
    assert 'Requests in flight' not in renderer
