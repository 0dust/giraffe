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
    target = config.targets[0]
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


FORBIDDEN_CHARACTERS = [chr(code) for code in (
    *range(0x20), *range(0x7f, 0xa0), *range(0x200b, 0x2010),
    *range(0x202a, 0x202f), *range(0x2066, 0x206a), 0x2060, 0xfeff)
    if code not in (9, 10)]


@pytest.mark.parametrize("character", FORBIDDEN_CHARACTERS)
def test_preview_rejects_controls_bidi_and_invisible_characters(tmp_path, character):
    path = script(tmp_path, f"# safe{character}hidden\nexit 0\n")
    with pytest.raises(ValueError) as error:
        preview_script(str(path))
    assert str(error.value) == "Restart script contains control or invisible characters. Remove them to review it safely."


def test_preview_accepts_newlines_tabs_and_visible_unicode(tmp_path):
    path = script(tmp_path, "# café 🦒\n\ttrue\n")
    assert preview_script(str(path))["contents"] == path.read_text()


@pytest.mark.parametrize("contents", ["echo restart\n", "\n#!/bin/sh\ntrue\n", ""])
def test_preview_requires_shebang_at_start(tmp_path, contents):
    path = script(tmp_path, "")
    path.write_text(contents)
    with pytest.raises(ValueError) as error:
        preview_script(str(path))
    assert str(error.value) == "Restart script must start with a #! line."


async def test_script_environment_removes_credentials_for_every_target_only(tmp_path, monkeypatch):
    names = ["COLD_API_ONE", "COLD_HEADER_ONE", "COLD_API_TWO", "COLD_HEADER_TWO"]
    for name in names:
        monkeypatch.setenv(name, "test-secret")
    monkeypatch.setenv("COLD_INHERITED", "kept")
    path = script(tmp_path, "\n".join(f'printf "%s\\n" "${{{name}-removed}}"' for name in names)
                  + '\nprintf "%s\\n" "$COLD_INHERITED"\n')
    run, target, _ = run_context(tmp_path, path)
    target.api_key_env, target.headers_env = names[0], {"X-Test": names[1]}
    run.config.targets.append(Target(name="other", url="http://localhost", model="tiny",
        api_key_env=names[2], headers_env={"X-Other": names[3]}))
    assert await runner._restart(run, target)
    assert (tmp_path / "restart.log").read_text().splitlines() == ["removed"] * 4 + ["kept"]
    assert all(os.environ[name] == "test-secret" for name in names)


async def test_log_tail_redacts_all_configured_keys_and_headers_but_preserves_private_raw_log(tmp_path, monkeypatch, capsys):
    from pydantic import SecretStr
    from giraffe.cli import _print_result
    from giraffe.models import RunReport
    from giraffe.reporting import _cold_start_html, write_report

    secrets = ["pasted-session-key", "configured-env-key", "header-value", "other-target-key", "other-header"]
    for name, value in zip(["COLD_API", "COLD_HEADER", "COLD_OTHER_API", "COLD_OTHER_HEADER"], secrets[1:]):
        monkeypatch.setenv(name, value)
    path = script(tmp_path, "printf '%s\\n' '" + " ".join(secrets) + "'\n")
    run, target, _ = run_context(tmp_path, path)
    target._api_key = SecretStr(secrets[0])
    target.api_key_env, target.headers_env = "COLD_API", {"X-Test": "COLD_HEADER"}
    run.config.targets.append(Target(name="other", url="http://localhost", model="tiny",
        api_key_env="COLD_OTHER_API", headers_env={"X-Other": "COLD_OTHER_HEADER"}))
    assert await runner._restart(run, target)
    raw = (tmp_path / "restart.log").read_text()
    assert all(secret in raw for secret in secrets)
    assert stat.S_IMODE((tmp_path / "restart.log").stat().st_mode) == 0o600
    tail = run.observations["local"]["restart"]["output_tail"]
    assert tail.count("[redacted]") == len(secrets)
    report = RunReport(run_id="cold", started_at="now", finished_at="now", overall="pass",
        manifest={}, observations={"targets": run.observations})
    write_report(report, tmp_path)
    _print_result(report, tmp_path / "report.json", None, tmp_path, {})
    for rendered in (report.model_dump_json(), _cold_start_html(report),
                     (tmp_path / "report.json").read_text(), capsys.readouterr().out):
        assert all(secret not in rendered for secret in secrets)
        assert "[redacted]" in rendered


async def test_redaction_covers_secret_crossing_raw_tail_cutoff(tmp_path, monkeypatch):
    secret = "boundary-credential-1234567890"
    monkeypatch.setenv("COLD_BOUNDARY", secret)
    path = script(tmp_path, f"printf '%s' '{secret}'\nprintf '%04080d' 0\n")
    run, target, _ = run_context(tmp_path, path)
    target.api_key_env = "COLD_BOUNDARY"
    assert await runner._restart(run, target)
    tail = run.observations["local"]["restart"]["output_tail"]
    assert secret[-16:] not in tail
    assert tail.startswith("[redacted]")


@pytest.mark.parametrize("secret", [b"boundary-credential", b"very-long-key-" * 1000])
def test_redaction_bounds_large_logs_and_secrets_before_truncation(tmp_path, secret):
    from giraffe.restart_script import read_output_tail

    path = tmp_path / "restart.log"
    path.write_bytes(b"prefix" * 10000 + secret + b"a" * 4080)
    result = read_output_tail(path, {secret})
    assert result == "[redacted]" + "a" * 4080
    assert len(result.encode()) <= 4096


@pytest.mark.parametrize("padding", [0, 4080])
def test_redaction_merges_overlapping_credential_values(tmp_path, padding):
    from giraffe.restart_script import read_output_tail

    path = tmp_path / "restart.log"
    path.write_bytes(b"prefixABCdef" + b"." * padding)
    assert read_output_tail(path, {b"prefixABC", b"ABCdef"}) == "[redacted]" + "." * padding


@pytest.mark.parametrize("contents,message", [
    ("#!/bin/sh\n# hidden\u202espoof\n", "Restart script contains control or invisible characters. Remove them to review it safely."),
    ("echo not-a-script\n", "Restart script must start with a #! line.")])
async def test_execution_revalidates_unsafe_contents_before_launch(tmp_path, contents, message):
    path = script(tmp_path, "true\n")
    run, target, _ = run_context(tmp_path, path)
    path.write_text(contents)
    assert not await runner._restart(run, target)
    assert run.observations["local"]["restart"]["reason"] == message


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
    child_pid = tmp_path / "child.pid"
    path = script(tmp_path, f"sleep 20 &\necho $! > '{child_pid}'\necho complete\n")
    run, target, events = run_context(tmp_path, path)
    started = time.monotonic()
    try:
        assert await runner._restart(run, target)
        assert time.monotonic() - started < 10
        obs = run.observations["local"]["restart"]
        assert "complete" in obs["output_tail"] and obs["script_ms"] >= 0
        assert stat.S_IMODE((tmp_path / "restart.log").stat().st_mode) == 0o600
        assert events[0][0][1:4] == ("restart_script", "Running restart script", "running")
    finally:
        if child_pid.exists():
            try:
                os.kill(int(child_pid.read_text()), 15)
            except ProcessLookupError:
                pass


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


@pytest.mark.parametrize("ending", ["success", "failure", "timeout", "stop", "cancel"])
async def test_flushed_script_output_is_retained_and_redacted_on_every_exit(tmp_path, monkeypatch, ending):
    secret = "exit-path-test-key"
    monkeypatch.setenv("COLD_EXIT_KEY", secret)
    ready = tmp_path / "output-flushed"
    body = f"printf '%s\\n' 'restart issued {secret}'\n: > '{ready}'\n"
    body += "exit 0\n" if ending == "success" else "exit 7\n" if ending == "failure" else "while :; do sleep .05; done\n"
    run, target, _ = run_context(tmp_path, script(tmp_path, body),
                                 restart_script_timeout_seconds=.5 if ending == "timeout" else 5)
    target.api_key_env = "COLD_EXIT_KEY"
    task = asyncio.create_task(runner._restart(run, target))
    try:
        async with asyncio.timeout(3):
            while not ready.exists():
                await asyncio.sleep(.005)
        if ending == "stop":
            run.stop.set()
        if ending == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await asyncio.wait_for(task, 3) is (ending == "success")
        assert (tmp_path / "restart.log").read_text() == f"restart issued {secret}\n"
        assert stat.S_IMODE((tmp_path / "restart.log").stat().st_mode) == 0o600
        observation = run.observations["local"]["restart"]
        assert observation["output_tail"] == "restart issued [redacted]\n"
        assert observation["status"] == ("hook_completed" if ending == "success" else "blocked")
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


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
