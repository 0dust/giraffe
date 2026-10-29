"""UI remediation is data; CLI guidance remains readable and flag-specific."""

import pytest

from giraffe.models import CheckResult
from giraffe import runner
from tests.test_load_test_phase_a import config as load_config
from tests.test_review26_phase4_accuracy import run_virtual
from tests.test_runner import FakeClient

pytestmark = pytest.mark.usefixtures("local_tokenizer")


def test_archived_checks_accept_missing_remediation_and_new_metadata_roundtrips():
    old = dict(id="first_output", target="local", title="Old title", status="inconclusive", summary="Timing observed.")
    archived = CheckResult.model_validate(old)
    assert archived.remediation is None
    assert archived.finding_summary is None
    value = {**old, "finding_summary": "No timing target configured.",
             "remediation": {"action": "set_acceptance", "check": "first_output", "limits": ["first_output_ms"]}}
    assert CheckResult.model_validate(value).model_dump()["remediation"] == value["remediation"]


@pytest.mark.parametrize("cap,expected", [
    ("requests", {"action": "set_limit", "limit": "max_duration", "clear": "max_requests"}),
    ("duration", {"action": "set_limit", "limit": "max_duration"}),
])
def test_load_interruption_emits_structured_remediation_without_changing_cli(monkeypatch, cap, expected):
    async def scenario():
        monkeypatch.setattr(runner, "LLMClient", FakeClient)
        configured = load_config()
        if cap == "requests":
            configured.max_requests = 6
        else:
            configured.max_duration_seconds = .05
        report = await runner.run_suite(configured)
        check = next(check for check in report.checks if check.id == "load")
        assert check.status == "inconclusive"
        assert all(check.remediation.get(key) == value for key, value in expected.items())
        assert "--max-duration" in check.summary
        if cap == "requests":
            assert "--no-max-requests" in check.summary
        assert check.finding_summary.startswith("1 of 1 load conditions unfinished:")
        assert "--" not in check.finding_summary
    run_virtual(monkeypatch, scenario())


def test_first_answer_target_metadata_is_independent_of_cli_prose():
    configured = load_config().model_copy(update={"checks": ["first_output"]})
    check = CheckResult(id="first_output", target="local", title="Time to first answer",
                        status="inconclusive", summary="Timing observed; no acceptance limit configured.",
                        metrics={"reason_code": "missing_acceptance_limit"})
    text = runner._verdict_summary(check, configured, [], "")
    assert "--first-output-ms" in text
    assert check.finding_summary == "Timing observed; no acceptance limit configured."
    assert check.remediation == {"action": "set_acceptance", "check": "first_output", "limits": ["first_output_ms"]}


def test_fixed_check_request_budget_exposes_minimum_without_parsing_cli_text(monkeypatch):
    async def scenario():
        monkeypatch.setattr(runner, "LLMClient", FakeClient)
        configured = load_config().model_copy(update={"checks": ["access", "correctness"], "max_requests": 2})
        report = await runner.run_suite(configured)
        check = next(check for check in report.checks if check.id == "correctness")
        assert check.remediation["action"] == "set_limit"
        assert check.remediation["limit"] == "max_requests"
        assert check.remediation["minimum"] > 2
        assert "--max-requests" in check.summary
        assert "--" not in check.finding_summary
    run_virtual(monkeypatch, scenario())


def test_load_missing_timing_target_emits_setting_metadata(monkeypatch):
    async def scenario():
        monkeypatch.setattr(runner, "LLMClient", FakeClient)
        configured = load_config()
        configured.load_test.pass_criteria.max_first_answer_ms = None
        configured.load_test.pass_criteria.max_completion_ms = None
        report = await runner.run_suite(configured)
        check = next(check for check in report.checks if check.id == "load")
        assert check.status == "inconclusive"
        assert check.remediation == {"action": "set_acceptance", "check": "load",
                                     "limits": ["max_first_answer_ms", "max_completion_ms"]}
        assert "load_test.pass_criteria.max_first_answer_ms" in check.summary
        assert "load_test." not in check.finding_summary
    run_virtual(monkeypatch, scenario())
