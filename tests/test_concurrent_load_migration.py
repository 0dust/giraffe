import json
from pathlib import Path

import pytest

import yaml

from giraffe.caps import MAX_SIMULTANEOUS_REQUESTS
from giraffe.cli import main
from giraffe.models import Deployment, RunConfig, migrate_concurrent_load, migrate_load_test, migrate_run_limits
from tests.test_concurrent_load import TARGET, configuration

pytestmark = pytest.mark.usefixtures("local_tokenizer")


def load_saved(value):
    from giraffe.cli import migrated_configuration
    return migrated_configuration(value)


@pytest.mark.parametrize('source,old_value,fragment', [
    ('levels', [2, 4], 'schedule.levels'),
    ('input_chars', {'short': 73, 'medium': 999, 'long': 8765}, '73'),
    ('output_tokens', {'short': 5, 'medium': 40, 'long': 256}, 'profile.long.output_tokens=128'),
    ('samples_per_bucket', 17, 'samples_per_bucket=17'),
    ('mixed_pairs', 13, 'mixed_pairs=13'),
    ('heavy_weights', {'long_input': 3, 'long_output': 7}, "'long_output': 7"),
    ('window_seconds', 77, 'window_seconds=77'),
    ('stress_cases', [{'name': 'short_input_long_output', 'max_input_tokens': 80, 'output_tokens': 8192, 'min_output_tokens': 7200}], '7200'),
])
def test_migration_notes_actual_values_and_no_input_mutation(source, old_value, fragment):
    values = {'targets': [TARGET], 'checks': ['concurrency'], 'concurrent_workloads': {source: old_value}}
    before = json.dumps(values)
    limited, limit_notes = migrate_run_limits(values)
    migrated, notes, warnings = migrate_concurrent_load(limited)
    assert json.dumps(values) == before
    config = load_saved(values)
    _, combined_notes, warnings = migrate_load_test(migrated)
    assert config._migration_notes == list(dict.fromkeys([*limit_notes, *notes, *combined_notes]))
    assert fragment in '\n'.join(notes)
    assert config.load_test.workloads.mixed.enabled is False
    assert 'concurrent_workloads' not in config.model_dump() and 'buckets' not in config.model_dump()
    assert not load_saved(config.model_dump())._migration_notes
    if source == 'stress_cases':
        assert config.load_test.workloads.stress.cases == ['short_long']
    if source == 'levels':
        assert config.load_test.workloads.interference.enabled


@pytest.mark.parametrize('scenario', ['streaming_fairness', 'mixed_interference'])
@pytest.mark.parametrize('stream,levels,enabled', [(True, [2], True), (False, [2], False), (True, [1, 2], False)])
def test_interference_migration_never_produces_invalid_allocation(scenario, stream, levels, enabled):
    c = load_saved({'targets': [TARGET], 'checks': ['concurrency'], 'stream': stream,
        'concurrent_workloads': {'scenarios': [scenario], 'levels': levels}})
    assert c.load_test.workloads.interference.enabled is enabled
    assert ('load' in c.checks) is enabled
    text = ' '.join([*c._migration_notes, *(w['msg'] for w in c._migration_warnings)])
    if enabled:
        assert '2 concurrent requests' in text
    else:
        assert 'concurrency levels of at least 2' in text
        assert any(w['removed_check'] == 'load' for w in c._migration_warnings)
    if scenario == 'mixed_interference':
        assert 'long-output heavy partners and before-output ordering have no equivalent' in text


@pytest.mark.parametrize('field,actual', [('first_output_ms', 12), ('latency_ms', 44), ('stream_gap_ms', 19),
    ('min_output_tokens_per_second', 3), ('max_error_rate', .3), ('min_correctness', .8), ('min_samples', 13), ('fairness_max_ratio', 3)])
def test_legacy_limits_and_options_are_explicitly_dropped(field, actual):
    values = {'targets': [TARGET], 'checks': ['buckets'], 'limits': {field: actual},
        'test_options': {'concurrency': {'samples': 17, 'limits': {'first_output_ms': 10}}}}
    c = load_saved(values)
    notes = '\n'.join(c._migration_notes)
    assert f'limits.{field}={actual}' in notes
    assert 'test_options.concurrency.samples=17' in notes and 'test_options.concurrency.limits.first_output_ms=10' in notes
    assert c.test_options == {}
    assert c.load_test.pass_criteria.max_first_answer_ms == 2000
    assert c.load_test.pass_criteria.max_completion_ms == 30000
    if field in ('stream_gap_ms', 'min_output_tokens_per_second'):
        assert 'no replacement criterion here' in notes and 'other checks as before' in notes


def test_disabled_block_stays_disabled_and_absent_checks_preserve_legacy_default():
    values = {'targets': [TARGET], 'concurrent_workloads': {}}
    default = load_saved(values)
    assert default.load_test.workloads.combinations.selected == list(__import__('giraffe.models', fromlist=['SHAPES']).SHAPES)
    assert default.load_test.workloads.stress.enabled
    assert not default.load_test.workloads.interference.enabled
    disabled = load_saved({**values, 'checks': ['serving']})
    assert disabled.checks == ['serving'] and disabled._migration_notes
    advanced = load_saved({**values, 'concurrent_workloads': {'input_token_tolerance': .2, 'calibration_attempts': 4}})
    assert advanced.load_test.input_token_tolerance == .1 and advanced.load_test.calibration_attempts == 3
    assert any('input_token_tolerance' in note and 'calibration_attempts' in note for note in advanced._migration_notes)


@pytest.mark.parametrize('changes,checks,note', [
    ({'max_duration_seconds': 1}, [], "Load test won't run: the imported Maximum duration (1 s) can't fit its sending windows. Raise Maximum duration, then select Load test again."),
    ({'max_output_tokens': 8}, ['concurrency'], 'output_tokens.long=96 copied to profile.long.output_tokens=8; clipped to max_output_tokens=8.'),
    ({'concurrency': 1}, ['concurrency'], 'levels=[1, 2] moved to schedule.levels=[1] (bounded by Simultaneous limit).'),
    ({'targets': [{**TARGET, 'deployment': {'serving': {'context_limit': 100}}}]}, [], "Some Load test combinations won't run: they exceed the server's 100-token context window."),
    ({'targets': [TARGET, {**TARGET, 'name': 'other'}], 'overlap_models': True, 'concurrency': 1}, [], "Load test won't run: 2 endpoints run at the same time and need at least 2 simultaneous requests, but the imported Simultaneous limit is 1. Raise Simultaneous limit, then select Load test again."),
])
def test_migration_bounds_settings_or_turns_off_unrunnable_load(changes, checks, note):
    c = load_saved({'targets': [TARGET], 'checks': ['buckets', 'concurrency'], 'concurrent_workloads': {}, **changes})
    assert c.checks == ['load' if check == 'concurrency' else check for check in checks] and note in [*c._migration_notes, *(w['msg'] for w in c._migration_warnings)]
    if not checks:
        assert any(w['removed_check'] == 'load' for w in c._migration_warnings)
    else:
        assert c.load_test.patterns.concurrent.levels == ([1] if changes.get('concurrency') == 1 else [1, 2])
        assert c.load_test.profile.long.output_tokens == (8 if changes.get('max_output_tokens') == 8 else 96)


def test_legacy_fixture_and_validation_configs_load_with_expected_notes():
    paths = [Path('tests/fixtures/legacy-concurrent-load.yaml'), *Path('docs/validation').glob('*.json')]
    configs = []
    for path in paths:
        values = yaml.safe_load(path.read_text())
        if 'targets' in values:
            c = load_saved(values)
            configs.append(path)
            assert any('Mixed load is new' in n for n in c._migration_notes), path
            assert any('input_chars=' in n for n in c._migration_notes), path
        elif path.name.endswith('-deployment.json'):
            Deployment.model_validate(values)
    assert len(configs) == 3


@pytest.mark.parametrize('command', ['run', 'validate', 'preview'])
def test_cli_prints_notes_on_stderr(command, capsys, monkeypatch, tmp_path):
    if command == 'run':
        from giraffe import cli
        from tests.test_web import report
        async def run(config, **kwargs):
            saved = report(config)
            saved.manifest['config_migration_notes'] = config._migration_notes
            return saved
        monkeypatch.setattr(cli, '_run', run)
    args = [command, '--config', 'tests/fixtures/legacy-concurrent-load.yaml']
    if command == 'run':
        args += ['--output', str(tmp_path), '--skip-preflight']
    assert main(args) in (0, 2)
    assert 'Migration: Mixed load is new' in capsys.readouterr().err


async def test_run_start_accepts_notes_as_metadata_and_manifest_stores_them(tmp_path, monkeypatch):
    import httpx
    from giraffe.web import create_app
    from tests.test_web import report, finished
    received = []
    async def run(config, **kwargs):
        received.extend(config._migration_notes)
        saved = report(config)
        saved.manifest['config_migration_notes'] = config._migration_notes
        return saved
    monkeypatch.setattr('giraffe.web.run_suite', run)
    app = create_app(tmp_path)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8787') as client:
        response = await client.post('/api/runs', json={'config': configuration().model_dump(), 'migration_notes': ['Original limits dropped']})
        assert response.status_code == 202
        detail = await finished(client, response.json()["id"])
    assert received == ['Original limits dropped']
    assert detail['report']['manifest']['config_migration_notes'] == received


def test_manifest_rerun_recomputes_legacy_notes(tmp_path, monkeypatch, capsys):
    from tests.test_web import report
    from giraffe import cli
    old = report(configuration())
    old.manifest['config'] = yaml.safe_load(Path('tests/fixtures/legacy-concurrent-load.yaml').read_text())
    source = tmp_path / 'old.json'
    source.write_text(old.model_dump_json())
    async def run(config, **kwargs):
        saved = report(config)
        saved.manifest['config_migration_notes'] = config._migration_notes
        return saved
    monkeypatch.setattr(cli, '_run', run)
    output = tmp_path / 'rerun'
    assert main(['run', '--manifest', str(source), '--skip-preflight', '--output', str(output)]) == 0
    assert 'Migration: Mixed load is new' in capsys.readouterr().err
    assert json.loads((output / 'report.json').read_text())['manifest']['config_migration_notes']


@pytest.mark.parametrize('arrivals', [{}, {'requests': 2}])
def test_unrunnable_legacy_default_keeps_automatic_check_selection(arrivals):
    values = {'targets': [TARGET], 'concurrent_workloads': {'scenarios': ['streaming_fairness']},
        'structured_json': True, 'arrivals': arrivals}
    config = load_saved(values)
    # The unrunnable old stream comparison cannot hide independent arrivals.
    assert config.checks == ['access', 'serving', 'first_output', 'generation', 'context',
                             'correctness', 'cancellation', 'recovery', 'json', 'load']
    assert config.load_test.workloads.combinations.selected == ['short_short']
    assert config.load_test.patterns.constant.enabled
    assert any('streaming_fairness' in w['msg'] for w in config._migration_warnings)
    assert all(w['removed_check'] is None for w in config._migration_warnings)


@pytest.mark.parametrize('models', [False, True])
def test_migration_accepts_nullable_sustained_duration_and_workload_models(models):
    from giraffe.models import Arrival, TrafficConfig
    arrivals, traffic = {'requests': 2}, {'rates': [1], 'duration_seconds': 1}
    if models:
        arrivals, traffic = Arrival(**arrivals), TrafficConfig(**traffic)
    config = load_saved(dict(targets=[TARGET], checks=['capacity', 'recovery', 'arrivals', 'concurrency'],
        sustained_seconds=None, arrivals=arrivals, traffic=traffic, concurrent_workloads={}))
    assert config.sustained_seconds == 5
    assert config.checks == ['load', 'recovery']
    assert config.load_test.patterns.rates == [1]
    assert config.load_test.schedule.duration_seconds == 1
    assert [test.source for test in config.load_test.migrated_tests] == ['arrivals', 'concurrency']
    arrivals_test = config.load_test.migrated_tests[0].settings
    assert arrivals_test.workloads.combinations.selected == ['short_short']
    assert arrivals_test.patterns.constant.enabled
    assert arrivals_test.schedule.duration_seconds == 2


@pytest.mark.parametrize('sustained,status', [(None, 200), ('not-a-number', 422)])
async def test_web_legacy_nullable_or_invalid_sustained_duration_never_crashes(sustained, status, tmp_path):
    import httpx
    from giraffe.web import create_app
    app = create_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8787') as client:
        response = await client.post('/api/config/validate', json={'targets': [TARGET], 'concurrent_workloads': {}, 'sustained_seconds': sustained})
    assert response.status_code == status, response.text
    if status == 200:
        assert response.json()['config']['sustained_seconds'] == 5
    else:
        assert response.json()['detail'][0]['field'] == 'sustained_seconds'


@pytest.mark.parametrize('field', ['buckets', 'concurrent_workloads'])
@pytest.mark.parametrize('settings', [
    {'levels': []}, {'levels': [0]}, {'levels': [MAX_SIMULTANEOUS_REQUESTS + 1]}, {'levels': [1, 1]}, {'levels': 'wrong'},
    {'levels': ['wrong']}, {'levels': [1.5]},
    {'output_tokens': {'short': 16, 'medium': 48, 'long': 0}},
    {'output_tokens': {'short': 16, 'medium': 48, 'long': -1}},
    {'output_tokens': {'short': 16, 'medium': 48, 'long': 65537}},
    {'output_tokens': {'short': 16, 'medium': 48, 'long': 'wrong'}},
    {'input_chars': {'short': 0, 'medium': 1024, 'long': 12288}},
    {'input_chars': {'short': 64, 'medium': 1024}},
    {'samples_per_bucket': 1}, {'samples_per_bucket': 'wrong'},
    {'mixed_pairs': 0}, {'mixed_pairs': 101},
    {'heavy_weights': {'long_input': 0, 'long_output': 1}},
    {'heavy_weights': {'long_input': 1}},
    {'window_seconds': 0}, {'window_seconds': 'wrong'}, {'unknown_setting': 1},
])
def test_invalid_legacy_values_are_rejected_before_migration(field, settings):
    from pydantic import ValidationError
    source = {'targets': [TARGET], 'checks': ['concurrency'], field: settings}
    before = json.dumps(source)
    with pytest.raises(ValidationError) as error:
        load_saved(source)
    assert json.dumps(source) == before
    assert error.value.errors()[0]['loc'][0] == field


@pytest.mark.parametrize('settings', [
    {'scenarios': []}, {'scenarios': ['unknown']}, {'scenarios': ['size_matrix', 'size_matrix']},
    {'calibration_attempts': 0}, {'calibration_attempts': 6}, {'calibration_attempts': 'wrong'},
    {'input_token_tolerance': -.1}, {'input_token_tolerance': .5},
    {'stress_cases': []}, {'stress_cases': [{'name': 'unknown', 'output_tokens': 128}]},
    {'stress_cases': [{'name': 'long_input_summary', 'input_tokens': 1, 'output_tokens': 128}]},
    {'stress_cases': [{'name': 'long_input_summary', 'output_tokens': 128}]},
    {'stress_cases': [{'name': 'short_input_long_output', 'input_tokens': 128, 'output_tokens': 128}]},
    {'stress_cases': [{'name': 'short_input_long_output', 'output_tokens': 128, 'min_output_tokens': 129}]},
])
def test_invalid_dropped_concurrent_fields_are_still_validated(settings):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        load_saved(dict(targets=[TARGET], concurrent_workloads=settings))


@pytest.mark.parametrize('field', ['buckets', 'concurrent_workloads'])
def test_valid_legacy_values_above_global_limits_preserve_old_bounding(field):
    values = dict(targets=[TARGET], checks=['concurrency'], concurrency=2, max_output_tokens=128,
        **{field: {'levels': [3], 'output_tokens': {'short': 16, 'medium': 48, 'long': 256}}})
    migrated, _ = migrate_run_limits(values)
    migrated, notes, warnings = migrate_concurrent_load(migrated)
    assert migrated['concurrent_load']['schedule']['levels'] == [2]
    assert migrated['concurrent_load']['profile']['long']['output_tokens'] == 128
    assert 'levels=[3] moved to schedule.levels=[2] (bounded by Simultaneous limit).' in notes
    assert 'output_tokens.long=256 copied to profile.long.output_tokens=128; clipped to max_output_tokens=128.' in notes
    config = RunConfig.model_validate(migrated)
    assert config.load_test.patterns.concurrent.levels == [2]
    assert config.load_test.profile.long.output_tokens == 128


@pytest.mark.parametrize('field,settings', [('buckets', {'output_tokens': {'short': 16, 'medium': 48, 'long': 'wrong'}}),
    ('concurrent_workloads', {'samples_per_bucket': 1}), ('concurrent_workloads', {'levels': [MAX_SIMULTANEOUS_REQUESTS + 1]})])
async def test_invalid_legacy_requests_return_422_without_starting_traffic(field, settings, tmp_path, monkeypatch):
    import httpx
    from giraffe.web import create_app
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid legacy input started endpoint traffic')
    monkeypatch.setattr('giraffe.web.run_suite', forbidden)
    monkeypatch.setattr('giraffe.client.LLMClient.execute', forbidden)
    source = {'targets': [TARGET], 'checks': ['concurrency'], field: settings}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)), base_url='http://127.0.0.1:8787') as client:
        for path in ['/api/config/draft', '/api/config/validate', '/api/config/preview', '/api/config/plan', '/api/runs']:
            response = await client.post(path, json={'config': source} if path == '/api/runs' else source)
            assert response.status_code == 422, (path, response.text)
            assert response.json()['detail'][0]['field'].startswith(field)


@pytest.mark.parametrize('check', ['concurrency', 'buckets'])
@pytest.mark.parametrize('options', [{'samples': 0}, {'samples': 'wrong'}, {'concurrency': 1025},
    {'limits': {'min_samples': 1}}, {'unknown_option': 1}])
def test_invalid_legacy_overrides_are_not_silently_dropped(check, options):
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as error:
        load_saved(dict(targets=[TARGET], checks=['buckets'], test_options={check: options}))
    assert error.value.errors()[0]['loc'][0] == 'test_options'


def test_migration_validation_does_not_restore_legacy_public_schema():
    schema = RunConfig.model_json_schema()
    assert not any('Legacy' in name or name in {'Buckets', 'ConcurrentWorkloads', 'TokenStressCase'} for name in schema['$defs'])
    assert not {'buckets', 'concurrent_workloads'} & schema['properties'].keys()


@pytest.mark.parametrize('checks', [['access'], ['access', 'serving'], ['access', 'serving', 'correctness']])
def test_removed_check_warning_names_all_remaining_checks(checks):
    from giraffe.models import CHECK_NAMES
    config = load_saved({'targets': [TARGET], 'checks': [*checks, 'buckets'],
                         'concurrent_workloads': {}, 'max_duration_seconds': .5})
    names = [CHECK_NAMES[c] for c in checks]
    remaining = names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1]
    assert config.checks == checks
    assert config._migration_warnings == [{'msg': "Load test won't run: the imported Maximum duration (0.5 s) can't fit its sending windows. Raise Maximum duration, then select Load test again. Only " + remaining + ' will run.', 'removed_check': 'load'}]


def test_context_window_warning_copy_and_partial_workload_metadata():
    target = {**TARGET, 'deployment': {'serving': {'context_limit': 1000}}}
    config = load_saved({'targets': [target], 'checks': ['access', 'concurrency'],
        'concurrent_workloads': {'levels': [2]}})
    messages = [w['msg'] for w in config._migration_warnings]
    assert "Some Load test combinations won't run: they exceed the server's 1,000-token context window." in messages
    assert "Load test's long-request comparison won't run: its request sizes exceed the server's 1,000-token context window." in messages
    assert "Load test large_input stress won't run: its request sizes exceed the server's 1,000-token context window." in messages
    assert config.checks == ['access', 'load']
    assert all(w['removed_check'] is None for w in config._migration_warnings)
    assert not any('turned off' in note for note in config._migration_notes)


def test_disabled_single_shape_mixed_only_load_is_a_check_removal_warning():
    from giraffe.models import ConcurrentLoad
    load = ConcurrentLoad().model_dump()
    for name in ('combinations', 'interference', 'stress'):
        load[name]['enabled'] = False
    load['mixed'].update(enabled=True, shares={'short_short': 100})
    config = load_saved({'targets': [TARGET], 'checks': ['access', 'concurrency'], 'concurrent_load': load})
    assert config.checks == ['access']
    assert config._migration_warnings == [{'msg': 'Legacy one-shape mixed workload disabled; mixed traffic requires at least two request shapes. Only Endpoint response will run.', 'removed_check': 'load'}]


def test_selected_legacy_check_with_all_workloads_disabled_warns_on_removal():
    from giraffe.models import ConcurrentLoad
    load = ConcurrentLoad().model_dump()
    for name in ('combinations', 'mixed', 'interference', 'stress'):
        load[name]['enabled'] = False
    config = load_saved({'targets': [TARGET], 'checks': ['access', 'concurrency'], 'concurrent_load': load})
    assert config.checks == ['access']
    assert config._migration_warnings == [{'msg': "Load test won't run: no imported load workload is enabled. Only Endpoint response will run.", 'removed_check': 'load'}]


def test_python_implicit_arrivals_keeps_historical_default_selection():
    config = RunConfig(targets=[TARGET], arrivals={'requests': 2})
    assert config.checks == ['access', 'serving', 'first_output', 'generation', 'load',
                             'context', 'correctness', 'cancellation', 'recovery']
    assert config._migration_warnings == []
