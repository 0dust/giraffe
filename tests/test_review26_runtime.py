"""Request-level regressions found by the issue 26 adversarial comparison."""

import json

import pytest

from giraffe import runner
from giraffe.cli import migrated_configuration
from giraffe.models import RunConfig
from tests.test_runner import FakeClient


def config(**changes):
    return RunConfig.model_validate({"targets": [{"name": "one", "url": "http://local", "model": "m"}],
                                     "checks": ["correctness"], **changes})


def test_overlap_clients_can_use_every_per_target_slot_without_pool_queueing():
    configured = config(targets=[{"name": name, "url": "http://local", "model": "m"}
                                 for name in ("one", "two")], overlap_models=True, concurrency=4,
                        test_options={"correctness": {"concurrency": 3}})
    run = runner._Run(configured, None, None)
    assert run.slots.maxsize == 4
    assert run.connection_limits == {"one": 3, "two": 3}
    assert all(limit >= min(3, run.slots.maxsize) for limit in run.connection_limits.values())


async def test_access_timeout_applies_to_initial_only_and_manifest_matches(monkeypatch):
    sent = []

    class RecordingClient(FakeClient):
        async def execute(self, spec, **kwargs):
            sent.append((spec.scenario, spec.request_timeout_seconds, spec.max_tokens))
            return await super().execute(spec, **kwargs)

    monkeypatch.setattr(runner, "LLMClient", RecordingClient)
    configured = config(checks=["access", "serving", "first_output"], test_options={
        "access": {"request_timeout_seconds": 2},
        "serving": {"samples": 2, "request_timeout_seconds": 7, "max_output_tokens": 80}})
    report = await runner.run_suite(configured)
    assert sent == [("initial", 2, 80), ("warm", 7, 80), ("warm", 7, 80)]
    assert report.manifest["effective_initial"]["request_timeout_seconds"] == 2
    assert report.manifest["effective_checks"]["serving"]["request_timeout_seconds"] == 7


async def test_tools_never_silently_disappear_when_suite_output_cap_matches_context(monkeypatch):
    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    report = await runner.run_suite(config(checks=["tools"], max_output_tokens=4096, context_limit=4096))
    assert len(report.requests) == 2
    assert {record.requested_max_tokens for record in report.requests} == {4096}
    assert all(record.scenario.startswith("tools_") for record in report.requests)


def test_recovery_allowance_records_request_limit_before_total_exhaustion(monkeypatch):
    from tests.test_review26_phase4_accuracy import run_virtual

    async def scenario():
        monkeypatch.setattr(runner, "LLMClient", FakeClient)
        configured = config(checks=["recovery", "tools"], max_requests=40, sustained_seconds=.05)
        report = await runner.run_suite(configured)
        assert len(report.requests) < 40
        assert report.observations["targets"]["one"]["sustained_batches"] > 0
        assert "max_requests" in report.limits_reached

    run_virtual(monkeypatch, scenario())


@pytest.mark.parametrize("samples,exhausted", [(6, False), (8, True)])
async def test_final_request_marks_exhaustion_even_without_another_admission_check(monkeypatch, samples, exhausted):
    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    configured = config(max_requests=6, samples=samples)
    report = await runner.run_suite(configured)
    assert len(report.requests) == 6
    assert ("max_requests" in report.limits_reached) == exhausted
    assert (report.abort_reason == "maximum request budget reached") == exhausted


async def test_legacy_custom_fixture_caps_survive_v2_export_and_v2_keeps_own_caps(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "LLMClient", FakeClient)
    path = tmp_path / "fixtures.json"
    path.write_text(json.dumps([{"fixture_id": "custom", "messages": [{"role": "user", "content": "Hello"}],
                               "scorer": "none", "max_tokens": 200}]))
    values = {"targets": [{"name": "one", "url": "http://local", "model": "m"}],
              "checks": ["correctness"], "max_output_tokens": 32, "custom_fixtures": str(path)}
    legacy = migrated_configuration(values)
    assert any(note.startswith("Custom-fixture output budgets") for note in legacy._migration_notes)
    for configured, expected in ((legacy, 32), (RunConfig.model_validate(legacy.model_dump()), 32),
                                 (RunConfig.model_validate(values), 200)):
        report = await runner.run_suite(configured)
        custom = [record for record in report.requests if record.fixture_id == "custom"]
        assert custom and {record.requested_max_tokens for record in custom} == {expected}


@pytest.mark.parametrize("check_id", ["generation", "correctness"])
async def test_output_remedy_names_the_check_override_that_reached_its_cap(monkeypatch, check_id):
    class ReasoningClient(FakeClient):
        async def execute(self, spec, **kwargs):
            record = await super().execute(spec, **kwargs)
            return record.model_copy(update={"output": "", "reasoning": "still thinking",
                "reasoning_tokens": spec.max_tokens, "completion_tokens": spec.max_tokens,
                "valid": False, "finish_reason": "length",
                "error": "reasoning exhausted the output budget before an answer"})

    monkeypatch.setattr(runner, "LLMClient", ReasoningClient)
    report = await runner.run_suite(config(checks=[check_id], max_output_tokens=32,
        stop_after_errors=100, test_options={check_id: {"max_output_tokens": 80,
            **({"concurrency": 1} if check_id == "correctness" else {})}}))
    check = next(check for check in report.checks if check.id == check_id)
    assert check.status == "inconclusive"
    assert f"test_options.{check_id}.max_output_tokens above 80" in check.summary
    assert "--max-output-tokens" not in check.summary


def test_burst_warning_accounts_for_other_endpoints_using_shared_slots():
    from pydantic import ValidationError
    from tests.test_planning import load_config
    configuration = load_config(concurrency=4, overlap_models=True,
        targets=[{"name": name, "url": "http://local", "model": "m"} for name in ("one", "two")])
    configuration.load_test.patterns.bursts.enabled = True
    configuration.load_test.patterns.bursts.size = 6
    configuration.load_test.patterns.bursts.first_seconds = 0
    with pytest.raises(ValidationError) as raised:
        RunConfig.model_validate(configuration.model_dump())
    assert raised.value.errors(include_url=False)[0]["ctx"]["fields"] == ["load_test.patterns.bursts.size", "concurrency"]
