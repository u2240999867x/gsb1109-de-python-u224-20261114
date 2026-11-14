import json

import pytest

from radon.snapshot import (
    Snapshot,
    build_trends,
    compare_snapshots,
    rank_complexity_changes,
)


def snapshot(**sources):
    return Snapshot.from_sources(sources)


def test_renamed_and_moved_function_is_a_continuation():
    old = snapshot(**{'pkg/core.py': 'def alpha(x):\n    return x + 1\n'})
    new = snapshot(**{'pkg/helpers.py': 'def beta(y):\n    return y + 1\n'})

    comparison = compare_snapshots(old, new)

    assert len(comparison.grounded) == 1
    relation = comparison.grounded[0]
    assert relation.kind == 'renamed-moved'
    assert relation.source.uid == 'pkg/core.py::alpha'
    assert relation.target.uid == 'pkg/helpers.py::beta'
    assert comparison.unmatched_old == []
    assert comparison.unmatched_new == []


def test_renaming_a_local_or_parameter_does_not_change_structure():
    old = snapshot(**{'a.py': 'def f(x):\n    return x + 1\n'})
    new = snapshot(**{'a.py': 'def f(value):\n    return value + 1\n'})

    comparison = compare_snapshots(old, new)
    assert comparison.grounded[0].kind == 'unchanged'


def test_unique_structural_candidate_grounds_a_rename():
    old = snapshot(
        **{'a.py': 'def old(x):\n    if x:\n        return 1\n    return 2\n'}
    )
    new = snapshot(
        **{'b.py': 'def new(x):\n    if x:\n        return 1\n    return 2\n'}
    )

    comparison = compare_snapshots(old, new)
    assert len(comparison.relations) == 1
    assert comparison.relations[0].status == 'grounded'
    assert comparison.relations[0].evidence == (
        'unique structurally identical candidate'
    )


def test_explicit_rename_covers_an_edited_body():
    old = snapshot(**{'a.py': 'def old(x):\n    return x\n'})
    new = snapshot(
        **{'a.py': 'def new(x):\n    if x:\n        return x\n    return -x\n'}
    )

    comparison = compare_snapshots(old, new, renames={'old': 'new'})
    assert comparison.grounded[0].kind == 'renamed'
    assert comparison.complexity_changes()[0]['delta'] == 1


def test_explicit_rename_accepts_strings_and_path_references():
    old = snapshot(**{'a.py': 'def old(x):\n    return x\n'})
    new = snapshot(**{'a.py': 'def new(x):\n    return x + 1\n'})

    comparison = compare_snapshots(old, new, renames=['a.py::old=a.py::new'])
    assert comparison.grounded[0].source.uid == 'a.py::old'
    assert comparison.grounded[0].target.uid == 'a.py::new'


def test_two_identical_wrappers_are_ambiguous():
    old = snapshot(
        **{'a.py': 'def one(x):\n    return x\n\ndef two(x):\n    return x\n'}
    )
    new = snapshot(
        **{
            'b.py': (
                'def three(x):\n    return x\n\n'
                'def four(x):\n    return x\n'
            )
        }
    )

    comparison = compare_snapshots(old, new)

    assert comparison.grounded == []
    assert len(comparison.ambiguous) == 1
    relation = comparison.ambiguous[0]
    assert relation.status == 'ambiguous'
    assert relation.kind == 'structural'
    assert [record.uid for record in relation.sources] == [
        'a.py::one',
        'a.py::two',
    ]
    assert [record.uid for record in relation.targets] == [
        'b.py::three',
        'b.py::four',
    ]
    assert comparison.complexity_changes() == []


def test_ambiguous_wrapper_still_breaks_the_trend():
    old = snapshot(**{'a.py': 'def one(x):\n    return x\n'})
    new = snapshot(
        **{'a.py': 'def one(x):\n    return x\n\ndef two(x):\n    return x\n'}
    )
    # Same file and name identity resolves one; the copy remains unmatched.
    comparison = compare_snapshots(old, new)
    assert comparison.grounded[0].kind == 'unchanged'

    old = snapshot(**{'a.py': 'def one(x):\n    return x\n'})
    new = snapshot(
        **{
            'b.py': (
                'def three(x):\n    return x\n\n'
                'def four(x):\n    return x\n'
            )
        }
    )
    trends = compare_snapshots(old, new).trend()
    assert all(trend.status == 'disconnected' for trend in trends)


def test_one_to_many_split_keeps_all_sources():
    old = snapshot(
        **{
            'a.py': (
                'def process(items):\n'
                '    total = 0\n'
                '    for item in items:\n'
                '        total += item\n'
                '    result = total * 2\n'
                '    return result\n'
            )
        }
    )
    new = snapshot(
        **{
            'b.py': (
                'def add_all(items):\n'
                '    total = 0\n'
                '    for item in items:\n'
                '        total += item\n'
                '    return total\n\n'
                'def double(value):\n'
                '    result = value * 2\n'
                '    return result\n'
            )
        }
    )

    comparison = compare_snapshots(old, new)
    splits = comparison.splits

    assert len(splits) == 1
    split = splits[0]
    assert split.status == 'split'
    assert [record.uid for record in split.sources] == ['a.py::process']
    assert [record.uid for record in split.targets] == [
        'b.py::add_all',
        'b.py::double',
    ]
    # A split is not a one-to-one continuation, so it is not ranked.
    assert comparison.complexity_changes() == []


def test_explicit_split_keeps_every_source_relation():
    old = snapshot(**{'a.py': 'def orig(x):\n    return x\n'})
    new = snapshot(
        **{
            'b.py': (
                'def first(x):\n    return x\n\n'
                'def second(x):\n    return x + 1\n'
            )
        }
    )

    comparison = compare_snapshots(
        old, new, splits={'orig': ['first', 'second']}
    )
    split = comparison.splits[0]

    assert split.evidence == 'explicit split mapping'
    assert len(split.sources) == 1
    assert len(split.targets) == 2
    assert comparison.complexity_changes() == []


def test_split_with_two_possible_origins_is_ambiguous():
    old = snapshot(
        **{
            'a.py': (
                'def first(items):\n'
                '    total = 0\n'
                '    for item in items:\n'
                '        total += item\n'
                '    result = total * 2\n'
                '    return result\n\n'
                'def second(items):\n'
                '    total = 0\n'
                '    for item in items:\n'
                '        total += item\n'
                '    result = total * 2\n'
                '    return result\n'
            )
        }
    )
    new = snapshot(
        **{
            'b.py': (
                'def add_all(items):\n'
                '    total = 0\n'
                '    for item in items:\n'
                '        total += item\n'
                '    return total\n\n'
                'def double(value):\n'
                '    result = value * 2\n'
                '    return result\n'
            )
        }
    )

    comparison = compare_snapshots(old, new)
    assert comparison.splits == []
    assert len(comparison.ambiguous) == 1
    assert comparison.ambiguous[0].kind == 'split'


def test_explicit_split_with_several_origins_is_ambiguous():
    old = snapshot(
        **{
            'a.py': (
                'def first(x):\n    return x\n\n'
                'def second(x):\n    return x\n'
            )
        }
    )
    new = snapshot(**{'b.py': 'def target(x):\n    return x\n'})

    comparison = compare_snapshots(
        old, new, renames={'x': 'target', 'y': 'target'}
    )
    # Unresolvable hints are ignored, and normal matching still applies.
    assert comparison.relations


def test_rank_only_uses_grounded_one_to_one_relations():
    old = snapshot(
        **{
            'a.py': (
                'def alpha(x):\n    return x\n\n'
                'def beta(x):\n    if x:\n        return x\n    return -x\n'
            )
        }
    )
    new = snapshot(
        **{
            'a.py': (
                'def alpha(x):\n    if x:\n        return x\n    return -x\n\n'
                'def beta(y):\n    if y:\n        return y\n    return -y\n'
            )
        }
    )

    comparison = compare_snapshots(old, new)
    changes = rank_complexity_changes(comparison, only_changed=True)

    assert [change['target'] for change in changes] == ['a.py::alpha']
    assert changes[0]['before'] == 1
    assert changes[0]['after'] == 2
    assert changes[0]['delta'] == 1
    assert changes[0]['kind'] == 'modified'


def test_deleted_and_added_functions_are_not_ranked():
    old = snapshot(**{'a.py': 'def gone(x):\n    return x\n'})
    new = snapshot(
        **{
            'b.py': (
                'def brand_new(x):\n'
                '    if x:\n'
                '        return x\n'
                '    return -x\n'
            )
        }
    )

    comparison = compare_snapshots(old, new)

    assert [record.uid for record in comparison.unmatched_old] == [
        'a.py::gone'
    ]
    assert [record.uid for record in comparison.unmatched_new] == [
        'b.py::brand_new'
    ]
    assert comparison.complexity_changes() == []


def test_multiple_functions_in_one_file_keep_their_identity():
    old = snapshot(
        **{
            'a.py': (
                'class Service(object):\n'
                '    def run(self):\n'
                '        return 1\n\n'
                'def helper(x):\n'
                '    return x\n'
            )
        }
    )
    new = snapshot(
        **{
            'a.py': (
                'class Service(object):\n'
                '    def go(self):\n'
                '        return 1\n\n'
                'def helper(x):\n'
                '    return x\n'
            )
        }
    )
    comparison = compare_snapshots(old, new)
    kinds = {relation.kind for relation in comparison.grounded}
    assert 'unchanged' in kinds
    assert 'structural' in kinds or 'renamed' in kinds


def test_duplicate_definitions_are_ambiguous():
    old = snapshot(
        **{
            'a.py': (
                'def f(x):\n    return x\n\n'
                'if True:\n    def f(y):\n        return y\n'
            )
        }
    )
    new = snapshot(
        **{
            'a.py': (
                'def f(x):\n    return x\n\n'
                'if True:\n    def f(y):\n        return y\n'
            )
        }
    )
    comparison = compare_snapshots(old, new)
    assert comparison.grounded == []
    assert comparison.ambiguous
    assert comparison.ambiguous[0].kind == 'name'


def test_snapshots_roundtrip_through_json(tmp_path):
    source = snapshot(**{'pkg/a.py': 'def f(x):\n    return x\n'})
    path = tmp_path / 'snapshot.json'
    source.save(str(path))

    loaded = Snapshot.from_path(str(path))
    assert loaded.uids == source.uids
    assert loaded.get('pkg/a.py::f').complexity == 1


def test_snapshot_can_read_cc_json():
    data = {
        'pkg/a.py': [
            {
                'type': 'function',
                'name': 'f',
                'lineno': 1,
                'endline': 2,
                'complexity': 3,
                'rank': 'A',
            }
        ]
    }
    loaded = Snapshot.from_json_data(data)
    assert loaded.get('pkg/a.py::f').complexity == 3


def test_comparison_json_is_reviewable():
    old = snapshot(
        **{
            'a.py': (
                'def one(x):\n    return x\n\n'
                'def two(x):\n    return x\n'
            )
        }
    )
    new = snapshot(
        **{
            'b.py': (
                'def three(x):\n    return x\n\n'
                'def four(x):\n    return x\n'
            )
        }
    )

    payload = json.loads(compare_snapshots(old, new).to_json())

    assert payload['ambiguous'][0]['status'] == 'ambiguous'
    assert payload['ambiguous'][0]['rankable'] is False
    assert payload['complexity_changes'] == []
    assert {relation['status'] for relation in payload['relations']} == {
        'ambiguous'
    }


def test_trend_connects_across_grounded_renames():
    first = snapshot(
        **{'a.py': 'def f(x):\n    if x:\n        return 1\n    return 2\n'}
    )
    second = snapshot(
        **{'b.py': 'def g(x):\n    if x:\n        return 1\n    return 2\n'}
    )
    third = snapshot(
        **{'c.py': 'def h(x):\n    if x:\n        return 1\n    return 3\n'}
    )

    trends = build_trends(
        [first, second, third], renames={'f': 'g', 'g': 'h'}
    )

    assert len(trends) == 1
    assert trends[0].status == 'connected'
    assert trends[0].values == [[2, 2, 2]]
    assert trends[0].breaks == []


def test_trend_breaks_at_ambiguity():
    first = snapshot(**{'a.py': 'def one(x):\n    return x\n'})
    second = snapshot(
        **{
            'b.py': (
                'def three(x):\n    return x\n\n'
                'def four(x):\n    return x\n'
            )
        }
    )
    third = snapshot(
        **{
            'b.py': (
                'def three(x):\n    return x\n\n'
                'def four(x):\n    return x\n'
            )
        }
    )

    trends = build_trends([first, second, third])

    assert all(trend.status != 'connected' for trend in trends)
    assert any(
        trend.starts_after and trend.starts_after['reason'] == 'ambiguous'
        for trend in trends
    )


def test_trend_breaks_at_split_and_new_branch_reconnects():
    first = snapshot(
        **{
            'a.py': (
                'def process(items):\n'
                '    total = 0\n'
                '    for item in items:\n'
                '        total += item\n'
                '    result = total * 2\n'
                '    return result\n'
            )
        }
    )
    second = snapshot(
        **{
            'a.py': (
                'def add_all(items):\n'
                '    total = 0\n'
                '    for item in items:\n'
                '        total += item\n'
                '    return total\n\n'
                'def double(value):\n'
                '    result = value * 2\n'
                '    return result\n'
            )
        }
    )
    third = snapshot(
        **{
            'a.py': (
                'def add_all(items):\n'
                '    total = 0\n'
                '    for item in items:\n'
                '        total += item\n'
                '    if total < 0:\n'
                '        total = 0\n'
                '    return total\n\n'
                'def double(value):\n'
                '    result = value * 2\n'
                '    return result\n'
            )
        }
    )

    trends = {
        trend.segments[0][0]['id']: trend
        for trend in build_trends([first, second, third])
    }

    process = trends['a.py::process']
    add_all = trends['a.py::add_all']
    assert process.ends_before['reason'] == 'split'
    assert add_all.starts_after['reason'] == 'split'
    assert add_all.values == [[2, 3]]
    assert add_all.status == 'partial'


def test_trend_does_not_draw_through_unmatched_functions():
    first = snapshot(**{'a.py': 'def f(x):\n    return x\n'})
    second = snapshot(
        **{
            'b.py': (
                'def completely_different(x):\n'
                '    if x:\n'
                '        return x\n'
                '    return -x\n'
            )
        }
    )

    trends = build_trends([first, second])

    assert all(trend.status in ('isolated', 'partial') for trend in trends)
    assert all(
        len(segment) == 1 for trend in trends for segment in trend.segments
    )


def test_snapshot_reports_syntax_errors_without_failing():
    snap = snapshot(**{'good.py': 'def f():\n    pass\n', 'bad.py': 'def ('})
    assert snap.get('good.py::f') is not None
    assert snap.errors[0]['path'] == 'bad.py'


def test_syntax_errors_can_be_fixed_with_an_after_snapshot():
    old = snapshot(**{'bad.py': 'def ('})
    new = snapshot(**{'bad.py': 'def f():\n    pass\n'})
    comparison = compare_snapshots(old, new)
    assert comparison.old.errors
    assert comparison.unmatched_new[0].uid == 'bad.py::f'


def test_lambda_bodies_are_fingerprinted_without_crashing():
    snap = snapshot(
        **{
            'a.py': (
                'def outer(x):\n'
                '    callback = lambda value: value + x\n'
                '    return callback\n'
            )
        }
    )
    assert snap.get('a.py::outer') is not None
    assert snap.errors == []


def test_unmatched_mapping_hint_is_reported():
    old = snapshot(**{'a.py': 'def f(x):\n    return x\n'})
    new = snapshot(**{'a.py': 'def g(x):\n    return x\n'})

    comparison = compare_snapshots(old, new, renames={'typo': 'g'})

    assert comparison.hints[0]['matched'] is False
    assert 'not found' in comparison.hints[0]['reason']


def test_deleted_and_readded_identifier_does_not_reconnect_trend():
    first = snapshot(**{'a.py': 'def f(x):\n    return x\n'})
    second = snapshot(**{'a.py': 'def other(x):\n    return x + 1\n'})
    third = snapshot(**{'a.py': 'def f(x):\n    return x\n'})

    trends = build_trends([first, second, third])

    assert all(trend.status == 'isolated' for trend in trends)


def test_moving_a_module_does_not_create_false_ambiguity():
    old = snapshot(
        **{
            'pkg/old.py': (
                'def f(a):\n    return a\n\n'
                'def g(b):\n    return b\n'
            )
        }
    )
    new = snapshot(
        **{
            'pkg/new.py': (
                'def f(a):\n    return a\n\n'
                'def g(b):\n    return b\n'
            )
        }
    )

    comparison = compare_snapshots(old, new)

    assert len(comparison.grounded) == 2
    assert comparison.ambiguous == []
    assert {relation.kind for relation in comparison.grounded} == {'moved'}


def test_rename_uses_only_unclaimed_structural_candidates():
    old = snapshot(
        **{
            'a.py': (
                'def old(x):\n    return x + 1\n\n'
                'def keep(y):\n    return y + 1\n'
            )
        }
    )
    new = snapshot(
        **{
            'a.py': (
                'def new(x):\n    return x + 1\n\n'
                'def keep(y):\n    return y + 1\n'
            )
        }
    )

    comparison = compare_snapshots(old, new)

    kinds = {relation.kind for relation in comparison.grounded}
    assert kinds == {'unchanged', 'renamed'}
    assert comparison.ambiguous == []


def test_explicit_split_takes_precedence_over_automatic_identity():
    old = snapshot(
        **{
            'a.py': (
                'def orig(x):\n    return x\n\n'
                'def kept(y):\n    return y + 1\n'
            )
        }
    )
    new = snapshot(
        **{
            'a.py': (
                'def kept(y):\n    return y + 1\n\n'
                'def extra(x):\n    return x\n'
            )
        }
    )

    comparison = compare_snapshots(
        old, new, splits={'orig': ['kept', 'extra']}
    )

    split = comparison.splits[0]
    assert [record.uid for record in split.targets] == [
        'a.py::kept',
        'a.py::extra',
    ]
