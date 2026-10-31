"""Executable-script boundaries, including inherited file handles and child survival."""
import asyncio
import os
import stat
import time
from types import SimpleNamespace

import pytest

from giraffe import runner
from giraffe.models import RunConfig, Target
from giraffe.restart_script import preview_script


def script(tmp_path, text):
    path = tmp_path / "restart.sh"
    path.write_text("#!/bin/sh\n" + text)
    path.chmod(0o700)
    return path


def run_context(tmp_path, path, **options):
    target = Target(name="local", url="http://localhost:11434/v1", model="tiny", restart_script=str(path))
    config = RunConfig(targets=[target], restart_target="local", **options)
    config.restart_script_sha256 = preview_script(str(path))["sha256"]
    config._restart_log_dir = tmp_path
    events = []
    run = SimpleNamespace(config=config, observations={"local": {}}, stop=asyncio.Event(),
        deadline=time.monotonic()+10, allowed=lambda _: True,
        preparation=lambda *args, **kwargs: events.append((args, kwargs)))
    return run, target, events


def test_preview_identifies_exact_executable_text(tmp_path):
    path = script(tmp_path, "echo '<script> & secrets'\n")
    result = preview_script(str(path))
    assert result["path"] == str(path.resolve())
    assert result["size"] == path.stat().st_size
    assert result["contents"] == path.read_text()
    assert len(result["sha256"]) == 64 and result["machine"]


@pytest.mark.parametrize("kind,match", [("missing", "exist"), ("directory", "regular"),
    ("permission", "executable"), ("size", "64 KiB"), ("binary", "UTF-8")])
def test_preview_field_errors(tmp_path, kind, match):
    path = tmp_path / "script"
    if kind == "directory":
        path.mkdir()
    elif kind != "missing":
        path.write_bytes(b"\xff" if kind == "binary" else b"a" * (65537 if kind == "size" else 1))
        path.chmod(0o600 if kind == "permission" else 0o700)
    with pytest.raises(ValueError, match=match):
        preview_script(str(path))


async def test_no_confirmation_or_changed_hash_never_executes(tmp_path):
    marker = tmp_path / "ran"
    path = script(tmp_path, f"touch '{marker}'\n")
    run, target, _ = run_context(tmp_path, path)
    run.config.restart_script_sha256 = None
    assert not await runner._restart(run, target)
    assert not marker.exists()
    run.config.restart_script_sha256 = preview_script(str(path))["sha256"]
    path.write_text(path.read_text() + "echo changed\n")
    assert not await runner._restart(run, target)
    assert run.observations["local"]["restart"]["reason"] == "The script changed. Review it again."
    assert not marker.exists()


async def test_script_exits_without_waiting_for_inherited_output_handle(tmp_path):
    path = script(tmp_path, "sleep 0.6 &\necho complete\n")
    run, target, events = run_context(tmp_path, path)
    started = time.monotonic()
    assert await runner._restart(run, target)
    assert time.monotonic() - started < .5
    obs = run.observations["local"]["restart"]
    assert "complete" in obs["output_tail"] and obs["script_ms"] >= 0
    assert stat.S_IMODE((tmp_path / "restart.log").stat().st_mode) == 0o600
    assert events[0][0][1:4] == ("restart_script", "Running restart script", "running")


async def test_nonzero_is_blocked_with_output_and_exit_code(tmp_path):
    run, target, _ = run_context(tmp_path, script(tmp_path, "echo failure\nexit 7\n"))
    assert not await runner._restart(run, target)
    obs = run.observations["local"]["restart"]
    assert obs["status"] == "blocked" and obs["exit_code"] == 7
    assert "failure" in obs["output_tail"]


async def test_script_has_its_own_timeout_and_only_last_4k_output_is_reported(tmp_path):
    path = script(tmp_path, "sleep .1\nprintf '%05000d' 0\necho tail\n")
    run, target, _ = run_context(tmp_path, path, request_timeout_seconds=.01,
                                 restart_script_timeout_seconds=3)
    assert await runner._restart(run, target)
    data = (tmp_path / "restart.log").read_bytes()
    assert len(data) > 4096
    assert run.observations["local"]["restart"]["output_tail"] == data[-4096:].decode()


async def test_cancelling_restart_task_preserves_started_child(tmp_path):
    child_pid = tmp_path / "child.pid"
    path = script(tmp_path, f"sleep 10 &\necho $! > '{child_pid}'\nwhile :; do sleep .05; done\n")
    run, target, _ = run_context(tmp_path, path, restart_script_timeout_seconds=5)
    task = asyncio.create_task(runner._restart(run, target))
    try:
        async with asyncio.timeout(3):
            while not child_pid.exists():
                await asyncio.sleep(.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pid = int(child_pid.read_text())
        os.kill(pid, 0)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if child_pid.exists():
            os.kill(int(child_pid.read_text()), 15)


@pytest.mark.parametrize("stop", [False, True])
async def test_stop_and_timeout_terminate_only_script_and_preserve_child(tmp_path, stop):
    child_pid = tmp_path / "child.pid"
    path = script(tmp_path, f"sleep 10 &\necho $! > '{child_pid}'\nwhile :; do sleep .05; done\n")
    run, target, _ = run_context(tmp_path, path, restart_script_timeout_seconds=2)
    task = asyncio.create_task(runner._restart(run, target))
    until = time.monotonic() + 3
    while not child_pid.exists() and time.monotonic() < until and not task.done():
        await asyncio.sleep(.005)
    assert child_pid.exists(), run.observations
    if stop:
        run.stop.set()
    assert not await asyncio.wait_for(task, 4)
    pid = int(child_pid.read_text())
    try:
        os.kill(pid, 0)
    finally:
        # Test owns this harmless sleeper; Giraffe must leave it alive.
        os.kill(pid, 15)
    assert run.observations["local"]["restart"]["status"] == "blocked"


def test_legacy_restart_command_is_dropped_with_migration_note():
    config = RunConfig(targets=[dict(name="local", url="http://localhost/v1", model="tiny",
        restart_command=["anything"])], restart_target="local")
    assert "restart_command" not in config.model_dump_json()
    assert config.targets[0].restart_script is None
    assert config.restart_target is None
    assert any("put it in a script" in note for note in config._migration_notes)
