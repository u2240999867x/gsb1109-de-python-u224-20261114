# SPDX-FileCopyrightText: 2024 pydot contributors
#
# SPDX-License-Identifier: MIT

"""Frozen dictionaries."""

from __future__ import annotations

import copy
from typing import Any, Dict, NamedTuple, Union


class Endpoint(NamedTuple):
    """Structured reference to a node, optionally with port and compass.

    A DOT node reference such as ``"sales:archive":id:sw`` consists of
    up to three colon-separated parts.  Splitting such a reference
    naively on ``:`` breaks quoted IDs whose names contain a colon.  An
    ``Endpoint`` keeps the logical node name, port and compass separately,
    with ``None`` marking a part that was not specified.

    As a named tuple it is hashable and can be used directly as a
    composite database key.  ``to_dict()`` returns a plain, JSON-safe
    mapping of the same fields.
    """

    name: str
    port: str | None = None
    compass: str | None = None

    @property
    def node(self) -> str:
        """Alias for :attr:`name`."""
        return self.name

    @property
    def id(self) -> str:
        """Alias for :attr:`name`."""
        return self.name

    @property
    def compass_point(self) -> str | None:
        """Alias for :attr:`compass`."""
        return self.compass

    @property
    def position(self) -> str | None:
        """Alias for :attr:`compass` (the Graphviz compass position)."""
        return self.compass

    @property
    def is_default(self) -> bool:
        """Whether this reference is a bare node with no port or compass."""
        return self.port is None and self.compass is None

    @property
    def default(self) -> bool:
        """Alias for :attr:`is_default`."""
        return self.is_default

    @property
    def default_state(self) -> bool:
        """Alias for :attr:`is_default`."""
        return self.is_default

    def to_dict(self) -> dict[str, str | None]:
        """Return the endpoint key as a plain mapping."""
        return {
            "node": self.name,
            "port": self.port,
            "compass": self.compass,
        }

    def as_dict(self) -> dict[str, str | None]:
        """Alias for :meth:`to_dict`."""
        return self.to_dict()

    def key(self) -> tuple[str, str | None, str | None]:
        """Return the composite key as a plain tuple."""
        return (self.name, self.port, self.compass)

    @property
    def unquoted_name(self) -> str:
        """The node part with its surrounding DOT quotes removed."""
        return unquote_id(self.name)

    @property
    def bare_name(self) -> str:
        """Alias for :attr:`unquoted_name`."""
        return self.unquoted_name

    @classmethod
    def from_string(cls, ref: str) -> Endpoint:
        """Parse a DOT node reference into an endpoint key."""
        return endpoint_from_ref(ref)

    @classmethod
    def parse(cls, ref: str) -> Endpoint:
        """Alias for :meth:`from_string`."""
        return cls.from_string(ref)

    def to_string(self) -> str:
        """Return the endpoint key as a DOT node reference."""
        from pydot.core import format_node_ref

        return format_node_ref(self.name, self.port, self.compass)


def unquote_id(value: str) -> str:
    """Remove the enclosing DOT quotes from an ID, if present.

    Only the two escapes that pydot itself emits inside quoted IDs are
    decoded, so ordinary backslashes in names are left untouched.
    """
    if len(value) < 2 or not (value.startswith('"') and value.endswith('"')):
        return value
    body = value[1:-1]
    out = []
    i = 0
    while i < len(body):
        char = body[i]
        if char == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in ('"', "\\"):
                out.append(nxt)
                i += 2
                continue
        out.append(char)
        i += 1
    return "".join(out)


# Descriptive aliases for callers that prefer a more explicit name.
EndpointKey = Endpoint
StructuredEndpoint = Endpoint


def split_node_ref(ref: str) -> tuple[str, str | None, str | None]:
    """Split a DOT node reference into node, port and compass parts.

    Only colons outside double-quoted strings and HTML-like IDs act as
    separators, so a quoted node name such as ``"sales:archive"`` is kept
    intact.  Backslash-escaped quotes inside a quoted ID do not end the
    quoted span.  Missing parts are returned as ``None``.

    At most two separators are consumed: the first separates the node
    from the port, the second separates the port from the compass point.
    Any further colons remain part of the compass value.  A colon at the
    start or end of a component is not treated as a separator, matching
    DOT's tolerance for names such as ``a:``.
    """
    parts: list[str] = []
    start = 0
    in_quote = False
    html_depth = 0
    i = 0
    n = len(ref)

    while i < n:
        char = ref[i]
        if in_quote:
            if char == "\\" and i + 1 < n:
                i += 2
                continue
            if char == '"':
                in_quote = False
        elif html_depth:
            if char == "<":
                html_depth += 1
            elif char == ">":
                html_depth -= 1
        elif char == '"':
            in_quote = True
        elif char == "<":
            html_depth = 1
        elif char == ":" and len(parts) < 2:
            # Do not split on a leading colon, nor on a colon that would
            # leave an empty component (e.g. a trailing colon in a name).
            if i > start and i + 1 < n and ref[i + 1] != ":":
                parts.append(ref[start:i])
                start = i + 1
        i += 1

    parts.append(ref[start:])

    node = parts[0]
    port = parts[1] if len(parts) > 1 else None
    compass = parts[2] if len(parts) > 2 else None
    return node, port or None, compass or None


def endpoint_key(ref: str) -> Endpoint:
    """Build an :class:`Endpoint` key from a DOT node reference."""
    return endpoint_from_ref(ref)


# Alias spelling the same operation as a verb for callers that prefer it.
parse_endpoint = endpoint_key


def endpoint_from_ref(ref: str) -> Endpoint:
    """Build an :class:`Endpoint` key from a DOT node reference."""
    node, port, compass = split_node_ref(ref)
    return Endpoint(node, port, compass)


class FrozenDict(dict):  # type: ignore
    """Frozen dictionary, values are immutable after creation.

    Extended version of ASPN's Python Cookbook Recipe:
    https://code.activestate.com/recipes/414283/

    This version freezes dictionaries used as values within dictionaries."""

    _block_msg = "A frozendict cannot be modified."

    def __delitem__(self, key: Any) -> None:
        raise AttributeError(self._block_msg)

    def __setitem__(self, key: Any, value: Any) -> None:
        raise AttributeError(self._block_msg)

    def clear(self) -> None:
        raise AttributeError(self._block_msg)

    def pop(self, key: Any, default: Any = None) -> None:
        raise AttributeError(self._block_msg)

    def popitem(self) -> tuple[Any, Any]:
        raise AttributeError(self._block_msg)

    def setdefault(self, key: Any, default: Any = None) -> None:
        raise AttributeError(self._block_msg)

    def update(self, *E: Any, **F: Any) -> None:
        raise AttributeError(self._block_msg)

    @staticmethod
    def _freeze_arg(in_arg: Any) -> Any:
        if not isinstance(in_arg, dict):
            return in_arg
        arg = copy.copy(in_arg)
        for k, v in arg.items():
            if isinstance(v, FrozenDict):
                continue
            elif isinstance(v, dict):
                arg[k] = FrozenDict(v)
            elif isinstance(v, list):
                arg[k] = tuple(
                    FrozenDict(e) if isinstance(e, dict) else e for e in v
                )
        return arg

    def __new__(cls, *args: Any, **kw: Any) -> Any:
        new = dict.__new__(cls)
        args_ = [cls._freeze_arg(arg) for arg in args]
        dict.__init__(new, *args_, **cls._freeze_arg(kw))
        return new

    def __init__(self, *args: Any, **kw: Any):
        pass

    def __reduce__(self) -> tuple[type[FrozenDict], tuple[dict[Any, Any]]]:
        """Unpickle through the constructor without assigning frozen items."""
        return type(self), (dict(self),)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, self.__class__):
            return hash(self) == hash(other)
        return NotImplemented

    def __ne__(self, other: Any) -> bool:
        return not self == other

    def __hash__(self) -> int:  # type: ignore
        try:
            return self._cached_hash  # type: ignore
        except AttributeError:
            self._cached_hash = hash(tuple(self.items()))
            return self._cached_hash

    def __repr__(self) -> str:
        dict_repr = dict.__repr__(self)
        return f"FrozenDict({dict_repr})"


# Backwards-compatible typing alias
AttributeDict = Dict[str, Any]
EdgeEndpoint = Union[str, int, float, FrozenDict]
