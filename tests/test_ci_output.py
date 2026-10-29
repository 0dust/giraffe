"""CI exports preserve verdicts without exposing endpoint credentials."""
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from giraffe.ci_output import write_junit, write_summary_md
from giraffe.models import CheckResult, RunReport


def report() -> RunReport:
    return RunReport(
        run_id="run-123", started_at="2026-10-01T00:00:00Z", finished_at="2026-10-01T00:00:01Z",
        overall="inconclusive", manifest={"config": {"checks": ["access", "json", "tools", "gpu"],
            "targets": [{"name": "local", "model": "test-model", "url":
                "https://user:p'ass@[::1]:8000/v1?api_key=SECRET#TOKEN"}]}},
        checks=[CheckResult(id=check, target="local", title=title, status=status, summary=reason,
                            required=check != "gpu") for check, title, status, reason in [
            ("access", "Endpoint access", "pass", "Connected"),
            ("json", "JSON format", "fail", "Invalid <JSON> & syntax"),
            ("tools", "Tool calling", "inconclusive", "No comparable samples"),
            ("gpu", "GPU metrics", "skipped", "Metrics unavailable"),
            ("context", "Context", "skipped", "Not selected."),
        ]],
    )


def test_junit_schema_counts_identity_reasons_and_url_redaction(tmp_path):
    path = tmp_path / "nested" / "junit.xml"
    source = report()
    before = source.model_dump_json()
    write_junit(source, path)
    root = ET.parse(path).getroot()
    assert root.tag == "testsuites"
    suite, = root.findall("testsuite")
    assert suite.attrib.items() >= {("tests", "4"), ("failures", "1"), ("skipped", "2")}
    cases = suite.findall("testcase")
    assert [(case.get("name"), case.get("classname")) for case in cases] == [
        ("access", "local"), ("json", "local"), ("tools", "local"), ("gpu", "local")]
    assert cases[1].find("failure").get("message") == "Invalid <JSON> & syntax"
    assert cases[2].find("skipped").text == "No comparable samples"
    properties = {p.get("name"): p.get("value") for p in suite.findall("properties/property")}
    assert cases[0].find("system-out").text == "Title: Endpoint access"
    assert properties == {"model": "test-model", "url":
                          "https://[::1]:8000/v1[redacted]", "run_id": "run-123"}
    assert all(secret not in path.read_text() for secret in ("user", "p'ass", "SECRET", "TOKEN"))
    assert source.model_dump_json() == before


@pytest.mark.parametrize("status,element", [("blocked", "error"), ("inconclusive", "skipped"),
                                             ("skipped", "skipped"), ("fail", "failure"), ("pass", None)])
def test_junit_status_mapping_and_legacy_selection(tmp_path, status, element):
    source = report()
    source.manifest = {}
    source.checks = [CheckResult(id="access", target="old-target", title="Endpoint access",
                               status=status, summary="Reason\x00with control")]
    path = tmp_path / "junit.xml"
    write_junit(source, path)
    case, = ET.parse(path).getroot().findall("testsuite/testcase")
    assert (case.find(element) is not None) if element else not any(
        case.find(tag) is not None for tag in ("failure", "skipped"))
    assert "\x00" not in path.read_text()


def test_baseline_regression_fails_its_selected_check_without_extra_testcase(tmp_path):
    source = report()
    source.overall = "fail"
    source.checks[1].status = "pass"
    source.checks.append(CheckResult(id="baseline", target="local", title="Baseline comparison",
                                    status="fail", summary="One material regression"))
    source.baseline = {"run_id": "before", "status": "fail", "targets": [{"target": "local",
        "status": "fail", "comparisons": [{"check": "access", "metric": "error_rate",
        "status": "fail", "current": .1, "baseline": 0, "worsening": 10,
        "change_unit": "percentage_points"}]}]}
    path = tmp_path / "junit.xml"
    write_junit(source, path)
    cases = ET.parse(path).getroot().findall("testsuite/testcase")
    assert len(cases) == 4
    assert "baseline" in cases[0].find("failure").get("message").lower()
    assert cases[1].find("failure") is None
    summary = tmp_path / "summary.md"
    write_summary_md(source, summary, Path("runs/run-123/report.html"))
    text = summary.read_text()
    assert "**FAIL**" in text
    assert "Baseline delta" in text and "before" in text and "10" in text
    assert "percentage points" in text
    assert "runs/run-123/report.html" in text
    assert "Not selected" not in text


@pytest.mark.parametrize('status,element', [('blocked', 'error'), ('fail', 'failure')])
def test_inconclusive_baseline_preserves_current_error_or_failure(tmp_path, status, element):
    from giraffe.reporting import compare_baseline
    from tests.test_reporting import make_report

    current, baseline = make_report(count=1), make_report('before', count=1)
    for source in (current, baseline):
        source.manifest['config']['checks'] = ['correctness']
    current.overall = current.checks[0].status = status
    current.checks[0].summary = 'Current check could not complete' if status == 'blocked' else 'Wrong answer'
    compared = compare_baseline(current, baseline)
    comparison = next(check for check in compared.checks if check.id == 'baseline')
    assert compared.overall == status and comparison.status == 'inconclusive'
    junit, summary = tmp_path / 'junit.xml', tmp_path / 'summary.md'
    write_junit(compared, junit)
    root = ET.parse(junit).getroot()
    assert root.attrib == {'tests': '1', 'failures': str(int(status == 'fail')),
                           'errors': str(int(status == 'blocked'))}
    case, = root.findall('testsuite/testcase')
    assert case.find('skipped') is None
    reason = current.checks[0].summary + ' Baseline comparison: ' + comparison.summary
    assert case.find(element).get('message') == reason
    write_summary_md(compared, summary, tmp_path / 'report.json')
    assert f'| local | correctness | {status} | {reason} |' in summary.read_text()


def test_legacy_baseline_failure_and_markdown_escaping(tmp_path):
    source = report()
    source.checks[0].summary = "Connected | <script>\nnext"
    source.checks.append(CheckResult(id="baseline", target="local", title="Baseline comparison",
                                    status="fail", summary="Legacy baseline regression"))
    source.baseline = {"run_id": "before", "status": "fail"}
    path = tmp_path / "junit.xml"
    write_junit(source, path)
    case = ET.parse(path).getroot().find("testsuite/testcase")
    assert "Legacy baseline regression" in case.find("failure").get("message")
    summary = tmp_path / "nested" / "summary.md"
    write_summary_md(source, summary, Path("runs/report.json"))
    text = summary.read_text()
    assert "\\|" in text and "<script>" in text
    assert "&lt;" not in text and "&gt;" not in text
    assert "SECRET" not in text


def test_junit_common_xsd_shape(tmp_path):
    # Jenkins' junit-4.xsd: suite properties precede cases; testcase children
    # are skipped/error/failure/system-out/system-err, in that order.
    source = report()
    path = tmp_path / 'junit.xml'
    write_junit(source, path)
    root = ET.parse(path).getroot()
    assert set(root.attrib) <= {'name', 'time', 'tests', 'failures', 'disabled', 'errors'}
    for suite in root:
        assert suite.tag == 'testsuite' and {'name', 'tests'} <= suite.attrib.keys()
        assert suite[0].tag == 'properties'
        assert len(suite.findall('properties')) == 1
        for case in suite.findall('testcase'):
            assert {'name', 'classname'} <= case.attrib.keys()
            assert set(case.attrib) <= {'name', 'classname', 'assertions', 'time', 'status'}
            order = {'skipped': 0, 'error': 1, 'failure': 2, 'system-out': 3, 'system-err': 4}
            assert [order[c.tag] for c in case] == sorted(order[c.tag] for c in case)
            assert case.find('properties') is None
            for skipped in case.findall('skipped'):
                assert not skipped.attrib and skipped.text


def test_junit_target_properties_are_unambiguous(tmp_path):
    source = report()
    source.manifest['config']['targets'].append({'name': 'other', 'model': 'second', 'url': 'http://other/v1'})
    source.checks.append(CheckResult(id='access', target='other', title='Endpoint access', status='pass', summary='Connected'))
    path = tmp_path / 'junit.xml'
    write_junit(source, path)
    suites = ET.parse(path).findall('testsuite')
    assert len(suites) == 2
    for suite in suites:
        properties = {p.get('name'): p.get('value') for p in suite.findall('properties/property')}
        cases = suite.findall('testcase')
        assert len({case.get('classname') for case in cases}) == 1
        if cases[0].get('classname') == 'other':
            assert properties['model'] == 'second' and properties['url'] == 'http://other/v1'


def test_blocked_saved_report_without_results_covers_selected_checks(tmp_path):
    source = report()
    source.overall, source.abort_reason, source.checks = 'blocked', 'Endpoint could not be reached', []
    path = tmp_path / 'blocked.xml'
    write_junit(source, path)
    cases = ET.parse(path).findall('.//testcase')
    assert [case.get('name') for case in cases] == ['access', 'json', 'tools', 'gpu']
    assert all(case.find('error').get('message') == source.abort_reason for case in cases)


def test_markdown_summary_preserves_comparisons_and_escapes_by_context(tmp_path):
    source = report()
    source.run_id = 'run|one'
    source.checks[0].summary = '# correctness 0.0% < allowed 100.0% > reference & stable | `limit`'
    source.abort_reason = '- partial < > & | `reason`'
    path = tmp_path / 'summary.md'
    write_summary_md(source, path, Path('runs/a|b/report.json'))
    text = path.read_text()
    assert r'| local | access | pass | \# correctness 0.0% < allowed 100.0% > reference & stable \| \`limit\` |' in text
    assert 'Run: run|one' in text
    assert r'Reason: \- partial < > & | \`reason\`' in text
    assert 'Report: runs/a|b/report.json' in text
    assert all(entity not in text for entity in ('&lt;', '&gt;', '&amp;'))


@pytest.mark.parametrize('reason,escaped', [
    ('> quoted', r'\> quoted'), ('* item', r'\* item'),
    ('1. item', r'1\. item'), ('``code``', r'\`\`code\`\`'),
])
def test_markdown_summary_escapes_leading_controls(tmp_path, reason, escaped):
    source = report()
    source.checks[0].summary = reason
    path = tmp_path / 'summary.md'
    write_summary_md(source, path, None)
    assert f'| local | access | pass | {escaped} |' in path.read_text()
