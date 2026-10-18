"""The setup form's saved configuration, API plan and real traffic agree."""

from collections import Counter
from contextlib import nullcontext
import json
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest

from giraffe.models import Buckets, RunConfig
from giraffe.planning import request_plan
from giraffe.runner import run_suite
from giraffe.web import create_app
from tests.fake_endpoint import FakeEndpoint
from tests.test_workload_reliability import MemoryEndpoint


@pytest.mark.parametrize("endpoint_transport", ["socket", "memory"])
async def test_concurrent_setup_plan_matches_requests_with_saved_dataset_and_low_output_cap(
    tmp_path, monkeypatch, endpoint_transport
):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise the real setup configuration module")
    module = (Path(__file__).parents[1] / "src/giraffe/static/form-config.mjs").as_uri()
    endpoint_context = (FakeEndpoint(first_token_delay=.03, chunk_delay=.001)
                        if endpoint_transport == "socket" else
                        nullcontext(MemoryEndpoint(monkeypatch, delay=.03)))
    with endpoint_context as endpoint:
        source = RunConfig(
            targets=[{"name": "local", "url": endpoint.url, "model": "fake-model"}],
            checks=["access", "correctness"], concurrency=16,
            max_requests=200, max_output_tokens=32, max_duration_seconds=30,
            request_timeout_seconds=3, limits={"min_samples": 2},
            request_source="request_pack",
            request_pack={"version": 1, "cases": [{
                "fixture_id": "saved-question",
                "messages": [{"role": "user", "content": "SAVED_QUESTION_MUST_NOT_BE_SENT"}],
                "scorer": "exact", "expected": "A",
            }]},
            test_options={"correctness": {"concurrency": 12}},
        ).model_dump(mode="json")
        script = f"""
import {{applySetupPreset,configureBuckets,configureSetupLoad}} from {json.dumps(module)};
const source={json.dumps(source)};
const defaults={json.dumps(Buckets().model_dump(mode='json'))};
const values=new FormData();
values.set('buckets_levels','1, 4, 8');
let config=applySetupPreset(source,'concurrent',defaults);
config=configureBuckets(config,values,defaults);
config=configureSetupLoad(config,'concurrent');
process.stdout.write(JSON.stringify(config));
"""
        result = subprocess.run(
            [node, "--input-type=module", "-"], input=script,
            capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, result.stderr
        values = json.loads(result.stdout)
        assert values["concurrency"] == 8
        assert values["max_requests"] == 200
        assert values["max_output_tokens"] == 32
        assert values["request_pack"] == source["request_pack"]
        assert values["test_options"] == source["test_options"]
        assert values["buckets"]["output_tokens"] == {"short": 16, "medium": 32, "long": 32}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(tmp_path / "runs")),
            base_url="http://giraffe.test",
        ) as client:
            validation = await client.post("/api/config/validate", json=values)
            assert validation.status_code == 200, validation.text
            preview = await client.post("/api/config/preview", json=validation.json()["config"])
            assert preview.status_code == 200, preview.text
            plan = preview.json()["request_plan"]
            assert plan["count_kind"] == "exact"
            assert plan["planned_total"] == 190
            assert plan["required_request_limit"] == 190
            assert len(plan["bucket_cases"]) == 9
            assert plan["evaluator"]["call_upper_bound"] == 0
            assert endpoint.requests == []  # Neither validation nor planning sends inference.

        configuration = RunConfig.model_validate(preview.json()["config"])
        assert request_plan(configuration) == plan
        assert plan["bucket_levels"] == [
            {"concurrency": 1, "requests_per_case": 5, "requests_per_target": 45},
            {"concurrency": 4, "requests_per_case": 8, "requests_per_target": 72},
            {"concurrency": 8, "requests_per_case": 8, "requests_per_target": 72},
        ]
        planned_scenarios = {"initial": 1}
        for level in plan["bucket_levels"]:
            for input_shape in ("short", "medium", "long"):
                for output_shape in ("short", "medium", "long"):
                    scenario = f"bucket_c{level['concurrency']}_{input_shape}_{output_shape}"
                    planned_scenarios[scenario] = level["requests_per_case"]
        report = await run_suite(configuration)
        assert report.abort_reason is None
        assert len(report.requests) == len(endpoint.requests) == 190
        assert Counter(item.scenario for item in report.requests) == planned_scenarios
        bucket_requests = [item for item in report.requests if item.scenario.startswith("bucket_c")]
        assert Counter(item.workload["level"] for item in bucket_requests) == {1: 45, 4: 72, 8: 72}
        assert all(item.valid for item in report.requests)
        assert all(call["max_tokens"] <= 32 for call in endpoint.requests)
        assert all("SAVED_QUESTION_MUST_NOT_BE_SENT" not in json.dumps(call)
                   for call in endpoint.requests)
        peak_active = endpoint.peak_active if endpoint_transport == "socket" else endpoint.peak
        assert 2 <= peak_active <= 8


@pytest.mark.parametrize("check,options", [
    ("cancellation", {}),
    ("prefix", {}),
    ("consistency", {"consistency": {"levels": [1, 2]}}),
    ("tools", {"forced_tool_diagnostic": True}),
])
async def test_fixed_specialist_plan_matches_runner_requests(check, options):
    with FakeEndpoint() as endpoint:
        config = RunConfig(
            targets=[{"name": "local", "url": endpoint.url, "model": "fake-model"}],
            checks=[check], max_requests=100, max_duration_seconds=30,
            **options,
        )
        plan = request_plan(config)
        assert plan["count_kind"] == "exact"
        assert endpoint.requests == []
        report = await run_suite(config)
        assert report.abort_reason is None
        assert len(report.requests) == plan["planned_total"]
        assert len(endpoint.requests) == plan["planned_total"]
