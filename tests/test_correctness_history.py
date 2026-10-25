from datetime import datetime, timedelta, timezone

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
