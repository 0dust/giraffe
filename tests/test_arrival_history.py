import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from giraffe.latency import load_observations
from giraffe.load_test import admission_limit, settings_sets
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


def save(root, cfg=None, *, age=0, folder="previous", sent=4, source="primary"):
    cfg = cfg or config()
    timestamp = (datetime.now(timezone.utc) - timedelta(days=age)).isoformat()
    point = next(point for point in points(cfg.load_test) if point["source"] == source)
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


def test_delivery_history_keeps_latency_measurements_contract(tmp_path):
    from giraffe.latency import latency_summary

    saved = latency_summary(save(tmp_path))["targets"][0]
    assert saved["measurements"] == []  # This report has delivery totals, no request timings.
    assert len(saved["arrival_history"]) == 1
    assert saved["arrival_history"][0]["dispatched"] == 4


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


@pytest.mark.parametrize("saved_proxy,planned_proxy", [
    (None, "http://proxy-a.local:8080"),
    ("http://proxy-a.local:8080", None),
    ("http://proxy-a.local:8080", "http://proxy-b.local:8080"),
])
def test_proxy_route_change_does_not_reuse_persisted_history(tmp_path, saved_proxy, planned_proxy):
    cfg = RunConfig.model_validate({**config().model_dump(mode="json"), "proxy": saved_proxy})
    save(tmp_path, cfg)
    assert plan(tmp_path, cfg)["arrival_history"][0]["status"] == "warning"

    changed = RunConfig.model_validate({**cfg.model_dump(mode="json"), "proxy": planned_proxy})
    row = plan(tmp_path, changed)["arrival_history"][0]
    assert row["status"] == "unknown"
    assert "completion_p95_ms_by_shape" not in row
    assert "suggested_trial_rate" not in row

    # The new route contributes only a hash to history, not a persisted proxy URL.
    summary = json.loads((tmp_path / "previous" / ".web.json").read_text())["latency_summary"]
    assert "proxy-a.local" not in json.dumps(summary)
    assert "proxy-b.local" not in json.dumps(summary)


@pytest.mark.parametrize("concurrency,before_rates,after_rates,limits,status", [
    (None, [1], [1, 100], (1, 100), "unknown"),
    (None, [1, 100], [1], (100, 1), "unknown"),
    (None, [1, 100], [1, 50, 100], (100, 100), "warning"),
    (4, [1], [1, 100], (4, 4), "warning"),
])
@pytest.mark.parametrize("source", ["primary", "legacy_0_arrivals"])
def test_rate_list_reuses_history_only_with_same_admission_limit(
    tmp_path, concurrency, before_rates, after_rates, limits, status, source,
):
    values = config().model_dump(mode="json")
    values["concurrency"] = concurrency
    settings = values["load_test"]
    settings["patterns"]["constant"]["enabled"] = False
    settings["patterns"]["poisson"]["enabled"] = True
    settings["patterns"]["rates"] = before_rates
    settings["request_timeout_seconds"] = 1
    if source != "primary":
        settings.pop("migrated_tests")
        values["load_test"] = config().load_test.model_dump(mode="json")
        values["load_test"]["migrated_tests"] = [{"source": "arrivals", "settings": settings}]
    previous = RunConfig.model_validate(values)
    save(tmp_path, previous, source=source)
    point_id = next(point["id"] for point in points(previous.load_test) if point["source"] == source)
    old_row = next(row for row in plan(tmp_path, previous)["arrival_history"]
                   if row["point_id"] == point_id)
    assert old_row["status"] == "warning"

    settings["patterns"]["rates"] = after_rates
    current = RunConfig.model_validate(values)
    assert (admission_limit(previous, dict(settings_sets(previous.load_test))[source]),
            admission_limit(current, dict(settings_sets(current.load_test))[source])) == limits
    row = next(row for row in plan(tmp_path, current)["arrival_history"] if row["point_id"] == point_id)
    assert row["status"] == status
    if status == "warning":
        assert row["run_id"] == "previous"
        assert row["suggested_trial_rate"] == 0.5
    else:
        assert "suggested_trial_rate" not in row


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
        value["latency_summary"]["targets"][0]["arrival_history"][0]["dispatched"] = 900
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


@pytest.mark.parametrize("reverse", [False, True])
async def test_shared_endpoint_keeps_each_targets_delivery_evidence(tmp_path, monkeypatch, reverse):
    from copy import deepcopy
    from giraffe.client import _headers
    from giraffe.web import create_app

    values = config().model_dump(mode="json")
    base = values["targets"][0]
    values["targets"] = [{**base, "name": name, "identity": {"tenant": name},
                          "headers_env": {"X-Tenant": f"TENANT_{name.upper()}"}}
                         for name in (["bob", "alice"] if reverse else ["alice", "bob"])]
    for name in ("alice", "bob"):
        monkeypatch.setenv(f"TENANT_{name.upper()}", f"private-{name}-route")
    cfg = RunConfig.model_validate(values)
    assert _headers(cfg.targets[0])["X-Tenant"] != _headers(cfg.targets[1])["X-Tenant"]
    report = save(tmp_path)
    observation = report.observations["targets"]["local"]
    report.manifest["config"] = cfg.model_dump(mode="json")
    report.observations["targets"] = {target.name: deepcopy(observation) for target in cfg.targets}
    bob = report.observations["targets"]["bob"]["load_test"]["points"][0]["groups"][0]
    bob.update(dispatched=10, not_sent=0, local_drops=0, completed=10, scored=10, correct=10)
    write_report(report, tmp_path / "previous")
    expected = {"alice": ("warning", 4, 6), "bob": ("observed", 10, 0)}
    direct = plan(tmp_path, cfg)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)),
                                 base_url="http://localhost") as client:
        response = await client.post("/api/config/preview", json=cfg.model_dump(mode="json"))
    assert response.status_code == 200
    for result in (direct, response.json()["request_plan"]):
        assert {row["target"]: (row["status"], row.get("dispatched"), row.get("not_sent"))
                for row in result["arrival_history"]} == expected
        assert all(row["run_id"] == "previous" for row in result["arrival_history"])
    # A name reused with different routing credentials is not comparable history.
    cfg.targets[0].headers_env = {"X-Tenant": "CHANGED_ROUTE"}
    assert all(row["status"] == "unknown" for row in plan(tmp_path, cfg)["arrival_history"])
    assert "private-" not in (tmp_path / "previous" / ".web.json").read_text()


def test_setup_arrival_history_is_visible_and_tracks_the_current_plan(tmp_path):
    from tests.test_setup_async_regressions import SETUP
    from tests.test_ux_results import run_render_checks

    cfg = config()
    save(tmp_path, cfg, folder="source-run")
    value = plan(tmp_path, cfg)
    setup = SETUP.replace("renderRequestPreview=", "")
    run_render_checks(setup + "\nconst config=" + cfg.model_dump_json()
                      + ";const plan=" + json.dumps(value) + r''';
state.config=config;state.jsonDraft=state.jsonBase='synced';renderNew();
$('#setup-advanced').open=false;
const original=JSON.stringify(state.config),message=plan.arrival_history[0].message;
api=()=>{throw new Error('History rendering must not send traffic');};
function current(){
  state.jsonDraft=state.jsonBase='synced';
  state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,config,plan};
  renderRequestPlan(config);
}
function visible(){
  const outlet=$('#setup-history');assert(outlet,'History needs a visible setup outlet');
  const disclosure=outlet.querySelector('.arrival-history');assert(disclosure);
  assert.equal(disclosure.querySelector('summary').textContent,'Previous arrival delivery');
  assert(!outlet.closest('#setup-advanced'));assert(!outlet.closest('#run-blueprint'));
  for(let ancestor=disclosure.parentNode;ancestor;ancestor=ancestor.parentNode){
    assert(!ancestor.hidden,'History ancestor must be visible');
    assert(ancestor.tagName!=='DETAILS'||ancestor.open,'History must not be inside a closed disclosure');
  }
  assert.equal(disclosure.querySelector('a').getAttribute('href'),'#runs/source-run');
  assert.equal($('#main').textContent.split(message).length-1,1,'History appears once');
  assert(!$('#setup-advanced').open,'History does not force Advanced open');
}
function cleared(){assert($('#setup-history').hidden);assert.equal($('#setup-history').textContent,'');}
current();visible();
configChanged();cleared();current();visible();
for(const status of ['pending','error']){
  state.requestPreview.status=status;renderRequestPreview();cleared();current();visible();
}
state.routeVersion++;renderRequestPreview();cleared();current();visible();
state.jsonDraft='unapplied settings';configChanged();cleared();current();visible();
state.requestPreview.plan={...plan,arrival_history:[]};renderRequestPreview();cleared();
assert.equal(JSON.stringify(state.config),original);
''', dom=True)
