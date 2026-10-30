"""Tokenizer prerequisites are resolved before a run, without inference."""
import asyncio
import sys
import time
from pathlib import Path

import httpx
import pytest

from giraffe import local_tokenizer as sizing
from giraffe.models import RunConfig


@pytest.fixture(autouse=True)
def isolated_tokenizer_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))


def configuration(**updates):
    return RunConfig.model_validate({
        "config_version": 2,
        "targets": [{"name": "local", "url": "http://endpoint/v1", "model": "served-alias", "tokenizer": "auto"}],
        "checks": ["load"], **updates,
    })


@pytest.mark.usefixtures("local_tokenizer")
async def test_setup_uses_explicit_tokenizer_and_reuses_validation(monkeypatch):
    c = configuration()
    c.targets[0].tokenizer = "Qwen/Qwen2.5-7B"
    await sizing.ensure_tokenizers(c)
    assert c.targets[0]._tokenizer_setup["tokenizer"] == "Qwen/Qwen2.5-7B"
    assert c.targets[0]._tokenizer_setup["source"] == "configured"
    async def forbidden(*args, **kwargs):
        pytest.fail("Already validated tokenizer reloaded")
    monkeypatch.setattr(sizing, "_start_worker", forbidden)
    await sizing.ensure_tokenizers(c)


async def test_successful_override_is_remembered_for_empty_field(monkeypatch, local_tokenizer):
    explicit = configuration()
    explicit.targets[0].tokenizer = "Qwen/Qwen2.5-7B"
    await sizing.ensure_tokenizers(explicit)
    worker = sizing._start_worker
    names = []
    async def loaded(name, **kwargs):
        names.append(name)
        return await worker(name, **kwargs)
    monkeypatch.setattr(sizing, "_start_worker", loaded)
    automatic = configuration()  # A fresh request, with no private setup state.
    await sizing.ensure_tokenizers(automatic)
    assert names == ["Qwen/Qwen2.5-7B"]
    assert automatic.targets[0].tokenizer == "auto"
    assert automatic.targets[0]._tokenizer_setup["source"] == "remembered_mapping"
    assert automatic.model_dump()["targets"][0]["tokenizer"] == "auto"
    # The same alias on another endpoint must not inherit this mapping.
    other = configuration()
    other.targets[0].url = "http://other/v1"
    await sizing.ensure_tokenizers(other)
    assert names[-1] == "served-alias"
    # An explicit replacement wins and becomes the next automatic selection.
    explicit.targets[0].tokenizer = "another/model"
    await sizing.ensure_tokenizers(explicit)
    await sizing.ensure_tokenizers(configuration())
    assert names[-1] == "another/model"


async def test_failed_override_is_not_remembered(monkeypatch, local_tokenizer):
    worker = sizing._start_worker
    async def missing(name, **kwargs):
        if name == "missing/model":
            raise sizing.TokenizerUnavailable("Unavailable")
        return await worker(name, **kwargs)
    monkeypatch.setattr(sizing, "_start_worker", missing)
    explicit = configuration()
    explicit.targets[0].tokenizer = "missing/model"
    with pytest.raises(sizing.TokenizerUnavailable):
        await sizing.ensure_tokenizers(explicit)
    automatic = configuration()
    await sizing.ensure_tokenizers(automatic)
    assert automatic.targets[0]._tokenizer_setup["tokenizer"] == "served-alias"


async def test_remembered_local_directory_survives_working_directory_change(tmp_path, monkeypatch, local_tokenizer):
    (tmp_path / "model").mkdir()
    monkeypatch.chdir(tmp_path)
    explicit = configuration()
    explicit.targets[0].tokenizer = "model"
    await sizing.ensure_tokenizers(explicit)
    monkeypatch.chdir(tmp_path.parent)
    automatic = configuration()
    await sizing.ensure_tokenizers(automatic)
    assert automatic.targets[0]._tokenizer_setup["tokenizer"] == str(tmp_path / "model")


async def test_new_web_app_launches_with_remembered_mapping_and_empty_override(tmp_path, monkeypatch, local_tokenizer):
    from tests.test_web import browser, finished, successful_runner
    explicit = configuration()
    explicit.targets[0].tokenizer = "Qwen/Qwen2.5-7B"
    await sizing.ensure_tokenizers(explicit)
    worker = sizing._start_worker
    async def matching_only(name, **kwargs):
        assert name == "Qwen/Qwen2.5-7B"
        return await worker(name, **kwargs)
    monkeypatch.setattr(sizing, "_start_worker", matching_only)
    monkeypatch.setattr("giraffe.web.run_suite", successful_runner)
    automatic = configuration().model_dump(mode="json")
    async with browser(tmp_path / "runs", defaults=automatic) as client:
        bootstrap = (await client.get("/api/bootstrap")).json()
        assert bootstrap["config"]["targets"][0]["tokenizer"] == "auto"
        response = await client.post("/api/runs", json={"config": automatic})
        assert response.status_code == 202, response.text
        await finished(client, response.json()["id"])


async def test_alias_resolves_only_matching_advertised_model_root(monkeypatch, local_tokenizer):
    from tests.test_connection import endpoint
    requests, _ = endpoint(monkeypatch, lambda request: httpx.Response(200, json={"data": [
        {"id": "another-alias", "root": "wrong/model"},
        {"id": "served-alias", "root": "Qwen/Qwen2.5-7B"},
    ]}))
    worker = sizing._start_worker
    calls = []
    async def load(name, **kwargs):
        calls.append((name, kwargs))
        if name == "served-alias":
            raise sizing.TokenizerUnavailable("Missing tokenizer")
        return await worker(name, **kwargs)
    monkeypatch.setattr(sizing, "_start_worker", load)
    c = configuration()
    await sizing.ensure_tokenizers(c)
    assert c.targets[0]._tokenizer_setup["tokenizer"] == "Qwen/Qwen2.5-7B"
    assert c.targets[0]._tokenizer_setup["source"] == "server_model_root"
    assert len(requests) == 1 and requests[0].method == "GET"
    assert requests[0].url.path == "/v1/models"
    assert calls[-1][0] == "Qwen/Qwen2.5-7B"
    from types import SimpleNamespace
    result = await sizing.prepare_messages(SimpleNamespace(deadline=time.monotonic() + 10),
        c.targets[0], "load", lambda n: [{"role": "user", "content": "x" * n}], 128)
    assert result["status"] == "calibrated"
    assert result["tokenizer"] == "Qwen/Qwen2.5-7B"
    assert result["tokenizer_source"] == "server_model_root"
    assert len(requests) == 1  # Sizing reuses the resolved identity offline.


async def test_unresolved_alias_uses_builtin_without_requiring_model_identity(monkeypatch, local_tokenizer):
    from tests.test_connection import endpoint
    requests, _ = endpoint(monkeypatch, lambda request: httpx.Response(200, json={
        "data": [{"id": "served-alias"}]}))
    worker = sizing._start_worker
    async def missing(name, **kwargs):
        if name != "builtin":
            raise sizing.TokenizerUnavailable("Tokenizer assets unavailable")
        return await worker(name, **kwargs)
    async def unresolved(name):
        return None
    monkeypatch.setattr(sizing, "_start_worker", missing)
    monkeypatch.setattr(sizing, "resolve_hub_name", unresolved)
    config = configuration()
    await sizing.ensure_tokenizers(config)
    assert config.targets[0]._tokenizer_setup["source"] == "builtin_fallback"
    assert config.targets[0]._tokenizer_setup["tokenizer"] == "builtin"
    assert config.targets[0].tokenizer == "auto"
    assert len(requests) == 1 and requests[0].method == "GET"


async def test_saved_requests_do_not_require_tokenizer(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Saved prompt should not load a tokenizer")
    monkeypatch.setattr(sizing, "_start_worker", forbidden)
    c = configuration(request_source="request_pack", request_pack={"version": 1, "cases": [
        {"fixture_id": "one", "messages": [{"role": "user", "content": "Hi"}], "scorer": "none"}]})
    await sizing.ensure_tokenizers(c)
    await sizing.ensure_tokenizers(configuration(checks=["correctness"]))
    assert not c.targets[0]._tokenizer_setup


async def test_automatic_setup_timeout_kills_worker_and_uses_builtin(monkeypatch, local_tokenizer):
    workers = []
    worker = sizing._start_worker
    async def hung(*args, **kwargs):
        if args[0] == "builtin":
            return await worker(*args, **kwargs)
        p = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        workers.append(p)
        return p
    monkeypatch.setattr(sizing, "_start_worker", hung)
    monkeypatch.setattr(sizing, "AUTOMATIC_SETUP_TIMEOUT_SECONDS", .08)
    started = time.monotonic()
    config = configuration()
    await sizing.ensure_tokenizers(config)
    assert config.targets[0]._tokenizer_setup["source"] == "builtin_fallback"
    assert time.monotonic() - started < .5
    assert workers and all(p.returncode is not None for p in workers)


async def test_explicit_override_timeout_still_reports_error(monkeypatch):
    async def slow(*args, **kwargs):
        await asyncio.sleep(60)
    monkeypatch.setattr(sizing, "_verify_tokenizer", slow)
    monkeypatch.setattr(sizing, "SETUP_TIMEOUT_SECONDS", .02)
    config = configuration()
    config.targets[0].tokenizer = "explicit/model"
    with pytest.raises(sizing.TokenizerUnavailable, match='Could not load tokenizer "explicit/model": setup timed out'):
        await sizing.ensure_tokenizers(config)


async def test_builtin_allows_fresh_alias_to_complete_actual_web_run(tmp_path, monkeypatch, local_tokenizer):
    from tests.fake_endpoint import FakeEndpoint
    from tests.test_load_test_phase_a import values
    from tests.test_web import browser
    worker = sizing._start_worker
    async def unavailable(name, **kwargs):
        assert name == "builtin", "Default must not load or resolve the opaque alias"
        return await worker(name, **kwargs)
    monkeypatch.setattr(sizing, "_start_worker", unavailable)
    with FakeEndpoint() as endpoint:
        config = values()
        config.update(max_requests=1, stream=False, max_duration_seconds=3, metrics_timeout_seconds=.2)
        config["targets"] = [{"name": "fresh", "url": endpoint.url, "model": "opaque-customer-alias", "tokenizer": "builtin"}]
        async with browser(tmp_path / "runs") as client:
            response = await client.post("/api/runs", json={"config": config})
            assert response.status_code == 202, response.text
            # A real loopback server plus automatic telemetry can exceed the
            # two-second helper used by mocked web tests on a busy machine.
            async with asyncio.timeout(10):
                while True:
                    detail = (await client.get(f"/api/runs/{response.json()['id']}")).json()
                    if detail["state"] not in {"running", "stopping"}:
                        break
                    await asyncio.sleep(.02)
        records = detail["report"]["requests"]
        assert len(records) == len(endpoint.requests) == 1
        record = records[0]
        assert record["status"] == "completed" and record["valid"] and record["score"]
        prepared = record["workload"]["input_preparation"]
        assert prepared["method"] == "local_tokenizer"
        assert prepared["tokenizer"] == "builtin"
        assert prepared["token_count_scope"] == "builtin_reference"
        assert prepared["input_tokens"] > 0
        assert record["input_tokens"] is not None  # Only the endpoint supplies measured usage.
        assert not record["scenario"].startswith("load_calibration")


async def test_cancelled_setup_reaps_worker(monkeypatch):
    started = asyncio.Event()
    workers = []
    async def hung(*args, **kwargs):
        p = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        workers.append(p)
        started.set()
        return p
    monkeypatch.setattr(sizing, "_start_worker", hung)
    task = asyncio.create_task(sizing.ensure_tokenizers(configuration()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert workers[0].returncode is not None


async def test_programmatic_suite_blocks_before_inference(monkeypatch):
    from giraffe.runner import run_suite
    async def missing(*args, **kwargs):
        raise sizing.TokenizerUnavailable("Tokenizer missing")
    monkeypatch.setattr(sizing, "ensure_tokenizers", missing)
    with pytest.raises(sizing.TokenizerUnavailable, match="Tokenizer missing"):
        await run_suite(configuration())


@pytest.mark.parametrize("root", ["/private/tokenizer", "./tokenizer", "../tokenizer", "https://host/model"])
async def test_remote_root_never_becomes_local_path_or_url(root, monkeypatch):
    from giraffe.preflight import ModelDiscovery, discover_models
    from tests.test_connection import endpoint
    endpoint(monkeypatch, lambda request: httpx.Response(200, json={"data": [
        {"id": "served-alias", "root": root}]}))
    result = await discover_models(ModelDiscovery(url="http://endpoint/v1"), include_tokenizers=True)
    assert result["tokenizers"] == {}


async def test_ambiguous_model_roots_are_not_guessed(monkeypatch):
    from giraffe.preflight import ModelDiscovery, discover_models
    from tests.test_connection import endpoint
    endpoint(monkeypatch, lambda request: httpx.Response(200, json={"data": [
        {"id": "served-alias", "root": "one/model"},
        {"id": "served-alias", "root": "another/model"}]}))
    result = await discover_models(ModelDiscovery(url="http://endpoint/v1"), include_tokenizers=True)
    assert result["tokenizers"] == {}


async def test_worker_launch_error_is_actionable(monkeypatch):
    async def cannot_spawn(*args, **kwargs):
        raise OSError("private system details")
    monkeypatch.setattr(sizing, "_start_worker", cannot_spawn)
    c = configuration()
    c.targets[0].tokenizer = "Qwen/Qwen2.5-7B"
    with pytest.raises(sizing.TokenizerUnavailable, match="worker") as error:
        await sizing.ensure_tokenizers(c)
    assert "private" not in str(error.value)


async def test_worker_downloads_missing_assets_then_sizes_without_chat_template(tmp_path, monkeypatch):
    (tmp_path / "transformers.py").write_text('''
import os
from pathlib import Path
class AutoTokenizer:
    chat_template = None
    @classmethod
    def from_pretrained(cls, name, **kwargs):
        assert name == "Qwen/Qwen2.5-7B"
        assert kwargs["trust_remote_code"] is False
        cached = Path(__file__).with_suffix(".cached")
        if kwargs["local_files_only"] and not cached.exists():
            raise OSError("Not cached")
        if not kwargs["local_files_only"]:
            assert os.environ.get("HF_HUB_OFFLINE") != "1"
            cached.touch()
        return cls()
    def encode(self, content, *, add_special_tokens):
        assert add_special_tokens is False
        return list(content)
''')
    monkeypatch.setenv("PYTHONPATH", f"{tmp_path}:{Path(sizing.__file__).parents[1]}")
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    c = configuration()
    c.targets[0].tokenizer = "Qwen/Qwen2.5-7B"
    await sizing.ensure_tokenizers(c)
    from types import SimpleNamespace
    result = await sizing.prepare_messages(SimpleNamespace(deadline=time.monotonic() + 10),
        c.targets[0], "load", lambda n: [{"role": "user", "content": "x" * n}], 128)
    assert result["status"] == "calibrated", result
    assert result["chat_template"] is False
    assert result["token_count_scope"] == "message_content"
    assert result["local_files_only"] is True
    assert abs(result["input_tokens"] - 128) <= 12.8
    assert (tmp_path / "transformers.cached").exists()


async def test_api_rejects_tokenizer_failure_before_creating_run(tmp_path, monkeypatch):
    from tests.test_web import browser
    async def missing(*args, **kwargs):
        raise sizing.TokenizerUnavailable("Set targets[0].tokenizer. No run was started.")
    monkeypatch.setattr(sizing, "ensure_tokenizers", missing)
    async with browser(tmp_path) as client:
        response = await client.post("/api/runs", json={"config": configuration().model_dump(mode="json")})
        assert response.status_code == 422, response.text
        assert "tokenizer" in response.text
        assert (await client.get("/api/runs")).json()["runs"] == []


def test_cli_checks_tokenizer_even_when_connection_probe_is_skipped(tmp_path, monkeypatch, capsys):
    from giraffe.cli import main
    async def missing(*args, **kwargs):
        raise sizing.TokenizerUnavailable("Set targets[0].tokenizer. No run was started.")
    monkeypatch.setattr(sizing, "ensure_tokenizers", missing)
    path = tmp_path / "config.json"
    path.write_text(configuration().model_dump_json())
    output = tmp_path / "output"
    assert main(["run", "--config", str(path), "--skip-preflight", "--output", str(output)]) == 3
    assert "tokenizer" in capsys.readouterr().err
    assert not output.exists()
