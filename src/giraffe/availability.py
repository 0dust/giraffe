"""Explain missing performance evidence using facts from one request window."""
from __future__ import annotations


from collections import Counter
from dataclasses import dataclass, replace

from .run_evidence import RunEvidence, cancelled_by_stop, request_phase_end_ms, run_stop_action, stop_ends_phase
from .scoring_facts import scoring_gap

CAUSES = ("check_not_selected", "data_missing", "too_few_samples", "not_applicable",
          "endpoint_unsupported", "not_configured", "collector_unavailable", "not_observed", "unknown")
NO_ACTION_OK = {"not_applicable", "endpoint_unsupported", "not_observed"}
FORBIDDEN_TEXT = ("No eligible observations in this scope.", "Collect eligible observations for this scope.",
    "Select the corresponding workload or transport to collect this measure.",
    "Enable the corresponding collector and rerun the workload.",
    "No fresh compatible observations or nonzero measurement population.",
    "Collect at least two fresh compatible exporter samples during the workload.")
LIMITS = {"reason": 200, "action": 170}


@dataclass(frozen=True)
class Explanation:
    cause: str
    reason: str
    action: str | None = None
    detail: str | None = None
    state: str | None = None
    phase: str = "requests"


def canonical_check(check):
    from .models import LOAD_CHECK_ALIASES
    return LOAD_CHECK_ALIASES.get(check, check)


def window_check_ids(window, cohort):
    """Checks owning a window, independent of its completed-reply population."""
    if window.get("check_ids") is not None:
        return frozenset(canonical_check(check) for check in window["check_ids"])
    ids = {canonical_check(check) for record in cohort for check in record.check_ids}
    if any(record.load_point for record in cohort):
        ids.add("load")
    return frozenset(ids)


@dataclass(frozen=True)
class WindowIdentity:
    owned_checks: frozenset[str]
    cohort: list
    dispatched: list


@dataclass
class WindowFacts:
    report: object
    window: dict
    population: list
    good: list
    rate_good: list
    selected_series: list
    duration: float | None
    finished: list
    cohort: list | None = None
    dispatched: list | None = None

    def __post_init__(self):
        from . import states
        from .models import CHECK_NAMES
        self.cohort = self.population if self.cohort is None else self.cohort
        self.dispatched = ([r for r in self.cohort if r.dispatch_ms is not None]
                           if self.dispatched is None else self.dispatched)
        self.identity = WindowIdentity(window_check_ids(self.window, self.cohort), self.cohort, self.dispatched)
        self.owned_checks = self.identity.owned_checks
        self.d = len(self.dispatched)
        self.c_in = len(self.population)
        end = self.window.get("end_ms")
        self.late = [r for r in self.dispatched if r.completed_ms is not None and end is not None
                     and (r.completed_ms > end if self.window.get("end_inclusive") else r.completed_ms >= end)]
        self.in_flight = [r for r in self.dispatched if r.completed_ms is None]
        self.config = self.report.manifest.get("config", {})
        target = self.window.get("target") or (self.cohort[0].target if self.cohort else None)
        self.evidence = RunEvidence.from_report(self.report, target, self.good)
        self.run_stop, self.scoring = self.evidence.stop, self.evidence.scoring
        # A cohort identifies workload ownership, never interruption of a stage.
        # Only this window's dispatches/completions can establish that it stopped.
        own = [*self.dispatched, *self.late, *self.in_flight, *self.population]
        interrupted = any(cancelled_by_stop(r) or (r.completed_ms is None and not r.intentional
                          and r.status != states.REQUEST_COMPLETED and r.dispatch_ms is not None) for r in own)
        start = self.window.get("start_ms")
        stop_ms = request_phase_end_ms(self.report, target) if self.run_stop and not self.d else None
        # Missing interval bounds can hide dispatches from the numeric window
        # count; a recorded dispatch still disproves an unreached window.
        unreached = (not self.d and not any(r.dispatch_ms is not None for r in own)
                     and (start is None or (stop_ms is not None and stop_ms <= start)))
        self.stop_affected = bool(self.run_stop and (interrupted or unreached))
        selection = self.report.manifest.get("effective_checks") or self.config.get("checks") or ()
        self.selected = tuple(dict.fromkeys(canonical_check(check) for check in selection))
        self.run_checks = {canonical_check(check) for r in self.report.requests for check in r.check_ids}
        if any(r.load_point for r in self.report.requests):
            self.run_checks.add("load")
        # Keep the authoritative selection, then recover omitted selection metadata
        # from recorded checks. All hints use the same canonical, ordered list.
        recorded = (canonical_check(check) for r in self.report.requests for check in r.check_ids)
        self.hint_checks = tuple(dict.fromkeys((*self.selected, *(c for c in recorded if c in CHECK_NAMES),
                                               *(("load",) if "load" in self.run_checks else ()))))
        self.configured_stream = self.config.get("stream", True)
        design_checks = {"gateway_access", "gateway_policy"}
        self.nonstreaming_by_design = tuple(bool(design_checks.intersection(r.check_ids)) or
                                           r.scenario in {"judging", "preflight"} for r in self.cohort)
        self.stream_intended = tuple(self.configured_stream and not designed for designed in self.nonstreaming_by_design)
        self.streamed = any(r.stream for r in self.dispatched or self.cohort)
        self.timing_versioned = any(r.timing_schema >= 1 for r in self.cohort)
        self.nonstreaming_check = next((CHECK_NAMES[c] for r, intended in zip(self.cohort, self.stream_intended)
                                       for c in r.check_ids if c in CHECK_NAMES and not intended), "This workload")
        self.token_evidence = {key: Counter(r.workload.get("token_evidence", {}).get(key, "not_recorded")
                                          for r in self.good) for key in ("usage", "logprobs", "reason")}
        self.reasoning_records = tuple(self.has_reasoning(r) for r in self.population)
        self.reasoning_count = sum(self.reasoning_records)
        self.reasoning_seen = bool(self.reasoning_count)
        self.reasoning_unknown = any(self.text_not_kept(r) and not seen
                                     for r, seen in zip(self.population, self.reasoning_records))
        self.reasoning_unseparated = sum(self.has_reasoning(r) and r.output_tokens is None
                                        and r.completion_tokens is not None for r in self.good)
        self.pace_reasons = Counter(reason[0] for r in self.good if (reason := r.generation_pace_reason))
        self.first_token_reasons = Counter(r.first_token_reason for r in self.population if r.first_token_reason)
        self.no_usage = sum(r.completion_tokens is None and r.output_tokens is None for r in self.good)
        self.no_cached = sum(r.cached_prompt_tokens is None for r in self.good)
        self.n_stream_good = sum(r.stream for r in self.good)
        self.scheduled = any(r.scheduled_ms is not None for r in self.cohort)
        self.missing_timestamps = (any(r.dispatch_ms is None for r in self.dispatched or self.cohort)
                                   or any(r.completed_ms is None for r in self.finished))
        self.missing_usage = {name: sum(getattr(r, attr) is None for r in self.good) for name, attr in
                              (("usage.prompt_tokens", "input_tokens"), ("usage.completion_tokens", "completion_tokens"),
                               ("usage.prompt_tokens_details.cached_tokens", "cached_prompt_tokens"))}
        self.missing_answer_usage = sum(r.output_tokens is None for r in self.good)
        self.answer_scored = any(r.score is not None for r in self.good)
        self.answer_scoring_configured = any(fact["scorer"] not in (None, "none") for fact in self.scoring)
        self.load_window = self.owns("load")
        load = self.config.get("load_test") or {}
        sources = {"primary": load, **{f"legacy_{i}_{item['source']}": item["settings"]
                                       for i, item in enumerate(load.get("migrated_tests", []))}}
        self.load_settings = [sources.get((r.load_point or {}).get("source", "primary"), load) for r in self.cohort] or [load]
        self.patterns = {pattern for r in self.cohort
                         if (pattern := (r.load_point or {}).get("pattern") or r.workload.get("pattern"))}
        observation = self.report.observations.get("targets", {}).get(self.window.get("target") or
                      (self.cohort[0].target if self.cohort else None), {})
        transitions = [row for i, row in enumerate(observation.get("schedule_transitions", []))
                       if i in self.window.get("schedule_indices", []) and row.get("kind") != "drain"]
        transition_patterns = {row.get("pattern") or ("concurrent" if row.get("intended_concurrency") is not None else "constant")
                               for row in transitions}
        if self.window.get("kind") == "stage" and transition_patterns:
            self.patterns = transition_patterns
        elif not self.patterns:
            self.patterns.update(transition_patterns)
        if not self.patterns:
            self.patterns = {name for settings in self.load_settings for name, pattern in settings.get("patterns", {}).items()
                             if isinstance(pattern, dict) and pattern.get("enabled")}
        if self.scheduled and not transition_patterns:
            self.patterns.add("constant")
        self.arrival_pattern = bool(self.patterns.intersection({"constant", "poisson", "bursts", "steady_with_bursts"}))
        points = [r.load_point for r in self.cohort if r.load_point]
        self.bursts_only = bool(self.patterns) and self.patterns <= {"bursts"}
        self.all_bursty = bool(points) and all(point.get("bursts") or point.get("pattern") == "bursts" for point in points)
        self.held_concurrency = self.load_window and not self.arrival_pattern
        criteria = [settings.get("pass_criteria", {}) for settings in self.load_settings]
        if any("capacity" in r.check_ids for r in self.cohort) and not any(r.load_point for r in self.cohort):
            limits = (self.report.manifest.get("effective_checks", {}).get("capacity") or self.config).get("limits", {})
            self.latency_limits = any(limits.get(key) is not None for key in ("first_output_ms", "latency_ms"))
        else:
            self.latency_limits = any(c.get(key) is not None for c in criteria for key in ("max_first_answer_ms", "max_completion_ms"))
        self.interference_enabled = any(settings.get("workloads", {}).get("interference", {}).get("enabled", False)
                                        for settings in self.load_settings) or any(point.get("type") == "interference" or
                                                                                point.get("pattern") == "interference" for point in points)
        self.interference_mode = next((point["interference_mode"] for point in points if point.get("interference_mode")),
                                      self.load_settings[0].get("workloads", {}).get("interference", {}).get("mode"))

    @staticmethod
    def has_reasoning(record):
        return bool(record.reasoning_tokens or record.first_reasoning_ms is not None or record.reasoning_stripped
                    or record.reasoning_read_timestamps_ms or record.reasoning.strip()
                    or (record.completion_tokens is not None and record.output_tokens is None))

    def text_not_kept(self, record):
        if record.output or record.reasoning or record.tool_calls:
            return False
        policy = self.config.get("retention")
        return bool(record.output_chars or record.answer_chars or
                    self.report.manifest.get("storage", {}).get("response_bodies_omitted") or
                    policy == "none")

    @property
    def n(self):
        return len(self.population)

    @property
    def n_good(self):
        return len(self.good)

    @property
    def n_failed(self):
        return self.n - self.n_good

    def owns(self, check):
        return canonical_check(check) in self.owned_checks

    def ran(self, check):
        check = canonical_check(check)
        return check in self.selected or self.owns(check) or check in self.run_checks

    def checks_flag(self, add):
        add = canonical_check(add)
        return "--checks " + ",".join(dict.fromkeys((*self.hint_checks, add)))

    def add_check(self, check):
        suffix = "" if self.hint_checks else " (added to your selected checks)"
        return self.checks_flag(check) + suffix

    @staticmethod
    def dominant(counter):
        return counter.most_common(1)[0] if counter else (None, 0)


@dataclass(frozen=True)
class Rule:
    explain: object
    dependencies: frozenset = frozenset()
    usage_fields: tuple = ()
    owner_check: str | None = None
    stop_sensitive: bool = False


LEGACY_TIMING = Explanation("data_missing", "These requests were recorded without streamed timing evidence (older Giraffe).",
                            "Rerun with the current Giraffe.")


def nonstreamed(f):
    if not f.configured_stream and not all(f.nonstreaming_by_design):
        return Explanation("not_applicable", "Replies were not streamed, so token timing cannot be observed.",
                           "Turn streaming on (omit --no-stream).")
    return Explanation("not_applicable", f"{f.nonstreaming_check} sends non-streamed requests, so token timing is not observed in this window.",
                       "Choose a streamed workload in the Measurement menu.")


def applicability(rule, f):
    if rule.owner_check:
        from .models import CHECK_NAMES
        check = rule.owner_check
        name = CHECK_NAMES[check]
        if not f.ran(check):
            return Explanation("check_not_selected", f"{name} was not selected, and this measure comes from it.", f"Add it: {f.add_check(check)}.")
        if not f.owns(check):
            return Explanation("not_applicable", f"This window measures {f.window.get('workload_label', 'another workload')}, which is not part of {name}, so this measure does not apply.",
                               f"Choose a {name} workload in the Measurement menu.")
    if "streaming" in rule.dependencies and f.cohort and not f.streamed and all(f.nonstreaming_by_design):
        return nonstreamed(f)
    return None


def failed_reply_summary(f):
    # Reuse the request-result vocabulary with safe structured fields only.
    # Neither response/error bodies nor endpoint URLs enter this explanation.
    from . import states
    from .conditions import request_result_text
    from .reporting import connection_summary
    failures = Counter()
    for record in f.population:
        if record.status == states.REQUEST_COMPLETED and record.valid:
            continue
        label = ("Invalid completion" if record.status == states.REQUEST_COMPLETED and not record.valid else
                 request_result_text({name: getattr(record, name) for name in
                                      ("status", "valid", "http_status", "termination_reason", "termination_limit_seconds")}))
        if label == "Request failed":
            # The existing connection helper whitelists exception classes and
            # never copies the free-form error. Pass no endpoint URL.
            connection = connection_summary([record], "")
            if connection:
                label = connection.removeprefix("Could not connect to  (").removesuffix("); no checks could run")
        failures[label] += 1
    label, count = f.dominant(failures)
    return f"; most common: {label}, {count} of {f.n_failed}" if label else ""


def window_gap(rule, f):
    if "replies" not in rule.dependencies or f.c_in or not f.d or f.window.get("kind") != "stage":
        return None
    duration = f"{f.duration:g} s " if f.duration else ""
    outcome = (f"{len(f.late)} finished after it ended (during drain)." if f.late else
               f"{len(f.in_flight)} had not finished when the run ended.")
    reason = f"This {duration}stage dispatched {f.d} request(s), but none completed inside its measurement interval; {outcome}"
    action = ("Lengthen the stage so replies finish inside it: raise load_test.schedule.duration_seconds, or lower the output cap (--max-output-tokens)."
              if f.ran("load") else
              "Choose the Drain window or the full run in the Measurement menu to see these replies.")
    return Explanation("too_few_samples", reason, action)


def precondition(rule, f):
    if "replies" not in rule.dependencies:
        return None
    if not f.n and not f.d:
        if f.stop_affected:
            return Explanation("too_few_samples", f"{f.run_stop[1]} No request had been sent in this window.",
                               run_stop_action(f.run_stop[0]))
        return Explanation("too_few_samples", "No requests fell in this window.",
                           "Choose a window that sent requests in the Measurement menu, or rerun with more requests.")
    if not f.n_good:
        if f.stop_affected and f.population and all(cancelled_by_stop(r) for r in f.population):
            return Explanation("data_missing", f"{f.run_stop[1]} {f.n_failed} of {f.n} request(s) were cancelled by it.",
                               run_stop_action(f.run_stop[0]))
        return Explanation("data_missing", f"No valid completed replies in this window ({f.n_failed} of {f.n} failed or invalid{failed_reply_summary(f)}).",
                           "Fix the failing requests first (see the check results); this measure uses valid replies only.")
    if "timing" in rule.dependencies and not f.timing_versioned:
        return LEGACY_TIMING
    if "streaming" in rule.dependencies and not f.streamed:
        return nonstreamed(f)
    return None

def request_rate(key, row, f):
    if not f.n and not f.d:
        if f.stop_affected:
            return Explanation("too_few_samples", f"{f.run_stop[1]} No request had been sent in this window.",
                               run_stop_action(f.run_stop[0]))
        return precondition(Rule(None, frozenset({"replies"})), f)
    if f.missing_timestamps:
        return Explanation("data_missing", "Request start/completion timestamps were not recorded for this window, so requests per second cannot be computed.",
                           "Rerun with a current Giraffe version; saved requests without timing cannot be recovered.")
    span = (f.window.get("end_ms") or 0) - (f.window.get("start_ms") or 0)
    action = (f"Add Load test: {f.add_check('load')}. Or raise Maximum requests (--max-requests)."
              if not f.ran("load") else "Raise Maximum requests (--max-requests) or the load duration.")
    return Explanation("too_few_samples", f"This window has {f.d} dispatched request(s) spanning {span:g} ms; a rate needs a window longer than 0 s.", action)


USAGE_FIELDS = {
    "input_tokens": ("usage.prompt_tokens",), "input_length": ("usage.prompt_tokens",),
    "input_throughput": ("usage.prompt_tokens",),
    "cached_tokens": ("usage.prompt_tokens_details.cached_tokens",),
    "cached_length": ("usage.prompt_tokens_details.cached_tokens",),
    "cached_share": ("usage.prompt_tokens_details.cached_tokens",),
    **{key: ("usage.prompt_tokens", "usage.completion_tokens") for key in
       ("total_tokens", "total_throughput", "total_per_in_flight")},
}


def usage(key, row, f):
    from . import states
    fields = USAGE_FIELDS.get(key, ("usage.completion_tokens",))
    missing = [name for name in fields if f.missing_usage.get(name)]
    # A total requires paired fields. Name the actual missing population, not
    # completion usage merely because most token rows read it.
    field_name = " and ".join(missing) if missing else fields[0]
    if f.reasoning_unseparated and key in {"output_throughput", "answer_tokens", "answer_length", "output_rate", "output_per_in_flight"}:
        field_name = "usage.completion_tokens_details.reasoning_tokens"
        result = Explanation("data_missing",
            f"{f.reasoning_unseparated}/{f.n_good} replies include reasoning but no {field_name}, so answer tokens cannot be separated.",
            f"Use an endpoint that reports {field_name}.",
            f"{field_name} separates reasoning from answer tokens; completion throughput includes reasoning.")
    elif f.token_evidence["usage"].get(states.TOKEN_EVIDENCE_REJECTED) and all(f.missing_usage.get(name) == f.n_good for name in (missing or fields)):
        return Explanation("endpoint_unsupported", f"The endpoint rejected stream_options.include_usage and returned no {field_name}. Request throughput and latency are still measured.")
    else:
        count = max((f.missing_usage[name] for name in missing), default=f.missing_answer_usage)
        result = Explanation("data_missing",
            f"The endpoint returned no token counts ({field_name}) on {count}/{f.n_good} valid replies for {row['label'].lower()}.",
            f"Check that the endpoint returns {field_name}" + (" (Giraffe requests it with stream_options.include_usage)." if len(missing) < 2 else "."),
            f"{field_name} must be present on the replies counted by this measure. Request throughput and latency still work.")
    if key in RATE_USAGE and not f.ran("load"):
        result = Explanation(result.cause, result.reason,
            result.action + f" Separately, to compare rates under traffic add Load test: {f.add_check('load')}.", result.detail)
    if key in RATE_USAGE and not f.duration:
        return request_rate(key, row, f)
    return result


def pace(key, row, f):
    from . import states
    code, count = f.dominant(f.pace_reasons)
    m = f.n_good
    name = "generation pace" if key == "generation_pace" else "TPOT"
    if code == "not_streamed":
        return nonstreamed(f)
    if code == "no_usage":
        unsupported = bool(f.token_evidence["usage"].get(states.TOKEN_EVIDENCE_REJECTED))
        return Explanation("endpoint_unsupported" if unsupported else "data_missing",
            f"The endpoint returned no token counts (usage.completion_tokens) on {count}/{m} streamed replies; {name} needs counts at the first and last streamed read.",
            None if unsupported else "Check that the endpoint returns usage.completion_tokens; Giraffe requests it via stream_options.include_usage.")
    if code == "reasoning_unseparated":
        return Explanation("data_missing", f"{count}/{m} replies include reasoning but the endpoint reports no reasoning_tokens, so answer-token pace is unknown.",
                           "Use an endpoint that reports usage.completion_tokens_details.reasoning_tokens.")
    if code == "short_answer":
        action = (f"Add Generation, which requests a longer answer: {f.add_check('generation')}." if not f.ran("generation") else
                  "Raise Output cap (--max-output-tokens) or use prompts that produce longer answers.")
        return Explanation("too_few_samples", f"Answers were too short: {count}/{m} replies had 1 token or fewer, so there is no token interval to time.", action)
    if code == "single_read":
        return Explanation("endpoint_unsupported", f"{count}/{m} answers arrived in a single network read (streaming is buffered by the server or a proxy), leaving no first-to-last interval.",
                           "Check the recorded answer-read counts; compare the endpoint directly if you suspect buffering.")
    if code == "no_timing":
        return Explanation("data_missing", f"First/last answer times were not recorded on {count}/{m} replies.", "Rerun with a current Giraffe version.")
    if code == "tool_call":
        return Explanation("not_applicable", f"Tool-call replies ({count}/{m}) have no answer-token stream to time.")
    if code == "final_only_usage":
        return Explanation("endpoint_unsupported", f"The endpoint reported only a final token total on {count}/{m} replies; {name} needs token counts at the first and last streamed read.",
            "Use streamed logprobs or set request_options.stream_options.continuous_usage_stats: true on a compatible server.",
            "Giraffe uses streamed logprobs automatically when supported. Enable cumulative completion token usage alongside streamed content; final-only usage cannot establish generation pace (README: Measuring generation pace).")
    # Direct TPOT additionally needs a proven first event boundary and schema 1.
    if key == "tpot_direct":
        return Explanation("data_missing", "No proven first answer-token boundary was recorded for direct TPOT.",
                           "Rerun with streaming and token evidence; the TPOT estimate remains available when read-boundary counts are known.")
    return Explanation("data_missing", "No usable answer-read token interval was recorded.", "Rerun with streaming and token counts at the first and last answer reads.")


def response_latency(key, row, f):
    missing = sum(r.elapsed_ms is None or r.completed_ms is None for r in f.good)
    if missing:
        return Explanation("data_missing", f"Elapsed time or completion timestamps are missing on {missing}/{f.n_good} valid replies.", "Rerun with a current Giraffe version.")
    return Explanation("unknown", "Reply timestamps were recorded, but Giraffe produced no response-latency value.", "Open the request timing evidence for this workload.")


def first_output(key, row, f):
    if f.reasoning_seen:
        return Explanation("data_missing", f"{f.reasoning_count}/{f.n} requests produced only reasoning text and no answer before stopping.",
                           "Raise Output cap (--max-output-tokens) so the model can finish reasoning and answer." if any(
                               r.finish_reason == "length" and f.has_reasoning(r) for r in f.population) else
                           "Check the recorded finish reason and whether the endpoint returned a final answer.")
    return Explanation("data_missing", f"No request in this window received answer text ({f.n_failed} of {f.n} failed or timed out first).", "Fix the request errors first (see Outcomes).")


def client_ttft(key, row, f):
    reason, count = f.dominant(f.first_token_reasons)
    labels = {"No qualifying streamed generated-content event was observed.": "no qualifying generated-content event",
              "Non-streamed replies do not provide first-token arrival timing.": "non-streamed response",
              "Timing schema predates client content-event collection.": "legacy content-event evidence"}
    if reason in labels:
        return Explanation("data_missing", f"No timed streamed content event on {count}/{f.n} requests ({labels[reason]}).",
                           "Open the streamed content-event evidence for these requests.")
    return Explanation("data_missing", f"No first streamed content-event timestamp was recorded on {f.n}/{f.n} requests.",
                       "Open the streamed content-event evidence for these requests.")


def itl(key, row, f):
    from . import states
    evidence = f.token_evidence["logprobs"]
    if evidence.get(states.TOKEN_EVIDENCE_SUPPORTED):
        return Explanation("data_missing", f"Streamed logprobs were requested but {evidence[states.TOKEN_EVIDENCE_SUPPORTED]}/{f.n_good} replies lack separate single-token reads with matching answer usage.",
                           "The server or a proxy is batching tokens; test the endpoint directly. Mean TPOT is still reported when token counts are available.")
    if evidence.get(states.TOKEN_EVIDENCE_REJECTED):
        return Explanation("endpoint_unsupported", "The endpoint rejected logprobs, so chunk gaps cannot establish individual token arrival times.")
    if evidence.get(states.TOKEN_EVIDENCE_NOT_RETURNED):
        return Explanation("endpoint_unsupported", "The endpoint accepted logprobs but returned no token boundaries in the stream.")
    if evidence.get("shape_unverified"):
        return Explanation("not_applicable", "Automatic logprobs are not added to tool-call or structured-output requests, so token boundaries are not recorded.",
                           f"Run a plain-text Generation check: {f.add_check('generation')}." if not f.ran("generation") else "Choose a plain-text Generation window in the Measurement menu.")
    if evidence.get("disabled_by_request"):
        return Explanation("not_configured", "request_options disable logprobs, so token boundaries are not recorded.",
                           "Remove the explicit logprobs override from request_options to let Giraffe collect them.")
    return Explanation("data_missing", "No streamed token-evidence probe result was recorded for these requests.",
                       "Check the streamed token and usage support line in the run output and the request evidence.")


def reasoning(key, row, f):
    if not f.reasoning_seen and f.reasoning_unknown:
        return Explanation("data_missing", "Response text was not kept in this saved report, so Giraffe cannot tell whether the model emitted reasoning.",
                           "Rerun, or set Keep response text to All (--retention all) to see reasoning timing.")
    if not f.reasoning_seen:
        return Explanation("not_applicable", "The model returned no reasoning content in this window, so there is nothing to time. This is expected for models that do not emit reasoning."
                           if key == "first_reasoning" else "The model returned no reasoning content.")
    if key in {"reasoning_tokens", "reasoning_length"}:
        return Explanation("data_missing", f"The endpoint reports no usage.completion_tokens_details.reasoning_tokens on {f.reasoning_count}/{f.n} replies that contain reasoning.",
                           "Use an endpoint that reports usage.completion_tokens_details.reasoning_tokens.")
    if key == "reasoning_read_gap":
        return Explanation("too_few_samples", "Reasoning did not arrive in at least two separate network reads, so there is no reasoning-read interval.",
                           "Use a server reasoning parser that returns reasoning as a separate field." if f.configured_stream else "Enable streaming and a server reasoning parser that returns reasoning as a separate field.")
    return Explanation("data_missing", f"Reasoning appeared inline or in non-streamed replies on {f.reasoning_count}/{f.n} requests, so its first arrival has no timestamp.",
                       "Use a server reasoning parser that returns reasoning as a separate field." if f.configured_stream else "Enable streaming and a server reasoning parser that returns reasoning as a separate field.")


ARRIVAL_ACTION = "Enable Constant arrivals (load_test.patterns.constant.enabled) in Load test settings."
LATENCY_ACTION = "Set Maximum completion time per response (ms) (load_test.pass_criteria.max_completion_ms)."


def fixed_concurrency():
    return Explanation("not_applicable", "This Load test window holds a fixed concurrency, so requests have no scheduled arrival time.", ARRIVAL_ACTION)


def goodput(key, row, f):
    reason = row.get("reason") or "No scored and timing-qualified load-point evidence in this scope."
    if "not pooled" in reason:
        return Explanation("not_applicable", reason, "Choose a single load point in the Measurement menu.")
    if "unscored" in reason or (f.good and not f.answer_scored):
        gap = scoring_gap(f, "load")
        if gap:
            if "No first-answer or completion latency limit" in reason and len(gap.action or "") + len(LATENCY_ACTION) + 1 <= LIMITS["action"]:
                gap = replace(gap, action=(gap.action or "") + " " + LATENCY_ACTION)
            return gap
    if "No first-answer or completion latency limit" in reason:
        return Explanation("not_configured", reason, LATENCY_ACTION)
    return Explanation("data_missing", reason, "Open the load-point results and resolve the recorded timing or observation-coverage gaps.")


def load_owned(key, row, f):
    if (key == "offered_rate" and f.bursts_only) or (key == "highest_acceptable_rate" and (f.bursts_only or f.all_bursty)):
        return Explanation("not_applicable", "Bursts do not define a single steady arrival rate for this Load test window.",
                           "Choose a Constant arrivals or Poisson arrivals window without Bursts in the Measurement menu.")
    if key in {"offered_rate", "drops", "highest_acceptable_rate"} and f.held_concurrency:
        return fixed_concurrency()
    if key == "latency_attainment":
        if not f.latency_limits:
            return Explanation("not_configured", "No first-answer or completion latency limit is configured for this Load test window.", LATENCY_ACTION)
        return Explanation("data_missing", "This Load test window has latency limits but no per-response attainment evidence.",
                           "Open the load-point results and check the response timing qualification.")
    if key in {"decode_interference", "response_interference"}:
        if f.interference_enabled:
            return Explanation("not_applicable", "This window has no matched short/long interference comparison; enabling interference alone does not create a comparison here.",
                               "Choose an interference comparison in the Measurement menu.")
        label = "Short request during an active long answer" if f.interference_mode == "short_during_long" else "Long input during an active short stream"
        return Explanation("not_configured", "Interference is not enabled for this Load test window.",
                           f"Enable {label} (workloads.interference.enabled) in Load test settings.")
    if key == "sampled_concurrency":
        if f.arrival_pattern and "concurrent" not in f.patterns:
            return Explanation("not_applicable", "This Load test window schedules arrivals rather than holding sampled concurrency levels.",
                               "Enable Maintain concurrent requests (load_test.patterns.concurrent.enabled) to test held concurrency.")
        return Explanation("data_missing", "No intended-concurrency transitions were recorded for this Load test window.",
                           "Rerun with the current Giraffe to record the concurrency schedule.")
    if key == "workload_occupancy":
        return Explanation("data_missing", "Per-workload occupancy and share accounting was not recorded for this Load test window.",
                           "Open the load-point results and check workload delivery and occupancy accounting.")
    if key == "highest_acceptable_rate":
        if not f.latency_limits:
            return Explanation("not_configured", "No latency limit is configured to qualify the tested arrival rates.", LATENCY_ACTION)
        if not f.answer_scored:
            gap = scoring_gap(f, "load")
            if gap:
                return gap
        return Explanation("data_missing", "No tested arrival rate met the recorded acceptance criteria in this window.",
                           "Open the arrival-rate load-point results and resolve their failed acceptance criteria.")
    if key == "drops":
        return Explanation("data_missing", "The scheduled-arrival ledger was not recorded for this Load test window; local drops cannot be distinguished from unsent work.",
                           "Rerun with the current Giraffe to record scheduled, dispatched and unsent requests.")
    return Explanation("data_missing", "No intended arrival-rate transitions were recorded for this Load test window.",
                       "Rerun with the current Giraffe to record the arrival schedule.")


def owner(check):
    def rule(key, row, f):
        from .models import CHECK_NAMES
        name = CHECK_NAMES[check]
        if check == "load":
            return goodput(key, row, f) if key == "goodput" else load_owned(key, row, f)
        if check in {"correctness", "json", "tools"}:
            gap = scoring_gap(f, check)
            if gap:
                return gap
        return Explanation("data_missing", f"{name} has no {row['label'].lower()} evidence for this window.", f"Open the {name} results, resolve missing evidence, and rerun this workload.")
    rule.owner_check = check
    return rule


def arrival(key, row, f):
    if f.held_concurrency:
        return fixed_concurrency()
    if f.scheduled:
        return Explanation("data_missing", "Arrival timestamps were not recorded for these requests.", "Rerun with a current Giraffe version to record scheduled arrival and dispatch timing.")
    if f.load_window and f.arrival_pattern:
        return Explanation("data_missing", "This Load test window enabled scheduled arrivals but recorded no scheduled arrival timestamps.",
                           "Rerun with the current Giraffe and check the recorded arrival schedule.")
    if f.ran("load"):
        return Explanation("not_applicable", "This window has no scheduled arrival time, so arrival-relative delays do not exist.",
                           "Choose a Load test arrival-pattern window in the Measurement menu.")
    return Explanation("not_applicable", "Requests were sent serially or at a held concurrency, with no scheduled arrival time, so arrival-relative delays do not exist.",
                       f"For arrival-based timing add Load test with an arrival rate: {f.add_check('load')}.")

def cache(key, row, f):
    field_name = "usage.prompt_tokens_details.cached_tokens"
    if f.no_cached < f.n_good:
        return Explanation("data_missing" if f.missing_usage["usage.prompt_tokens"] else "too_few_samples",
                           f"{field_name} has no matching nonzero usage.prompt_tokens population.",
                           f"Return matching {field_name} and nonzero usage.prompt_tokens.")
    action = (f"Check for {field_name}. To test prefix caching add Prompt cache evidence: {f.add_check('prefix')}."
              if not f.ran("prefix") else None)
    return Explanation("endpoint_unsupported", f"The endpoint reported no cached-prompt token count ({field_name}) on {f.no_cached}/{f.n_good} replies.", action)


def read_gap(key, row, f):
    if not f.streamed:
        return nonstreamed(f)
    return Explanation("too_few_samples", "Answers did not arrive in at least two separate network reads, so there is no first-to-last interval.",
                       "Check the recorded answer-read counts; compare the endpoint directly if you suspect buffering.")


def baseline(key, row, f):
    if f.report.baseline is None:
        return Explanation("not_configured", "No baseline attached.", "Compare against a saved report: giraffe run --baseline REPORT (UI: pick a baseline).", state="no_baseline")
    target = f.window.get("target") or (f.cohort[0].target if f.cohort else None)
    entries = [entry for entry in f.report.baseline.get("targets", []) if entry.get("target") == target]
    reasons = [reason for entry in entries for reason in entry.get("reasons", []) if isinstance(reason, str)]
    if reasons:
        return Explanation("data_missing", ("Baseline comparison: " + " ".join(reasons))[:LIMITS["reason"]],
                           "Open the baseline comparison and resolve its recorded compatibility or sample gaps.")
    return Explanation("data_missing", "The baseline has no like-for-like qualified measurement for this workload.", "Rerun the baseline with the same checks and workload.")


def event_loop(key, row, f):
    threshold = f.report.observations.get("client_timing_health", {}).get("delay_floor_ms")
    text = f"the {threshold:g} ms recording threshold" if isinstance(threshold, (int, float)) else "the recording threshold"
    return Explanation("not_observed", f"No client scheduling delay above {text}; the load generator kept up.")


def telemetry(key, row, f):
    # Telemetry owns its copy and normally supplies cause before this pass.
    from . import states
    cause = {"not_collected": "collector_unavailable", "not_supported": "endpoint_unsupported",
             "not_observed": "not_observed", "not_applicable": "not_applicable",
             "insufficient_evidence": "too_few_samples"}.get(row["state"], "data_missing")
    status = row.get("details", {}).get("collection_status") or row["state"]
    if status == states.TELEMETRY_RUN_STOPPED and f.stop_affected:
        return Explanation("collector_unavailable", f.run_stop[1], run_stop_action(f.run_stop[0]))
    actions = {states.TELEMETRY_AUTHENTICATION_DENIED: "Check the exporter credentials (metrics_api_key_env or metrics_headers_env).",
               states.TELEMETRY_ENDPOINT_ABSENT: "Set targets[].metrics_url to the existing exporter route.",
               states.TELEMETRY_RESPONSE_LIMIT: "Reduce the exporter's response size or expose a smaller metrics endpoint."}
    return Explanation(cause, row.get("reason") or f"The exporter returned no {row['label'].lower()} samples.",
                       row.get("action") or (None if cause in NO_ACTION_OK else actions.get(status,
                       "Check the recorded exporter collection status and required metric families.")))


def unknown_rule(key, row, f):
    return Explanation("unknown", f"Giraffe recorded no {row['label']} values in this window ({row['sample_count']} of {row['eligible_count']} eligible).",
                       "Open the request evidence for this workload and check whether the endpoint returned the fields this measure reads.")


RULES = {
    "request_throughput": request_rate, "dispatch_rate": request_rate,
    "error_rate": lambda k, r, f: Explanation("too_few_samples", "No requests eligible for error accounting fell in this window.", "Pick a window with completed or failed requests, or rerun with more requests."),
    "peak_concurrency": request_rate, "average_concurrency": request_rate,
    "delayed_intervals": lambda k, r, f: Explanation("data_missing", "No client scheduling-health samples were recorded in this run.", "Rerun with a current Giraffe version to record client scheduling health."),
    "timing_quality": request_rate,
    "response_latency": response_latency, "first_useful_output": first_output,
    "client_ttft": client_ttft, "itl": itl,
    "first_reasoning": reasoning, "reasoning_tokens": reasoning, "reasoning_length": reasoning, "reasoning_read_gap": reasoning,
    "tpot": pace, "tpot_estimate": pace, "tpot_direct": pace, "generation_pace": pace,
    "answer_read_gap": read_gap, "longest_stream_pause": read_gap, "answer_streaming": read_gap,
    "completion_tail": lambda k, r, f: Explanation("data_missing", "No matching last-answer and completion timestamps were recorded.", "Rerun with a current Giraffe version; fix request errors if replies stopped before answer text."),
    "client_preparation": lambda k, r, f: Explanation("data_missing", "Client preparation timestamps were not recorded for these requests.", "Rerun with a current Giraffe version to record client preparation timing."),
    "configured_ceiling": lambda k, r, f: Explanation("not_configured", "No Simultaneous limit is set; Giraffe applied no cap on in-flight requests.", "Set Simultaneous limit (config key concurrency) to cap parallel requests."),
    "max_event_loop_delay": event_loop,
    "cached_tokens": cache, "cached_length": cache, "cached_share": cache,
    "knee": lambda k, r, f: Explanation("not_applicable", "Giraffe does not infer a saturation point; it reports tested rates and acceptance only.", "Compare the tested load stages in the Load test results."),
    "websocket_rtt": lambda k, r, f: Explanation("not_applicable", "Giraffe sends HTTP/SSE requests; there is no WebSocket round trip."),
    "persistent_user_delivery": lambda k, r, f: Explanation("data_missing", "Not every planned user has a known shared-window output rate.", "Raise Maximum requests so every planned user completes a request in the common window."),
}
for _key in ("input_tokens", "answer_tokens", "completion_tokens", "total_tokens", "input_throughput", "output_throughput", "completion_throughput", "total_throughput", "output_rate", "output_per_in_flight", "total_per_in_flight"):
    RULES[_key] = usage
for _key in ("client_wait", "dispatch_delay", "arrival_first_output", "arrival_completion"):
    RULES[_key] = arrival
for _check, _keys in {"load": ("drops", "offered_rate", "sampled_concurrency", "highest_acceptable_rate", "latency_attainment", "goodput", "workload_occupancy", "decode_interference", "response_interference"),
                      "sessions": ("turn_latency", "turn_generation", "conversation_completion", "user_fairness"),
                      "recovery": ("recovery_ratios",), "cancellation": ("cancel_followup",),
                      "json": ("structured_correctness",), "tools": ("tool_correctness",), "correctness": ("correctness",)}.items():
    for _key in _keys:
        RULES[_key] = owner(_check)
FAMILY_RULES = (("baseline_", baseline), ("spec_decode_", telemetry), ("mtplx_", telemetry),
                ("gpu_", telemetry), ("server_", telemetry), ("host_", telemetry))
for _key in ("kv_cache", "prefix_cache", "prefix_hit_rate", "preemptions", "throttling"):
    RULES[_key] = telemetry


# Dependencies describe evidence populations, not a naming convention. Scheduler
# rates, collectors and baseline caches do not require successful replies.
RATE_USAGE = {"input_throughput", "output_throughput", "completion_throughput", "total_throughput",
              "output_rate", "output_per_in_flight", "total_per_in_flight"}
REPLY_KEYS = {"response_latency", "first_useful_output", "first_reasoning", "reasoning_tokens", "reasoning_length",
              "reasoning_read_gap", "client_ttft", "itl", "tpot", "tpot_estimate", "tpot_direct", "generation_pace",
              "answer_read_gap", "longest_stream_pause", "answer_streaming", "completion_tail", "cached_tokens",
              "cached_length", "cached_share", "input_tokens", "input_length", "answer_tokens", "answer_length",
              "completion_tokens", "completion_length", "total_tokens", "correctness", "structured_correctness",
              "tool_correctness", "goodput", "latency_attainment", "turn_latency", "turn_generation",
              "conversation_completion", "user_fairness", "recovery_ratios", "cancel_followup",
              "highest_acceptable_rate", "persistent_user_delivery", "arrival_first_output", "arrival_completion"} | RATE_USAGE
TIMING_KEYS = {"first_reasoning", "client_ttft", "itl", "tpot", "tpot_estimate", "tpot_direct", "generation_pace",
               "answer_read_gap", "reasoning_read_gap", "longest_stream_pause", "answer_streaming", "completion_tail"}
STREAMING_KEYS = TIMING_KEYS - {"completion_tail"}
for _key, _function in tuple(RULES.items()):
    _dependencies = {name for name, keys in (("replies", REPLY_KEYS), ("timing", TIMING_KEYS),
                     ("streaming", STREAMING_KEYS), ("rate", RATE_USAGE)) if _key in keys}
    _fields = USAGE_FIELDS.get(_key, ("usage.completion_tokens",) if _function in {usage, pace} else
                               ("usage.completion_tokens_details.reasoning_tokens",) if _key in {"reasoning_tokens", "reasoning_length"} else ())
    if _fields:
        _dependencies.add("usage")
    if _key.startswith("reasoning") or _key in {"first_reasoning", "tpot", "tpot_estimate", "tpot_direct", "generation_pace", "output_throughput", "answer_tokens", "answer_length", "output_rate", "output_per_in_flight"}:
        _dependencies.add("reasoning")
    _owner = getattr(_function, "owner_check", None)
    _stop_sensitive = _key in REPLY_KEYS | {"request_throughput", "dispatch_rate", "error_rate", "peak_concurrency", "average_concurrency", "timing_quality"} or _owner is not None
    RULES[_key] = Rule(_function, frozenset(_dependencies), _fields, _owner, _stop_sensitive)
FAMILY_RULES = tuple((prefix, Rule(function)) for prefix, function in FAMILY_RULES)


def with_stop(explanation, rule, f):
    if (not explanation or not f.run_stop or not rule.stop_sensitive or not f.stop_affected
            or not stop_ends_phase(f.run_stop, explanation.phase)
            or explanation.cause not in {"too_few_samples", "data_missing"} or f.run_stop[1] in explanation.reason):
        return explanation
    reason = f"{f.run_stop[1]} {explanation.reason}"
    return replace(explanation, reason=reason[:LIMITS["reason"]], action=run_stop_action(f.run_stop[0]))


def explain_window(metrics, distributions, facts):
    """Apply shared preconditions, then specific causes; never alter measured rows."""
    common_rule = Rule(None, frozenset({"replies", "timing", "streaming"}), stop_sensitive=True)
    common = applicability(common_rule, facts) or window_gap(common_rule, facts) or precondition(common_rule, facts)
    common = with_stop(common, common_rule, facts)
    availability = ({name: getattr(common, name) for name in ("cause", "reason", "action")}
                    if common else None)
    for key, row in metrics.items():
        if row["value"] is not None:
            continue
        rule = RULES.get(key)
        if rule is None and key.endswith("_length"):
            rule = Rule(usage, frozenset({"replies", "usage"}), USAGE_FIELDS.get(key, ("usage.completion_tokens",)))
        if rule is None:
            rule = next((rule for prefix, rule in FAMILY_RULES if key.startswith(prefix)), Rule(unknown_rule))
        if row.get("cause") is None:
            explanation = applicability(rule, facts) or window_gap(rule, facts) or precondition(rule, facts) or rule.explain(key, row, facts)
            explanation = with_stop(explanation, rule, facts)
            state = explanation.state or {
                "not_applicable": "not_applicable", "endpoint_unsupported": "not_supported",
                "check_not_selected": "not_collected", "collector_unavailable": "not_collected",
                "not_configured": "not_collected", "data_missing": "insufficient_evidence",
                "too_few_samples": "insufficient_evidence", "not_observed": "not_observed",
            }.get(explanation.cause, row["state"])
            if ("usage" in rule.dependencies and "rate" in rule.dependencies
                    and explanation.cause == "data_missing" and facts.n_good and facts.duration
                    and any(facts.missing_usage.get(field) == facts.n_good for field in rule.usage_fields)):
                state = "not_measured"
            row.update(cause=explanation.cause, reason=explanation.reason, action=explanation.action,
                       detail=explanation.detail, state=state)
        if key in distributions and distributions[key]["value"] is None:
            distributions[key].update({field: row.get(field) for field in ("cause", "reason", "action", "detail", "state")})

    return availability
