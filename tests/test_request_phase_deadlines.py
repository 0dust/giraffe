"""A queued request must respect the earlier phase admission deadline."""

import asyncio

import pytest

from giraffe import runner
from giraffe.models import RequestSpec
from tests.test_review26_phase4_accuracy import run_virtual
from tests.test_runner import FakeClient, config


pytestmark = pytest.mark.usefixtures("local_tokenizer")


def test_queued_request_expires_at_phase_deadline_before_run_deadline(monkeypatch):
    async def scenario():
        settings = config(concurrency=1, max_duration_seconds=10)
        events = []
        state = runner._Run(settings, events.append, None)
        target = settings.targets[0]
        request = RequestSpec(fixture_id="queued", scenario="warm", check_ids=["serving"], messages=[])
        assert state.try_acquire_slot()
        loop = asyncio.get_running_loop()
        phase_deadline = loop.time() + 2
        # The occupied slot becomes available after the phase, before the run ends.
        release = loop.call_at(phase_deadline + 1, state.release_slot)
        try:
            record = await state.request(target, FakeClient(target, settings), request,
                                         deadline_override=phase_deadline)
            assert record is None
            assert loop.time() == phase_deadline
            assert not any(event["event"] == "request_started" for event in events)
            result = next(check for check in runner._checks(state, target) if check.id == "serving")
            assert result.status == "inconclusive"
            assert result.metrics["completed"] == 0
        finally:
            release.cancel()

    run_virtual(monkeypatch, scenario())


def test_admission_deadline_does_not_shorten_an_admitted_request(monkeypatch):
    async def scenario():
        settings = config(concurrency=1, max_duration_seconds=10)
        state = runner._Run(settings, None, None)
        target = settings.targets[0]
        request = RequestSpec(fixture_id="draining", scenario="warm", check_ids=["serving"], messages=[])
        monkeypatch.setattr(FakeClient, "delay", .1)
        cutoff = asyncio.get_running_loop().time() + .05
        record = await state.request(target, FakeClient(target, settings), request,
                                     admission_deadline=cutoff)
        assert record.status == "completed"
        assert record.dispatch_ms < cutoff * 1000 < record.completed_ms
        assert state.attempted == state.budget_used == 1
        assert state.slots.qsize() == 1

    run_virtual(monkeypatch, scenario())


def test_waiting_admission_deadline_never_takes_budget_or_owns_occupied_slot(monkeypatch):
    async def scenario():
        settings = config(concurrency=1, max_duration_seconds=10)
        state = runner._Run(settings, None, None)
        target = settings.targets[0]
        request = RequestSpec(fixture_id="queued", scenario="warm", check_ids=["serving"], messages=[])
        assert state.try_acquire_slot()
        cutoff = asyncio.get_running_loop().time() + .05
        record = await state.request(target, FakeClient(target, settings), request,
                                     admission_deadline=cutoff)
        assert record is None
        assert state.attempted == state.budget_used == 0
        assert state.slots.qsize() == 0
        state.release_slot()
        assert state.slots.qsize() == 1

    run_virtual(monkeypatch, scenario())


@pytest.mark.parametrize("exception", [ValueError, TimeoutError])
def test_local_preparation_exception_records_an_error_without_an_http_attempt(monkeypatch, exception):
    class PreparationClient(FakeClient):
        _supports_dispatch_guard = True

        async def execute(self, spec, **kwargs):
            raise exception("secret-sk-123")

    async def scenario():
        settings = config(stop_after_errors=1)
        retained, events = [], []
        state = runner._Run(settings, events.append, None)
        state.record_sink = retained.append
        target = settings.targets[0]
        request = RequestSpec(fixture_id="preparation", scenario="warm", check_ids=["serving"], messages=[])
        record = await state.request(target, PreparationClient(target, settings), request,
            prepared_admission=True, admission_deadline=state.deadline)
        assert state.records == retained == [record]
        assert record.error == f"request could not be prepared ({exception.__name__})"
        assert record.status == "failed" and not record.valid
        assert record.dispatch_ms is record.timing_start_ms is None
        assert record.overlap == 0
        assert state.attempted == state.budget_used == state.counts[target.name] == 0
        assert state.errors == 1 and state.stop_reason() == "error threshold reached"
        assert state.endpoint_health[target.name]["consecutive_failures"] == 0
        assert not any(event["event"] == "request_started" for event in events)
        assert all(row["running"] == row["started"] == row["sent"] == 0 for row in state.conditions.values())
        assert state.slots.qsize() == state.slots.maxsize

    run_virtual(monkeypatch, scenario())


def test_http_client_preparation_failure_returns_a_sanitized_local_record(monkeypatch):
    from giraffe.client import LLMClient

    async def scenario():
        settings = config()
        state = runner._Run(settings, None, None)
        target = settings.targets[0]
        request = RequestSpec(fixture_id="preparation", scenario="warm", check_ids=["serving"], messages=[])

        def fail(*args, **kwargs):
            raise ValueError("secret-sk-123")

        async with LLMClient(target, settings) as client:
            monkeypatch.setattr(client._client, "build_request", fail)
            record = await state.request(target, client, request,
                prepared_admission=True, admission_deadline=state.deadline)
        assert state.records == [record]
        assert record.error == "request could not be prepared (ValueError)"
        assert "secret-sk-123" not in record.model_dump_json()
        assert state.errors == 1
        assert state.attempted == state.budget_used == 0
        assert record.dispatch_ms is None

    run_virtual(monkeypatch, scenario())


@pytest.mark.parametrize("refusal", ["cancelled", "dispatch", "admission", "phase_deadline", "run_deadline", "user_stop"])
def test_unadmitted_refusals_and_cancellation_do_not_become_preparation_failures(monkeypatch, refusal):
    from giraffe.models import RequestRecord

    class PreparationClient(FakeClient):
        _supports_dispatch_guard = True

        async def execute(self, spec, **kwargs):
            if refusal == "user_stop":
                self.stop.set()
                raise ValueError("secret-sk-123")
            if refusal == "dispatch":
                raise runner._DispatchRefused
            if refusal in {"phase_deadline", "run_deadline"}:
                await asyncio.sleep(.2)
                raise ValueError("secret-sk-123")
            return RequestRecord(id="cancelled", target=self.target.name, fixture_id=spec.fixture_id,
                scenario=spec.scenario, check_ids=spec.check_ids, started_at=runner._now(),
                status="cancelled", error="run cancelled")

    async def scenario():
        settings = config(max_duration_seconds=.1 if refusal == "run_deadline" else 5)
        state = runner._Run(settings, None, None)
        target = settings.targets[0]
        client = PreparationClient(target, settings)
        client.stop = state.stop
        request = RequestSpec(fixture_id="preparation", scenario="warm", check_ids=["serving"], messages=[])
        record = await state.request(target, client, request,
            prepared_admission=True, admission_deadline=.05 if refusal == "phase_deadline" else state.deadline,
            admission_check=lambda: refusal != "admission")
        assert record is None
        assert state.records == []
        assert state.attempted == state.budget_used == state.errors == 0
        assert state.slots.qsize() == state.slots.maxsize

    run_virtual(monkeypatch, scenario())
