"""Measure candidate arrival counts with the real 1,000/s scheduler, without sockets.

The proposed 100,000 count is installed with model_copy solely for this experiment
so the current schema need not accept it. Every request uses runner._Run.request,
LLMClient and real SSE parsing through HTTPX MockTransport. Ten percent of answers
are deliberately incorrect; every answer includes distinct retained reasoning.
Exports measure actual all/failures/none artifacts, not extrapolations.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
from pathlib import Path
import tempfile
import time

from giraffe.caps import MAX_LIVE_CONDITIONS, MAX_LIVE_REQUESTS
from giraffe.models import Arrival, CheckResult, RunConfig, RunReport, Target
from giraffe.reporting import write_report
from giraffe.report_stream import _Reader
from giraffe.resources import PhaseSampler, current_rss_bytes, peak_rss_bytes
from giraffe.runner import _Run
from giraffe.storage import DEFAULT_LIMITS
from giraffe.workloads import arrivals

from _measurement import FakeEndpoint, LiveJob, distribution, environment, mock_client


def storage_metadata(path):
    """Read only the report prefix; loading 100k records would distort export RSS."""
    with path.open() as handle:
        reader = _Reader(handle)
        for key in reader.members():
            value = reader.value()
            if key == "manifest":
                return value.get("storage", {})
            if key == "requests":
                raise RuntimeError("Report field order changed; manifest must precede requests")
    return {}


async def measure(args, count):
    target = Target(name="measurement", url="http://measurement.invalid/v1", model="fake-stream")
    config = RunConfig(targets=[target], checks=["arrivals"], default_concurrency=args.in_flight,
                       arrivals=Arrival(requests=2, requests_per_second=args.rate,
                                        max_in_flight=args.in_flight, max_pending=256),
                       stop_after_errors=count + 1, request_timeout_seconds=30)
    config = config.model_copy(update={"arrivals": config.arrivals.model_copy(update={"requests": count})})
    live = LiveJob()
    run = _Run(config, live.progress, None)
    endpoint = FakeEndpoint(chunks=args.chunks, delay=args.chunk_interval_ms / 1000,
                            body_bytes=args.body_bytes, arrivals=True)
    client = mock_client(target, config, endpoint, args.in_flight)
    client.on_response = lambda *_: run.endpoint_response(target.name)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with PhaseSampler("traffic", interval=.05) as sampler:
        run.rss_at_start_bytes = sampler.initial_rss_bytes
        sampler.on_sample = run.memory_sample
        started, cpu = time.monotonic(), time.process_time()
        task = asyncio.create_task(arrivals(run, target, client, {}))
        monitor = asyncio.create_task(run.monitor([task]))
        try:
            await task
            await monitor
        finally:
            await client.__aexit__(None, None, None)
        wall, cpu_elapsed = time.monotonic() - started, time.process_time() - cpu
        sampler.sample()
        traffic_rss = current_rss_bytes()
        sampler.detach()
        observation = run.observations[target.name]["arrivals"]
        waits = [r.client_wait_ms for r in run.records if r.client_wait_ms is not None]
        failures = [r.id for r in run.records if r.score is False]
        report = RunReport(run_id=f"measure-arrivals-{count}", started_at=started_at,
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), overall="fail",
            manifest={"config": config.model_dump(mode="json")}, requests=run.records,
            checks=[CheckResult(id="arrivals", target=target.name, title="Arrivals measurement",
                                status="fail", summary="Synthetic 10% answer failures.", evidence_ids=failures)],
            observations={target.name: run.observations[target.name],
                          "client_timing_health": run.timing_health.snapshot()})
        exports = []
        with tempfile.TemporaryDirectory(prefix="giraffe-arrivals-measure-") as temporary:
            for retention in ("all", "failures", "none"):
                phase = f"export_{retention}"
                sampler.set_phase(phase)
                report.manifest["config"]["retention"] = retention
                directory = Path(temporary) / retention
                export_start = time.monotonic()
                path, _ = write_report(report, directory, max_run_storage=DEFAULT_LIMITS["max_run_storage"])
                sampler.sample()
                exports.append({"retention": retention, "report_json_bytes": path.stat().st_size,
                    "directory_bytes": sum(p.stat().st_size for p in directory.iterdir() if p.is_file()),
                    "storage": storage_metadata(path), "export_seconds": time.monotonic() - export_start,
                    "sampled_peak_rss_bytes": sampler.peaks[phase]})
                gc.collect()
        rss_peaks = dict(sampler.peaks)
        baseline_rss = sampler.initial_rss_bytes
    max_rss = max((v for v in rss_peaks.values() if v is not None), default=None)
    growth = max(0, max_rss - baseline_rss) if max_rss is not None and baseline_rss is not None else None
    lateness = distribution(waits)
    live_snapshot = live.snapshot()
    gates = {
        "all_requests_completed": len(run.records) == count and all(r.status == "completed" for r in run.records),
        "no_local_drops": not observation["unissued"],
        "lateness_within_50ms": lateness["max"] is not None and lateness["max"] <= 50,
        "rss_growth_below_1GiB": growth is not None and growth < 1024**3,
        "exports_fit_256MiB": all(row["report_json_bytes"] <= DEFAULT_LIMITS["max_run_storage"]
                                     and not row["storage"].get("limit_exceeded") for row in exports),
        "live_request_window_bounded": live_snapshot["container_peaks"]["requests"] <= MAX_LIVE_REQUESTS,
        "live_conditions_bounded": live_snapshot["container_peaks"]["conditions"] <= MAX_LIVE_CONDITIONS,
        "live_running_evictions_drained": not live_snapshot["container_final"]["evicted_running"],
    }
    return {"requested": count, "offered_rate_rps": args.rate, "in_flight_limit": args.in_flight,
            "issued": len(run.records), "completed": sum(r.status == "completed" for r in run.records),
            "score_failures": len(failures), "endpoint_peak": endpoint.peak,
            "wall_seconds": wall, "cpu_seconds": cpu_elapsed,
            "cpu_ms_per_request": cpu_elapsed * 1000 / len(run.records) if run.records else None,
            "scheduler_lateness_ms": lateness,
            "scheduler_lateness_definition": "RequestRecord.client_wait_ms: actual runner dispatch minus independent scheduled arrival",
            "timing_health": run.timing_health.snapshot(), "arrivals_observation": observation,
            "rss_baseline_bytes": baseline_rss, "rss_after_traffic_bytes": traffic_rss,
            "sampled_rss_peaks_bytes": rss_peaks, "sampled_peak_growth_bytes": growth,
            "process_peak_rss_bytes": peak_rss_bytes(), "rss_sample_interval_seconds": .05,
            "live_web": live_snapshot, "exports": exports, "gates": gates,
            "candidate_passes_all_gates": all(gates.values())}


async def main_async(args):
    return {"environment": environment(), "parameters": vars(args),
            "gate_definitions": {"lateness_ms": 50, "well_under_4GiB": "sampled RSS growth below 1 GiB",
                                 "storage_limit_bytes": DEFAULT_LIMITS["max_run_storage"]},
            "limitations": ["MockTransport excludes socket/TLS/connection-pool overhead.",
                "RSS is sampled every 50 ms; unobserved spikes can occur between samples.",
                "Memory/storage depends on actual answer and input sizes; this is a 1 KiB reasoning workload by default.",
                "Web container test runs the production web progress tracker; no HTTP server is bound.",
                "Accepted ceiling is not demonstrated delivery capacity."],
            "measurements": [await measure(args, count) for count in args.requests]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, nargs="+", default=[10000, 100000])
    parser.add_argument("--rate", type=float, default=1000)
    parser.add_argument("--in-flight", type=int, default=256)
    parser.add_argument("--body-bytes", type=int, default=1024)
    parser.add_argument("--chunks", type=int, default=4)
    parser.add_argument("--chunk-interval-ms", type=float, default=5)
    parser.add_argument("--output")
    args = parser.parse_args()
    if any(n < 2 for n in args.requests) or min(args.rate, args.in_flight, args.body_bytes,
                                                args.chunks, args.chunk_interval_ms) <= 0:
        parser.error("counts must be >=2; rate, in-flight, body size, chunks and interval must be positive")
    text = json.dumps(asyncio.run(main_async(args)), indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
