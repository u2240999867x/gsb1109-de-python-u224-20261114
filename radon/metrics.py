'''Module holding functions related to miscellaneous metrics, such as Halstead
metrics or the Maintainability Index.
'''

import ast
import collections
import math

from radon.raw import analyze
from radon.visitors import ComplexityVisitor, HalsteadVisitor, HalsteadMetrics

# Halstead metrics
HalsteadReport = collections.namedtuple(
    'HalsteadReport',
    'h1 h2 N1 N2 vocabulary length '
    'calculated_length volume '
    'difficulty effort time bugs',
)

# `total` is a HalsteadReport for the entire scanned file, while `functions` is
# a list of `HalsteadReport`s for each function in the file.
Halstead = collections.namedtuple("Halstead", "total functions")


def h_visit(code):
    '''Compile the code into an AST tree and then pass it to
    :func:`~radon.metrics.h_visit_ast`.
    '''
    return h_visit_ast(ast.parse(code))


def h_visit_ast(ast_node):
    '''
    Visit the AST node using the :class:`~radon.visitors.HalsteadVisitor`
    visitor. The results are `HalsteadReport` namedtuples with the following
    fields:

        * h1: the number of distinct operators
        * h2: the number of distinct operands
        * N1: the total number of operators
        * N2: the total number of operands
        * h: the vocabulary, i.e. h1 + h2
        * N: the length, i.e. N1 + N2
        * calculated_length: h1 * log2(h1) + h2 * log2(h2)
        * volume: V = N * log2(h)
        * difficulty: D = h1 / 2 * N2 / h2
        * effort: E = D * V
        * time: T = E / 18 seconds
        * bugs: B = V / 3000 - an estimate of the errors in the implementation

    The actual return of this function is a namedtuple with the following
    fields:

        * total: a `HalsteadReport` namedtuple for the entire scanned file
        * functions: a list of `HalsteadReport`s for each toplevel function

    Nested functions are not tracked.
    '''
    visitor = HalsteadVisitor.from_ast(ast_node)
    total = halstead_visitor_report(visitor)
    functions = [
        (v.context, halstead_visitor_report(v))
        for v in visitor.function_visitors
    ]

    return Halstead(total, functions)


def merge_halstead_metrics(*batches):
    '''Merge Halstead data collected from independent traversals (for example
    separate files or batches of files) into a single
    :class:`~radon.visitors.HalsteadMetrics`.

    Total operator/operand counts are summed, since every occurrence counts
    towards the length. Vocabulary is de-duplicated with the explicit scope
    rules of a single traversal:

        * operators are shared program vocabulary, so equal operator names
          (e.g. ``Add`` appearing in both batches) count only once;
        * operands carry their ``(context, token)`` scope key, so the same
          name in two different functions stays distinct, while the same
          operand in the same scope is only counted once.

    Merging in any order yields the same result, and merging all batches of a
    code base is equivalent to traversing it in a single pass.
    '''
    metrics = HalsteadMetrics(0, 0, set(), set())
    for batch in batches:
        metrics = HalsteadMetrics(
            metrics.operators + batch.operators,
            metrics.operands + batch.operands,
            metrics.operators_seen | batch.operators_seen,
            metrics.operands_seen | batch.operands_seen,
        )
    return metrics


def halstead_metrics_report(metrics):
    '''Return a HalsteadReport computed from merged
    :class:`~radon.visitors.HalsteadMetrics` data.

    This is the batch-friendly counterpart of
    :func:`halstead_visitor_report`: the vocabulary (``h1``, ``h2``) is
    derived from the de-duplicated sets while the length (``N1``, ``N2``)
    comes from the accumulated occurrence counts.
    '''
    h1, h2 = len(metrics.operators_seen), len(metrics.operands_seen)
    N1, N2 = metrics.operators, metrics.operands
    h = h1 + h2
    N = N1 + N2
    if h1 and h2:
        length = h1 * math.log(h1, 2) + h2 * math.log(h2, 2)
    else:
        length = 0
    volume = N * math.log(h, 2) if h != 0 else 0
    difficulty = (h1 * N2) / float(2 * h2) if h2 != 0 else 0
    effort = difficulty * volume
    return HalsteadReport(
        h1,
        h2,
        N1,
        N2,
        h,
        N,
        length,
        volume,
        difficulty,
        effort,
        effort / 18.0,
        volume / 3000.0,
    )


def halstead_visitor_report(visitor):
    """Return a HalsteadReport from a HalsteadVisitor instance."""
    return halstead_metrics_report(visitor.to_metrics())


def mi_compute(halstead_volume, complexity, sloc, comments):
    '''Compute the Maintainability Index (MI) given the Halstead Volume, the
    Cyclomatic Complexity, the SLOC number and the number of comment lines.
    Usually it is not used directly but instead :func:`~radon.metrics.mi_visit`
    is preferred.
    '''
    if any(metric <= 0 for metric in (halstead_volume, sloc)):
        return 100.0
    sloc_scale = math.log(sloc)
    volume_scale = math.log(halstead_volume)
    comments_scale = math.sqrt(2.46 * math.radians(comments))
    # Non-normalized MI
    nn_mi = (
        171
        - 5.2 * volume_scale
        - 0.23 * complexity
        - 16.2 * sloc_scale
        + 50 * math.sin(comments_scale)
    )
    return min(max(0.0, nn_mi * 100 / 171.0), 100.0)


def mi_parameters(code, count_multi=True):
    '''Given a source code snippet, compute the necessary parameters to
    compute the Maintainability Index metric. These include:

        * the Halstead Volume
        * the Cyclomatic Complexity
        * the number of LLOC (Logical Lines of Code)
        * the percent of lines of comment

    :param multi: If True, then count multiline strings as comment lines as
        well. This is not always safe because Python multiline strings are not
        always docstrings.
    '''
    ast_node = ast.parse(code)
    raw = analyze(code)
    comments_lines = raw.comments + (raw.multi if count_multi else 0)
    comments = comments_lines / float(raw.sloc) * 100 if raw.sloc != 0 else 0
    return (
        h_visit_ast(ast_node).total.volume,
        ComplexityVisitor.from_ast(ast_node).total_complexity,
        raw.lloc,
        comments,
    )


def mi_visit(code, multi):
    '''Visit the code and compute the Maintainability Index (MI) from it.'''
    return mi_compute(*mi_parameters(code, multi))


def mi_rank(score):
    r'''Rank the score with a letter:

        * A if :math:`\text{score} > 19`;
        * B if :math:`9 < \text{score} \le 19`;
        * C if :math:`\text{score} \le 9`.
    '''
    return chr(65 + (9 - score >= 0) + (19 - score >= 0))
