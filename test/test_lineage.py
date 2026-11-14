# SPDX-FileCopyrightText: 2026 pydot contributors
#
# SPDX-License-Identifier: MIT

"""Tests for lineage recompute manifests."""

from __future__ import annotations

import json

import pytest

import pydot
from pydot import lineage


def test_replacement_of_source_reaches_all_consumers() -> None:
    old = """
        digraph lineage {
          old_src [kind="table"];
          new_src [kind="table"];
          summary [kind="summary", sql="select * from old_src"];
          report [kind="report"];
          old_src -> summary [role="input"];
          summary -> report;
        }
    """
    new = """
        digraph lineage {
          old_src [kind="table"];
          new_src [kind="table"];
          summary [kind="summary", sql="select * from new_src"];
          report [kind="report"];
          new_src -> summary [role="input"];
          summary -> report;
        }
    """

    manifest = pydot.recompute_manifest(old, new)

    assert manifest.affected == ("report", "summary")
    by_dataset = {group.datasets[0]: group for group in manifest.groups}
    assert by_dataset["summary"].reason_path[0] in {"summary", "new_src"}
    assert by_dataset["summary"].reason_path[-1] == "summary"
    assert by_dataset["report"].reason_path[-1] == "report"
    assert all(group.reasons for group in manifest.groups)


def test_replacement_reason_prefers_new_source() -> None:
    old = "digraph { old_src -> summary; summary -> report; }"
    new = "digraph { new_src -> summary; summary -> report; }"

    manifest = pydot.recompute_manifest(old, new)

    summary = next(
        group for group in manifest.groups if group.datasets == ("summary",)
    )
    assert summary.reason_path == ("new_src", "summary")
    assert summary.reasons[0].cause.kind == lineage.CONNECTION_ADDED

    report = next(
        group for group in manifest.groups if group.datasets == ("report",)
    )
    assert report.reason_path == ("new_src", "summary", "report")


def test_removal_reason_follows_old_graph() -> None:
    old = "digraph { src -> a; src -> b; a -> out; }"
    new = "digraph { src -> b; a -> out; }"

    manifest = pydot.recompute_manifest(old, new)

    assert manifest.affected == ("a", "out")
    assert manifest.groups[0].reason_path == ("src", "a")
    assert manifest.groups[0].reasons[0].cause.kind == (
        lineage.CONNECTION_REMOVED
    )


def test_presentation_only_change_is_ignored() -> None:
    old = """
        digraph { a [label="Old", shape=box]; a -> b [color=red]; }
    """
    new = """
        digraph { a [label="New", shape=ellipse]; a -> b [color=blue]; }
    """

    manifest = pydot.recompute_manifest(old, new)

    assert manifest.groups == ()
    assert manifest.affected == ()
    assert manifest.changes == ()


def test_semantic_attribute_change_affects_node_and_downstream() -> None:
    old = """
        digraph { src [sql="select 1"]; mid [sql="select 2"]; out;
                  src -> mid; mid -> out; }
    """
    new = """
        digraph { src [sql="select 1"]; mid [sql="select 3"]; out;
                  src -> mid; mid -> out; }
    """

    manifest = pydot.recompute_manifest(old, new)

    assert manifest.affected == ("mid", "out")
    mid = manifest.groups[0]
    assert mid.datasets == ("mid",)
    assert mid.reason_path == ("mid",)
    assert mid.reasons[0].cause.kind == lineage.ATTRIBUTES_CHANGED
    assert mid.reasons[0].cause.attributes == ("sql",)
    assert manifest.groups[1].reason_path == ("mid", "out")


def test_removed_connection_propagates_through_old_graph() -> None:
    old = "digraph { a -> b; b -> c; c -> d; }"
    new = "digraph { a; b -> c; c -> d; }"

    manifest = pydot.recompute_manifest(old, new)

    assert manifest.affected == ("b", "c", "d")
    for group in manifest.groups:
        assert group.reasons[0].cause.kind == lineage.CONNECTION_REMOVED
        assert group.reason_path[0] == "a"
        assert group.reason_path[-1] == group.datasets[0]


def test_removed_connection_to_orphan_dataset_affects_consumer() -> None:
    old = "digraph { upstream; upstream -> report; }"
    new = "digraph { upstream; report; }"

    manifest = pydot.recompute_manifest(old, new)

    assert manifest.affected == ("report",)
    reason = manifest.groups[0].reasons[0]
    assert reason.cause.kind == lineage.CONNECTION_REMOVED
    assert reason.cause.connection == ("upstream", "report")
    assert reason.path == ("upstream", "report")


def test_cycle_members_are_reported_once_as_a_group() -> None:
    old = """
        digraph {
          src [sql="a"];
          left [sql="b"];
          right [sql="c"];
          tail;
          src -> left;
          left -> right;
          right -> left;
          right -> tail;
        }
    """
    new = """
        digraph {
          src [sql="a"];
          left [sql="changed"];
          right [sql="c"];
          tail;
          src -> left;
          left -> right;
          right -> left;
          right -> tail;
        }
    """

    manifest = pydot.recompute_manifest(old, new)

    cyclic = [group for group in manifest.groups if group.cyclic]
    assert len(cyclic) == 1
    assert set(cyclic[0].datasets) == {"left", "right"}
    assert manifest.affected.count("left") == 1
    assert manifest.affected.count("right") == 1
    assert "tail" in manifest.affected
    assert cyclic[0].reasons

    # Every member must have a reason path via the changed dataset.
    paths = {reason.path for reason in cyclic[0].reasons}
    assert ("left",) in paths
    assert ("left", "right") in paths


def test_cycle_connected_to_source_is_fully_reported() -> None:
    old = "digraph { a -> b; b -> c; c -> b; b -> out; }"
    new = "digraph { a -> b; b -> c; c -> b; b -> out; a [sql=1]; }"

    manifest = pydot.recompute_manifest(old, new)

    cyclic = manifest.cyclic_groups
    assert len(cyclic) == 1
    assert set(cyclic[0].datasets) == {"b", "c"}
    assert "out" in manifest.affected


def test_isolated_equivalent_nodes_do_not_take_a_slot() -> None:
    old = """
        digraph { core -> core_out; scratch [sql="select 1"]; }
    """
    new = """
        digraph { core -> core_out; tmp_scratch [sql="select 1"]; }
    """

    manifest = pydot.recompute_manifest(old, new)

    assert manifest.groups == ()
    assert manifest.changes == ()


def test_isolated_node_with_changed_semantics_is_reported() -> None:
    old = 'digraph { core -> out; scratch [sql="select 1"]; }'
    new = 'digraph { core -> out; scratch [sql="select 2"]; }'

    manifest = pydot.recompute_manifest(old, new)

    assert manifest.affected == ("scratch",)
    assert manifest.groups[0].reason_path == ("scratch",)


def test_isolated_new_node_is_not_recomputed() -> None:
    old = "digraph { a -> b; }"
    new = 'digraph { a -> b; brand_new [sql="x"]; }'

    manifest = pydot.recompute_manifest(old, new)

    assert manifest.affected == ()
    assert any(
        change.kind == lineage.DATASET_ADDED for change in manifest.changes
    )


def test_connection_attribute_change_is_reported() -> None:
    old = 'digraph { a -> b [role="input"]; b -> c; }'
    new = 'digraph { a -> b [role="lookup"]; b -> c; }'

    manifest = pydot.recompute_manifest(old, new)

    assert manifest.affected == ("b", "c")
    assert manifest.groups[0].reasons[0].cause.kind == (
        lineage.CONNECTION_CHANGED
    )
    assert manifest.groups[0].reasons[0].cause.attributes == ("role",)


def test_duplicate_identical_edges_do_not_change_dependencies() -> None:
    old = "digraph { a -> b; a -> b; b -> c; }"
    new = "digraph { a -> b; b -> c; }"

    manifest = pydot.recompute_manifest(old, new)

    assert manifest.groups == ()
    assert manifest.changes == ()


def test_dropping_one_of_two_distinct_edges_is_reported() -> None:
    old = 'digraph { a -> b [kind="read"]; a -> b [kind="write"]; b -> c; }'
    new = 'digraph { a -> b [kind="read"]; b -> c; }'

    manifest = pydot.recompute_manifest(old, new)

    assert manifest.affected == ("b", "c")
    assert manifest.groups[0].reasons[0].cause.kind == (
        lineage.CONNECTION_REMOVED
    )


def test_endpoint_port_is_a_semantic_connection_change() -> None:
    old = "digraph { orders:total -> report; }"
    new = "digraph { orders:net -> report; }"

    manifest = pydot.recompute_manifest(old, new)

    assert manifest.affected == ("report",)
    cause = manifest.groups[0].reasons[0].cause
    assert cause.kind == lineage.CONNECTION_CHANGED
    assert cause.connection == ("orders", "report")
    assert cause.attributes == ("source_port",)


def test_quoted_dataset_names_with_ports_are_normalized() -> None:
    old = 'digraph { "table A":amount -> report; }'
    new = 'digraph { "table A":amount -> report; }'

    manifest = pydot.recompute_manifest(old, new)

    assert manifest.groups == ()


def test_undirected_graph_treats_edges_as_bidirectional() -> None:
    old = "graph { a -- b; b -- c; }"
    new = "graph { b -- c; }"

    manifest = pydot.recompute_manifest(old, new)

    # Node "a" disappears with its only edge, so it is not recomputed;
    # both surviving neighbors are reached through the removed link.
    assert manifest.affected == ("b", "c")


def test_quoted_reserved_word_is_a_real_dataset() -> None:
    old = 'digraph { "node" [sql="1"]; "node" -> b; }'
    new = 'digraph { "node" [sql="2"]; "node" -> b; }'

    manifest = pydot.recompute_manifest(old, new)

    assert manifest.affected == ("b", "node")


def test_unquoted_defaults_are_not_datasets() -> None:
    old = "digraph { node [shape=box]; a -> b; }"
    new = "digraph { node [shape=ellipse]; a -> b; }"

    manifest = pydot.recompute_manifest(old, new)

    assert manifest.groups == ()


def test_subgraph_nodes_and_edges_are_included() -> None:
    old = """
        digraph {
          subgraph cluster_x { a [sql="1"]; a -> b; }
          b -> c;
        }
    """
    new = """
        digraph {
          subgraph cluster_x { a [sql="2"]; a -> b; }
          b -> c;
        }
    """

    manifest = pydot.recompute_manifest(old, new)

    assert manifest.affected == ("a", "b", "c")


def test_accepts_graph_objects_and_single_element_sequences() -> None:
    old = pydot.graph_from_dot_data("digraph { a [sql=1]; a -> b; }")[0]
    new = pydot.graph_from_dot_data("digraph { a [sql=2]; a -> b; }")[0]

    assert pydot.recompute_manifest(old, new).affected == ("a", "b")
    assert pydot.recompute_manifest([old], [new]).affected == ("a", "b")


def test_accepts_paths(tmp_path: pytest.TempPathFactory) -> None:
    old_path = tmp_path / "old.dot"
    new_path = tmp_path / "new.dot"
    old_path.write_text("digraph { a [sql=1]; a -> b; }", encoding="utf-8")
    new_path.write_text("digraph { a [sql=2]; a -> b; }", encoding="utf-8")

    manifest = pydot.recompute_manifest(old_path, new_path)

    assert manifest.affected == ("a", "b")


def test_manifest_serializes_to_json() -> None:
    old = 'digraph { a [sql="1"]; a -> b; }'
    new = 'digraph { a [sql="2"]; a -> b; }'

    payload = json.loads(
        json.dumps(pydot.recompute_manifest(old, new).to_dict())
    )

    assert payload["datasets"] == ["a", "b"]
    assert payload["affected"][0]["reasons"][0]["path"] == ["a"]


def test_custom_ignored_attribute() -> None:
    old = 'digraph { a [warehouse="x"]; a -> b; }'
    new = 'digraph { a [warehouse="y"]; a -> b; }'

    assert pydot.recompute_manifest(old, new).affected == ("a", "b")
    assert (
        pydot.recompute_manifest(
            old, new, ignored_attributes={"warehouse"}
        ).affected
        == ()
    )


def test_custom_attribute_predicate() -> None:
    old = 'digraph { a [paint="x"]; a -> b; }'
    new = 'digraph { a [paint="y"]; a -> b; }'

    manifest = pydot.recompute_manifest(
        old, new, attribute_predicate=lambda name: name == "paint"
    )

    assert manifest.groups == ()


def test_computed_attributes_restricts_semantics() -> None:
    old = 'digraph { a [sql="1", owner="alice"]; a -> b; }'
    new = 'digraph { a [sql="1", owner="bob"]; a -> b; }'

    only_sql = pydot.recompute_manifest(old, new, computed_attributes={"sql"})
    assert only_sql.groups == ()

    both = pydot.recompute_manifest(
        old, new, computed_attributes={"sql", "owner"}
    )
    assert both.affected == ("a", "b")


def test_multiple_graphs_in_one_input_raise() -> None:
    with pytest.raises(pydot.Error):
        pydot.recompute_manifest(
            "digraph { a } digraph { b }", "digraph { a }"
        )


def test_malformed_dot_raises() -> None:
    with pytest.raises(pydot.Error, match="Could not parse"):
        pydot.recompute_manifest("digraph { a }", "digraph { a")


def test_missing_path_raises(tmp_path: pytest.TempPathFactory) -> None:
    with pytest.raises(OSError):
        pydot.recompute_manifest(tmp_path / "missing.dot", "digraph { a }")


def test_reason_paths_are_shortest_and_deterministic() -> None:
    old = """
        digraph {
          changed [sql="1"];
          changed -> short;
          short -> target;
          changed -> long_a;
          long_a -> long_b;
          long_b -> target;
        }
    """
    new = """
        digraph {
          changed [sql="2"];
          changed -> short;
          short -> target;
          changed -> long_a;
          long_a -> long_b;
          long_b -> target;
        }
    """

    first = pydot.recompute_manifest(old, new)
    second = pydot.recompute_manifest(old, new)

    assert first.to_dict() == second.to_dict()
    target = next(
        group for group in first.groups if group.datasets == ("target",)
    )
    assert target.reason_path == ("changed", "short", "target")
