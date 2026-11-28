"""Merged deployment and telemetry features remain visible without embedded JSON."""

import asyncio
import json
import shlex
import time
from html import unescape

import pytest

from giraffe.cli import load_report, main
from giraffe.deployment import snapshot as deployment_snapshot
from giraffe.launch_settings import LAUNCH_FIELD
from giraffe.models import RunConfig
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


LAUNCH_EVIDENCE_CASES = {
    "sglang-argv": (True, False),
    "sglang-quoted": (True, False),
    "vllm-redacted-model": (True, False),
    "vllm-unchanged": (False, False),
    "vllm-changed-tp": (False, True),
    "vllm-redacted-model-and-changed-tp": (True, True),
}


async def saved_launch_evidence_comparison(tmp_path, mode):
    redacted, confounded = LAUNCH_EVIDENCE_CASES[mode]
    runs = []
    for arm, fraction, count in (("baseline", "0.7", "2"), ("current", "0.8", "4")):
        deployment = {"runtime": {"image": arm}, "intended_change": "runtime.image"}
        if mode.startswith("sglang"):
            prefix = "python -m sglang.launch_server --model-path example/model --mem-fraction-static"
            command = f"{prefix} '{fraction}'" if mode == "sglang-quoted" else [*prefix.split(), fraction]
            metadata = tmp_path / f"{arm}-metadata.json"
            metadata.write_text(json.dumps({"serving": {"launch_arguments": command}}))
            deployment["metadata_file"] = str(metadata)
        else:
            model = f"example/{arm}-model" if redacted else "example/model"
            command = ["vllm", "serve", model, "-tp", count if confounded else "2"]
            if redacted:
                deployment["launch_command"] = command
            else:
                deployment["serving"] = {"launch_arguments": command}
        config = RunConfig(targets=[{
            "name": "local", "url": "http://localhost:1/v1", "model": "alias", "deployment": deployment,
        }])
        evidence = await deployment_snapshot(config.targets[0], config, time.monotonic() + 5, asyncio.Event())
        report = make_report(arm)
        report.manifest["config"] = config.model_dump(mode="json")
        report.observations = {"targets": {"local": {"deployment": evidence}}}
        runs.append(report)
    baseline_path, _ = write_report(runs[0], tmp_path / "baseline")
    return compare_baseline(runs[1], load_report(baseline_path))


@pytest.mark.parametrize("mode", LAUNCH_EVIDENCE_CASES)
async def test_saved_launch_evidence_controls_deployment_summary(tmp_path, mode):
    redacted, confounded = LAUNCH_EVIDENCE_CASES[mode]
    comparison = await saved_launch_evidence_comparison(tmp_path, mode)
    json_path, html_path = write_report(comparison, tmp_path / "comparison", html=True)
    diff = json.loads(json_path.read_text())["baseline"]["targets"][0]["configuration_diff"]
    launch = next(row for row in diff["fields"] if row["field"] == LAUNCH_FIELD)
    old, new = [launch[side]["configured"]["value"] for side in ("baseline", "current")]
    assert (old != new) == confounded
    for command in (old, new):
        assert isinstance(command, str) == (mode == "sglang-quoted")
        argv = shlex.split(command) if isinstance(command, str) else command
        if mode.startswith("sglang"):
            assert "--mem-fraction-static=[redacted]" in argv
        else:
            assert argv[2] == ("[redacted]" if redacted else "example/model")
    expected_changes = {"runtime.image", LAUNCH_FIELD} if confounded else {"runtime.image"}
    assert {row["field"] for row in diff["fields"] if row["change"] != "no_observed_change"} == expected_changes
    assert diff["confounded"] is confounded
    launch_comparison = diff["launch_comparison"]
    assert launch_comparison["status"] == ("unverified" if redacted else "not_requested")
    html = unescape(html_path.read_text())
    section = html.split('<section id="baseline">', 1)[1].split('</section>', 1)[0]
    assert ("Launch setting comparison unverified" in section) == redacted
    assert ("(one intended change)" in section) == (not (redacted or confounded))
    assert ("Confounded: multiple unrelated changes" in section) == confounded
    warnings = launch_comparison["warnings"]
    if redacted:
        assert any("redacted" in warning and warning in section for warning in warnings)
    else:
        assert not warnings


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
