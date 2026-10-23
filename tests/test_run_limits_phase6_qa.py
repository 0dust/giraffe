"""Regressions from the orchestrator's real Chromium review."""

import json

import pytest

from giraffe.cli import main
from giraffe.models import CATALOGUE_NAMES, RunConfig, RunReport
from giraffe.planning import request_plan
from tests.test_ux_catalogue import browser_logic

TARGET = {"name": "local", "url": "http://localhost/v1", "model": "test"}


def test_limit_names_match_bounds_and_edit_destinations():
    browser_logic(r"""
const labels={max_requests:'Stop after this many requests',max_duration_seconds:'Stop test traffic after',concurrency:'Limit simultaneous requests'};
const markup=renderPlanBounds(config)+renderSharedRunSettings(config);
for(const [path,label] of Object.entries(labels)){
  assert(markup.includes('<dt>'+label+'</dt>'));
  assert.equal(settingDestination(path).label,label);
  assert(settingActions([path]).includes('Edit '+label));
}
assert.doesNotMatch(markup,/Request ceiling|Traffic time limit|Maximum at once/);
""")


def test_budget_copy_uses_catalogue_names_and_grouped_numbers_without_changing_rows():
    configured = RunConfig(targets=[TARGET], checks=["access", "correctness", "load"],
                           max_requests=1, load_test={
                               "workloads": {"combinations": {"selected": ["short_short"]}},
                               "patterns": {"concurrent": {"enabled": False}, "rates": [100]},
                               "schedule": {"duration_seconds": 1000}})
    plan = request_plan(configured)
    assert plan["planned_total"] == 100007  # 100k arrivals + connection + answers; local sizing sends no requests.
    assert [(row["id"], row["title"]) for row in plan["rows"]] == [
        ("initial", "Connection check"), ("access", "Network and access"),
        ("correctness", "Known-answer correctness"),
        ("load_combinations", "Input → output combinations"), ("load_calibration", "Local prompt preparation")]
    notice = next(item for item in plan["request_warnings"] if item["kind"] == "incomplete_coverage")
    assert notice["actions"][0] == {"label": "Use 100,007 total requests", "field": "max_requests", "value": 100007}
    assert "Answer correctness, Load test" in notice["msg"]
    assert [action["label"] for action in notice["actions"][1:]] == [
        "Edit Answer correctness", "Edit Load test"]
    larger_budget = request_plan(configured.model_copy(update={"max_requests": 10000}))
    larger_notice = next(item for item in larger_budget["request_warnings"] if item["kind"] == "incomplete_coverage")
    assert "request limit (Stop after this many requests: 10,000)" in larger_notice["msg"]
    assert larger_notice["values"]["max_requests"] == 10000
    browser_logic(r"""
const plan=PLAN;state.jsonDraft=state.jsonBase='synced';
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan};
nodes['#request-plan']={};renderRequestPlan(config);
for(const markup of [renderRunBlueprint(config,plan),nodes['#request-plan'].innerHTML]){
  assert.match(markup,/Use 100,007 total requests/);
  assert.match(markup,/Answer correctness, Load test/);
  assert.match(markup,/data-budget="100007"/);
}
const elements={max_requests:{value:'1',disabled:true},max_requests_enabled:{checked:false}};
nodes['#new-run-form']={elements};nodes['#config-error']={};configChanged=updateBounds=()=>{};
await listeners.click({target:{closest:q=>q==='[data-action]'?{dataset:{action:'use-request-budget'}}:q==='[data-budget]'?{dataset:{budget:'100007'}}:null}});
assert.equal(elements.max_requests.value,100007);assert.equal(elements.max_requests_enabled.checked,true);
assert.equal(elements.max_requests.disabled,false);assert.equal(elements.max_requests.required,true);
""".replace('PLAN', json.dumps(plan)), configured)


def test_warning_names_match_every_catalogue_entry():
    browser_logic(r"""
for(const [check,name] of Object.entries(NAMES))assert.equal(catalogTitle(check),name);
""".replace('NAMES', json.dumps(CATALOGUE_NAMES)))


def test_time_budget_warning_formats_action_without_changing_numeric_value():
    configured = RunConfig(targets=[TARGET], checks=["load"], max_duration_seconds=1,
                           load_test={"workloads": {"combinations": {"selected": ["short_short"]}},
                                      "patterns": {"concurrent": {"enabled": False}, "rates": [25]},
                                      "schedule": {"duration_seconds": 3600}})
    warning = request_plan(configured)["time_warnings"][0]
    assert warning["actions"][0] == {"label": "Use 3,900 s", "field": "max_duration_seconds", "value": 3900}
    assert "Load test" in warning["msg"]
    assert warning["values"]["minimum_seconds"] == 3600
    assert warning["values"]["suggested_seconds"] == 3900
    assert warning["values"]["fully_known"] is True  # One sending window plus the 300-second request timeout.


@pytest.mark.parametrize("command", ["validate", "preview", "run"])
def test_cli_limit_labels_and_grouped_request_count(command, tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.json"
    configured = RunConfig(targets=[TARGET], checks=["correctness"], max_requests=100007,
                           max_duration_seconds=3600, concurrency=40)
    path.write_text(configured.model_dump_json())

    async def run(config, **kwargs):
        return RunReport(run_id="label-qa", started_at="now", finished_at="now", overall="pass",
                         manifest={"config": config.model_dump(mode="json")})

    monkeypatch.setattr("giraffe.cli._run", run)
    args = [command, "--config", str(path), "--output-format", "text"]
    if command == "run":
        args += ["--skip-preflight", "--output", str(tmp_path / "run")]
    assert main(args) == 0
    captured = capsys.readouterr()
    text = captured.err if command == "run" else captured.out
    assert "Stop after this many requests: 100,007" in text
    if command == "run":
        assert "Limit simultaneous requests: 40" in text
        assert "Stop test traffic after: 3,600s" in text


def test_overlap_warning_groups_request_count_and_keeps_values_numeric():
    configured = RunConfig(targets=[{**TARGET, "name": name} for name in ["one", "two", "three"]],
                           checks=["correctness"], overlap_models=True, concurrency=512,
                           test_options={"correctness": {"concurrency": 512}})
    warning, = request_plan(configured)["concurrency_warnings"]
    assert warning["msg"] == (
        "Answer correctness: Shared across 3 endpoints: up to 1,536 requests may wait for the 512-request limit.")
    assert warning["values"] == {"test_options.correctness.concurrency": 512, "concurrency": 512}
