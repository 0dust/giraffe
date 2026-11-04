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
