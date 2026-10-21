"""Optional suite bounds preserve each check's own parallelism and stop rules."""

import asyncio
import math

import httpx
import pytest

from giraffe import concurrent_load, runner, workloads
from giraffe.client import LLMClient
from giraffe.conditions import condition_for
from giraffe.fixtures import builtin_fixtures
from giraffe.models import RunConfig, required_concurrency
from tests.test_concurrent_load import Client, configuration, perform, point, short_windows
from tests.test_runner import FakeClient, pack_config


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
    configured = RunConfig(targets=[TARGET], checks=["arrivals", "capacity"], concurrency=40,
                           default_concurrency=3, arrivals={"max_in_flight": 2})
    assert condition_for("arrival", {}, configured)["parameters"]["concurrency"] == 2
    assert condition_for("traffic_stage_0", {}, configured)["parameters"]["concurrency"] == 3


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
    configured = RunConfig(targets=[TARGET, {**TARGET, "name": "two"}],
                           checks=["correctness", "arrivals"], overlap_models=True,
                           concurrency=ceiling, default_concurrency=1,
                           test_options={"correctness": {"concurrency": 3}},
                           arrivals={"requests": 4, "requests_per_second": 1000, "max_in_flight": 2})
    assert required_concurrency(configured)["peak"] == 6
    assert runner._Run(configured, None, None).slots.maxsize == expected
    report = await runner.run_suite(configured)
    assert 2 <= client.peak <= expected
    assert client.connections == [3, 3]
    assert report.abort_reason is None


async def test_arrivals_own_inflight_bound_is_smaller_than_shared_pool(client):
    configured = RunConfig(targets=[TARGET], checks=["correctness", "arrivals"], default_concurrency=4,
                           arrivals={"requests": 8, "requests_per_second": 1000, "max_in_flight": 2})
    run = runner._Run(configured, None, None)
    assert run.slots.maxsize == 4
    target = configured.targets[0]
    await workloads.arrivals(run, target, client(target, configured, max_connections=4), builtin_fixtures(configured))
    assert len(run.records) == 8
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
    configured = short_windows(configuration(max_requests=None, max_duration_seconds=None))
    run, check, _, _ = await perform(configured, Client())
    result = point(check)
    assert result["request_allowance"] is None
    assert result["sent"] >= 2
    assert result["window_reason"] != "point request allowance used"
    assert math.isinf(run.deadline)


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
