"""Regression coverage for issue 89 launch evidence comparison and retention."""

import asyncio
import json
import shlex
import time

import pytest

from giraffe.caps import MAX_LAUNCH_ARGUMENTS, MAX_SANITIZE_STRING_CHARS
from giraffe.deployment import deployment_diff, load_metadata, safe_launch, sanitize, snapshot
from giraffe.launch_settings import LAUNCH_FIELD, WHOLE_FIELD_WARNING
from giraffe.models import RunConfig, RunReport
from giraffe.reporting import compare_baseline, write_report
from tests.fake_endpoint import FakeEndpoint
from tests.test_reporting import make_report


PREFIX = ["vllm", "serve", "org/example-model"]
SPECULATION = f"{LAUNCH_FIELD}.--speculative-config"
SPECULATION_METADATA = "extension.requested_speculative_config"


def arguments(count, *extra):
    return [*PREFIX, "--speculative-config", json.dumps({
        "method": "ngram", "num_speculative_tokens": count}), *extra]


def evidence(command, **fields):
    values = {LAUNCH_FIELD: command, **fields}
    return {"intended_change": SPECULATION, "fields": {
        key: {"configured": {"value": value}} for key, value in values.items()}}


def capped_command(count, suffix):
    prefix = shlex.join(arguments(count))
    partial = "--max-num-seqs=1"
    return prefix.ljust(MAX_SANITIZE_STRING_CHARS - len(partial)) + partial + suffix


def test_clipped_configuration_file_and_legacy_command_are_unverified(tmp_path):
    before, after = capped_command(2, "6"), capped_command(3, "28")
    legacy_before, legacy_after = sanitize(before), sanitize(after)
    assert legacy_before.endswith("--max-num-seqs=1")
    assert legacy_after.endswith("--max-num-seqs=1")
    old_diff = deployment_diff(evidence(legacy_after), evidence(legacy_before))
    assert old_diff["launch_comparison"]["status"] == "unverified"
    assert old_diff["launch_comparison"]["settings"] == []

    path = tmp_path / "metadata.json"
    path.write_text(json.dumps({"serving": {"launch_arguments": after}}))
    imported = load_metadata(path)["serving"]["launch_arguments"]
    assert "--max-num-seqs=1" not in str(imported)
    current = evidence(imported)
    diff = deployment_diff(current, evidence(shlex.join(arguments(2, "--max-num-seqs=16"))))
    assert diff["launch_comparison"]["status"] == "unverified"
    assert not any(row["flag"] == "--max-num-seqs" for row in diff["launch_comparison"]["settings"])


@pytest.mark.parametrize("legacy", [
    arguments(2) + ["--enforce-eager"] * (MAX_LAUNCH_ARGUMENTS - len(arguments(2))),
    arguments(2, "--max-num-seqs=" + "1" * MAX_SANITIZE_STRING_CHARS)[:-1] + [
        ("--max-num-seqs=" + "1" * MAX_SANITIZE_STRING_CHARS)[:MAX_SANITIZE_STRING_CHARS]],
])
def test_legacy_argument_count_and_token_caps_are_unverified(legacy):
    diff = deployment_diff(evidence(arguments(3)), evidence(legacy))
    assert diff["launch_comparison"]["status"] == "unverified"
    assert any("size limit" in warning for warning in diff["launch_comparison"]["warnings"])
    assert diff["launch_comparison"]["settings"] == []


@pytest.mark.parametrize("as_string", [False, True])
def test_safe_launch_drops_partial_tokens_and_preserves_limit_warning(as_string):
    flag = "--revision"
    value = "harmless-boundary-marker" + "x" * MAX_SANITIZE_STRING_CHARS
    command = arguments(2, flag, value)
    saved = safe_launch(shlex.join(command) if as_string else command)
    assert "harmless-boundary-marker" not in str(saved)
    assert safe_launch(saved) == saved
    diff = deployment_diff(evidence(arguments(3)), evidence(saved))
    assert any("size limit" in warning for warning in diff["launch_comparison"]["warnings"])


@pytest.mark.parametrize("shape", ["missing", "null", "disagreeing"])
@pytest.mark.parametrize("supplied_in_current", [False, True])
def test_only_supplied_metadata_is_checked_against_its_command(shape, supplied_in_current):
    count = 3 if supplied_in_current else 2
    before, after = evidence(arguments(2)), evidence(arguments(3))
    with_metadata, other = (after, before) if supplied_in_current else (before, after)
    with_metadata["fields"][SPECULATION_METADATA] = {
        "configured": {"value": {"method": "ngram", "k": count}}}
    if shape != "missing":
        value = None if shape == "null" else {"method": "ngram", "k": 5}
        other["fields"][SPECULATION_METADATA] = {"configured": {"value": value}}
    diff = deployment_diff(after, before)
    assert diff["confounded"] is (shape != "missing")
    assert diff["launch_comparison"]["status"] == ("checked" if shape == "missing" else "unverified")
    assert diff["launch_comparison"]["settings"][0]["change"] == "changed"


@pytest.mark.parametrize("value, expected_status, expected_diff", [
    ("0.9", "checked", []),
    ("0.85", "checked", [(0.9, 0.85, "changed")]),
    ("nan", "unverified", [(0.9, "[redacted]", "unverified")]),
    ("-inf", "unverified", [(0.9, "[redacted]", "unverified")]),
    ("-0.1", "unverified", [(0.9, "[redacted]", "unverified")]),
    ("1.1", "unverified", [(0.9, "[redacted]", "unverified")]),
    ("nonsense", "unverified", [(0.9, "[redacted]", "unverified")]),
])
def test_gpu_utilization_compares_as_a_bounded_float(value, expected_status, expected_diff):
    flag = "--gpu-memory-utilization"
    before = evidence(arguments(2, flag, "0.90"))
    after = evidence(arguments(3, flag + "=" + value))
    diff = deployment_diff(after, before)
    assert diff["launch_comparison"]["status"] == expected_status
    assert diff["confounded"] is (value == "0.85")
    assert [
        (row["baseline"], row["current"], row["change"])
        for row in diff["launch_comparison"]["settings"] if row["flag"] == flag
    ] == expected_diff
    json.dumps(diff["launch_comparison"], allow_nan=False)


@pytest.mark.parametrize("source", ["deployment", "metadata file", "collector"])
@pytest.mark.parametrize("equals", [False, True])
@pytest.mark.parametrize("as_string", [False, True])
async def test_structured_speculation_is_sanitized_before_snapshot_and_export(
        tmp_path, source, equals, as_string):
    marker = "harmless-speculation-marker"
    payload = json.dumps({"method": "ngram", "num_speculative_tokens": 3, "model": marker})
    spec_args = ["--speculative-config=" + payload] if equals else ["--speculative-config", payload]
    args = [*PREFIX, *spec_args, "--max-num-seqs", "16"]
    serving = {"launch_arguments": shlex.join(args) if as_string else args}

    async def collect(deployment, url="http://localhost:1/v1"):
        config = RunConfig(targets=[{"name": "local", "url": url,
            "model": "alias", "deployment": deployment}])
        saved = await snapshot(config.targets[0], config, time.monotonic() + 5, asyncio.Event())
        return config, saved

    if source == "metadata file":
        path = tmp_path / "metadata.json"
        path.write_text(json.dumps({"serving": serving}))
        config, saved = await collect({"metadata_file": str(path)})
    elif source == "collector":
        with FakeEndpoint() as endpoint:
            endpoint.discovery_export = {"serving": serving}
            config, saved = await collect({
                "collector_url": endpoint.url.removesuffix("/v1") + "/configuration"}, endpoint.url)
    else:
        config, saved = await collect({"serving": serving})

    fact = saved["fields"][LAUNCH_FIELD]["reported" if source == "collector" else "configured"]
    argv = fact["value"]
    if isinstance(argv, str):
        argv = shlex.split(argv)
    assert argv == [*PREFIX, "--speculative-config=[redacted]", "--max-num-seqs", "16"]
    assert marker not in config.model_dump_json()
    report = make_report()
    report.manifest["config"] = config.model_dump(mode="json")
    report.observations = {"targets": {"local": {"deployment": saved}}}
    json_path, html_path = write_report(report, tmp_path, html=True)
    for output in (json_path, html_path):
        rendered = output.read_text()
        assert marker not in rendered
        assert "org/example-model" in rendered
        assert "--max-num-seqs" in rendered


@pytest.mark.parametrize("as_string", [False, True])
def test_supported_structured_speculation_can_still_be_compared(tmp_path, as_string):
    path = tmp_path / "metadata.json"

    def load(count, seqs):
        args = arguments(count, "--max-num-seqs", str(seqs))
        path.write_text(json.dumps({"serving": {"launch_arguments": shlex.join(args) if as_string else args}}))
        return load_metadata(path)["serving"]["launch_arguments"]

    before, after = load(2, 16), load(3, 128)
    diff = deployment_diff(evidence(after), evidence(before))
    assert diff["launch_comparison"]["status"] == "checked"
    assert diff["confounded"]
    assert any(row["flag"] == "--max-num-seqs" and row["baseline"] == 16 and row["current"] == 128
               for row in diff["launch_comparison"]["settings"])


@pytest.mark.parametrize("as_string", [False, True])
@pytest.mark.parametrize("change", [None, "setting", "order", "scope"])
def test_legacy_supported_structured_speculation_preserves_real_launch_changes(tmp_path, as_string, change):
    # Before structured launch sanitization, metadata import kept the original argv spelling.
    args = [*PREFIX, "--speculative-config", '{"num_speculative_tokens":2,"method":"ngram"}',
            "--max-num-seqs", "16"]
    old = sanitize(shlex.join(args) if as_string else args)
    if change == "setting":
        args[-1] = "32"
    elif change == "order":
        args[3:] = args[5:] + args[3:5]
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps({"serving": {"launch_arguments": shlex.join(args) if as_string else args}}))
    before = evidence(old, **{"extension.arm": "control"})
    after = evidence(load_metadata(path)["serving"]["launch_arguments"], **{"extension.arm": "treatment"})
    after["intended_change"] = "extension.arm"
    for saved, scope in ((before, "deployment"), (after, "replica-B" if change == "scope" else "deployment")):
        saved["fields"][LAUNCH_FIELD]["configured"].update(
            provenance="user-supplied metadata file", scope=scope, status="unverified")

    diff = deployment_diff(after, before)
    row = next(row for row in diff["fields"] if row["field"] == LAUNCH_FIELD)
    assert row["change"] == ("changed" if change else "no_observed_change")
    assert diff["confounded"] is bool(change)
    assert before["fields"][LAUNCH_FIELD]["configured"]["value"] == old


@pytest.mark.parametrize("old_dtype,new_dtype,unknown,confounded,warned", [
    ("float16", "bfloat16", False, True, None),
    ("float16", "bfloat16", True, True, "--custom-option"),
    ("unknown-type", "another-type", False, False, "--dtype"),
    ("[redacted]", "float16", False, True, "--dtype"),
])
def test_declared_runtime_change_qualifies_the_launch_evidence(
        tmp_path, old_dtype, new_dtype, unknown, confounded, warned):
    def load(dtype, image):
        args = [*PREFIX, "--dtype", dtype]
        if unknown:
            args += ["--custom-option", "placeholder"]
        path = tmp_path / "metadata.json"
        path.write_text(json.dumps({"serving": {"launch_arguments": args}}))
        snapshot = evidence(load_metadata(path)["serving"]["launch_arguments"], **{"runtime.image": image})
        snapshot["intended_change"] = "runtime.image"
        return snapshot

    diff = deployment_diff(load(new_dtype, "image-new"), load(old_dtype, "image-old"))
    assert diff["confounded"] is confounded
    warnings = diff["launch_comparison"]["warnings"]
    assert warnings == ([f"The value of {warned} is redacted; equivalence is unverified."] if warned else [])
    assert diff["launch_comparison"]["status"] == ("unverified" if warned else "not_requested")
    assert "unknown-type" not in json.dumps(diff)
    assert "another-type" not in json.dumps(diff)


def test_legacy_positional_difference_remains_unverified_after_masking():
    before, after = evidence([*PREFIX, "before-operand"]), evidence([*PREFIX, "after-operand"])
    after["intended_change"] = "extension.arm"
    diff = deployment_diff(after, before)
    assert diff["confounded"] is True
    row = diff["fields"][0]
    assert row["change"] == "changed"
    assert row["baseline"] == row["current"] == {"configured": {"value": [*PREFIX, "[redacted]"]}}
    assert diff["launch_comparison"]["status"] == "unverified"


@pytest.mark.parametrize("old,new,confounded,warning", [
    (["[redacted]"], ["[redacted]"], False, "Launch evidence is redacted"),
    ("'[redacted]'", PREFIX, True, "Launch evidence is redacted"),
    (["vllm", "serve", "[redacted]"], PREFIX, True, "The launch model operand is redacted"),
])
def test_unrelated_experiment_qualifies_redacted_commands_and_model_operands(old, new, confounded, warning):
    before, after = evidence(old), evidence(new)
    after["intended_change"] = "runtime.image"
    diff = deployment_diff(after, before)
    assert diff["confounded"] is confounded
    assert diff["launch_comparison"]["status"] == "unverified"
    assert any(message.startswith(warning) for message in diff["launch_comparison"]["warnings"])


def test_whole_field_only_does_not_check_redacted_options_individually():
    before = evidence([*PREFIX, "--dtype=[redacted]"])
    after = evidence([*PREFIX, "--dtype=float16"])
    after["intended_change"] = LAUNCH_FIELD
    diff = deployment_diff(after, before)
    assert diff["confounded"] is False
    assert diff["launch_comparison"] == {
        "status": "whole_field", "settings": [], "warnings": [WHOLE_FIELD_WARNING],
    }


def test_legacy_launch_baseline_is_sanitized_in_new_json_and_html_exports(tmp_path):
    marker = "harmless-legacy-launch-marker"
    old_args = arguments(2)
    old_args[-1] = json.dumps({"method": "ngram", "num_speculative_tokens": 2, "model": marker})
    baseline, current = make_report("baseline"), make_report()
    for report, argv in ((baseline, sanitize(old_args)), (current, safe_launch(arguments(3), structured=True))):
        deployment = evidence(argv)
        deployment["fields"][LAUNCH_FIELD]["configured"].update(
            provenance="user-supplied metadata file", scope="deployment", status="unverified",
            collected_at=report.started_at, freshness="collection time; underlying update time unknown")
        report.observations = {"targets": {"local": {"deployment": deployment}}}
    source = tmp_path / "saved-baseline.json"
    original = baseline.model_dump_json().encode()
    source.write_bytes(original)
    baseline = RunReport.model_validate_json(source.read_bytes())

    comparison = compare_baseline(current, baseline)
    diff = comparison.baseline["targets"][0]["configuration_diff"]
    assert diff["fields"][0]["baseline"]["configured"]["value"] == [*PREFIX, "--speculative-config=[redacted]"]
    assert diff["launch_comparison"]["settings"][0]["baseline"] == "[redacted]"
    json_path, html_path = write_report(comparison, tmp_path / "comparison", html=True)
    for path in json_path, html_path:
        rendered = path.read_text()
        assert marker not in rendered
        assert "--speculative-config" in rendered
        assert "[redacted]" in rendered
    assert source.read_bytes() == original
    assert baseline.model_dump_json().encode() == original
