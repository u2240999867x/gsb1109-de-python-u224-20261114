'''Explain the difference between two Maintainability Index (MI) scores.

The MI computed by :func:`radon.metrics.mi_compute` is a single number, so a
code health report that only prints two scores (an "old" and a "new" one)
cannot say *why* the score moved. This module keeps, for each of the two
revisions, the original inputs and the measurement convention used to derive
them, and then re-computes the contribution of every operand one at a time.

The decomposition is a sequential (path) decomposition: starting from the old
operands, each operand is replaced by its new value in a pre-defined order and
the MI is re-computed after every replacement. The difference between two
consecutive results is the contribution of the operand that was replaced.
Because the last state is exactly the new state, the contributions telescope
and sum back to the observed total change, even when the zero-line branch is
taken or the final value is truncated by the 0/100 clamps. A different
replacement order can be supplied, which produces a different but equally
valid decomposition of the same total change.

When an old operand or the comment-counting convention was not recorded, the
affected factor is explicitly marked as unattributable instead of guessing a
contribution for it.
'''

import math
from collections import OrderedDict, namedtuple

from radon.metrics import mi_compute, mi_parameters
from radon.raw import analyze

# The canonical replacement order. It mirrors the operand order of
# :func:`radon.metrics.mi_compute`: Halstead volume, cyclomatic complexity,
# SLOC and comment percentage.
MI_FACTORS = ('halstead_volume', 'complexity', 'sloc', 'comments')

FACTOR_LABELS = OrderedDict([
    ('halstead_volume', 'Halstead volume'),
    ('complexity', 'Cyclomatic complexity'),
    ('sloc', 'SLOC (logical lines of code)'),
    ('comments', 'Comment percentage'),
])

# The constants and bounds of the MI formula, stored together with every
# snapshot so the measurement convention (计量口径) is reproducible.
MI_FORMULA = OrderedDict([
    ('base', 171.0),
    ('volume_coefficient', 5.2),
    ('complexity_coefficient', 0.23),
    ('sloc_coefficient', 16.2),
    ('comment_coefficient', 50.0),
    ('comment_radians_factor', 2.46),
    ('normalization_divisor', 171.0),
    ('lower_bound', 0.0),
    ('upper_bound', 100.0),
])

# Floating-point tolerance used when checking that a decomposition closes.
CLOSURE_TOLERANCE = 1e-12


class MISnapshot(object):
    '''An immutable record of everything needed to reproduce one MI score.

    It stores the original source (when available), the four MI operands, the
    raw counts from which the comment percentage was derived, and the
    measurement convention (the ``multi`` flag and the formula constants).
    '''

    def __init__(self, code, multi, halstead_volume, complexity, sloc,
                 comments, comment_lines=None, raw_sloc=None, raw=None,
                 formula=None):
        self.code = code
        # ``multi`` may be ``None`` when the comment-counting convention of a
        # historical record is unknown.
        self.multi = multi
        self.halstead_volume = halstead_volume
        self.complexity = complexity
        self.sloc = sloc
        self.comments = comments
        self.comment_lines = comment_lines
        self.raw_sloc = raw_sloc
        self.raw = raw
        self.formula = OrderedDict(
            formula if formula is not None else MI_FORMULA)
        self.mi = mi_compute(halstead_volume, complexity, sloc, comments)

    @classmethod
    def from_code(cls, code, multi=True):
        '''Build a snapshot from source code and a comment convention.'''
        ast_parameters = mi_parameters(code, multi)
        volume, complexity, lloc, comments = ast_parameters
        raw = analyze(code)
        comment_lines = raw.comments + (raw.multi if multi else 0)
        return cls(
            code,
            bool(multi),
            volume,
            complexity,
            lloc,
            comments,
            comment_lines=comment_lines,
            raw_sloc=raw.sloc,
            raw=raw,
        )

    @classmethod
    def from_recorded(cls, operands, multi=None, code=None, raw=None):
        '''Rebuild a snapshot from recorded operands.

        Use this for historical revisions whose source is no longer available.
        ``operands`` is a mapping with the four keys of :data:`MI_FACTORS`;
        ``multi`` is the comment-counting convention (or ``None`` if it was
        not recorded) and ``raw`` the optional :class:`radon.raw.Module`.
        '''
        missing = [f for f in MI_FACTORS if f not in operands]
        if missing:
            raise ValueError('missing recorded operands: {0}'.format(missing))
        comment_lines = getattr(raw, 'comments', None)
        raw_sloc = getattr(raw, 'sloc', None)
        return cls(
            code,
            multi,
            operands['halstead_volume'],
            operands['complexity'],
            operands['sloc'],
            operands['comments'],
            comment_lines=comment_lines,
            raw_sloc=raw_sloc,
            raw=raw,
        )

    @property
    def comment_convention_known(self):
        '''Whether the multiline-string comment convention was recorded.'''
        return self.multi is not None

    @property
    def on_zero_line_branch(self):
        '''Whether the special non-positive operand branch is active.'''
        return self.halstead_volume <= 0 or self.sloc <= 0

    def __repr__(self):
        return (
            'MISnapshot(mi={0!r}, multi={1!r}, operands=({2!r}, {3!r}, '
            '{4!r}, {5!r}))'.format(
                self.mi, self.multi, self.halstead_volume,
                self.complexity, self.sloc, self.comments,
            )
        )


# The result of evaluating the MI formula for one concrete operand state.
# ``nn_mi`` is the non-normalized MI (``None`` on the zero-line branch),
# ``pre_clamp`` the normalized value before truncation and ``mi`` the final
# value. ``clamped``/``bound``/``truncated`` describe whether truncation bit.
MIComputation = namedtuple('MIComputation', [
    'nn_mi', 'pre_clamp', 'mi', 'clamped', 'bound', 'truncated',
])


# One row of a decomposition. ``pre_clamp_contribution`` is the movement the
# factor produced *before* the 0/100 truncation, so the real contribution stays
# visible even when the final score is pinned at a bound.
Contribution = namedtuple('Contribution', [
    'factor',
    'label',
    'before_value',
    'after_value',
    'contribution',
    'pre_clamp_contribution',
    'attributable',
    'reason',
])


# The full decomposition of one old -> new comparison.
MIDecomposition = namedtuple('MIDecomposition', [
    'order',
    'total_change',
    'contributions',
    'residual',
    'attribution_complete',
])


def _volume_term(volume):
    return -MI_FORMULA['volume_coefficient'] * math.log(volume)


def _complexity_term(complexity):
    return -MI_FORMULA['complexity_coefficient'] * complexity


def _sloc_term(sloc):
    return -MI_FORMULA['sloc_coefficient'] * math.log(sloc)


def _comments_term(comments):
    radians = math.radians(comments)
    scale = math.sqrt(MI_FORMULA['comment_radians_factor'] * radians)
    return MI_FORMULA['comment_coefficient'] * math.sin(scale)


def _non_normalized_mi(volume, complexity, sloc, comments):
    '''Return the non-normalized MI, or ``None`` on the zero-line branch.'''
    if volume <= 0 or sloc <= 0:
        return None
    return (
        MI_FORMULA['base']
        + _volume_term(volume)
        + _complexity_term(complexity)
        + _sloc_term(sloc)
        + _comments_term(comments)
    )


def _finalize(nn_mi):
    '''Apply normalization and the 0/100 clamps, recording every step.'''
    if nn_mi is None:
        # The special branch in mi_compute pins the result at 100 regardless
        # of the operands; no normalized value exists.
        return MIComputation(None, None, MI_FORMULA['upper_bound'],
                             True, 'upper', False)
    pre_clamp = nn_mi * 100.0 / MI_FORMULA['normalization_divisor']
    if pre_clamp < MI_FORMULA['lower_bound']:
        return MIComputation(nn_mi, pre_clamp, MI_FORMULA['lower_bound'],
                             True, 'lower', True)
    if pre_clamp > MI_FORMULA['upper_bound']:
        return MIComputation(nn_mi, pre_clamp, MI_FORMULA['upper_bound'],
                             True, 'upper', True)
    return MIComputation(nn_mi, pre_clamp, pre_clamp, False, None, False)


def compute_mi_state(halstead_volume, complexity, sloc, comments):
    '''Evaluate the full MI pipeline for one operand state.'''
    nn_mi = _non_normalized_mi(halstead_volume, complexity, sloc, comments)
    return _finalize(nn_mi)


def snapshot_computation(snapshot):
    '''Re-compute the :class:`MIComputation` behind a snapshot.'''
    return compute_mi_state(
        snapshot.halstead_volume,
        snapshot.complexity,
        snapshot.sloc,
        snapshot.comments,
    )


def _validate_order(order):
    order = tuple(order)
    if sorted(order) != sorted(MI_FACTORS):
        raise ValueError(
            'order must be a permutation of {0}, got {1}'.format(
                MI_FACTORS, order)
        )
    return order


def _describe_step(previous, current):
    '''Explain how the clamps/branch behaved during one replacement.'''
    if previous.nn_mi is None and current.nn_mi is None:
        return 'zero-line branch active on both sides: score pinned at 100'
    if previous.nn_mi is None:
        return 'leaves the zero-line branch; score is no longer pinned at 100'
    if current.nn_mi is None:
        return 'enters the zero-line branch: score is pinned at 100'
    if not previous.clamped and not current.clamped:
        return 'continuous change, no truncation'
    parts = []
    for tag, computation in (('before', previous), ('after', current)):
        if computation.clamped:
            parts.append('{0} step truncated at the {1} bound ({2:g})'.format(
                tag, computation.bound, computation.mi,
            ))
        else:
            parts.append('{0} step unclamped'.format(tag))
    return '; '.join(parts)


def explain_mi_change(old, new, order=MI_FACTORS, unavailable_factors=()):
    '''Decompose the MI change between two :class:`MISnapshot` instances.

    :param old: the snapshot of the old revision.
    :param new: the snapshot of the new revision.
    :param order: the replacement order, i.e. a permutation of
        :data:`MI_FACTORS`. Defaults to the canonical formula order.
    :param unavailable_factors: factors whose old operand was not recorded;
        they are reported as unattributable and their change lands in the
        residual.
    :returns: an :class:`MIDecomposition` whose contributions sum back to
        ``new.mi - old.mi`` (with the residual absorbing anything that could
        not be attributed).
    '''
    order = _validate_order(order)
    unavailable = set(unavailable_factors)
    # An unrecorded comment convention makes the comment factor incomparable.
    if not (old.comment_convention_known and new.comment_convention_known):
        unavailable.add('comments')
    elif old.multi != new.multi:
        unavailable.add('comments')

    current = {factor: getattr(old, factor) for factor in MI_FACTORS}
    previous = compute_mi_state(
        current['halstead_volume'], current['complexity'],
        current['sloc'], current['comments'],
    )

    contributions = []
    for factor in order:
        before_value = current[factor]
        if factor in unavailable:
            reason = (
                'unattributable: old operand or comment convention '
                'not recorded'
            )
            contributions.append(Contribution(
                factor, FACTOR_LABELS[factor], before_value, None, 0.0, None,
                False, reason,
            ))
            continue

        after_value = getattr(new, factor)
        current[factor] = after_value
        computation = compute_mi_state(
            current['halstead_volume'], current['complexity'],
            current['sloc'], current['comments'],
        )
        contribution = computation.mi - previous.mi
        if previous.pre_clamp is None or computation.pre_clamp is None:
            pre_clamp_contribution = None
        else:
            pre_clamp_contribution = computation.pre_clamp - previous.pre_clamp
        contributions.append(Contribution(
            factor,
            FACTOR_LABELS[factor],
            before_value,
            after_value,
            contribution,
            pre_clamp_contribution,
            True,
            _describe_step(previous, computation),
        ))
        previous = computation

    total_change = new.mi - old.mi
    explained = sum(item.contribution for item in contributions)
    residual_value = total_change - explained
    closes = math.isclose(residual_value, 0.0, abs_tol=CLOSURE_TOLERANCE)
    if closes:
        residual = Contribution(
            'unattributed', 'Unattributed change', None, None, 0.0, None,
            True,
            'all change attributed; contributions close the total change',
        )
    else:
        residual = Contribution(
            'unattributed',
            'Unattributed change',
            None,
            None,
            residual_value,
            None,
            False,
            'unattributable: missing operand/convention records, so the '
            'remaining change cannot be assigned to a factor',
        )
    complete = closes and all(item.attributable for item in contributions)
    return MIDecomposition(
        tuple(order),
        total_change,
        tuple(contributions),
        residual,
        complete,
    )


def explain_mi_change_from_codes(old_code, new_code, old_multi=True,
                                 new_multi=True, order=MI_FACTORS,
                                 unavailable_factors=()):
    '''Convenience wrapper building both snapshots from source code.'''
    old = MISnapshot.from_code(old_code, old_multi)
    new = MISnapshot.from_code(new_code, new_multi)
    return explain_mi_change(
        old, new, order=order, unavailable_factors=unavailable_factors,
    )


def _format_number(value):
    if value is None:
        return 'n/a'
    return '{0:.6g}'.format(value)


def format_health_report(old, new, decomposition):
    '''Render a human-readable code health report for one comparison.

    The report contains the stored inputs and conventions of both revisions,
    the per-factor contributions in the chosen order, the pre-truncation
    contribution whenever a clamp was active, and a closure line proving the
    contributions add up to the total change.
    '''
    lines = []
    lines.append('Maintainability Index change report')
    lines.append('===================================')
    lines.append('MI old: {0:.6f}    MI new: {1:.6f}    total change: {2:+.6f}'
                 .format(old.mi, new.mi, decomposition.total_change))
    lines.append('Replacement order: {0}'.format(
        ' -> '.join(decomposition.order)))
    lines.append('')
    lines.append('Stored inputs and measurement conventions')
    lines.append('-----------------------------------------')
    lines.append('multiline strings counted as comments: old={0!r} new={1!r}'
                 .format(old.multi, new.multi))
    header = '{0:<24}{1:>16}{2:>16}'.format('factor', 'old', 'new')
    lines.append(header)
    for factor in MI_FACTORS:
        lines.append('{0:<24}{1:>16}{2:>16}'.format(
            FACTOR_LABELS[factor],
            _format_number(getattr(old, factor)),
            _format_number(getattr(new, factor)),
        ))
    lines.append('comment lines: old={0!r} new={1!r}'.format(
        old.comment_lines, new.comment_lines))
    lines.append('raw SLOC: old={0!r} new={1!r}'.format(
        old.raw_sloc, new.raw_sloc))
    lines.append('')
    lines.append('Contributions (recomputed in replacement order)')
    lines.append('-----------------------------------------------')
    for item in decomposition.contributions:
        lines.append('* {0}: {1:+.6f}'.format(item.label, item.contribution))
        lines.append('    values: {0} -> {1}'.format(
            _format_number(item.before_value),
            _format_number(item.after_value),
        ))
        if item.pre_clamp_contribution is not None and not math.isclose(
                item.pre_clamp_contribution, item.contribution,
                abs_tol=CLOSURE_TOLERANCE):
            lines.append('    contribution before 0/100 truncation: {0:+.6f}'
                         .format(item.pre_clamp_contribution))
        lines.append('    {0}{1}'.format(
            '' if item.attributable else 'UNATTRIBUTABLE - ', item.reason))
    lines.append('* {0}: {1:+.6f}'.format(
        decomposition.residual.label, decomposition.residual.contribution))
    if not decomposition.residual.attributable:
        lines.append('    {0}'.format(decomposition.residual.reason))
    lines.append('')
    explained = sum(item.contribution for item in decomposition.contributions)
    lines.append('Closure: {0:+.6f} (contributions) + {1:+.6f} (residual) '
                 '= {2:+.6f} (total change)'.format(
                     explained, decomposition.residual.contribution,
                     decomposition.total_change))
    lines.append('Attribution complete: {0}'.format(
        decomposition.attribution_complete))
    return '\n'.join(lines)
