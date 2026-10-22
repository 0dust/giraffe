import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from giraffe.models import ConcurrentLoad, ConfigDraft, LoadTest, RunConfig, SHAPES
from giraffe.planning import request_plan
from giraffe.web import _configuration_errors, create_app
from tests.test_concurrent_load import TARGET


@pytest.mark.parametrize('concurrency', [1, 2, 3])
@pytest.mark.parametrize('cap', [1, 32, 64])
def test_inherited_levels_respect_ceiling_with_notice_and_profile_is_independent(concurrency, cap):
    config = RunConfig(targets=[TARGET], checks=['load'], concurrency=concurrency, max_output_tokens=cap)
    assert config.load_test.patterns.concurrent.levels == [1]
    assert [getattr(config.load_test.profile, size).output_tokens for size in ('short', 'medium', 'long')] == [64, 256, 512]
    notice = next(n for n in request_plan(config)['concurrency_warnings'] if n.get('check') == 'load')
    assert 'allows [1] per endpoint' in notice['msg']
    assert notice['fields'] == ['load_test.patterns.concurrent.levels', 'concurrency']
    assert not request_plan(config)['errors']
    from giraffe.load_test import build_spec, calibration_keys, point_shapes, test_points
    settings = config.load_test
    run = SimpleNamespace(config=config, pack_specs=[], load_custom_specs=[])
    calibration = {key: {'filler_chars': 128} for key in calibration_keys(settings)}
    specs = [build_spec(run, settings, point, shape, calibration)
        for point in test_points(settings) for shape in point_shapes(settings, point)]
    assert specs
    assert all(spec.max_tokens == getattr(settings.profile, spec.load_point['shape'].split('_')[1]).output_tokens
        for spec in specs)
    assert max(spec.max_tokens for spec in specs) == 512
    assert all(spec.check_ids == ['load'] for spec in specs)
    restored = RunConfig.model_validate(config.model_dump())
    assert restored.load_test == config.load_test
    assert restored.concurrency_notices() == config.concurrency_notices()


@pytest.mark.parametrize('cls', [RunConfig, ConfigDraft])
def test_partial_defaults_fit_overlap_allocation_and_preserve_explicit_fields(cls):
    config = cls(targets=[TARGET, {**TARGET, 'name': 'second'}], checks=['load'], concurrency=6,
        overlap_models=True, max_output_tokens=32,
        load_test={'profile': {'long': {'output_tokens': 1024}}, 'schedule': {'duration_seconds': 2}})
    assert config.load_test.patterns.concurrent.levels == [1]
    assert [getattr(config.load_test.profile, size).output_tokens for size in ('short', 'medium', 'long')] == [64, 256, 1024]
    assert config.load_test.schedule.duration_seconds == 2
    plan = request_plan(config)
    assert {point['level'] for point in plan['load_test']['points'] if point['pattern'] == 'concurrent'} == {1}
    assert not plan['errors']


@pytest.mark.parametrize('values,field', [
    ({'load_test': {'patterns': {'concurrent': {'levels': [4]}}}, 'concurrency': 2}, 'load_test.patterns.concurrent.levels'),
])
def test_explicit_over_limit_values_remain_structured_errors(values, field):
    values = {'targets': [TARGET], 'checks': ['load'], **values}
    with pytest.raises(ValidationError) as exc:
        RunConfig.model_validate(values)
    assert _configuration_errors(exc.value.errors(), values)[0]['field'] == field


def test_dormant_load_relationships_do_not_block_other_checks():
    # Saved legacy drafts remain available to unrelated checks, including their
    # invalid cross-field relationships; primitive schema errors still reject.
    config = RunConfig(targets=[TARGET], checks=['serving'], concurrency=1, max_output_tokens=1, stream=False,
        concurrent_load={'profile': {'short': {'output_tokens': 128}, 'long': {'output_tokens': 1}},
            'combinations': {'selected': []}, 'mixed': {'shares': {'short_short': 90}},
            'stress': {'enabled': True, 'large_input': False, 'large_output': False},
            'interference': {'enabled': True}, 'schedule': {'levels': [4]}})
    assert config.checks == ['serving'] and not request_plan(config)['errors']
    with pytest.raises(ValidationError):
        ConcurrentLoad.model_validate(config.concurrent_load.model_dump())
    with pytest.raises(ValidationError):
        RunConfig(targets=[TARGET], checks=['serving'], concurrent_load={'schedule': {'levels': [0]}})


@pytest.mark.parametrize('endpoint', ['draft', 'validate', 'preview', 'plan'])
async def test_web_returns_derived_defaults_without_validation_errors(endpoint, tmp_path):
    app = create_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8787') as client:
        response = await client.post('/api/config/' + endpoint, json={'config_version': 2, 'targets': [TARGET], 'checks': ['load'], 'concurrency': 2, 'max_output_tokens': 1})
    assert response.status_code == 200, response.text
    data = response.json()
    assert 'levels' not in data['config']['load_test']['patterns']['concurrent']
    assert {p['level'] for p in data['request_plan']['load_test']['points'] if p['pattern'] == 'concurrent'} == {1}
    assert data['config']['load_test']['profile']['long']['output_tokens'] == 512
    assert any(n.get('check') == 'load' for n in data['request_plan']['concurrency_warnings'])
    assert not data['request_plan']['errors']


@pytest.mark.parametrize('seed', [-1, 2**53])
def test_mixed_seed_rejects_values_outside_javascript_safe_range(seed):
    with pytest.raises(ValidationError):
        LoadTest(patterns={'poisson': {'seed': seed}})


@pytest.mark.parametrize('seed', [0, 2**53 - 1])
def test_mixed_seed_accepts_javascript_safe_boundaries(seed):
    assert LoadTest(patterns={'poisson': {'seed': seed}}).patterns.poisson.seed == seed


@pytest.mark.parametrize('shares,shape', [({'long_short': 90, 'short_long': 0}, 'long_short'), ({}, SHAPES[0])])
def test_mixed_total_error_focuses_a_real_share_input(shares, shape):
    values = {'targets': [TARGET], 'checks': ['load'], 'load_test': {'workloads': {'mixed': {
        'enabled': True, 'selected': [shape, 'long_long'], 'share_mode': 'custom', 'shares': shares}}}}
    with pytest.raises(ValidationError) as exc:
        RunConfig.model_validate(values)
    issue, = _configuration_errors(exc.value.errors(), values)
    assert issue['fields'] == ['load_test.workloads.mixed.shares', 'load_test.workloads.mixed.share_mode']
    assert all(action.get('field') in issue['fields'] or action.get('fields') for action in issue['actions'])


@pytest.mark.parametrize('invalid', [True, '32', 1.5])
def test_derived_defaults_do_not_replace_explicit_invalid_profile_values(invalid):
    with pytest.raises(ValidationError):
        RunConfig(targets=[TARGET], load_test={'profile': {'long': {'output_tokens': invalid}}})


def test_numeric_global_values_use_their_validated_integer_values_for_defaults():
    config = RunConfig(targets=[TARGET], concurrency='2', max_output_tokens='32')
    assert config.load_test.patterns.concurrent.levels == [1]
    assert config.load_test.profile.long.output_tokens == 512
    assert config.concurrency == 2 and config.max_output_tokens == 32


@pytest.mark.parametrize('command', ['run', 'validate', 'preview'])
def test_cli_accepts_derived_defaults(command, tmp_path, monkeypatch, capsys):
    from giraffe import cli
    from tests.test_web import report
    path = tmp_path / 'defaults.json'
    path.write_text(json.dumps({'targets': [TARGET], 'checks': ['concurrency'], 'concurrency': 3, 'max_output_tokens': 1}))
    if command == 'run':
        async def run(config, **kwargs):
            assert config.load_test.patterns.concurrent.levels == [1]
            assert config.default_concurrency == config.concurrency == 3
            assert config.load_test.profile.long.output_tokens == 1
            return report(config)
        monkeypatch.setattr(cli, '_run', run)
    args = [command, '--config', str(path)]
    if command == 'run':
        args += ['--output', str(tmp_path / 'report'), '--skip-preflight']
    assert cli.main(args) == 0
    assert 'exceeds' not in capsys.readouterr().err


def test_explicit_profile_cap_can_exceed_suite_default_without_adjustment():
    config = RunConfig(targets=[TARGET], max_output_tokens=32,
        load_test={'profile': {'long': {'output_tokens': 1024}}})
    assert config.load_test.profile.long.output_tokens == 1024
    assert config.max_output_tokens == 32


def test_partial_profile_defaults_do_not_adjust_to_make_output_sizes_monotonic():
    with pytest.raises(ValidationError) as exc:
        RunConfig(targets=[TARGET], load_test={'profile': {'long': {'output_tokens': 16}}})
    assert _configuration_errors(exc.value.errors(), {})[0]['fields'] == ['load_test.profile.long.output_tokens']
