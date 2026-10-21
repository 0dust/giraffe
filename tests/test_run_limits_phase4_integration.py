"""Run-start resource checks and observation plumbing without listening sockets."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from giraffe import open_files
from giraffe.cli import main
from giraffe.models import RunConfig
from tests.test_web import browser, finished, report, successful_runner


def configuration():
    return RunConfig(targets=[{"name": "local", "url": "http://localhost/v1", "model": "test"}],
                     checks=["correctness"], default_concurrency=40)


@pytest.fixture
def limits(monkeypatch):
    state = {"soft": 64, "hard": 4096, "calls": []}

    def setrlimit(kind, values):
        state["calls"].append(values)
        state["soft"], state["hard"] = values

    monkeypatch.setattr(open_files, "resource", SimpleNamespace(
        RLIMIT_NOFILE=1, RLIM_INFINITY=-1,
        getrlimit=lambda kind: (state["soft"], state["hard"]), setrlimit=setrlimit))
    monkeypatch.setattr(open_files, "_open_descriptors", lambda: 12)
    monkeypatch.setattr(open_files.sys, "platform", "linux")
    return state


async def test_preview_only_warns_start_raises_and_manifest_records_before_after(tmp_path, monkeypatch, limits):
    monkeypatch.setattr("giraffe.web.run_suite", successful_runner)
    values = configuration().model_dump(mode="json")
    async with browser(tmp_path) as client:
        preview = await client.post("/api/config/preview", json=values)
        assert preview.status_code == 200, preview.text
        plan = preview.json()["request_plan"]
        assert plan["open_files"]["need"] == 84
        assert "raised automatically" in plan["resource_warnings"][0]["msg"]
        assert limits["calls"] == []
        response = await client.post("/api/runs", json={"config": values})
        assert response.status_code == 202, response.text
        identifier = response.json()["id"]
        assert (await finished(client, identifier))["state"] == "completed"
    assert limits["calls"] == [(84, 4096)]
    saved = json.loads((tmp_path / identifier / "report.json").read_text())
    assert saved["manifest"]["open_files"]["before"] == {"soft": 64, "hard": 4096}
    assert saved["manifest"]["open_files"]["after"] == {"soft": 84, "hard": 4096}
    assert "latency_summary" in json.loads((tmp_path / identifier / ".web.json").read_text())


async def test_hard_limit_blocks_only_start_and_copy_cli_is_independent(tmp_path, monkeypatch, limits):
    limits["hard"] = 70
    monkeypatch.setattr("giraffe.web.run_suite", lambda *a, **k: pytest.fail("blocked run started"))
    values = configuration().model_dump(mode="json")
    async with browser(tmp_path) as client:
        for path in ("/api/config/preview", "/api/config/validate", "/api/config/plan", "/api/config/draft"):
            response = await client.post(path, json=values)
            assert response.status_code == 200, response.text
            plan = response.json()["request_plan"]
            assert plan["errors"] == []
            issue = plan["resource_issues"][0]
            assert issue["blocking"] is True
            assert issue["values"]["required"] == 84
            assert "ulimit -n 4096" in issue["msg"]
            assert "Restart `giraffe web` from that shell" in issue["msg"]
        response = await client.post("/api/runs", json={"config": values})
        assert response.status_code == 422, response.text
        assert response.json()["detail"][0]["values"]["required"] == 84
        calls = list(limits["calls"])
        copied = await client.post("/api/config/cli", json={"config": values})
        assert copied.status_code == 200, copied.text
        assert "giraffe run" in copied.json()["command"]
        assert limits["calls"] == calls == [(70, 70)]
    assert not list(tmp_path.glob("*/report.json"))


def test_cli_run_raises_before_traffic_and_preserves_summary_on_final_metadata_write(tmp_path, monkeypatch, capsys, limits):
    config = configuration()
    path = tmp_path / "config.json"
    path.write_text(config.model_dump_json())

    async def run(configuration, **kwargs):
        assert limits["calls"] == [(84, 4096)]
        return report(configuration)

    monkeypatch.setattr("giraffe.cli._run", run)
    destination = tmp_path / "saved"
    assert main(["run", "--config", str(path), "--skip-preflight", "--output", str(destination)]) == 0
    capsys.readouterr()
    saved = json.loads((destination / "report.json").read_text())
    assert saved["manifest"]["open_files"]["after"]["soft"] == 84
    assert json.loads((destination / ".web.json").read_text())["latency_summary"]["version"] == 3


def test_cli_hard_limit_blocks_without_traffic(tmp_path, monkeypatch, capsys, limits):
    limits["hard"] = 70
    path = tmp_path / "config.json"
    path.write_text(configuration().model_dump_json())
    monkeypatch.setattr("giraffe.cli._run", lambda *a, **k: pytest.fail("blocked CLI sent traffic"))
    assert main(["run", "--config", str(path), "--skip-preflight", "--runs-dir", str(tmp_path / "runs")]) == 3
    assert "needs 84 open files" in capsys.readouterr().err
    assert not list((tmp_path / "runs").glob("*/report.json"))


async def test_web_preview_and_cli_use_recent_summary_from_selected_runs_directory(tmp_path, monkeypatch, capsys):
    from giraffe.conditions import condition_for
    from giraffe.fixtures import builtin_fixtures
    from giraffe.models import RequestRecord
    from giraffe.reporting import write_report

    config = configuration()
    # Include all actual fixture sizes/caps so every finite group has a match.
    specs = builtin_fixtures(config, check_id="correctness")["correctness"]
    current = report(config)
    current.finished_at = datetime.now(timezone.utc).isoformat()
    current.requests = [RequestRecord(id=str(index), target="local", fixture_id=spec.fixture_id,
        scenario="correctness", check_ids=["correctness"], started_at=current.started_at,
        status="completed", valid=True, elapsed_ms=100 + 20 * index, input_chars=spec.input_chars,
        requested_max_tokens=spec.max_tokens, stream=spec.stream) for index, spec in enumerate(specs * 4)]
    condition = condition_for("correctness", config=config, request_parameters={"concurrency": 40})
    current.observations = {"targets": {"local": {"conditions": [{"condition": condition}]}}}
    runs = tmp_path / "observed"
    write_report(current, runs / "last")
    # This also proves preview never needs to deserialize report.json.
    (runs / "last/report.json").write_text("not JSON")
    async with browser(runs) as client:
        response = await client.post("/api/config/preview", json=config.model_dump(mode="json"))
        assert response.status_code == 200, response.text
        observed = response.json()["request_plan"]
        assert observed["confidence"] == "low"
        assert observed["confidence_details"]["insufficient_observations"]
        assert observed["unknown_time_components"] == []
        assert observed["estimated_low_seconds"] > 0
    path = tmp_path / "config.json"
    path.write_text(config.model_dump_json())
    assert main(["preview", "--config", str(path), "--runs-dir", str(runs)]) == 0
    cli_plan = json.loads(capsys.readouterr().out)["request_plan"]
    assert cli_plan["time_components"] == observed["time_components"]
    assert main(["preview", "--config", str(path), "--runs-dir", str(tmp_path / "empty")]) == 0
    unknown = json.loads(capsys.readouterr().out)["request_plan"]
    assert unknown["confidence"] == "low"
    assert unknown["unknown_time_components"][0]["requests"] == 6


@pytest.mark.parametrize("command", ["validate", "preview", "run"])
def test_cli_announces_time_coverage_and_estimate_risk_without_changing_budget(tmp_path, monkeypatch, capsys, command):
    config = RunConfig(targets=[{"name": "local", "url": "http://localhost", "model": "test"}],
                       checks=["capacity"], max_duration_seconds=5,
                       traffic={"rates": [1], "duration_seconds": 20})
    path = tmp_path / "config.json"
    path.write_text(config.model_dump_json())

    async def run(configuration, **kwargs):
        assert configuration.max_duration_seconds == 5
        assert configuration.traffic.duration_seconds == 20
        return report(configuration)

    monkeypatch.setattr("giraffe.cli._run", run)
    flags = ["--skip-preflight", "--output", str(tmp_path / "saved")] if command == "run" else ["--output-format", "text"]
    assert main([command, "--config", str(path), *flags]) == 0
    captured = capsys.readouterr()
    assert "Incomplete coverage" in captured.out + captured.err
    assert "Capacity under load" in captured.out + captured.err
    assert "estimated traffic duration may exceed" in captured.out + captured.err
    assert json.loads(path.read_text())["max_duration_seconds"] == 5
