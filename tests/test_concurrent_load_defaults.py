import json

import httpx
import pytest
from pydantic import ValidationError

from giraffe.models import ConcurrentLoad, ConfigDraft, RunConfig, SHAPES
from giraffe.planning import request_plan
from giraffe.web import _configuration_errors, create_app
from tests.test_concurrent_load import TARGET, Client, perform


@pytest.mark.parametrize('concurrency', [1, 2, 3])
@pytest.mark.parametrize('cap', [1, 32, 64])
async def test_defaults_fit_safety_limits_and_execute(concurrency, cap):
    config = RunConfig(targets=[TARGET], concurrency=concurrency, max_output_tokens=cap)
    assert config.concurrent_load.schedule.levels == [1]
    assert [getattr(config.concurrent_load.profile, size).output_tokens for size in ('short', 'medium', 'long')] == [min(n, cap) for n in (32, 64, 128)]
    assert not request_plan(config)['errors']
    run, check, client, _ = await perform(config, Client())
    assert client.started and all(spec.max_tokens <= cap for spec in client.specs)
    assert 'combinations' in check.metrics['load_types']


@pytest.mark.parametrize('cls', [RunConfig, ConfigDraft])
def test_partial_defaults_fit_overlap_allocation_and_preserve_explicit_fields(cls):
    config = cls(targets=[TARGET, {**TARGET, 'name': 'second'}], concurrency=6, overlap_models=True,
        max_output_tokens=32, concurrent_load={'profile': {'long': {'output_tokens': 16}}, 'schedule': {'duration_seconds': 2}})
    assert config.concurrent_load.schedule.levels == [1]
    assert [getattr(config.concurrent_load.profile, size).output_tokens for size in ('short', 'medium', 'long')] == [16, 16, 16]
    assert config.concurrent_load.schedule.duration_seconds == 2
    assert request_plan(config)['concurrent_load']['combinations']['levels'] == [1]


@pytest.mark.parametrize('values,field', [
    ({'concurrent_load': {'schedule': {'levels': [4]}}, 'concurrency': 2}, 'concurrent_load.schedule.levels'),
    ({'concurrent_load': {'profile': {'long': {'output_tokens': 64}}}, 'max_output_tokens': 32}, 'concurrent_load.profile.long.output_tokens'),
])
def test_explicit_over_limit_values_remain_structured_errors(values, field):
    values = {'targets': [TARGET], **values}
    with pytest.raises(ValidationError) as exc:
        RunConfig.model_validate(values)
    assert _configuration_errors(exc.value.errors(), values)[0]['field'] == field


def test_dormant_load_relationships_do_not_block_other_checks():
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
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/api/config/' + endpoint, json={'targets': [TARGET], 'concurrency': 2, 'max_output_tokens': 1})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['config']['concurrent_load']['schedule']['levels'] == [1]
    assert data['config']['concurrent_load']['profile']['long']['output_tokens'] == 1
    assert not data['request_plan']['errors']


@pytest.mark.parametrize('seed', [-1, 2**53])
def test_mixed_seed_rejects_values_outside_javascript_safe_range(seed):
    with pytest.raises(ValidationError):
        ConcurrentLoad(mixed={'seed': seed})


@pytest.mark.parametrize('seed', [0, 2**53 - 1])
def test_mixed_seed_accepts_javascript_safe_boundaries(seed):
    assert ConcurrentLoad(mixed={'seed': seed}).mixed.seed == seed


@pytest.mark.parametrize('shares,shape', [({'long_short': 90, 'short_long': 0}, 'long_short'), ({}, SHAPES[0])])
def test_mixed_total_error_focuses_a_real_share_input(shares, shape):
    values = {'targets': [TARGET], 'concurrent_load': {'mixed': {'shares': shares}}}
    with pytest.raises(ValidationError) as exc:
        RunConfig.model_validate(values)
    assert _configuration_errors(exc.value.errors(), values)[0]['fields'] == ['concurrent_load.mixed.shares.' + shape]


@pytest.mark.parametrize('invalid', [True, '32', 1.5])
def test_derived_defaults_do_not_replace_explicit_invalid_profile_values(invalid):
    with pytest.raises(ValidationError):
        RunConfig(targets=[TARGET], concurrent_load={'profile': {'long': {'output_tokens': invalid}}})


def test_numeric_global_values_use_their_validated_integer_values_for_defaults():
    config = RunConfig(targets=[TARGET], concurrency='2', max_output_tokens='32')
    assert config.concurrent_load.schedule.levels == [1]
    assert config.concurrent_load.profile.long.output_tokens == 32


@pytest.mark.parametrize('command', ['run', 'validate', 'preview'])
def test_cli_accepts_derived_defaults(command, tmp_path, monkeypatch, capsys):
    from giraffe import cli
    from tests.test_web import report
    path = tmp_path / 'defaults.json'
    path.write_text(json.dumps({'targets': [TARGET], 'concurrency': 3, 'max_output_tokens': 1}))
    if command == 'run':
        async def run(config, **kwargs):
            assert config.concurrent_load.schedule.levels == [1]
            assert config.concurrent_load.profile.long.output_tokens == 1
            return report(config)
        monkeypatch.setattr(cli, '_run', run)
    args = [command, '--config', str(path)]
    if command == 'run':
        args += ['--output', str(tmp_path / 'report'), '--skip-preflight']
    assert cli.main(args) == 0
    assert 'exceeds' not in capsys.readouterr().err
