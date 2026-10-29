"""Established built-in encoding, short-name resolution, and honest provenance."""
import threading
import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

from giraffe import local_tokenizer as sizing
from giraffe.models import RunConfig


def config(tokenizer="builtin"):
    return RunConfig(targets=[dict(name="fresh", url="http://endpoint/v1",
                                  model="opaque-customer-alias", tokenizer=tokenizer)], checks=["load"])


async def test_explicit_builtin_never_looks_up_alias_or_remembered_mapping(monkeypatch, local_tokenizer):
    async def forbidden(*args, **kwargs):
        pytest.fail("Default builtin must not look up model names")
    monkeypatch.setattr(sizing, "_resolve_tokenizer", forbidden)
    monkeypatch.setattr(sizing, "_remembered_alias", lambda _: pytest.fail("No saved mapping needed"))
    c = config()
    await sizing.ensure_tokenizers(c)
    assert c.targets[0]._tokenizer_setup["tokenizer"] == "builtin"
    assert c.targets[0]._tokenizer_setup["source"] == "builtin"


@pytest.mark.parametrize("missing_status", [401, 404])
@pytest.mark.parametrize("rows,expected", [
    ([{"id": "Qwen/Qwen2.5-7B", "downloads": 100}], "Qwen/Qwen2.5-7B"),
    ([{"id": "Qwen/Qwen2.5-7B-Instruct", "downloads": 100}], None),
    ([{"id": "one/Qwen2.5-7B", "downloads": 10},
      {"id": "two/qwen2.5-7b", "downloads": 100}], "two/qwen2.5-7b"),
    ([], None),
])
async def test_hub_resolution_only_accepts_exact_or_suffix_match(monkeypatch, rows, expected, missing_status):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url.host == "huggingface.co"
        assert "authorization" not in request.headers
        return httpx.Response(200, json=rows) if request.url.path == "/api/models" else httpx.Response(missing_status)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    assert await sizing.resolve_hub_name("qwen2.5-7b") == expected
    assert len(requests) == 2


async def test_offline_resolution_never_queries_hub(monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: pytest.fail("Offline lookup sent network request"))
    assert await sizing.resolve_hub_name("qwen2.5-7b") is None


async def test_automatic_resolution_downloads_and_uses_resolved_tokenizer(monkeypatch, local_tokenizer):
    from tests.test_connection import endpoint
    endpoint(monkeypatch, lambda request: httpx.Response(200, json={"data": []}))
    worker = sizing._start_worker
    calls = []
    async def start(name, **kw):
        calls.append((name, kw.get("allow_download")))
        if name == "opaque-customer-alias":
            raise sizing.TokenizerUnavailable("Not cached")
        return await worker(name, **kw)
    async def resolve(name):
        return "Qwen/Qwen2.5-7B"
    monkeypatch.setattr(sizing, "_start_worker", start)
    monkeypatch.setattr(sizing, "resolve_hub_name", resolve)
    c = config("auto")
    await sizing.ensure_tokenizers(c)
    assert calls == [("opaque-customer-alias", False), ("Qwen/Qwen2.5-7B", True)]
    assert c.targets[0]._tokenizer_setup["source"] == "hub_resolution"
    assert c.targets[0]._tokenizer_setup["tokenizer"] == "Qwen/Qwen2.5-7B"
    # A fresh config for the same endpoint reuses the resolved local identity.
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    async def forbidden(name):
        pytest.fail("A cached resolved identity should not need another Hub lookup")
    monkeypatch.setattr(sizing, "resolve_hub_name", forbidden)
    await sizing.ensure_tokenizers(config("auto"))
    assert calls[-1] == ("Qwen/Qwen2.5-7B", False)


async def test_failed_resolved_download_uses_builtin(monkeypatch, local_tokenizer):
    worker = sizing._start_worker
    async def resolve(*args):
        return "gated/model", "hub_resolution"
    async def start(name, **kw):
        if name != "builtin":
            raise sizing.TokenizerUnavailable("Unavailable")
        return await worker(name, **kw)
    monkeypatch.setattr(sizing, "_resolve_tokenizer", resolve)
    monkeypatch.setattr(sizing, "_start_worker", start)
    c = config("auto")
    await sizing.ensure_tokenizers(c)
    assert c.targets[0]._tokenizer_setup["source"] == "builtin_fallback"
    assert c.targets[0]._tokenizer_setup["tokenizer"] == "builtin"


async def test_real_builtin_counts_text_and_reuses_cache_offline(monkeypatch):
    pytest.importorskip("tiktoken")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    try:
        encoding = sizing._builtin_encoding(False)
    except OSError:
        pytest.skip("Real integration needs previously cached o200k_base encoding")
    # No transformers import or Hugging Face model is involved.
    c = config()
    await sizing.ensure_tokenizers(c)
    run = SimpleNamespace(deadline=time.monotonic() + 15)
    result = await sizing.prepare_messages(run, c.targets[0], "load", lambda n: [
        {"role": "user", "content": "Count 中文 <|endoftext|> " + ("some text. " * n)[:n]}], 128)
    assert result["status"] == "calibrated", result
    expected = sum(len(encoding.encode(m["content"], disallowed_special=())) for m in result["messages"])
    assert result["input_tokens"] == expected
    assert abs(expected - 128) <= 12.8
    assert result["token_count_scope"] == "builtin_reference"
    assert result["encoding"] == "o200k_base"
    assert "not the served model" in result["reason"]
    assert not result["request_ids"]
    assert "characters_per_token" not in result


async def test_default_is_automatic(monkeypatch, local_tokenizer):
    c = config(None)
    await sizing.ensure_tokenizers(c)
    assert c.targets[0]._tokenizer_setup["tokenizer"] == "opaque-customer-alias"
    assert c.targets[0]._tokenizer_setup["source"] == "target_model"


async def test_preview_resolves_short_name_and_never_contacts_endpoint(monkeypatch, local_tokenizer):
    worker = sizing._start_worker
    async def start(name, **kw):
        if name == "qwen2.5-7b":
            raise sizing.TokenizerUnavailable("Not cached")
        return await worker(name, **kw)
    async def resolve(name):
        assert name == "qwen2.5-7b"
        return "Qwen/Qwen2.5-7B"
    async def forbidden(*args, **kw):
        pytest.fail("First-page tokenizer check must not contact an inference endpoint")
    monkeypatch.setattr(sizing, "_start_worker", start)
    monkeypatch.setattr(sizing, "resolve_hub_name", resolve)
    monkeypatch.setattr("giraffe.preflight.discover_models", forbidden)
    result = await sizing.preview_tokenizer("served-alias", "qwen2.5-7b")
    assert result["status"] == "ready"
    assert result["tokenizer"] == "Qwen/Qwen2.5-7B"


async def test_preview_failure_discloses_builtin_and_same_box_correction(monkeypatch, local_tokenizer):
    worker = sizing._start_worker
    async def start(name, **kw):
        if name != "builtin":
            raise sizing.TokenizerUnavailable("Not cached")
        return await worker(name, **kw)
    async def resolve(name):
        return None
    monkeypatch.setattr(sizing, "_start_worker", start)
    monkeypatch.setattr(sizing, "resolve_hub_name", resolve)
    result = await sizing.preview_tokenizer("opaque-alias")
    assert result["status"] == "fallback" and result["tokenizer"] == "builtin"
    assert "Built-in o200k_base will be used" in result["message"]
    assert "Enter a model name in Token ID" in result["message"]


async def test_preview_api_downloads_before_checks_and_does_not_start_run(tmp_path, monkeypatch, local_tokenizer):
    from tests.test_web import browser
    async def forbidden(*args, **kw):
        pytest.fail("Tokenizer preview must not start inference")
    monkeypatch.setattr("giraffe.web.run_suite", forbidden)
    async with browser(tmp_path / "runs") as client:
        response = await client.post("/api/targets/tokenizer", json={"model": "alias", "token_id": "builtin"})
        assert response.status_code == 200, response.text
        assert response.json()["tokenizer"] == "builtin"
        assert (await client.get("/api/bootstrap")).json()["active_run_id"] is None
        assert (await client.get("/api/runs")).json()["runs"] == []


async def test_preview_api_reports_builtin_load_failure(tmp_path, monkeypatch):
    from tests.test_web import browser
    async def unavailable(*args, **kw):
        raise sizing.TokenizerUnavailable("Encoding unavailable")
    monkeypatch.setattr(sizing, "_start_worker", unavailable)
    async with browser(tmp_path / "runs") as client:
        response = await client.post("/api/targets/tokenizer", json={"token_id": "builtin"})
        assert response.status_code == 422
        assert "internet access" in response.text


async def test_preview_loads_explicit_repository_without_public_search(monkeypatch, local_tokenizer):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    worker = sizing._start_worker
    calls = []

    async def start(name, **kwargs):
        calls.append((name, kwargs.get("allow_download")))
        if name == "organization/private-model" and not kwargs.get("allow_download"):
            raise sizing.TokenizerUnavailable("Not cached")
        return await worker(name, **kwargs)

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(401)), **kw))
    monkeypatch.setattr(sizing, "_start_worker", start)
    result = await sizing.preview_tokenizer("serving-alias", "organization/private-model")
    assert result["status"] == "ready" and result["tokenizer"] == "organization/private-model"
    assert calls == [("organization/private-model", False), ("organization/private-model", True)]


async def test_tokenizer_preview_cannot_overlap_active_run(tmp_path, monkeypatch):
    from tests.test_web import browser, config, report

    async def running(configuration, *, progress, stop_event):
        await stop_event.wait()
        return report(configuration, cancelled=True)

    calls = []

    async def preview(*args):
        calls.append(args)
        return {"status": "ready", "tokenizer": "builtin"}

    monkeypatch.setattr("giraffe.web.run_suite", running)
    monkeypatch.setattr(sizing, "preview_tokenizer", preview)
    async with browser(tmp_path) as client:
        assert (await client.post("/api/runs", json={"config": config(checks=["access"])})).status_code == 202
        response = await client.post("/api/targets/tokenizer", json={"token_id": "builtin"})
        assert response.status_code == 409 and not calls


async def test_run_start_proceeds_while_tokenizer_preview_downloads(tmp_path, monkeypatch):
    from tests.test_web import browser, config, finished, report

    entered, release = asyncio.Event(), asyncio.Event()
    started = threading.Event()

    async def preview(*args):
        entered.set()
        await release.wait()
        return {"status": "ready", "tokenizer": "builtin"}

    async def running(configuration, *, progress, stop_event):
        started.set()
        return report(configuration)

    monkeypatch.setattr(sizing, "preview_tokenizer", preview)
    monkeypatch.setattr("giraffe.web.run_suite", running)
    async with browser(tmp_path) as client:
        preview_task = asyncio.create_task(client.post("/api/targets/tokenizer", json={"token_id": "builtin"}))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            response = await asyncio.wait_for(client.post("/api/runs", json={"config": config(checks=["access"])}), 2)
            assert response.status_code == 202
            assert await asyncio.to_thread(started.wait, 2)
            assert not preview_task.done()
            assert (await finished(client, response.json()["id"]))["state"] == "completed"
        finally:
            release.set()
            assert (await preview_task).status_code == 200
