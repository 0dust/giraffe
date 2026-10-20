"""Two authorized five-minute diagnostics with short matched overhead controls."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import shutil
import time

from . import __version__
from .baseline import baseline_snapshot
from .driver import require_ignored, run_case, suite_source_hashes, write_json
from .packet_control import delayed_read_control
from .packet_analysis import analyze_case
from .scenarios import _case, digest, validate_matrix
from .timing_analysis import overhead_criteria
from .timing_observer import OBSERVER_VERSION


def cases():
    result = []
    for profile in ('concurrency250', 'burst250'):
        sustained = profile == 'concurrency250'
        for stage, duration in [('control', 15 if sustained else 10), ('five-minute', 300)]:
            for arm in ('baseline', 'capture') if stage == 'control' else ('capture',):
                scenario = _case(f'E{3 if sustained else 4}-packet-{profile}-{stage}-{arm}',
                    'E3' if sustained else 'E4', 'sustained_concurrency' if sustained else 'periodic_batch',
                    active_seconds=duration, concurrency=250, response='auto',
                    request_count=None if sustained else 250 * duration,
                    event_count=4 if sustained else 1, event_delay_ms=1000 if sustained else 0,
                    first_delay_ms=20 if sustained else 0, request_timeout_seconds=15,
                    expected={'outcome': 'completed'}, healthy_capacity=False,
                    repetitions=3 if stage == 'control' else 1,
                    purpose='Same-host five-minute packet diagnostic with independently measured server occupancy and burst receipt history; not Task 3 or endurance.')
                if not sustained:
                    scenario['batch_size'] = 250
                result.append(scenario)
    return result


def prepare(study, source, baseline, python):
    require_ignored(study)
    study.mkdir(parents=True, exist_ok=False)
    original = json.loads((source / 'suite-manifest.json').read_text())
    original_hash = original.pop('frozen_sha256')
    if digest(original) != original_hash:
        raise ValueError('Source manifest changed')
    suite = copy.deepcopy(original)
    selected = cases()
    validate_matrix(selected, require_all_experiments=False)
    suite.update(study_id=study.name, suite_version=__version__, cases=selected,
        created_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        baseline=baseline_snapshot(baseline, python), suite_source_hashes=suite_source_hashes(),
        source_study={'path': str(source.resolve()), 'frozen_sha256': original_hash},
        execution_selection={'scope': 'Two 300-second traffic windows plus three short matched pairs per profile.',
            'burst': '250 scheduled together at seconds 0 through 299; 75000 total.',
            'concurrency': '250 persistent client workers; actual server occupancy measured independently.',
            'product_exception': 'User authorized only Arrival.requests ceiling 10000 to 75000; isolated clean local build.',
            'endurance': 'Not authorized; Task 3 excluded.'})
    suite.pop('reviewed_execution_plan', None)
    suite['observation_policy'] = {'version': OBSERVER_VERSION,
        'observer_source_sha256': suite['suite_source_hashes']['timing_observer.py'],
        'scope': 'Unobserved versus observer plus owned loopback capture; three short matched pairs.'}
    suite['packet_policy'] = {'overhead': overhead_criteria(), 'precision_budget_ns': 100000,
        'minimum_exported_precision_coverage': .99,
        'long_run_transfer': 'Short matched controls do not establish absence of duration-dependent observer overhead.',
        'server_occupancy': 'Measured receipt through response_finished for matching measured response IDs; excludes controls and probes.',
        'qualification': 'Keep observed packet timing, arithmetic, load delivery and overhead qualification separate.'}
    suite['frozen_sha256'] = digest(suite)
    write_json(study / 'suite-manifest.json', suite)
    archive = study / 'frozen-source'
    archive.mkdir()
    for name in suite['suite_source_hashes']:
        shutil.copy2(Path(__file__).parent / name, archive / name)
    return suite


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', required=True, type=Path)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--baseline', required=True, type=Path)
    parser.add_argument('--python', required=True)
    args = parser.parse_args()
    suite = prepare(args.study, args.source, args.baseline, args.python)
    if not delayed_read_control(args.study / 'positive-control')['passed']:
        raise RuntimeError('Known delayed-read positive control failed')
    observer = {'enabled': True, 'version': OBSERVER_VERSION, 'uncertainty_budget_ns': 100000,
                'observer_source_sha256': suite['suite_source_hashes']['timing_observer.py']}
    for profile in ('concurrency250', 'burst250'):
        selected = [c for c in suite['cases'] if profile in c['id']]
        for rep in (1, 2, 3):
            controls = [c for c in selected if '-control-' in c['id']]
            for scenario in controls if rep != 2 else reversed(controls):
                execute(args.study, suite, scenario, rep, observer)
        execute(args.study, suite, next(c for c in selected if '-five-minute-' in c['id']), 1, observer)
    print('All traffic finished; raw evidence ready for offline analysis.', flush=True)


def execute(study, suite, scenario, rep, observer):
    if suite_source_hashes() != suite['suite_source_hashes']:
        raise RuntimeError('Suite source changed after freeze')
    capture = scenario['id'].endswith('-capture')
    print(f'START {scenario["id"]} r{rep}', flush=True)
    run_case(study, suite, scenario, rep, observer=observer if capture else None, capture=capture)
    directory = study / 'current-python' / f'{scenario["id"]}-r{rep}'
    manifest = json.loads((directory / 'manifest.json').read_text())
    if manifest['execution_status'] != 'completed' or not manifest.get('baseline_unchanged'):
        raise RuntimeError(f'Run did not complete cleanly: {directory}')
    if capture and not manifest['packet_capture_health']['recording_gate_pass']:
        raise RuntimeError(f'Capture incomplete: {directory}')
    if capture and '-control-' in scenario['id'] and rep == 1:
        summary = analyze_case(directory)
        print(f'PREFLIGHT matched={summary["matched"]}/{summary["exported_measured"]} '
              f'precision={summary["exported_precision_coverage"]} health={summary["observer_health_pass"]}', flush=True)
        if not summary['matched'] or not summary['observer_health_pass'] or summary['issues']:
            raise RuntimeError(f'Preflight packet correlation invalid: {directory}')
    print(f'END {scenario["id"]} r{rep}', flush=True)


if __name__ == '__main__':
    main()
