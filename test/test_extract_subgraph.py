# SPDX-FileCopyrightText: 2025 pydot contributors
#
# SPDX-License-Identifier: MIT

"""Unit testing of subgraph extraction between sources and targets."""

from __future__ import annotations

import pytest

import pydot


def _edge_pairs(graph: pydot.Graph) -> list[tuple[str, str]]:
    return sorted(
        (str(edge.get_source()), str(edge.get_destination()))
        for edge in graph.get_edges()
    )


def _node_names(graph: pydot.Graph) -> list[str]:
    return sorted(node.get_name() for node in graph.get_nodes())


def test_extract_keeps_only_source_to_target_path() -> None:
    graph = pydot.graph_from_dot_data(
        "digraph G { s->a->b->t; a->dead->x; s->other; }"
    )[0]

    result = graph.extract_subgraph(["s"], ["t"])

    assert _edge_pairs(result) == [("a", "b"), ("b", "t"), ("s", "a")]
    assert _node_names(result) == ["a", "b", "s", "t"]


def test_extract_multiple_sources_and_targets() -> None:
    graph = pydot.graph_from_dot_data(
        "digraph G { s1->hub->t1; s2->hub->t2; junk->hub; }"
    )[0]

    result = graph.extract_subgraph(["s1", "s2"], ["t1", "t2"])

    assert _edge_pairs(result) == [
        ("hub", "t1"),
        ("hub", "t2"),
        ("s1", "hub"),
        ("s2", "hub"),
    ]


def test_extract_feedback_cycle_keeps_contributing_part() -> None:
    graph = pydot.graph_from_dot_data(
        "digraph G { s->m->t; m->lp->m; lp->side; }"
    )[0]

    result = graph.extract_subgraph(["s"], ["t"])

    assert _edge_pairs(result) == [
        ("lp", "m"),
        ("m", "lp"),
        ("m", "t"),
        ("s", "m"),
    ]
    assert ("lp", "side") not in _edge_pairs(result)


def test_extract_cutoff_blocks_traversal_and_keeps_alternative() -> None:
    # The cutoff sits on a blocked branch leading nowhere useful, so it
    # is excluded together with the hidden part. The surviving route
    # around it is retained.
    graph = pydot.graph_from_dot_data(
        "digraph G { s->c->hidden->t2; s->x->t; }"
    )[0]

    result = graph.extract_subgraph(["s"], ["t"], ["c"])
    pairs = _edge_pairs(result)

    assert pairs == [("s", "x"), ("x", "t")]
    assert "c" not in _node_names(result)
    assert "hidden" not in _node_names(result)
    assert not any(src == "c" for src, _ in pairs)


def test_extract_cutoff_target_is_shown_as_boundary() -> None:
    graph = pydot.graph_from_dot_data("digraph G { s->c->hidden; s->x->t; }")[
        0
    ]

    result = graph.extract_subgraph(["s"], ["t", "c"], ["c"])
    pairs = _edge_pairs(result)

    assert ("s", "c") in pairs
    assert not any(src == "c" for src, _ in pairs)
    assert "hidden" not in _node_names(result)
    assert ("x", "t") in pairs


def test_extract_cutoff_uses_alternative_route() -> None:
    graph = pydot.graph_from_dot_data("digraph G { s->c->gone; s->a->t; }")[0]

    result = graph.extract_subgraph(["s"], ["t"], ["c"])

    assert _edge_pairs(result) == [("a", "t"), ("s", "a")]


def test_extract_cutoff_target_keeps_incoming_edge() -> None:
    graph = pydot.graph_from_dot_data("digraph G { s->c->after; }")[0]

    result = graph.extract_subgraph(["s"], ["c"], ["c"])

    assert _edge_pairs(result) == [("s", "c")]
    assert not any(src == "c" for src, _ in _edge_pairs(result))


def test_extract_cutoff_source_keeps_outgoing_boundary_edge() -> None:
    graph = pydot.graph_from_dot_data("digraph G { s->a->t; }")[0]

    result = graph.extract_subgraph(["s"], ["t"], ["s"])

    assert _edge_pairs(result) == [("a", "t"), ("s", "a")]


def test_extract_no_path_returns_empty_graph() -> None:
    graph = pydot.graph_from_dot_data("digraph G { s->a; b->t; }")[0]

    result = graph.extract_subgraph(["s"], ["t"])

    assert result.get_edges() == []
    assert _node_names(result) == []


def test_extract_unknown_node_raises() -> None:
    graph = pydot.graph_from_dot_data("digraph G { s->t; }")[0]

    with pytest.raises(pydot.Error):
        graph.extract_subgraph(["s"], ["missing"])
    with pytest.raises(pydot.Error):
        graph.extract_subgraph(["missing"], ["t"])
    with pytest.raises(pydot.Error):
        graph.extract_subgraph(["s"], ["t"], ["missing"])


def test_extract_undirected_prunes_dead_branches() -> None:
    graph = pydot.graph_from_dot_data("graph G { s--a--t; a--dead; }")[0]

    result = graph.extract_subgraph(["s"], ["t"])

    assert result.get_type() == "graph"
    assert _edge_pairs(result) == [("a", "t"), ("s", "a")]
    assert "dead" not in _node_names(result)


def test_extract_undirected_keeps_reachable_cycle() -> None:
    graph = pydot.graph_from_dot_data(
        "graph G { s--a--b--t; a--c--b; b--dang; }"
    )[0]

    pairs = _edge_pairs(graph.extract_subgraph(["s"], ["t"]))
    unordered = {frozenset(pair) for pair in pairs}

    assert frozenset(("a", "c")) in unordered
    assert frozenset(("b", "c")) in unordered
    assert frozenset(("a", "b")) in unordered
    assert not any("dang" in pair for pair in pairs)


def test_extract_undirected_cutoff() -> None:
    blocked = pydot.graph_from_dot_data("graph G { s--c--t; }")[0]
    blocked_result = blocked.extract_subgraph(["s"], ["t"], ["c"])
    assert blocked_result.get_edges() == []

    boundary = pydot.graph_from_dot_data("graph G { s--c--x; }")[0]
    boundary_result = boundary.extract_subgraph(["s"], ["c"], ["c"])
    assert _edge_pairs(boundary_result) == [("s", "c")]


def test_extract_preserves_attributes_and_original_graph() -> None:
    graph = pydot.graph_from_dot_data(
        "digraph G { s->a [color=red, penwidth=2]; a->t; a->z; }"
    )[0]
    original_edge_count = len(graph.get_edges())

    result = graph.extract_subgraph(["s"], ["t"])

    edge = result.get_edge("s", "a")[0]
    assert edge.get_attributes()["color"] == "red"
    assert edge.get_attributes()["penwidth"] == "2"
    assert len(graph.get_edges()) == original_edge_count
    assert ("a", "z") in _edge_pairs(graph)
    assert ("a", "z") not in _edge_pairs(result)


def test_extract_materializes_implicit_nodes_and_defaults() -> None:
    graph = pydot.graph_from_dot_data(
        "digraph G { node [shape=box]; s[label=Sales]; s->a->t; }"
    )[0]

    result = graph.extract_subgraph(["s"], ["t"])

    assert set(_node_names(result)) == {"a", "node", "s", "t"}
    assert result.get_node("s")[0].get("label") == "Sales"


def test_extract_accepts_node_objects_and_module_function() -> None:
    graph = pydot.graph_from_dot_data("digraph G { s->a->t; }")[0]

    result = graph.extract_subgraph([pydot.Node("s")], [pydot.Node("t")])
    assert _edge_pairs(result) == [("a", "t"), ("s", "a")]

    module_result = pydot.extract_subgraph(graph, ["s"], ["t"])
    assert _edge_pairs(module_result) == [("a", "t"), ("s", "a")]


def test_extract_preserves_graph_kind_and_name() -> None:
    dot = pydot.graph_from_dot_data("digraph G { s->a->t; }")[0]
    assert isinstance(dot.extract_subgraph(["s"], ["t"]), pydot.Dot)

    cluster = pydot.Cluster("team")
    cluster.add_edge(pydot.Edge("s", "a"))
    cluster.add_edge(pydot.Edge("a", "t"))

    result = cluster.extract_subgraph(["s"], ["t"])

    assert isinstance(result, pydot.Cluster)
    assert result.get_name() == "cluster_team"
    assert _edge_pairs(result) == [("a", "t"), ("s", "a")]


def test_extract_source_equals_target_keeps_reachable_cycle() -> None:
    graph = pydot.graph_from_dot_data("digraph G { s->a->s; s->b; }")[0]

    result = graph.extract_subgraph(["s"], ["s"])

    assert _edge_pairs(result) == [("a", "s"), ("s", "a")]
