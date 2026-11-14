import textwrap

from radon.metrics import (
    halstead_metrics_report,
    halstead_visitor_report,
    h_visit,
    merge_halstead_metrics,
)
from radon.visitors import HalsteadVisitor


def dedent(code):
    return textwrap.dedent(code).strip()


def _metrics(code):
    '''Collect the mergeable Halstead data for one batch of source code.'''
    return HalsteadVisitor.from_code(dedent(code)).to_metrics()


def _merged_report(*codes):
    '''Analyze every batch independently, then merge and report.'''
    return halstead_metrics_report(
        merge_halstead_metrics(*[_metrics(code) for code in codes])
    )


def _full_report(code):
    '''The reference result obtained from a single traversal.'''
    return h_visit(dedent(code)).total


def test_metrics_carry_explicit_scope_keys():
    '''Operators are plain names (shared vocabulary) while operands carry
    their ``(context, token)`` scope key.'''
    metrics = _metrics(
        '''
        a = b + c

        def f():
            y = x + 1
        '''
    )
    assert metrics.operators_seen == {'Add'}
    assert (None, 'b') in metrics.operands_seen
    assert (None, 'c') in metrics.operands_seen
    assert ('f', 'x') in metrics.operands_seen
    assert ('f', 1) in metrics.operands_seen


def test_shared_operator_is_deduplicated_across_batches():
    '''Both batches use addition: the operator occurrence counts add up
    (N1), but the vocabulary counts it only once (h1).'''
    first = '''
    a = b + c
    '''
    second = '''
    d = e + f
    '''
    report = _merged_report(first, second)
    assert report.h1 == 1
    assert report.N1 == 2
    assert report.h2 == 4
    assert report.N2 == 4
    assert report == _full_report(first + '\n' + second)


def test_same_named_locals_in_different_functions_stay_distinct():
    '''The local variable ``x`` belongs to two different functions, so the
    scope key keeps the two operands apart in the vocabulary.'''
    first = '''
    def f():
        return x + 1
    '''
    second = '''
    def g():
        return x + 2
    '''
    report = _merged_report(first, second)
    # f's x/1 and g's x/2 are four distinct scoped operands
    assert report.h2 == 4
    assert report.N2 == 4
    assert report == _full_report(first + '\n' + second)


def test_shared_top_level_operand_is_deduplicated():
    '''Outside functions, the same name in both batches is the same shared
    vocabulary entry and is counted only once.'''
    first = '''
    a = a + 1
    '''
    second = '''
    a = a + 2
    '''
    report = _merged_report(first, second)
    assert report.h1 == 1
    assert report.N1 == 2
    assert report.h2 == 3
    assert report.N2 == 4
    assert report == _full_report(first + '\n' + second)


def test_two_partitions_of_the_same_files_match_full_traversal():
    '''Splitting the same file list in two different ways and merging the
    batches must reproduce a single traversal over all the files.'''
    files = [
        '''
        total = a + b

        def f(x):
            return x + 1
        ''',
        '''
        def g(x):
            return x - 2

        value = g(3)
        ''',
        '''
        def f(x):
            return x * 2

        shared = total + value
        ''',
        '''
        other = shared ** 2
        mixed = other - value
        ''',
    ]
    batches = [_metrics(code) for code in files]

    # First split: first half vs second half.
    split_one = merge_halstead_metrics(
        merge_halstead_metrics(*batches[:2]),
        merge_halstead_metrics(*batches[2:]),
    )
    # Second split: interleaved files, merged one at a time.
    split_two = merge_halstead_metrics()
    for index in (0, 2, 1, 3):
        split_two = merge_halstead_metrics(split_two, batches[index])

    expected = _full_report('\n'.join(files))
    assert halstead_metrics_report(split_one) == expected
    assert halstead_metrics_report(split_two) == expected
    assert halstead_metrics_report(split_one) == (
        halstead_metrics_report(split_two)
    )


def test_empty_merge_matches_empty_traversal():
    '''Merging no batches yields the same all-zero report as scanning empty
    code in one pass.'''
    assert halstead_metrics_report(merge_halstead_metrics()) == (
        h_visit('').total
    )


def test_visitor_report_equivalent_to_metrics_report():
    '''The legacy visitor-based report and the merged-data report agree.'''
    code = '''
    a = b + c
    d = c - f

    def f(b):
        a = 2 - 4
        d = a + b
        return a ** d
    '''
    visitor = HalsteadVisitor.from_code(dedent(code))
    assert halstead_visitor_report(visitor) == (
        halstead_metrics_report(visitor.to_metrics())
    )
