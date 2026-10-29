from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

import httpx


async def _finish_cleanup(task: asyncio.Task) -> None:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        # Retrieve any cleanup exception before propagating caller cancellation.
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    task.result()


class _LeasedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, release: Callable[[], None],
                 closing: set[asyncio.Task]):
        self._stream = stream
        self._release = release
        self._closing = closing
        self._close_task: asyncio.Task | None = None

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            yield chunk

    async def _close(self) -> None:
        try:
            await self._stream.aclose()
        finally:
            self._release()

    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
            self._closing.add(self._close_task)
            self._close_task.add_done_callback(self._closing.discard)
        await _finish_cleanup(self._close_task)


class HTTPClientPool:
    """Lease one persistent single-connection HTTPX client per active response.

    HTTPcore 1.0's shared HTTP/1.1 pool scans its connection list repeatedly and
    can assign the same idle connection to multiple waiting requests. Separate
    leased lanes keep that bookkeeping constant per request while preserving
    the configured total connection limit and HTTPX's transport semantics.
    """

    def __init__(self, factory: Callable[[], httpx.AsyncClient], max_connections: int):
        self._factory = factory
        self._first = factory()
        self._clients = [self._first]
        self._idle = deque([self._first])
        self._slots = asyncio.Semaphore(max_connections)
        self._closing: set[asyncio.Task] = set()
        self._close_task: asyncio.Task | None = None
        self._retirement_error: BaseException | None = None
        self._closed = False

    def build_request(self, *args, **kwargs) -> httpx.Request:
        return self._first.build_request(*args, **kwargs)

    async def send(self, request: httpx.Request, *, stream: bool = False) -> httpx.Response:
        if self._closed:
            raise RuntimeError("Cannot send a request after the client is closed")
        await self._slots.acquire()
        client = None
        try:
            if self._closed:
                raise RuntimeError("Cannot send a request after the client is closed")
            if self._idle:
                client = self._idle.pop()
            else:
                client = self._factory()
                self._clients.append(client)
            response = await client.send(request, stream=stream)
        except BaseException as error:
            try:
                if client is not None:
                    if isinstance(error, asyncio.CancelledError):
                        # Interrupted HTTPcore cleanup can strand its connection.
                        # Retire this lane without replaying the cancelled request.
                        closing = asyncio.create_task(client.aclose())
                        self._closing.add(closing)
                        closing.add_done_callback(self._closing.discard)
                        try:
                            await _finish_cleanup(closing)
                        except Exception as cleanup_error:
                            raise error from cleanup_error
                        finally:
                            failure = asyncio.CancelledError() if closing.cancelled() else closing.exception()
                            if failure is None:
                                self._clients.remove(client)
                            else:
                                self._closed = True
                                if self._retirement_error is None:
                                    self._retirement_error = failure
                    else:
                        self._idle.append(client)
            finally:
                self._slots.release()
            raise

        def release() -> None:
            self._idle.append(client)
            self._slots.release()

        if response.is_closed:
            release()
        else:
            response.stream = _LeasedStream(response.stream, release, self._closing)
        return response

    @asynccontextmanager
    async def stream(self, method: str, url: str | httpx.URL, **kwargs):
        response = await self.send(self.build_request(method, url, **kwargs), stream=True)
        try:
            yield response
        finally:
            await response.aclose()

    async def _close(self) -> None:
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)
        outcomes = await asyncio.gather(*(client.aclose() for client in self._clients),
                                        return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
        if self._retirement_error is not None:
            # HTTPX may already report closed after failed transport cleanup.
            raise self._retirement_error

    async def aclose(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close())
        await _finish_cleanup(self._close_task)
