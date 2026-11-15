# SPDX-FileCopyrightText: 2024 pydot contributors
#
# SPDX-License-Identifier: MIT

"""Column-level lineage tracing for port-aware operator graphs.

A data pipeline can be modelled as a DOT graph in which each node is an
operator and each edge connects an output port to an input port, for
example ``orders:amount -> calc:amount``.  This module traces the source
columns that actually reach a report column, taking the port carried by
every endpoint into account.

Operators that change the shape of their input declare a mapping from
input ports to the output ports they feed::

    graph.set_operator_mapping(
        "calc",
        {"amount": ["total"], "tax": ["total"]},
    )
    result = graph.trace_lineage("report:total")

An output port that is not mentioned by an operator mapping is an
unknown boundary: tracing stops there and reports the operator's input
ports, instead of pretending that every upstream column reaches that
output.

When several upstream paths reach the same port, the result contains the
complete set of source columns but only a bounded number of witness
paths (``max_evidence``).  This keeps expensive lineage results useful
without allowing path enumeration to grow without limit.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeAlias

from pydot.exceptions import Error

if TYPE_CHECKING:
    from pydot.core import Graph

__all__ = [
    "Column",
    "ColumnLineage",
    "EvidencePath",
    "LineageResult",
    "OperatorPortMapping",
    "UnknownBoundary",
    "trace_lineage",
]

PortName: TypeAlias = str | None
"""A port name, or ``None`` for an endpoint without a port."""

_QUOTED_ID = re.compile(r'^"(.*)"$', re.S)


def _unquote(value: str) -> str:
    """Remove one layer of double quotes from a DOT identifier."""
    stripped = value.strip()
    match = _QUOTED_ID.match(stripped)
    return match.group(1) if match else stripped


def _split_endpoint(endpoint: Any) -> tuple[str, PortName]:
    """Split a DOT edge endpoint into a node name and a port name.

    ``node:port`` selects a port.  A third component is a compass point
    (``node:port:compass``) and does not identify a data port, so it is
    ignored here.  Colons inside quoted identifiers are preserved.
    """
    text = str(endpoint)
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False

    for char in text:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\" and quoted:
            current.append(char)
            escaped = True
        elif char == '"':
            current.append(char)
            quoted = not quoted
        elif char == ":" and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))

    node = _unquote(parts[0])
    if len(parts) == 1:
        return node, None
    port = _unquote(parts[1]) if parts[1].strip() else None
    return node, port


def _validate_port(port: Any) -> PortName:
    if port is not None and not isinstance(port, str):
        raise TypeError(
            f"port names must be strings or None, not {type(port).__name__}"
        )
    return port


def _normalize_ports(
    value: str | PortName | Iterable[PortName],
) -> tuple[PortName, ...]:
    """Turn a single port or an iterable of ports into a tuple."""
    if value is None or isinstance(value, str):
        return (_validate_port(value),)

    if not isinstance(value, Iterable):
        raise TypeError(
            "port mappings must contain a port name or an iterable "
            f"of port names, not {type(value).__name__}"
        )

    ports = [_validate_port(port) for port in value]
    # Deduplicate while preserving the declared order.
    return tuple(dict.fromkeys(ports))


def _invert(
    mapping: Mapping[PortName, tuple[PortName, ...]],
) -> dict[PortName, tuple[PortName, ...]]:
    inverted: dict[PortName, list[PortName]] = {}
    for key, values in mapping.items():
        for value in values:
            destinations = inverted.setdefault(value, [])
            if key not in destinations:
                destinations.append(key)
    return {key: tuple(values) for key, values in inverted.items()}


@dataclass(frozen=True)
class Column:
    """A node/port pair identifying a column in the pipeline."""

    node: str
    port: PortName = None

    @classmethod
    def parse(cls, value: Column | str) -> Column:
        """Build a column from ``"node"`` or ``"node:port"``."""
        if isinstance(value, Column):
            return value
        if not isinstance(value, str):
            raise TypeError(
                "a column must be a Column or a 'node:port' string, "
                f"not {type(value).__name__}"
            )
        return cls(*_split_endpoint(value))

    def __str__(self) -> str:
        if self.port is None:
            return self.node
        return f"{self.node}:{self.port}"


@dataclass(frozen=True)
class EvidencePath:
    """One witness path from the requested column back to a source.

    ``columns`` starts at the requested report column and then follows
    the flow upstream, so the final element is the reached source.  The
    path is evidence only; it is not an exhaustive list of all paths.
    """

    columns: tuple[Column, ...]

    def __str__(self) -> str:
        return " <- ".join(str(column) for column in self.columns)


@dataclass(frozen=True)
class UnknownBoundary:
    """An operator whose output port cannot be attributed to inputs."""

    operator: str
    output_port: PortName
    input_ports: tuple[PortName, ...]
    reason: str
    path: tuple[Column, ...] = ()

    def __str__(self) -> str:
        target = Column(self.operator, self.output_port)
        inputs = ", ".join(
            str(Column(self.operator, port)) for port in self.input_ports
        )
        return f"{target} has unknown sources ({inputs})"


@dataclass(frozen=True)
class LineageResult:
    """The upstream source columns reachable from one report column.

    ``sources`` is complete for the known part of the graph.  If
    ``unknown`` is non-empty, the result is deliberately incomplete:
    each entry identifies an operator whose port mapping did not say
    which inputs feed an output.  ``evidence`` holds up to the requested
    number of distinct witness paths, and ``evidence_truncated`` says
    whether more were found but omitted.
    """

    target: Column
    sources: tuple[Column, ...] = ()
    unknown: tuple[UnknownBoundary, ...] = ()
    evidence: tuple[EvidencePath, ...] = ()
    evidence_truncated: bool = False

    @property
    def is_complete(self) -> bool:
        """Whether every traversed operator had an explicit mapping."""
        return not self.unknown

    def source_names(self) -> list[str]:
        """Return the source columns as ``node:port`` strings."""
        return [str(source) for source in self.sources]


class OperatorPortMapping:
    """The input/output port relationship declared by one operator.

    The primary form maps each *input* port to the output ports it feeds,
    matching the way a pipeline is usually described::

        OperatorPortMapping({"amount": ["total"]})

    A mapping may also be declared in the opposite (output -> inputs)
    direction::

        OperatorPortMapping(outputs_to_inputs={"total": ["amount"]})

    or with the explicit :meth:`from_outputs` constructor.  Both forms
    are equivalent once constructed and can be used for tracing in
    either direction.

    In input -> outputs form, an output that is not mentioned anywhere
    means "unknown", so an operator can declare some outputs and leave
    others as unknown boundaries.  The output -> inputs form can also
    express an output with no upstream columns by mapping it to an empty
    sequence.
    """

    def __init__(
        self,
        inputs_to_outputs: Mapping[
            PortName, str | PortName | Iterable[PortName]
        ]
        | None = None,
        *,
        outputs_to_inputs: Mapping[
            PortName, str | PortName | Iterable[PortName]
        ]
        | None = None,
        direction: str | None = None,
    ) -> None:
        if inputs_to_outputs is not None and outputs_to_inputs is not None:
            raise ValueError(
                "declare either inputs_to_outputs or outputs_to_inputs"
            )
        if inputs_to_outputs is None and outputs_to_inputs is None:
            raise ValueError(
                "declare either inputs_to_outputs or outputs_to_inputs"
            )

        if direction is not None and inputs_to_outputs is not None:
            inputs_to_outputs, outputs_to_inputs = self._orient(
                inputs_to_outputs, direction
            )

        if outputs_to_inputs is not None:
            self._outputs_to_inputs = {
                _validate_port(output): _normalize_ports(inputs)
                for output, inputs in outputs_to_inputs.items()
            }
            self._inputs_to_outputs = _invert(self._outputs_to_inputs)
        else:
            assert inputs_to_outputs is not None
            self._inputs_to_outputs = {
                _validate_port(input_port): _normalize_ports(outputs)
                for input_port, outputs in inputs_to_outputs.items()
            }
            self._outputs_to_inputs = _invert(self._inputs_to_outputs)

    @staticmethod
    def _orient(
        mapping: Mapping[PortName, str | PortName | Iterable[PortName]],
        direction: str,
    ) -> tuple[
        Mapping[PortName, str | PortName | Iterable[PortName]] | None,
        Mapping[PortName, str | PortName | Iterable[PortName]] | None,
    ]:
        normalized = direction.replace("-", "_").lower()
        if normalized in ("inputs_to_outputs", "input_to_output", "in_to_out"):
            return mapping, None
        if normalized in (
            "outputs_to_inputs",
            "output_to_input",
            "out_to_in",
        ):
            return None, mapping
        raise ValueError(
            "direction must be 'inputs_to_outputs' or 'outputs_to_inputs', "
            f"not {direction!r}"
        )

    @classmethod
    def from_inputs(
        cls,
        inputs_to_outputs: Mapping[
            PortName, str | PortName | Iterable[PortName]
        ],
    ) -> OperatorPortMapping:
        """Build a mapping declared as input port -> output ports."""
        return cls(inputs_to_outputs=inputs_to_outputs)

    @classmethod
    def from_outputs(
        cls,
        outputs_to_inputs: Mapping[
            PortName, str | PortName | Iterable[PortName]
        ],
    ) -> OperatorPortMapping:
        """Build a mapping declared as output port -> input ports."""
        return cls(outputs_to_inputs=outputs_to_inputs)

    def inputs_for(self, output_port: PortName) -> tuple[PortName, ...] | None:
        """Return the inputs feeding ``output_port``, or ``None``.

        ``None`` means the output was not declared and therefore forms
        an unknown boundary.  An empty tuple is a positive declaration
        that the output has no upstream columns.
        """
        return self._outputs_to_inputs.get(output_port)

    def outputs_for(self, input_port: PortName) -> tuple[PortName, ...]:
        """Return the output ports fed by ``input_port``."""
        return self._inputs_to_outputs.get(input_port, ())

    def as_outputs_to_inputs(
        self,
    ) -> dict[PortName, tuple[PortName, ...]]:
        """Return a copy of the mapping in output -> inputs form."""
        return dict(self._outputs_to_inputs)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._outputs_to_inputs!r})"


def _coerce_mapping(
    mapping: OperatorPortMapping
    | Mapping[PortName, str | PortName | Iterable[PortName]],
    direction: str | None = None,
) -> OperatorPortMapping:
    if isinstance(mapping, OperatorPortMapping):
        if direction is not None:
            raise ValueError(
                "direction cannot be combined with an OperatorPortMapping"
            )
        return mapping
    if direction is None:
        direction = "inputs_to_outputs"
    return OperatorPortMapping(mapping, direction=direction)


class _GraphIndex:
    """Port-aware upstream adjacency derived from a pydot graph."""

    def __init__(self, graph: Graph) -> None:
        self.incoming: dict[Column, tuple[Column, ...]] = {}
        self.node_input_ports: dict[str, tuple[PortName, ...]] = {}
        self.nodes: set[str] = {
            _unquote(str(node.get_name())) for node in graph.get_nodes()
        }

        incoming: dict[Column, list[Column]] = {}
        node_ports: dict[str, list[PortName]] = {}

        for edge in graph.get_edges():
            source = self._endpoint(edge.get_source())
            destination = self._endpoint(edge.get_destination())
            if source is None or destination is None:
                # Subgraph endpoints do not identify a node/port column.
                continue

            self.nodes.add(source.node)
            self.nodes.add(destination.node)

            upstream = incoming.setdefault(destination, [])
            if source not in upstream:
                upstream.append(source)

            ports = node_ports.setdefault(destination.node, [])
            if destination.port not in ports:
                ports.append(destination.port)

        self.incoming = {
            column: tuple(sources) for column, sources in incoming.items()
        }
        self.node_input_ports = {
            node: tuple(ports) for node, ports in node_ports.items()
        }

    @staticmethod
    def _endpoint(value: Any) -> Column | None:
        if isinstance(value, dict):
            return None
        return Column.parse(str(value))


class ColumnLineage:
    """Trace column lineage over a pydot graph.

    ``mappings`` may be supplied directly here, or declared on the graph
    with :meth:`pydot.core.Graph.set_operator_mapping`.  Both sources
    are merged, with mappings passed to this constructor taking
    precedence.
    """

    def __init__(
        self,
        graph: Graph,
        mappings: Mapping[
            str,
            OperatorPortMapping
            | Mapping[PortName, str | PortName | Iterable[PortName]],
        ]
        | None = None,
        *,
        direction: str | None = None,
    ) -> None:
        self.graph = graph
        self._mappings: dict[str, OperatorPortMapping] = {}

        stored = graph.obj_dict.get("operator_mappings") or {}
        for operator, mapping in stored.items():
            self._mappings[operator] = _coerce_mapping(mapping)

        for operator, mapping in (mappings or {}).items():
            self._mappings[operator] = _coerce_mapping(mapping, direction)

        self._index = _GraphIndex(graph)
        self._reset_trace_state(0)
        self._effective_mappings = dict(self._mappings)

    def declare(
        self,
        operator: str,
        mapping: OperatorPortMapping
        | Mapping[PortName, str | PortName | Iterable[PortName]],
        *,
        direction: str | None = None,
        replace: bool = False,
    ) -> None:
        """Declare or replace an operator's input/output port mapping."""
        if not replace and operator in self._mappings:
            raise Error(f"operator mapping already declared for {operator!r}")
        self._mappings[operator] = _coerce_mapping(mapping, direction)

    declare_operator = declare

    def get_operator_mapping(
        self, operator: str
    ) -> OperatorPortMapping | None:
        """Return the mapping currently used for ``operator``."""
        return self._mappings.get(operator)

    def remove_operator_mapping(self, operator: str) -> bool:
        """Remove a local operator mapping, returning whether it existed."""
        return self._mappings.pop(operator, None) is not None

    def trace(
        self, column: Column | str, *, max_evidence: int = 10
    ) -> LineageResult:
        """Trace the source columns that reach ``column``.

        Edge endpoints are re-read on every call, so rewiring an edge to
        a different port of the same node is reflected in the result.
        """
        if max_evidence < 0:
            raise ValueError("max_evidence must be zero or greater")

        target = Column.parse(column)
        self._reset_trace_state(max_evidence)
        self._index = _GraphIndex(self.graph)
        self._effective_mappings = dict(self._mappings)

        self._collect(
            target,
            (target,),
            frozenset(),
            as_output=target not in self._index.incoming,
        )
        self._collect_evidence(
            target, as_output=target not in self._index.incoming
        )

        return LineageResult(
            target=target,
            sources=tuple(sorted(self._sources, key=self._sort_key)),
            unknown=tuple(self._unknowns.values()),
            evidence=tuple(self._evidence),
            evidence_truncated=self._evidence_truncated,
        )

    def sources_for(
        self, column: Column | str, *, max_evidence: int = 10
    ) -> tuple[Column, ...]:
        """Convenience wrapper returning only the known source columns."""
        return self.trace(column, max_evidence=max_evidence).sources

    def _reset_trace_state(self, max_evidence: int) -> None:
        self._sources: set[Column] = set()
        self._unknowns: dict[
            tuple[str, PortName, tuple[PortName, ...]], UnknownBoundary
        ] = {}
        self._evidence: list[EvidencePath] = []
        self._evidence_seen: set[tuple[Column, ...]] = set()
        self._evidence_truncated = False
        self._max_evidence = max_evidence
        self._memo: dict[tuple[Column, bool], frozenset[Column]] = {}

    @staticmethod
    def _sort_key(column: Column) -> tuple[str, bool, str]:
        return (
            column.node,
            column.port is not None,
            column.port or "",
        )

    @staticmethod
    def _port_sort_key(port: PortName) -> tuple[bool, str]:
        return (port is not None, port or "")

    def _add_source(self, column: Column) -> None:
        self._sources.add(column)

    def _add_evidence(self, path: tuple[Column, ...]) -> None:
        if path in self._evidence_seen:
            return
        self._evidence_seen.add(path)
        if len(self._evidence) >= self._max_evidence:
            self._evidence_truncated = True
            return
        self._evidence.append(EvidencePath(path))

    def _collect_evidence(self, target: Column, *, as_output: bool) -> None:
        """Record a bounded number of witness paths to known sources."""
        if self._max_evidence == 0:
            return

        trail: list[Column] = [target]

        def walk(
            column: Column,
            visiting: frozenset[tuple[Column, bool]],
            *,
            as_output: bool,
        ) -> None:
            key = (column, as_output)
            if self._evidence_truncated:
                return
            if key in visiting:
                return

            upstream = self._index.incoming.get(column, ())
            input_ports = self._index.node_input_ports.get(column.node, ())

            if not as_output:
                if upstream:
                    next_visiting = visiting | {key}
                    for source in upstream:
                        trail.append(source)
                        walk(source, next_visiting, as_output=True)
                        trail.pop()
                elif column in self._sources:
                    self._add_evidence(tuple(trail))
            elif not input_ports and not upstream:
                if (
                    column.node in self._index.nodes
                    and column in self._sources
                ):
                    self._add_evidence(tuple(trail))
            else:
                mapping = self._effective_mappings.get(column.node)
                inputs = (
                    mapping.inputs_for(column.port)
                    if mapping is not None
                    else None
                )
                if inputs is None:
                    return
                next_visiting = visiting | {key}
                for input_port in inputs:
                    input_column = Column(column.node, input_port)
                    trail.append(input_column)
                    walk(input_column, next_visiting, as_output=False)
                    trail.pop()

        walk(target, frozenset(), as_output=as_output)

    def _add_unknown(
        self,
        column: Column,
        input_ports: tuple[PortName, ...],
        path: tuple[Column, ...],
        reason: str,
    ) -> None:
        ordered_ports = tuple(sorted(input_ports, key=self._port_sort_key))
        key = (column.node, column.port, ordered_ports)
        if key in self._unknowns:
            return
        self._unknowns[key] = UnknownBoundary(
            operator=column.node,
            output_port=column.port,
            input_ports=ordered_ports,
            reason=reason,
            path=path,
        )

    def _collect(
        self,
        column: Column,
        path: tuple[Column, ...],
        visiting: frozenset[tuple[Column, bool]],
        *,
        as_output: bool,
    ) -> frozenset[Column]:
        key = (column, as_output)
        if key in visiting:
            return frozenset()
        if key in self._memo:
            return self._memo[key]

        upstream = self._index.incoming.get(column, ())
        input_ports = self._index.node_input_ports.get(column.node, ())

        if column.node not in self._index.nodes:
            # A node that is not part of the graph cannot be a source.
            result: frozenset[Column] = frozenset()
        elif not as_output:
            # An input column receives its value from its incoming edges.
            if upstream:
                next_visiting = visiting | {key}
                found: set[Column] = set()
                for source in upstream:
                    found.update(
                        self._collect(
                            source,
                            path + (source,),
                            next_visiting,
                            as_output=True,
                        )
                    )
                result = frozenset(found)
            elif input_ports or column.node not in self._index.nodes:
                # This is an input port of an operator, but no edge feeds
                # it, or the requested node does not exist.  Neither is a
                # source column.
                result = frozenset()
            else:
                self._add_source(column)
                result = frozenset({column})
        elif (
            not input_ports
            and not upstream
            and column.node in self._index.nodes
        ):
            # An isolated output node is a source relation.  If it has
            # incoming edges, it still has an operator mapping to consult.
            self._add_source(column)
            result = frozenset({column})
        else:
            # This is an output column of an operator.  Only the input
            # ports declared for that output may contribute to it.
            mapping = self._effective_mappings.get(column.node)
            inputs = (
                mapping.inputs_for(column.port)
                if mapping is not None
                else None
            )
            if inputs is None:
                reason = (
                    "operator has no port mapping"
                    if mapping is None
                    else "output port is not declared in the operator mapping"
                )
                self._add_unknown(column, input_ports, path, reason)
                result = frozenset()
            else:
                next_visiting = visiting | {key}
                found = set()
                for input_port in inputs:
                    input_column = Column(column.node, input_port)
                    found.update(
                        self._collect(
                            input_column,
                            path + (input_column,),
                            next_visiting,
                            as_output=False,
                        )
                    )
                result = frozenset(found)

        self._memo[key] = result
        return result


def trace_lineage(
    graph: Graph,
    column: Column | str,
    *,
    mappings: Mapping[
        str,
        OperatorPortMapping
        | Mapping[PortName, str | PortName | Iterable[PortName]],
    ]
    | None = None,
    direction: str | None = None,
    max_evidence: int = 10,
) -> LineageResult:
    """Trace the source columns reachable from ``column``.

    ``mappings`` maps operator names to their port relationships, as
    accepted by :class:`OperatorPortMapping`.  Mappings stored on the
    graph with ``Graph.set_operator_mapping`` are also used.
    """
    return ColumnLineage(graph, mappings, direction=direction).trace(
        column, max_evidence=max_evidence
    )
