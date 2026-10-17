from __future__ import annotations

from itertools import combinations

from giraffe.logprobs import compare_samples, comparison_status


def corpus_width(settings):
    return max(settings.levels)


async def run_logprob_consistency(run, target, client, fixtures):
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
                        spec.options.update(temperature=settings.temperature, logprobs=True,
                                            top_logprobs=options.top_logprobs)
                        if settings.seed is not None:
                            spec.options['seed'] = settings.seed
                        spec.workload = {'level': level, 'input_bucket': shape, 'equality': settings.equality,
                                         'temperature': settings.temperature, 'seed': settings.seed,
                                         'logprobs': True, 'top_logprobs': options.top_logprobs,
                                         'phase': phase, 'round': repeat, 'corpus_index': slot,
                                         'corpus_size': width, 'response_mode': 'complete',
                                         'option_support': 'requested; runtime enforcement unverified'}
                        specs.append(spec)
                    await run.group(target, client, specs, scenario, count=width, concurrency=level,
                                    checks=['consistency'])


def summarize_logprobs(records, settings):
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
                       'achieved_overlap': max((r.overlap for r in batch), default=0)}
                      for repeat in range(settings.repetitions)]
            available = [r for r in samples if usable(r)]
            comparison = compare_samples(available, prefix_cache=prefix_cache)
            complete = (len(observed) == planned and set(observed) == expected
                        and len(priming) == width * options.priming_rounds
                        and all(usable(r) and r.logprob_evidence.status == 'available' for r in cohort)
                        and all(item['achieved_overlap'] >= level for item in rounds))
            status = comparison_status(comparison, options, complete)
            if options.support == 'required' and any(r.logprob_evidence and
                    r.logprob_evidence.status in {'missing', 'rejected'} for r in cohort):
                status = 'fail'
            conditions.append({'shape': shape, 'level': level, 'status': status, 'coverage_complete': complete,
                'planned_requests': planned, 'issued_requests': len(samples),
                'completed_requests': sum(r.status == 'completed' and r.valid for r in samples),
                'missing_requests': max(0, planned - len(samples)), 'usable_samples': len(available),
                'priming_planned': width * options.priming_rounds, 'priming_issued': len(priming),
                'rounds': rounds, 'request_ids': [r.id for r in samples],
                'evidence_outcomes': [{'request_id': r.id,
                    'status': r.logprob_evidence.status if r.logprob_evidence else 'incomplete',
                    'reason': r.logprob_evidence.reason if r.logprob_evidence else r.status,
                    'response_status': r.status, 'finish_reason': r.finish_reason, 'usable': usable(r)} for r in cohort],
                **comparison})
    cross_conditions = []
    if options.compare_levels:
        for shape in settings.shapes:
            for left, right in combinations(dict.fromkeys([1, *settings.levels]), 2):
                samples = [r for r in measured if r.workload['input_bucket'] == shape and r.workload['level'] in {left, right} and usable(r)]
                comparison = compare_samples(samples, cross=True, prefix_cache=prefix_cache)
                complete = all(c['coverage_complete'] for c in conditions if c['shape'] == shape and c['level'] in {left, right})
                cross_conditions.append({'shape': shape, 'levels': [left, right],
                    'status': comparison_status(comparison, options, complete), **comparison})
    statuses = [item['status'] for item in conditions + cross_conditions]
    status = 'fail' if 'fail' in statuses else 'inconclusive' if 'inconclusive' in statuses else 'pass' if options.strict else 'diagnostic'
    return {'status': status, 'strict': options.strict, 'tolerance': options.tolerance,
            'top_logprobs': options.top_logprobs, 'support': options.support,
            'response_mode': 'complete', 'corpus_size': width, 'repetitions': settings.repetitions,
            'priming_rounds': options.priming_rounds, 'conditions': conditions, 'cross_conditions': cross_conditions,
            'interpretation': 'Natural-log probability differences, not throughput or correctness. Client overlap does not establish server batch membership. Unknown alternatives are not probability zero.'}
