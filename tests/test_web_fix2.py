"""Reviewer regressions for cross-thread handoff and bounded recovery."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import errno
import threading
import time

import pytest

from giraffe import web
from giraffe.evidence import EvidenceWriter, recover_evidence
from giraffe.storage import write_metadata
from tests.test_evidence import record
from tests.test_web import browser, config, finished, report, start, successful_runner

pytestmark = pytest.mark.usefixtures('local_tokenizer')


def test_cached_hit_does_not_wait_for_unrelated_cold_factory():
    cache = web._ReportCache()
    cache.get('hot', 1, lambda: 'ready')
    entered, release = threading.Event(), threading.Event()
    def slow():
        entered.set()
        assert release.wait(2)
        return 'cold'
    with ThreadPoolExecutor(2) as pool:
        cold = pool.submit(cache.get, 'cold', 1, slow)
        try:
            assert entered.wait(1)
            assert pool.submit(cache.get, 'hot', 1, lambda: None).result(.2) == 'ready'
        finally:
            release.set()
        assert cold.result(1) == 'cold'


def test_cancelled_worker_cannot_kill_shared_host():
    owner = web._RunLoop()
    async def measured(stop):
        return 123
    first = web._RunThread(measured, owner=owner)
    first.future.cancel()
    first.start()
    second = web._RunThread(measured, owner=owner)
    try:
        first.thread.join(.15)  # A canceled queued job must be skipped, not kill the host.
        second.start()
        assert second.future.result(2) == 123
    finally:
        owner.close()


def test_dead_shared_host_is_restarted_or_rejected_immediately():
    owner = web._RunLoop()
    owner.thread = threading.Thread(target=lambda: None)
    owner.thread.start()
    owner.thread.join(1)
    async def measured(stop):
        return 123
    worker = web._RunThread(measured, owner=owner)
    try:
        worker.start()
        assert worker.future.result(.5) == 123
    finally:
        owner.close()


async def test_terminal_publish_failure_still_releases_active(tmp_path, monkeypatch):
    track = web._track_live_progress
    def broken(job, event, limit):
        if event['event'] == 'run_finished':
            raise RuntimeError('terminal publication failed')
        return track(job, event, limit)
    monkeypatch.setattr(web, '_track_live_progress', broken)
    monkeypatch.setattr(web, 'run_suite', successful_runner)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        await finished(client, identifier)
        for _ in range(100):
            if (await client.get('/api/bootstrap')).json()['active_run_id'] is None:
                break
            await asyncio.sleep(.01)
        assert (await client.get('/api/bootstrap')).json()['active_run_id'] is None
        assert (await client.post('/api/runs', json={'config': config()})).status_code == 202


async def test_progress_boundary_snapshots_nested_condition_state(tmp_path, monkeypatch):
    ready = threading.Event()
    async def measured(configuration, *, progress, stop_event):
        condition = {'target': 'local', 'condition': {'id': 'warm'}, 'status': 'running',
                     'request_statuses': {'a': 'running'}}
        progress({'event': 'condition_started', 'condition_summary': condition})
        condition['request_statuses']['b'] = 'completed'
        ready.set()
        await stop_event.wait()
        return report(configuration)
    monkeypatch.setattr(web, 'run_suite', measured)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        assert await asyncio.to_thread(ready.wait, 2)
        delta = (await client.get(f'/api/runs/{identifier}/events?after=0')).json()
        event = next(row for row in delta['events'] if row['event'] == 'condition_started')
        assert event['condition_summary']['request_statuses'] == {'a': 'running'}
        await client.post(f'/api/runs/{identifier}/cancel', json={})


async def test_check_updates_do_not_replace_progress_or_carry_request_payload(tmp_path, monkeypatch):
    async def measured(configuration, *, progress, stop_event):
        progress({'event': 'request_completed', 'request_slot': 0,
                  'request': {'id': 'last', 'status': 'completed'}, 'completed': 1})
        return report(configuration)
    monkeypatch.setattr(web, 'run_suite', measured)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        detail = await finished(client, identifier)
        delta = (await client.get(f'/api/runs/{identifier}/events?after=0')).json()
        check = next(row for row in delta['events'] if row['event'] == 'check_finished')
        assert 'request_slot' not in check and 'request' not in check
        assert detail['progress']['event'] == 'request_completed'
        assert detail['progress']['completed'] == 1


def test_evidence_handoff_does_not_deepcopy_retained_body_on_caller(tmp_path, monkeypatch, request):
    original = type(record()).model_copy
    caller = threading.get_ident()
    def copy(self, *args, **kwargs):
        assert not (kwargs.get('deep') and threading.get_ident() == caller)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(type(record()), 'model_copy', copy)
    writer = EvidenceWriter(tmp_path, {'config': {'retention': 'all'}})
    try:
        large = record(response_events=[{'text': 'x' * 10000} for _ in range(256)])
        began = time.perf_counter()
        writer.record_finished(large)
        elapsed = (time.perf_counter() - began) * 1000
        request.node.user_properties.append(('large_evidence_handoff_ms', elapsed))
        assert elapsed < 25
    finally:
        writer.close()
    assert len(recover_evidence(tmp_path, {}).requests) == 1


async def test_overflow_is_disclosed_in_final_and_recovered_reports(tmp_path, monkeypatch):
    class Overflow(EvidenceWriter):
        def record_finished(self, record):
            self.dropped_count += 1
    monkeypatch.setattr(web, 'EvidenceWriter', Overflow)
    async def measured(configuration, *, progress, stop_event):
        from giraffe.runner import record_sink
        for index in range(7):
            record_sink.get()(record(index))
        return report(configuration)
    monkeypatch.setattr(web, 'run_suite', measured)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        detail = await finished(client, identifier)
        assert '7 evidence records not written to evidence.jsonl' in detail['report']['warnings']
        assert detail['report']['observations']['evidence_dropped_count'] == 7
    directory = tmp_path / 'recovered'
    writer = Overflow(directory)
    for index in range(7):
        writer.record_finished(record(index))
    writer.close()
    partial = recover_evidence(directory, {})
    assert partial.observations['evidence_dropped_count'] == 7
    assert '7 evidence records not written to evidence.jsonl' in partial.warnings


async def test_recovery_enospc_has_bounded_retries_and_terminal_error(tmp_path, monkeypatch):
    directory = tmp_path / 'crashed'
    writer = EvidenceWriter(directory, {'config': config()})
    writer.record_finished(record())
    writer.close()
    write_metadata(directory, {'id': 'crashed', 'state': 'interrupted', 'config': config()})
    calls = []
    original_write = web.write_report
    def fail(report, directory, **kwargs):
        if directory.name == 'crashed':
            calls.append(1)
            raise OSError(errno.ENOSPC, 'disk full')
        return original_write(report, directory, **kwargs)
    async def held(configuration, *, progress, stop_event):
        await stop_event.wait()
        return report(configuration)
    monkeypatch.setattr(web, 'run_suite', held)
    monkeypatch.setattr(web, 'write_report', fail)
    monkeypatch.setattr(web, '_RECOVERY_SECONDS', .005)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        before = len(calls)
        await asyncio.sleep(.1)
        assert len(calls) == before
        await client.post(f'/api/runs/{identifier}/cancel', json={})
        await finished(client, identifier)
        for _ in range(200):
            detail = (await client.get('/api/runs/crashed')).json()
            if detail['state'] == 'error':
                break
            await asyncio.sleep(.005)
        assert detail['state'] == 'error'
        assert 'recovery failed' in detail['error'].lower()
        assert len(calls) == 5
        await asyncio.sleep(.1)
        assert len(calls) == 5


async def test_bootstrap_and_history_discover_live_cli_run(tmp_path):
    import os
    import socket
    directory = tmp_path / 'cli'
    write_metadata(directory, {'id': 'cli', 'state': 'running', 'heartbeat_at': web._now(),
        'owner_pid': os.getpid() + 1, 'owner_host': socket.gethostname(), 'config': config()})
    async with browser(tmp_path) as client:
        for endpoint in ('/api/bootstrap', '/api/runs'):
            payload = (await client.get(endpoint)).json()
            assert payload['active_run_id'] is None
            assert payload['disk_active_id'] == 'cli'
        assert (await client.get('/api/runs/cli')).json()['stoppable'] is False


async def test_previous_job_still_stopping_rejects_new_post(tmp_path, monkeypatch):
    # Exercise expiry of the report-saving window without waiting 30 seconds.
    monkeypatch.setattr(web, "_PARTIAL_REPORT_SECONDS", .05)
    tasks = []
    entered, stopping, release = threading.Event(), threading.Event(), threading.Event()
    original = web._RunThread.start
    def capture(worker):
        tasks.append(asyncio.current_task())
        original(worker)
    async def held(configuration, *, progress, stop_event):
        entered.set()
        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            stopping.set()
            assert await asyncio.to_thread(release.wait, 5)
        return report(configuration)
    monkeypatch.setattr(web._RunThread, 'start', capture)
    monkeypatch.setattr(web, 'run_suite', held)
    async with browser(tmp_path) as client:
        await start(client)
        assert await asyncio.to_thread(entered.wait, 2)
        tasks[0].cancel()
        assert await asyncio.to_thread(stopping.wait, 2)
        try:
            await asyncio.wait_for(asyncio.gather(tasks[0], return_exceptions=True), 3)
            response = await client.post('/api/runs', json={'config': config()})
            assert response.status_code == 409
            assert 'previous run still stopping' in response.json()['detail'].lower()
        finally:
            release.set()
        for _ in range(100):
            await asyncio.sleep(.01)
            response = await client.post('/api/runs', json={'config': config()})
            if response.status_code == 202:
                break
        assert response.status_code == 202
        await client.post(f"/api/runs/{response.json()['id']}/cancel", json={})


async def test_dead_thread_next_post_restarts_engine(tmp_path, monkeypatch):
    owners = []
    original = web._RunThread.start
    def capture(worker):
        owners.append(worker.owner)
        original(worker)
    monkeypatch.setattr(web._RunThread, 'start', capture)
    monkeypatch.setattr(web, 'run_suite', successful_runner)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        await finished(client, identifier)
        owner = owners[0]
        await asyncio.to_thread(owner.close)
        assert not owner.thread.is_alive()
        owner.closing.clear()  # Simulate a thread which exited unexpectedly, not app shutdown.
        response = await asyncio.wait_for(client.post('/api/runs', json={'config': config()}), 2)
        assert response.status_code == 202
        assert (await asyncio.wait_for(finished(client, response.json()['id']), 3))['state'] == 'completed'


def test_watchdog_exits_a_hung_test_process(tmp_path):
    import os
    import subprocess
    import sys
    test = tmp_path / 'test_stalled.py'
    test.write_text('def test_stalled():\n    import time\n    time.sleep(60)\n')
    env = dict(os.environ, GIRAFFE_TEST_TIMEOUT_SECONDS='.2')
    result = subprocess.run([sys.executable, '-m', 'pytest', '-p', 'tests.conftest',
                             str(test), '-q'], env=env, capture_output=True, timeout=8)
    assert result.returncode == 1
    assert b'Timeout' in result.stderr and b'test_stalled' in result.stderr
