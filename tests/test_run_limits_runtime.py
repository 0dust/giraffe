"""Optional suite bounds preserve each check's own parallelism and stop rules."""

import asyncio
import math
from types import SimpleNamespace

import httpx
import pytest

from giraffe import concurrent_load, runner
from giraffe.client import LLMClient
from giraffe.conditions import condition_for
from giraffe.models import RunConfig, required_concurrency
from tests.test_runner import FakeClient, pack_config


pytestmark = pytest.mark.usefixtures("local_tokenizer")


TARGET = {"name": "one", "url": "http://local", "model": "m"}


def config(**values):
    return RunConfig(targets=[TARGET], checks=["correctness"], **values)


@pytest.fixture
def client(monkeypatch):
    class Client(FakeClient):
        active, peak, closed, scenarios = 0, 0, [], []
        connections = []

        def __init__(self, target, config, *, max_connections):
            super().__init__(target, config)
            self.connections.append(max_connections)

    monkeypatch.setattr(runner, "LLMClient", Client)
    return Client


def test_unlimited_run_has_finite_pool_no_quotas_and_infinite_deadline():
    run = runner._Run(config(default_concurrency=7), None, None)
    assert math.isinf(run.deadline)
    assert run.slots.maxsize == 7
    assert run.max_connections == 7
    assert run.quotas == {}
    run.budget_used = 100_000
    assert run.remaining("one") is None
    assert run.allowed("one")
    assert runner._budget_guidance(run.config) == ""


def test_remaining_respects_global_and_endpoint_quota():
    run = runner._Run(config(max_requests=5), None, None)
    assert run.remaining("one") == 5
    for _ in range(5):
        assert run.reserve_offer("one")
    assert run.remaining("one") == 0
    assert not run.allowed("one")
    assert run.limits_reached == {"max_requests"}


def test_check_config_copies_effective_parallelism_never_ceiling():
    original = config(default_concurrency=2, concurrency=40)
    assert runner._check_config(original, "correctness").concurrency == 2
    assert original.concurrency == 40


def test_condition_parameters_use_effective_inflight_values():
    from tests.test_planning import load_config
    from giraffe.load_test import test_points
    configured = load_config(concurrent=True, concurrency=40, default_concurrency=3)
    configured.load_test.patterns.concurrent.levels = [2]
    point = next(p for p in test_points(configured.load_test) if p["pattern"] == "concurrent")
    condition = condition_for(point["id"], {}, configured, request_parameters={"concurrency": point["level"]})
    assert condition["check"] == "load"
    assert condition["parameters"]["concurrency"] == 2


def test_http_client_uses_same_explicit_connection_and_keepalive_limits(monkeypatch):
    captured = {}
    real = httpx.AsyncClient

    def capture(**values):
        captured.update(values)
        return real(**values)

    monkeypatch.setattr(httpx, "AsyncClient", capture)
    configured = config(concurrency=40)
    transport = LLMClient(configured.targets[0], configured, max_connections=3)._create_client()
    assert captured["limits"].max_connections == 3
    assert captured["limits"].max_keepalive_connections == 3
    asyncio.run(transport.aclose())


async def test_all_limits_unset_completes_finite_checks(client):
    report = await runner.run_suite(config(default_concurrency=2, samples=2))
    assert report.requests
    assert report.abort_reason is None
    assert report.manifest["config"]["max_requests"] is None
    assert report.manifest["config"]["max_duration_seconds"] is None
    assert report.manifest["config"]["concurrency"] is None
    assert report.limits_reached == []
    assert client.connections == [2]


@pytest.mark.parametrize("ceiling,expected", [(None, 6), (4, 4)])
async def test_mixed_overlap_peak_stays_inside_required_slot_pool(client, ceiling, expected):
    from tests.test_planning import load_config
    # One maintained group and finite correctness share one global slot pool.
    configured = load_config(concurrent=True, concurrency=ceiling, default_concurrency=1,
        targets=[TARGET, {**TARGET, "name": "two"}], overlap_models=True,
        test_options={"correctness": {"concurrency": 3}})
    configured.request_source = "generated"
    configured.request_pack = None
    configured.load_test.patterns.constant.enabled = False
    configured.load_test.schedule.duration_seconds = .04
    configured.checks = ["correctness", "load"]
    assert required_concurrency(configured)["peak"] == 6
    assert runner._Run(configured, None, None).slots.maxsize == expected
    report = await runner.run_suite(configured)
    assert 2 <= client.peak <= expected
    assert client.connections == [3, 3]
    assert report.abort_reason is None


async def test_arrivals_own_inflight_bound_is_smaller_than_shared_pool(client):
    from giraffe.load_test import admission_limit, execute
    from tests.test_planning import load_config
    configured = load_config(duration=1, rates=[1000], concurrency=2, default_concurrency=4)
    configured.load_test.schedule.duration_seconds = .02
    run = runner._Run(configured, None, None)
    assert run.slots.maxsize == admission_limit(configured, configured.load_test) == 2
    await execute(run, configured.targets[0], client(configured.targets[0], configured, max_connections=2))
    point = run.observations[configured.targets[0].name]["load_test"]["points"][0]
    assert point["local_drops"] > 0
    assert client.peak == 2


async def test_pack_loaded_effective_one_with_unset_ceiling_passes(client):
    report = await runner.run_suite(pack_config(checks=["correctness"], concurrency=None,
                                                default_concurrency=1, max_requests=None,
                                                max_duration_seconds=None))
    check = next(row for row in report.checks if row.id == "correctness")
    assert check.status == "pass"
    assert all(case["loaded"]["configured_concurrency"] == 1
               for case in check.metrics["request_pack"]["cases"])
    assert all(record.overlap == 1 for record in report.requests)


async def test_recovery_unlimited_allowance_prime_and_effective_windows(client):
    configured = RunConfig(targets=[TARGET], checks=["recovery"], concurrency=40,
                           default_concurrency=2, samples=2, limits={"min_samples": 2},
                           sustained_seconds=1)
    configured.sustained_seconds = .025
    report = await runner.run_suite(configured)
    check = next(row for row in report.checks if row.id == "recovery")
    assert report.abort_reason is None
    assert any(record.scenario == "recovery_prime" for record in report.requests)
    assert check.metrics["sustained_budget"]["allowance"] is None
    windows = check.metrics["rolling_windows"]
    assert windows and all(window["attempted"] == 2 for window in windows[:-1])
    assert sum(window["attempted"] for window in windows) == check.metrics["sustained"]["attempted"]
    assert "max_requests" not in check.summary


async def test_concurrent_load_unlimited_allowance_ends_at_own_window():
    from giraffe.load_test import execute
    from tests.test_load_test_phase_a import Client, config as load_configuration
    configured = load_configuration()
    configured.load_test.schedule.duration_seconds = .04
    run = runner._Run(configured, None, None)
    result = (await execute(run, configured.targets[0], Client()))["points"][0]
    assert result["dispatched"] >= 2
    assert not result["cut_short"]
    assert math.isinf(run.deadline)
    assert run.remaining("local") is None


def test_load_budget_remedies_are_silent_when_limits_unset():
    metrics = {"load_types": {"combinations": {"points": [
        {"reason_code": "budget_limited", "status": "inconclusive"},
        {"reason_code": "time_limited", "status": "inconclusive"},
    ]}}}
    assert concurrent_load.remedies(metrics, config()) == ""


def test_overlap_clients_can_fill_pool_without_httpx_wait_and_keep_one_for_small_pools():
    targets = [TARGET, {**TARGET, "name": "two"}]
    configured = RunConfig(targets=targets, checks=["correctness"], overlap_models=True,
                           concurrency=5, test_options={"correctness": {"concurrency": 4}})
    run = runner._Run(configured, None, None)
    assert run.slots.maxsize == 5
    assert run.connection_limits == {"one": 4, "two": 4}
    assert all(limit <= run.slots.maxsize for limit in run.connection_limits.values())
    configured.concurrency = 1
    run = runner._Run(configured, None, None)
    assert run.slots.maxsize == 1 and run.connection_limits == {"one": 1, "two": 1}


async def test_judging_connections_use_judging_parallelism(monkeypatch):
    from giraffe import judging
    from tests.test_judging import configuration, fixture, record

    settings = configuration(concurrency=7)
    seen = []

    class JudgeClient:
        def __init__(self, target, config, *, max_connections):
            seen.append(max_connections)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def execute(self, spec):
            return record(output='{"verdict": true, "reason": "matches"}', finish_reason="stop")

    monkeypatch.setattr(judging, "LLMClient", JudgeClient)
    summary = await judging.judge_records(settings, [record()], [fixture()])
    assert seen == [7]
    assert summary["passed_requests"] == 1


def test_workload_pool_saturation_does_not_claim_looser_ceiling_was_reached():
    run = runner._Run(config(concurrency=40, default_concurrency=2), None, None)
    assert run.try_acquire_slot() and run.try_acquire_slot()
    assert not run.try_acquire_slot()
    assert "concurrency" not in run.limits_reached


async def test_preparation_deadline_is_saved_when_target_finishes_before_stop_monitor(client, monkeypatch):
    from giraffe import local_tokenizer, telemetry
    from tests.test_load_test_phase_a import config as load_config

    clock = SimpleNamespace(now=0.)
    local_time = SimpleNamespace(monotonic=lambda: clock.now)
    monkeypatch.setattr(local_tokenizer, "time", local_time)
    monkeypatch.setattr(runner, "time", local_time)

    async def ready(*args, **kwargs):
        return {"ready": True}

    class Worker:
        returncode = None

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    worker = Worker()

    async def start(*args, **kwargs):
        return worker

    async def blocked(_):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            clock.now = .01
            raise

    async def no_exporter(*args, **kwargs):
        return {"status": "unavailable", "reason": "Fake endpoint has no exporter", "samples": [],
                "elapsed_seconds": 0, "unavailable": []}

    async def after_target_completion(self, tasks):
        # Force the reported ordering: every target finishes before the first
        # stop observation. Preparation itself must preserve its deadline cause.
        await asyncio.gather(*tasks, return_exceptions=True)

    monkeypatch.setattr(local_tokenizer, "_verify_tokenizer", ready)
    monkeypatch.setattr(local_tokenizer, "_start_worker", start)
    monkeypatch.setattr(local_tokenizer, "_read_worker", blocked)
    monkeypatch.setattr(telemetry, "scrape", no_exporter)
    monkeypatch.setattr(runner._Run, "monitor", after_target_completion)
    configured = load_config()
    configured.max_duration_seconds = .01
    events = []
    report = await runner.run_suite(configured, progress=events.append)
    assert report.abort_reason == "maximum run duration reached"
    assert report.limits_reached == ["max_duration_seconds"]
    assert not report.requests and not client.scenarios
    assert worker.returncode == -9
    target = report.observations["targets"]["local"]
    preparation = next(value for value in target["preparations"].values()
                       if value["scenario"].startswith("load_preparation"))
    assert preparation["status"] == "unavailable"
    assert "maximum run duration reached" in preparation["reason"]
    assert "exceeded 10s" not in preparation["reason"]
    assert any(event["event"] == "run_stopping" and event["abort_reason"] == report.abort_reason
               for event in events)
    check = next(check for check in report.checks if check.id == "load")
    assert check.status == "inconclusive"
    assert "maximum run duration reached" in check.summary
    saved = report.model_dump(mode="json")
    assert saved["abort_reason"] == report.abort_reason
    assert saved["limits_reached"] == report.limits_reached


async def test_completed_run_is_not_aborted_by_client_cleanup_crossing_deadline(client, monkeypatch):
    from giraffe import telemetry

    clock = SimpleNamespace(now=0.)
    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=lambda: clock.now))

    async def close(self, *_):
        clock.now = 2.

    async def no_exporter(*args, **kwargs):
        return {"status": "unavailable", "reason": "Fake endpoint has no exporter", "samples": [],
                "elapsed_seconds": 0, "unavailable": []}

    async def after_target_completion(self, tasks):
        await asyncio.gather(*tasks, return_exceptions=True)

    monkeypatch.setattr(client, "__aexit__", close)
    monkeypatch.setattr(telemetry, "scrape", no_exporter)
    monkeypatch.setattr(runner._Run, "monitor", after_target_completion)
    report = await runner.run_suite(pack_config(checks=["correctness"], max_duration_seconds=1,
                                                max_memory_mb=0, default_concurrency=1, max_requests=None))
    assert clock.now > 1
    assert report.requests and all(record.status == "completed" for record in report.requests)
    assert report.abort_reason is None and report.limits_reached == []
    assert next(check for check in report.checks if check.id == "correctness").status == "pass"
