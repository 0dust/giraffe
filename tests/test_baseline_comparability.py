"""Machine-readable comparability preserves the existing human explanations."""

import pytest

from giraffe import states
from giraffe.reporting import compare_baseline
from tests.test_reporting import make_report


def test_identical_runs_are_comparable():
    current = make_report()
    compared = compare_baseline(current, current)
    assert compared.baseline["comparability"] == "comparable"
    target = compared.baseline["targets"][0]
    assert target["comparability"] == "comparable"
    assert target["reasons"] == target["reason_details"] == []


@pytest.mark.parametrize("previous_version", [None, 1, 2])
def test_outcome_counting_version_requires_matching_baseline(previous_version):
    current = make_report()
    current.manifest["outcome_counting_version"] = 2
    previous = current.model_copy(deep=True)
    if previous_version is None:
        del previous.manifest["outcome_counting_version"]
    else:
        previous.manifest["outcome_counting_version"] = previous_version
    result = compare_baseline(current, previous)
    target = result.baseline["targets"][0]
    details = [detail for detail in target["reason_details"]
               if detail["code"] == "outcome_counting_differs"]
    if previous_version == 2:
        assert details == []
        assert result.baseline["comparability"] == target["comparability"] == "comparable"
    else:
        assert details == [{"code": "outcome_counting_differs",
                            "text": "Request outcome counting rules differ; rerun the baseline."}]
        assert result.baseline["comparability"] == target["comparability"] == "not_comparable"


@pytest.mark.usefixtures("local_tokenizer")
def test_access_only_comparison_retains_outcome_counting_reason():
    from giraffe.models import CheckResult
    from tests.test_gateway_access import config
    from tests.test_web import report

    current = report(config())
    current.checks = [CheckResult(id="gateway_access", target="proxy", title="Access", status="pass", summary="Observed")]
    current.manifest.update(gateway_access_workload_version=1, outcome_counting_version=2)
    previous = current.model_copy(deep=True)
    del previous.manifest["outcome_counting_version"]
    result = compare_baseline(current, previous)
    target = result.baseline["targets"][0]
    assert target["reasons"] == ["Request outcome counting rules differ; rerun the baseline."]
    assert target["reason_details"][0]["code"] == "outcome_counting_differs"
    assert target["comparability"] == result.baseline["comparability"] == "not_comparable"


@pytest.mark.parametrize("codes,expected", [
    ([], "comparable"), (["setting_missing"], "unknown"),
    (["setting_unknown", "comparison_unavailable"], "unknown"),
    (["setting_missing", "schema_differs"], "not_comparable"),
    (["setting_differs"], "not_comparable"),
    (["target_unknown"], "unknown"), (["judge_incomplete"], "unknown"),
    (["user_delivery_zero_baseline"], "unknown"),
])
def test_reason_verdict_derivation(codes, expected):
    assert states.baseline_comparability(codes) == expected


@pytest.mark.parametrize("verdicts,expected", [
    ([], "comparable"), (["comparable", "comparable"], "comparable"),
    (["comparable", "unknown"], "unknown"),
    (["unknown", "not_comparable", "comparable"], "not_comparable"),
])
def test_worst_target_verdict(verdicts, expected):
    assert states.worst_baseline_comparability(verdicts) == expected


@pytest.mark.parametrize("change,code,verdict,text,side", [
    ("schema", "schema_differs", "not_comparable", "Report schema versions differ.", None),
    ("abort", "run_aborted", "not_comparable",
     "Current run was aborted; incomplete traffic cannot establish a regression.", "current"),
    ("setting", "setting_differs", "not_comparable", "Run setting overlap_models differs.", None),
    ("target", "target_absent", "not_comparable", "Target is absent from the baseline.", "baseline"),
    ("pack", "request_pack_unknown", "unknown",
     "Request pack identity is missing; comparability is unknown.", "baseline"),
])
def test_real_comparison_reasons(change, code, verdict, text, side):
    current = make_report()
    if change == "pack":
        current.manifest["config"].update(checks=["correctness"], request_source="request_pack", request_pack={
            "version": 1, "cases": [{"fixture_id": "answer", "messages": [{"role": "user", "content": "Return 42"}],
                                     "scorer": "exact", "expected": "42"}]})
        current.manifest["request_pack"] = {"sha256": "same-pack"}
    previous = current.model_copy(deep=True)
    if change == "schema":
        current.schema_version = "unsupported"
    elif change == "abort":
        current.abort_reason = states.STOP_CANCELLED_BY_USER
    elif change == "setting":
        current.manifest["config"]["overlap_models"] = True
    elif change == "target":
        previous.manifest["config"]["targets"][0]["name"] = "other"
    elif change == "pack":
        del previous.manifest["request_pack"]
    before = current.model_dump_json(), previous.model_dump_json()
    result = compare_baseline(current, previous)
    target = result.baseline["targets"][0]
    assert target["reasons"] == [text]
    detail, = target["reason_details"]
    assert detail["text"] == text and detail["code"] == code
    assert detail.get("side") == side
    assert result.baseline["comparability"] == target["comparability"] == verdict
    assert (current.model_dump_json(), previous.model_dump_json()) == before
    if change == "setting":
        assert list(detail) == ["code", "key", "current", "baseline", "text"]
        assert detail == {"code": "setting_differs", "key": "overlap_models", "current": True, "baseline": False, "text": text}


def test_top_level_uses_worst_real_target():
    current = make_report()
    other = dict(current.manifest["config"]["targets"][0], name="other")
    current.manifest["config"]["targets"].append(other)
    previous = current.model_copy(deep=True)
    previous.manifest["config"]["targets"].pop()
    result = compare_baseline(current, previous)
    assert [t["comparability"] for t in result.baseline["targets"]] == ["comparable", "not_comparable"]
    assert result.baseline["comparability"] == "not_comparable"


@pytest.mark.usefixtures("local_tokenizer")
def test_gateway_access_transport_reason():
    from giraffe.models import CheckResult
    from tests.test_gateway_access import config
    from tests.test_web import report

    current = report(config())
    current.checks = [CheckResult(id="gateway_access", target="proxy", title="Access", status="pass", summary="Observed")]
    previous = current.model_copy(deep=True)
    previous.manifest["config"]["proxy"] = "http://forward-proxy"
    result = compare_baseline(current, previous)
    target = result.baseline["targets"][0]
    text = "Gateway access transport settings differ."
    assert target["reasons"] == [text]
    assert target["reason_details"] == [{"code": "gateway_access_transport_differs", "text": text}]
    assert result.baseline["comparability"] == target["comparability"] == "not_comparable"


@pytest.mark.usefixtures("local_tokenizer")
def test_load_exclusion_reason():
    from tests.test_load_test_baseline import concurrent_report

    previous = concurrent_report()
    previous.checks[0].metrics = {"points": []}
    current = previous.model_copy(deep=True)
    current.requests[0].requested_max_tokens += 1
    result = compare_baseline(current, previous)
    target = result.baseline["targets"][0]
    text, = target["reasons"]
    assert text == "load/load_primary_combinations_short_short_concurrent_c1.short_short: Requested work differs: requested output caps."
    assert target["reason_details"] == [{"code": "load_work_excluded", "text": text}]
    assert result.baseline["comparability"] == target["comparability"] == "not_comparable"


def test_missing_detail_is_guarded_and_keeps_production_fallback(monkeypatch, caplog):
    from giraffe.reporting import _comparison_reason_details

    with pytest.raises(AssertionError, match="Undeclared baseline reason code"):
        _comparison_reason_details(["New reason."], {})
    monkeypatch.setattr(states, "STRICT_TRANSITIONS", False)
    with caplog.at_level("WARNING", logger="giraffe.states"):
        assert _comparison_reason_details(["New reason."], {}) == [
            {"code": "comparison_unavailable", "text": "New reason."}]
    assert "New reason." in caplog.text


@pytest.mark.parametrize("missing", [True, False, "tokens"])
def test_interference_missing_evidence_and_changed_work_have_distinct_verdicts(missing):
    from giraffe.multi_user_metrics import summarize
    from tests.test_multi_user_reporting import user_report
    from tests.test_multi_user_scenarios import matched_observation

    previous = user_report()
    source, records = matched_observation()
    previous.manifest["config"]["multi_user"].update(source["settings"])
    for window in source["windows"]:
        for item in records:
            if item.workload["window_id"] == window["id"]:
                item.target = "local"
                item.workload.update(scenario=window["scenario"], trigger=window["trigger"],
                                     heavy_profile=window["heavy_profile"], population=window["population"])
    previous.requests = records
    previous.checks[0].metrics["multi_user"] = summarize(source, records)
    current = previous.model_copy(deep=True)
    if missing == "tokens":
        current.requests[-1].output_tokens = None
        text = "Multi-user user-01 at 2 users: Matched heavy work differs or lacks observed token evidence."
        code, verdict = "user_delivery_incomplete", "unknown"
    elif missing:
        current.requests.pop(0)
        text = "Multi-user user-01 at 2 users: Matched control request evidence is incomplete."
        code, verdict = "user_delivery_incomplete", "unknown"
    else:
        current.requests[-1].input_tokens *= 2
        text = "Multi-user user-01 at 2 users: Matched heavy work differs or lacks observed token evidence."
        code, verdict = "user_delivery_differs", "not_comparable"
    target = compare_baseline(current, previous).baseline["targets"][0]
    assert text in target["reasons"]
    matching = [detail for detail in target["reason_details"] if detail["text"] == text]
    assert matching and all(detail["code"] == code for detail in matching)
    assert target["comparability"] == verdict
    if missing:
        assert all(detail["side"] == "current" for detail in matching)


@pytest.mark.parametrize("legacy_side", ["current", "baseline"])
def test_previous_workload_format_records_its_actual_side(legacy_side):
    from giraffe.models import CheckResult

    current = make_report()
    current.checks.append(CheckResult(id="concurrency", target="local", title="Concurrent workloads",
                                      status="pass", summary="Measured", metrics={"load_types": {}}))
    previous = current.model_copy(deep=True)
    (current if legacy_side == "current" else previous).checks[-1].metrics = {}
    target = compare_baseline(current, previous).baseline["targets"][0]
    assert target["reasons"][0] == "baseline uses the previous Concurrent workloads format"
    assert target["reason_details"][0] == {
        "code": "workload_format_changed", "side": legacy_side,
        "text": "baseline uses the previous Concurrent workloads format"}
    assert target["comparability"] == "not_comparable"


def test_repeated_user_reason_preserves_each_missing_side():
    from copy import deepcopy
    from tests.test_multi_user_reporting import user_report

    current = user_report()
    windows = current.checks[0].metrics["multi_user"]["windows"]
    other = deepcopy(windows[0])
    other.update(id="other-window", scenario="repeated_conversations")
    windows.append(other)
    clones = []
    for record in current.requests:
        clone = record.model_copy(deep=True, update={"id": "other-" + record.id})
        clone.workload.update(window_id="other-window", scenario="repeated_conversations")
        clones.append(clone)
    current.requests.extend(clones)
    for user in other["users"]:
        user["request_ids"] = ["other-" + request_id for request_id in user["request_ids"]]
    previous = current.model_copy(deep=True)
    # Both scenarios emit identical prose, but a different run lacks the coverage.
    windows[0]["users"][0]["coverage"]["status"] = "incomplete"
    previous.checks[0].metrics["multi_user"]["windows"][1]["users"][0]["coverage"]["status"] = "incomplete"
    target = compare_baseline(current, previous).baseline["targets"][0]
    text = "Multi-user user-01 at 5 users: Missing or incomplete shared-window coverage."
    matching = [detail for detail in target["reason_details"] if detail["text"] == text]
    assert matching == [
        {"code": "user_delivery_incomplete", "text": text, "side": "baseline"},
        {"code": "user_delivery_incomplete", "text": text, "side": "current"}]
    assert target["comparability"] == "unknown"


@pytest.mark.parametrize("missing", [True, False, "both_samples"])
def test_fairness_missing_exposure_and_changed_overlap(missing):
    from giraffe.multi_user_metrics import summarize
    from tests.test_multi_user_reporting import user_report

    previous = user_report()
    settings = previous.manifest["config"]["multi_user"]
    settings["pass_criteria"]["max_throughput_ratio"] = 1.1
    for request in previous.requests:
        request.stream = True
        request.answer_chunks = request.answer_reads = 3
        request.last_output_ms = 90
    source = previous.observations["targets"]["local"]["multi_user"]
    source["settings"] = settings
    previous.checks[0].metrics["multi_user"] = summarize(source, previous.requests)
    current = previous.model_copy(deep=True)
    sample = current.checks[0].metrics["multi_user"]["windows"][0]["fairness"]["users"][0]
    if missing == "both_samples":
        sample["valid_samples"] += 1
        previous.checks[0].metrics["multi_user"]["windows"][0]["fairness"]["users"][0]["valid_samples"] += 1
    else:
        sample["active_exposure_ms"] = None if missing else sample["active_exposure_ms"] / 2
    target = compare_baseline(current, previous).baseline["targets"][0]
    text = "Multi-user fairness at 5 users: Observed fairness work or overlap differs or lacks evidence."
    assert target["reasons"] == [text]
    detail, = target["reason_details"]
    assert detail["code"] == ("user_fairness_unknown" if missing else "user_fairness_differs")
    assert detail.get("side") == ("current" if missing is True else None)
    assert target["comparability"] == ("unknown" if missing else "not_comparable")


@pytest.mark.parametrize("missing_side", [None, "current", "baseline", "both"])
def test_target_identity_difference_and_missing_keys(missing_side):
    current = make_report()
    previous = current.model_copy(deep=True)
    key = "parent" if missing_side else "model"
    if missing_side:
        for side, run in (("current", current), ("baseline", previous)):
            if missing_side in {side, "both"}:
                del run.manifest["config"]["targets"][0][key]
    else:
        current.manifest["config"]["targets"][0][key] = "different-model"
    result = compare_baseline(current, previous)
    target = result.baseline["targets"][0]
    text = f"Target {key} differs or is unknown."
    assert target["reasons"] == [text]
    detail, = target["reason_details"]
    assert detail["code"] == ("target_unknown" if missing_side else "target_differs")
    assert detail["text"] == text and detail["key"] == key
    if missing_side:
        expected = ["code", "key", "current", "baseline", "text"]
        if missing_side != "both":
            expected.append("side")
        assert detail.get("side") == (None if missing_side == "both" else missing_side)
    else:
        expected = ["code", "key", "baseline", "current", "text"]
        assert detail["baseline"] == "test" and detail["current"] == "different-model"
    assert list(detail) == expected
    assert result.baseline["comparability"] == target["comparability"] == (
        "unknown" if missing_side else "not_comparable")


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("missing_side", ["current", "baseline"])
def test_load_exclusion_missing_samples_uses_the_compared_group(missing_side):
    from tests.test_load_test_baseline import concurrent_report

    previous = concurrent_report()
    previous.checks[0].metrics = {"points": []}
    current = previous.model_copy(deep=True)
    run = current if missing_side == "current" else previous
    scenario = run.requests[0].scenario
    run.requests = [record for record in run.requests if record.scenario != scenario]
    result = compare_baseline(current, previous)
    target = result.baseline["targets"][0]
    text = "load/load_primary_combinations_short_short_concurrent_c1.short_short: Request samples are absent from one run."
    assert target["reasons"] == [text]
    assert target["load_exclusions"][0]["reason"] == "Request samples are absent from one run."
    assert target["reason_details"] == [{"code": "load_samples_missing", "text": text, "side": missing_side}]
    assert result.baseline["comparability"] == target["comparability"] == "unknown"


def test_zero_baseline_user_delivery_is_unknown():
    from tests.test_multi_user_reporting import user_report

    current = user_report()
    previous = current.model_copy(deep=True)
    previous.checks[0].metrics["multi_user"]["windows"][0]["users"][0]["output_tokens_per_second"] = 0
    target = compare_baseline(current, previous).baseline["targets"][0]
    text = "Multi-user user-01 has zero baseline delivery."
    assert target["reasons"] == [text]
    assert target["reason_details"] == [{"code": "user_delivery_zero_baseline", "text": text, "side": "baseline"}]
    assert target["comparability"] == "unknown"
