import textwrap

import pytest

from radon.complexity import cc_visit
from radon.dashboard import (
    MODULE_BLOCK_NAME,
    ChangeRecord,
    Exemption,
    QualityDashboard,
    attribute_changes,
)


def dedent(code):
    return textwrap.dedent(code).strip() + '\n'


OUTER_INNER_OLD = dedent(
    '''
    def outer():
        def inner():
            return 1
        return inner()
    '''
)

OUTER_INNER_NEW = dedent(
    '''
    def outer():
        def inner():
            return 2
        return inner()
    '''
)

CLASS_CODE = dedent(
    '''
    class Foo:
        bar = 1

        def method(self, x):
            if x:
                return x
            return 0
    '''
)

COMPLEX_FUNC = dedent(
    '''
    def complex_func(a, b, c):
        if a:
            if b:
                if c:
                    return 1
        return 0
    '''
)

MOVED_INNER_OLD = dedent(
    '''
    def outer():
        def inner(a, b, c):
            if a:
                if b:
                    if c:
                        return 1
            return 0
        return inner(1, 2, 3)
    '''
)

MOVED_INNER_NEW = dedent(
    '''
    def outer():
        x = 1
        y = 2
        def inner(a, b, c):
            if a:
                if b:
                    if c:
                        return 1
            return 0
        return inner(1, 2, 3)
    '''
)

BLOATED_OLD = dedent(
    '''
    def bloated(a, b, c):
        if a:
            if b:
                if c:
                    return 1
        return 0


    def simple():
        return 1


    def kept():
        return 42
    '''
)

BLOATED_NEW = dedent(
    '''
    def kept():
        return 42
    '''
)


def test_added_lines_attributed_to_innermost_block():
    new_blocks = cc_visit(OUTER_INNER_NEW)
    # Line 3 (`return 2`) belongs to `inner`, line 4 (`return inner()`)
    # belongs to `outer` itself.
    records = attribute_changes([3, 4], [], new_blocks, [])
    by_name = {r.name: r for r in records}
    assert by_name['outer.inner'].added_lines == {3}
    assert by_name['outer.inner'].kind == 'function'
    # The inner function's change is not reported on the outer one.
    assert by_name['outer'].added_lines == {4}
    assert len(records) == 2


def test_deleted_lines_attributed_using_old_version():
    old_blocks = cc_visit(OUTER_INNER_OLD)
    records = attribute_changes([], [3], [], old_blocks)
    (record,) = records
    assert record.name == 'outer.inner'
    assert record.deleted_lines == {3}
    assert record.is_removal


def test_method_changes_not_double_reported_on_class():
    blocks = cc_visit(CLASS_CODE)
    # Line 5 (`if x:`) is inside the method, line 2 (`bar = 1`) is
    # class-level.
    records = attribute_changes([5, 2], [], blocks, [])
    by_name = {r.name: r for r in records}
    assert by_name['Foo.method'].added_lines == {5}
    assert by_name['Foo.method'].kind == 'method'
    assert by_name['Foo'].added_lines == {2}
    assert by_name['Foo'].kind == 'class'
    # Each line is reported exactly once.
    assert sum(r.changed_lines for r in records) == 2


def test_module_level_lines_get_module_record():
    records = attribute_changes([1], [], [], [])
    (record,) = records
    assert record.name == MODULE_BLOCK_NAME
    assert record.kind == 'module'
    assert record.added_lines == {1}


def test_added_and_deleted_lines_merge_into_single_record():
    old_blocks = cc_visit(OUTER_INNER_OLD)
    new_blocks = cc_visit(OUTER_INNER_NEW)
    records = attribute_changes([3], [3], new_blocks, old_blocks)
    (record,) = records
    assert record.name == 'outer.inner'
    assert record.added_lines == {3}
    assert record.deleted_lines == {3}
    assert not record.is_addition
    assert not record.is_removal
    assert record.changed_lines == 2


def test_exemption_versions_normalization():
    exemption = Exemption('f', 1, 2, 'v1')
    assert exemption.versions == frozenset({'v1'})
    exemption = Exemption('f', 1, 2, ['v1', 'v2'], reason='legacy')
    assert exemption.versions == frozenset({'v1', 'v2'})
    assert exemption.reason == 'legacy'


def test_violation_exempted_only_with_matching_range_and_version():
    dashboard = QualityDashboard(
        3,
        exemptions=[
            Exemption('complex_func', 1, 6, versions={'v2'}, reason='legacy')
        ],
    )
    report = dashboard.compare(
        None, COMPLEX_FUNC, added_lines=[1], new_version='v2'
    )
    assert report.violations[0].complexity == 4
    assert report.violations[0].exempted
    assert report.active_violations == []
    assert report.exempted_violations[0].name == 'complex_func'
    # The same exemption is not valid for another version.
    report = dashboard.compare(
        None, COMPLEX_FUNC, added_lines=[1], new_version='v3'
    )
    assert [v.name for v in report.active_violations] == ['complex_func']


def test_moved_inner_function_cannot_reuse_exemption():
    exemption = Exemption('outer.inner', 2, 7, versions={'v2'})
    dashboard = QualityDashboard(3, exemptions=[exemption])
    report = dashboard.compare(
        MOVED_INNER_OLD,
        MOVED_INNER_NEW,
        added_lines=[2, 3],
        old_version='v1',
        new_version='v2',
    )
    # `outer.inner` moved from lines 2-7 to lines 4-9: the exemption of
    # its original position no longer applies.
    assert [v.name for v in report.active_violations] == ['outer.inner']
    # An exemption on the new position does apply.
    dashboard = QualityDashboard(
        3, exemptions=[Exemption('outer.inner', 4, 9, versions={'v2'})]
    )
    report = dashboard.compare(
        MOVED_INNER_OLD,
        MOVED_INNER_NEW,
        added_lines=[2, 3],
        old_version='v1',
        new_version='v2',
    )
    assert report.active_violations == []
    assert [v.name for v in report.exempted_violations] == ['outer.inner']


def test_removed_over_threshold_function_is_explained():
    dashboard = QualityDashboard(3)
    report = dashboard.compare(
        BLOATED_OLD,
        BLOATED_NEW,
        deleted_lines=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
        old_version='v1',
        new_version='v2',
    )
    # The only over-threshold block that disappeared is `bloated`.
    assert [r.name for r in report.removals] == ['bloated']
    record = report.explain_removal('bloated')
    assert record.complexity == 4
    assert record.threshold == 3
    assert record.old_version == 'v1'
    assert record.new_version == 'v2'
    assert 'v1' in record.reason and 'v2' in record.reason
    # `simple` was removed too, but it never violated the threshold.
    assert report.explain_removal('simple') is None
    assert report.explain_removal('kept') is None
    # The deletion is attributed to the deleted block in the old version.
    change = report.changes_for('bloated')
    assert change.deleted_lines == {1, 2, 3, 4, 5, 6}
    assert change.is_removal


def test_surviving_block_is_not_a_removal():
    dashboard = QualityDashboard(3)
    report = dashboard.compare(
        COMPLEX_FUNC, COMPLEX_FUNC, old_version='v1', new_version='v2'
    )
    assert report.removals == []
    assert [v.name for v in report.active_violations] == ['complex_func']


def test_report_changes_for():
    dashboard = QualityDashboard(10)
    report = dashboard.compare(
        OUTER_INNER_OLD,
        OUTER_INNER_NEW,
        added_lines=[3],
        deleted_lines=[3],
        old_version='v1',
        new_version='v2',
    )
    record = report.changes_for('outer.inner')
    expected = ChangeRecord('outer.inner', 'function')
    expected.added_lines.add(3)
    expected.deleted_lines.add(3)
    assert record == expected
    assert report.changes_for('missing') is None
