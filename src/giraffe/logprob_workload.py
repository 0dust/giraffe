from __future__ import annotations

import asyncio
import time
from itertools import combinations

from giraffe.logprobs import compare_samples, comparison_status
from giraffe.overlap import NEEDS_STREAM, evidenced_overlap, peak_evidenced_concurrency, scheduling_delay_windows

# Few near-certain tokens say nothing about probability consistency.
MIN_COMPARED_POSITIONS = 32


def sample_count(count, level):
    return ((max(count, level) + level - 1) // level) * level


async def refill(run, target, client, specs, count, level, *, end=None, condition_plan=True):
    if condition_plan and specs:
        run.start_condition(target.name, specs[0].scenario, specs[0].workload, count,
                            "upper_bound" if end is not None else "exact")
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
                # Client in-flight counts only show dispatch; a serial server queues them.
                record.workload.update(client_peak_inflight=evidence['peak'],
                                       client_time_weighted_inflight=overlap)
                records.append(record)
            else:
                break

    try:
        await asyncio.gather(*(worker() for _ in range(min(level, count))))
    finally:
        if condition_plan and specs:
            run.finish_condition(target.name, specs[0].scenario, specs[0].workload,
                run.condition_reason(target.name, "sending window ended")
                if end is not None and time.monotonic() >= end and len(records) < count else None)
    average = area / (last - begin) if begin is not None and last > begin else 0
    delays = scheduling_delay_windows(run)
    peak = peak_evidenced_concurrency(records, delays)
    for record in records:
        evidenced = evidenced_overlap(record, getattr(run, 'records', records), delays)
        record.workload.update(cell_client_time_weighted_inflight=average, cell_evidenced_concurrency=peak,
                               evidenced_overlap=evidenced, observed_overlap=min(evidenced, peak),
                               load_qualified=level == 1 or (record.workload['client_time_weighted_inflight'] >= level - .5
                                                             and evidenced >= 2 and peak >= level))
    if len(records) < count:
        run.unfinished[target.name].update(check for spec in specs for check in spec.check_ids)
    return records


def corpus_width(settings):
    return max(settings.levels)


def logprob_fixtures(config, fixtures):
    """Non-repetitive copy passage (>= 64 tokens) so token comparisons cover many positions."""
    from giraffe.workloads import COPY_PASSAGES, consistency_output_cap, label_context, spec
    answer = COPY_PASSAGES[0]
    result = {}
    for shape in ('short', 'long'):
        context = ''
        if shape == 'long':
            context = label_context(max(64, (config.context_limit - 256) * 2)) + '\nThe background above is context only.\n'
        base = fixtures[shape][0]
        result[shape] = spec(context + f'Copy the following text exactly, with no extra text:\n{answer}',
                             base.scenario, 'consistency', expected=answer,
                             max_tokens=consistency_output_cap(config),
                             request_timeout_seconds=config.request_timeout_seconds, options=dict(base.options))
    return result


async def run_logprob_consistency(run, target, client, fixtures):
    from giraffe.fixtures import builtin_fixtures
    from giraffe.runner import _check_config
    from giraffe.workloads import consistency_options

    config = _check_config(run.config, "consistency")
    settings = config.consistency
    options = settings.logprobs
    width = corpus_width(settings)
    templates = logprob_fixtures(config, builtin_fixtures(config))
    levels = list(dict.fromkeys([1, *settings.levels]))
    for shape in settings.shapes:
        for level in levels:
            run.plan_condition(target.name, f'consistency_c{level}_{shape}',
                planned=width * (options.priming_rounds + settings.repetitions))
    try:
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
                            spec = templates[shape].model_copy(deep=True)
                            spec.fixture_id = f'logprob_{shape}_{slot}'
                            spec.scenario, spec.check_ids, spec.stream = scenario, ['consistency'], options.response_mode == 'stream'
                            spec.options = consistency_options(config, spec.options)
                            spec.options.update(logprobs=True, top_logprobs=options.top_logprobs)
                            spec.workload = {'level': level, 'input_bucket': shape, 'equality': settings.equality,
                                             'temperature': (config.request_options | spec.options).get('temperature'), 'seed': settings.seed,
                                             'logprobs': True, 'top_logprobs': options.top_logprobs,
                                             'phase': phase, 'round': repeat, 'corpus_index': slot,
                                             'corpus_size': width, 'response_mode': options.response_mode,
                                             'option_support': 'requested; runtime enforcement unverified'}
                            specs.append(spec)
                        await refill(run, target, client, specs, width, level, condition_plan=False)
    finally:
        for shape in settings.shapes:
            for level in levels:
                run.finish_condition(target.name, f'consistency_c{level}_{shape}')


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
                       'client_time_weighted_inflight': batch[0].workload.get('cell_client_time_weighted_inflight') if batch else None,
                       'evidenced_concurrency': batch[0].workload.get('cell_evidenced_concurrency') if batch else None,
                       'evidenced_overlap': max((r.workload.get('evidenced_overlap', 0) for r in batch), default=0),
                       'tail_samples': [r.id for r in batch if not r.workload.get('load_qualified', False)]}
                      for repeat in range(settings.repetitions)]
            available = [r for r in samples if usable(r) and r.workload.get('load_qualified', False)
                         and (level == 1 or r.stream)]
            comparison = compare_samples(available, prefix_cache=prefix_cache)
            complete = (len(observed) == planned and set(observed) == expected
                        and len(priming) == width * options.priming_rounds
                        and all(usable(r) and r.logprob_evidence.status == 'available' for r in cohort)
                        and len(available) >= min_samples
                        and comparison['compared_positions'] >= MIN_COMPARED_POSITIONS)
            status = comparison_status(comparison, options, complete)
            reasons = []
            truncated = sorted({r.requested_max_tokens for r in cohort if r.finish_reason == 'length'})
            if truncated:
                reasons.append(f"The output budget ({', '.join(map(str, truncated))} tokens) was exhausted before the complete logprob passage; raise max_output_tokens.")
            if level > 1 and len(available) < min_samples:
                reasons.append(NEEDS_STREAM if not any(r.stream for r in samples) else
                               'Requested concurrency was not evidenced by overlapping server output.')
            if comparison['compared_positions'] < MIN_COMPARED_POSITIONS:
                reasons.append(f"Fewer than {MIN_COMPARED_POSITIONS} token positions were compared.")
                status = 'inconclusive' if status in {'pass', 'diagnostic'} else status
            if options.support == 'required' and any(r.logprob_evidence and
                    r.logprob_evidence.status in {'missing', 'rejected'} for r in cohort):
                status = 'fail'
            conditions.append({'shape': shape, 'level': level, 'status': status, 'coverage_complete': complete,
                'coverage_reasons': reasons,
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
                samples = [r for r in measured if r.workload['input_bucket'] == shape and r.workload['level'] in {left, right} and usable(r) and r.workload.get('load_qualified', False) and (r.workload['level'] == 1 or r.stream)]
                comparison = compare_samples(samples, cross=True, prefix_cache=prefix_cache)
                complete = all(c['coverage_complete'] for c in conditions if c['shape'] == shape and c['level'] in {left, right})
                cross_status = comparison_status(comparison, options, complete)
                if comparison['compared_positions'] < MIN_COMPARED_POSITIONS and cross_status in {'pass', 'diagnostic'}:
                    cross_status = 'inconclusive'
                cross_conditions.append({'shape': shape, 'levels': [left, right],
                    'status': cross_status, **comparison})
    statuses = [item['status'] for item in conditions + cross_conditions]
    status = 'fail' if 'fail' in statuses else 'inconclusive' if 'inconclusive' in statuses else 'pass' if options.strict else 'diagnostic'
    deltas = [c['max_absolute_delta'] for c in cross_conditions if c['max_absolute_delta'] is not None]
    return {'status': status, 'strict': options.strict,
            'max_cross_level_delta': max(deltas, default=None),
            'cross_level_token_divergences': sum(c['token_divergences'] for c in cross_conditions), 'tolerance': options.tolerance,
            'top_logprobs': options.top_logprobs, 'support': options.support,
            'response_mode': options.response_mode, 'corpus_size': width, 'repetitions': settings.repetitions,
            'priming_rounds': options.priming_rounds, 'conditions': conditions, 'cross_conditions': cross_conditions,
            'within_level_interpretation': 'Run-to-run repeatability; cross_conditions compare concurrency against level 1.',
            'interpretation': 'Natural-log probability differences, not throughput or correctness. Client overlap does not establish server batch membership. Unknown alternatives are not probability zero.'}
