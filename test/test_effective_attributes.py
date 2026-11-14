# SPDX-FileCopyrightText: 2025 pydot contributors
#
# SPDX-License-Identifier: MIT

"""Tests for effective node attributes and their provenance."""

from __future__ import annotations

import pydot
from pydot import EffectiveAttribute, EffectiveAttributes


def _scenario() -> pydot.Dot:
    """Edge introduces A before the default changes; B comes after."""
    (graph,) = pydot.graph_from_dot_data(
        """
        digraph lineage {
            node [retention=old];
            A -> S;
            node [retention=new];
            A [owner=alice];
            B;
        }
        """
    )
    return graph


def test_edge_introduction_freezes_defaults() -> None:
    graph = _scenario()
    attrs = graph.get_effective_node_attributes("A")
    assert attrs.to_dict() == {"retention": "old", "owner": "alice"}
    assert attrs["retention"].value == "old"
    assert attrs["retention"].source == "default"
    assert attrs["owner"].value == "alice"
    assert attrs["owner"].source == "explicit"


def test_later_default_applies_to_new_nodes() -> None:
    graph = _scenario()
    b_attrs = graph.get_effective_node_attributes("B")
    assert b_attrs.to_dict() == {"retention": "new"}
    assert b_attrs["retention"].is_inherited


def test_node_appearing_in_edge_keeps_early_default() -> None:
    graph = _scenario()
    s_attrs = graph.get_effective_node_attributes("S")
    assert s_attrs.to_dict() == {"retention": "old"}
    a_attrs = graph.get_effective_node_attributes("A")
    assert a_attrs["retention"].inherited
    assert not a_attrs["retention"].explicit


def test_sources_are_distinguishable() -> None:
    graph = _scenario()
    attrs = graph.get_effective_node_attributes("A")
    assert attrs.provenance() == {"retention": "default", "owner": "explicit"}
    assert attrs.inherited() == {"retention": "old"}
    assert attrs.explicit() == {"owner": "alice"}
    assert attrs.get_source("retention") == "inherited"
    assert attrs.get_source("owner") == "set"


def test_plain_dict_equality_still_works() -> None:
    graph = _scenario()
    attrs = graph.get_effective_node_attributes("A")
    assert attrs == {"retention": "old", "owner": "alice"}
    assert attrs["retention"] == "old"
    assert attrs["retention"] == ("old", "default")


def test_effective_attributes_are_exported() -> None:
    graph = _scenario()
    attrs = graph.get_effective_attributes("A")
    assert isinstance(attrs, EffectiveAttributes)
    assert isinstance(attrs["owner"], EffectiveAttribute)


def test_node_effective_attributes_use_parent_graph() -> None:
    graph = _scenario()
    node = graph.get_node("A")[0]
    assert node.get_effective_attributes().to_dict() == {
        "retention": "old",
        "owner": "alice",
    }


def test_all_nodes_at_once() -> None:
    graph = _scenario()
    resolved = graph.get_effective_node_attributes()
    assert set(resolved) == {"A", "S", "B"}
    assert resolved["A"].to_dict() == {"retention": "old", "owner": "alice"}
    assert resolved["S"].to_dict() == {"retention": "old"}
    assert resolved["B"].to_dict() == {"retention": "new"}
    assert list(graph.iter_effective_attributes()) == list(resolved.items())


def test_explicit_attribute_beats_earlier_inherited_value() -> None:
    graph = _scenario()
    attrs = graph.get_effective_node_attributes("A")
    assert attrs["retention"].value == "old"
    assert attrs["retention"].explicit is False


def test_explicit_node_attribute_overrides_default() -> None:
    (graph,) = pydot.graph_from_dot_data(
        "digraph { node [retention=old]; A [retention=custom]; }"
    )
    attrs = graph.get_effective_node_attributes("A")
    assert attrs["retention"].value == "custom"
    assert attrs["retention"].source == "explicit"


def test_subgraph_captures_defaults_on_entry() -> None:
    (graph,) = pydot.graph_from_dot_data(
        """
        digraph {
            node [retention=old];
            subgraph cluster_c {
                C -> D;
            }
            node [retention=new];
            E;
        }
        """
    )
    assert graph.get_effective_node_attributes("C").to_dict() == {
        "retention": "old"
    }
    assert graph.get_effective_node_attributes("D").to_dict() == {
        "retention": "old"
    }
    assert graph.get_effective_node_attributes("E").to_dict() == {
        "retention": "new"
    }


def test_subgraph_local_defaults_do_not_leak_out() -> None:
    (graph,) = pydot.graph_from_dot_data(
        """
        digraph {
            node [retention=old];
            subgraph cluster_a {
                node [color=red];
                A;
                subgraph cluster_b {
                    B;
                    node [retention=deep, color=blue];
                    C;
                }
                D;
            }
            E;
        }
        """
    )
    assert graph.get_effective_node_attributes("A").to_dict() == {
        "retention": "old",
        "color": "red",
    }
    assert graph.get_effective_node_attributes("B").to_dict() == {
        "retention": "old",
        "color": "red",
    }
    assert graph.get_effective_node_attributes("C").to_dict() == {
        "retention": "deep",
        "color": "blue",
    }
    assert graph.get_effective_node_attributes("D").to_dict() == {
        "retention": "old",
        "color": "red",
    }
    assert graph.get_effective_node_attributes("E").to_dict() == {
        "retention": "old"
    }


def test_subgraph_query_is_scoped_to_its_nodes() -> None:
    (graph,) = pydot.graph_from_dot_data(
        """
        digraph {
            node [retention=old];
            subgraph cluster_a { A -> B; }
            node [retention=new];
            C;
        }
        """
    )
    subgraph = graph.get_subgraph("cluster_a")[0]
    assert set(subgraph.get_effective_node_attributes()) == {"A", "B"}
    assert subgraph.get_effective_node_attributes("A").to_dict() == {
        "retention": "old"
    }


def test_programmatic_construction_follows_insertion_order() -> None:
    graph = pydot.Dot(graph_type="digraph", graph_name="lineage")
    graph.set_node_defaults(retention="old")
    graph.add_edge(pydot.Edge("A", "B"))
    graph.set_node_defaults(retention="new", owner="bob")
    graph.add_node(pydot.Node("A", owner="alice"))
    graph.add_node(pydot.Node("C"))

    assert graph.get_effective_node_attributes("A").to_dict() == {
        "retention": "old",
        "owner": "alice",
    }
    assert graph.get_effective_node_attributes("B").to_dict() == {
        "retention": "old"
    }
    assert graph.get_effective_node_attributes("C").to_dict() == {
        "retention": "new",
        "owner": "bob",
    }


def test_subgraph_added_before_default_does_not_see_it() -> None:
    graph = pydot.Dot(graph_type="digraph")
    subgraph = pydot.Subgraph("cluster_a")
    subgraph.add_node(pydot.Node("X"))
    graph.add_subgraph(subgraph)
    graph.set_node_defaults(retention="late")
    assert graph.get_effective_node_attributes("X").to_dict() == {}


def test_provenance_metadata_points_at_source_statement() -> None:
    graph = _scenario()
    attrs = graph.get_effective_node_attributes("A")
    assert attrs["retention"].graph_name == "lineage"
    assert attrs["owner"].graph_name == "lineage"
    assert attrs["retention"].sequence is not None
    assert attrs["owner"].sequence is not None
    assert attrs["retention"].sequence != attrs["owner"].sequence


def test_effective_attribute_helpers() -> None:
    entry = EffectiveAttribute("retention", "old", "default")
    assert entry.get_value() == "old"
    assert entry.get_source() == "inherited"
    assert entry.as_dict() == {"value": "old", "source": "default"}
    assert entry.get("value") == "old"
    assert entry.get("missing") is None
    assert tuple(entry) == ("old", "default")
    assert entry == {"value": "old", "source": "inherited"}
    assert repr(entry) == (
        "EffectiveAttribute(name='retention', value='old', source='default')"
    )


def test_unknown_node_yields_empty_attributes() -> None:
    graph = _scenario()
    attrs = graph.get_effective_node_attributes("missing")
    assert attrs.to_dict() == {}
    assert attrs.node_name == "missing"
    assert pydot.Dot().get_effective_node_attributes("G") == {}


def test_module_level_helpers() -> None:
    graph = _scenario()
    assert pydot.effective_attributes(graph, "A") == {
        "retention": "old",
        "owner": "alice",
    }
    assert pydot.effective_node_attributes(graph, "A") == {
        "retention": "old",
        "owner": "alice",
    }


def test_export_effective_attributes_records() -> None:
    graph = _scenario()
    records = graph.export_effective_attributes()
    assert records == [
        {
            "node": "A",
            "attribute": "retention",
            "value": "old",
            "source": "default",
            "graph": "lineage",
            "sequence": 1,
        },
        {
            "node": "A",
            "attribute": "owner",
            "value": "alice",
            "source": "explicit",
            "graph": "lineage",
            "sequence": 4,
        },
        {
            "node": "S",
            "attribute": "retention",
            "value": "old",
            "source": "default",
            "graph": "lineage",
            "sequence": 1,
        },
        {
            "node": "B",
            "attribute": "retention",
            "value": "new",
            "source": "default",
            "graph": "lineage",
            "sequence": 3,
        },
    ]


def test_ports_and_quote_handling() -> None:
    (graph,) = pydot.graph_from_dot_data(
        """
        digraph {
            node [retention=old];
            "A B":p1 -> c:sw;
            node [retention=new];
            "A B" [owner=alice];
        }
        """
    )
    assert graph.get_effective_node_attributes('"A B"').to_dict() == {
        "retention": "old",
        "owner": "alice",
    }
    assert graph.get_effective_node_attributes("c").to_dict() == {
        "retention": "old"
    }


def test_subgraph_endpoint_nodes_are_discovered() -> None:
    (graph,) = pydot.graph_from_dot_data(
        """
        digraph {
            node [retention=old];
            { X; Y } -> Z;
            node [retention=new];
            X [owner=a];
        }
        """
    )
    assert graph.get_effective_node_attributes("X").to_dict() == {
        "retention": "old",
        "owner": "a",
    }
    assert graph.get_effective_node_attributes("Y").to_dict() == {
        "retention": "old"
    }
    assert graph.get_effective_node_attributes("Z").to_dict() == {
        "retention": "old"
    }


def test_standalone_node_reports_explicit_attributes() -> None:
    node = pydot.Node("A", retention="custom")
    attrs = node.get_effective_attributes()
    assert attrs.to_dict() == {"retention": "custom"}
    assert attrs["retention"].explicit
