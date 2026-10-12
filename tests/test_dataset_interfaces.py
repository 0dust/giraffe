import asyncio
import json

import pytest

from giraffe.cli import main
from tests.fake_endpoint import FakeEndpoint
from tests.test_judge_interfaces import judged_config
from tests.test_web import browser, finished, report


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e999", '"\\ud800"'])
async def test_invalid_json_values_return_import_errors(tmp_path, value):
    async with browser(tmp_path) as client:
        response = await client.post("/api/datasets/preview", json={
            "text": '[{"question":"q","answer":"a","extra":' + value + '}]',
            "format": "json"})
    assert response.status_code == 200
    result = response.json()
    assert result["request_pack"] is None and result["preview_rows"] == []
    assert "finite numbers and valid Unicode" in result["errors"][0]["message"]


async def test_import_api_cli_parity_without_endpoint_calls(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("giraffe.client.LLMClient.__aenter__", lambda *a: pytest.fail("Import sent traffic"))
    source = tmp_path / "examples.csv"
    source.write_text('question,answer\nhello,world\n')
    async with browser(tmp_path / "runs") as client:
        response = await client.post("/api/datasets/preview", json={
            "text": source.read_text(), "format": "csv", "input_field": "question",
            "expected_field": "answer", "scorer": "exact"})
        assert response.status_code == 200
        assert (await client.get("/api/runs")).json()["runs"] == []
    assert main(["import-dataset", "--file", str(source), "--input-field", "question",
                 "--expected-field", "answer", "--scorer", "exact"]) == 0
    assert json.loads(capsys.readouterr().out) == response.json()


async def test_try_example_is_single_http_request_and_saved_report(tmp_path):
    with FakeEndpoint(mode="wrong") as endpoint:
        values = judged_config()
        values["stream"] = False
        values["retention"] = "all"
        values["targets"][0].update(url=endpoint.url, restart_command=["should-never-run"],
                                   deployment={"collector_url": endpoint.url + "/collector"})
        values["targets"].append({"name": "unfinished", "url": "http://127.0.0.1:1", "model": ""})
        values["restart_target"] = "local"
        values["request_pack"]["cases"][0].update(scorer="exact", expected="INCORRECT")
        values["request_pack"]["cases"][0].pop("rubric")
        values["request_pack"]["cases"].append({"fixture_id": "other", "messages": [
            {"role": "user", "content": "DO NOT SEND"}], "scorer": "exact", "expected": "ignored"})
        async with browser(tmp_path) as client:
            response = await client.post("/api/runs/try-example", json={
                "config": values, "fixture_id": "refund", "target_name": "local"})
            assert response.status_code == 202, response.text
            result = await finished(client, response.json()["id"])
            assert result["state"] == "completed", result
            saved = result["report"]
        assert len(endpoint.requests) == 1
        assert endpoint.collector_reads == endpoint.metrics_reads == 0
        assert endpoint.requests[0]["messages"][0]["content"] == "What is the return window?"
        assert len(saved["requests"]) == 1 and saved["requests"][0]["score"] is True
        assert saved["manifest"]["config"]["max_requests"] == 1
        assert saved["manifest"]["config"]["checks"] == ["correctness"]
        assert saved["requests"][0]["output"] == "INCORRECT"


async def test_try_example_uses_existing_busy_and_cancel_lifecycle(tmp_path, monkeypatch):
    started = asyncio.Event()

    async def waiting(config, *, progress, stop_event):
        started.set()
        await stop_event.wait()
        return report(config, cancelled=True)

    monkeypatch.setattr("giraffe.web.run_suite", waiting)
    body = {"config": judged_config(), "fixture_id": "refund", "target_name": "local"}
    async with browser(tmp_path) as client:
        response = await client.post("/api/runs/try-example", json=body)
        assert response.status_code == 202
        await started.wait()
        assert (await client.post("/api/runs/try-example", json=body)).status_code == 409
        identifier = response.json()["id"]
        assert (await client.post(f"/api/runs/{identifier}/cancel", json={})).status_code == 200
        result = await finished(client, identifier)
        assert result["report"]["abort_reason"] == "cancelled by user"


def test_try_example_cli_projects_configuration(tmp_path, monkeypatch, capsys):
    values = judged_config()
    values["targets"].append({"name": "draft", "url": "http://localhost:3", "model": ""})
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values))
    captured = []

    async def run(config):
        captured.append(config)
        return report(config)

    monkeypatch.setattr("giraffe.cli._run", run)
    assert main(["try-example", "--config", str(path), "--case", "refund", "--target", "local",
                 "--output", str(tmp_path / "output"), "--output-format", "json"]) == 0
    assert len(captured) == 1 and captured[0].max_requests == 1
    assert captured[0].checks == ["correctness"] and len(captured[0].targets) == 1
    assert json.loads(capsys.readouterr().out)["request_count"] == 1
    assert (tmp_path / "output" / "report.json").exists()
