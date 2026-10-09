import json
import os
import shlex
import stat
import subprocess
import sys

import pytest

from giraffe.handoff import cli_command
from giraffe.models import RunConfig, RunReport, Target


@pytest.fixture
def config():
    return RunConfig(targets=[Target(name="local", url="http://localhost:8000", model="tiny")],
                     checks=["correctness"])


@pytest.fixture
def baseline():
    return RunReport(run_id="before", started_at="then", finished_at="then", overall="pass",
                     manifest={"config": {"model": "old"}}, observations={"detail": "retained"})


def run_command(tmp_path, command, *, exit_code=0, extra_executables=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    temp_dir = tmp_path / "temp files"
    temp_dir.mkdir()
    capture = tmp_path / "captured.json"
    stub = bin_dir / "giraffe"
    stub.write_text(f"#!{sys.executable}\n" + """
import json
import os
from pathlib import Path
import stat
import sys

args = sys.argv[1:]
captured = {"args": args, "stdin": sys.stdin.read()}
if "--baseline" in args:
    path = Path(args[args.index("--baseline") + 1])
    captured.update(baseline=path.read_text(), baseline_path=str(path),
                    mode=stat.S_IMODE(path.stat().st_mode))
Path(os.environ["CAPTURE"]).write_text(json.dumps(captured))
sys.exit(int(os.environ["GIRAFFE_EXIT"]))
""")
    stub.chmod(0o700)
    for name, content in (extra_executables or {}).items():
        executable = bin_dir / name
        executable.write_text("#!/bin/sh\n" + content)
        executable.chmod(0o700)
    result = subprocess.run(
        ["/bin/sh", "-c", command], capture_output=True, text=True, timeout=5,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
             "TMPDIR": str(temp_dir), "CAPTURE": str(capture), "GIRAFFE_EXIT": str(exit_code)},
    )
    captured = json.loads(capture.read_text()) if capture.exists() else None
    return result, captured, temp_dir


def test_normal_command_has_exact_config_and_needs_no_tempfile(tmp_path, config):
    command = cli_command(config)
    result, captured, temp_dir = run_command(tmp_path, command)
    assert result.returncode == 0, result.stderr
    assert captured["args"] == ["run", "--config", "-", "--output-format", "json"]
    assert captured["stdin"] == config.model_dump_json(indent=2) + "\n"
    assert list(temp_dir.iterdir()) == []
    assert "mktemp" not in command
    assert "--baseline" not in command


@pytest.mark.parametrize("exit_code", [0, 3, 127])
def test_baseline_is_exact_private_and_removed_without_changing_exit_status(
    tmp_path, config, baseline, exit_code,
):
    command = cli_command(config, baseline=baseline)
    result, captured, temp_dir = run_command(tmp_path, command, exit_code=exit_code)
    assert result.returncode == exit_code, result.stderr
    assert captured["stdin"] == config.model_dump_json(indent=2) + "\n"
    assert captured["baseline"] == baseline.model_dump_json(indent=2) + "\n"
    assert captured["args"] == ["run", "--config", "-", "--output-format", "json",
                                 "--baseline", captured["baseline_path"]]
    assert captured["mode"] == stat.S_IRUSR | stat.S_IWUSR
    assert list(temp_dir.iterdir()) == []


def test_shell_metacharacters_and_restart_target_are_literal(tmp_path, config, baseline):
    marker = tmp_path / "injected"
    attack = f"--$(touch {shlex.quote(str(marker))}); `touch {shlex.quote(str(marker))}`\n' \" $HOME"
    config.targets[0].name = attack
    config.targets[0].model = attack
    config.targets[0].restart_command = ["restart", attack]
    config.restart_target = attack
    config.request_options["user"] = attack
    baseline.observations["user"] = attack
    command = cli_command(config, mode="cold-start", restart_target=attack, baseline=baseline)
    result, captured, temp_dir = run_command(tmp_path, command)
    assert result.returncode == 0, result.stderr
    assert captured["args"][:2] == ["cold-start", "--restart=" + attack]
    assert captured["stdin"] == config.model_dump_json(indent=2) + "\n"
    assert captured["baseline"] == baseline.model_dump_json(indent=2) + "\n"
    assert not marker.exists()
    assert list(temp_dir.iterdir()) == []


def test_delimiter_collisions_cannot_end_either_heredoc(
    tmp_path, monkeypatch, config, baseline,
):
    marker = tmp_path / "injected"
    # Exercise the delimiter defense independently of JSON's newline escaping.
    config_payload = "GIRAFFE_CONFIG_EOF\nGIRAFFE_CONFIG_EOF_1\n"
    baseline_payload = "GIRAFFE_BASELINE_EOF\nGIRAFFE_BASELINE_EOF_1\n"
    attack = f"touch {shlex.quote(str(marker))}\n"
    monkeypatch.setattr(RunConfig, "model_dump_json", lambda self, **kwargs: config_payload + attack)
    monkeypatch.setattr(RunReport, "model_dump_json", lambda self, **kwargs: baseline_payload + attack)
    result, captured, temp_dir = run_command(tmp_path, cli_command(config, baseline=baseline))
    assert result.returncode == 0, result.stderr
    assert captured["stdin"] == config_payload + attack + "\n"
    assert captured["baseline"] == baseline_payload + attack + "\n"
    assert not marker.exists()
    assert list(temp_dir.iterdir()) == []


def test_generation_creates_no_files(tmp_path, monkeypatch, config, baseline):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    cli_command(config, baseline=baseline)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failed_tool", ["mktemp", "cat"])
def test_temp_creation_or_write_failure_does_not_run_giraffe(
    tmp_path, config, baseline, failed_tool,
):
    result, captured, temp_dir = run_command(
        tmp_path, cli_command(config, baseline=baseline),
        extra_executables={failed_tool: "exit 9\n"},
    )
    assert result.returncode == 1
    assert captured is None
    assert list(temp_dir.iterdir()) == []


@pytest.mark.parametrize("kwargs", [
    {"mode": "unknown"},
    {"restart_target": "local"},
    {"mode": "cold-start"},
    {"mode": "cold-start", "restart_target": "unknown"},
    {"mode": "cold-start", "restart_target": "local"},
])
def test_invalid_mode_or_restart_is_rejected(config, kwargs):
    with pytest.raises(ValueError):
        cli_command(config, **kwargs)


def test_implicit_or_mismatched_restart_is_rejected(config):
    config.targets[0].restart_command = ["restart"]
    config.restart_target = "local"
    with pytest.raises(ValueError, match="explicit cold-start"):
        cli_command(config)
    config.restart_target = "other"
    with pytest.raises(ValueError, match="match"):
        cli_command(config, mode="cold-start", restart_target="local")


def test_restart_target_nul_is_rejected(config):
    config.targets[0].name = "bad\x00name"
    config.targets[0].restart_command = ["restart"]
    with pytest.raises(ValueError, match="NUL"):
        cli_command(config, mode="cold-start", restart_target="bad\x00name")
