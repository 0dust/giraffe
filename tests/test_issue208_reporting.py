"""Request accounting stays consistent across CLI and exported summaries."""
import json

import httpx
import pytest

from giraffe.cli import _print_result, _run, main
from giraffe.models import RequestRecord, RunConfig, RunReport, Target
from giraffe.preflight import PreflightError, check_target
from giraffe.reporting import _render, refresh_saved_observations, request_count_lines


def report():
    counts = {"sent": 2, "budget_counted": 1, "budget_exempt": 1,
              "budget_counted_unsent": 0, "saved_observations": 0, "budget_exempt_by_reason": {"restart_readiness": 1}}
    return RunReport(run_id="accounting", started_at="now", finished_at="now", overall="pass", manifest={},
                     observations={"request_counts": {**counts, "complete": True, "targets": {"local": dict(counts)}}})


def test_text_and_html_accounting(tmp_path, capsys):
    value = report()
    expected = "Requests: 2 sent · 1 counted toward the limit · 1 exempt · 0 saved observations"
    assert request_count_lines(value) == [expected, "Exempt: 1 restart readiness probe"]
    _print_result(value, tmp_path / "report.json", None, tmp_path, {}, quiet=True)
    assert expected in capsys.readouterr().out
    html = _render(value)
    assert expected in html
    assert '<details class="html-info">' in html
    assert "Connection preflight and readiness probes" in html
    path = tmp_path / "report.json"
    path.write_text(value.model_dump_json())
    assert main(["report", str(path)]) == 0
    output = capsys.readouterr().out.splitlines()
    assert output[0] == f"PASS  0 checks passed · 2 requests sent · report: {path}"
    assert output[1:3] == [expected, "Exempt: 1 restart readiness probe"]


def test_saved_observations_counts_only_present_measurements():
    value = report()
    value.requests = [RequestRecord(id=str(index), target="local", fixture_id="probe", scenario=scenario,
                                    check_ids=["access"], started_at="now", status="completed")
                      for index, scenario in enumerate(("readiness", "performance_evidence_probe", "initial"))]
    refresh_saved_observations(value)
    counts = value.observations["request_counts"]
    assert counts["saved_observations"] == counts["targets"]["local"]["saved_observations"] == 1
    value.requests.pop()  # A record removed by a memory/evidence guard is not saved.
    refresh_saved_observations(value)
    assert counts["saved_observations"] == counts["targets"]["local"]["saved_observations"] == 0


async def test_live_progress_uses_charged_budget_and_forwards_preflight(monkeypatch, capsys):
    async def run(config, *, progress, stop_event, preflight_counts):
        assert preflight_counts == {"local": 1}
        progress({"target": "local", "scenario": "initial", "completed": 8,
                  "budget_used": 1, "sent": 2, "budget_exempt": 1})
        return report()

    monkeypatch.setattr("giraffe.runner.run_suite", run)
    config = RunConfig(targets=[Target(name="local", url="http://server", model="tiny")], max_requests=2)
    await _run(config, preflight_counts={"local": 1})
    output = capsys.readouterr().err
    assert "1 of 2 counted requests · 1 exempt" in output
    assert "8 of 2" not in output


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("status", [200, 500])
async def test_preflight_counts_transport_even_if_response_fails(monkeypatch, status):
    from tests.test_connection import answer, endpoint

    requests, _ = endpoint(monkeypatch, lambda request: httpx.Response(status, json=answer()))
    target = Target(name="local", url="http://server", model="tiny")
    config = RunConfig(targets=[target])
    if status == 200:
        result = await check_target(target, config)
        assert result["inference_requests_sent"] == 1
    else:
        with pytest.raises(PreflightError) as error:
            await check_target(target, config)
        assert error.value.inference_requests_sent == 1
    assert len([request for request in requests if request.method == "POST"]) == 1


@pytest.mark.usefixtures("local_tokenizer")
async def test_preflight_preparation_failure_is_not_sent(monkeypatch):
    target = Target(name="local", url="http://server", model="tiny", api_key_env="ISSUE208_MISSING_KEY")
    monkeypatch.delenv("ISSUE208_MISSING_KEY", raising=False)
    with pytest.raises(PreflightError) as error:
        await check_target(target, RunConfig(targets=[target]))
    assert error.value.inference_requests_sent == 0


@pytest.mark.usefixtures("local_tokenizer")
def test_cli_forwards_real_preflight_count(tmp_path, monkeypatch, capsys):
    from tests.test_connection import answer, endpoint

    requests, _ = endpoint(monkeypatch, lambda request: httpx.Response(200, json=answer()))

    async def run(config, **options):
        assert options["preflight_counts"] == {"local": 1}
        return report()

    monkeypatch.setattr("giraffe.cli._run", run)
    assert main(["run", "--url", "http://server", "--model", "tiny", "--checks", "access",
                 "--max-requests", "1", "--output", str(tmp_path / "run"), "--json"]) == 0
    assert len([request for request in requests if request.method == "POST"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] == "pass"
    assert result["request_counts"] == report().observations["request_counts"]
    assert "request_count" not in result


def test_counted_unsent_and_recovered_suffix_on_text_and_html():
    value = report()
    counts = value.observations["request_counts"]
    counts.update(budget_counted=3, budget_counted_unsent=2, complete=False)
    expected = ("Requests: 2 sent · 3 counted toward the limit · 1 exempt · 0 saved observations"
                " · 2 counted but not sent (recovered; may be incomplete)")
    assert request_count_lines(value)[0] == expected
    assert expected in _render(value)


def test_saved_observations_creates_missing_target():
    value = report()
    value.requests = [RequestRecord(id="other", target="missing", fixture_id="answer", scenario="initial",
                                    check_ids=["access"], started_at="now", status="completed")]
    refresh_saved_observations(value)
    counts = value.observations["request_counts"]
    assert counts["saved_observations"] == counts["targets"]["missing"]["saved_observations"] == 1


async def test_journal_drops_do_not_remove_saved_report_observations(monkeypatch, tmp_path):
    from giraffe.evidence import EvidenceWriter
    from giraffe.reporting import write_report
    from giraffe.runner import record_sink

    record = RequestRecord(id="measurement", target="local", fixture_id="answer", scenario="initial",
                           check_ids=["access"], started_at="now", status="completed", sent=True)

    def drop(self, value):
        self.dropped_count += 1

    async def run(config, **options):
        record_sink.get()(record)
        value = report()
        value.requests = [record]
        return value

    monkeypatch.setattr(EvidenceWriter, "record_finished", drop)
    monkeypatch.setattr("giraffe.runner.run_suite", run)
    config = RunConfig(targets=[Target(name="local", url="http://server", model="tiny")], retention="none")
    value = await _run(config, directory=tmp_path, quiet=True)
    path, _ = write_report(value, tmp_path, max_run_storage=0, preserve_evidence=True)
    saved = json.loads(path.read_text())
    assert saved["observations"]["evidence_dropped_count"] == 1
    assert len(saved["requests"]) == saved["observations"]["request_counts"]["saved_observations"] == 1


def test_report_command_prints_run_summary_with_check_lines(tmp_path, capsys):
    from giraffe.models import CheckResult

    value = report()
    value.overall = "fail"
    value.checks = [CheckResult(id="access", target="local", title="Access", status="fail", summary="No answer.")]
    path = tmp_path / "report.json"
    path.write_text(value.model_dump_json())
    assert main(["report", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert output.startswith(f"FAIL  0 checks passed · 2 requests sent · report: {path}\n")
    assert request_count_lines(value)[0] in output
    assert "Exempt: 1 restart readiness probe" in output
    assert "  local / access: fail — No answer." in output


@pytest.mark.parametrize("html", [False, True])
def test_report_summary_uses_selected_artifact_path(tmp_path, capsys, html):
    source = tmp_path / "selected.json"
    source.write_text(report().model_dump_json())
    destination = tmp_path / "custom.html"
    args = ["report", str(source)]
    if html:
        args.extend(["--html", "--output", str(destination)])
    assert main(args) == 0
    expected = destination if html else source
    assert f"report: {expected}" in capsys.readouterr().out
    assert expected.is_file()
