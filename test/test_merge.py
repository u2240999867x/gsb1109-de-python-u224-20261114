# SPDX-FileCopyrightText: 2026 pydot contributors
#
# SPDX-License-Identifier: MIT

"""Tests for multi-source DOT merging."""

from __future__ import annotations

import json

import pytest

import pydot
from pydot.merge import (
    DISCREPANCY_ATTR,
    PROVENANCE_ATTR,
    UNRESOLVED_ATTR,
    DotMerger,
    MergeReferenceError,
    merge_dot_sources,
    scoped_name,
)

REFERENCE = {
    "canonical_nodes": [
        {"id": "orders"},
        {"id": "shipments"},
    ],
    "correspondences": [
        {"source": "chain_a", "local": "orders", "canonical": "orders"},
        {"source": "chain_b", "local": "orders", "canonical": "orders"},
        {"source": "chain_c", "local": "hub", "canonical": "orders"},
        {"source": "chain_c", "local": "hub", "canonical": "shipments"},
    ],
}

CHAIN_A = """
digraph chain_a {
    node [label="\\N"];
    staging [label="temp table a"];
    orders [retention_days="30", label="official orders"];
    staging -> orders [label="loads"];
}
"""

CHAIN_B = """
digraph chain_b {
    staging [label="temp table b"];
    orders [retention_days="45", label="official orders"];
    staging -> orders;
}
"""

CHAIN_C = """
digraph chain_c {
    staging;
    hub;
    hub -> staging;
}
"""


def build_merger() -> DotMerger:
    merger = DotMerger(REFERENCE)
    merger.add_source("chain_a", CHAIN_A)
    merger.add_source("chain_b", CHAIN_B)
    merger.add_source("chain_c", CHAIN_C)
    return merger


def node_names(graph: pydot.Dot) -> set[str]:
    return {node.get_name() for node in graph.get_node_list()}


def edge_endpoints(graph: pydot.Dot) -> set[tuple[str, str]]:
    return {
        (str(edge.get_source()), str(edge.get_destination()))
        for edge in graph.get_edge_list()
    }


def test_local_names_remain_source_scoped() -> None:
    graph, report = build_merger().merge()

    names = node_names(graph)
    # Three chains each call their temp table "staging": none collapse.
    assert {
        scoped_name("chain_a", "staging"),
        scoped_name("chain_b", "staging"),
        scoped_name("chain_c", "staging"),
    } <= names
    # Default node statements are not treated as entities.
    assert "node" not in names
    assert scoped_name("chain_c", "hub") in names


def test_only_reference_approved_nodes_merge() -> None:
    graph, report = build_merger().merge()

    names = node_names(graph)
    # Both chains' orders nodes are approved and merge into one node.
    assert "orders" in names
    orders_nodes = graph.get_node("orders")
    assert len(orders_nodes) == 1
    assert orders_nodes[0].get(PROVENANCE_ATTR) == "chain_a,chain_b"
    assert "orders" in report.merged_nodes
    # A scoped node is never reported as merged.
    assert not any(
        name.startswith("chain_") for name in report.merged_nodes
    )


def test_edges_keep_original_provenance() -> None:
    graph, _ = build_merger().merge()

    expected = {
        (scoped_name("chain_a", "staging"), "orders"): "chain_a",
        (scoped_name("chain_b", "staging"), "orders"): "chain_b",
        (
            scoped_name("chain_c", "hub"),
            scoped_name("chain_c", "staging"),
        ): "chain_c",
    }
    assert len(graph.get_edge_list()) == 3
    for edge in graph.get_edge_list():
        key = (str(edge.get_source()), str(edge.get_destination()))
        assert edge.get(PROVENANCE_ATTR) == expected[key]
    assert set(expected) == edge_endpoints(graph)


def test_retention_days_conflict_recorded_as_discrepancy() -> None:
    graph, report = build_merger().merge()

    assert len(report.discrepancies) == 1
    discrepancy = report.discrepancies[0]
    assert discrepancy.canonical == "orders"
    assert discrepancy.attribute == "retention_days"
    assert discrepancy.values == {
        "chain_a": "30",
        "chain_b": "45",
    }

    orders = graph.get_node("orders")[0]
    payload = json.loads(orders.get(DISCREPANCY_ATTR))
    assert payload == {"retention_days": {"chain_a": "30", "chain_b": "45"}}
    # Non-conflicting attributes are still merged.
    assert orders.get("label") == "official orders"


def test_ambiguous_correspondence_isolated_without_swallowing() -> None:
    graph, report = build_merger().merge()

    assert len(report.ambiguous_correspondences) == 1
    ambiguous = report.ambiguous_correspondences[0]
    assert ambiguous.source == "chain_c"
    assert ambiguous.local_name == "hub"
    assert ambiguous.canonical_candidates == ["orders", "shipments"]

    names = node_names(graph)
    # The suspicious local node stays source-scoped.
    assert scoped_name("chain_c", "hub") in names
    # Both candidate canonical identities survive as distinct nodes.
    assert "orders" in names
    assert "shipments" in names
    shipments = graph.get_node("shipments")[0]
    assert shipments.get(UNRESOLVED_ATTR) == "true"
    assert shipments.get(PROVENANCE_ATTR) == "chain_c"


def test_ambiguity_does_not_redirect_local_edges() -> None:
    graph, _ = build_merger().merge()
    assert (
        scoped_name("chain_c", "hub"),
        scoped_name("chain_c", "staging"),
    ) in edge_endpoints(graph)


def test_merged_graph_roundtrips_through_dot() -> None:
    graph, report = build_merger().merge()
    text = graph.to_string()

    reparsed = pydot.graph_from_dot_data(text)
    assert reparsed is not None
    roundtrip = reparsed[0]
    assert node_names(roundtrip) == node_names(graph)
    assert edge_endpoints(roundtrip) == edge_endpoints(graph)
    # The report is JSON-serializable.
    json.dumps(report.to_dict())


def test_reference_rejects_unknown_canonical_node() -> None:
    bad_reference = {
        "canonical_nodes": [{"id": "orders"}],
        "correspondences": [
            {
                "source": "chain_a",
                "local": "ghost",
                "canonical": "does_not_exist",
            }
        ],
    }
    with pytest.raises(MergeReferenceError):
        DotMerger(bad_reference)


def test_duplicate_source_id_rejected() -> None:
    merger = DotMerger(REFERENCE)
    merger.add_source("chain_a", CHAIN_A)
    with pytest.raises(ValueError):
        merger.add_source("chain_a", CHAIN_B)


def test_convenience_wrapper_accepts_files(tmp_path) -> None:
    reference_path = tmp_path / "reference.json"
    reference_path.write_text(json.dumps(REFERENCE), encoding="utf-8")
    dot_path = tmp_path / "chain_a.dot"
    dot_path.write_text(CHAIN_A, encoding="utf-8")

    graph, report = merge_dot_sources(
        [("chain_a", dot_path)], reference_path
    )
    assert "orders" in node_names(graph)
    assert scoped_name("chain_a", "staging") in node_names(graph)
    assert report.merged_nodes == ["orders"]


def test_cli(tmp_path) -> None:
    from pydot.merge import main

    reference_path = tmp_path / "reference.json"
    reference_path.write_text(json.dumps(REFERENCE), encoding="utf-8")
    sources = []
    for source_id, text in (
        ("chain_a", CHAIN_A),
        ("chain_b", CHAIN_B),
        ("chain_c", CHAIN_C),
    ):
        path = tmp_path / f"{source_id}.dot"
        path.write_text(text, encoding="utf-8")
        sources.append(f"{source_id}={path}")

    out_path = tmp_path / "merged.dot"
    report_path = tmp_path / "report.json"
    exit_code = main(
        [
            "--reference",
            str(reference_path),
            "--source",
            sources[0],
            "--source",
            sources[1],
            "--source",
            sources[2],
            "--out",
            str(out_path),
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 0
    assert out_path.exists()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["merged_nodes"] == ["orders"]
    assert len(payload["ambiguous_correspondences"]) == 1
    assert len(payload["discrepancies"]) == 1
