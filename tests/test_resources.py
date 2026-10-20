"""Process RAM sampling and partial-run guardrails without endpoint sockets."""

import asyncio
import ctypes
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from giraffe import resources, runner
from giraffe.planning import request_plan
from tests.test_runner import FakeClient, config, judged_pack_config, judge_summary


@pytest.mark.parametrize("helper", [resources.current_rss_bytes, resources.peak_rss_bytes])
def test_rss_helpers_are_optional_positive_byte_counts(helper):
    value = helper()
    assert value is None or isinstance(value, int) and value > 0


def test_linux_resident_pages_and_read_failure(monkeypatch):
    monkeypatch.setattr(resources.sys, "platform", "linux")
    monkeypatch.setattr(resources.Path, "read_text", lambda _: "999 13 4 1")
    monkeypatch.setattr(resources.os, "sysconf", lambda _: 4096)
    assert resources.current_rss_bytes() == 13 * 4096
    monkeypatch.setattr(resources.Path, "read_text", lambda _: "invalid")
    assert resources.current_rss_bytes() is None


def test_macos_task_info_resident_bytes_and_failure(monkeypatch):
    def task_info(task, flavor, pointer, count):
        assert task == 17 and flavor == 20
        pointer._obj.resident_size = 123456
        return 0
    library = SimpleNamespace(mach_task_self=lambda: 17, task_info=task_info)
    monkeypatch.setattr(resources.sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", lambda _: library)
    assert resources.current_rss_bytes() == 123456
    library.task_info = lambda *args: 1
    assert resources.current_rss_bytes() is None
    monkeypatch.setattr(ctypes, "CDLL", lambda _: (_ for _ in ()).throw(OSError("unavailable")))
    assert resources.current_rss_bytes() is None


@pytest.mark.parametrize("platform, multiplier", [("darwin", 1), ("linux", 1024)])
def test_process_lifetime_peak_units(monkeypatch, platform, multiplier):
    monkeypatch.setattr(resources.sys, "platform", platform)
    monkeypatch.setitem(sys.modules, "resource", SimpleNamespace(
        RUSAGE_SELF=0, getrusage=lambda _: SimpleNamespace(ru_maxrss=123)))
    assert resources.peak_rss_bytes() == 123 * multiplier
    monkeypatch.setitem(sys.modules, "resource", None)
    assert resources.peak_rss_bytes() is None


def test_unsupported_rss_is_unavailable(monkeypatch):
    monkeypatch.setattr(resources.sys, "platform", "unsupported")
    assert resources.current_rss_bytes() is None
    assert resources.peak_rss_bytes() is None


def test_phase_sampler_samples_boundaries_and_never_reports_missing_as_zero(monkeypatch):
    values = iter([None, 10, 20, 15, None])
    monkeypatch.setattr(resources, "current_rss_bytes", lambda: next(values))
    with resources.PhaseSampler("traffic", interval=100) as sampler:
        sampler.sample()
        sampler.set_phase("scoring")
    assert sampler.peaks == {"traffic": 20, "scoring": 15}
    assert not sampler._thread.is_alive()


@pytest.fixture
def fake_client(monkeypatch):
    class Client(FakeClient):
        active, peak, closed, scenarios = 0, 0, [], []
    monkeypatch.setattr(runner, "LLMClient", Client)
    return Client


async def test_guardrail_stops_before_traffic_with_partial_inconclusive_report(monkeypatch, fake_client):
    values = iter([1, 1])
    monkeypatch.setattr(resources, "current_rss_bytes", lambda: next(values, 2 * 1024**2))
    events = []
    report = await runner.run_suite(config(max_memory_mb=1), progress=events.append)
    assert report.overall == "inconclusive"
    assert report.abort_reason.startswith("memory limit reached: RAM grew by")
    assert not report.requests
    assert all(check.status != "pass" for check in report.checks)
    assert any(event["event"] == "run_stopping" for event in events)
    data = report.observations["resources"]
    assert data["memory_limit_reached"] and data["guardrail_active"]
    assert data["memory_limit_bytes"] == 1024**2
    assert data["sampled_peak_rss_bytes"] == {"traffic": 2 * 1024**2, "scoring": 2 * 1024**2}
    assert data["rss_at_start_bytes"] == 1
    assert data["sampled_peak_growth_bytes"]["traffic"] == 2 * 1024**2 - 1
    assert data["sampling"] == resources.SAMPLING


@pytest.mark.parametrize("rss, limit", [(2 * 1024**2, 0), (None, 1)])
async def test_disabled_or_unavailable_guardrail_does_not_stop(monkeypatch, fake_client, rss, limit):
    monkeypatch.setattr(resources, "current_rss_bytes", lambda: rss)
    monkeypatch.setattr(runner, "peak_rss_bytes", lambda: 100 * 1024**3)
    report = await runner.run_suite(config(max_memory_mb=limit))
    assert report.overall == "pass"
    assert report.abort_reason is None
    data = report.observations["resources"]
    assert not data["guardrail_active"] and not data["memory_limit_reached"]
    assert data["sampled_peak_rss_bytes"] == {"traffic": rss, "scoring": rss}
    assert data["process_peak_rss_bytes"] == 100 * 1024**3
    if rss is None:
        assert data["memory_measurement"] == "not measured on this platform"


async def test_memory_guardrail_interrupts_active_requests(monkeypatch, fake_client):
    monkeypatch.setattr(resources, "current_rss_bytes", lambda: 1)
    original = fake_client.execute

    async def execute(self, spec, **kwargs):
        monkeypatch.setattr(resources, "current_rss_bytes", lambda: 2 * 1024**2)
        return await original(self, spec, **kwargs)

    monkeypatch.setattr(fake_client, "execute", execute)
    monkeypatch.setattr(fake_client, "delay", 2)
    original_sampler = runner.PhaseSampler
    monkeypatch.setattr(runner, "PhaseSampler", lambda phase: original_sampler(phase, interval=.01))
    report = await runner.run_suite(config(max_memory_mb=1))
    assert report.overall == "inconclusive"
    assert report.observations["resources"]["memory_limit_reached"]
    assert len(report.requests) < 5
    assert all(check.status != "pass" for check in report.checks)
    assert fake_client.active == 0
    assert fake_client.closed == ["service"]


async def test_scoring_guardrail_preserves_reason_and_unfinished_checks(monkeypatch, fake_client):
    monkeypatch.setattr(resources, "current_rss_bytes", lambda: 1)
    original_sampler = runner.PhaseSampler
    monkeypatch.setattr(runner, "PhaseSampler", lambda phase: original_sampler(phase, interval=.01))

    async def judge_records(config, records, specs, *, stop_event, **kwargs):
        monkeypatch.setattr(resources, "current_rss_bytes", lambda: 2 * 1024**2)
        await asyncio.wait_for(stop_event.wait(), 1)
        return judge_summary(records, stopped="cancelled")

    monkeypatch.setattr("giraffe.judging.judge_records", judge_records)
    report = await runner.run_suite(judged_pack_config(max_memory_mb=1))
    assert report.overall == "inconclusive"
    assert report.abort_reason.startswith("memory limit reached")
    assert all(check.status != "pass" for check in report.checks)
    assert report.observations["resources"]["sampled_peak_rss_bytes"]["scoring"] == 2 * 1024**2


def test_memory_limit_validation_and_plan():
    assert config().max_memory_mb == 4096
    assert request_plan(config(max_memory_mb=12))["memory_limit_bytes"] == 12 * 1024**2
    assert request_plan(config(max_memory_mb=0))["memory_limit_bytes"] == 0
    with pytest.raises(ValidationError):
        config(max_memory_mb=-1)


async def test_transient_sample_during_sync_aggregation_is_warning_after_completed_work(monkeypatch, fake_client):
    monkeypatch.setattr(resources, "current_rss_bytes", lambda: 1)
    original_sampler, original_checks = runner.PhaseSampler, runner._checks
    samplers = []

    def sampler(phase):
        value = original_sampler(phase, interval=100)
        samplers.append(value)
        return value

    def checks(run, target):
        result = original_checks(run, target)
        monkeypatch.setattr(resources, "current_rss_bytes", lambda: 2 * 1024**2)
        samplers[0].sample()
        monkeypatch.setattr(resources, "current_rss_bytes", lambda: 1)
        return result

    monkeypatch.setattr(runner, "PhaseSampler", sampler)
    monkeypatch.setattr(runner, "_checks", checks)
    report = await runner.run_suite(config(max_memory_mb=1))
    assert report.overall == "pass"
    assert report.observations["resources"]["memory_limit_reached"]
    assert not report.observations["resources"]["memory_limit_stopped_run"]
    assert report.observations["resources"]["memory_interrupted_checks"] == []
    assert report.observations["resources"]["sampled_peak_rss_bytes"]["scoring"] == 2 * 1024**2
    assert next(check for check in report.checks if check.id == "serving").status == "pass"
    assert report.abort_reason is None
    assert any("No check was interrupted" in warning for warning in report.warnings)


async def test_memory_limit_preserves_existing_failures(monkeypatch, fake_client):
    monkeypatch.setattr(resources, "current_rss_bytes", lambda: 1)
    monkeypatch.setattr(fake_client, "bad_target", "service")
    original = runner._checks

    def checks(run, target):
        result = original(run, target)
        run.memory_sample(2 * 1024**2)
        return result

    monkeypatch.setattr(runner, "_checks", checks)
    report = await runner.run_suite(config(max_memory_mb=1, checks=["correctness"], concurrency=1))
    assert report.overall == "fail"
    assert report.observations["resources"]["memory_limit_reached"]
    assert report.abort_reason is None


async def test_completed_blocked_access_is_preserved_after_late_memory_warning(monkeypatch, fake_client):
    monkeypatch.setattr(resources, "current_rss_bytes", lambda: 1)
    original = fake_client.execute

    async def denied(self, spec, **kwargs):
        record = await original(self, spec, **kwargs)
        record.http_status, record.valid = 401, False
        monkeypatch.setattr(resources, "current_rss_bytes", lambda: 2 * 1024**2)
        return record

    monkeypatch.setattr(fake_client, "execute", denied)
    report = await runner.run_suite(config(max_memory_mb=1, checks=["access"]))
    assert next(check for check in report.checks if check.id == "access").status == "blocked"
    assert report.observations["resources"]["memory_limit_reached"]
    assert report.overall == "blocked"
    assert not report.observations["resources"]["memory_limit_stopped_run"]
