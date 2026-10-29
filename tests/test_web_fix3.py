"""Cold-report isolation and transient helper failures."""
import asyncio
import gc
import json
import subprocess
import threading

import pytest

from giraffe import web
from giraffe.models import RunConfig
from giraffe.reporting import write_report
from tests.test_web import browser, config, report, start

pytestmark = pytest.mark.usefixtures('local_tokenizer')


def save_report(root, count=1):
    value = report(RunConfig.model_validate(config()))
    value.requests = [value.requests[0].model_copy(update={'id': str(index)}) for index in range(count)]
    path, _ = write_report(value, root / 'saved')
    return path


async def test_one_to_four_mib_report_uses_helper(tmp_path, monkeypatch):
    path = save_report(tmp_path, 1024)
    assert 1024**2 < path.stat().st_size < 4 * 1024**2
    calls = []
    def read(self, *args):
        calls.append(args)
        return 200, b'{"isolated":true}'
    monkeypatch.setattr(web._ReportProcess, 'read', read)
    async with browser(tmp_path) as client:
        response = await client.get('/api/runs/saved')
        assert response.json().get('isolated') is True
    assert len(calls) == 1


@pytest.mark.parametrize('kind', ['', '/performance'])
@pytest.mark.parametrize('status', [404, 500, 504])
async def test_helper_error_is_not_cached(tmp_path, monkeypatch, kind, status):
    save_report(tmp_path)
    monkeypatch.setattr(web, '_ISOLATED_REPORT_BYTES', 0)
    calls = []
    def read(self, *args):
        calls.append(1)
        return (status, b'{"detail":"temporary"}') if len(calls) == 1 else (200, b'{"ok":true}')
    monkeypatch.setattr(web._ReportProcess, 'read', read)
    async with browser(tmp_path) as client:
        assert (await client.get('/api/runs/saved' + kind)).status_code == status
        assert (await client.get('/api/runs/saved' + kind)).json() == {'ok': True}
        assert len(calls) == 2
        assert (await client.get('/api/runs/saved' + kind)).json() == {'ok': True}
        assert len(calls) == 2


async def test_saved_helper_does_not_block_live_performance(tmp_path, monkeypatch):
    save_report(tmp_path)
    monkeypatch.setattr(web, '_ISOLATED_REPORT_BYTES', 0)
    started = threading.Event()
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    def read(self, *args):
        entered.set()
        try:
            assert release.wait(2)
            return 200, b'{"ok":true}'
        finally:
            exited.set()
    async def held(configuration, *, progress, stop_event):
        started.set()
        await stop_event.wait()
        return report(configuration)
    monkeypatch.setattr(web._ReportProcess, 'read', read)
    monkeypatch.setattr(web, 'run_suite', held)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        # Measure helper isolation after startup GC has completed.
        assert await asyncio.to_thread(started.wait, 2)
        saved = asyncio.create_task(client.get('/api/runs/saved/performance'))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            assert not exited.is_set()
            response = await asyncio.wait_for(client.get(f'/api/runs/{identifier}/performance'), .3)
            assert response.status_code == 200
            assert not exited.is_set()
        finally:
            release.set()
            await saved
        await client.post(f'/api/runs/{identifier}/cancel', json={})


def test_import_stdout_cannot_corrupt_helper_protocol(tmp_path, monkeypatch):
    save_report(tmp_path)
    commands = []
    def capture(command, **kwargs):
        commands.append(command)
        raise RuntimeError('capture bootstrap')
    with monkeypatch.context() as patch:
        patch.setattr(web.subprocess, 'Popen', capture)
        with pytest.raises(RuntimeError, match='capture bootstrap'):
            web._ReportProcess().read(tmp_path, 'saved', 'detail')
    command = commands[0]
    command[-1] = command[-1].replace('from giraffe.web',
        "print('python import noise'); __import__('os').write(1,b'fd import noise\\n'); from giraffe.web")
    request = json.dumps([str(tmp_path), 'saved', 'detail', None, None, 0]) + '\n'
    result = subprocess.run(command, input=request.encode(), capture_output=True, timeout=10)
    import struct
    status, size = struct.unpack('!II', result.stdout[:8])
    assert status == 200
    assert len(result.stdout[8:]) == size
    assert json.loads(result.stdout[8:])['report']['request_count'] == 1
    assert b'python import noise' in result.stderr and b'fd import noise' in result.stderr


def test_measurement_gc_policy_restores_state_and_records_pauses():
    thresholds = gc.get_threshold()
    frozen = gc.get_freeze_count()
    with web._measurement_gc() as pauses:
        assert gc.get_freeze_count() > 0
        assert gc.get_threshold()[2] > thresholds[2]
        gc.collect(0)
    assert gc.get_threshold() == thresholds
    assert bool(gc.get_freeze_count()) == bool(frozen)
    assert pauses['gc_pause_count'] >= 1
    assert pauses['gc_pause_total_ms'] >= pauses['gc_pause_max_ms'] >= 0


async def test_run_start_does_not_wait_for_recovery_parse(tmp_path, monkeypatch):
    import socket
    import sys
    from giraffe.storage import run_alive, write_metadata
    from tests.test_web import finished

    # A guessed PID (getpid() + 1) may belong to a live same-host process,
    # which must remain protected even after its heartbeat goes stale.
    with subprocess.Popen([sys.executable, '-c', 'pass']) as owner:
        owner.wait(timeout=5)
    directory = tmp_path / 'interrupted'
    meta = {'id': directory.name, 'state': 'running', 'config': config(),
            'owner_pid': owner.pid, 'owner_host': socket.gethostname(),
            'heartbeat_at': web._now()}
    write_metadata(directory, meta)
    assert run_alive(directory, meta)  # Fresh heartbeat defers startup recovery.
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    original_recover = web.recover_evidence
    def recover(directory, metadata, *, pause):
        entered.set()
        assert release.wait(3)
        assert pause.is_set()
        exited.set()
        return None
    async def held(configuration, *, progress, stop_event):
        await stop_event.wait()
        return report(configuration)
    monkeypatch.setattr(web, 'recover_evidence', recover)
    monkeypatch.setattr(web, 'run_suite', held)
    monkeypatch.setattr(web, '_RECOVERY_SECONDS', .01)
    async with browser(tmp_path) as client:
        stale = meta | {'heartbeat_at': '2000-01-01T00:00:00Z'}
        assert not run_alive(directory, stale)
        write_metadata(directory, stale)
        assert await asyncio.to_thread(entered.wait, 1)
        try:
            identifier = await asyncio.wait_for(start(client), .5)
        finally:
            release.set()
        assert await asyncio.to_thread(exited.wait, 1)
        monkeypatch.setattr(web, 'recover_evidence', original_recover)
        await client.post(f'/api/runs/{identifier}/cancel', json={})
        await finished(client, identifier)


def test_helper_deadline_scales_with_report_size(tmp_path, monkeypatch):
    path = save_report(tmp_path, 1024)
    timeouts = []
    selector = web.selectors.DefaultSelector
    class TimedSelector:
        def __enter__(self):
            self.value = selector()
            return self
        def __exit__(self, *args):
            self.value.close()
        def register(self, *args):
            return self.value.register(*args)
        def select(self, timeout):
            timeouts.append(timeout)
            return self.value.select(timeout)
    monkeypatch.setattr(web.selectors, 'DefaultSelector', TimedSelector)
    helper = web._ReportProcess()
    try:
        assert helper.read(tmp_path, 'saved', 'detail')[0] == 200
    finally:
        helper.close()
    assert 30 < timeouts[0] <= 30 + path.stat().st_size / (10 * 1024**2)


async def test_cli_owner_does_not_block_web_run(tmp_path, monkeypatch):
    import os
    import socket
    from giraffe.storage import write_metadata

    write_metadata(tmp_path / 'cli', {'id': 'cli', 'state': 'running',
        'heartbeat_at': web._now(), 'owner_pid': os.getpid() + 1,
        'owner_host': socket.gethostname(), 'config': config()})
    async def held(configuration, *, progress, stop_event):
        await stop_event.wait()
        return report(configuration)
    monkeypatch.setattr(web, 'run_suite', held)
    async with browser(tmp_path) as client:
        identifier = await start(client)
        history = (await client.get('/api/runs')).json()
        assert history['active_run_id'] == identifier
        assert history['disk_active_id'] == 'cli'
        assert (await client.get(f'/api/runs/{identifier}')).json()['stoppable'] is True
        assert (await client.get('/api/runs/cli')).json()['stoppable'] is False
        await client.post(f'/api/runs/{identifier}/cancel', json={})
