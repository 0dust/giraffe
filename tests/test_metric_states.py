"""Metric evidence vocabulary and saved-report envelope contracts."""
import json
import pytest

from giraffe import performance, performance_html, states, telemetry
from tests.performance_fixture import synthetic_report_1000
from tests.test_performance import record, report
from tests.test_performance_telemetry import counters, snapshot


def metric(value=None, **kwargs):
    return performance.metric(value, label="Test", unit="ms", source="test", target="t", window="full", **kwargs)


def metric_rows(value):
    if isinstance(value, dict):
        if {"value", "state", "reason", "action"} <= value.keys():
            yield value
        for child in value.values():
            yield from metric_rows(child)
    elif isinstance(value, list):
        for child in value:
            yield from metric_rows(child)


def assert_envelopes(value):
    rows = list(metric_rows(value))
    assert rows
    for row in rows:
        assert row["state"] in states.METRIC_STATES, row
        assert (row["value"] is not None) == (row["state"] in states.METRIC_VALUE_STATES), row


def test_vocabulary_and_description():
    assert states.METRIC_STATES == ("measured", "estimated", "not_measured", "not_observed", "not_collected",
                                    "not_applicable", "insufficient_evidence", "not_supported", "no_baseline")
    assert states.METRIC_VALUE_STATES == {"measured", "estimated"}
    assert set(states.METRIC_STATE_LABELS) == set(states.METRIC_STATES)
    assert set(states.METRIC_STATE_ACTIONS) == set(states.METRIC_STATES) - states.METRIC_VALUE_STATES
    description = states.describe()
    assert description["metric_states"] == list(states.METRIC_STATES)
    assert description["metric_value_states"] == ["measured", "estimated"]
    assert description["metric_state_labels"] == states.METRIC_STATE_LABELS
    assert description["metric_state_actions"] == states.METRIC_STATE_ACTIONS


@pytest.mark.parametrize("value", [0, False, {}, [], 1.5])
def test_value_presence_is_not_truthiness(value):
    assert metric(value)["state"] == "measured"
    assert_envelopes(metric(value))


def test_metric_defaults_and_downgrade():
    assert metric()["state"] == "not_measured"
    assert metric()["reason"] == "No eligible observations in this scope."
    for state in ("measured", "estimated"):
        assert metric(state=state)["state"] == "insufficient_evidence"
        assert metric(float("nan"), state=state)["state"] == "insufficient_evidence"
    for state, action in states.METRIC_STATE_ACTIONS.items():
        assert metric(state=state)["action"] == action
    assert metric(state="no_baseline")["action"] == "Attach a baseline with comparable workload and measurement coverage."
    assert metric(state="not_applicable")["action"] == "Select the corresponding workload or transport to collect this measure."
    assert metric(state="not_collected")["action"] == "Enable the corresponding collector and rerun the workload."
    assert metric(state="not_observed")["action"] == "Collect eligible observations for this scope."
    assert metric(reason="Specific reason", action="Specific action")["reason"] == "Specific reason"
    assert metric(action="Specific action")["action"] == "Specific action"


@pytest.mark.parametrize("state,value", [("unknown", None), ("measured", None), ("estimated", None),
                                         ("not_collected", 0), ("not_applicable", {})])
def test_strict_guard(state, value, monkeypatch):
    monkeypatch.setattr(states, "STRICT_TRANSITIONS", True)
    with pytest.raises(AssertionError, match="metric"):
        states.check_metric_state(state, value)


def test_production_guard_warns(monkeypatch, caplog):
    monkeypatch.setattr(states, "STRICT_TRANSITIONS", False)
    states.check_metric_state("not_measured", 0)
    assert "metric" in caplog.text


def test_constructors_guard_invalid_rows():
    with pytest.raises(AssertionError, match="metric"):
        metric(1, state="not_collected")
    with pytest.raises(AssertionError, match="metric"):
        metric(state="unknown")
    with pytest.raises(AssertionError, match="metric"):
        telemetry._derived(None, "ms", "test", state="measured")
    with pytest.raises(AssertionError, match="metric"):
        telemetry._derived(0, "ms", "test", state="not_observed")
    for state in states.METRIC_STATES:
        row = telemetry._derived(0 if state in states.METRIC_VALUE_STATES else None, "ms", "test", state=state)
        assert_envelopes(row)
        assert row["action"] == (None if state in states.METRIC_VALUE_STATES else
                                 "Collect at least two fresh compatible exporter samples during the workload.")
        assert row["counts"] == {"sample": 0, "eligible": 0, "missing": 0}
    missing = telemetry._derived(None, "ms", "test")
    assert missing["reason"] == "No fresh compatible observations or nonzero measurement population."
    assert missing["action"] == "Collect at least two fresh compatible exporter samples during the workload."
    assert telemetry._derived(None, "ms", "test", action="Specific action")["action"] == "Specific action"


def test_representative_report_sweep():
    from tests.test_multi_user_reporting import user_report

    samples = [snapshot(counters("a", 1, 1), 0), snapshot(counters("a", 3, 2), 2)]
    for data in ({}, {"snapshots": samples}):
        assert_envelopes(telemetry.derive_server_metrics(data))
    reports = [report([]), report([record()]), synthetic_report_1000()]
    reports.extend([user_report(), user_report(missing=True)])
    reports.append(report([record()], observations={"targets": {"t": {"serving_telemetry": {"snapshots": samples}}}}))
    for saved in reports:
        output = performance.build_performance(saved)
        assert_envelopes(output)
        for target in output["targets"]:
            for window in target["windows"]:
                assert_envelopes(performance.build_performance(saved, target=target["target"], window=window["id"]))


def test_html_labels_use_shared_table(monkeypatch):
    assert performance_html.METRIC_STATE_LABELS is states.METRIC_STATE_LABELS
    for state, label in states.METRIC_STATE_LABELS.items():
        assert label in performance_html._availability({"value": None, "state": state})
    monkeypatch.setitem(states.METRIC_STATE_LABELS, "not_measured", "Shared label")
    assert "Shared label" in performance_html._availability(metric())
    assert "Future state" in performance_html._availability({"state": "future_state"})
    assert "&lt;script&gt;" in performance_html._availability({"state": "<script>"})


@pytest.mark.parametrize("state", [states.METRIC_MEASURED, states.METRIC_ESTIMATED])
def test_html_missing_value_with_foreign_value_state_is_not_measured(state):
    row = metric()
    row["state"] = state
    assert "<strong>Not measured</strong>" in performance_html._card(row)
    assert '<td colspan="7">Not measured' in performance_html._distributions([row], "test")
    assert row["state"] == state and row["value"] is None


def test_ui_labels_read_bootstrap_vocabulary():
    from tests.test_ux_results import run_render_checks

    run_render_checks("const labels=" + json.dumps(states.METRIC_STATE_LABELS) + r''';
for(const [value,label] of Object.entries(labels))assert.equal(metricStateLabel(value),label);
assert.equal(metricStateLabel('future_state'),human('future_state'));
state.bootstrap.states.metric_state_labels.not_measured='Shared label';
assert.equal(metricStateLabel('not_measured'),'Shared label');
''')


def test_cli_availability_uses_shared_labels(monkeypatch):
    saved = report([record()])
    monkeypatch.setitem(states.METRIC_STATE_LABELS, "not_measured", "Shared label")
    assert "shared label" in "\n".join(performance.cli_summary(saved))


def test_cli_lowercases_shared_labels_and_unknown_codes(monkeypatch):
    saved = report([record()])
    saved.performance = performance.build_performance(saved)
    target = saved.performance["targets"][0]
    rows = target["summaries"][target["default_window"]]["metrics"]
    rows["response_latency"].update(value=None, state=states.METRIC_INSUFFICIENT_EVIDENCE)
    rows["client_ttft"].update(value=None, state="FUTURE_EVIDENCE")
    rows["generation_pace"].update(value=12, state=states.METRIC_ESTIMATED)
    text = "\n".join(performance.cli_summary(saved))
    assert rows["response_latency"]["label"] + ": insufficient evidence" in text
    assert rows["client_ttft"]["label"] + ": future evidence" in text
    assert " (estimated)" in text
    monkeypatch.setitem(states.METRIC_STATE_LABELS, states.METRIC_ESTIMATED, "Shared estimate label")
    assert " (shared estimate label)" in "\n".join(performance.cli_summary(saved))


def test_all_states_enforce_value_presence():
    for state in states.METRIC_STATES:
        has_value = state in states.METRIC_VALUE_STATES
        states.check_metric_state(state, 0 if has_value else None)
        with pytest.raises(AssertionError, match="metric"):
            states.check_metric_state(state, None if has_value else 0)


def test_metric_description_reaches_public_capabilities():
    from giraffe.capabilities import capabilities

    assert capabilities()["states"]["metric_state_labels"] == states.METRIC_STATE_LABELS
