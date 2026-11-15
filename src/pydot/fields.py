# SPDX-FileCopyrightText: 2024 pydot contributors
#
# SPDX-License-Identifier: MIT

"""Extract column catalogs from field-style node labels.

Field graphs typically mix two label styles:

* record labels (``shape=record``), where ``|`` separates fields and
  ``<name>`` assigns a port to a field, and
* HTML-like labels, where ``<TD PORT="name">`` cells inside ``<TABLE>``
  elements assign ports, possibly in nested tables.

This module parses both styles into a :class:`ColumnCatalog` so that a
collector can decide, for every field edge, whether its port target is
:attr:`PortTarget.UNIQUE`, :attr:`PortTarget.MISSING` or
:attr:`PortTarget.AMBIGUOUS`.

Display text and port IDs are tracked separately: a port ID is a stable
identifier, so renaming a column's display text never breaks the mapping
from an existing edge to its column.  In record labels an escaped ``\\|``
is literal text, not a field separator.  In HTML-like labels a port
defined in a nested table shadows a same-named port of an outer table,
never the other way around.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from pydot.core import Edge, Graph, Node

__all__ = [
    "Column",
    "ColumnCatalog",
    "EdgePortReport",
    "PortTarget",
    "classify_edge_ports",
    "parse_html_label",
    "parse_record_label",
    "split_endpoint",
]


class PortTarget(enum.Enum):
    """How an edge's port reference resolves against a column catalog."""

    UNIQUE = "unique"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"


@dataclass
class Column:
    """One addressable column of a node's field label.

    `port` is the stable identifier edges refer to; `text` is the
    display title and may be renamed without affecting edge resolution.
    `depth` is the nesting level the column was declared at (record
    brace groups or nested HTML tables); deeper declarations shadow
    same-named shallower ones.
    """

    port: str | None
    text: str
    depth: int = 0


_RECORD_STRUCTURAL_CHARS = frozenset("|{}<>\\")
_RECORD_PORT_RE: re.Pattern[str] = re.compile(r"<([^<>]*)>")


def _unescape_record_text(text: str) -> str:
    """Resolve structural escapes (``\\|``, ``\\{``, ...) in record text."""
    out: list[str] = []
    i = 0
    while i < len(text):
        if (
            text[i] == "\\"
            and i + 1 < len(text)
            and text[i + 1] in _RECORD_STRUCTURAL_CHARS
        ):
            out.append(text[i + 1])
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _split_record_fields(label: str) -> list[tuple[int, str]]:
    """Split a record label into ``(depth, raw_text)`` fields.

    Fields are separated by unescaped ``|``; ``{`` and ``}`` group
    fields and increase the nesting depth.  Escaped characters are kept
    verbatim for later unescaping.
    """
    fields: list[tuple[int, str]] = []
    buf: list[str] = []
    depth = 0

    def flush(structural: bool) -> None:
        nonlocal buf
        raw = "".join(buf)
        buf = []
        # Empty fields produced purely by brace boundaries are noise.
        if structural and not raw:
            return
        fields.append((depth, raw))

    i = 0
    while i < len(label):
        ch = label[i]
        if ch == "\\" and i + 1 < len(label):
            buf.append(label[i : i + 2])
            i += 2
            continue
        if ch == "|":
            flush(structural=False)
        elif ch == "{":
            flush(structural=True)
            depth += 1
        elif ch == "}":
            flush(structural=True)
            depth = max(0, depth - 1)
        else:
            buf.append(ch)
        i += 1
    flush(structural=False)
    return fields


def parse_record_label(label: str) -> list[Column]:
    """Parse a record-style label into its columns.

    ``|`` separates fields, ``<name>`` marks a field's port, and an
    escaped ``\\|`` stays literal text instead of splitting a field.
    """
    columns: list[Column] = []
    for depth, raw in _split_record_fields(label):
        port = None
        text_raw = raw
        match = _RECORD_PORT_RE.search(raw)
        if match is not None:
            port = match.group(1) or None
            text_raw = raw[: match.start()] + raw[match.end() :]
        text = _unescape_record_text(text_raw).strip()
        if port is None and not text:
            continue
        columns.append(Column(port=port, text=text, depth=depth))
    return columns


class _HTMLLabelParser(HTMLParser):
    """Collect ``(port, depth, text)`` triples from an HTML-like label."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.columns: list[tuple[str | None, int, str]] = []
        self._table_depth = 0
        # Frames of open <TD> elements: [port, table_depth, text_parts].
        self._td_stack: list[list[Any]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag == "table":
            self._table_depth += 1
        elif tag == "td":
            port = dict(attrs).get("port")
            self._td_stack.append([port, self._table_depth, []])

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag == "td":
            port = dict(attrs).get("port")
            self.columns.append((port, self._table_depth, ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self._table_depth = max(0, self._table_depth - 1)
        elif tag == "td" and self._td_stack:
            port, depth, parts = self._td_stack.pop()
            self.columns.append((port, depth, "".join(parts).strip()))

    def handle_data(self, data: str) -> None:
        # Text belongs to the innermost open <TD> of the *current*
        # table; text of nested tables never leaks into outer cells.
        for frame in reversed(self._td_stack):
            if frame[1] == self._table_depth:
                frame[2].append(data)
                break


def parse_html_label(label: str) -> list[Column]:
    """Parse an HTML-like label into its columns.

    Every ``<TD>`` becomes a column; its ``PORT`` attribute provides
    the stable port ID and its text content the display title.  Cells
    of nested tables are recorded at a greater depth so their ports
    shadow same-named ports of outer tables.
    """
    parser = _HTMLLabelParser()
    parser.feed(label)
    parser.close()
    return [
        Column(port=port, text=text, depth=depth)
        for port, depth, text in parser.columns
    ]


def _strip_quotes(text: str) -> str:
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        return text[1:-1]
    return text


class ColumnCatalog:
    """The column directory extracted from one node's label.

    Resolution is keyed on port IDs only, so display titles can be
    renamed (see :meth:`set_text`) without invalidating the columns
    that existing edges point to.
    """

    def __init__(self, columns: Iterable[Column] = ()) -> None:
        self._columns = list(columns)

    @classmethod
    def from_label(cls, label: str | None) -> ColumnCatalog:
        """Build a catalog from a raw label value.

        Quoted values are record labels; unquoted ``<...>`` values are
        HTML-like labels, matching the DOT language rules.
        """
        if label is None:
            return cls()
        text = str(label).strip()
        if not text:
            return cls()
        if text.startswith('"') and text.endswith('"') and len(text) >= 2:
            return cls(parse_record_label(_strip_quotes(text)))
        if text.startswith("<") and text.endswith(">"):
            return cls(parse_html_label(text))
        return cls(parse_record_label(text))

    @classmethod
    def from_node(cls, node: Node) -> ColumnCatalog:
        """Build a catalog from a node's ``label`` attribute."""
        return cls.from_label(node.get("label"))

    @property
    def columns(self) -> list[Column]:
        """All extracted columns, in declaration order."""
        return list(self._columns)

    def ports(self) -> list[str]:
        """Port IDs present in the catalog, in declaration order."""
        seen: list[str] = []
        for column in self._columns:
            if column.port is not None and column.port not in seen:
                seen.append(column.port)
        return seen

    def _candidates(self, port: str) -> list[Column]:
        """Columns that a port reference resolves to.

        Only declarations at the deepest nesting level are visible, so
        a port inside a nested table is never overridden by a
        same-named port of an outer table.
        """
        entries = [c for c in self._columns if c.port == port]
        if not entries:
            return []
        max_depth = max(c.depth for c in entries)
        return [c for c in entries if c.depth == max_depth]

    def classify(self, port: str | None) -> PortTarget:
        """Classify a port reference as UNIQUE, MISSING or AMBIGUOUS."""
        if not port:
            return PortTarget.MISSING
        candidates = self._candidates(port)
        if not candidates:
            return PortTarget.MISSING
        if len(candidates) > 1:
            return PortTarget.AMBIGUOUS
        return PortTarget.UNIQUE

    def resolve(self, port: str | None) -> Column | None:
        """Return the unique column for a port, or None."""
        if not port:
            return None
        candidates = self._candidates(port)
        if len(candidates) != 1:
            return None
        return candidates[0]

    def set_text(self, port: str, text: str) -> bool:
        """Rename the display title of the column owning `port`.

        The port ID is untouched, so edges already resolved to this
        column keep resolving to it.  Returns False when the port does
        not resolve to a unique column.
        """
        column = self.resolve(port)
        if column is None:
            return False
        column.text = text
        return True


_COMPASS_POINTS = frozenset(
    ("n", "ne", "e", "se", "s", "sw", "w", "nw", "c", "_")
)


def _find_closing_quote(text: str, start: int = 1) -> int:
    i = start
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i
        i += 1
    return -1


def _unquote(text: str) -> str:
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        return text[1:-1].replace('\\"', '"')
    return text


def split_endpoint(endpoint: Any) -> tuple[str, str | None] | None:
    """Split an edge endpoint into ``(node_name, port)``.

    Handles quoted node names (``"a b":p1``), quoted ports
    (``a:"p 1"``) and trailing compass points (``a:p1:n``).  The node
    name is returned exactly as stored so it can be looked up with
    :meth:`Graph.get_node`.  Returns None for non-string endpoints
    (e.g. subgraph references).
    """
    if not isinstance(endpoint, str):
        return None

    if endpoint.startswith('"'):
        closing = _find_closing_quote(endpoint)
        if closing == -1:
            return (endpoint, None)
        name = endpoint[: closing + 1]
        rest = endpoint[closing + 1 :]
        portspec = rest[1:] if rest.startswith(":") else ""
    else:
        name, sep, portspec = endpoint.partition(":")
        if not sep:
            return (endpoint, None)

    if not portspec:
        return (name, None)

    if portspec.startswith('"'):
        closing = _find_closing_quote(portspec)
        if closing == -1:
            return (name, _unquote(portspec))
        port = _unquote(portspec[: closing + 1])
    else:
        port = portspec.split(":", 1)[0]

    if port in _COMPASS_POINTS:
        return (name, None)
    return (name, port or None)


@dataclass
class EdgePortReport:
    """The resolution of one edge endpoint's port reference."""

    edge: Edge
    node: str
    port: str
    status: PortTarget
    column: Column | None


def _walk_graphs(graph: Graph) -> Iterable[Graph]:
    yield graph
    for subgraph in graph.get_subgraph_list():
        yield from _walk_graphs(subgraph)


def classify_edge_ports(graph: Graph) -> list[EdgePortReport]:
    """Classify the port target of every field edge in `graph`.

    Edges (including those in subgraphs) whose endpoints carry a port
    reference are checked against the column catalog of the target
    node.  Each returned report tells whether the port target is
    :attr:`PortTarget.UNIQUE`, :attr:`PortTarget.MISSING` or
    :attr:`PortTarget.AMBIGUOUS`, and exposes the resolved column when
    unique.
    """
    nodes: dict[str, Node] = {}
    edges: list[Edge] = []
    for sub in _walk_graphs(graph):
        edges.extend(sub.get_edge_list())
        for node in sub.get_node_list():
            nodes.setdefault(node.get_name(), node)

    catalogs: dict[str, ColumnCatalog] = {}
    reports: list[EdgePortReport] = []
    for edge in edges:
        for endpoint in (edge.get_source(), edge.get_destination()):
            split = split_endpoint(endpoint)
            if split is None:
                continue
            name, port = split
            if port is None:
                continue
            if name not in catalogs:
                node = nodes.get(name)
                catalogs[name] = (
                    ColumnCatalog.from_node(node)
                    if node is not None
                    else ColumnCatalog()
                )
            catalog = catalogs[name]
            reports.append(
                EdgePortReport(
                    edge=edge,
                    node=name,
                    port=port,
                    status=catalog.classify(port),
                    column=catalog.resolve(port),
                )
            )
    return reports
