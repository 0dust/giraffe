"""Measure deterministic report exports; no endpoint or socket is used."""

import argparse
from pathlib import Path
import tempfile

from giraffe.models import CheckResult, RequestRecord, RunConfig, RunReport, Target
from giraffe.reporting import write_report


def synthetic_report(requests, body_size, retention):
    config = RunConfig(targets=[Target(name="synthetic", url="http://localhost:8000", model="synthetic")],
                       retention=retention)
    records = [RequestRecord(id=f"request-{index:06d}", target="synthetic", fixture_id="synthetic-v1",
        scenario="warm", check_ids=["correctness"], started_at="2026-10-20T00:00:00Z",
        status="completed", http_status=200, valid=True, score=index % 10 != 0,
        score_message="Matched" if index % 10 else "Did not match", elapsed_ms=100,
        output="x" * body_size, output_chars=body_size, output_tokens=body_size // 4,
        input_chars=32, requested_max_tokens=4096) for index in range(requests)]
    return RunReport(run_id="synthetic", started_at="2026-10-20T00:00:00Z",
        finished_at="2026-10-20T00:00:01Z", overall="fail", manifest={"config": config.model_dump(mode="json")},
        checks=[CheckResult(id="correctness", target="synthetic", title="Known-answer correctness",
            status="fail", summary="Synthetic: every tenth answer is incorrect.",
            evidence_ids=[record.id for record in records if record.score is False])], requests=records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--body-sizes", type=int, nargs="+", default=[0, 1024, 16384])
    args = parser.parse_args()
    if args.requests < 1 or any(size < 0 for size in args.body_sizes):
        parser.error("requests must be positive and body sizes non-negative")
    print("requests body_bytes retention json_bytes html_bytes total_bytes bytes_per_request")
    with tempfile.TemporaryDirectory(prefix="giraffe-storage-measure-") as temporary:
        for body_size in args.body_sizes:
            for retention in ("all", "failures", "none"):
                report = synthetic_report(args.requests, body_size, retention)
                directory = Path(temporary) / f"{body_size}-{retention}"
                paths = write_report(report, directory, max_run_storage=0)
                sizes = [path.stat().st_size for path in paths]
                print(args.requests, body_size, retention, *sizes, sum(sizes),
                      f"{sum(sizes) / args.requests:.2f}")


if __name__ == "__main__":
    main()
