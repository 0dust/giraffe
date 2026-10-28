"""Run-scoped RAM growth and sampler teardown without endpoint sockets."""

import asyncio

import pytest

from giraffe import resources, runner
from giraffe.models import Target
from tests.test_runner import FakeClient, config


@pytest.fixture
def fake_client(monkeypatch):
    class Client(FakeClient):
        active, peak, closed, scenarios = 0, 0, [], []
        bad_target = None
    monkeypatch.setattr(runner, "LLMClient", Client)
    return Client


def test_sampler_reads_callback_once_under_lock(monkeypatch):
    received = []
    class Sampler(resources.PhaseSampler):
        reads = 0
        @property
        def on_sample(self):
            self.reads += 1
            return received.append if self.reads == 1 else None
        @on_sample.setter
        def on_sample(self, value):
            pass
    monkeypatch.setattr(resources, "current_rss_bytes", lambda: 17)
    sampler = Sampler("traffic")
    sampler.sample()
    assert received == [17]
    assert sampler.reads == 1


@pytest.mark.parametrize("reason", ["cancelled by user", "maximum run duration reached",
                                     "error threshold reached", "Endpoint service unresponsive"])
def test_memory_guardrail_preserves_first_real_stop_reason(reason):
    events = []
    run = runner._Run(config(max_memory_mb=1), events.append, None)
    run.rss_at_start_bytes = 100 * 1024**2
    run.reason = reason
    run.memory_sample(102 * 1024**2)
    assert run.memory_limit_reached
    assert run.reason == reason
    assert events[-1]["abort_reason"] == reason


@pytest.mark.parametrize("reason", [None, "maximum request budget reached"])
def test_memory_guardrail_reason_shows_binary_growth_above_limit(reason):
    run = runner._Run(config(max_memory_mb=4096), None, None)
    run.rss_at_start_bytes = 20 * 1024**3
    run.reason = reason
    run.memory_sample(run.rss_at_start_bytes + 4096 * 1024**2 + 1)
    assert run.reason == "memory limit reached: RAM grew by 4,097 MiB, over the 4,096 MiB limit"


async def test_preexisting_process_ram_does_not_count_against_growth_limit(monkeypatch, fake_client):
    monkeypatch.setattr(resources, "current_rss_bytes", lambda: 8 * 1024**3)
    report = await runner.run_suite(config(max_memory_mb=1))
    data = report.observations["resources"]
    assert report.overall == "pass"
    assert data["rss_at_start_bytes"] == 8 * 1024**3
    assert data["sampled_peak_growth_bytes"] == {"traffic": 0, "scoring": 0}
    assert data["guardrail_active"] and not data["memory_limit_reached"]


async def test_missing_initial_rss_never_invents_later_baseline(monkeypatch, fake_client):
    values = iter([None, None])
    monkeypatch.setattr(resources, "current_rss_bytes", lambda: next(values, 8 * 1024**3))
    report = await runner.run_suite(config(max_memory_mb=1))
    data = report.observations["resources"]
    assert report.overall == "pass"
    assert data["rss_at_start_bytes"] is None
    assert data["sampled_peak_growth_bytes"] == {"traffic": None, "scoring": None}
    assert not data["guardrail_active"] and not data["memory_limit_reached"]


async def test_failure_detaches_sampler_and_queued_callbacks_are_noops(monkeypatch, fake_client):
    monkeypatch.setattr(resources, "current_rss_bytes", lambda: 1)
    original = runner.PhaseSampler
    samplers, runs, events = [], [], []
    def sampler(phase):
        value = original(phase, interval=100)
        samplers.append(value)
        return value
    def fixtures(config):
        runs.append(samplers[0].run)
        monkeypatch.setattr(resources, "current_rss_bytes", lambda: 2 * 1024**2)
        samplers[0].sample()
        raise RuntimeError("fixture failed")
    monkeypatch.setattr(runner, "PhaseSampler", sampler)
    monkeypatch.setattr(runner, "builtin_fixtures", fixtures)
    with pytest.raises(RuntimeError, match="fixture failed"):
        await runner.run_suite(config(max_memory_mb=1), progress=events.append)
    before = list(events)
    await asyncio.sleep(0)
    assert events == before
    assert runs[0].memory_sampling_done
    assert not runs[0].memory_limit_reached
    assert samplers[0].on_sample is None
    assert not samplers[0]._thread.is_alive()
    runs[0].memory_sample(100 * 1024**3)
    assert not runs[0].memory_limit_reached
    assert events == before


async def test_cancellation_always_detaches_sampler(monkeypatch, fake_client):
    monkeypatch.setattr(resources, "current_rss_bytes", lambda: 1)
    entered = asyncio.Event()
    original = runner.PhaseSampler
    samplers = []
    def sampler(phase):
        value = original(phase, interval=100)
        samplers.append(value)
        return value
    async def target(run, target, fixtures):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(runner, "PhaseSampler", sampler)
    monkeypatch.setattr(runner, "_target", target)
    task = asyncio.create_task(runner.run_suite(config()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await task
    assert cancelled.value.giraffe_report.abort_reason == "run cancelled"
    assert samplers[0].run.memory_sampling_done
    assert samplers[0].on_sample is None
    assert not samplers[0]._thread.is_alive()


async def test_finished_check_keeps_pass_when_later_check_is_interrupted(monkeypatch, fake_client):
    monkeypatch.setattr(resources, "current_rss_bytes", lambda: 1)
    original = runner._Run.group
    async def group(run, target, client, specs, scenario, **kwargs):
        if scenario.startswith("correctness"):
            run.memory_sample(2 * 1024**2)
        return await original(run, target, client, specs, scenario, **kwargs)
    monkeypatch.setattr(runner._Run, "group", group)
    report = await runner.run_suite(config(checks=["serving", "correctness"], max_memory_mb=1))
    checks = {check.id: check for check in report.checks}
    assert checks["serving"].status == "pass"
    assert checks["correctness"].status == "inconclusive"
    assert report.overall == "inconclusive"
    assert report.observations["resources"]["memory_interrupted_checks"] == [
        {"target": "service", "id": "correctness"}]


async def test_finished_target_keeps_pass_when_later_target_is_interrupted(monkeypatch, fake_client):
    monkeypatch.setattr(resources, "current_rss_bytes", lambda: 1)
    original = runner._target
    async def target(run, target, fixtures):
        if target.name == "second":
            run.memory_sample(2 * 1024**2)
        return await original(run, target, fixtures)
    monkeypatch.setattr(runner, "_target", target)
    report = await runner.run_suite(config(targets=[
        Target(name="service", model="local", url="http://localhost:1234"),
        Target(name="second", model="local", url="http://localhost:1235")],
        overlap_models=False, max_memory_mb=1))
    checks = {(check.target, check.id): check for check in report.checks}
    assert checks["service", "serving"].status == "pass"
    assert checks["second", "serving"].status == "inconclusive"
    assert report.observations["resources"]["memory_interrupted_checks"] == [
        {"target": "second", "id": "serving"}]


async def test_ram_growth_in_run_setup_counts_from_first_sample(monkeypatch, fake_client):
    monkeypatch.setattr(resources, "current_rss_bytes", lambda: 100 * 1024**2)
    original = runner._Run.__init__
    def initialize(run, *args):
        original(run, *args)
        monkeypatch.setattr(resources, "current_rss_bytes", lambda: 102 * 1024**2)
    monkeypatch.setattr(runner._Run, "__init__", initialize)
    report = await runner.run_suite(config(max_memory_mb=1))
    data = report.observations["resources"]
    assert data["rss_at_start_bytes"] == 100 * 1024**2
    assert data["sampled_peak_growth_bytes"]["traffic"] == 2 * 1024**2
    assert data["memory_limit_stopped_run"]
    assert report.overall == "inconclusive"
    assert not report.requests


async def test_optional_check_interruption_does_not_downgrade_finished_required_check(monkeypatch, fake_client):
    monkeypatch.setattr(resources, "current_rss_bytes", lambda: 1)
    async def metrics(*args):
        pass
    async def finish_gpu(run, target):
        run.memory_sample(2 * 1024**2)
    monkeypatch.setattr(runner, "_metrics", metrics)
    monkeypatch.setattr(runner, "_finish_gpu_observation", finish_gpu)
    report = await runner.run_suite(config(checks=["serving", "gpu"], max_memory_mb=1))
    assert report.overall == "pass"
    assert report.abort_reason is None
    assert next(check for check in report.checks if check.id == "serving").status == "pass"
    assert report.observations["resources"]["memory_interrupted_checks"] == [
        {"target": "service", "id": "gpu"}]


async def test_cancelled_later_request_does_not_mark_finished_check_unfinished(monkeypatch, fake_client):
    monkeypatch.setattr(resources, "current_rss_bytes", lambda: 1)
    original_execute, original_sampler = fake_client.execute, runner.PhaseSampler
    async def execute(client, spec, **kwargs):
        if spec.scenario.startswith("correctness"):
            monkeypatch.setattr(resources, "current_rss_bytes", lambda: 2 * 1024**2)
            monkeypatch.setattr(fake_client, "delay", 2)
        return await original_execute(client, spec, **kwargs)
    monkeypatch.setattr(fake_client, "execute", execute)
    monkeypatch.setattr(runner, "PhaseSampler", lambda phase: original_sampler(phase, interval=.01))
    report = await runner.run_suite(config(checks=["serving", "correctness"], max_memory_mb=1))
    checks = {check.id: check for check in report.checks}
    assert checks["serving"].status == "pass"
    assert checks["correctness"].status == "inconclusive"
    assert report.overall == "inconclusive"
    assert report.observations["resources"]["memory_interrupted_checks"] == [
        {"target": "service", "id": "correctness"}]


@pytest.mark.parametrize("initial_reason", [None, "maximum request budget reached"])
@pytest.mark.parametrize("pending_stop, expected", [
    ("cancel", "cancelled by user"), ("deadline", "maximum run duration reached")])
def test_memory_sample_resolves_pending_real_stop_before_assigning_reason(initial_reason, pending_stop, expected):
    events = []
    run = runner._Run(config(max_memory_mb=1), events.append, None)
    run.rss_at_start_bytes = 1
    run.reason = initial_reason
    if pending_stop == "cancel":
        run.stop.set()
    else:
        run.deadline = 0
    run.memory_sample(2 * 1024**2)
    assert run.memory_limit_reached
    assert run.reason == expected
    assert run.memory_stop_reason is None
    assert [event["abort_reason"] for event in events if event["event"] == "run_stopping"] == [expected]


def test_late_memory_warning_does_not_resolve_new_stop_conditions_after_completed_work():
    events = []
    run = runner._Run(config(max_memory_mb=1), events.append, None)
    run.rss_at_start_bytes = 1
    run.finish_checks("service", {"serving"})
    run.stop.set()
    run.deadline = 0
    run.memory_sample(2 * 1024**2)
    assert run.memory_limit_reached
    assert run.reason is None
    assert not run.memory_interrupted_checks
    assert not events
