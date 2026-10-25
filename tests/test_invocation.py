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


@pytest.mark.parametrize("flag", ["--url", "--metrics-url", "--host-metrics-url", "--proxy"])
@pytest.mark.parametrize("equals", [False, True])
@pytest.mark.parametrize("url,expected", [
    ("https://alice:'private-value@[::1]:8443/v1?tenant=hidden-query#hidden-fragment",
     "https://[::1]:8443/v1[redacted]"),
    ("https://alice:%27private-value@example.com/v1", "https://example.com/v1[redacted]"),
    ("https://'private-value@example.com/v1", "https://example.com/v1[redacted]"),
    ("https://alice:'private-value@[invalid/v1", "[redacted]"),
])
def test_imported_url_arguments_are_redacted_at_every_export_boundary(tmp_path, flag, equals, url, expected):
    from giraffe.reporting import write_report

    argv = ["run", *([f"{flag}={url}"] if equals else [flag, url])]
    safe_argv = ["run", *([f"{flag}={expected}"] if equals else [flag, expected])]
    report = RunReport(run_id="r", started_at="now", finished_at="now", overall="pass",
                      manifest={"invocation": {"source": "cli", "argv": argv}})
    exported = reproduction_export(report)
    write_report(report, tmp_path, html=True)
    saved = json.loads((tmp_path / "report.json").read_text())
    assert exported["invocation"]["argv"] == saved["manifest"]["invocation"]["argv"] == safe_argv
    for text in (json.dumps(saved), (tmp_path / "report.html").read_text(), json.dumps(exported)):
        for private in ("alice", "private-value", "hidden-query", "hidden-fragment"):
            assert private not in text


@pytest.mark.parametrize("scheme", ["https", "HTTPS", "hTtPs", "http", "HTTP", "HtTp"])
@pytest.mark.parametrize("equals", [False, True])
@pytest.mark.parametrize("password", ["synthetic-url-value", "'synthetic-quote-value", "!$&'()*+,;=:synthetic-punctuation"])
def test_overridden_url_credentials_never_reach_saved_reports_or_exports(
    tmp_path, monkeypatch, capsys, local_tokenizer, scheme, equals, password,
):
    from giraffe.cli import main

    safe_url = "https://example.com/v1"
    unsafe_url = f"{scheme}://alice:{password}@example.com/v1?tenant=hidden-query#hidden-fragment"
    arguments = ["run", *([f"--url={unsafe_url}"] if equals else ["--url", unsafe_url]),
                 "--url", safe_url, "--model", "tiny", "--checks", "correctness",
                 "--skip-preflight", "--html", "--output", str(tmp_path), "--output-format", "json"]

    async def run(config, **kwargs):
        assert config.targets[0].url == safe_url
        return RunReport(run_id="r", started_at="now", finished_at="now", overall="pass",
                         manifest={"config": config.model_dump(mode="json")})

    monkeypatch.setattr("giraffe.cli._run", run)
    assert main(arguments) == 0
    capsys.readouterr()
    saved = tmp_path / "report.json"
    assert main(["export", str(saved)]) == 0
    exported = json.loads(capsys.readouterr().out)
    invocation = json.loads(saved.read_text())["manifest"]["invocation"]
    assert exported["invocation"] == invocation == cli_invocation(arguments)
    expected_url = f"{scheme.lower()}://example.com/v1[redacted]"
    assert invocation["argv"][1 if equals else 2] == ("--url=" if equals else "") + expected_url
    for text in (saved.read_text(), (tmp_path / "report.html").read_text(), json.dumps(exported)):
        for private in ("alice", "synthetic-url-value", "synthetic-quote-value", "synthetic-punctuation",
                        "hidden-query", "hidden-fragment"):
            assert private not in text
