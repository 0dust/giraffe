"""Deployment and telemetry provenance, missing-data handling and secret exclusion."""

import asyncio
import json
import threading
import time

import httpx
import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from giraffe import telemetry
from giraffe.deployment import deployment_diff, safe_launch, load_metadata
from giraffe.caps import MAX_SANITIZE_ITEMS, MAX_SANITIZE_STRING_CHARS
from giraffe.models import Deployment, RunConfig, Target
from giraffe.reporting import compare_baseline, reproduction_export, write_report
from giraffe.runner import _Run, run_suite
from tests.fake_endpoint import FakeEndpoint
from tests.test_reporting import make_report
from tests.test_workloads import config


def test_large_baseline_comparison_preserves_all_rows_while_redacting_secrets(tmp_path):
    report = make_report()
    report.baseline = {
        "comparisons": [
            {"request_id": str(index), "api_key": "private-test-value"} for index in range(300)
        ]
    }
    write_report(report, tmp_path, html=True)
    saved = json.loads((tmp_path / "report.json").read_text())
    assert len(saved["baseline"]["comparisons"]) == 300
    assert all(row["api_key"] == "[redacted]" for row in saved["baseline"]["comparisons"])
    assert "private-test-value" not in (tmp_path / "report.html").read_text()


async def test_imported_snapshot_keeps_source_scope_and_original_collection_time(tmp_path):
    path = tmp_path / "deployment.json"
    path.write_text(
        json.dumps(
            {
                "runtime": {"version": "old"},
                "scope": "one exported replica",
                "collected_at": "2020-01-01T00:00:00Z",
            }
        )
    )
    with FakeEndpoint() as endpoint:
        report = await run_suite(
            config(
                endpoint,
                targets=[
                    target(
                        endpoint,
                        deployment={
                            "metadata_file": str(path),
                            "runtime": {"engine": "manual"},
                        },
                    )
                ],
            )
        )
    fields = report.observations["targets"]["local"]["deployment"]["fields"]
    imported = fields["runtime.version"]["configured"]
    assert imported["provenance"] == "user-supplied metadata file"
    assert imported["scope"] == "one exported replica"
    assert imported["collected_at"] == "2020-01-01T00:00:00Z"
    assert imported["status"] == "stale"
    assert fields["runtime.engine"]["configured"]["provenance"] == "user-supplied"


@pytest.mark.parametrize("intended", ["fake-model", "different-model"])
async def test_observed_served_model_preserves_configured_identity_and_updates_unknowns(intended):
    with FakeEndpoint() as endpoint:
        report = await run_suite(config(
            endpoint,
            targets=[target(endpoint, deployment={"model": {"served": intended}})],
        ))
    snapshot = report.observations["targets"]["local"]["deployment"]
    row = snapshot["fields"]["model.served"]
    assert row["configured"]["value"] == intended
    assert row["reported"]["value"] == ["fake-model"]
    assert "model.served" not in snapshot["unknown_fields"]
    assert (row.get("status") == "conflicting") == (intended != "fake-model")


@pytest.mark.parametrize("timestamp", [[], "password=hidden-time-secret"])
async def test_invalid_collector_timestamp_does_not_block_inference_or_leak_secrets(timestamp):
    with FakeEndpoint() as endpoint:
        endpoint.discovery_export = {"runtime": {"version": "reported"}, "collected_at": timestamp}
        report = await run_suite(
            config(
                endpoint,
                targets=[
                    target(
                        endpoint,
                        deployment={
                            "collector_url": endpoint.url.removesuffix("/v1") + "/configuration",
                        },
                    )
                ],
            )
        )
    assert any(r.valid for r in report.requests)
    fields = report.observations["targets"]["local"]["deployment"]["fields"]
    assert fields["runtime.version"]["reported"]["freshness"] == "unknown"
    assert "hidden-time-secret" not in report.model_dump_json()


def target(endpoint, **extra):
    return Target(
        name="local",
        url=endpoint.url,
        model="fake-model",
        metrics_url=endpoint.url.removesuffix("/v1") + "/metrics",
        metrics_profile="vllm-v1",
        **extra,
    )


async def test_discovery_conflict_mid_run_change_and_client_target_separation():
    with FakeEndpoint(first_token_delay=0.03) as endpoint:
        endpoint.collector_changes = True
        t = target(
            endpoint,
            deployment={
                "discovery": "vllm",
                "collector_url": endpoint.url.removesuffix("/v1") + "/configuration",
                "serving": {"scheduler": "intended"},
                "intended_change": "serving.scheduler",
            },
        )
        report = await run_suite(
            config(endpoint, targets=[t], metrics=True, metrics_interval_seconds=0.1,
                   gpu_min_observation_seconds=0)
        )
    snapshot = report.observations["targets"]["local"]["deployment"]
    assert snapshot["fields"]["runtime.version"]["reported"]["value"] == "0.30.0"
    assert snapshot["fields"]["serving.scheduler"]["status"] == "conflicting"
    assert snapshot["continuity"] == "unstable"
    assert "hostname" in report.manifest["run_location"]
    assert "hardware.gpu_type" not in snapshot["fields"]
    assert snapshot["unknown_fields"]
    assert snapshot["observed_changes"]


async def test_ollama_discovery_uses_actual_runtime_response():
    with FakeEndpoint() as endpoint:
        report = await run_suite(
            config(endpoint, targets=[target(endpoint, deployment={"discovery": "ollama"})])
        )
    fields = report.observations["targets"]["local"]["deployment"]["fields"]
    assert fields["model.quantization"]["reported"]["value"] == "Q4_K_M"
    assert fields["runtime.engine"]["reported"]["value"] == "ollama"
    assert fields["model.digest"]["reported"]["value"] == "model-digest"
    assert fields["serving.context_limit"]["reported"]["value"] == 2048


async def test_periodic_serving_scrapes_cover_phases_bounded_cardinality_and_samples():
    with FakeEndpoint(first_token_delay=0.05) as endpoint:
        report = await run_suite(
            config(
                endpoint,
                targets=[target(endpoint)],
                metrics=True,
                metrics_interval_seconds=0.1,
                gpu_min_observation_seconds=0,
                metrics_max_samples=3,
                metrics_max_series=2,
            )
        )
    data = report.observations["targets"]["local"]["serving_telemetry"]
    assert len(data["snapshots"]) <= 3
    assert data["snapshots"][0]["phase"] == "start" and data["snapshots"][-1]["phase"] == "end"
    assert all(len(s["samples"]) <= 2 for s in data["snapshots"])
    assert data["cache_pressure"]["status"] == "observed"
    assert any(s["series_limit_reached"] for s in data["snapshots"])
    assert all(
        s["freshness"].startswith("scrape only")
        for snap in data["snapshots"]
        for s in snap["samples"]
    )


@pytest.mark.parametrize(
    "body,state",
    [
        ("", "empty_response"),
        ("[]", "empty_response"),
        ("opaque json", "unsupported_format"),
        ("unrelated_counter 0\n", "absent_families"),
        ("vllm:num_requests_running 3\n", "available"),
    ],
)
async def test_metrics_success_does_not_imply_healthy_or_disabled(body, state):
    with FakeEndpoint() as endpoint:
        endpoint.metrics_text = body
        settings = config(endpoint, targets=[target(endpoint)], metrics=True)
        sample = await telemetry.scrape(_Run(settings, None, None), settings.targets[0], "load")
    assert sample["status"] == state
    assert sample.get("instrumentation") == "unknown"


@pytest.mark.parametrize(
    "status,state",
    [
        (401, "authentication_denied"),
        (403, "authentication_denied"),
        (404, "endpoint_absent"),
        (500, "connection_failure"),
    ],
)
async def test_statistics_http_failures_are_distinct(status, state):
    with FakeEndpoint() as endpoint:
        endpoint.metrics_status = status
        settings = config(endpoint, targets=[target(endpoint)], metrics=True)
        sample = await telemetry.scrape(_Run(settings, None, None), settings.targets[0], "load")
    assert sample["status"] == state


async def test_statistics_and_collector_never_inherit_inference_secrets(monkeypatch):
    monkeypatch.setenv("INFERENCE_SECRET", "test-inference-secret")
    monkeypatch.setenv("METRICS_SECRET", "test-metrics-secret")
    seen = []
    actual = httpx.AsyncClient

    def response(request):
        seen.append(request)
        return httpx.Response(200, text="vllm:num_requests_running 1\n")

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: actual(**kw, transport=httpx.MockTransport(response))
    )
    t = Target(
        name="local",
        url="http://localhost:8000/v1",
        model="m",
        api_key_env="INFERENCE_SECRET",
        metrics_url="http://localhost:9000/metrics",
        metrics_api_key_env="METRICS_SECRET",
        metrics_profile="vllm-v1",
    )
    settings = RunConfig(targets=[t], metrics=True)
    sample = await telemetry.scrape(_Run(settings, None, None), t, "load")
    assert seen[0].headers["Authorization"] == "Bearer test-metrics-secret"
    assert "test-inference-secret" not in str(sample) + settings.model_dump_json()
    assert "test-metrics-secret" not in str(sample) + settings.model_dump_json()


async def test_statistics_client_setup_does_not_block_inference_scheduling(monkeypatch):
    actual = httpx.AsyncClient
    setup_started = threading.Event()
    release_setup = threading.Event()

    def slow_client(**kwargs):
        setup_started.set()
        if not release_setup.wait(timeout=1):
            raise AssertionError("Metrics client setup blocked the event loop")
        return actual(**kwargs, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="vllm:num_requests_running 1\n")))

    monkeypatch.setattr(httpx, "AsyncClient", slow_client)
    target = Target(name="local", model="m", url="http://localhost:8000/v1",
                    metrics_url="http://localhost:9000/metrics", metrics_profile="vllm-v1")
    settings = RunConfig(targets=[target], metrics=True)

    async def inference_progress():
        while not setup_started.is_set():
            await asyncio.sleep(0)
        release_setup.set()

    sample, _ = await asyncio.gather(
        telemetry.scrape(_Run(settings, None, None), target, "load"), inference_progress())
    assert sample["status"] == "available"


async def test_discovery_client_setup_keeps_event_loop_responsive(monkeypatch):
    from giraffe.deployment import snapshot

    actual = httpx.AsyncClient
    started, release = threading.Event(), threading.Event()

    def slow_client(**kwargs):
        started.set()
        if not release.wait(timeout=1):
            raise AssertionError("Discovery client setup blocked the event loop")
        return actual(**kwargs, transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"version": "test-version"})))

    monkeypatch.setattr(httpx, "AsyncClient", slow_client)
    t = Target(name="local", url="http://local", model="test",
               deployment={"discovery": "vllm"})

    async def heartbeat():
        while not started.is_set():
            await asyncio.sleep(0)
        release.set()

    result, _ = await asyncio.gather(
        snapshot(t, RunConfig(targets=[t]), time.monotonic()+5, asyncio.Event()), heartbeat())
    assert result["fields"]["runtime.version"]["reported"]["value"] == "test-version"


def test_counters_resets_replicas_stale_samples_and_gauge_peaks():
    settings = RunConfig(
        targets=[
            {
                "name": "local",
                "url": "http://localhost:1",
                "model": "m",
                "metrics_profile": "vllm-v1",
            }
        ]
    )
    data = {"snapshots": [], "collection_gaps_seconds": []}
    for index, (counter, gauge, replica) in enumerate(
        [(10, 0.4, "a"), (12, 0.95, "a"), (1, 0.5, "a"), (20, 0.3, "b")]
    ):
        body = f'vllm:num_preemptions_total{{replica="{replica}"}} {counter}\nvllm:kv_cache_usage_perc{{replica="{replica}"}} {gauge}\n'
        parsed = telemetry.parse(body, settings.targets[0], settings)
        data["snapshots"].append({**parsed, "phase": "capacity", "collected_at": str(index)})
    summary = telemetry.summarize(data, 0.9)
    series = [row for row in summary["series"] if row["family"] == "preemptions"]
    assert series[0]["resets"] == 1 and series[0]["delta"] is None
    assert series[1]["delta"] is None  # Never bridge replicas.
    assert summary["cache_pressure"]["status"] == "observed"
    stale = telemetry.parse(
        f"vllm:num_requests_running 1 {int((time.time() - 300) * 1000)}",
        settings.targets[0],
        settings,
    )
    assert stale["status"] == "stale_samples"
    assert "running" in stale["missing_families"]


def test_configuration_diff_visible_for_incomparable_and_legacy_baselines():
    current, baseline = make_report(), make_report("baseline")
    current.observations = {
        "targets": {
            "local": {
                "deployment": {
                    "fields": {
                        "runtime.image_digest": {
                            "configured": {
                                "value": "sha256:new",
                                "provenance": "user-supplied",
                                "status": "unverified",
                            }
                        },
                        "backend.attention": {
                            "configured": {
                                "value": "flash",
                                "provenance": "user-supplied",
                                "status": "unverified",
                            }
                        },
                    },
                    "intended_change": "runtime.image_digest",
                }
            }
        }
    }
    baseline.manifest["config"]["concurrency"] = 8
    baseline.limits_reached = ["concurrency"]
    original = baseline.model_dump_json()
    result = compare_baseline(current, baseline)
    assert result.baseline["status"] == "inconclusive"
    assert any("concurrency differs and its limit stopped work" in reason
               for reason in result.baseline["targets"][0]["reasons"])
    diff = result.baseline["targets"][0]["configuration_diff"]
    assert diff["unknown_baseline"] is True and diff["confounded"] is True
    assert {r["field"] for r in diff["fields"]} == {"runtime.image_digest", "backend.attention"}
    assert baseline.model_dump_json() == original


def test_deployment_change_alone_keeps_comparable_workload():
    current, baseline = make_report(), make_report("baseline")
    for report, value in ((current, "new"), (baseline, "old")):
        report.observations = {
            "targets": {
                "local": {
                    "deployment": {
                        "fields": {
                            "runtime.image": {
                                "configured": {
                                    "value": value,
                                    "status": "unverified",
                                    "provenance": "user-supplied",
                                }
                            }
                        }
                    }
                }
            }
        }
    result = compare_baseline(current, baseline)
    assert result.baseline["status"] == "pass"
    assert result.baseline["targets"][0]["configuration_diff"]["fields"][0]["change"] == "changed"


def test_secret_exclusion_across_config_report_baseline_html_and_reproduction(tmp_path):
    unsafe = {
        "runtime": {"image": "https://user:password@host/image?token=hidden"},
        "serving": {
            "configuration": {
                "api_key": "metadata-secret",
                "nested": {"password": "nested-secret"},
                "scheduler": "fcfs",
            }
        },
        "extension": {"authorization": "Bearer extension-secret", "note": "api_key=inline-secret"},
        "launch_command": [
            "vllm",
            "serve",
            "model",
            "--api-key",
            "launch-secret",
            "--max-model-len",
            "1024",
        ],
        "scope": "password=scope-secret",
    }
    config = RunConfig(
        targets=[{"name": "local", "url": "http://localhost:1", "model": "m", "deployment": unsafe}]
    )
    report = make_report()
    report.manifest["config"] = config.model_dump(mode="json")
    write_report(report, tmp_path, html=True)
    exported = json.dumps(reproduction_export(report))
    saved = (
        (tmp_path / "report.json").read_text() + (tmp_path / "report.html").read_text() + exported
    )
    for secret in [
        "metadata-secret",
        "nested-secret",
        "extension-secret",
        "inline-secret",
        "launch-secret",
        "scope-secret",
        "user:password",
        "token=hidden",
    ]:
        assert secret not in saved
    assert "[redacted]" in saved
    assert "fcfs" in saved
    assert safe_launch("vllm serve m --max-model-len 512 --max-model-len 1024")[-4:] == [
        "--max-model-len",
        "512",
        "--max-model-len",
        "1024",
    ]


def test_import_is_allowlisted_and_bounded(tmp_path):
    path = tmp_path / "metadata.json"
    path.write_text(
        json.dumps(
            {
                "runtime": {"image": "safe", "unknown_dump": "unsafe"},
                "environment": {"AWS_SECRET": "hidden"},
                "extension": {"password": "also-hidden"},
            }
        )
    )
    imported = load_metadata(path)
    assert "unsafe" not in json.dumps(imported) and "hidden" not in json.dumps(imported)
    assert imported["runtime"]["image"] == "safe"
    path.write_text("x" * 65537)
    with pytest.raises(ValueError, match="64 KiB"):
        load_metadata(path)


def test_unknown_metadata_does_not_claim_unchanged():
    diff = deployment_diff({}, {})
    assert diff["unknown_baseline"] and "cannot establish unchanged" in diff["interpretation"]


async def test_collector_instrumentation_evidence_and_discovery_absence_are_qualified():
    with FakeEndpoint() as endpoint:
        endpoint.discovery_export = {"serving": {"instrumentation": "disabled"}}
        endpoint.metrics_text = "[]"
        report = await run_suite(
            config(
                endpoint,
                targets=[
                    target(
                        endpoint,
                        deployment={
                            "collector_url": endpoint.url.removesuffix("/v1") + "/configuration"
                        },
                    )
                ],
                metrics=True,
            )
        )
    snaps = report.observations["targets"]["local"]["serving_telemetry"]["snapshots"]
    assert snaps[0]["status"] == "empty_response"
    assert snaps[0]["instrumentation_evidence"]["value"] == "disabled"
    assert snaps[0]["instrumentation_provenance"] == "configured-collector"


async def test_missing_metadata_file_keeps_endpoint_testing_available(tmp_path):
    with FakeEndpoint() as endpoint:
        report = await run_suite(
            config(
                endpoint,
                targets=[
                    target(endpoint, deployment={"metadata_file": str(tmp_path / "missing.json")})
                ],
            )
        )
    assert report.overall == "pass"
    assert (
        report.observations["targets"]["local"]["deployment"]["discovery"][0]["status"]
        == "metadata_file_unavailable"
    )


def test_flat_configuration_imports_preserve_intent_and_safe_launch(tmp_path):
    path = tmp_path / "configuration.json"
    path.write_text(
        json.dumps(
            {
                "max_model_len": 1024,
                "attention_backend": "FLASH_ATTN",
                "api_key": "do-not-save",
                "launch_command": "vllm serve m --api-key secret --max-model-len 1024",
            }
        )
    )
    imported = load_metadata(path)
    assert imported["serving"]["context_limit"] == 1024
    assert imported["backend"]["attention"] == "FLASH_ATTN"
    assert "do-not-save" not in json.dumps(imported) and "secret" not in json.dumps(imported)


async def test_runtime_telemetry_is_collected_when_gpu_check_is_deselected():
    with FakeEndpoint() as endpoint:
        report = await run_suite(config(endpoint, checks=["serving"],
                                        targets=[target(endpoint)], metrics=True))
        assert endpoint.metrics_reads >= 2
    assert not report.manifest["config"]["metrics"]
    assert next(check for check in report.checks if check.id == "gpu").status == "skipped"


@pytest.mark.parametrize("intended,confounded", [
    (["extension.requested_speculative_config", "serving.launch_arguments"], False),
    (["extension.requested_speculative_config"], True),
    (["extension", "serving.*"], True),
    ("extension.requested_speculative_config", True),
    (None, False),
    ("", False),
])
def test_intended_change_matches_exact_fields_without_hiding_other_changes(intended, confounded):
    fields = ("extension.requested_speculative_config", "serving.launch_arguments")
    baseline = {"fields": {field: {"configured": {"value": "off"}} for field in fields},
                "continuity": "stable", "unknown_fields": ["hardware.layout"]}
    current = {"fields": {field: {"configured": {"value": "on"}} for field in fields},
               "intended_change": intended, "continuity": "unverified"}
    diff = deployment_diff(current, baseline)
    assert diff["confounded"] is confounded
    assert diff["intended_change"] == intended
    assert [(row["field"], row["change"]) for row in diff["fields"]] == [
        (field, "changed") for field in fields
    ]
    assert not diff["unknown_baseline"]
    assert diff["unknown_fields"] == ["hardware.layout"]
    assert diff["continuity"] == {"baseline": "stable", "current": "unverified"}
    assert "not evidence of causality" in diff["interpretation"]

    current["fields"]["extension.arm"] = {"configured": {"value": "on"}}
    with_bookkeeping = deployment_diff(current, baseline)
    assert with_bookkeeping["confounded"] is bool(intended)
    assert with_bookkeeping["fields"][0]["field"] == "extension.arm"
    assert with_bookkeeping["fields"][0]["change"] == "added"


def test_intended_change_keeps_legacy_single_field_and_unknown_baseline_behavior():
    current = {"intended_change": "runtime.image", "fields": {
        "runtime.image": {"configured": {"value": "new"}},
    }}
    diff = deployment_diff(current, {})
    assert not diff["confounded"]
    assert diff["unknown_baseline"]
    assert diff["fields"][0]["change"] == "added"
    current["intended_change"] = ["runtime.image"]
    assert deployment_diff(current, {})["confounded"] is False


@pytest.mark.parametrize("intended", [
    None, "", "runtime.image", ["extension.requested_speculative_config", "serving.launch_arguments"],
    ["extension.custom field.with.dots", "extension.custom field.with.dots"],
])
def test_intended_change_public_schema_and_round_trip(intended):
    payload = {"intended_change": intended}
    Draft202012Validator(Deployment.model_json_schema()).validate(payload)
    deployment = Deployment.model_validate(payload)
    assert Deployment.model_validate_json(deployment.model_dump_json()).intended_change == intended


@pytest.mark.parametrize("intended", [
    [], [""], [1], [["runtime.image"]],
    ["runtime.image"] * (MAX_SANITIZE_ITEMS + 1),
    ["x" * (MAX_SANITIZE_STRING_CHARS + 1)],
])
def test_intended_change_rejects_invalid_or_truncated_new_lists(intended):
    payload = {"intended_change": intended}
    assert not Draft202012Validator(Deployment.model_json_schema()).is_valid(payload)
    with pytest.raises(ValidationError):
        Deployment.model_validate(payload)


SPECULATION_SETTING = "serving.launch_arguments.--speculative-config"
SPECULATION_ARGS = ["--speculative-config", '{"method":"eagle3","num_speculative_tokens":3}']


def launch_snapshot(arguments, *, enabled=False, intended=None, **fields):
    values = {
        "serving.launch_arguments": safe_launch(arguments),
        "extension.requested_speculative_config": {"method": "eagle3", "k": 3} if enabled else None,
        **fields,
    }
    return {"fields": {name: {"configured": {"value": value}} for name, value in values.items()},
            "intended_change": intended}


@pytest.mark.parametrize("intended", [SPECULATION_SETTING, [SPECULATION_SETTING]])
@pytest.mark.parametrize("as_string", [False, True])
def test_individual_speculation_setting_covers_equivalent_metadata(intended, as_string):
    import shlex

    before = ["vllm", "serve", "model", "--max-num-seqs", "16"]
    after = before + SPECULATION_ARGS
    if as_string:
        before, after = shlex.join(before), shlex.join(after)
    diff = deployment_diff(launch_snapshot(after, enabled=True, intended=intended),
                           launch_snapshot(before))
    assert diff["confounded"] is False
    assert diff["launch_comparison"]["status"] == "unverified"
    assert diff["launch_comparison"]["warnings"] == [
        "The launch model operand is redacted; its equivalence is unverified."]
    changes = diff["launch_comparison"]["settings"]
    assert [(row["flag"], row["change"], row["intended"]) for row in changes] == [
        ("--speculative-config", "added", True)]


@pytest.mark.parametrize("before,after,flag,change", [
    (["--max-num-seqs", "16"], ["--max-num-seqs", "32"], "--max-num-seqs", "changed"),
    ([], ["--max-num-batched-tokens=4096"], "--max-num-batched-tokens", "added"),
    (["--max-num-seqs", "16"], [], "--max-num-seqs", "removed"),
    ([], ["--enforce-eager"], "--enforce-eager", "added"),
    ([], ["--unknown-option", "private-value"], "--unknown-option", "added"),
])
def test_individual_speculation_flags_additional_launch_changes(before, after, flag, change):
    prefix = ["vllm", "serve", "model"]
    diff = deployment_diff(
        launch_snapshot(prefix + after + SPECULATION_ARGS, enabled=True, intended=SPECULATION_SETTING),
        launch_snapshot(prefix + before))
    assert diff["confounded"] is True
    assert any(row["flag"] == flag and row["change"] == change and not row["intended"]
               for row in diff["launch_comparison"]["settings"])
    assert "private-value" not in json.dumps(diff)


@pytest.mark.parametrize("flag,field", [
    ("--max-num-seqs", "serving.max_num_seqs"),
    ("--max-num-batched-tokens", "serving.max_num_batched_tokens"),
])
def test_individual_batching_setting_covers_its_metadata(flag, field):
    prefix = ["vllm", "serve", "model"]
    diff = deployment_diff(
        launch_snapshot(prefix + [flag + "=32"], intended="serving.launch_arguments." + flag,
                        **{field: 32}),
        launch_snapshot(prefix + [flag, "16"], **{field: 16}))
    assert diff["confounded"] is False
    assert diff["launch_comparison"]["status"] == "unverified"


def test_whole_launch_declaration_keeps_behavior_and_explains_its_scope():
    prefix = ["vllm", "serve", "model", "--max-num-seqs"]
    intended = ["serving.launch_arguments", "extension.requested_speculative_config"]
    diff = deployment_diff(
        launch_snapshot(prefix + ["32"] + SPECULATION_ARGS, enabled=True, intended=intended),
        launch_snapshot(prefix + ["16"]))
    assert diff["confounded"] is False
    assert diff["intended_change"] == intended
    assert "additional changes within that field are not checked separately" in " ".join(
        diff["launch_comparison"]["warnings"])


@pytest.mark.parametrize("arguments,reason", [
    (["--max-num-seqs", "16", "--max-num-seqs", "32"], "duplicate"),
    (["--unknown-option", "hidden"], "unknown"),
    (["--config", "settings.yaml"], "configuration"),
    (["--max-num-seqs", "[redacted]"], "redacted"),
])
def test_ambiguous_launch_inputs_never_claim_a_checked_comparison(arguments, reason):
    prefix = ["vllm", "serve", "model"]
    diff = deployment_diff(
        launch_snapshot(prefix + arguments + SPECULATION_ARGS, enabled=True, intended=SPECULATION_SETTING),
        launch_snapshot(prefix + arguments))
    assert diff["launch_comparison"]["status"] == "unverified"
    assert reason in " ".join(diff["launch_comparison"]["warnings"]).lower()


def test_changed_argument_order_and_redacted_speculation_remain_unverified():
    before = ["vllm", "serve", "model", "--max-num-seqs", "16", "--max-model-len", "1024"]
    after = ["vllm", "serve", "model", "--max-model-len", "1024", "--max-num-seqs", "16"]
    diff = deployment_diff(
        launch_snapshot(after + ["--speculative-config=[redacted]"], enabled=True,
                        intended=SPECULATION_SETTING), launch_snapshot(before))
    warnings = " ".join(diff["launch_comparison"]["warnings"]).lower()
    assert diff["launch_comparison"]["status"] == "unverified"
    assert "order" in warnings and "redacted" in warnings


def test_speculation_metadata_disagreement_is_not_accepted_as_equivalent():
    prefix = ["vllm", "serve", "model"]
    current = launch_snapshot(prefix + SPECULATION_ARGS, enabled=True, intended=SPECULATION_SETTING)
    current["fields"]["extension.requested_speculative_config"]["configured"]["value"]["k"] = 8
    diff = deployment_diff(current, launch_snapshot(prefix))
    assert diff["confounded"] is True
    assert diff["launch_comparison"]["status"] == "unverified"
    assert "equivalence" in " ".join(diff["launch_comparison"]["warnings"]).lower()


@pytest.mark.parametrize("payload", [
    '{"method":"eagle3","num_speculative_tokens":3,"model":"private-model"}',
    '{"method":"eagle3","method":"mtp","num_speculative_tokens":3}',
    '{"method":"password=private-method","num_speculative_tokens":3}',
])
def test_speculation_launch_import_redacts_unsupported_or_ambiguous_values(payload):
    arguments = safe_launch(["vllm", "serve", "model", "--speculative-config", payload])
    assert arguments[-1] == "--speculative-config=[redacted]"
    assert "private-" not in json.dumps(arguments)


def test_setting_comparison_ignores_collection_time_but_keeps_evidence_context_changes():
    prefix = ["vllm", "serve", "model"]
    baseline = launch_snapshot(prefix)
    current = launch_snapshot(prefix + SPECULATION_ARGS, enabled=True, intended=SPECULATION_SETTING)
    for snapshot, stamp in ((baseline, "2026-11-17T00:00:00Z"), (current, "2026-11-18T00:00:00Z")):
        for row in snapshot["fields"].values():
            row["configured"].update(collected_at=stamp, scope="one replica", provenance="user-supplied")
    assert deployment_diff(current, baseline)["confounded"] is False
    current["fields"]["serving.launch_arguments"]["configured"]["scope"] = "another replica"
    diff = deployment_diff(current, baseline)
    assert diff["confounded"] is True
    assert diff["launch_comparison"]["status"] == "unverified"


def test_individual_setting_does_not_hide_a_changed_imported_model_operand():
    prefix = ["vllm", "serve", "model"]
    baseline = launch_snapshot(prefix)
    current = launch_snapshot(prefix + SPECULATION_ARGS, enabled=True, intended=SPECULATION_SETTING)
    baseline["fields"]["serving.launch_arguments"]["configured"]["value"][2] = "old-model"
    current["fields"]["serving.launch_arguments"]["configured"]["value"][2] = "new-model"
    assert deployment_diff(current, baseline)["confounded"] is True


def test_unknown_individual_setting_selector_is_explicitly_unverified():
    prefix = ["vllm", "serve", "model"]
    diff = deployment_diff(
        launch_snapshot(prefix + ["--max-num-seqs", "32"],
                        intended="serving.launch_arguments.--unknown-setting"),
        launch_snapshot(prefix))
    assert diff["confounded"] is True
    assert diff["launch_comparison"]["status"] == "unverified"


def test_known_imported_model_and_supported_settings_can_be_checked():
    prefix = ["vllm", "serve", "model"]
    baseline = launch_snapshot(prefix)
    current = launch_snapshot(prefix + SPECULATION_ARGS, enabled=True, intended=SPECULATION_SETTING)
    for saved in (baseline, current):
        saved["fields"]["serving.launch_arguments"]["configured"]["value"][2] = "model"
    diff = deployment_diff(current, baseline)
    assert diff["confounded"] is False
    assert diff["launch_comparison"]["status"] == "checked"
    assert diff["launch_comparison"]["warnings"] == []


async def test_redacted_model_operand_stays_unverified_after_collection():
    from giraffe.deployment import snapshot

    async def collect(model, enabled):
        command = ["vllm", "serve", model] + (SPECULATION_ARGS if enabled else [])
        config = RunConfig(targets=[{"name": "local", "url": "http://localhost:1/v1", "model": "alias",
            "deployment": {"launch_command": command,
                           "extension": {"requested_speculative_config": {"method": "eagle3", "k": 3} if enabled else None},
                           "intended_change": SPECULATION_SETTING if enabled else None}}])
        return await snapshot(config.targets[0], config, time.monotonic() + 5, asyncio.Event())

    baseline, current = await collect("old-model", False), await collect("new-model", True)
    for saved in (baseline, current):
        assert saved["fields"]["serving.launch_arguments"]["configured"]["value"][2] == "[redacted]"
    diff = deployment_diff(current, baseline)
    assert diff["confounded"] is False
    assert diff["launch_comparison"]["status"] == "unverified"
    assert any("model operand" in warning and "redacted" in warning
               for warning in diff["launch_comparison"]["warnings"])


def test_competing_prefix_cache_flags_have_explicit_precedence_uncertainty():
    prefix = ["vllm", "serve", "model", "--enable-prefix-caching", "--no-enable-prefix-caching"]
    diff = deployment_diff(
        launch_snapshot(prefix + SPECULATION_ARGS, enabled=True, intended=SPECULATION_SETTING),
        launch_snapshot(prefix))
    assert diff["launch_comparison"]["status"] == "unverified"
    assert any("prefix-caching" in warning and "precedence" in warning
               for warning in diff["launch_comparison"]["warnings"])


def test_redacted_value_does_not_establish_an_additional_setting_change():
    prefix = ["vllm", "serve", "model", "--attention-backend"]
    diff = deployment_diff(
        launch_snapshot(prefix + ["[redacted]"] + SPECULATION_ARGS,
                        enabled=True, intended=SPECULATION_SETTING),
        launch_snapshot(prefix + ["FLASH_ATTN"]))
    assert diff["confounded"] is False
    assert any(row["flag"] == "--attention-backend" and row["change"] == "unverified"
               for row in diff["launch_comparison"]["settings"])
    assert any("--attention-backend" in warning and "redacted" in warning
               for warning in diff["launch_comparison"]["warnings"])


@pytest.mark.parametrize("enabled", [True, False])
async def test_speculation_metadata_addition_and_removal_are_one_setting(enabled):
    from giraffe.deployment import snapshot

    async def collect(on):
        config = RunConfig(targets=[{"name": "local", "url": "http://localhost:1/v1", "model": "alias",
            "deployment": {"launch_command": ["vllm", "serve", "model"] + (SPECULATION_ARGS if on else []),
                "extension": {"requested_speculative_config": {"method": "eagle3", "k": 3}} if on else {},
                "intended_change": SPECULATION_SETTING}}])
        return await snapshot(config.targets[0], config, time.monotonic() + 5, asyncio.Event())

    baseline, current = await collect(not enabled), await collect(enabled)
    diff = deployment_diff(current, baseline)
    assert diff["confounded"] is False
    assert diff["launch_comparison"]["warnings"] == [
        "The launch model operand is redacted; its equivalence is unverified."]
    assert diff["launch_comparison"]["settings"][0]["change"] == ("added" if enabled else "removed")


def test_existing_metadata_context_changes_are_not_covered_as_equivalent():
    prefix = ["vllm", "serve", "model"]
    baseline = launch_snapshot(prefix)
    current = launch_snapshot(prefix + SPECULATION_ARGS, enabled=True, intended=SPECULATION_SETTING)
    baseline["fields"]["extension.requested_speculative_config"]["configured"]["scope"] = "one replica"
    current["fields"]["extension.requested_speculative_config"]["configured"]["scope"] = "another replica"
    diff = deployment_diff(current, baseline)
    assert diff["confounded"] is True
    assert any("equivalence" in warning for warning in diff["launch_comparison"]["warnings"])


@pytest.mark.parametrize("equals", [False, True])
def test_legacy_redacted_speculation_is_unverified_without_an_individual_selector(equals):
    prefix = ["vllm", "serve", "model", "--max-num-seqs", "16"]
    speculation = ["--speculative-config=" + SPECULATION_ARGS[1]] if equals else SPECULATION_ARGS
    baseline = launch_snapshot(prefix, **{"extension.arm": "control"})
    baseline["fields"]["serving.launch_arguments"]["configured"]["value"].append(
        "--speculative-config=[redacted]")
    current = launch_snapshot(prefix + speculation, intended="extension.arm",
                              **{"extension.arm": "treatment"})
    diff = deployment_diff(current, baseline)
    assert diff["confounded"] is False
    assert diff["launch_comparison"]["status"] == "unverified"
    assert any(row["flag"] == "--speculative-config" and row["change"] == "unverified"
               for row in diff["launch_comparison"]["settings"])
    assert any("model operand" in warning and "redacted" in warning
               for warning in diff["launch_comparison"]["warnings"])
    raw = next(row for row in diff["fields"] if row["field"] == "serving.launch_arguments")
    assert raw["change"] == "changed"
    assert raw["baseline"] == baseline["fields"]["serving.launch_arguments"]
    assert raw["current"] == current["fields"]["serving.launch_arguments"]


@pytest.mark.parametrize("change", ["value", "added", "removed", "order", "positional", "model", "scope"])
def test_legacy_speculation_compatibility_does_not_hide_other_launch_differences(change):
    prefix = ["vllm", "serve", "model", "--max-num-seqs", "16", "--max-model-len", "1024"]
    baseline = launch_snapshot(prefix, **{"extension.arm": "control"})
    baseline["fields"]["serving.launch_arguments"]["configured"]["value"].append(
        "--speculative-config=[redacted]")
    current = launch_snapshot(prefix + SPECULATION_ARGS, intended="extension.arm",
                              **{"extension.arm": "treatment"})
    row = current["fields"]["serving.launch_arguments"]["configured"]
    if change == "value":
        row["value"][4] = "32"
    elif change == "added":
        row["value"].append("--enforce-eager")
    elif change == "removed":
        del row["value"][3:5]
    elif change == "order":
        row["value"][3:7] = row["value"][5:7] + row["value"][3:5]
    elif change == "positional":
        row["value"].append("extra-operand")
    elif change == "model":
        row["value"][2] = "another-model"
    else:
        row["scope"] = "replica-B"
    assert deployment_diff(current, baseline)["confounded"] is True


@pytest.mark.parametrize("arguments,reason", [
    (["--max-num-seqs", "32"], "model operand"),
    (["--max-num-seqs", "16", "--max-num-seqs", "32"], "duplicate"),
    (["--max-num-seqs", "unsupported"], "unsupported"),
])
def test_mixed_whole_and_individual_launch_declarations_keep_uncertainty(arguments, reason):
    intended = ["serving.launch_arguments", "serving.launch_arguments.--max-num-seqs"]
    baseline = launch_snapshot(["vllm", "serve", "model", "--max-num-seqs", "16"])
    current = launch_snapshot(["vllm", "serve", "model"] + arguments, intended=intended)
    diff = deployment_diff(current, baseline)
    assert diff["confounded"] is False
    assert diff["launch_comparison"]["status"] == "unverified"
    warnings = " ".join(diff["launch_comparison"]["warnings"]).lower()
    assert reason in warnings
    assert "additional changes within that field are not checked separately" in warnings
    assert all(row["intended"] for row in diff["launch_comparison"]["settings"])


@pytest.mark.parametrize("key,value", [
    ("scope", "replica-B"), ("provenance", "runtime-reported"), ("status", "runtime_reported"),
])
async def test_imported_batching_metadata_must_match_its_launch_evidence_context(tmp_path, key, value):
    from giraffe.deployment import snapshot

    async def collect(count):
        path = tmp_path / f"metadata-{count}.json"
        path.write_text(json.dumps({"scope": "deployment", "max_num_seqs": count}))
        config = RunConfig(targets=[{"name": "local", "url": "http://localhost:1/v1", "model": "alias",
            "deployment": {"launch_command": ["vllm", "serve", "model", "--max-num-seqs", str(count)],
                "metadata_file": str(path), "scope": "deployment",
                "intended_change": "serving.launch_arguments.--max-num-seqs"}}])
        return await snapshot(config.targets[0], config, time.monotonic() + 5, asyncio.Event())

    baseline, current = await collect(16), await collect(32)
    # Metadata files and explicit launch commands are both user declarations of this scope.
    assert deployment_diff(current, baseline)["confounded"] is False
    for saved in (baseline, current):
        saved["fields"]["serving.max_num_seqs"]["configured"][key] = value
    diff = deployment_diff(current, baseline)
    assert diff["confounded"] is True
    assert diff["launch_comparison"]["status"] == "unverified"
    assert any("serving.max_num_seqs" in warning and "equivalence" in warning
               for warning in diff["launch_comparison"]["warnings"])


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("scope,confounded", [("deployment", False), ("replica-B", True)])
async def test_imported_one_sided_speculation_metadata_requires_matching_launch_scope(
        tmp_path, enabled, scope, confounded):
    from giraffe.deployment import snapshot

    async def collect(on):
        path = tmp_path / f"metadata-{on}.json"
        path.write_text(json.dumps({"scope": scope, "extension": {
            "requested_speculative_config": {"method": "eagle3", "k": 3}} if on else {}}))
        config = RunConfig(targets=[{"name": "local", "url": "http://localhost:1/v1", "model": "alias",
            "deployment": {"launch_command": ["vllm", "serve", "model"] + (SPECULATION_ARGS if on else []),
                "metadata_file": str(path), "scope": "deployment",
                "intended_change": SPECULATION_SETTING}}])
        return await snapshot(config.targets[0], config, time.monotonic() + 5, asyncio.Event())

    baseline, current = await collect(not enabled), await collect(enabled)
    diff = deployment_diff(current, baseline)
    assert diff["confounded"] is confounded
    if confounded:
        assert any("equivalence" in warning for warning in diff["launch_comparison"]["warnings"])


@pytest.mark.parametrize("provenance", [["user-supplied"], {"source": "user-supplied"}])
@pytest.mark.parametrize("matching", [False, True])
def test_saved_metadata_provenance_can_be_compared_without_hashing(provenance, matching):
    current, baseline = make_report(), make_report("baseline")
    for report, count in ((current, 32), (baseline, 16)):
        saved = launch_snapshot(["vllm", "serve", "model", "--max-num-seqs", str(count)],
            intended="serving.launch_arguments.--max-num-seqs", **{"serving.max_num_seqs": count})
        saved["fields"]["serving.max_num_seqs"]["configured"]["provenance"] = provenance
        saved["fields"]["serving.launch_arguments"]["configured"]["provenance"] = (
            provenance if matching else "user-supplied")
        report.observations = {"targets": {"local": {"deployment": saved}}}
    diff = compare_baseline(current, baseline).baseline["targets"][0]["configuration_diff"]
    assert diff["confounded"] is not matching
    assert diff["launch_comparison"]["status"] == "unverified"
    if not matching:
        assert any("equivalence" in warning for warning in diff["launch_comparison"]["warnings"])


@pytest.mark.parametrize("change", ["none", "value", "scope", "provenance", "status", "removed"])
def test_legacy_speculation_migration_keeps_unchanged_unsupported_launch_sources(change):
    prefix = ["vllm", "serve", "model"]
    baseline = launch_snapshot(prefix, **{"extension.arm": "control"})
    baseline["fields"]["serving.launch_arguments"]["configured"]["value"].append(
        "--speculative-config=[redacted]")
    current = launch_snapshot(prefix + SPECULATION_ARGS, intended="extension.arm",
                              **{"extension.arm": "treatment"})
    reported = {"value": ["python", "-m", "vllm.entrypoints.openai.api_server", "--max-num-seqs", "16"],
                "provenance": "configured-collector", "scope": "deployment", "status": "runtime_reported"}
    for saved in (baseline, current):
        saved["fields"]["serving.launch_arguments"]["reported"] = json.loads(json.dumps(reported))
    row = current["fields"]["serving.launch_arguments"]["reported"]
    if change == "value":
        row["value"][-1] = "32"
    elif change in {"scope", "provenance", "status"}:
        row[change] = "different evidence"
    elif change == "removed":
        del current["fields"]["serving.launch_arguments"]["reported"]
    diff = deployment_diff(current, baseline)
    assert diff["confounded"] is (change != "none")
    assert diff["launch_comparison"]["status"] == "unverified"
    assert any("bounded vllm serve" in warning for warning in diff["launch_comparison"]["warnings"])


def test_individual_selector_does_not_cover_an_unsupported_launch_source():
    prefix = ["vllm", "serve", "model"]
    baseline = launch_snapshot(prefix)
    current = launch_snapshot(prefix + SPECULATION_ARGS, enabled=True, intended=SPECULATION_SETTING)
    for saved in (baseline, current):
        saved["fields"]["serving.launch_arguments"]["reported"] = {
            "value": ["python", "-m", "vllm.entrypoints.openai.api_server"]}
    diff = deployment_diff(current, baseline)
    assert diff["confounded"] is True
    assert diff["launch_comparison"]["status"] == "unverified"


@pytest.mark.parametrize("scope,confounded", [("deployment", False), ("replica-B", True)])
async def test_explicit_launch_override_keeps_its_own_context_after_metadata_import(tmp_path, scope, confounded):
    from giraffe.deployment import snapshot

    async def collect(count):
        path = tmp_path / f"override-{count}.json"
        path.write_text(json.dumps({"scope": scope, "serving": {
            "max_num_seqs": count, "launch_arguments": ["vllm", "serve", "file-model"]}}))
        config = RunConfig(targets=[{"name": "local", "url": "http://localhost:1/v1", "model": "alias",
            "deployment": {"launch_command": ["vllm", "serve", "deployment-model", "--max-num-seqs", str(count)],
                "metadata_file": str(path), "scope": "deployment",
                "intended_change": "serving.launch_arguments.--max-num-seqs"}}])
        return await snapshot(config.targets[0], config, time.monotonic() + 5, asyncio.Event())

    baseline, current = await collect(16), await collect(32)
    diff = deployment_diff(current, baseline)
    assert diff["confounded"] is confounded
    for saved, count in ((baseline, 16), (current, 32)):
        launch = saved["fields"]["serving.launch_arguments"]["configured"]
        assert launch["value"] == safe_launch(["vllm", "serve", "deployment-model", "--max-num-seqs", str(count)])
        assert launch["scope"] == "deployment"
        assert launch["provenance"] == "user-supplied"
        metadata = saved["fields"]["serving.max_num_seqs"]["configured"]
        assert metadata["scope"] == scope
        assert metadata["provenance"] == "user-supplied metadata file"
