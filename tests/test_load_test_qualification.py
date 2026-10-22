"""Measured shape qualification uses endpoint lengths, never selection estimates."""
from types import SimpleNamespace

from giraffe.load_test import build_spec, input_class_bounds, input_qualification, saved_shape_warnings, test_points as load_points
from tests.test_load_test_phase_a import config, values
from giraffe.models import RunConfig
from giraffe.runner import _Run


def row(tokens, valid=True):
    return SimpleNamespace(input_tokens=tokens, valid=valid, status="completed")


def test_log_scale_boundaries_and_exact_boundary_membership():
    settings = config().load_test
    assert input_class_bounds(settings, "short") == (None, 256)
    assert input_class_bounds(settings, "medium") == (256, 1024)
    assert input_class_bounds(settings, "long") == (1024, None)
    assert not input_qualification(settings, "short", [row(256)])["qualified"]
    assert input_qualification(settings, "medium", [row(256)])["qualified"]
    assert input_qualification(settings, "long", [row(1024)])["qualified"]


def test_outside_tolerance_and_minimum_in_class_counts():
    settings = config().load_test
    records = [row(128)] * 9 + [row(512)]
    assert input_qualification(settings, "short", records)["qualified"]
    assert not input_qualification(settings, "short", records + [row(512)])["qualified"]
    settings.pass_criteria.min_valid_responses = 2
    assert not input_qualification(settings, "long", [row(2048), row(None)])["qualified"]


def test_unknown_counts_remain_unknown_and_never_qualify():
    evidence = input_qualification(config().load_test, "short", [row(None)])
    assert not evidence["qualified"]
    assert evidence["actual_median"] is None
    assert evidence["known_valid"] == 0
    assert evidence["unknown_valid"] == 1
    assert "Input token counts not reported by the endpoint" in evidence["reason"]


def test_saved_pack_plan_warns_for_missing_shape_but_not_available_long_case():
    v = values()
    v.update(request_source="request_pack", request_pack={"version": 1, "cases": [{
        "fixture_id": "tiny", "messages": [{"role": "user", "content": "Say K7P4"}],
        "scorer": "exact", "expected": "K7P4", "max_tokens": 24}]})
    v["load_test"]["workloads"]["combinations"]["selected"] = ["long_long"]
    c = RunConfig.model_validate(v)
    assert saved_shape_warnings(c) == ["No saved question is long enough for Long input; Long → Long results will be inconclusive"]
    c.request_pack.cases[0].messages[0].content = "x" * 8192
    assert saved_shape_warnings(c) == []


def source_config():
    v = values()
    v.update(request_source="request_pack", request_pack={"version": 1, "cases": [
        {"fixture_id": name, "messages": [{"role": "user", "content": content}],
         "scorer": "exact", "expected": "K7P4", "max_tokens": cap}
        for name, content, cap in (("tiny", "Say K7P4", 24), ("large", "x" * 8192, 256))
    ]})
    return RunConfig.model_validate(v)


def test_saved_selection_keeps_questions_and_budgets_for_each_requested_class():
    c = source_config()
    run = _Run(c, None, None)
    p = load_points(c.load_test)[0]
    short = build_spec(run, c.load_test, p, "short_short", {})
    long = build_spec(run, c.load_test, p, "long_long", {})
    assert short.workload["case_id"] == "tiny"
    assert long.workload["case_id"] == "large"
    assert short.messages[0]["content"] == "Say K7P4"
    assert long.messages[0]["content"] == "x" * 8192
    assert short.max_tokens == short.requested_output_tokens == 24
    assert long.max_tokens == long.requested_output_tokens == 256


def test_seeded_weighted_selection_is_repeatable_within_eligible_class():
    c = source_config()
    c.request_pack.cases.append(c.request_pack.cases[0].model_copy(update={"fixture_id": "other", "weight": 2}))
    def sequence():
        run = _Run(c, None, None)
        point = load_points(c.load_test)[0]
        return [build_spec(run, c.load_test, point, "short_short", {}).workload["case_id"] for _ in range(30)]
    assert sequence() == sequence()
    assert set(sequence()) == {"tiny", "other"}


def test_measured_saved_length_overrides_selection_estimate_only():
    from tests.test_load_test_phase_a import record
    c = source_config()
    c.request_pack.cases[1].messages[0].content = "Another tiny question"
    run = _Run(c, None, None)
    known = record()
    known.fixture_id, known.input_tokens = "pack:tiny", 2048
    run.records = [known]
    selected = build_spec(run, c.load_test, load_points(c.load_test)[0], "long_short", {})
    assert selected.workload["case_id"] == "tiny"
    # Changing the selection estimate never manufactures endpoint evidence.
    assert not input_qualification(c.load_test, "long", [row(None)])["qualified"]
