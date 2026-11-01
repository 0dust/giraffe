"""Fresh browser limits are launch conveniences, not configuration defaults."""

import pytest

from giraffe.models import RunConfig
from giraffe.planning import request_plan
from giraffe.web import _bootstrap_config
from tests.test_web import browser


LIMITS = {"max_requests": 50_000, "max_duration_seconds": 7200, "concurrency": 128}


def test_fresh_browser_defaults_do_not_change_run_config_defaults():
    fresh = _bootstrap_config(None)
    assert {name: fresh[name] for name in LIMITS} == LIMITS
    configuration = RunConfig(targets=[{"name": "local", "url": "http://localhost/v1", "model": "test"}])
    assert all(getattr(configuration, name) is None for name in LIMITS)


@pytest.mark.parametrize("defaults", [{}, {"config_version": 2},
                                      {"config_version": 2, **dict.fromkeys(LIMITS)},
                                      {"config_version": 2, "max_requests": 17,
                                       "max_duration_seconds": 45, "concurrency": 2}])
def test_launcher_config_preserves_its_own_limits_including_unset(defaults):
    actual = _bootstrap_config(defaults)
    assert {name: actual[name] for name in LIMITS} == {name: defaults.get(name) for name in LIMITS}


@pytest.mark.parametrize("checks", [None, ["load"]])
def test_generous_limits_preserve_planned_work_without_new_warnings(checks):
    values = _bootstrap_config(None)
    values["targets"][0]["model"] = "test"
    if checks is not None:
        values["checks"] = checks
    bounded = request_plan(RunConfig.model_validate(values))
    unbounded = request_plan(RunConfig.model_validate({**values, **dict.fromkeys(LIMITS)}))
    assert bounded["planned_total"] == unbounded["planned_total"]
    assert bounded["rows"] == unbounded["rows"]
    assert bounded["warnings"] == unbounded["warnings"]
    assert bounded["request_warnings"] == bounded["time_warnings"] == bounded["errors"] == []


async def test_bootstrap_supplied_config_keeps_unset_limits(tmp_path):
    async with browser(tmp_path, defaults={"config_version": 2}) as client:
        result = (await client.get("/api/bootstrap")).json()
    assert all(result["config"][name] is None for name in LIMITS)
