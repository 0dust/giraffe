"""Merged deployment and telemetry features remain visible without embedded JSON."""

from html import unescape

import pytest

from giraffe.cli import load_report
from giraffe.reporting import compare_baseline, reproduction_export, write_report
from tests.test_reporting import make_report


FIELDS = ["extension.requested_speculative_config", "serving.launch_arguments"]


def deployment_comparison(*, confounded=False):
    current, baseline = make_report(), make_report("baseline")
    for value, setting in ((current, "on"), (baseline, "off")):
        value.observations = {"targets": {"local": {"deployment": {
            "fields": {field: {"configured": {"value": setting}} for field in FIELDS},
            "intended_change": FIELDS[:1] if confounded else FIELDS,
        }}}}
    return compare_baseline(current, baseline)


@pytest.mark.parametrize("confounded", [False, True])
def test_baseline_visibly_explains_grouped_and_confounded_deployment_changes(confounded, tmp_path):
    value = deployment_comparison(confounded=confounded)
    diff = value.baseline["targets"][0]["configuration_diff"]
    assert diff["confounded"] is confounded
    _, path = write_report(value, tmp_path, html=True)
    saved = load_report(tmp_path)
    assert saved.baseline['targets'][0]['configuration_diff'] == diff
    assert reproduction_export(saved)['deployments']['local']['intended_change'] == diff['intended_change']
    html = unescape(path.read_text())
    section = html.split('<section id="baseline">', 1)[1].split('</section>', 1)[0]
    assert f'Deployment change: {", ".join(FIELDS)}' in section
    if confounded:
        assert 'Confounded: multiple unrelated changes' in section
        assert '(one intended change)' not in section
    else:
        assert '(one intended change)' in section
        assert 'Confounded:' not in section
    assert 'not evidence of causality' in section
    assert '<script' not in html and 'data:application/json' not in html


def gpu_series_limited_report():
    from tests.test_gpu_reliability import combined_snapshot, gpu_check
    from tests.test_performance import record, report

    first, config = combined_snapshot()
    last, _ = combined_snapshot(elapsed=1, xid=79)
    assert first['series_limit_reached'] and len(first['samples']) <= config.metrics_max_series
    check = gpu_check([first, last], config)
    assert check.status == 'fail' and check.metrics['observed_gpu_count'] == 8
    request = record()
    request.target = 'test'
    return report([request], config=config.model_dump(), checks=[check], observations={
        'targets': {'test': {'serving_telemetry': {'snapshots': [first, last]}}}})


def test_series_limited_gpu_evidence_remains_in_checks_and_server_observations(tmp_path):
    _, path = write_report(gpu_series_limited_report(), tmp_path, html=True)
    html = unescape(path.read_text())
    checks = html.split('<section id="checks">', 1)[1].split('<section id="baseline">', 1)[0]
    assert 'GPU 7: XID changed during observation (value 79.0)' in checks
    assert 'DCGM_FI_DEV_XID_ERRORS' in checks
    server = html.split('>08 Server observations</h2>', 1)[1].split('</section>', 1)[0]
    assert 'GPU device errors' in server and '>79 ' in server
    assert 'Speculative decoding acceptance rate' in server
    assert 'GPU device errors' not in html.split('<section id="checks">', 1)[0]
    assert '<script' not in html and 'data:application/json' not in html
