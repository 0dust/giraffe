"""PR #50 review reproductions through the real executor, without sockets."""

import pytest

from giraffe.load_test import execute, result
from giraffe.models import RunConfig
from giraffe.reporting import _load_test_html
from giraffe.runner import _Run
from tests.test_load_test_phase_a import Client, config, values


pytestmark = pytest.mark.usefixtures("local_tokenizer")


def saved(cap=256, *, shape="short_short", interference=False, long_case=False):
    v = values()
    cases = [{
        "fixture_id": "mine", "messages": [{"role": "user", "content": "Say K7P4"}],
        "scorer": "exact", "expected": "K7P4", "max_tokens": cap,
    }]
    if long_case:
        cases.append({**cases[0], "fixture_id": "long", "messages": [
            {"role": "user", "content": "x" * 8192},
        ]})
    v.update(request_source="request_pack", request_pack={"version": 1, "cases": cases})
    v["load_test"]["workloads"]["combinations"]["selected"] = [shape]
    if interference:
        v["load_test"]["workloads"] = {
            "combinations": {"enabled": False}, "interference": {"enabled": True},
        }
        v["load_test"]["patterns"]["concurrent"]["levels"] = [2]
    return RunConfig.model_validate(v)


class MissingUsage(Client):
    async def execute(self, *args, **kwargs):
        record = await super().execute(*args, **kwargs)
        record.input_tokens = record.output_tokens = record.completion_tokens = None
        return record


async def perform(c, client=None):
    # Small internal windows retain the scheduler/scorer path without long tests.
    c.load_test.schedule.duration_seconds = 0.15
    run, client = _Run(c, None, None), client or Client()
    observed = await execute(run, c.targets[0], client)
    rows = [r for r in run.records if r.load_point and r.load_point.get("point_id")]
    return observed, result(run, c.targets[0]), rows, client


async def test_saved_cap_is_sent_scored_and_disclosed_without_replacing_question():
    observed, check, rows, client = await perform(saved())
    group = observed["points"][0]["groups"][0]
    assert check.status == group["status"] == "pass"
    assert observed["highest_accepted"][0]["value"] == 1
    assert rows and all(r.requested_max_tokens == r.requested_output_tokens == 256 for r in rows)
    assert all(r.completion_tokens == 256 for r in rows)
    assert all(s.messages == [{"role": "user", "content": "Say K7P4"}] for s in client.specs)
    note = "Saved question budget 256 tokens used instead of profile Short output cap 64"
    assert [budget["note"] for budget in group["saved_question_budgets"]] == [note]
    assert note in _load_test_html(observed)


async def test_long_combination_with_short_saved_question_cannot_establish_capacity():
    observed, check, rows, _ = await perform(saved(24, shape="long_long"))
    group = observed["points"][0]["groups"][0]
    assert rows and all(r.input_tokens == 2 for r in rows)
    assert check.status == group["status"] == "inconclusive"
    assert observed["highest_accepted"] == []
    assert group["reason"] == "Long input not exercised: actual median 2 tokens, target 2048 (Long ≥ 1024)"
    assert group["input_qualification"]["in_class"] == 0
    assert group["correct_percent"] == 100
    assert group["completion_p50_ms"] > 0
    # A correct early-finishing answer is not an output-cap violation.
    assert all(r.completion_tokens == r.requested_max_tokens == 24 for r in rows)


async def test_interference_with_unexercised_long_probe_is_inconclusive():
    observed, check, rows, _ = await perform(saved(24, interference=True), Client((0.025,)))
    point = observed["points"][0]
    probes = [r for r in rows if r.load_point["role"] == "probe"]
    assert probes and all(r.input_tokens == 2 and r.requested_input_tokens == 2048 for r in probes)
    assert check.status == point["status"] == "inconclusive"
    assert point["reason"].startswith("long-input probe not exercised: Long input not exercised")
    assert observed["highest_accepted"] == []
    qualification = point["interference"]["input_qualification"]
    assert point["interference"]["triggered"]
    assert all(point["interference"][phase]["matched_count"] >= 1 for phase in ("baseline", "probe"))
    assert qualification["baseline"]["qualified"]
    assert qualification["probe"]["qualified"]
    assert not qualification["long_probe"]["qualified"]


async def test_missing_usage_keeps_metrics_but_cannot_establish_capacity():
    observed, check, rows, _ = await perform(config(), MissingUsage())
    group = observed["points"][0]["groups"][0]
    assert check.status == group["status"] == "inconclusive"
    assert observed["highest_accepted"] == []
    assert group["reason"] == (
        "Input token counts not reported by the endpoint; timing and correctness are shown but capacity is not established"
    )
    assert [calibration["status"] for calibration in observed["calibrations"]] == ["calibrated"]
    assert rows and all(r.input_tokens is None and r.output_tokens is None for r in rows)
    assert group["input_qualification"]["actual_median"] is None
    assert group["input_qualification"]["unknown_valid"] == group["valid"]
    assert group["correct_percent"] == 100
    assert group["first_answer_p50_ms"] == 1
    assert group["completion_p50_ms"] > 0


async def test_builtin_calibrated_workload_still_establishes_accepted_capacity():
    observed, check, _, _ = await perform(config())
    assert check.status == "pass"
    assert observed["highest_accepted"][0]["value"] == 1
    assert [calibration["status"] for calibration in observed["calibrations"]] == ["calibrated"]
    assert observed["points"][0]["groups"][0]["input_qualification"]["qualified"]


async def test_saved_pack_uses_genuinely_long_unchanged_case_for_long_workload():
    observed, check, rows, client = await perform(saved(24, shape="long_long", long_case=True))
    group = observed["points"][0]["groups"][0]
    assert check.status == group["status"] == "pass"
    assert observed["highest_accepted"][0]["value"] == 1
    assert rows and all(r.input_tokens == 2048 for r in rows)
    assert all(s.messages == [{"role": "user", "content": "x" * 8192}] for s in client.specs)
    assert group["input_qualification"]["qualified"]
    assert set(group["case_coverage"]) == {"long"}


async def test_saved_pack_uses_genuinely_long_case_for_interference_probe():
    observed, check, rows, _ = await perform(saved(24, interference=True, long_case=True), Client((0.025,)))
    assert check.status == "pass"
    probes = [r for r in rows if r.load_point["role"] == "probe"]
    shorts = [r for r in rows if r.load_point["role"] == "short_stream"]
    assert probes and all(r.input_tokens == 2048 and r.workload["case_id"] == "long" for r in probes)
    assert shorts and all(r.input_tokens == 2 and r.workload["case_id"] == "mine" for r in shorts)
    assert all(q["qualified"] for q in observed["points"][0]["interference"]["input_qualification"].values())


async def test_stress_output_coverage_keeps_profile_floor_for_smaller_saved_budget():
    c = saved(24, long_case=True)
    c.load_test.workloads.combinations.enabled = False
    c.load_test.workloads.stress.enabled = True
    c.load_test.workloads.stress.cases = ["long_long"]
    c.load_test.workloads.stress.min_output_percent = 80
    observed, check, rows, _ = await perform(c)
    group = observed["points"][0]["groups"][0]
    assert check.status == "inconclusive"
    assert rows and all(r.requested_output_tokens == r.requested_max_tokens == 24 for r in rows)
    assert group["lengths"]["output_cap"] == 512  # Profile target stays visible separately.
    assert group["coverage"]["threshold_tokens"] == 410
    assert group["coverage"]["output_covered"] == 0
    assert group["reason"] == "Saved question budget 24 tokens cannot reach the required long-output stress coverage of 410 tokens (80% of Long output cap 512)"
    assert observed["highest_accepted"] == []


async def test_short_long_stress_saved_budget_warning_plan_and_report():
    from giraffe.planning import request_plan

    c = saved(24)
    c.load_test.workloads.combinations.enabled = False
    c.load_test.workloads.stress.enabled = True
    c.load_test.workloads.stress.cases = ["short_long"]
    plan = request_plan(c)
    assert any("budget 24 tokens cannot reach" in text and "410 tokens" in text for text in plan["warnings"])
    assert any("410" in text and "512" in text for text in plan["load_test"]["execution_summary"])
    observed, check, rows, _ = await perform(c)
    group = observed["points"][0]["groups"][0]
    assert check.status == "inconclusive" and observed["highest_accepted"] == []
    assert group["coverage"]["threshold_tokens"] == 410
    assert all(r.requested_max_tokens == 24 for r in rows)
    html = _load_test_html(observed)
    assert "Saved question budget 24" in html
    assert "Required long-output stress coverage: 410 tokens (80% of Long output cap 512)" in html


class GeneratedOutput(Client):
    def __init__(self, tokens):
        super().__init__()
        self.tokens = tokens

    async def execute(self, spec, **kwargs):
        record = await super().execute(spec, **kwargs)
        if not spec.scenario.startswith("load_calibration"):
            record.output_tokens = record.completion_tokens = self.tokens
        return record


async def test_stress_saved_budget_reaching_configured_floor_qualifies():
    c = saved(450)
    c.load_test.workloads.combinations.enabled = False
    c.load_test.workloads.stress.enabled = True
    c.load_test.workloads.stress.cases = ["short_long"]
    observed, check, rows, _ = await perform(c, GeneratedOutput(410))
    group = observed["points"][0]["groups"][0]
    assert check.status == "pass" and observed["highest_accepted"][0]["value"] == 1
    assert group["coverage"]["threshold_tokens"] == 410
    assert group["coverage"]["output_covered"] == group["valid"]
    assert all(r.requested_max_tokens == r.requested_output_tokens == 450 and r.output_tokens == 410 for r in rows)


async def test_stress_insufficient_budget_reason_keeps_unexercised_input_evidence():
    c = saved(24)
    c.load_test.workloads.combinations.enabled = False
    c.load_test.workloads.stress.enabled = True
    c.load_test.workloads.stress.cases = ["long_long"]
    observed, check, _, _ = await perform(c)
    group = observed["points"][0]["groups"][0]
    assert check.status == "inconclusive" and observed["highest_accepted"] == []
    assert "budget 24 tokens cannot reach" in group["reason"]
    assert "Long input not exercised" in group["input_qualification"]["reason"]


async def test_stress_saved_budget_violation_is_endpoint_failure():
    c = saved(24)
    c.load_test.workloads.combinations.enabled = False
    c.load_test.workloads.stress.enabled = True
    c.load_test.workloads.stress.cases = ["short_long"]
    observed, check, _, _ = await perform(c, GeneratedOutput(25))
    group = observed["points"][0]["groups"][0]
    assert check.status == "fail" and group["reason_code"] == "output_cap"
    assert group["coverage"]["threshold_tokens"] == 410
    assert observed["highest_accepted"] == []


async def test_builtin_long_output_stress_keeps_configured_floor():
    c = config()
    c.load_test.workloads.combinations.enabled = False
    c.load_test.workloads.stress.enabled = True
    c.load_test.workloads.stress.cases = ["short_long", "long_long"]
    observed, check, rows, _ = await perform(c, GeneratedOutput(410))
    assert check.status == "pass"
    assert len(observed["highest_accepted"]) == 2
    assert all(group["coverage"]["threshold_tokens"] == 410 and group["coverage"]["output_covered"] == group["valid"]
               for point in observed["points"] for group in point["groups"])
    assert rows and all(r.requested_max_tokens == r.requested_output_tokens == 512 for r in rows)


async def test_mixed_traffic_qualifies_each_shape_independently():
    c = saved(24)
    c.load_test.workloads.combinations.enabled = False
    c.load_test.workloads.mixed.enabled = True
    c.load_test.workloads.mixed.selected = ["short_short", "long_short"]
    observed, check, _, _ = await perform(c)
    groups = {group["shape"]: group for group in observed["points"][0]["groups"]}
    assert groups["short_short"]["dispatched"] > 0
    assert groups["long_short"]["dispatched"] > 0
    assert groups["short_short"]["status"] == "pass"
    assert groups["short_short"]["input_qualification"]["qualified"]
    assert groups["long_short"]["status"] == "inconclusive"
    assert not groups["long_short"]["input_qualification"]["qualified"]
    assert check.status == "inconclusive"
    assert observed["highest_accepted"] == []


# Fake inference clients in this module use the matching local tokenizer.
