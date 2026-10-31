"""Installed UI smoke checks exercise the served asset graph and reject damage."""

import importlib.util
from importlib import resources
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx
import pytest

from giraffe.web import create_app


@pytest.fixture
def smoke():
    path = Path(__file__).parents[1] / ".github/scripts/smoke_package.py"
    spec = importlib.util.spec_from_file_location("smoke_package", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_smoke_accepts_transformed_assets_and_follows_versioned_dependencies(tmp_path, smoke):
    static = resources.files("giraffe").joinpath("static")
    requested = []

    async def record_request(request):
        requested.append(str(request.url))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(tmp_path)), base_url="http://localhost",
        event_hooks={"request": [record_request]},
    ) as client:
        await smoke.check_ui_assets(client, static)
    for name in ("boot.js", "styles.css", "app.js", "form-config.mjs"):
        assert any(f"/static/{name}?v=" in url for url in requested)
    assert not any(url.endswith("/static/form-config.mjs") for url in requested)


def test_smoke_checks_packaged_tokenizer_without_network_or_writable_cache(smoke):
    smoke.check_builtin_tokenizer(resources.files("giraffe").joinpath("data"))


@pytest.mark.parametrize("damage", ["encoding", "notice"])
def test_smoke_rejects_damaged_tokenizer_resources(tmp_path, smoke, damage):
    data = resources.files("giraffe").joinpath("data")
    for name in ("o200k_base.tiktoken", "THIRD_PARTY_NOTICES.txt"):
        (tmp_path / name).write_bytes(data.joinpath(name).read_bytes())
    name = "o200k_base.tiktoken" if damage == "encoding" else "THIRD_PARTY_NOTICES.txt"
    (tmp_path / name).write_bytes(b"corrupted")
    with pytest.raises(RuntimeError, match="tokenizer"):
        smoke.check_builtin_tokenizer(tmp_path)


@pytest.mark.parametrize("damage", [
    "missing_boot_reference", "missing_boot", "corrupt_boot", "corrupt_styles",
    "corrupt_script", "raw_script", "stale_script_version", "stale_dependency_version",
    "corrupt_dependency", "missing_dependency", "missing_import", "corrupt_font",
])
async def test_smoke_rejects_invalid_asset_graph_or_content(tmp_path, smoke, monkeypatch, damage):
    static = resources.files("giraffe").joinpath("static")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(tmp_path)), base_url="http://localhost",
    ) as client:
        get = client.get

        async def damaged_get(url):
            response = await get(url)
            content, status = response.content, response.status_code
            path = urlsplit(url).path
            if path == "/" and damage == "missing_boot_reference":
                content = re.sub(rb'<script src="/static/boot.js[^\"]*"></script>', b"", content)
            elif path == "/" and damage == "stale_script_version":
                content = re.sub(rb"(/static/app.js\?v=)[^\"]+", rb"\g<1>stale", content)
            elif path == "/static/boot.js":
                if damage == "missing_boot":
                    status = 404
                elif damage == "corrupt_boot":
                    content += b"\n// damaged boot script\n"
            elif path == "/static/styles.css" and damage == "corrupt_styles":
                content += b"\n/* damaged stylesheet */\n"
            elif path == "/static/app.js":
                if damage == "raw_script":
                    content = static.joinpath("app.js").read_bytes()
                elif damage == "corrupt_script":
                    content += b"\nthrow new Error('damaged');\n"
                elif damage == "stale_dependency_version":
                    content = re.sub(rb"(form-config.mjs\?v=)[^']+", rb"\g<1>stale", content)
                elif damage == "missing_import":
                    content = content.replace(b"form-config.mjs", b"missing.mjs")
            elif path == "/static/form-config.mjs":
                if damage == "corrupt_dependency":
                    content += b"\n// damaged dependency\n"
                elif damage == "missing_dependency":
                    status = 404
            elif path == "/static/fonts/Geist-Variable.woff2" and damage == "corrupt_font":
                content = b""
            return httpx.Response(status, content=content, request=response.request)

        monkeypatch.setattr(client, "get", damaged_get)
        with pytest.raises((RuntimeError, httpx.HTTPStatusError)):
            await smoke.check_ui_assets(client, static)
