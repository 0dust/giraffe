"""Old-main request settings, exercised through each check's runtime fixture path."""

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from giraffe.cli import migrated_configuration
from giraffe.fixtures import builtin_fixtures, traffic_fixtures
from giraffe.runner import _check_config

SNAPSHOTS = json.loads(Path('tests/fixtures/run-limits-phase3-defaults.json').read_text())['cases']


@pytest.mark.parametrize('name', SNAPSHOTS)
@pytest.mark.parametrize('legacy', [False, True], ids=['current-example', 'legacy-main'])
def test_unconfigured_requests_match_main_through_each_check_path(name, legacy):
    expected = SNAPSHOTS[name]
    source = (expected['source'] if legacy or name == 'default' else
              yaml.safe_load(Path(name).read_text()))
    config = migrated_configuration({**source, **({'config_version': 2} if name == 'default' and not legacy else {})})
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
