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
