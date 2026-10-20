"""Compare deterministic previous and compact exports; no endpoint or socket is used."""

import argparse
import json
from pathlib import Path
import tempfile

from giraffe.models import CheckResult, EvaluationEvidence, RequestRecord, RunConfig, RunReport, Target
from giraffe.reporting import _atomic_write, _render, _retained, _storage_limited, write_report


def synthetic_report(requests, body_size, retention, input_size=2048, input_cases=10):
    config = RunConfig(targets=[Target(name="synthetic", url="http://localhost:8000", model="synthetic")],
                       retention=retention)
    records = [RequestRecord(id=f"request-{index:06d}", target="synthetic", fixture_id="synthetic-v1",
        scenario="warm", check_ids=["correctness"], started_at="2026-10-20T00:00:00Z",
        status="completed", http_status=200, valid=True, score=index % 10 != 0,
        score_message="Matched" if index % 10 else "Did not match", elapsed_ms=100 + index / 10,
        first_output_ms=10, last_output_ms=90, max_stream_gap_ms=5, answer_reads=16,
        first_read_answer_chars=body_size // 16, answer_chars=body_size,
        output="x" * body_size, output_chars=body_size, output_tokens=body_size // 4,
        input_chars=input_size, requested_max_tokens=4096,
        evaluation=EvaluationEvidence(messages=[{"role": "user", "content":
            f"Case {index % input_cases}: ".ljust(input_size, "p")}], scorer="exact", expected="42",
            expected_provided=True)) for index in range(requests)]
    return RunReport(run_id="synthetic", started_at="2026-10-20T00:00:00Z",
        finished_at="2026-10-20T00:00:01Z", overall="fail", manifest={"config": config.model_dump(mode="json")},
        checks=[CheckResult(id="correctness", target="synthetic", title="Known-answer correctness",
            status="fail", summary="Synthetic: every tenth answer is incorrect.",
            evidence_ids=[record.id for record in records if record.score is False])], requests=records)


def previous_artifacts(report):
    """Previous disk format: defaults, repeated inputs, pretty JSON and automatic HTML."""
    retained = _retained(report)
    _storage_limited(retained, 0)
    return retained.model_dump_json(indent=2).encode("utf-8"), _render(retained).encode("utf-8")


def measure(report, directory):
    old_json, old_html = previous_artifacts(report)
    path, html = write_report(report, directory, max_run_storage=0)
    assert html is None
    lifecycle = {"id": report.run_id, "state": "completed", "run_id": report.run_id,
                 "started_at": report.started_at, "finished_at": report.finished_at}
    old_web = len((json.dumps(lifecycle, indent=2) + "\n").encode("utf-8"))
    state_path = directory / ".web.json"
    _atomic_write(state_path, json.dumps(lifecycle, separators=(",", ":")) + "\n")
    new_web = state_path.stat().st_size
    current = path.stat().st_size + new_web
    previous = len(old_json) + len(old_html) + old_web
    return {"old_json": len(old_json), "old_html": len(old_html), "old_web": old_web,
            "new_json": path.stat().st_size, "new_web": new_web, "previous": previous,
            "current": current, "saved_percent": (previous - current) * 100 / previous}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=300)
    parser.add_argument("--body-sizes", type=int, nargs="+", default=[16384])
    args = parser.parse_args()
    if args.requests < 1 or any(size < 0 for size in args.body_sizes):
        parser.error("requests must be positive and body sizes non-negative")
    print("requests body_bytes retention old_json old_html old_web new_json new_web previous_total current_total saved_percent")
    with tempfile.TemporaryDirectory(prefix="giraffe-storage-measure-") as temporary:
        for body_size in args.body_sizes:
            for retention in ("all", "failures", "none"):
                report = synthetic_report(args.requests, body_size, retention)
                result = measure(report, Path(temporary) / f"{body_size}-{retention}")
                print(args.requests, body_size, retention, result["old_json"], result["old_html"], result["old_web"], result["new_json"], result["new_web"],
                      result["previous"], result["current"], f"{result['saved_percent']:.2f}%")


if __name__ == "__main__":
    main()
