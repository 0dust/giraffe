"""Speculative decoding evidence survives report export and stays server scoped."""
import json

import pytest

from giraffe.cli import load_report
from giraffe.reporting import write_report
from giraffe.report_stream import expanded_report_chunks
from giraffe.telemetry import derive_server_metrics
from tests.test_performance import record, report, summary


SPEC_VALUES = {
    "spec_decode_drafts": (1996, "drafts"),
    "spec_decode_draft_tokens": (5988, "tokens"),
    "spec_decode_accepted_tokens": (5042, "tokens"),
    "spec_decode_proposal_length": (3, "tokens/draft"),
    "spec_decode_acceptance_ratio": (5042 / 5988, "fraction"),
}


def saved_report(*, partial=False, unavailable=False):
    details = dict(observed_intervals=1, excluded_intervals=int(partial),
                   duration_seconds=10, first_elapsed_seconds=2, last_elapsed_seconds=12,
                   partial_coverage=partial, activity="observed",
                   attribution="Server-wide counters may include unrelated traffic.")
    derived = {**derive_server_metrics({}), **{key: dict(value=None if unavailable else value, unit=unit,
                        source="vLLM speculative decoding counter deltas", state="insufficient_evidence" if unavailable else "measured",
                        reason="Optional counters were not exposed; enabled state is unknown." if unavailable else None,
                        sample_count=1, eligible_count=2 if partial else 1, details=dict(details))
               for key, (value, unit) in SPEC_VALUES.items()}}
    derived["spec_decode_accepted_tokens"]["details"].update(
        accepted_tokens_by_position=[] if unavailable else [{"position": 0, "accepted_tokens": 1934,
                                                              "drafts": 1996, "acceptance_ratio": 1934 / 1996}],
        position_coverage=dict(details))
    return report([record()], observations={"targets": {"t": {"serving_telemetry": {"derived": derived}}}})


def test_export_preserves_named_server_rows_and_counter_scope(tmp_path):
    value = saved_report(partial=True)
    path, html_path = write_report(value, tmp_path, html=True)
    hydrated = load_report(path)
    rows = {row["id"]: row for row in summary(hydrated)["sections"]["server"]}
    for key, (expected, _) in SPEC_VALUES.items():
        row = rows[key]
        assert row["value"] == pytest.approx(expected)
        assert row["partial"] is True
        assert row["scope"] == "server-wide"
        assert row["details"]["selected_window_attribution"] == "not established"
        assert "1 / 2" in row["display_note"]
        assert "2–12 s" in row["display_note"]
        assert "10 s" in row["display_note"]
    exported = json.loads("".join(expanded_report_chunks(path)))
    assert exported["performance"] == hydrated.performance
    html = html_path.read_text()
    assert "Speculative decoding acceptance rate" in html
    assert "84.2" in html and "<small>%</small>" in html
    assert "Partial coverage" in html and "1 / 2" in html
    assert "2–12 s" in html and "selected request window" in html
    assert "Accepted speculative tokens by position" in html
    assert "zero-based" in html and "all drafts" in html


def test_unavailable_optional_counters_are_not_reported_as_disabled():
    rows = {row["id"]: row for row in summary(saved_report(unavailable=True))["sections"]["server"]}
    row = rows["spec_decode_acceptance_ratio"]
    assert row["value"] is None and row["state"] == "insufficient_evidence"
    assert "enabled state is unknown" in row["reason"]
    assert "spec_decode_accepted_by_position" not in rows


def test_raw_scrapes_feed_saved_report_and_refresh_older_derived_cache(tmp_path):
    from giraffe.models import RunConfig, Target
    from giraffe.telemetry import parse

    target = Target(name="t", model="model", url="http://localhost:8000/v1", metrics_profile="vllm-v1")
    config = RunConfig(targets=[target])
    def scrape(drafts, proposed, accepted, elapsed):
        body = (f'vllm:spec_decode_num_drafts_total{{engine="0"}} {drafts}\n'
                f'vllm:spec_decode_num_draft_tokens_total{{engine="0"}} {proposed}\n'
                f'vllm:spec_decode_num_accepted_tokens_total{{engine="0"}} {accepted}\n')
        return parse(body, target, config) | {"elapsed_seconds": elapsed}
    telemetry = {"snapshots": [scrape(20, 60, 50, 2), scrape(2016, 6048, 5092, 12)],
                 "derived": {key: row for key, row in derive_server_metrics({}).items()
                             if not key.startswith("spec_decode_")}}
    value = report([record()], observations={"targets": {"t": {"serving_telemetry": telemetry}}})
    path, _ = write_report(value, tmp_path)
    rows = summary(load_report(path))["metrics"]
    assert rows["spec_decode_drafts"]["value"] == 1996
    assert rows["spec_decode_acceptance_ratio"]["value"] == pytest.approx(5042 / 5988)
