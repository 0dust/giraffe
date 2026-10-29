"""GPU verdicts require fresh per-device evidence, and inspect adverse values."""

import time

import pytest

from giraffe import telemetry
from giraffe.models import RunConfig, Target


NAMES = ["DCGM_FI_DEV_FB_USED", "DCGM_FI_DEV_GPU_UTIL", "DCGM_FI_DEV_GPU_TEMP",
         "DCGM_FI_DEV_CLOCKS_EVENT_REASONS", "DCGM_FI_DEV_XID_ERRORS"]


def snapshot(devices=None, *, timestamp=None, elapsed=0):
    devices = {"0": [100, 30, 45, 0, 0]} if devices is None else devices
    body = "\n".join(f'{name}{{gpu="{gpu}"}} {value}' +
                     (f" {timestamp}" if timestamp is not None else "")
                     for gpu, values in devices.items() for name, value in zip(NAMES, values))
    target = Target(name="test", model="model", url="http://localhost:1234")
    config = RunConfig(targets=[target], checks=["gpu"])
    return {"elapsed_seconds": elapsed, "collected_at": telemetry.stamp(),
            **telemetry.parse(body, target, config)}


def test_gpu_frozen_untimestamped_values_have_unknown_freshness():
    metrics, status, summary = telemetry.assess_gpu([snapshot(), snapshot(elapsed=1)], 60)
    assert status == "inconclusive"
    assert metrics["devices"][0]["freshness"] == "unknown"
    assert len(metrics["devices"][0]["unknown_freshness_families"]) == 5
    assert "not device health certification" in summary


def test_gpu_fresh_timestamps_establish_freshness_without_value_changes():
    metrics, status, summary = telemetry.assess_gpu([snapshot(timestamp=time.time() * 1000)], 60)
    assert status == "pass"
    assert metrics["devices"][0]["freshness"] == "source timestamps"
    assert "not device health certification" in summary


def test_gpu_value_changes_across_scrapes_establish_device_freshness():
    metrics, status, _ = telemetry.assess_gpu(
        [snapshot(), snapshot({"0": [101, 30, 45, 0, 0]}, elapsed=1)], 60)
    assert status == "pass"
    assert metrics["devices"][0]["freshness"] == "values changed across scrapes"


def test_gpu_new_label_series_is_not_a_value_change():
    first = snapshot()
    second = snapshot({"0": [101, 30, 45, 0, 0]}, elapsed=1)
    for sample in second["samples"]:
        sample["labels"]["pod"] = "replacement"
    assert telemetry.assess_gpu([first, second], 60)[1] == "inconclusive"


def test_gpu_each_device_requires_all_families():
    snap = snapshot({"0": [100, 30, 45, 0, 0], "1": [100]}, timestamp=time.time() * 1000)
    metrics, status, _ = telemetry.assess_gpu([snap], 60)
    assert status == "inconclusive"
    assert not metrics["devices"][0]["missing_families"]
    assert metrics["devices"][1]["missing_families"] == [
        "device_errors", "temperature", "throttling", "utilization"]


def test_gpu_one_changing_device_cannot_establish_another_devices_freshness():
    first = snapshot({"0": [100, 30, 45, 0, 0], "1": [100, 30, 45, 0, 0]})
    second = snapshot({"0": [101, 30, 45, 0, 0], "1": [100, 30, 45, 0, 0]}, elapsed=1)
    metrics, status, _ = telemetry.assess_gpu([first, second], 60)
    assert status == "inconclusive"
    assert metrics["devices"][1]["freshness"] == "unknown"


@pytest.mark.parametrize("xid,throttle", [(79, 0), (0, 8), (0, 32), (0, 64), (0, 128)])
def test_gpu_frozen_adverse_values_fail(xid, throttle):
    snap = snapshot({"0": [80000, 0, 99, throttle, xid]})
    before = snapshot({"0": [80000, 0, 99, throttle, 0]})
    metrics, status, summary = telemetry.assess_gpu([before, snap], 60)
    assert status == "fail"
    assert metrics["alerts"][0]["value"] == (xid or throttle)
    assert "not device health certification" in summary


def test_gpu_idle_clock_reason_is_not_thermal_or_power_throttling():
    snap = snapshot({"0": [100, 0, 45, 1, 0]}, timestamp=time.time() * 1000)
    assert telemetry.assess_gpu([snap], 60)[1] == "pass"


def test_gpu_stale_timestamps_cannot_be_revived_by_value_changes():
    timestamp = (time.time() - 300) * 1000
    metrics, status, _ = telemetry.assess_gpu(
        [snapshot(timestamp=timestamp),
         snapshot({"0": [101, 30, 45, 0, 0]}, timestamp=timestamp, elapsed=1)], 60)
    assert status == "inconclusive"
    assert len(metrics["unavailable_families"]) == 5


def test_gpu_explicit_freshness_limit_is_applied_at_assessment():
    snap = snapshot(timestamp=(time.time() - 30) * 1000)
    assert telemetry.assess_gpu([snap], 60)[1] == "pass"
    assert telemetry.assess_gpu([snap], 5)[1] == "inconclusive"


def test_gpu_unidentified_device_does_not_claim_per_device_coverage():
    snap = snapshot(timestamp=time.time() * 1000)
    for sample in snap["samples"]:
        sample["labels"] = {}
    assert telemetry.assess_gpu([snap], 60)[1] == "inconclusive"


@pytest.mark.parametrize("reason", [1, 2, 4, 16, 1 | 2 | 4 | 16])
def test_gpu_benign_clock_reasons_are_informational(reason):
    snap = snapshot({"0": [100, 30, 45, reason, 0]}, timestamp=time.time()*1000)
    metrics, status, _ = telemetry.assess_gpu([snap, snap], 60)
    assert status == "pass" and not metrics["alerts"]
    assert metrics["informational_clock_events"]


def test_gpu_constant_preexisting_xid_is_historical():
    snap = snapshot({"0": [100, 30, 45, 0, 79]}, timestamp=time.time()*1000)
    metrics, status, _ = telemetry.assess_gpu([snap, snap], 60)
    assert status == "pass" and not metrics["alerts"]
    assert metrics["warnings"][0]["historical_xid"] == 79


def test_gpu_one_adverse_throttle_snapshot_does_not_fail():
    snap = snapshot({"0": [100, 30, 45, 8, 0]}, timestamp=time.time()*1000)
    assert telemetry.assess_gpu([snap], 60)[1] == "pass"


def test_gpu_xid_after_driver_reset_is_a_new_event():
    snaps = [snapshot({'0': [100, 30, 45, 0, xid]}, timestamp=time.time()*1000)
             for xid in (79, 0, 79)]
    assert telemetry.assess_gpu(snaps, 60)[1] == 'fail'


def combined_snapshot(*, elapsed=0, engines=8, devices=8, limit=128, spec=True, xid=0, order="spec_first"):
    """A combined endpoint with ordinary serving, optional SD, and DCGM samples."""
    serving = [f'{name}{{engine="{engine}"}} {value}'
               for engine in range(engines)
               for name, value in (("vllm:kv_cache_usage_perc", .5), ("vllm:num_requests_running", 1),
                                   ("vllm:num_requests_waiting", 0), ("vllm:num_preemptions_total", 0),
                                   ("vllm:prefix_cache_hits_total", elapsed),
                                   ("vllm:prefix_cache_queries_total", elapsed * 2))]
    if engines:
        serving += [f"vllm:time_to_first_token_seconds_sum {elapsed}",
                    f"vllm:time_to_first_token_seconds_count {elapsed * 2}"]
    optional = [f'{name}{{engine="{engine}"{position}}} {elapsed * value}'
                for engine in range(max(engines, 1))
                for name, position, value in [
                    ("vllm:spec_decode_num_drafts_total", "", 1),
                    ("vllm:spec_decode_num_draft_tokens_total", "", 3),
                    ("vllm:spec_decode_num_accepted_tokens_total", "", 2),
                    *(('vllm:spec_decode_num_accepted_tokens_per_pos_total', f',position="{pos}"', int(pos < 2))
                      for pos in range(3))]] if spec else []
    gpu = [f'{name}{{gpu="{device}"}} {value} {time.time() * 1000}'
           for device in range(devices)
           for name, value in zip(NAMES, [100, 30, 45, 0, xid if device == devices - 1 else 0])]
    parts = {"spec_first": [optional, serving, gpu], "spec_last": [serving, gpu, optional],
             "gpu_first": [gpu, optional, serving]}[order]
    target = Target(name="test", model="model", url="http://localhost:1234", metrics_profile="vllm-v1")
    config = RunConfig(targets=[target], checks=["gpu"], metrics_max_series=limit,
                       gpu_min_observation_seconds=0)
    snap = telemetry.parse("\n".join(line for part in parts for line in part), target, config)
    return snap | {"elapsed_seconds": elapsed, "collected_at": telemetry.stamp()}, config


def gpu_check(snapshots, config):
    """Exercise the runner's actual GPU acceptance check without making requests."""
    import asyncio

    from giraffe import runner

    run = runner._Run(config, None, asyncio.Event())
    target = config.targets[0]
    assert target.deployment.hardware.get("gpu_count") is None
    run.observations[target.name]["metrics"] = [runner._gpu_snapshot(s) for s in snapshots]
    return next(result for result in runner._checks(run, target) if result.id == "gpu")


@pytest.mark.parametrize("order", ["spec_first", "spec_last", "gpu_first"])
def test_optional_spec_series_never_displace_failing_gpu_at_default_cap(order):
    for spec in (False, True):
        first, config = combined_snapshot(spec=spec, order=order)
        last, _ = combined_snapshot(elapsed=1, spec=spec, xid=79, order=order)
        result = gpu_check([first, last], config)
        assert result.metrics["observed_gpu_count"] == 8
        assert result.status == "fail"
        assert any(a["gpu"] == "7" and a["value"] == 79 for a in result.metrics["alerts"])
        assert len(first["samples"]) <= 128


def test_optional_spec_series_preserve_complete_gpu_at_small_cap():
    for spec in (False, True):
        snap, config = combined_snapshot(engines=0, devices=1, limit=10, spec=spec)
        assert gpu_check([snap], config).status == "pass"
        assert len(snap["samples"]) <= 10


def test_actual_gpu_truncation_cannot_pass_without_declared_gpu_count():
    snap, config = combined_snapshot(engines=0, devices=2, limit=5, spec=False)
    result = gpu_check([snap], config)
    assert result.metrics["observed_gpu_count"] == 1
    assert result.status == "inconclusive"


@pytest.mark.parametrize("metadata", [None, [], "memory", [None], [""], ["bogus"], [["memory"]]])
def test_legacy_or_invalid_truncation_metadata_cannot_support_gpu_pass(metadata):
    snap = snapshot(timestamp=time.time() * 1000)
    snap["series_limit_reached"] = True
    if metadata is None:
        snap.pop("truncated_families", None)
    else:
        snap["truncated_families"] = metadata
    assert telemetry.assess_gpu([snap], 60)[1] == "inconclusive"


def test_observed_gpu_failure_still_fails_when_other_gpu_samples_are_lost():
    first, config = combined_snapshot(engines=0, devices=2, limit=5, spec=False)
    last = {**first, "samples": [dict(sample) for sample in first["samples"]]}
    last["samples"][-1]["value"] = 79
    assert gpu_check([first, last], config).status == "fail"


def test_zero_retained_gpu_samples_record_truncated_coverage():
    snap, config = combined_snapshot(engines=1, devices=1, limit=1)
    result = gpu_check([snap], config)
    assert result.status == "inconclusive"
    assert "truncated" in result.summary
    assert result.metrics["observed_gpu_count"] == 0


@pytest.mark.parametrize("loss", ["label_count", "label_value"])
def test_truncated_gpu_labels_cannot_support_gpu_pass(loss):
    target = Target(name="test", model="model", url="http://localhost:1234")
    config = RunConfig(targets=[target], checks=["gpu"])
    labels = ('gpu="0",' + ','.join(f'extra{i}="x"' for i in range(16)) if loss == "label_count"
              else 'gpu="' + '0' * 257 + '"')
    body = '\n'.join(f'{name}{{{labels}}} {value} {time.time() * 1000}'
                     for name, value in zip(NAMES, [100, 30, 45, 0, 0]))
    snap = telemetry.parse(body, target, config)
    assert set(snap["truncated_families"]) == telemetry.GPU_FAMILIES
    assert gpu_check([snap], config).status == "inconclusive"


def test_optional_only_loss_does_not_hide_new_xid_label_series_or_disappearance():
    first, config = combined_snapshot(engines=0, devices=2, limit=12)
    last, _ = combined_snapshot(engines=0, devices=2, limit=12, xid=79, elapsed=1)
    for sample in first["samples"]:
        if sample["family"] == "device_errors":
            sample["labels"]["err_code"] = "0"
    for sample in last["samples"]:
        if sample["family"] == "device_errors":
            sample["labels"]["err_code"] = str(int(sample["value"]))
    assert gpu_check([first, last], config).status == "fail"
    disappeared, _ = combined_snapshot(engines=0, devices=1, limit=10, elapsed=1)
    assert gpu_check([first, disappeared], config).status == "fail"
