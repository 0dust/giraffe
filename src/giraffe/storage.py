"""Inspect saved-run disk use and explicitly clean up eligible reports."""

from __future__ import annotations

from collections import Counter
import contextvars
from datetime import datetime, timezone
from functools import lru_cache
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import tempfile
import threading
import time
from typing import Any

from . import models


DEFAULT_LIMITS = {"max_total_storage": 5 * 1024 ** 3, "min_free_disk": 1024 ** 3,
                  "max_run_storage": 256 * 1024 ** 2}
# HTML remains recognized for legacy runs and explicit CLI exports.
_FILES = {"report.json", "report.html", ".web.json", ".keep", "summary.json", "evidence.jsonl", "giraffe.log"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
logger = logging.getLogger(__name__)
_metadata_lock = threading.RLock()
# Readers that must merge several metadata fields can share the publication
# transaction with report writers and the heartbeat timer.
metadata_lock = _metadata_lock
HEARTBEAT_SECONDS = 5.0
HEARTBEAT_STALE_SECONDS = 30.0
LEGACY_HEARTBEAT_STALE_SECONDS = 120.0
HEARTBEAT_CLOSE_SECONDS = 5.0


def fsync_directory(directory: Path | str) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_metadata(directory: Path, values: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".web.json.",
                                         suffix=".tmp", dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(values, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(directory / ".web.json")
        fsync_directory(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_metadata(directory: Path | str, values: dict) -> None:
    """The single publisher for web metadata, shared with report and timer writers."""
    with _metadata_lock:
        _write_metadata(Path(directory), values)


def update_metadata(directory: Path | str, changes: dict) -> dict:
    with _metadata_lock:
        directory = Path(directory)
        values = _read(directory / ".web.json") | changes
        _write_metadata(directory, values)
        return values


class Heartbeat:
    """Keep ownership fresh even while the request or server event loop is busy."""

    def __init__(self, directory: Path | str, *, interval: float = HEARTBEAT_SECONDS):
        self.directory = Path(directory)
        self.interval = min(max(interval, 0.01), HEARTBEAT_SECONDS)
        self._stop = threading.Event()
        context = contextvars.copy_context()
        self._thread = threading.Thread(target=context.run, args=(self._run,),
                                        name="giraffe-heartbeat", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            try:
                with _metadata_lock:
                    values = _read(self.directory / ".web.json")
                    if values.get("state") not in {"running", "stopping"}:
                        return
                    values["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
                    _write_metadata(self.directory, values)
            except Exception:
                logger.exception("Could not save heartbeat for %s", self.directory.name)
            self._stop.wait(self.interval)

    def close(self):
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join(timeout=HEARTBEAT_CLOSE_SECONDS)
            if self._thread.is_alive():
                logger.warning("Heartbeat did not stop within %gs for %s",
                               HEARTBEAT_CLOSE_SECONDS, self.directory.name)


def parse_size(value: str | int) -> int:
    if isinstance(value, int):
        if value >= 0:
            return value
        raise ValueError("Disk size must be non-negative")
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(B|[KMGT]I?B)?\s*", value, re.I)
    if not match:
        raise ValueError("Use a non-negative disk size such as 500MiB, 2GiB, or 0")
    suffix = (match.group(2) or "B").upper()
    power = "KMGT".find(suffix[0]) + 1 if suffix != "B" else 0
    result = float(match.group(1)) * (1024 if "I" in suffix else 1000) ** power
    if not result.is_integer():
        raise ValueError("Disk size must resolve to a whole number of bytes")
    return int(result)


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f}".rstrip("0").rstrip(".") + f" {unit}"
        value /= 1024


def _file_sizes(directory: Path) -> dict[tuple[int, int], int]:
    if directory.is_symlink() or not directory.is_dir():
        return {}
    sizes = {}
    try:
        children = list(directory.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return sizes
    for child in children:
        try:
            info = child.lstat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        if stat.S_ISREG(info.st_mode):
            sizes[(info.st_dev, info.st_ino)] = info.st_size
        elif stat.S_ISDIR(info.st_mode):
            sizes.update(_file_sizes(child))
    return sizes


def run_size(directory: Path | str, *, seen: set[tuple[int, int]] | None = None) -> int:
    seen = set() if seen is None else seen
    sizes = _file_sizes(Path(directory))
    total = sum(size for identity, size in sizes.items() if identity not in seen)
    seen.update(sizes)
    return total


def _read(path: Path) -> dict:
    if path.is_symlink():
        return {}
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def is_giraffe_run(directory: Path, meta: dict | None = None, report: dict | None = None) -> bool:
    meta = _read(directory / ".web.json") if meta is None else meta
    if meta.get("id") == directory.name:
        return True
    report = _fields(directory / "report.json") if report is None else report
    return (all(isinstance(report.get(key), str) and report[key]
                for key in ("run_id", "started_at", "suite_version", "finished_at"))
            and models.is_supported_schema(report.get("schema_version"))
            and isinstance(report.get("overall"), str)
            and report["overall"] in {"pass", "fail", "blocked", "inconclusive", "skipped"}
            and isinstance(report.get("manifest"), dict))


def _recognized(directory: Path) -> bool:
    if directory.is_symlink() or not directory.is_dir() or not is_giraffe_run(directory):
        return False
    try:
        children = list(directory.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return False
    for child in children:
        try:
            mode = child.lstat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            return False
        if not stat.S_ISREG(mode):
            return False
        if child.name not in _FILES and not re.fullmatch(r"tmp[A-Za-z0-9_]{8}", child.name) and not any(
            re.fullmatch(re.escape(name) + r"(?:\.[A-Za-z0-9_-]+)?\.tmp", child.name)
            for name in _FILES
        ):
            return False
    return True


@lru_cache(maxsize=256)
def _report_fields(path: str, mtime_ns: int, size: int) -> dict:
    # Giraffe writes identity fields before requests; avoid decoding retained bodies.
    decoder = json.JSONDecoder()
    fields = {}
    try:
        with open(path) as stream:
            prefix = stream.read(65536)
        if not prefix.lstrip().startswith("{"):
            return fields
        index = prefix.index("{") + 1
        while index < len(prefix):
            while index < len(prefix) and prefix[index] in " \t\r\n,":
                index += 1
            if prefix[index] == "}":
                return fields
            key, index = decoder.raw_decode(prefix, index)
            if not isinstance(key, str):
                return fields
            while prefix[index] in " \t\r\n:":
                index += 1
            if key == "requests":
                return fields
            if key == "manifest" and prefix[index] == "{":
                # Its presence/type completes the canonical header. Never decode
                # a potentially large manifest just to establish ownership.
                fields["manifest"] = {}
                return fields
            value, index = decoder.raw_decode(prefix, index)
            if key in {"run_id", "source_run_id", "started_at", "schema_version", "suite_version",
                       "finished_at", "overall"}:
                fields[key] = value
        return fields
    except (OSError, ValueError, IndexError):
        # Unknown/reordered/oversized headers are protected, never decoded in full.
        return fields


def _fields(path: Path) -> dict:
    try:
        if path.is_symlink():
            return {}
        info = path.stat()
        return _report_fields(str(path), info.st_mtime_ns, info.st_size)
    except (FileNotFoundError, NotADirectoryError):
        return {}


def _baseline_sources(root: Path) -> tuple[set[str], bool]:
    directory = root / ".baselines"
    if directory.is_symlink():
        return set(), True
    try:
        baselines = list(directory.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return set(), False
    sources = set()
    unknown = False
    for baseline in baselines:
        if baseline.is_symlink():
            unknown = True
            continue
        if baseline.name.startswith("."):
            continue
        if baseline.is_dir():
            paths = [baseline / name for name in ("baseline.json", "report.json")
                     if (baseline / name).exists() or (baseline / name).is_symlink()]
            if not paths:
                continue
        elif baseline.is_file() and baseline.suffix == ".json":
            paths = [baseline]
        else:
            continue
        values = [_fields(path) if path.name == "report.json" else _read(path) for path in paths]
        ids = {value[key] for value in values for key in ("run_id", "source_run_id")
               if isinstance(value.get(key), str) and value[key]}
        sources.update(ids)
        unknown |= any(not value for value in values) or not bool(ids)
    return sources, unknown


def _timestamp(value: Any, fallback: float) -> float:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError):
        return fallback


def run_alive(directory: Path | str, meta: dict | None = None, *, active_id: str | None = None) -> bool:
    directory = Path(directory)
    if directory.name == active_id:
        return True
    meta = _read(directory / ".web.json") if meta is None else meta
    try:
        modified = (directory / ".web.json").stat().st_mtime
    except (FileNotFoundError, NotADirectoryError):
        modified = 0
    # A heartbeat is authoritative when present; legacy rows use marker mtime.
    heartbeat = _timestamp(meta.get("heartbeat_at"), modified)
    grace = (HEARTBEAT_STALE_SECONDS if meta.get("heartbeat_at")
             else LEGACY_HEARTBEAT_STALE_SECONDS)
    if heartbeat >= time.time() - grace:
        return True
    if meta.get("owner_host") != socket.gethostname():
        return False
    pid = meta.get("owner_pid")
    # An inactive row owned by this process (including PID 1 in a container)
    # cannot be rescued by probing our own PID.
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (ProcessLookupError, OSError, OverflowError):
        return False
    return True


def _run(directory: Path, active_id: str | None, sources: set[str], unknown_source: bool) -> dict | None:
    if directory.name.startswith(".") or directory.is_symlink() or not directory.is_dir():
        return None
    if not any((directory / name).exists() or (directory / name).is_symlink()
               for name in ("report.json", ".web.json")):
        return None
    try:
        modified = directory.stat().st_mtime
    except (FileNotFoundError, NotADirectoryError):
        return None
    meta = _read(directory / ".web.json")
    meta_run_id = meta.get("run_id") if isinstance(meta.get("run_id"), str) else None
    report = {}
    if (not (meta.get("id") == directory.name and meta.get("started_at") and meta_run_id)
            or (sources and meta_run_id not in sources and directory.name not in sources)):
        report = _fields(directory / "report.json")
    started = _timestamp(meta.get("started_at"), _timestamp(report.get("started_at"), modified))
    kept = (directory / ".keep").exists() or (directory / ".keep").is_symlink()
    reason = None
    if not is_giraffe_run(directory, meta, report):
        reason = "not a Giraffe run"
    elif directory.name == active_id:
        reason = "active run"
    elif not _recognized(directory):
        reason = "contains files Giraffe did not write"
    elif kept:
        reason = "kept run"
    elif any(isinstance(identifier, str) and identifier in sources for identifier in
             (directory.name, meta_run_id, report.get("run_id"))):
        reason = "source run of a saved baseline"
    elif unknown_source:
        reason = "saved baseline source could not be read"
    elif meta.get("state") in {"running", "stopping"} and run_alive(directory, meta, active_id=active_id):
        reason = "run is " + meta["state"]
    if not directory.is_dir():
        return None
    return {"id": directory.name, "bytes": run_size(directory),
            "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
            "protected": reason is not None, "protected_reason": reason, "kept": kept}


def _runs(root: Path, active_id: str | None) -> list[dict]:
    try:
        directories = list(root.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []
    sources, unknown_source = _baseline_sources(root)
    rows = [_run(directory, active_id, sources, unknown_source) for directory in directories]
    return sorted((row for row in rows if row is not None),
                  key=lambda row: (row["started_at"], row["id"]), reverse=True)


def _disk_usage(root: Path):
    while not root.exists():
        if root.parent == root:
            break
        root = root.parent
    return shutil.disk_usage(root)


def storage_summary(root: Path | str, *, active_id: str | None = None, limits: dict,
                    auto_cleanup: dict | None = None) -> dict:
    root = Path(root).resolve()
    runs = _runs(root, active_id)
    seen = set()
    runs_bytes = sum(run_size(root / run["id"], seen=seen) for run in runs)
    baseline_logical = run_size(root / ".baselines")
    baseline_bytes = run_size(root / ".baselines", seen=seen)
    return {"runs": runs, "runs_bytes": runs_bytes,
            "baselines_bytes": baseline_bytes,
            "baselines_shared_bytes": baseline_logical - baseline_bytes,
            "free_bytes": _disk_usage(root).free,
            "limits": DEFAULT_LIMITS | limits, "auto_cleanup": auto_cleanup or {}}


def automatic_cleanup(root: Path | str, policy: dict, *, active_id: str) -> dict:
    """Apply only explicitly configured criteria, protecting the triggering run."""
    if not policy:
        return {"deleted": [], "skipped": [], "bytes_freed": 0}
    plan = cleanup_plan(root, active_id=active_id, **policy)
    return apply_cleanup(root, [row["id"] for row in plan["candidates"]],
                         active_id=active_id, **policy)


def check_start(root: Path | str, limits: dict) -> None:
    configured = DEFAULT_LIMITS | limits
    maximum, minimum = configured["max_total_storage"], configured["min_free_disk"]
    if not maximum and not minimum:
        return
    root = Path(root).resolve()
    summary = storage_summary(root, limits=configured)
    if maximum and summary["runs_bytes"] > maximum:
        raise ValueError(f"Saved runs use {summary['runs_bytes']} bytes on disk, exceeding the "
                         f"{maximum}-byte limit. Use giraffe cleanup or Clean up in Runs before starting.")
    if minimum and summary["free_bytes"] < minimum:
        raise ValueError(f"Free disk is {summary['free_bytes']} bytes, below the {minimum}-byte minimum. "
                         "Use giraffe cleanup or Clean up in Runs before starting.")


def cleanup_plan(root: Path | str, *, older_than_days: int | None = None, keep_latest: int | None = None,
                 max_total_bytes: int | None = None, active_id: str | None = None) -> dict:
    if isinstance(max_total_bytes, int) and max_total_bytes <= 0:
        raise ValueError("Cleanup max total must be greater than 0; use --older-than 0 to remove all unprotected runs")
    for value in (older_than_days, keep_latest, max_total_bytes):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError("Cleanup criteria must be non-negative integers")
    root = Path(root).resolve()
    runs = _runs(root, active_id)
    cutoff = datetime.now(timezone.utc).timestamp() - (older_than_days or 0) * 86400
    candidates = {}
    for index, run in enumerate(runs):
        if run["protected"]:
            continue
        reasons = []
        if older_than_days is not None and _timestamp(run["started_at"], 0) < cutoff:
            reasons.append("older_than_days")
        if keep_latest is not None and index >= keep_latest:
            reasons.append("keep_latest")
        if reasons:
            candidates[run["id"]] = run | {"criterion": ", ".join(reasons)}

    inventories = {run["id"]: _file_sizes(root / run["id"]) for run in runs}
    sizes = {}
    references = Counter()
    for inventory in inventories.values():
        sizes.update(inventory)
        references.update(inventory.keys())
    total = remaining = sum(sizes.values())

    def remove(identifier):
        nonlocal remaining
        for identity in inventories[identifier]:
            references[identity] -= 1
            if references[identity] == 0:
                remaining -= sizes[identity]

    for identifier in candidates:
        remove(identifier)
    if max_total_bytes is not None:
        for run in reversed(runs):
            if remaining <= max_total_bytes:
                break
            if run["protected"] or run["id"] in candidates:
                continue
            candidates[run["id"]] = run | {"criterion": "max_total_bytes"}
            remove(run["id"])
    ordered = [candidates[run["id"]] for run in reversed(runs) if run["id"] in candidates]
    return {"candidates": ordered,
            "protected": [run | {"reason": run["protected_reason"]} for run in runs if run["protected"]],
            "bytes_freed": total - remaining, "runs_bytes_after": remaining}


def apply_cleanup(root: Path | str, ids: list[str], **criteria) -> dict:
    root = Path(root).resolve()
    plan = cleanup_plan(root, **criteria)
    candidates = {run["id"]: run for run in plan["candidates"]}
    protected = {run["id"]: run for run in plan["protected"]}
    deleted, skipped = [], []

    def stored_bytes():
        seen = set()
        total = sum(run_size(root / row["id"], seen=seen)
                    for row in _runs(root, criteria.get("active_id")))
        return total, total + run_size(root / ".baselines", seen=seen)

    _, before = stored_bytes()
    for identifier in dict.fromkeys(ids):
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            skipped.append({"id": identifier, "reason": "invalid run id"})
            continue
        candidate = candidates.get(identifier)
        if candidate is None:
            skipped.append({"id": identifier, "reason": protected.get(identifier, {}).get(
                "reason", "no longer a cleanup candidate")})
            continue
        directory = root / identifier
        sources, unknown_source = _baseline_sources(root)
        current = _run(directory, criteria.get("active_id"), sources, unknown_source)
        reason = current["protected_reason"] if current else "run directory unavailable"
        if not reason and current["started_at"] != candidate["started_at"]:
            reason = "run changed since cleanup plan"
        if not reason and (directory.resolve().parent != root or not _recognized(directory)):
            reason = "contains files Giraffe did not write"
        if reason:
            skipped.append({"id": identifier, "reason": reason})
            continue
        try:
            shutil.rmtree(directory)
        except OSError as error:
            skipped.append({"id": identifier, "reason": str(error)})
        else:
            deleted.append(identifier)
    remaining, after = stored_bytes()
    return {"deleted": deleted, "skipped": skipped, "bytes_freed": max(0, before - after),
            "runs_bytes_after": remaining}


def set_kept(directory: Path | str, kept: bool) -> None:
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Run directory unavailable")
    marker = directory / ".keep"
    if marker.is_symlink():
        raise ValueError("Keep marker must not be a symlink")
    if kept:
        marker.touch()
    else:
        marker.unlink(missing_ok=True)
