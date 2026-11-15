# SPDX-FileCopyrightText: 2026 pydot contributors
#
# SPDX-License-Identifier: MIT

"""Tests for path-scoped subgraph grouping and membership tables."""

from __future__ import annotations

import json

import pytest

import pydot


REOPENED_OPS = """\
digraph G {
    subgraph cluster_ops { order; stock; }
    subgraph cluster_ops { customer; order; }
    a -> b;
}
"""


def _groups(graph: pydot.Graph) -> dict[str, pydot.GroupMembership]:
    return {row.group: row for row in graph.get_group_table()}


def test_named_reopen_is_one_logical_group() -> None:
    (graph,) = pydot.graph_from_dot_data(REOPENED_OPS)
    groups = _groups(graph)

    assert set(groups) == {"cluster_ops"}
    ops = groups["cluster_ops"]
    assert ops.kind == "named"
    assert ops.occurrences == 2
    assert ops.members == ["customer", "order", "stock"]
    assert ops.direct_members == ["customer", "order", "stock"]


def test_anonymous_blocks_stay_separate_groups() -> None:
    src = """\
    digraph G {
        { order; payment; }
        { customer; }
        { order; }
    }
    """
    (graph,) = pydot.graph_from_dot_data(src)
    groups = _groups(graph)

    assert list(groups) == [
        "{anonymous #1}",
        "{anonymous #2}",
        "{anonymous #3}",
    ]
    assert groups["{anonymous #1}"].members == ["order", "payment"]
    assert groups["{anonymous #2}"].members == ["customer"]
    assert groups["{anonymous #3}"].members == ["order"]
    for row in groups.values():
        assert row.kind == "anonymous"
        assert row.occurrences == 1


def test_dataset_can_belong_to_several_groups() -> None:
    src = """\
    digraph G {
        subgraph cluster_ops { order; }
        { order; payment; }
    }
    """
    (graph,) = pydot.graph_from_dot_data(src)
    table = graph.get_membership_table()

    order_rows = table.for_dataset("order")
    assert [row.group for row in order_rows] == [
        "cluster_ops",
        "{anonymous #1}",
    ]
    assert all(row.dataset == "order" for row in order_rows)


def test_anonymous_group_does_not_absorb_named_nodes() -> None:
    """A node in a named group and an anonymous group appears in both."""
    src = """\
    digraph G {
        subgraph cluster_ops { order; }
        { order; }
    }
    """
    (graph,) = pydot.graph_from_dot_data(src)
    table = graph.get_membership_table()

    assert [row.group for row in table.for_dataset("order")] == [
        "cluster_ops",
        "{anonymous #1}",
    ]
    assert len(table.for_group("cluster_ops")) == 1
    assert len(table.for_group("{anonymous #1}")) == 1


def test_membership_inherits_into_ancestors() -> None:
    src = """\
    digraph G {
        subgraph cluster_outer {
            a;
            subgraph cluster_inner { b; }
        }
    }
    """
    (graph,) = pydot.graph_from_dot_data(src)
    table = graph.get_membership_table()

    b_rows = table.for_dataset("b")
    assert [row.group for row in b_rows] == [
        "cluster_outer",
        "cluster_outer/cluster_inner",
    ]
    assert [row.direct for row in b_rows] == [False, True]

    direct_only = graph.get_membership_table(include_nested=False)
    assert [row.group for row in direct_only.for_dataset("b")] == [
        "cluster_outer/cluster_inner"
    ]


def test_moving_unrelated_node_keeps_anonymous_membership() -> None:
    src = """\
    digraph G {
        x;
        { order; payment; }
        y;
        { customer; }
    }
    """
    moved = """\
    digraph G {
        { order; payment; }
        x;
        y;
        { customer; }
    }
    """
    (before,) = pydot.graph_from_dot_data(src)
    (after,) = pydot.graph_from_dot_data(moved)

    def snapshot(graph: pydot.Graph) -> list[tuple[str, list[str]]]:
        return [
            (row.group, row.members) for row in graph.get_group_table()
        ]

    assert snapshot(before) == snapshot(after)
    assert snapshot(before) == [
        ("{anonymous #1}", ["order", "payment"]),
        ("{anonymous #2}", ["customer"]),
    ]


def test_moving_named_group_does_not_renumber_anonymous_groups() -> None:
    base = """\
    digraph G {
        { first; }
        subgraph named { n; }
        { second; }
    }
    """
    reordered = """\
    digraph G {
        subgraph named { n; }
        { first; }
        { second; }
    }
    """
    (g1,) = pydot.graph_from_dot_data(base)
    (g2,) = pydot.graph_from_dot_data(reordered)

    def anonymous(graph: pydot.Graph) -> dict[str, list[str]]:
        return {
            row.group: row.members
            for row in graph.get_group_table()
            if row.kind == "anonymous"
        }

    assert anonymous(g1) == {
        "{anonymous #1}": ["first"],
        "{anonymous #2}": ["second"],
    }
    assert anonymous(g2) == anonymous(g1)


def test_same_short_name_under_different_declared_scopes() -> None:
    src = "digraph { subgraph orders { o; } }"
    (sales,) = pydot.graph_from_dot_data(src, path_scope=["sales"])
    (ops,) = pydot.graph_from_dot_data(src, path_scope=["ops"])

    assert [row.group for row in sales.get_membership_table()] == [
        "sales/orders"
    ]
    assert [row.group for row in ops.get_membership_table()] == ["ops/orders"]
    assert [row.path for row in sales.get_membership_table()] == [
        ("sales", "orders")
    ]
    assert [row.path for row in ops.get_membership_table()] == [
        ("ops", "orders")
    ]


def test_scope_can_be_declared_on_graph_or_subgraph() -> None:
    graph = pydot.Dot(graph_type="digraph")
    graph.set_group_scope(["imports", "2026"])
    sub = pydot.Subgraph("orders")
    sub.add_node(pydot.Node("o"))
    graph.add_subgraph(sub)

    assert graph.get_group_scope() == ("imports", "2026")
    assert [row.group for row in graph.get_membership_table()] == [
        "imports/2026/orders"
    ]

    pinned = pydot.Subgraph("orders")
    pinned.set_group_path(["other", "orders"])
    pinned.add_node(pydot.Node("p"))
    graph.add_subgraph(pinned)

    assert [row.group for row in graph.get_membership_table()] == [
        "imports/2026/orders",
        "other/orders",
    ]


def test_dot_export_shows_reopened_and_anonymous_paths() -> None:
    src = """\
    digraph G {
        subgraph cluster_ops { order; }
        subgraph cluster_ops { customer; }
        { order; }
    }
    """
    (graph,) = pydot.graph_from_dot_data(src)
    out = graph.to_string(show_group_paths=True)

    assert "// group-path: cluster_ops (occurrence 1 of 2)" in out
    assert "// group-path: cluster_ops (occurrence 2 of 2)" in out
    assert "// group-path: {anonymous #1}" in out
    assert out.index("occurrence 1 of 2") < out.index("occurrence 2 of 2")

    default_out = graph.to_string()
    assert "group-path:" not in default_out


def test_export_tables_include_full_paths(tmp_path: object) -> None:
    (graph,) = pydot.graph_from_dot_data(REOPENED_OPS, path_scope="warehouse")
    table = graph.get_membership_table()

    csv_text = table.to_csv()
    assert csv_text.splitlines()[0].startswith("dataset,group,path,name")
    assert "warehouse/cluster_ops" in csv_text

    as_json = json.loads(table.to_json())
    assert as_json[0]["path"] == ["warehouse", "cluster_ops"]
    assert as_json[0]["group"] == "warehouse/cluster_ops"

    path = tmp_path / "membership.csv"  # type: ignore[operator]
    table.write_csv(path)
    assert path.read_text(encoding="utf-8") == csv_text


def test_group_table_export_and_lookup() -> None:
    (graph,) = pydot.graph_from_dot_data(REOPENED_OPS)
    groups = graph.get_group_table()

    assert groups.paths() == ["cluster_ops"]
    assert len(graph.find_groups("cluster_ops")) == 2
    assert len(graph.find_groups("missing")) == 0
    assert len(graph.get_group("cluster_ops")) == 2

    rows = json.loads(groups.to_json())
    assert rows[0]["occurrences"] == 2
    assert rows[0]["members"] == ["customer", "order", "stock"]


def test_reopening_named_group_does_not_merge_child_anonymous_groups() -> None:
    src = """\
    digraph G {
        subgraph cluster_ops {
            { order; }
        }
        subgraph cluster_ops {
            { customer; }
        }
    }
    """
    (graph,) = pydot.graph_from_dot_data(src)
    groups = _groups(graph)

    assert set(groups) == {
        "cluster_ops",
        "cluster_ops/{anonymous #1}",
        "cluster_ops/{anonymous #2}",
    }
    assert groups["cluster_ops/{anonymous #1}"].members == ["order"]
    assert groups["cluster_ops/{anonymous #2}"].members == ["customer"]


def test_default_declarations_are_optional_members() -> None:
    src = (
        "digraph G { subgraph cluster_ops "
        "{ node [shape=box]; n; } }"
    )
    (graph,) = pydot.graph_from_dot_data(src)

    without = graph.get_group_table()
    assert without[0].members == ["n"]

    with_defaults = graph.get_group_table(include_defaults=True)
    assert with_defaults[0].members == ["n", "node"]


def test_membership_table_is_stable_after_round_trip() -> None:
    (graph,) = pydot.graph_from_dot_data(REOPENED_OPS)
    before = graph.get_membership_table().to_dicts()

    (round_tripped,) = pydot.graph_from_dot_data(graph.to_string())
    after = round_tripped.get_membership_table().to_dicts()

    def project(rows: list[dict[str, object]]) -> list[tuple[object, ...]]:
        return [
            (row["dataset"], row["group"], row["kind"], row["direct"])
            for row in rows
        ]

    assert project(before) == project(after)


def test_scope_must_not_split_single_component_into_characters() -> None:
    (graph,) = pydot.graph_from_dot_data(REOPENED_OPS, path_scope="sales")
    rows = graph.get_membership_table()

    assert rows[0].path == ("sales", "cluster_ops")
    assert rows[0].group == "sales/cluster_ops"


@pytest.mark.parametrize("scope", ["sales", ["sales"]])
def test_scope_string_and_list_are_equivalent(scope: object) -> None:
    src = "digraph { subgraph orders { o; } }"
    (from_str,) = pydot.graph_from_dot_data(src, path_scope="sales")
    (from_list,) = pydot.graph_from_dot_data(src, path_scope=scope)

    assert from_str.get_group_table().to_dicts() == (
        from_list.get_group_table().to_dicts()
    )


def test_occurrence_view_links_reopened_blocks() -> None:
    (graph,) = pydot.graph_from_dot_data(REOPENED_OPS)
    rows = graph.get_group_occurrences()

    assert [row.group for row in rows] == ["cluster_ops", "cluster_ops"]
    assert [row.occurrence for row in rows] == [1, 2]
    assert [row.members for row in rows] == [
        ["order", "stock"],
        ["customer", "order"],
    ]


def test_occurrence_view_keeps_anonymous_blocks_distinct() -> None:
    src = "digraph G { { order; } { customer; } }"
    (graph,) = pydot.graph_from_dot_data(src)
    rows = graph.get_group_occurrences()

    assert [row.group for row in rows] == [
        "{anonymous #1}",
        "{anonymous #2}",
    ]
    assert [row.kind for row in rows] == ["anonymous", "anonymous"]


def test_module_level_helpers_and_export(tmp_path: object) -> None:
    (graph,) = pydot.graph_from_dot_data(REOPENED_OPS, path_scope="root")

    table = pydot.get_group_membership_table(graph)
    assert set(table.paths()) == {"root/cluster_ops"}

    groups = pydot.get_group_table(graph)
    assert groups[0].occurrences == 2

    csv_path = tmp_path / "membership.csv"  # type: ignore[operator]
    json_path = tmp_path / "membership.json"  # type: ignore[operator]
    pydot.export_group_membership(graph, csv_path)
    pydot.export_group_membership(graph, json_path, format="json")

    assert "root/cluster_ops" in csv_path.read_text(encoding="utf-8")
    assert json.loads(json_path.read_text(encoding="utf-8"))[0][
        "group"
    ] == "root/cluster_ops"

    with pytest.raises(pydot.Error, match="Unsupported table format"):
        pydot.export_group_membership(graph, csv_path, format="xml")


def test_nested_and_reopened_groups_keep_absolute_paths() -> None:
    src = """\
    digraph G {
        subgraph cluster_outer {
            a;
            subgraph cluster_ops { b; }
        }
        subgraph cluster_outer {
            c;
            { dense; }
        }
    }
    """
    (graph,) = pydot.graph_from_dot_data(src)
    groups = _groups(graph)

    assert set(groups) == {
        "cluster_outer",
        "cluster_outer/cluster_ops",
        "cluster_outer/{anonymous #1}",
    }
    assert groups["cluster_outer"].members == ["a", "b", "c", "dense"]
    assert groups["cluster_outer"].direct_members == ["a", "c"]
    assert groups["cluster_outer/cluster_ops"].members == ["b"]
    assert groups["cluster_outer/{anonymous #1}"].members == ["dense"]


def test_subtree_table_has_absolute_paths() -> None:
    src = """\
    digraph G {
        { outside; }
        subgraph cluster_outer {
            a;
            subgraph cluster_ops { b; }
        }
    }
    """
    (graph,) = pydot.graph_from_dot_data(src)
    outer = graph.get_subgraph("cluster_outer")[0]
    inner = outer.get_subgraph("cluster_ops")[0]

    assert [(row.group, row.members) for row in outer.get_group_table()] == [
        ("cluster_outer", ["a", "b"]),
        ("cluster_outer/cluster_ops", ["b"]),
    ]
    assert [(row.group, row.members) for row in inner.get_group_table()] == [
        ("cluster_outer/cluster_ops", ["b"])
    ]

    whole = outer.get_group_table(recursive=True)
    assert "{anonymous #1}" in [row.group for row in whole]
    assert "cluster_outer" in [row.group for row in whole]


def test_subtree_table_tracks_later_additions() -> None:
    src = "digraph G { subgraph cluster_ops { a; } }"
    (graph,) = pydot.graph_from_dot_data(src)
    ops = graph.get_subgraph("cluster_ops")[0]
    ops.add_node(pydot.Node("late"))

    assert [(row.group, row.members) for row in ops.get_group_table()] == [
        ("cluster_ops", ["a", "late"])
    ]
    assert [(row.group, row.members) for row in graph.get_group_table()] == [
        ("cluster_ops", ["a", "late"])
    ]


def test_occurrence_rows_expose_shared_path() -> None:
    (graph,) = pydot.graph_from_dot_data(REOPENED_OPS)
    rows = graph.get_group_occurrences()

    assert rows[0].path == rows[1].path == ("cluster_ops",)
    assert rows[0].occurrence == 1
    assert rows[1].occurrence == 2
    assert rows[0].members != rows[1].members


def test_exported_csv_keeps_anonymous_paths_typed() -> None:
    src = "digraph G { { order; } }"
    (graph,) = pydot.graph_from_dot_data(src)
    rows = graph.get_membership_table().to_dicts()

    assert rows[0]["path"] == [1]
    assert rows[0]["group"] == "{anonymous #1}"
    assert rows[0]["kind"] == "anonymous"
