"""Compare legacy and streamed exports in fresh processes; no endpoint or socket is used."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from giraffe.deployment import safe_config, sanitize
from giraffe.models import RequestRecord, RunReport
from giraffe.reporting import _atomic_write, _render, _without_answer_excerpts, write_report
from giraffe.resources import current_rss_bytes, peak_rss_bytes
from measure_storage import synthetic_report


def _old_retained(report: RunReport) -> RunReport:
    result = report.model_copy(deep=True)
    result.manifest["config"] = safe_config(result.manifest.get("config", {}))
    if result.baseline:
        result.baseline = sanitize(result.baseline, bounded=False)
    policy = result.manifest.get("config", {}).get("retention", "failures")
    if policy == "none":
        for check in result.checks:
            check.summary = _without_answer_excerpts(check.summary)
            for scenario in check.metrics.get("scenarios", {}).values():
                scenario["summary"] = _without_answer_excerpts(scenario["summary"])
    failed_evidence = {rid for check in result.checks if check.status in {"fail", "blocked"}
                       for rid in check.evidence_ids}
    for record in result.requests:
        keep = policy == "all" or (policy == "failures" and (
            (record.status in {"failed", "timeout"} and record.scenario not in {"client_cancel", "client_deadline"})
            or (record.status == "completed" and not record.valid) or record.score is False
            or record.evaluation_error is not None or record.id in failed_evidence
        ))
        if not keep:
            _old_omit_response(record)
    return result


def _old_omit_response(record: RequestRecord) -> None:
    if record.score_message:
        record.score_message = _without_answer_excerpts(record.score_message)
    record.output = ""
    record.reasoning = ""
    record.tool_calls = []
    if record.logprob_evidence:
        record.logprob_evidence.tokens = []
    record.assistant_message = None
    if record.tool_conversation:
        record.tool_conversation["final_content"] = None
        for call in record.tool_conversation["calls"]:
            call["result"] = None
        if record.evaluation:
            record.evaluation.messages = []
    if record.judgement:
        for vote in record.judgement.votes:
            vote.reason = None


def _has_response(record: RequestRecord) -> bool:
    return bool(record.output or record.reasoning or record.tool_calls or record.assistant_message
                or (record.logprob_evidence and record.logprob_evidence.tokens)
                or (record.judgement and any(v.reason for v in record.judgement.votes))
                or (record.tool_conversation and (record.tool_conversation.get("final_content")
                    or any(call.get("result") is not None
                           for call in record.tool_conversation.get("calls", [])))))


def _old_storage_limited(report: RunReport, limit: int) -> str:
    storage = {"limit_bytes": limit, "response_bodies_omitted": 0,
               "reason": "Per-run JSON storage limit on disk.", "limit_exceeded": False}
    report.manifest["storage"] = storage
    contents = report.model_dump_json(indent=2)
    if not limit or len(contents.encode("utf-8")) <= limit:
        return contents
    evidence = {rid for check in report.checks if check.status in {"fail", "blocked", "inconclusive"}
                for rid in check.evidence_ids}
    ordered = sorted((r for r in report.requests if _has_response(r)), key=lambda r: bool(
        r.id in evidence or r.score is False or r.evaluation_error
        or (r.status in {"failed", "timeout"} and r.scenario not in {"client_cancel", "client_deadline"})
        or (r.status == "completed" and not r.valid)))
    previous_reason = storage["reason"]
    storage["reason"] = "Response bodies omitted to fit the per-run JSON limit on disk."
    size = len(contents.encode("utf-8")) + len(storage["reason"]) - len(previous_reason)
    for record in ordered:
        before = record.model_dump_json(indent=2)
        _old_omit_response(record)
        after = record.model_dump_json(indent=2)
        size -= len(before.encode("utf-8")) - len(after.encode("utf-8"))
        size -= 4 * (before.count("\n") - after.count("\n"))
        previous_count = storage["response_bodies_omitted"]
        storage["response_bodies_omitted"] += 1
        size += len(str(storage["response_bodies_omitted"])) - len(str(previous_count))
        if size <= limit:
            contents = report.model_dump_json(indent=2)
            size = len(contents.encode("utf-8"))
            if size <= limit:
                return contents
    contents = report.model_dump_json(indent=2)
    if len(contents.encode("utf-8")) > limit:
        storage["limit_exceeded"] = True
        storage["reason"] = ("Request metadata exceeds the per-run JSON limit on disk; "
                             "all request metadata was preserved.")
        contents = report.model_dump_json(indent=2)
    return contents



def measure(requests, body_size, approach, retention):
    report = synthetic_report(requests, body_size, retention)
    for index, record in enumerate(report.requests):
        record.output = (str(index).zfill(12) + "x" * body_size)[:body_size]
    holding = current_rss_bytes()
    holding_peak = peak_rss_bytes()
    samples = [holding] if holding is not None else []
    done = threading.Event()

    def sample():
        while not done.wait(.005):
            value = current_rss_bytes()
            if value is not None:
                samples.append(value)

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="giraffe-memory-measure-") as temporary:
            directory = Path(temporary)
            html = approach.endswith("html")
            if approach.startswith("old"):
                retained = _old_retained(report)
                retained.schema_version = "2"
                _atomic_write(directory / "report.json", _old_storage_limited(retained, 0))
                if html:
                    _atomic_write(directory / "report.html", _render(retained))
            else:
                write_report(report, directory, max_run_storage=0, html=html)
            artifact_files = sorted(path.name for path in directory.iterdir())
            json_bytes = (directory / "report.json").stat().st_size
            html_bytes = (directory / "report.html").stat().st_size if html else 0
            end = current_rss_bytes()
            if end is not None:
                samples.append(end)
    finally:
        done.set()
        thread.join()
    return {"requests": requests, "body_bytes": body_size, "retention": retention,
            "approach": approach, "artifact_files": artifact_files,
            "json_bytes": json_bytes, "html_bytes": html_bytes, "holding_rss_bytes": holding,
            "holding_process_peak_bytes": holding_peak,
            "export_sampled_peak_bytes": max(samples, default=None),
            "process_peak_bytes": peak_rss_bytes(), "seconds": round(time.monotonic() - started, 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, nargs="+", default=[1000, 10000])
    parser.add_argument("--body-sizes", type=int, nargs="+", default=[1024, 16384])
    parser.add_argument("--retention", choices=["all", "failures", "none"], default="all")
    parser.add_argument("--child", choices=["old-json-html", "new-json-html", "old-json", "new-json"])
    args = parser.parse_args()
    if any(n < 1 for n in args.requests) or any(n < 0 for n in args.body_sizes):
        parser.error("requests must be positive and body sizes non-negative")
    if args.child:
        print(json.dumps(measure(args.requests[0], args.body_sizes[0], args.child, args.retention)))
        return
    print("requests body_bytes retention approach json_bytes html_bytes holding_MiB holding_process_peak_MiB sampled_export_peak_MiB process_peak_MiB seconds")
    for requests in args.requests:
        for body_size in args.body_sizes:
            for approach in ("old-json-html", "new-json-html", "old-json", "new-json"):
                result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                    "--requests", str(requests), "--body-sizes", str(body_size),
                    "--retention", args.retention, "--child", approach],
                    check=True, capture_output=True, text=True)
                measured = json.loads(result.stdout)
                sizes = ["unavailable" if measured[key] is None else f"{measured[key] / 1024**2:.1f}"
                         for key in ("holding_rss_bytes", "holding_process_peak_bytes", "export_sampled_peak_bytes", "process_peak_bytes")]
                print(requests, body_size, args.retention, approach, measured["json_bytes"], measured["html_bytes"], *sizes, measured["seconds"], flush=True)


if __name__ == "__main__":
    main()
