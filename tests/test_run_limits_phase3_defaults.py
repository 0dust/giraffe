"""Old-main request settings, exercised through each check's runtime fixture path."""

import hashlib
import json
from pathlib import Path

import pytest

from giraffe.cli import migrated_configuration
from giraffe.fixtures import builtin_fixtures, traffic_fixtures
from giraffe.runner import _check_config

SNAPSHOTS = json.loads(Path('tests/fixtures/run-limits-phase3-defaults.json').read_text())['cases']


@pytest.mark.parametrize('name', SNAPSHOTS)
def test_historical_requests_with_explicit_timeout_match_each_check_path(name):
    expected = SNAPSHOTS[name]
    # Current examples intentionally use the current schema and shared defaults.
    # Historical request compatibility is pinned to the immutable source snapshot.
    source = dict(expected['source'])
    if 'checks' not in source:
        source['checks'] = ['access', 'serving', 'first_output', 'generation', 'load',
                            'context', 'correctness', 'cancellation', 'recovery']
        if source.get('structured_json'):
            source['checks'].append('json')
    # Pin the historical timeout: this snapshot verifies payload compatibility,
    # while dedicated default tests cover the new 300-second default.
    source = {"request_timeout_seconds": 30, **source}
    config = migrated_configuration(source)
    actual = {}
    for check, groups in expected['requests'].items():
        if check.startswith('_suite_'):
            built = builtin_fixtures(config) if check == '_suite_builtin' else traffic_fixtures(config)
            traffic = {}
        else:
            effective = _check_config(config, check)
            built = builtin_fixtures(effective, check_id=check)
            traffic = traffic_fixtures(effective, check_id=check) if check in {'capacity', 'recovery'} else {}
        actual[check] = {}
        for group in groups:
            actual[check][group] = []
            for spec in (traffic if group in traffic else built)[group]:
                data = spec.model_dump(mode='json')
                data['request_timeout_seconds'] = float(spec.request_timeout_seconds)
                digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                actual[check][group].append({'fixture_id': spec.fixture_id, 'max_tokens': spec.max_tokens,
                    'request_timeout_seconds': spec.request_timeout_seconds, 'payload_sha256': digest})
    assert actual == expected['requests']
