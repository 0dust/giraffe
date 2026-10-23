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
