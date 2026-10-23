"""Request settings frozen from the reviewer's archived old main, not Phase 2."""

import hashlib
import json
from pathlib import Path

import pytest

from giraffe.cli import migrated_configuration
from giraffe.fixtures import builtin_fixtures
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
def test_legacy_explicit_timeout_request_settings_match_old_main(name):
    case = CASES[name]
    # Freeze the archived 30 s default explicitly; new omitted timeouts are 300 s.
    config = migrated_configuration({'request_timeout_seconds': 30, **case['source']})
    expected = case['expected']
    for check, settings in expected['settings'].items():
        if check in {'capacity', 'arrivals', 'concurrency'}:
            continue  # Their traffic is intentionally replaced by issue #45.
        # Sections 1.3 and 2.1 resolve read-only pack parallelism per endpoint.
        if name in PACK_OVERLAP_CASES and check == 'correctness':
            assert settings['concurrency'] == 4
            settings = settings | {'concurrency': 2}
        effective = config.effective_check(check)
        assert {field: effective[field] for field in settings} == settings, check
    for check, groups in expected['fixtures'].items():
        if check in {'capacity', 'arrivals', 'concurrency'}:
            continue
        effective = _check_config(config, check)
        actual = builtin_fixtures(effective, check_id=check)
        assert {group: request_values(actual[group]) for group in groups} == groups, check
    if 'pack' in expected:
        assert request_values(pack_specs(config)) == expected['pack']
    old_checks = set(case['source'].get('checks', []))
    replaced = old_checks & {'concurrency', 'capacity', 'arrivals', 'buckets', 'fairness', 'mixed', 'token_stress'}
    if replaced or case['source'].get('arrivals') is not None:
        assert 'load' in config.checks
        assert not set(config.checks) & {'concurrency', 'capacity', 'arrivals'}
        sources = [config.load_test.legacy_source, *[test.source for test in config.load_test.migrated_tests]]
        assert len(sources) == len(set(sources))
        assert all(source in {'concurrency', 'capacity', 'arrivals'} for source in sources)
        assert any('migrated' in note.lower() for note in config._migration_notes)
        # A saved normalized migration must remain runnable, including legacy
        # token ceilings that made profile sizes equal.
        repeated = type(config).model_validate(config.model_dump())
        assert repeated.load_test == config.load_test
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
