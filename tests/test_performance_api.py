"""Derived performance stays bounded and separate from stored request evidence."""
import threading
import json

import httpx
import pytest

from giraffe.reporting import write_report
from giraffe.web import create_app
from tests.performance_fixture import synthetic_report_1000


@pytest.mark.asyncio
async def test_default_payload_and_selected_window_are_bounded(tmp_path):
    report = synthetic_report_1000()
    path, _ = write_report(report, tmp_path / 'saved')
    raw = json.loads(path.read_text())
    assert raw['performance']
    assert all('request_series' not in target for target in raw['performance']['targets'])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)), base_url='http://localhost') as client:
        response = await client.get('/api/runs/saved')
        assert response.status_code == 200
        assert len(response.content) < 1_500_000
        data = response.json()['report']
        assert data['requests_compact'] and data['request_count'] == 1000
        assert len(data['requests']) == 1000
        target = data['performance']['targets'][0]
        assert len(target['summaries']) == 1
        assert len(target['request_series']) == 1000
        assert target['full_series_summary_window'] == target['default_window']
        assert 'full_actual_concurrency' not in target
        assert 'full_completion_buckets' not in target
        assert target['summaries'][target['default_window']]['window']['request_count'] == 1000
        assert all('request_series' not in summary for summary in target['summaries'].values())
        window = target['windows'][-1]['id']
        selected = (await client.get('/api/runs/saved/performance', params={'window': window})).json()
        assert list(selected['targets'][0]['summaries']) == [window]
        summary = selected['targets'][0]['summaries'][window]
        assert summary['window']['id'] == window
        assert summary['request_row_indices']
        evidence = await client.get('/api/runs/saved/requests/SYNTHETIC-0001')
        assert evidence.status_code == 200
        assert evidence.json()['output_events']
        assert 'output_events' not in data['requests'][0]
        assert (await client.get('/api/runs/saved/performance', params={'window':'missing'})).status_code == 422
        assert (await client.get('/api/runs/saved/performance', params={'target':'missing'})).status_code == 422


@pytest.mark.asyncio
async def test_saved_selection_cache_is_small_and_invalidated_by_file_revision(tmp_path, monkeypatch):
    from giraffe import web

    report = synthetic_report_1000()
    path, _ = write_report(report, tmp_path / 'saved')
    calls = []
    original = web._ReportProcess.read

    def counting(self, *selection):
        calls.append(selection)
        return original(self, *selection)

    # This report exceeds the cold-parse threshold; observe helper dispatches,
    # while retaining the cache-hit and changed-measurement assertions.
    monkeypatch.setattr(web._ReportProcess, 'read', counting)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)), base_url='http://localhost') as client:
        url = '/api/runs/saved/performance'
        default = (await client.get(url)).json()
        count = len(calls)
        assert count == 1
        assert (await client.get(url)).json() == default
        assert len(calls) == count
        windows = default['targets'][0]['windows']
        for descriptor in windows[1:3]:
            assert (await client.get(url, params={'window': descriptor['id']})).status_code == 200
        count = len(calls)
        assert (await client.get(url)).status_code == 200
        assert len(calls) == count  # Encoded selections share the bounded byte cache.
        raw = json.loads(path.read_text())
        raw['requests'][0]['elapsed_ms'] = 999999
        path.write_text(json.dumps(raw))
        count = len(calls)
        refreshed = (await client.get(url)).json()
        assert len(calls) > count
        assert refreshed['targets'][0]['request_series'][0]['values']['response_latency_ms'] == 999999


@pytest.mark.asyncio
async def test_live_performance_is_on_demand_debounced_and_discloses_bounded_coverage(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from giraffe import web

    fixture = synthetic_report_1000()
    release = threading.Event()
    dispatched = threading.Event()
    updated = threading.Event()
    now = [1.0]
    monkeypatch.setattr(web, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(web, 'MAX_LIVE_REQUESTS', 1)

    async def runner(config, *, progress, stop_event):
        def emit(slot):
            row = fixture.requests[slot].model_copy(update={'target': 'local'})
            progress({'event':'request_completed', 'target':'local', 'scenario':row.scenario,
                      'request_slot':slot, 'request':row.model_dump(mode='json'),
                      'performance':{'must_not_leak':True}})
        emit(0)
        dispatched.set()
        assert await asyncio.to_thread(release.wait, 3)
        emit(1)
        updated.set()
        await stop_event.wait()
        return fixture

    monkeypatch.setattr(web, 'run_suite', runner)
    app = create_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            response = await client.post('/api/runs', json={'config':{'targets':[{'name':'local','url':'http://localhost/v1','model':'test'}],'checks':['serving']}})
            assert response.status_code == 202
            identifier = response.json()['id']
            assert await asyncio.to_thread(dispatched.wait, 3)
            detail = (await client.get(f'/api/runs/{identifier}')).json()
            assert detail['report'] is None
            assert 'performance' not in detail['progress']
            assert all('performance' not in event for event in detail['events'])
            first = (await client.get(f'/api/runs/{identifier}/performance')).json()
            assert first['coverage']['retained_requests'] == 1
            release.set()
            assert await asyncio.to_thread(updated.wait, 3)
            same = (await client.get(f'/api/runs/{identifier}/performance')).json()
            assert same == first
            now[0] += .6
            changed = (await client.get(f'/api/runs/{identifier}/performance')).json()
            assert changed['coverage']['retained_requests'] == 1
            assert changed['coverage']['started_requests'] == 2
            assert changed['coverage']['partial'] is True
            assert changed['targets'][0]['request_series'][0]['request_id'] == 'SYNTHETIC-0002'
            await client.post(f'/api/runs/{identifier}/cancel', json={})


def test_embedded_performance_is_preserved_on_load(tmp_path):
    from giraffe.cli import load_report
    from giraffe.report_stream import expanded_report_chunks

    report = synthetic_report_1000()
    path, _ = write_report(report, tmp_path)
    raw = json.loads(path.read_text())
    raw['performance'] = {'performance_schema':0, 'obsolete':True}
    path.write_text(json.dumps(raw))
    loaded = load_report(path)
    exported = json.loads(''.join(expanded_report_chunks(path)))
    # AM5: saved summaries are authoritative even when newer code is installed.
    assert loaded.performance == raw['performance']
    assert exported['performance'] == loaded.performance
    assert exported['performance']['obsolete'] is True


def test_compact_request_rows_preserve_false_and_zero_measurements():
    from giraffe.web import _api_report

    report = synthetic_report_1000()
    report.requests[0] = report.requests[0].model_copy(update={
        'status': 'completed', 'valid': False, 'score': None,
        'elapsed_ms': 0, 'dispatch_ms': 0, 'output_chars': 0,
        'evaluation_error': 'Evaluator unavailable',
    })
    row = _api_report(report)['requests'][0]
    assert row['valid'] is False
    assert row['elapsed_ms'] == 0
    assert row['dispatch_ms'] == 0
    assert row['output_chars'] == 0
    assert row['evaluation_error'] == 'Evaluator unavailable'
    assert 'output_events' not in row
