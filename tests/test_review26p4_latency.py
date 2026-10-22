import json
from datetime import datetime, timezone

import pytest

from giraffe.fixtures import FIXTURE_VERSION, builtin_fixtures
from giraffe.latency import (calibrated_context_fixtures, comparable_latency, input_bucket,
                             latency_summary, load_observations)
from giraffe.models import RequestRecord, RunConfig, RunReport
from giraffe.reporting import write_report


def config(**kwargs):
    return RunConfig(targets=[{"name": "local", "model": "model", "url": "http://localhost:8000"}],
                     checks=["context"], **kwargs)


def report(*, run_id="run", elapsed=(10, 20, 30, 1000), family="context", inputs=None):
    current = config()
    return RunReport(run_id=run_id, overall="pass", started_at="2026-10-20T00:00:00Z",
        finished_at="2026-10-20T00:01:00Z", manifest={"config": current.model_dump(mode="json")},
        requests=[RequestRecord(id=str(index), target="local", fixture_id="fixture", scenario=family, check_ids=["context"],
            status="completed", valid=True, started_at="2026-10-20T00:00:00Z", elapsed_ms=value,
            input_chars=(inputs or [128] * len(elapsed))[index], requested_max_tokens=128,
            stream=True) for index, value in enumerate(elapsed)])


def test_summary_records_empirical_tail_and_separate_run_identity():
    saved = latency_summary(report())
    assert saved["version"] == 3
    row = saved["targets"][0]["measurements"][0]
    assert row["n"] == 4
    assert row["p50_ms"] == 25
    assert row["p90_ms"] == pytest.approx(709)
    assert row["p99_ms"] == pytest.approx(970.9)
    assert row["max_ms"] == 1000
    assert len(row["quantiles_ms"]) == 21
    assert row["quantiles_ms"][10] == row["p50_ms"]
    assert row["quantiles_ms"][18] == 709
    assert row["quantiles_ms"][19] == 854.5
    assert row["quantiles_ms"][20] == row["max_ms"]
    assert row["cohort"] == "run"


def test_lookup_and_explicit_cohort_never_combine_runs(tmp_path):
    for name, elapsed in (("old", (10,)), ("new", (1000,))):
        directory = tmp_path / name
        directory.mkdir()
        saved = latency_summary(report(run_id=name, elapsed=elapsed))
        saved["finished_at"] = "2026-10-20T00:01:00Z" if name == "old" else "2026-10-20T00:02:00Z"
        (directory / ".web.json").write_text(json.dumps({"latency_summary": saved}))
    observed = load_observations(tmp_path, config(), now=datetime(2026, 10, 21, tzinfo=timezone.utc))
    assert len({entry["cohort"] for entry in observed["local"]}) == 2
    match = dict(family="context", concurrency=1, stream=True, output_cap=128, input_bucket=input_bucket(128))
    newest = comparable_latency(observed, "local", **match)
    assert newest["p50_ms"] == newest["p99_ms"] == newest["max_ms"] == 1000
    oldest = comparable_latency(observed, "local", cohort=str((tmp_path / "old/.web.json").resolve()), **match)
    assert oldest["p50_ms"] == oldest["max_ms"] == 10
    assert comparable_latency(observed, "local", cohort="missing", **match) is None


def test_old_summary_tail_is_unknown_and_malformed_tail_is_not_used():
    match = dict(family="context", concurrency=1, stream=True, output_cap=128, input_bucket=input_bucket(128))
    row = {**match, "n": 5, "p50_ms": 10, "p90_ms": 20}
    assert comparable_latency({"local": [row]}, "local", **match) == {
        "n": 5, "p50_ms": 10, "p90_ms": 20, "cohort": None}
    assert comparable_latency({"local": [{**row, "p99_ms": 15, "max_ms": float("nan")}]},
        "local", **match) == {"n": 5, "p50_ms": 10, "p90_ms": 20, "cohort": None}


def test_context_rebuilds_actual_calibrated_inputs_and_rejects_different_window():
    current = report()
    current.observations = {"targets": {"local": {"context_calibration": {
        "status": "sized_from_observed_usage", "fixture_context_limit": 24000,
        "adjustments": [{"fixture_context_limit": 32000}]}}}}
    observed = {"local": latency_summary(current)["targets"][0]["measurements"]}
    actual = calibrated_context_fixtures(observed, "local", config(), cohort="run")
    profile = observed["local"][0]["context_profile"]
    assert profile["fixture_context_limit"] == 24000
    expected = builtin_fixtures(config().model_copy(update={"context_limit": 24000}), check_id="context")["context"]
    assert [spec.input_chars for spec in actual] == [spec.input_chars for spec in expected]
    assert {input_bucket(spec.input_chars) for spec in actual} != {
        input_bucket(spec.input_chars) for spec in builtin_fixtures(config(), check_id="context")["context"]}
    assert calibrated_context_fixtures(observed, "local", config(context_limit=8192)) is None
    assert calibrated_context_fixtures(observed, "local", config(), cohort="another") is None
    observed["local"][0]["context_profile"]["fixture_version"] = FIXTURE_VERSION + ".changed"
    assert calibrated_context_fixtures(observed, "local", config()) is None


@pytest.mark.parametrize("profile", [None, "invalid", [], {"fixture_context_limit": True},
    {"fixture_context_limit": 127}, {"fixture_context_limit": 65537},
    {"fixture_context_limit": 10**100}, {"fixture_context_limit": "24000"}])
def test_context_profile_malformed_or_excessive_limits_stay_unknown(profile, monkeypatch):
    import giraffe.fixtures as fixtures

    def unexpected_build(*_args, **_kwargs):
        pytest.fail("malformed or excessive metadata must not build inputs")

    monkeypatch.setattr(fixtures, "builtin_fixtures", unexpected_build)
    if isinstance(profile, dict):
        profile = {"fixture_version": FIXTURE_VERSION, "context_limit": 4096, **profile}
    observed = {"local": [{"family": "context", "context_profile": profile}]}
    assert calibrated_context_fixtures(observed, "local", config()) is None


def test_large_valid_context_profile_does_not_allocate_inputs_during_preview(monkeypatch):
    import giraffe.fixtures as fixtures

    def unexpected_build(*_args, **_kwargs):
        pytest.fail("large context inputs must remain unknown in preview")

    monkeypatch.setattr(fixtures, "builtin_fixtures", unexpected_build)
    observed = {"local": [{"family": "context", "context_profile": {
        "fixture_version": FIXTURE_VERSION, "context_limit": 1000000, "fixture_context_limit": 16000000}}]}
    assert calibrated_context_fixtures(observed, "local", config(context_limit=1000000)) is None


def test_cancellation_normal_probes_remain_observable_but_cancelled_calls_do_not():
    current = report(family="cancellation_probe")
    cancelled = current.requests[0].model_copy(update={"scenario": "client_cancel", "status": "cancelled",
                                                     "intentional": True})
    current.requests.append(cancelled)
    rows = latency_summary(current)["targets"][0]["measurements"]
    assert len(rows) == 1
    assert rows[0]["family"] == "cancellation"
    assert rows[0]["n"] == 4


@pytest.mark.parametrize("error", [ValueError("sensitive"), TypeError("sensitive"), RuntimeError("sensitive")])
def test_summary_any_exception_preserves_report_json(tmp_path, monkeypatch, error):
    import giraffe.latency as latency

    def fail(_report):
        raise error

    monkeypatch.setattr(latency, "latency_summary", fail)
    current = report()
    output, _ = write_report(current, tmp_path)
    saved = json.loads(output.read_text())
    assert len(saved["requests"]) == 4
    assert saved["warnings"] == [f"Latency summary could not be saved: {type(error).__name__}"]
    assert "sensitive" not in output.read_text()
