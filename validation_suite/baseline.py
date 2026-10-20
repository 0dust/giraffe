"""Unmodified Giraffe CLI adapter and lossless normalization of its saved report."""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sqlite3
import tempfile
from collections.abc import Iterator, Iterable
from typing import Any, TextIO

from .scenarios import EXECUTION_POLICY, RESOURCE_POLICY, digest, poisson_offsets as expected_poisson_offsets


def ensure_execution_allowed(scenario: dict) -> None:
    """Enforce this reviewed selection, including direct adapter calls."""
    if scenario.get("experiment_id") == "E7" or scenario.get("active_seconds", 0) > EXECUTION_POLICY["maximum_active_seconds"]:
        raise ValueError("E7 and active durations above 300 seconds are not authorized by this reviewed selection; no subprocess was started")


class _ReportReader:
    """Incremental JSON values with a fixed input buffer/value bound."""

    def __init__(self, handle: TextIO):
        self.handle = handle
        self.buffer = ""
        self.position = 0
        self.eof = False
        self.decoder = json.JSONDecoder(parse_constant=self._nonfinite)

    @staticmethod
    def _nonfinite(value: str) -> None:
        raise ValueError(f"Non-finite JSON number: {value}")

    def _fill(self) -> None:
        self.buffer = self.buffer[self.position:]
        self.position = 0
        chunk = self.handle.read(64 * 1024)
        self.buffer += chunk
        self.eof = not chunk

    def peek(self) -> str:
        while True:
            while self.position < len(self.buffer) and self.buffer[self.position].isspace():
                self.position += 1
            if self.position < len(self.buffer):
                return self.buffer[self.position]
            if self.eof:
                return ""
            self._fill()

    def consume(self, expected: str) -> None:
        if self.peek() != expected:
            raise ValueError(f"Malformed product report: expected {expected!r}")
        self.position += 1

    def value(self, limit: int) -> Any:
        self.peek()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.position)
            except json.JSONDecodeError as error:
                if len(self.buffer) - self.position > limit:
                    raise ValueError("Product report JSON value exceeds the bounded decoder limit") from error
                if self.eof:
                    raise ValueError(f"Malformed or incomplete product report: {error}") from error
                self._fill()
                continue
            # A scalar number can end at a read boundary while more digits follow.
            if end == len(self.buffer) and not self.eof:
                self._fill()
                continue
            if len(self.buffer[self.position:end].encode("utf-8")) > limit:
                raise ValueError("Product report JSON value exceeds the bounded decoder limit")
            self.position = end
            return value


def iter_report_requests(path: Path, *, metadata: dict | None = None) -> Iterator[dict]:
    """Yield requests without materializing the report; validate its complete JSON."""
    metadata = metadata if metadata is not None else {}
    limits = RESOURCE_POLICY["product_report_decoder"]
    seen: set[str] = set()
    report_format = None
    # Inputs can exceed the metadata value bound in aggregate. Decode one entry
    # at a time and keep the lookup on disk, rather than retaining the table in RAM.
    with tempfile.TemporaryDirectory(prefix="giraffe-inputs-") as temporary, \
            closing(sqlite3.connect(str(Path(temporary) / "inputs.sqlite"))) as inputs, \
            path.open(encoding="utf-8") as handle:
        inputs.execute("PRAGMA cache_size=-1024")
        inputs.execute("CREATE TABLE inputs (ref TEXT PRIMARY KEY, messages TEXT)")
        reader = _ReportReader(handle)
        reader.consume("{")
        while reader.peek() != "}":
            key = reader.value(1024)
            if not isinstance(key, str) or key in seen:
                raise ValueError("Product report object has an invalid or duplicate key")
            seen.add(key)
            if len(seen) > 64:
                raise ValueError("Product report exceeds the bounded top-level member count")
            reader.consume(":")
            if key == "inputs":
                reader.consume("{")
                while reader.peek() != "}":
                    reference = reader.value(1024)
                    reader.consume(":")
                    messages = reader.value(limits["request_value_bytes"])
                    if not isinstance(reference, str) or not isinstance(messages, list):
                        raise ValueError("Invalid product input table entry")
                    encoded = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
                    if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != reference:
                        raise ValueError("Saved input SHA-256 mismatch")
                    try:
                        inputs.execute("INSERT INTO inputs VALUES (?, ?)",
                                       (reference, encoded))
                    except sqlite3.IntegrityError as error:
                        raise ValueError("Duplicate product input table entry") from error
                    if reader.peek() == "}":
                        break
                    reader.consume(",")
                    if reader.peek() == "}":
                        raise ValueError("Trailing comma in product input table")
                reader.consume("}")
            elif key == "requests":
                reader.consume("[")
                while reader.peek() != "]":
                    raw = reader.value(limits["request_value_bytes"])
                    if not isinstance(raw, dict):
                        raise ValueError("Product report request must be an object")
                    if "input_ref" in raw:
                        if report_format != 2:
                            raise ValueError("Input references require product report format 2 before requests")
                        reference = raw.pop("input_ref")
                        if not isinstance(reference, str) or not isinstance(raw.get("evaluation"), dict):
                            raise ValueError("Invalid product input reference")
                        entry = inputs.execute("SELECT messages FROM inputs WHERE ref=?", (reference,)).fetchone()
                        if entry is None or "messages" in raw["evaluation"]:
                            raise ValueError("Missing or ambiguous product input reference")
                        raw["evaluation"]["messages"] = json.loads(entry[0])
                    yield raw
                    if reader.peek() == "]":
                        break
                    reader.consume(",")
                    if reader.peek() == "]":
                        raise ValueError("Trailing comma in product request array")
                reader.consume("]")
            else:
                value = reader.value(limits["metadata_value_bytes"])
                if key == "report_format":
                    if value != 2:
                        raise ValueError("Unsupported product report format")
                    report_format = value
                if key in {"overall", "abort_reason", "observations"}:
                    metadata[key] = value
            if reader.peek() == "}":
                break
            reader.consume(",")
            if reader.peek() == "}":
                raise ValueError("Trailing comma in product report object")
        reader.consume("}")
        if reader.peek() or "requests" not in seen:
            raise ValueError("Product report contains trailing data or no requests array")
        if ("inputs" in seen) != (report_format == 2):
            raise ValueError("Product report format and input table disagree")


def _output(command: list[str], *, cwd: Path | None = None, env: dict | None = None) -> str:
    return subprocess.check_output(command, cwd=cwd, env=env, text=True).strip()


def baseline_environment(checkout: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str((checkout / "src").resolve())
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def baseline_snapshot(checkout: Path, python: str) -> dict:
    checkout = checkout.resolve()
    tracked = _output(["git", "ls-files", "-z"], cwd=checkout).split("\0")
    hashes = {name: hashlib.sha256((checkout / name).read_bytes()).hexdigest()
              for name in tracked if name and (checkout / name).is_file()}
    environment = baseline_environment(checkout)
    probe = _output([python, "-c", "import giraffe, json, sys; "
                     "print(json.dumps({'file': giraffe.__file__, 'python': sys.version}))"],
                    cwd=checkout, env=environment)
    identity = json.loads(probe)
    if not Path(identity["file"]).resolve().is_relative_to(checkout / "src"):
        raise ValueError("Baseline import escaped the pinned source checkout")
    dependency_probe = ("import importlib.metadata as m, json, hashlib; "
        "print(json.dumps(sorted([{'name': d.metadata.get('Name', ''), 'version': d.version, "
        "'record_sha256': hashlib.sha256((d.read_text('RECORD') or '').encode()).hexdigest()} "
        "for d in m.distributions()], key=lambda x: (x['name'].lower(), x['version'])), sort_keys=True))")
    dependencies = _output([python, "-c", dependency_probe], cwd=checkout, env=environment)
    hardware = {}
    if platform.system() == "Darwin":
        for name in ("hw.memsize", "hw.logicalcpu", "hw.model"):
            sampled = subprocess.run(["sysctl", "-n", name], text=True, capture_output=True)
            hardware[name] = {"value": sampled.stdout.strip() if sampled.returncode == 0 else None,
                              "state": "measured" if sampled.returncode == 0 else "not_measured",
                              "source": f"sysctl -n {name}",
                              "reason": "" if sampled.returncode == 0 else sampled.stderr.strip()}
    return {"sha": _output(["git", "rev-parse", "HEAD"], cwd=checkout),
            "status_porcelain": _output(["git", "status", "--porcelain", "--untracked-files=all"], cwd=checkout),
            "tracked_file_hashes": hashes, "tracked_tree_sha256": digest(hashes),
            "product_source_sha256": digest({key: value for key, value in hashes.items() if key.startswith("src/")}),
            "dependency_specification_sha256": digest({key: value for key, value in hashes.items()
                if key in {"pyproject.toml", "uv.lock", "requirements.txt", "requirements-dev.txt"}}),
            "installed_dependencies": dependencies,
            "installed_dependencies_sha256": hashlib.sha256(dependencies.encode()).hexdigest(),
            "import_proof": identity, "checkout": str(checkout), "python_executable": python,
            "platform": platform.platform(), "machine": platform.machine(), "cpu_count": os.cpu_count(),
            "hardware_observations": hardware}


def support_reason(scenario: dict) -> str | None:
    injector = scenario.get("interference")
    if injector in {"evaluation", "result_consumer", "report_disk", "combined"}:
        return (f"Unchanged CLI has no independently configurable {injector} delay injector. "
                "External CPU/disk contention would not isolate this internal stage; no monkeypatch applied.")
    if scenario.get("fault") in {"evaluator_crash", "worker_crash"}:
        return "Baseline has no independently addressable evaluator/traffic worker process; process-local crash isolation unavailable."
    if scenario.get("fault") in {"slow_storage", "terminate_during_persistence"}:
        return "No supported bounded slow filesystem or internal persistence-stage hook is available; timing an external kill would not prove this boundary."
    return None


def _pack(scenario: dict) -> dict:
    expected = scenario.get("scorer_expected", scenario["response"])
    if scenario["fixture"] == "fragmented_utf8":
        expected = "café 🦒"
    if scenario["fixture"] == "incorrect":
        expected = "42"
    count = scenario["request_count"] if scenario["mode"] == "burst_pack" else 1
    return {"version": 1, "cases": [{
            "fixture_id": f"controlled-{index:06d}" if scenario["mode"] == "burst_pack" else "controlled",
            "messages": [{"role": "user", "content": scenario["prompt"] +
                          (f"\nValidation-ID: {index:06d}" if scenario["mode"] == "burst_pack" else "")}],
            "scorer": "exact", "expected": expected,
            "max_tokens": scenario["max_output_tokens"]} for index in range(count)]}


def make_configuration(scenario: dict, endpoint_url: str) -> dict:
    """Use public RunConfig fields only; all budgets and auxiliary work explicit."""
    ensure_execution_allowed(scenario)
    duration = scenario["active_seconds"]
    concurrency = scenario["concurrency"]
    mode = scenario["mode"]
    config: dict[str, Any] = {
        "targets": [{"name": "reference", "url": endpoint_url, "model": "validation"}],
        "concurrency": concurrency, "stream": scenario["stream"], "retention": "all",
        "samples": 1, "max_output_tokens": scenario["max_output_tokens"],
        "context_limit": max(4096, scenario["prompt_bytes"] + scenario["max_output_tokens"] + 64),
        "request_timeout_seconds": scenario["request_timeout_seconds"],
        "max_duration_seconds": duration + scenario["request_timeout_seconds"] + 15,
        "stop_after_errors": RESOURCE_POLICY["max_case_requests"],
        "max_requests": 1, "checks": ["correctness"],
    }
    if scenario["fixture"] == "tool_calls":
        config.update(checks=["tools"], tool_calling=True, max_requests=4,
                      tool_checks={"modes": ["stream" if scenario["stream"] else "complete"]})
    elif mode == "serial":
        config.update(request_source="request_pack", request_pack=_pack(scenario), max_requests=2)
    elif mode == "burst_pack":
        config.update(request_source="request_pack", request_pack=_pack(scenario),
                      max_requests=scenario["request_count"] * 2)
    elif mode in {"burst", "periodic_batch"}:
        count = scenario["request_count"]
        if count == 1:
            config.update(request_source="request_pack", request_pack=_pack(scenario), max_requests=2)
        else:
            config.update(checks=["arrivals"], max_requests=count,
                arrivals={"mode": "burst", "requests": count,
                    "burst_size": scenario.get("batch_size", count), "max_pending": concurrency,
                    "interval_seconds": 1, "seed": scenario["seed"]})
    elif mode == "sustained_concurrency":
        # recovery is the supported persistent-refill workload, not group().
        response_ms = scenario.get("first_delay_ms", 0) + max(1, scenario.get("event_count", len(scenario.get("events", [])))) * scenario.get("event_delay_ms", 0)
        nominal_seconds = max(.001, response_ms / 1000)
        cap = min(RESOURCE_POLICY["max_case_requests"], math.ceil(duration * concurrency / nominal_seconds * 1.5) + 16)
        config.update(checks=["recovery"], max_requests=max(cap, concurrency * 2 + 2),
                      sustained_seconds=duration,
                      traffic={"long_input_chars": 4096, "long_output_words": 32})
    elif mode in {"fixed_rate", "poisson"}:
        count = math.ceil(scenario["rate_rps"] * duration)
        config.update(checks=["capacity", "correctness"], request_source="request_pack",
                      request_pack=_pack(scenario), max_requests=count * (2 if mode == "poisson" else 1) + 2,
                      traffic={"rates": [scenario["rate_rps"]], "duration_seconds": duration,
                               "arrival": "poisson" if mode == "poisson" else "steady",
                               "max_in_flight": concurrency, "seed": scenario["seed"],
                               "scheduler_lag_tolerance_ms": 20, "drain_timeout_seconds": 5})
    else:
        raise ValueError(f"Unsupported traffic mode: {mode}")
    if scenario.get("interference") == "telemetry":
        config.update(metrics=True, metrics_interval_seconds=.1, metrics_timeout_seconds=2,
                      metrics_max_samples=4096)
        config["targets"][0]["metrics_url"] = endpoint_url.removesuffix("/v1") + "/metrics"
    if scenario.get("fault") == "global_budget":
        config["max_requests"] = 5
    return config


def baseline_command(checkout: Path, python: str, config: Path, output: Path) -> list[str]:
    return [str(Path(python).absolute()), "-m", "giraffe.cli", "run", "--config", str(config.resolve()),
            "--output", str(output.resolve()), "--output-format", "json"]


def _phase(raw: dict, scenario: dict) -> str:
    if raw.get("scenario") in {"pack_control", "recovery_baseline", "recovery"}:
        return "control"
    return "measured"


def iter_normalized_results(requests: Iterable[dict], scenario: dict, context: dict) -> Iterator[dict]:
    measured_index, auxiliary_index = 0, 0
    poisson_offsets = {}
    if scenario["mode"] == "poisson":
        for index, offset in enumerate(expected_poisson_offsets(scenario)):
            key = round(offset * 1e9)
            if key in poisson_offsets:
                raise ValueError("Poisson offsets collide at the exported correlation precision")
            poisson_offsets[key] = index
    for sequence, raw in enumerate(requests):
        phase = _phase(raw, scenario)
        provenance = "adapter_enumeration"
        if phase == "control":
            index = auxiliary_index
            auxiliary_index += 1
            if scenario["mode"] == "burst_pack":
                index = int(raw["fixture_id"].rsplit("-", 1)[1])
                provenance = "unique_request_pack_fixture"
            else:
                provenance = "single_control_request" if scenario["mode"] != "sustained_concurrency" else "named_recovery_probe"
            logical = f"{context['case_id']}:control:{index:06d}"
        else:
            index = measured_index
            measured_index += 1
            if raw.get("scheduled_offset_ms") is not None and scenario.get("rate_rps") and scenario["mode"] != "poisson":
                index = round(raw["scheduled_offset_ms"] * scenario["rate_rps"] / 1000)
                provenance = "scheduled_offset"
            elif scenario["mode"] == "poisson" and raw.get("scheduled_offset_ms") is not None:
                key = round(raw["scheduled_offset_ms"] * 1e6)
                if key not in poisson_offsets:
                    raise ValueError("Exported arrival does not match the frozen Poisson scheduling contract")
                index = poisson_offsets[key]
                provenance = "scheduled_offset"
            elif "arrival_index" in raw.get("workload", {}):
                index = raw["workload"]["arrival_index"]
                provenance = "arrival_index"
            elif scenario["mode"] == "burst_pack":
                index = int(raw["fixture_id"].rsplit("-", 1)[1])
                provenance = "unique_request_pack_fixture"
            elif scenario["mode"] == "sustained_concurrency":
                provenance = "dynamic_duration_admission"
            elif scenario["mode"] == "serial" or scenario.get("request_count") == 1:
                provenance = "single_measured_request"
            logical = f"{context['case_id']}:measured:{index:06d}"
        timing = {"latency_ms": raw.get("elapsed_ms"), "first_answer_ms": raw.get("first_output_ms"),
                  "first_reasoning_ms": raw.get("first_reasoning_ms"),
                  "max_stream_gap_ms": raw.get("max_stream_gap_ms"),
                  "dispatch_ms": raw.get("dispatch_ms"), "completed_ms": raw.get("completed_ms"),
                  "scheduled_ms": raw.get("scheduled_ms"), "dispatch_lag_ms": raw.get("dispatch_lag_ms"),
                  "client_preparation_ms": raw.get("client_preparation_ms"), "client_wait_ms": raw.get("client_wait_ms")}
        for field in ("dispatch_ms", "completed_ms"):
            if isinstance(raw.get(field), (int, float)):
                timing["lifecycle_" + field.removesuffix("_ms") + "_ns"] = round(raw[field] * 1_000_000)
        yield {"schema_version": "1", **context, "logical_request_id": logical,
                     "attempt_id": f"{logical}:attempt:1", "worker_id": "baseline-process",
                     "event_kind": "result_persisted", "event_sequence": sequence,
                     "monotonic_ns": None, "clock_domain": f"baseline-run-relative:{context['case_id']}",
                     "observation_source": "baseline-supported-report-export", "phase": phase,
                     "response_id": raw.get("server_request_id"), "content": raw.get("output"),
                     "fixture_id": raw.get("fixture_id"),
                     "status": raw.get("status"), "score": raw.get("score"),
                     "prompt_tokens": raw.get("input_tokens"), "completion_tokens": raw.get("completion_tokens"),
                     "output_tokens": raw.get("output_tokens"), "stream_terminated": raw.get("stream_terminated"),
                     "valid": raw.get("valid"), "timing": timing,
                     "correlation_confidence": "unique_response_id" if raw.get("server_request_id") else "client_only_unmatched",
                     "planned_identity_provenance": provenance,
                     "event_contents": None, "raw": raw}


def normalize_results(report: dict, scenario: dict, context: dict) -> list[dict]:
    """Compatibility helper for small fixtures; live exports use the iterator."""
    def requests():
        from copy import deepcopy

        for raw in report.get("requests", []):
            if "input_ref" in raw:
                raw = deepcopy(raw)
                reference = raw.pop("input_ref")
                messages = report["inputs"][reference]
                encoded = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
                if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != reference:
                    raise ValueError("Saved input SHA-256 mismatch")
                raw["evaluation"]["messages"] = deepcopy(messages)
            yield raw

    return list(iter_normalized_results(requests(), scenario, context))


def exported_phases(report: dict, scenario: dict, context: dict) -> dict:
    """Preserve observed runner boundaries without aligning unrelated clocks."""
    target = report.get("observations", {}).get("targets", {}).get("reference", {})
    domain = f"baseline-run-relative:{context['case_id']}"
    result = {"clock_domain": domain, "boundary": "exported runner lifecycle; milliseconds since product run start",
              "active_seconds_requested": scenario["active_seconds"],
              "early_termination_reason": report.get("abort_reason"), "retries": 0,
              "evidence": "product-report/report.json"}
    stages = target.get("capacity", [])
    if stages and scenario["mode"] in {"fixed_rate", "poisson"}:
        stage = stages[0]
        start_ms, end_ms = stage.get("timing_interval_ms", [None, None])
        if start_ms is not None and end_ms is not None:
            observed_ms = end_ms - start_ms
            active_ms = min(scenario["active_seconds"] * 1000, observed_ms)
            result.update(active_start_ns=round(start_ms * 1e6), active_end_ns=round((start_ms + active_ms) * 1e6),
                          total_stage_seconds=observed_ms / 1000, drain_seconds=max(0, observed_ms - active_ms) / 1000,
                          fully_offered=stage.get("fully_offered"), not_offered=stage.get("not_offered"),
                          dropped_local=stage.get("dropped_local"), dropped_late=stage.get("dropped_late"),
                          stage_reasons=stage.get("reasons", []),
                          active_end_boundary="requested active deadline capped by observed stage return; drain reported separately")
    elif scenario["mode"] == "sustained_concurrency":
        boundaries = {row["event"]: row["elapsed_seconds"] for row in target.get("phase_boundaries", [])
                      if row.get("phase") == "sustained"}
        if {"scenario_started", "scenario_finished"} <= boundaries.keys():
            start, end = boundaries["scenario_started"], boundaries["scenario_finished"]
            active_end = min(end, start + scenario["active_seconds"])
            result.update(active_start_ns=round(start * 1e9), active_end_ns=round(active_end * 1e9),
                          total_sustained_seconds=end - start, drain_seconds=max(0, end - active_end),
                          active_end_boundary="requested admission deadline capped by observed worker return; final response drain separate",
                          active_window_qualification="derived from supported recovery deadline contract and observed worker-return boundary",
                          sustained_completion=target.get("sustained_completion"),
                          sustained_budget=target.get("sustained_budget"))
    elif scenario['mode'] == 'periodic_batch' and 'validation_arrival_origin_ms' in report:
        start = report['validation_arrival_origin_ms'] / 1000
        result.update(active_start_ns=round(start * 1e9),
                      active_end_ns=round((start + scenario['active_seconds']) * 1e9),
                      active_end_boundary='Scheduled burst window; includes quiet remainder after final burst, not an observed process lifetime',
                      arrival_schedule=target.get('arrivals'))
    # Finite burst and serial cases have no duration contract. Their observed
    # processing window is separate from an active-duration test.
    result["requested_duration_applicable"] = scenario["mode"] in {"fixed_rate", "poisson", "sustained_concurrency"}
    return result
