"""Deployment and telemetry provenance, missing-data handling and secret exclusion."""

import json
import time

import httpx
import pytest

from giraffe import telemetry
from giraffe.deployment import deployment_diff, safe_launch, load_metadata
from giraffe.models import RunConfig, Target
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
    write_report(report, tmp_path)
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
            config(endpoint, targets=[t], metrics=True, metrics_interval_seconds=0.1)
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
    original = baseline.model_dump_json()
    result = compare_baseline(current, baseline)
    assert result.baseline["status"] == "inconclusive"
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
    write_report(report, tmp_path)
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


async def test_explicitly_deselected_telemetry_does_not_poll_metrics():
    with FakeEndpoint() as endpoint:
        report = await run_suite(config(endpoint, checks=["serving"],
                                        targets=[target(endpoint)], metrics=True))
        assert endpoint.metrics_reads == 0
    assert not report.manifest["config"]["metrics"]
    assert next(check for check in report.checks if check.id == "gpu").status == "skipped"
