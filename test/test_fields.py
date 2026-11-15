# SPDX-FileCopyrightText: 2024 pydot contributors
#
# SPDX-License-Identifier: MIT
from __future__ import annotations

import pydot
from pydot.fields import (
    Column,
    ColumnCatalog,
    PortTarget,
    classify_edge_ports,
    parse_html_label,
    parse_record_label,
    split_endpoint,
)


def test_parse_record_label_ports_and_text_separate() -> None:
    columns = parse_record_label("<f0> left|<f1> right")
    assert [(c.port, c.text) for c in columns] == [
        ("f0", "left"),
        ("f1", "right"),
    ]


def test_parse_record_label_escaped_pipe_is_not_a_separator() -> None:
    columns = parse_record_label(r"<f0> a\|b|<f1> c")
    assert [(c.port, c.text) for c in columns] == [
        ("f0", "a|b"),
        ("f1", "c"),
    ]


def test_parse_record_label_nested_braces() -> None:
    columns = parse_record_label("<f0> top|{<f1> a|<f2> b}")
    assert [(c.port, c.text, c.depth) for c in columns] == [
        ("f0", "top", 0),
        ("f1", "a", 1),
        ("f2", "b", 1),
    ]


def test_parse_html_label_ports_and_text_separate() -> None:
    label = (
        "<<TABLE><TR><TD PORT='f0'>left</TD>"
        "<TD PORT='f1'>right</TD></TR></TABLE>>"
    )
    columns = parse_html_label(label)
    assert [(c.port, c.text) for c in columns] == [
        ("f0", "left"),
        ("f1", "right"),
    ]


def test_parse_html_label_nested_table_port_shadows_outer() -> None:
    label = (
        "<<TABLE><TR><TD PORT='dup'>outer</TD><TD>"
        "<TABLE><TR><TD PORT='dup'>inner</TD></TR></TABLE>"
        "</TD></TR></TABLE>>"
    )
    catalog = ColumnCatalog(parse_html_label(label))
    assert catalog.classify("dup") is PortTarget.UNIQUE
    column = catalog.resolve("dup")
    assert column is not None
    assert column.text == "inner"


def test_parse_html_label_nested_text_does_not_leak_outer() -> None:
    label = (
        "<<TABLE><TR><TD PORT='outer'>before"
        "<TABLE><TR><TD PORT='inner'>nested</TD></TR></TABLE>"
        "after</TD></TR></TABLE>>"
    )
    catalog = ColumnCatalog(parse_html_label(label))
    outer = catalog.resolve("outer")
    inner = catalog.resolve("inner")
    assert outer is not None and outer.text == "beforeafter"
    assert inner is not None and inner.text == "nested"


def test_catalog_ambiguous_port() -> None:
    catalog = ColumnCatalog(parse_record_label("<f0> a|<f0> b"))
    assert catalog.classify("f0") is PortTarget.AMBIGUOUS
    assert catalog.resolve("f0") is None


def test_catalog_missing_port() -> None:
    catalog = ColumnCatalog(parse_record_label("<f0> a"))
    assert catalog.classify("nope") is PortTarget.MISSING
    assert catalog.classify(None) is PortTarget.MISSING
    assert catalog.resolve("nope") is None


def test_catalog_from_node_quoted_record_label() -> None:
    node = pydot.Node("rec", shape="record", label='"<f0> a|<f1> b"')
    catalog = ColumnCatalog.from_node(node)
    assert catalog.ports() == ["f0", "f1"]


def test_catalog_from_node_html_label() -> None:
    node = pydot.Node(
        "html",
        label="<<TABLE><TR><TD PORT='p1'>cell</TD></TR></TABLE>>",
    )
    catalog = ColumnCatalog.from_node(node)
    assert catalog.ports() == ["p1"]
    column = catalog.resolve("p1")
    assert column is not None and column.text == "cell"


def test_catalog_from_node_without_label() -> None:
    catalog = ColumnCatalog.from_node(pydot.Node("plain"))
    assert catalog.columns == []
    assert catalog.classify("f0") is PortTarget.MISSING


def test_set_text_keeps_edge_resolution() -> None:
    catalog = ColumnCatalog(parse_record_label("<f0> old title"))
    before = catalog.resolve("f0")
    assert before is not None and before.text == "old title"

    assert catalog.set_text("f0", "new title") is True
    after = catalog.resolve("f0")
    assert after is before
    assert after.text == "new title"
    assert catalog.classify("f0") is PortTarget.UNIQUE

    assert catalog.set_text("missing", "x") is False


def test_split_endpoint_variants() -> None:
    assert split_endpoint("a") == ("a", None)
    assert split_endpoint("a:f0") == ("a", "f0")
    assert split_endpoint("a:f0:n") == ("a", "f0")
    assert split_endpoint("a:n") == ("a", None)
    assert split_endpoint('"a b":f0') == ('"a b"', "f0")
    assert split_endpoint('a:"f 0"') == ("a", "f 0")
    assert split_endpoint("a:") == ("a", None)


def _mixed_graph() -> pydot.Dot:
    (graph,) = pydot.graph_from_dot_data(
        """
        digraph fields {
            rec [shape=record, label="<f0> left|<f1> mid\\|dle|<f0> dup"];
            html [
                shape=plaintext,
                label=<
                    <TABLE BORDER="0" CELLBORDER="1">
                        <TR><TD PORT="h0">alpha</TD></TR>
                        <TR><TD><TABLE BORDER="0" CELLBORDER="1">
                            <TR><TD PORT="h1">beta</TD></TR>
                        </TABLE></TD></TR>
                    </TABLE>
                >];
            plain [label="no ports here"];
            rec:f1 -> html:h0;
            rec:f0 -> html:h1;
            rec:missing -> plain:f9;
            html:h0 -> plain;
        }
        """
    )
    return graph


def test_classify_edge_ports_mixed_graph() -> None:
    reports = classify_edge_ports(_mixed_graph())
    by_endpoint = {(r.node, r.port): r for r in reports}

    # rec:f1 -> html:h0 : both targets unique
    assert by_endpoint[("rec", "f1")].status is PortTarget.UNIQUE
    assert by_endpoint[("html", "h0")].status is PortTarget.UNIQUE
    assert by_endpoint[("html", "h0")].column is not None
    assert by_endpoint[("html", "h0")].column.text == "alpha"

    # rec:f0 is declared twice at the same level -> ambiguous
    assert by_endpoint[("rec", "f0")].status is PortTarget.AMBIGUOUS
    assert by_endpoint[("rec", "f0")].column is None

    # nested table port resolves to the inner cell
    h1 = by_endpoint[("html", "h1")]
    assert h1.status is PortTarget.UNIQUE
    assert h1.column is not None and h1.column.text == "beta"

    # ports absent from the catalogs are missing
    assert by_endpoint[("rec", "missing")].status is PortTarget.MISSING
    assert by_endpoint[("plain", "f9")].status is PortTarget.MISSING

    # port-less endpoints (plain node) produce no reports
    assert all(r.port for r in reports)
    assert len(reports) == 7


def test_classify_edge_ports_survives_display_rename() -> None:
    graph = _mixed_graph()
    reports = classify_edge_ports(graph)
    target = next(r for r in reports if (r.node, r.port) == ("html", "h0"))
    column = target.column
    assert column is not None

    # Renaming the display title must not break edge -> column lookup.
    node = graph.get_node("html")[0]
    catalog = ColumnCatalog.from_node(node)
    assert catalog.set_text("h0", "renamed title") is True

    reports_after = classify_edge_ports(graph)
    target_after = next(
        r for r in reports_after if (r.node, r.port) == ("html", "h0")
    )
    assert target_after.status is PortTarget.UNIQUE
    assert target_after.column is not None
    assert target_after.column.port == column.port


def test_column_defaults() -> None:
    column = Column(port=None, text="t")
    assert column.depth == 0
