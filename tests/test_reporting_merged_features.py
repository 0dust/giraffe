"""Merged deployment and telemetry features remain visible without embedded JSON."""

import json
from html import unescape

import pytest

from giraffe.cli import load_report, main
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


def launch_setting_comparison(mode, *, intended=None):
    from tests.test_deployment import SPECULATION_ARGS, SPECULATION_SETTING, launch_snapshot

    current, baseline = make_report(), make_report("baseline")
    prefix = ["vllm", "serve", "model", "--max-num-seqs"]
    if intended is None:
        intended = FIELDS if mode in {"whole", "legacy"} else SPECULATION_SETTING
    speculation = ["--speculative-config=[redacted]"] if mode == "unverified" else SPECULATION_ARGS
    snapshots = (
        launch_snapshot(prefix + ["32" if mode in {"batching", "whole", "legacy"} else "16"] + speculation,
                        enabled=True, intended=intended),
        launch_snapshot(prefix + ["16"]),
    )
    if mode == "mixed":
        snapshots = (
            launch_snapshot(prefix + ["32"], intended=[
                "serving.launch_arguments", "serving.launch_arguments.--max-num-seqs"]),
            launch_snapshot(prefix + ["16"]),
        )
    elif mode == "legacy_redaction":
        snapshots = (
            launch_snapshot(prefix + ["16"] + SPECULATION_ARGS, intended="extension.arm",
                            **{"extension.arm": "treatment"}),
            launch_snapshot(prefix + ["16"], **{"extension.arm": "control"}),
        )
        snapshots[1]["fields"]["serving.launch_arguments"]["configured"]["value"].append(
            "--speculative-config=[redacted]")
    for report, snapshot in zip((current, baseline), snapshots, strict=True):
        report.observations = {"targets": {"local": {"deployment": snapshot}}}
    result = compare_baseline(current, baseline)
    if mode == "legacy":
        result.baseline["targets"][0]["configuration_diff"].pop("launch_comparison")
    return result


@pytest.mark.parametrize("mode", ["speculation", "batching", "whole", "legacy", "unverified", "mixed", "legacy_redaction"])
def test_launch_setting_results_survive_json_html_and_cli(mode, tmp_path, capsys):
    from giraffe.cli import _print_result

    value = launch_setting_comparison(mode)
    diff = value.baseline["targets"][0]["configuration_diff"]
    json_path, html_path = write_report(value, tmp_path, html=True)
    assert load_report(tmp_path).baseline["targets"][0]["configuration_diff"] == diff
    _print_result(value, json_path, html_path, tmp_path, {})
    html, cli = unescape(html_path.read_text()), capsys.readouterr().out
    for output in (html, cli):
        if mode in {"whole", "legacy"}:
            assert "additional changes within that field are not checked separately" in output
        elif mode in {"unverified", "mixed", "legacy_redaction"}:
            assert "Launch setting comparison unverified" in output
            assert "redacted" in output
            assert "(one intended change)" not in output
            if mode == "mixed":
                assert "additional changes within that field are not checked separately" in output
        elif mode == "batching":
            assert "--max-num-seqs" in output and "additional change" in output
            assert "16" in output and "32" in output
        else:
            assert "--speculative-config" in output and "intended" in output
    assert diff["confounded"] is (mode in {"batching", "unverified"})


@pytest.mark.parametrize(("intended", "confounded"), [(FIELDS[-1], True), (FIELDS, False)])
def test_inspect_and_report_warn_about_legacy_whole_launch_field(intended, confounded, tmp_path, capsys):
    value = launch_setting_comparison("legacy", intended=intended)
    diff = value.baseline["targets"][0]["configuration_diff"]
    assert diff["confounded"] is confounded
    json_path, _ = write_report(value, tmp_path)
    original = json_path.read_bytes()
    warning = ("The whole serving.launch_arguments field was declared intended; additional changes "
               "within that field are not checked separately.")

    assert main(["report", str(json_path), "--html"]) == 0
    html = unescape((tmp_path / "report.html").read_text())
    assert html.count(warning) == 1
    assert ("Confounded: multiple unrelated changes." in html) is confounded
    capsys.readouterr()

    assert main(["inspect", str(json_path)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    inspected_diff = inspected["baseline"]["targets"][0]["configuration_diff"]
    assert inspected_diff.pop("launch_comparison") == {
        "status": "whole_field", "settings": [], "warnings": [warning],
    }
    assert inspected_diff == diff
    assert json_path.read_bytes() == original


@pytest.mark.parametrize("legacy", [False, True])
def test_inspect_preserves_existing_launch_comparison_shape(legacy, tmp_path, capsys):
    value = launch_setting_comparison("batching" if legacy else "whole")
    diff = value.baseline["targets"][0]["configuration_diff"]
    if legacy:
        diff.pop("launch_comparison")
    json_path, _ = write_report(value, tmp_path)

    assert main(["inspect", str(json_path)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["baseline"]["targets"][0]["configuration_diff"] == diff


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
