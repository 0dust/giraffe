"""Offline, identity-preserving verification of independent case evidence.

Endpoint application writes remain application observations. Their timestamps never
become packet or client-read observations by arithmetic or by a generous tolerance.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from validation_suite.reporting import empty_report, metric, set_metric, write_report

MAX_ARTIFACT_ROWS = 200_000
MAX_LINE_BYTES = 2 * 1024**2
# Bounds describe working memory and scratch storage, not a total raw-input cap.
MAX_COMPACT_BYTES = 128 * 1024**2
MAX_ATTEMPT_BYTES = 16 * 1024**2
MAX_IDENTITIES = 200_000
MAX_SQLITE_BYTES = 2 * 1024**3
MAX_RECONCILIATION_ISSUES = 100_000
MAX_ISSUE_DETAIL_BYTES = 2048
MAX_TIMING_SAMPLES = 4_000_000


def verification_limits() -> dict[str, int | str]:
    return {"backend": "sqlite_file_offsets_and_per_attempt_checks", "max_line_bytes": MAX_LINE_BYTES,
            "max_compact_json_bytes": MAX_COMPACT_BYTES, "max_attempt_bytes": MAX_ATTEMPT_BYTES,
            "max_distinct_attempt_ids": MAX_IDENTITIES, "sqlite_cache_bytes": 8 * 1024**2,
            "max_sqlite_bytes": MAX_SQLITE_BYTES, "minimum_scratch_free_bytes": 256 * 1024**2,
            "max_reconciliation_issues": MAX_RECONCILIATION_ISSUES, "max_issue_detail_bytes": MAX_ISSUE_DETAIL_BYTES,
            "max_timing_distribution_samples": MAX_TIMING_SAMPLES}


class EvidenceLimitError(ValueError):
    """An explicit working-set or scratch bound prevented complete interpretation."""


def _strict_json(data):
    def invalid_constant(value):
        raise ValueError(f"Non-finite JSON number: {value}")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("JSON number is outside the finite floating-point range")
        return number

    try:
        return json.loads(data, parse_constant=invalid_constant, parse_float=finite_float)
    except RecursionError as error:
        raise ValueError("JSON nesting exceeds the parser bound") from error


def bounded_jsonl_rows(path: str | Path, limits: dict[str, Any] | None = None):
    """Yield one JSON object at a time; reject malformed or over-bound records."""
    path = Path(path)
    maximum = (limits or verification_limits())["max_line_bytes"]
    with path.open("rb") as stream:
        number = 0
        while line := stream.readline(maximum + 1):
            number += 1
            if len(line) > maximum:
                raise EvidenceLimitError(f"{path.name}:{number}: JSONL record exceeds {maximum} bytes")
            if line.strip():
                value = _strict_json(line)
                if not isinstance(value, dict):
                    raise ValueError(f"{path.name}:{number}: JSONL record is not an object")
                yield value


def _compact_row(row: dict[str, Any], source: str) -> dict[str, Any] | None:
    """Payload checks use original file offsets; metrics retain only small summaries."""
    common = {name: row[name] for name in ("kind", "event_kind", "phase", "population", "attempt_id",
              "monotonic_ns", "clock_domain", "source") if name in row}
    if not row.get("attempt_id") and row.get("logical_request_id"):
        common["logical_request_id"] = row["logical_request_id"]
    if source == "receiver-events":
        kind, metadata = _kind(row), _metadata(row)
        if kind not in {"request_received", "response_event", "response_finished", "response_prepared"}:
            return None
        if kind == "response_event":
            return None  # Content/sequence checks and per-attempt timing aggregates use original offsets.
        if kind == "response_prepared" and metadata.get("expected", {}).get("fixture") not in {
                "http_error", "stream_error_before", "stream_error_after", "malformed_stream",
                "missing_termination", "truncated", "hang", "disconnect"}:
            return None
        common = {name: row[name] for name in ("kind", "event_kind", "attempt_id", "monotonic_ns", "clock_domain") if name in row}
        if not row.get("attempt_id") and row.get("logical_request_id"):
            common["logical_request_id"] = row["logical_request_id"]
        if kind == "response_finished":
            common.update({name: row[name] for name in ("phase", "population") if name in row})
        common["metadata"] = {name: metadata[name] for name in ("response_id", "outcome", "population") if name in metadata}
        for name in ("content", "reasoning"):
            if metadata.get(name):
                common["metadata"][name] = True
        if kind == "response_prepared":
            common["metadata"]["expected"] = {"fixture": metadata.get("expected", {}).get("fixture")}
        return common
    if source == "results":
        for name in ("kind", "event_kind", "monotonic_ns", "source"):
            common.pop(name, None)
        common.update({name: row[name] for name in ("id", "response_id", "fixture_id",
                      "status", "outcome", "valid", "score", "prompt_tokens", "input_tokens", "completion_tokens",
                      "output_tokens", "timing", "error", "error_type", "exception_type") if name in row})
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        common["raw"] = {name: raw[name] for name in ("id", "scenario", "output_tokens_source", "error", "error_type") if name in raw
                         and (name not in {"error", "error_type"} or raw[name] is not None)}
        if "timing" in common:
            common["timing"] = {name: value for name, value in common["timing"].items() if value is not None}
        return common
    if source == "planned":
        common.pop("monotonic_ns", None)
        common.pop("source", None)
        if _kind(row) != "sustained_admission_intent":
            common.pop("kind", None)
            common.pop("event_kind", None)
        common.update({name: row[name] for name in ("fixture_id", "scheduled_offset_ms") if name in row})
        expected = row.get("expected", {})
        common["expected"] = {name: value for name, value in expected.items() if name not in {"content", "event_contents", "request_marker", "prompt"}}
        if isinstance(expected.get("content"), str):
            if len(expected["content"].encode()) <= 64:
                common["expected"]["content"] = expected["content"]
            else:
                common["expected"]["content_sha256"] = hashlib.sha256(expected["content"].encode()).hexdigest()
        return common
    common.update({name: row[name] for name in ("fault", "reason", "mechanism") if name in row})
    if row.get("metadata"):
        common["metadata"] = {name: row["metadata"][name] for name in
                              ("population", "reason", "fault", "protocol_complete", "content") if name in row["metadata"]}
    return common


class _StoredRow(dict):
    pass


class _SqlRows:
    def __init__(self, store, source: str):
        self.store, self.source = store, source

    def __len__(self):
        return self.store.connection.execute("SELECT count(*) FROM records WHERE source=?", (self.source,)).fetchone()[0]

    def __iter__(self):
        with (self.store.directory / f"{self.source}.jsonl").open("rb") as stream:
            for rowid, offset, length in self.store.connection.execute("SELECT id, offset, length FROM records WHERE source=? ORDER BY ordinal", (self.source,)):
                stream.seek(offset)
                row = _StoredRow(_strict_json(stream.read(length)))
                row.rowid = rowid
                yield row


class _SqlGroup:
    def __init__(self, store, bucket: str, identity: str):
        self.store, self.bucket, self.identity = store, bucket, identity

    def append(self, row):
        self.store.connection.execute("INSERT INTO groups VALUES(?,?,?)", (self.bucket, self.identity, row.rowid))
        if self.store.connection.total_changes % 1000 == 0:
            self.store.check_bounds()

    def __len__(self):
        return self.store.connection.execute("SELECT count(*) FROM groups WHERE bucket=? AND identity=?", (self.bucket, self.identity)).fetchone()[0]

    def __iter__(self):
        query = "SELECT r.source,r.offset,r.length FROM groups g JOIN records r ON r.id=g.rowid WHERE g.bucket=? AND g.identity=? ORDER BY r.ordinal"
        for source, offset, length in self.store.connection.execute(query, (self.bucket, self.identity)):
            stream = self.store.files[source]
            stream.seek(offset)
            yield _strict_json(stream.read(length))

    def __getitem__(self, index):
        if index != 0:
            raise IndexError(index)
        return next(iter(self))


class _SqlGroups:
    def __init__(self, store, bucket):
        self.store, self.bucket = store, bucket

    def __getitem__(self, identity):
        return _SqlGroup(self.store, self.bucket, identity)

    def __iter__(self):
        return (row[0] for row in self.store.connection.execute("SELECT DISTINCT identity FROM groups WHERE bucket=?", (self.bucket,)))


class _EvidenceStore:
    """SQLite indexes original byte offsets; full response bodies never accumulate."""
    def __init__(self, directory: Path):
        self.directory = directory
        self.temporary = tempfile.TemporaryDirectory(prefix="giraffe-verify-")
        self.connection = sqlite3.connect(str(Path(self.temporary.name) / "evidence.sqlite"))
        self.connection.executescript("PRAGMA cache_size=-8192; PRAGMA temp_store=FILE; PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; CREATE TABLE records(id INTEGER PRIMARY KEY,source TEXT,ordinal INTEGER,offset INTEGER,length INTEGER,compact TEXT); CREATE INDEX source_order ON records(source,ordinal); CREATE TABLE groups(bucket TEXT,identity TEXT,rowid INTEGER); CREATE INDEX attempt_group ON groups(bucket,identity); CREATE TABLE attempts(identity TEXT PRIMARY KEY,bytes INTEGER);")
        page_size = self.connection.execute("PRAGMA page_size").fetchone()[0]
        self.connection.execute(f"PRAGMA max_page_count={max(1, MAX_SQLITE_BYTES // page_size)}")
        self.files, self.errors, self.compact = {}, [], {}
        self.application_timings, self.application_by_response_id = {}, {}
        self.limited, self.compact_bytes, self.peak_attempt_bytes = False, 0, 0
        self.rows = Counter()

    def close(self):
        for stream in self.files.values():
            stream.close()
        self.connection.close()
        self.temporary.cleanup()

    def check_bounds(self):
        pages = self.connection.execute("PRAGMA page_count").fetchone()[0]
        if pages * self.connection.execute("PRAGMA page_size").fetchone()[0] > MAX_SQLITE_BYTES:
            raise EvidenceLimitError("verifier safety limit: scratch database exceeds disk allowance")
        if shutil.disk_usage(self.temporary.name).free < 256 * 1024**2:
            raise EvidenceLimitError("verifier safety limit: insufficient scratch disk headroom")
        if self.connection.execute("SELECT count(*) FROM attempts").fetchone()[0] > MAX_IDENTITIES:
            raise EvidenceLimitError("verifier safety limit: attempt identity working set exceeds allowance")

    def load(self):
        self.check_bounds()
        for source in ("planned", "client-events", "receiver-events", "results"):
            path = self.directory / f"{source}.jsonl"
            self.compact[source] = []
            if not path.exists():
                self.errors.append(f"missing artifact: {path.name}")
                continue
            self.files[source] = path.open("rb")
            with path.open("rb") as stream:
                ordinal = 0
                while True:
                    offset = stream.tell()
                    line = stream.readline(MAX_LINE_BYTES + 1)
                    if not line:
                        break
                    ordinal += 1
                    if len(line) > MAX_LINE_BYTES:
                        raise EvidenceLimitError(f"verifier safety limit: {path.name}:{ordinal} exceeds line bound")
                    if not line.strip():
                        continue
                    try:
                        row = _strict_json(line)
                        if not isinstance(row, dict):
                            raise ValueError("row must be an object")
                        for field in ("metadata", "raw", "timing", "expected"):
                            if field in row and not isinstance(row[field], dict) and not (field in {"metadata", "raw"} and row[field] is None):
                                raise ValueError(f"{field} has an invalid object type")
                        for field in ("content", "reasoning"):
                            if _metadata(row).get(field) is not None and not isinstance(_metadata(row)[field], str):
                                raise ValueError(f"metadata.{field} must be a string or null")
                        for field in ("attempt_id", "logical_request_id", "response_id"):
                            if row.get(field) is not None and not isinstance(row[field], str):
                                raise ValueError(f"{field} must be a string or null")
                        if _metadata(row).get("response_id") is not None and not isinstance(_metadata(row)["response_id"], str):
                            raise ValueError("metadata.response_id must be a string or null")
                    except (ValueError, UnicodeDecodeError) as error:
                        if len(self.errors) < 1000:
                            self.errors.append(f"{path.name}:{ordinal}: {error}")
                        continue
                    identity = _id(row)
                    if identity:
                        self.connection.execute("INSERT INTO attempts VALUES(?,?) ON CONFLICT(identity) DO UPDATE SET bytes=bytes+excluded.bytes", (identity, len(line)))
                        size = self.connection.execute("SELECT bytes FROM attempts WHERE identity=?", (identity,)).fetchone()[0]
                        self.peak_attempt_bytes = max(self.peak_attempt_bytes, size)
                        if size > MAX_ATTEMPT_BYTES:
                            raise EvidenceLimitError("verifier safety limit: one attempt exceeds bounded payload allowance")
                    compact = _compact_row(row, source)
                    encoded = json.dumps(compact, separators=(",", ":"), allow_nan=False) if compact is not None else None
                    self.compact_bytes += len(encoded.encode()) if encoded else 0
                    if self.compact_bytes > MAX_COMPACT_BYTES:
                        raise EvidenceLimitError("verifier safety limit: compact metadata exceeds working-set allowance")
                    if compact is not None:
                        self.compact[source].append(compact)
                    self.connection.execute("INSERT INTO records(source,ordinal,offset,length,compact) VALUES(?,?,?,?,?)", (source, ordinal, offset, len(line), None))
                    self.rows[source] += 1
                    if ordinal % 1000 == 0:
                        self.connection.commit()
                        self.check_bounds()
            self.connection.commit()
            self.check_bounds()
        return {source: _SqlRows(self, source) if source in self.files else [] for source in self.compact}

    def groups(self, bucket):
        return _SqlGroups(self, bucket)

    def add_application_timing(self, identity, receipt, event_rows, terminal_rows):
        if not isinstance(receipt.get("monotonic_ns"), int):
            return
        clock, received = receipt.get("clock_domain"), receipt["monotonic_ns"]
        answer_first = reasoning_first = previous_answer = largest_gap = terminal_last = None
        for row in event_rows:
            timestamp, metadata = row.get("monotonic_ns"), _metadata(row)
            if row.get("clock_domain") != clock or not isinstance(timestamp, int):
                continue
            if metadata.get("content"):
                answer_first = timestamp if answer_first is None else min(answer_first, timestamp)
                if previous_answer is not None:
                    gap = timestamp-previous_answer
                    largest_gap = gap if largest_gap is None else max(largest_gap, gap)
                previous_answer = timestamp
            if metadata.get("reasoning"):
                reasoning_first = timestamp if reasoning_first is None else min(reasoning_first, timestamp)
        for row in terminal_rows:
            timestamp = row.get("monotonic_ns")
            if row.get("clock_domain") == clock and isinstance(timestamp, int):
                terminal_last = timestamp if terminal_last is None else max(terminal_last, timestamp)
        vectors = {"first_answer_ms": (answer_first-received)/1e6 if answer_first is not None else None,
                   "first_reasoning_ms": (reasoning_first-received)/1e6 if reasoning_first is not None else None,
                   "latency_ms": (terminal_last-received)/1e6 if terminal_last is not None else None,
                   "max_stream_gap_ms": largest_gap/1e6 if largest_gap is not None else None}
        self.compact_bytes += len(json.dumps({str(identity): vectors}).encode())
        if self.compact_bytes > MAX_COMPACT_BYTES:
            raise EvidenceLimitError("verifier safety limit: compact timing aggregates exceed working-set allowance")
        self.application_timings[identity] = vectors
        response_id = _metadata(receipt).get("response_id")
        if response_id:
            self.application_by_response_id[response_id] = vectors

    def statistics(self):
        return {**verification_limits(), "raw_rows": dict(self.rows), "compact_json_bytes": self.compact_bytes,
                "maximum_attempt_bytes": self.peak_attempt_bytes, "complete": not self.limited, "artifact_error_count": len(self.errors),
                "scratch_bytes": self.connection.execute("PRAGMA page_count").fetchone()[0] * self.connection.execute("PRAGMA page_size").fetchone()[0]}


UNCERTAIN_ISSUES = {"receipt_identity_unknown", "receipt_correlation_unknown", "result_identity_unknown",
                    "artifact_incomplete", "timing_missing", "not_dispatched_unconfirmed", "planned_slot_correlation_unknown"}


def percentile(values: Iterable[float], percent: float) -> float | None:
    """Nearest-rank quantile; no interpolation or averaging of worker quantiles."""
    samples = sorted(values)
    if not 0 <= percent <= 100:
        raise ValueError("percentile outside [0, 100]")
    if not samples:
        return None
    return samples[max(0, math.ceil(percent / 100 * len(samples)) - 1)]


def distribution(values: Iterable[float], *, unit: str = "ms",
                 qualified_count: int | None = None) -> dict[str, Any]:
    samples = sorted(values)
    if not all(math.isfinite(value) for value in samples):
        raise ValueError("non-finite measurement")
    n = len(samples)
    qualified = n if qualified_count is None else qualified_count
    if not 0 <= qualified <= n:
        raise ValueError("qualified sample count outside population")
    # Median is the middle pair average. Tail percentiles use nearest rank.
    median = (samples[(n - 1) // 2] + samples[n // 2]) / 2 if n else None
    return {
        "unit": unit, "sample_count": n, "min": samples[0] if n else None,
        "median": median, "p95": percentile(samples, 95), "p99": percentile(samples, 99),
        "max": samples[-1] if n else None,
        "percentile_method": "nearest_rank; median=middle_pair_average",
        "qualification_coverage": {"qualified_count": qualified, "total_count": n,
                                   "percentage": 100 * qualified / n if n else None},
        "tail_claims": {"p95": "unqualified" if qualified < n else
                        "supported" if n >= 20 else "unsupported_small_sample",
                        "p99": "unqualified" if qualified < n else
                        "supported" if n >= 100 else "unsupported_small_sample"},
    }


def time_weighted_concurrency(intervals: list[tuple[int, int]], start_ns: int,
                              end_ns: int, target: int | None = None,
                              tolerance: float = 0) -> dict[str, Any]:
    """Integrate clipped intervals, including idle time and simultaneous boundaries."""
    if end_ns <= start_ns or any(end < start for start, end in intervals):
        raise ValueError("invalid concurrency interval")
    changes: Counter[int] = Counter({start_ns: 0, end_ns: 0})
    for start, end in intervals:
        if end <= start_ns or start >= end_ns:
            continue
        changes[max(start_ns, start)] += 1
        changes[min(end_ns, end)] -= 1
    current = area = in_tolerance = 0
    minimum: int | None = None
    maximum = 0
    ordered = sorted(changes)
    for index, timestamp in enumerate(ordered[:-1]):
        current += changes[timestamp]
        duration = ordered[index + 1] - timestamp
        if duration:
            area += current * duration
            minimum = current if minimum is None else min(minimum, current)
            maximum = max(maximum, current)
            if target is not None and abs(current - target) <= tolerance:
                in_tolerance += duration
    duration = end_ns - start_ns
    return {"mean": area / duration, "minimum": minimum, "maximum": maximum,
            "within_tolerance_percent": 100 * in_tolerance / duration if target is not None else None,
            "duration_seconds": duration / 1e9,
            "method": "time_weighted_piecewise_constant_client_unfinished_requests"}


def receiver_spacing(timestamps_ns: list[int], rate_rps: float,
                     thresholds: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Receiver-local delivery jitter, not client-read or packet timing accuracy."""
    ordered = sorted(timestamps_ns)
    gaps = [(right-left)/1e6 for left, right in zip(ordered, ordered[1:])]
    tolerance = thresholds.get("receiver_spacing_relative_tolerance")
    coverage = thresholds.get("receiver_spacing_coverage_fraction")
    period_ms = 1000 / rate_rps if rate_rps > 0 else None
    in_band = sum(abs(gap-period_ms) <= period_ms*tolerance for gap in gaps) if period_ms is not None and tolerance is not None else None
    required = 1 if len(gaps) < 20 else coverage
    fraction = in_band / len(gaps) if in_band is not None and gaps else None
    details = {"target_period_ms": period_ms, "gap_distribution": distribution(gaps),
               "relative_tolerance": tolerance, "required_fraction": required,
               "observed_fraction": fraction, "violations": len(gaps)-in_band if in_band is not None else None,
               "sample_policy": "all intervals required below20 samples; otherwise frozen coverage fraction",
               "claim": "provisional engineering delivery jitter; not network or client-read timing accuracy"}
    if not gaps or period_ms is None or tolerance is None or required is None:
        return "inconclusive", details
    return ("pass" if fraction >= required else "fail"), details


def _load_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.exists():
        return [], [f"missing artifact: {path.name}"]
    rows, errors = [], []
    with path.open("rb") as stream:
        for number in range(1, MAX_ARTIFACT_ROWS + 2):
            line = stream.readline(MAX_LINE_BYTES + 1)
            if not line:
                break
            if number > MAX_ARTIFACT_ROWS or len(line) > MAX_LINE_BYTES:
                return [], [f"verifier safety limit: {path.name}; raw evidence retained, not interpreted"]
            if not line.strip():
                continue
            try:
                value = _strict_json(line)
                if not isinstance(value, dict):
                    raise ValueError("row must be an object")
                rows.append(value)
            except (ValueError, UnicodeDecodeError) as error:
                errors.append(f"{path.name}:{number}: {error}")
    return rows, errors


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    return row.get("metadata") or {}


def _kind(row: dict[str, Any]) -> str:
    return row.get("kind", row.get("event_kind", ""))


def _outcome(value: Any) -> str:
    return {"completed": "success", "complete": "success", "succeeded": "success",
            "failed": "error", "failure": "error", "timed_out": "timeout",
            "canceled": "cancelled"}.get(str(value), str(value or "unresolved"))


def _expected(plan: dict[str, Any], scenario: dict[str, Any],
              result: dict[str, Any] | None = None) -> dict[str, Any]:
    fixture = (result or {}).get("fixture_id", plan.get("fixture_id"))
    catalog = scenario.get("expected_by_fixture", {}).get(fixture, {})
    return {**scenario.get("expected", {}), **catalog, **plan.get("expected", {})}


def _id(row: dict[str, Any]) -> str | None:
    return row.get("attempt_id") or row.get("logical_request_id")


def _phase(row: dict[str, Any]) -> str:
    return row.get("phase", row.get("population", _metadata(row).get("population", "measured")))


class _RetainedProductivity:
    """Fixed scalar cohorts, populated once per already reconciled result group."""
    phases = {"measured", "control", "warmup", "probe", "reference", "retry", "unknown"}
    volumes = ("choice_messages", "answer_chunks", "answer_utf8_bytes", "reasoning_utf8_bytes")

    def __init__(self):
        self.cohorts = {phase: {name: Counter() for name in ("counts", "sum", "known", "missing", "not_applicable")}
                        for phase in sorted(self.phases)}

    @classmethod
    def declared_phase(cls, row):
        labels = [value for value in (row.get("phase"), row.get("population"), _metadata(row).get("population"))
                  if value is not None and value != ""]
        if not labels or any(not isinstance(value, str) or value not in cls.phases for value in labels):
            return "unknown"
        return labels[0] if all(value == labels[0] for value in labels) else "unknown"

    def add(self, outputs, stream_config=None):
        if not outputs:
            return
        phase = self.declared_phase(outputs[0])
        if any(self.declared_phase(row) != phase for row in outputs):
            phase = "unknown"
        cohort = self.cohorts[phase]
        if len(outputs) != 1:
            cohort["counts"]["duplicate_result_groups_excluded"] += 1
            cohort["counts"]["duplicate_rows_excluded"] += len(outputs)
            return
        row, counts = outputs[0], cohort["counts"]
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        counts["unique_retained_attempts"] += 1
        counts["protocol_valid_completed"] += row.get("status") == "completed" and row.get("valid") is True
        score = row.get("score", raw.get("score"))
        counts["score_true" if score is True else "score_false" if score is False else "score_unavailable"] += 1
        if "evaluation_error" in raw:
            counts["evaluation_error_known"] += 1
            counts["evaluation_errors"] += raw["evaluation_error"] is not None
        else:
            counts["evaluation_error_unavailable"] += 1
        stream = raw.get("stream", stream_config)
        for name, value in (("choice_messages", raw.get("chunks")), ("answer_chunks", raw.get("answer_chunks"))):
            if stream is False:
                cohort["not_applicable"][name] += 1
            elif stream is not True or type(value) is not int or value < 0:
                cohort["missing"][name] += 1
            else:
                cohort["sum"][name] += value
                cohort["known"][name] += 1
        for name, value in (("answer_utf8_bytes", row.get("content", raw.get("output"))),
                            ("reasoning_utf8_bytes", raw.get("reasoning"))):
            try:
                size = len(value.encode("utf-8")) if isinstance(value, str) else None
            except UnicodeEncodeError:
                size = None
            if size is None:
                cohort["missing"][name] += 1
            else:
                cohort["sum"][name] += size
                cohort["known"][name] += 1

    def summary(self, window):
        output = {}
        for phase, cohort in self.cohorts.items():
            counts = {name: cohort["counts"][name] for name in (
                "unique_retained_attempts", "protocol_valid_completed", "score_true", "score_false", "score_unavailable",
                "evaluation_error_known", "evaluation_errors", "evaluation_error_unavailable",
                "duplicate_result_groups_excluded", "duplicate_rows_excluded")}
            rates = {name: counts[name]/window["seconds"] if window["seconds"] else None
                     for name in ("unique_retained_attempts", "protocol_valid_completed", "score_true", "score_false")}
            volumes = {}
            for name in self.volumes:
                known, missing = cohort["known"][name], cohort["missing"][name]
                total = cohort["sum"][name] if known and not missing else None
                volumes[name] = {"unambiguous_retained_total": total,
                                 "known_subset_sum": cohort["sum"][name] if known else None,
                                 "known_samples": known, "missing_samples": missing,
                                 "not_applicable_samples": cohort["not_applicable"][name],
                                 "rate_per_second": total/window["seconds"] if total is not None and window["seconds"] else None}
            output[phase] = {"counts": counts, "rates_per_second": rates, "volumes": volumes}
        return {"window": window, "by_explicit_phase": output,
                "scope": "Unambiguous retained results only; duplicate groups excluded and counted. Measured numerators use a mixed-phase whole-process window; no active-phase pipeline or pure parser/evaluator speed claim."}


def _candidate_window(events, wall_seconds=None):
    start = end = None
    starts = ends = 0
    for row in events:
        if _kind(row) == "candidate_process_started":
            start, starts = row, starts+1
        elif _kind(row) == "candidate_process_exited":
            end, ends = row, ends+1
    if starts == ends == 1:
        low, high, domain = start.get("monotonic_ns"), end.get("monotonic_ns"), start.get("clock_domain")
        if (type(low) is int and type(high) is int and high > low and isinstance(domain, str)
                and domain.strip() and domain == end.get("clock_domain")):
            return {"seconds": (high-low)/1e9, "clock_domain": domain,
                    "boundary": "driver-observed post-Popen to post-wait; mixed-phase process window",
                    "observation_uncertainty": "Supervisory start/exit observation delay is not independently bounded."}
    if type(wall_seconds) in {int, float} and math.isfinite(wall_seconds) and wall_seconds > 0:
        return {"seconds": wall_seconds, "clock_domain": "external driver monotonic elapsed",
                "boundary": "broader case startup through owned endpoint shutdown; mixed-phase window",
                "observation_uncertainty": "Whole-case lifecycle window, not per-record completion timestamps."}
    return {"seconds": None, "boundary": "unavailable or ambiguous candidate/case window"}


def auxiliary_attempt_metric(planned: list[dict[str, Any]], results: list[dict[str, Any]],
                             reconciled: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep declared request plans and observed retained attempts as separate populations."""
    def declared_phase(row: dict[str, Any]) -> str:
        return row.get("phase") or row.get("population") or _metadata(row).get("population") or "unknown"

    plans = Counter(declared_phase(row) for row in planned if _kind(row) != "sustained_admission_intent")
    retained: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unidentified = 0
    for row in results:
        identity = _id(row) or row.get("id") or row.get("raw", {}).get("id")
        if identity:
            retained[identity].append(row)
        else:
            unidentified += 1
    phases, scenarios = Counter(), Counter()
    for same_attempt in retained.values():
        labels = {declared_phase(row) for row in same_attempt}
        phases[next(iter(labels)) if len(labels) == 1 else "unknown"] += 1
        raw_names = {row.get("raw", {}).get("scenario") for row in same_attempt
                     if isinstance(row.get("raw", {}).get("scenario"), str)}
        if len(raw_names) == 1:
            scenarios[next(iter(raw_names))] += 1
    receipt_only = sum(row["receipt_count"] for row in reconciled if row["receipt_count"] and not row["result_count"])
    duplicates = sum(len(same_attempt)-1 for same_attempt in retained.values())
    incomplete = bool(receipt_only or unidentified or duplicates or phases.get("unknown") or not retained)
    return metric({"planned_request_rows_by_phase": dict(plans),
                   "observed_retained_result_attempts_by_phase": dict(phases),
                   "observed_retained_result_raw_scenarios": dict(scenarios),
                   "observed_counts_are_lower_bounds": True,
                   "unidentified_retained_result_rows": unidentified,
                   "duplicate_retained_result_rows": duplicates,
                   "receipt_rows_without_retained_result_join": receipt_only,
                   "receipt_only_attempt_phases": None if receipt_only else {},
                   "unexported_auxiliary_attempt_counts": "unknown"},
                  state="unknown" if incomplete else "measured", unit="planned request rows / retained attempts",
                  population="explicitly labelled request plans and distinct retained client result IDs", verdict="inconclusive",
                  reason="Duration admission intents are not requests. Plans are separate from observed retained result attempts, whose counts are lower bounds. "
                         "Phase/population labels are reported explicitly; raw scenario names appear only when exported, without inferred control/probe/retry distinctions. "
                         "Receipt-only and unexported attempts cannot be phase-counted from retained results.",
                  evidence=["planned.jsonl", "results.jsonl", "reconciliation.jsonl"])


def _result_value(result: dict[str, Any], field: str) -> Any:
    aliases = {"content": "output", "completion_tokens": "output_tokens",
               "prompt_tokens": "input_tokens", "outcome": "status"}
    if field in result:
        return result[field]
    return result.get(aliases.get(field, field))


def _result_outcome(result: dict[str, Any]) -> str:
    outcome = _outcome(_result_value(result, "outcome"))
    # Model-answer scoring is a separate boolean; valid is protocol validity.
    return "error" if outcome == "success" and result.get("valid") is False else outcome


def _lifecycle(result: dict[str, Any]) -> tuple[int, int] | None:
    timing = result.get("timing", {})
    for left, right in (("lifecycle_dispatch_ns", "lifecycle_completed_ns"), ("dispatch_ns", "completed_ns")):
        if isinstance(timing.get(left), int) and isinstance(timing.get(right), int):
            return timing[left], timing[right]
    return None


def _planned_identity_limited(result: dict[str, Any], scenario: dict[str, Any], count: int) -> bool:
    provenance = result.get("planned_identity_provenance")
    raw = result.get("raw")
    if count <= 1 or scenario.get("mode") == "sustained_concurrency":
        return False
    if provenance is not None:
        return provenance == "adapter_enumeration"
    return raw is not None and raw.get("scheduled_offset_ms") is None and "arrival_index" not in raw.get("workload", {})


def _reference_health(directory: Path, rows: list[dict[str, Any]]) -> tuple[str, list[str]]:
    path = directory / "receiver-health.json"
    if not path.exists():
        return "unknown", ["Independent receiver-health.json checkpoint is absent."]
    try:
        health = json.loads(path.read_text())
    except (ValueError, OSError) as error:
        return "no", [f"Unreadable receiver checkpoint: {error}"]
    reasons = []
    for flag in ("complete", "flush_complete"):
        if health.get(flag) is not True:
            reasons.append(f"Recorder {flag} was not confirmed true.")
    for counter in ("overflow_count", "truncated_count", "omitted_count", "write_errors"):
        if health.get(counter) != 0:
            reasons.append(f"Recorder {counter}={health.get(counter, 'unknown')}.")
    count, last, malformed, unordered = 0, None, False, False
    for count, row in enumerate(rows, 1):
        sequence = row.get("sequence")
        malformed |= not isinstance(sequence, int)
        unordered |= sequence != count - 1
        last = sequence
    if not count or malformed:
        reasons.append("Reference sequence coverage is missing or malformed.")
    elif unordered:
        reasons.append("Reference sequence has missing, duplicate, or out-of-order records.")
    written = health.get("written")
    if written is None:
        reasons.append("Recorder written count is absent.")
    elif written != count:
        reasons.append(f"Recorder claims {written} records; artifact contains {count}.")
    if count and health.get("last_sequence") != last:
        reasons.append("Recorder checkpoint last_sequence does not match retained evidence.")
    if health.get("attempted") is not None and health["attempted"] != written:
        reasons.append("Recorder attempted/written count differs.")
    if health.get("events_sha256"):
        digest = hashlib.sha256()
        with (directory / "receiver-events.jsonl").open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != health["events_sha256"]:
            reasons.append("Authoritative recorder checkpoint hash differs from retained events.")
    return ("no", reasons) if reasons else ("yes", ["Recorder checkpoint and ordered evidence agree."])


def _connect_error(result: dict[str, Any]) -> bool:
    error_type = result.get("error_type", result.get("exception_type", (result.get("raw") or {}).get("error_type")))
    error = result.get("error", (result.get("raw") or {}).get("error"))
    return (isinstance(error_type, str) and error_type.rsplit(".", 1)[-1] == "ConnectError"
            or isinstance(error, str) and error.endswith("(ConnectError)"))


def _refusal_injected(scenario: dict[str, Any], client) -> bool:
    return scenario.get("fault") == "connection_refused" and any(
        _kind(row) == "external_fault_injected" and row.get("fault", _metadata(row).get("fault")) == "connection_refused"
        for row in client)


def _fault_behavior(scenario: dict[str, Any], manifest: dict[str, Any], client: list[dict[str, Any]],
                    receiver: list[dict[str, Any]], results: list[dict[str, Any]],
                    codes: Counter[str]) -> tuple[str, dict[str, Any]]:
    protocol_faults = {"http_error", "stream_error_before", "stream_error_after", "malformed_stream",
                       "missing_termination", "truncated", "hang", "disconnect"}
    fault = scenario.get("fault")
    if not fault and scenario.get("fixture") in protocol_faults:
        fault = scenario["fixture"]
    if not fault:
        return "not_applicable", {"reason": "No execution fault was specified for this case."}
    injections = [row for row in client if _kind(row) == "external_fault_injected"
                  and row.get("fault", _metadata(row).get("fault")) == fault]
    configured = [row for row in client if _kind(row) == "fault_configured"
                  and row.get("fault", _metadata(row).get("fault")) == fault]
    prepared = any(_kind(row) == "response_prepared" and _metadata(row).get("expected", {}).get("fixture") == fault
                   for row in receiver)
    triggered = bool(injections) or (fault in protocol_faults and prepared)
    if fault == "global_budget":
        triggered = bool(configured) and bool(manifest.get("product_abort_reason"))
    elif fault == "connection_refused":
        triggered = bool(injections) and any(_connect_error(row) and _result_outcome(row) == "error" for row in results)
    elif fault == "bounded_storage":
        triggered = bool(configured) and (manifest.get("returncode") == -25 or manifest.get("storage_failure_confirmed") is True)
    exits = [row for row in client if _kind(row) == "candidate_process_exited"]
    cleanup_seconds = None
    if injections and exits and injections[-1].get("clock_domain") == exits[-1].get("clock_domain"):
        cleanup_seconds = (exits[-1]["monotonic_ns"] - injections[-1]["monotonic_ns"]) / 1e9
    deadline = scenario.get("fault_expectations", {}).get("cleanup_deadline_seconds", 10)
    classification_failed = bool(codes["false_success"] or codes["outcome_mismatch"])
    details = {"mechanism": fault, "injection_confirmed": triggered,
               "configured_evidence_count": len(configured), "signal_evidence_count": len(injections),
               "classification_failures": classification_failed, "cleanup_seconds": cleanup_seconds,
               "cleanup_deadline_seconds": deadline, "persisted_client_result_count": len(results),
               "recovery": "not_measured; no replay was performed"}
    if classification_failed or cleanup_seconds is not None and (cleanup_seconds < 0 or cleanup_seconds > deadline):
        return "fail", details
    if not triggered:
        details["reason"] = "Fault specification lacks evidence that the intended boundary was exercised."
        return "inconclusive", details
    if fault in protocol_faults and results:
        if any(code in codes for code in ("receipt_identity_unknown", "receipt_correlation_unknown")):
            details["reason"] = "Expected error results exist, but per-attempt independent correlation is incomplete."
            return "inconclusive", details
        return "pass", details
    if fault == "connection_refused":
        supported = bool(results) and all(_connect_error(row) and _result_outcome(row) == "error" for row in results)
        details["reason"] = "Confirmed stopped-endpoint injection and exported ConnectError records support expected connection refusal; an endpoint receipt is not expected."
        return ("pass" if supported else "inconclusive"), details
    if fault in {"user_cancel", "controller_crash", "terminate_before_persistence", "bounded_storage"}:
        details["reason"] = "Cleanup/result durability require both exited-process evidence and retained client records."
        return ("pass" if exits and results and (cleanup_seconds is not None or fault == "bounded_storage") else "inconclusive"), details
    return "pass" if fault == "global_budget" and triggered and results else "inconclusive", details


def reconcile(planned: list[dict[str, Any]], client: list[dict[str, Any]],
              receiver: list[dict[str, Any]], results: list[dict[str, Any]],
              scenario: dict[str, Any], *, storage=None, productivity=None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Join exact attempts or unique independent response IDs; never join by prompt text."""
    issues: list[dict[str, Any]] = []

    def issue(code: str, identity: str | None, detail: str, evidence: str) -> None:
        if len(issues) >= MAX_RECONCILIATION_ISSUES:
            raise EvidenceLimitError("verifier safety limit: discrepancy working set exceeds allowance")
        encoded = detail.encode()
        if len(encoded) > MAX_ISSUE_DETAIL_BYTES:
            detail = encoded[:MAX_ISSUE_DETAIL_BYTES].decode(errors="replace") + " [detail bounded; raw evidence retained]"
        issues.append(dict(code=code, attempt_id=identity, detail=detail, evidence=evidence))

    plans = storage.groups("plans") if storage else defaultdict(list)
    receipts = storage.groups("receipts") if storage else defaultdict(list)
    terminals = storage.groups("terminals") if storage else defaultdict(list)
    events = storage.groups("events") if storage else defaultdict(list)
    dispatches = storage.groups("dispatches") if storage else defaultdict(list)
    not_dispatched = storage.groups("not_dispatched") if storage else defaultdict(list)
    parsed = storage.groups("parsed") if storage else defaultdict(list)
    response_ids: dict[str, set[str]] = defaultdict(set)
    result_response_ids: dict[str, set[str]] = defaultdict(set)
    for row in results:
        if row.get("response_id") and _id(row):
            result_response_ids[row["response_id"]].add(_id(row))
    reference_identity_map: dict[str, str] = {}
    for row in receiver:
        rid = _metadata(row).get("response_id")
        candidates = result_response_ids.get(rid, set())
        if _id(row) and len(candidates) == 1:
            reference_identity_map[_id(row)] = next(iter(candidates))
    unknown_receipts = {_id(row) for row in receiver if _kind(row) == "request_received"
                        and _id(row) not in reference_identity_map
                        and _metadata(row).get("correlation_confidence") == "response_id_only"}
    unknown_receipts -= {_id(row) for row in planned if _id(row)}
    refusal_injected = _refusal_injected(scenario, client)
    duration_intent = scenario.get("planned_semantics") == "duration_admission_intent"
    measured_result_count = sum(_phase(row) == "measured" for row in results)
    for row in planned:
        identity = _id(row)
        if identity:
            plans[identity].append(row)
        elif not duration_intent:
            issue("plan_identity_missing", None, "Planned work has no stable attempt/logical ID.", "planned.jsonl")
    for row in receiver:
        identity = reference_identity_map.get(_id(row), _id(row))
        if not identity:
            if _kind(row) == "request_received":
                issue("receipt_identity_unknown", None, "Receipt cannot be joined to a logical attempt.", "receiver-events.jsonl")
            continue
        if _kind(row) == "request_received":
            receipts[identity].append(row)
            response_id = _metadata(row).get("response_id")
            if response_id:
                response_ids[response_id].add(identity)
        elif _kind(row) == "response_finished":
            terminals[identity].append(row)
        elif _kind(row) == "response_event":
            events[identity].append(row)
    for row in client:
        identity = _id(row)
        if _kind(row) in {"request_dispatched", "dispatch", "request_dispatch"} and identity:
            dispatches[identity].append(row)
        elif _kind(row) in {"not_dispatched", "request_dropped"} and identity:
            not_dispatched[identity].append(row)
    for row in results:
        identity = _id(row)
        if not identity:
            rid = row.get("response_id")
            candidates = response_ids.get(rid, set())
            if len(candidates) == 1:
                identity = next(iter(candidates))
            else:
                issue("result_identity_unknown", None,
                      f"No unique attempt/response-ID join (response_id={rid!r}).", "results.jsonl")
                continue
        parsed[identity].append(row)
    identities = sorted(set(plans) | set(receipts) | set(terminals) | set(events) | set(dispatches) | set(parsed))
    reconciled = []
    for identity in identities:
        plan_rows, received, finished, outputs = plans[identity], receipts[identity], terminals[identity], parsed[identity]
        if productivity is not None:
            productivity.add(outputs, scenario.get("stream"))
        expected = _expected(plan_rows[0] if plan_rows else {}, scenario, outputs[0] if outputs else None)
        local_issues: list[str] = []

        def fault(code: str, detail: str, evidence: str) -> None:
            local_issues.append(code)
            issue(code, identity, detail, evidence)

        if len(plan_rows) > 1:
            fault("duplicate_plan", "Duplicate planned attempt identity.", "planned.jsonl")
        if len(received) > 1:
            fault("duplicate_request", "Multiple endpoint receipts for one attempt.", "receiver-events.jsonl")
        if len(dispatches[identity]) > 1:
            fault("duplicate_dispatch", "Multiple dispatch observations for one attempt.", "client-events.jsonl")
        if len(outputs) > 1:
            fault("duplicate_result", "Multiple result records for one attempt.", "results.jsonl")
        known_drop = bool(not_dispatched[identity]) and not received and not dispatches[identity]
        attempt_unconfirmed = bool(plan_rows) and not received and not outputs and not dispatches[identity] and not known_drop
        expected_refusal = refusal_injected and bool(outputs) and all(_connect_error(row) and _result_outcome(row) == "error" for row in outputs)
        if plan_rows and not received and not known_drop and not expected_refusal:
            fault("receipt_correlation_unknown" if unknown_receipts else "not_dispatched_unconfirmed" if attempt_unconfirmed else "missing_receipt",
                  "Planned attempt has no exact independent receipt join; uncorrelated receipts cannot be assigned by prompt/order."
                  if unknown_receipts else "Planned work has no dispatch/receipt/result evidence; attempt admission is unconfirmed."
                  if attempt_unconfirmed else "Planned attempt has no independent receipt.", "planned.jsonl,receiver-events.jsonl")
        if (received or dispatches[identity]) and not outputs and not known_drop and identity not in unknown_receipts:
            fault("missing_result", "Attempt has no retained parsed result; attribution unresolved.", "results.jsonl")
        if identity in unknown_receipts:
            fault("receipt_identity_unknown", "Endpoint response ID was not exported by any retained client result; exact receipt/result correlation is unavailable.", "receiver-events.jsonl,results.jsonl")
        if not plan_rows and (received or outputs) and not duration_intent and identity not in unknown_receipts:
            fault("unplanned_attempt", "Observed attempt has no matching admission intent.", "planned.jsonl")
        if received and not finished:
            fault("missing_reference_terminal", "Receipt has no retained endpoint terminal observation.", "receiver-events.jsonl")
        if len(finished) > 1:
            fault("duplicate_reference_terminal", "Multiple endpoint terminal observations.", "receiver-events.jsonl")
        event_rows = events[identity]
        sequence = [_metadata(row).get("event_seq", row.get("event_seq")) for row in event_rows]
        if sequence and (any(not isinstance(seq, int) for seq in sequence)
                         or sequence != list(range(len(sequence)))):
            fault("event_sequence", f"Non-contiguous/duplicate semantic event sequence: {sequence}.", "receiver-events.jsonl")
        if "event_contents" in expected and (received or event_rows):
            actual_events = [_metadata(row)["content"] for row in event_rows
                             if _metadata(row).get("content")]
            if actual_events != expected["event_contents"]:
                fault("reference_event_mismatch", "Reference events differ from independently specified golden events.", "planned.jsonl,receiver-events.jsonl")
        if "content" in expected and event_rows:
            reconstructed = "".join(_metadata(row).get("content") or "" for row in event_rows)
            if reconstructed != expected["content"]:
                fault("reference_content_corruption", "Endpoint event reconstruction differs from independent manifest golden content.", "planned.jsonl,receiver-events.jsonl")
        if outputs:
            result = outputs[0]
            if result.get("planned_identity_provenance") == "unique_request_pack_fixture":
                marker = expected.get("request_marker")
                request = _metadata(received[0]).get("request") if received else None
                if not marker or not isinstance(request, dict):
                    fault("planned_slot_correlation_unknown", "Unique fixture provenance lacks its frozen marker or independent request body.", "planned.jsonl,receiver-events.jsonl")
                else:
                    lines = [line for message in request.get("messages", [])
                             if isinstance(message.get("content"), str)
                             for line in message["content"].splitlines()]
                    if marker not in lines:
                        fault("request_marker_mismatch", "Frozen unique request-pack marker is absent from the independently received request; fixture/result assignment is incorrect.", "planned.jsonl,receiver-events.jsonl,results.jsonl")
            if _planned_identity_limited(result, scenario, measured_result_count):
                fault("planned_slot_correlation_unknown", "Adapter enumeration supplies a retained-result index, not an exported identity for the preplanned slot. Endpoint/result response-ID join remains separate.", "planned.jsonl,results.jsonl")
            if event_rows and _result_outcome(result) == "success" and finished:
                emitted = "".join(_metadata(row).get("content") or "" for row in event_rows)
                if _result_value(result, "content") is not None and _result_value(result, "content") != emitted:
                    fault("server_client_content_mismatch", "Retained client output differs from matched endpoint semantic events; loss attribution unresolved.", "receiver-events.jsonl,results.jsonl")
            for field in ("content", "prompt_tokens", "completion_tokens", "score"):
                if field in expected and _result_value(result, field) != expected[field]:
                    code = "content_corruption" if field == "content" else f"{field}_mismatch"
                    fault(code, f"{field}: expected {expected[field]!r}; recorded {_result_value(result, field)!r}.", "planned.jsonl,results.jsonl")
            if "outcome" in expected:
                actual_outcome = _result_outcome(result)
                desired = _outcome(expected["outcome"])
                if actual_outcome != desired:
                    fault("false_success" if actual_outcome == "success" else "outcome_mismatch",
                          f"Expected {desired}; recorded {actual_outcome}.", "planned.jsonl,results.jsonl")
            if result.get("event_contents") is not None and "event_contents" in expected:
                if result["event_contents"] != expected["event_contents"]:
                    fault("client_event_mismatch", "Client event reconstruction differs from golden events.", "results.jsonl")
            timing = result.get("timing", {})
            for name in ("latency_ms", "first_answer_ms", "first_byte_ms", "first_reasoning_ms"):
                if isinstance(timing.get(name), (float, int)) and timing[name] < 0:
                    fault("timestamp_arithmetic", f"Negative client {name}.", "results.jsonl")
            if "dispatch_ns" in timing and "completed_ns" in timing:
                calculated = (timing["completed_ns"] - timing["dispatch_ns"]) / 1e6
                if calculated < 0 or ("latency_ms" in timing and
                                      not math.isclose(calculated, timing["latency_ms"], abs_tol=1e-6)):
                    fault("timestamp_arithmetic", "Client duration differs from its same-domain timestamp vector.", "results.jsonl")
            if "timing" in expected:
                for name, desired in expected["timing"].items():
                    if timing.get(name) != desired:
                        fault("timing_missing" if timing.get(name) is None else "timing_mismatch",
                              f"Golden timing {name}={desired}; observed {timing.get(name)}.", "planned.jsonl,results.jsonl")
        if storage is not None and received:
            for receipt in received:
                storage.add_application_timing(identity, receipt, event_rows, finished)
        reconciled.append({"schema_version": 1, "attempt_id": identity,
                           "logical_request_id": (plan_rows or received or outputs or [{}])[0].get("logical_request_id"),
                           "planned_count": len(plan_rows), "dispatch_count": len(dispatches[identity]),
                           "receipt_count": len(received), "result_count": len(outputs),
                           "reference_terminal_count": len(finished), "reference_event_count": len(event_rows),
                           "phase": _phase((plan_rows or outputs or received or [{}])[0]),
                           "outcome": _result_outcome(outputs[0]) if outputs else "not_dispatched" if known_drop else "admission_unconfirmed" if attempt_unconfirmed else "unresolved",
                           "correlation_confidence": "exact_identity" if plan_rows and (received or outputs) else "unknown",
                           "issues": local_issues})
    return reconciled, issues


def failure_classification_metric(manifest: dict[str, Any], scenario: dict[str, Any],
                                  planned: list[dict[str, Any]], client: list[dict[str, Any]],
                                  results: list[dict[str, Any]], issues: list[dict[str, Any]]) -> dict[str, Any]:
    """Fixture expectations alone cannot establish a client classification defect."""
    mismatches = [row for row in issues if row["code"] == "outcome_mismatch"]
    mismatch_ids = {row["attempt_id"] for row in mismatches}
    plans = {_id(row): row for row in planned}
    plan_counts = Counter(_id(row) for row in planned)
    result_by_id = {_id(row): row for row in results}
    result_counts = Counter(_id(row) for row in results)
    confirmed = set()
    if manifest.get("deterministic_control") is True:
        for row in client:
            identity = _id(row)
            if (identity not in mismatch_ids or plan_counts[identity] != 1 or result_counts[identity] != 1
                    or _kind(row) != "client_protocol_complete_reference"
                    or row.get("source") != "independent_control_reference"):
                continue
            expected = _expected(plans[identity], scenario, result_by_id[identity])
            metadata = _metadata(row)
            if (_outcome(expected.get("outcome")) == "success"
                    and metadata.get("protocol_complete") is True
                    and isinstance(metadata.get("content"), str)
                    and hashlib.sha256(metadata["content"].encode()).hexdigest() ==
                    (expected.get("content_sha256") or (hashlib.sha256(expected["content"].encode()).hexdigest() if isinstance(expected.get("content"), str) else None))):
                confirmed.add(identity)
    fully_attributed = bool(mismatches) and len(confirmed) == len(mismatches)
    return metric({"fixture_outcome_mismatches": len(mismatches),
                   "confirmed_valid_completion_misclassifications": len(confirmed) if confirmed else None,
                   "unattributed_fixture_outcome_mismatches": len(mismatches)-len(confirmed)},
                  state="measured" if fully_attributed else "unknown", unit="requests / attribution",
                  population="retained fixture-outcome mismatches", verdict="fail" if confirmed else "inconclusive",
                  reason=("Deterministic independent client-protocol-complete reference matches the attempt and golden content. "
                          if confirmed else "Independent client-protocol-complete evidence is unavailable. ")
                         + "Fixture outcome mismatches are retained; expected completion or server emission alone does not prove a valid completion was misclassified. "
                         + ("Some attribution remains unknown." if not fully_attributed else "All recorded mismatch attribution is independently supported in this control."),
                  evidence=["manifest.json", "planned.jsonl", "client-events.jsonl", "results.jsonl", "reconciliation.jsonl"])


def timing_profile_fingerprint(scenario: dict[str, Any]) -> str:
    # Compare all workload behavior, including future knobs, rather than an
    # allowlist that can silently omit endpoint delays or fault configuration.
    labels = {"id", "purpose", "tier", "repetitions", "active_seconds", "observer_enabled",
              "timing_profile", "calibration_for", "healthy_capacity", "selection_rule",
              "token_count_provenance", "payload_size_provenance", "identity_policy"}
    descriptor = {name: value for name, value in scenario.items() if name not in labels}
    if scenario.get("mode") in {"fixed_rate", "poisson", "sustained_concurrency"}:
        # Matched short controls retain the arrival geometry, not total volume.
        descriptor.pop("request_count", None)
    return hashlib.sha256(json.dumps(descriptor, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _identity_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate_client_source_identity(directory: Path, manifest: dict, health: dict) -> dict:
    """Prove the observed client against an immutable Git blob and saved snapshots."""
    proof = {"valid": False, "issues": [], "method": "pinned Git blob plus before/after source/dependency snapshot chain"}
    try:
        baseline = manifest.get("baseline", {})
        commit = baseline.get("sha", "")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("Pinned baseline commit identity is absent or invalid")
        checkout = Path(baseline["checkout"]).resolve()
        snapshots = []
        for name in ("baseline-before.json", "baseline-after.json"):
            path = directory / name
            if path.stat().st_size > MAX_LINE_BYTES:
                raise ValueError("Baseline identity snapshot exceeds the bounded metadata allowance")
            snapshot = _strict_json(path.read_bytes())
            if not isinstance(snapshot, dict):
                raise ValueError("Baseline identity snapshot must be an object")
            hashes = snapshot.get("tracked_file_hashes")
            if not isinstance(hashes, dict) or not hashes or any(not isinstance(key, str) or not isinstance(value, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", value) for key, value in hashes.items()):
                raise ValueError("Baseline snapshot source-file hashes are absent or invalid")
            source_digest = _identity_digest({key: value for key, value in hashes.items() if key.startswith("src/")})
            dependency_digest = _identity_digest({key: value for key, value in hashes.items()
                                                  if key in {"pyproject.toml", "uv.lock", "requirements.txt", "requirements-dev.txt"}})
            dependencies = snapshot.get("installed_dependencies")
            if (snapshot.get("sha") != commit or snapshot.get("status_porcelain") != ""
                    or Path(snapshot.get("checkout", "")).resolve() != checkout
                    or snapshot.get("tracked_tree_sha256") != _identity_digest(hashes)
                    or source_digest != baseline.get("product_source_sha256")
                    or source_digest != snapshot.get("product_source_sha256")
                    or dependency_digest != snapshot.get("dependency_specification_sha256")
                    or not isinstance(dependencies, str)
                    or hashlib.sha256(dependencies.encode()).hexdigest() != snapshot.get("installed_dependencies_sha256")):
                raise ValueError("Baseline snapshot commit/source/dependency aggregate proof differs")
            snapshots.append(snapshot)
        before, after = snapshots
        if (before["tracked_file_hashes"] != after["tracked_file_hashes"]
                or before["installed_dependencies_sha256"] != after["installed_dependencies_sha256"]):
            raise ValueError("Baseline source or installed dependencies changed during the case")
        expected_path = checkout / "src/giraffe/client.py"
        if (Path(health.get("baseline", "")).resolve() != checkout
                or Path(health.get("import_proof", "")).resolve() != expected_path):
            raise ValueError("Observer client import path does not match the pinned baseline")
        object_name = commit + ":src/giraffe/client.py"
        command = ["git", "-C", str(checkout)]
        size = int(subprocess.check_output([*command, "cat-file", "-s", object_name], stderr=subprocess.PIPE, timeout=10))
        if not 0 < size <= MAX_LINE_BYTES:
            raise ValueError("Pinned Git client blob exceeds the bounded source allowance")
        blob = subprocess.check_output([*command, "show", object_name], stderr=subprocess.PIPE, timeout=10)
        source_sha = hashlib.sha256(blob).hexdigest()
        if (len(blob) != size or before["tracked_file_hashes"].get("src/giraffe/client.py") != source_sha
                or health.get("source_sha256_before") != source_sha or health.get("source_sha256_after") != source_sha
                or health.get("source_unchanged") is not True):
            raise ValueError("Observer/snapshot client hashes do not match the pinned Git blob")
        object_id = subprocess.check_output([*command, "rev-parse", object_name], stderr=subprocess.PIPE, timeout=10).decode().strip()
        proof.update(valid=True, commit=commit, path="src/giraffe/client.py", git_blob=object_id,
                     client_sha256=source_sha, product_source_sha256=baseline["product_source_sha256"],
                     import_proof=str(expected_path), snapshot_files=["baseline-before.json", "baseline-after.json"])
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        proof["issues"].append(f"Pinned baseline client identity is unproven: {type(error).__name__}: {error}")
    return proof


def _observer_timing(directory: Path, manifest: dict[str, Any], results, thresholds, *, write):
    config = manifest.get("timing_observer", {})
    if not config.get("enabled"):
        return None
    from validation_suite.timing_observer import reference_intervals_file, validate_timing_health

    output = {"qualified": False, "measurement_failure": False, "qualified_count": 0,
              "observed_count": len(results), "metrics": {}, "issues": [],
              "scope": "named timer/body-return boundaries in this observed execution only; no packet arrival or old-run transfer",
              "criteria": {name: thresholds.get(name) for name in ("timing_error_ms", "timing_relative_error", "timing_coverage_fraction")}}
    try:
        expected_sha = config.get("observer_source_sha256")
        if not isinstance(expected_sha, str) or len(expected_sha) != 64:
            raise ValueError("Frozen observer source identity is absent")
        health_path = directory / "timing-health.json"
        if health_path.stat().st_size > MAX_LINE_BYTES:
            raise ValueError("Timing health exceeds the bounded metadata allowance")
        health_raw = _strict_json(health_path.read_bytes())
        if not isinstance(health_raw, dict):
            raise ValueError("Timing health must be an object")
        health = validate_timing_health(directory, expected_observer_sha256=expected_sha)
        output["health"] = health
        identity = validate_client_source_identity(directory, manifest, health_raw)
        output["source_identity"] = identity
        output["issues"].extend(identity["issues"])
        overhead_spec = config.get("overhead_evidence", {})
        if not isinstance(overhead_spec, dict) or not isinstance(overhead_spec.get("path"), str) or not overhead_spec["path"].strip():
            output["overhead"] = {"valid_for_profile": False,
                                  "reason": "Matched observer overhead evidence is absent; calibration constituents cannot qualify themselves."}
            raise ValueError(output["overhead"]["reason"])
        overhead_path = Path(overhead_spec.get("path", ""))
        if not overhead_path.is_absolute():
            overhead_path = directory / overhead_path
        overhead_bytes = overhead_path.read_bytes()
        if hashlib.sha256(overhead_bytes).hexdigest() != overhead_spec.get("sha256"):
            raise ValueError("Matched overhead artifact hash differs from the manifest")
        overhead = _strict_json(overhead_bytes)
        profile = overhead.get("profiles", {}).get(overhead_spec.get("profile"), {})
        scenario = manifest.get("scenario", {})
        profile_matches = (manifest.get("experiment_id") == "E1" and str(overhead_spec.get("profile", "")).startswith("E1-"))
        if manifest.get("experiment_id") in {"E3", "E4"}:
            frozen_fingerprint = timing_profile_fingerprint(scenario)
            profile_matches = (profile.get("workload_fingerprint") == frozen_fingerprint
                               and config.get("workload_fingerprint") == frozen_fingerprint
                               and profile.get("experiment_id") == manifest.get("experiment_id"))
        pairs = profile.get("pairs", [])
        inflations = [pair["observed_elapsed_ms"]-pair["control_elapsed_ms"] for pair in pairs]
        numerical_gate = (len(inflations) >= 3 and profile.get("elapsed_gate_pass") is True
                          and distribution(inflations)["median"] <= profile.get("median_budget_ms", -1)
                          and max(inflations) <= profile.get("max_budget_ms", -1))
        if "gap_gate_pass" in profile:
            gap_changes = [pair["observed_gap_ms"]-pair["control_gap_ms"] for pair in pairs
                           if pair.get("observed_gap_ms") is not None and pair.get("control_gap_ms") is not None]
            numerical_gate &= (profile["gap_gate_pass"] is True and bool(gap_changes)
                               and distribution(gap_changes)["median"] <= profile.get("gap_budget_ms", -1))
        overhead_valid = (profile_matches and numerical_gate
                          and all(overhead.get(name) is True for name in
                                  ("baseline_unchanged", "source_unchanged_during_execution", "observer_health_all_complete"))
                          and overhead.get("observer_source_sha256") == expected_sha
                          and overhead.get("baseline_sha") == manifest.get("baseline", {}).get("sha")
                          and profile.get("overhead_gate_pass") is True and len(profile.get("pairs", [])) >= 3
                          and all(value is True for name, value in profile.items() if name.endswith("_gate_pass")))
        output["overhead"] = {"valid_for_profile": overhead_valid, "profile": overhead_spec.get("profile"),
                              "artifact": str(overhead_path.resolve()), "sha256": overhead_spec.get("sha256"),
                              "claim": "matching calibrated workload only; no transfer to other load profiles or original unobserved runs"}
        if not overhead_valid:
            output["issues"].append("matching workload observer overhead evidence is unavailable or failed")
        maximum_uncertainty = int(thresholds.get("timing_error_ms", 0) * 1e6 * thresholds.get("reference_uncertainty_fraction_of_interval", .1))
        uncertainty = min(config.get("uncertainty_budget_ns", 100_000), maximum_uncertainty)
        raw_ids = Counter((row.get("raw") or {}).get("id") for row in results)
        candidates = {(row.get("raw") or {}).get("id"): row for row in results if (row.get("raw") or {}).get("id")}
        vectors = defaultdict(lambda: defaultdict(list))
        qualified_samples = Counter()
        covered, reference_ids, samples = set(), set(), 0
        trusted = health.get("reference_valid_for_observed_run") is True and overhead_valid and not output["issues"]
        with tempfile.TemporaryDirectory(prefix="giraffe-timing-reference-") as temporary:
            references = Path(temporary) / "timing-reference.jsonl"
            analysis = reference_intervals_file(directory / "timing-events.jsonl", references,
                                                uncertainty_budget_ns=uncertainty)
            output["reference_analysis"] = analysis
            trusted &= analysis.get("complete") is True
            for reference in bounded_jsonl_rows(references):
                identity = reference.get("record_id")
                if identity not in candidates:
                    continue  # Control-phase references are outside this measured population.
                if identity in reference_ids or raw_ids[identity] != 1:
                    output["issues"].append("ambiguous or duplicated exact raw request ID")
                    continue
                reference_ids.add(identity)
                result, request_ok = candidates[identity], not reference.get("issues")
                expected_metrics = {"completion_ms": "latency_ms"}
                expected_metrics.update({name: name for name in ("first_answer_ms", "first_reasoning_ms")
                                         if isinstance(result.get("timing", {}).get(name), (int, float))})
                if isinstance(result.get("timing", {}).get("max_stream_gap_ms"), (int, float)):
                    expected_metrics["max_answer_event_gap_ms"] = "max_stream_gap_ms"
                for name, client_field in expected_metrics.items():
                    interval, reported = reference.get(name), result.get("timing", {}).get(client_field)
                    if not isinstance(interval, dict) or not isinstance(reported, (int, float)):
                        request_ok = False
                        continue
                    low, high = interval["lower_ms"], interval["upper_ms"]
                    error = max(low-reported, reported-high, 0)
                    for field, value in (("reported_ms", reported), ("reference_lower_ms", low), ("reference_upper_ms", high),
                                         ("error_outside_reference_ms", error), ("uncertainty_ms", interval["uncertainty_width_ns"]/1e6)):
                        vectors[client_field][field].append(value)
                        samples += 1
                    precise = interval.get("qualified_for_observed_run") is True and interval["uncertainty_width_ns"] <= uncertainty
                    allowed_error = min(thresholds.get("timing_error_ms", 0),
                                        thresholds.get("timing_relative_error", .1) * max(abs(low), abs(high)))
                    metric_ok = trusted and precise and error <= allowed_error
                    qualified_samples[client_field] += int(metric_ok)
                    request_ok &= precise and error <= allowed_error
                    if trusted and precise and error > allowed_error:
                        output["measurement_failure"] = True
                for interval in reference.get("read_to_semantic_ready_ms", []):
                    vectors["body_read_to_semantic_ready"]["reference_lower_ms"].append(interval["lower_ms"])
                    vectors["body_read_to_semantic_ready"]["reference_upper_ms"].append(interval["upper_ms"])
                    samples += 2
                    qualified_samples["body_read_to_semantic_ready"] += int(trusted and interval.get("qualified_for_observed_run") is True
                                                                            and interval["uncertainty_width_ns"] <= uncertainty)
                if samples > MAX_TIMING_SAMPLES:
                    raise EvidenceLimitError("timing metric sample working-set bound exceeded")
                if trusted and request_ok:
                    covered.add(identity)
        output["qualified_count"] = len(covered)
        output["qualified_metric_samples"] = dict(qualified_samples)
        output["metric_sample_bound"] = MAX_TIMING_SAMPLES
        output["metric_scalar_samples"] = samples
        expected_measurements = (len(results) if manifest.get("planned_semantics") == "duration_admission_intent" else
                                 scenario.get("request_count", scenario.get("count", len(results))))
        expected_measurements = len(results) if expected_measurements is None else expected_measurements
        coverage = len(covered) / expected_measurements if expected_measurements else 0
        output["expected_measurements"] = expected_measurements
        output["coverage_population"] = "finite requested measured slots; observed attempts for duration admission intent"
        output["coverage_fraction"] = coverage
        output["qualified"] = bool(results) and trusted and not output["issues"] and coverage >= thresholds.get("timing_coverage_fraction", 1) and not output["measurement_failure"]
        output["metrics"] = {name: {field: distribution(values, qualified_count=qualified_samples[name])
                                    for field, values in fields.items()} for name, fields in vectors.items()}
        output["reason"] = ("Independent observer intervals, exact raw IDs, pinned source/clock/health and matching overhead evidence meet frozen error/coverage bounds for this observed execution."
                            if output["qualified"] else "Observed timing evidence lacks required health, precision, exact coverage, matching overhead, or agreement with frozen bounds.")
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        output["issues"].append(str(error))
        output["reason"] = "Observed timing reference could not be completely qualified; retained evidence and reported durations remain diagnostic."
    if write:
        (directory / "timing-verification.json").write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    return output


def _verify_case(case_dir: str | Path, manifest: dict[str, Any] | None = None,
                 *, write: bool = True, storage=None) -> dict[str, Any]:
    directory = Path(case_dir)
    if manifest is None:
        manifest = json.loads((directory / "manifest.json").read_text())
    report = empty_report(manifest)
    productivity = _RetainedProductivity()
    scenario = {**manifest.get("scenario", {}), "planned_semantics": manifest.get("planned_semantics")}
    artifacts, artifact_errors = {}, []
    names = ("planned", "client-events", "receiver-events", "results")
    safety_limited = False
    if storage is None:
        small_input_bytes = sum((directory / f"{name}.jsonl").stat().st_size for name in names
                                if (directory / f"{name}.jsonl").exists())
        if small_input_bytes > 16 * 1024**2:
            safety_limited = True
            artifacts = {name: [] for name in names}
            artifact_errors.append("verifier safety limit: small reference-only path exceeds16MiB; use default disk-backed verification")
        else:
            for name in names:
                artifacts[name], errors = _load_jsonl(directory / f"{name}.jsonl")
                artifact_errors.extend(errors)
    else:
        try:
            artifacts = storage.load()
        except (EvidenceLimitError, sqlite3.Error, OSError, ValueError) as error:
            storage.limited = safety_limited = True
            storage.errors.append(f"verifier safety limit: {error}; raw evidence retained without interpretation")
            artifacts = {name: [] for name in names}
        artifact_errors.extend(storage.errors)
    planned, client, receiver, results = (artifacts[name] for name in names)
    try:
        reconciled, issues = reconcile(planned, client, receiver, results, scenario,
                                       storage=None if safety_limited else storage, productivity=productivity)
        if storage is not None:
            storage.check_bounds()
        reference_valid, health_reasons = _reference_health(directory, receiver)
    except (EvidenceLimitError, sqlite3.Error, OSError, ValueError) as error:
        if storage is None:
            raise
        storage.limited = safety_limited = True
        artifact_errors.append(f"verifier safety limit: {error}; raw evidence retained without interpretation")
        reconciled, issues = [], []
        planned, client, receiver, results = [], [], [], []
        reference_valid, health_reasons = "unknown", ["Evidence could not be completely interpreted within the bounded verifier."]
    if storage is not None and not safety_limited:
        planned, client, receiver, results = (storage.compact[name] for name in names)
    unique_receipts = {_metadata(row).get("response_id") or _id(row) for row in receiver if _kind(row) == "request_received"}
    unique_results = {_id(row) or row.get("raw", {}).get("id") or f"unidentified-row-{index}" for index, row in enumerate(results)}
    if reference_valid == "yes" and len(unique_receipts) > len(unique_results):
        issues.append({"code": "result_count_deficit", "attempt_id": None,
                       "detail": f"{len(unique_receipts)} distinct independent endpoint requests but only {len(unique_results)} retained result identities. Per-request loss boundary remains unresolved.",
                       "evidence": "receiver-events.jsonl,results.jsonl"})
    issues.extend({"code": "artifact_incomplete", "attempt_id": None, "detail": error,
                   "evidence": "manifest.json"} for error in artifact_errors)
    codes = Counter(issue["code"] for issue in issues)
    population = "measured planned logical requests and their explicit attempts"
    evidence = ["planned.jsonl", "client-events.jsonl", "receiver-events.jsonl", "results.jsonl", "reconciliation.jsonl"]

    def observed(label: str, value: Any, unit: str = "count", *, verdict: str = "pass",
                 reason: str = "Directly reconstructed from retained identity evidence.",
                 refs: list[str] | None = None, threshold: Any = None,
                 window: str = "whole case including drain", state: str = "measured",
                 population_scope: str | None = None) -> None:
        if safety_limited and label not in {"Limitations, failed gates and next diagnostic"} and label not in report["verdicts"]:
            value, state, verdict = None, "not_measured", "inconclusive"
            reason = "Verifier bounded offline input limit reached; raw evidence retained without interpretation."
        set_metric(report, label, metric(value, state=state, unit=unit, population=population_scope or population,
                                        window=window, threshold=threshold, verdict=verdict,
                                        reason=reason, evidence=refs or evidence))

    def unavailable(label: str, reason: str, *, state: str = "not_measured",
                    value: Any = None, refs: list[str] | None = None) -> None:
        set_metric(report, label, metric(value, state=state, reason=reason, evidence=refs,
                                        verdict="not_applicable" if state == "not_applicable" else "inconclusive"))

    measured_reconciliation = [row for row in reconciled if row["phase"] == "measured" and "receipt_identity_unknown" not in row["issues"]]
    measured_planned = [row for row in planned if _phase(row) == "measured"]
    measured_results = [row for row in results if _phase(row) == "measured"]
    totals = {name: sum(row[name] for row in measured_reconciliation) for name in
              ("planned_count", "dispatch_count", "receipt_count", "result_count", "reference_terminal_count")}
    healthy = scenario.get("healthy_capacity", True) and not scenario.get("fault") and not scenario.get("expected_fault")
    finite = scenario.get("request_count", scenario.get("count"))
    if manifest.get("planned_semantics") == "duration_admission_intent":
        finite = None
    mode = scenario.get("mode", "serial")
    correlation_incomplete = bool(codes["receipt_identity_unknown"] or codes["result_identity_unknown"] or codes["receipt_correlation_unknown"])
    slot_correlation_incomplete = bool(codes["planned_slot_correlation_unknown"])
    active = manifest.get("phases", {})
    start, end = active.get("active_start_ns"), active.get("active_end_ns")
    clock_domain = active.get("clock_domain")
    active_seconds = (end - start) / 1e9 if isinstance(start, int) and isinstance(end, int) and end > start else None
    if manifest.get("planned_semantics") == "duration_admission_intent":
        unavailable("Requests planned", "Verifier bounded offline input limit reached; raw evidence retained without interpretation."
                    if safety_limited else "Duration admission intent has no predetermined finite request count.",
                    state="not_measured" if safety_limited else "unknown",
                    value=None if safety_limited else {"admission_intents": sum(_kind(row) == "sustained_admission_intent" for row in planned)}, refs=["planned.jsonl"])
    else:
        observed("Requests planned", totals["planned_count"], refs=["planned.jsonl"])
    if finite is not None and totals["planned_count"] != finite and not artifact_errors:
        issues.append(dict(code="planned_count_mismatch", attempt_id=None,
                           detail=f"Requested {finite}; planned artifact has {totals['planned_count']}.", evidence="manifest.json,planned.jsonl"))
        codes["planned_count_mismatch"] += 1
    if totals["dispatch_count"] or manifest.get("observability", {}).get("dispatch_events"):
        observed("Requests dispatched", totals["dispatch_count"], refs=["client-events.jsonl"])
    else:
        unavailable("Requests dispatched", "Entering HTTP/external endpoint receipt cannot replace an unexported baseline dispatch observation.")
    if correlation_incomplete:
        unavailable("Requests confirmed received by endpoint", "Complete receipts exist but measured/control identities cannot all be assigned exactly.", state="unknown",
                    value={"all_population_receipts": sum(_kind(row) == "request_received" for row in receiver),
                           "correlated_measured_receipts": sum(row["receipt_count"] for row in measured_reconciliation if row["planned_count"])},
                    refs=["receiver-events.jsonl", "results.jsonl"])
    elif reference_valid == "yes":
        observed("Requests confirmed received by endpoint", totals["receipt_count"], refs=["receiver-events.jsonl", "receiver-health.json"])
    else:
        observed("Requests confirmed received by endpoint", totals["receipt_count"], state="unreliable",
                 verdict="inconclusive", reason="; ".join(health_reasons), refs=["receiver-events.jsonl"])
    observed("Duplicate requests", codes["duplicate_request"], population_scope="all reconciled attempt phases", verdict="fail" if codes["duplicate_request"] else "pass")
    observed("Logical-request and attempt reconciliation", {"totals": totals, "issue_counts": dict(codes)},
             unit="identities", population_scope="measured lifecycle totals and all-phase issue counts", verdict="fail" if set(codes)-UNCERTAIN_ISSUES else "inconclusive" if issues else "pass", reason="Lifecycle populations are separate; no overlapping counters are summed into conservation.")
    nondispatched = [row for row in client if _kind(row) in {"not_dispatched", "request_dropped"}]
    if nondispatched or manifest.get("observability", {}).get("dispatch_events"):
        observed("Requests never dispatched, by reason", dict(Counter(_metadata(row).get("reason", "unknown") for row in nondispatched)),
                 population_scope="all explicitly retained non-dispatch event phases")
        refused_ids = {_id(row) for row in results if _connect_error(row) and _result_outcome(row) == "error"} if _refusal_injected(scenario, client) else set()
        expected_refused = sum(row["dispatch_count"] > 0 and row["receipt_count"] == 0 and row["attempt_id"] in refused_ids for row in reconciled)
        unconfirmed = sum(row["dispatch_count"] > 0 and row["receipt_count"] == 0 and row["attempt_id"] not in refused_ids for row in reconciled)
        observed("Dispatched requests with receipt unconfirmed", {"unexpected": unconfirmed, "expected_connection_refused": expected_refused} if expected_refused else unconfirmed, population_scope="all reconciled attempt phases",
                 verdict="inconclusive" if correlation_incomplete else "fail" if unconfirmed else "pass",
                 state="unknown" if correlation_incomplete else "measured",
                 reason="Receipt joins are unavailable." if correlation_incomplete else "Confirmed ConnectError after stopped-endpoint injection has no expected receipt; other unconfirmed dispatches remain discrepancies." if expected_refused else "Retained explicit dispatch and receipt identities compared.")
    set_metric(report, "Warm-up, probe, reference and retry attempt counts",
               metric(reason="Verifier bounded offline input limit reached; raw evidence retained without interpretation.",
                      evidence=evidence) if safety_limited else auxiliary_attempt_metric(planned, results, reconciled))
    result_absent = sum(row["receipt_count"] > 0 and row["result_count"] == 0 and "receipt_identity_unknown" not in row["issues"] for row in reconciled)
    if correlation_incomplete:
        unavailable("Responses absent from Giraffe records", "Uncorrelated receipts cannot establish absent client results.", state="unknown")
    else:
        observed("Responses absent from Giraffe records", result_absent, population_scope="all correlated attempt phases", verdict="fail" if result_absent else "pass",
                 reason="A server observation without a result is an unreconciled discrepancy, not confirmed client loss.")
    terminal_rows = [row for row in receiver if _kind(row) == "response_finished"]
    terminal_outcomes = Counter(_outcome(_metadata(row).get("outcome")) for row in terminal_rows)
    terminal_phases: dict[str, Counter[str]] = defaultdict(Counter)
    for row in terminal_rows:
        declared = row.get("phase") or row.get("population") or _metadata(row).get("population") or "unknown"
        terminal_phases[declared][_outcome(_metadata(row).get("outcome"))] += 1
    set_metric(report, "Endpoint completed and failed responses", metric(
        None if safety_limited else {"all_phase_outcomes": dict(terminal_outcomes),
         "by_explicit_endpoint_phase": {phase: dict(counts) for phase, counts in terminal_phases.items()}},
        state="measured" if not safety_limited else "not_measured", unit="responses",
        population="all endpoint terminal records; endpoint-declared phase labels",
        verdict="inconclusive" if safety_limited else "pass",
        reason="Verifier bounded offline input limit reached; raw evidence retained without interpretation." if safety_limited else
               "All endpoint phases are included. Endpoint-declared labels do not establish the baseline measured/control phase; absent labels remain unknown.",
        evidence=["receiver-events.jsonl"]))
    outcomes = Counter(row["outcome"] for row in measured_reconciliation)
    observed("Successful, failed, timed-out, cancelled and unresolved requests", dict(outcomes), unit="requests")
    observed("Partial responses, timeouts and cancellations", {name: count for name, count in outcomes.items()
                                                               if name != "success"}, unit="requests")
    processed = sum(row["result_count"] == 1 for row in measured_reconciliation)
    observed("Records processed, evaluated and persisted", {"all_populations_persisted_result_rows": len(results),
                                                           "unique_measured_results": processed},
             unit="records", population_scope="all persisted result rows and unique measured result attempts, separately labelled", reason="Retained rows prove persistence only. Internal processing/evaluation stages are not inferred.")
    productivity_labels = ("Responses processed per second", "Stream protocol events processed per second",
                           "Response bytes processed per second")
    if artifact_errors or safety_limited:
        for label in productivity_labels:
            unavailable(label, "Complete retained-productivity evidence is unavailable or bounded parsing stopped; unread records are not zero.", refs=evidence)
    else:
        summary = productivity.summary(_candidate_window(client, manifest.get("wall_seconds")))
        reason = (summary["scope"] + " Scorer True/False are completed exported verdicts; null/absent scores and evaluation errors are distinct. "
                  "Choice-message counts exclude usage-only messages and DONE; retained UTF-8 strings are not wire bytes. "
                  "Missing values preserve known-subset coverage; no isolated parsing/evaluation latency or continuous backlog is inferred.")
        for label, fields in ((productivity_labels[0], None), (productivity_labels[1], {"choice_messages", "answer_chunks"}),
                              (productivity_labels[2], {"answer_utf8_bytes", "reasoning_utf8_bytes"})):
            cohorts = summary["by_explicit_phase"].values()
            retained = sum(cohort["counts"]["unique_retained_attempts"] for cohort in cohorts)
            state = "measured" if retained and summary["window"]["seconds"] else "unknown"
            if fields is not None:
                known = sum(cohort["volumes"][name]["known_samples"] for cohort in cohorts for name in fields)
                missing = sum(cohort["volumes"][name]["missing_samples"] for cohort in cohorts for name in fields)
                inapplicable = sum(cohort["volumes"][name]["not_applicable_samples"] for cohort in cohorts for name in fields)
                state = ("not_applicable" if inapplicable and not known and not missing else
                         "measured" if known and not missing and summary["window"]["seconds"] else "unknown")
            value = {"window": summary["window"], "by_explicit_phase": {
                phase: ({"counts": cohort["counts"], "rates_per_second": cohort["rates_per_second"]} if fields is None else
                        {name: volume for name, volume in cohort["volumes"].items() if name in fields})
                for phase, cohort in summary["by_explicit_phase"].items()}}
            observed(label, value, unit="retained counts / declared whole-process seconds", state=state,
                     verdict="not_applicable" if state == "not_applicable" else "inconclusive",
                     population_scope="unambiguous retained attempt groups by explicit phase; duplicates excluded and counted",
                     window=summary["window"]["boundary"], reason=reason, refs=["client-events.jsonl", "results.jsonl", "manifest.json"])
    if manifest.get("observability", {}).get("complete_response_evidence"):
        observed("Responses fully processed by Giraffe", sum(row["outcome"] == "success" for row in measured_reconciliation))
    else:
        unavailable("Responses fully processed by Giraffe", "Result export alone does not expose every parsed protocol event and termination boundary.")
    content_codes = ("content_corruption", "reference_content_corruption", "server_client_content_mismatch", "event_sequence", "reference_event_mismatch", "client_event_mismatch", "duplicate_result")
    content_failures = {code: codes[code] for code in content_codes if codes[code]}
    observed("Missing, duplicate or corrupted response content/events", content_failures, unit="discrepancies", population_scope="all reconciled attempt phases",
             verdict="fail" if content_failures else "pass", reason="Golden content is compared where exported; absent per-event client observations remain a coverage limitation.")
    observed("Unreconciled response differences", issues, unit="discrepancies", population_scope="all reconciled attempt phases", verdict="fail" if set(codes)-UNCERTAIN_ISSUES else "inconclusive" if issues else "pass")
    observed("Errors incorrectly classified as successes", codes["false_success"], population_scope="all reconciled attempt phases", verdict="fail" if codes["false_success"] else "pass")
    set_metric(report, "Valid completions incorrectly classified as failures",
               metric(reason="Verifier bounded offline input limit reached; raw evidence retained without interpretation.",
                      evidence=evidence) if safety_limited else failure_classification_metric(manifest, scenario, planned, client, results, issues))
    result_by_id = {_id(row): row for row in measured_results}
    golden_rows = [_expected(row, scenario, result_by_id.get(_id(row))) for row in measured_planned if _id(row)]
    if manifest.get("planned_semantics") == "duration_admission_intent":
        golden_rows = [_expected({}, scenario, row) for row in measured_results]
    scoring_expected = sum("score" in row and row["score"] is not None for row in golden_rows)
    score_observed = sum(row.get("score") is not None for row in measured_results)
    if scoring_expected:
        observed("Expected and actual scores/verdicts", {"expected_records": scoring_expected,
                 "actual_records": score_observed,
                 "expected_scores": dict(Counter(str(row.get("score")) for row in golden_rows)),
                 "actual_scores": dict(Counter(str(row.get("score")) for row in measured_results)),
                 "mismatches": codes["score_mismatch"]}, verdict="fail" if codes["score_mismatch"] else "pass" if score_observed >= scoring_expected else "inconclusive")
    else:
        unavailable("Expected and actual scores/verdicts", "Independent expected scores were not specified for these attempts.")
    token_expected = sum(any(field in row for field in ("prompt_tokens", "completion_tokens")) for row in golden_rows)
    if token_expected:
        measured_plan_by_id = {_id(row): row for row in measured_planned if _id(row)}
        token_pairs: Counter[str] = Counter()
        for result in measured_results:
            expected = _expected(measured_plan_by_id.get(_id(result), {}), scenario, result)
            pair = {"expected_prompt_tokens": expected.get("prompt_tokens", "not_specified"),
                    "expected_completion_tokens": expected.get("completion_tokens", "not_specified"),
                    "reported_prompt_tokens": _result_value(result, "prompt_tokens"),
                    "reported_completion_tokens": result.get("completion_tokens"),
                    "reported_output_tokens": result.get("output_tokens"),
                    "source": result.get("raw", {}).get("output_tokens_source", "not_exported")}
            token_pairs[json.dumps(pair, sort_keys=True)] += 1
        observed("Expected and reported input/output tokens", {"golden_records": token_expected,
                 "expected_actual_groups": [{**json.loads(pair), "count": count} for pair, count in token_pairs.items()],
                 "input_mismatches": codes["prompt_tokens_mismatch"], "output_mismatches": codes["completion_tokens_mismatch"]},
                 verdict="fail" if codes["prompt_tokens_mismatch"] or codes["completion_tokens_mismatch"] else "pass")
    else:
        unavailable("Expected and reported input/output tokens", "No independently specified token-usage golden values for these attempts.")
    if manifest.get("token_count_provenance"):
        observed("Token-count source and reasoning/answer accounting", manifest["token_count_provenance"], unit="declared provenance",
                 refs=["manifest.json"], reason="Synthetic fixture usage is not a model tokenizer; semantic event counts do not establish individual tokens or separate reasoning token counts.")
    else:
        unavailable("Token-count source and reasoning/answer accounting", "No explicit token-count provenance was retained.", state="unknown")
    observed("Unknown losses requiring investigation", {"missing_results": result_absent,
                                                       "unresolved_attempts": outcomes.get("unresolved", 0)},
             verdict="fail" if result_absent else "pass")
    unavailable("Confirmed client-side response/event loss", "Application emission without a client result cannot locate the loss boundary.", state="unknown")
    unavailable("Results lost between processing, evaluation and persistence", "Baseline does not expose independently checkpointed intermediate records.")
    retention = manifest.get("retention")
    if retention is not None:
        observed("Records intentionally excluded by retention", retention, refs=["manifest.json"],
                 reason="Explicit retention manifest; hidden results remain accounting discrepancies.")
    else:
        unavailable("Records intentionally excluded by retention", "No explicit retention/exclusion audit was exported.", state="unknown")
    observed("Recorder overflow, truncation and checkpoint loss", {"valid": reference_valid, "reasons": health_reasons},
             unit="checkpoint", verdict="pass" if reference_valid == "yes" else "inconclusive",
             refs=["receiver-health.json", "receiver-events.jsonl"])
    unavailable("Attribution evidence and confidence", "Exact IDs establish joins; absent wire/client checkpoints prevent loss localization.",
                state="unknown", value={"identity": "exact where retained", "loss_boundary": "unresolved"}, refs=evidence)

    dispatch = [row for row in client if _kind(row) in {"request_dispatched", "dispatch", "request_dispatch"} and _phase(row) == "measured"]
    measured_attempts = {row["attempt_id"] for row in measured_reconciliation}
    response_to_attempt = {row.get("response_id"): _id(row) for row in measured_results if row.get("response_id")}
    measured_reference_ids = {_id(row) for row in receiver if _kind(row) == "request_received" and
                              (_id(row) in measured_attempts or response_to_attempt.get(_metadata(row).get("response_id")) in measured_attempts)}
    receipts = [row for row in receiver if _kind(row) == "request_received" and _id(row) in measured_reference_ids]
    finishes = [row for row in receiver if _kind(row) == "response_finished" and _id(row) in measured_reference_ids]
    traffic: list[dict[str, Any]] = []
    receipt_times = [row["monotonic_ns"] for row in receipts if isinstance(row.get("monotonic_ns"), int)]
    dispatch_times = [row["monotonic_ns"] for row in dispatch if isinstance(row.get("monotonic_ns"), int)]
    thresholds = manifest.get("thresholds", {})
    lateness = [row.get("timing", {}).get("dispatch_lag_ms") for row in measured_results]
    lateness = [value for value in lateness if isinstance(value, (float, int))]
    if lateness:
        late_dist = distribution(lateness)
        allowed_p95 = thresholds.get("dispatch_lateness_p95_ms")
        allowed_max = thresholds.get("dispatch_lateness_max_ms")
        late_verdict = ("pass" if allowed_p95 is not None and allowed_max is not None
                        and late_dist["p95"] <= allowed_p95 and late_dist["max"] <= allowed_max
                        else "fail" if allowed_p95 is not None and allowed_max is not None else "inconclusive")
        for label in ("Scheduled-start lateness distribution", "Dispatch lateness"):
            observed(label, late_dist, unit="ms", verdict=late_verdict,
                     threshold={"p95_ms": allowed_p95, "max_ms": allowed_max},
                     reason="Baseline exported scheduled-to-dispatch intervals use the baseline's own relative monotonic clock.")
    else:
        late_verdict = "inconclusive"
    for label, field in (("Request preparation time", "client_preparation_ms"),
                         ("Connection-pool or execution-slot wait", "client_wait_ms")):
        values = [r.get("timing", {}).get(field) for r in measured_results]
        values = [value for value in values if isinstance(value, (float, int))]
        if values:
            if field == "client_wait_ms":
                observed(label, {"combined_scheduled_to_dispatch_ms": distribution(values), "isolated_pool_or_slot_wait": None},
                         unit="ms", state="unknown", verdict="inconclusive",
                         reason="Exported client_wait_ms is combined scheduled-to-dispatch delay. It cannot isolate connection-pool or execution-slot wait; dispatch-lateness gates are unchanged.")
            else:
                observed(label, distribution(values), unit="ms", verdict="inconclusive",
                         reason="Exported client-relative preparation duration; no independent preparation boundary or acceptance gate.")
    load_verdict = "inconclusive"
    if finite is not None and not correlation_incomplete and (totals["planned_count"] != finite or totals["receipt_count"] != finite or codes["duplicate_request"]):
        load_verdict = "fail"
    elif finite is not None and not correlation_incomplete and reference_valid == "yes" and mode in {"serial", "one_at_a_time", "single"}:
        load_verdict = "pass"
    if mode == "fixed_rate" and late_verdict == "fail":
        load_verdict = "fail"
    if mode in {"burst", "burst_pack"}:
        spreads = {"dispatch_ms": (max(dispatch_times) - min(dispatch_times)) / 1e6 if dispatch_times else None,
                   "receipt_ms": (max(receipt_times) - min(receipt_times)) / 1e6 if receipt_times else None}
        tolerance = thresholds.get("launch_spread_ms")
        good = spreads["dispatch_ms"] is not None and tolerance is not None and spreads["dispatch_ms"] <= tolerance
        launch_failed = spreads["dispatch_ms"] is not None and tolerance is not None and spreads["dispatch_ms"] > tolerance
        launch_covered = finite is not None and len(dispatch_times) == totals["dispatch_count"] == finite
        observed("Burst dispatch and receiver arrival spreads", spreads, unit="ms",
                 threshold={"client_dispatch_max_ms": tolerance, "receiver_application_receipt_spread": "observed; no numeric gate declared"},
                 reason="Launch threshold applies only to client dispatch spread. Receiver application receipt spread is separately observed; it does not establish packet arrival or share this numeric gate.",
                 verdict="fail" if launch_failed else "pass" if good and launch_covered and reference_valid == "yes" else "inconclusive")
        if load_verdict != "fail":
            if launch_failed and reference_valid == "yes":
                load_verdict = "fail"
            elif good and launch_covered and reference_valid == "yes" and totals["receipt_count"] == finite:
                load_verdict = "pass"
            else:
                load_verdict = "inconclusive"
    else:
        unavailable("Burst dispatch and receiver arrival spreads", "This scenario does not request burst traffic.", state="not_applicable")
    rate = scenario.get("rate_rps")
    spacing_verdict, spacing_details = ("inconclusive", {})
    if rate is not None:
        observed("Requested arrival rate", rate, unit="requests/s", refs=["manifest.json"], window="active phase")
        if mode == "fixed_rate":
            spacing_verdict, spacing_details = receiver_spacing(receipt_times, rate, thresholds)
            observed("Catch-up bursts and rate overshoots", spacing_details, unit="receiver inter-arrival ms",
                     state="measured" if reference_valid == "yes" else "unreliable",
                     verdict=spacing_verdict if reference_valid == "yes" else "inconclusive",
                     reason="Adjacent correlated endpoint receipts use one receiver clock. Frozen provisional spacing rule is independent of per-second count allowance.")
            if spacing_verdict == "fail" and reference_valid == "yes":
                load_verdict = "fail"
    else:
        unavailable("Requested arrival rate", "This scenario has no fixed arrival-rate contract.", state="not_applicable")
    target = scenario.get("concurrency") if mode == "sustained_concurrency" else None
    if target is not None:
        observed("Target concurrency", target, unit="unfinished client requests", refs=["manifest.json"], window="active phase")
    else:
        unavailable("Target concurrency", "This scenario has no sustained-concurrency contract.", state="not_applicable")
    if active_seconds is not None:
        observed("Actual load-generation duration", active_seconds, unit="s", refs=["manifest.json"], window="active phase")
        same_clock = clock_domain is not None and all(row.get("clock_domain") == clock_domain for row in dispatch)
        if same_clock:
            def count_in(rows):
                return sum(start <= row.get("monotonic_ns", -1) < end for row in rows)
            reference_same_clock = all(row.get("clock_domain") == clock_domain for row in receipts + finishes)
            rates = {"dispatch_rps": count_in(dispatch) / active_seconds if dispatch else None,
                     "receipt_rps": count_in(receipts) / active_seconds if reference_same_clock else None,
                     "completion_rps": count_in(finishes) / active_seconds if reference_same_clock else None,
                     "successful_completion_rps": sum(start <= row.get("monotonic_ns", -1) < end and
                                                       _outcome(_metadata(row).get("outcome")) == "success"
                                                       for row in finishes) / active_seconds if reference_same_clock else None,
                     "client_completion_rps": sum(start <= (_lifecycle(row) or (-1, -1))[1] < end
                                                   for row in measured_results) / active_seconds,
                     "successful_client_completion_rps": sum(start <= (_lifecycle(row) or (-1, -1))[1] < end
                                                              and _result_outcome(row) == "success" for row in measured_results) / active_seconds}
            observed("Actual dispatch, receipt, completion and successful-completion rates", rates, unit="requests/s",
                     window="client clock active [start,end); drain completions excluded", refs=["manifest.json", "client-events.jsonl", "receiver-events.jsonl"],
                     reason="Client metrics use one exported relative clock. Reference rates are null unless the reference clock and active phase are established comparable.")
            for index in range(math.ceil(active_seconds)) if reference_same_clock else ():
                left, right = start + int(index * 1e9), min(end, start + int((index + 1) * 1e9))
                received = sum(left <= timestamp < right for timestamp in receipt_times)
                traffic.append({"second": index, "duration_seconds": (right-left)/1e9,
                                "receipts": received, "receipt_rps": received/((right-left)/1e9)})
            if rate is not None:
                tolerance = thresholds.get("rate_window_error_fraction", thresholds.get("rate_window_relative_error"))
                allowance = thresholds.get("rate_window_count_allowance", 0)
                deviations = [max(0, abs(row["receipts"] - rate * row["duration_seconds"]) - allowance) /
                              (rate * row["duration_seconds"]) for row in traffic] if rate > 0 else []
                rate_pass = bool(deviations) and tolerance is not None and max(deviations) <= tolerance
                observed("Per-second traffic history and rate deviations", {"bins": traffic, "maximum_fractional_error": max(deviations) if deviations else None,
                         "receiver_spacing": spacing_details},
                         unit="requests/s", window="active phase; final bin uses its actual width", threshold=tolerance,
                         verdict="pass" if rate_pass and reference_valid == "yes" else "fail" if deviations and tolerance is not None else "inconclusive")
                if load_verdict != "fail":
                    all_gates = rate_pass and spacing_verdict == late_verdict == "pass"
                    load_verdict = "pass" if all_gates and reference_valid == "yes" else "fail" if not rate_pass and tolerance is not None and deviations else "inconclusive"
        else:
            unavailable("Actual dispatch, receipt, completion and successful-completion rates", "Active window and event clocks are not proven comparable.", state="unknown")
        interval_rows = [result for result in measured_results if _lifecycle(result) is not None
                         and result.get("clock_domain") == clock_domain]
        if interval_rows and len(interval_rows) == len(measured_results):
            occupancy = time_weighted_concurrency([_lifecycle(r) for r in interval_rows], start, end,
                                                  target, thresholds.get("concurrency_tolerance", (target or 0) * thresholds.get("occupancy_relative_tolerance", 0)))
            observed("Actual time-weighted mean, minimum and maximum concurrency", occupancy, unit="unfinished client requests", window="active phase")
            if target is not None:
                required = thresholds.get("occupancy_min_percent", thresholds.get("occupancy_time_coverage", 0) * 100)
                ramp_ns = int(thresholds.get("ramp_exclusion_seconds", 0) * 1e9)
                after_ramp = start + ramp_ns
                qualified_occupancy = time_weighted_concurrency(
                    [_lifecycle(r) for r in interval_rows],
                    after_ramp, end, target, target * thresholds.get("occupancy_relative_tolerance", 0)) if after_ramp < end else None
                occupancy_percent = qualified_occupancy["within_tolerance_percent"] if qualified_occupancy else None
                occupancy_pass = required is not None and occupancy_percent is not None and occupancy_percent >= required
                if occupancy_percent is None:
                    unavailable("Active time within concurrency tolerance", "Ramp exclusion consumes the whole active phase; no qualification window remains.")
                else:
                    observed("Active time within concurrency tolerance", occupancy_percent, unit="percent", threshold=required,
                             window="active phase after declared ramp exclusion", verdict="pass" if occupancy_pass else "fail" if required is not None else "inconclusive")
                if load_verdict != "fail":
                    # Occupancy alone does not prove timely replenishment or a non-batch scheduler.
                    load_verdict = "inconclusive" if occupancy_pass or occupancy_percent is None else "fail" if required is not None else "inconclusive"
                lifecycle = sorted([(_lifecycle(r)[0], 1) for r in interval_rows] +
                                   [(_lifecycle(r)[1], -1) for r in interval_rows])
                count, filled, vacancies, gaps = 0, False, [], []
                for timestamp, change in lifecycle:
                    if timestamp >= end:
                        break
                    if change < 0:
                        count -= 1
                        if filled and timestamp >= start:
                            vacancies.append(timestamp)
                    else:
                        if vacancies:
                            vacancy = vacancies.pop(0)
                            if timestamp >= after_ramp:
                                gaps.append((timestamp - vacancy) / 1e6)
                        count += 1
                        filled |= count >= target
                if gaps:
                    gap_dist = distribution(gaps)
                    p95_gate, max_gate = thresholds.get("replacement_gap_p95_ms"), thresholds.get("replacement_gap_max_ms")
                    gap_pass = p95_gate is not None and max_gate is not None and gap_dist["p95"] <= p95_gate and gap_dist["max"] <= max_gate
                    observed("Replacement gap distribution", gap_dist, unit="ms", threshold={"p95_ms": p95_gate, "max_ms": max_gate},
                             verdict="pass" if gap_pass else "fail" if p95_gate is not None and max_gate is not None else "inconclusive",
                             reason=f"Same-clock completion-to-next-replacement dispatch. Unreplaced vacancies at active end: {len(vacancies)}; right-censored gaps retained as limitation.")
                    if occupancy_pass and gap_pass and reference_valid == "yes" and load_verdict != "fail":
                        load_verdict = "pass"
                    elif not gap_pass and p95_gate is not None and max_gate is not None:
                        load_verdict = "fail"
    requested_duration = scenario.get("active_seconds", scenario.get("duration_seconds")) if mode in {"sustained_concurrency", "fixed_rate", "poisson"} else None
    if requested_duration is not None:
        observed("Requested active duration", requested_duration, unit="s", refs=["manifest.json"], window="active phase")
        if active_seconds is not None:
            allowed = thresholds.get("active_duration_error_seconds")
            if allowed is not None and abs(requested_duration - active_seconds) > allowed:
                load_verdict = "fail"
                observed("Early termination and reason", {"requested_seconds": requested_duration, "actual_seconds": active_seconds},
                         unit="s", verdict="fail", threshold=allowed, refs=["manifest.json"])
    else:
        unavailable("Requested active duration", "Finite-count scenario has no duration contract.", state="not_applicable")

    receiver_window = manifest.get("receiver_active_window", {})
    receiver_start, receiver_end = receiver_window.get("start_ns"), receiver_window.get("end_ns")
    if rate is not None and isinstance(receiver_start, int) and isinstance(receiver_end, int) and receiver_end > receiver_start:
        receiver_seconds = (receiver_end - receiver_start) / 1e9
        receiver_domain = receiver_window.get("clock_domain")
        if all(row.get("clock_domain") == receiver_domain for row in receipts + finishes):
            traffic = []
            for index in range(math.ceil(receiver_seconds)):
                left = receiver_start + index * 1_000_000_000
                right = min(receiver_end, left + 1_000_000_000)
                received = sum(left <= timestamp < right for timestamp in receipt_times)
                traffic.append({"second": index, "duration_seconds": (right-left)/1e9,
                                "receipts": received, "receipt_rps": received/((right-left)/1e9)})
            allowance = thresholds.get("rate_window_count_allowance", 0)
            deviations = [max(0, abs(row["receipts"] - rate * row["duration_seconds"]) - allowance) /
                          (rate * row["duration_seconds"]) for row in traffic]
            tolerance = thresholds.get("rate_window_relative_error", thresholds.get("rate_window_error_fraction"))
            aligned = receiver_window.get("qualification") == "qualified" and reference_valid == "yes"
            within = tolerance is not None and max(deviations) <= tolerance
            observed("Per-second traffic history and rate deviations", {"bins": traffic, "maximum_fractional_error": max(deviations),
                     "window": receiver_window, "receiver_spacing": spacing_details}, unit="requests/s", threshold=tolerance,
                     state="measured" if aligned else "unreliable",
                     verdict="pass" if aligned and within else "fail" if aligned and tolerance is not None else "inconclusive",
                     reason=receiver_window.get("reason", "Receiver-local window; alignment qualification is separate."), window="receiver-local declared [start,end)")
            receiver_rates = {"receipt_rps": sum(receiver_start <= timestamp < receiver_end for timestamp in receipt_times) / receiver_seconds,
                              "completion_rps": sum(receiver_start <= row.get("monotonic_ns", -1) < receiver_end for row in finishes) / receiver_seconds,
                              "successful_completion_rps": sum(receiver_start <= row.get("monotonic_ns", -1) < receiver_end and
                                                               _outcome(_metadata(row).get("outcome")) == "success" for row in finishes) / receiver_seconds}
            current_rates = report["sections"]["2_traffic_delivery"]["actual_dispatch_receipt_completion_and_successful_completion_rates"]
            combined = {"client_active_window": current_rates["value"], "receiver_local_window": receiver_rates,
                        "receiver_window_qualification": receiver_window.get("qualification", "unknown")}
            observed("Actual dispatch, receipt, completion and successful-completion rates", combined, unit="requests/s",
                     reason="Each side uses its own declared clock and full time-window denominator; receiver phase alignment remains explicit.",
                     window="separate client-active and receiver-local windows")
            if aligned and load_verdict != "fail":
                load_verdict = "pass" if within and late_verdict == spacing_verdict == "pass" else "fail" if not within or "fail" in {late_verdict, spacing_verdict} else "inconclusive"

    if isinstance(manifest.get("wall_seconds"), (int, float)):
        observed("Total wall-clock duration", manifest["wall_seconds"], unit="s", refs=["manifest.json"],
                 reason="External driver monotonic elapsed time includes startup, product execution and owned endpoint shutdown.")
    if isinstance(active.get("drain_seconds"), (int, float)):
        observed("Final response-drain duration", active["drain_seconds"], unit="s", refs=["manifest.json"],
                 reason="Exported phase duration; end-boundary definition retained in manifest.")
    if active.get("early_termination_reason") is not None or manifest.get("product_abort_reason") is not None:
        observed("Early termination and reason", active.get("early_termination_reason") or manifest.get("product_abort_reason"),
                 unit="reason", verdict="fail" if healthy else "inconclusive", refs=["manifest.json"])
        if healthy and requested_duration is not None:
            load_verdict = "fail"
    budgets = {field: active[field] for field in ("not_offered", "fully_offered", "sustained_budget", "sustained_completion", "stage_reasons") if field in active}
    if budgets:
        observed("Budget exhaustion and effect on requested workload", budgets, unit="observations", refs=["manifest.json"],
                 verdict="fail" if active.get("not_offered", 0) or active.get("fully_offered") is False else "inconclusive")
    drops = {field: active[field] for field in ("dropped_local", "dropped_late", "not_offered") if field in active}
    if drops:
        observed("Local-capacity and late-arrival drops", drops, unit="requests", refs=["manifest.json"],
                 verdict="fail" if any(value for value in drops.values() if isinstance(value, (int, float))) else "pass")
        if healthy and any(value for value in drops.values() if isinstance(value, (int, float))):
            load_verdict = "fail"

    delivery_scope = "Applicable gates are derived from retained evidence; absent observations never pass."
    if mode in {"burst", "burst_pack"}:
        delivery_scope = ("Finite burst delivery gates evaluate client dispatch launch spread and exact receipt identities/count; known gate failures prevent delivery acceptance. "
                          "Receiver application receipt spread is retained separately; the client launch threshold does not apply to it or prove wire arrival timing.")
    if (mode == "fixed_rate" and load_verdict != "fail" and reference_valid == "yes"
            and not correlation_incomplete and finite is not None
            and totals["planned_count"] == totals["dispatch_count"] == totals["receipt_count"] == finite
            and len(lateness) == finite and spacing_verdict == late_verdict == "pass"
            and active_seconds is not None and requested_duration is not None
            and abs(active_seconds-requested_duration) <= thresholds.get("active_duration_error_seconds", -1)
            and len(dispatch_times) == finite and all(start <= timestamp < end for timestamp in dispatch_times)):
        load_verdict = "pass"
        delivery_scope = ("Provisional same-host delivery only: exact intended identities, client own-clock active phase/lateness/duration, "
                          "and independent receiver-local spacing pass. Receiver absolute phase alignment and client-read timing remain unqualified.")
    if slot_correlation_incomplete and load_verdict == "pass":
        load_verdict = "inconclusive"

    # Arithmetic checks remain separate from real-time reference qualification.
    timings = [result.get("timing", {}) for result in results]
    latencies = [timing["latency_ms"] for timing in timings if isinstance(timing.get("latency_ms"), (int, float))]
    if latencies:
        observed("Reported and reference completion latency", {"reported": distribution(latencies, qualified_count=0),
                 "reference_boundary": manifest.get("reference", {}).get("boundary", "endpoint_application")}, unit="ms",
                 state="unreliable", verdict="inconclusive", reason="Client durations retained; application emission duration is not independent client completion latency.")
    if scenario.get("stream") is False:
        unavailable("Reported and reference answer-event gaps", "Non-streaming responses have no inter-answer event gaps.", state="not_applicable")
    # Paired durations use each side's own clock. Differences retain their
    # distinct observation boundaries and are diagnostic, not accuracy proof.
    receipt_by_id = {_metadata(row).get("response_id"): row for row in receipts}
    reference_by_attempt: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in receiver:
        if _id(row):
            reference_by_attempt[_id(row)].append(row)
    paired: dict[str, list[float]] = defaultdict(list)
    for result in measured_results:
        receipt = receipt_by_id.get(result.get("response_id"))
        if not receipt or not isinstance(receipt.get("monotonic_ns"), int):
            continue
        attempt = _id(receipt)
        observed_rows = [row for row in reference_by_attempt[attempt] if
                         row.get("clock_domain") == receipt.get("clock_domain")]
        answer_times = [row["monotonic_ns"] for row in observed_rows if _kind(row) == "response_event"
                        and _metadata(row).get("content") and isinstance(row.get("monotonic_ns"), int)]
        reasoning_times = [row["monotonic_ns"] for row in observed_rows if _kind(row) == "response_event"
                           and _metadata(row).get("reasoning") and isinstance(row.get("monotonic_ns"), int)]
        terminal_times = [row["monotonic_ns"] for row in observed_rows if _kind(row) == "response_finished"
                          and isinstance(row.get("monotonic_ns"), int)]
        vectors = {"first_answer_ms": (min(answer_times) - receipt["monotonic_ns"]) / 1e6 if answer_times else None,
                   "first_reasoning_ms": (min(reasoning_times) - receipt["monotonic_ns"]) / 1e6 if reasoning_times else None,
                   "latency_ms": (max(terminal_times) - receipt["monotonic_ns"]) / 1e6 if terminal_times else None,
                   "max_stream_gap_ms": max((right-left)/1e6 for left, right in zip(answer_times, answer_times[1:])) if len(answer_times) >= 2 else None}
        if storage is not None:
            vectors = storage.application_by_response_id.get(result.get("response_id"), vectors)
        for name, app_value in vectors.items():
            client_value = result.get("timing", {}).get(name)
            if app_value is not None:
                paired[f"application_{name}"].append(app_value)
            if isinstance(client_value, (int, float)):
                paired[f"client_{name}"].append(client_value)
                if app_value is not None:
                    paired[f"client_minus_application_{name}"].append(client_value - app_value)
    for label, names in (
        ("Reported and reference first-byte, first-answer and first-reasoning timings", ("first_answer_ms", "first_reasoning_ms")),
        ("Reported and reference completion latency", ("latency_ms",)),
        ("Reported and reference answer-event gaps", ("max_stream_gap_ms",)),
    ):
        available = {name: distribution(values, qualified_count=0) for name, values in paired.items()
                     if any(name.endswith(suffix) for suffix in names)}
        if available and not (scenario.get("stream") is False and label.endswith("gaps")):
            observed(label, available, unit="ms", state="unreliable", verdict="inconclusive",
                     reason="Paired per-ID within-side durations only. Client includes transport/read work; application starts at receipt and ends at drain/terminal. First byte and packet timing remain unobserved.",
                     refs=["receiver-events.jsonl", "results.jsonl"])
    reference = manifest.get("reference", {})
    uncertainty = reference.get("uncertainty_ms")
    timing_budget = thresholds.get("timing_error_ms")
    trusted_boundary = reference.get("boundary") in {"wire_capture", "independent_client_capture"}
    deterministic = manifest.get("deterministic_control") is True
    trusted_boundary |= deterministic and reference.get("boundary") == "deterministic_vectors"
    timing_goldens = sum("timing" in _expected(row, scenario) for row in planned)
    precision = isinstance(uncertainty, (int, float)) and isinstance(timing_budget, (int, float)) and uncertainty <= timing_budget
    timing_qualified = bool(results) and timing_goldens == len(results) and reference_valid == "yes" and trusted_boundary and precision and not codes["timing_mismatch"] and not codes["timestamp_arithmetic"] and not codes["timing_missing"]
    observed_timing = _observer_timing(directory, manifest, measured_results, thresholds, write=write) if not safety_limited else None
    if observed_timing is not None:
        timing_qualified = observed_timing["qualified"] and reference_valid == "yes" and not codes["timestamp_arithmetic"]
    timing_state = "qualified" if timing_qualified else "unreliable" if latencies or codes["timing_mismatch"] else "unknown"
    timing_reason = ("Independent same-boundary timing vectors meet frozen error/coverage requirements." if timing_qualified else
                     "Timing lacks a complete independent same-boundary reference, precision/clock bound, or valid arithmetic; app writes are not wire/client reads.")
    if observed_timing is not None:
        timing_reason = observed_timing["reason"] + " Body-yield/socket-return observations are not packet arrival and do not qualify original unobserved runs."
        observed_metrics = observed_timing["metrics"]
        references = [name for name in ("timing-events.jsonl", "timing-health.json", "manifest.json") if (directory / name).is_file()]
        for label, names in (
            ("Reported and reference first-byte, first-answer and first-reasoning timings", ("first_answer_ms", "first_reasoning_ms")),
            ("Reported and reference completion latency", ("latency_ms",)),
            ("Reported and reference answer-event gaps", ("max_stream_gap_ms",)),
        ):
            values = {name: observed_metrics[name] for name in names if name in observed_metrics}
            if values:
                observed(label, {"metrics": values, "first_byte": "Not assessed by this primary timer check; supplemental socket-return analysis is separate. Packet arrival is unobserved."} if len(names) > 1 else values,
                         unit="ms", state="measured" if timing_qualified else "unreliable",
                         verdict="pass" if timing_qualified else "fail" if observed_timing["measurement_failure"] else "inconclusive",
                         reason=timing_reason, refs=references)
        if "body_read_to_semantic_ready" in observed_metrics:
            observed("Independently measurable response-read delay", observed_metrics["body_read_to_semantic_ready"], unit="ms",
                     state="measured" if timing_qualified else "unreliable", verdict="inconclusive",
                     reason="Independently bracketed body-read return to semantic-ready delay only; network/packet arrival delay is unobserved.", refs=references)
        observed("Timing error distributions and allowed tolerances", observed_timing, unit="ms / declared reference bounds",
                 state="measured" if timing_qualified else "unreliable", threshold=timing_budget,
                 verdict="fail" if observed_timing["measurement_failure"] else "pass" if timing_qualified else "inconclusive", reason=timing_reason, refs=references)
    measured_latency_count = sum(isinstance(row.get("timing", {}).get("latency_ms"), (int, float))
                                 for row in measured_results)
    measured_expected_count = (None if manifest.get("planned_semantics") == "duration_admission_intent"
                               else sum(_kind(row) != "sustained_admission_intent" for row in measured_planned))
    coverage_denominator = measured_expected_count if measured_expected_count is not None else measured_latency_count
    qualified_measured_count = observed_timing["qualified_count"] if observed_timing is not None else len(measured_results) if timing_qualified else 0
    observed("Qualified measurement count and percentage", {"qualified_count": qualified_measured_count,
             "observed_count": measured_latency_count, "expected_count": measured_expected_count,
             "percentage": 100 * qualified_measured_count / coverage_denominator if coverage_denominator else None}, unit="measurements / percent",
             verdict="pass" if timing_qualified else "inconclusive", reason=timing_reason +
             " Coverage counts exclude auxiliary phases; duration admission intent has no finite expected request count.")
    unavailable("Reference clock/network/observation uncertainty", "Reference uncertainty must include observation boundary and clock-domain comparability.",
                state="unknown" if uncertainty is None else "unreliable", value=reference, refs=["manifest.json"])
    if observed_timing is not None:
        observed("Reference clock/network/observation uncertainty", {
            "observer_health": observed_timing.get("health"), "overhead": observed_timing.get("overhead"),
            "boundary": observed_timing["scope"], "network_packet_arrival": "not_measured",
            "original_unobserved_run_qualification": False}, unit="bounded observer clock intervals / explicit unknowns",
            state="measured" if observed_timing.get("health", {}).get("reference_valid_for_observed_run") else "unreliable",
            verdict="inconclusive", reason="Named observer clock/body boundaries have measured bounds; packet arrival/network transit remains unobserved. Overhead applies only to the matching observed profile.",
            refs=references)
    if observed_timing is None or "body_read_to_semantic_ready" not in observed_timing["metrics"]:
        unavailable("Independently measurable response-read delay", "No lower-level packet/client-read paired observation is available.")
    unavailable("Buffered/coalesced events and unavailable token-level timing", "SSE event spacing cannot establish individual token timing; bundled semantic events can share a write.",
                state="unknown", value=manifest.get("stream_observation"), refs=["manifest.json"])
    observed("Unreliable, missing or unknown measurements, by reason", {"reason": timing_reason, "golden_timing_records": timing_goldens,
             "arithmetic_failures": codes["timestamp_arithmetic"], "timing_mismatches": codes["timing_mismatch"]}, unit="measurements")
    if deterministic:
        results_by_id = {_id(row): row for row in results}
        errors = [abs(results_by_id[_id(plan)]["timing"][name] - desired) / (1e6 if name.endswith("_ns") else 1)
                  for plan in planned if _id(plan) in results_by_id
                  for name, desired in _expected(plan, scenario).get("timing", {}).items()
                  if isinstance(results_by_id[_id(plan)].get("timing", {}).get(name), (int, float))]
        observed("Timing error distributions and allowed tolerances", distribution(errors, qualified_count=None),
                 unit="ms", threshold=timing_budget, verdict="fail" if codes["timing_mismatch"] or codes["timestamp_arithmetic"] else "pass",
                 reason="Deterministic arithmetic controls are distinct from live clock/observer qualification.")
    fault = scenario.get("fault", scenario.get("expected_fault"))
    if fault:
        observed("Injected fault, timing and expected behavior", fault, unit="fault specification", refs=["manifest.json"])
    else:
        unavailable("Injected fault, timing and expected behavior", "Healthy delivery case; no fault was injected.", state="not_applicable")
    observed("Requests unresolved at shutdown", outcomes.get("unresolved", 0), verdict="fail" if outcomes.get("unresolved", 0) else "pass")
    observed("Partial-evidence durability and last confirmed checkpoint", {"reference_valid": reference_valid, "reasons": health_reasons},
             unit="checkpoint", verdict="pass" if reference_valid == "yes" else "inconclusive", refs=["receiver-health.json"])
    stability_verdict = "inconclusive"
    if manifest.get("resource_summary"):
        resources = manifest["resource_summary"]
        for label, field in (("CPU by process/thread and aggregate CPU", "cpu"),
                             ("RAM per process and aggregate RAM, peak and growth", "ram"),
                             ("Connections, file descriptors and leaks", "connections")):
            if field in resources:
                observed(label, resources[field], unit="see values", refs=["resources.csv"],
                         verdict="inconclusive",
                         reason="Observation only; no acceptance gate for CPU/connections or short-run RAM growth. Samples do not prove unobserved peaks, leak absence, or one-hour stability.")
        growth_budget = thresholds.get("memory_growth_bytes_per_hour")
        if report["experiment_id"] == "E7" and active_seconds is not None and active_seconds >= 3600 and isinstance(growth_budget, (int, float)):
            allowed_growth = growth_budget * active_seconds / 3600
            roles = resources.get("ram", {})
            checks = {role: {"observed_growth_bytes": roles.get(role, {}).get("growth"),
                             "allowed_growth_bytes": allowed_growth,
                             "sample_count": roles.get(role, {}).get("sample_count", 0)}
                      for role in ("candidate", "reference", "driver")}
            exceeded = [role for role, value in checks.items() if isinstance(value["observed_growth_bytes"], (int, float))
                        and value["observed_growth_bytes"] > allowed_growth]
            covered = all(isinstance(value["observed_growth_bytes"], (int, float)) and value["sample_count"] >= 2 for value in checks.values())
            stability_verdict = "fail" if exceeded else "pass" if covered else "inconclusive"
            value = {"observations": roles, "growth_gate": checks, "exceeded_roles": exceeded,
                     "actual_active_seconds": active_seconds, "retention_deduction_bytes": None,
                     "scope": "sampled first-to-last process RSS; no deduction for retained records; no leak/unsampled-peak proof"}
            observed("RAM per process and aggregate RAM, peak and growth", value, unit="bytes", refs=["resources.csv", "manifest.json"],
                     threshold={"memory_growth_bytes_per_hour": growth_budget}, verdict=stability_verdict,
                     reason="E7 observed at least one hour; frozen RSS growth allowance applies to candidate, reference and driver. Finite sampling limits remain explicit.")
            observed("Memory, connections and backlog trends over time", {"rss_growth": checks, "exceeded_roles": exceeded,
                     "connections": "not_measured", "backlog": "not_measured"}, unit="bytes / explicit telemetry states",
                     threshold={"memory_growth_bytes_per_hour": growth_budget}, verdict="fail" if exceeded else "inconclusive",
                     reason="RSS threshold can fail stability independently; missing connection/backlog telemetry cannot pass their endurance checks.", refs=["resources.csv", "manifest.json"])

    direct_failure = bool(set(codes)-UNCERTAIN_ISSUES)
    correlation_missing = correlation_incomplete or slot_correlation_incomplete or codes["not_dispatched_unconfirmed"]
    correctness = "fail" if direct_failure else "inconclusive" if artifact_errors or correlation_missing or not results or reference_valid != "yes" else "pass"
    evaluation = "fail" if codes["score_mismatch"] else "pass" if scoring_expected == len(golden_rows) and scoring_expected and score_observed >= scoring_expected and not codes["missing_result"] else "inconclusive"
    expected_fault, fault_details = _fault_behavior(scenario, manifest, client, receiver, results, codes)
    if expected_fault != "not_applicable":
        observed("Observed detection, classification and recovery", fault_details, unit="observations",
                 verdict=expected_fault, reason=fault_details.get("reason", "Injection and observed outcomes checked independently of healthy delivery."))
        if fault_details.get("cleanup_seconds") is not None:
            observed("Shutdown time and deadline", {"observed_seconds": fault_details["cleanup_seconds"],
                     "deadline_seconds": fault_details["cleanup_deadline_seconds"]}, unit="s",
                     verdict="pass" if 0 <= fault_details["cleanup_seconds"] <= fault_details["cleanup_deadline_seconds"] else "fail")
    expected_delivery_shortfall = scenario.get("fault") in {"connection_refused", "global_budget", "user_cancel"} and expected_fault == "pass"
    aggregate_load_verdict = "inconclusive" if expected_delivery_shortfall and load_verdict == "fail" else load_verdict
    timing_failure = "fail" if observed_timing is not None and observed_timing["measurement_failure"] else "inconclusive"
    overall = "fail" if "fail" in (correctness, evaluation, aggregate_load_verdict, stability_verdict, expected_fault, timing_failure) else "pass" if correctness == evaluation == load_verdict == "pass" and timing_qualified else "inconclusive"
    if safety_limited:
        overall, load_verdict, correctness, evaluation = ("inconclusive",) * 4
    comparison_invalid = manifest.get("baseline_unchanged") is False
    if comparison_invalid:
        overall = "inconclusive"
    if any(_kind(row) == "resource_safety_stop" for row in client):
        report["execution_status"] = "aborted"
    if report["execution_status"] in {"blocked", "not_run"}:
        overall = "not run"
        load_verdict = correctness = evaluation = "inconclusive"
        timing_state, reference_valid = "unknown", "unknown"
        applicable_config = {"requests_planned", "requested_arrival_rate", "target_concurrency",
                             "requested_active_duration", "injected_fault_timing_and_expected_behavior"}
        for section, fields in report["sections"].items():
            if section == "12_final_verdict":
                continue
            for name in fields:
                if name not in applicable_config:
                    fields[name] = metric(reason=manifest.get("execution_reason", "Case was not executed; no observation was collected."))
    if report["execution_status"] == "aborted" and overall == "pass":
        overall = "inconclusive"
    capacity = "not_applicable" if not healthy or deterministic else "disqualifies" if overall == "fail" else "inconclusive"
    limitations = [*(health_reasons if reference_valid != "yes" else []), *artifact_errors]
    limitations.extend(manifest.get("limitations", []))
    if expected_delivery_shortfall and load_verdict == "fail":
        limitations.append("Intentional injected fault prevented requested healthy delivery. The delivery failure remains recorded; independently passing fault behavior alone does not establish a product fault or capacity qualification.")
    if comparison_invalid:
        limitations.append("Baseline source/dependency identity changed: this comparison is invalid and requires a new pinned rerun.")
    if not timing_qualified:
        limitations.append(timing_reason)
    limitations.append("Same-host endpoint shares CPU/RAM/network constraints; no absolute generator-capacity qualification.")
    verdicts = {"requested_load_delivered": load_verdict, "response_accounting_correctness": correctness,
                "measurement_accuracy": timing_state, "evaluation_correctness": evaluation,
                "reference_test_valid": reference_valid, "expected_fault_behavior": expected_fault,
                "overall_experiment_result": overall, "capacity_contribution": capacity}
    report["verdicts"] = verdicts
    for label, value in verdicts.items():
        state = "measured" if value not in {"unknown", "inconclusive", "not run", "not_applicable"} else "unknown"
        if value == "not_applicable":
            state = "not_applicable"
        verdict = "pass" if value in {"pass", "qualified", "yes", "qualifies"} else "fail" if value in {"fail", "disqualifies"} else "not_applicable" if state == "not_applicable" else "inconclusive"
        observed(label, value, unit="verdict", state=state, verdict=verdict,
                 reason=timing_reason if label == "measurement_accuracy" else
                 "; ".join(health_reasons) if label == "reference_test_valid" else
                 delivery_scope if label == "requested_load_delivered" else
                 "This observed run cannot qualify the requested profile; this verdict does not identify an absolute product capacity limit or attribute a harness safety abort to a Giraffe defect."
                 if label == "capacity_contribution" and value == "disqualifies" else
                 "Applicable gates are derived from retained evidence; absent observations never pass.")
    unavailable("Highest reliably supported workload", "Study-level qualified range requires comparable repeats, duration, resources and independent reference; one case cannot establish it.",
                state="unknown", value="../../summary.md", refs=["manifest.json"])
    observed("Limitations, failed gates and next diagnostic", {"limitations": limitations, "issues": issues,
             "next_diagnostic": "Inspect reconciliation identities, retain missing client boundaries, and obtain independent same-boundary timing evidence."},
             unit="evidence-backed statements", verdict="fail" if overall == "fail" else "inconclusive")
    if write:
        (directory / "reconciliation.jsonl").write_text("".join(json.dumps(row, allow_nan=False) + "\n" for row in reconciled))
        with (directory / "traffic-series.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["second", "duration_seconds", "receipts", "receipt_rps"])
            writer.writeheader()
            writer.writerows(traffic)
        write_report(directory, report)
    return report


def verify_case(case_dir: str | Path, manifest: dict[str, Any] | None = None,
                *, write: bool = True, disk_backed: bool = True, backend_statistics: dict[str, Any] | None = None) -> dict[str, Any]:
    if not disk_backed:
        return _verify_case(case_dir, manifest, write=write)
    storage = _EvidenceStore(Path(case_dir))
    try:
        report = _verify_case(case_dir, manifest, write=write, storage=storage)
        statistics = storage.statistics()
        if backend_statistics is not None:
            backend_statistics.update(statistics)
        if write:
            (Path(case_dir) / "verification-backend.json").write_text(json.dumps(statistics, indent=2) + "\n")
        return report
    finally:
        storage.close()


generate_report = verify_case


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_dir", type=Path)
    args = parser.parse_args()
    report = verify_case(args.case_dir)
    print(json.dumps(report["verdicts"], indent=2))


if __name__ == "__main__":
    main()
