"""Planning and baseline explanations preserve policy execution semantics."""
import pytest

from giraffe.models import required_concurrency
from giraffe.planning import request_plan
from giraffe.reporting import compare_baseline
from tests.test_gateway_policy import exercise


@pytest.mark.parametrize("settings", [{"gateway": "unsupported"}, {"capability": "unsupported"}])
async def test_unsupported_policy_plans_no_requests_time_or_slots(monkeypatch, settings):
    check, run, sent = await exercise(monkeypatch, **settings)
    assert not sent
    assert check.metrics["gateway_policy"]["qualification"] == "unavailable"
    plan = request_plan(run.config)
    row = next(row for row in plan["rows"] if row["id"] == "gateway_policy")
    assert row["requests_per_target"] == 0
    assert row["kind"] == "exact"
    assert "unsupported" in row["summary"].lower()
    assert plan["minimum_seconds"] == 0
    assert required_concurrency(run.config)["by_check"]["gateway_policy"] == 0


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("behavior", ["ignore", "unknown"])
async def test_baseline_preserves_current_inconclusive_policy_reason(monkeypatch, behavior):
    with monkeypatch.context() as context:
        _, baseline, _ = await exercise(context, full=True)
    with monkeypatch.context() as context:
        current_check, current, _ = await exercise(context, full=True, behavior=behavior)
    assert current_check.status == "inconclusive"
    compared = compare_baseline(current, baseline)
    rows = [row for row in compared.baseline["targets"][0]["comparisons"] if row["metric"] == "policy_acceptance"]
    assert len(rows) == 2
    assert all(row["status"] == "inconclusive" for row in rows)
    assert all(row["reason"] == current_check.summary for row in rows)


@pytest.mark.usefixtures("local_tokenizer")
async def test_matching_legacy_policy_summaries_do_not_establish_a_baseline(monkeypatch):
    check, report, _ = await exercise(monkeypatch, full=True)
    assert check.status == "pass"
    assert compare_baseline(report, report.model_copy(deep=True)).baseline["status"] == "pass"

    check.metrics["gateway_policy"]["version"] = 7
    compared = compare_baseline(report, report.model_copy(deep=True)).baseline
    assert compared["status"] == "inconclusive"
    assert all(row["status"] == "inconclusive" for row in compared["targets"][0]["comparisons"])


@pytest.mark.usefixtures("local_tokenizer")
async def test_policy_baseline_attests_source_separately_from_shared_work(monkeypatch):
    check, report, _ = await exercise(monkeypatch, full=True)
    assert check.status == "pass"

    legacy = report.model_copy(deep=True)
    legacy.manifest.pop("gateway_policy_source_sha256")
    legacy.manifest["fixture_pack"].pop("shared_workload_fixture_sha256")
    assert compare_baseline(report, legacy).baseline["status"] == "pass"

    different = report.model_copy(deep=True)
    different.manifest["gateway_policy_source_sha256"] = "0" * 64
    compared = compare_baseline(report, different).baseline
    assert compared["status"] == "inconclusive"
    assert all(row["status"] == "inconclusive" for row in compared["targets"][0]["comparisons"])


@pytest.mark.parametrize("mode", ["key_rpm", "priority_rpm"])
def test_supported_policy_keeps_declared_plan(mode):
    from tests.test_gateway_policy import configuration

    config = configuration(mode)
    plan = request_plan(config)
    row = next(row for row in plan["rows"] if row["id"] == "gateway_policy")
    assert row["requests_per_target"] == 13
    assert row["kind"] == "exact"
    assert plan["minimum_seconds"] == 2
    assert required_concurrency(config)["by_check"]["gateway_policy"] == 2


@pytest.mark.parametrize("mode", ["key_rpm", "priority_rpm"])
def test_finite_policy_plan_requires_only_its_declared_request_cap(mode):
    from tests.test_gateway_policy import configuration

    config = configuration(mode).model_copy(update={"max_requests": 13})
    plan = request_plan(config)
    assert plan["planned_total"] == plan["known_total"] == plan["required_request_limit"] == 13
    assert plan["count_kind"] == "exact"
    assert plan["request_warnings"] == []
    truncated = request_plan(config.model_copy(update={"max_requests": 12}))
    assert [warning["kind"] for warning in truncated["request_warnings"]] == ["incomplete_coverage"]
    assert truncated["request_warnings"][0]["values"]["required_request_limit"] == 13
