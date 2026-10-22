"""Malformed configuration input is rejected before compatibility migration."""

import io
import json
import os
from collections import deque
from copy import deepcopy

import httpx
import pytest
from pydantic import ValidationError

from giraffe.cli import main, migrated_configuration
from giraffe.configuration_errors import configuration_errors
from giraffe.models import (
    DEFAULT_CHECKS, ConfigDraft, RunConfig, migrate_concurrent_load,
    migrate_load_test, migrate_run_limits,
)
from giraffe.web import create_app


TARGET = {"name": "local", "url": "http://127.0.0.1:1/v1", "model": "test"}
INVALID_CHECKS = [None, False, 0, 1.5, "load", {}, {"load": True}, [{}], [[]], [None], [1], [True]]
INVALID_VERSIONS = [-1, 0, 1, 3, 999999, None, True, False, 2.0, 2.5, "2", [], {}]
VERSION_MESSAGE = (
    "Unsupported configuration version. Use config_version: 2; "
    "older unversioned files can omit config_version."
)


@pytest.mark.parametrize("updates,error_location", [
    ({"checks": ["access"]}, None),
    ({"config_version": 2, "checks": ["access"]}, None),
    ({"config_version": 2, "checks": [{}]}, ("checks", 0)),
    ({"config_version": 3, "checks": ["access"]}, ("config_version",)),
], ids=["unversioned", "versioned", "invalid-checks", "unsupported-version"])
@pytest.mark.parametrize("parse", [RunConfig.model_validate, ConfigDraft.model_validate, migrated_configuration,
                                  migrate_run_limits, migrate_concurrent_load, migrate_load_test])
def test_configuration_parsing_does_not_read_process_environment(monkeypatch, parse, updates, error_location):
    def forbidden(*args, **kwargs):
        pytest.fail("Configuration parsing read the process environment")

    monkeypatch.setattr(os, "getenv", forbidden)
    values = {"targets": [TARGET], **updates}
    if error_location is not None:
        with pytest.raises(ValidationError) as caught:
            parse(values)
        error, = caught.value.errors()
        assert error["loc"] == error_location
    else:
        result = parse(values)
        config = result[0] if isinstance(result, tuple) else result
        assert (config["checks"] if isinstance(config, dict) else config.checks) == ["access"]


def test_null_checks_is_a_field_validation_error_before_migration():
    with pytest.raises(ValidationError) as caught:
        migrated_configuration({"targets": [TARGET], "checks": None})

    error, = caught.value.errors()
    assert error["loc"] == ("checks",)
    assert error["type"] == "list_type"


def test_future_version_is_rejected_before_legacy_load_migration():
    with pytest.raises(ValidationError) as caught:
        migrated_configuration({"config_version": 3, "targets": [TARGET], "checks": ["concurrency"]})

    error, = caught.value.errors()
    assert error["loc"] == ("config_version",)


def test_version_diagnostic_explains_supported_import_contract():
    values = {"config_version": 3, "targets": [TARGET]}
    with pytest.raises(ValidationError) as caught:
        migrated_configuration(values)

    error, = configuration_errors(caught.value.errors(), values)
    assert error["field"] == "config_version"
    assert error["msg"] == VERSION_MESSAGE


@pytest.mark.parametrize("parse", [RunConfig.model_validate, migrated_configuration])
def test_migration_errors_preserve_hidden_input_diagnostics(parse):
    values = {"targets": [TARGET], "checks": [{"api_key": "private-import-value"}]}
    with pytest.raises(ValidationError) as caught:
        parse(values)

    assert "private-import-value" not in str(caught.value)
    assert "private-import-value" not in json.dumps(configuration_errors(caught.value.errors(), values))


@pytest.mark.parametrize("checks", INVALID_CHECKS)
@pytest.mark.parametrize("parse", [RunConfig.model_validate, ConfigDraft.model_validate, migrated_configuration,
                                  migrate_run_limits, migrate_concurrent_load, migrate_load_test])
def test_malformed_checks_keep_field_locations_at_every_migration_boundary(checks, parse):
    values = {"targets": [TARGET], "checks": checks}
    original = deepcopy(values)
    with pytest.raises(ValidationError) as caught:
        parse(values)

    assert values == original
    error, = caught.value.errors()
    assert error["loc"] == (("checks", 0) if isinstance(checks, list) else ("checks",))
    assert error["type"] == ("string_type" if isinstance(checks, list) else "list_type")


@pytest.mark.parametrize("version", INVALID_VERSIONS)
@pytest.mark.parametrize("parse", [RunConfig.model_validate, ConfigDraft.model_validate, migrated_configuration,
                                  migrate_run_limits, migrate_concurrent_load, migrate_load_test])
def test_only_explicit_integer_version_two_is_supported(version, parse):
    values = {"config_version": version, "targets": [TARGET], "checks": ["concurrency"]}
    original = deepcopy(values)
    with pytest.raises(ValidationError) as caught:
        parse(values)

    assert values == original
    error, = caught.value.errors()
    assert error["loc"] == ("config_version",)
    assert error["type"] == ("literal_error" if type(version) is int else "int_type")
    diagnostic, = configuration_errors(caught.value.errors(), values)
    assert diagnostic["fields"] == ["config_version"]
    assert diagnostic["msg"] == VERSION_MESSAGE


@pytest.mark.parametrize("container", [list, tuple, set, frozenset, deque, iter])
@pytest.mark.parametrize("parse", [RunConfig.model_validate, ConfigDraft.model_validate, migrated_configuration])
def test_python_check_collection_inputs_keep_the_canonical_list_contract(container, parse):
    config = parse({"targets": [TARGET], "checks": container(["access"])})
    assert config.checks == ["access"]
    assert config.config_version == 2
    assert config.model_dump(mode="json")["checks"] == ["access"]


@pytest.mark.parametrize("check", ["concurrency", "capacity", "arrivals", "fairness", "buckets", "mixed", "token_stress"])
def test_supported_legacy_check_ids_still_migrate_and_export_v2(check):
    values = {"targets": [TARGET], "checks": [check]}
    if check in {"fairness", "mixed"}:
        values["concurrent_workloads"] = {"levels": [2]}
    config = migrated_configuration(values)
    assert config.checks == ["load"]
    assert config._migration_notes
    assert config.model_dump(mode="json")["config_version"] == 2
    restored = migrated_configuration(config.model_dump(mode="json"))
    assert restored.model_dump(mode="json") == config.model_dump(mode="json")
    assert restored._migration_notes == []


def test_missing_and_empty_checks_keep_distinct_draft_semantics():
    missing = migrated_configuration({"targets": [TARGET]}, ConfigDraft)
    empty = migrated_configuration({"targets": [TARGET], "checks": []}, ConfigDraft)
    assert missing.checks == list(DEFAULT_CHECKS)
    assert empty.checks == []
    for config in (missing, empty):
        assert config.config_version == 2
        assert migrated_configuration(config.model_dump(mode="json"), ConfigDraft).checks == config.checks


@pytest.fixture
def forbidden_inference(monkeypatch):
    calls = []

    async def reject(*args, **kwargs):
        calls.append(True)
        pytest.fail("Malformed configuration reached an inference client")

    monkeypatch.setattr("giraffe.client.LLMClient.__aenter__", reject)
    return calls


@pytest.mark.parametrize("endpoint", ["draft", "preview", "validate", "plan", "cli", "runs"])
@pytest.mark.parametrize("settings,field", [
    *[({"checks": value}, "checks.0" if isinstance(value, list) else "checks")
      for value in (None, False, 3, "load", {}, [{}], [[]], [None])],
    *[({"config_version": value}, "config_version") for value in INVALID_VERSIONS],
])
async def test_http_boundaries_return_actionable_422_before_inference(
        tmp_path, endpoint, settings, field, forbidden_inference):
    values = {"config_version": 2, "targets": [TARGET], "checks": ["access"], **settings}
    nested = endpoint in {"cli", "runs"}
    path = "/api/runs" if endpoint == "runs" else f"/api/config/{endpoint}"
    app = create_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as client:
            response = await client.post(path, json={"config": values} if nested else values)
            assert response.status_code == 422, response.text
            diagnostic, = response.json()["detail"]
            assert diagnostic["field"] == field
            assert diagnostic["fields"] == [field]
            assert diagnostic["loc"] == ["body", *(["config"] if nested else []),
                                         *[int(part) if part.isdigit() else part for part in field.split(".")]]
            assert diagnostic["actions"] == [{"label": "Edit " + field.replace("_", " "), "field": field}]
            if field == "config_version":
                assert diagnostic["msg"] == VERSION_MESSAGE
            bootstrap = (await client.get("/api/bootstrap")).json()
            assert bootstrap["active_run_id"] is None
    assert forbidden_inference == []
    assert not list(tmp_path.glob("*/.web.json"))


@pytest.mark.parametrize("command", ["validate", "preview", "run"])
@pytest.mark.parametrize("settings,field", [
    ({"checks": None}, "checks"), ({"checks": 3}, "checks"), ({"checks": [{}]}, "checks.0"),
    ({"config_version": -1}, "config_version"), ({"config_version": 3}, "config_version"),
    ({"config_version": True}, "config_version"), ({"config_version": 2.0}, "config_version"),
    ({"config_version": "2"}, "config_version"),
])
def test_cli_commands_report_consistent_usage_errors(
        command, settings, field, monkeypatch, capsys, forbidden_inference):
    values = {"config_version": 2, "targets": [TARGET], "checks": ["access"], **settings}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(values)))
    assert main([command, "--config", "-", "--output-format", "json"]) == 4
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["outcome"] == "error"
    assert result["exit_code"] == 4
    error, = result["errors"]
    assert error["field"] == field
    assert error["fields"] == [field]
    if field == "config_version":
        assert error["message"] == VERSION_MESSAGE
    assert "Traceback" not in output.err
    assert forbidden_inference == []
