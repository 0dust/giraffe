"""Output allowances that consume the declared window are visible before sending."""

import pytest

from giraffe.models import RunConfig
from giraffe.planning import request_plan


TARGET = {"name": "service", "url": "http://endpoint.invalid/v1", "model": "model"}


def plan(**updates):
    return request_plan(RunConfig.model_validate({"targets": [TARGET], **updates}))


def space_warnings(result):
    return [warning for warning in result["warnings"] if "no room for the prompt" in warning]


def test_prefix_warning_names_output_reservation_and_available_remedies():
    result = plan(checks=["prefix"], max_output_tokens=2048, context_limit=4096)
    prefix = next(row for row in result["rows"] if row["id"] == "prefix")
    assert prefix["requests_per_target"] == 0
    warning = next(message for message in result["warnings"] if "Prefix coverage" in message)
    assert "max_output_tokens=2048" in warning
    assert "context_limit=4096" in warning
    assert "prefix.prefix_chars" in warning
    assert "Reduce" in warning


@pytest.mark.parametrize("budget", [4096, 8192])
def test_ordinary_budget_at_or_above_window_is_advisory(budget):
    result = plan(checks=["correctness"], max_output_tokens=budget, context_limit=4096)
    warnings = space_warnings(result)
    assert len(warnings) == 1
    assert f"max_output_tokens={budget}" in warnings[0]
    assert "context_limit=4096" in warnings[0]
    assert any(row["id"] == "correctness" and row["requests_per_target"] for row in result["rows"])


def test_warning_uses_context_override_and_effective_output_owner():
    result = plan(checks=["context"], max_output_tokens=32, context_limit=4096,
                  test_options={"context": {"context_limit": 128, "max_output_tokens": 128}})
    warning, = space_warnings(result)
    assert "test_options.context.max_output_tokens=128" in warning
    assert "test_options.context.context_limit=128" in warning


def test_shared_initial_warning_uses_selected_first_output_owner():
    result = plan(checks=["access", "serving", "first_output"], max_output_tokens=32,
                  test_options={"first_output": {"max_output_tokens": 4096}})
    warning, = space_warnings(result)
    assert "test_options.first_output.max_output_tokens=4096" in warning


def test_generation_initial_request_retains_its_independent_global_budget():
    result = plan(checks=["generation"], max_output_tokens=4096,
                  test_options={"generation": {"max_output_tokens": 32}})
    warning, = space_warnings(result)
    assert "Initial connection" in warning
    assert "max_output_tokens=4096" in warning


@pytest.mark.parametrize("updates", [
    {"checks": ["correctness"], "test_options": {"correctness": {"max_output_tokens": 32}}},
    {"checks": ["access", "serving", "first_output"], "test_options": {"serving": {"max_output_tokens": 32}}},
    {"checks": ["sessions"]},
    {"checks": ["load"]},
    {"checks": ["gpu"]},
])
def test_unused_global_budget_does_not_trigger_warning(updates):
    assert not space_warnings(plan(max_output_tokens=4096, **updates))


def test_dormant_override_cannot_hide_access_warning():
    result = plan(checks=["access"], max_output_tokens=4096,
                  test_options={"serving": {"max_output_tokens": 32}})
    warning, = space_warnings(result)
    assert "max_output_tokens=4096" in warning


def test_request_pack_uses_its_explicit_budget_instead_of_global_allowance():
    result = plan(checks=["correctness"], max_output_tokens=4096, request_source="request_pack",
                  request_pack={"version": 1, "cases": [{"fixture_id": "one",
                      "messages": [{"role": "user", "content": "Return A"}],
                      "scorer": "exact", "expected": "A", "max_tokens": 32}]})
    assert not space_warnings(result)


def test_nonconsuming_selected_check_does_not_hide_ordinary_check_warning():
    result = plan(checks=["load", "correctness"], max_output_tokens=4096)
    warning, = space_warnings(result)
    assert "max_output_tokens=4096" in warning


@pytest.mark.parametrize("check", ["load", "gpu"])
@pytest.mark.parametrize("budget", [32, 4096])
def test_restart_readiness_consumes_initial_budget_without_ordinary_checks(check, budget):
    result = plan(checks=[check], max_output_tokens=budget, restart_target="service",
                  test_options={"serving": {"max_output_tokens": 32}})
    warnings = space_warnings(result)
    assert len(warnings) == int(budget >= 4096)
    if warnings:
        assert "Restart readiness" in warnings[0]
        assert "max_output_tokens=4096" in warnings[0]
    assert not any(row["id"] == "initial" for row in result["rows"])
    assert any(row["id"] == "restart_readiness" for row in result["rows"])


@pytest.mark.parametrize("budget", [32, 4096])
def test_restart_and_selected_response_share_effective_owner(budget):
    result = plan(checks=["load", "first_output"], max_output_tokens=4096, restart_target="service",
                  test_options={"first_output": {"max_output_tokens": budget}})
    warnings = space_warnings(result)
    assert len(warnings) == int(budget >= 4096)
    if warnings:
        assert "test_options.first_output.max_output_tokens=4096" in warnings[0]
