"""Historical inputs retain their payloads apart from corrected output allowances."""

import hashlib
import json
from pathlib import Path

import pytest

from giraffe.cli import migrated_configuration
from giraffe.fixtures import builtin_fixtures, traffic_fixtures
from giraffe.runner import _check_config

SNAPSHOTS = json.loads(Path('tests/fixtures/run-limits-phase3-defaults.json').read_text())['cases']


@pytest.mark.parametrize('name', SNAPSHOTS)
def test_historical_inputs_use_resolved_budgets_and_preserve_other_request_fields(name):
    expected = SNAPSHOTS[name]
    # Current examples intentionally use the current schema and shared defaults.
    # The source and historical payload hashes remain immutable. Output allowance
    # and the context prompt space it reserves intentionally changed in issue 78.
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
    for check, groups in expected['requests'].items():
        effective = config if check.startswith('_suite_') else _check_config(config, check)

        def build(settings):
            if check == '_suite_traffic':
                return traffic_fixtures(settings)
            built = builtin_fixtures(settings)
            if check in {'capacity', 'recovery'}:
                built.update(traffic_fixtures(settings))
            return built

        current = build(effective)
        historical_budgets = {row['max_tokens'] for rows in groups.values() for row in rows}
        restored = {cap: build(effective.model_copy(update={'max_output_tokens': cap}))
                    for cap in historical_budgets}
        for group, historical in groups.items():
            assert bool(current[group]) == bool(historical)
            current_ids = {spec.fixture_id for spec in current[group]}
            historical_ids = {row['fixture_id'] for row in historical}
            assert current_ids <= historical_ids if group == 'context' else current_ids == historical_ids
            for spec in current[group]:
                expected_cap = (min(8, effective.max_output_tokens)
                                if spec.fixture_id.startswith('limits.output_cap') else effective.max_output_tokens)
                assert spec.max_tokens == expected_cap
                assert spec.request_timeout_seconds == effective.request_timeout_seconds

            # Restoring only each old allowance must reproduce its exact saved
            # payload, including the corresponding context prompt reservation.
            replayed = []
            for row in historical:
                spec = next(spec for spec in restored[row['max_tokens']][group]
                            if spec.fixture_id == row['fixture_id'])
                data = spec.model_dump(mode='json')
                data['request_timeout_seconds'] = float(spec.request_timeout_seconds)
                digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                replayed.append({'fixture_id': spec.fixture_id, 'max_tokens': spec.max_tokens,
                    'request_timeout_seconds': spec.request_timeout_seconds, 'payload_sha256': digest})
            assert replayed == historical
