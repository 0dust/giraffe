"""Bounded, private scoring evidence for saved-report availability explanations."""
from collections import Counter


JUDGE_ERRORS = frozenset({"timeout", "cancelled", "http_error", "request_failed", "truncated_response",
                         "invalid_completion", "invalid_verdict", "phase_deadline", "not_evaluated",
                         "client_initialization"})
SCORERS = frozenset({"none", "exact", "contains", "json", "tool", "judge", "label"})
def _score_message_code(message):
    from .fixtures import REASONING_WITHOUT_ANSWER, REFUSAL_SCORE_MESSAGE, TRUNCATED_ANSWER
    messages = {TRUNCATED_ANSWER: "truncated", REASONING_WITHOUT_ANSWER: "reasoning_only",
                      "AI judgment pending.": "pending", "Answer not scored: request did not complete validly.": "invalid_request",
                      "AI judge panel tied": "tied", "AI judge evaluation incomplete": "incomplete",
                      REFUSAL_SCORE_MESSAGE: "refused"}
    return messages.get(message)


def _bounded(facts):
    """Apply the same whitelist to transient aggregation facts and raw records."""
    errors = facts.get("judge_errors")
    errors = errors if isinstance(errors, list) else []
    votes = facts.get("judge_votes")
    return {"scorer": facts.get("scorer") if facts.get("scorer") in SCORERS else None,
            "expected": bool(facts.get("expected")),
            "judgement": facts.get("judgement") if facts.get("judgement") in {"pass", "fail", "inconclusive"} else None,
            "judge_errors": sorted({error if error in JUDGE_ERRORS else "other" for error in errors if isinstance(error, str)}),
            "judge_votes": max(0, min(votes, 8)) if isinstance(votes, int) else 0,
            "score_message_code": facts.get("score_message_code") if facts.get("score_message_code") in
                                  {"truncated", "reasoning_only", "pending", "invalid_request", "tied", "incomplete", "refused"} else None,
            "evaluation_error": facts.get("evaluation_error") if facts.get("evaluation_error") in
                                {"schema_recursion", "tool_evaluation_budget"} else None}


def scoring_facts(record) -> dict:
    """Keep categories only, never inputs, answer text, judge reasons or endpoints."""
    evaluation, judgement = record.evaluation, record.judgement
    cached = record.workload.get("_scoring")
    if evaluation is None and judgement is None and isinstance(cached, dict):
        return _bounded(cached)
    scorer = evaluation.scorer if evaluation else None
    return {"scorer": scorer if scorer in SCORERS else None,
            "expected": bool(evaluation and evaluation.expected_provided),
            "judgement": judgement.status if judgement else None,
            "judge_errors": sorted({vote.error_category if vote.error_category in JUDGE_ERRORS else "other"
                                    for vote in judgement.votes if vote.status == "error"}) if judgement else [],
            "judge_votes": len(judgement.votes) if judgement else 0,
            "score_message_code": _score_message_code(record.score_message),
            "evaluation_error": record.evaluation_error if record.evaluation_error in
                                {"schema_recursion", "tool_evaluation_budget"} else None}


def _cause(record, facts, config):
    from . import states
    if record.status != states.REQUEST_COMPLETED or not record.valid:
        return "invalid_reply", None
    if facts["evaluation_error"]:
        return "evaluation_error", facts["evaluation_error"]
    scorer, message = facts["scorer"], facts["score_message_code"]
    if scorer in (None, "none") or (not facts["expected"] and scorer in {"exact", "contains", "judge", "label"}):
        return "no_expected", None
    if message in {"truncated", "reasoning_only"}:
        return message, None
    if scorer == "judge":
        # An absent config in older/synthetic reports cannot negate recorded votes.
        if "judging" in config and not config["judging"].get("judges"):
            return "judge_not_configured", None
        if facts["judgement"] is None and message == "pending":
            return "judging_not_run", None
        if facts["judgement"] == "inconclusive":
            if facts["judge_votes"] == 0:
                return "judge_skipped", None
            if message == "tied":
                return "judge_tied", None
            if facts["judge_errors"]:
                return "judge_error", facts["judge_errors"][0]
            return "judge_inconclusive", None
    return "unscored_other", None


def scoring_gap(f, check):
    """Explain the dominant recorded reason valid replies have no score."""
    from .availability import Explanation

    facts = getattr(f, "scoring", None)
    if facts is None:
        facts = [scoring_facts(r) for r in f.good]
    relevant = [(r, fact) for r, fact in zip(f.good, facts)
                if check not in {"json", "tools"} or check in r.check_ids]
    pairs = [(r, fact) for r, fact in relevant if r.score is None]
    if not pairs:
        if check in {"json", "tools"}:
            name = {"json": "JSON correctness", "tools": "Tool-call correctness"}[check]
            return Explanation("data_missing", f"{name} has no scored replies in this window.",
                               f"Open the {name} results and fix the failing requests, then rerun.")
        return None
    classified = [(fact, _cause(r, fact, f.config)) for r, fact in pairs]
    cause, count = Counter(item[0] for _, item in classified).most_common(1)[0]
    categories = Counter(category for fact, (kind, category) in classified if kind == cause
                         for category in (fact["judge_errors"] if cause == "judge_error" else [category])
                         if category is not None)
    category = categories.most_common(1)[0][0] if categories else None
    n = f"{count}/{len(relevant)}"
    from .run_evidence import stop_ends_phase, run_stop_action
    stop = getattr(f, "run_stop", None)
    stop = stop if stop_ends_phase(stop, "judging") else None
    pending_reason = (f"{n} replies were never judged: {stop[1]}" if stop else
                      f"{n} replies were never judged: judging was pending or cancelled when the run ended.")
    pending_action = "Rerun judging; check judging.max_duration_seconds allows the judge phase to finish."
    if stop:
        pending_action = run_stop_action(stop[0])
    elif getattr(f, "report", None) and f.report.observations.get("judging", {}).get("stop_reason") == "phase_deadline":
        pending_reason = f"{n} replies were never judged: the judge phase reached its duration limit."
        pending_action = "Raise judging.max_duration_seconds, then rerun."
    table = {
        "no_expected": ("not_configured", f"{n} valid replies have no expected answer or scorer, so they could not be scored.",
                        "Add expected answers with a request pack (--request-pack PATH) or custom fixtures (--fixtures), then rerun Answer correctness."),
        "judge_not_configured": ("not_configured", f"{n} replies need an AI judge, but no judge endpoint is configured.",
                                 "Add a judge (judging.judges: name, url, model) and rerun."),
        "judge_inconclusive": ("data_missing", f"{n} replies were judged but the AI judge panel was inconclusive; the expected answer was recorded, so none of them has a verdict.",
                               "Open the judge votes in the request evidence; raise judging.max_output_tokens or add a judge (judging.judges)."),
        "judge_skipped": ("data_missing", f"{n} replies have no verdict because the AI judge panel recorded no votes.",
                           "Open the judge votes in the request evidence and rerun judging."),
        "judge_tied": ("data_missing", f"{n} replies got a tied AI judge panel, so no verdict was recorded.",
                       "Add an odd number of judges (judging.judges) and rerun."),
        "judging_not_run": ("data_missing", pending_reason, pending_action),
        "truncated": ("data_missing", f"{n} replies were cut off by the output limit before a complete answer, so they could not be scored.",
                      "Raise Output cap (--max-output-tokens) and rerun."),
        "reasoning_only": ("data_missing", f"{n} replies contain only reasoning and no final answer, so they could not be scored.",
                           "Raise Output cap (--max-output-tokens) so the model can finish reasoning and answer."),
        "evaluation_error": ("data_missing", f"{n} replies were not scored because the scorer hit its evaluation limit ({category}); this is not a wrong answer.",
                             "Simplify the JSON schema or tool expectation and rerun."),
        "invalid_reply": ("data_missing", f"{n} replies were invalid, so they could not be scored.", "Fix the failing requests first (see Outcomes)."),
        "unscored_other": ("unknown", f"Giraffe recorded no score for {n} valid replies and no scorer reason.", "Open the request evidence for this workload.")}
    if cause == "judge_error":
        labels = {"timeout": "timed out", "cancelled": "cancelled", "truncated_response": "truncated",
                  "invalid_completion": "invalid verdict", "invalid_verdict": "invalid verdict",
                  "phase_deadline": "not run", "not_evaluated": "not run"}
        label = labels.get(category, "endpoint error")
        actions = {"timeout": "Raise judging.request_timeout_seconds or judging.max_duration_seconds, then rerun.",
                   "truncated_response": "Raise judging.max_output_tokens, then rerun.",
                   "invalid_completion": "Use a judge model that answers with the required verdict JSON (judging.judges).",
                   "invalid_verdict": "Use a judge model that answers with the required verdict JSON (judging.judges).",
                   "phase_deadline": "Raise judging.max_duration_seconds, then rerun.",
                   "not_evaluated": "Raise judging.max_duration_seconds, then rerun.", "cancelled": pending_action}
        result = ("data_missing", f"{n} replies have no verdict because the judge call failed ({label}).",
                  actions.get(category, "Check the judge endpoint and credentials (judging.judges), then rerun."))
    else:
        result = table[cause]
    return Explanation(*result, phase="judging")
