"""Compare real pre-interference-mode reports with the same current workloads.

The fixtures were generated from BASE_COMMIT's unmodified ``src`` tree. To
regenerate with the current dev environment, extract that tree outside the repo
and run this module with the extracted ``src`` first on PYTHONPATH:

    git archive 49639265e12c3d282f65015fd7cdf7292c3b3943 src --output /tmp/base.tar
    mkdir -p /tmp/interference-base
    tar -xf /tmp/base.tar -C /tmp/interference-base
    PYTHONPATH=/tmp/interference-base/src:. .venv/bin/python \
        -m tests.test_interference_upgrade_baseline tests/fixtures /tmp/interference-base/src \
        multi-user migrated-load load-prefix multi-user-prefix

Only run_location is normalized. The full measured requests and the identities
computed by the historical runner remain intact; gzip uses a fixed mtime.
"""

from copy import deepcopy
import asyncio
import gzip
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from giraffe import concurrent_load, load_test, multi_user, multi_user_metrics, runner
from giraffe import session_sampling, traffic, workloads
from giraffe.deployment import fixture_hash
from giraffe.models import RunConfig, RunReport
from giraffe.reporting import _comparison_manifest, _same_load_workload, _settings_reasons, compare_baseline
from tests.virtual_clock import run_virtual


BASE_COMMIT = "49639265e12c3d282f65015fd7cdf7292c3b3943"
BASE_LOAD_SOURCE = "fa40b2a909d2b067529ab02bd686c61adb8a175063e481d802ad33d308ef49fa"
BASE_MULTI_USER_SOURCE = "914d6728904a6790358feb7631fb690173a637a5deb73cbdb19947ea8104120e"
FIXTURES = Path(__file__).parent / "fixtures"


class AnswerStream(httpx.AsyncByteStream):
    def __init__(self, input_tokens=128, cached_tokens=None):
        self.input_tokens, self.cached_tokens = input_tokens, cached_tokens

    async def __aiter__(self):
        for text in ("K7", "P", "4"):
            await asyncio.sleep(.004)
            yield ("data: " + json.dumps({"choices": [{"delta": {"content": text},
                                                       "finish_reason": None}]}) + "\n\n").encode()
        usage = {"prompt_tokens": self.input_tokens, "completion_tokens": 4}
        if self.cached_tokens is not None:
            usage["prompt_tokens_details"] = {"cached_tokens": self.cached_tokens}
        yield ("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}],
                                      "usage": usage})
               + "\n\ndata: [DONE]\n\n").encode()


def upgrade_config(kind):
    common = {"targets": [{"name": "local", "url": "http://unused.test/v1", "model": "test"}],
              "max_requests": None, "max_duration_seconds": None, "max_memory_mb": 0}
    if kind in {"load-prefix", "multi-user-prefix"}:
        config = upgrade_config("multi-user").model_dump(mode="json")
        config.update(checks=["multi_user" if kind == "multi-user-prefix" else "load", "prefix"],
                      prefix={"prefix_chars": 4096, "repeats": 4, "history_turns": 2})
        config["limits"]["min_samples"] = 2
        config["load_test"] = {
            "workloads": {"combinations": {"selected": ["short_short"]}},
            "patterns": {"concurrent": {"enabled": False}, "constant": {"enabled": True},
                         "rates": [40]}, "schedule": {"duration_seconds": 1},
            "pass_criteria": {"min_valid_responses": 2, "max_completion_ms": 1000}}
        return RunConfig.model_validate(config)
    if kind == "multi-user":
        return RunConfig.model_validate({**common, "config_version": 2, "checks": ["multi_user"],
            "multi_user": {"levels": [2], "duration_seconds": .4,
                           "think_time_seconds": {"values": [.01]}, "shapes": ["short_short"],
                           "pass_criteria": {"min_samples": 2}}})
    return RunConfig.model_validate({**common, "checks": ["capacity", "arrivals"], "stream": False,
        "traffic": {"rates": [12], "duration_seconds": 1,
                    "mix": {"short": 1, "long_input": 0, "long_output": 0}},
        "arrivals": {"mode": "steady", "requests": 12, "requests_per_second": 12},
        "limits": {"min_samples": 2, "first_output_ms": None, "latency_ms": 1000,
                   "stream_gap_ms": None, "min_output_tokens_per_second": None}})


async def generate_upgrade_report(monkeypatch, kind, *, prefix_budget=None):
    config = upgrade_config(kind)
    if prefix_budget is not None:
        values = config.model_dump(mode="json")
        values["test_options"]["prefix"] = {"max_output_tokens": prefix_budget}
        config = RunConfig.model_validate(values)

    async def handler(request):
        if request.method != "POST":
            return httpx.Response(404)
        payload = json.loads(request.content)
        text = payload["messages"][-1]["content"]
        prefix_trial = "\nTrial " in text
        cached = 1024 if prefix_trial and "Control " not in text and "Trial 0000" not in text else 0
        if payload["stream"]:
            stream = AnswerStream(1280 if prefix_trial else 128, cached if prefix_trial else None)
            return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})
        await asyncio.sleep(.004)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "K7P4"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 128, "completion_tokens": 4}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    report = await runner.run_suite(config)
    assert report.abort_reason is None and not report.limits_reached
    assert report.overall == "pass", [(check.id, check.status, check.summary) for check in report.checks]
    assert report.requests and all(record.valid for record in report.requests)
    report.manifest["run_location"] = {"hostname": "fixture", "system": "fixture",
                                       "machine": "fixture", "python": "3.12.1"}
    return report


def baseline(kind):
    report = RunReport.model_validate_json(gzip.decompress(
        (FIXTURES / f"pre-interference-mode-{kind}.json.gz").read_bytes()))
    assert report.manifest["test_fixture_provenance"]["commit"] == BASE_COMMIT
    return report


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("kind", ["multi-user", "migrated-load"])
def test_pre_interference_mode_report_remains_comparable(monkeypatch, kind):
    current = run_virtual(monkeypatch, generate_upgrade_report(monkeypatch, kind))
    old = baseline(kind)
    # Hold the timing method constant to isolate workload compatibility.
    current.manifest["timing_measurement_version"] = old.manifest["timing_measurement_version"]
    before = current.model_dump_json(), old.model_dump_json()
    assert _settings_reasons(current, old) == []
    assert _settings_reasons(old, current) == []
    compared = compare_baseline(current, old).baseline["targets"][0]
    assert compared["reasons"] == []
    if kind == "multi-user":
        assert any(row["check"] == "multi_user" and row["status"] in {"pass", "fail"}
                   for row in compared["comparisons"])
    else:
        assert compared["load_test"]["status"] == "pass"
        assert len(compared["load_test"]["points"]) == 2
        assert all(point["status"] == "pass" for point in compared["load_test"]["points"])
    assert (current.model_dump_json(), old.model_dump_json()) == before


def test_migrated_default_mode_normalization_preserves_explicit_nested_modes():
    old = baseline("migrated-load")
    normalized = _comparison_manifest(old.manifest)
    for settings in [normalized["effective_checks"]["load"], *[
            item["settings"] for item in normalized["effective_checks"]["load"]["migrated_tests"]]]:
        assert settings["workloads"]["interference"]["mode"] == "long_input_during_short"
    changed = deepcopy(old.manifest)
    changed["effective_checks"]["load"]["migrated_tests"][0]["settings"]["workloads"]["interference"]["mode"] = "short_during_long"
    assert (_comparison_manifest(changed)["effective_checks"]["load"]["migrated_tests"][0]
            ["settings"]["workloads"]["interference"]["mode"]) == "short_during_long"


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("kind", ["multi-user", "migrated-load"])
@pytest.mark.parametrize("change", ["settings", "source", "generator"])
def test_pre_mode_upgrade_does_not_hide_changed_work(monkeypatch, kind, change):
    current = run_virtual(monkeypatch, generate_upgrade_report(monkeypatch, kind))
    old = baseline(kind)
    provenance = old.manifest["test_fixture_provenance"]
    field = "multi_user" if kind == "multi-user" else "load_test"
    source_field = "multi_user_source_sha256" if kind == "multi-user" else "load_test_source_sha256"
    source = provenance["multi_user_source_sha256" if kind == "multi-user" else "load_source_sha256"]
    identity = {"generator_source_sha256": provenance["generator_source_sha256"], source_field: source}
    if kind == "multi-user":
        identity["multi_user_window_policy"] = "completed-output-v1"
    if change == "settings":
        if kind == "multi-user":
            old.manifest["config"][field]["levels"] = [3]
        else:
            old.manifest["config"][field]["migrated_tests"][0]["settings"]["patterns"]["rates"] = [13]
    elif change == "source":
        identity[source_field] = "unknown"
    else:
        identity["generator_source_sha256"] = "unknown"
    identity["settings"] = {field: old.manifest["config"][field]}
    old.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash(identity)
    assert "Fixture pack identity differs." in _settings_reasons(current, old)
    assert "Fixture pack identity differs." in _settings_reasons(old, current)


def test_historical_source_cannot_attest_reverse_mode_in_migrated_settings():
    old = baseline("migrated-load")
    current = old.model_copy(deep=True)
    for report in (current, old):
        settings = report.manifest["config"]["load_test"]
        settings["migrated_tests"][0]["settings"]["workloads"]["interference"]["mode"] = "short_during_long"
        report.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash({
            "generator_source_sha256": report.manifest["test_fixture_provenance"]["generator_source_sha256"],
            "load_test_source_sha256": BASE_LOAD_SOURCE, "settings": {"load_test": settings}})
    assert not _same_load_workload(current.manifest, old.manifest)


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("kind", ["load-prefix", "multi-user-prefix"])
def test_prefix_companion_report_remains_comparable_across_load_source_upgrade(monkeypatch, kind):
    current = run_virtual(monkeypatch, generate_upgrade_report(monkeypatch, kind))
    old = baseline(kind)
    current.manifest["timing_measurement_version"] = old.manifest["timing_measurement_version"]
    before = current.model_dump_json(), old.model_dump_json()
    assert _settings_reasons(current, current) == _settings_reasons(old, old) == []
    assert _settings_reasons(current, old) == []
    assert _settings_reasons(old, current) == []
    compared = compare_baseline(current, old).baseline["targets"][0]
    assert compared["reasons"] == []
    assert compared["status"] == "pass"
    assert (current.model_dump_json(), old.model_dump_json()) == before


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("kind", ["load-prefix", "multi-user-prefix"])
@pytest.mark.parametrize("change", ["prefix-settings", "source", "unverified-source", "generator",
                                    "older-prefix-budget-generator"])
def test_prefix_source_upgrade_keeps_changed_work_incomparable(monkeypatch, kind, change):
    current = run_virtual(monkeypatch, generate_upgrade_report(monkeypatch, kind))
    old = baseline(kind)
    checks = old.manifest["config"]["checks"]
    resolved = RunConfig.model_validate(old.manifest["config"])
    identity = {"generator_source_sha256": old.manifest["test_fixture_provenance"]["generator_source_sha256"],
                "settings": {field: getattr(resolved, field).model_dump(mode="json")
                             for field in ("prefix", "load_test", "multi_user")
                             if ("load" if field == "load_test" else field) in checks}}
    source_field = "load_test_source_sha256" if kind == "load-prefix" else "multi_user_source_sha256"
    identity[source_field] = BASE_LOAD_SOURCE if kind == "load-prefix" else BASE_MULTI_USER_SOURCE
    if kind == "multi-user-prefix":
        identity["multi_user_window_policy"] = "completed-output-v1"
    else:
        identity["settings"]["load_test"]["workloads"]["interference"].pop("mode")
    assert fixture_hash(identity) == old.manifest["fixture_pack"]["workload_fixture_sha256"]
    if change == "prefix-settings":
        old.manifest["config"]["prefix"]["repeats"] += 1
        identity["settings"]["prefix"]["repeats"] += 1
    elif change == "source":
        identity[source_field] = "unknown"
    elif change == "unverified-source":
        identity[source_field] = ("9b694c97a39d449d8ea15b09a9aeb406bf835179c0882827685c4d7888687d36"
                                  if kind == "load-prefix" else
                                  "bcef9038f2fc90862a69fde8d5529234b828757bb62f89468964ec5ee95c3d2e")
    else:
        identity["generator_source_sha256"] = (
            "2c5cfa60063bd236302582d25e7fe37e7807b829f487168cd09407d37bd60e6e"
            if change == "older-prefix-budget-generator" else "unknown")
    old.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash(identity)
    assert "Fixture pack identity differs." in _settings_reasons(current, old)
    assert "Fixture pack identity differs." in _settings_reasons(old, current)


@pytest.mark.usefixtures("local_tokenizer")
@pytest.mark.parametrize("kind", ["load-prefix", "multi-user-prefix"])
def test_prefix_source_upgrade_keeps_changed_actual_request_budgets_incomparable(monkeypatch, kind):
    current = run_virtual(monkeypatch, generate_upgrade_report(monkeypatch, kind, prefix_budget=256))
    old = baseline(kind)
    current.manifest["timing_measurement_version"] = old.manifest["timing_measurement_version"]
    assert {row.requested_max_tokens for row in current.requests if "prefix" in row.check_ids} == {256}
    assert {row.requested_max_tokens for row in old.requests if "prefix" in row.check_ids} == {128}
    assert _settings_reasons(current, old)
    assert _settings_reasons(old, current)


if __name__ == "__main__":
    import sys
    import tempfile

    from tests.conftest import local_tokenizer

    modules = (multi_user, multi_user_metrics, load_test, concurrent_load, session_sampling, traffic)
    source_root = Path(sys.argv[2]).resolve()
    for name, module in tuple(sys.modules.items()):
        if name == "giraffe" or name.startswith("giraffe."):
            assert Path(module.__file__).resolve().is_relative_to(source_root), module.__file__
    def source(modules):
        return hashlib.sha256(b"".join(Path(module.__file__).read_bytes() for module in modules)).hexdigest()
    assert source((load_test, concurrent_load, traffic)) == BASE_LOAD_SOURCE
    assert source((multi_user, multi_user_metrics, load_test, concurrent_load,
                   session_sampling, traffic)) == BASE_MULTI_USER_SOURCE
    assert workloads.FIXTURE_SOURCE_SHA256 == "a04c62a2e7b10ab0aef201cf5c283fe53df694d247ca5ce35caca1340a83f387"
    destination = Path(sys.argv[1])
    for kind in sys.argv[3:] or ("multi-user", "migrated-load"):
        with tempfile.TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as patch:
            local_tokenizer.__wrapped__(patch, Path(temporary))
            report = run_virtual(patch, generate_upgrade_report(patch, kind))
        report.manifest["test_fixture_provenance"] = {
            "commit": BASE_COMMIT, "transport": "httpx.MockTransport",
            "tokenizer": "tests.conftest.local_tokenizer", "load_source_sha256": BASE_LOAD_SOURCE,
            "multi_user_source_sha256": BASE_MULTI_USER_SOURCE,
            "generator_source_sha256": workloads.FIXTURE_SOURCE_SHA256}
        encoded = (json.dumps(report.model_dump(mode="json", exclude_defaults=True), indent=2) + "\n").encode()
        path = destination / f"pre-interference-mode-{kind}.json.gz"
        path.write_bytes(gzip.compress(encoded, mtime=0))
        print(kind, report.overall, len(report.requests), fixture_hash(report.manifest["fixture_pack"]))
