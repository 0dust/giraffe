"""One declared deployment change survives collection and reproduction exports."""

import json
from html import unescape

import pytest

from giraffe.cli import load_report
from giraffe.models import RunConfig
from giraffe.reporting import compare_baseline, reproduction_export, write_report
from giraffe.runner import run_suite
from tests.fake_endpoint import FakeEndpoint
from tests.test_workloads import config


pytestmark = pytest.mark.usefixtures("local_tokenizer")
FIELDS = ["extension.requested_speculative_config", "serving.launch_arguments"]


async def test_grouped_change_survives_run_baseline_export_and_rerun(tmp_path):
    with FakeEndpoint() as endpoint:
        def settings(enabled):
            launch = ["vllm", "serve", "fake-model"]
            if enabled:
                launch += ["--speculative-config", '{"method":"eagle3","num_speculative_tokens":3}']
            return config(endpoint, targets=[{
                "name": "local", "url": endpoint.url, "model": "fake-model",
                "deployment": {
                    "extension": {"requested_speculative_config": {"method": "eagle3", "k": 3} if enabled else None},
                    "launch_command": launch,
                    "intended_change": FIELDS if enabled else None,
                },
            }])

        baseline = await run_suite(settings(False))
        current = await run_suite(settings(True))
        assert current.requests and any(r.valid for r in current.requests)
        original = baseline.model_dump_json()
        comparison = compare_baseline(current, baseline)
        diff = comparison.baseline["targets"][0]["configuration_diff"]
        assert {r["field"] for r in diff["fields"] if r["change"] != "no_observed_change"} == set(FIELDS)
        assert diff["intended_change"] == FIELDS
        assert diff["confounded"] is False
        assert "not evidence of causality" in diff["interpretation"]
        assert baseline.model_dump_json() == original

        write_report(comparison, tmp_path, html=True)
        saved = load_report(tmp_path)
        assert saved.baseline["targets"][0]["configuration_diff"] == diff
        html = unescape((tmp_path / "report.html").read_text())
        assert f'Deployment change: {", ".join(FIELDS)} (one intended change)' in html
        assert 'Confounded: multiple unrelated changes' not in html
        assert 'data:application/json' not in html and '<script' not in html
        assert all(field in html for field in FIELDS)
        exported = reproduction_export(saved)
        assert exported["deployments"]["local"]["intended_change"] == FIELDS
        rerun_config = RunConfig.model_validate(exported["config"])
        assert rerun_config.targets[0].deployment.intended_change == FIELDS
        assert "[redacted]" in json.dumps(rerun_config.targets[0].deployment.launch_command)
        rerun = await run_suite(rerun_config)
        rerun_diff = compare_baseline(rerun, baseline).baseline["targets"][0]["configuration_diff"]
        assert rerun_diff["confounded"] is False
        assert rerun_diff["intended_change"] == FIELDS


async def test_grouped_change_survives_configuration_api_and_cli_export(tmp_path):
    from tests.test_web import browser

    async with browser(tmp_path) as client:
        values = (await client.get("/api/bootstrap")).json()["config"]
        values["targets"][0]["model"] = "test-model"
        values["targets"][0]["deployment"]["intended_change"] = FIELDS
        response = await client.post("/api/config/draft", json=values)
        assert response.status_code == 200, response.text
        saved = response.json()["config"]
        assert saved["targets"][0]["deployment"]["intended_change"] == FIELDS
        again = await client.post("/api/config/draft", json=saved)
        assert again.json()["config"] == saved
        exported = await client.post("/api/config/cli", json={"config": saved})
        assert exported.status_code == 200, exported.text
        assert all(field in exported.json()["command"] for field in FIELDS)
