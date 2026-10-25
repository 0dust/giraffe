import json

import pytest

from giraffe.invocation import cli_invocation
from giraffe.models import RunReport
from giraffe.reporting import reproduction_export


def test_invocation_preserves_order_overrides_and_environment_references(monkeypatch):
    monkeypatch.setenv("MY_KEY", "never-copy-this-value")
    argv = ["run", "--config", "run.yaml", "--max-requests=20", "--max-requests", "30",
            "--api-key-env", "MY_KEY", "--skip-preflight"]
    value = cli_invocation(argv)
    assert value == {"source": "cli", "argv": argv}
    assert "never-copy-this-value" not in json.dumps(value)


def test_invocation_redacts_unknown_values_credentials_and_request_content():
    argv = ["run", "--url", "https://user:password@example.com/v1?key=private",
            "--proxy=https://user:password@proxy.test", "--api-key", "opaque-secret",
            "--request-options={\"messages\":[\"private question\"]}", "private positional",
            "--api-k", "another-secret"]
    value = cli_invocation(argv)
    text = json.dumps(value)
    for secret in ["password", "private", "opaque-secret", "another-secret"]:
        assert secret not in text
    assert value["argv"][2] == "https://example.com/v1[redacted]"
    assert value["argv"][4:6] == ["--api-key", "[redacted]"]


@pytest.mark.parametrize("source", ["web_api", "python_api", "unknown"])
def test_reproduction_preserves_non_cli_origin_without_inventing_command(source):
    report = RunReport(run_id="r", started_at="now", finished_at="now", overall="pass",
                       manifest={"invocation": {"source": source, "argv": None}})
    assert reproduction_export(report)["invocation"] == {"source": source, "argv": None}


def test_legacy_report_has_unknown_origin_and_export_sanitizes_imported_command():
    report = RunReport(run_id="r", started_at="now", finished_at="now", overall="pass",
                       manifest={})
    assert reproduction_export(report)["invocation"] == {"source": "unknown", "argv": None}
    report.manifest["invocation"] = {"source": "cli", "argv": ["run", "--api-key=secret"]}
    assert reproduction_export(report)["invocation"]["argv"] == ["run", "--api-key=[redacted]"]


@pytest.mark.parametrize("command", ["run", "cold-start", "try-example"])
def test_command_identity_is_preserved(command):
    assert cli_invocation([command])["argv"] == [command]


def test_report_files_redact_imported_invocation(tmp_path):
    from giraffe.reporting import write_report
    report = RunReport(run_id="r", started_at="now", finished_at="now", overall="pass",
                       manifest={"invocation": {"source": "cli", "argv": ["run", "--api-key=secret-value"]}})
    write_report(report, tmp_path, html=True)
    saved = json.loads((tmp_path / "report.json").read_text())
    assert saved["manifest"]["invocation"]["argv"] == ["run", "--api-key=[redacted]"]
    assert "secret-value" not in (tmp_path / "report.html").read_text()
