"""Real HTTP/1.1 response completion and connection reuse."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from giraffe.client import LLMClient
from giraffe.models import RequestSpec, RunConfig, Target


@asynccontextmanager
async def endpoint(*, connection_delay=0, eof_delay=0):
    connections, tasks, writers = [], set(), set()

    async def handle(reader, writer):
        tasks.add(asyncio.current_task())
        writers.add(writer)
        connections.append(writer.get_extra_info('peername'))
        try:
            await asyncio.sleep(connection_delay)
            while True:
                headers = await reader.readuntil(b'\r\n\r\n')
                length = next(int(line.split(b':', 1)[1]) for line in headers.split(b'\r\n')
                              if line.lower().startswith(b'content-length:'))
                await reader.readexactly(length)
                data = ('data: ' + json.dumps({'choices': [{'delta': {'content': '42'},
                    'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 8, 'completion_tokens': 1}})
                    + '\n\ndata: [DONE]\n\n').encode()
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n'
                             b'Transfer-Encoding: chunked\r\n\r\n'
                             + f'{len(data):x}\r\n'.encode() + data + b'\r\n')
                await writer.drain()
                await asyncio.sleep(eof_delay)
                writer.write(b'0\r\n\r\n')
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            writers.discard(writer)
            tasks.discard(asyncio.current_task())

    server = await asyncio.start_server(handle, '127.0.0.1', 0)
    target = Target(name='local', url=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1',
                    model='test')
    try:
        yield target, connections
    finally:
        server.close()
        for writer in tuple(writers):
            writer.close()
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await server.wait_closed()


def request():
    return RequestSpec(fixture_id='keepalive', scenario='serving', check_ids=['serving'],
                       messages=[{'role': 'user', 'content': 'Return 42'}])


@pytest.mark.parametrize('concurrency', [1, 2])
async def test_completed_stream_reuses_http_connection(concurrency):
    async with endpoint() as (target, connections):
        async with LLMClient(target, RunConfig(targets=[target]), max_connections=concurrency) as client:
            records = []
            for _ in range(3):
                records.extend(await asyncio.gather(*(client.execute(request()) for _ in range(concurrency))))
        assert all(record.status == 'completed' for record in records)
        assert len(connections) == concurrency, f'Completed streams opened {len(connections)} TCP connections'


async def test_done_with_delayed_http_eof_returns_before_transport_cleanup():
    async with endpoint(eof_delay=.06) as (target, connections):
        async with LLMClient(target, RunConfig(targets=[target]), max_connections=1) as client:
            record = await client.execute(request())
            assert record.status == 'completed'
            assert client._stream_drains  # EOF is still pending when execute returns.
            timing = (record.elapsed_ms, record.first_token_ms, record.stream_end_ms, record.response_body_bytes)
            await asyncio.gather(*client._stream_drains)
            assert timing == (record.elapsed_ms, record.first_token_ms, record.stream_end_ms, record.response_body_bytes)
            assert (await client.execute(request())).status == 'completed'
        assert len(connections) == 1


async def test_done_without_http_eof_has_bounded_cleanup(monkeypatch):
    monkeypatch.setattr('giraffe.client._STREAM_DRAIN_SECONDS', .02)
    async with endpoint(eof_delay=10) as (target, connections):
        async with asyncio.timeout(1):
            async with LLMClient(target, RunConfig(targets=[target]), max_connections=1) as client:
                records = [await client.execute(request()) for _ in range(2)]
            assert all(record.status == 'completed' for record in records)
            assert not client._stream_drains
        assert len(connections) == 2


async def test_cancelled_client_shutdown_closes_pending_stream():
    async with endpoint(eof_delay=10) as (target, _):
        client = await LLMClient(target, RunConfig(targets=[target])).__aenter__()
        try:
            assert (await client.execute(request())).status == 'completed'
            shutdown = asyncio.create_task(client.__aexit__())
            await asyncio.sleep(0)  # Let shutdown join the pending transport drain.
            shutdown.cancel()
            with pytest.raises(asyncio.CancelledError):
                await shutdown
            assert not client._stream_drains
            assert client._client is None
        finally:
            await client.__aexit__()

async def test_repeated_concurrent_streams_keep_pool_bookkeeping_linear(monkeypatch):
    import httpcore

    idle_checks = 0
    original = httpcore.AsyncHTTPConnection.is_idle

    def is_idle(connection):
        nonlocal idle_checks
        idle_checks += 1
        return original(connection)

    monkeypatch.setattr(httpcore.AsyncHTTPConnection, 'is_idle', is_idle)
    concurrency = 64
    async with endpoint() as (target, connections):
        async with LLMClient(target, RunConfig(targets=[target]), max_connections=concurrency) as client:
            records = []
            for _ in range(4):
                records.extend(await asyncio.gather(*(client.execute(request()) for _ in range(concurrency))))
        assert all(record.valid for record in records)
        assert len(connections) == concurrency
    # Count pool work instead of asserting a machine-dependent wall-clock limit.
    # Reused HTTP/1.1 connections must not cause a scan of every other connection
    # for every request, or retry assignments to already-claimed connections.
    assert idle_checks <= len(records) * 16


async def test_waiting_request_timeout_does_not_release_a_draining_connection():
    async with endpoint(eof_delay=.05) as (target, connections):
        async with LLMClient(target, RunConfig(targets=[target]), max_connections=1) as client:
            assert (await client.execute(request())).valid
            impatient = request().model_copy(update={'request_timeout_seconds': .005})
            timed_out = await client.execute(impatient)
            assert timed_out.status == 'timeout'
            assert (await client.execute(request())).valid
        assert len(connections) == 1


async def test_serial_requests_reuse_one_connection_despite_high_configured_concurrency():
    async with endpoint() as (target, connections):
        async with LLMClient(target, RunConfig(targets=[target]), max_connections=64) as client:
            for _ in range(4):
                assert (await client.execute(request())).valid
                await asyncio.gather(*client._stream_drains)
            assert len(client._client._clients) == 1
        assert len(connections) == 1


async def test_burst_then_serial_reuses_the_most_recent_idle_connection():
    async with endpoint() as (target, connections):
        async with LLMClient(target, RunConfig(targets=[target]), max_connections=8) as client:
            assert all(record.valid for record in await asyncio.gather(
                *(client.execute(request()) for _ in range(8))))
            await asyncio.gather(*client._stream_drains)
            serial_lanes = []
            for _ in range(3):
                assert (await client.execute(request())).valid
                await asyncio.gather(*client._stream_drains)
                serial_lanes.append(client._client._idle[-1])
            assert all(lane is serial_lanes[0] for lane in serial_lanes)
        assert len(connections) == 8


async def test_lazy_client_creation_failure_restores_connection_slot():
    import httpx
    from giraffe.transport import HTTPClientPool

    blocked, release = asyncio.Event(), asyncio.Event()
    calls, clients = 0, []

    async def handle(_):
        blocked.set()
        await release.wait()
        return httpx.Response(200, text='ok')

    def factory():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError('construction failed')
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        clients.append(client)
        return client

    pool = HTTPClientPool(factory, 2)
    try:
        first = asyncio.create_task(pool.send(pool.build_request('GET', 'http://local')))
        await asyncio.wait_for(blocked.wait(), 1)
        with pytest.raises(RuntimeError, match='construction failed'):
            await pool.send(pool.build_request('GET', 'http://local'))
        release.set()
        assert (await first).status_code == 200
        assert (await pool.send(pool.build_request('GET', 'http://local'))).status_code == 200
        assert pool._slots._value == 2
    finally:
        release.set()
        await pool.aclose()
    assert all(client.is_closed for client in clients)


async def test_close_failure_still_closes_every_client_and_is_idempotent():
    import httpx
    from giraffe.transport import HTTPClientPool

    entered, release = asyncio.Event(), asyncio.Event()
    clients = []

    async def handle(_):
        entered.set()
        await release.wait()
        return httpx.Response(200, text='ok')

    def factory():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        clients.append(client)
        return client

    pool = HTTPClientPool(factory, 2)
    first = asyncio.create_task(pool.send(pool.build_request('GET', 'http://local')))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(pool.send(pool.build_request('GET', 'http://local')))
    while len(clients) < 2:
        await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    original = clients[0].aclose

    async def fail_after_close():
        await original()
        raise httpx.CloseError('close failed')

    clients[0].aclose = fail_after_close
    for _ in range(2):
        with pytest.raises(httpx.CloseError, match='close failed'):
            await pool.aclose()
    assert all(client.is_closed for client in clients)


async def test_cancelled_stream_close_keeps_lane_until_transport_close_finishes():
    import httpx
    from giraffe.transport import HTTPClientPool

    closing, finish = asyncio.Event(), asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'ok'

        async def aclose(self):
            closing.set()
            await finish.wait()

    pool = HTTPClientPool(lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, stream=Stream()))), 1)
    try:
        first = await pool.send(pool.build_request('GET', 'http://local'), stream=True)
        close = asyncio.create_task(first.aclose())
        await asyncio.wait_for(closing.wait(), 1)
        waiting = asyncio.create_task(pool.send(pool.build_request('GET', 'http://local'), stream=True))
        await asyncio.sleep(0)
        close.cancel()
        await asyncio.sleep(0)
        assert not waiting.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await close
        second = await waiting
        await second.aclose()
        assert pool._slots._value == 1
    finally:
        finish.set()
        await pool.aclose()


async def test_all_lanes_share_cookie_state_and_request_settings(monkeypatch):
    import httpx

    entered, release = asyncio.Event(), asyncio.Event()
    captured, requests = [], []
    original = httpx.AsyncClient

    async def handle(request):
        requests.append(request)
        if len(requests) == 2:
            entered.set()
        await release.wait()
        return httpx.Response(200, headers={'set-cookie': 'session=shared; Path=/'}, json={
            'choices': [{'message': {'content': '42'}, 'finish_reason': 'stop'}]})

    def factory(**kwargs):
        captured.append(dict(kwargs))
        kwargs['proxy'] = None  # Capture proxy configuration without contacting it.
        return original(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, 'AsyncClient', factory)
    monkeypatch.setenv('TRANSPORT_TEST_KEY', 'test-key')
    monkeypatch.setenv('TRANSPORT_TEST_HEADER', 'test-header')
    target = Target(name='local', url='http://local', model='test', api_key_env='TRANSPORT_TEST_KEY',
                    headers_env={'x-custom': 'TRANSPORT_TEST_HEADER'})
    config = RunConfig(targets=[target], stream=False, proxy='http://proxy.local:8080')
    async with LLMClient(target, config, max_connections=2) as client:
        records = asyncio.gather(client.execute(request()), client.execute(request()))
        await asyncio.wait_for(entered.wait(), 1)
        release.set()
        assert all(record.valid for record in await records)
        assert (await client.execute(request())).valid
    assert len(captured) == 2
    assert captured[0]['verify'] is captured[1]['verify']
    assert captured[0]['cookies'] is captured[1]['cookies']
    assert all(options['trust_env'] and not options['follow_redirects'] for options in captured)
    assert all(options['proxy'] == 'http://proxy.local:8080' for options in captured)
    assert all(request.headers['authorization'] == 'Bearer test-key' and
               request.headers['x-custom'] == 'test-header' for request in requests)
    assert requests[-1].headers['cookie'] == 'session=shared'
