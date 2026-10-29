"""Attest fixture identities across the run-limit split, without ignoring changes."""

from copy import deepcopy
import hashlib
from pathlib import Path

import pytest

from giraffe import concurrent_load, workloads, logprobs, logprob_workload
from giraffe.deployment import fixture_hash
from giraffe.models import RunConfig
from giraffe.reporting import _run_limits_workload_identity, _settings_reasons
from tests.test_reporting import make_report


LEGACY_GENERATOR = "4aa7fb48fa4be4a51b3341f34d86755dcfcca5c1043d1d362205d006115eccb0"
LEGACY_LOAD = "33b66f3784ad9613db082fa1f00a2f3292fc910b9f842bd3fe018340a0fe7aa7"


IDENTITY_TABLE_GUIDANCE = (
    "Review request-level compatibility and update/extend _run_limits_workload_identity "
    "in src/giraffe/reporting.py after editing concurrent_load.py, workloads.py or logprobs.py. "
    "Do not bridge changed workloads.")

def reports(check="sessions", *, arrivals=None):
    config = RunConfig(targets=[{"name": "local", "url": "http://localhost:8000", "model": "test"}],
                       checks=[check if check not in {"arrivals", "concurrency"} else "access"], max_output_tokens=2048,
                       **({"arrivals": arrivals} if arrivals else {}))
    if check in {"arrivals", "concurrency"}:
        from giraffe.models import Arrival
        config = config.model_copy(update={"checks": [check], **({"arrivals": Arrival.model_validate(arrivals or {})} if check == "arrivals" else {})})
    current, old = make_report(), make_report("baseline")
    current.manifest["config"] = config.model_dump(mode="json")
    old.manifest["config"] = deepcopy(current.manifest["config"])
    old_config = old.manifest["config"]
    old_config.pop("config_version")
    old_config.pop("default_concurrency", None)
    old_config.update(concurrency=4, max_requests=300, max_duration_seconds=300)
    settings = {field: (getattr(config, field).model_dump(mode="json")
                        if hasattr(getattr(config, field), "model_dump") else getattr(config, field))
                for field, checks in workloads.WORKLOAD_CHECKS.items() if check in checks}
    if check in {'arrivals', 'concurrency'}:
        field = 'arrivals' if check == 'arrivals' else 'concurrent_load'
        settings[field] = getattr(config, field).model_dump(mode='json')
    old_settings = deepcopy(settings)
    if check == "arrivals":
        old_settings["arrivals"].pop("max_in_flight")
    # Legacy exports saved the resolved values, including inherited parallelism.
    old_config.update(old_settings)
    for report, values, generator, load_source in (
        (current, settings, workloads.FIXTURE_SOURCE_SHA256,
         hashlib.sha256(Path(concurrent_load.__file__).read_bytes()).hexdigest()),
        (old, old_settings, LEGACY_GENERATOR, LEGACY_LOAD),
    ):
        identity = {"generator_source_sha256": generator, "settings": values}
        if check == "concurrency":
            identity["concurrent_load_source_sha256"] = load_source
        report.manifest["fixture_pack"] = {
            "version": "0.1.0", "custom": None, "custom_sha256": None,
            "builtin_sha256": "same-built-in-fixtures",
            "workload_fixture_sha256": fixture_hash(identity),
        }
    return current, old, settings, old_settings


@pytest.mark.parametrize("check", ["sessions", "consistency"])
def test_a23_real_workload_fingerprints_match_legacy_baseline_after_limit_split(check):
    current, old, _, _ = reports(check)
    before = old.model_dump_json()
    assert (current.manifest["fixture_pack"]["workload_fixture_sha256"]
            != old.manifest["fixture_pack"]["workload_fixture_sha256"])
    assert _settings_reasons(current, old) == [], IDENTITY_TABLE_GUIDANCE
    assert old.model_dump_json() == before


@pytest.mark.parametrize("which", ["current", "baseline"])
def test_unknown_generator_source_is_not_normalized(which):
    current, old, settings, old_settings = reports()
    report, values = (current, settings) if which == "current" else (old, old_settings)
    report.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash({
        "generator_source_sha256": "unknown-generator-revision", "settings": values,
    })
    assert "Fixture pack identity differs." in _settings_reasons(current, old)


@pytest.mark.parametrize("which", ["current", "baseline"])
def test_unknown_concurrent_load_source_is_not_normalized(which):
    current, old, settings, old_settings = reports("concurrency")
    report, values, generator = ((current, settings, workloads.FIXTURE_SOURCE_SHA256)
                                 if which == "current" else (old, old_settings, LEGACY_GENERATOR))
    report.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash({
        "generator_source_sha256": generator, "settings": values,
        "concurrent_load_source_sha256": "unknown-load-revision",
    })
    assert "Fixture pack identity differs." in _settings_reasons(current, old)


@pytest.mark.parametrize("change", ["requests", "max_in_flight"])
def test_actual_arrival_workload_change_remains_noncomparable(change):
    current, old, settings, _ = reports("arrivals")
    current.manifest["config"]["arrivals"][change] = 16
    settings["arrivals"][change] = 16
    current.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash({
        "generator_source_sha256": workloads.FIXTURE_SOURCE_SHA256, "settings": settings,
    })
    reasons = _settings_reasons(current, old)
    assert "Run setting arrivals differs." in reasons
    assert "Fixture pack identity differs." in reasons


def test_changed_builtin_fixture_is_not_hidden_by_verified_optional_workload_identity():
    current, old, _, _ = reports()
    current.manifest["fixture_pack"]["builtin_sha256"] = "changed-fixture-prompts"
    assert "Fixture pack identity differs." in _settings_reasons(current, old)


def test_source_bridge_does_not_apply_to_versioned_reports():
    current, old, _, _ = reports()
    old.manifest["config"]["config_version"] = 2
    assert "Fixture pack identity differs." in _settings_reasons(current, old)


def test_source_bridge_requires_an_explicitly_attested_generator_revision(monkeypatch):
    monkeypatch.setattr(workloads, "FIXTURE_SOURCE_SHA256", "future-unreviewed-generator")
    current, old, _, _ = reports()
    assert "Fixture pack identity differs." in _settings_reasons(current, old)


def test_unchanged_text_generator_has_reviewed_identity_table_entry():
    configured = RunConfig(targets=[{"name": "local", "url": "http://localhost", "model": "test"}],
                           checks=["consistency"])
    identity = _run_limits_workload_identity({"config": configured.model_dump(mode="json")}, revision="issue45")
    actual = {
        "generator_source_sha256": hashlib.sha256(Path(workloads.__file__).read_bytes()).hexdigest(),
    }
    assert identity is not None, IDENTITY_TABLE_GUIDANCE
    for field, digest in actual.items():
        assert identity[field] == digest, (
            f"{field} changed: review request-level compatibility and update/extend "
            "_run_limits_workload_identity in src/giraffe/reporting.py; do not bridge changed workloads.")


@pytest.mark.parametrize("check", ["arrivals", "concurrency"])
def test_replaced_load_generator_is_not_bridged_to_historical_workload(check):
    _, old, _, _ = reports(check)
    current = make_report()
    config = RunConfig(targets=[{"name": "local", "url": "http://localhost", "model": "test"}], checks=["load"])
    current.manifest["config"] = config.model_dump(mode="json")
    current.manifest["fixture_pack"] = deepcopy(old.manifest["fixture_pack"])
    current.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash({"load_test": config.load_test.model_dump()})
    before = old.model_dump_json()
    assert "Fixture pack identity differs." in _settings_reasons(current, old)
    assert old.model_dump_json() == before


@pytest.mark.parametrize("response_mode", ["stream", "complete"])
def test_streaming_logprob_generator_is_not_bridged_to_historical_evidence(response_mode):
    current, old, _, _ = reports("consistency")
    configured = RunConfig(targets=[{"name": "local", "url": "http://localhost", "model": "test"}],
                           checks=["consistency"], consistency={"logprobs": {"response_mode": "complete"}})
    old.manifest["config"] = configured.model_dump(mode="json")
    historical = _run_limits_workload_identity(old.manifest, revision="issue45")
    assert historical is not None
    old.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash(historical)
    configured.consistency.logprobs.response_mode = response_mode
    current.manifest["config"] = configured.model_dump(mode="json")
    identity = deepcopy(historical)
    identity["settings"]["consistency"] = configured.consistency.model_dump(mode="json")
    identity["logprob_source_sha256"] = hashlib.sha256(
        Path(logprobs.__file__).read_bytes() + Path(logprob_workload.__file__).read_bytes()).hexdigest()
    assert identity["logprob_source_sha256"] != historical["logprob_source_sha256"]
    current.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash(identity)
    before = old.model_dump_json()
    assert "Fixture pack identity differs." in _settings_reasons(current, old)
    assert old.model_dump_json() == before
