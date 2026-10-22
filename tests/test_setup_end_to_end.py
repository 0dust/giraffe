"""The setup form's saved configuration, API plan and real traffic agree."""

from collections import Counter
from contextlib import nullcontext
import json
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest

from giraffe.models import LoadTest, RunConfig
from giraffe.planning import request_plan
from giraffe.open_files import open_file_guard
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
            checks=["access"], concurrency=16,
            max_requests=500, max_output_tokens=32, max_duration_seconds=30,
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
import {{applySetupPreset,configureLoadTest,configureSetupLoad}} from {json.dumps(module)};
const source={json.dumps(source)};
const defaults={json.dumps(LoadTest().model_dump(mode='json'))};
const values=new FormData();
values.set('load_rendered','1');
values.set('load_workloads_combinations_enabled','on');
for(const shape of ['short_short','long_short','short_long'])values.set('load_workloads_combinations_selected_'+shape,'on');
values.set('load_patterns_concurrent_enabled','on');
values.set('load_patterns_concurrent_levels','1, 4, 8');
values.set('load_schedule_finish_seconds','0.3');
values.set('load_schedule_duration_seconds','1');
values.set('load_mixed_enabled','');
values.set('load_profile_short_output_tokens','8');
values.set('load_profile_medium_output_tokens','16');
values.set('load_profile_long_output_tokens','32');
let config=applySetupPreset(source,'concurrent',defaults);
config=configureLoadTest(config,values,defaults);
config=configureSetupLoad(config,'concurrent');
process.stdout.write(JSON.stringify(config));
"""
        result = subprocess.run(
            [node, "--input-type=module", "-"], input=script,
            capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, result.stderr
        values = json.loads(result.stdout)
        assert values["concurrency"] == source["concurrency"] == 16
        assert values["max_requests"] == 500
        assert values["max_output_tokens"] == 32
        assert values["request_pack"] == source["request_pack"]
        assert values["test_options"] == source["test_options"]
        assert {size: profile["output_tokens"] for size, profile in values["load_test"]["profile"].items()} == {"short": 8, "medium": 16, "long": 32}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(tmp_path / "runs")),
            base_url="http://giraffe.test",
        ) as client:
            validation = await client.post("/api/config/validate", json=values)
            assert validation.status_code == 200, validation.text
            preview = await client.post("/api/config/preview", json=validation.json()["config"])
            assert preview.status_code == 200, preview.text
            plan = preview.json()["request_plan"]
            assert plan["count_kind"] == "bounded"
            assert plan["configured_load_seconds"] == 9
            assert plan["load_test"]["scheduled_tests"] == 9
            assert {point["level"] for point in plan["load_test"]["points"]} == {1, 4, 8}
            rows = {row["id"]: row for row in plan["rows"]}
            assert rows["load_combinations"]["requests_per_target"] is None
            assert "load_calibration" not in rows  # Saved questions retain their own input; synthetic calibration is unnecessary.
            assert plan["evaluator"]["call_upper_bound"] == 0
            assert endpoint.requests == []

        configuration = RunConfig.model_validate(preview.json()["config"])
        guard = open_file_guard(configuration, web=True)
        assert plan == {**request_plan(configuration), "open_files": guard,
                        "resource_warnings": guard["warnings"],
                        "resource_issues": [dict(issue, blocking=True) for issue in guard["errors"]]
                        + [dict(issue, blocking=False) for issue in guard["warnings"]]}
        # Keep the real client/runner traffic test short without changing form validation.
        configuration.load_test.schedule.duration_seconds = .2
        report = await run_suite(configuration)
        assert report.abort_reason is None
        assert len(report.requests) == len(endpoint.requests) <= 500
        load_requests = [item for item in report.requests if item.load_point and item.load_point["type"] != "calibration"]
        expected = {f"load_primary_combinations_{shape}_concurrent_c{level}" for shape in ("short_short", "long_short", "short_long") for level in (1, 4, 8)}
        assert {item.condition_id for item in load_requests} == expected
        assert set(Counter(item.load_point["level"] for item in load_requests)) == {1, 4, 8}
        assert all(item.workload["case_id"] == "saved-question" for item in load_requests)
        load_check = next(item for item in report.checks if item.id == "load")
        points = load_check.metrics["points"]
        assert len(points) == 9
        assert sum(point["dispatched"] for point in points) == len(load_requests)
        assert all(point["configured_window_seconds"] == .2 for point in points)
        assert all(item.valid for item in report.requests)
        assert all(call["max_tokens"] <= 32 for call in endpoint.requests)
        assert any("SAVED_QUESTION_MUST_NOT_BE_SENT" in json.dumps(call)
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
