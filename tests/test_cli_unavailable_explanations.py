"""CLI availability guidance through the actual default run and HTTP client."""
from giraffe.cli import main
from giraffe.models import RunReport
from tests.fake_endpoint import FakeEndpoint


def test_default_run_without_usage_explains_cli_kpis(tmp_path, capsys):
    with FakeEndpoint(omit_usage=True) as endpoint:
        result = main([
            "run", "--url", endpoint.url, "--model", "fake-model",
            "--max-requests", "50", "--max-duration", "30",
            "--output", str(tmp_path / "run"),
        ])
    output = capsys.readouterr().out
    assert result in {0, 1, 2}
    saved = RunReport.model_validate_json((tmp_path / "run" / "report.json").read_text())
    target = saved.performance["targets"][0]
    metrics = target["summaries"][target["default_window"]]["metrics"]
    row = metrics["output_throughput"]
    assert row["cause"] == "data_missing"
    assert "no token counts" in row["reason"]
    assert metrics["request_throughput"]["value"] is not None
    assert f"Output token throughput: {row['state'].replace('_', ' ')} - {row['reason']}" in output
    assert "Next:" in output
    assert "--checks access,serving,correctness,first_output,load" in output
    assert "Availability:" not in output


def test_all_failed_kpis_collapse_shared_precondition_first():
    from giraffe.performance import KPI_IDS, cli_summary, refresh_performance
    from tests.test_reporting import dead_endpoint_report

    report = refresh_performance(dead_endpoint_report())
    before = report.model_dump_json()
    target = report.performance['targets'][0]
    summary = target['summaries'][target['default_window']]
    common = summary['availability']
    rows = [summary['metrics'][key] for key in KPI_IDS]
    matching = [row for row in rows if row['value'] is None
                and all(row.get(field) == common[field] for field in common)]
    lines = cli_summary(report)
    unavailable = lines[lines.index('  Not available:') + 1:]
    state = matching[0]['state'].replace('_', ' ')
    expected = '    ' + ', '.join(row['label'] for row in matching) + f": {state} - {common['reason']} Next: {common['action']}"
    assert unavailable[0] == expected
    assert sum(common['reason'] in line for line in lines) == 1
    for row in rows:
        if row['value'] is None and row not in matching:
            assert f"    {row['label']}: {row['state'].replace('_', ' ')} - {row['reason']}" in '\n'.join(unavailable)
    assert report.model_dump_json() == before


def test_no_usage_kpis_without_shared_precondition_keep_individual_lines():
    from giraffe.performance import KPI_IDS, cli_summary
    from tests.test_unavailable_explanations import issue_report

    report = issue_report()
    target = report.performance['targets'][0]
    summary = target['summaries'][target['default_window']]
    assert summary['availability'] is None
    lines = cli_summary(report)
    for key in KPI_IDS:
        row = summary['metrics'][key]
        if row['value'] is None:
            expected = f"    {row['label']}: {row['state'].replace('_', ' ')} - {row['reason']}" + (f" Next: {row['action']}" if row.get('action') else '')
            assert expected in lines


def test_collapsed_kpis_with_different_states_say_unavailable():
    from giraffe.performance import KPI_IDS, cli_summary, refresh_performance
    from tests.test_reporting import dead_endpoint_report

    report = refresh_performance(dead_endpoint_report())
    target = report.performance['targets'][0]
    summary = target['summaries'][target['default_window']]
    common = summary['availability']
    row = next(summary['metrics'][key] for key in KPI_IDS
               if summary['metrics'][key]['value'] is None
               and all(summary['metrics'][key].get(field) == common[field] for field in common))
    row['state'] = 'not_measured'
    lines = cli_summary(report)
    assert ': unavailable - ' + common['reason'] in lines[lines.index('  Not available:') + 1]


def test_unmatched_window_precondition_keeps_cli_lines_unchanged():
    from giraffe.performance import cli_summary
    from tests.test_unavailable_explanations import issue_report

    report = issue_report()
    before = cli_summary(report)
    target = report.performance['targets'][0]
    summary = target['summaries'][target['default_window']]
    summary['availability'] = {'cause': 'too_few_samples', 'reason': 'An unrelated precondition.', 'action': 'Different advice.'}
    assert cli_summary(report) == before
