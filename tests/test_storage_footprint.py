"""Normal exports must shrink without changing measured or retained evidence."""

import json
import shutil

from fastapi.testclient import TestClient
import pytest

from giraffe.cli import load_report, main
from giraffe.reporting import _atomic_write, _retained, _storage_limited, compare_baseline, save_baseline_report, write_report
from giraffe.storage import automatic_cleanup, cleanup_plan, set_kept, storage_summary
from giraffe.web import _report, create_app
from scripts.measure_storage import measure, previous_artifacts, synthetic_report
from validation_suite.baseline import iter_report_requests


@pytest.mark.parametrize("retention", ["all", "failures", "none"])
def test_footprint_round_trip_preserves_models_api_and_failure_evidence(tmp_path, retention):
    report = synthetic_report(60, 4096, retention, input_size=2048, input_cases=3)
    original = report.model_dump_json()
    directory = tmp_path / "saved"
    sizes = measure(report, directory)
    assert sizes["current"] < sizes["previous"] * .72
    raw = json.loads((directory / "report.json").read_text())
    assert raw["report_format"] == 2 and len(raw["inputs"]) == 3
    assert all("input_ref" in row and "messages" not in row["evaluation"] for row in raw["requests"])
    assert (directory / "report.json").read_text() == json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
    expected = _retained(report)
    _storage_limited(expected, 0)
    assert load_report(directory) == _report(directory / "report.json") == expected
    assert list(iter_report_requests(directory / "report.json")) == expected.model_dump(mode="json")["requests"]
    assert report.model_dump_json() == original
    assert [row.evaluation.messages for row in expected.requests] == [row.evaluation.messages for row in report.requests]
    assert [bool(row.output) for row in expected.requests] == [
        retention == "all" or (retention == "failures" and row.score is False) for row in report.requests]
    # Same report through the legacy loader and every report-bearing API response.
    legacy_json, _ = previous_artifacts(report)
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "report.json").write_bytes(legacy_json)
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8787") as client:
        new = client.get("/api/runs/saved").json()["report"]
        old = client.get("/api/runs/legacy").json()["report"]
        assert new == old
        assert new["requests_compact"] and new["request_count"] == len(expected.requests)
        assert {k: v for k, v in new.items() if k not in {"requests", "request_count", "requests_compact"}} == expected.model_dump(mode="json", exclude={"requests"})
        for record in expected.requests:
            evidence = client.get(f"/api/runs/saved/requests/{record.id}")
            assert evidence.status_code == 200
            assert evidence.json() == record.model_dump(mode="json")
        # AM5 may append a hydrated raw series after stored metadata; JSON key
        # order differs from fully embedded legacy snapshots, values do not.
        assert client.get("/api/runs/saved/report.json").json() == client.get("/api/runs/legacy/report.json").json()
        html = client.get("/api/runs/saved/report.html")
        assert html.status_code == 200 and "attachment" in html.headers["content-disposition"]
        assert html.content == client.get("/api/runs/legacy/report.html").content
    assert not (directory / "report.html").exists()


def test_cli_html_is_explicit_and_legacy_html_is_regenerated(tmp_path, capsys):
    directory = tmp_path / "run"
    write_report(synthetic_report(10, 100, "failures"), directory)
    assert not (directory / "report.html").exists()
    (directory / "report.html").write_text("stale")
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8787") as client:
        assert "stale" not in client.get("/api/runs/run/report.html").text
    output = tmp_path / "export.html"
    assert main(["report", str(directory), "--html", "--output", str(output)]) == 0
    assert "Known-answer correctness" in output.read_text()
    assert main(["report", str(directory), "--html"]) == 0
    assert output.read_bytes() == (directory / "report.html").read_bytes()


@pytest.mark.parametrize("fallback", [False, True])
def test_baseline_survives_atomic_source_rewrite_and_deletion(tmp_path, monkeypatch, fallback):
    source = tmp_path / "source"
    write_report(synthetic_report(20, 100, "all"), source)
    baseline = tmp_path / ".baselines" / "chosen" / "report.json"
    if fallback:
        monkeypatch.setattr("giraffe.reporting.os.link", lambda *a, **k: (_ for _ in ()).throw(OSError("unsupported")))
    save_baseline_report(source / "report.json", baseline)
    expected = load_report(baseline)
    summary = storage_summary(tmp_path, limits={})
    assert baseline.stat().st_ino != (source / "report.json").stat().st_ino
    assert baseline.stat().st_mode & 0o777 == 0o600
    assert summary["baselines_bytes"] == baseline.stat().st_size
    assert summary["baselines_shared_bytes"] == 0
    assert cleanup_plan(tmp_path, keep_latest=0)["protected"][0]["protected_reason"] == "source run of a saved baseline"
    replacement = synthetic_report(5, 200, "none")
    write_report(replacement, source)
    assert load_report(baseline) == expected
    shutil.rmtree(source)
    assert load_report(baseline) == expected
    assert compare_baseline(expected, load_report(baseline)) == compare_baseline(expected, expected)
    assert storage_summary(tmp_path, limits={})["baselines_shared_bytes"] == 0


def test_automatic_cleanup_reuses_protections_and_records_exact_ids(tmp_path):
    for name in ("old", "kept", "baseline-source", "live", "unknown", "trigger"):
        directory = tmp_path / name
        write_report(synthetic_report(2, 10, "none"), directory)
        value = json.loads((directory / "report.json").read_text())
        value["run_id"] = name
        _atomic_write(directory / "report.json", json.dumps(value, separators=(",", ":")))
    set_kept(tmp_path / "kept", True)
    (tmp_path / "live" / ".web.json").write_text('{"state":"running"}')
    (tmp_path / "unknown" / "mine.txt").write_text("owned by user")
    save_baseline_report(tmp_path / "baseline-source" / "report.json",
                         tmp_path / ".baselines" / "chosen" / "report.json")
    result = automatic_cleanup(tmp_path, {"keep_latest": 0}, active_id="trigger")
    assert result["deleted"] == ["old"]
    assert {path.name for path in tmp_path.iterdir()} == {"kept", "baseline-source", "live", "unknown", "trigger", ".baselines"}


def test_json_limit_measures_deduplicated_bytes(tmp_path):
    report = synthetic_report(60, 100, "all", input_size=4096, input_cases=2)
    path, _ = write_report(report, tmp_path, max_run_storage=0)
    limit = path.stat().st_size + 200
    path, _ = write_report(report, tmp_path, max_run_storage=limit)
    assert path.stat().st_size <= limit
    assert load_report(path).manifest["storage"]["response_bodies_omitted"] == 0
    assert all(row.output for row in load_report(path).requests)


@pytest.mark.parametrize("policy", [{"keep_latest": 1}, {"older_than_days": 0}, {"max_total_bytes": 1}])
def test_web_automatic_cleanup_is_logged_and_policy_is_visible(tmp_path, monkeypatch, policy, local_tokenizer):
    from tests.test_storage_web import completed
    from tests.test_web import config, report as web_report

    write_report(synthetic_report(2, 10, "none"), tmp_path / "old")

    async def runner(configuration, **kwargs):
        return web_report(configuration)

    monkeypatch.setattr("giraffe.web.run_suite", runner)
    with TestClient(create_app(tmp_path, min_free_disk=0, auto_cleanup=policy), base_url="http://127.0.0.1:8787") as client:
        assert client.get("/api/storage").json()["auto_cleanup"] == policy
        identifier = client.post("/api/runs", json={"config": config()}).json()["id"]
        detail = completed(client, identifier)
        assert detail["auto_cleanup"]["deleted"] == ["old"]
        assert client.get("/api/storage").json()["last_auto_cleanup"]["deleted"] == ["old"]
    metadata = json.loads((tmp_path / identifier / ".web.json").read_text())
    assert metadata["auto_cleanup"]["deleted"] == ["old"]
    assert metadata["auto_cleanup_policy"] == policy
    assert not (tmp_path / identifier / "report.html").exists()
    with TestClient(create_app(tmp_path, auto_cleanup=policy), base_url="http://127.0.0.1:8787") as client:
        assert client.get("/api/storage").json()["last_auto_cleanup"]["deleted"] == ["old"]


@pytest.mark.parametrize("html", [False, True])
def test_cli_automatic_cleanup_and_explicit_html(tmp_path, monkeypatch, capsys, html, local_tokenizer):
    from tests.test_storage_cli_regressions import report, run_arguments

    write_report(synthetic_report(2, 10, "none"), tmp_path / "old")

    async def runner(configuration, **kwargs):
        return report()

    monkeypatch.setattr("giraffe.cli._run", runner)
    directory = tmp_path / "trigger"
    arguments = run_arguments(directory) + ["--runs-dir", str(tmp_path), "--auto-cleanup-older-than", "0"]
    if html:
        arguments += ["--html"]
    assert main(arguments) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["auto_cleanup"]["deleted"] == ["old"]
    assert (directory / "report.html").is_file() is html
    metadata = json.loads((directory / ".web.json").read_text())
    assert metadata["auto_cleanup"]["deleted"] == ["old"]
    assert main(["storage", "--runs-dir", str(tmp_path), "--auto-cleanup-keep-latest", "5",
                 "--output-format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["auto_cleanup"] == {"keep_latest": 5}


def test_input_table_streams_past_metadata_value_limit(tmp_path, monkeypatch):
    from validation_suite.scenarios import RESOURCE_POLICY

    report = synthetic_report(60, 10, "none", input_size=2048, input_cases=60)
    path, _ = write_report(report, tmp_path)
    # The table exceeds 64 keys and the per-metadata-value bound, but no entry does.
    monkeypatch.setitem(RESOURCE_POLICY["product_report_decoder"], "metadata_value_bytes", 10000)
    raw = json.loads(path.read_text())
    # Add more unique entries to exercise >64 table members, separate from top-level members.
    from giraffe.report_inputs import input_ref
    for i in range(10):
        messages = [{"role": "user", "content": str(i) + "x" * 2048}]
        raw["inputs"][input_ref(messages)] = messages
    path.write_text(json.dumps(raw, separators=(",", ":")))
    records = list(iter_report_requests(path))
    assert len(records) == 60
    assert records[59]["evaluation"]["messages"] == report.requests[59].evaluation.messages


@pytest.mark.parametrize("fault", ["unknown_version", "missing_input", "ambiguous_input", "invalid_requests"])
def test_invalid_input_references_fail_loaders(tmp_path, fault):
    path, _ = write_report(synthetic_report(2, 10, "all"), tmp_path)
    raw = json.loads(path.read_text())
    if fault == "unknown_version":
        raw["report_format"] = 3
    elif fault == "missing_input":
        raw["inputs"].clear()
    elif fault == "ambiguous_input":
        raw["requests"][0]["evaluation"]["messages"] = []
    else:
        raw["requests"] = None
    path.write_text(json.dumps(raw, separators=(",", ":")))
    with pytest.raises(ValueError):
        load_report(path)
    with pytest.raises(ValueError):
        list(iter_report_requests(path))


@pytest.mark.parametrize("retention", ["failures", "none"])
def test_tool_input_deduplication_follows_retention(tmp_path, retention):
    report = synthetic_report(2, 100, retention)
    for record in report.requests:
        record.tool_conversation = {"final_content": "private", "calls": [{"result": "private"}]}
        record.evaluation.messages = [{"role": "user", "content": "café 🦒"},
                                      {"role": "tool", "content": "private", "tool_call_id": "exact"}]
    path, _ = write_report(report, tmp_path, max_run_storage=0)
    expected = _retained(report)
    _storage_limited(expected, 0)
    assert load_report(path) == expected
    raw = json.loads(path.read_text())
    assert len(raw["inputs"]) == (1 if retention == "failures" else 0)
    assert list(iter_report_requests(path)) == expected.model_dump(mode="json")["requests"]


def test_baseline_import_keeps_retention_and_default_fields(tmp_path):
    source = tmp_path / "report.json"
    report = synthetic_report(3, 100, "none")
    source.write_text(report.model_dump_json(indent=2))
    destination = tmp_path / ".baselines" / "chosen" / "report.json"
    save_baseline_report(source, destination)
    assert all(not row.output for row in load_report(destination).requests)
    assert load_report(source).requests[0].output
    raw = json.loads(destination.read_text())
    assert raw["report_format"] == 2
    assert raw["requests"][0]["client_wait_ms"] is None
    assert raw["requests"][0]["response_events"] == []


def test_synthetic_thousand_default_performance_is_persisted_without_series(tmp_path):
    from tests.performance_fixture import synthetic_report_1000
    from giraffe.reporting import _json_size, _report_json_chunks

    report = synthetic_report_1000()
    path, _ = write_report(report, tmp_path / "synthetic")
    raw = json.loads(path.read_text())
    performance = raw["performance"]

    def has_request_series(value):
        if isinstance(value, dict):
            return "request_series" in value or any(has_request_series(item) for item in value.values())
        return isinstance(value, list) and any(has_request_series(item) for item in value)

    # Snapshot size follows the current metric contract, including additive
    # speculative-decoding rows. Bound duplication structurally: one default
    # target/window summary and no second copy of the 1,000 raw request rows.
    assert not has_request_series(performance)
    assert len(performance["targets"]) == 1
    target = performance["targets"][0]
    assert list(target["summaries"]) == [target["default_window"]]
    assert len(raw["requests"]) == 1000
    saved = load_report(path)
    baseline = saved.model_copy(update={"performance": {}})
    baseline_json = "".join(_report_json_chunks(baseline))
    baseline_size = len(baseline_json.encode("utf-8"))
    empty_performance = json.loads(baseline_json)["performance"]
    # Speculative-decoding rows bring the measured ratio to about 1.1113;
    # 1.12 allows under one percentage point of margin for snapshot metadata.
    assert path.stat().st_size <= 1.12 * baseline_size

    def encoded_size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    # Every extra byte is the single persisted performance snapshot, with the
    # same metadata, conditions and request evidence on either side.
    assert path.stat().st_size - baseline_size == encoded_size(performance) - encoded_size(empty_performance)
    assert _json_size(saved) == path.stat().st_size

    # Snapshot bytes count toward the existing per-run cap. This fixture has no
    # response bodies to trim, so a cap below its metadata size must be reported
    # as exceeded while all request evidence remains available.
    limited_path, _ = write_report(report, tmp_path / "limited", max_run_storage=baseline_size)
    limited = load_report(limited_path)
    assert limited.manifest["storage"]["limit_bytes"] == baseline_size
    assert limited.manifest["storage"]["limit_exceeded"] is True
    assert len(limited.requests) == 1000
    assert limited_path.stat().st_size > baseline_size
    assert _json_size(limited) == limited_path.stat().st_size
