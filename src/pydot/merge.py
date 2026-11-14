# SPDX-FileCopyrightText: 2026 pydot contributors
#
# SPDX-License-Identifier: MIT

"""Merge DOT graphs delivered by several collection sources.

The merger keeps every source-local name in its own scope: nodes that
the reference material does not explicitly recognize are emitted as
``"<source>__<local name>"`` and never collapse together, even when
several sources use the same local name (for example three chains each
calling their temporary table ``staging``).

Only nodes that the reference explicitly maps to a canonical identity
are merged into that canonical node. When several sources claim
different values for the same attribute of a shared node, the clash is
reported as a property discrepancy instead of being silently resolved;
edges are always copied with their original source recorded as
provenance.

When one source-local node is mapped to more than one canonical
identity, the whole suspicious correspondence is reported separately
and none of the canonical nodes is swallowed: the local node stays
source-scoped and every candidate canonical identity is retained.

Reference files are JSON documents of the form::

    {
        "canonical_nodes": [
            {"id": "orders", "label": "official order table"}
        ],
        "correspondences": [
            {"source": "chain_a", "local": "orders",
             "canonical": "orders"},
            {"source": "chain_b", "local": "orders",
             "canonical": "orders"}
        ]
    }

Only top-level nodes and edges are in scope: nodes nested inside
subgraphs are not remapped, and edges ending on a subgraph keep that
endpoint unchanged.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import pydot

#: Node names produced by DOT ``graph``/``node``/``edge`` default
#: statements; they carry defaults rather than real entities.
DEFAULT_NODE_NAMES = frozenset(("graph", "node", "edge"))

#: Attribute used to record which source(s) an element came from.
PROVENANCE_ATTR = "provenance"

#: Attribute holding the machine-readable discrepancy record.
DISCREPANCY_ATTR = "discrepancies"

#: Attribute marking canonical nodes retained because of an unresolved
#: ambiguous correspondence.
UNRESOLVED_ATTR = "unresolved"


class MergeReferenceError(ValueError):
    """Raised when the reference material is inconsistent."""


@dataclass(frozen=True)
class NodeCorrespondence:
    """One approved ``(source, local node) -> canonical node`` rule."""

    source: str
    local_name: str
    canonical: str


@dataclass
class PropertyDiscrepancy:
    """Conflicting attribute values claimed for one canonical node."""

    canonical: str
    attribute: str
    values: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical": self.canonical,
            "attribute": self.attribute,
            "values": dict(self.values),
        }


@dataclass
class AmbiguousCorrespondence:
    """A local node mapped to several canonical identities."""

    source: str
    local_name: str
    canonical_candidates: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "local_name": self.local_name,
            "canonical_candidates": list(self.canonical_candidates),
        }


@dataclass
class MergeReport:
    """Outcome of a merge, including every conflict encountered."""

    merged_nodes: list[str] = field(default_factory=list)
    source_scoped_nodes: list[str] = field(default_factory=list)
    discrepancies: list[PropertyDiscrepancy] = field(default_factory=list)
    ambiguous_correspondences: list[AmbiguousCorrespondence] = field(
        default_factory=list
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "merged_nodes": list(self.merged_nodes),
            "source_scoped_nodes": list(self.source_scoped_nodes),
            "discrepancies": [item.to_dict() for item in self.discrepancies],
            "ambiguous_correspondences": [
                item.to_dict() for item in self.ambiguous_correspondences
            ],
        }


def scoped_name(source_id: str, local_name: str) -> str:
    """Return the source-scoped output name for a local node.

    A double underscore separates source and local name. Underscores
    are valid in unquoted DOT identifiers, so scoped names survive a
    DOT write/parse round-trip without being mistaken for ports.
    """
    return f"{source_id}__{local_name}"


def _dot_value(value: Any) -> str:
    """Normalize a value parsed from DOT into its plain string form."""
    text = str(value)
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        return text[1:-1]
    return text


def load_reference(
    data: Mapping[str, Any] | str | Path,
) -> tuple[frozenset[str], list[NodeCorrespondence]]:
    """Load and validate a JSON reference document.

    Returns the known canonical node identifiers and the approved
    correspondences. Raises :class:`MergeReferenceError` when a
    correspondence points at an unknown canonical identity.
    """

    if isinstance(data, (str, Path)):
        with open(data, encoding="utf-8") as handle:
            data = json.load(handle)

    if not isinstance(data, Mapping):
        raise MergeReferenceError(
            "reference must be a JSON object with 'canonical_nodes' "
            "and 'correspondences'"
        )

    canonical_nodes = frozenset(
        str(node["id"]) for node in data.get("canonical_nodes", [])
    )

    correspondences: list[NodeCorrespondence] = []
    for entry in data.get("correspondences", []):
        correspondence = NodeCorrespondence(
            source=str(entry["source"]),
            local_name=str(entry["local"]),
            canonical=str(entry["canonical"]),
        )
        if correspondence.canonical not in canonical_nodes:
            raise MergeReferenceError(
                f"correspondence {correspondence.source}:"
                f"{correspondence.local_name} points at unknown "
                f"canonical node {correspondence.canonical!r}"
            )
        correspondences.append(correspondence)

    return canonical_nodes, correspondences


def _new_node_obj_dict(node: pydot.Node, name: str) -> dict[str, Any]:
    """Build an independent obj_dict without copying parent graphs."""
    return {
        "name": name,
        "port": node.obj_dict.get("port"),
        "type": "node",
        "parent_graph": None,
        "sequence": None,
        "attributes": dict(node.get_attributes()),
    }


def _new_edge_obj_dict(
    edge: pydot.Edge,
    points: tuple[Any, Any],
    attributes: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "points": points,
        "type": "edge",
        "parent_graph": None,
        "sequence": None,
        "attributes": dict(attributes),
    }


def _remap_endpoint(
    endpoint: Any, name_mapping: Mapping[str, str]
) -> Any:
    """Remap one edge endpoint, preserving any ``:port`` suffix."""

    if not isinstance(endpoint, str):
        # Subgraph endpoints (FrozenDict) are passed through untouched.
        return endpoint

    if endpoint in name_mapping:
        return name_mapping[endpoint]

    head, separator, port = endpoint.partition(":")
    if not separator or head not in name_mapping:
        return endpoint
    return f"{name_mapping[head]}:{port}"


class DotMerger:
    """Merge source DOT graphs under the rules of a reference document."""

    def __init__(
        self,
        reference: Mapping[str, Any] | str | Path,
        conflict_attributes: Sequence[str] = ("retention_days",),
    ) -> None:
        canonical_nodes, correspondences = load_reference(reference)
        self._canonical_nodes = canonical_nodes
        self._conflict_attributes = tuple(conflict_attributes)

        raw_groups: dict[tuple[str, str], list[str]] = {}
        for correspondence in correspondences:
            key = (correspondence.source, correspondence.local_name)
            raw_groups.setdefault(key, []).append(
                correspondence.canonical
            )

        self._unambiguous: dict[tuple[str, str], str] = {}
        self._ambiguous: dict[tuple[str, str], list[str]] = {}
        for key, canonicals in raw_groups.items():
            unique = list(dict.fromkeys(canonicals))
            if len(unique) == 1:
                self._unambiguous[key] = unique[0]
            else:
                self._ambiguous[key] = unique

        self._sources: list[tuple[str, pydot.Dot]] = []
        self._source_ids: set[str] = set()

    def add_source(
        self,
        source_id: str,
        graph: pydot.Dot | str | Path,
    ) -> None:
        """Register one collection source and its DOT graph."""

        if not source_id:
            raise ValueError("source_id must be a non-empty string")
        if source_id in self._source_ids:
            raise ValueError(f"duplicate source id: {source_id!r}")

        if isinstance(graph, pydot.Dot):
            parsed = graph
        elif isinstance(graph, Path) or (
            isinstance(graph, str) and "\n" not in graph and "{" not in graph
        ):
            graphs = pydot.graph_from_dot_file(str(graph))
            if not graphs:
                raise ValueError(
                    f"no graph parsed from source {source_id!r}"
                )
            parsed = graphs[0]
        elif isinstance(graph, str):
            graphs = pydot.graph_from_dot_data(graph)
            if not graphs:
                raise ValueError(
                    f"no graph parsed from source {source_id!r}"
                )
            parsed = graphs[0]
        else:
            raise TypeError(
                "graph must be a pydot.Dot, DOT text or a path to a "
                f".dot file, got {type(graph)!r}"
            )

        self._source_ids.add(source_id)
        self._sources.append((source_id, parsed))

    def merge(
        self, graph_name: str = "merged"
    ) -> tuple[pydot.Dot, MergeReport]:
        """Build the merged graph and its conflict report."""

        report = MergeReport()
        graph_type = self._combined_graph_type()
        merged = pydot.Dot(graph_name, graph_type=graph_type)

        # canonical name -> merge state accumulated across sources
        gathered: dict[str, dict[str, Any]] = {}
        emitted_scoped: set[str] = set()
        reported_ambiguous: set[tuple[str, str]] = set()
        retained_unresolved: set[str] = set()

        for source_id, source_graph in self._sources:
            name_mapping = self._plan_source_names(
                source_id, source_graph, report, reported_ambiguous
            )
            self._merge_nodes(
                source_id,
                source_graph,
                name_mapping,
                gathered,
                emitted_scoped,
                merged,
            )
            self._merge_edges(source_id, source_graph, name_mapping, merged)

        self._emit_canonical_nodes(merged, gathered, report)
        self._retain_ambiguous_canonicals(
            merged, report, gathered, retained_unresolved
        )
        self._finalize_report(
            report, gathered, emitted_scoped, retained_unresolved
        )
        return merged, report

    def _combined_graph_type(self) -> str:
        for _, graph in self._sources:
            if graph.get_graph_type() == "digraph":
                return "digraph"
        return "graph"

    def _plan_source_names(
        self,
        source_id: str,
        graph: pydot.Dot,
        report: MergeReport,
        reported_ambiguous: set[tuple[str, str]],
    ) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for node in graph.get_node_list():
            local_name = node.get_name()
            if local_name in DEFAULT_NODE_NAMES:
                continue
            key = (source_id, local_name)
            if key in self._ambiguous:
                mapping[local_name] = scoped_name(source_id, local_name)
                if key not in reported_ambiguous:
                    reported_ambiguous.add(key)
                    report.ambiguous_correspondences.append(
                        AmbiguousCorrespondence(
                            source=source_id,
                            local_name=local_name,
                            canonical_candidates=list(
                                self._ambiguous[key]
                            ),
                        )
                    )
            elif key in self._unambiguous:
                mapping[local_name] = self._unambiguous[key]
            else:
                mapping[local_name] = scoped_name(source_id, local_name)
        return mapping

    def _merge_nodes(
        self,
        source_id: str,
        graph: pydot.Dot,
        name_mapping: Mapping[str, str],
        gathered: dict[str, dict[str, Any]],
        emitted_scoped: set[str],
        merged: pydot.Dot,
    ) -> None:
        for node in graph.get_node_list():
            local_name = node.get_name()
            if local_name in DEFAULT_NODE_NAMES:
                continue
            output_name = name_mapping[local_name]

            if output_name in self._canonical_nodes:
                self._gather_canonical(
                    source_id, output_name, node, gathered
                )
                continue

            if output_name in emitted_scoped:
                continue
            output_node = pydot.Node(
                obj_dict=_new_node_obj_dict(node, output_name)
            )
            output_node.set(PROVENANCE_ATTR, source_id)
            merged.add_node(output_node)
            emitted_scoped.add(output_name)

    def _gather_canonical(
        self,
        source_id: str,
        canonical: str,
        node: pydot.Node,
        gathered: dict[str, dict[str, Any]],
    ) -> None:
        state = gathered.setdefault(
            canonical,
            {
                "template": node,
                "attributes": {},
                "claimants": {},
                "provenance": [],
                "conflicts": {},
            },
        )
        state["provenance"].append(source_id)

        target = state["attributes"]
        claimants = state["claimants"]
        conflicts = state["conflicts"]

        for attr_name, raw_value in node.get_attributes().items():
            value = _dot_value(raw_value)
            if attr_name not in target:
                target[attr_name] = value
                claimants[attr_name] = source_id
                continue
            if target[attr_name] == value:
                continue
            if attr_name not in self._conflict_attributes:
                # Untracked clashes keep the first claimed value.
                continue

            bucket = conflicts.setdefault(
                attr_name,
                {claimants[attr_name]: target[attr_name]},
            )
            bucket[source_id] = value

    def _emit_canonical_nodes(
        self,
        merged: pydot.Dot,
        gathered: dict[str, dict[str, Any]],
        report: MergeReport,
    ) -> None:
        for canonical, state in gathered.items():
            output_node = pydot.Node(
                obj_dict=_new_node_obj_dict(state["template"], canonical)
            )
            output_node.obj_dict["attributes"] = dict(state["attributes"])
            output_node.set(
                PROVENANCE_ATTR,
                ",".join(dict.fromkeys(state["provenance"])),
            )
            if state["conflicts"]:
                output_node.set(
                    DISCREPANCY_ATTR,
                    json.dumps(
                        state["conflicts"], separators=(",", ":")
                    ),
                )
                for attr_name, values in state["conflicts"].items():
                    report.discrepancies.append(
                        PropertyDiscrepancy(
                            canonical=canonical,
                            attribute=attr_name,
                            values=dict(values),
                        )
                    )
            merged.add_node(output_node)

    def _retain_ambiguous_canonicals(
        self,
        merged: pydot.Dot,
        report: MergeReport,
        gathered: dict[str, dict[str, Any]],
        retained_unresolved: set[str],
    ) -> None:
        for item in report.ambiguous_correspondences:
            for canonical in item.canonical_candidates:
                if canonical in gathered:
                    # Already legitimately claimed by another source.
                    continue
                retained = pydot.Node(
                    canonical,
                    **{
                        PROVENANCE_ATTR: item.source,
                        UNRESOLVED_ATTR: "true",
                    },
                )
                merged.add_node(retained)
                gathered[canonical] = {
                    "template": retained,
                    "attributes": dict(retained.get_attributes()),
                    "claimants": {},
                    "provenance": [item.source],
                    "conflicts": {},
                }
                retained_unresolved.add(canonical)

    def _finalize_report(
        self,
        report: MergeReport,
        gathered: dict[str, dict[str, Any]],
        emitted_scoped: set[str],
        retained_unresolved: set[str],
    ) -> None:
        report.merged_nodes = sorted(
            name
            for name in gathered
            if name in self._canonical_nodes
            and name not in retained_unresolved
        )
        report.source_scoped_nodes = sorted(emitted_scoped)
        report.discrepancies.sort(
            key=lambda item: (item.canonical, item.attribute)
        )
        report.ambiguous_correspondences.sort(
            key=lambda item: (item.source, item.local_name)
        )

    def _merge_edges(
        self,
        source_id: str,
        graph: pydot.Dot,
        name_mapping: Mapping[str, str],
        merged: pydot.Dot,
    ) -> None:
        for edge in graph.get_edge_list():
            source = _remap_endpoint(edge.get_source(), name_mapping)
            destination = _remap_endpoint(
                edge.get_destination(), name_mapping
            )
            attributes = dict(edge.get_attributes())
            attributes[PROVENANCE_ATTR] = source_id
            merged.add_edge(
                pydot.Edge(
                    obj_dict=_new_edge_obj_dict(
                        edge, (source, destination), attributes
                    )
                )
            )


def merge_dot_sources(
    sources: Sequence[tuple[str, pydot.Dot | str | Path]],
    reference: Mapping[str, Any] | str | Path,
    *,
    graph_name: str = "merged",
    conflict_attributes: Sequence[str] = ("retention_days",),
) -> tuple[pydot.Dot, MergeReport]:
    """Convenience wrapper around :class:`DotMerger`."""

    merger = DotMerger(
        reference, conflict_attributes=conflict_attributes
    )
    for source_id, graph in sources:
        merger.add_source(source_id, graph)
    return merger.merge(graph_name=graph_name)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge multi-source DOT graphs against a reference."
    )
    parser.add_argument(
        "--reference", required=True, help="JSON reference document"
    )
    parser.add_argument(
        "--source",
        action="append",
        required=True,
        metavar="ID=PATH",
        help="source id and DOT file, repeatable",
    )
    parser.add_argument("--out", required=True, help="merged DOT output")
    parser.add_argument(
        "--report", required=True, help="merge report JSON output"
    )
    args = parser.parse_args(argv)

    merger = DotMerger(args.reference)
    for spec in args.source:
        source_id, delimiter, path = spec.partition("=")
        if not delimiter:
            parser.error(f"--source expects ID=PATH, got {spec!r}")
        merger.add_source(source_id, Path(path))

    graph, report = merger.merge()
    Path(args.out).write_text(graph.to_string(), encoding="utf-8")
    Path(args.report).write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
