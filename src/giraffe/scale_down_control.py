"""Bounded single-file CLI mailbox for a run-owned scale-down controller."""
from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import time

from .models import ScaleDownMarker

EVENT_FILE = ".scale-down-event.json"
STATUS_FILE = ".scale-down-status.json"
MAX_MARKER_BYTES = 16 * 1024
MAX_STATUS_BYTES = 64 * 1024
POLL_SECONDS = .1


def _read(path: Path, limit: int):
    # O_NONBLOCK prevents a manually supplied FIFO from hanging measurement.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Control input must be a bounded regular JSON file")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Control input exceeds its byte limit")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Control input must be a JSON object")
    return value


def _identity(path):
    info = path.lstat()
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size


def _publish(path: Path, value: dict, *, exclusive=False):
    data = (json.dumps(value, allow_nan=False, separators=(",", ":")) + "\n").encode()
    limit = MAX_MARKER_BYTES if path.name == EVENT_FILE else MAX_STATUS_BYTES
    if len(data) > limit:
        raise ValueError("Control input exceeds its byte limit")
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        identity = _identity(Path(temporary))
        if exclusive:
            # Atomic publication without overwriting a pending/accepted marker.
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        return identity
    finally:
        Path(temporary).unlink(missing_ok=True)


def _input(directory):
    path = directory / EVENT_FILE
    try:
        identity = _identity(path)
    except FileNotFoundError:
        return None
    try:
        return identity, _read(path, MAX_MARKER_BYTES), None
    except (OSError, ValueError, UnicodeError) as error:
        return identity, {}, f"Invalid marker file ({type(error).__name__})."


def _save_status(directory, status, rejected_identity=None):
    if rejected_identity is not None:
        path = directory / EVENT_FILE
        try:
            # Supported producers only create absent paths. Free the rejected
            # slot before exposing its ACK so an immediate retry can publish.
            # The ACK identifies its file, so it cannot reject a replacement.
            if _identity(path) == rejected_identity:
                path.unlink()
        except FileNotFoundError:
            pass
    _publish(directory / STATUS_FILE, status)


class ScaleDownFileControl:
    """I/O stays off the measured loop; acceptance stays on that loop."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.control = None
        self.task = None
        self.ready = asyncio.Event()
        self.done = asyncio.Event()
        self.last_submission = None
        self.last_submission_identity = None

    def bind(self, control):
        self.control = control
        self.task = asyncio.create_task(self._poll())

    def _status(self):
        return {**self.control.status(), "submission_acknowledgment": self.last_submission,
                "submission_identity": self.last_submission_identity}

    async def _poll(self):
        previous, seen = None, None
        while True:
            incoming = await asyncio.to_thread(_input, self.directory)
            rejected = None
            if incoming is not None and incoming[0] != seen:
                seen, marker, error = incoming
                self.last_submission = self.control.accept_marker(marker)
                self.last_submission_identity = list(seen)
                if error:
                    self.last_submission["reason"] = error
                if not self.last_submission["accepted"]:
                    rejected = seen
            status = self._status()
            if status != previous or rejected is not None:
                await asyncio.to_thread(_save_status, self.directory, status, rejected)
                previous = status
            self.ready.set()
            if self.done.is_set():
                return
            try:
                await asyncio.wait_for(self.done.wait(), POLL_SECONDS)
            except TimeoutError:
                pass

    async def close(self):
        if self.task is not None:
            self.done.set()
            await self.task


def _existing_ack(status, marker, submission_identity=None):
    for key in ("acknowledgment", "submission_acknowledgment"):
        ack = status.get(key)
        if (ack and ack.get("marker") == marker
                and (ack.get("accepted") or submission_identity is not None
                     and status.get("submission_identity") == list(submission_identity))):
            return {"outcome": "accepted" if ack["accepted"] else "rejected", "acknowledgment": ack}
    return None


def submit_marker(directory: Path, *, event_id: str, replica_id: str | None = None,
                  source: str = "operator", operator_at: str | None = None, wait_seconds: float = 5):
    """Publish intent, then report only the worker's acknowledgment as accepted."""
    from .storage import run_alive

    if not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 60:
        raise ValueError("wait-seconds must be finite and between 0 and 60")
    directory = Path(directory)
    try:
        status = _read(directory / STATUS_FILE, MAX_STATUS_BYTES)
    except FileNotFoundError:
        raise ValueError("This run directory has no CLI scale-down control. For a run started from the UI, "
                         "use Mark removal in the browser or POST /api/runs/<run-id>/scale-down/event.") from None
    marker = ScaleDownMarker(event_id=event_id, nonce=status["nonce"], target=status["target"],
                             replica_id=replica_id, source=source, operator_at=operator_at).model_dump(mode="json")
    if result := _existing_ack(status, marker):
        return result
    if (status.get("acknowledgment") or {}).get("accepted"):
        raise ValueError("A different marker has already been accepted")
    meta = _read(directory / ".web.json", 1024 * 1024)
    if (status.get("phase") in {"finalizing", "finished"}
            or meta.get("state") != "running" or not run_alive(directory, meta)):
        raise ValueError("The scale-down run is not active")
    try:
        submission_identity = _publish(directory / EVENT_FILE, marker, exclusive=True)
    except FileExistsError:
        incoming = _input(directory)
        if incoming is None or incoming[2] is not None:
            raise ValueError("Marker mailbox changed or is invalid; inspect run status before retrying") from None
        submission_identity, existing, _ = incoming
        if existing != marker:
            raise ValueError("A different marker is pending; inspect its acknowledgment before retrying") from None
    deadline = time.monotonic() + wait_seconds
    while True:
        status = _read(directory / STATUS_FILE, MAX_STATUS_BYTES)
        if status.get("nonce") != marker["nonce"]:
            raise ValueError("The active scenario changed before acknowledgment")
        if result := _existing_ack(status, marker, submission_identity):
            return result
        if time.monotonic() >= deadline:
            return {"outcome": "submitted_without_acknowledgment", "acknowledgment": None}
        time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
