"""Bounded single-slot capacity-release evidence from native Ollama debug logs.

This proves slot reuse, not a per-request abort reason. It requires an isolated
single-runner deployment and rejects ambiguous/interleaved cache admissions.
"""

import re
from datetime import datetime
from urllib.parse import urlsplit

from giraffe.sglang_traces import TraceTail
from giraffe.caps import MAX_OLLAMA_HEADER_BYTES


class OllamaLogTail(TraceTail):
    def __init__(self, path, target):
        super().__init__(path)
        with self.path.open("rb") as source:
            header = source.read(min(self.offset, MAX_OLLAMA_HEADER_BYTES)).decode(errors="replace")
        port = urlsplit(target.url).port or 80
        parallelism = re.findall(r"\bmsg=load\b.*\bOperation:commit\b.*\bParallel:(\d+)\b", header)
        if (not parallelism or any(value != "1" for value in parallelism)
                or f"127.0.0.1:{port}" not in header
                or target.deployment.serving.get("parallelism") != 1
                or target.deployment.serving.get("exclusive") is not True):
            raise ValueError("Ollama requires native single-slot startup logs, parallelism=1 and exclusive=true")

    def decode(self, lines):
        rows = []
        for raw in lines:
            line = raw.decode(errors="replace")
            if any(message in line for message in (
                    'msg="context limit hit - shifting"',
                    'msg="kv cache removal not supported', 'msg="kv cache cannot shift')):
                raise ValueError("Ollama cache shifting makes growth evidence ambiguous")
            if 'msg="loading cache slot"' not in line:
                continue
            timestamp = re.search(r"\btime=([^\s]+)", line)
            fields = dict(re.findall(r"\b(id|cache|prompt)=(\d+)", line))
            if not timestamp or len(fields) != 3:
                raise ValueError("Unrecognized Ollama cache admission log format")
            when = int(datetime.fromisoformat(timestamp[1]).timestamp() * 1e9)
            rows.append({"source": "ollama::runner", "admitted_at_ns": when,
                         **{key: int(value) for key, value in fields.items()}})
        return rows


def evaluate_slot_reuse(rows, started_at_ns, cancelled_at_ns, cleanup_seconds, control):
    # Native timestamps have millisecond precision. A full prior millisecond
    # allowance is safe only for the admission; never extend the cleanup bound.
    rows = [row for row in rows if row["admitted_at_ns"] >= started_at_ns - 1_000_000]
    if len(rows) != 2 or rows[0]["id"] != rows[1]["id"]:
        return None
    first, reused = rows
    if not first["admitted_at_ns"] <= cancelled_at_ns <= reused["admitted_at_ns"]:
        return None
    # A deterministic uncancelled reference must reach the token cap. Compare
    # native cache growth to reject fully generated reference-length requests.
    tokens = None if control is None else (control.completion_tokens if control.completion_tokens is not None else control.output_tokens)
    generated = reused["cache"] - first["prompt"]
    if (not control or not control.valid or control.finish_reason != "length"
            or tokens is None or control.requested_max_tokens != tokens
            or generated < 0):
        return None
    delay = (reused["admitted_at_ns"] - cancelled_at_ns) / 1e9
    full_budget = generated >= tokens - 1
    return {"status": "fail" if full_budget else "pass" if delay <= cleanup_seconds else "inconclusive",
            "reason": ("Cancelled request grew its cache to the capped reference length; work was not released early."
                       if full_budget else
                       "The same isolated runner slot admitted the follow-up within the cleanup deadline."
                       if delay <= cleanup_seconds else
                       "Slot reuse was late; logs do not identify when the prior request actually terminated."),
            "termination_ms": None, "slot_reuse_ms": delay * 1000,
            "evidence_kind": "single_slot_reuse", "native_admissions": rows, "reference_output_tokens": tokens,
            "cancelled_cache_growth_tokens": generated,
            "scope": "Capacity release verified in an isolated single-slot run; abort reason and exact termination time unavailable."}
