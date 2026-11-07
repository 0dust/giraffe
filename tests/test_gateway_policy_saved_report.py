"""Untrusted saved policy counters remain text in HTML and browser reports."""
import json

import pytest

from giraffe.models import CheckResult, RunReport
from giraffe.reporting import write_report
from tests.test_reporting import make_report
from tests.test_ux_catalogue import browser_logic


@pytest.mark.parametrize("counter", ["declared_rpm", "admitted", "rejected", "completed", "planned"])
def test_loaded_policy_counter_cannot_create_active_html(tmp_path, counter):
    payload = '<img src=x onerror="alert(1)">'
    user = dict(identity="limited", priority="low", declared_rpm=2, admitted=2, rejected=3,
                completed=2, planned=5, input_tokens=[8], output_tokens=[2], completion_tokens=[2])
    user[counter] = payload
    policy = dict(qualification="unverified", interpretation="fixture", users=[user],
                  peer_continuation=False, overlap_observed=False, comparable_actual_work=False)
    report = make_report()
    report.checks = [CheckResult(id="gateway_policy", title="Gateway quotas & priorities", target="local",
                                status="inconclusive", summary="fixture", metrics={"gateway_policy": policy})]
    loaded = RunReport.model_validate_json(report.model_dump_json())
    _, path = write_report(loaded, tmp_path, html=True)
    assert payload not in path.read_text()
    browser_logic("const markup=renderGatewayPolicyResults("+json.dumps(policy)+");assert.doesNotMatch(markup,/<img src=x/);")
