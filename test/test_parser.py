# SPDX-FileCopyrightText: 2025 pydot contributors
#
# SPDX-License-Identifier: MIT

"""Unit testing of individual dot_parser classes."""

from __future__ import annotations

import textwrap

import pyparsing as pp
import pytest

import pydot
from pydot import dot_parser
from pydot.dot_parser import HTML, GraphParser


def test_HTML_valid() -> None:
    """Test successful HTML parses."""
    parsed = HTML().parse_string("<<b>Bold Text</b>>")
    assert isinstance(parsed, pp.ParseResults)
    assert list(parsed) == ["<<b>Bold Text</b>>"]

    parsed2 = HTML().parse_string("<Label #1>")
    assert isinstance(parsed2, pp.ParseResults)
    assert list(parsed2) == ["<Label #1>"]


def test_HTML_invalid() -> None:
    """Test HTML parsing failure."""
    with pytest.raises(pp.ParseException) as exc:
        HTML().parse_string("<<b>Unbalanced tags</b>")
    assert "HTML: expected '>' to match '<' on line 1" in str(exc.value)


def test_edge_subgraph_anon() -> None:
    """Test parsing of an edge with an anonymous subgraph endpoint."""
    parser = GraphParser.edge_stmt
    res = parser.parse_string("""a -- { b; c; }""")
    assert len(res) == 1
    edge = res[0]
    expected = textwrap.dedent("""
        a -- {
        b;
        c;
        };""").strip()
    assert edge.to_string() == expected


def test_edge_subgraph_explicit() -> None:
    """Test parsing of an edge with an explicit subgraph endpoint."""
    parser = GraphParser.edge_stmt
    res = parser.parse_string("""a -- subgraph XY { b; c; }""")
    assert len(res) == 1
    edge = res[0]
    expected = textwrap.dedent("""
        a -- subgraph XY {
        b;
        c;
        };""").strip()
    assert edge.to_string() == expected


def test_AttrList_repr() -> None:
    parser = GraphParser.attr_list("a_list")
    res = parser.parse_string("[color=red, shape=square]")
    assert isinstance(res, pp.ParseResults)
    a_list = res.a_list
    assert isinstance(a_list, pp.ParseResults)
    assert len(a_list) == 1
    repr_str = repr(a_list[0])
    assert repr_str == "P_AttrList({'color': 'red', 'shape': 'square'})"


def test_DefaultStatement_repr() -> None:
    parser = GraphParser.default_stmt("defaults")
    res = parser.parse_string("node [color=blue];")
    assert isinstance(res, pp.ParseResults)
    defaults = res.defaults
    repr_str = repr(defaults)
    assert repr_str == "DefaultStatement(node, {'color': 'blue'})"


def test_comma_nodes_are_individually_discoverable() -> None:
    (graph,) = dot_parser.parse_dot_data("graph { a, b, c [color=red]; }")
    nodes = graph.get_nodes()
    assert len(nodes) == 3
    assert nodes[0].get_name() == "a"
    assert nodes[1].get_name() == "b"
    assert nodes[2].get_name() == "c"
    for name in ("a", "b", "c"):
        assert graph.get_node(name)[0].get_attributes() == {"color": "red"}

    graph.get_node("a")[0].set("color", "blue")
    assert graph.get_node("a")[0].get("color") == "blue"
    assert graph.get_node("b")[0].get("color") == "red"


def test_list_node_names_and_attributes_preserve_commas() -> None:
    (graph,) = dot_parser.parse_dot_data(
        'digraph { "a,b", <c,d>, plain [label="x,y"][color=red]; }'
    )
    nodes = graph.get_nodes()
    assert len(nodes) == 3
    assert nodes[0].get_name() == '"a,b"'
    assert nodes[1].get_name() == "<c,d>"
    assert nodes[2].get_name() == "plain"
    for node in nodes:
        assert node.get_attributes() == {"label": '"x,y"', "color": "red"}


def test_comma_edge_chain_is_unrolled() -> None:
    (graph,) = dot_parser.parse_dot_data(
        "digraph { a, b -> c, d -> e [penwidth=5]; }"
    )
    edges = graph.get_edges()
    assert len(edges) == 6
    for source, destination in (
        ("a", "c"),
        ("a", "d"),
        ("b", "c"),
        ("b", "d"),
        ("c", "e"),
        ("d", "e"),
    ):
        (edge,) = graph.get_edge(source, destination)
        assert edge.get_attributes() == {"penwidth": "5"}


def test_edge_lists_preserve_quoted_ids_and_ports() -> None:
    (graph,) = dot_parser.parse_dot_data(
        'digraph { "a,b":out:sw, c:in -> d:n, "e,f":port; }'
    )
    assert len(graph.get_edges()) == 4
    for source, destination in (
        ('"a,b":out:sw', "d:n"),
        ('"a,b":out:sw', '"e,f":port'),
        ("c:in", "d:n"),
        ("c:in", '"e,f":port'),
    ):
        assert len(graph.get_edge(source, destination)) == 1


def test_comma_lists_with_subgraph_endpoint() -> None:
    parser = GraphParser.edge_stmt
    (first, second, third, fourth) = parser.parse_string(
        "a, b -> { c; d; } -> e, f"
    )
    assert first.to_string() == "a -- {\nc;\nd;\n};"
    assert second.to_string() == "b -- {\nc;\nd;\n};"
    assert third.to_string() == "{\nc;\nd;\n} -- e;"
    assert fourth.to_string() == "{\nc;\nd;\n} -- f;"


def test_keyword_case_insensitive_graph_type() -> None:
    """Graph type keywords are case-insensitive."""
    for src, expected_type in [
        ("DIGRAPH G { a -> b }", "digraph"),
        ("GRAPH G { a -- b }", "graph"),
        ("Digraph G { a -> b }", "digraph"),
        ("Graph G { a -- b }", "graph"),
    ]:
        (g,) = dot_parser.parse_dot_data(src)
        assert g.get_type() == expected_type, src


def test_keyword_case_insensitive_strict() -> None:
    """STRICT keyword is case-insensitive."""
    for src in [
        "STRICT DIGRAPH G { a -> b }",
        "STRICT GRAPH G { a -- b }",
        "Strict Digraph G { a -> b }",
        "strict GRAPH G { a -- b }",
    ]:
        (g,) = dot_parser.parse_dot_data(src)
        assert g.get_strict(), src


def test_keyword_case_insensitive_node_edge_defaults() -> None:
    """NODE and EDGE default-attribute statements are case-insensitive."""
    for node_kw, edge_kw in [
        ("NODE", "EDGE"),
        ("Node", "Edge"),
        ("node", "edge"),
    ]:
        src = (
            f"digraph G {{ {node_kw} [color=red]; {edge_kw}"
            " [style=dashed]; a -> b }}"
        )
        (g,) = dot_parser.parse_dot_data(src)
        assert g.get_node_defaults() == [{"color": "red"}], src
        assert g.get_edge_defaults() == [{"style": "dashed"}], src


def test_keyword_case_insensitive_subgraph() -> None:
    """SUBGRAPH keyword is case-insensitive."""
    for kw in ["subgraph", "SUBGRAPH", "Subgraph"]:
        src = f"digraph G {{ {kw} SUB {{ a; b; }} }}"
        (g,) = dot_parser.parse_dot_data(src)
        subs = g.get_subgraphs()
        assert len(subs) == 1, src
        assert subs[0].get_name() == "SUB", src


def test_strict_graph_parsing() -> None:
    res = dot_parser.parse_dot_data("strict graph G { a; b; }")
    assert isinstance(res, list)
    assert len(res) == 1
    graph = res[0]
    assert graph.get_strict()
    assert graph.to_string() == "strict graph G {\na;\nb;\n}\n"

    res2 = dot_parser.parse_dot_data(
        """
        graph G { a; b; }
        strict digraph H { c; d; }
        """
    )
    assert isinstance(res2, list)
    assert len(res2) == 2
    assert not res2[0].get_strict()
    assert res2[1].get_strict()


def test_backslash_continuations() -> None:
    src = textwrap.dedent(r"""
        graph G {
            "my very long node \
        name" [color=red];
            "my indented and wrapped \
            node name" [shape=square];
            "my node name containing \ backslash";
        }""")
    res = dot_parser.parse_dot_data(src)
    assert isinstance(res, list)
    assert len(res) == 1
    graph = res[0]
    nodes = graph.get_nodes()
    assert len(nodes) == 3
    assert nodes[0].get_name() == '"my very long node name"'
    assert nodes[1].get_name() == '"my indented and wrapped     node name"'
    assert nodes[2].get_name() == '"my node name containing \\ backslash"'


def test_plus_concatenation() -> None:
    src = textwrap.dedent(r"""
        digraph G {
            "my" + "concatenated" + "name";
            "myconcatenated" + " with " + "ports" [color="r" + "ed"];
            "my\
        concatenated" + " with ports":p45:sw -> "my\
        concatenated\
        name":ne [penwidth=5, arrows="b" + "o" + "t" + "h"];
        "con" + "catenated" [label="this is a long\
        long label" + " that just goes on \
        and on and on"];
        }""")
    res = dot_parser.parse_dot_data(src)
    assert isinstance(res, list)
    assert len(res) == 1
    graph = res[0]
    nodes = graph.get_nodes()
    edges = graph.get_edges()

    assert len(nodes) == 3
    assert nodes[0].get_name() == '"myconcatenatedname"'
    assert nodes[1].get_name() == '"myconcatenated with ports"'
    assert nodes[1].get("color") == '"red"'
    assert nodes[2].get_name() == '"concatenated"'
    assert nodes[2].get("label") == (
        '"this is a longlong label that just goes on and on and on"'
    )

    assert len(edges) == 1
    edge = edges[0]
    assert edge.get_source() == f"{nodes[1].get_name()}:p45:sw"
    assert edge.get_destination() == '"myconcatenatedname":ne'
    assert edge.get("arrows") == '"both"'


def test_bad_parse_brace(capsys) -> None:
    src = textwrap.dedent(r"""
        graph G {
            a -- b;
            {{[{foo:bar:baz}]}}};
        }""")
    expected = textwrap.dedent("""
        {{[{foo:bar:baz}]}}};
        ^
    Expected '}', found '{'  (at char 27), (line:4, col:5)
    """).strip()
    res = dot_parser.parse_dot_data(src)
    assert res is None
    captured = capsys.readouterr()
    assert captured.out.strip() == expected


def test_bad_parse_bracket(capsys) -> None:
    src = textwrap.dedent(r"""
        graph G {
            a -- b;
            node [shape=box;
        }""")
    expected = textwrap.dedent("""
        node [shape=box;
             ^
    Expected '}', found '['  (at char 32), (line:4, col:10)
    """).strip()
    res = dot_parser.parse_dot_data(src)
    assert res is None
    captured = capsys.readouterr()
    assert captured.out.strip() == expected


def test_comments_are_discarded() -> None:
    """Test that all three comment forms are ignored.

    The DOT language accepts C-style block comments, C++-style line
    comments, and lines beginning with '#', which are discarded as
    preprocessor output. Both dot and pydot also discard a '#' comment
    at the end of a line, and both accept a comment anywhere whitespace
    is allowed, including between the parts of an edge statement.
    """
    expected = "digraph {\na -> b;\n}\n"

    for source in [
        "digraph { /* block */ a -> b }",
        "digraph {\n/* spanning\n   two lines */\na -> b\n}",
        "digraph { a -> b // trailing\n}",
        '# 1 "preprocessed.dot"\ndigraph { a -> b }',
        "digraph {\n# a line comment\na -> b\n}",
        "digraph { a -> b; # trailing\n}",
        "digraph { a -> b # trailing\n}",
    ]:
        (g,) = dot_parser.parse_dot_data(source)
        assert g.to_string() == expected, f"differs for: {source!r}"

    # Comments go wherever whitespace goes, edge statements included.
    interleaved = (
        "graph G {\n"
        "             a // node a\n"
        "/* edgeop */ --\n"
        "             b  # node b\n"
        "             [ /* width of line */ penwidth=5];\n"
        "}"
    )
    (g,) = dot_parser.parse_dot_data(interleaved)
    assert g.to_string() == "graph G {\na -- b [penwidth=5];\n}\n"

    # Inside a quoted string none of the three forms starts a comment.
    quoted = (
        "graph G {\n"
        '  a [label="# node a"];\n'
        '  b [label="// node b"];\n'
        '  "/* node */ c";\n'
        '  a -- b -- "/* node */ c";\n'
        "}"
    )
    (g,) = dot_parser.parse_dot_data(quoted)
    assert g.to_string() == (
        "graph G {\n"
        'a [label="# node a"];\n'
        'b [label="// node b"];\n'
        '"/* node */ c";\n'
        "a -- b;\n"
        'b -- "/* node */ c";\n'
        "}\n"
    )


def test_quoted_node_name_with_colon_is_not_split() -> None:
    """A quoted ID containing ':' stays one node, not an implicit chain."""
    src = 'digraph { "sales:archive":id -> consumer; }'
    (graph,) = dot_parser.parse_dot_data(src)
    (edge,) = graph.get_edges()
    assert edge.get_source() == '"sales:archive":id'
    assert edge.get_source_endpoint() == pydot.Endpoint(
        '"sales:archive"', "id", None
    )
    assert edge.get_destination_endpoint() == pydot.Endpoint("consumer")


def test_endpoint_key_preserves_node_port_and_compass() -> None:
    src = 'digraph { "sales:archive":id:nw -> sink:in:sw [weight=2]; }'
    (graph,) = dot_parser.parse_dot_data(src)
    (edge,) = graph.get_edges()
    source, destination = edge.get_endpoints()
    assert source == pydot.Endpoint('"sales:archive"', "id", "nw")
    assert destination == pydot.Endpoint("sink", "in", "sw")
    assert source.is_default is False
    assert pydot.Endpoint("sink").is_default is True


def test_endpoint_key_defaults_are_none() -> None:
    (graph,) = dot_parser.parse_dot_data('digraph { "sales:archive" -> b; }')
    (edge,) = graph.get_edges()
    key = edge.get_source_endpoint()
    assert isinstance(key, pydot.Endpoint)
    assert (key.name, key.port, key.compass) == ('"sales:archive"', None, None)
    assert key.to_dict() == {
        "node": '"sales:archive"',
        "port": None,
        "compass": None,
    }


def test_escaped_quote_name_coexists_with_bare_node() -> None:
    """Escaped-quote IDs and the same name without a port are distinct."""
    src = (
        "digraph {\n"
        '  "a\\"b":id -> x;\n'
        '  "a\\"b" -> y;\n'
        '  "sales:archive":id -> z;\n'
        '  "sales:archive" -> w;\n'
        "}\n"
    )
    (graph,) = dot_parser.parse_dot_data(src)
    sources = [edge.get_source_endpoint() for edge in graph.get_edges()]
    assert sources == [
        pydot.Endpoint('"a\\"b"', "id", None),
        pydot.Endpoint('"a\\"b"', None, None),
        pydot.Endpoint('"sales:archive"', "id", None),
        pydot.Endpoint('"sales:archive"', None, None),
    ]


def test_endpoint_keys_survive_dot_roundtrip() -> None:
    src = (
        "digraph {\n"
        '  "a\\"b":id:sw -> "a\\"b";\n'
        '  "sales:archive":id -> "sales:archive";\n'
        '  "sales:archive":id:ne -> other:n;\n'
        "}\n"
    )
    (graph,) = dot_parser.parse_dot_data(src)
    first = graph.get_edge_endpoint_keys()
    rendered = graph.to_string()
    (roundtrip,) = dot_parser.parse_dot_data(rendered)
    assert roundtrip.get_edge_endpoint_keys() == first
    assert roundtrip.to_string() == rendered


def test_node_endpoint_key_preserves_parts() -> None:
    (graph,) = dot_parser.parse_dot_data(
        'digraph { "sales:archive":id:sw; "sales:archive"; }'
    )
    keys = [node.get_endpoint() for node in graph.get_nodes()]
    assert keys == [
        pydot.Endpoint('"sales:archive"', "id", "sw"),
        pydot.Endpoint('"sales:archive"', None, None),
    ]
    assert [node.to_string() for node in graph.get_nodes()] == [
        '"sales:archive":id:sw;',
        '"sales:archive";',
    ]


def test_bare_endpoint_name_with_colon_is_quoted_on_output() -> None:
    """A node key with a colon and no port must stay one node."""
    edge = pydot.Edge(pydot.Endpoint("sales:archive"), "sink")
    assert edge.to_string() == '"sales:archive" -- sink;'
    (graph,) = dot_parser.parse_dot_data(f"digraph {{ {edge.to_string()} }}")
    key = graph.get_edges()[0].get_source_endpoint()
    assert key.name == '"sales:archive"'
    assert key.port is None
