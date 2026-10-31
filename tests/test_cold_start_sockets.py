"""Opt-in local HTTP acceptance; not executed in the socket-restricted workspace.

Run with GIRAFFE_RUN_COLD_SOCKET_TESTS=1 PYTHONPATH=$PWD/src python -m pytest -q
 tests/test_cold_start_sockets.py. These fake servers do not validate Ollama or llama.cpp.
"""

import asyncio
import json
import os
import signal
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

from giraffe import runner
from giraffe.models import RunConfig, Target
from giraffe.restart_script import preview_script


pytestmark = pytest.mark.skipif(
    os.environ.get("GIRAFFE_RUN_COLD_SOCKET_TESTS") != "1",
    reason="Opt-in socket tests: set GIRAFFE_RUN_COLD_SOCKET_TESTS=1 outside the sandbox",
)


def server_script(tmp_path, ending):
    metadata = tmp_path / "server.json"
    server = tmp_path / "server.py"
    server.write_text(
        "import json, os\nfrom pathlib import Path\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        body = json.dumps({'pid': os.getpid()}).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Length', str(len(body)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(body)\n"
        "    def log_message(self, *args):\n"
        "        pass\n"
        "server = HTTPServer(('127.0.0.1', 0), Handler)\n"
        f"Path({str(metadata)!r}).write_text(json.dumps({{'pid': os.getpid(), 'port': server.server_port}}))\n"
        "server.serve_forever()\n"
    )
    script = tmp_path / "restart.py"
    script.write_text(
        f"#!{sys.executable} -S\n"
        "import subprocess, sys, time\nfrom pathlib import Path\n"
        f"subprocess.Popen([sys.executable, '-S', {str(server)!r}])\n"
        f"while not Path({str(metadata)!r}).exists():\n    time.sleep(.01)\n"
        "print('restart issued', flush=True)\n"
        + ("time.sleep(60)\n" if ending != "success" else "")
    )
    script.chmod(0o700)
    return script, metadata


def context(tmp_path, script):
    target = Target(name="local", url="http://localhost/v1", model="tiny", restart_script=str(script))
    config = RunConfig(targets=[target], restart_target="local", restart_script_timeout_seconds=3,
                       restart_script_sha256=preview_script(str(script))["sha256"])
    config._restart_log_dir = tmp_path / "run"
    return SimpleNamespace(config=config, observations={"local": {}}, stop=asyncio.Event(),
        deadline=time.monotonic()+10, allowed=lambda _: True,
        preparation=lambda *args, **kwargs: None), target


@pytest.mark.parametrize("ending", ["success", "stop", "timeout"])
async def test_background_http_server_survives_script_exit_stop_and_timeout(tmp_path, ending):
    script, metadata = server_script(tmp_path, ending)
    run, target = context(tmp_path, script)
    task = asyncio.create_task(runner._restart(run, target))
    pid = None
    try:
        async with asyncio.timeout(5):
            while not metadata.exists():
                await asyncio.sleep(.01)
        info = json.loads(metadata.read_text())
        pid = info["pid"]
        if ending == "stop":
            # The child publishes metadata before the script resumes and prints.
            # Stop only after there is flushed script output to retain.
            log_path = run.config._restart_log_dir / "restart.log"
            async with asyncio.timeout(3):
                while not log_path.exists() or "restart issued" not in log_path.read_text():
                    await asyncio.sleep(.01)
            run.stop.set()
        result = await asyncio.wait_for(task, 1 if ending == "success" else 5)
        assert result is (ending == "success")
        # The HTTP response proves the inherited-output child remains useful,
        # beyond merely having a live/zombie PID after script termination.
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(f"http://127.0.0.1:{info['port']}/health", timeout=1)
        assert response.status_code == 200 and response.json() == {"pid": pid}
        observed = run.observations["local"]["restart"]
        assert observed["status"] == ("hook_completed" if ending == "success" else "blocked")
        assert "restart issued" in observed["output_tail"]
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if pid is None and metadata.exists():
            pid = json.loads(metadata.read_text())["pid"]
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


async def test_changed_after_confirmation_never_starts_background_http_server(tmp_path):
    script, metadata = server_script(tmp_path, "success")
    run, target = context(tmp_path, script)
    script.write_text(script.read_text() + "# changed after confirmation\n")
    assert not await runner._restart(run, target)
    assert run.observations["local"]["restart"]["reason"] == "The script changed. Review it again."
    assert not metadata.exists()
