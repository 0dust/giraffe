import importlib.util
import json
from pathlib import Path

import pytest

from giraffe.models import CheckResult, RequestRecord, RunReport
from giraffe.reporting import _json_size, _retained, write_report


@pytest.fixture
def legacy_export(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("legacy_memory_measure", scripts / "measure_memory.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def report_fixture(policy):
    excerpt = 'Incorrect. Expected: "expected"; received: "wrong".'
    records = []
    for index in range(3):
        records.append(RequestRecord.model_validate({
            "id": f"r{index}", "target": "local", "fixture_id": "fixture", "scenario": "warm",
            "check_ids": ["correctness"], "started_at": "2026-10-20T00:00:00Z",
            "status": "completed", "valid": True, "score": index != 0,
            "score_message": excerpt, "output": "界\nresponse " * 300,
            "reasoning": "thought " * 50, "tool_calls": [{"name": "lookup", "arguments": "{}"}],
            "assistant_message": {"role": "assistant", "content": "retained answer"},
            "tool_conversation": {"final_content": "answer", "calls": [{"result": "private", "id": "call"}]},
            "evaluation": {"messages": [{"role": "user", "content": "prompt"}], "scorer": "exact",
                           "expected": None, "expected_provided": True},
            "logprob_evidence": {"status": "available", "reason": "measured", "requested_top_logprobs": 1,
                "tokens": [{"token": "a", "bytes": [97], "logprob": -.2, "position": 0,
                            "top_logprobs": [{"token": "b", "bytes": [98], "logprob": -1.2}]}]},
            "judgement": {"status": "pass", "votes": [{"judge": "judge", "model": "model",
                "url": "http://localhost:8000", "status": "completed", "verdict": True, "reason": "rationale"}]},
        }))
    return RunReport(run_id="memory", started_at="2026-10-20T00:00:00Z",
        finished_at="2026-10-20T00:00:01Z", overall="fail", manifest={"config": {"retention": policy}},
        requests=records, checks=[CheckResult(id="correctness", target="local", title="Correctness",
            status="fail", summary=excerpt, evidence_ids=["r0"], metrics={"scenarios": {
                "warm": {"title": "Warm", "status": "fail", "summary": excerpt, "count": 3},
                "unchanged": {"title": "Empty", "status": "inconclusive", "summary": "No excerpt", "count": 0}}})])


@pytest.mark.parametrize("policy", ["all", "failures", "none"])
@pytest.mark.parametrize("limit", [0, 1, 19000])
def test_streamed_export_matches_legacy_json_and_does_not_mutate_input(tmp_path, legacy_export, policy, limit):
    report = report_fixture(policy)
    before = report.model_dump_json()
    expected = json.loads(legacy_export._old_storage_limited(legacy_export._old_retained(report), limit))
    json_path, _ = write_report(report, tmp_path, max_run_storage=limit)
    saved = json.loads(json_path.read_text())
    assert saved == expected
    assert saved["overall"] == report.overall
    assert report.model_dump_json() == before
    assert _json_size(RunReport.model_validate(saved)) == json_path.stat().st_size


def test_retention_shares_unchanged_records_but_copies_modified_nested_evidence():
    report = report_fixture("all")
    retained = _retained(report)
    assert retained.requests[0] is report.requests[0]
    assert retained.checks is report.checks
    assert retained.manifest is not report.manifest
    report.manifest["config"]["retention"] = "none"
    retained = _retained(report)
    assert retained.requests[0] is not report.requests[0]
    assert retained.requests[0].tool_conversation is not report.requests[0].tool_conversation
    assert retained.requests[0].tool_conversation["calls"][0] is not report.requests[0].tool_conversation["calls"][0]
    assert retained.checks[0].metrics is not report.checks[0].metrics
    assert retained.checks[0].metrics["scenarios"]["warm"] is not report.checks[0].metrics["scenarios"]["warm"]
    assert retained.checks[0].metrics["scenarios"]["unchanged"] is report.checks[0].metrics["scenarios"]["unchanged"]
    assert report.requests[0].judgement.votes[0].reason == "rationale"
    assert report.requests[0].logprob_evidence.tokens
    assert report.requests[0].evaluation.messages
    assert 'Expected: "expected"' in report.checks[0].metrics["scenarios"]["warm"]["summary"]


def test_streamed_export_never_serializes_all_requests_together(tmp_path, monkeypatch):
    report = report_fixture("all")
    original = RunReport.model_dump_json

    def metadata_only(self, *args, **kwargs):
        assert kwargs.get("exclude") == {"requests"}
        return original(self, *args, **kwargs)

    monkeypatch.setattr(RunReport, "model_dump_json", metadata_only)
    json_path, _ = write_report(report, tmp_path, max_run_storage=1)
    assert len(json.loads(json_path.read_text())["requests"]) == 3


def test_empty_requests_streams_valid_identical_json(tmp_path, legacy_export):
    report = report_fixture("none")
    report.requests = []
    expected = json.loads(legacy_export._old_storage_limited(legacy_export._old_retained(report), 0))
    json_path, _ = write_report(report, tmp_path, max_run_storage=0)
    assert json.loads(json_path.read_text()) == expected


def test_comparison_preserves_memory_stop_inconclusive_with_blocked_checks():
    from giraffe.reporting import compare_baseline

    report = report_fixture("all")
    report.checks[0].status = "blocked"
    report.overall = "inconclusive"
    report.abort_reason = "memory limit reached: process RAM exceeded the limit"
    report.observations["resources"] = {"memory_limit_reached": True,
        "memory_interrupted_checks": [{"target": "local", "id": "correctness"}]}
    baseline = report.model_copy(deep=True)
    result = compare_baseline(report, baseline)
    assert result.checks[0].status == "blocked"
    assert result.overall == "inconclusive"


def test_comparison_keeps_completed_results_when_memory_limit_only_warns():
    from giraffe.reporting import compare_baseline
    from tests.test_reporting import make_report

    report = make_report()
    report.observations["resources"] = {"memory_limit_reached": True,
        "memory_limit_stopped_run": False, "memory_interrupted_checks": []}
    result = compare_baseline(report, make_report("baseline"))
    assert result.overall == "pass"
    assert all(check.status == "pass" for check in result.checks)
    assert result.abort_reason is None
