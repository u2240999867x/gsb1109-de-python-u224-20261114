import ast
import itertools

import pytest

from radon.complexity import cc_visit
from radon.topk import (
    ModuleSummary,
    Symbol,
    TopKAnalyzer,
    derive_top_k,
    summarize_blocks,
    summarize_blocks_by_module,
    summarize_module,
    summarize_paths,
    top_k,
    top_k_from_blocks,
)
from radon.visitors import Function


def function(name, complexity, is_method=False, classname=None):
    return Function(name, 1, 0, 2, is_method, classname, [], complexity)


def identifiers(symbols):
    return [symbol.name for symbol in symbols]


def test_summarize_blocks_retains_top_ten_and_cutoff():
    blocks = [function('b{0}'.format(i), i) for i in range(15)]
    summary = summarize_blocks(blocks, name='mod')
    assert summary.total == 15
    assert summary.known_count == 10
    assert summary.unknown == 5
    assert summary.cutoff == 5
    assert not summary.complete
    assert identifiers(summary.symbols) == [
        'b14', 'b13', 'b12', 'b11', 'b10',
        'b9', 'b8', 'b7', 'b6', 'b5',
    ]


def test_summarize_blocks_complete_below_limit():
    blocks = [function('a', 3), function('b', 1)]
    summary = summarize_blocks(blocks, name='mod')
    assert summary.complete
    assert summary.cutoff is None
    assert summary.unknown == 0


def test_summarize_blocks_keeps_ties_at_cutoff():
    blocks = [function('a', 5), function('b', 5), function('c', 5)]
    summary = summarize_blocks(blocks, name='mod', limit=2)
    assert summary.known_count == 2
    assert summary.total == 3
    assert summary.cutoff == 5


def test_derive_is_independent_of_file_names():
    summaries = [
        ModuleSummary('z', [('one', 5)], total=2, cutoff=5),
        ModuleSummary('a', [('two', 5)], total=2, cutoff=5),
    ]
    result = derive_top_k(summaries, 1)
    assert {s.name for s in result.certain} == {'one', 'two'}
    assert {s.name for s in result.possible} == {'one', 'two'}
    assert result.remaining == 2
    # Changing the file names does not change which named symbols are known
    # to tie; the module identities are carried through unchanged.
    renamed = [
        ModuleSummary('x', [('one', 5)], total=2, cutoff=5),
        ModuleSummary('y', [('two', 5)], total=2, cutoff=5),
    ]
    renamed_result = derive_top_k(renamed, 1)
    assert set(renamed_result.certain_names) == set(result.certain_names)
    assert renamed_result.remaining == result.remaining


def test_known_tie_at_boundary_is_all_certain_when_unknowns_are_low():
    summaries = [
        ModuleSummary('a', [('top', 9), ('tie1', 7), ('tie2', 7)],
                      total=4, cutoff=1),
    ]
    result = derive_top_k(summaries, 2)
    assert identifiers(result.certain) == ['top', 'tie1', 'tie2']
    assert result.remaining_known == 0


def test_unknown_contender_at_boundary_narrows_after_restore():
    analyzer = TopKAnalyzer(2)
    analyzer.add_summary(
        ModuleSummary(
            'a',
            [('top', 9), ('low', 5)],
            total=3,
            cutoff=7,
        )
    )
    before = analyzer.derive()
    assert identifiers(before.certain) == ['top']
    assert {s.name for s in before.possible} == {'top', 'low'}
    assert before.unknown_possible == 1
    assert before.remaining == 2
    assert before.remaining_known == 1

    analyzer.restore_module('a', [('top', 9), ('low', 5), ('hidden', 2)])
    after = analyzer.derive()
    assert identifiers(after.certain) == ['top', 'low']
    assert {s.name for s in after.possible} == {'top', 'low'}
    assert after.unknown_possible == 0
    assert after.remaining == 0
    assert after.complete


def test_restored_tied_boundary_is_certain():
    summary = ModuleSummary(
        'a', [('top', 9), ('edge', 7)], total=3, cutoff=7
    )
    result = derive_top_k([summary], 2)
    assert identifiers(result.certain) == ['top', 'edge']
    assert result.unknown_possible == 1
    assert result.complete is False


def test_restored_result_matches_direct_radon_aggregation():
    sources = {
        'pkg/a.py': '''
def alpha(x):
    if x:
        return 1
    return 0


def beta(x):
    if x and x > 1:
        return 2
    return 0
''',
        'pkg/b.py': '''
class Thing(object):
    def one(self, x):
        if x:
            return 1
        return 0

    def two(self, x):
        for i in range(x):
            if i:
                return i
        return 0


def gamma(x):
    return 1 if x else 0
''',
    }
    blocks = {
        name: cc_visit(source) for name, source in sources.items()
    }
    summaries = [
        summarize_blocks(values, name=name, limit=2)
        for name, values in blocks.items()
    ]
    analyzer = TopKAnalyzer(3, summaries)
    partial = analyzer.derive()
    assert not partial.complete

    all_blocks = blocks
    analyzer.restore_module('pkg/a.py', all_blocks['pkg/a.py'])
    analyzer.restore_module('pkg/b.py', all_blocks['pkg/b.py'])
    result = analyzer.derive()
    assert result.complete
    assert result.certain == result.possible

    direct = top_k_from_blocks(
        {name: values for name, values in blocks.items()}, 3
    )
    assert sorted(
        (
            (symbol.module, symbol.name, symbol.score)
            for symbol in result.certain
        ),
        key=lambda item: (-item[2], item[0], item[1]),
    ) == sorted(
        (
            (symbol.module, symbol.name, symbol.score)
            for symbol in direct
        ),
        key=lambda item: (-item[2], item[0], item[1]),
    )
    assert {symbol.score for symbol in result.certain} == {
        symbol.score for symbol in direct
    }


def test_direct_top_k_includes_tie_at_boundary():
    blocks = [function('a', 8), function('b', 6), function('c', 6),
              function('d', 1)]
    assert identifiers(top_k_from_blocks(blocks, 2)) == ['a', 'b', 'c']
    assert identifiers(top_k_from_blocks(blocks, 3)) == ['a', 'b', 'c']


def test_exact_mode_excludes_tied_boundary_from_certain():
    summaries = [
        ModuleSummary('m', [('a', 8), ('b', 6), ('c', 6)], total=3)
    ]
    inclusive = derive_top_k(summaries, 2)
    exact = derive_top_k(summaries, 2, tie_mode='exact')
    assert identifiers(inclusive.certain) == ['a', 'b', 'c']
    assert exact.certain_names == ('a',)
    assert set(exact.possible_names) == {'a', 'b', 'c'}


def test_duplicate_symbol_names_remain_distinct():
    summaries = [
        ModuleSummary('a', [('run', 5)], total=2, cutoff=4),
        ModuleSummary('b', [('run', 5)], total=2, cutoff=4),
    ]
    result = derive_top_k(summaries, 1)
    assert len(result.uncertain) == 0
    assert {symbol.identifier for symbol in result.possible} == {
        'a:run', 'b:run'
    }


@pytest.mark.parametrize(
    'summaries,k,expected_certain,expected_possible',
    [
        (
            [('m', [('a', 10), ('b', 8), ('c', 8)], 5, 4)],
            2,
            {'a', 'b', 'c'},
            {'a', 'b', 'c'},
        ),
        (
            [
                ('m', [('a', 10), ('b', 8), ('c', 8)], 5, 8),
                ('other', [], 2, 9),
            ],
            2,
            {'a'},
            {'a', 'b', 'c'},
        ),
        (
            [('m', [('a', 10), ('b', 9), ('c', 1)], 4, 1)],
            2,
            {'a', 'b'},
            {'a', 'b'},
        ),
    ],
)
def test_certain_and_possible_bounds(
    summaries, k, expected_certain, expected_possible
):
    values = [
        ModuleSummary(name, entries, total=total, cutoff=cutoff)
        for name, entries, total, cutoff in summaries
    ]
    result = derive_top_k(values, k)
    assert {s.name for s in result.certain} == expected_certain
    assert {s.name for s in result.possible} == expected_possible


def brute_force(summaries, k, mode):
    """Enumerate every unseen-score assignment for small cases."""
    groups = []
    for name, known, unknown_count, cutoff in summaries:
        choices = []
        for assignment in itertools.product(
            range(0, cutoff + 1), repeat=unknown_count
        ):
            items = [(name + ':' + symbol, score) for symbol, score in known]
            items += [
                ('{0}:u{1}'.format(name, index), score)
                for index, score in enumerate(assignment)
            ]
            choices.append(items)
        groups.append(choices)

    certain = None
    possible = set()
    possible_unknown = set()
    for combination in itertools.product(*groups):
        items = sorted(
            (item for group in combination for item in group),
            key=lambda item: -item[1],
        )
        if len(items) <= k:
            selections = [set(item[0] for item in items)]
        else:
            threshold = items[k - 1][1]
            if mode == 'include':
                selections = [
                    set(name for name, score in items if score >= threshold)
                ]
            else:
                above = [
                    name for name, score in items if score > threshold
                ]
                tied = [
                    name for name, score in items if score == threshold
                ]
                selections = [
                    set(above) | set(chosen)
                    for chosen in itertools.combinations(
                        tied, k - len(above)
                    )
                ]
        local = set.intersection(*selections)
        certain = local if certain is None else certain & local
        possible |= set.union(*selections)
        possible_unknown |= {
            name for name in set.union(*selections) if ':u' in name
        }
    return certain or set(), possible, len(possible_unknown)


@pytest.mark.parametrize('mode', ['include', 'exact'])
def test_bounds_match_exhaustive_completions(mode):
    cases = [
        [('a', [('x', 3), ('y', 2)], 1, 2)],
        [('a', [('x', 5)], 2, 3), ('b', [('y', 5)], 1, 1)],
        [('a', [('x', 4), ('y', 4)], 2, 4)],
        [('a', [('x', 1)], 1, 1), ('b', [('y', 1)], 2, 1)],
    ]
    for case in cases:
        for k in range(1, 5):
            (
                expected_certain,
                expected_possible,
                expected_unknown,
            ) = brute_force(case, k, mode)
            values = [
                ModuleSummary(
                    name, known, total=len(known) + count, cutoff=cutoff
                )
                for name, known, count, cutoff in case
            ]
            result = derive_top_k(values, k, tie_mode=mode)
            certain = {symbol.identifier for symbol in result.certain}
            possible = {
                symbol.identifier
                for symbol in result.possible
            }
            expected_known_possible = {
                name for name in expected_possible if ':u' not in name
            }
            expected_known_certain = {
                name for name in expected_certain if ':u' not in name
            }
            assert certain == expected_known_certain
            assert possible == expected_known_possible
            assert result.unknown_possible == expected_unknown


def test_summarize_module_uses_radon_visitor():
    summary = summarize_module(
        '''
def f(x):
    if x:
        return 1
    return 0
''',
        name='m',
        limit=10,
    )
    assert summary.complete
    assert [(s.name, s.score) for s in summary.symbols] == [('f', 2)]


def test_summarize_blocks_by_module_groups_symbols():
    blocks = [
        Symbol('a', 'f', 2),
        Symbol('b', 'g', 4),
        Symbol('a', 'h', 1),
    ]
    summaries = {
        summary.name: summary
        for summary in summarize_blocks_by_module(blocks, limit=10)
    }
    assert summaries['a'].total == 2
    assert summaries['b'].total == 1
    assert summaries['a'].symbols[0].name == 'f'


def test_analyzer_accepts_dictionaries_and_tuples():
    analyzer = TopKAnalyzer(
        2,
        [
            {'module': 'a', 'symbols': [('x', 5)], 'total': 2,
             'cutoff': 5},
            ('b', [('y', 4)], 1, 4),
        ],
    )
    assert len(analyzer) == 2
    assert not analyzer.complete
    assert {s.name for s in analyzer.derive().possible} == {'x', 'y'}


def test_top_k_alias_defaults_to_ten():
    summaries = [ModuleSummary('m', [('x', 1)])]
    assert top_k(summaries, 10).certain_names == ('x',)


def test_summarize_paths_reads_python_files(tmp_path):
    (tmp_path / 'one.py').write_text('def a():\n    pass\n')
    nested = tmp_path / 'nested'
    nested.mkdir()
    (nested / 'two.py').write_text('def b():\n    pass\n')
    summaries = summarize_paths([str(tmp_path)], limit=10)
    assert [s.name for s in summaries] == [
        str(tmp_path / 'one.py'),
        str(nested / 'two.py'),
    ]


def test_invalid_summary_arguments():
    with pytest.raises(ValueError):
        ModuleSummary('m', [('a', 1)], total=0)
    with pytest.raises(ValueError):
        ModuleSummary('m', [], total=2)
    with pytest.raises(ValueError):
        ModuleSummary('m', [('a', 1)], total=2, complete=True)
    with pytest.raises(ValueError):
        derive_top_k([ModuleSummary('m', [('a', 1)], total=2, cutoff=1)], 0)
    with pytest.raises(ValueError):
        derive_top_k(
            [ModuleSummary('m', [('a', 2)], total=2, cutoff=2)],
            1,
            min_score=3,
        )
