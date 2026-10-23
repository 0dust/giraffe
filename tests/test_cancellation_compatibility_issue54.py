"""Compare cancellation workloads through the actual local HTTP transport."""

from datetime import datetime
import json

from giraffe.models import RunConfig
from giraffe.reporting import _settings_reasons, compare_baseline, write_report
from giraffe.runner import run_suite
from tests.fake_endpoint import FakeEndpoint


def cancellation_config(endpoint, *, chunks=5, cleanup=5, recovery=False):
    return RunConfig(
        targets=[dict(name="fixture", url=endpoint.url, model="fake-model")],
        checks=["cancellation", "recovery"] if recovery else ["cancellation"],
        samples=2, default_concurrency=1, max_requests=80, max_duration_seconds=10,
        request_timeout_seconds=2, retention="all", sustained_seconds=.015,
        limits={"min_samples": 2, "regression_percent": 1000000},
        test_options={"cancellation": {"backend_verification": "off",
                                      "cancel_after_chunks": chunks, "cleanup_seconds": cleanup}},
    )


def comparison_summary(reports):
    previous, current = reports
    result = compare_baseline(current, previous)
    return result, {
        "configured_cancellation": [report.manifest["effective_checks"]["cancellation"] for report in reports],
        "cancelled_answer_chunks": [[record.answer_chunks for record in report.requests
                                      if record.scenario == "client_cancel"] for report in reports],
        "fixture_hash_equal": previous.manifest["fixture_pack"] == current.manifest["fixture_pack"],
        "incompatible_reasons": _settings_reasons(current, previous),
        "baseline_status": result.baseline["status"],
    }


async def test_http_chunk_cutoff_changes_executed_work_and_is_incompatible(tmp_path):
    reports = []
    with FakeEndpoint(first_token_delay=.002, chunk_delay=.003) as endpoint:
        for chunks in (1, 10):
            report = await run_suite(cancellation_config(endpoint, chunks=chunks))
            write_report(report, tmp_path / f"chunks-{chunks}")
            reports.append(report)
    result, summary = comparison_summary(reports)
    write_report(result, tmp_path / "comparison")
    print(json.dumps(summary))
    assert summary["cancelled_answer_chunks"] == [[1], [10]]
    assert summary["fixture_hash_equal"]
    assert "Recorded effective_checks differs or is missing." in summary["incompatible_reasons"]
    assert summary["baseline_status"] == "inconclusive"
    assert all(not target["comparisons"] for target in result.baseline["targets"])


async def test_http_cleanup_wait_changes_executed_recovery_work_and_is_incompatible(tmp_path):
    reports, waits = [], []
    with FakeEndpoint(first_token_delay=.001, chunk_delay=.001) as endpoint:
        for cleanup in (.03, .15):
            report = await run_suite(cancellation_config(endpoint, cleanup=cleanup, recovery=True))
            write_report(report, tmp_path / f"cleanup-{cleanup}")
            reports.append(report)
            deadline = next(record for record in report.requests if record.scenario == "client_deadline")
            recovery = next(record for record in report.requests
                            if record.scenario in {"recovery_prime", "recovery_baseline"})
            waits.append(datetime.fromisoformat(recovery.started_at).timestamp() -
                         datetime.fromisoformat(deadline.started_at).timestamp() - deadline.elapsed_ms / 1000)
    result, summary = comparison_summary(reports)
    summary["observed_cleanup_wait_seconds"] = waits
    write_report(result, tmp_path / "comparison")
    print(json.dumps(summary))
    assert waits[0] >= .025 and waits[1] >= .145
    assert waits[1] - waits[0] >= .08
    assert summary["fixture_hash_equal"]
    assert "Recorded effective_checks differs or is missing." in summary["incompatible_reasons"]
    assert summary["baseline_status"] == "inconclusive"
