# SPDX-FileCopyrightText: 2024 pydot contributors
#
# SPDX-License-Identifier: MIT

"""Tests for :mod:`pydot.lineage`."""

from __future__ import annotations

import pydot
from pydot.lineage import (
    Column,
    ColumnLineage,
    OperatorPortMapping,
    trace_lineage,
)


def _orders_graph() -> pydot.Dot:
    graph = pydot.Dot("orders", graph_type="digraph")
    graph.add_edge(pydot.Edge("orders:amount", "calc:amount"))
    graph.add_edge(pydot.Edge("orders:tax", "calc:tax"))
    graph.add_edge(pydot.Edge("calc:total", "report:total"))
    return graph


def test_amount_output_does_not_inherit_tax() -> None:
    graph = _orders_graph()
    graph.set_operator_mapping("calc", {"amount": ["total"]})

    result = graph.trace_lineage("report:total")

    assert result.source_names() == ["orders:amount"]
    assert result.is_complete
    assert result.unknown == ()


def test_both_inputs_reach_output_when_declared() -> None:
    graph = _orders_graph()
    graph.set_operator_mapping("calc", {"amount": ["total"], "tax": ["total"]})

    result = graph.trace_lineage("report:total")

    assert result.source_names() == ["orders:amount", "orders:tax"]


def test_unknown_boundary_without_mapping() -> None:
    graph = _orders_graph()

    result = graph.trace_lineage("report:total")

    assert result.sources == ()
    assert not result.is_complete
    assert len(result.unknown) == 1
    boundary = result.unknown[0]
    assert boundary.operator == "calc"
    assert boundary.output_port == "total"
    assert boundary.input_ports == ("amount", "tax")
    assert "no port mapping" in boundary.reason


def test_undeclared_output_is_unknown_boundary() -> None:
    graph = _orders_graph()
    graph.set_operator_mapping("calc", {"amount": []})

    result = graph.trace_lineage("report:total")

    assert result.sources == ()
    assert not result.is_complete
    assert result.unknown[0].input_ports == ("amount", "tax")


def test_declared_output_without_inputs_is_known_empty() -> None:
    graph = _orders_graph()
    graph.set_operator_mapping(
        "calc", OperatorPortMapping.from_outputs({"total": []})
    )

    result = graph.trace_lineage("report:total")

    assert result.sources == ()
    assert result.is_complete


def test_rewiring_edges_changes_the_answer() -> None:
    graph = pydot.Dot("orders", graph_type="digraph")
    graph.add_edge(pydot.Edge("orders:amount", "calc:amount"))
    graph.add_edge(pydot.Edge("orders:tax", "calc:tax"))
    graph.add_edge(pydot.Edge("calc:total", "report:total"))
    graph.set_operator_mapping("calc", {"amount": ["total"], "tax": ["total"]})

    graph.del_edge("calc:total", "report:total")
    graph.add_edge(pydot.Edge("calc:total", "report:total"))

    before = graph.trace_lineage("report:total")
    assert before.source_names() == ["orders:amount", "orders:tax"]

    graph.del_edge("orders:tax", "calc:tax")
    graph.add_edge(pydot.Edge("orders:tax", "calc:amount"))

    after = graph.trace_lineage("report:total")
    assert after.source_names() == ["orders:amount", "orders:tax"]


def test_rewiring_output_to_another_port_changes_the_answer() -> None:
    graph = pydot.Dot("orders", graph_type="digraph")
    graph.add_edge(pydot.Edge("orders:amount", "calc:amount"))
    graph.add_edge(pydot.Edge("orders:tax", "calc:tax"))
    graph.add_edge(pydot.Edge("calc:total", "report:total"))
    graph.set_operator_mapping(
        "calc",
        {"amount": ["total"], "tax": ["tax_total"]},
    )

    assert graph.trace_lineage("report:total").source_names() == [
        "orders:amount"
    ]

    # Feed the report from calc's other output port instead.  The same
    # operator and the same edges now produce a different lineage.
    graph.del_edge("calc:total", "report:total")
    graph.add_edge(pydot.Edge("calc:tax_total", "report:total"))

    result = graph.trace_lineage("report:total")
    assert result.source_names() == ["orders:tax"]
    assert result.is_complete


def test_rewiring_input_to_another_port_changes_the_answer() -> None:
    graph = pydot.Dot("orders", graph_type="digraph")
    graph.add_edge(pydot.Edge("orders:amount", "calc:amount"))
    graph.add_edge(pydot.Edge("orders:tax", "calc:tax"))
    graph.add_edge(pydot.Edge("calc:total", "report:total"))
    graph.set_operator_mapping(
        "calc",
        {"amount": ["total"], "tax": ["tax_total"]},
    )

    graph.del_edge("orders:tax", "calc:tax")
    graph.add_edge(pydot.Edge("orders:tax", "calc:amount"))

    result = graph.trace_lineage("report:total")
    assert result.source_names() == ["orders:amount", "orders:tax"]


def test_multiple_paths_give_bounded_evidence() -> None:
    graph = pydot.Dot("orders", graph_type="digraph")
    graph.add_edge(pydot.Edge("orders:amount", "left:amount"))
    graph.add_edge(pydot.Edge("orders:tax", "right:tax"))
    graph.add_edge(pydot.Edge("left:out", "join:left"))
    graph.add_edge(pydot.Edge("right:out", "join:right"))
    graph.add_edge(pydot.Edge("join:total", "report:total"))
    graph.set_operator_mapping("left", {"amount": ["out"]})
    graph.set_operator_mapping("right", {"tax": ["out"]})
    graph.set_operator_mapping("join", {"left": ["total"], "right": ["total"]})

    result = graph.trace_lineage("report:total", max_evidence=1)

    assert result.source_names() == ["orders:amount", "orders:tax"]
    assert len(result.evidence) == 1
    assert result.evidence_truncated
    assert result.evidence[0].columns[0] == Column("report", "total")
    assert result.evidence[0].columns[-1] in result.sources


def test_multiple_paths_metadata_is_deterministic() -> None:
    graph = pydot.Dot("orders", graph_type="digraph")
    graph.add_edge(pydot.Edge("orders:amount", "join:left"))
    graph.add_edge(pydot.Edge("orders:tax", "join:right"))
    graph.add_edge(pydot.Edge("join:total", "report:total"))
    graph.set_operator_mapping("join", {"left": ["total"], "right": ["total"]})

    first = graph.trace_lineage("report:total")
    second = graph.trace_lineage("report:total")

    assert first == second
    assert len(first.evidence) == 2


def test_declaration_direction_inputs_to_outputs() -> None:
    graph = _orders_graph()
    graph.set_operator_mapping("calc", {"tax": ["total"]})

    result = graph.trace_lineage("report:total")

    assert result.source_names() == ["orders:tax"]


def test_declaration_direction_outputs_to_inputs() -> None:
    graph = _orders_graph()
    graph.set_operator_mapping(
        "calc", {"total": ["amount"]}, direction="outputs_to_inputs"
    )

    result = graph.trace_lineage("report:total")

    assert result.source_names() == ["orders:amount"]


def test_mapping_declared_at_operator_level() -> None:
    graph = _orders_graph()
    mapping = OperatorPortMapping.from_outputs({"total": ["tax"]})

    result = trace_lineage(graph, "report:total", mappings={"calc": mapping})

    assert result.source_names() == ["orders:tax"]


def test_direct_input_target_is_traced_upstream() -> None:
    graph = _orders_graph()
    graph.set_operator_mapping("calc", {"amount": ["total"]})

    result = graph.trace_lineage("calc:amount")

    assert result.source_names() == ["orders:amount"]


def test_cycle_terminates_at_unknown_boundary() -> None:
    graph = pydot.Dot("cycle", graph_type="digraph")
    graph.add_edge(pydot.Edge("a:out", "b:in"))
    graph.add_edge(pydot.Edge("b:out", "a:in"))

    # Only "a" declares a mapping; "b" leaves its output unexplained.
    graph.set_operator_mapping("a", {"in": ["out"]})

    result = graph.trace_lineage("b:out")

    assert result.sources == ()
    assert not result.is_complete
    assert [boundary.operator for boundary in result.unknown] == ["b"]
    assert result.evidence == ()


def test_cycle_with_mappings_terminates_without_sources() -> None:
    graph = pydot.Dot("cycle", graph_type="digraph")
    graph.add_edge(pydot.Edge("a:out", "b:in"))
    graph.add_edge(pydot.Edge("b:out", "a:in"))
    graph.set_operator_mapping("a", {"in": ["out"]})
    graph.set_operator_mapping("b", {"in": ["out"]})

    result = graph.trace_lineage("b:out")

    assert result.sources == ()
    assert result.is_complete


def test_column_parse() -> None:
    assert Column.parse("orders:amount") == Column("orders", "amount")
    assert Column.parse("orders") == Column("orders")
    assert str(Column("orders", "amount")) == "orders:amount"


def test_quoted_ids_and_compass_points() -> None:
    assert Column.parse('"my orders":"line:item"') == Column(
        "my orders", "line:item"
    )
    assert Column.parse("node:port:n") == Column("node", "port")


def test_graph_mapping_management() -> None:
    graph = _orders_graph()
    graph.set_operator_mapping("calc", {"amount": ["total"]})

    assert graph.get_operator_mapping("calc") is not None
    assert graph.remove_operator_mapping("calc")
    assert not graph.remove_operator_mapping("calc")
    assert graph.get_operator_mapping("calc") is None


def test_operator_mapping_reverse_lookup() -> None:
    mapping = OperatorPortMapping({"amount": ["total"], "tax": ["total"]})

    assert mapping.inputs_for("total") == ("amount", "tax")
    assert mapping.outputs_for("amount") == ("total",)


def test_duplicate_declaration_requires_replace() -> None:
    lineage = ColumnLineage(_orders_graph())
    lineage.declare("calc", {"amount": ["total"]})

    try:
        lineage.declare("calc", {"tax": ["total"]})
    except pydot.Error:
        pass
    else:  # pragma: no cover
        raise AssertionError("duplicate declaration should fail")

    lineage.declare("calc", {"tax": ["total"]}, replace=True)
    assert lineage.get_operator_mapping("calc") is not None


def test_unknown_boundary_records_the_witness_path() -> None:
    graph = _orders_graph()

    result = graph.trace_lineage("report:total")

    boundary = result.unknown[0]
    assert boundary.path[-1] == Column("calc", "total")
    assert boundary.path[0] == Column("report", "total")


def test_boundary_below_a_declared_operator_is_reported() -> None:
    graph = pydot.Dot("orders", graph_type="digraph")
    graph.add_edge(pydot.Edge("orders:amount", "upper:in"))
    graph.add_edge(pydot.Edge("middle:out", "upper:in"))
    graph.add_edge(pydot.Edge("orders:amount", "middle:in"))
    graph.set_operator_mapping("upper", {"in": ["out"]})
    # "middle" has no mapping: its output is a nested unknown boundary.

    result = graph.trace_lineage("upper:out")

    assert result.source_names() == ["orders:amount"]
    assert not result.is_complete
    assert [boundary.operator for boundary in result.unknown] == ["middle"]
    assert result.unknown[0].path[-1] == Column("middle", "out")


def test_text_round_trip_does_not_preserve_mappings() -> None:
    graph = _orders_graph()
    graph.set_operator_mapping("calc", {"amount": ["total"]})

    reparsed = pydot.graph_from_dot_data(graph.to_string())[0]

    # Mappings are Python-side metadata, not DOT syntax.  Tracing the
    # reparsed graph keeps the unknown boundary explicit.
    assert reparsed.get_operator_mapping("calc") is None
    assert not reparsed.trace_lineage("report:total").is_complete


def test_mapping_survives_pickling() -> None:
    import pickle

    graph = _orders_graph()
    graph.set_operator_mapping("calc", {"amount": ["total"]})

    restored = pickle.loads(pickle.dumps(graph))

    assert restored.trace_lineage("report:total").source_names() == [
        "orders:amount"
    ]


def test_max_evidence_zero_returns_sources_only() -> None:
    graph = _orders_graph()
    graph.set_operator_mapping("calc", {"amount": ["total"]})

    result = graph.trace_lineage("report:total", max_evidence=0)

    assert result.source_names() == ["orders:amount"]
    assert result.evidence == ()
    assert not result.evidence_truncated


def test_unknown_report_column_has_no_sources() -> None:
    graph = _orders_graph()

    # A completely absent node is not silently invented as a source.
    result = graph.trace_lineage("nowhere:missing")
    assert result.sources == ()
    assert result.is_complete

    # An existing node asked for an undeclared port is an unknown
    # boundary, because that node's inputs cannot be attributed to it.
    result = graph.trace_lineage("report:missing")
    assert result.sources == ()
    assert not result.is_complete
    assert result.unknown[0].operator == "report"


def test_edge_without_ports() -> None:
    graph = pydot.Dot("plain", graph_type="digraph")
    graph.add_edge(pydot.Edge("orders", "calc"))
    graph.add_edge(pydot.Edge("calc", "report"))
    graph.set_operator_mapping("calc", {None: [None]})

    result = graph.trace_lineage("report")

    assert result.source_names() == ["orders"]
    assert result.is_complete
