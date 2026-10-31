"""Exercise a wheel installed in a fresh environment, outside the checkout."""

import asyncio
import hashlib
from importlib import metadata, resources
import os
from pathlib import Path
import re
import subprocess
import sys
import sysconfig
import tempfile
from urllib.parse import parse_qs, urljoin, urlsplit

import giraffe
import httpx
from giraffe.web import create_app


def check_builtin_tokenizer(data):
    encoding = data.joinpath("o200k_base.tiktoken").read_bytes()
    expected_hash = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
    if hashlib.sha256(encoding).hexdigest() != expected_hash:
        raise RuntimeError("Installed built-in tokenizer encoding is missing or corrupted")
    notices = data.joinpath("THIRD_PARTY_NOTICES.txt").read_text()
    for required in (expected_hash, "MIT License", "Copyright (c) 2022 OpenAI, Shantanu Jain"):
        if required not in notices:
            raise RuntimeError("Installed tokenizer third-party notices are missing or incomplete")
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory) / "home"
        cache = Path(directory) / "cache"
        home.mkdir(mode=0o500)
        cache.mkdir(mode=0o500)
        environment = dict(os.environ, HOME=str(home), XDG_CACHE_HOME=str(cache),
                           TIKTOKEN_CACHE_DIR=str(cache))
        try:
            subprocess.run([sys.executable, "-c", '''
import os
import socket
def blocked(*args, **kwargs):
    raise AssertionError("Built-in tokenizer attempted network access")
class BlockedSocket(socket.socket):
    def __new__(cls, *args, **kwargs):
        return blocked()
socket.socket = BlockedSocket
socket.create_connection = blocked
from giraffe.local_tokenizer import _builtin_encoding
environment = dict(os.environ)
encoding = _builtin_encoding()
text = "Giraffe tokenizes offline. 🦒"
assert encoding.decode(encoding.encode(text)) == text
assert dict(os.environ) == environment, "Built-in tokenizer changed the environment"
'''], check=True, timeout=30, env=environment)
            if any(home.iterdir()) or any(cache.iterdir()):
                raise RuntimeError("Built-in tokenizer wrote to home or cache")
        finally:
            home.chmod(0o700)
            cache.chmod(0o700)


def check_asset_version(url, content):
    version = f"{giraffe.__version__}-{hashlib.sha256(content).hexdigest()[:16]}"
    if parse_qs(urlsplit(url).query).get("v") != [version]:
        raise RuntimeError(f"Installed UI asset has a stale or missing version: {url}")


async def check_ui_assets(client, static):
    response = await client.get("/")
    response.raise_for_status()
    if "<!doctype html>" not in response.text.lower():
        raise RuntimeError("Installed UI did not serve its HTML page")
    urls = re.findall(r'(?:src|href)="(/static/[^\"]+)"', response.text)
    for name in ("boot.js", "styles.css", "app.js"):
        references = [url for url in urls if urlsplit(url).path == f"/static/{name}"]
        if len(references) != 1:
            raise RuntimeError(f"Installed UI must reference its asset once: {name}")
        url = references[0]
        response = await client.get(url)
        response.raise_for_status()
        expected = static.joinpath(name).read_bytes()
        if name == "app.js":
            # Only the dependency URL may differ from the packaged script.
            pattern = rb"from '(\./form-config\.mjs(?:\?[^']*)?)'"
            original = re.search(pattern, expected)
            served = re.search(pattern, response.content)
            if original is None or served is None:
                raise RuntimeError("Installed UI script is missing its form-config import")
            dependency_url = urljoin(url, served.group(1).decode())
            dependency = await client.get(dependency_url)
            dependency.raise_for_status()
            expected_dependency = static.joinpath("form-config.mjs").read_bytes()
            if not expected_dependency or dependency.content != expected_dependency:
                raise RuntimeError("Installed UI asset is missing or corrupted: form-config.mjs")
            check_asset_version(dependency_url, dependency.content)
            expected = expected[:original.start(1)] + served.group(1) + expected[original.end(1):]
        if not expected or response.content != expected:
            raise RuntimeError(f"Installed UI asset is missing or corrupted: {name}")
        check_asset_version(url, response.content)
    for name in ("fonts/Geist-Variable.woff2", "fonts/GeistMono-Variable.woff2", "fonts/OFL.txt"):
        expected = static.joinpath(name).read_bytes()
        response = await client.get(f"/static/{name}")
        response.raise_for_status()
        if not expected or response.content != expected:
            raise RuntimeError(f"Installed UI asset is missing or corrupted: {name}")


async def main():
    package = Path(giraffe.__file__).resolve()
    if not package.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError(f"Smoke test imported source instead of the installed wheel: {package}")
    installed = metadata.distribution("giraffe-llm")
    if installed.version != giraffe.__version__:
        raise RuntimeError("Distribution and runtime versions disagree")
    entries = {e.name: e.value for e in installed.entry_points if e.group == "console_scripts"}
    if entries.get("giraffe") != "giraffe.cli:main":
        raise RuntimeError("The giraffe command is missing or points to the wrong entry point")
    command = Path(sysconfig.get_path("scripts")) / "giraffe"
    subprocess.run([str(command), "--help"], check=True, timeout=30)
    check_builtin_tokenizer(resources.files("giraffe").joinpath("data"))
    static = resources.files("giraffe").joinpath("static")
    with tempfile.TemporaryDirectory() as directory:
        app = create_app(Path(directory) / "runs", token="package-smoke-token")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            response = await client.get("/?token=package-smoke-token", follow_redirects=True)
            response.raise_for_status()
            await check_ui_assets(client, static)
    print(f"Installed giraffe-llm {installed.version}: CLI, offline tokenizer and UI assets passed")


if __name__ == "__main__":
    asyncio.run(main())
