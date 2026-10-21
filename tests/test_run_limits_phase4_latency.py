import errno
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from giraffe.conditions import condition_for
from giraffe.latency import comparable_latency, input_bucket, latency_summary, load_observations
from giraffe.models import RequestRecord, RunConfig, RunReport
from giraffe.reporting import write_report


NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


def config(**changes):
    return RunConfig(targets=[{"name": "local", "url": "http://localhost:8000", "model": "test"}],
                     checks=["correctness"], **changes)


def record(index=0, **changes):
    return RequestRecord(**{"id": str(index), "target": "local", "fixture_id": "math", "scenario": "correctness_c4",
        "condition_id": "correctness_c4", "check_ids": ["correctness"], "started_at": "2026-10-20T10:00:00Z",
        "status": "completed", "valid": True, "elapsed_ms": 100 * (index + 1), "input_chars": 128,
        "requested_max_tokens": 128, "stream": True, **changes})


def report(records=None, run_config=None):
    condition = condition_for("correctness_c4", request_parameters={"concurrency": 4})
    return RunReport(run_id="example", started_at="2026-10-20T10:00:00Z", finished_at="2026-10-20T10:01:00Z",
        overall="pass", manifest={"config": (run_config or config()).model_dump(mode="json")},
        observations={"targets": {"local": {"conditions": [{"condition": condition}]}}},
        requests=records if records is not None else [record(index) for index in range(5)])


def test_summary_uses_actual_settings_condition_family_and_percentiles():
    saved = latency_summary(report())
    assert saved["targets"][0]["url"] == "http://localhost:8000/v1/chat/completions"
    assert saved["targets"][0]["measurements"] == [{"family": "correctness", "concurrency": 4,
        "stream": True, "output_cap": 128, "input_bucket": "chars:0-255", "n": 5,
        "p50_ms": 300, "p90_ms": 460, "p99_ms": 496, "max_ms": 500, "min_ms": 100,
        "histogram_ms": {"scale": 32, "zero": 0, "buckets": [[212, 1], [244, 1], [263, 1], [276, 1], [286, 1]]},
        "quantiles_ms": pytest.approx([100 + 20 * index for index in range(21)]), "cohort": "example"}]
    assert input_bucket(255) == "chars:0-255"
    assert input_bucket(256) == "chars:256-1023"
    assert input_bucket(1024) == "chars:1024-4095"
    assert "output" not in saved["targets"][0]["measurements"][0]


@pytest.mark.parametrize("changes", [
    {"scenario": "initial"}, {"scenario": "recovery_prime"}, {"scenario": "readiness"},
    {"scenario": "context_calibration"}, {"scenario": "load_calibration_short_short_answer"},
    {"scenario": "stress_calibration_short"}, {"status": "timeout"}, {"status": "failed"},
    {"status": "cancelled"}, {"valid": False}, {"error": "protocol failure"},
    {"intentional": True}, {"client_cancelled_ms": 50},
])
def test_summary_excludes_unrepresentative_or_unsuccessful_requests(changes):
    assert latency_summary(report([record(**changes)]))["targets"][0]["measurements"] == []


def test_cold_start_excludes_entire_run():
    assert latency_summary(report(run_config=config(restart_target="local")))["targets"] == []


def test_write_report_persists_summary_without_replacing_web_metadata(tmp_path):
    (tmp_path / ".web.json").write_text(json.dumps({"state": "running", "resources": {"rss": 123}}))
    current = report()
    write_report(current, tmp_path)
    metadata = json.loads((tmp_path / ".web.json").read_text())
    assert metadata["state"] == "running"
    assert metadata["resources"] == {"rss": 123}
    assert metadata["latency_summary"] == latency_summary(current)
    # A CLI run has no existing web metadata.
    write_report(current, tmp_path / "cli")
    assert json.loads((tmp_path / "cli/.web.json").read_text())["latency_summary"] == latency_summary(current)


def test_summary_write_failure_keeps_complete_report_and_saves_warning(tmp_path, monkeypatch):
    import giraffe.reporting as reporting

    original = reporting._atomic_write

    def fail_metadata(path, content):
        if path.name == ".web.json":
            raise OSError(errno.ENOSPC, "sensitive filesystem path")
        return original(path, content)

    monkeypatch.setattr(reporting, "_atomic_write", fail_metadata)
    current = report()
    json_path, _ = write_report(current, tmp_path)
    expected = "Latency summary could not be saved: disk full"
    assert current.warnings == [expected]
    assert json.loads(json_path.read_text())["warnings"] == [expected]


def save_summary(tmp_path, name, current):
    directory = tmp_path / name
    directory.mkdir()
    (directory / ".web.json").write_text(json.dumps({"latency_summary": latency_summary(current)}))
    return directory / ".web.json"


def test_lookup_normalizes_endpoint_and_remaps_target_name_without_reading_reports(tmp_path, monkeypatch):
    save_summary(tmp_path, "recent", report())
    selected = config()
    selected.targets[0].url = "http://localhost:8000/v1/"
    selected.targets[0].name = "renamed"
    original = Path.read_text

    def metadata_only(path, *args, **kwargs):
        assert path.name == ".web.json"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", metadata_only)
    observations = load_observations(tmp_path, selected, now=NOW)
    assert observations["renamed"][0]["n"] == 5
    assert observations["renamed"][0]["family"] == "correctness"


@pytest.mark.parametrize("change", ["url", "model", "old", "future", "deployment"])
def test_lookup_rejects_other_endpoints_models_deployments_or_age(tmp_path, change):
    current = report()
    selected = config()
    if change == "url":
        selected.targets[0].url = "http://localhost:9000"
    elif change == "model":
        selected.targets[0].model = "another"
    elif change == "deployment":
        selected.targets[0].deployment.runtime = {"version": "2"}
    elif change == "old":
        current.finished_at = "2026-09-20T00:00:00Z"
    else:
        current.finished_at = "2026-10-22T00:00:00Z"
    save_summary(tmp_path, "other", current)
    assert load_observations(tmp_path, selected, now=NOW) == {"local": []}


def test_lookup_uses_matching_deployment_fingerprint_ignoring_collection_time(tmp_path):
    selected = config()
    selected.targets[0].deployment.runtime = {"version": "1"}
    current = report(run_config=selected)
    save_summary(tmp_path, "matched", current)
    selected.targets[0].deployment.collected_at = "2026-10-21T00:00:00Z"
    assert load_observations(tmp_path, selected, now=NOW)["local"][0]["n"] == 5


def test_metadata_cache_uses_path_and_mtime_and_latest_comparable_run(tmp_path, monkeypatch):
    path = save_summary(tmp_path, "old", report([record(elapsed_ms=150)]))
    newer = report([record(elapsed_ms=200)])
    newer.finished_at = "2026-10-20T11:00:00Z"
    save_summary(tmp_path, "new", newer)
    cache = {}
    observations = load_observations(tmp_path, config(), now=NOW, cache=cache)
    assert len(cache) == 2
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: pytest.fail("cached metadata reread"))
    assert load_observations(tmp_path, config(), now=NOW, cache=cache) == observations
    monkeypatch.setattr(Path, "read_text", original)
    previous = path.stat().st_mtime_ns
    path.write_text(json.dumps({"latency_summary": latency_summary(report([record(elapsed_ms=300)]))}))
    import os
    os.utime(path, ns=(previous + 1, previous + 1))
    updated = load_observations(tmp_path, config(), now=NOW, cache=cache)
    assert len(cache) == 2
    assert updated["local"][0]["p50_ms"] == 200
    assert updated["local"][1]["p50_ms"] == 300
    assert comparable_latency(updated, "local", family="correctness", concurrency=4,
        stream=True, output_cap=128, input_bucket=input_bucket(128)) == {"n": 1, "p50_ms": 200, "p90_ms": 200,
        "p99_ms": 200, "max_ms": 200, "quantiles_ms": [200] * 21,
        "cohort": str((tmp_path / "new/.web.json").resolve()),
        "min_ms": 200, "histogram_ms": {"scale": 32, "zero": 0, "buckets": [[244, 1]]},
        "cohorts": [str((tmp_path / "new/.web.json").resolve())], "run_count": 1,
        "batch_timing_coverage": 0, "drift_detected": True, "latency_trending": False, "effective_n": 1}


@pytest.mark.parametrize("changed", [
    {"family": "serving"}, {"concurrency": 1}, {"stream": False}, {"input_bucket": input_bucket(500)},
    {"output_cap": 95}, {"output_cap": 161},
])
def test_comparable_latency_requires_every_comparison_dimension(changed):
    entry = latency_summary(report())["targets"][0]["measurements"][0]
    assert comparable_latency({"local": [{**entry, **changed}]}, "local", family="correctness", concurrency=4,
        stream=True, output_cap=128, input_bucket=input_bucket(128)) is None


@pytest.mark.parametrize("cap", [96, 128, 160])
def test_comparable_output_caps_include_exact_25_percent_boundary(cap):
    entry = latency_summary(report())["targets"][0]["measurements"][0]
    assert comparable_latency({"local": [{**entry, "output_cap": cap}]}, "local", family="correctness", concurrency=4,
        stream=True, output_cap=128, input_bucket=input_bucket(128)) == {"n": 5, "p50_ms": 300, "p90_ms": 460,
        "p99_ms": 496, "max_ms": 500,
        "quantiles_ms": pytest.approx([100 + 20 * index for index in range(21)]), "cohort": "example",
        "min_ms": 100, "histogram_ms": {"scale": 32, "zero": 0, "buckets": [[212, 1], [244, 1], [263, 1], [276, 1], [286, 1]]},
        "cohorts": ["example"], "run_count": 1, "batch_timing_coverage": 0, "drift_detected": False,
        "latency_trending": False, "effective_n": 5}


def test_summary_separates_actual_spec_dimensions_and_condition_level():
    current = report([
        record(0), record(1, stream=False), record(2, requested_max_tokens=256),
        record(3, input_chars=1000),
        record(4, condition_id="load_combo_short_short_c8", scenario="load_combo_short_short_c8"),
    ])
    measurements = latency_summary(current)["targets"][0]["measurements"]
    assert len(measurements) == 5
    assert sum(entry["n"] for entry in measurements) == 5
    assert next(entry for entry in measurements if entry["family"] == "concurrency")["concurrency"] == 8
    assert next(entry for entry in measurements if entry["output_cap"] == 256)["p50_ms"] == 300
    assert next(entry for entry in measurements if entry["input_bucket"] == input_bucket(1000))["p50_ms"] == 400
    assert next(entry for entry in measurements if not entry["stream"])["p50_ms"] == 200


def test_summary_concurrency_matches_effective_overlapping_client_shares():
    configured = RunConfig(targets=[{"name": name, "url": "http://localhost", "model": "test"}
                                    for name in ("local", "other")],
        checks=["correctness"], overlap_models=True, concurrency=4,
        test_options={"correctness": {"concurrency": 3, "samples": 6}})
    current = report([record(target=name) for name in ("local", "other")], run_config=configured)
    condition = condition_for("correctness_c4", request_parameters={"concurrency": 3})
    current.observations = {"targets": {name: {"conditions": [{"condition": condition}]}
                                      for name in ("local", "other")}}
    summary = latency_summary(current)
    assert [target["measurements"][0]["concurrency"] for target in summary["targets"]] == [3, 3]


def test_lookup_includes_exact_thirty_day_boundary_and_ignores_malformed_metadata(tmp_path):
    current = report()
    current.finished_at = "2026-09-21T00:00:00Z"
    save_summary(tmp_path, "boundary", current)
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".web.json").write_text("{")
    assert load_observations(tmp_path, config(), now=NOW)["local"][0]["n"] == 5


def test_lookup_computes_current_identity_once_and_prunes_replaced_or_removed_cache_entries(tmp_path, monkeypatch):
    import giraffe.latency as latency

    paths = [save_summary(tmp_path, str(index), report()) for index in range(3)]
    fingerprints = []
    original = latency._deployment_fingerprint

    def fingerprint(deployment):
        fingerprints.append(deployment)
        return original(deployment)

    monkeypatch.setattr(latency, "_deployment_fingerprint", fingerprint)
    cache = {}
    assert len(load_observations(tmp_path, config(), now=NOW, cache=cache)["local"]) == 3
    assert len(fingerprints) == 1
    assert len(cache) == 3
    paths[0].write_text(json.dumps({"latency_summary": latency_summary(report([record(elapsed_ms=1234)]))}))
    updated = load_observations(tmp_path, config(), now=NOW, cache=cache)["local"]
    assert len(cache) == len(updated) == 3
    assert any(entry["p50_ms"] == 1234 for entry in updated)
    for path in paths:
        path.unlink()
    assert load_observations(tmp_path, config(), now=NOW, cache=cache) == {"local": []}
    assert cache == {}


def test_lookup_rejects_missing_current_fingerprint_when_saved_deployment_known(tmp_path):
    deployed = config()
    deployed.targets[0].deployment.runtime = {"version": "1"}
    save_summary(tmp_path, "known", report(run_config=deployed))
    assert load_observations(tmp_path, config(), now=NOW) == {"local": []}


def test_lookup_matches_local_metadata_facts_and_rejects_changed_or_missing_file(tmp_path):
    metadata_file = tmp_path / "deployment.json"
    metadata_file.write_text(json.dumps({"runtime": {"version": "1"}}))
    selected = config()
    selected.targets[0].deployment.metadata_file = str(metadata_file)
    save_summary(tmp_path, "imported", report(run_config=selected))
    assert load_observations(tmp_path, selected, now=NOW)["local"][0]["n"] == 5
    metadata_file.write_text(json.dumps({"runtime": {"version": "2"}}))
    assert load_observations(tmp_path, selected, now=NOW) == {"local": []}
    metadata_file.unlink()
    assert load_observations(tmp_path, selected, now=NOW) == {"local": []}


def test_lookup_never_discards_explicit_observed_deployment_fingerprint(tmp_path):
    current = report()
    current.observations["targets"]["local"]["deployment"] = {"fingerprint": "observed-runtime-1"}
    saved = latency_summary(current)
    assert saved["targets"][0]["deployment_fingerprint"] == "observed-runtime-1"
    save_summary(tmp_path, "observed", current)
    assert load_observations(tmp_path, config(), now=NOW) == {"local": []}
