"""A browser-configured run can be handed to a shell without changing its contract."""

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from collections import Counter

import pytest

from giraffe.models import RunConfig
from tests.fake_endpoint import FakeEndpoint
from tests.evidence_helpers import workload_payloads, workload_records
from tests.test_web import browser, finished, start


def affected_requests(report, target, row):
    """Independent interval oracle for this fixture's ordinary correctness traffic."""
    records = [r for r in report["requests"] if r["target"] == target
               and row["check"] in r["check_ids"] and r["scenario"] == row["scenario"]]
    assert records
    assert not any(r["scenario"] == "warm" for r in report["requests"])
    health = report["observations"]["client_timing_health"]
    assert health["schema_version"] == 1
    windows = health["windows"] + ([health["overflow_window"]] if health["overflow_window"] else [])
    affected = set()
    for record in records:
        assert record.get("scheduled_ms") is None and record.get("dispatch_lag_ms") is None
        start = record["timing_start_ms"]
        end = min(record["completed_ms"], start + record["elapsed_ms"])
        if row["metric"] == "first_output_ms":
            end = min(end, start + record["first_output_ms"])
        elif row["metric"] == "generation_tokens_per_second" and record["last_output_ms"] is not None:
            end = min(end, start + record["last_output_ms"])
        if any(w["lag_ms"] > 100 and start < w["end_ms"] and w["start_ms"] < end for w in windows):
            affected.add(record["id"])
    return affected


def assert_handoff_outcome(returncode, envelope, replay, original, *, compare):
    """Only observed client delay may make this otherwise healthy replay inconclusive."""
    for report in (original, replay):
        assert report["abort_reason"] is None
        selected = [c for c in report["checks"] if c["required"] and c["id"] != "baseline"]
        assert selected and all(c["status"] == "pass" for c in selected)
        assert all(r["status"] == "completed" and r["valid"] and not r["error"]
                   and not r["evaluation_error"] for r in report["requests"])
        assert all(r["score"] is True for r in workload_records(report["requests"]))
    expected = "pass"
    if compare:
        baseline = replay["baseline"]
        assert baseline["run_id"] == original["run_id"]
        assert baseline["targets"]
        for target in baseline["targets"]:
            assert not target["reasons"]
            assert target["comparisons"]
            delayed = False
            for row in target["comparisons"]:
                if "worsening" not in row:
                    assert row["status"] == "inconclusive"
                    assert max(row["current_samples"], row["baseline_samples"]) < baseline["min_samples"]
                    continue
                affected = False
                if row["metric"] in {"latency_ms", "first_output_ms", "max_stream_gap_ms",
                                     "output_tokens_per_second", "generation_tokens_per_second"}:
                    for label, report in (("Current", replay), ("Baseline", original)):
                        ids = affected_requests(report, target["target"], row)
                        quality = row.get("timing_quality", {}).get(label)
                        assert quality and quality["scope"] == "request_intervals"
                        assert quality["threshold_ms"] == 100
                        assert set(quality["affected_request_ids"]) == ids
                        assert quality["status"] == ("client_delayed" if ids else "qualified")
                        affected |= bool(ids)
                else:
                    assert row["metric"] in {"valid_completion_rate", "error_rate", "correctness_rate"}
                assert row["status"] == ("inconclusive" if affected else "pass"), row
                delayed |= affected
            assert target["status"] == ("inconclusive" if delayed else "pass")
            if delayed:
                expected = "inconclusive"
        assert baseline["status"] == expected
        assert [c["status"] for c in replay["checks"] if c["id"] == "baseline"] == [expected]
    else:
        assert replay["baseline"] is None
    assert replay["overall"] == envelope["outcome"] == expected
    assert returncode == envelope["exit_code"] == {"pass": 0, "inconclusive": 2}[expected]


@pytest.mark.parametrize("compare,pause", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("request_source", ["builtin", "request_pack"])
async def test_copied_command_replays_ui_configuration_and_baseline(
    tmp_path, monkeypatch, compare, pause, request_source,
):
    from giraffe.client import LLMClient

    execute = LLMClient.execute
    paused = False

    async def execute_with_host_pause(self, spec, **kwargs):
        nonlocal paused
        if pause and not paused and "correctness" in spec.check_ids:
            paused = True
            # Block during real HTTP traffic, leaving the production monitor active.
            asyncio.get_running_loop().call_later(.01, time.sleep, .25)
        return await execute(self, spec, **kwargs)

    monkeypatch.setattr(LLMClient, "execute", execute_with_host_pause)
    with FakeEndpoint(first_token_delay=.02, chunk_delay=.02) as endpoint:
        config = RunConfig(
            targets=[{"name": "local", "url": endpoint.url, "model": "fake-model"}],
            checks=["correctness"], samples=6, max_requests=20,
            limits={"regression_percent": 1_000_000},
            test_options={"capacity": {"limits": {"latency_ms": None}}},
        ).model_dump(mode="json")
        if request_source == "request_pack":
            from giraffe.cli import configuration, parser

            pack_path = tmp_path / "operator-pack.json"
            pack_path.write_text(json.dumps({"version": 1, "cases": [{
                "fixture_id": f"operator-question-{index}",
                "messages": [{"role": "user", "content": f"Return 42 for question {index}"}],
                "scorer": "exact", "expected": "42"} for index in range(6)]}))
            config_path = tmp_path / "run-config.json"
            config_path.write_text(json.dumps(config))
            config = configuration(parser().parse_args([
                "run", "--config", str(config_path), "--request-pack", str(pack_path),
            ])).model_dump(mode="json")
            pack_path.unlink()
            config_path.unlink()
        async with browser(tmp_path / "ui-runs") as client:
            original = await finished(client, await start(client, config))
            assert original["report"]["overall"] == "pass"
            source_report = json.loads((tmp_path / "ui-runs" / original["id"] / "report.json").read_text())
            assert paused == pause
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
            # Paste into the shell via stdin: embedded baselines can exceed
            # Linux's per-argument limit when passed through `sh -c`.
            result = await asyncio.to_thread(
                subprocess.run, ["/bin/sh"], input=command, cwd=tmp_path,
                env=env, capture_output=True, text=True, timeout=30,
            )
            envelope = json.loads(result.stdout)
            replay = json.loads(Path(envelope["report_paths"]["json"]).read_text())
            assert_handoff_outcome(result.returncode, envelope, replay, source_report, compare=compare)
            assert replay["manifest"]["config"] == exported.json()
            traffic = endpoint.requests[before:]
            assert len(traffic) == len(replay["requests"]) + 1
            assert endpoint.preflight_markers[before:] == ["1"] + [None] * len(replay["requests"])
            assert traffic[0] == {"model": "fake-model", "messages": [{"role": "user", "content": "Reply with OK."}],
                                  "stream": False, "max_tokens": 32, "n": 1}
            assert all(record["fixture_id"] != "connection" and record["scenario"] != "connection"
                       for record in replay["requests"])
            assert {r["fixture_id"] for r in replay["requests"]} == {
                r["fixture_id"] for r in original["report"]["requests"]
            }
            if request_source == "request_pack":
                test_records = workload_records(replay["requests"])
                assert len(test_records) == 12
                assert test_records[0]["fixture_id"] == "pack:operator-question-0"
                replayed = workload_payloads(traffic[1:])
                original_traffic = workload_payloads(endpoint.requests[:before])
                assert replayed[0]["messages"] == config["request_pack"]["cases"][0]["messages"]
                assert replayed[:6] == original_traffic[:6]
                assert Counter(json.dumps(request, sort_keys=True) for request in replayed[6:]) == Counter(
                    json.dumps(request, sort_keys=True) for request in original_traffic[6:])
            if compare:
                assert replay["baseline"]["run_id"] == original["report"]["run_id"]
                if pause:
                    assert replay["baseline"]["status"] == "inconclusive"
                    assert result.returncode == 2
                # The oracle must reject broken exits, real failures, and lost baseline evidence.
                with pytest.raises(AssertionError):
                    wrong_exit = 0 if result.returncode == 2 else 2
                    assert_handoff_outcome(wrong_exit, envelope, replay, source_report, compare=True)
                for defect in ("failed_rate", "unexplained_inconclusive", "different_baseline", "lost_delay"):
                    broken = deepcopy(replay)
                    rows = broken["baseline"]["targets"][0]["comparisons"]
                    if defect in {"failed_rate", "unexplained_inconclusive"}:
                        next(row for row in rows if row["metric"] == "correctness_rate")["status"] = (
                            "fail" if defect == "failed_rate" else "inconclusive"
                        )
                    elif defect == "different_baseline":
                        broken["baseline"]["run_id"] = "not-the-exported-baseline"
                    else:
                        if not pause:
                            continue
                        row = next(row for row in rows if row["status"] == "inconclusive"
                                   and "worsening" in row)
                        row["timing_quality"]["Baseline"]["affected_request_ids"] = []
                    with pytest.raises(AssertionError):
                        assert_handoff_outcome(result.returncode, envelope, broken, source_report, compare=True)
            else:
                assert replay["baseline"] is None
