"""Token evidence contracts, conditional probability comparisons, and complete HTTP runs."""

import copy
import json
from itertools import combinations

import pytest
from pydantic import ValidationError

from giraffe.logprobs import collect_logprobs, compare_samples, comparison_status
from giraffe.logprob_workload import summarize_logprobs
from giraffe.models import Consistency, LogprobConsistency, RequestRecord
from giraffe.planning import request_plan
from giraffe.reporting import _settings_reasons, write_report
from giraffe.runner import run_suite
from tests.fake_endpoint import FakeEndpoint
from tests.test_workloads import check, config
from tests.test_ux_catalogue import browser_logic


def token(text='a', probability=-.05, alternatives=None, **extra):
    return {'token': text, 'bytes': list(text.encode()), 'logprob': probability,
            'top_logprobs': alternatives or [], **extra}


def evidence(tokens=None, top_k=2):
    tokens = tokens or [token()]
    return collect_logprobs({'content': tokens}, ''.join(t['token'] for t in tokens), top_k)


def record(name, tokens=None, *, level=2, slot=0, repeat=0, **extra):
    return RequestRecord(id=name, target='local', fixture_id=f'fixture{slot}', scenario=f'consistency_c{level}_short',
        check_ids=['consistency'], started_at='2026-10-17T00:00:00Z', status='completed', valid=True,
        finish_reason='stop', overlap=level, request_hash='same', observed_model='m', backend_id='b',
        workload={'level': level, 'corpus_index': slot, 'round': repeat, 'phase': 'measured', 'input_bucket': 'short', 'observed_overlap': level, 'load_qualified': True},
        logprob_evidence=evidence(tokens, 0), **extra)


def settings(**options):
    return Consistency(repetitions=3, levels=[1, 2], shapes=['short'], logprobs=options)


def run_config(endpoint, *, options=None, repetitions=3, **extra):
    return config(endpoint, checks=['consistency'], consistency={
        'repetitions': repetitions, 'levels': [1, 2], 'shapes': ['short'], 'seed': 17,
        'logprobs': {'top_logprobs': 2, **(options or {})}}, **extra)


@pytest.mark.parametrize('options', [{'strict': True}, {'tolerance': -1}, {'tolerance': float('nan')},
    {'tolerance': float('inf')}, {'top_logprobs': -1}, {'top_logprobs': 21}, {'response_mode': 'stream'},
    {'priming_rounds': 3}, {'unknown': 1}])
def test_invalid_settings_rejected(options):
    with pytest.raises(ValidationError):
        LogprobConsistency(**options)


def test_tolerance_never_implicitly_enables_strict_mode():
    assert not LogprobConsistency(tolerance=0).strict
    assert LogprobConsistency(strict=True, tolerance=0).strict


@pytest.mark.parametrize('raw,status', [(None, 'missing'), ({'content': None}, 'missing'),
    ({'content': []}, 'missing'), ([], 'invalid'), ({'content': {}}, 'invalid')])
def test_missing_and_invalid_collections(raw, status):
    assert collect_logprobs(raw, 'a', 2).status == status


@pytest.mark.parametrize('change', [{'logprob': float('nan')}, {'logprob': float('inf')},
    {'logprob': .1}, {'logprob': True}, {'logprob': None}, {'logprob': '-.1'},
    {'bytes': [256]}, {'bytes': [True]}, {'bytes': []}, {'token': 1}, {'token_id': -1},
    {'token_id': True}, {'top_logprobs': None}, {'top_logprobs': [token(), token()]},
    {'top_logprobs': [token(probability=-.1)]}])
def test_invalid_token_evidence_is_not_transport_or_probability_success(change):
    assert collect_logprobs({'content': [{**token(), **change}]}, 'a', 2).status == 'invalid'


def test_nullable_and_split_utf8_token_bytes_and_content_alignment():
    split = [token('�', bytes=[0xc3]), token('�', bytes=[0xa9])]
    result = collect_logprobs({'content': split}, 'é', 0)
    assert result.status == 'available'
    assert [t.position for t in result.tokens] == [0, 1]
    assert collect_logprobs({'content': [token('é', bytes=None)]}, 'é', 0).status == 'available'
    assert collect_logprobs({'content': split}, 'wrong', 0).status == 'invalid'


def test_fewer_alternatives_allowed_but_absent_and_sentinel_values_unknown():
    assert evidence([token(alternatives=[token()])], 5).status == 'available'
    assert evidence([token()], 1).status == 'incomplete'
    assert evidence([token(probability=-9999)], 0).status == 'incomplete'
    assert evidence([token(alternatives=[token('b', -9999)])]).status == 'incomplete'


def test_collection_limits_are_explicit(monkeypatch):
    import giraffe.logprobs as module
    assert collect_logprobs({'content': [token()] * 4097}, 'a'*4097, 0).status == 'incomplete'
    monkeypatch.setattr(module, 'MAX_ENTRIES', 1)
    assert evidence([token(alternatives=[token()])]).status == 'incomplete'
    monkeypatch.setattr(module, 'MAX_ENTRIES', 100)
    monkeypatch.setattr(module, 'MAX_TEXT_BYTES', 1)
    assert evidence().status == 'incomplete'


def test_extrema_cover_later_pairs_and_compare_raw_unrounded_values():
    records = [record(str(i), [token(probability=p)]) for i, p in enumerate([-.05, -.08, -.02])]
    result = compare_samples(records)
    assert result['sample_pairs'] == result['compared_token_pairs'] == 3
    assert result['max_chosen_delta'] == .06
    assert result['examples'][0]['request_ids'] == ['1', '2']
    assert comparison_status(result, LogprobConsistency(tolerance=0), True) == 'inconclusive'
    assert comparison_status(result, LogprobConsistency(strict=True, tolerance=.04), True) == 'fail'
    assert comparison_status(result, LogprobConsistency(strict=True, tolerance=.06), True) == 'pass'
    tiny = compare_samples([record('x', [token(probability=-.00001)]), record('y', [token(probability=-.0000100001)])])
    assert 0 < tiny['max_absolute_delta'] < 1e-9
    assert comparison_status(tiny, LogprobConsistency(strict=True, tolerance=0), True) == 'fail'


def test_alternative_order_is_irrelevant_and_membership_changes_are_separate():
    a, b = token('a', -.1), token('b', -2)
    records = [record('a'), record('b')]
    records[0].logprob_evidence = evidence([token(probability=-.1, alternatives=[a, b])])
    records[1].logprob_evidence = evidence([token(probability=-.1, alternatives=[b, a])])
    result = compare_samples(records)
    assert result['max_alternative_delta'] == 0 and result['top_k_membership_changes'] == 0
    records[1].logprob_evidence = evidence([token(probability=-.1, alternatives=[a, token('c', -3)])])
    result = compare_samples(records)
    assert result['top_k_membership_changes'] == 1
    assert result['max_alternative_delta'] == 0
    assert comparison_status(result, LogprobConsistency(strict=True, tolerance=0), True) == 'inconclusive'


def test_no_probability_comparison_after_history_diverges_even_if_text_reconverges():
    records = [record('a', [token('a'), token('x'), token('z', -.1)]),
               record('b', [token('a'), token('y'), token('z', -9)])]
    result = compare_samples(records)
    assert result['token_divergences'] == 1
    assert result['compared_token_pairs'] == 1 and result['max_chosen_delta'] == 0
    assert comparison_status(result, LogprobConsistency(strict=True, tolerance=0), True) == 'fail'
    records[1].logprob_evidence = evidence([token('a')], 0)
    assert compare_samples(records)['token_divergences'] == 1


def test_sentinel_is_never_a_numeric_probability():
    result = compare_samples([record('a'), record('b', [token(probability=-9999)])])
    assert result['unknown_values'] == 1 and result['max_absolute_delta'] is None
    assert comparison_status(result, LogprobConsistency(strict=True, tolerance=0), True) == 'inconclusive'


@pytest.mark.parametrize('field,value', [('request_hash', 'different'), ('observed_model', 'changed'), ('backend_id', 'other')])
def test_incompatible_request_or_deployment_identity_is_not_compared(field, value):
    records = [record('a'), record('b', [token(probability=-5)])]
    setattr(records[1], field, value)
    result = compare_samples(records)
    assert result['identity_mismatches'] == 1 and result['compared_token_pairs'] == 0
    assert comparison_status(result, LogprobConsistency(strict=True, tolerance=0), True) == 'inconclusive'


def test_cross_level_ranges_do_not_use_within_level_extremes():
    records = [record('a', [token(probability=-.02)], level=1), record('b', [token(probability=-.08)], level=1),
               record('c', [token(probability=-.05)], level=2)]
    result = compare_samples(records, cross=True)
    assert result['max_absolute_delta'] == pytest.approx(.03)
    assert result['sample_pairs'] == result['compared_token_pairs'] == 2
    for example in result['examples']:
        assert 'c' in example['request_ids']
    assert comparison_status(result, LogprobConsistency(strict=True, tolerance=.04), True) == 'pass'


def test_extrema_match_brute_force_pairs_across_corpora_and_history_prefixes():
    import random
    rng = random.Random(41)
    for cross in (False, True):
        records = [record(str(i), [token(rng.choice('ab'), -rng.random()), token('z', -rng.random())],
                          level=1+i%2, slot=i%3) for i in range(18)]
        deltas, count, divergences = [], 0, 0
        for a, b in combinations(records, 2):
            if a.workload['corpus_index'] != b.workload['corpus_index'] or (cross and a.workload['level'] == b.workload['level']):
                continue
            for x, y in zip(a.logprob_evidence.tokens, b.logprob_evidence.tokens):
                if x.bytes != y.bytes:
                    divergences += 1
                    break
                count += 1
                deltas.append(abs(x.logprob-y.logprob))
        result = compare_samples(records, cross=cross)
        assert result['compared_token_pairs'] == count
        assert result['token_divergences'] == divergences
        assert result['max_absolute_delta'] == max(deltas)


@pytest.mark.parametrize('strict,expected', [(False, 'inconclusive'), (True, 'fail')])
async def test_identical_answers_with_probability_drift_are_detected_through_http(strict, expected):
    with FakeEndpoint(mode='logprob_variable', first_token_delay=.02) as endpoint:
        configuration = run_config(endpoint, repetitions=4, options={
            'strict': strict, 'tolerance': .04, 'compare_levels': True, 'priming_rounds': 2})
        report = await run_suite(configuration)
        payloads = endpoint.requests
    result = check(report, 'consistency')
    assert result.status == expected
    probabilities = result.metrics['logprobs']
    assert probabilities['conditions'][0]['max_absolute_delta'] == .06
    assert probabilities['conditions'][1]['max_absolute_delta'] is None
    assert not any(c['coverage_complete'] for c in probabilities['conditions'])
    assert all(g['agreement_rate'] == 1 for g in result.metrics['buckets'].values())
    assert len(payloads) == 24 and all(not p['stream'] and p['logprobs'] and p['top_logprobs'] == 2 for p in payloads)
    assert all(p['messages'] == payloads[0]['messages'] and p['temperature'] == 0 and p['seed'] == 17 for p in payloads)
    assert len({r.request_hash for r in report.requests}) == 1
    assert len({r.history_hash for r in report.requests}) == 1
    assert all(m['role'] != 'assistant' for p in payloads for m in p['messages'])


@pytest.mark.parametrize('mode,support,expected', [
    ('missing', 'discover', 'inconclusive'), ('missing', 'required', 'fail'),
    ('rejected', 'discover', 'inconclusive'), ('rejected', 'required', 'fail'),
    ('sentinel', 'discover', 'inconclusive'), ('malformed', 'required', 'inconclusive'),
    ('length', 'discover', 'inconclusive'), ('http_error', 'discover', 'fail'), ('wrong', 'discover', 'inconclusive'),
])
async def test_unsupported_invalid_and_failed_completions_never_pass(mode, support, expected):
    with FakeEndpoint(mode=f'logprob_{mode}') as endpoint:
        report = await run_suite(run_config(endpoint, options={'strict': True, 'tolerance': 0, 'support': support}))
    assert check(report, 'consistency').status == expected


async def test_rounds_priming_and_budget_coverage_are_explicit():
    with FakeEndpoint(mode='logprob_healthy') as endpoint:
        configuration = run_config(endpoint, options={'priming_rounds': 1, 'strict': True, 'tolerance': 0})
        plan = request_plan(configuration)
        report = await run_suite(configuration)
        limited = await run_suite(configuration.model_copy(update={'max_requests': 11}))
    assert plan['planned_per_target'] == len(report.requests) == 16
    result = check(report, 'consistency')
    assert result.status == 'inconclusive'
    for cohort in result.metrics['logprobs']['conditions']:
        assert cohort['priming_issued'] == cohort['priming_planned'] == 2
        assert cohort['issued_requests'] == cohort['planned_requests'] == 6
        assert cohort['sample_pairs'] == (6 if cohort['level'] == 1 else 0)
        assert all(r['achieved_overlap'] == 0 for r in cohort['rounds'])
    assert len(limited.requests) == 11 and check(limited, 'consistency').status == 'inconclusive'
    assert any(c['missing_requests'] for c in check(limited, 'consistency').metrics['logprobs']['conditions'])
    modified = copy.deepcopy(report.requests)
    for r in modified:
        if r.workload['level'] == 2 and r.workload['round'] == 1:
            r.workload["load_qualified"] = False
    assert summarize_logprobs(modified, configuration.consistency)['status'] == 'inconclusive'
    for r in modified:
        if r.workload['level'] == 2:
            r.workload['load_qualified'] = False
    assert summarize_logprobs(modified, configuration.consistency)['status'] == 'inconclusive'


async def test_disabled_mode_uses_text_workload_count_and_wire_contract():
    with FakeEndpoint() as endpoint:
        configuration = run_config(endpoint)
        configuration.consistency.logprobs = None
        report = await run_suite(configuration)
        assert len(endpoint.requests) == 21
        assert all('logprobs' not in p and 'top_logprobs' not in p for p in endpoint.requests)
    assert all(r.logprob_evidence is None for r in report.requests)
    assert 'logprobs' not in check(report, 'consistency').metrics


@pytest.mark.parametrize('retention,retained', [('all', True), ('none', False), ('failures', False)])
async def test_retention_and_export_preserve_measurements_without_token_leakage(tmp_path, retention, retained):
    with FakeEndpoint(mode='logprob_variable') as endpoint:
        report = await run_suite(run_config(endpoint, retention=retention, repetitions=4, options={'priming_rounds': 2}))
    write_report(report, tmp_path, html=True)
    data = (tmp_path/'report.json').read_text()
    html = (tmp_path/'report.html').read_text()
    assert ('ALT_ONLY_SECRET' in data) == retained
    assert ('ALT_ONLY_SECRET' in html) == retained
    stored = json.loads(data)
    assert next(c for c in stored['checks'] if c['id'] == 'consistency')['metrics']['logprobs']['conditions'][0]['max_absolute_delta'] == .06
    example = next(c for c in stored['checks'] if c['id'] == 'consistency')['metrics']['logprobs']['conditions'][0]['examples'][0]
    for rid in example['request_ids']:
        index = next(i for i, r in enumerate(stored['requests']) if r['id'] == rid)
        assert f'href="#request-{index}"' in html
    assert 'Token log probabilities' in html
    assert report.requests[0].logprob_evidence.tokens  # export does not mutate in-memory evidence


async def test_logprob_settings_prevent_comparing_timings_with_ordinary_run():
    with FakeEndpoint(mode='logprob_healthy') as endpoint:
        enabled = await run_suite(run_config(endpoint))
        configuration = run_config(endpoint)
        configuration.consistency.logprobs = None
        disabled = await run_suite(configuration)
    assert _settings_reasons(enabled, disabled)
    assert enabled.manifest['fixture_pack']['workload_fixture_sha256'] != disabled.manifest['fixture_pack']['workload_fixture_sha256']


def test_browser_controls_roundtrip_and_evidence_links():
    browser_logic(r'''
const settings={top_logprobs:2,strict:true,tolerance:0,priming_rounds:1,compare_levels:true};
assert.equal(JSON.stringify(parseLogprobSettings(JSON.stringify(settings))),JSON.stringify(settings));
assert.equal(parseLogprobSettings('null'),null);
for(const invalid of ['[1]','true','{'])assert.throws(()=>parseLogprobSettings(invalid));
config.consistency={logprobs:settings};
const original=JSON.stringify(config);
assert.match(renderCheckSelection(config),/profile_consistency_logprobs/);
assert.deepEqual(Array.from(concurrentSettings(config).schedule.levels),[1,4]);
assert.equal(JSON.stringify(config),original);
const html=renderLogprobResults({status:'fail',conditions:[{shape:'short',level:2,examples:[{kind:'chosen',position:0,absolute_delta:.0000001,request_ids:['id1','id2']}]}]});
assert.match(html,/data-request="id1"/);
assert.match(html,/data-request="id2"/);
assert.match(renderLogprobEvidence({logprob_evidence:{tokens:[{token:'<script>'}]}}),/&lt;script&gt;/);
''')


@pytest.mark.parametrize('mode,evidence_status,probability_status', [
    ('missing', 'missing', 'inconclusive'), ('rejected', 'rejected', 'inconclusive'),
    ('sentinel', 'incomplete', 'inconclusive'), ('malformed', 'invalid', 'inconclusive'),
    ('length', 'available', 'inconclusive'), ('http_error', 'incomplete', 'inconclusive'),
])
async def test_http_probability_evidence_classification_and_eligibility(mode, evidence_status, probability_status):
    with FakeEndpoint(mode=f'logprob_{mode}') as endpoint:
        report = await run_suite(run_config(endpoint))
    assert {r.logprob_evidence.status for r in report.requests} == {evidence_status}
    result = check(report, 'consistency').metrics['logprobs']
    assert result['status'] == probability_status
    assert not any(c['coverage_complete'] for c in result['conditions'])
    assert all(c['support'] == 'discover' for c in [result])
    if mode in {'length', 'missing', 'malformed', 'rejected', 'http_error'}:
        assert not any(e['usable'] for c in result['conditions'] for e in c['evidence_outcomes'])


def test_real_collection_limits_and_shared_budget_admission():
    from giraffe.logprobs import LogprobBudget, MAX_RUN_BYTES
    alternatives = [token(str(i), -2) for i in range(20)]
    over_entries = [token('x', alternatives=alternatives)] * 781
    assert collect_logprobs({'content': over_entries}, 'x'*781, 20).status == 'incomplete'
    huge = 'x' * 262145
    assert collect_logprobs({'content': [token(huge)]}, huge, 0).status == 'incomplete'
    text = 'x' * 262144
    raw = {'content': [token(text)]}
    assert collect_logprobs(raw, text, 0).status == 'available'
    budget = LogprobBudget()
    outcomes = [budget.collect(raw, text, 0) for _ in range(20)]
    assert outcomes[0].status == 'available' and outcomes[-1].status == 'incomplete'
    assert budget.exhausted and budget.used_bytes <= MAX_RUN_BYTES
    assert not outcomes[-1].tokens
    class MustNotParse(dict):
        def get(self, *args):
            pytest.fail('Exhausted budget parsed another token collection')
    assert budget.collect(MustNotParse(), '', 0).status == 'incomplete'


async def test_failure_retention_keeps_probability_violation_evidence(tmp_path):
    with FakeEndpoint(mode='logprob_variable') as endpoint:
        report = await run_suite(run_config(endpoint, repetitions=4, options={
            'strict': True, 'tolerance': .04, 'priming_rounds': 2}, retention='failures'))
    write_report(report, tmp_path)
    stored = json.loads((tmp_path/'report.json').read_text())
    result = next(c for c in stored['checks'] if c['id'] == 'consistency')
    assert result['status'] == 'fail'
    representative = result['metrics']['logprobs']['conditions'][0]['examples'][0]['request_ids']
    assert all(r['logprob_evidence']['tokens'] for r in stored['requests'] if r['id'] in representative)


async def test_duration_timeout_and_cancellation_leave_explicit_missing_rounds():
    import asyncio
    with FakeEndpoint(mode='logprob_healthy', first_token_delay=.08) as endpoint:
        timeout = await run_suite(run_config(endpoint, request_timeout_seconds=.01))
        duration = await run_suite(run_config(endpoint, max_duration_seconds=.03))
        stop = asyncio.Event()
        def progress(event):
            if event['event'] == 'request_completed':
                stop.set()
        cancelled = await run_suite(run_config(endpoint), progress=progress, stop_event=stop)
    for report in [timeout, duration, cancelled]:
        result = check(report, 'consistency')
        assert result.status != 'pass'
        assert result.metrics['logprobs']['status'] == 'inconclusive'
        assert not all(c['coverage_complete'] for c in result.metrics['logprobs']['conditions'])
    assert any(c['missing_requests'] for c in check(cancelled, 'consistency').metrics['logprobs']['conditions'])


async def test_api_preview_export_execution_retention_and_rerun(tmp_path):
    from tests.test_web import browser, start, finished
    with FakeEndpoint(mode='logprob_healthy') as endpoint:
        values = run_config(endpoint, options={'strict': True, 'tolerance': 0, 'priming_rounds': 1}, retention='none').model_dump(mode='json')
        async with browser(tmp_path) as client:
            preview = await client.post('/api/config/preview', json=values)
            assert preview.status_code == 200, preview.text
            assert preview.json()['request_plan']['planned_per_target'] == 16
            normalized = preview.json()['config']
            assert normalized['consistency']['logprobs'] == values['consistency']['logprobs']
            cli = await client.post('/api/config/cli', json={'config': normalized})
            assert cli.status_code == 200 and '"top_logprobs": 2' in cli.json()['command']
            assert not endpoint.requests
            identifier = await start(client, normalized)
            detail = await finished(client, identifier)
            result = next(c for c in detail['report']['checks'] if c['id'] == 'consistency')
            assert result['status'] == 'inconclusive'
            assert all(not r['logprob_evidence']['tokens'] for r in detail['report']['requests'])
            saved = (await client.get(f'/api/runs/{identifier}/config.json')).json()
            assert saved['consistency']['logprobs'] == normalized['consistency']['logprobs']
            rerun = await start(client, saved)
            repeated = await finished(client, rerun)
            assert next(c for c in repeated['report']['checks'] if c['id'] == 'consistency')['status'] == 'inconclusive'
            invalid = copy.deepcopy(saved)
            invalid['consistency']['logprobs']['response_mode'] = 'stream'
            count = len(endpoint.requests)
            assert (await client.post('/api/runs', json={'config': invalid})).status_code == 422
            assert len(endpoint.requests) == count == 32


def test_cli_example_and_execution(tmp_path, capsys):
    from giraffe.cli import configuration, main, parser
    example = configuration(parser().parse_args(['run', '--config', 'examples/logprob-consistency.yaml']))
    assert request_plan(example)['planned_per_target'] == 48
    with FakeEndpoint(mode='logprob_variable') as endpoint:
        values = run_config(endpoint, repetitions=4, options={
            'strict': True, 'tolerance': .04, 'priming_rounds': 2}).model_dump(mode='json')
        path = tmp_path/'config.json'
        path.write_text(json.dumps(values))
        assert main(['run', '--config', str(path), '--output', str(tmp_path/'run')]) == 1
    stored = json.loads((tmp_path/'run'/'report.json').read_text())
    assert next(c for c in stored['checks'] if c['id'] == 'consistency')['metrics']['logprobs']['status'] == 'fail'
    assert 'local / consistency: fail' in capsys.readouterr().out


def test_form_edits_enable_disable_and_preserve_invalid_draft():
    browser_logic(r'''
const values=new Map([['profile_edits_consistency','["logprobs"]'],['profile_consistency_logprobs','{"top_logprobs":2,"strict":true,"tolerance":0}']]);
const enabled=configureProfiles(config,values,bootstrap.profile_defaults);
assert.equal(enabled.consistency.logprobs.top_logprobs,2);
assert.equal(enabled.consistency.logprobs.tolerance,0);
values.set('profile_consistency_logprobs','null');
assert.equal(configureProfiles(enabled,values,bootstrap.profile_defaults).consistency.logprobs,null);
values.set('profile_consistency_logprobs','{');
const draft=configureProfiles(enabled,values,bootstrap.profile_defaults,{allowInvalid:true});
assert.equal(draft.consistency.logprobs,'{');
assert.throws(()=>configureProfiles(draft,new Map(),bootstrap.profile_defaults));
''')


def test_oversized_numeric_probability_is_invalid_not_a_resource_limit():
    result = collect_logprobs({'content': [token(probability=-(10**1000))]}, 'a', 0)
    assert result.status == 'invalid' and not result.tokens


def test_many_cross_level_top_k_memberships_keep_exact_counts_and_representatives():
    records = []
    for i in range(200):
        item = record(str(i), level=1 if i < 100 else 2)
        item.logprob_evidence = evidence([token(alternatives=[token(f'alt{i}', -2)])], 1)
        records.append(item)
    result = compare_samples(records, cross=True)
    assert result['top_k_membership_changes'] == result['compared_token_pairs'] == 10000
    assert len(result['examples']) == 16
    for example in result['examples']:
        a, b = example['request_ids']
        assert int(a) < 100 <= int(b)


def test_comparison_work_limit_is_inconclusive_with_partial_measurements(monkeypatch):
    import giraffe.logprobs as module
    monkeypatch.setattr(module, 'MAX_COMPARISON_ENTRIES', 1)
    result = compare_samples([record('a'), record('b')])
    assert result['comparison_limit']
    assert comparison_status(result, LogprobConsistency(strict=True, tolerance=0), True) == 'inconclusive'



def test_round2_logprob_tail_exclusion_and_qualified_sample_minimum():
    options = settings(strict=True, tolerance=0, top_logprobs=0)
    rows = [record(f'{level}-{repeat}-{slot}', [token(str(i), probability=-4 if level == 2 and slot else -.05) for i in range(32)],
                   level=level, repeat=repeat, slot=slot)
            for level in (1, 2) for repeat in range(3) for slot in range(2)]
    for row in rows:
        if row.workload['level'] == 2 and row.workload['corpus_index'] == 1:
            row.workload['load_qualified'] = False
    result = summarize_logprobs(rows, options, min_samples=3)
    loaded = next(c for c in result['conditions'] if c['level'] == 2)
    assert result['status'] == 'pass'
    assert result['max_cross_level_delta'] == 0
    assert loaded['qualified_samples'] == 3 and len(loaded['tail_samples']) == 3
    assert all(len(r['tail_samples']) == 1 and r['qualified_samples'] == 1 for r in loaded['rounds'])
    for row in rows:
        if row.workload['level'] == 2 and row.workload['round'] == 2:
            row.workload['load_qualified'] = False
    result = summarize_logprobs(rows, options, min_samples=3)
    assert result['status'] == 'inconclusive'
    assert result['conditions'][1]['qualified_samples'] == 2


def test_round2_logprob_minimum_counts_qualified_requests_across_rounds():
    options = settings(strict=True, tolerance=0, top_logprobs=0)
    rows = [record(f'{level}-{repeat}-{slot}', [token(str(i)) for i in range(32)], level=level, repeat=repeat, slot=slot)
            for level in (1, 2) for repeat in range(3) for slot in range(2)]
    result = summarize_logprobs(rows, options, min_samples=5)
    assert result['status'] == 'pass'
    assert all(c['qualified_samples'] == 6 and c['coverage_complete'] for c in result['conditions'])
