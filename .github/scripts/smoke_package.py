"""Exercise a wheel installed in a fresh environment, outside the checkout."""

import asyncio
from importlib import metadata, resources
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile

import giraffe
import httpx
from giraffe.web import create_app


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
    static = resources.files("giraffe").joinpath("static")
    with tempfile.TemporaryDirectory() as directory:
        app = create_app(Path(directory) / "runs", token="package-smoke-token")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            response = await client.get("/?token=package-smoke-token", follow_redirects=True)
            response.raise_for_status()
            if "<!doctype html>" not in response.text.lower():
                raise RuntimeError("Installed UI did not serve its HTML page")
            for name in (
                "app.js", "form-config.mjs", "styles.css",
                "fonts/Geist-Variable.woff2", "fonts/GeistMono-Variable.woff2", "fonts/OFL.txt",
            ):
                expected = static.joinpath(name).read_bytes()
                response = await client.get(f"/static/{name}")
                response.raise_for_status()
                if not expected or response.content != expected:
                    raise RuntimeError(f"Installed UI asset is missing or corrupted: {name}")
    print(f"Installed giraffe-llm {installed.version}: CLI and UI assets passed")


if __name__ == "__main__":
    asyncio.run(main())
