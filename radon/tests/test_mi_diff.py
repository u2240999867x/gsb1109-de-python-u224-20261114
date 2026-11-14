import itertools
import math
import textwrap

import pytest

from radon.metrics import mi_compute, mi_parameters, mi_visit
from radon.raw import analyze
from radon.mi_diff import (
    CLOSURE_TOLERANCE,
    FACTOR_LABELS,
    MI_FACTORS,
    MISnapshot,
    compute_mi_state,
    explain_mi_change,
    explain_mi_change_from_codes,
    format_health_report,
)

dedent = lambda code: textwrap.dedent(code).strip()


first_mi = '''
     def f(a, b, c):
         return (a ** b) % c

     k = f(1, 2, 3)
     print(k ** 2 - 1)
'''

second_mi = '''
     class A(object):

         def __init__(self, n):
             # this is awesome
             self.n = sum(i for i in range(n) if i&1)

         def m(self, j):
             """Just compute it.
             Example.
             """
             if j > 421:
                 return (self.n + 2) ** j
             return (self.n - 2) ** j

     a = A(4)
     a.m(42)  # i don't know why, but it works
'''


def make_snapshots(multi=True):
    old = MISnapshot.from_code(dedent(first_mi), multi)
    new = MISnapshot.from_code(dedent(second_mi), multi)
    return old, new


def test_snapshot_from_code_matches_mi_visit():
    code = dedent(second_mi)
    snapshot = MISnapshot.from_code(code, True)
    assert snapshot.mi == mi_visit(code, True)
    volume, complexity, lloc, comments = mi_parameters(code, True)
    assert snapshot.halstead_volume == volume
    assert snapshot.complexity == complexity
    assert snapshot.sloc == lloc
    assert snapshot.comments == comments
    assert snapshot.multi is True
    raw = analyze(code)
    assert snapshot.raw == raw
    assert snapshot.comment_lines == raw.comments + raw.multi
    assert snapshot.raw_sloc == raw.sloc


def test_snapshot_stores_comment_convention():
    code = dedent(second_mi)
    counted = MISnapshot.from_code(code, True)
    not_counted = MISnapshot.from_code(code, False)
    assert counted.multi is True
    assert not_counted.multi is False
    assert counted.comments != not_counted.comments
    assert counted.comment_lines > not_counted.comment_lines
    assert not_counted.comment_lines == analyze(code).comments


def test_snapshot_from_recorded():
    operands = {
        'halstead_volume': 200, 'complexity': 10, 'sloc': 78, 'comments': 45,
    }
    snapshot = MISnapshot.from_recorded(operands, multi=False)
    assert snapshot.mi == mi_compute(200, 10, 78, 45)
    assert snapshot.multi is False
    assert snapshot.comment_convention_known is True
    assert snapshot.code is None
    assert snapshot.comment_lines is None


def test_snapshot_from_recorded_unknown_convention():
    operands = {
        'halstead_volume': 10, 'complexity': 1, 'sloc': 5, 'comments': 0,
    }
    snapshot = MISnapshot.from_recorded(operands, multi=None)
    assert snapshot.multi is None
    assert snapshot.comment_convention_known is False


def test_snapshot_from_recorded_requires_all_operands():
    with pytest.raises(ValueError):
        MISnapshot.from_recorded({'halstead_volume': 1, 'complexity': 1})


def test_snapshot_zero_line_branch_flag():
    empty = MISnapshot.from_code('', True)
    assert empty.on_zero_line_branch is True
    assert empty.mi == 100.0
    old, _ = make_snapshots()
    assert old.on_zero_line_branch is False


def test_compute_mi_state_matches_mi_compute():
    cases = [
        (10, 2, 5, 0.5),
        (200, 10, 78, 45),
        (0, 1, 2, 0),
        (10, 2, 0, 0),
        (10 ** 9, 200, 10 ** 6, 0),
        (1, 0, 1, 100),
    ]
    for operands in cases:
        state = compute_mi_state(*operands)
        assert state.mi == mi_compute(*operands)


def test_compute_mi_state_records_pipeline_steps():
    normal = compute_mi_state(200, 10, 78, 45)
    assert normal.clamped is False
    assert normal.truncated is False
    assert normal.bound is None
    assert normal.nn_mi is not None
    assert math.isclose(normal.pre_clamp, normal.nn_mi * 100.0 / 171.0)
    assert math.isclose(normal.mi, normal.pre_clamp)

    zero = compute_mi_state(0, 1, 2, 0)
    assert zero.nn_mi is None
    assert zero.pre_clamp is None
    assert zero.clamped is True
    assert zero.bound == 'upper'
    assert zero.truncated is False
    assert zero.mi == 100.0

    upper = compute_mi_state(1, 0, 1, 100)
    assert upper.clamped is True
    assert upper.truncated is True
    assert upper.bound == 'upper'
    assert upper.pre_clamp > 100.0
    assert upper.mi == 100.0

    lower = compute_mi_state(10 ** 9, 200, 10 ** 6, 0)
    assert lower.clamped is True
    assert lower.truncated is True
    assert lower.bound == 'lower'
    assert lower.pre_clamp < 0.0
    assert lower.mi == 0.0


def test_decomposition_closes_total_change():
    old, new = make_snapshots()
    decomposition = explain_mi_change(old, new)
    assert decomposition.order == MI_FACTORS
    expected_total = new.mi - old.mi
    assert math.isclose(decomposition.total_change, expected_total)
    explained = sum(item.contribution for item in decomposition.contributions)
    assert math.isclose(explained + decomposition.residual.contribution,
                        decomposition.total_change, abs_tol=CLOSURE_TOLERANCE)
    assert decomposition.residual.contribution == 0.0
    assert decomposition.attribution_complete is True
    assert len(decomposition.contributions) == len(MI_FACTORS)


def test_contributions_are_sequential_mi_steps():
    old, new = make_snapshots()
    decomposition = explain_mi_change(old, new)
    current = {factor: getattr(old, factor) for factor in MI_FACTORS}
    running_mi = old.mi
    for item in decomposition.contributions:
        current[item.factor] = item.after_value
        expected_mi = mi_compute(
            current['halstead_volume'], current['complexity'],
            current['sloc'], current['comments'],
        )
        assert math.isclose(running_mi + item.contribution, expected_mi)
        # Each factor is replaced exactly once, so its starting value is the
        # recorded old operand.
        assert item.before_value == getattr(old, item.factor)
        running_mi = expected_mi
    assert math.isclose(running_mi, new.mi)


def test_alternative_orders_keep_total_but_redistribute():
    old, new = make_snapshots()
    canonical = explain_mi_change(old, new)
    seen = set()
    for order in itertools.permutations(MI_FACTORS):
        decomposition = explain_mi_change(old, new, order=order)
        assert decomposition.order == order
        assert math.isclose(decomposition.total_change, canonical.total_change)
        explained = sum(
            item.contribution for item in decomposition.contributions)
        assert math.isclose(explained, decomposition.total_change,
                            abs_tol=CLOSURE_TOLERANCE)
        seen.add(tuple(round(item.contribution, 12)
                       for item in decomposition.contributions))
    # At least two orders must distribute the non-additive change differently.
    assert len(seen) > 1


def test_invalid_order_rejected():
    old, new = make_snapshots()
    with pytest.raises(ValueError):
        explain_mi_change(old, new, order=('sloc',))
    with pytest.raises(ValueError):
        explain_mi_change(old, new, order=MI_FACTORS + ('sloc',))


def test_zero_line_branch_attributes_real_change():
    old = MISnapshot.from_code('', True)
    new = MISnapshot.from_code(dedent(second_mi), True)
    assert old.mi == 100.0
    decomposition = explain_mi_change(old, new)
    explained = sum(item.contribution for item in decomposition.contributions)
    assert math.isclose(explained, decomposition.total_change,
                        abs_tol=CLOSURE_TOLERANCE)
    reasons = {
        item.factor: item.reason for item in decomposition.contributions
    }
    assert any('zero-line branch' in reason for reason in reasons.values())
    # Factors replaced while the branch still pins the score contribute zero.
    for item in decomposition.contributions:
        if 'active on both sides' in item.reason:
            assert item.contribution == 0.0
            assert item.pre_clamp_contribution is None
    assert decomposition.attribution_complete is True


def test_truncation_keeps_pre_clamp_contribution():
    old = MISnapshot.from_recorded(
        {'halstead_volume': 200, 'complexity': 10, 'sloc': 78, 'comments': 45},
        multi=True,
    )
    new = MISnapshot.from_recorded(
        {'halstead_volume': 1, 'complexity': 0, 'sloc': 1, 'comments': 100},
        multi=True,
    )
    assert compute_mi_state(1, 0, 1, 100).truncated is True
    decomposition = explain_mi_change(old, new)
    by_factor = {item.factor: item for item in decomposition.contributions}
    # The comment change is masked by the clamp: final contribution is zero,
    # but the real (pre-truncation) movement remains visible.
    comment_item = by_factor['comments']
    assert comment_item.contribution == 0.0
    assert comment_item.pre_clamp_contribution is not None
    assert not math.isclose(comment_item.pre_clamp_contribution, 0.0)
    assert 'truncated' in comment_item.reason
    explained = sum(item.contribution for item in decomposition.contributions)
    assert math.isclose(explained, decomposition.total_change,
                        abs_tol=CLOSURE_TOLERANCE)


def test_lower_clamp_decomposition_closes():
    old = MISnapshot.from_recorded(
        {'halstead_volume': 200, 'complexity': 10, 'sloc': 78, 'comments': 45},
        multi=True,
    )
    new = MISnapshot.from_recorded(
        {'halstead_volume': 10 ** 9, 'complexity': 200,
         'sloc': 10 ** 6, 'comments': 0},
        multi=True,
    )
    assert compute_mi_state(10 ** 9, 200, 10 ** 6, 0).bound == 'lower'
    decomposition = explain_mi_change(old, new)
    explained = sum(item.contribution for item in decomposition.contributions)
    assert math.isclose(explained, decomposition.total_change,
                        abs_tol=CLOSURE_TOLERANCE)


def test_unknown_comment_convention_is_unattributable():
    old = MISnapshot.from_recorded(
        {'halstead_volume': 41.5, 'complexity': 1, 'sloc': 4, 'comments': 0.0},
        multi=None,
    )
    new = MISnapshot.from_code(dedent(second_mi), True)
    decomposition = explain_mi_change(old, new)
    by_factor = {item.factor: item for item in decomposition.contributions}
    comment_item = by_factor['comments']
    assert comment_item.attributable is False
    assert comment_item.contribution == 0.0
    assert comment_item.after_value is None
    assert 'UNATTRIBUTABLE' not in comment_item.reason
    assert 'unattributable' in comment_item.reason
    assert all(by_factor[f].attributable is True
               for f in MI_FACTORS if f != 'comments')
    assert decomposition.attribution_complete is False
    # Even with an unattributable factor, the attributed steps close exactly.
    explained = sum(item.contribution for item in decomposition.contributions)
    assert math.isclose(explained + decomposition.residual.contribution,
                        decomposition.total_change, abs_tol=CLOSURE_TOLERANCE)


def test_explicitly_unavailable_factor_lands_in_residual_logic():
    old = MISnapshot.from_recorded(
        {'halstead_volume': 41.5, 'complexity': 1, 'sloc': 4, 'comments': 0.0},
        multi=True,
    )
    new = MISnapshot.from_code(dedent(second_mi), True)
    decomposition = explain_mi_change(old, new, unavailable_factors=('sloc',))
    by_factor = {item.factor: item for item in decomposition.contributions}
    assert by_factor['sloc'].attributable is False
    assert by_factor['sloc'].after_value is None
    assert decomposition.attribution_complete is False
    explained = sum(item.contribution for item in decomposition.contributions)
    assert math.isclose(explained + decomposition.residual.contribution,
                        decomposition.total_change, abs_tol=CLOSURE_TOLERANCE)


def test_residual_absorbs_sole_unattributed_driver():
    # When the only changing operand is unavailable, its whole movement must
    # surface in the residual rather than being silently dropped.
    old = MISnapshot.from_recorded(
        {'halstead_volume': 100, 'complexity': 5, 'sloc': 20,
         'comments': 10}, multi=True,
    )
    new = MISnapshot.from_recorded(
        {'halstead_volume': 100, 'complexity': 5, 'sloc': 80,
         'comments': 10}, multi=True,
    )
    decomposition = explain_mi_change(
        old, new, unavailable_factors=('sloc',))
    assert all(
        item.contribution == 0.0
        for item in decomposition.contributions
        if item.factor != 'sloc'
    )
    assert math.isclose(decomposition.residual.contribution,
                        decomposition.total_change, abs_tol=CLOSURE_TOLERANCE)
    assert decomposition.residual.attributable is False


def test_mismatched_comment_conventions_is_unattributable():
    old = MISnapshot.from_recorded(
        {'halstead_volume': 10, 'complexity': 1, 'sloc': 5, 'comments': 10},
        multi=True,
    )
    new = MISnapshot.from_recorded(
        {'halstead_volume': 12, 'complexity': 2, 'sloc': 6, 'comments': 20},
        multi=False,
    )
    decomposition = explain_mi_change(old, new)
    by_factor = {item.factor: item for item in decomposition.contributions}
    assert by_factor['comments'].attributable is False
    assert by_factor['halstead_volume'].attributable is True
    assert decomposition.attribution_complete is False


def test_from_codes_wrapper_equivalent_to_snapshots():
    old_code, new_code = dedent(first_mi), dedent(second_mi)
    direct = explain_mi_change(
        MISnapshot.from_code(old_code, True),
        MISnapshot.from_code(new_code, True),
    )
    wrapped = explain_mi_change_from_codes(old_code, new_code)
    assert wrapped.total_change == direct.total_change
    assert [c.contribution for c in wrapped.contributions] == \
        [c.contribution for c in direct.contributions]


def test_health_report_contains_inputs_conventions_and_closure():
    old, new = make_snapshots()
    decomposition = explain_mi_change(old, new)
    report = format_health_report(old, new, decomposition)
    assert 'Maintainability Index change report' in report
    assert 'Replacement order:' in report
    assert 'multiline strings counted as comments' in report
    for label in FACTOR_LABELS.values():
        assert label in report
    assert 'Closure:' in report
    assert 'Attribution complete: True' in report
    # The stored raw inputs are exposed.
    assert 'comment lines:' in report
    assert 'raw SLOC:' in report


def test_health_report_flags_unattributable_factor():
    old = MISnapshot.from_recorded(
        {'halstead_volume': 41.5, 'complexity': 1, 'sloc': 4, 'comments': 0.0},
        multi=None,
    )
    new = MISnapshot.from_code(dedent(second_mi), True)
    decomposition = explain_mi_change(old, new)
    report = format_health_report(old, new, decomposition)
    assert 'UNATTRIBUTABLE' in report
    assert 'Attribution complete: False' in report
