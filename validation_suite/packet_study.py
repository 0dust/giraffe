"""Narrow sequential packet diagnostic using the existing baseline runner.

This module never selects Task 3 or endurance. Raw capture parsing runs offline.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import shutil
import time

from . import __version__
from .driver import require_ignored, run_case, suite_source_hashes, write_json
from .packet_analysis import analyze_case
from .packet_control import delayed_read_control
from .scenarios import digest, validate_matrix
from .timing_analysis import analyze_overhead_pairs, overhead_criteria
from .timing_observer import OBSERVER_VERSION


ARMS = ('baseline', 'observer', 'capture')


def study_cases(source: dict) -> list[dict]:
    template = next(c for c in source['cases'] if c['experiment_id'] == 'E4' and
                    c['mode'] == 'fixed_rate' and c['fixture'] == 'normal')
    cases = []
    for rate, paced in [(50, False), (100, False), (200, False), (200, True)]:
        profile = f'r{rate}' + ('-paced' if paced else '-fast')
        for arm in ARMS:
            scenario = copy.deepcopy(template)
            response = '42' if not paced else '42 42 42 42'
            count = 4 if paced else 1
            contents = [response[len(response)*i//count:len(response)*(i+1)//count] for i in range(count)]
            scenario.update(id=f'E4-packet-{profile}-{arm}', active_seconds=10,
                rate_rps=rate, request_count=rate*10, repetitions=3, concurrency=256,
                response=response, response_bytes=len(response), event_count=count,
                first_delay_ms=20 if paced else 0, event_delay_ms=5 if paced else 0,
                tier='smoke', healthy_capacity=False,
                purpose='Short same-host packet timing diagnostic; fixed evenly spaced arrivals; no capacity claim.')
            scenario['expected'].update(content=response, event_contents=contents,
                                        completion_tokens=len(response.split()))
            cases.append(scenario)
    return cases


def prepare(study: Path, source: Path) -> dict:
    require_ignored(study)
    study.mkdir(parents=True, exist_ok=True)
    if (study / 'suite-manifest.json').exists():
        raise ValueError('Study already frozen; choose a new directory')
    original = json.loads((source / 'suite-manifest.json').read_text())
    expected = original.pop('frozen_sha256')
    if digest(original) != expected:
        raise ValueError('Source study manifest hash mismatch')
    suite = copy.deepcopy(original)
    cases = study_cases(original)
    validate_matrix(cases, require_all_experiments=False)
    suite.update(study_id=study.name, suite_version=__version__, cases=cases,
        created_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        suite_source_hashes=suite_source_hashes(),
        source_study={'path': str(source.resolve()), 'frozen_sha256': expected},
        execution_selection={'scope': 'Only 10-second fixed-rate packet diagnostics, three repetitions per arm.',
            'order': 'Per profile rotate baseline/observer/capture order across three repetitions.',
            'endurance': 'Not authorized or selected; Task 3 excluded.'},
        reference={'same_host': True, 'boundary': 'host software loopback capture, exact complete SSE event bytes',
            'wire_reference': True, 'limitations': 'Empirical clock mapping; no physical NIC accuracy guarantee or sole Giraffe-code attribution.'})
    suite.pop('reviewed_execution_plan', None)
    suite['observation_policy'] = {'version': OBSERVER_VERSION,
        'observer_source_sha256': suite['suite_source_hashes']['timing_observer.py'],
        'scope': 'Three matched arms: baseline, observer, observer plus owned loopback capture.',
        'qualification': 'Positive delayed-read control, per-metric precision/coverage, and matched overhead.'}
    suite['packet_policy'] = {'overhead': overhead_criteria(), 'precision_budget_ns': 100_000,
        'minimum_exported_precision_coverage': .99,
        'delay_control': 'Three zero-delay and three 50ms delayed reads before Giraffe tests.',
        'normal_run_transfer': 'Requires total observer-plus-capture overhead gate; measured delay still includes host scheduling.',
        'first_answer': 'Complete first nonempty content SSE event available vs timestamp Giraffe uses for that event.',
        'distributions': 'All matched responses retained, including wide intervals; precise subset also reported.'}
    suite['frozen_sha256'] = digest(suite)
    write_json(study / 'suite-manifest.json', suite)
    archive = study / 'frozen-source'
    archive.mkdir()
    for name in suite['suite_source_hashes']:
        shutil.copy2(Path(__file__).parent / name, archive / name)
    return suite


def analyze(study: Path, suite: dict) -> dict:
    summaries = {}
    control_passed = json.loads((study / 'positive-control' / 'control-result.json').read_text())['passed']
    for profile in ('r50-fast', 'r100-fast', 'r200-fast', 'r200-paced'):
        def case_dir(arm, rep):
            return study / 'current-python' / f'E4-packet-{profile}-{arm}-r{rep}'
        overhead = {}
        for name, control, observed in [('observer', 'baseline', 'observer'),
                                        ('capture_incremental', 'observer', 'capture'),
                                        ('total', 'baseline', 'capture')]:
            pairs = [{'repetition': rep, 'control_case_dir': str(case_dir(control, rep)),
                      'observed_case_dir': str(case_dir(observed, rep))} for rep in (1, 2, 3)]
            artifact = analyze_overhead_pairs(profile, pairs, study / f'overhead-{profile}-{name}.json',
                observer_source_sha256=suite['suite_source_hashes']['timing_observer.py'],
                baseline_sha=suite['baseline']['sha'], pace_ms=5 if profile.endswith('paced') else None)
            overhead[name] = artifact['profiles'][profile]
        summaries[profile] = {'overhead': overhead, 'packet_cases': []}
        for rep in (1, 2, 3):
            directory = case_dir('capture', rep)
            summary = json.loads((directory / 'packet-timing.json').read_text()) if (directory / 'packet-timing.json').exists() else analyze_case(directory)
            summary['overhead_qualified'] = overhead['total']['overhead_gate_pass']
            summary['actual_giraffe_run_qualified'] = bool(control_passed and summary['matched'] and
                summary['capture_recording_pass'] and summary['clock_mapping_pass'] and
                summary['observer_health_pass'] and not summary['issues'] and
                summary['exported_precision_coverage'] >= .99)
            summary['normal_run_transfer_supported'] = summary['actual_giraffe_run_qualified'] and summary['overhead_qualified']
            summary['qualification_pending'] = None
            summary['positive_control'] = str(study / 'positive-control' / 'control-result.json')
            write_json(directory / 'packet-timing.json', summary)
            summaries[profile]['packet_cases'].append(summary)
    write_json(study / 'packet-study-results.json', summaries)
    return summaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    args = parser.parse_args()
    suite = prepare(args.study, args.source)
    control = delayed_read_control(args.study / 'positive-control')
    if not control['passed']:
        raise RuntimeError('Known delayed-read control failed; no Giraffe run started')
    print('Positive control passed', flush=True)
    cases = {c['id']: c for c in suite['cases']}
    observer = {'enabled': True, 'version': OBSERVER_VERSION, 'uncertainty_budget_ns': 100_000,
                'observer_source_sha256': suite['suite_source_hashes']['timing_observer.py']}
    for profile in ('r50-fast', 'r100-fast', 'r200-fast', 'r200-paced'):
        for rep in (1, 2, 3):
            order = ARMS[rep-1:] + ARMS[:rep-1]
            for arm in order:
                scenario = cases[f'E4-packet-{profile}-{arm}']
                if suite_source_hashes() != suite['suite_source_hashes']:
                    raise RuntimeError('Suite changed after freeze; stopping')
                print(f'START {scenario["id"]} repetition {rep}', flush=True)
                run_case(args.study, suite, scenario, rep,
                    observer=observer if arm != 'baseline' else None, capture=arm == 'capture')
                directory = args.study / 'current-python' / f'{scenario["id"]}-r{rep}'
                manifest = json.loads((directory / 'manifest.json').read_text())
                if manifest['execution_status'] != 'completed' or not manifest.get('baseline_unchanged'):
                    raise RuntimeError(f'Run did not complete cleanly: {directory}')
                if arm == 'capture' and not manifest['packet_capture_health']['recording_gate_pass']:
                    raise RuntimeError(f'Capture recording failed: {directory}')
                if arm == 'capture':
                    matched = analyze_case(directory)
                    print(f'PACKET matched={matched["matched"]}/{matched["exported_measured"]} '
                          f'precise={matched["precise"]} clock_gate={matched["clock_mapping_pass"]}', flush=True)
                    if not matched['matched']:
                        raise RuntimeError('No matching packet/client evidence; stopping')
                print(f'END {scenario["id"]} repetition {rep}', flush=True)
    print('Traffic complete; starting offline packet matching', flush=True)
    analyze(args.study, suite)
    print('Packet study complete', flush=True)


if __name__ == '__main__':
    main()
