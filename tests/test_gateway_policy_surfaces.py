"""Planning and baseline explanations preserve policy execution semantics."""
import pytest

from giraffe.models import required_concurrency
from giraffe.planning import request_plan
from giraffe.reporting import compare_baseline
from tests.test_gateway_policy import exercise
from tests.test_ux_results import run_render_checks


@pytest.mark.parametrize("status", ["fail", "inconclusive"])
@pytest.mark.parametrize("warning_mode", ["absent", "empty", "other", "matching"])
def test_policy_baseline_comparison_has_own_section_and_escaped_reason(status, warning_mode):
    run_render_checks("const status='" + status + "',warningMode='" + warning_mode + "';" + r'''
const row={check:'gateway_policy',metric:'policy_acceptance',aggregation:'observed',
 status,baseline:'observed',current:'observed',baseline_samples:13,current_samples:13,
 reason:'  Over-admitted <script>quota</script> & "limited"  '};
if(warningMode==='empty')row.warnings=[];
if(warningMode==='other')row.warnings=[{message:'Separate policy evidence warning'}];
if(warningMode==='matching')row.warnings=[{message:row.reason.trim()}];
const expectHint=warningMode==='other'||status==='fail'&&['absent','empty'].includes(warningMode);
const result=report([],{baseline:{run_id:'reference',targets:[{target:'local',comparisons:[row]}]}});
state.detail={report:result};
assert(resultsSections().some(([id,label,parent])=>id==='gateway-policy-outcomes'&&
 label==='Gateway policy outcomes'&&parent==='comparison'));
assert(!resultsSections().some(([id])=>id==='timing'||id==='gateway-attribution-outcomes'));
for(const [scope,prefix] of [['results','results'],['performance','performance-comparison']]){
 const html=renderComparison(result,{scope});
 const container=document.createElement('div');container.innerHTML=html;
 const section=container.querySelector('#'+prefix+'-gateway-policy-outcomes');
 assert(section);
 assert.equal(section.querySelector('h3').textContent,'Gateway policy outcomes');
 assert(section.querySelector('[data-comparison-group="gateway_policy"] [data-metric="policy_acceptance"]'));
 assert.equal(section.querySelector('h4').textContent,'Policy acceptance');
 assert.match(html,/Over-admitted &lt;script&gt;quota&lt;\/script&gt; &amp; &quot;limited&quot;/);
 const article=section.querySelector('[data-metric]'),hint=article.querySelector('.hint');
 assert.equal(Boolean(hint),expectHint);
 if(expectHint){
  assert.equal(hint.textContent,row.reason);
  assert.equal(article.querySelector('.comparison-value-grid').nextSibling,hint);
 }
 assert.equal(container.querySelector('script'),null);
 assert.doesNotMatch(html,/Response timing/);
 // The existing collapsed warning details repeat the summary as supporting evidence.
 for(const details of article.querySelectorAll('details:not([open])'))details.parentNode.removeChild(details);
 assert.equal(article.textContent.split(row.reason.trim()).length-1,1);
}
state.comparisonCheck='gateway_attribution';
const filtered=renderComparison(result);
assert.match(filtered,/id="results-gateway-policy-outcomes"/);
assert.match(filtered,/No measurements match these filters/);
assert.doesNotMatch(filtered,/data-metric="policy_acceptance"/);
''', dom=True)


def test_policy_baseline_reason_is_only_shown_for_nonpassing_rows_with_text():
    run_render_checks(r'''
const row={check:'gateway_policy',metric:'policy_acceptance',aggregation:'observed',
 baseline:'observed',current:'observed',baseline_samples:13,current_samples:13};
const passing=comparisonTable([{...row,status:'pass',reason:'Policy accepted <script>quota</script>'}]);
assert.doesNotMatch(passing,/<p class="hint">|Policy accepted|<script>/);
for(const status of ['fail','inconclusive'])for(const reason of ['', '   ', null, undefined, 42, {}]){
 assert.doesNotMatch(comparisonTable([{...row,status,reason}]),/<p class="hint">/);
}
''')


def test_policy_comparison_preserves_attribution_and_timing_sections():
    run_render_checks(r'''
const common={aggregation:'observed',status:'fail',baseline_samples:1,current_samples:1};
const rows=[{...common,check:'gateway_policy',metric:'policy_acceptance',
 baseline:'observed',current:'observed',reason:'Policy quota exceeded'},
 {...common,check:'gateway_attribution',metric:'attribution_outcome',scenario:'alice',
 baseline:'verified',current:'mismatch',reason:'Attribution mismatch'},
 {...common,check:'serving',metric:'latency_ms',aggregation:'median',
 baseline:100,current:200,reason:'Latency increased'}];
const result=report([],{baseline:{run_id:'reference',targets:[{target:'local',comparisons:rows}]}});
const container=document.createElement('div');container.innerHTML=renderComparison(result);
for(const [id,title,metric] of [
 ['gateway-policy-outcomes','Gateway policy outcomes','policy_acceptance'],
 ['gateway-attribution-outcomes','Gateway attribution outcomes','attribution_outcome'],
 ['timing','Response timing','latency_ms']]){
 const section=container.querySelector('#results-'+id);
 assert.equal(section.querySelector('h3').textContent,title);
 assert.equal(section.querySelectorAll('[data-metric]').length,1);
 assert(section.querySelector('[data-metric="'+metric+'"]'));
 if(metric!=='policy_acceptance')assert.equal(section.querySelector('[data-metric] .hint'),null);
}
const titles=Array.from(container.querySelectorAll('.comparison-section h3'),node=>node.textContent);
assert.equal(titles.indexOf('Gateway policy outcomes'),titles.indexOf('Gateway attribution outcomes')+1);
state.detail={report:report([],{baseline:{targets:[]}})};
assert(!resultsSections().some(([id])=>id==='gateway-policy-outcomes'));
assert.doesNotMatch(renderComparison(state.detail.report),/Gateway policy outcomes/);
''', dom=True)


@pytest.mark.parametrize("settings", [{"gateway": "unsupported"}, {"capability": "unsupported"}])
async def test_unsupported_policy_plans_no_requests_time_or_slots(monkeypatch, settings):
    check, run, sent = await exercise(monkeypatch, **settings)
    assert not sent
    assert check.metrics["gateway_policy"]["qualification"] == "unavailable"
    plan = request_plan(run.config)
    row = next(row for row in plan["rows"] if row["id"] == "gateway_policy")
    assert row["requests_per_target"] == 0
    assert row["kind"] == "exact"
    assert "unsupported" in row["summary"].lower()
    assert plan["minimum_seconds"] == 0
    assert required_concurrency(run.config)["by_check"]["gateway_policy"] == 0


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("behavior", ["ignore", "unknown"])
async def test_baseline_preserves_current_inconclusive_policy_reason(monkeypatch, behavior):
    with monkeypatch.context() as context:
        _, baseline, _ = await exercise(context, full=True)
    with monkeypatch.context() as context:
        current_check, current, _ = await exercise(context, full=True, behavior=behavior)
    assert current_check.status == "inconclusive"
    compared = compare_baseline(current, baseline)
    rows = [row for row in compared.baseline["targets"][0]["comparisons"] if row["metric"] == "policy_acceptance"]
    row, = rows
    assert row["status"] == "inconclusive"
    assert row["reason"] == current_check.summary


@pytest.mark.usefixtures("local_tokenizer")
async def test_baseline_counts_one_policy_failure_and_keeps_each_identity_outcome(monkeypatch):
    import json

    from giraffe.reporting import _baseline_table_html
    from tests.test_ux_catalogue import browser_logic

    with monkeypatch.context() as context:
        baseline_check, baseline, _ = await exercise(context, full=True)
    with monkeypatch.context() as context:
        current_check, current, _ = await exercise(context, full=True, behavior="overadmit")

    assert baseline_check.status == "pass"
    assert current_check.status == "fail"
    assert current_check.metrics["gateway_policy"]["peer_continuation"] is True
    comparison = compare_baseline(current, baseline).baseline
    target = comparison["targets"][0]
    assert comparison["status"] == target["status"] == "fail"
    assert "1 material metric regression(s)" in target["summary"]
    row, = target["comparisons"]
    assert row["metric"] == "policy_acceptance" and row["status"] == "fail"
    assert row["baseline_samples"] == row["current_samples"] == 13
    for side, check in (("baseline", baseline_check), ("current", current_check)):
        assert row[f"{side}_outcomes"] == check.metrics["gateway_policy"]["users"]
    assert [user["admitted"] for user in row["baseline_outcomes"]] == [2, 8]
    assert [user["admitted"] for user in row["current_outcomes"]] == [3, 8]

    html = _baseline_table_html(comparison)
    assert html.count(">Policy acceptance<") == 1
    assert "limited · 2 admitted · 3 rejected" in html
    assert "limited · 3 admitted · 2 rejected" in html
    assert html.count("peer · 8 admitted · 0 rejected") == 2

    browser_logic("const rows=" + json.dumps([row]) + """
const html=comparisonTable(rows);
assert.equal((html.match(/Observed failure/g)||[]).length,1);
assert.match(html,/limited · 2 admitted · 3 rejected/);
assert.match(html,/limited · 3 admitted · 2 rejected/);
assert.equal((html.match(/peer · 8 admitted · 0 rejected/g)||[]).length,2);
""")


@pytest.mark.usefixtures("local_tokenizer")
async def test_matching_legacy_policy_summaries_do_not_establish_a_baseline(monkeypatch):
    check, report, _ = await exercise(monkeypatch, full=True)
    assert check.status == "pass"
    assert compare_baseline(report, report.model_copy(deep=True)).baseline["status"] == "pass"

    check.metrics["gateway_policy"]["version"] = 7
    compared = compare_baseline(report, report.model_copy(deep=True)).baseline
    assert compared["status"] == "inconclusive"
    assert all(row["status"] == "inconclusive" for row in compared["targets"][0]["comparisons"])


@pytest.mark.usefixtures("local_tokenizer")
async def test_policy_baseline_attests_source_separately_from_shared_work(monkeypatch):
    check, report, _ = await exercise(monkeypatch, full=True)
    assert check.status == "pass"

    legacy = report.model_copy(deep=True)
    legacy.manifest.pop("gateway_policy_source_sha256")
    legacy.manifest["fixture_pack"].pop("shared_workload_fixture_sha256")
    assert compare_baseline(report, legacy).baseline["status"] == "pass"

    different = report.model_copy(deep=True)
    different.manifest["gateway_policy_source_sha256"] = "0" * 64
    compared = compare_baseline(report, different).baseline
    assert compared["status"] == "inconclusive"
    assert all(row["status"] == "inconclusive" for row in compared["targets"][0]["comparisons"])


@pytest.mark.usefixtures("local_tokenizer")
async def test_blocked_policy_check_cannot_establish_baseline_comparison(monkeypatch):
    from giraffe import runner

    with monkeypatch.context() as context:
        successful_check, successful, _ = await exercise(context, full=True)
    assert successful_check.status == "pass"
    metrics = runner._metrics

    async def fail_end_metrics(run, target, phase):
        if phase == "end":
            raise RuntimeError("end telemetry fixture failed")
        return await metrics(run, target, phase)

    with monkeypatch.context() as context:
        context.setattr(runner, "_metrics", fail_end_metrics)
        blocked_check, blocked, _ = await exercise(context, full=True)
    assert blocked_check.status == "blocked"
    assert blocked_check.metrics["gateway_policy"]["qualification"] == "observed"

    for current, baseline in ((successful, blocked), (blocked, successful)):
        comparison = compare_baseline(current, baseline).baseline
        assert comparison["status"] == "inconclusive"
        row, = comparison["targets"][0]["comparisons"]
        assert row["status"] == "inconclusive"


def test_policy_plan_follows_shared_workloads():
    from giraffe.models import RunConfig
    from tests.test_gateway_policy import configuration

    raw = configuration().model_dump()
    raw.update(checks=["gateway_policy", "multi_user", "sessions", "consistency", "tools"],
               multi_user={"levels": [1]}, sessions={"modes": ["fixed"]}, consistency={})
    rows = [row["id"] for row in request_plan(RunConfig.model_validate(raw))["rows"]]
    assert rows == ["multi_user_1", "sessions", "consistency", "tools", "gateway_policy"]


@pytest.mark.parametrize("mode", ["key_rpm", "priority_rpm"])
def test_supported_policy_keeps_declared_plan(mode):
    from tests.test_gateway_policy import configuration

    config = configuration(mode)
    plan = request_plan(config)
    row = next(row for row in plan["rows"] if row["id"] == "gateway_policy")
    assert row["requests_per_target"] == 13
    assert row["kind"] == "exact"
    assert plan["minimum_seconds"] == 2
    assert required_concurrency(config)["by_check"]["gateway_policy"] == 2


@pytest.mark.parametrize("mode", ["key_rpm", "priority_rpm"])
def test_finite_policy_plan_requires_only_its_declared_request_cap(mode):
    from tests.test_gateway_policy import configuration

    config = configuration(mode).model_copy(update={"max_requests": 13})
    plan = request_plan(config)
    assert plan["planned_total"] == plan["known_total"] == plan["required_request_limit"] == 13
    assert plan["count_kind"] == "exact"
    assert plan["request_warnings"] == []
    truncated = request_plan(config.model_copy(update={"max_requests": 12}))
    assert [warning["kind"] for warning in truncated["request_warnings"]] == ["incomplete_coverage"]
    assert truncated["request_warnings"][0]["values"]["required_request_limit"] == 13
