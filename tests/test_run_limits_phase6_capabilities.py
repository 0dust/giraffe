"""UI bounds come from the configuration schema and retained runtime caps."""

from giraffe.capabilities import capabilities
from giraffe.models import CHECK_LIMIT_FIELDS, CHECK_OPTION_FIELDS, RunConfig


def test_limits_resolve_nullable_nested_and_list_schema_fields():
    limits = capabilities()["limits"]
    assert limits["concurrency"] == {"type": "integer", "minimum": 1, "maximum": 512}
    assert limits["max_duration_seconds"] == {"type": "number", "exclusiveMinimum": 0}
    assert limits["arrivals.requests"]["maximum"] == 10000
    assert limits["arrivals.burst_size"]["maximum"] == 1024
    assert limits["sessions.workloads"]["maxItems"] == 16
    assert limits["sessions.workloads[].turns"]["maxItems"] == 32
    assert limits["judging.concurrency"]["maximum"] == 512
    assert limits["concurrent_load.profile.long.output_tokens"]["maximum"] == 65536
    assert limits["concurrent_load.interference.max_slowdown"]["exclusiveMinimum"] == 1


def test_each_check_exposes_only_its_supported_request_and_pass_fields():
    limits = capabilities()["limits"]
    for check, fields in CHECK_OPTION_FIELDS.items():
        for field in fields:
            schema = limits[f"test_options.*.{field}"]
            assert limits[f"test_options.{check}.{field}"] == schema
        for field in CHECK_LIMIT_FIELDS[check]:
            assert limits[f"test_options.{check}.limits.{field}"] == limits[f"limits.{field}"]
    assert "test_options.arrivals.concurrency" not in limits
    assert "test_options.sessions.max_output_tokens" not in limits


def test_validator_and_payload_caps_have_discoverable_limits():
    limits = capabilities()["limits"]
    assert limits["traffic.rates"] == {
        "type": "array", "minItems": 1, "maxItems": 16,
        "items": {"type": "number", "exclusiveMinimum": 0, "maximum": 10000},
    }
    assert limits["traffic.planned_arrivals"]["maximum"] == 100000
    for field in ("concurrent_load.schedule.levels", "consistency.levels"):
        assert limits[field]["minItems"] == 1
        assert limits[field]["maxItems"] == 16
        assert limits[field]["items"] == limits["default_concurrency"]
    assert limits["concurrent_load.mixed.shares.long_short"]["maximum"] == 100
    assert limits["tool_checks.fixtures"]["maxItems"] == 16
    assert limits["tool_checks.bytes"]["maximum"] == 65536
    assert limits["request_pack.bytes"]["maximum"] == 1024 * 1024
    assert limits["request_pack.cases"]["maxItems"] == 1000
    assert "consistency.logprobs_json" not in limits
    assert limits["live.requests"]["maximum"] == 4096
    for field in ("sessions.turns", "sessions.workloads[].turns"):
        assert limits[field]["minItems"] == 2
        assert limits[field]["maxItems"] == 32
        assert limits[field]["items"] == {"type": "string", "minLength": 1, "maxLength": 8192}
        assert limits[f"{field}[]"] == limits[field]["items"]
    assert limits["sessions.workloads"]["maxItems"] == 16
    assert limits["judging.judges"]["maxItems"] == 8


def test_bounds_are_stable_when_request_settings_change():
    configured = RunConfig(targets=[{"name": "local", "url": "http://localhost/v1", "model": "test"}],
                           default_concurrency=7, max_output_tokens=512,
                           test_options={"correctness": {"samples": 2}})
    assert capabilities(configured)["limits"] == capabilities()["limits"]


def test_inherited_consistency_output_rule_is_not_a_hard_cap():
    discovery = capabilities()
    check = next(check for check in discovery["checks"] if check["id"] == "consistency")
    rule = check["inheritance"]["max_output_tokens"]
    assert rule == {"suite_field": "max_output_tokens", "maximum_default": 192}
    assert "maximum" not in discovery["limits"]["test_options.consistency.max_output_tokens"]
    config = RunConfig(targets=[{"name": "local", "url": "http://localhost/v1", "model": "test"}],
                       max_output_tokens=512,
                       test_options={"consistency": {"max_output_tokens": 1024}})
    assert config.effective_check("consistency")["max_output_tokens"] == 1024
    assert config.model_copy(update={"test_options": {}}).effective_check("consistency")["max_output_tokens"] == 192


def test_cleanup_bounds_are_shared_by_discovery_and_web_request_model():
    from giraffe.web import CleanupCriteria

    limits = capabilities()["limits"]
    for name in ("older_than_days", "keep_latest", "max_total_bytes"):
        node = CleanupCriteria.model_json_schema()["properties"][name]
        bound = node.get("minimum", next((part["minimum"] for part in node["anyOf"] if "minimum" in part), None))
        assert limits[f"cleanup.{name}"]["minimum"] == bound
    assert limits["cleanup.max_total_bytes"]["minimum"] == 1
