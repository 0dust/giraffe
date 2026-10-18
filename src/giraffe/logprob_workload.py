from __future__ import annotations

import asyncio
import time
from itertools import combinations

from giraffe.logprobs import compare_samples, comparison_status


def sample_count(count, level):
    return ((max(count, level) + level - 1) // level) * level


async def refill(run, target, client, specs, count, level, *, end=None):
    active, records = {}, []
    issued = 0
    area, last, begin = 0., None, None

    def tick():
        nonlocal area, last, begin
        now = time.monotonic()
        if last is not None:
            area += len(active) * (now - last)
        else:
            begin = now
        last = now

    async def worker():
        nonlocal issued
        while issued < count and run.allowed(target.name) and (end is None or time.monotonic() < end):
            index = issued
            issued += 1
            request = specs[index % len(specs)].model_copy(deep=True)
            evidence = {'peak': 0, 'start_area': 0., 'started': None, 'ended': None}

            def started():
                tick()
                evidence['started'] = last
                evidence['start_area'] = area
                active[index] = evidence
                for item in active.values():
                    item['peak'] = max(item['peak'], len(active))

            try:
                if end is None:
                    record = await run.request(target, client, request, on_start=started)
                else:
                    async with run.slot(False, end) as admitted:
                        if not admitted or time.monotonic() >= end:
                            break
                        record = await run.request(target, client, request, on_start=started,
                                                   slot_reserved=True)
            finally:
                if index in active:
                    tick()
                    evidence['ended'] = last
                    active.pop(index)
            if record is not None:
                duration = (evidence['ended'] - evidence['started']
                            if evidence['started'] is not None and evidence['ended'] is not None else 0)
                overlap = (area - evidence['start_area']) / duration if duration > 0 else 0
                record.workload.update(observed_overlap=evidence['peak'],
                                       time_weighted_inflight=overlap,
                                       load_qualified=level == 1 or overlap >= level - .5)
                records.append(record)
            else:
                break

    await asyncio.gather(*(worker() for _ in range(min(level, count))))
    average = area / (last - begin) if begin is not None and last > begin else 0
    for record in records:
        record.workload['cell_time_weighted_inflight'] = average
    if len(records) < count:
        run.unfinished[target.name].update(check for spec in specs for check in spec.check_ids)
    return records


def corpus_width(settings):
    return max(settings.levels)


async def run_logprob_consistency(run, target, client, fixtures):
    from giraffe.workloads import consistency_options

    settings = run.config.consistency
    options = settings.logprobs
    width = corpus_width(settings)
    for shape in settings.shapes:
        for level in dict.fromkeys([1, *settings.levels]):
            scenario = f'consistency_c{level}_{shape}'
            for phase, rounds in [('priming', options.priming_rounds), ('measured', settings.repetitions)]:
                for repeat in range(rounds):
                    if not run.allowed(target.name):
                        run.unfinished[target.name].add('consistency')
                        return
                    specs = []
                    for slot in range(width):
                        spec = fixtures[shape][0].model_copy(deep=True)
                        spec.fixture_id = f'logprob_{shape}_{slot}'
                        spec.scenario, spec.check_ids, spec.stream = scenario, ['consistency'], False
                        spec.options = consistency_options(run.config, spec.options)
                        spec.options.update(logprobs=True, top_logprobs=options.top_logprobs)
                        spec.workload = {'level': level, 'input_bucket': shape, 'equality': settings.equality,
                                         'temperature': (run.config.request_options | spec.options).get('temperature'), 'seed': settings.seed,
                                         'logprobs': True, 'top_logprobs': options.top_logprobs,
                                         'phase': phase, 'round': repeat, 'corpus_index': slot,
                                         'corpus_size': width, 'response_mode': 'complete',
                                         'option_support': 'requested; runtime enforcement unverified'}
                        specs.append(spec)
                    await refill(run, target, client, specs, width, level)


def summarize_logprobs(records, settings, *, min_samples=2):
    options = settings.logprobs
    width = corpus_width(settings)
    conditions = []
    prefix_cache = {}
    measured = [r for r in records if r.workload.get('phase') == 'measured']
    def usable(r):
        return (r.status == 'completed' and r.valid and r.finish_reason == 'stop'
                and r.logprob_evidence is not None and bool(r.logprob_evidence.tokens))
    for shape in settings.shapes:
        for level in dict.fromkeys([1, *settings.levels]):
            cohort = [r for r in records if r.workload.get('input_bucket') == shape and r.workload.get('level') == level]
            samples = [r for r in cohort if r.workload.get('phase') == 'measured']
            priming = [r for r in cohort if r.workload.get('phase') == 'priming']
            planned = width * settings.repetitions
            expected = {(repeat, slot) for repeat in range(settings.repetitions) for slot in range(width)}
            observed = [(r.workload['round'], r.workload['corpus_index']) for r in samples]
            rounds = [{'round': repeat, 'issued': len(batch := [r for r in samples if r.workload['round'] == repeat]),
                       'completed': sum(r.valid and r.status == 'completed' for r in batch),
                       'achieved_overlap': max((r.workload.get('observed_overlap', 0) for r in batch), default=0),
                       'qualified_samples': sum(r.workload.get('load_qualified', False) for r in batch),
                       'time_weighted_inflight': batch[0].workload.get('cell_time_weighted_inflight') if batch else None,
                       'tail_samples': [r.id for r in batch if not r.workload.get('load_qualified', False)]}
                      for repeat in range(settings.repetitions)]
            available = [r for r in samples if usable(r) and r.workload.get('load_qualified', False)]
            comparison = compare_samples(available, prefix_cache=prefix_cache)
            complete = (len(observed) == planned and set(observed) == expected
                        and len(priming) == width * options.priming_rounds
                        and all(usable(r) and r.logprob_evidence.status == 'available' for r in cohort)
                        and len(available) >= min_samples)
            status = comparison_status(comparison, options, complete)
            if options.support == 'required' and any(r.logprob_evidence and
                    r.logprob_evidence.status in {'missing', 'rejected'} for r in cohort):
                status = 'fail'
            conditions.append({'shape': shape, 'level': level, 'status': status, 'coverage_complete': complete,
                'planned_requests': planned, 'issued_requests': len(samples),
                'completed_requests': sum(r.status == 'completed' and r.valid for r in samples),
                'missing_requests': max(0, planned - len(samples)), 'usable_samples': len(available),
                'qualified_samples': len(available),
                'tail_samples': [r.id for r in samples if not r.workload.get('load_qualified', False)],
                'priming_planned': width * options.priming_rounds, 'priming_issued': len(priming),
                'rounds': rounds, 'request_ids': [r.id for r in samples],
                'evidence_outcomes': [{'request_id': r.id,
                    'status': r.logprob_evidence.status if r.logprob_evidence else 'incomplete',
                    'reason': r.logprob_evidence.reason if r.logprob_evidence else r.status,
                    'response_status': r.status, 'finish_reason': r.finish_reason, 'usable': usable(r)} for r in cohort],
                **comparison})
    cross_conditions = []
    for shape in settings.shapes:
        for left, right in combinations(dict.fromkeys([1, *settings.levels]), 2):
            if options.compare_levels or left == 1:
                samples = [r for r in measured if r.workload['input_bucket'] == shape and r.workload['level'] in {left, right} and usable(r) and r.workload.get('load_qualified', False)]
                comparison = compare_samples(samples, cross=True, prefix_cache=prefix_cache)
                complete = all(c['coverage_complete'] for c in conditions if c['shape'] == shape and c['level'] in {left, right})
                cross_conditions.append({'shape': shape, 'levels': [left, right],
                    'status': comparison_status(comparison, options, complete), **comparison})
    statuses = [item['status'] for item in conditions + cross_conditions]
    status = 'fail' if 'fail' in statuses else 'inconclusive' if 'inconclusive' in statuses else 'pass' if options.strict else 'diagnostic'
    deltas = [c['max_absolute_delta'] for c in cross_conditions if c['max_absolute_delta'] is not None]
    return {'status': status, 'strict': options.strict,
            'max_cross_level_delta': max(deltas, default=None),
            'cross_level_token_divergences': sum(c['token_divergences'] for c in cross_conditions), 'tolerance': options.tolerance,
            'top_logprobs': options.top_logprobs, 'support': options.support,
            'response_mode': 'complete', 'corpus_size': width, 'repetitions': settings.repetitions,
            'priming_rounds': options.priming_rounds, 'conditions': conditions, 'cross_conditions': cross_conditions,
            'within_level_interpretation': 'Run-to-run repeatability; cross_conditions compare concurrency against level 1.',
            'interpretation': 'Natural-log probability differences, not throughput or correctness. Client overlap does not establish server batch membership. Unknown alternatives are not probability zero.'}
