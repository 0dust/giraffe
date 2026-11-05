"""Attest fixture identities across the run-limit split, without ignoring changes."""

from copy import deepcopy
import hashlib
from pathlib import Path

import pytest

from giraffe import concurrent_load, workloads, logprobs, logprob_workload, runner, session_sampling
from giraffe.deployment import fixture_hash
from giraffe.models import RunConfig, Sessions
from giraffe.reporting import _run_limits_workload_identity, _settings_reasons, compare_baseline
from tests.test_runner import client as client
from tests.test_reporting import make_report


LEGACY_GENERATOR = "4aa7fb48fa4be4a51b3341f34d86755dcfcca5c1043d1d362205d006115eccb0"
LEGACY_LOAD = "33b66f3784ad9613db082fa1f00a2f3292fc910b9f842bd3fe018340a0fe7aa7"
PRE_MULTI_USER_GENERATOR = "ab5875f7c635f8f454429a88aac87d52ce3a141a84bcf7b936fa737ffa41fe83"


@pytest.mark.asyncio
@pytest.mark.parametrize("load", [False, True], ids=["quick-check", "load"])
async def test_browser_safety_limits_preserve_real_baseline_comparability(load):
    from tests.test_concurrent_load import configuration

    config = (configuration(max_requests=None) if load else RunConfig(
        targets=[{"name": "local", "url": "http://localhost:8000", "model": "test"}]))
    assert config.max_requests is config.max_duration_seconds is config.concurrency is None
    previous = await runner.run_suite(config)
    limited = config.model_copy(update={
        "max_requests": 50_000, "max_duration_seconds": 7200, "concurrency": 128})
    current = await runner.run_suite(limited)

    compared = compare_baseline(current, previous)

    assert not previous.limits_reached and not current.limits_reached
    assert current.manifest["effective_checks"] == previous.manifest["effective_checks"]
    assert current.manifest["fixture_pack"] == previous.manifest["fixture_pack"]
    target = compared.baseline["targets"][0]
    assert target["reasons"] == []
    assert target["comparisons"]


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
    identity = _run_limits_workload_identity({"config": configured.model_dump(mode="json")}, revision="gateway_conversations")
    actual = {
        "generator_source_sha256": hashlib.sha256(Path(workloads.__file__).read_bytes()).hexdigest(),
    }
    assert identity is not None, IDENTITY_TABLE_GUIDANCE
    for field, digest in actual.items():
        assert identity[field] == digest, (
            f"{field} changed: review request-level compatibility and update/extend "
            "_run_limits_workload_identity in src/giraffe/reporting.py; do not bridge changed workloads.")


@pytest.mark.parametrize("check", ["sessions", "consistency", "prefix"])
def test_gateway_dispatch_preserves_core_predecessor_specialist_baselines(check):
    current, old, _, _ = reports(check)
    old.manifest["config"] = deepcopy(current.manifest["config"])
    predecessor = _run_limits_workload_identity(old.manifest, revision="shared_conversations")
    assert predecessor["generator_source_sha256"] == "d9a9e921584262148e06b636f0e8f195b7e530fd2a69c5e98f013359cf947ede"
    old.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash(predecessor)
    before = old.model_dump_json()
    assert _settings_reasons(current, old) == []
    assert _settings_reasons(old, current) == []
    assert old.model_dump_json() == before


def test_presampling_generator_identity_retains_the_original_session_settings():
    current, old, _, _ = reports()
    prior = deepcopy(current.manifest)
    prior["config"]["sessions"].pop("sampling")
    identity = _run_limits_workload_identity(prior, revision="issue45")
    assert "sampling" not in identity["settings"]["sessions"]
    assert identity["generator_source_sha256"] == "9c47aa627a061bd90f0e181ce26b7faa912be790ae75547348c72445c4144b35"
    prior["fixture_pack"]["workload_fixture_sha256"] = fixture_hash(identity)
    old.manifest = prior
    # Use an actual legacy baseline so the bridge is eligible.
    old.manifest["config"].pop("config_version")
    legacy = _run_limits_workload_identity(old.manifest, revision="legacy")
    old.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash(legacy)
    assert _settings_reasons(current, old) == []


@pytest.mark.parametrize("revision", ["legacy", "issue45", "issue95", "issue95_pauses", "issue95_pause_evidence"])
def test_sampled_sessions_are_never_attested_as_unchanged_historical_traffic(revision):
    current, _, _, _ = reports()
    current.manifest["config"]["sessions"].update(
        profile="performance", modes=["live"],
        workloads=[{"name": "chat", "turns": ["First", "Second"]}],
        sampling={"seed": 42, "think_time_seconds": {"values": [0, 1]}})
    assert _run_limits_workload_identity(current.manifest, revision=revision) is None


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


def prefix_reports():
    current, old, settings, _ = reports("prefix")
    old.manifest["config"] = deepcopy(current.manifest["config"])
    old.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash({
        "generator_source_sha256": PRE_MULTI_USER_GENERATOR, "settings": settings,
    })
    return current, old, settings


def test_unchanged_versioned_prefix_baseline_survives_multi_user_wiring():
    current, old, _ = prefix_reports()
    before = old.model_dump_json()
    assert current.manifest["fixture_pack"] != old.manifest["fixture_pack"]
    assert _settings_reasons(current, old) == []
    assert _settings_reasons(old, current) == []
    assert old.model_dump_json() == before


@pytest.mark.parametrize("which", ["current", "baseline"])
def test_prefix_bridge_rejects_unknown_generator_revisions(which):
    current, old, settings = prefix_reports()
    report = current if which == "current" else old
    report.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash({
        "generator_source_sha256": "unknown-prefix-revision", "settings": settings,
    })
    assert "Fixture pack identity differs." in _settings_reasons(current, old)


@pytest.mark.parametrize("change", ["changed", "missing"])
def test_prefix_bridge_requires_matching_saved_settings(change):
    current, old, settings = prefix_reports()
    if change == "changed":
        current.manifest["config"]["prefix"]["repeats"] += 1
        settings["prefix"]["repeats"] += 1
        current.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash({
            "generator_source_sha256": workloads.FIXTURE_SOURCE_SHA256, "settings": settings,
        })
    else:
        old.manifest["config"].pop("prefix")
    assert "Fixture pack identity differs." in _settings_reasons(current, old)


@pytest.mark.parametrize("revision", ["issue45", "issue95", "issue95_pauses"])
def test_prefix_bridge_does_not_attest_earlier_unreviewed_prefix_revisions(revision):
    current, _, _ = prefix_reports()
    assert _run_limits_workload_identity(current.manifest, revision=revision) is None


@pytest.mark.parametrize("check", ["sessions", "consistency", "prefix"])
def test_shared_executor_preserves_exact_persistent_user_revision_for_legacy_checks(check):
    current, old, _, _ = reports(check)
    old.manifest = deepcopy(current.manifest)
    identity = _run_limits_workload_identity(old.manifest, revision="persistent_users")
    assert identity["generator_source_sha256"] == "aee9ca71bc5ffd780d1574422b761e3177471a12f951a10b04322573ca062f76"
    old.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash(identity)
    assert _settings_reasons(current, old) == []
    old.manifest["fixture_pack"]["workload_fixture_sha256"] = "unreviewed-source"
    assert "Fixture pack identity differs." in _settings_reasons(current, old)


@pytest.mark.parametrize("revision", ["persistent_users", "shared_conversations", "gateway_attribution", "gateway_conversations"])
def test_conversation_wiring_does_not_bridge_multi_user_workloads(revision):
    configured = RunConfig(targets=[{"name": "local", "url": "http://localhost", "model": "test"}],
                           checks=["multi_user"])
    assert _run_limits_workload_identity({"config": configured.model_dump(mode="json")}, revision=revision) is None


def sampled_reports(profile):
    current, old, _, _ = reports()
    configured = Sessions(profile=profile, **({'modes':['live'], 'workloads':[{'name':'chat', 'turns':['First','Second','Third']}]} if profile=='performance' else {}), sampling={'seed':73,'think_time_seconds':{'values':[0,.1], 'weights':[1,2]}, **({'turn_count':{'values':[2,3]}, 'output_tokens':{'values':[16,32]}} if profile=='performance' else {})})
    current.manifest['config']['sessions'] = configured.model_dump(mode='json')
    old.manifest = deepcopy(current.manifest)
    for report, generator, sampler in (
        (current, workloads.FIXTURE_SOURCE_SHA256, hashlib.sha256(Path(session_sampling.__file__).read_bytes()).hexdigest()),
        (old, 'aee9ca71bc5ffd780d1574422b761e3177471a12f951a10b04322573ca062f76', '2fab1370faeab7ddb91a7ae9c0e49520ee9a67e305441a0663877bb12bca24c3')):
        identity = {'generator_source_sha256':generator, 'session_sampling_source_sha256':sampler, 'settings':{'sessions':configured.model_dump(mode='json')}}
        report.manifest['fixture_pack']['workload_fixture_sha256'] = fixture_hash(identity)
    return current, old

@pytest.mark.parametrize('profile',['correctness','performance'])
def test_exact_predecessor_sampled_sessions_remain_comparable(profile):
    current, old = sampled_reports(profile)
    assert _settings_reasons(current, old) == []
    assert _settings_reasons(old, current) == []


@pytest.mark.parametrize("which", ["current", "baseline"])
def test_sampled_session_bridge_rejects_unknown_sampling_sources(which):
    current, old = sampled_reports("performance")
    report = current if which == "current" else old
    identity = _run_limits_workload_identity(report.manifest,
        revision="gateway_conversations" if which == "current" else "persistent_users")
    identity["session_sampling_source_sha256"] = "unreviewed-sampler"
    report.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash(identity)
    assert "Fixture pack identity differs." in _settings_reasons(current, old)


def test_sampled_session_bridge_rejects_changed_sampling_settings():
    current, old = sampled_reports("performance")
    current.manifest["config"]["sessions"]["sampling"]["seed"] += 1
    identity = _run_limits_workload_identity(current.manifest, revision="gateway_conversations")
    current.manifest["fixture_pack"]["workload_fixture_sha256"] = fixture_hash(identity)
    assert "Fixture pack identity differs." in _settings_reasons(current, old)
