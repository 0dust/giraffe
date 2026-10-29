"""RAM flags and export measurements through local CLI and API boundaries."""

import errno
import json
import time

import pytest
from fastapi.testclient import TestClient

from giraffe.cli import configuration, main, parser
from giraffe.web import create_app
from tests.test_web import config, report


@pytest.mark.parametrize("value", [0, 32, 4096])
def test_cli_memory_flag(value):
    args = parser().parse_args(["run", "--url", "http://localhost", "--model", "tiny", "--max-memory", str(value)])
    assert configuration(args).max_memory_mb == value


def test_cli_memory_flag_rejects_negative(tmp_path, capsys):
    assert main(["run", "--url", "http://localhost", "--model", "tiny", "--max-memory", "-1",
                 "--output", str(tmp_path / "new")]) == 4
    assert "max_memory_mb" in capsys.readouterr().err
    assert not (tmp_path / "new").exists()


@pytest.mark.parametrize("output_format", ["text", "json"])
def test_cli_prints_export_ram_and_saves_it_in_metadata(tmp_path, monkeypatch, capsys, output_format, local_tokenizer):
    async def suite(configuration, **kwargs):
        result = report(configuration)
        result.observations["resources"] = {
            "sampled_peak_rss_bytes": {"traffic": 123, "scoring": None},
            "process_peak_rss_bytes": 456, "memory_limit_bytes": 4096 * 1024**2,
            "memory_limit_reached": False, "guardrail_active": True}
        return result

    monkeypatch.setattr("giraffe.cli._run", suite)
    monkeypatch.setattr("giraffe.resources.current_rss_bytes", lambda: 789)
    directory = tmp_path / "run"
    assert main(["run", "--url", "http://localhost", "--model", "tiny", "--skip-preflight",
                 "--min-free-disk", "0", "--output", str(directory),
                 "--output-format", output_format, "--verbose"]) == 0
    output = capsys.readouterr().out
    metadata = json.loads((directory / ".web.json").read_text())
    assert metadata["resources"]["sampled_peak_rss_bytes"]["export"] == 789
    if output_format == "json":
        result = json.loads(output)
        assert result["resources"]["sampled_peak_rss_bytes"] == {"traffic": 123, "scoring": None, "export": 789}
    else:
        assert "Peak memory (RAM)" in output and "export" in output
        assert "not recorded" in output
        assert "Process lifetime" in output


@pytest.mark.parametrize("write_fails", [False, True])
def test_web_export_memory_survives_and_is_distinct_from_disk(tmp_path, monkeypatch, write_fails, local_tokenizer):
    async def suite(configuration, **kwargs):
        return report(configuration)

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left")

    monkeypatch.setattr("giraffe.web.run_suite", suite)
    monkeypatch.setattr("giraffe.resources.current_rss_bytes", lambda: 321)
    if write_fails:
        monkeypatch.setattr("giraffe.web.write_report", full)
    with TestClient(create_app(tmp_path, min_free_disk=0), base_url="http://127.0.0.1:8787") as client:
        started = client.post("/api/runs", json={"config": config()})
        assert started.status_code == 202
        identifier = started.json()["id"]
        for _ in range(100):
            detail = client.get(f"/api/runs/{identifier}").json()
            if detail["state"] not in {"running", "stopping"}:
                break
            time.sleep(.01)
        assert detail["state"] == ("error" if write_fails else "completed")
        assert detail["resources"]["sampled_peak_rss_bytes"]["export"] == 321
        assert detail["bytes"] > 0
        saved = json.loads((tmp_path / identifier / ".web.json").read_text())
        assert saved["resources"] == detail["resources"]
        assert "memory_limit_bytes" not in client.get("/api/storage").json()
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8787") as client:
        assert client.get(f"/api/runs/{identifier}").json()["resources"] == saved["resources"]


def test_unavailable_export_rss_is_saved_as_null(tmp_path, monkeypatch, capsys, local_tokenizer):
    async def suite(configuration, **kwargs):
        return report(configuration)

    monkeypatch.setattr("giraffe.cli._run", suite)
    monkeypatch.setattr("giraffe.resources.current_rss_bytes", lambda: None)
    directory = tmp_path / "run"
    assert main(["run", "--url", "http://localhost", "--model", "tiny", "--skip-preflight",
                 "--min-free-disk", "0", "--output", str(directory), "--verbose"]) == 0
    assert "not measured on this platform" in capsys.readouterr().out
    assert json.loads((directory / ".web.json").read_text())["resources"]["sampled_peak_rss_bytes"]["export"] is None


def test_cli_ram_help_and_output_use_growth_and_binary_units(capsys):
    from giraffe.cli import _print_memory

    with pytest.raises(SystemExit) as result:
        main(["run", "--help-all"])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    assert "Maximum memory (RAM growth during this run)" in help_text
    assert "MiB" in help_text
    _print_memory({"rss_at_start_bytes": 8 * 1024**3, "sampled_peak_rss_bytes": {"traffic": 12 * 1024**3},
                   "sampled_peak_growth_bytes": {"traffic": 4 * 1024**3}, "memory_limit_bytes": 4096 * 1024**2,
                   "memory_limit_reached": True, "memory_limit_stopped_run": False})
    output = capsys.readouterr().out
    assert "RAM at run start: 8.00 GiB" in output
    assert "traffic: 4.00 GiB" in output
    assert "Maximum memory (RAM growth during this run): 4.00 GiB" in output
    assert "no work was stopped" in output
    assert "not recorded" in output
    assert "not measured on this platform" not in output
    assert " MB" not in output and " GB" not in output


def test_cli_missing_ram_and_explicit_unavailable_are_distinct(capsys):
    from giraffe.cli import _print_memory

    _print_memory({})
    assert "not measured on this platform" not in capsys.readouterr().out
    _print_memory({"memory_measurement": "unavailable"})
    assert "not measured on this platform" in capsys.readouterr().out
