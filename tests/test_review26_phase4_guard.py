"""Resource previews stay portable; only traffic start enforces process limits."""

import json

import pytest

from giraffe import open_files
from giraffe.cli import main
from giraffe.models import RunConfig
from tests.test_run_limits_phase4_integration import limits as limits
from tests.test_ux_catalogue import browser_logic
from tests.test_web import browser, finished, report, successful_runner


def config(**values):
    return RunConfig(targets=[{"name": "local", "url": "http://localhost/v1", "model": "test",
                               "restart_command": ["true"]}],
                     checks=["correctness"], default_concurrency=40, **values)


async def test_real_validated_snapshot_copy_cli_accepts_resource_block(tmp_path, limits):
    limits["hard"] = 70
    cfg = config()
    async with browser(tmp_path) as client:
        validated = await client.post("/api/config/validate", json=cfg.model_dump(mode="json"))
        assert validated.status_code == 200, validated.text
        normalized = validated.json()
        copied = await client.post("/api/config/cli", json={"config": normalized["config"], "mode": "normal"})
        assert copied.status_code == 200, copied.text
        command = copied.json()
        started = await client.post("/api/runs", json={"config": normalized["config"]})
        assert started.status_code == 422, started.text
    assert normalized["request_plan"]["errors"] == []
    assert normalized["request_plan"]["resource_issues"][0]["blocking"] is True
    # Run the application's real copyCli -> validatedSnapshot path using the
    # actual ASGI responses, rather than replacing validation with a no-op.
    browser_logic("""
nodes['#config-error']={hidden:true};nodes['#cli-fallback']={hidden:true};
nodes['#cli-command']={value:'',focus(){},select(){}};
nodes['#toast']={};
state.jsonDraft=state.jsonBase='synced';
state.requestPreview={status:'current',revision:state.configRevision,route:state.routeVersion,plan:VALIDATED.request_plan};
readForm=()=>({config:structuredClone(config),mode:'normal'});
const paths=[];
api=async(path,options)=>{
  paths.push(path);
  if(path==='/api/config/validate')return VALIDATED;
  assert.equal(path,'/api/config/cli');return COPIED;
};
assert.equal(currentSettingErrors().length,0);
assert.equal(canStartRun(),false);
assert.match(renderPlanTime(VALIDATED.request_plan),/Restart `giraffe web`/);
await copyCli();
assert.equal(JSON.stringify(paths),JSON.stringify(['/api/config/validate','/api/config/cli']));
assert.equal(nodes['#config-error'].hidden,true);
assert.equal(nodes['#cli-fallback'].hidden,false);
assert.equal(nodes['#cli-command'].value,COPIED.command);
""".replace("VALIDATED", json.dumps(normalized)).replace("COPIED", json.dumps(command)), cfg)


@pytest.mark.parametrize("checks,source", [(["correctness"], "builtin"), (["access"], "request_pack")])
def test_inactive_judges_contribute_no_sockets(limits, checks, source):
    cfg = config(request_source=source, request_pack={"version": 1, "cases": [{
        "fixture_id": "one", "scorer": "judge", "expected": "answer",
        "messages": [{"role": "user", "content": "Question"}]}]},
        judging={"concurrency": 100, "judges": [{"name": "judge", "url": "http://judge", "model": "m"}]})
    cfg.checks = checks
    guarded = open_files.open_file_guard(cfg)
    assert guarded["counts"]["judging_sockets"] == 0
    assert guarded["counts"]["socket_peak"] == guarded["counts"]["target_sockets"]


def test_no_judge_endpoints_contribute_no_sockets(limits):
    guarded = open_files.open_file_guard(config(judging={"concurrency": 100}))
    assert guarded["counts"]["judging_sockets"] == 0
    assert guarded["need"] == 84
    assert all(item["count"] > 0 for item in guarded["contributors"])
    notice, = guarded["warnings"]
    assert len(notice["actions"]) == 1
    assert notice["actions"][0]["label"] == "Edit target sockets"


@pytest.mark.parametrize("hard,expected", [(4096, 84), (80, 80), (-1, 84)])
def test_macos_failed_sysctl_fallback_raises_above_real_soft_limit(limits, monkeypatch, hard, expected):
    def unavailable(*args, **kwargs):
        raise OSError("sysctl unavailable")

    monkeypatch.setattr(open_files.sys, "platform", "darwin")
    monkeypatch.setattr(open_files.subprocess, "check_output", unavailable)
    monkeypatch.setattr(open_files.os, "sysconf", lambda key: limits["soft"])
    limits.update(soft=64, hard=hard)
    guarded = open_files.open_file_guard(config(), raise_limit=True)
    assert limits["calls"] == [(expected, hard)]
    assert guarded["effective_hard"] == (hard if hard != -1 else 10240)
    assert bool(guarded["errors"]) is (hard == 80)


@pytest.mark.parametrize("command", ["cold-start", "try-example"])
@pytest.mark.parametrize("hard", [70, 4096])
def test_other_traffic_cli_commands_guard_before_dispatch(tmp_path, monkeypatch, capsys, limits, command, hard):
    limits["hard"] = hard
    cfg = config(request_source="request_pack", request_pack={"version": 1, "cases": [{
        "fixture_id": "one", "scorer": "none", "messages": [{"role": "user", "content": "Question"}]}]})
    path = tmp_path / "config.json"
    path.write_text(cfg.model_dump_json())
    calls = []

    async def run(configuration, **kwargs):
        assert limits["calls"] == [(84, 4096)]
        calls.append(configuration)
        return report(configuration)

    monkeypatch.setattr("giraffe.cli._run", run)
    flags = ["--restart", "local"] if command == "cold-start" else ["--case", "one", "--target", "local"]
    destination = tmp_path / "saved"
    result = main([command, "--config", str(path), "--output", str(destination), *flags])
    assert result == (3 if hard == 70 else 0)
    if hard == 70:
        assert not calls
        assert limits["calls"] == [(70, 70)]
        assert "needs 84 open files" in capsys.readouterr().err
        assert not (destination / "report.json").exists()
    else:
        assert len(calls) == 1
        saved = json.loads((destination / "report.json").read_text())
        assert saved["manifest"]["open_files"]["after"]["soft"] == 84


async def test_web_summary_and_lifecycle_metadata_share_one_lock(tmp_path, monkeypatch, limits):
    from giraffe import web

    from giraffe import storage
    metadata_lock = storage.metadata_lock
    original_report, original_write = web.write_report, storage._write_metadata
    calls = []

    def write_report(*args, **kwargs):
        assert not metadata_lock._is_owned()  # Slow export must not hold the publication lock.
        calls.append("report export")
        return original_report(*args, **kwargs)

    def write_metadata(directory, values):
        assert metadata_lock._is_owned()
        calls.append("report summary" if "latency_summary" in values else "lifecycle metadata")
        return original_write(directory, values)

    monkeypatch.setattr(web, "write_report", write_report)
    monkeypatch.setattr(storage, "_write_metadata", write_metadata)
    monkeypatch.setattr(web, "run_suite", successful_runner)
    async with browser(tmp_path) as client:
        response = await client.post("/api/runs", json={"config": config().model_dump(mode="json")})
        assert response.status_code == 202, response.text
        identifier = response.json()["id"]
        assert (await finished(client, identifier))["state"] == "completed"
    assert "report export" in calls and "report summary" in calls
    assert calls.count("lifecycle metadata") >= 2
    saved = json.loads((tmp_path / identifier / ".web.json").read_text())
    assert saved["state"] == "completed"
    assert saved["latency_summary"]["version"] == 3
    assert "resources" in saved
