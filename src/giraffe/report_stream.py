"""Read saved reports with a disk-backed input lookup and one request in RAM."""

from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import tempfile

from .caps import MAX_REPORT_TOP_LEVEL_MEMBERS
from .models import RequestRecord, RunReport
from .report_inputs import verify_input


_STRING_SPECIAL = re.compile(r'["\\]')
_CONTAINER_SPECIAL = re.compile(r'["{}\[\]]')
_SCALAR_END = re.compile(r'[\s,:}\]]')


class _Reader:
    def __init__(self, handle):
        self.handle = handle
        self.buffer = ""
        self.position = 0
        self.eof = False
        self.decoder = json.JSONDecoder()

    def _fill(self):
        self.buffer = self.buffer[self.position:]
        self.position = 0
        chunk = self.handle.read(65536)
        self.buffer += chunk
        self.eof = not chunk

    def peek(self):
        while True:
            while self.position < len(self.buffer) and self.buffer[self.position].isspace():
                self.position += 1
            if self.position < len(self.buffer):
                return self.buffer[self.position]
            if self.eof:
                return ""
            self._fill()

    def consume(self, expected):
        if self.peek() != expected:
            raise ValueError(f"Malformed saved report: expected {expected!r}")
        self.position += 1

    def value(self):
        first = self.peek()
        if not first:
            raise ValueError("Malformed or incomplete saved report")
        container = first in "[{"
        string = first == '"'
        scalar = not container and not string
        depth = 0
        escaped = False
        parts = []
        start = self.position
        cursor = start + 1 if string else start
        while True:
            end = None
            if scalar:
                match = _SCALAR_END.search(self.buffer, cursor)
                if match:
                    end = match.start()
                elif self.eof:
                    end = len(self.buffer)
            else:
                while cursor < len(self.buffer):
                    if escaped:
                        cursor += 1
                        escaped = False
                    special = _STRING_SPECIAL if string else _CONTAINER_SPECIAL
                    match = special.search(self.buffer, cursor)
                    if match is None:
                        break
                    token = match.group()
                    cursor = match.end()
                    if string:
                        if token == "\\":
                            if cursor == len(self.buffer):
                                escaped = True
                            else:
                                cursor += 1
                        else:
                            string = False
                            if not container:
                                end = cursor
                                break
                    elif token == '"':
                        string = True
                    elif token in "[{":
                        depth += 1
                    else:
                        depth -= 1
                        if depth == 0:
                            end = cursor
                            break
            if end is not None:
                parts.append(self.buffer[start:end])
                self.position = end
                encoded = "".join(parts)
                try:
                    value, consumed = self.decoder.raw_decode(encoded)
                except json.JSONDecodeError as error:
                    raise ValueError("Malformed or incomplete saved report") from error
                if consumed != len(encoded):
                    raise ValueError("Malformed saved report value")
                return value
            if self.eof:
                raise ValueError("Malformed or incomplete saved report")
            # Keep scan state across refills; decode each complete value once.
            # Separate pieces avoid repeatedly copying an ever-growing prefix.
            parts.append(self.buffer[start:])
            self.position = len(self.buffer)
            self._fill()
            start = cursor = 0

    def members(self, *, unique=True):
        self.consume("{")
        seen = set()
        while self.peek() != "}":
            key = self.value()
            if not isinstance(key, str) or (unique and key in seen):
                raise ValueError("Invalid or duplicate saved report key")
            if unique:
                seen.add(key)
                if len(seen) > MAX_REPORT_TOP_LEVEL_MEMBERS:
                    raise ValueError("Saved report exceeds 64 top-level members")
            self.consume(":")
            yield key
            if self.peek() == "}":
                break
            self.consume(",")
            if self.peek() == "}":
                raise ValueError("Trailing comma in saved report")
        self.consume("}")

    def requests(self):
        self.consume("[")
        while self.peek() != "]":
            record = self.value()
            if not isinstance(record, dict):
                raise ValueError("Saved request must be an object")
            yield record
            if self.peek() == "]":
                break
            self.consume(",")
            if self.peek() == "]":
                raise ValueError("Trailing comma in saved requests")
        self.consume("]")


def _request(raw, inputs):
    if "input_ref" in raw:
        reference = raw.pop("input_ref")
        if not isinstance(reference, str) or not isinstance(raw.get("evaluation"), dict):
            raise ValueError("Invalid saved input reference")
        entry = inputs.execute("SELECT messages FROM inputs WHERE ref=?", (reference,)).fetchone()
        if entry is None or "messages" in raw["evaluation"]:
            raise ValueError("Missing or ambiguous saved input reference")
        raw["evaluation"]["messages"] = json.loads(entry[0])
    return RequestRecord.model_validate(raw)


def expanded_report_chunks(path: Path):
    """Validate before yielding, then stream expanded metadata and individual requests.

    Keep the source inode open across passes. The temporary lookup holds unique
    inputs on disk; memory scales with metadata and the largest individual value,
    rather than the sum of request evidence. For old reports without performance
    data, only numeric/status/timing records are retained to build the new views;
    prompts and response bodies still pass through one at a time.
    """
    with tempfile.TemporaryDirectory(prefix="giraffe-download-") as temporary, \
            closing(sqlite3.connect(str(Path(temporary) / "inputs.sqlite"), check_same_thread=False)) as inputs, \
            path.open(encoding="utf-8") as handle:
        inputs.execute("PRAGMA cache_size=-1024")
        inputs.execute("CREATE TABLE inputs (ref TEXT PRIMARY KEY, messages TEXT)")
        reader = _Reader(handle)
        metadata = {}
        measurements = []
        has_inputs = has_requests = False
        for key in reader.members():
            if key == "inputs":
                has_inputs = True
                for reference in reader.members(unique=False):
                    messages = reader.value()
                    verify_input(reference, messages)
                    try:
                        inputs.execute("INSERT INTO inputs VALUES (?, ?)",
                                       (reference, json.dumps(messages, ensure_ascii=False, separators=(",", ":"))))
                    except sqlite3.IntegrityError as error:
                        raise ValueError("Duplicate saved input table entry") from error
            elif key == "requests":
                has_requests = True
                # The table precedes requests in format 2. Legacy requests are expanded.
                for raw in reader.requests():
                    if "input_ref" in raw and metadata.get("report_format") != 2:
                        raise ValueError("Input references require report format 2 before requests")
                    record = _request(raw, inputs)
                    measurements.append(record.model_copy(update={
                            "output": "", "reasoning": "", "reasoning_stripped": bool(record.reasoning.strip()) or record.reasoning_stripped,
                            "evaluation": None, "tool_calls": [], "assistant_message": None,
                            "tool_conversation": None, "logprob_evidence": None,
                            "response_events": [], "judgement": None}))
            else:
                metadata[key] = reader.value()
        if reader.peek() or not has_requests:
            raise ValueError("Saved report has trailing data or no requests")
        report_format = metadata.pop("report_format", None)
        if has_inputs != (report_format == 2) or report_format not in (None, 2):
            raise ValueError("Unsupported saved report format")
        # Old embedded summaries are ignored: exports always derive the current
        # default selection from retained measurement evidence.
        metadata.pop("performance", None)
        header = RunReport.model_validate({**metadata, "requests": measurements})
        yield header.model_dump_json(exclude={"requests"})[:-1] + ',"requests":['
        handle.seek(0)
        reader = _Reader(handle)
        first = True
        for key in reader.members():
            if key == "inputs":
                for _ in reader.members(unique=False):
                    reader.value()
            elif key == "requests":
                for raw in reader.requests():
                    record = _request(raw, inputs)
                    yield ("" if first else ",") + record.model_dump_json()
                    first = False
            else:
                reader.value()
        yield "]}"


def load_request(path: Path, request_id: str):
    """Find retained evidence with one request and disk-backed shared inputs in RAM."""
    with tempfile.TemporaryDirectory(prefix="giraffe-request-") as temporary, \
            closing(sqlite3.connect(str(Path(temporary) / "inputs.sqlite"))) as inputs, \
            path.open(encoding="utf-8") as handle:
        inputs.execute("PRAGMA cache_size=-1024")
        inputs.execute("CREATE TABLE inputs (ref TEXT PRIMARY KEY, messages TEXT)")
        reader = _Reader(handle)
        report_format = None
        has_inputs = has_requests = False
        found = None
        for key in reader.members():
            if key == "report_format":
                report_format = reader.value()
            elif key == "inputs":
                has_inputs = True
                for reference in reader.members(unique=False):
                    messages = reader.value()
                    verify_input(reference, messages)
                    try:
                        inputs.execute("INSERT INTO inputs VALUES (?, ?)",
                                       (reference, json.dumps(messages, ensure_ascii=False, separators=(",", ":"))))
                    except sqlite3.IntegrityError as error:
                        raise ValueError("Duplicate saved input table entry") from error
            elif key == "requests":
                has_requests = True
                for raw in reader.requests():
                    if "input_ref" in raw and report_format != 2:
                        raise ValueError("Input references require report format 2 before requests")
                    record = _request(raw, inputs)
                    if record.id == request_id:
                        found = record
            else:
                reader.value()
        if reader.peek() or not has_requests or has_inputs != (report_format == 2) or report_format not in (None, 2):
            raise ValueError("Unsupported or malformed saved report")
        return found
