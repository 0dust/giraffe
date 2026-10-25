import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from giraffe.latency import load_observations
from giraffe.load_test import test_points as points
from giraffe.models import RunConfig, RunReport
from giraffe.planning import request_plan
from giraffe.reporting import write_report


def config():
    return RunConfig(
        targets=[
            {
                "name": "local",
                "url": "http://server/v1",
                "model": "tiny",
                "deployment": {"runtime": {"version": "one"}},
            }
        ],
        checks=["load"],
        load_test={
            "patterns": {
                "concurrent": {"enabled": False},
                "constant": {"enabled": True},
                "rates": [1],
            },
            "workloads": {"combinations": {"selected": ["short_short"]}},
        },
    )


def save(root, cfg=None, *, age=0, folder="previous", sent=4):
    cfg = cfg or config()
    timestamp = (datetime.now(timezone.utc) - timedelta(days=age)).isoformat()
    point = points(cfg.load_test)[0]
    point["groups"] = [
        {
            "shape": "short_short",
            "scheduled": 10,
            "dispatched": sent,
            "not_sent": 10 - sent,
            "local_drops": 10 - sent,
            "late_arrivals": 0,
            "not_offered": 0,
            "completed": sent,
            "scored": sent,
            "correct": sent - 1,
            "completion_p95_ms": 2400,
        }
    ]
    report = RunReport(
        run_id="internal-id",
        started_at=timestamp,
        finished_at=timestamp,
        overall="inconclusive",
        manifest={"config": cfg.model_dump(mode="json")},
        observations={"targets": {"local": {"load_test": {"points": [point]}}}},
    )
    write_report(report, root / folder)
    return report


def plan(root, cfg=None):
    cfg = cfg or config()
    return request_plan(cfg, observations=load_observations(root, cfg))


def test_delivery_counts_correctness_and_trial_are_advisory(tmp_path):
    save(tmp_path)
    cfg = config()
    before = cfg.model_dump_json()
    value = plan(tmp_path, cfg)
    row = value["arrival_history"][0]
    assert row["status"] == "warning" and row["run_id"] == "previous"
    assert (row["dispatched"], row["scheduled"], row["not_sent"]) == (4, 10, 6)
    assert (row["correct"], row["scored"]) == (3, 4)
    assert row["completion_p95_ms_by_shape"] == {"short_short": 2400}
    assert row["suggested_trial_rate"] == 0.5
    assert row["message"] in value["warnings"]
    assert "not a capacity estimate" in row["message"]
    assert cfg.model_dump_json() == before


@pytest.mark.parametrize(
    "change",
    [
        "rate",
        "model",
        "endpoint",
        "deployment",
        "duration",
        "output",
        "stream",
        "ceiling",
        "scorer",
    ],
)
def test_incompatible_history_is_unknown(tmp_path, change):
    save(tmp_path)
    cfg = config()
    if change == "rate":
        cfg.load_test.patterns.rates = [2]
    elif change == "model":
        cfg.targets[0].model = "other"
    elif change == "endpoint":
        cfg.targets[0].url = "http://other/v1"
    elif change == "deployment":
        cfg.targets[0].deployment.runtime["version"] = "two"
    elif change == "duration":
        cfg.load_test.schedule.duration_seconds = 30
    elif change == "output":
        cfg.load_test.profile.short.output_tokens *= 2
    elif change == "stream":
        cfg.stream = not cfg.stream
    elif change == "ceiling":
        cfg.concurrency = 10
    elif change == "scorer":
        cfg.request_options = {"temperature": 0.7}
    assert all(row["status"] == "unknown" for row in plan(tmp_path, cfg)["arrival_history"])


@pytest.mark.parametrize("kind", ["missing", "stale", "unknown_deployment", "malformed"])
def test_missing_untrusted_or_stale_evidence_is_unknown(tmp_path, kind):
    cfg = config()
    if kind == "unknown_deployment":
        cfg.targets[0].deployment.runtime = {}
    if kind != "missing":
        save(tmp_path, cfg, age=31 if kind == "stale" else 0)
    if kind == "malformed":
        path = tmp_path / "previous" / ".web.json"
        value = json.loads(path.read_text())
        value["latency_summary"]["targets"][0]["measurements"][0]["dispatched"] = 900
        path.write_text(json.dumps(value))
    assert plan(tmp_path, cfg)["arrival_history"][0]["status"] == "unknown"


def test_latest_complete_delivery_replaces_old_warning_without_capacity_claim(tmp_path):
    save(tmp_path, age=1, folder="older")
    save(tmp_path, sent=10)
    row = plan(tmp_path)["arrival_history"][0]
    assert row["status"] == "observed" and row["run_id"] == "previous"
    assert row["suggested_trial_rate"] is None and row["correct"] == 9


async def test_web_preview_reads_saved_summary_without_request_traffic(tmp_path):
    from giraffe.web import create_app

    save(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(tmp_path)), base_url="http://localhost"
    ) as client:
        response = await client.post("/api/config/preview", json=config().model_dump(mode="json"))
    assert response.status_code == 200
    assert response.json()["request_plan"]["arrival_history"][0]["suggested_trial_rate"] == 0.5


def test_cli_preview_exposes_same_advisory(tmp_path, capsys):
    from giraffe.cli import main

    save(tmp_path)
    path = tmp_path / "config.json"
    path.write_text(config().model_dump_json())
    assert main(["preview", "--config", str(path), "--runs-dir", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["request_plan"]["arrival_history"][0]["run_id"] == "previous"


def test_arrival_evidence_links_source_run_and_escapes_text():
    from tests.test_ux_setup import run_browser_logic
    run_browser_logic(r"""
const html=renderArrivalHistory({arrival_history:[{message:'<private>',run_id:'saved/run'}]});
assert.ok(html.includes('&lt;private&gt;'));
assert.ok(html.includes('#runs/saved%2Frun'));
assert.ok(!html.includes('<private>'));
assert.equal(renderArrivalHistory({}), '');
""")
