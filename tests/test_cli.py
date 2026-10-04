import json

from giraffe.cli import configuration, main, parser
from giraffe.models import RunReport


def test_cli_one_command_has_builtin_suite():
    config = configuration(parser().parse_args([
        "run", "--url", "http://localhost:11434/v1", "--model", "tiny",
        "--max-requests", "50", "--json", "--latency-ms", "5000",
    ]))
    assert len(config.checks) == 12
    assert config.max_requests == 50
    assert config.structured_json
    assert config.limits.latency_ms == 5000


def test_config_cannot_restart_without_explicit_command(tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"targets": [{"name": "test", "url": "http://localhost",
                                             "model": "tiny"}], "restart_target": "test"}))
    assert main(["run", "--config", str(config)]) == 2
    assert "cold-start --restart" in capsys.readouterr().err
    parsed = configuration(parser().parse_args([
        "cold-start", "--config", str(config), "--restart", "test",
    ]))
    assert parsed.restart_target == "test"


def test_baseline_save_never_replaces_implicitly(tmp_path):
    source, destination = tmp_path / "run.json", tmp_path / "baseline.json"
    report = RunReport(run_id="first", started_at="now", finished_at="now", overall="pass",
                       manifest={})
    source.write_text(report.model_dump_json())
    assert main(["baseline", "save", str(source), str(destination)]) == 0
    report.run_id = "second"
    source.write_text(report.model_dump_json())
    assert main(["baseline", "save", str(source), str(destination)]) == 2
    assert json.loads(destination.read_text())["run_id"] == "first"
    assert main(["baseline", "save", str(source), str(destination), "--replace"]) == 0
    assert json.loads(destination.read_text())["run_id"] == "second"


def test_existing_report_directory_rejected_before_network(tmp_path, capsys):
    (tmp_path / "report.json").write_text("existing")
    assert main(["run", "--url", "http://localhost", "--model", "tiny",
                 "--output", str(tmp_path)]) == 2
    assert "not empty" in capsys.readouterr().err


def test_cli_errors_do_not_print_raw_config(tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"targets": [{"name": "test", "url": "https://user:secret@x",
                                             "model": "tiny"}]}))
    assert main(["run", "--config", str(config)]) == 2
    assert "secret" not in capsys.readouterr().err


def test_run_duration_must_be_finite(capsys):
    assert main(["run", "--url", "http://localhost", "--model", "tiny",
                 "--max-duration", "inf"]) == 2
    assert "finite" in capsys.readouterr().err
