import errno
import json
from pathlib import Path

import pytest

from giraffe.models import CheckResult, RequestRecord, RunReport
from giraffe.reporting import ReportWriteError, html_failure_warning, save_failure_summary, write_report


def tiny_report():
    return RunReport(run_id="complete", started_at="2026-10-20T00:00:00Z",
        finished_at="2026-10-20T00:00:01Z", overall="pass",
        manifest={"config": {"retention": "all"}},
        checks=[CheckResult(id="correctness", title="Correctness", target="local",
                            status="pass", summary="Answer correct", evidence_ids=["answer"])],
        requests=[RequestRecord(id="answer", target="local", fixture_id="tiny", scenario="warm",
            check_ids=["correctness"], started_at="2026-10-20T00:00:00Z", status="completed",
            valid=True, score=True, output="Complete evidence", elapsed_ms=10)])


@pytest.mark.parametrize("code,reason", [(errno.ENOSPC, "disk full"), (errno.EACCES, "permission denied")])
def test_html_write_failure_preserves_complete_json_and_reports_phase(tmp_path, monkeypatch, code, reason):
    report = tiny_report()
    before = report.model_dump_json()
    (tmp_path / "report.html").write_text("stale HTML")
    replace = Path.replace

    def fail_html_replace(self, target):
        if Path(target).name == "report.html":
            raise OSError(code, "write failed")
        return replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_html_replace)
    with pytest.raises(ReportWriteError) as raised:
        write_report(report, tmp_path, html=True)
    error = raised.value
    assert error.failed_file == "report.html" and error.json_complete is True
    assert error.errno == code and error.__cause__.errno == code
    saved = json.loads((tmp_path / "report.json").read_text())
    assert saved["overall"] == "pass"
    assert saved["requests"][0]["output"] == "Complete evidence"
    assert saved["checks"][0]["status"] == "pass"
    assert not (tmp_path / "report.html").exists()
    assert not list(tmp_path.glob("*.tmp"))
    expected = f"HTML report could not be saved: {reason}; report.json is complete"
    assert html_failure_warning(error) == expected
    assert save_failure_summary(report, tmp_path, error) == expected
    assert json.loads((tmp_path / "report.json").read_text()) == saved
    assert not (tmp_path / "summary.json").exists()
    assert report.model_dump_json() == before


def test_json_write_failure_still_saves_summary_with_error_state(tmp_path, monkeypatch):
    report = tiny_report()
    replace = Path.replace

    def fail_json_replace(self, target):
        if Path(target).name == "report.json":
            raise OSError(errno.ENOSPC, "disk full")
        return replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_json_replace)
    with pytest.raises(ReportWriteError) as raised:
        write_report(report, tmp_path, html=True)
    error = raised.value
    assert error.failed_file == "report.json" and error.json_complete is False
    assert error.errno == errno.ENOSPC
    message = save_failure_summary(report, tmp_path, error)
    assert "1 requests was lost; a summary was saved" in message
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["state"] == "error"
    assert not (tmp_path / "report.json").exists()
    assert not (tmp_path / "report.html").exists()
    assert not list(tmp_path.glob("*.tmp"))
