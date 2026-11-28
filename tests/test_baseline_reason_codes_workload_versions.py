"""Workload-version reasons preserve main's identity checks and explanations."""

import pytest

from giraffe.models import CheckResult
from giraffe.reporting import compare_baseline
from tests.test_gateway_access import config
from tests.test_web import report


@pytest.fixture
def access_report(local_tokenizer):
    current = report(config())
    current.manifest["gateway_access_workload_version"] = 1
    current.manifest["fixture_pack"] = {"workload_fixture_sha256": "b" * 64}
    current.checks = [CheckResult(id="gateway_access", target="proxy", title="Access",
                                  status="pass", summary="Observed")]
    return current


@pytest.mark.parametrize("side", ["current", "baseline"])
@pytest.mark.parametrize("version", [2, None, True, 1.0, "1"])
def test_access_version_reason_details(access_report, side, version):
    current = access_report
    baseline = current.model_copy(deep=True)
    changed = current if side == "current" else baseline
    if version is None:
        del changed.manifest["gateway_access_workload_version"]
    else:
        changed.manifest["gateway_access_workload_version"] = version
    # Historical source hashes and fixture digests cannot attest a missing version.
    for run in (current, baseline):
        run.manifest["gateway_access_source_sha256"] = "a" * 64
        run.manifest["fixture_pack"]["workload_fixture_sha256"] = "b" * 64
    before = current.model_dump_json(), baseline.model_dump_json()
    result = compare_baseline(current, baseline)
    target = result.baseline["targets"][0]
    text = "Gateway access workload version differs or is missing. Rerun the access check and baseline."
    known = type(version) is int
    detail = {
        "code": "gateway_access_workload_version_differs" if known else "gateway_access_workload_version_unknown",
        "key": "gateway_access_workload_version",
        "current": current.manifest.get("gateway_access_workload_version"),
        "baseline": baseline.manifest.get("gateway_access_workload_version"),
        "text": text,
    }
    if not known:
        detail["side"] = side
    assert target["reasons"] == [text]
    assert target["reason_details"] == [detail]
    assert result.baseline["comparability"] == target["comparability"] == ("not_comparable" if known else "unknown")
    assert (current.model_dump_json(), baseline.model_dump_json()) == before


def test_access_both_versions_missing_is_unknown_without_one_missing_side(access_report):
    del access_report.manifest["gateway_access_workload_version"]
    target = compare_baseline(access_report, access_report).baseline["targets"][0]
    detail, = target["reason_details"]
    assert detail["code"] == "gateway_access_workload_version_unknown"
    assert detail["current"] is detail["baseline"] is None
    assert "side" not in detail
    assert target["comparability"] == "unknown"


def test_access_matching_versions_ignore_obsolete_source_hashes(access_report):
    baseline = access_report.model_copy(deep=True)
    access_report.manifest["gateway_access_source_sha256"] = "a" * 64
    baseline.manifest["gateway_access_source_sha256"] = "b" * 64
    baseline.manifest["fixture_pack"]["workload_fixture_sha256"] = "c" * 64
    result = compare_baseline(access_report, baseline)
    target = result.baseline["targets"][0]
    assert target["reasons"] == target["reason_details"] == []
    assert result.baseline["comparability"] == target["comparability"] == "comparable"
