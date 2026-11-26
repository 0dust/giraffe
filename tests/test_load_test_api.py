"""ASGI transport only: structured config errors and migration/planning I/O."""

import json
from copy import deepcopy
from datetime import datetime, timezone

import httpx
import pytest

from giraffe.client import completion_url
from giraffe.latency import input_bucket
from giraffe.models import RunConfig
from giraffe.web import create_app
from tests.test_load_test_phase_a import values


@pytest.mark.parametrize("endpoint", ["draft", "preview", "validate"])
async def test_load_config_api_preserves_dedicated_settings(tmp_path, endpoint):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(tmp_path)), base_url="http://127.0.0.1:8787"
    ) as client:
        payload = values()
        payload["load_test"]["patterns"].update(poisson={"enabled": True, "seed": 37}, rates=[10])
        response = await client.post("/api/config/" + endpoint, json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["config"]["checks"] == ["load"]
    assert body["config"]["load_test"]["patterns"]["poisson"]["seed"] == 37
    assert body["request_plan"]["load_test"]["approximate"]
    assert body["request_plan"]["required_concurrency"]["by_check"]["load"] == 512
    assert "load_combinations" in [r["id"] for r in body["request_plan"]["rows"]]


@pytest.mark.parametrize("endpoint", ["draft", "preview", "validate"])
async def test_load_burst_ceiling_error_names_both_fields_and_actions(tmp_path, endpoint):
    payload = values()
    payload["concurrency"] = 4
    payload["load_test"]["patterns"]["bursts"] = {
        "enabled": True,
        "size": 8,
        "first_seconds": 0,
        "gap_seconds": 1,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(tmp_path)), base_url="http://127.0.0.1:8787"
    ) as client:
        response = await client.post("/api/config/" + endpoint, json=payload)
    assert response.status_code == 422, response.text
    error = response.json()["detail"][0]
    assert error["fields"] == ["load_test.patterns.bursts.size", "concurrency"]
    assert {a["field"] for a in error["actions"]} == set(error["fields"])
    assert error["values"]["concurrency"] == 4


async def test_load_api_migration_notes_and_independent_schedules_roundtrip(tmp_path):
    payload = {
        "targets": values()["targets"],
        "config_version": 2,
        "checks": ["capacity", "arrivals"],
        "traffic": {"rates": [3], "duration_seconds": 5, "arrival": "poisson"},
        "arrivals": {"mode": "burst", "burst_size": 2, "requests": 8, "interval_seconds": 3},
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(tmp_path)), base_url="http://127.0.0.1:8787"
    ) as client:
        response = await client.post("/api/config/validate", json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["migration_notes"]
        assert "not blended" in " ".join(body["migration_notes"])
        assert body["config"]["checks"] == ["load"]
        assert body["config"]["load_test"]["schedule"]["duration_seconds"] == 5
        assert (
            body["config"]["load_test"]["migrated_tests"][0]["settings"]["schedule"][
                "duration_seconds"
            ]
            == 12
        )
        again = await client.post("/api/config/validate", json=body["config"])
    assert again.status_code == 200, again.text
    assert again.json()["config"]["load_test"] == body["config"]["load_test"]


async def test_budget_and_time_conflicts_warn_before_start_with_field_actions(tmp_path):
    payload = values()
    payload.update(max_requests=2, max_duration_seconds=0.5)
    payload["load_test"]["patterns"] = {
        "concurrent": {"enabled": False},
        "constant": {"enabled": True},
        "rates": [10],
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(tmp_path)), base_url="http://127.0.0.1:8787"
    ) as client:
        response = await client.post("/api/config/validate", json=payload)
    assert response.status_code == 200, response.text
    plan = response.json()["request_plan"]
    assert any("max_requests" in w["fields"] and w["actions"] for w in plan["load_warnings"])
    assert any(
        "max_duration_seconds" in w["fields"] and w["actions"] for w in plan["time_warnings"]
    )


@pytest.mark.parametrize(("duration", "latency_ms", "estimated_requests", "required"), [
    (20, 6400, 4, 26),
    (20, 5000, 4, 21),  # A request ready to start exactly at the cutoff is too late.
    (26, 6400, None, None),  # The fifth request can finish after sending stops.
    (20, None, None, None),
])
async def test_preview_warns_when_a_concurrent_condition_is_likely_to_miss_its_sample_minimum(
    tmp_path, duration, latency_ms, estimated_requests, required,
):
    payload = values()
    load = payload["load_test"]
    load["workloads"]["combinations"]["selected"] = ["short_short", "short_long"]
    load["patterns"]["concurrent"]["levels"] = [1, 2]
    load["schedule"]["duration_seconds"] = duration
    del load["pass_criteria"]["min_valid_responses"]
    config = RunConfig.model_validate(payload)
    profile = config.load_test.profile

    if latency_ms is not None:
        previous = tmp_path / "previous"
        previous.mkdir()
        (previous / ".web.json").write_text(json.dumps({"latency_summary": {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "targets": [{"url": "http://unused.test/v1/chat/completions", "model": "test",
                         "measurements": [{
                             "family": "load.combinations.concurrent",
                             "concurrency": 1,
                             "stream": config.stream,
                             "output_cap": profile.long.output_tokens,
                             "input_bucket": input_bucket(profile.short.input_tokens * 4),
                             "n": 4,
                             "p50_ms": latency_ms,
                             "p90_ms": latency_ms,
                         }]}],
        }}))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(tmp_path)), base_url="http://127.0.0.1:8787"
    ) as client:
        response = await client.post("/api/config/preview", json=payload)
    assert response.status_code == 200, response.text
    plan = response.json()["request_plan"]
    warnings = [row for row in plan["load_warnings"] if row.get("kind") == "load_sample_shortfall"]
    if estimated_requests is None:
        assert warnings == []
        return

    warning, = warnings
    assert warning["msg"] in plan["warnings"]
    assert warning["msg"] == (
        f"Short input → Long output, 1 at once: about {estimated_requests} of the 5 required valid "
        f"responses can start in {duration:g} s (recent responses take about {latency_ms / 1000:.1f} s). "
        f"Giraffe will extend sending to about {required} s; set “Send new requests for” to {required} s to plan for it."
    )
    load_times = [item for item in plan["time_timeline"] if item["id"] == "load_combinations"]
    assert load_times
    assert sorted(item["fixed_seconds"] for item in load_times) == sorted([duration] * 3 + [required])
    assert warning["values"]["estimated_requests"] == estimated_requests
    assert warning["values"]["min_valid_responses"] == 5
    assert warning["fields"] == ["load_test.schedule.duration_seconds"]
    assert warning["actions"] == [{"label": f"Set to {required} s",
                                   "field": "load_test.schedule.duration_seconds", "value": required}]


async def shortfall_preview(tmp_path, payload, samples, *, full_plan=False):
    config = RunConfig.model_validate(payload)
    measurements = []
    for shape, concurrency, p50, p90 in samples:
        input_size, output_size = shape.split('_')
        measurement = {
            'family': 'load.combinations.concurrent', 'concurrency': concurrency,
            'stream': config.stream, 'output_cap': getattr(config.load_test.profile, output_size).output_tokens,
            'input_bucket': input_bucket(getattr(config.load_test.profile, input_size).input_tokens * 4),
            'n': 4, 'p50_ms': p50,
        }
        if p90 is not None:
            measurement['p90_ms'] = p90
        measurements.append(measurement)
    previous = tmp_path / 'previous'
    previous.mkdir()
    (previous / '.web.json').write_text(json.dumps({'latency_summary': {
        'finished_at': datetime.now(timezone.utc).isoformat(),
        'targets': [{'url': completion_url(target.url), 'model': target.model, 'measurements': measurements}
                    for target in config.targets],
    }}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(tmp_path)), base_url='http://127.0.0.1:8787'
    ) as client:
        response = await client.post('/api/config/preview', json=payload)
    assert response.status_code == 200, response.text
    plan = response.json()['request_plan']
    warnings = [row for row in plan['load_warnings']
                if row.get('kind') in {'load_sample_shortfall', 'load_timeout_shortfall', 'load_timeout_risk'}]
    assert all(row['msg'] in plan['warnings'] for row in warnings)
    return plan if full_plan else warnings


@pytest.mark.parametrize('p50,p90', [(4000, 6400), (6400, None), (6400, 0), (6400, -1)])
async def test_shortfall_uses_p90_with_p50_fallback(tmp_path, p50, p90):
    payload = values()
    payload['load_test']['schedule']['duration_seconds'] = 20
    payload['load_test']['pass_criteria']['min_valid_responses'] = 5
    warning, = await shortfall_preview(tmp_path, payload, [('short_short', 1, p50, p90)])
    assert warning['kind'] == 'load_sample_shortfall'
    assert warning['values']['estimated_requests'] == 4
    assert warning['actions'] == [{'label': 'Set to 26 s', 'field': 'load_test.schedule.duration_seconds', 'value': 26}]
    assert warning['values']['conditions'] == [{
        'target': 'local', 'condition': 'Short input → Short output, 1 at once',
        'estimated_requests': 4, 'required_seconds': 26,
    }]


async def test_load_upper_allowance_adds_only_predicted_extension(tmp_path):
    payload = values()
    load = payload['load_test']
    load['schedule']['duration_seconds'] = 20
    load['request_timeout_seconds'] = 7
    load['pass_criteria']['min_valid_responses'] = 5
    plan = await shortfall_preview(tmp_path, payload, [('short_short', 1, 6400, 6400)], full_plan=True)
    assert plan['load_test']['time_with_allowances_seconds'] == 33
    assert plan['load_test']['sending_extension_seconds'] == 6
    assert plan['load_test']['upper_allowance_label'] == 'Upper allowance including request deadlines'
    assert plan['check_time_summaries'][0]['total_seconds'] == 33


@pytest.mark.parametrize('latencies_ms', [(6400, 8000), (8000, 6400)])
async def test_load_upper_allowance_uses_largest_extension_across_targets(tmp_path, latencies_ms):
    payload = values()
    payload['targets'].append({**payload['targets'][0], 'name': 'other', 'model': 'other'})
    load = payload['load_test']
    load['schedule']['duration_seconds'] = 20
    load['request_timeout_seconds'] = 10
    load['pass_criteria']['min_valid_responses'] = 5
    await shortfall_preview(tmp_path, payload, [('short_short', 1, 6400, 6400)])
    history_path = tmp_path / 'previous' / '.web.json'
    history = json.loads(history_path.read_text())
    for target, latency in zip(history['latency_summary']['targets'], latencies_ms):
        target['measurements'][0].update(p50_ms=latency, p90_ms=latency)
    history_path.write_text(json.dumps(history))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(tmp_path)), base_url='http://127.0.0.1:8787'
    ) as client:
        response = await client.post('/api/config/preview', json=payload)
    assert response.status_code == 200, response.text
    plan = response.json()['request_plan']
    warning, = [row for row in plan['load_warnings'] if row.get('kind') == 'load_sample_shortfall']
    assert sorted(item['required_seconds'] for item in warning['values']['conditions']) == [26, 33]
    assert plan['load_test']['sending_extension_seconds'] == 13
    assert plan['load_test']['time_with_allowances_seconds'] == 43


async def test_load_upper_allowance_labels_unpredicted_points_even_with_other_predictions(tmp_path):
    payload = values()
    load = payload['load_test']
    load['schedule']['duration_seconds'] = 20
    load['request_timeout_seconds'] = 7
    load['pass_criteria']['min_valid_responses'] = 5
    load['patterns']['concurrent']['levels'] = [1, 2]
    plan = await shortfall_preview(tmp_path, payload, [('short_short', 1, 6400, 6400)], full_plan=True)
    assert plan['load_test']['time_with_allowances_seconds'] == 60
    assert plan['load_test']['upper_allowance_label'] == 'Upper allowance before any window extension'


@pytest.mark.parametrize('timeout', [5, 6.4])
@pytest.mark.parametrize('p50', [4000, 6400])
async def test_timeout_warning_distinguishes_typical_latency_from_tail(tmp_path, timeout, p50):
    payload = values()
    payload['load_test']['schedule']['duration_seconds'] = 30
    payload['load_test']['pass_criteria']['min_valid_responses'] = 5
    payload['load_test']['request_timeout_seconds'] = timeout
    warning, = await shortfall_preview(tmp_path, payload, [('short_short', 1, p50, 6400)])
    assert warning['fields'] == ['load_test.request_timeout_seconds']
    if p50 == 4000:
        assert warning['kind'] == 'load_timeout_risk'
        assert warning['msg'] == (
            'Short input → Short output, 1 at once: at least 1 in 10 recent responses took longer than the '
            f'{timeout:g} s request timeout (90th percentile about 6.4 s), so some may time out and the test '
            'may miss its 5 required valid responses. Raise the request timeout to at least 8 s.'
        )
    else:
        assert warning['kind'] == 'load_timeout_shortfall'
        assert warning['msg'] == (
            'Short input → Short output, 1 at once: recent responses typically take about 6.4 s, longer than the '
            f'{timeout:g} s request timeout, so most will time out. Raise the request timeout to at least 8 s.'
        )
    assert 'none will count' not in warning['msg']
    assert warning['actions'] == [{'label': 'Set timeout to 8 s', 'field': 'load_test.request_timeout_seconds', 'value': 8}]
    assert 'estimated_requests' not in warning['values']
    assert all('estimated_requests' not in row for row in warning['values']['conditions'])
    assert warning['values']['min_valid_responses'] == 5


async def test_timeout_risk_also_warns_when_window_is_too_short(tmp_path):
    payload = values()
    payload['load_test']['schedule']['duration_seconds'] = 20
    payload['load_test']['pass_criteria']['min_valid_responses'] = 5
    payload['load_test']['request_timeout_seconds'] = 5
    risk, shortfall = await shortfall_preview(tmp_path, payload, [('short_short', 1, 4000, 6400)])
    assert risk['kind'] == 'load_timeout_risk'
    assert shortfall['kind'] == 'load_sample_shortfall'
    assert shortfall['values']['estimated_requests'] == 4
    assert shortfall['actions'][0]['value'] == 26


async def test_timeout_risks_group_per_timeout_field(tmp_path):
    payload = values()
    load = payload['load_test']
    load['schedule']['duration_seconds'] = 30
    load['pass_criteria']['min_valid_responses'] = 5
    load['request_timeout_seconds'] = 5
    load['workloads']['combinations']['selected'] = ['short_short', 'short_long']
    warning, = await shortfall_preview(tmp_path, payload, [
        ('short_short', 1, 4000, 6400), ('short_long', 1, 4500, 7100),
    ])
    assert warning['kind'] == 'load_timeout_risk'
    assert warning['msg'] == (
        '2 conditions had at least 1 in 10 recent responses longer than the 5 s request timeout, '
        'so some may time out and miss the 5 required valid responses: '
        'Short input → Short output, 1 at once (90th percentile about 6.4 s); '
        'Short input → Long output, 1 at once (90th percentile about 7.1 s). '
        'Raise the request timeout to at least 9 s.'
    )
    assert 'estimated_requests' not in warning['values']


@pytest.mark.parametrize('targets', [1, 2])
async def test_shortfall_groups_conditions_per_field_with_target_prefix_only_for_comparisons(tmp_path, targets):
    payload = values()
    if targets == 2:
        payload['targets'].append({**payload['targets'][0], 'name': 'second', 'url': 'http://second.test/v1'})
    load = payload['load_test']
    load['schedule']['duration_seconds'] = 20
    load['pass_criteria']['min_valid_responses'] = 5
    load['workloads']['combinations']['selected'] = ['short_short', 'short_long']
    warning, = await shortfall_preview(tmp_path, payload, [
        ('short_short', 1, 5000, 5000), ('short_long', 1, 6400, 6400),
    ])
    assert warning['kind'] == 'load_sample_shortfall'
    details = [f'{target + ": " if targets > 1 else ""}{shape}, 1 at once (about 4)'
               for target in ['local', 'second'][:targets]
               for shape in ['Short input → Short output', 'Short input → Long output']]
    assert warning['msg'] == (
        f'{2 * targets} conditions may get fewer than the 5 required valid responses in 20 s: '
        + '; '.join(details) + '. Giraffe will extend sending as needed (about 26 s); set “Send new requests for” to 26 s to plan for it.'
    )
    assert warning['actions'][0]['value'] == 26
    assert [row['required_seconds'] for row in warning['values']['conditions']] == [21, 26] * targets


async def test_timeout_shortfalls_group_per_timeout_field(tmp_path):
    payload = values()
    load = payload['load_test']
    load['request_timeout_seconds'] = 5
    load['workloads']['combinations']['selected'] = ['short_short', 'short_long']
    warning, = await shortfall_preview(tmp_path, payload, [
        ('short_short', 1, 6400, 6400), ('short_long', 1, 7100, 7100),
    ])
    assert warning['kind'] == 'load_timeout_shortfall'
    assert len(warning['values']['conditions']) == 2
    assert warning['msg'] == (
        '2 conditions typically take longer than the 5 s request timeout, '
        'so most responses will time out: '
        'Short input → Short output, 1 at once (about 6.4 s); '
        'Short input → Long output, 1 at once (about 7.1 s). '
        'Raise the request timeout to at least 9 s.'
    )
    assert 'estimated_requests' not in warning['values']
    assert warning['actions'] == [{
        'label': 'Set timeout to 9 s', 'field': 'load_test.request_timeout_seconds', 'value': 9,
    }]


async def test_migrated_shortfalls_use_source_settings_paths(tmp_path):
    payload = values()
    load = payload['load_test']
    load['schedule']['duration_seconds'] = 20
    load['pass_criteria']['min_valid_responses'] = 5
    migrated = deepcopy(load)
    migrated['request_timeout_seconds'] = 5
    load['migrated_tests'] = [{'source': 'concurrency', 'settings': migrated}]
    warnings = await shortfall_preview(tmp_path, payload, [('short_short', 1, 6400, 6400)])
    assert [(row['kind'], row['fields']) for row in warnings] == [
        ('load_sample_shortfall', ['load_test.schedule.duration_seconds']),
        ('load_timeout_shortfall', ['load_test.migrated_tests.0.settings.request_timeout_seconds']),
    ]
    assert warnings[1]['actions'][0]['field'] == warnings[1]['fields'][0]


async def test_group_uses_smallest_estimate_and_largest_required_duration(tmp_path):
    payload = values()
    load = payload['load_test']
    load['schedule']['duration_seconds'] = 20
    load['pass_criteria']['min_valid_responses'] = 5
    load['workloads']['combinations']['selected'] = ['short_short', 'short_long']
    warning, = await shortfall_preview(tmp_path, payload, [
        ('short_short', 1, 9000, 9000), ('short_long', 1, 6400, 6400),
    ])
    assert warning['values']['estimated_requests'] == 3
    assert warning['actions'][0]['value'] == 37
    assert '(about 3); Short input → Long output, 1 at once (about 4)' in warning['msg']


async def test_single_estimated_response_uses_exact_response_copy(tmp_path):
    payload = values()
    load = payload['load_test']
    load['schedule']['duration_seconds'] = 1
    load['pass_criteria']['min_valid_responses'] = 5
    warning, = await shortfall_preview(tmp_path, payload, [('short_short', 1, 6400, 6400)])
    assert warning['msg'] == (
        'Short input → Short output, 1 at once: about 1 of the 5 required valid responses can start '
        'in 1 s (recent responses take about 6.4 s). Giraffe will extend sending to about 26 s; set “Send new requests for” to 26 s to plan for it.'
    )
