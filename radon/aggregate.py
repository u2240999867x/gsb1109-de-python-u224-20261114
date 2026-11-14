'''This module merges per-shard function complexity reports into a single
team-aware frequency distribution and computes function-level percentiles
from it.

A *shard* is the report produced by one collection run. Every shard records
the owning team of each function it collected together with the function
cyclomatic complexity, for example::

    {
        'team': 'payments',
        'functions': [
            {'team': 'payments', 'complexity': 4},
            {'team': 'payments', 'complexity': 12},
        ],
    }

The raw ``(team, complexity)`` pairs may also be passed as plain tuples or as
a mapping of complexity to multiplicity.

Shards are merged by summing frequency counts. Splitting a large team into
many small shards therefore never inflates its weight: each function keeps
exactly one vote through its multiplicity in the merged histogram, while a
team owning a single function cannot end up on equal footing with a team
owning hundreds of them. Teams without any countable function keep a sample
size of zero and get no percentile instead of a misleadingly perfect score.
'''

import math
from collections import Counter

from radon.complexity import cc_rank


def _normalise_functions(functions, default_team):
    '''Yield ``(team, complexity)`` pairs from any supported shard format.

    A function record without an explicit team inherits the team of the
    shard that collected it.
    '''
    if isinstance(functions, dict):
        def _records():
            for complexity, multiplicity in functions.items():
                multiplicity = int(multiplicity)
                if multiplicity < 0:
                    raise ValueError(
                        'Function multiplicity must be a non-negative value'
                    )
                for _ in range(multiplicity):
                    yield default_team, complexity

        records = _records()
    else:
        records = functions
    for record in records:
        if isinstance(record, dict):
            team = record.get('team', default_team)
            complexity = record['complexity']
        else:
            team, complexity = record
        complexity = int(complexity)
        if complexity < 0:
            raise ValueError('Complexity must be a non-negative value')
        yield team, complexity


def _rank_allowed(complexity, ranks):
    '''Return True when *complexity* passes the rank boundary filter.'''
    if not ranks:
        return True
    return cc_rank(complexity) in ranks


class FunctionComplexityAggregate(object):
    '''An exact, mergeable histogram of function complexities per team.

    For every ``(team, complexity)`` pair the aggregate stores the number of
    functions observed. Instances combine additively, so shards collected
    independently can be merged without re-reading the original functions and
    duplicate scores keep their real multiplicity.
    '''

    def __init__(self):
        self._teams = {}

    @classmethod
    def from_shards(cls, shards):
        '''Build an aggregate from an iterable of shard reports.'''
        aggregate = cls()
        for shard in shards:
            aggregate.merge_shard(shard)
        return aggregate

    def merge_shard(self, shard):
        '''Merge a single shard report, summing counts per team and score.

        A shard without any function still registers its team, so the team
        is reported with a sample size of zero rather than disappearing.
        '''
        team = shard['team']
        self._teams.setdefault(team, Counter())
        for function_team, complexity in _normalise_functions(
            shard.get('functions', ()), team
        ):
            histogram = self._teams.setdefault(function_team, Counter())
            histogram[complexity] += 1
        return self

    def merge(self, other):
        '''Merge another aggregate into this one by adding frequencies.'''
        for team, counts in other._teams.items():
            self._teams.setdefault(team, Counter()).update(counts)
        return self

    @property
    def teams(self):
        '''Return the sorted names of every team seen in a shard.'''
        return sorted(self._teams)

    def sample_size(self, team=None, ranks=None):
        '''Return the number of countable functions for a team or overall.

        Teams mentioned by a shard but without any function, or teams filtered
        out completely by *ranks*, report a sample size of zero.
        '''
        return sum(self._histogram(team, ranks).values())

    def distribution(self, team=None, ranks=None):
        '''Return the exact distribution as an ordered ``score -> count`` map.

        Scores are sorted ascending and every duplicate score is represented
        once with its real count. When *ranks* is given (a container of
        ``radon`` rank letters such as ``('A', 'B')``), only functions whose
        rank falls inside those existing boundaries are counted.
        '''
        histogram = self._histogram(team, ranks)
        return dict(sorted(histogram.items()))

    def percentile(self, percent, team=None, ranks=None):
        '''Return the *percent* percentile using the ceiling-rank method.

        The selected rank is ``ceil(percent / 100 * n)`` (1-based) and the
        value at that rank is returned, so ties keep their real multiplicity.
        Return ``None`` when no function matches instead of inventing a
        score. *percent* must be in the interval ``(0, 100]``.
        '''
        if not 0 < percent <= 100:
            raise ValueError('percent must be greater than 0 and at most 100')
        histogram = self._histogram(team, ranks)
        total = sum(histogram.values())
        if total == 0:
            return None
        rank = max(int(math.ceil(percent / 100.0 * total)), 1)
        cumulative = 0
        for score in sorted(histogram):
            cumulative += histogram[score]
            if cumulative >= rank:
                return score

    def p90(self, team=None, ranks=None):
        '''Return the function-level 90th percentile, or ``None`` if empty.'''
        return self.percentile(90, team=team, ranks=ranks)

    def team_summary(self, team, ranks=None):
        '''Return a serialisable summary for a single team.'''
        return {
            'team': team,
            'sample_size': self.sample_size(team, ranks),
            'distribution': self.distribution(team, ranks),
            'p90': self.p90(team, ranks),
        }

    def overall_summary(self, ranks=None):
        '''Return a serialisable summary across every team.'''
        return {
            'sample_size': self.sample_size(ranks=ranks),
            'distribution': self.distribution(ranks=ranks),
            'p90': self.p90(ranks=ranks),
        }

    def to_dict(self):
        '''Serialise to plain dictionaries, ready for JSON or cross-run merge.
        '''
        return {
            team: dict(counts)
            for team, counts in sorted(self._teams.items())
        }

    @classmethod
    def from_dict(cls, data):
        '''Rebuild an aggregate from :meth:`to_dict` output.'''
        aggregate = cls()
        for team, counts in data.items():
            histogram = aggregate._teams.setdefault(team, Counter())
            for score, count in counts.items():
                histogram[int(score)] += int(count)
        return aggregate

    def _histogram(self, team=None, ranks=None):
        if team is not None:
            sources = [(team, self._teams.get(team, Counter()))]
        else:
            sources = self._teams.items()
        histogram = Counter()
        for _, counts in sources:
            for complexity, count in counts.items():
                if _rank_allowed(complexity, ranks):
                    histogram[complexity] += count
        return histogram
