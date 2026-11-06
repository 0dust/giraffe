"""Bounded, read-only LiteLLM spend-log attribution for marked test traffic."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import ssl
import time
import uuid

import httpx

from .identities import credential_headers, identity_headers, identity_scope, litellm_key_fingerprint
from .models import CheckResult, RequestSpec


CHECK = "gateway_attribution"
COLLECTION_VERSION = 4
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_PAGES = 8
PAGE_SIZE = 128
POLL_FRACTIONS = (0, .425, .85)
_UNSUPPORTED_READER_STATUSES = {404, 405, 501}


def configured_probe_count(config, target):
    settings, gateway = config.gateway_attribution, target.gateway
    if (CHECK in config.checks and settings and gateway and gateway.profile == "litellm"
            and all(alias in target.test_identities for alias in settings.identities)):
        return settings.samples * len(settings.identities)
    return 0


def _state(state, reason, **details):
    return {"state": state, "reason": reason, "matched_requests": 0,
            "mismatched_requests": 0, "unverified_requests": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "identities": [], **details}


def _number(value):
    return type(value) is int and 0 <= value <= 10**12


def _normalize(rows, marker, expected):
    """Normalize transient row evidence; never include it in reports."""
    matched = defaultdict(dict)
    owners = {}
    unrelated = invalid = 0
    for row in rows:
        if not isinstance(row, dict):
            invalid += 1
            continue
        metadata = row.get("metadata")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (ValueError, UnicodeError, RecursionError):
                metadata = None
        tags = metadata.get("spend_logs_metadata") if isinstance(metadata, dict) else None
        same_session = row.get("session_id") == marker
        if not isinstance(tags, dict) or tags.get("giraffe_run") != marker:
            # LiteLLM can share a session ID across child calls. Without a
            # matching request marker, a row in this session is ambiguous.
            if same_session:
                invalid += 1
            else:
                unrelated += 1
            continue
        local_id = tags.get("giraffe_request")
        gateway_id = row.get("request_id")
        if (not isinstance(local_id, str) or local_id not in expected
                or not same_session
                or not isinstance(gateway_id, str) or not 0 < len(gateway_id) <= 256):
            invalid += 1
            continue
        prompt, completion, total = (row.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens"))
        actual_key = row.get("api_key")
        key_valid = (isinstance(actual_key, str) and len(actual_key) == 64
                     and all(char in "0123456789abcdef" for char in actual_key))
        complete = (all(_number(n) for n in (prompt, completion, total))
                    and prompt + completion == total and row.get("status") == "success"
                    and key_valid)
        value = {"complete": complete,
                 "key_hash": actual_key if key_valid else None,
                 "identity_matches": bool(expected[local_id]._gateway_key_hash)
                    and actual_key == expected[local_id]._gateway_key_hash,
                 "prompt_tokens": prompt if _number(prompt) else None,
                 "completion_tokens": completion if _number(completion) else None}
        previous = matched[local_id].get(gateway_id)
        # Repeated pages/scrapes of one row are not additional requests. A row
        # that changes its final usage or owner is unstable evidence.
        if previous is not None and previous != value:
            value["complete"] = False
            invalid += 1
        owner = owners.setdefault(gateway_id, local_id)
        if owner != local_id:
            matched[owner][gateway_id]["complete"] = value["complete"] = False
            invalid += 1
        matched[local_id][gateway_id] = value
    return matched, unrelated, invalid


def _assess(records, matched, planned, *, complete, unstable=False):
    identities = {}
    totals = _state("unverified", "Some marked requests lack complete attribution evidence.",
                    planned_requests=planned, observed_requests=len(records))
    for record in records:
        alias = record.identity
        row = identities.setdefault(alias, {"identity": alias, "observed_requests": 0,
            "matched_requests": 0, "mismatched_requests": 0, "unverified_requests": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "request_ids": []})
        row["observed_requests"] += 1
        row["request_ids"].append(record.id)
        observations = list(matched.get(record.id, {}).values())
        good = (complete and not unstable and record.status == "completed" and record.valid
                and (not record.stream or record._gateway_terminal_usage)
                and record._gateway_key_hash and _number(record.input_tokens) and _number(record.completion_tokens)
                and len(observations) == 1 and observations[0]["complete"])
        if not good:
            field = "unverified_requests"
        else:
            observation = observations[0]
            field = "matched_requests" if (observation["identity_matches"]
                and observation["prompt_tokens"] == record.input_tokens
                and observation["completion_tokens"] == record.completion_tokens) else "mismatched_requests"
            if field == "matched_requests":
                for metric in ("prompt_tokens", "completion_tokens"):
                    row[metric] += observation[metric]
                    totals[metric] += observation[metric]
        row[field] += 1
        totals[field] += 1
    totals["identities"] = list(identities.values())
    if totals["mismatched_requests"]:
        totals.update(state="mismatch", reason="Marked requests have different gateway identity or token attribution.")
    elif records and len(records) == planned and totals["matched_requests"] == planned:
        totals.update(state="verified", reason="Every marked request matches its gateway identity and final token counts.")
    return totals


def _client(config, headers, timeout):
    verify = ssl.create_default_context(cafile=config.ca_bundle) if config.ca_bundle else True
    return httpx.AsyncClient(headers=headers, verify=verify, proxy=config.proxy,
                             timeout=timeout, follow_redirects=False)


def _spend_page(client, url, marker, start_date, end_date, page, page_size):
    return client.stream("GET", url, params={"session_id": marker, "page": page,
        "page_size": page_size, "start_date": start_date, "end_date": end_date})


async def _reader_preflight(run, target, headers):
    """Check the evidence endpoint without admitting an inference request."""
    gateway = target.gateway
    now = time.monotonic()
    stop_reason = run.stop_reason(target.name)
    if stop_reason and stop_reason != "maximum request budget reached":
        return None
    timeout = min(run.config.gateway_attribution.settle_seconds, run.config.metrics_timeout_seconds,
                  (run.deadline - now) / 2)
    if timeout <= 0:
        return None
    try:
        marker = "giraffe-" + uuid.uuid4().hex
        current = datetime.now(timezone.utc)
        start_date = current.strftime("%Y-%m-%d %H:%M:%S")
        end_date = (current + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
        with identity_scope(True):
            async with asyncio.timeout_at(now + timeout):
                reader = await asyncio.to_thread(_client, run.config, headers, timeout)
                async with reader:
                    async with _spend_page(reader, gateway.url, marker, start_date, end_date, 1, 1) as response:
                        return response.status_code
    except (httpx.HTTPError, OSError, TimeoutError, ValueError):
        return None


async def _pages(client, url, marker, *, start_date, end_date, max_rows, stop):
    rows = []
    row_ids = set()
    expected_total = None
    page_size = min(PAGE_SIZE, max_rows)
    for page in range(1, MAX_PAGES + 1):
        if stop.is_set():
            return rows, False
        async with _spend_page(client, url, marker, start_date, end_date, page, page_size) as response:
            if response.status_code in _UNSUPPORTED_READER_STATUSES:
                return "unsupported", False
            if response.status_code != 200:
                return "unavailable", False
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BODY_BYTES:
                    return rows, False
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError, RecursionError):
                return rows, False
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                return rows, False
            data = payload["data"]
            total = payload.get("total")
            if (payload.get("total_is_capped", False) is not False
                    or type(total) is not int or not 0 <= total <= max_rows
                    or expected_total is not None and total != expected_total):
                return rows, False
            pages = (total + page_size - 1) // page_size
            if any(type(payload.get(key)) is not int or payload[key] != expected
                   for key, expected in (("page", page), ("page_size", page_size), ("total_pages", pages))):
                return rows, False
            if len(data) != min(page_size, total - len(rows)):
                return rows, False
            # Offset pages can overlap when the result changes between reads.
            for row in data:
                row_id = row.get("request_id") if isinstance(row, dict) else None
                if not isinstance(row_id, str) or not row_id or row_id in row_ids:
                    return rows, False
                row_ids.add(row_id)
            expected_total = total
            rows.extend(data)
            if len(rows) == total:
                return rows, True
    return rows, False


async def execute(run, target, client, *, deferred=False):
    settings, gateway = run.config.gateway_attribution, target.gateway
    observation = _state("unavailable", "Gateway evidence endpoint and credentials are not configured.")
    run.observations[target.name][CHECK] = observation
    if gateway is None or settings is None:
        return
    if gateway.profile != "litellm":
        observation.update(state="unsupported", reason="No supported attribution adapter selected for this gateway profile.")
        return
    # The same credential-reference validation/resolution applies to the reader,
    # but its credential is never taken from an inference identity.
    try:
        headers = credential_headers(gateway)
        fingerprints = set()
        for alias in settings.identities:
            fingerprint = litellm_key_fingerprint(identity_headers(target, alias))
            if fingerprint is None:
                continue
            if fingerprint in fingerprints:
                observation.update(state="unverified", reason="Selected aliases must resolve to distinct LiteLLM keys.")
                return
            fingerprints.add(fingerprint)
    except ValueError:
        observation.update(reason="Gateway reader or selected identity credentials are unavailable.")
        return
    if deferred or run.config.max_requests is not None and len(run.config.checks) > 1:
        reader_status = await _reader_preflight(run, target, headers)
        if reader_status in _UNSUPPORTED_READER_STATUSES:
            observation.update(state="unsupported", reason="Read-only gateway spend logs are unsupported.",
                               planned_requests=0, observed_requests=0)
            return
        if deferred and reader_status != 200:
            observation.update(reason="Read-only gateway spend logs were unavailable within the remaining run budget.")
            return
    marker = "giraffe-" + uuid.uuid4().hex
    start_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    records = []
    planned = settings.samples * len(settings.identities)
    run.plan_condition(target.name, CHECK, planned=planned)
    try:
        for _ in range(settings.samples):
            for alias in settings.identities:
                if not run.allowed(target.name):
                    break
                spec = RequestSpec(fixture_id="gateway_attribution.v1", scenario=CHECK, check_ids=[CHECK],
                    identity=alias, messages=[{"role": "user", "content": "Reply with OK."}],
                    max_tokens=min(16, run.config.max_output_tokens),
                    options={"stream_options": {"include_usage": True}} if run.config.stream else {},
                    workload={"gateway_run": marker})
                record = await run.request(target, client, spec)
                if record is not None:
                    records.append(record)
    except asyncio.CancelledError:
        records = [record for record in run.records if record.target == target.name
                   and record.workload.get("gateway_run") == marker]
        observation = _assess(records, {}, planned, complete=False)
        observation.update(reason="Gateway identity probes were interrupted before attribution completed.")
        run.observations[target.name][CHECK] = observation
        for record in records:
            record._gateway_key_hash = None
        raise
    finally:
        run.finish_condition(target.name, CHECK)
    if not records:
        observation.update(state="unverified", reason="No identified requests were admitted within the run budget.", planned_requests=planned)
        return
    collection_started = time.monotonic()
    settle_deadline = collection_started + settings.settle_seconds
    deadline = min(run.deadline, settle_deadline)
    collection_window = max(0, deadline - collection_started)
    end_date = (datetime.now(timezone.utc) + timedelta(seconds=collection_window + 1)).strftime("%Y-%m-%d %H:%M:%S")
    attempts, previous, unstable = 0, {}, False
    assessment = _assess(records, {}, planned, complete=False)
    run.observations[target.name][CHECK] = assessment
    expected = {record.id: record for record in records}
    try:
        with identity_scope(True):
            remaining = max(.001, min(run.config.metrics_timeout_seconds, deadline - time.monotonic()))
            reader = await asyncio.to_thread(_client, run.config, headers, remaining)
            async with reader:
                for fraction in POLL_FRACTIONS:
                    next_read = collection_started + collection_window * fraction
                    if (delay := next_read - time.monotonic()) > 0:
                        try:
                            await asyncio.wait_for(run.stop.wait(), timeout=delay)
                        except TimeoutError:
                            pass
                    if time.monotonic() >= deadline or run.stop.is_set():
                        break
                    attempts += 1
                    async with asyncio.timeout_at(deadline):
                        rows, complete = await _pages(reader, gateway.url, marker,
                            start_date=start_date, end_date=end_date,
                            max_rows=run.config.metrics_max_series, stop=run.stop)
                    if isinstance(rows, str):
                        assessment = _assess(records, {}, planned, complete=False)
                        assessment.update(state=rows, reason="Read-only gateway spend logs are unavailable or unsupported.")
                        break
                    matched, unrelated, invalid = _normalize(rows, marker, expected)
                    current = {(local_id, gateway_id): value for local_id, entries in matched.items()
                               for gateway_id, value in entries.items()}
                    unstable |= invalid > 0 or any(key not in current or (value["complete"] and current[key] != value)
                                                   for key, value in previous.items())
                    previous = current
                    # Early complete pages can predate conflicting rows from
                    # asynchronous writes. Qualify only after the final poll.
                    settled = (attempts == len(POLL_FRACTIONS) and deadline == settle_deadline
                               and time.monotonic() < deadline)
                    assessment = _assess(records, matched, planned,
                        complete=settled and complete and invalid == 0, unstable=unstable)
                    assessment.update(unrelated_rows=unrelated, invalid_rows=invalid,
                                      collection_complete=settled and complete, evidence_changed=unstable)
                if run.stop.is_set():
                    assessment = _assess(records, {}, planned, complete=False)
                    assessment.update(reason="Gateway evidence collection was interrupted.")
                elif (assessment["state"] == "unverified"
                      and (attempts < len(POLL_FRACTIONS) or deadline < settle_deadline)):
                    assessment.update(reason="The final spend-log read could not qualify within the run budget.")
    except asyncio.CancelledError:
        assessment = _assess(records, {}, planned, complete=False)
        assessment.update(reason="Gateway evidence collection was interrupted.")
        raise
    except (httpx.HTTPError, OSError, TimeoutError, ValueError):
        assessment = _assess(records, {}, planned, complete=False)
        assessment.update(reason="Gateway evidence collection did not complete within its bounded window.",
                          collection_complete=False, evidence_changed=unstable)
    finally:
        # Secret-derived fingerprints are transient and never included in reports.
        for record in records:
            record._gateway_key_hash = None
        assessment.update(collection_attempts=attempts, collection_version=COLLECTION_VERSION,
            source="litellm_spend_logs_v2",
            evidence_scope="Exact generated request markers; aggregate counters and counter resets are not used.")
        run.observations[target.name][CHECK] = assessment


def result(run, target):
    selected = CHECK in run.config.checks
    data = (run.observations[target.name].get(CHECK)
            or _state("unverified", "Gateway attribution was not exercised.")) if selected else _state("not_selected", "Not selected.")
    status = {"not_selected": "skipped", "verified": "pass", "mismatch": "fail"}.get(data["state"], "inconclusive")
    configured = (run.config.gateway_attribution is not None and target.gateway is not None
                  and target.gateway.profile == "litellm")
    required = selected and (run.config.checks == [CHECK] or (configured and data["state"] != "unsupported"))
    return CheckResult(id=CHECK, target=target.name, title="Gateway usage attribution", required=required,
                       status=status, summary=data["reason"], metrics=data,
                       evidence_ids=[r.id for r in run.records if r.target == target.name and CHECK in r.check_ids])
