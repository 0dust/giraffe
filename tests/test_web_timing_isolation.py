"""Socket-free CI acceptance: web polling must not delay the traffic loop."""

import asyncio
import gc
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from giraffe import performance, runner, telemetry, web
from giraffe.models import RequestRecord, RunConfig
from giraffe.reporting import write_report
from tests.test_web import config, finished, report, start


pytestmark = [pytest.mark.usefixtures("local_tokenizer"), pytest.mark.timing_isolation]


@pytest_asyncio.fixture(autouse=True)
async def production_timing(monkeypatch):
    loop = asyncio.get_running_loop()
    previous_debug = loop.get_debug()
    frozen = bool(gc.get_freeze_count())
    loop.set_debug(False)
    monkeypatch.delenv("PYTHONASYNCIODEBUG", raising=False)
    try:
        yield
    finally:
        loop.set_debug(previous_debug)
        if not frozen:
            gc.unfreeze()


class FixedDelayClient:
    """Complete valid fixture answers on the real runner's event loop."""

    threads = []

    def __init__(self, target, configuration, **kwargs):
        self.target = target

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, spec, *, first_output_event=None):
        self.threads.append(threading.get_ident())
        started = time.monotonic()
        await asyncio.sleep(.04)
        first_output_ms = (time.monotonic() - started) * 1000
        if first_output_event is not None:
            first_output_event.set()
        await asyncio.sleep(.04)
        elapsed_ms = (time.monotonic() - started) * 1000
        answer = str(spec.expected or "answer")
        return RequestRecord(
            id=uuid4().hex, target=self.target.name, fixture_id=spec.fixture_id,
            scenario=spec.scenario, check_ids=spec.check_ids,
            started_at=datetime.now(timezone.utc).isoformat(), status="completed",
            http_status=200, valid=True, output=answer, output_chars=len(answer),
            input_chars=spec.input_chars, input_tokens=max(1, round(spec.input_chars / 4)),
            output_tokens=2, completion_tokens=2, requested_max_tokens=spec.max_tokens,
            stream=spec.stream, stream_terminated=True, finish_reason="stop",
            elapsed_ms=elapsed_ms, first_output_ms=first_output_ms,
            last_output_ms=elapsed_ms, stream_end_ms=elapsed_ms,
            answer_chunks=2, answer_reads=2, chunks=2,
        )


@pytest.mark.parametrize("large_report", [False, True, "uncached", "band"], ids=["slow-compute", "4096-record-report", "uncached-4096-record-report", "uncached-1-to-4-mib-report"])
async def test_two_polling_clients_do_not_delay_real_run_loop(tmp_path, monkeypatch, request, large_report):
    monkeypatch.setattr(runner, "LLMClient", FixedDelayClient)
    monkeypatch.setattr(FixedDelayClient, "threads", [])

    async def no_exporter(*args, **kwargs):
        return {"status": "unavailable", "reason": "In-process endpoint has no exporter",
                "samples": [], "elapsed_seconds": 0, "unavailable": []}

    monkeypatch.setattr(telemetry, "scrape", no_exporter)
    original_performance = performance.build_performance
    performance_threads = []

    def measured_performance(*args, **kwargs):
        # Model a slow report/performance read deterministically. On a shared
        # traffic loop this blocks requests and the 50 ms generator-lag monitor.
        performance_threads.append(threading.get_ident())
        if not large_report:
            time.sleep(.04)
        return original_performance(*args, **kwargs)

    monkeypatch.setattr(performance, "build_performance", measured_performance)
    real_run_suite = runner.run_suite
    reports = []
    completed = threading.Event()

    async def capture_report(*args, **kwargs):
        result = await real_run_suite(*args, **kwargs)
        reports.append(result)
        completed.set()
        return result

    monkeypatch.setattr(web, "run_suite", capture_report)
    configuration = config(checks=["serving"], samples=60 if large_report in {"uncached", "band"} else 12, concurrency=1,
                           max_requests=62 if large_report in {"uncached", "band"} else 14, max_duration_seconds=10,
                           request_timeout_seconds=2)
    if large_report:
        # Exercise actual parsing, model/compact serialization and performance
        # construction under the GIL, without replacing CPU work with sleep.
        saved = report(RunConfig.model_validate(configuration))
        saved.run_id = "cpu-poll"
        template = saved.requests[0]
        saved.requests = [template.model_copy(update={
            "id": f"cpu-{index}", "elapsed_ms": 80.0 + index % 10,
            "first_output_ms": 40.0 + index % 5, "input_tokens": 128,
            "output_tokens": 32, "completion_tokens": 32,
            "output": "answer " * 128, "output_chars": 896,
        }) for index in range(1024 if large_report == "band" else 4096)]
        path, _ = write_report(saved, tmp_path / saved.run_id)
        if large_report == "band":
            assert 1024**2 < path.stat().st_size < 4 * 1024**2
        performance_threads.clear()  # Fixture construction is outside the web app.
    if large_report in {"uncached", "band"}:
        monkeypatch.setattr(web, "_REPORT_CACHE_BYTES", 0)
        web._report_cache.clear()
    app = web.create_app(tmp_path)
    counts = [[0, 0], [0, 0]]
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with (httpx.AsyncClient(transport=transport, base_url="http://localhost") as control,
                    httpx.AsyncClient(transport=transport, base_url="http://localhost") as first,
                    httpx.AsyncClient(transport=transport, base_url="http://localhost") as second):
            if large_report:
                # Warm the large report cache and both API serialization paths
                # before measuring either run; cold imports/cache population
                # must not benefit just the polled run.
                detail = await first.get("/api/runs/cpu-poll")
                assert detail.status_code == 200, detail.text
                assert len(detail.json()["report"]["requests"]) == (1024 if large_report == "band" else 4096)
                assert len(detail.content) > (250_000 if large_report == "band" else 1_000_000)
                performance_response = await second.get("/api/runs/cpu-poll/performance")
                assert performance_response.status_code == 200, performance_response.text
            # Run the same traffic once before collecting the control sample.
            gc.collect()
            gc.freeze()
            identifier = await start(control, configuration)
            assert await asyncio.to_thread(completed.wait, 10), "Warm-up run stalled"
            assert (await finished(control, identifier))["state"] == "completed"
            completed.clear()
            # The control makes no HTTP requests while request timing is active.
            gc.collect()
            gc.freeze()
            identifier = await start(control, configuration)
            assert await asyncio.to_thread(completed.wait, 10), "Unpolled run stalled"
            assert (await finished(control, identifier))["state"] == "completed"
            completed.clear()
            gc.collect()
            gc.freeze()
            identifier = await start(control, configuration)
            path = f"/api/runs/{'cpu-poll' if large_report else identifier}"

            async def hammer(client, count):
                while not completed.is_set():
                    for index, suffix in enumerate(("", "/performance")):
                        response = await client.get(path + suffix)
                        assert response.status_code == 200, response.text
                        count[index] += 1
                    # Each tab polls both resources 50 times per second, well
                    # above the UI rate, without an unbounded CPU spin.
                    await asyncio.sleep(.02)

            polling = [asyncio.create_task(hammer(client, count))
                       for client, count in zip((first, second), counts)]
            try:
                assert await asyncio.to_thread(completed.wait, 10), "Polled run stalled"
                await asyncio.wait_for(asyncio.gather(*polling), 5)
                assert (await finished(control, identifier))["state"] == "completed"
            finally:
                for task in polling:
                    task.cancel()
                await asyncio.gather(*polling, return_exceptions=True)

    assert len(reports) == 3
    assert all("gc_pause_max_ms" in result.observations["client_timing_health"] for result in reports)
    for result in reports:
        assert len(result.requests) == configuration["samples"] + 1
        assert all(record.status == "completed" and record.valid for record in result.requests)
        assert result.observations["client_timing_health"]["sample_count"] >= 10
    assert all(min(count) >= 5 for count in counts), counts
    run_threads = set(FixedDelayClient.threads)
    assert threading.get_ident() not in run_threads
    assert performance_threads and threading.get_ident() not in performance_threads
    baseline_lag, polled_lag = [result.observations["generator_max_lag_ms"] for result in reports[1:]]
    request.node.user_properties.extend([
        ("baseline_generator_max_lag_ms", baseline_lag),
        ("polled_generator_max_lag_ms", polled_lag), ("poll_counts", counts),
    ])
    assert polled_lag <= 25, (baseline_lag, polled_lag)
    assert polled_lag <= baseline_lag + 10, (baseline_lag, polled_lag)
