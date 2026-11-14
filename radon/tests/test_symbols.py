import json
import textwrap

import pytest

from radon.complexity import cc_visit
from radon.symbols import (
    CLASS_AGGREGATE,
    FUNCTION_OWN,
    METHOD_OWN,
    symbol_records,
    symbol_table,
    symbols_from_blocks,
    symbols_from_visitor,
)
from radon.visitors import ComplexityVisitor

dedent = lambda code: textwrap.dedent(code).strip()

HIERARCHY = '''
class A:
    def run(self):
        def run():
            if flag:
                return 1
            return 0
        return run

class B:
    def run(self):
        return 1
'''


def test_hierarchy_records():
    table = symbol_table(dedent(HIERARCHY), 'sample.py')
    assert [r.qualname for r in table] == [
        'A',
        'A.run',
        'A.run.run',
        'B',
        'B.run',
    ]
    assert [r.scope for r in table] == ['', 'A', 'A.run', '', 'B']
    assert [r.kind for r in table] == [
        'class',
        'method',
        'function',
        'class',
        'method',
    ]
    assert [r.order for r in table] == [1, 2, 3, 4, 5]
    assert [r.depth for r in table] == [0, 1, 2, 0, 1]
    assert table[2].parent_id == table[1].symbol_id
    assert table[1].parent_id == table[0].symbol_id
    assert len({r.symbol_id for r in table}) == len(table)


def test_line_ranges():
    table = symbol_table(dedent(HIERARCHY))
    expected = {
        'A': (1, 7),
        'A.run': (2, 7),
        'A.run.run': (3, 6),
        'B': (9, 11),
        'B.run': (10, 11),
    }
    assert {r.qualname: r.line_range for r in table} == expected


def test_metric_levels():
    table = symbol_table(dedent(HIERARCHY))
    by_name = {r.qualname: r for r in table}
    assert by_name['A'].metric_level == CLASS_AGGREGATE
    assert by_name['A'].is_aggregate is True
    assert by_name['A.run'].metric_level == METHOD_OWN
    assert by_name['A.run.run'].metric_level == FUNCTION_OWN
    assert by_name['B.run'].measurement_level == 'method'
    assert by_name['A.run.run'].measurement_level == 'function'
    assert by_name['A'].measurement_level == 'class'


def test_class_aggregate_and_method_own_are_distinct():
    table = symbol_table(dedent(HIERARCHY))
    by_name = {r.qualname: r for r in table}
    # A has a nested closure inside its method: the class aggregate is not
    # the method's own score and neither is a sum of the other records.
    assert by_name['A'].complexity == 2
    assert by_name['A'].real_complexity == 2
    assert by_name['A.run'].complexity == 1
    assert by_name['A.run.run'].complexity == 2
    assert by_name['A'].metric_level != by_name['A.run'].metric_level
    assert by_name['A'].aggregate_complexity == 2
    assert by_name['A'].own_complexity is None
    assert by_name['A.run'].aggregate_complexity is None
    assert by_name['A.run'].own_complexity == 1


def test_aggregate_and_own_values_never_summed():
    table = symbol_table(dedent(HIERARCHY))
    aggregates = [r.complexity for r in table if r.is_aggregate]
    own = [r.complexity for r in table if not r.is_aggregate]
    assert aggregates == [2, 2]
    assert own == [1, 2, 1]
    # The module total must not be the naive sum of every record.
    assert sum(aggregates) + sum(own) != table.total_complexity


REPEATED = '''
def same():
    return 1

def other():
    return 2

def same():
    if flag:
        return 3
    return 4
'''


def test_repeated_definitions_are_kept():
    table = symbol_table(dedent(REPEATED))
    names = [r.qualname for r in table]
    assert names == ['same', 'other', 'same']
    same = [r for r in table if r.name == 'same']
    assert len(same) == 2
    assert same[0].symbol_id != same[1].symbol_id
    assert [r.occurrence for r in same] == [1, 2]
    assert [r.order for r in same] == [1, 3]
    assert [r.line_range for r in same] == [(1, 2), (7, 10)]
    # Scope order counts every definition in the scope, not only collisions.
    assert [r.scope_order for r in table] == [1, 2, 3]
    assert [r.complexity for r in same] == [1, 2]


def test_module_total_does_not_double_count_nesting():
    table = symbol_table(dedent(HIERARCHY))
    visitor = ComplexityVisitor.from_code(dedent(HIERARCHY))

    # The class aggregate already contains the method own complexity, and the
    # method already contains neither its closure nor the class aggregate.
    assert table.legacy_total_complexity == visitor.total_complexity
    # A.run's closure is the only contribution the traditional total misses.
    assert table.nested_complexity == 1
    assert table.total_complexity == visitor.total_complexity + 1


def test_deep_nesting_contributes_once():
    code = dedent(
        '''
        def outer():
            def inner():
                if a:
                    pass
            if b:
                pass
        '''
    )
    table = symbol_table(code)
    visitor = ComplexityVisitor.from_code(code)
    assert table.legacy_total_complexity == visitor.total_complexity
    assert table.nested_complexity == 1
    assert table.total_complexity == visitor.total_complexity + 1


def test_as_dict_is_json_serialisable():
    table = symbol_table(dedent(HIERARCHY), 'sample.py')
    payload = json.loads(json.dumps(table.as_dict()))
    assert payload['module'] == 'sample.py'
    assert len(payload['records']) == len(table)
    record = payload['records'][1]
    assert record['qualified_name'] == 'A.run'
    assert record['definition_order'] == 2
    assert record['metric_level'] == METHOD_OWN
    assert record['rank'] == 'A'


def test_blocks_and_visitor_inputs():
    code = dedent(HIERARCHY)
    from_blocks = symbols_from_blocks(cc_visit(code), 'sample.py')
    from_visitor = symbols_from_visitor(
        ComplexityVisitor.from_code(code), 'sample.py'
    )
    from_code = symbol_table(code, 'sample.py')
    for table in (from_blocks, from_visitor):
        assert [r.qualname for r in table] == [r.qualname for r in from_code]
        assert [r.metric_level for r in table] == [
            r.metric_level for r in from_code
        ]
        assert (
            table.legacy_total_complexity
            == from_code.legacy_total_complexity
        )


def test_symbol_records_accepts_code_and_ast():
    import ast

    code = dedent(HIERARCHY)
    assert symbol_records(code) == symbol_records(ast.parse(code))


def test_invalid_input():
    with pytest.raises(TypeError):
        symbol_table(42)
