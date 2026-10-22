"""Load checks need active-stream and matched-control evidence, not timing proxies."""

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone

import pytest

from giraffe import runner
from giraffe.models import Limits, RequestRecord
from tests.test_runner import check, config


class LoadClient:
    """An in-process streaming endpoint with independently controlled answer/close times."""

    def __init__(self, target, configuration, *, max_connections=None):
        self.target = target

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def execute(self, spec, *, first_output_event=None):
        cls = type(self)
        started = time.monotonic()
        index = sum(item[0].scenario == spec.scenario for item in cls.calls)
        item = [spec, started, None]
        cls.calls.append(item)
        delay = cls.delay(spec, index)
        await asyncio.sleep(.003)
        if first_output_event is not None:
            first_output_event.set()
        await asyncio.sleep(max(.001, delay-.003))
        item[2] = time.monotonic()
        elapsed = (item[2]-started)*1000
        first, last, reported_elapsed = cls.timing(spec, elapsed)
        output = json.dumps(spec.expected) if spec.scorer == "json" else str(spec.expected)
        return RequestRecord(
            id=uuid.uuid4().hex, target=self.target.name, fixture_id=spec.fixture_id,
            scenario=spec.scenario, check_ids=spec.check_ids,
            started_at=datetime.now(timezone.utc).isoformat(), status="completed", valid=True,
            http_status=200, output=output, output_chars=len(output),
            input_chars=spec.input_chars, input_tokens=max(1, spec.input_chars//4),
            output_tokens=max(1, len(output.split())), elapsed_ms=reported_elapsed,
            first_output_ms=first, last_output_ms=last, answer_chunks=2, answer_reads=2, chunks=2,
            max_stream_gap_ms=max(.1, last-first), stream=spec.stream,
            stream_terminated=True, finish_reason="stop", requested_max_tokens=spec.max_tokens,
        )

    @staticmethod
    def delay(spec, index):
        return .02

    @staticmethod
    def timing(spec, elapsed):
        return 1, elapsed-1, elapsed


@pytest.fixture
def endpoint(monkeypatch):
    class Endpoint(LoadClient):
        calls = []
    monkeypatch.setattr(runner, "LLMClient", Endpoint)
    return Endpoint








@pytest.mark.parametrize("slow_metric", ["latency", "first_output"])
async def test_recovery_ratio_compares_same_fixture_before_and_after_load(endpoint, monkeypatch, slow_metric):
    def timing(spec, elapsed):
        if spec.scenario == "recovery_baseline":
            return 2, 8, 10
        if spec.scenario == "recovery":
            return (8, 9, 10) if slow_metric == "first_output" else (2, 8, 40)
        return 1, elapsed-1, elapsed
    monkeypatch.setattr(endpoint, "timing", staticmethod(timing))
    report = await runner.run_suite(config(checks=["recovery"], sustained_seconds=.045,
        limits=Limits(recovery_max_ratio=2, min_samples=2)))
    before = [item[0] for item in endpoint.calls if item[0].scenario == "recovery_baseline"]
    after = [item[0] for item in endpoint.calls if item[0].scenario == "recovery"]
    assert len(before) == len(after) == 2
    assert len({spec.fixture_id for spec in before+after}) == 1
    assert all(before[0].messages == spec.messages for spec in before+after)
    result = check(report, "recovery")
    assert result.status == "fail"
    assert result.metrics[f"{slow_metric}_ratio"] == 4


async def test_recovery_without_timing_acceptance_limit_is_inconclusive(endpoint):
    # Amortize worker startup/refill delays so occupancy is not the inconclusive reason.
    report = await runner.run_suite(config(checks=["recovery"], sustained_seconds=.25))
    result = check(report, "recovery")
    assert result.metrics["sustained"]["completed"] > 0
    assert result.metrics["recovery"]["correct"] == 2
    assert result.status == "inconclusive"
    assert result.summary.startswith("Recovery observed; no acceptance limit configured.")
    assert "To conclude, set limits.recovery_max_ratio" in result.summary


async def test_recovery_refills_workers_while_slow_peer_is_still_running(endpoint, monkeypatch):
    def delay(spec, index):
        return .07 if spec.scenario == "sustained" and index == 0 else .008
    monkeypatch.setattr(endpoint, "delay", staticmethod(delay))
    report = await runner.run_suite(config(checks=["recovery"], concurrency=3,
        sustained_seconds=.045, limits=Limits(recovery_max_ratio=10, min_samples=2)))
    sustained = [item for item in endpoint.calls if item[0].scenario == "sustained"]
    assert len([item for item in sustained if item[1] < sustained[0][2]]) > 3
    specs = [item[0] for item in sustained]
    assert all(spec.scorer == "exact" and spec.expected is not None for spec in specs)
    assert any(spec.input_chars >= 4096 for spec in specs)
    assert any(len(spec.expected.split()) >= 32 for spec in specs)
    assert len({spec.fixture_id for spec in specs}) >= 2
    assert all(record.score is True for record in report.requests)


async def test_recovery_reserves_later_fixed_checks_and_caps_remaining_budget_share(endpoint, monkeypatch):
    monkeypatch.setattr(endpoint, "delay", staticmethod(lambda spec, index: .004))
    report = await runner.run_suite(config(checks=["recovery", "consistency"], max_requests=26,
        sustained_seconds=.1, consistency={"shapes": ["short"], "levels": [1, 2], "repetitions": 2, "prompts_per_shape": 2},
        limits=Limits(recovery_max_ratio=10, min_samples=2)))
    sustained = [record for record in report.requests if record.scenario == "sustained"]
    budget = check(report, "recovery").metrics["sustained_budget"]
    assert len(sustained) <= (26-2)//2
    assert len(sustained) <= budget["allowance"]
    assert budget["reserved_later"] > 2
    assert check(report, "consistency").metrics["attempted"] > 0
    assert len([record for record in report.requests if record.scenario == "recovery"]) == 2
    assert report.abort_reason is None


async def test_recovery_tiny_load_allowance_cannot_claim_configured_concurrency(endpoint, monkeypatch):
    monkeypatch.setattr(endpoint, "delay", staticmethod(
        lambda spec, index: .06 if spec.scenario == "sustained" else .004))
    report = await runner.run_suite(config(checks=["recovery"], concurrency=3, max_requests=5,
        sustained_seconds=.045, limits=Limits(recovery_max_ratio=10, min_samples=2)))
    result = check(report, "recovery")
    assert result.metrics["sustained"]["attempted"] == 1
    assert result.status == "inconclusive"
    assert not report.observations["targets"]["service"]["sustained_complete"]






async def test_recovery_consumed_allowance_can_qualify_before_duration(endpoint):
    report = await runner.run_suite(config(checks=['recovery'], max_requests=40,
        sustained_seconds=10, limits=Limits(recovery_max_ratio=10, min_samples=2)))
    result = check(report, 'recovery')
    assert result.status == 'pass'
    assert result.metrics['sustained_budget']['allowance'] == 35
    assert result.metrics['sustained']['attempted'] == 35
    assert len([r for r in report.requests if r.scenario == 'recovery_prime']) == 1
    assert result.metrics['sustained_completion']['reason'] == 'allowance consumed with sustained concurrency'
    assert result.metrics['sustained_completion']['time_weighted_in_flight'] >= 1.8


@pytest.mark.parametrize('failure,status', [('timeout', 'fail'), ('failed', 'fail')])
async def test_recovery_sustained_timeouts_and_errors_fail(endpoint, monkeypatch, failure, status):
    original = endpoint.execute
    async def fail_load(self, spec, **kwargs):
        result = await original(self, spec, **kwargs)
        if spec.scenario == 'sustained':
            result.status, result.valid = failure, False
            result.http_status = None if failure == 'timeout' else 503
        return result
    monkeypatch.setattr(endpoint, 'execute', fail_load)
    report = await runner.run_suite(config(checks=['recovery'], max_requests=40,
        sustained_seconds=10, stop_after_errors=100, limits=Limits(recovery_max_ratio=10, min_samples=2)))
    result = check(report, 'recovery')
    assert result.status == status
    assert result.metrics['sustained']['error_rate'] == 1
    assert result.metrics['sustained_error_rate'] == 1
    assert result.metrics['recovery']['error_rate'] == 0
    assert result.metrics['sustained_non_timeout_error_rate'] == (0 if failure == 'timeout' else 1)




@pytest.mark.parametrize('check_id,scenario,expected', [
    ('recovery', 'recovery_baseline', 'inconclusive'),
    ('recovery', 'recovery', 'inconclusive'),
    ('recovery', 'sustained', 'pass'),
])
async def test_matched_ratio_qualifies_client_delay_in_both_controls_and_probes(
    endpoint, monkeypatch, check_id, scenario, expected,
):
    original = runner._checks

    def delayed(run, target):
        record = next(row for row in run.records if row.scenario == scenario)
        run.timing_health.windows[:] = [{
            'start_ms': record.dispatch_ms, 'end_ms': record.completed_ms, 'lag_ms': 500,
        }]
        return original(run, target)

    monkeypatch.setattr(runner, '_checks', delayed)
    limits = Limits(min_samples=2, **{
        'recovery_max_ratio' if check_id == 'recovery' else 'fairness_max_ratio': 100,
    })
    report = await runner.run_suite(config(checks=[check_id], sustained_seconds=.25, limits=limits))
    outcome = check(report, check_id)
    assert outcome.status == expected
    assert outcome.metrics['timing_quality']['status'] == (
        'qualified' if expected == 'pass' else 'client_delayed')
