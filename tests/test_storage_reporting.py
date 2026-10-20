import errno
import json

import pytest

from giraffe.models import CheckResult, RequestRecord, RunReport
from giraffe.reporting import save_failure_summary, write_report


def report_with_bodies(policy="all"):
    return RunReport(run_id="saved", started_at="2026-10-01T00:00:00Z",
        finished_at="2026-10-01T00:00:01Z", overall="fail",
        manifest={"config": {"retention": policy}},
        checks=[CheckResult(id="correctness", title="Correctness", target="local", status="fail",
                            summary="One wrong answer", evidence_ids=["wrong"])],
        requests=[RequestRecord(id=name, target="local", fixture_id="tiny", scenario="warm",
            check_ids=["correctness"], started_at="2026-10-01T00:00:00Z", status="completed",
            valid=True, score=name != "wrong", output="界" * 4000, reasoning="thought" * 100,
            output_chars=4000, elapsed_ms=20, output_tokens=100)
            for name in ("wrong", "right")])


def saved_report(tmp_path, report, limit):
    path, html = write_report(report, tmp_path, max_run_storage=limit, html=True)
    return RunReport.model_validate_json(path.read_text()), path, html


def test_limit_drops_nonfailure_bodies_first_and_preserves_original(tmp_path):
    report = report_with_bodies()
    before = report.model_dump_json()
    _, full_path, _ = saved_report(tmp_path, report, 0)
    limit = full_path.stat().st_size - 6000
    saved, path, html = saved_report(tmp_path, report, limit)
    assert path.stat().st_size <= limit
    assert saved.requests[0].output == report.requests[0].output
    assert saved.requests[1].output == saved.requests[1].reasoning == ""
    assert saved.manifest["storage"]["response_bodies_omitted"] == 1
    assert saved.manifest["storage"]["limit_bytes"] == limit
    assert saved.manifest["storage"]["limit_exceeded"] is False
    assert "Storage limit on disk" in html.read_text()
    assert "Response bodies omitted for 1 request" in html.read_text()
    assert report.model_dump_json() == before
    assert saved.overall == report.overall
    assert saved.checks == report.checks
    for original, retained in zip(report.requests, saved.requests):
        for field in ("elapsed_ms", "status", "score", "output_chars", "output_tokens", "check_ids"):
            assert getattr(original, field) == getattr(retained, field)


def test_metadata_is_saved_when_even_bodyless_json_exceeds_limit(tmp_path):
    report = report_with_bodies()
    saved, path, html = saved_report(tmp_path, report, 1)
    assert path.stat().st_size > 1
    assert all(not record.output and not record.reasoning for record in saved.requests)
    assert saved.manifest["storage"]["response_bodies_omitted"] == 2
    assert saved.manifest["storage"]["limit_exceeded"] is True
    assert "Request metadata exceeds" in html.read_text()
    assert len(saved.requests) == 2
    assert saved.checks == report.checks
    assert saved.overall == "fail"


def test_html_storage_limit_uses_binary_units(tmp_path):
    report = report_with_bodies()
    report.manifest["storage"] = {"limit_bytes": 5 * 1024**3, "response_bodies_omitted": 1}
    from giraffe.reporting import _render

    html = _render(report)
    assert "Storage limit on disk:</strong> 5 GiB limit." in html
    assert "Response bodies omitted for 1 request." in html


@pytest.mark.parametrize("policy,expected", [("all", [True, True]), ("failures", [True, False]),
                                             ("none", [False, False])])
def test_storage_disabled_keeps_each_retention_policy(tmp_path, policy, expected):
    saved, _, html = saved_report(tmp_path, report_with_bodies(policy), 0)
    assert [bool(record.output) for record in saved.requests] == expected
    assert saved.manifest["storage"]["response_bodies_omitted"] == 0
    assert saved.manifest["storage"]["limit_exceeded"] is False
    assert "Storage limit on disk:" not in html.read_text()


@pytest.mark.parametrize("limit", [-1, True, 1.5, "256MiB"])
def test_export_rejects_invalid_storage_limits(tmp_path, limit):
    with pytest.raises(ValueError, match="non-negative integer"):
        write_report(report_with_bodies(), tmp_path, max_run_storage=limit)


def test_failure_summary_removes_partial_pass_report_and_reports_error(tmp_path):
    report = report_with_bodies()
    report.overall = "pass"
    (tmp_path / "report.json").write_text('{"overall":"pass"}')
    (tmp_path / "report.html").write_text("pass")
    message = save_failure_summary(report, tmp_path, OSError(errno.ENOSPC, "full"))
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert "disk full" in message and "2 requests was lost" in message
    assert "a summary was saved" in message
    assert summary["state"] == "error" and summary["overall"] == "pass"
    assert summary["error"] == message and summary["request_count"] == 2
    assert summary["checks"][0]["summary"] == "One wrong answer"
    assert not (tmp_path / "report.json").exists()
    assert not (tmp_path / "report.html").exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_failure_summary_handles_no_disk_space_for_summary(tmp_path, monkeypatch):
    def no_space(*args):
        raise OSError(errno.ENOSPC, "full")
    monkeypatch.setattr("giraffe.reporting._atomic_write", no_space)
    message = save_failure_summary(report_with_bodies(), tmp_path, OSError(errno.ENOSPC, "full"))
    assert "no summary could be saved" in message
    assert not (tmp_path / "summary.json").exists()


def test_failure_summary_bounds_large_check_summaries(tmp_path):
    report = report_with_bodies()
    report.checks[0].summary = "long explanation " * 10000
    save_failure_summary(report, tmp_path, OSError(errno.ENOSPC, "full"))
    path = tmp_path / "summary.json"
    summary = json.loads(path.read_text())
    assert path.stat().st_size < 4000
    assert summary["checks"][0]["summary"].endswith("[truncated]")


def test_atomic_export_does_not_follow_temp_symlink(tmp_path):
    elsewhere = tmp_path / "outside"
    elsewhere.write_text("unchanged")
    (tmp_path / "report.json.tmp").symlink_to(elsewhere)
    with pytest.raises(OSError):
        write_report(report_with_bodies(), tmp_path, html=True)
    assert elsewhere.read_text() == "unchanged"
