'''Harvesters for snapshot continuity reports and multi-snapshot trends.'''

import json

from radon.cli.colors import GREEN, RED, RESET
from radon.snapshot import Snapshot, build_trends, compare_snapshots


def _parse_mappings(values):
    '''Turn a list of ``old=new`` strings into an ordered mapping.'''
    if not values:
        return None
    pairs = []
    for value in values:
        if '=' not in value:
            raise ValueError(
                'expected a mapping of the form old=new, got {0!r}'.format(
                    value
                )
            )
        old, new = value.split('=', 1)
        targets = [
            target.strip() for target in new.split(',') if target.strip()
        ]
        if not targets:
            raise ValueError('empty target in mapping {0!r}'.format(value))
        if len(targets) == 1:
            pairs.append((old.strip(), targets[0]))
        else:
            pairs.append((old.strip(), targets))
    return pairs


class SnapshotHarvester(object):
    '''Compare two source snapshots and expose a reviewable report.

    The harvester mirrors :class:`~radon.cli.harvest.Harvester` closely,
    but it works on paths rather than file objects, because an old and a new
    snapshot may be directories, files, or saved JSON reports.
    '''

    def __init__(self, old, new, config):
        self.old = old
        self.new = new
        self.config = config
        self._comparison = None

    @property
    def comparison(self):
        if self._comparison is None:
            self._comparison = compare_snapshots(
                Snapshot.from_path(self.old),
                Snapshot.from_path(self.new),
                renames=_parse_mappings(
                    getattr(self.config, 'rename', None)
                ),
                splits=_parse_mappings(getattr(self.config, 'split', None)),
                no_assert=getattr(self.config, 'no_assert', False),
            )
        return self._comparison

    def _ranked_changes(self):
        changes = self.comparison.complexity_changes(
            only_changed=getattr(self.config, 'only_changed', False)
        )
        if not getattr(self.config, 'show_zero', True):
            changes = [
                change for change in changes if change['delta'] != 0
            ]
            for index, change in enumerate(changes, 1):
                change['rank'] = index
        return changes

    def as_dict(self):
        payload = self.comparison.as_dict()
        payload['complexity_changes'] = self._ranked_changes()
        payload['renames'] = [
            relation.as_dict()
            for relation in self.comparison.relations
            if relation.kind in ('renamed', 'renamed-moved', 'explicit')
        ]
        payload['splits'] = [
            relation.as_dict()
            for relation in self.comparison.relations
            if relation.status == 'split'
        ]
        payload['ambiguous'] = [
            relation.as_dict() for relation in self.comparison.ambiguous
        ]
        return payload

    def as_json(self):
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)

    def _format_relation(self, relation, marker):
        sources = ', '.join(
            '{0} ({1})'.format(record.uid, record.complexity)
            for record in relation.sources
        )
        targets = ', '.join(
            '{0} ({1})'.format(record.uid, record.complexity)
            for record in relation.targets
        )
        return '{0} {1} -> {2} [{3}{4}{5}]'.format(
            marker,
            sources or '<none>',
            targets or '<none>',
            RED if relation.status == 'ambiguous' else GREEN,
            relation.kind,
            RESET,
        )

    def to_terminal(self):
        changes = self._ranked_changes()
        if changes:
            yield 'Complexity changes (grounded one-to-one only):', (), {}
            for change in changes:
                marker = '+' if change['delta'] > 0 else (
                    '-' if change['delta'] < 0 else '='
                )
                color = RED if change['delta'] > 0 else (
                    GREEN if change['delta'] < 0 else ''
                )
                yield (
                    '  {0} {1} {2} -> {3} ({4}){5}',
                    (
                        change['rank'],
                        marker,
                        change['source'],
                        change['target'],
                        '{0:+d}'.format(change['delta']),
                        RESET if color else '',
                    ),
                    {},
                )
                yield (
                    '      {0} -> {1} [{2}] {3}',
                    (
                        change['before'],
                        change['after'],
                        '{0}->{1}'.format(
                            change['before_rank'], change['after_rank']
                        ),
                        change['kind'],
                    ),
                    {'indent': 2},
                )
        else:
            yield 'No rankable complexity changes.', (), {}

        renames = [
            relation
            for relation in self.comparison.relations
            if relation.kind in ('renamed', 'renamed-moved')
        ]
        if renames:
            yield 'Renames and moves:', (), {}
            for relation in renames:
                yield self._format_relation(relation, '~'), (), {'indent': 1}

        splits = self.comparison.splits
        if splits:
            yield 'Splits:', (), {}
            for relation in splits:
                yield self._format_relation(relation, '/'), (), {'indent': 1}

        extractions = [
            relation
            for relation in self.comparison.relations
            if relation.kind == 'explicit' and relation.status == 'grounded'
        ]
        if extractions:
            yield 'Explicit continuations:', (), {}
            for relation in extractions:
                yield self._format_relation(relation, '='), (), {'indent': 1}

        ambiguous = self.comparison.ambiguous
        if ambiguous:
            yield 'Ambiguous continuations (excluded from ranking):', (), {}
            for relation in ambiguous:
                yield self._format_relation(relation, '?'), (), {'indent': 1}
                yield '    {0}', (relation.evidence,), {'indent': 2}

        unmatched_hints = [
            hint for hint in self.comparison.hints if not hint['matched']
        ]
        if unmatched_hints:
            yield 'Unmatched mapping hints:', (), {}
            for hint in unmatched_hints:
                yield '  {0} -> {1}: {2}', (
                    hint['old'],
                    hint['new'],
                    hint['reason'],
                ), {}

        unmatched_old = self.comparison.unmatched_old
        unmatched_new = self.comparison.unmatched_new
        if unmatched_old or unmatched_new:
            yield 'No continuation found:', (), {}
            for record in unmatched_old:
                yield '- {0} ({1})', (record.uid, record.complexity), {
                    'indent': 1
                }
            for record in unmatched_new:
                yield '+ {0} ({1})', (record.uid, record.complexity), {
                    'indent': 1
                }


class TrendHarvester(object):
    '''Report function-complexity histories across several snapshots.'''

    def __init__(self, snapshots, config, labels=None):
        self.snapshots = snapshots
        self.config = config
        self.labels = labels

    def _trends(self):
        trends = build_trends(
            [
                snapshot
                if isinstance(snapshot, Snapshot)
                else Snapshot.from_path(snapshot)
                for snapshot in self.snapshots
            ],
            labels=self.labels,
            renames=_parse_mappings(getattr(self.config, 'rename', None)),
            splits=_parse_mappings(getattr(self.config, 'split', None)),
        )
        return trends

    def as_json(self):
        payload = {
            'labels': self.labels,
            'trends': [trend.as_dict() for trend in self._trends()],
        }
        payload['broken'] = [
            trend.as_dict()
            for trend in self._trends()
            if trend.status != 'grounded'
        ]
        return json.dumps(payload, indent=2, sort_keys=True)

    def to_terminal(self):
        trends = self._trends()
        connected = [
            trend for trend in trends if trend.status == 'connected'
        ]
        partial = [
            trend for trend in trends if trend.status == 'partial'
        ]
        broken = [
            trend for trend in trends if trend.status == 'broken'
        ]
        isolated = [
            trend for trend in trends if trend.status == 'isolated'
        ]
        if connected:
            yield 'Connected trends:', (), {}
            for trend in connected:
                yield '  {0}', (_trend_text(trend),), {}
            yield '', (), {}
        if partial:
            yield (
                'Partial lineages (connected only within their range):',
                (),
                {},
            )
            for trend in partial:
                yield '  {0}', (_trend_text(trend),), {}
            yield '', (), {}
        if broken:
            yield 'Broken trends:', (), {}
            for trend in broken:
                yield '  {0}', (_trend_text(trend),), {}
                for segment in trend.segments:
                    yield '    {0}', (
                        ', '.join(
                            '{0}:{1}'.format(
                                point['snapshot'], point['complexity']
                            )
                            for point in segment
                        ),
                    ), {'indent': 2}
            yield '', (), {}
        if isolated:
            yield 'Isolated points (no connected neighbour):', (), {}
            for trend in isolated:
                point = trend.segments[0][0]
                reasons = []
                if trend.starts_after is not None:
                    reasons.append(
                        'starts after {0}'.format(
                            trend.starts_after['reason']
                        )
                    )
                if trend.ends_before is not None:
                    reasons.append(
                        'ends before {0}'.format(
                            trend.ends_before['reason']
                        )
                    )
                if not reasons and trend.breaks:
                    reasons.append(trend.breaks[0]['reason'])
                suffix = ' ({0})'.format('; '.join(reasons)) if reasons else ''
                yield '  {0}:{1} {2}{3}', (
                    point['snapshot'],
                    point['id'],
                    point['complexity'],
                    suffix,
                ), {}


def _trend_text(trend):
    points = ', '.join(
        '{0}:{1}'.format(point['snapshot'], point['complexity'])
        for segment in trend.segments
        for point in segment
    )
    reasons = []
    if trend.starts_after is not None:
        reasons.append('starts after {0}'.format(trend.starts_after['reason']))
    if trend.ends_before is not None:
        reasons.append('ends before {0}'.format(trend.ends_before['reason']))
    suffix = ''
    if reasons:
        suffix = ' [{0}]'.format('; '.join(reasons))
    return '{0} -> {1}{2}'.format(
        trend.segments[0][0]['id'], points, suffix
    )
