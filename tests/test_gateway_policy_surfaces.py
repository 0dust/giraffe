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
    row, = rows
    assert row["status"] == "inconclusive"
    assert row["reason"] == current_check.summary


@pytest.mark.usefixtures("local_tokenizer")
async def test_baseline_counts_one_policy_failure_and_keeps_each_identity_outcome(monkeypatch):
    import json

    from giraffe.reporting import _baseline_table_html
    from tests.test_ux_catalogue import browser_logic

    with monkeypatch.context() as context:
        baseline_check, baseline, _ = await exercise(context, full=True)
    with monkeypatch.context() as context:
        current_check, current, _ = await exercise(context, full=True, behavior="overadmit")

    assert baseline_check.status == "pass"
    assert current_check.status == "fail"
    assert current_check.metrics["gateway_policy"]["peer_continuation"] is True
    comparison = compare_baseline(current, baseline).baseline
    target = comparison["targets"][0]
    assert comparison["status"] == target["status"] == "fail"
    assert "1 material metric regression(s)" in target["summary"]
    row, = target["comparisons"]
    assert row["metric"] == "policy_acceptance" and row["status"] == "fail"
    assert row["baseline_samples"] == row["current_samples"] == 13
    for side, check in (("baseline", baseline_check), ("current", current_check)):
        assert row[f"{side}_outcomes"] == check.metrics["gateway_policy"]["users"]
    assert [user["admitted"] for user in row["baseline_outcomes"]] == [2, 8]
    assert [user["admitted"] for user in row["current_outcomes"]] == [3, 8]

    html = _baseline_table_html(comparison)
    assert html.count(">Policy acceptance<") == 1
    assert "limited · 2 admitted · 3 rejected" in html
    assert "limited · 3 admitted · 2 rejected" in html
    assert html.count("peer · 8 admitted · 0 rejected") == 2

    browser_logic("const rows=" + json.dumps([row]) + """
const html=comparisonTable(rows);
assert.equal((html.match(/Observed failure/g)||[]).length,1);
assert.match(html,/limited · 2 admitted · 3 rejected/);
assert.match(html,/limited · 3 admitted · 2 rejected/);
assert.equal((html.match(/peer · 8 admitted · 0 rejected/g)||[]).length,2);
""")


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


@pytest.mark.usefixtures("local_tokenizer")
async def test_blocked_policy_check_cannot_establish_baseline_comparison(monkeypatch):
    from giraffe import runner

    with monkeypatch.context() as context:
        successful_check, successful, _ = await exercise(context, full=True)
    assert successful_check.status == "pass"
    metrics = runner._metrics

    async def fail_end_metrics(run, target, phase):
        if phase == "end":
            raise RuntimeError("end telemetry fixture failed")
        return await metrics(run, target, phase)

    with monkeypatch.context() as context:
        context.setattr(runner, "_metrics", fail_end_metrics)
        blocked_check, blocked, _ = await exercise(context, full=True)
    assert blocked_check.status == "blocked"
    assert blocked_check.metrics["gateway_policy"]["qualification"] == "observed"

    for current, baseline in ((successful, blocked), (blocked, successful)):
        comparison = compare_baseline(current, baseline).baseline
        assert comparison["status"] == "inconclusive"
        row, = comparison["targets"][0]["comparisons"]
        assert row["status"] == "inconclusive"


def test_policy_plan_follows_shared_workloads():
    from giraffe.models import RunConfig
    from tests.test_gateway_policy import configuration

    raw = configuration().model_dump()
    raw.update(checks=["gateway_policy", "multi_user", "sessions", "consistency", "tools"],
               multi_user={"levels": [1]}, sessions={"modes": ["fixed"]}, consistency={})
    rows = [row["id"] for row in request_plan(RunConfig.model_validate(raw))["rows"]]
    assert rows == ["multi_user_1", "sessions", "consistency", "tools", "gateway_policy"]


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
