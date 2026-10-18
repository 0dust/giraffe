"""Cancellation and stop verdicts require exercised server behavior."""
import pytest

from giraffe import runner
from tests.test_runner import FakeClient, check, config


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    return FakeClient


async def test_client_cancellation_waits_for_answer_chunks_not_ttft_timer(client, monkeypatch):
    original, seen = client.execute, []
    async def delayed(self, spec, **kwargs):
        if spec.scenario == "client_cancel":
            seen.append((spec.cancel_after_ms, spec.cancel_after_chunks))
        record = await original(self, spec, **kwargs)
        if spec.scenario == "client_cancel":
            record.first_output_ms = 500
        return record
    monkeypatch.setattr(client, "execute", delayed)
    report = await runner.run_suite(config(checks=["cancellation"]))
    assert seen == [(None, 5)]
    assert check(report, "cancellation").status == "pass"


async def test_disconnect_before_any_answer_is_inconclusive(client, monkeypatch):
    original = client.execute
    async def no_answer(self, spec, **kwargs):
        record = await original(self, spec, **kwargs)
        if spec.scenario == "client_cancel":
            record.first_output_ms, record.answer_chunks = None, 0
        return record
    monkeypatch.setattr(client, "execute", no_answer)
    report = await runner.run_suite(config(checks=["cancellation"]))
    assert check(report, "cancellation").status == "inconclusive"


@pytest.mark.parametrize("control,output,finish,status", [
    ("blue [END] green", "blue", "stop", "pass"),
    ("blue [END] green", "blue [END] green", "stop", "fail"),
    ("blue [END] green", "blue green", "stop", "fail"),
    ("blue [END] green", "blue", "length", "inconclusive"),
    ("blue", "blue", "stop", "inconclusive"),
    ("unrelated", "blue", "stop", "inconclusive"),
    ("unrelated", "a green tree", "stop", "inconclusive"),
])
async def test_stop_requires_full_output_control_and_server_finish_reason(
        client, monkeypatch, control, output, finish, status):
    original = client.execute
    async def answer(self, spec, **kwargs):
        record = await original(self, spec, **kwargs)
        if spec.fixture_id.startswith("limits.stop_control"):
            record.output = control
        elif spec.fixture_id.startswith("limits.stop."):
            record.output, record.finish_reason = output, finish
        return record
    monkeypatch.setattr(client, "execute", answer)
    report = await runner.run_suite(config(checks=["cancellation"]))
    assert check(report, "cancellation").status == status


@pytest.mark.parametrize("usage,finish,status", [(None, "length", "pass"),
    (8, "length", "pass"), (2, "length", "fail"), (9, "length", "fail"),
    (None, "stop", "inconclusive")])
async def test_output_cap_length_finish_is_evidence_and_must_match_usage(
        client, monkeypatch, usage, finish, status):
    original = client.execute
    async def capped(self, spec, **kwargs):
        record = await original(self, spec, **kwargs)
        if spec.fixture_id.startswith("limits.output_cap"):
            record.output_tokens, record.completion_tokens, record.finish_reason = usage, usage, finish
        return record
    monkeypatch.setattr(client, "execute", capped)
    report = await runner.run_suite(config(checks=["cancellation"]))
    assert check(report, "cancellation").status == status


async def test_run_cancelled_during_protocol_tail_is_not_midstream_client_cancellation(client, monkeypatch):
    original = client.execute
    async def tail_cancel(self, spec, **kwargs):
        record = await original(self, spec, **kwargs)
        if spec.scenario == "client_cancel":
            record.client_cancelled_at_ns = None
            record.finish_reason = "stop"
        return record
    monkeypatch.setattr(client, "execute", tail_cancel)
    report = await runner.run_suite(config(checks=["cancellation"]))
    assert check(report, "cancellation").status == "inconclusive"


async def test_legacy_cancel_timer_override_is_explicitly_reported(client):
    report = await runner.run_suite(config(checks=["cancellation"],
        test_options={"cancellation": {"cancel_after_ms": 10}}))
    assert any("cancel_after_ms does not schedule" in warning for warning in report.warnings)
    assert check(report, "cancellation").status == "pass"
