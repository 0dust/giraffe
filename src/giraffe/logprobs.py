from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from bisect import bisect_right

from .caps import MAX_LOGPROB_EXAMPLES
from giraffe.models import LogprobAlternative, LogprobEvidence, LogprobToken

MAX_TOKENS = 4096
MAX_ENTRIES = 16384
MAX_TEXT_BYTES = 512 * 1024
MAX_RUN_BYTES = 16 * 1024 * 1024
MAX_COMPARISON_ENTRIES = 2_000_000
SENTINEL = -9999.0


class _CollectionLimit(Exception):
    pass


class LogprobBudget:
    def __init__(self):
        self.used_bytes = 0
        self.pending_bytes = 0
        self.exhausted = False

    def collect(self, raw, output, top_k):
        if self.exhausted:
            return self.limit(top_k)
        return self.retain(collect_logprobs(raw, output, top_k))

    def reserve(self, size):
        if not self.exhausted and self.used_bytes + self.pending_bytes + size <= MAX_RUN_BYTES:
            self.pending_bytes += size
            return True
        self.exhausted = True
        return False

    def release(self, size):
        self.pending_bytes -= size

    def retain(self, evidence):
        if not self.exhausted:
            size = len(evidence.model_dump_json().encode())
            if self.used_bytes + self.pending_bytes + size <= MAX_RUN_BYTES:
                self.used_bytes += size
                return evidence
            self.exhausted = True
        return self.limit(evidence.requested_top_logprobs)

    @staticmethod
    def limit(top_k):
        return LogprobEvidence(status='incomplete', requested_top_logprobs=top_k,
            reason='Run logprob evidence exceeded the 16 MiB collection limit.')


def identity(token):
    if token.token_id is not None:
        return ('id', token.token_id)
    if token.bytes is not None:
        return ('bytes', tuple(token.bytes))
    return ('text', token.token)


class LogprobCollector:
    """Bound token evidence as chunks arrive; align UTF-8 only after assembly."""

    def __init__(self, top_k, budget=None):
        self.top_k = top_k
        self.budget = budget
        self.reserved_bytes = 0
        self.tokens = []
        self.entries = self.text_bytes = 0
        self.error = None

    def outcome(self, status, reason, tokens=()):
        return LogprobEvidence(status=status, reason=reason,
                              requested_top_logprobs=self.top_k, tokens=list(tokens))

    def alternative(self, value):
        if not isinstance(value, dict) or not isinstance(value.get('token'), str):
            raise ValueError('Invalid token identity.')
        token_bytes = value.get('bytes')
        if token_bytes is not None and (not isinstance(token_bytes, list) or not token_bytes or
                any(type(b) is not int or not 0 <= b <= 255 for b in token_bytes)):
            raise ValueError('Invalid token bytes.')
        token_id = value.get('token_id')
        if token_id is not None and (type(token_id) is not int or token_id < 0):
            raise ValueError('Invalid token ID.')
        probability = value.get('logprob')
        if type(probability) not in {int, float} or not math.isfinite(probability) or probability > 0:
            raise ValueError('Logprobs must be finite, non-positive numbers.')
        self.text_bytes += len(value['token'].encode()) + len(token_bytes or [])
        self.entries += 1
        if self.text_bytes > MAX_TEXT_BYTES or self.entries > MAX_ENTRIES:
            raise _CollectionLimit
        return LogprobAlternative(token=value['token'], bytes=token_bytes, token_id=token_id, logprob=probability)

    def release(self):
        if self.budget is not None:
            self.budget.release(self.reserved_bytes)
            self.reserved_bytes = 0

    def discard(self):
        self.tokens.clear()
        self.release()

    def add(self, raw):
        if self.budget is not None and self.budget.exhausted:
            self.error = self.budget.limit(self.top_k)
            self.discard()
            return
        if self.error is not None or raw is None or isinstance(raw, dict) and raw.get('content') is None:
            return
        if not isinstance(raw, dict) or not isinstance(raw.get('content'), list):
            self.error = self.outcome('invalid', 'Malformed content logprob collection.')
            self.discard()
            return
        try:
            if len(self.tokens) + len(raw['content']) > MAX_TOKENS:
                raise _CollectionLimit
            for value in raw['content']:
                chosen = self.alternative(value)
                top = value.get('top_logprobs', [] if self.top_k == 0 else None)
                if not isinstance(top, list) or len(top) > self.top_k:
                    raise ValueError('Invalid top-logprob list or count.')
                alternatives = [self.alternative(item) for item in top]
                if len({identity(item) for item in alternatives}) != len(alternatives):
                    raise ValueError('Duplicate alternative token identity.')
                if any(identity(item) == identity(chosen) and item.logprob != chosen.logprob for item in alternatives):
                    raise ValueError('Chosen and alternative token logprobs conflict.')
                token = LogprobToken(**chosen.model_dump(), position=len(self.tokens), top_logprobs=alternatives)
                if self.budget is not None:
                    size = len(token.model_dump_json().encode()) + 1
                    if not self.budget.reserve(size):
                        self.error = self.budget.limit(self.top_k)
                        self.discard()
                        return
                    self.reserved_bytes += size
                self.tokens.append(token)
        except _CollectionLimit:
            self.error = self.outcome('incomplete', 'Token evidence exceeds its bounded collection budget.')
        except (ValueError, OverflowError, UnicodeError, RecursionError):
            self.error = self.outcome('invalid', 'Invalid token identities, probabilities, or alternatives.')
        if self.error is not None:
            self.discard()

    def finish(self, output):
        # The caller retains the completed evidence synchronously after release.
        self.release()
        if self.budget is not None and self.budget.exhausted:
            self.tokens.clear()
            return self.budget.limit(self.top_k)
        if self.error is not None:
            return self.error
        if not self.tokens:
            return self.outcome('missing', 'No content token logprobs returned.')
        try:
            if all(token.bytes is not None for token in self.tokens):
                reconstructed = bytes(b for token in self.tokens for b in token.bytes).decode('utf-8')
            else:
                reconstructed = ''.join(token.token for token in self.tokens)
            if reconstructed != output:
                raise ValueError('Token evidence does not reconstruct the assistant content.')
        except (ValueError, UnicodeError):
            return self.outcome('invalid', 'Token evidence does not reconstruct the assistant content.')
        partial = any(token.logprob == SENTINEL or (self.top_k and not token.top_logprobs) or
                      any(item.logprob == SENTINEL for item in token.top_logprobs) for token in self.tokens)
        return self.outcome('incomplete' if partial else 'available',
                            'Sentinel or absent alternative probabilities are unknown.' if partial else
                            'Content token logprobs collected; returned alternatives are not the full distribution.', self.tokens)


def collect_logprobs(raw, output, top_k):
    collector = LogprobCollector(top_k)
    collector.add(raw)
    return collector.finish(output)


def compare_samples(records, *, cross=False, prefix_cache=None):
    prefix_cache = {} if prefix_cache is None else prefix_cache
    summary = {'sample_pairs': 0, 'compared_token_pairs': 0, 'compared_positions': 0,
               'compared_alternative_values': 0, 'token_divergences': 0, 'top_k_membership_changes': 0,
               'max_chosen_delta': None, 'max_alternative_delta': None, 'max_absolute_delta': None,
               'unknown_values': 0, 'examples': [], 'identity_mismatches': 0, 'comparison_limit': False}
    conditions = {r.id: r.workload['level'] for r in records}
    work = 0

    def pairs(ids):
        count = math.comb(len(ids), 2)
        if cross:
            count -= sum(math.comb(n, 2) for n in Counter(conditions[rid] for rid in ids).values())
        return count

    by_slot = defaultdict(list)
    for record in records:
        by_slot[record.workload['corpus_index']].append(record)

    def example(kind, position, a, b, delta=None):
        if len(summary['examples']) < MAX_LOGPROB_EXAMPLES:
            summary['examples'].append({'kind': kind, 'position': position,
                                        'request_ids': [a, b], 'absolute_delta': delta})

    def ranges(values, position, kind):
        values = [(record, value) for record, value in values if value != SENTINEL]
        if not pairs([rid for rid, _ in values]):
            return
        if cross:
            levels = sorted({conditions[rid] for rid, _ in values})
            left = [item for item in values if conditions[item[0]] == levels[0]]
            right = [item for item in values if conditions[item[0]] == levels[1]]
            extremes = [(min(left, key=lambda x: x[1]), max(right, key=lambda x: x[1])),
                        (min(right, key=lambda x: x[1]), max(left, key=lambda x: x[1]))]
            low, high = max(extremes, key=lambda pair: pair[1][1] - pair[0][1])
        else:
            low = min(values, key=lambda item: item[1])
            high = max(values, key=lambda item: item[1])
        delta = high[1] - low[1]
        field = 'max_chosen_delta' if kind == 'chosen' else 'max_alternative_delta'
        summary[field] = max(summary[field] or 0, delta)
        summary['max_absolute_delta'] = max(summary['max_absolute_delta'] or 0, delta)
        if delta:
            example(kind, position, low[0], high[0], delta)
        if kind == 'chosen':
            summary['compared_token_pairs'] += pairs([rid for rid, _ in values])
        else:
            summary['compared_alternative_values'] += len(values)

    for samples in by_slot.values():
        compatible = defaultdict(list)
        for record in samples:
            compatible[(record.request_hash, record.observed_model, record.backend_id)].append(record)
        summary['identity_mismatches'] += len(compatible) - 1
        for group in compatible.values():
            summary['sample_pairs'] += pairs([r.id for r in group])
            prefixes = defaultdict(list)
            for record in group:
                tokens = record.logprob_evidence.tokens
                work += sum(1 + len(token.top_logprobs) for token in tokens)
                if work > MAX_COMPARISON_ENTRIES:
                    summary['comparison_limit'] = True
                    return summary
                if record.id not in prefix_cache:
                    history = hashlib.sha256()
                    keys = []
                    for token in tokens:
                        keys.append(history.digest())
                        history.update(json.dumps(identity(token), ensure_ascii=False).encode() + b'\0')
                    prefix_cache[record.id] = [*keys, history.digest()]
                for position, token in enumerate([*tokens, None]):
                    prefixes[(position, prefix_cache[record.id][position])].append((record.id, token))
            for (position, _), values in prefixes.items():
                if not pairs([rid for rid, _ in values]):
                    continue
                chosen, alternatives = defaultdict(list), defaultdict(list)
                memberships = defaultdict(list)
                identities = defaultdict(list)
                for rid, token in values:
                    identities[identity(token) if token is not None else ('end',)].append(rid)
                    if token is None:
                        continue
                    chosen[identity(token)].append((rid, token.logprob))
                    membership = frozenset(identity(item) for item in token.top_logprobs)
                    memberships[membership].append(rid)
                    for item in token.top_logprobs:
                        alternatives[identity(item)].append((rid, item.logprob))
                    summary['unknown_values'] += (token.logprob == SENTINEL) + sum(
                        item.logprob == SENTINEL for item in token.top_logprobs)
                summary['compared_positions'] += bool(chosen)
                for different in (identities, memberships):
                    field = 'token_divergences' if different is identities else 'top_k_membership_changes'
                    differences = pairs([rid for group in different.values() for rid in group]) - sum(
                        pairs(group) for group in different.values())
                    summary[field] += differences
                    if differences and len(summary['examples']) < MAX_LOGPROB_EXAMPLES:
                        groups = list(different.values())
                        representatives, level_groups = [], defaultdict(list)
                        for index, ids in enumerate(groups):
                            first = {}
                            for rid in ids:
                                first.setdefault(conditions[rid], rid)
                            representatives.append(first)
                            for level in first:
                                level_groups[level].append(index)
                        for index, left in enumerate(representatives):
                            if len(summary['examples']) >= MAX_LOGPROB_EXAMPLES:
                                break
                            if cross and len(left) == 1:
                                other = next((ids for level, ids in level_groups.items() if level not in left), [])
                                candidates = other[bisect_right(other, index):]
                            else:
                                candidates = range(index + 1, len(groups))
                            for right_index in candidates:
                                if len(summary['examples']) >= MAX_LOGPROB_EXAMPLES:
                                    break
                                right = representatives[right_index]
                                pair = next((a, b) for a in left.values() for b in right.values()
                                            if not cross or conditions[a] != conditions[b])
                                example(field, position, *pair)
                for observations in chosen.values():
                    ranges(observations, position, 'chosen')
                for observations in alternatives.values():
                    ranges(observations, position, 'alternative')
    return summary


def comparison_status(summary, settings, complete):
    if settings.strict and summary['token_divergences']:
        return 'fail'
    if summary['max_absolute_delta'] is not None and summary['max_absolute_delta'] > (settings.tolerance or 0):
        return 'fail' if settings.strict else 'inconclusive'
    if not complete or summary['comparison_limit'] or summary['identity_mismatches'] or not summary['compared_token_pairs'] or any(
            summary[key] for key in ('token_divergences', 'top_k_membership_changes', 'unknown_values')):
        return 'inconclusive'
    return 'pass' if settings.strict else 'diagnostic'
