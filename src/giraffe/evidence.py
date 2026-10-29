"""Append-only, bounded-durability evidence independent of request event loops."""

from __future__ import annotations

from copy import deepcopy
import contextvars
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import queue
import threading
import time

from pydantic import ValidationError

from .models import CHECK_NAMES, CheckResult, RequestRecord, RunReport
from .storage import fsync_directory

logger = logging.getLogger(__name__)
FSYNC_RECORDS = 50
FSYNC_SECONDS = 1.0
QUEUE_SIZE = 1024
CLOSE_SECONDS = 5.0
QUEUE_WAIT_SECONDS = 0.1


def _has_non_finite(value) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_has_non_finite(child) for child in value.values())
    if isinstance(value, (tuple, list)):
        return any(_has_non_finite(child) for child in value)
    return False


def _json_line(value: dict, *, non_finite: bool = False) -> str:
    """Keep invalid endpoint numbers explicit without writing non-JSON literals."""
    def finite(item):
        nonlocal non_finite
        if isinstance(item, float) and not math.isfinite(item):
            non_finite = True
            return None
        if isinstance(item, dict):
            return {key: finite(child) for key, child in item.items()}
        if isinstance(item, (tuple, list)):
            return [finite(child) for child in item]
        return item

    value = finite(value)
    if non_finite:
        value["non_finite_values"] = True
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"


class EvidenceWriter:
    def __init__(self, directory: Path | str, manifest: dict | None = None,
                 run_id: str | None = None, started_at: str | None = None, *,
                 fsync_records: int = FSYNC_RECORDS, fsync_seconds: float = FSYNC_SECONDS,
                 queue_size: int = QUEUE_SIZE, close_seconds: float = CLOSE_SECONDS):
        self.directory = Path(directory)
        self.manifest = deepcopy(manifest or {})
        self.run_id = run_id or self.directory.name
        self.started_at = started_at or datetime.now(timezone.utc).isoformat()
        self.fsync_records = max(1, fsync_records)
        self.fsync_seconds = max(0.001, fsync_seconds)
        self._queue = queue.Queue(maxsize=max(1, queue_size))
        self._error = None
        self._closed = False
        self._stop = threading.Event()
        self.close_seconds = max(0.001, close_seconds)
        self.dropped_count = 0
        context = contextvars.copy_context()
        self._thread = threading.Thread(target=context.run, args=(self._write,),
                                        name="giraffe-evidence", daemon=True)
        self._thread.start()

    def record_finished(self, record: RequestRecord) -> None:
        # Judging happens later and mutates the original record. Snapshot now,
        # then serialize and apply retention entirely on the writer thread.
        if self._closed or self._error is not None:
            self.dropped_count += 1
            return
        # Completed transport payloads are no longer mutated. Judging replaces
        # scalar/model fields, so copy their bindings without traversing retained
        # output/events on the measurement loop. Retention runs on the writer.
        self._enqueue(("record", record.model_copy()))

    def _enqueue(self, item) -> None:
        if self._closed or self._error is not None:
            self.dropped_count += 1
            return
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self.dropped_count += 1

    def progress(self, event: dict) -> None:
        if (event.get("event", "").startswith(("check_", "condition_"))
                                 or event.get("check") or event.get("condition_summary")):
            if self._closed or self._error is not None:
                self.dropped_count += 1
                return
            # Request bodies have their own record line; do not traverse or duplicate
            # them when persisting a condition transition alongside a completion.
            value = {key: child for key, child in event.items() if key != "request"}
            for key in ("condition", "condition_summary"):
                if key in value:
                    value[key] = deepcopy(value[key])
            self._enqueue(("progress", value))

    def _retained(self, record: RequestRecord | None = None, *, checks=()) -> RunReport:
        from .reporting import _retained

        report = RunReport.model_construct(run_id=self.run_id, started_at=self.started_at,
            finished_at=self.started_at, overall="inconclusive", manifest=self.manifest,
            requests=[] if record is None else [record], checks=list(checks))
        return _retained(report)

    def _write(self):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.directory / "evidence.jsonl",
                                 os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                header = {"type": "header", "run_id": self.run_id, "started_at": self.started_at,
                          "manifest": self._retained().manifest}
                stream.write(_json_line(header, non_finite=_has_non_finite(self.manifest)))
                stream.flush()
                os.fsync(stream.fileno())
                fsync_directory(self.directory)
                pending = 0
                last_dropped = 0
                last_sync = time.monotonic()
                while True:
                    timeout = min(QUEUE_WAIT_SECONDS, max(0, self.fsync_seconds - (
                        time.monotonic() - last_sync))) if pending else QUEUE_WAIT_SECONDS
                    try:
                        item = self._queue.get(timeout=timeout)
                    except queue.Empty:
                        item = None
                    if self.dropped_count != last_dropped:
                        last_dropped = self.dropped_count
                        stream.write(_json_line({"type": "dropped", "count": last_dropped}))
                        pending += 1
                    if item is None and self._stop.is_set() and self._queue.empty():
                        stream.flush()
                        os.fsync(stream.fileno())
                        break
                    if item is not None:
                        kind, value = item
                        non_finite = _has_non_finite(value) if kind != "record" else False
                        if kind == "record":
                            non_finite = _has_non_finite(value.model_dump(mode="python"))
                            value = self._retained(value).requests[0].model_dump(mode="json")
                        elif self.manifest.get("config", {}).get("retention", "failures") == "none":
                            # Apply the same summary scrubbing as final reports.
                            from .reporting import _without_answer_excerpts
                            value = deepcopy(value)
                            if isinstance(value.get("check"), dict):
                                try:
                                    check = CheckResult.model_validate(value["check"])
                                except ValidationError:
                                    if isinstance(value["check"].get("summary"), str):
                                        value["check"]["summary"] = _without_answer_excerpts(value["check"]["summary"])
                                else:
                                    value["check"] = self._retained(checks=[check]).checks[0].model_dump(mode="json")
                            for key in ("summary", "reason"):
                                if isinstance(value.get(key), str):
                                    value[key] = _without_answer_excerpts(value[key])
                        stream.write(_json_line({"type": kind, kind: value}, non_finite=non_finite))
                        pending += 1
                    if pending and (pending >= self.fsync_records
                                    or time.monotonic() - last_sync >= self.fsync_seconds):
                        stream.flush()
                        os.fsync(stream.fileno())
                        pending = 0
                        last_sync = time.monotonic()
        except Exception as error:
            self._error = error
            while True:
                try:
                    self._queue.get_nowait()
                    self.dropped_count += 1
                except queue.Empty:
                    break
            logger.exception("Evidence writer failed for %s", self.run_id)

    @property
    def stopped(self) -> bool:
        return not self._thread.is_alive()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._stop.set()
        self._thread.join(timeout=self.close_seconds)
        if not self.stopped:
            raise TimeoutError(f"Evidence writer did not stop within {self.close_seconds:g}s")
        if self._error is not None:
            raise self._error


def recover_evidence(directory: Path | str, meta: dict, *, pause=None) -> RunReport | None:
    """Recover complete lines; an interrupted final append is safely ignored."""
    directory = Path(directory)
    path = directory / "evidence.jsonl"
    if path.is_symlink():
        return None
    try:
        stream = path.open(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    header, records, checks, conditions = {}, {}, {}, {}
    dropped = meta.get("evidence_dropped_count", 0)
    with stream:
        for line in stream:
            if pause is not None and pause.is_set():
                return None
            try:
                value = json.loads(line)
                if value.get("type") == "header":
                    header = value
                elif value.get("type") == "dropped":
                    dropped = max(dropped, int(value["count"]))
                elif value.get("type") == "record":
                    record = RequestRecord.model_validate(value["record"])
                    # The journal precedes judging; never infer a judge outcome.
                    if record.judgement is not None or (record.evaluation and record.evaluation.scorer == "judge"):
                        record.score, record.score_message, record.judgement = None, None, None
                    records[record.id] = record
                elif value.get("type") == "progress":
                    event = value["progress"]
                    if isinstance(event.get("check"), dict):
                        check = CheckResult.model_validate(event["check"])
                        checks[(check.target, check.id)] = check
                    if isinstance(event.get("condition_summary"), dict):
                        condition = event["condition_summary"]
                        condition_id = condition.get("condition_id") or (condition.get("condition") or {}).get("id")
                        conditions[(condition.get("target"), condition_id)] = condition
            except (ValueError, TypeError, KeyError, AttributeError, ValidationError):
                logger.warning("Ignoring incomplete evidence line in %s", directory.name)
    if not header and not records:
        return None
    manifest = header.get("manifest", meta.get("manifest", {}))
    config = manifest.get("config", {})
    for target in config.get("targets", []):
        for check_id in config.get("checks", []):
            checks.setdefault((target["name"], check_id), CheckResult(id=check_id, target=target["name"],
                title=CHECK_NAMES.get(check_id, check_id), status="inconclusive", summary=""))
    for record in records.values():
        for check_id in record.check_ids:
            checks.setdefault((record.target, check_id), CheckResult(id=check_id, target=record.target,
                title=CHECK_NAMES.get(check_id, check_id), status="inconclusive", summary=""))
    reason = f"Giraffe stopped before this run finished; {len(records)} requests recorded."
    for check in checks.values():
        check.status, check.summary = "inconclusive", reason
    targets = {}
    for (target, condition_id), condition in conditions.items():
        # Journal rows describe executor lifecycle, not persisted verdicts.
        # Let RunReport rebuild aggregates from the finished request evidence.
        condition = {**condition, "condition": condition.get("condition") or {"id": condition_id},
                     "status": "interrupted", "reason": reason}
        targets.setdefault(target, {"conditions": []})["conditions"].append(condition)
    return RunReport(run_id=header.get("run_id", meta.get("run_id", directory.name)),
        started_at=header.get("started_at", meta.get("started_at", datetime.now(timezone.utc).isoformat())),
        finished_at=datetime.now(timezone.utc).isoformat(), overall="inconclusive", manifest=manifest,
        requests=list(records.values()), checks=list(checks.values()),
        abort_reason=reason, warnings=["Judge scores are missing for recovered request evidence."] +
            ([f"{dropped} evidence records not written to evidence.jsonl"] if dropped else []),
        observations={"interrupted": True, "judge_scores_missing": True, "evidence_dropped_count": dropped,
                      "targets": targets})
