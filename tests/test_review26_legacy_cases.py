"""Request settings frozen from the reviewer's archived old main, not Phase 2."""

import hashlib
import json
from pathlib import Path

import pytest

from giraffe.cli import migrated_configuration
from giraffe.fixtures import builtin_fixtures, traffic_fixtures
from giraffe.request_pack import pack_specs
from giraffe.runner import _check_config

CASES = json.loads((Path(__file__).parent / 'fixtures' / 'review26-legacy-effective.json').read_text())['cases']
PACK_OVERLAP_CASES = {'p01_pack_ov_c4.yaml', 'p03_pack_ov_cap_c4.yaml'}


def request_values(specs):
    return [{
        'fixture_id': spec.fixture_id,
        'max_tokens': spec.max_tokens,
        'request_timeout_seconds': spec.request_timeout_seconds,
        'input_chars': spec.input_chars,
        'messages_sha256': hashlib.sha256(json.dumps(
            spec.messages, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
    } for spec in specs]


@pytest.mark.parametrize('name', CASES)
def test_legacy_request_settings_match_old_main(name):
    case = CASES[name]
    config = migrated_configuration(case['source'])
    expected = case['expected']
    for check, settings in expected['settings'].items():
        # Sections 1.3 and 2.1 resolve read-only pack parallelism per endpoint.
        if name in PACK_OVERLAP_CASES and check == 'correctness':
            assert settings['concurrency'] == 4
            settings = settings | {'concurrency': 2}
        effective = config.effective_check(check)
        assert {field: effective[field] for field in settings} == settings, check
    for check, groups in expected['fixtures'].items():
        effective = _check_config(config, check)
        actual = builtin_fixtures(effective, check_id=check)
        assert {group: request_values(actual[group]) for group in groups} == groups, check
    if 'traffic' in expected:
        actual = traffic_fixtures(_check_config(config, 'capacity'), check_id='capacity')
        assert {group: request_values(specs) for group, specs in actual.items()} == expected['traffic']
    if 'pack' in expected:
        assert request_values(pack_specs(config)) == expected['pack']
    if 'concurrent_load' in expected:
        assert {
            'levels': config.concurrent_load.schedule.levels,
            'profile': config.concurrent_load.profile.model_dump(mode='json'),
        } == expected['concurrent_load']
    if 'arrivals_max_in_flight' in expected:
        assert config.effective_arrivals().max_in_flight == expected['arrivals_max_in_flight']
    if 'capacity_max_in_flight' in expected:
        assert config.effective_traffic().max_in_flight == expected['capacity_max_in_flight']
    if 'sessions_concurrency' in expected:
        assert config.sessions.concurrency == expected['sessions_concurrency']
    if 'consistency_levels' in expected:
        assert config.consistency.levels == expected['consistency_levels']


@pytest.mark.parametrize('name', sorted(PACK_OVERLAP_CASES))
def test_legacy_pack_overlap_documents_inherited_parallelism_change(name):
    config = migrated_configuration(CASES[name]['source'])
    assert CASES[name]['expected']['settings']['correctness']['concurrency'] == 4
    assert config.effective_check('correctness')['concurrency'] == 2
    assert [note for note in config._migration_notes if 'Request-pack correctness' in note] == [
        'Request-pack correctness has no per-check concurrency override; the preserved '
        '4-request shared limit resolves inherited parallelism to 2 per endpoint across '
        '2 overlapping endpoints.'
    ]
