import pytest
from pydantic import ValidationError

from giraffe.models import RunConfig, migrate_run_limits, required_concurrency

TARGET = dict(name='one', url='http://local', model='m')


def cfg(**values):
    return RunConfig(targets=[TARGET], checks=['correctness'], **values)


def test_nullable_defaults_and_v2_python_configs():
    config = cfg()
    assert config.config_version == 2
    assert (config.concurrency, config.max_requests, config.max_duration_seconds) == (None, None, None)
    assert config.effective_check('correctness')['concurrency'] == 4
    assert config._migration_notes == []


def test_64_vs_40_names_both_fields_and_values():
    with pytest.raises(ValidationError) as error:
        cfg(concurrency=40, test_options={'correctness': {'concurrency': 64}})
    context = error.value.errors()[0]['ctx']
    assert context['fields'] == ['test_options.correctness.concurrency', 'concurrency']
    assert context['values'] == {'test_options.correctness.concurrency': 64, 'concurrency': 40}
    assert '64' in str(error.value) and '40' in str(error.value)


def test_inherited_parallelism_notices_and_explicit_overlap_warning():
    config = cfg(concurrency=2)
    assert config.effective_check('correctness')['concurrency'] == 2
    assert config.concurrency_notices()[0]['fields'] == ['default_concurrency', 'concurrency']
    config = RunConfig(targets=[TARGET, {**TARGET, 'name': 'two'}], checks=['correctness'],
                       overlap_models=True, concurrency=4,
                       test_options={'correctness': {'concurrency': 4}})
    assert config.effective_check('correctness')['concurrency'] == 4
    assert '8' in config.concurrency_notices()[0]['msg']
    assert required_concurrency(config) == {'per_target_peak': 4, 'peak': 8, 'by_check': {'correctness': 4}}


def test_legacy_omissions_and_explicit_concurrency_preserve_parallelism():
    migrated, notes = migrate_run_limits({'targets': [TARGET]})
    assert len(notes) == 3
    assert all(migrated[field] is None for field in ('concurrency', 'max_requests', 'max_duration_seconds'))
    migrated, notes = migrate_run_limits({'targets': [TARGET], 'checks': ['correctness'], 'concurrency': 40,
                                        'max_requests': None, 'max_duration_seconds': 17})
    assert migrated['default_concurrency'] == 40 and len(notes) == 1
    assert migrated['max_duration_seconds'] == 17
    assert RunConfig(**migrated).effective_check('correctness')['concurrency'] == 40


def test_arrivals_capacity_and_own_load_profile_defaults():
    config = RunConfig(targets=[TARGET], checks=['load'], default_concurrency=8, max_output_tokens=1)
    assert config.load_test.patterns.concurrent.levels == [1, 4]
    assert config.load_test.profile.long.output_tokens == 512
    # The longer default per-request timeout raises the conservative arrival
    # admission requirement to the existing 512-request safety ceiling.
    assert config.load_test.request_timeout_seconds == 300
    assert required_concurrency(config)['peak'] == 512
    assert config.load_test.patterns.constant.enabled


@pytest.mark.parametrize('check,setting,field', [
    ('correctness', {'test_options': {'correctness': {'concurrency': 64}}}, 'test_options.correctness.concurrency'),
    ('json', {'test_options': {'json': {'concurrency': 64}}}, 'test_options.json.concurrency'),
    ('recovery', {'test_options': {'recovery': {'concurrency': 64}}}, 'test_options.recovery.concurrency'),
    ('load', {'load_test': {'patterns': {'concurrent': {'levels': [64]}}}}, 'load_test.patterns.concurrent.levels'),
    ('load', {'load_test': {'patterns': {'bursts': {'enabled': True, 'size': 64, 'first_seconds': 0}}}}, 'load_test.patterns.bursts.size'),
    ('sessions', {'sessions': {'concurrency': 8}}, 'sessions.concurrency'),
    ('consistency', {'consistency': {'levels': [1, 64]}}, 'consistency.levels'),
    ('load', {'load_test': {'patterns': {'concurrent': {'levels': [64]}}}}, 'load_test.patterns.concurrent.levels'),
])
def test_all_explicit_check_conflicts_name_the_setting(check, setting, field):
    ceiling = 4 if check == 'sessions' else 40
    with pytest.raises(ValidationError) as error:
        RunConfig(targets=[TARGET], checks=[check], concurrency=ceiling, **setting)
    context = error.value.errors()[0]['ctx']
    assert context['fields'] == [field, 'concurrency']
    assert context['values']['concurrency'] == ceiling
    assert context['values'][field] == ([1, 64] if check == 'consistency' else
                                       [64] if field.endswith('levels') else
                                       8 if check == 'sessions' else 64)


@pytest.mark.parametrize('check,setting', [
    ('capacity', {'traffic': {'max_in_flight': 4}}),
    ('arrivals', {'arrivals': {'max_in_flight': 4}}),
    ('sessions', {'sessions': {'concurrency': 4}}),
    ('consistency', {'consistency': {'levels': [1, 4]}}),
])
def test_explicit_overlap_work_is_preserved_with_a_warning(check, setting):
    config = RunConfig(targets=[TARGET, {**TARGET, 'name': 'two'}], checks=[check],
                       concurrency=4, overlap_models=True, **setting)
    if check in {'capacity', 'arrivals'}:
        assert config.checks == ['load']
        assert required_concurrency(config)['per_target_peak'] == 4
        assert config.concurrency == 4
        assert any('max_in_flight dropped' in note for note in config._migration_notes)
        return
    assert required_concurrency(config)['per_target_peak'] == 4
    assert len(config.concurrency_notices()) == 1
    message = config.concurrency_notices()[0]['msg']
    assert config.concurrency_notices()[0]['check'] == check
    assert 'up to 8 requests' in message
    assert ('may be dropped locally' if check in {'capacity', 'arrivals'} else 'may wait') in message


def test_concurrent_load_overlap_blocks_even_when_minimum_inherited_level_cannot_fit():
    with pytest.raises(ValidationError) as error:
        RunConfig(targets=[TARGET, {**TARGET, 'name': 'two'}], checks=['load'],
                  concurrency=1, overlap_models=True)
    assert error.value.errors()[0]['ctx']['fields'] == ['load_test.patterns.concurrent.levels', 'concurrency']


@pytest.mark.parametrize('check', ['sessions', 'consistency', 'load'])
def test_inherited_settings_round_trip_and_restore_after_raising_limit(check):
    config = RunConfig(targets=[TARGET], checks=[check], concurrency=1,
        **({'load_test': {'patterns': {'constant': {'enabled': False}}}} if check == 'load' else {}))
    restored = RunConfig.model_validate(config.model_dump())
    assert restored == config
    assert restored.concurrency_notices() == config.concurrency_notices()
    values = restored.model_dump()
    values['concurrency'] = 16
    raised = RunConfig.model_validate(values)
    assert required_concurrency(raised)['peak'] == (4 if check == 'load' else 2)
    assert raised.concurrency_notices() == []


def test_sessions_and_consistency_keep_their_own_builtin_defaults():
    config = RunConfig(targets=[TARGET], checks=['sessions', 'consistency'], default_concurrency=8)
    assert config.sessions.concurrency == 2
    assert config.consistency.levels == [1, 2]
    assert required_concurrency(config)['peak'] == 2


def test_required_peak_covers_serial_and_bounded_session_checks():
    config = RunConfig(targets=[TARGET], checks=['access', 'serving', 'generation', 'context',
                       'cancellation', 'prefix', 'tools', 'sessions'], sessions={'sessions': 2, 'concurrency': 8})
    peaks = required_concurrency(config)
    assert peaks['by_check'] == {check: 2 if check == 'sessions' else 1 for check in config.checks}
    assert peaks['per_target_peak'] == peaks['peak'] == 2


def test_v2_limit_ranges_and_unset_serialization_stay_finite():
    import json
    config = cfg(concurrency=512, default_concurrency=512)
    assert config.effective_check('correctness')['concurrency'] == 512
    assert json.loads(config.model_dump_json())['max_requests'] is None
    for field in ('concurrency', 'default_concurrency'):
        with pytest.raises(ValidationError):
            cfg(**{field: 513})
    from giraffe.models import Arrival
    with pytest.raises(ValidationError):
        Arrival(max_pending=257)
    assert Arrival(burst_size=1024).burst_size == 1024


def test_legacy_overlap_fixture_preserves_per_endpoint_work():
    from pathlib import Path
    import yaml
    values = yaml.safe_load(Path('tests/fixtures/legacy-run-limits-overlap.yaml').read_text())
    migrated, notes = migrate_run_limits(values)
    config = RunConfig.model_validate(migrated)
    assert len(notes) == 3
    assert config.concurrency == config.default_concurrency == 40
    assert all(config.effective_check(check)['concurrency'] == 40
               for check in ('correctness', 'json', 'recovery'))
    assert config.checks.count('load') == 1
    assert len(config.load_test.migrated_tests) == 1
    assert len(config.concurrency_notices()) == 3
    reloaded = RunConfig.model_validate(config.model_dump())
    assert required_concurrency(reloaded)['peak'] == 80


def test_legacy_request_pack_overlap_keeps_limits_and_reports_inherited_parallelism_exception():
    values = {'targets': [TARGET, {**TARGET, 'name': 'two'}], 'checks': ['correctness'],
              'concurrency': 40, 'overlap_models': True, 'request_source': 'request_pack',
              'request_pack': {'version': 1, 'cases': [{'fixture_id': 'one',
                  'messages': [{'role': 'user', 'content': 'hi'}], 'scorer': 'exact', 'expected': 'hi'}]}}
    migrated, notes = migrate_run_limits(values)
    config = RunConfig.model_validate(migrated)
    assert config.concurrency == config.default_concurrency == 40
    assert 'correctness' not in config.test_options
    assert config.effective_check('correctness')['concurrency'] == 20
    assert any('no per-check concurrency override' in note and '20 per endpoint' in note for note in notes)
    assert config.concurrency_notices()[0]['effective_concurrency'] == 20
