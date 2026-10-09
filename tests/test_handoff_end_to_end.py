"""A browser-configured run can be handed to a shell without changing its contract."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from giraffe.models import RunConfig
from tests.fake_endpoint import FakeEndpoint
from tests.test_web import browser, finished, start


@pytest.mark.parametrize("compare", [False, True])
async def test_copied_command_replays_ui_configuration_and_baseline(tmp_path, compare):
    with FakeEndpoint(first_token_delay=.001, chunk_delay=0) as endpoint:
        config = RunConfig(
            targets=[{"name": "local", "url": endpoint.url, "model": "fake-model"}],
            checks=["correctness"], samples=6, max_requests=20,
            limits={"regression_percent": 1_000_000},
            test_options={"capacity": {"limits": {"latency_ms": None}}},
        ).model_dump(mode="json")
        async with browser(tmp_path / "ui-runs") as client:
            original = await finished(client, await start(client, config))
            assert original["report"]["overall"] == "pass"
            exported = await client.get(f"/api/runs/{original['id']}/config.json")
            assert exported.status_code == 200
            assert exported.json() == original["report"]["manifest"]["config"]

            baseline_id = None
            if compare:
                saved = await client.post("/api/baselines", json={
                    "run_id": original["id"], "name": "before upgrade",
                })
                assert saved.status_code == 201, saved.text
                baseline_id = saved.json()["id"]
            response = await client.post("/api/config/cli", json={
                "config": exported.json(), "baseline_id": baseline_id,
            })
            assert response.status_code == 200, response.text
            before = len(endpoint.requests)
            assert len((await client.get("/api/runs")).json()["runs"]) == 1
            command = response.json()["command"]
            env = dict(os.environ)
            env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
            env["NO_PROXY"] = "127.0.0.1,localhost"
            result = await asyncio.to_thread(
                subprocess.run, ["/bin/sh", "-c", command], cwd=tmp_path,
                env=env, capture_output=True, text=True, timeout=30,
            )
            assert result.returncode == 0, result.stderr + result.stdout
            envelope = json.loads(result.stdout)
            assert envelope["outcome"] == "pass"
            replay = json.loads(Path(envelope["report_paths"]["json"]).read_text())
            assert replay["manifest"]["config"] == exported.json()
            assert len(endpoint.requests) > before
            assert {r["fixture_id"] for r in replay["requests"]} == {
                r["fixture_id"] for r in original["report"]["requests"]
            }
            if compare:
                assert replay["baseline"]["run_id"] == original["report"]["run_id"]
                assert replay["baseline"]["status"] == "pass"
            else:
                assert replay["baseline"] is None
