"""Measure the real runner/SSE client at 256, 512 and 1024 simultaneous requests.

Run --mode in-process without sockets, or --mode socket outside a restricted
sandbox. Socket mode starts one asyncio HTTP/1.1 chunked streaming endpoint on
loopback (not a thread per connection). --url instead measures an external fake
endpoint. Output distinguishes accepted workload from delivered fake-client peak.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import time

from giraffe.client import LLMClient
from giraffe.caps import MAX_SIMULTANEOUS_REQUESTS
from giraffe.models import RequestSpec, RunConfig, Target
from giraffe.runner import _Run

from _measurement import FakeEndpoint, distribution, environment, mock_client


async def serve_fake(reader, writer, endpoint):
    try:
        while True:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next((int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                           if line.lower().startswith(b"content-length:")), 0)
            await reader.readexactly(length)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                         b"Transfer-Encoding: chunked\r\nConnection: keep-alive\r\n\r\n")
            async for chunk in endpoint.stream():
                writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                await writer.drain()
            writer.write(b"0\r\n\r\n")
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


async def measure(args, level):
    endpoint = FakeEndpoint(chunks=args.chunks, delay=args.chunk_interval_ms / 1000,
                            body_bytes=args.body_bytes, barrier=level)
    server = None
    url = args.url or "http://measurement.invalid/v1"
    if args.mode == "socket" and not args.url:
        server = await asyncio.start_server(lambda r, w: serve_fake(r, w, endpoint),
                                            "127.0.0.1", 0, backlog=max(2048, level * 2))
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1"
    target = Target(name="measurement", url=url, model="fake-stream")
    config = RunConfig(targets=[target], checks=["correctness"],
                       default_concurrency=min(level, MAX_SIMULTANEOUS_REQUESTS),
                       request_timeout_seconds=60, stop_after_errors=100000)
    # Experiments must remain repeatable after evidence rejects a candidate bound.
    # Bypass validation only here; the real runtime receives the same candidate
    # default_concurrency used before a production schema decision was made.
    config = config.model_copy(update={"default_concurrency": level})
    run = _Run(config, None, None)
    spec = RequestSpec(fixture_id="measurement", scenario="warm", check_ids=["correctness"],
                       messages=[{"role": "user", "content": "Return deterministic measurement text."}],
                       max_tokens=max(128, args.body_bytes), stream=True)
    client = (mock_client(target, config, endpoint, level) if args.mode == "in-process"
              else await LLMClient(target, config, max_connections=level).__aenter__())
    client.on_response = lambda *_: run.endpoint_response(target.name)
    lateness = []

    async def batch(due):
        async def request():
            lateness.append(max(0, (time.monotonic() - due) * 1000))
            return await run.request(target, client, spec)
        await asyncio.gather(*(request() for _ in range(level)))

    # Start heartbeat before releasing the synchronized cohort; includes launch and drain stalls.
    async def workload():
        for _ in range(args.rounds):
            endpoint.gate.clear()
            await batch(time.monotonic())

    started, cpu = time.monotonic(), time.process_time()
    task = asyncio.create_task(workload())
    monitor = asyncio.create_task(run.monitor([task]))
    try:
        await task
        await monitor
        elapsed, cpu_elapsed = time.monotonic() - started, time.process_time() - cpu
    finally:
        await client.__aexit__(None, None, None)
        if server is not None:
            server.close()
            await server.wait_closed()
    records = run.records
    return {"mode": args.mode, "level": level,
            "candidate_above_current_schema": level > MAX_SIMULTANEOUS_REQUESTS,
            "candidate_construction": "validated config then model_copy(default_concurrency=level) for candidate measurement",
            "rounds": args.rounds, "requests": len(records),
            "completed": sum(r.status == "completed" for r in records),
            "failed": sum(r.status != "completed" for r in records), "wall_seconds": elapsed,
            "cpu_seconds": cpu_elapsed, "cpu_ms_per_request": cpu_elapsed * 1000 / len(records) if records else None,
            "runner_peak": max(run.peak.values(), default=0),
            "endpoint_peak": endpoint.peak if not args.url else None,
            "timing_health": run.timing_health.snapshot(),
            "scheduler_lateness_ms": distribution(lateness),
            "scheduler_lateness_definition": "request coroutine start minus common cohort release; includes cohort launch serialization",
            "fake_chunk_lateness_ms": distribution(endpoint.chunk_lateness_ms),
            "fake_chunk_lateness_definition": "actual minus scheduled per-stream chunk emission, fixed cadence",
            "elapsed_ms": distribution([r.elapsed_ms for r in records])}


async def main_async(args):
    return {"environment": environment(), "parameters": vars(args),
            "limitations": ["Fake endpoint is in the measured process; CPU includes its SSE production.",
                             "In-process MockTransport exercises parsing/runner, not sockets/TLS/HTTP connection pooling.",
                             "Accepted ceiling is not demonstrated delivery capacity."],
            "measurements": [await measure(args, level) for level in args.levels]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["in-process", "socket"], default="in-process")
    parser.add_argument("--levels", type=int, nargs="+", default=[256, 512, 1024])
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--chunks", type=int, default=4)
    parser.add_argument("--chunk-interval-ms", type=float, default=50)
    parser.add_argument("--body-bytes", type=int, default=1024)
    parser.add_argument("--url", help="External fake streaming endpoint (socket mode only)")
    parser.add_argument("--output")
    args = parser.parse_args()
    if any(n < 1 for n in args.levels) or min(args.rounds, args.chunks, args.body_bytes, args.chunk_interval_ms) <= 0:
        parser.error("levels, rounds, chunks, body size and chunk interval must be positive")
    if args.url and args.mode != "socket":
        parser.error("--url requires --mode socket")
    result = asyncio.run(main_async(args))
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
