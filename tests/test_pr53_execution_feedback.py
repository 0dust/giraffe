"""Review regressions for request limits, preparation and condition identity."""
import asyncio

import pytest

from giraffe.conditions import ConditionRequestIndex, condition_aggregate, planned_conditions
from giraffe.models import RequestRecord, RequestSpec, RunConfig
from giraffe.runner import _Run


def configuration(**values):
    return RunConfig.model_validate({"targets": [{"name": "local", "model": "test", "url": "http://localhost:1234"}],
                                    "checks": ["correctness"], **values})


def test_shared_default_and_saved_load_override_have_distinct_effective_limits():
    config = configuration(load_test={"request_timeout_seconds": 120,
                                      "schedule": {"duration_seconds": 15, "finish_seconds": 15}})
    assert config.request_timeout_seconds == 300
    assert config.effective_check("correctness")["request_timeout_seconds"] == 300
    assert config.effective_check("context")["request_timeout_seconds"] == 300
    assert config.load_test.request_timeout_seconds == 120
    assert config.load_test.schedule.model_dump() == {"duration_seconds": 15}


@pytest.mark.asyncio
async def test_early_intentional_cancellation_is_not_the_later_overall_deadline():
    config = configuration(checks=["cancellation"], max_duration_seconds=60)
    run = _Run(config, None, None)
    spec = RequestSpec(fixture_id="intent", scenario="client_cancel", check_ids=["cancellation"],
                       messages=[{"role": "user", "content": "hello"}], cancel_after_ms=10)

    class Client:
        async def execute(self, spec, **kwargs):
            await asyncio.sleep(.01)
            return RequestRecord(id="cancelled", target="local", fixture_id=spec.fixture_id,
                                 scenario=spec.scenario, check_ids=spec.check_ids, started_at="now",
                                 status="cancelled", error="intentional cancellation")

    record = await run.request(config.targets[0], Client(), spec)
    assert record.intentional
    assert record.termination_reason == "intentional_cancellation"
    assert record.termination_limit_seconds == .01
    assert "run duration" not in record.error


def test_paged_request_reason_preserves_limit_without_endpoint_error_body():
    index = ConditionRequestIndex()
    index.update({"event": "request_completed", "request_slot": 0, "request": {
        "id": "timeout", "target": "local", "scenario": "correctness_c1", "status": "timeout",
        "error": "private endpoint body", "request_timeout_seconds": 120,
        "termination_reason": "request_timeout", "termination_limit_seconds": 120}})
    page = index.page("correctness_c1", target="local")
    row = page["rows"][0]
    assert row["result_text"] == "Request timeout (120 s)"
    assert "private endpoint body" not in str(page)


def test_planned_matrix_expansion_uses_unique_executor_ids_and_effective_timeouts():
    config = configuration(checks=["load"], load_test={"request_timeout_seconds": 120,
        "workloads": {"combinations": {"selected": ["short_short", "long_long"]}},
        "patterns": {"concurrent": {"levels": [1, 4]}, "constant": {"enabled": True}, "rates": [1, 2]}})
    rows = planned_conditions(config)
    # Two shapes, each with two concurrency and two rate conditions.
    assert len(rows) == len({(row["target"], row["condition_id"]) for row in rows}) == 8
    assert all(row["planned_condition"] and row["condition"]["parameters"]["request_timeout_seconds"] == 120 for row in rows)
    assert not any("preparation" in row["condition_id"] or "calibration" in row["condition_id"] for row in rows)


def test_missing_local_preparation_is_not_an_executed_failed_condition():
    row = condition_aggregate({"condition": {"id": "load"}, "status": "finished", "started": 0,
                               "load_point_result": {"status": "inconclusive", "input_not_calibrated": True},
                               "reason": "Matching tokenizer unavailable"}, verdict="inconclusive")
    assert row["status"] == "skipped"
    assert row["reasons"] == ["Matching tokenizer unavailable"]


def test_later_preparation_does_not_erase_earlier_unavailable_result():
    run = _Run(configuration(), None, None)
    run.preparation("local", "context_preparation", "Preparing context", "unavailable", "Tokenizer unavailable")
    run.preparation("local", "load_preparation", "Preparing load", "completed")
    observation = run.observations["local"]
    assert observation["preparation"]["status"] == "completed"
    assert observation["preparations"]["context_preparation"]["reason"] == "Tokenizer unavailable"
    for index in range(100):
        run.preparation("local", f"load_{index}", "Preparing load", "completed")
    assert len(observation["preparations"]) == 64
