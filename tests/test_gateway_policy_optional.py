"""Selecting optional policies without configuring them must not invent traffic."""
import json

import pytest

from giraffe.models import RunConfig
from giraffe.planning import request_plan
from giraffe.runner import run_suite


def configuration():
    return RunConfig(targets=[{"name": "proxy", "url": "http://127.0.0.1:1", "model": "test"}],
                     checks=["gateway_policy"], max_memory_mb=0)


async def test_unconfigured_policy_is_unavailable_without_traffic(tmp_path, capsys):
    from giraffe.cli import _print_result
    from giraffe.reporting import write_report, reproduction_export, compare_baseline

    config = configuration()
    report = await run_suite(config)
    check = next(check for check in report.checks if check.id == "gateway_policy")
    assert check.status == "inconclusive"
    assert check.metrics["gateway_policy"]["qualification"] == "unavailable"
    assert not report.requests
    row = next(row for row in request_plan(config)["rows"] if row["id"] == "gateway_policy")
    assert row["requests_per_target"] == 0 and "unavailable" in json.dumps(row)
    json_path, html_path = write_report(report, tmp_path, html=True)
    assert "unavailable" in html_path.read_text()
    assert reproduction_export(report)["gateway_policy"]["proxy"]["qualification"] == "unavailable"
    _print_result(report, json_path, html_path, tmp_path, {})
    assert "unavailable" in capsys.readouterr().out
    assert compare_baseline(report, report.model_copy(deep=True)).baseline["status"] == "inconclusive"


def test_configured_policy_still_requires_all_identity_references():
    from tests.test_gateway_policy import configuration as configured_policy

    raw = configured_policy().model_dump(mode="json")
    raw["targets"][0]["test_identities"].pop("peer")
    with pytest.raises(ValueError, match="Every policy identity"):
        RunConfig.model_validate(raw)


def test_selecting_optional_policy_does_not_save_example_identity_defaults():
    from tests.test_ux_catalogue import browser_logic

    browser_logic('''
const values=new FormData();values.set('gateway_policy_rendered','1');
const result=configureGatewayPolicy(config,values);
assert.equal(result.gateway_policy,undefined);
const check=bootstrap.checks.find(c=>c.id==='gateway_policy');
assert.match(effectiveCheckSummary(check,config),/Not configured/);
''', configuration())
