"""Established built-in encoding, short-name resolution, and honest provenance."""
import threading
import asyncio
import builtins
import json
import os
import socket
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


async def test_real_builtin_counts_text_offline_without_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    inaccessible = tmp_path / "not-a-directory"
    inaccessible.write_text("No cache or home can be created below this file.")
    for variable in ("HOME", "XDG_CACHE_HOME", "TIKTOKEN_CACHE_DIR", "HF_HOME"):
        monkeypatch.setenv(variable, str(inaccessible / "unwritable"))
    encoding = sizing._builtin_encoding()
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


async def test_preview_verifies_exact_typed_name_without_resolution(monkeypatch, local_tokenizer):
    worker = sizing._start_worker
    calls = []
    async def start(name, **kw):
        calls.append((name, kw))
        return await worker(name, **kw)
    async def forbidden(*args, **kw):
        pytest.fail("An explicit Token ID must not be inferred or contact an inference endpoint")
    monkeypatch.setattr(sizing, "_start_worker", start)
    monkeypatch.setattr(sizing, "resolve_hub_name", forbidden)
    monkeypatch.setattr("giraffe.preflight.discover_models", forbidden)
    result = await sizing.preview_tokenizer("served-alias", "qwen2.5-7b")
    assert result == {"status": "ready", "tokenizer": "qwen2.5-7b", "message": ""}
    assert calls == [("qwen2.5-7b", {"allow_download": True})]


@pytest.mark.parametrize("token_id", ["", "auto", "Automatic", "builtin"])
async def test_automatic_and_builtin_preview_never_load_or_resolve(monkeypatch, token_id):
    async def forbidden(*args, **kwargs):
        pytest.fail("Automatic and builtin do not require a preview check")
    monkeypatch.setattr(sizing, "_verify_tokenizer", forbidden)
    monkeypatch.setattr(sizing, "_resolve_tokenizer", forbidden)
    result = await sizing.preview_tokenizer("", token_id)
    assert result == {"status": "ready", "tokenizer": "builtin" if token_id == "builtin" else "auto", "message": ""}


async def test_preview_api_does_not_start_run(tmp_path, monkeypatch, local_tokenizer):
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


@pytest.mark.parametrize("token_id", ["auto", "builtin"])
async def test_preview_api_automatic_and_builtin_cannot_fail_to_load(tmp_path, monkeypatch, token_id):
    from tests.test_web import browser
    async def unavailable(*args, **kw):
        pytest.fail("Automatic and builtin must not load during preview")
    monkeypatch.setattr(sizing, "_start_worker", unavailable)
    async with browser(tmp_path / "runs") as client:
        response = await client.post("/api/targets/tokenizer", json={"token_id": token_id})
        assert response.status_code == 200
        assert response.json()["message"] == ""


@pytest.mark.parametrize("reason,code,preview_reason", [
    ("Tokenizer files could not be loaded", "tokenizer_unavailable", "Tokenizer files could not be loaded"),
    (sizing.HF_INSTALL_HINT, "missing_dependency", "Install giraffe-llm[hf-tokenizer], or pick Automatic"),
])
async def test_preview_api_returns_clean_typed_name_error(tmp_path, monkeypatch, reason, code, preview_reason):
    from tests.test_web import browser
    async def unavailable(*args, **kw):
        raise sizing.TokenizerUnavailable(reason, code=code)
    monkeypatch.setattr(sizing, "_verify_tokenizer", unavailable)
    async with browser(tmp_path / "runs") as client:
        response = await client.post("/api/targets/tokenizer", json={"token_id": "organization/model"})
        assert response.status_code == 422
        assert response.json()["detail"] == f'Could not load tokenizer "organization/model": {preview_reason}.'
        assert "tokenizer-preview" not in response.text
        assert "No run was started" not in response.text
        assert "reinstall" not in response.text


async def test_preview_timeout_returns_clean_error_and_cancels_verification(monkeypatch):
    cancelled = asyncio.Event()
    async def slow(*args, **kwargs):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
    monkeypatch.setattr(sizing, "_verify_tokenizer", slow)
    monkeypatch.setattr(sizing, "SETUP_TIMEOUT_SECONDS", .01)
    with pytest.raises(sizing.TokenizerUnavailable, match=r'Could not load tokenizer "slow/model": setup timed out\.'):
        await sizing.preview_tokenizer("alias", "slow/model")
    assert cancelled.is_set()


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
    assert calls == [("organization/private-model", True)]


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


def test_builtin_constants_match_upstream_constructor(monkeypatch):
    import tiktoken
    from tiktoken_ext import openai_public

    ranks = {b"x": 0}
    def load(url, expected_hash):
        assert url == "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
        assert expected_hash == sizing.BUILTIN_SHA256
        return ranks
    monkeypatch.setattr(openai_public, "load_tiktoken_bpe", load)
    upstream = openai_public.o200k_base()
    assert upstream == {"name": sizing.BUILTIN_ENCODING, "pat_str": sizing.BUILTIN_PATTERN,
                        "mergeable_ranks": ranks, "special_tokens": sizing.BUILTIN_SPECIAL_TOKENS}
    assert tiktoken.Encoding(**upstream).n_vocab == sizing.BUILTIN_N_VOCAB


def test_builtin_encoding_needs_no_network_writable_home_or_environment_changes(monkeypatch, tmp_path):
    import tiktoken
    from tiktoken import load

    inaccessible = tmp_path / "not-a-directory"
    inaccessible.write_text("No writes below here")
    for variable in ("HOME", "XDG_CACHE_HOME", "TIKTOKEN_CACHE_DIR", "HF_HOME"):
        monkeypatch.setenv(variable, str(inaccessible / "unwritable"))
    def forbidden(*args, **kwargs):
        pytest.fail("Packaged tokenizer must not access sockets or tiktoken's download/cache loader")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(tiktoken, "get_encoding", forbidden)
    monkeypatch.setattr(load, "read_file", forbidden)
    monkeypatch.setattr(load, "read_file_cached", forbidden)
    environment = dict(os.environ)
    encoding = sizing._builtin_encoding()
    text = "Hello, 世界! <|endoftext|>"
    assert encoding.decode(encoding.encode(text, disallowed_special=())) == text
    assert encoding.n_vocab == sizing.BUILTIN_N_VOCAB
    assert dict(os.environ) == environment


def test_builtin_data_hash_mismatch_is_a_corrupt_install(monkeypatch):
    monkeypatch.setattr(sizing, "BUILTIN_SHA256", "0" * 64)
    with pytest.raises(sizing.TokenizerUnavailable, match="integrity check; the install is corrupt") as error:
        sizing._builtin_encoding()
    assert error.value.code == "corrupt_install"


def test_builtin_missing_data_is_a_corrupt_install(monkeypatch):
    class MissingResource:
        def joinpath(self, _):
            return self
        def read_bytes(self):
            raise FileNotFoundError
    monkeypatch.setattr(sizing.resources, "files", lambda _: MissingResource())
    with pytest.raises(sizing.TokenizerUnavailable, match="data is missing; the install is corrupt") as error:
        sizing._builtin_encoding()
    assert error.value.code == "corrupt_install"


async def test_builtin_worker_inherits_environment_without_download_flags(monkeypatch):
    calls = []
    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await sizing._start_worker("builtin", allow_download=True)
    args, kwargs = calls[0]
    assert args == (sizing.sys.executable, "-m", "giraffe.local_tokenizer", "builtin")
    assert kwargs["env"] is None


def test_worker_missing_hf_extra_reports_cli_install_hint(monkeypatch, capsys):
    original_import = builtins.__import__
    def without_transformers(name, *args, **kwargs):
        if name == "transformers":
            raise ImportError("not installed")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", without_transformers)
    sizing._worker("organization/model", allow_download=True)
    result = json.loads(capsys.readouterr().out)
    assert result == {"error": "Install giraffe-llm[hf-tokenizer] to use the model's own tokenizer", "code": "missing_dependency"}


async def test_automatic_missing_hf_extra_falls_back_to_packaged_builtin(monkeypatch, local_tokenizer):
    worker = sizing._start_worker
    async def start(name, **kwargs):
        if name != sizing.BUILTIN:
            raise sizing.TokenizerUnavailable(sizing.HF_INSTALL_HINT, code="missing_dependency")
        return await worker(name, **kwargs)
    monkeypatch.setattr(sizing, "_start_worker", start)
    c = config("auto")
    await sizing.ensure_tokenizers(c)
    assert c.targets[0]._tokenizer_setup["source"] == "builtin_fallback"
    assert c.targets[0]._tokenizer_setup["tokenizer"] == "builtin"
    assert c.targets[0]._tokenizer_setup["requested"] == "auto"


def test_builtin_worker_failure_reports_corrupt_install_without_network_advice(monkeypatch, capsys):
    def broken():
        raise ValueError("invalid packaged encoding")
    monkeypatch.setattr(sizing, "_builtin_encoding", broken)
    sizing._worker("builtin")
    result = json.loads(capsys.readouterr().out)
    assert result == {"error": "Built-in tokenizer could not be loaded or used; the install is corrupt",
                      "code": "corrupt_install"}
    assert "network" not in result["error"]
    assert "cache" not in result["error"]
    assert "reinstall" not in result["error"]
