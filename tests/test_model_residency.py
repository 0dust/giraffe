import asyncio
import json
import time

import httpx
import pytest

from giraffe.deployment import model_residency
from giraffe.models import RunConfig


def config(**kwargs):
    return RunConfig(targets=[{"name": "local", "model": "tiny", "url": "http://server/v1",
                               "deployment": {"discovery": "ollama"}}], checks=["correctness"], **kwargs)


def inventory(monkeypatch, payload, status=200):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, json=payload)
    monkeypatch.setattr("giraffe.deployment._discovery_client",
                        lambda *_: httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    return calls


@pytest.mark.parametrize("payload,state", [({"models": []}, "not_resident"),
    ({"models": [{"model": "tiny:latest"}]}, "resident"),
    ({"models": [{"name": "other"}]}, "not_resident"),
    ({}, "unknown"), ({"models": None}, "unknown"), ({"models": [42]}, "unknown")])
async def test_runtime_inventory_is_required_for_residency(monkeypatch, payload, state):
    calls = inventory(monkeypatch, payload)
    cfg = config()
    value = await model_residency(cfg.targets[0], cfg)
    assert value["state"] == state
    assert value["source"] == "ollama /api/ps" and value["target"] == "local"
    assert bool(value["collected_at"]) == (state != "unknown")
    assert [(r.method, r.url.path) for r in calls] == [("GET", "/api/ps")]


@pytest.mark.parametrize("requested,listed,state", [
    ("llama3", "llama3:latest", "resident"),
    ("library/llama3:latest", "llama3:latest", "resident"),
    ("registry.ollama.ai/library/llama3:latest", "llama3:latest", "resident"),
    ("REGISTRY.OLLAMA.AI/Library/LLaMa3:LATEST", "llama3:latest", "resident"),
    ("llama3", "REGISTRY.OLLAMA.AI/Library/LLaMa3:LATEST", "resident"),
    ("https://registry.ollama.ai/library/llama3", "llama3", "resident"),
    ("tenant/llama3", "registry.ollama.ai/tenant/llama3:latest", "resident"),
    ("host:5000/tenant/llama3", "HOST:5000/Tenant/LLaMa3:latest", "resident"),
    ("tenant/llama3", "llama3:latest", "not_resident"),
    ("host:5000/library/llama3", "llama3:latest", "not_resident"),
    ("llama3:other", "llama3:latest", "not_resident"),
    ("host:5000/tenant/llama3", "host:5001/tenant/llama3", "not_resident"),
    ("llama3:", "llama3", "unknown"),
    ("llama3", "library//llama3", "unknown"),
])
@pytest.mark.parametrize("field", ["model", "name"])
async def test_ollama_name_aliases_preserve_registry_namespace_and_tag(
    monkeypatch, requested, listed, state, field,
):
    inventory(monkeypatch, {"models": [{field: listed}]})
    cfg = config()
    cfg.targets[0].model = requested
    result = await model_residency(cfg.targets[0], cfg)
    assert result["state"] == state
    assert bool(result["collected_at"]) == (state != "unknown")


async def test_disabled_failed_and_expired_discovery_remain_unknown(monkeypatch):
    calls = inventory(monkeypatch, {"error": "secret-value"}, status=500)
    cfg = config()
    assert (await model_residency(cfg.targets[0], cfg))["state"] == "unknown"
    assert "secret-value" not in json.dumps(await model_residency(cfg.targets[0], cfg))
    calls.clear()
    for kwargs in [{"deadline": time.monotonic()-1}, {"stop": asyncio.Event()}]:
        if "stop" in kwargs:
            kwargs["stop"].set()
        assert (await model_residency(cfg.targets[0], cfg, **kwargs))["state"] == "unknown"
    cfg.targets[0].deployment.discovery = "none"
    assert (await model_residency(cfg.targets[0], cfg))["state"] == "unknown"
    assert not calls


def test_cli_observes_before_inference_preflight(tmp_path, monkeypatch):
    from giraffe.cli import main
    from giraffe.models import RunReport
    events = []
    async def observe(target, config, **kwargs):
        events.append("residency")
        return {"state": "not_resident"}
    async def check(*args):
        events.append("inference")
    async def run(cfg, **kwargs):
        assert cfg._preparation_residency["local"]["state"] == "not_resident"
        return RunReport(run_id="r", started_at="now", finished_at="now", overall="pass", manifest={})
    async def tokenizer(*args):
        pass
    monkeypatch.setattr("giraffe.deployment.model_residency", observe)
    monkeypatch.setattr("giraffe.preflight.check_target", check)
    monkeypatch.setattr("giraffe.local_tokenizer.ensure_tokenizers", tokenizer)
    monkeypatch.setattr("giraffe.cli._run", run)
    path = tmp_path / "config.json"
    path.write_text(config().model_dump_json())
    assert main(["run", "--config", str(path), "--output", str(tmp_path / "run")]) == 0
    assert events == ["residency", "inference"]


@pytest.mark.usefixtures("local_tokenizer")
async def test_run_preserves_before_and_after_observations_and_recaptures_on_reuse(monkeypatch, tmp_path):
    from tests.fake_endpoint import FakeEndpoint
    from tests.test_workloads import config as run_config
    from giraffe.runner import run_suite
    from giraffe.reporting import write_report, reproduction_export
    states = iter(["not_resident", "resident", "resident", "resident"])
    async def observe(target, cfg, **kwargs):
        return {"state": next(states), "source": "ollama /api/ps", "target": target.name,
                "collected_at": "2026-10-25T10:00:00Z", "scope": "one endpoint"}
    monkeypatch.setattr("giraffe.deployment.model_residency", observe)
    with FakeEndpoint() as endpoint:
        cfg = run_config(endpoint, checks=["correctness"])
        report = await run_suite(cfg)
        second = await run_suite(cfg)
    value = report.observations["targets"]["local"]["deployment"]["model_residency"]
    assert value["before_preparation"]["state"] == "not_resident"
    assert value["after_preparation"]["state"] == "resident"
    assert second.observations["targets"]["local"]["deployment"]["model_residency"]["before_preparation"]["state"] == "resident"
    write_report(report, tmp_path, html=True)
    html = (tmp_path / "report.html").read_text()
    assert "Before preparation" in html and "After preparation" in html and "not_resident" in html
    assert reproduction_export(report)["deployments"]["local"]["model_residency"] == value
