"""Known-answer verdicts include loaded outcomes and qualify actual overlap."""
import pytest

from giraffe import runner
from tests.test_runner import FakeClient, check, config, pack_config


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    return FakeClient


@pytest.mark.parametrize("capacity", [True, False])
async def test_pack_loaded_wrong_answers_fail_correctness(client, monkeypatch, capacity):
    original = client.execute
    async def loaded_wrong(self, spec, **kwargs):
        record = await original(self, spec, **kwargs)
        if spec.scenario.startswith("traffic_stage_") or spec.scenario == "pack_loaded":
            record.output = "wrong"
        return record
    monkeypatch.setattr(client, "execute", loaded_wrong)
    report = await runner.run_suite(pack_config(checks=["correctness", *(["capacity"] if capacity else [])]))
    result = check(report, "correctness")
    assert result.status == "fail"
    assert result.metrics["request_pack"]["control_status"] == "pass"
    assert result.metrics["request_pack"]["loaded_status"] == "fail"


async def test_pack_case_failure_cannot_hide_in_aggregate_allowance(client, monkeypatch):
    original = client.execute
    async def wrong_case(self, spec, **kwargs):
        record = await original(self, spec, **kwargs)
        if spec.scenario == "pack_loaded" and spec.fixture_id == "pack:alpha":
            record.output = "wrong"
        return record
    monkeypatch.setattr(client, "execute", wrong_case)
    report = await runner.run_suite(pack_config(checks=["correctness"],
        test_options={"correctness": {"limits": {"min_correctness": .5}}}))
    result = check(report, "correctness")
    assert result.metrics["accuracy"] >= .5 and result.status == "fail"


@pytest.mark.parametrize("field,value", [("samples", 9), ("concurrency", 1)])
async def test_pack_correctness_overrides_are_explicitly_rejected(client, field, value):
    with pytest.raises(ValueError, match="samples/concurrency overrides are unsupported"):
        await runner.run_suite(pack_config(test_options={"correctness": {field: value}}))


async def test_pack_without_capacity_covers_all_cases_quietly_and_concurrently(client):
    report = await runner.run_suite(pack_config(checks=["correctness"], concurrency=4))
    quiet = [r for r in report.requests if r.scenario == "pack_control"]
    loaded = [r for r in report.requests if r.scenario == "pack_loaded"]
    assert len(quiet) == 2 and len(loaded) == 4
    assert all(r.overlap == 1 for r in quiet) and max(r.overlap for r in loaded) == 4
    assert {r.fixture_id for r in loaded} == {r.fixture_id for r in quiet}
    assert check(report, "correctness").status == "pass"


@pytest.mark.parametrize("check_id", ["correctness", "json"])
async def test_builtin_scored_check_needs_actual_concurrent_exercise(client, check_id):
    report = await runner.run_suite(config(checks=[check_id],
        test_options={check_id: {"concurrency": 1}}))
    result = check(report, check_id)
    # Effective concurrency=1 was explicitly selected: report serial evidence honestly.
    assert result.status == "pass" and "under load" not in result.summary


@pytest.mark.parametrize("check_id", ["correctness", "json"])
async def test_configured_concurrency_without_observed_overlap_is_inconclusive(client, monkeypatch, check_id):
    original = runner._Run.request
    async def serial_evidence(self, *args, **kwargs):
        record = await original(self, *args, **kwargs)
        if record:
            record.overlap = 1
            record.timing_start_ms = None
        return record
    monkeypatch.setattr(runner._Run, "request", serial_evidence)
    report = await runner.run_suite(config(checks=[check_id]))
    assert check(report, check_id).status == "inconclusive"
    assert "not exercised concurrently" in check(report, check_id).summary


@pytest.mark.parametrize("check_id", ["correctness", "json"])
async def test_single_concurrency_summary_does_not_claim_load(client, check_id):
    report = await runner.run_suite(config(checks=[check_id], concurrency=1))
    assert "under load" not in check(report, check_id).summary
    assert "under observed concurrency" not in check(report, check_id).summary


@pytest.mark.parametrize("check_id", ["correctness", "json"])
async def test_serial_wrong_scores_do_not_claim_concurrent_failure(client, monkeypatch, check_id):
    original = runner._Run.request
    async def serial_wrong(self, *args, **kwargs):
        record = await original(self, *args, **kwargs)
        if record:
            record.overlap, record.score = 1, False
        return record
    monkeypatch.setattr(runner._Run, "request", serial_wrong)
    report = await runner.run_suite(config(checks=[check_id]))
    result = check(report, check_id)
    assert result.metrics["accuracy"] == 0
    assert result.status == "fail" and "scoring failed" in result.summary
