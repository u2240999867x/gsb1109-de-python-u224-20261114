# SPDX-FileCopyrightText: 2026 pydot contributors
#
# SPDX-License-Identifier: MIT

"""Recompute manifests for field-lineage DOT graphs.

A lineage graph is a directed graph whose nodes are datasets (tables,
views, reports, ...) and whose edges are data dependencies.  This module
compares two versions of such a graph and reports which datasets in the
new version must be recomputed, together with at least one reason path
for every reported group.

Presentation-only changes such as ``label``, ``fontname`` or ``color``
are ignored.  Effective connection changes are not: additions propagate
through the new graph, while removals also propagate through the old
graph so that consumers which lose an input are not missed.  Members of
a dependency cycle are reported once, as a group, when any member is
affected.  An isolated dataset is not reported when it is semantically
identical to an isolated dataset in the other version, which keeps pure
renames out of the manifest.
"""

from __future__ import annotations

import heapq
import os
from collections import deque
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
    Union,
)

from pydot.classes import FrozenDict
from pydot.core import Graph, graph_from_dot_data, graph_from_dot_file
from pydot.exceptions import Error

__all__ = [
    "AffectedDatasetGroup",
    "Cause",
    "RecomputeManifest",
    "ReasonPath",
    "recompute_manifest",
]

# Attributes that only affect how the graph is drawn or documented.  A
# change to one of them never makes a dataset stale on its own.
PRESENTATION_ATTRIBUTES = frozenset(
    {
        "arrowhead",
        "arrowsize",
        "arrowtail",
        "bgcolor",
        "center",
        "color",
        "colorscheme",
        "comment",
        "constraint",
        "decorate",
        "dir",
        "distortion",
        "edgehref",
        "edgetarget",
        "edgetooltip",
        "edgeurl",
        "esep",
        "fillcolor",
        "fixedsize",
        "fontcolor",
        "fontname",
        "fontsize",
        "group",
        "headclip",
        "headhref",
        "headlabel",
        "headport",
        "headtarget",
        "headtooltip",
        "headurl",
        "height",
        "href",
        "id",
        "image",
        "imagescale",
        "label",
        "labelangle",
        "labeldistance",
        "labelfloat",
        "labelfontcolor",
        "labelfontname",
        "labelfontsize",
        "labelhref",
        "labeljust",
        "labelloc",
        "labeltarget",
        "labeltooltip",
        "labelurl",
        "layer",
        "layers",
        "layersep",
        "len",
        "lhead",
        "lp",
        "ltail",
        "margin",
        "minlen",
        "nojustify",
        "nodesep",
        "orientation",
        "overlap",
        "overlap_scaling",
        "pad",
        "page",
        "pagedir",
        "pencolor",
        "penwidth",
        "peripheries",
        "pin",
        "pos",
        "rank",
        "rankdir",
        "ranksep",
        "ratio",
        "rects",
        "regular",
        "rotate",
        "samehead",
        "sametail",
        "sep",
        "shape",
        "shapefile",
        "showboxes",
        "sides",
        "size",
        "skew",
        "sortv",
        "splines",
        "style",
        "tailclip",
        "tailhref",
        "taillabel",
        "tailport",
        "tailtarget",
        "tailtooltip",
        "tailurl",
        "target",
        "tooltip",
        "url",
        "vertices",
        "viewport",
        "weight",
        "width",
        "xlabel",
        "z",
    }
)

DATASET_ADDED = "dataset_added"
DATASET_REMOVED = "dataset_removed"
ATTRIBUTES_CHANGED = "attributes_changed"
CONNECTION_ADDED = "connection_added"
CONNECTION_REMOVED = "connection_removed"
CONNECTION_CHANGED = "connection_changed"

# Lower is a stronger explanation for a dataset.  New-version effects
# beat effects that only exist in the old graph.
_CAUSE_PRIORITY = {
    ATTRIBUTES_CHANGED: 0,
    CONNECTION_CHANGED: 0,
    CONNECTION_ADDED: 1,
    CONNECTION_REMOVED: 2,
    DATASET_ADDED: 3,
    DATASET_REMOVED: 4,
}

GraphInput = Union[Graph, str, bytes, "os.PathLike[str]", Sequence[Graph]]
AttributePredicate = Callable[[str], bool]
"""Return ``True`` for attribute names that should be ignored."""


def _canonical_value(value: Any) -> str:
    """Return a version-independent representation of an attribute."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    return text


def _canonical_name(value: Any) -> str:
    """Return the dataset name used for identity comparisons."""
    text = str(value)
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    return text


def _endpoint_parts(value: Any) -> tuple[str, str | None]:
    """Split an edge endpoint into a dataset name and an optional port.

    Ports are significant in field lineage (``table:column``), so they
    are preserved for change detection even though propagation follows
    dataset names.
    """
    text = str(value)
    if text.startswith('"'):
        end = 1
        while end < len(text):
            if text[end] == "\\":
                end += 2
                continue
            if text[end] == '"':
                break
            end += 1
        if end >= len(text):
            return _canonical_name(text), None
        name = _canonical_name(text[: end + 1])
        rest = text[end + 1 :]
        if rest.startswith(":"):
            return name, rest[1:]
        return _canonical_name(text), None

    if ":" in text:
        name, port = text.split(":", 1)
        return _canonical_name(name), port
    return _canonical_name(text), None


def _semantic_items(
    attrs: Mapping[str, Any], predicate: AttributePredicate
) -> tuple[tuple[str, str], ...]:
    """Return the comparable, non-cosmetic part of an attribute map."""
    values: dict[str, str] = {}
    for key, value in attrs.items():
        normalized = str(key).lower()
        if not predicate(normalized):
            values[normalized] = _canonical_value(value)
    return tuple(sorted(values.items()))


def _changed_attributes(
    old: tuple[tuple[str, str], ...],
    new: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    old_map = dict(old)
    new_map = dict(new)
    return tuple(
        sorted(
            key
            for key in set(old_map) | set(new_map)
            if old_map.get(key) != new_map.get(key)
        )
    )


@dataclass(frozen=True)
class Cause:
    """A semantic change that can make downstream datasets stale."""

    kind: str
    dataset: str | None = None
    connection: tuple[str, str] | None = None
    attributes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"kind": self.kind}
        if self.dataset is not None:
            result["dataset"] = self.dataset
        if self.connection is not None:
            result["connection"] = list(self.connection)
        if self.attributes:
            result["attributes"] = list(self.attributes)
        return result


@dataclass(frozen=True)
class ReasonPath:
    """A path of dataset names explaining why a dataset is affected."""

    path: tuple[str, ...]
    cause: Cause

    def to_dict(self) -> dict[str, Any]:
        return {"path": list(self.path), "cause": self.cause.to_dict()}


@dataclass(frozen=True)
class AffectedDatasetGroup:
    """One entry in the recompute manifest.

    ``datasets`` contains the datasets that must be recomputed.  Most
    entries contain one dataset; members of a dependency cycle are
    listed together in a single entry.  ``reasons`` always contains at
    least one path and cause.
    """

    datasets: tuple[str, ...]
    reasons: tuple[ReasonPath, ...]
    cyclic: bool = False

    @property
    def members(self) -> tuple[str, ...]:
        """Alias for :attr:`datasets`."""
        return self.datasets

    @property
    def reason_path(self) -> tuple[str, ...]:
        """The first (shortest) reason path for this group."""
        if not self.reasons:
            return ()
        return self.reasons[0].path

    @property
    def reason(self) -> ReasonPath | None:
        """The first reason for this group."""
        return self.reasons[0] if self.reasons else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "datasets": list(self.datasets),
            "reason_path": list(self.reason_path),
            "reasons": [reason.to_dict() for reason in self.reasons],
            "cyclic": self.cyclic,
        }


@dataclass(frozen=True)
class RecomputeManifest:
    """Result of comparing two lineage graph versions."""

    groups: tuple[AffectedDatasetGroup, ...] = field(default_factory=tuple)
    changes: tuple[Cause, ...] = field(default_factory=tuple)

    @property
    def entries(self) -> tuple[AffectedDatasetGroup, ...]:
        """Alias for :attr:`groups`."""
        return self.groups

    @property
    def affected(self) -> tuple[str, ...]:
        """All affected dataset names, in manifest order."""
        return tuple(
            dataset for group in self.groups for dataset in group.datasets
        )

    @property
    def affected_datasets(self) -> tuple[str, ...]:
        """Alias for :attr:`affected`."""
        return self.affected

    @property
    def cyclic_groups(self) -> tuple[AffectedDatasetGroup, ...]:
        """Only the entries that represent dependency cycles."""
        return tuple(group for group in self.groups if group.cyclic)

    def to_dict(self) -> dict[str, Any]:
        return {
            "datasets": list(self.affected),
            "affected": [group.to_dict() for group in self.groups],
            "changes": [change.to_dict() for change in self.changes],
        }

    def as_dict(self) -> dict[str, Any]:
        """Alias for :meth:`to_dict`."""
        return self.to_dict()

    def __iter__(self) -> Iterator[AffectedDatasetGroup]:
        return iter(self.groups)

    def __len__(self) -> int:
        return len(self.groups)

    def __bool__(self) -> bool:
        return bool(self.groups)

    def __contains__(self, dataset: object) -> bool:
        if isinstance(dataset, AffectedDatasetGroup):
            return dataset in self.groups
        return dataset in self.affected

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            return self.to_dict()[key]
        return self.groups[key]

    def get(self, key: str, default: Any = None) -> Any:
        """Dictionary-style access to the serialized manifest."""
        return self.to_dict().get(key, default)


@dataclass(frozen=True)
class _Edge:
    source: str
    target: str
    attributes: tuple[tuple[str, str], ...]


@dataclass
class _View:
    nodes: dict[str, dict[str, Any]]
    edges: list[_Edge]
    directed: bool
    incident: set[str] = field(default_factory=set)
    adjacency: dict[str, set[str]] = field(default_factory=dict)

    def finish(self) -> None:
        adjacency: dict[str, set[str]] = {name: set() for name in self.nodes}
        for edge in self.edges:
            adjacency.setdefault(edge.source, set()).add(edge.target)
            if not self.directed:
                adjacency.setdefault(edge.target, set()).add(edge.source)
            self.incident.add(edge.source)
            self.incident.add(edge.target)
        self.adjacency = adjacency


def _is_default_name(value: Any) -> bool:
    """Whether a node actually is an unquoted DOT default statement.

    ``node [shape=box]`` declares defaults and is not a dataset, while
    ``"node"`` is a perfectly valid quoted dataset name.
    """
    text = str(value)
    return text in {"graph", "node", "edge"}


def _subgraph_node_names(graph: Graph) -> list[str]:
    names: list[str] = []
    for node in graph.get_nodes():
        if _is_default_name(node.get_name()):
            continue
        names.append(_canonical_name(node.get_name()))
    for subgraph in graph.get_subgraph_list():
        names.extend(_subgraph_node_names(subgraph))
    return names


def _expand_endpoint(value: Any) -> list[tuple[str, str | None]]:
    """Expand an edge endpoint into (dataset, port) pairs."""
    if isinstance(value, FrozenDict):
        names = _subgraph_node_names(Graph(obj_dict=dict(value)))
        return [(name, None) for name in names]
    return [_endpoint_parts(value)]


def _collect(graph: Graph, predicate: AttributePredicate) -> _View:
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[_Edge] = []

    def walk(current: Graph) -> None:
        for node in current.get_nodes():
            if _is_default_name(node.get_name()):
                continue
            name = _canonical_name(node.get_name())
            nodes.setdefault(name, {}).update(node.get_attributes())

        for edge in current.get_edges():
            sources = _expand_endpoint(edge.get_source())
            targets = _expand_endpoint(edge.get_destination())
            attributes = list(
                _semantic_items(edge.get_attributes(), predicate)
            )
            for source, source_port in sources:
                nodes.setdefault(source, {})
                for target, target_port in targets:
                    nodes.setdefault(target, {})
                    edge_attributes = list(attributes)
                    if source_port is not None:
                        edge_attributes.append(("source_port", source_port))
                    if target_port is not None:
                        edge_attributes.append(("target_port", target_port))
                    edges.append(
                        _Edge(
                            source=source,
                            target=target,
                            attributes=tuple(sorted(edge_attributes)),
                        )
                    )

        for subgraph in current.get_subgraph_list():
            walk(subgraph)

    walk(graph)
    view = _View(
        nodes=nodes,
        edges=edges,
        directed=graph.get_type() == "digraph",
    )
    view.finish()
    return view


def _coerce_graph(value: GraphInput) -> Graph:
    if isinstance(value, Graph):
        return value
    if isinstance(value, os.PathLike):
        graphs = graph_from_dot_file(os.fspath(value))
    elif isinstance(value, bytes):
        graphs = graph_from_dot_data(value.decode("utf-8"))
    elif isinstance(value, str):
        if "{" not in value and os.path.exists(value):
            graphs = graph_from_dot_file(value)
        else:
            graphs = graph_from_dot_data(value)
    elif isinstance(value, Sequence):
        items = list(value)
        if len(items) == 1 and isinstance(items[0], Graph):
            return items[0]
        raise Error("Expected exactly one lineage graph")
    else:
        raise TypeError(f"Unsupported graph value: {type(value)!r}")

    if not graphs:
        raise Error("Could not parse the lineage graph")
    if len(graphs) != 1:
        raise Error("Expected exactly one lineage graph")
    return graphs[0]


def _diff_nodes(
    old: _View,
    new: _View,
    predicate: AttributePredicate,
) -> list[Cause]:
    causes: list[Cause] = []

    for name in sorted(set(old.nodes) & set(new.nodes)):
        old_sig = _semantic_items(old.nodes[name], predicate)
        new_sig = _semantic_items(new.nodes[name], predicate)
        if old_sig != new_sig:
            causes.append(
                Cause(
                    kind=ATTRIBUTES_CHANGED,
                    dataset=name,
                    attributes=_changed_attributes(old_sig, new_sig),
                )
            )

    old_only = set(old.nodes) - set(new.nodes)
    new_only = set(new.nodes) - set(old.nodes)

    old_isolated = {name for name in old_only if name not in old.incident}
    new_isolated = {name for name in new_only if name not in new.incident}

    old_by_sig: dict[tuple[tuple[str, str], ...], list[str]] = {}
    for name in sorted(old_isolated):
        signature = _semantic_items(old.nodes[name], predicate)
        old_by_sig.setdefault(signature, []).append(name)

    matched_old: set[str] = set()
    matched_new: set[str] = set()
    for name in sorted(new_isolated):
        signature = _semantic_items(new.nodes[name], predicate)
        for candidate in old_by_sig.get(signature, []):
            if candidate not in matched_old:
                matched_old.add(candidate)
                matched_new.add(name)
                break

    for name in sorted(old_only - matched_old):
        causes.append(Cause(kind=DATASET_REMOVED, dataset=name))

    for name in sorted(new_only - matched_new):
        causes.append(Cause(kind=DATASET_ADDED, dataset=name))

    return causes


def _edge_map(view: _View) -> dict[tuple[str, str], set[Any]]:
    """Group edge signatures by endpoint pair.

    A dependency is identified by its endpoints and semantic
    attributes; re-stating the exact same edge twice does not create a
    second effective connection.
    """
    result: dict[tuple[str, str], set[Any]] = {}
    for edge in view.edges:
        key = (edge.source, edge.target)
        if not view.directed and key[0] > key[1]:
            key = (key[1], key[0])
        result.setdefault(key, set()).add(edge.attributes)
    return result


def _signature_attributes(
    signatures: Iterable[tuple[tuple[str, str], ...]],
) -> tuple[str, ...]:
    names: set[str] = set()
    for signature in signatures:
        names.update(key for key, _ in signature)
    return tuple(sorted(names))


def _diff_edges(old: _View, new: _View) -> list[Cause]:
    old_map = _edge_map(old)
    new_map = _edge_map(new)
    causes: list[Cause] = []

    for key in sorted(set(old_map) | set(new_map)):
        old_set = old_map.get(key, set())
        new_set = new_map.get(key, set())
        if old_set == new_set:
            continue

        removed = old_set - new_set
        added = new_set - old_set

        if removed and added and len(old_set) == len(new_set):
            attribute_names: set[str] = set()
            for old_sig, new_sig in zip(sorted(removed), sorted(added)):
                attribute_names.update(_changed_attributes(old_sig, new_sig))
            causes.append(
                Cause(
                    kind=CONNECTION_CHANGED,
                    connection=key,
                    attributes=tuple(sorted(attribute_names)),
                )
            )
            continue

        if removed:
            causes.append(
                Cause(
                    kind=CONNECTION_REMOVED,
                    connection=key,
                    attributes=_signature_attributes(removed),
                )
            )
        if added:
            causes.append(
                Cause(
                    kind=CONNECTION_ADDED,
                    connection=key,
                    attributes=_signature_attributes(added),
                )
            )

    return causes


def _cause_key(cause: Cause) -> tuple[Any, ...]:
    return (
        _CAUSE_PRIORITY.get(cause.kind, 99),
        cause.kind,
        cause.dataset or "",
        cause.connection or ("", ""),
        cause.attributes,
    )


def _seed_for(cause: Cause) -> tuple[str, tuple[str, ...]] | None:
    if cause.connection is not None:
        source, target = cause.connection
        return target, (source, target)
    if cause.kind == ATTRIBUTES_CHANGED and cause.dataset is not None:
        return cause.dataset, (cause.dataset,)
    return None


def _impact_adjacency(view: _View) -> dict[str, set[str]]:
    """Forward-only dependency edges as seen in one graph version."""
    adjacency: dict[str, set[str]] = {name: set() for name in view.nodes}
    for name, neighbors in view.adjacency.items():
        adjacency.setdefault(name, set()).update(neighbors)
    return adjacency


def _propagation_view(cause: Cause) -> str:
    """Which graph a cause's effect should be followed through.

    Additions and changes are only visible in the new version, so they
    propagate through it.  Removals no longer exist there; their
    consumers are found by following the old version.
    """
    if cause.kind == CONNECTION_REMOVED:
        return "old"
    return "new"


def _best_reasons(
    adjacencies: Mapping[str, Mapping[str, set[str]]],
    seeds: Iterable[tuple[str, tuple[str, ...], Cause]],
) -> dict[str, ReasonPath]:
    """Find the shortest, strongest reason path for every reachable node."""
    best: dict[str, ReasonPath] = {}

    by_view: dict[str, list[tuple[str, tuple[str, ...], Cause]]] = {}
    for name, path, cause in seeds:
        by_view.setdefault(_propagation_view(cause), []).append(
            (name, path, cause)
        )

    for view, view_seeds in by_view.items():
        adjacency = adjacencies[view]
        for name, reason in _view_reasons(adjacency, view_seeds):
            current = best.get(name)
            if current is None or _reason_key(reason) < _reason_key(current):
                best[name] = reason

    return best


def _view_reasons(
    adjacency: Mapping[str, set[str]],
    seeds: Iterable[tuple[str, tuple[str, ...], Cause]],
) -> Iterator[tuple[str, ReasonPath]]:
    """Shortest-path search over one graph version."""
    best: dict[str, ReasonPath] = {}
    heap: list[tuple[int, tuple[Any, ...], tuple[str, ...], str]] = []

    def push(name: str, reason: ReasonPath) -> None:
        heapq.heappush(
            heap,
            (
                len(reason.path),
                _cause_key(reason.cause),
                reason.path,
                name,
            ),
        )

    for name, path, cause in seeds:
        reason = ReasonPath(path=path, cause=cause)
        current = best.get(name)
        if current is None or _reason_key(reason) < _reason_key(current):
            best[name] = reason
            push(name, reason)

    while heap:
        _, _, _, name = heapq.heappop(heap)
        popped = best.get(name)
        if popped is None:
            continue
        for neighbor in sorted(adjacency.get(name, ())):
            candidate = ReasonPath(
                path=popped.path + (neighbor,),
                cause=popped.cause,
            )
            current = best.get(neighbor)
            if current is None or _reason_key(candidate) < _reason_key(
                current
            ):
                best[neighbor] = candidate
                push(neighbor, candidate)

    yield from best.items()


def _reason_key(reason: ReasonPath) -> tuple[Any, ...]:
    return (
        len(reason.path),
        _cause_key(reason.cause),
        reason.path,
    )


def _strongly_connected_components(
    adjacency: Mapping[str, set[str]], nodes: Iterable[str]
) -> list[set[str]]:
    """Iterative Kosaraju SCC decomposition, safe for deep graphs."""
    node_set = set(nodes)
    for targets in adjacency.values():
        node_set.update(targets)

    visited: set[str] = set()
    finish_order: list[str] = []
    for root in sorted(node_set):
        if root in visited:
            continue
        visited.add(root)
        stack: list[tuple[str, Iterator[str]]] = [
            (root, iter(sorted(adjacency.get(root, ()))))
        ]
        while stack:
            node, neighbors = stack[-1]
            advanced = False
            for candidate in neighbors:
                if candidate not in visited:
                    visited.add(candidate)
                    stack.append(
                        (
                            candidate,
                            iter(sorted(adjacency.get(candidate, ()))),
                        )
                    )
                    advanced = True
                    break
            if not advanced:
                finish_order.append(node)
                stack.pop()

    reverse: dict[str, set[str]] = {name: set() for name in node_set}
    for source, targets in adjacency.items():
        for target in targets:
            reverse.setdefault(target, set()).add(source)

    assigned: set[str] = set()
    components: list[set[str]] = []
    for root in reversed(finish_order):
        if root in assigned:
            continue
        component = {root}
        assigned.add(root)
        component_stack = [root]
        while component_stack:
            node = component_stack.pop()
            for neighbor in sorted(reverse.get(node, ())):
                if neighbor not in assigned:
                    assigned.add(neighbor)
                    component.add(neighbor)
                    component_stack.append(neighbor)
        components.append(component)

    return components


def _cycle_groups(view: _View) -> dict[str, set[str]]:
    """Map each dataset to its dependency-cycle group.

    A cycle can only be recomputed as a unit, so grouping follows the
    strongly connected components of the version being recomputed.
    """
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    if view.directed:
        for component in _strongly_connected_components(
            view.adjacency, view.nodes
        ):
            if len(component) < 2:
                (only,) = component
                if only not in view.adjacency.get(only, set()):
                    continue
            first = sorted(component)[0]
            for member in component:
                union(first, member)

    grouped: dict[str, set[str]] = {}
    for node in sorted(parent):
        grouped.setdefault(find(node), set()).add(node)
    return {node: group for group in grouped.values() for node in group}


def _path_inside(
    adjacency: Mapping[str, set[str]],
    start: str,
    goal: str,
    allowed: set[str],
) -> tuple[str, ...] | None:
    if start == goal:
        return (start,)
    queue: deque[tuple[str, tuple[str, ...]]] = deque([(start, (start,))])
    seen = {start}
    while queue:
        current, path = queue.popleft()
        for neighbor in sorted(adjacency.get(current, ())):
            if neighbor not in allowed or neighbor in seen:
                continue
            next_path = path + (neighbor,)
            if neighbor == goal:
                return next_path
            seen.add(neighbor)
            queue.append((neighbor, next_path))
    return None


def recompute_manifest(
    old: GraphInput,
    new: GraphInput,
    *,
    computed_attributes: Iterable[str] | None = None,
    ignored_attributes: Iterable[str] | None = None,
    attribute_predicate: AttributePredicate | None = None,
) -> RecomputeManifest:
    """Compare two lineage DOT versions and build a recompute manifest.

    Args:
        old: The previous lineage graph.  May be a :class:`pydot.Graph`
            (or :class:`pydot.Dot`), DOT source text, DOT file contents
            as bytes, or a sequence containing exactly one graph.
        new: The current lineage graph, accepted in the same forms.
        computed_attributes: If given, treat only these attributes as
            semantic.  By default every attribute except the built-in
            presentation attributes is semantic.
        ignored_attributes: Extra attribute names to treat as cosmetic.
            The built-in presentation defaults remain ignored.
        attribute_predicate: Optional callback deciding whether an
            attribute name is cosmetic.  It receives the lowercase
            attribute name; returning ``True`` means "ignore".

    Returns:
        A :class:`RecomputeManifest`.  Each
        :class:`AffectedDatasetGroup` lists the datasets to recompute
        and at least one :class:`ReasonPath`; cycle members are emitted
        together exactly once.

    Raises:
        pydot.Error: If an input does not contain exactly one graph.
    """
    extra_ignored = {name.lower() for name in ignored_attributes or ()}

    if attribute_predicate is not None:
        base_predicate = attribute_predicate
        predicate: AttributePredicate = lambda name: (  # noqa: E731
            name in extra_ignored or base_predicate(name)
        )
    elif computed_attributes is not None:
        computed = {name.lower() for name in computed_attributes}
        predicate = lambda name: (  # noqa: E731
            name in extra_ignored or name not in computed
        )
    else:
        ignored = set(PRESENTATION_ATTRIBUTES)
        ignored.update(extra_ignored)
        predicate = ignored.__contains__

    old_view = _collect(_coerce_graph(old), predicate)
    new_view = _collect(_coerce_graph(new), predicate)

    causes = sorted(
        _diff_nodes(old_view, new_view, predicate)
        + _diff_edges(old_view, new_view),
        key=_cause_key,
    )

    adjacency = _impact_adjacency(new_view)
    adjacencies = {
        "old": _impact_adjacency(old_view),
        "new": adjacency,
    }
    seeds = []
    for cause in causes:
        if cause.kind == DATASET_REMOVED:
            # The dataset itself no longer exists; only its consumers
            # matter, and those are covered by the removed connections.
            continue
        if cause.kind == DATASET_ADDED and cause.dataset is not None:
            if cause.dataset in new_view.incident:
                # A new source or intermediate is not itself recomputed;
                # the connection cause already seeds its consumers.
                continue
        seed = _seed_for(cause)
        if seed is not None:
            seeds.append((seed[0], seed[1], cause))

    best = _best_reasons(adjacencies, seeds)
    cycle_groups = _cycle_groups(new_view)

    def is_ignorable_rename(node: str) -> bool:
        if node not in old_view.nodes or node not in new_view.nodes:
            return False
        if node in old_view.incident or node in new_view.incident:
            return False
        return _semantic_items(old_view.nodes[node], predicate) == (
            _semantic_items(new_view.nodes[node], predicate)
        )

    current_nodes = set(new_view.nodes)
    affected = {
        node
        for node in best
        if node in current_nodes and not is_ignorable_rename(node)
    }

    groups: list[AffectedDatasetGroup] = []
    grouped_nodes: set[str] = set()
    groups_by_root: dict[str, set[str]] = {}

    for node in sorted(affected):
        group = cycle_groups.get(node)
        if group is None:
            continue
        current_group = group & current_nodes
        if len(current_group) < 2:
            continue
        root = sorted(current_group)[0]
        groups_by_root.setdefault(root, set()).update(current_group)

    for root in sorted(groups_by_root):
        members = tuple(sorted(groups_by_root[root]))
        grouped_nodes.update(members)
        reasons: list[ReasonPath] = []
        seen: set[tuple[tuple[str, ...], Cause]] = set()
        for member in members:
            reason = best.get(member)
            if reason is None:
                anchor = next((m for m in members if m in best), None)
                if anchor is None:
                    continue
                base = best[anchor]
                suffix = _path_inside(adjacency, anchor, member, set(members))
                reason = (
                    base
                    if suffix is None
                    else ReasonPath(
                        path=base.path + suffix[1:],
                        cause=base.cause,
                    )
                )
            key = (reason.path, reason.cause)
            if key not in seen:
                seen.add(key)
                reasons.append(reason)
        if not reasons:
            continue
        reasons.sort(key=_reason_key)
        groups.append(
            AffectedDatasetGroup(
                datasets=members,
                reasons=tuple(reasons),
                cyclic=True,
            )
        )

    for node in sorted(affected - grouped_nodes):
        reason = best[node]
        groups.append(
            AffectedDatasetGroup(
                datasets=(node,),
                reasons=(reason,),
                cyclic=False,
            )
        )

    groups.sort(key=lambda group: (group.datasets, not group.cyclic))
    return RecomputeManifest(groups=tuple(groups), changes=tuple(causes))
