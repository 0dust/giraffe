"""A finished sampler cannot revive a web job; history polls keep compact summaries."""

import asyncio

from fastapi.testclient import TestClient

from giraffe.models import RunConfig
from giraffe.reporting import write_report
from giraffe.web import create_app
from tests.test_storage_web import completed
from tests.test_web import config, report


def test_progress_queued_after_failure_does_not_resurrect_active_run(tmp_path, monkeypatch, local_tokenizer):
    attempts = []

    async def suite(configuration, *, progress, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            asyncio.get_running_loop().call_soon(progress, {"event": "run_stopping", "abort_reason": "memory limit reached"})
            raise RuntimeError("Failed aggregation")
        return report(configuration)

    monkeypatch.setattr("giraffe.web.run_suite", suite)
    with TestClient(create_app(tmp_path, min_free_disk=0), base_url="http://127.0.0.1:8787") as client:
        first = client.post("/api/runs", json={"config": config()})
        assert first.status_code == 202
        assert completed(client, first.json()["id"])["state"] == "error"
        second = client.post("/api/runs", json={"config": config()})
        assert second.status_code == 202
        assert completed(client, second.json()["id"])["state"] == "completed"


def test_real_sampler_exception_after_high_rss_allows_another_web_run(tmp_path, monkeypatch, local_tokenizer):
    from giraffe import resources, runner

    samplers = []
    original_sampler = runner.PhaseSampler

    def sampler(phase):
        value = original_sampler(phase, interval=100)
        samplers.append(value)
        return value

    def failed_fixtures(configuration):
        monkeypatch.setattr(resources, "current_rss_bytes", lambda: 103 * 1024**2)
        samplers[-1].sample()
        raise RuntimeError("Fixture preparation failed")

    monkeypatch.setattr(resources, "current_rss_bytes", lambda: 100 * 1024**2)
    monkeypatch.setattr(runner, "PhaseSampler", sampler)
    monkeypatch.setattr(runner, "builtin_fixtures", failed_fixtures)
    with TestClient(create_app(tmp_path, min_free_disk=0), base_url="http://127.0.0.1:8787") as client:
        first = client.post("/api/runs", json={"config": config(max_memory_mb=1)})
        assert first.status_code == 202
        assert completed(client, first.json()["id"])["state"] == "error"
        assert samplers[-1].on_sample is None
        assert not samplers[-1]._thread.is_alive()

        async def successful(configuration, **kwargs):
            return report(configuration)

        monkeypatch.setattr("giraffe.web.run_suite", successful)
        second = client.post("/api/runs", json={"config": config(max_memory_mb=1)})
        assert second.status_code == 202
        assert completed(client, second.json()["id"])["state"] == "completed"


def test_runs_listing_caches_compact_report_summaries_and_refreshes_changed_reports(tmp_path, monkeypatch):
    directory = tmp_path / "saved"
    value = report(RunConfig.model_validate(config()))
    write_report(value, directory)
    from giraffe.report_stream import _Reader
    original = _Reader.__init__
    loads = []

    def load(reader, stream):
        loads.append(stream.name)
        return original(reader, stream)

    monkeypatch.setattr(_Reader, "__init__", load)
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8787") as client:
        for _ in range(3):
            rows = client.get("/api/runs").json()["runs"]
            assert rows[0]["request_count"] == 1
            assert rows[0]["check_counts"]["pass"] == 1
            assert rows[0]["overall"] == "pass"
            assert rows[0]["targets"][0]["model"] == "test-model"
        assert len(loads) == 1
        value.checks[0].status = "fail"
        value.overall = "fail"
        value.requests = []
        write_report(value, directory)
        rows = client.get("/api/runs").json()["runs"]
        assert rows[0]["request_count"] == 0
        assert rows[0]["check_counts"]["fail"] == 1
        assert rows[0]["overall"] == "fail"
        assert len(loads) == 2
        detail = client.get("/api/runs/saved").json()
        assert detail["report"]["checks"][0]["summary"] == "Completed"


def test_unreadable_report_listing_is_cached_without_losing_error_state(tmp_path, monkeypatch):
    directory = tmp_path / "broken"
    directory.mkdir()
    (directory / "report.json").write_text('{"broken":')
    from giraffe.report_stream import _Reader
    original, loads = _Reader.__init__, []

    def load(reader, stream):
        loads.append(stream.name)
        return original(reader, stream)

    monkeypatch.setattr(_Reader, "__init__", load)
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8787") as client:
        for _ in range(2):
            rows = client.get("/api/runs").json()["runs"]
            assert rows[0]["state"] == "error"
        assert len(loads) == 1
