from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from giraffe.latency import load_observations
from giraffe.models import RequestRecord, RunConfig, RunReport
from giraffe.planning import request_plan
from giraffe.reporting import write_report


def config(*, builtin=False):
    values = dict(targets=[{"name": "local", "url": "http://server/v1", "model": "tiny",
                           "deployment": {"runtime": {"version": "one"}}}], checks=["correctness"])
    if not builtin:
        values.update(request_source="request_pack", request_pack={"version": 1, "cases": [{
            "fixture_id": "question", "messages": [{"role": "user", "content": "Private question"}],
            "scorer": "exact", "expected": "Private expected answer"}]})
    return RunConfig(**values)


def save(root, cfg=None, *, score=False, age=0, folder="previous", record_changes=None):
    cfg = cfg or config()
    timestamp = (datetime.now(timezone.utc)-timedelta(days=age)).isoformat()
    record = dict(id="failed-answer", target="local", fixture_id="pack:question" if cfg.request_source == "request_pack" else "correctness.arithmetic.v1",
        scenario="pack_control" if cfg.request_source == "request_pack" else "correctness_c1",
        check_ids=["correctness"], started_at=timestamp, status="completed", valid=True, score=score,
        output="Private wrong answer", output_chars=20, requested_max_tokens=24)
    record.update(record_changes or {})
    report = RunReport(run_id="internal-id", started_at=timestamp, finished_at=timestamp, overall="fail",
        manifest={"config": cfg.model_dump(mode="json")}, requests=[RequestRecord(**record)])
    write_report(report, root / folder)
    return report


def plan(root, cfg=None):
    cfg = cfg or config()
    return request_plan(cfg, observations=load_observations(root, cfg))


@pytest.mark.parametrize("builtin", [False, True])
def test_matching_failure_warns_without_mutating_settings(tmp_path, builtin):
    cfg = config(builtin=builtin)
    before = cfg.model_dump_json()
    save(tmp_path, cfg)
    value = plan(tmp_path, cfg)
    warnings = [row for row in value["correctness_history"] if row["status"] == "warning"]
    assert len(warnings) == 1
    row = warnings[0]
    assert row["run_id"] == "previous" and row["failed_request_ids"] == ["failed-answer"]
    assert row["case_id"] == (None if builtin else "question")
    assert row["message"] in value["warnings"] and "does not predict" in row["message"]
    assert cfg.model_dump_json() == before
    metadata = (tmp_path / "previous" / ".web.json").read_text()
    for secret in ["Private question", "Private expected answer", "Private wrong answer"]:
        assert secret not in metadata


@pytest.mark.parametrize("change", ["question", "expected", "scorer", "cap", "timeout", "model", "url", "deployment", "concurrency"])
def test_changed_experiment_does_not_reuse_warning(tmp_path, change):
    save(tmp_path)
    cfg = config()
    case = cfg.request_pack.cases[0]
    if change == "question":
        case.messages[0].content = "Different question"
    elif change == "expected":
        case.expected = "Different expected"
    elif change == "scorer":
        case.scorer = "contains"
    elif change == "cap":
        case.max_tokens = 512
    elif change == "timeout":
        cfg.request_timeout_seconds = 42
    elif change == "model":
        cfg.targets[0].model = "other"
    elif change == "url":
        cfg.targets[0].url = "http://other/v1"
    elif change == "deployment":
        cfg.targets[0].deployment.runtime["version"] = "two"
    elif change == "concurrency":
        cfg.default_concurrency *= 2
    assert all(row["status"] == "unknown" for row in plan(tmp_path, cfg)["correctness_history"])


def test_changing_acceptance_threshold_does_not_hide_wrong_answers(tmp_path):
    save(tmp_path)
    cfg = config()
    cfg.limits.min_correctness = 0
    assert plan(tmp_path, cfg)["correctness_history"][0]["status"] == "warning"
    assert cfg.limits.min_correctness == 0


def test_newer_success_supersedes_failure(tmp_path):
    save(tmp_path, age=1, folder="old")
    save(tmp_path, score=True)
    row = plan(tmp_path)["correctness_history"][0]
    assert row["status"] == "observed" and row["failed"] == 0 and row["failed_request_ids"] == []


@pytest.mark.parametrize("builtin", [False, True])
@pytest.mark.parametrize("old_proxy,new_proxy", [
    (None, "http://proxy-a.local:8080"),
    ("http://proxy-a.local:8080", None),
    ("http://proxy-a.local:8080", "http://proxy-b.local:8080"),
])
async def test_proxy_changes_isolate_persisted_failures_and_newer_successes(
    tmp_path, builtin, old_proxy, new_proxy,
):
    from giraffe.web import create_app

    original = config(builtin=builtin)
    original.proxy = old_proxy
    changed = original.model_copy(update={"proxy": new_proxy})
    previous = save(tmp_path, original, folder="failure", age=1)
    fixture_id = previous.requests[0].fixture_id

    def history(result):
        return next(row for row in result["correctness_history"]
                    if row.get("fixture_id") == fixture_id)

    assert history(plan(tmp_path, original))["status"] == "warning"
    assert history(plan(tmp_path, changed))["status"] == "unknown"
    save(tmp_path, changed, folder="success", score=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)),
                                 base_url="http://localhost") as client:
        for cfg, expected_status, expected_run in (
            (original, "warning", "failure"), (changed, "observed", "success"),
        ):
            response = await client.post("/api/config/preview", json=cfg.model_dump(mode="json"))
            assert response.status_code == 200
            for result in (plan(tmp_path, cfg), response.json()["request_plan"]):
                row = history(result)
                assert row["status"] == expected_status
                assert row["run_id"] == expected_run
                assert row["failed_request_ids"] == (
                    ["failed-answer"] if expected_status == "warning" else []
                )


@pytest.mark.parametrize("builtin,legacy_key", [
    (False, "62cdd52032c73ae4d757fac28d13df673cdb051524dddba986a5f8905880d49e"),
    (True, "ac6acdf0d14f4b49794ab4ceec5be4472267cf5e20b6b5bb99427aba7295263e"),
])
def test_proxy_unqualified_version_two_history_is_unknown(tmp_path, builtin, legacy_key):
    cfg = config(builtin=builtin)
    save(tmp_path, cfg)
    metadata_path = tmp_path / "previous" / ".web.json"
    metadata = json.loads(metadata_path.read_text())
    # Keys written by 5c5374c before configured proxy became part of the context.
    metadata["latency_summary"]["targets"][0]["correctness_history"][0]["fixture_key"] = legacy_key
    metadata_path.write_text(json.dumps(metadata))
    assert all(row["status"] == "unknown" for row in plan(tmp_path, cfg)["correctness_history"])


def test_proxy_identity_is_only_a_digest_in_compact_history(tmp_path, monkeypatch):
    from giraffe.correctness_history import fixture_keys

    cfg = config()
    cfg.proxy = "http://private-proxy.internal:8080"
    expected = fixture_keys(cfg)
    monkeypatch.setenv("HTTPS_PROXY", "http://secret-user:secret-password@other-proxy.internal:8080")
    assert fixture_keys(cfg) == expected
    save(tmp_path, cfg)
    metadata = (tmp_path / "previous" / ".web.json").read_text()
    for private in (cfg.proxy, "private-proxy", "secret-user", "secret-password", "other-proxy"):
        assert private not in metadata
    saved = json.loads(metadata)["latency_summary"]["targets"][0]["correctness_history"][0]
    assert saved["fixture_key"] == expected["pack:question"]["key"]
    assert len(saved["fixture_key"]) == 64


def test_correctness_history_keeps_latency_measurements_contract(tmp_path):
    from giraffe.latency import latency_summary

    saved = latency_summary(save(tmp_path))["targets"][0]
    assert saved["measurements"]
    assert all(row["p50_ms"] <= row["p90_ms"] <= row["p99_ms"] <= row["max_ms"]
               for row in saved["measurements"])
    assert len(saved["correctness_history"]) == 1
    assert saved["correctness_history"][0]["failed_request_ids"] == ["failed-answer"]


@pytest.mark.parametrize("kind", ["stale", "unknown_deployment", "invalid", "unscored", "evaluation_error"])
def test_unknown_or_ineligible_evidence_cannot_warn(tmp_path, kind):
    cfg = config()
    if kind == "unknown_deployment":
        cfg.targets[0].deployment.runtime = {}
    changes = {"valid": False} if kind == "invalid" else {"score": None} if kind == "unscored" else {"evaluation_error": "judge unavailable"} if kind == "evaluation_error" else {}
    save(tmp_path, cfg, age=31 if kind == "stale" else 0, record_changes=changes)
    assert plan(tmp_path, cfg)["correctness_history"][0]["status"] == "unknown"


async def test_preview_links_retained_failed_answer(tmp_path):
    from giraffe.web import create_app
    save(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)), base_url="http://localhost") as client:
        response = await client.post("/api/config/preview", json=config().model_dump(mode="json"))
        assert response.status_code == 200
        row = response.json()["request_plan"]["correctness_history"][0]
        answer = await client.get(f"/api/runs/{row['run_id']}/requests/{row['failed_request_ids'][0]}")
    assert answer.status_code == 200 and answer.json()["output"] == "Private wrong answer"


def test_browser_history_links_and_try_one_use_existing_dialog():
    from tests.test_ux_setup import run_browser_logic
    run_browser_logic(r"""
const config={request_pack:{cases:[{fixture_id:'question'}]}};
const html=renderCorrectnessHistory(config,{correctness_history:[{status:'warning',message:'<message>',run_id:'saved/run',failed_request_ids:['bad/id'],case_id:'question',target:'second'}]});
assert.ok(html.includes('&lt;message&gt;'));
assert.ok(html.includes('#runs/saved%2Frun/requests/bad%2Fid'));
assert.ok(html.includes('data-action="try-example"'));
assert.ok(html.includes('data-try-target="second"'));
assert.equal(renderCorrectnessHistory(config,{correctness_history:[{status:'unknown'}]}),'');
state.config={...config,targets:[{name:'first',model:'tiny'},{name:'second',model:'tiny'}]};
state.jsonDraft=state.jsonBase='synced';captureFormDraft=()=>{};
nodes['#dataset-try-form']={elements:{try_target:{value:'first'}}};
datasetDialog=()=>{};
openTryExample(0,'second');
assert.equal(nodes['#dataset-try-form'].elements.try_target.value,'second');
assert.equal(state.datasetTry.fixture_id,'question');
""")


def test_failed_answer_deep_link_opens_request_after_run_load_and_same_run_navigation():
    from tests.test_ui_navigation import run_browser_script
    run_browser_script(r"""
const page=browser();await page.run('init()');
await page.run(`(async()=>{
let opened=[];openRequest=async id=>opened.push(id);
assert.equal(reportRoute('#runs/previous/results/load-acceptance').section,'load-acceptance');
assert.equal(reportRoute('#runs/previous/results/unknown-section'),null);
assert.equal(reportRoute('#runs/previous/requests/%ZZ'),null);
assert.equal(reportRoute('#runs/%ZZ/requests/answer'),null);
location.hash='#runs/previous/requests/bad%2Fid';await route();
assert.equal(state.detail.id,'previous');assert.equal(opened[0],'bad/id');
location.hash='#runs/previous/requests/second';await route();
assert.deepEqual(opened,['bad/id','second']);
})()`);
""")


def test_changed_evaluator_does_not_reuse_judged_failure(tmp_path):
    cfg = config()
    cfg.request_pack.cases[0].scorer = "judge"
    from giraffe.models import JudgingConfig
    cfg.judging = JudgingConfig.model_validate({"judges": [{"name": "judge", "url": "http://judge", "model": "one"}]})
    save(tmp_path, cfg)
    assert plan(tmp_path, cfg)["correctness_history"][0]["status"] == "warning"
    cfg.judging.judges[0].model = "two"
    assert plan(tmp_path, cfg)["correctness_history"][0]["status"] == "unknown"


@pytest.mark.parametrize("builtin", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
async def test_shared_endpoint_keeps_failures_and_answer_links_with_their_target(
    tmp_path, monkeypatch, builtin, reverse,
):
    from giraffe.client import _headers
    from giraffe.web import create_app

    cfg = config(builtin=builtin)
    values = cfg.model_dump(mode="json")
    base = values["targets"][0]
    values["targets"] = [{**base, "name": name, "identity": {"tenant": name},
                          "headers_env": {"X-Tenant": f"TENANT_{name.upper()}"}}
                         for name in (["bob", "alice"] if reverse else ["alice", "bob"])]
    for name in ("alice", "bob"):
        monkeypatch.setenv(f"TENANT_{name.upper()}", f"private-{name}-route")
    report = save(tmp_path, cfg)
    cfg = RunConfig.model_validate(values)
    assert _headers(cfg.targets[0])["X-Tenant"] != _headers(cfg.targets[1])["X-Tenant"]
    report.manifest["config"] = cfg.model_dump(mode="json")
    original = report.requests[0]
    report.requests = [original.model_copy(update={"id": f"{target.name}-answer", "target": target.name,
                                                   "score": target.name == "bob"}) for target in cfg.targets]
    write_report(report, tmp_path / "previous")
    direct = plan(tmp_path, cfg)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)),
                                 base_url="http://localhost") as client:
        response = await client.post("/api/config/preview", json=cfg.model_dump(mode="json"))
        assert response.status_code == 200
        for result in (direct, response.json()["request_plan"]):
            rows = {row["target"]: row for row in result["correctness_history"]
                    if row.get("fixture_id") == original.fixture_id}
            assert rows["alice"]["status"] == "warning"
            assert rows["alice"]["failed_request_ids"] == ["alice-answer"]
            assert rows["bob"]["status"] == "observed"
            assert rows["bob"]["failed_request_ids"] == []
            assert rows["alice"]["message"] in result["warnings"]
            answer = await client.get(f"/api/runs/{rows['alice']['run_id']}/requests/alice-answer")
            assert answer.status_code == 200 and answer.json()["target"] == "alice"
    cfg.targets[0].headers_env = {"X-Tenant": "CHANGED_ROUTE"}
    assert all(row["status"] == "unknown" for row in plan(tmp_path, cfg)["correctness_history"])
    assert "private-" not in (tmp_path / "previous" / ".web.json").read_text()


def test_combined_histories_survive_storage_and_render_visible_actions(tmp_path):
    from tests.test_arrival_history import config as arrival_config, save as save_arrival
    from tests.test_setup_async_regressions import SETUP
    from tests.test_ux_results import run_render_checks

    cfg = config()
    cfg.checks.append("load")
    cfg.load_test = arrival_config().load_test
    cfg.targets.append(cfg.targets[0].model_copy(update={"name": "second", "url": "http://second/v1"}))
    report = save(tmp_path, cfg, folder="source-run",
                  record_changes={"target": "second", "id": "failed/answer"})
    report.observations = save_arrival(tmp_path, cfg, folder="source-run").observations
    write_report(report, tmp_path / "source-run")
    saved = json.loads((tmp_path / "source-run" / ".web.json").read_text())["latency_summary"]
    assert len(saved["targets"]) == 2
    local, second = saved["targets"]
    assert len(local["arrival_history"]) == 1
    assert local["correctness_history"] == []
    assert second["arrival_history"] == []
    assert second["correctness_history"][0]["failed_request_ids"] == ["failed/answer"]
    assert second["measurements"]
    value = plan(tmp_path, cfg)
    assert any(row["status"] == "warning" for row in value["arrival_history"])
    assert any(row["status"] == "warning" for row in value["correctness_history"])
    setup = SETUP.replace("renderRequestPreview=", "")
    run_render_checks(setup + "\nconst config=" + cfg.model_dump_json()
                      + ";const plan=" + json.dumps(value) + r''';
state.config=config;state.jsonDraft=state.jsonBase='synced';renderNew();
state.setupStep=3;syncSetupStep();
const original=JSON.stringify(state.config);
const arrival=plan.arrival_history.find(row=>row.status==='warning');
const correctness=plan.correctness_history.find(row=>row.status==='warning');
api=()=>{throw new Error('Viewing history and opening Try one must not send requests');};
function current(){
  state.jsonDraft=state.jsonBase='synced';
  state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,config,plan};
  renderRequestPlan(config);
}
function visible(){
  const outlet=$('#setup-history');assert(outlet&&!outlet.hidden);
  assert(outlet.querySelector('.arrival-history'));
  const warning=outlet.querySelector('.notice.warning');assert(warning,'Correctness warning must be in the visible outlet');
  for(let ancestor=outlet;ancestor;ancestor=ancestor.parentNode){
    assert(!ancestor.hidden);assert(ancestor.tagName!=='DETAILS'||ancestor.open);
  }
  assert(outlet.closest('#setup-panel-3'));assert.equal(state.setupStep,3);
  for(const row of [arrival,correctness])assert.equal($('#main').textContent.split(row.message).length-1,1);
  assert.equal(warning.querySelector('a').getAttribute('href'),'#runs/source-run/requests/failed%2Fanswer');
  assert.equal(warning.querySelector('button').dataset.tryTarget,'second');
}
function cleared(){assert($('#setup-history').hidden);assert.equal($('#setup-history').textContent,'');}
current();visible();
configChanged();cleared();current();visible();
for(const status of ['pending','error']){state.requestPreview.status=status;renderRequestPreview();cleared();current();visible();}
state.routeVersion++;renderRequestPreview();cleared();current();visible();
state.jsonDraft='unapplied';configChanged();cleared();current();visible();
state.requestPreview.plan={...plan,arrival_history:[],correctness_history:[{status:'unknown'}]};renderRequestPreview();cleared();
current();visible();assert.equal(JSON.stringify(state.config),original);
const dialog=document.createElement('dialog');dialog.setAttribute('id','dataset-dialog');document.body.insertBefore(dialog,null);
const beforeTry=JSON.stringify(readForm().config);
(async()=>{
await listeners.click({target:$('#setup-history .notice button')});
assert(dialog.open);assert.equal($('#dataset-try-form').elements.try_target.value,'second');
assert.equal(state.datasetTry.fixture_id,'question');assert.equal(JSON.stringify(state.config),beforeTry);
})()
''', dom=True)


def test_failed_answer_deep_link_survives_initial_app_routing():
    from tests.test_ui_navigation import run_browser_script

    run_browser_script(r"""
const page=browser(undefined,'#runs/previous%2Frun/requests/bad%2Fid');
await page.run(`let opened=[];openRequest=async id=>opened.push(id);init()`);
await page.run(`
assert.equal(location.hash,'#runs/previous%2Frun/requests/bad%2Fid');
assert.equal(state.detail.id,'previous/run');assert.equal(opened[0],'bad/id');
assert.equal(reportView.tab,'results');
`);
""")
