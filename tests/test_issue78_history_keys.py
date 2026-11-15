"""Saved evidence recorded under other output caps is not reused, so FIXTURE_VERSION stays 0.3.0."""

import copy

from giraffe.arrival_history import workload_keys
from giraffe.correctness_history import fixture_keys
from giraffe.fixtures import FIXTURE_VERSION, builtin_fixtures
from giraffe.latency import calibrated_context_fixtures
from giraffe.models import RunConfig


def config(**updates):
    return RunConfig.model_validate({
        "targets": [{"name": "service", "url": "http://endpoint.invalid/v1", "model": "model"}], **updates})


def test_fixture_version_is_unchanged_because_history_keys_include_the_output_cap():
    assert FIXTURE_VERSION == "0.3.0"


def test_correctness_history_keys_change_with_the_output_cap():
    # 64 was the previous cap of short.copy and json.record; 2048 is the recommended reasoning cap.
    old = fixture_keys(config(checks=["correctness", "json"], max_output_tokens=64))
    new = fixture_keys(config(checks=["correctness", "json"], max_output_tokens=2048))
    assert old and old.keys() == new.keys()
    assert all(old[fixture_id]["key"] != new[fixture_id]["key"] for fixture_id in old)


def test_arrival_history_keys_change_with_the_output_cap():
    load = {"patterns": {"concurrent": {"enabled": False}, "constant": {"enabled": True}, "rates": [1]}}
    old = workload_keys(config(checks=["load"], max_output_tokens=128, load_test=load))
    new = workload_keys(config(checks=["load"], max_output_tokens=2048, load_test=load))
    assert old and old.keys() == new.keys()
    assert all(old[point] != new[point] for point in old)


def test_context_timing_profile_with_another_output_cap_is_not_reused():
    configured = config(checks=["context"], context_limit=4096, max_output_tokens=2048,
                        test_options={"context": {"max_output_tokens": 128}})
    specs = builtin_fixtures(configured.model_copy(update={"max_output_tokens": 128}))["context"]
    profile = {"fixture_version": FIXTURE_VERSION, "context_limit": 4096, "fixture_context_limit": 4096,
               "sizing_version": "full-window-v1", "method": "local_tokenizer", "tokenizer": "model",
               "fixtures": [{"fixture_id": spec.fixture_id, "input_chars": spec.input_chars, "output_cap": 128}
                            for spec in specs]}
    observations = {"service": [{"family": "context", "context_profile": profile}]}
    recovered = calibrated_context_fixtures(observations, "service", configured)
    assert recovered is not None and {spec.max_tokens for spec in recovered} == {128}
    stale = copy.deepcopy(observations)
    stale["service"][0]["context_profile"]["fixtures"][0]["output_cap"] = 24  # recorded under the former cap
    assert calibrated_context_fixtures(stale, "service", configured) is None
