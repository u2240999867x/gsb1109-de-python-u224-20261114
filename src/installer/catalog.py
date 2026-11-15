"""A searchable catalog of entry points declared by multiple releases.

Every declaration in an ``entry_points.txt`` file is stored together with the
release that declared it (distribution identity and version), its entry point
group, its command name, and its declaration conditions (requested extras and
a :pep:`508` environment marker).

Queries take the environment to evaluate conditions against and the set of
releases to consider.  Because declarations are never merged across versions,
an entry point that only existed in an older release is not reported for a
release that does not declare it.

Entries are grouped by entry point group, so a GUI entry point and a console
entry point with the same name are distinct providers.  When two providers in
the *same* group match, the ambiguity is reported explicitly by
:class:`QueryResult` and :meth:`CommandCatalog.resolve`.
"""

import os
import platform
import re
import sys
from collections.abc import Iterable, Iterator, Mapping
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Union

from installer.exceptions import InstallerError
from installer.utils import canonicalize_name

if TYPE_CHECKING:
    from installer.sources import WheelSource

__all__ = [
    "CONSOLE_GROUP",
    "GUI_GROUP",
    "SCRIPT_GROUPS",
    "AmbiguousCommand",
    "CatalogError",
    "CommandCatalog",
    "CommandNotFound",
    "CommandProvider",
    "InvalidCommand",
    "QueryResult",
    "Release",
    "build_catalog",
    "default_environment",
    "evaluate_marker",
    "normalize_group",
    "parse_entry_points",
    "parse_entry_points_file",
    "query_providers",
]

CONSOLE_GROUP = "console_scripts"
GUI_GROUP = "gui_scripts"
SCRIPT_GROUPS = frozenset({CONSOLE_GROUP, GUI_GROUP})

_GROUP_ALIASES = {
    "console": CONSOLE_GROUP,
    "gui": GUI_GROUP,
}
_SHORT_GROUPS = {value: key for key, value in _GROUP_ALIASES.items()}

_ENTRY_POINT_REGEX = re.compile(
    r"""
    ^\s*
    (?P<module>[\w.]+)\s*
    (?::\s*(?P<attrs>[\w.]+))?\s*
    (?:\[\s*(?P<extras>[^\]]*?)\s*\])?\s*
    (?:;\s*(?P<marker>.+?))?\s*
    $
    """,
    re.VERBOSE | re.UNICODE,
)

#: Marker variables defined by PEP 508, plus the legacy alias.
_MARKER_VARIABLES = frozenset(
    {
        "implementation_name",
        "implementation_version",
        "os_name",
        "platform_machine",
        "platform_python_implementation",
        "platform_release",
        "platform_system",
        "platform_version",
        "python_full_version",
        "python_implementation",
        "python_version",
        "sys_platform",
        "extra",
    }
)

#: Marker variables compared with :pep:`440` version semantics.
_VERSION_MARKERS = frozenset(
    {
        "implementation_version",
        "platform_release",
        "python_full_version",
        "python_version",
    }
)

_MARKER_TOKEN_REGEX = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<lparen>\()
  | (?P<rparen>\))
  | (?P<op>===|==|!=|<=|>=|~=|<|>)
  | (?P<string>'[^']*'|"[^"]*")
  | (?P<name>[A-Za-z_][A-Za-z0-9_.]*)
    """,
    re.VERBOSE,
)

_VERSION_REGEX = re.compile(
    r"""
    ^\s*v?
    (?:(?P<epoch>[0-9]+)!)?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?P<pre>
        [._-]?
        (?P<pre_l>alpha|a|beta|b|preview|pre|c|rc)
        [._-]?
        (?P<pre_n>[0-9]+)?
    )?
    (?P<post>
        (?:-(?P<post_n1>[0-9]+))
        |
        (?:
            [._-]?
            (?P<post_l>post|rev|r)
            [._-]?
            (?P<post_n2>[0-9]+)?
        )
    )?
    (?P<dev>
        [._-]?
        (?P<dev_l>dev)
        [._-]?
        (?P<dev_n>[0-9]+)?
    )?
    (?:\+
        (?P<local>[a-z0-9]+(?:[._-][a-z0-9]+)*)
    )?
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)

ReleaseKey = tuple[str, str]

ReleaseSelection = Union[
    "Release",
    Iterable["Release"],
    Iterable[tuple[str, str]],
    Iterable[str],
    Mapping[str, str | None | Iterable[str]],
    None,
]


class CatalogError(InstallerError):
    """Base class for errors raised by the command catalog."""


class InvalidCommand(CatalogError, ValueError):
    """Raised when an entry point declaration or condition cannot be parsed."""


class CommandNotFound(CatalogError, LookupError):
    """Raised when no provider satisfies a lookup."""

    def __init__(self, name: str, group: str) -> None:
        """Record the command name and group that had no provider."""
        super().__init__(f"No provider for {name!r} in group {group!r}")
        self.name = name
        self.group = group


class AmbiguousCommand(CatalogError, LookupError):
    """Raised when more than one provider satisfies a lookup in one group."""

    def __init__(
        self, name: str, group: str, providers: Iterable["CommandProvider"]
    ) -> None:
        """Record the command name, group, and the competing providers."""
        self.name = name
        self.group = group
        self.providers = tuple(providers)
        candidates = ", ".join(str(provider) for provider in self.providers)
        super().__init__(f"Ambiguous command {name!r} in group {group!r}: {candidates}")


def normalize_group(group: str) -> str:
    """Return the canonical entry point group name.

    ``"console"`` and ``"gui"`` are accepted as aliases for the
    ``console_scripts`` and ``gui_scripts`` group names.  Any other group
    name is returned with surrounding whitespace removed.
    """
    stripped = group.strip()
    return _GROUP_ALIASES.get(stripped, stripped)


def default_environment() -> dict[str, str]:
    """Return the :pep:`508` marker environment of the running interpreter."""
    implementation = getattr(sys, "implementation", None)
    implementation_name = (
        implementation.name if implementation is not None else "cpython"
    )
    implementation_version = platform.python_version()
    if implementation is not None:
        version_info = getattr(implementation, "version", None)
        if version_info is not None:
            implementation_version = ".".join(str(part) for part in version_info[:3])

    return {
        "implementation_name": implementation_name,
        "implementation_version": implementation_version,
        "os_name": os.name,
        "platform_machine": platform.machine(),
        "platform_python_implementation": platform.python_implementation(),
        "platform_release": platform.release(),
        "platform_system": platform.system(),
        "platform_version": platform.version(),
        "python_full_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "python_version": f"{sys.version_info[0]}.{sys.version_info[1]}",
        "sys_platform": sys.platform,
    }


def _marker_environment(
    environment: Mapping[str, str] | None,
) -> dict[str, str]:
    merged = default_environment()
    if environment is not None:
        merged.update(
            {
                str(key): str(value)
                for key, value in environment.items()
                if key not in ("extra", "extras")
            }
        )
    return merged


def _split_extras(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    result = []
    for item in re.split(r"[,\s]+", value.strip()):
        if item:
            normalized = canonicalize_name(item)
            if normalized not in result:
                result.append(normalized)
    return tuple(result)


def _requested_extras(
    environment: Mapping[str, str] | None, extras: Iterable[str] | None
) -> tuple[str, ...]:
    if extras is not None:
        values = list(extras)
    elif environment is not None and "extras" in environment:
        raw: Any = environment["extras"]
        if isinstance(raw, str):
            values = re.split(r"[,\s]+", raw)
        else:
            values = [str(item) for item in raw]
    elif environment is not None and "extra" in environment:
        values = re.split(r"[,\s]+", str(environment["extra"]))
    else:
        values = []

    result = []
    for item in values:
        normalized = canonicalize_name(str(item))
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


# --------------------------------------------------------------------------------------
# Environment markers
# --------------------------------------------------------------------------------------
class _MarkerExpression:
    values: tuple["_MarkerExpression", ...]

    def evaluate(self, environment: Mapping[str, str]) -> bool:
        raise NotImplementedError


@dataclass(frozen=True)
class _MarkerOperand:
    variable: str | None = None
    literal: str | None = None

    @property
    def name(self) -> str | None:
        return self.variable

    def value(self, environment: Mapping[str, str]) -> str:
        if self.literal is not None:
            return self.literal
        assert self.variable is not None
        try:
            return environment[self.variable]
        except KeyError:
            raise InvalidCommand(
                f"Undefined environment marker variable: {self.variable!r}"
            ) from None


@dataclass(frozen=True)
class _MarkerComparison(_MarkerExpression):
    lhs: _MarkerOperand
    operator: str
    rhs: _MarkerOperand

    def evaluate(self, environment: Mapping[str, str]) -> bool:
        lhs = self.lhs.value(environment)
        rhs = self.rhs.value(environment)
        key = self.lhs.name or self.rhs.name

        if "extra" in (self.lhs.name, self.rhs.name):
            lhs = canonicalize_name(lhs)
            rhs = canonicalize_name(rhs)

        if key in _VERSION_MARKERS and _specifier_is_valid(self.operator, rhs):
            return _evaluate_operator(lhs, self.operator, rhs)

        return _plain_operator(lhs, self.operator, rhs)


@dataclass(frozen=True)
class _MarkerAnd(_MarkerExpression):
    values: tuple[_MarkerExpression, ...]

    def evaluate(self, environment: Mapping[str, str]) -> bool:
        return all(value.evaluate(environment) for value in self.values)


@dataclass(frozen=True)
class _MarkerOr(_MarkerExpression):
    values: tuple[_MarkerExpression, ...]

    def evaluate(self, environment: Mapping[str, str]) -> bool:
        return any(value.evaluate(environment) for value in self.values)


def _references_variable(expression: _MarkerExpression, variable: str) -> bool:
    if isinstance(expression, _MarkerComparison):
        return variable in (expression.lhs.name, expression.rhs.name)
    return any(_references_variable(value, variable) for value in expression.values)


class _MarkerParser:
    def __init__(self, text: str) -> None:
        self._text = text
        self._tokens = self._tokenize(text)
        self._index = 0

    @staticmethod
    def _tokenize(text: str) -> list[tuple[str, str]]:
        tokens = []
        position = 0
        length = len(text)
        while position < length:
            match = _MARKER_TOKEN_REGEX.match(text, position)
            if match is None:
                raise InvalidCommand(f"Invalid environment marker: {text!r}")
            position = match.end()
            kind = match.lastgroup
            assert kind is not None
            if kind != "space":
                tokens.append((kind, match.group(kind)))
        return tokens

    def _peek(self) -> tuple[str, str] | None:
        if self._index >= len(self._tokens):
            return None
        return self._tokens[self._index]

    def _advance(self) -> tuple[str, str]:
        token = self._peek()
        if token is None:
            raise InvalidCommand(
                f"Unexpected end of environment marker: {self._text!r}"
            )
        self._index += 1
        return token

    def _accept_word(self, word: str) -> bool:
        token = self._peek()
        if token is not None and token[0] == "name" and token[1] == word:
            self._index += 1
            return True
        return False

    def _accept_kind(self, kind: str) -> str | None:
        token = self._peek()
        if token is not None and token[0] == kind:
            self._index += 1
            return token[1]
        return None

    def parse(self) -> _MarkerExpression:
        expression = self._parse_or()
        if self._peek() is not None:
            raise InvalidCommand(
                f"Unexpected trailing token in environment marker: {self._text!r}"
            )
        return expression

    def _parse_or(self) -> _MarkerExpression:
        values = [self._parse_and()]
        while self._accept_word("or"):
            values.append(self._parse_and())
        if len(values) == 1:
            return values[0]
        return _MarkerOr(tuple(values))

    def _parse_and(self) -> _MarkerExpression:
        values = [self._parse_atom()]
        while self._accept_word("and"):
            values.append(self._parse_atom())
        if len(values) == 1:
            return values[0]
        return _MarkerAnd(tuple(values))

    def _parse_atom(self) -> _MarkerExpression:
        if self._accept_kind("lparen"):
            expression = self._parse_or()
            if self._accept_kind("rparen") is None:
                raise InvalidCommand(
                    f"Unbalanced parenthesis in environment marker: {self._text!r}"
                )
            return expression
        return self._parse_comparison()

    def _parse_comparison(self) -> _MarkerExpression:
        lhs = self._parse_operand()
        operator = self._parse_operator()
        rhs = self._parse_operand()
        return _MarkerComparison(lhs, operator, rhs)

    def _parse_operator(self) -> str:
        if self._accept_word("not"):
            if not self._accept_word("in"):
                raise InvalidCommand(
                    f"Expected 'in' after 'not' in environment marker: {self._text!r}"
                )
            return "not in"
        if self._accept_word("in"):
            return "in"
        operator = self._accept_kind("op")
        if operator is None:
            raise InvalidCommand(
                f"Expected a comparison operator in environment marker: {self._text!r}"
            )
        return operator

    def _parse_operand(self) -> _MarkerOperand:
        kind, value = self._advance()
        if kind == "string":
            return _MarkerOperand(literal=value[1:-1])
        if kind == "name":
            if value not in _MARKER_VARIABLES:
                raise InvalidCommand(f"Unknown environment marker variable: {value!r}")
            return _MarkerOperand(variable=value)
        raise InvalidCommand(
            f"Expected an operand in environment marker, got {value!r}"
        )


def _parse_marker(text: str) -> _MarkerExpression:
    return _MarkerParser(text).parse()


@dataclass(frozen=True)
class _Version:
    """The parts of a :pep:`440` version that affect ordering."""

    epoch: int
    release: tuple[int, ...]
    pre: tuple[str, int] | None
    post: int | None
    dev: int | None
    local: tuple[tuple[int | str, str], ...] | None

    @property
    def is_prerelease(self) -> bool:
        return self.pre is not None or self.dev is not None

    @property
    def key(self) -> tuple[Any, ...]:
        """A comparison key that implements :pep:`440` ordering."""
        trimmed = self.release
        while len(trimmed) > 1 and trimmed[-1] == 0:
            trimmed = trimmed[:-1]

        if self.pre is None and self.post is None and self.dev is None:
            pre_rank, pre_number = 3, 0
        elif self.pre is None and self.post is None and self.dev is not None:
            pre_rank, pre_number = -1, 0
        elif self.pre is None:
            pre_rank, pre_number = 3, 0
        else:
            pre_rank, pre_number = {"a": 0, "b": 1, "rc": 2}[self.pre[0]], self.pre[1]

        post_rank = 0 if self.post is None else 1
        post_number = 0 if self.post is None else self.post
        dev_rank = 1 if self.dev is None else 0
        dev_number = 0 if self.dev is None else self.dev

        suffix = (pre_rank, pre_number, post_rank, post_number, dev_rank, dev_number)
        if self.local is None:
            return self.epoch, trimmed, suffix
        return self.epoch, trimmed, suffix, self.local

    def __lt__(self, other: "_Version") -> bool:
        return self.key < other.key

    def __le__(self, other: "_Version") -> bool:
        return self.key <= other.key

    def __gt__(self, other: "_Version") -> bool:
        return self.key > other.key

    def __ge__(self, other: "_Version") -> bool:
        return self.key >= other.key

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _Version):
            return NotImplemented
        return self.key == other.key

    def __hash__(self) -> int:
        return hash(self.key)

    def __str__(self) -> str:
        parts = []
        if self.epoch:
            parts.append(f"{self.epoch}!")
        parts.append(".".join(str(part) for part in self.release))
        if self.pre is not None:
            parts.append(f"{self.pre[0]}{self.pre[1]}")
        if self.post is not None:
            parts.append(f".post{self.post}")
        if self.dev is not None:
            parts.append(f".dev{self.dev}")
        if self.local is not None:
            parts.append("+" + ".".join(str(segment) for segment, _ in self.local))
        return "".join(parts)


def _parse_local(value: str) -> tuple[tuple[int | str, str], ...]:
    segments: list[tuple[int | str, str]] = []
    for part in re.split(r"[._-]", value.lower()):
        segments.append((int(part), "") if part.isdigit() else (-1, part))
    return tuple(segments)


def _parse_version(value: str) -> _Version | None:
    match = _VERSION_REGEX.match(value)
    if match is None:
        return None

    pre = None
    if match.group("pre_l") is not None:
        letter = match.group("pre_l").lower()
        letter = {
            "alpha": "a",
            "beta": "b",
            "c": "rc",
            "pre": "rc",
            "preview": "rc",
        }.get(letter, letter)
        pre = (letter, int(match.group("pre_n") or 0))

    post = None
    if match.group("post") is not None:
        number = match.group("post_n1") or match.group("post_n2")
        post = int(number or 0)

    dev = None
    if match.group("dev_l") is not None:
        dev = int(match.group("dev_n") or 0)

    local = None
    if match.group("local") is not None:
        local = _parse_local(match.group("local"))

    return _Version(
        epoch=int(match.group("epoch") or 0),
        release=tuple(int(part) for part in match.group("release").split(".")),
        pre=pre,
        post=post,
        dev=dev,
        local=local,
    )


def _base_dev0(version: _Version) -> _Version:
    """Return the ``.dev0`` of a version's base release (``1.2`` -> ``1.2.dev0``)."""
    return _Version(version.epoch, version.release, None, None, 0, None)


def _next_prefix_dev0(release: tuple[int, ...], epoch: int = 0) -> _Version:
    """Return the smallest version in the next prefix (``1.2`` -> ``1.3.dev0``)."""
    bumped = (*release[:-1], release[-1] + 1)
    return _Version(epoch, bumped, None, None, 0, None)


#: A local-segment sentinel that sorts above any real local label.
_LOCAL_MAX: tuple[tuple[float, str], ...] = ((float("inf"), ""),)


def _after_locals(version: _Version) -> tuple[Any, ...]:
    """Return a key sorting above ``version`` and all of its local variants."""
    epoch, release, suffix = version.key[:3]
    return epoch, release, suffix, _LOCAL_MAX


def _after_posts(version: _Version) -> tuple[Any, ...]:
    """Return a key sorting above ``version`` and all of its post releases."""
    epoch, release, suffix = version.key[:3]
    suffix = (suffix[0], suffix[1], 1, float("inf"), 1, 0)
    return epoch, release, suffix, _LOCAL_MAX


def _compare_wildcard(parsed: _Version, base: _Version, operator: str) -> bool:
    lower = _base_dev0(base)
    upper = _next_prefix_dev0(base.release, base.epoch)
    if operator == "==":
        return lower <= parsed < upper
    return not (lower <= parsed < upper)


def _specifier_is_valid(operator: str, right: str) -> bool:
    """Whether ``operator + right`` is a valid :pep:`440` specifier."""
    if operator == "===":
        return True
    if operator in ("in", "not in"):
        return False
    if right.endswith(".*"):
        return operator in ("==", "!=")

    parsed = _parse_version(right)
    if parsed is None:
        return False
    if operator in ("==", "!="):
        return True
    if operator in ("<", "<=", ">", ">="):
        return parsed.local is None
    if operator == "~=":
        return parsed.local is None and len(parsed.release) >= 2
    return False


def _plain_operator(left: str, operator: str, right: str) -> bool:
    """Compare plain strings when no valid version specifier applies."""
    if operator == "in":
        return left in right
    if operator == "not in":
        return left not in right
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    if operator == "<=":
        return left == right
    if operator == ">=":
        return left == right
    if operator == "===":
        return left == right
    # PEP 508 leaves `<` and `>` undefined for non-version markers.
    return False


def _compare_versions(left: str, right: str, operator: str) -> bool | None:
    parsed_left = _parse_version(left)
    if right.endswith(".*"):
        parsed_base = _parse_version(right[:-2])
        if parsed_base is None or parsed_left is None:
            return None
        if operator not in ("==", "!="):
            return False
        return _compare_wildcard(parsed_left, parsed_base, operator)

    parsed_right = _parse_version(right)
    if parsed_left is None or parsed_right is None:
        return None

    if operator == "===":
        return left == right

    has_local = "+" in right
    left_key = parsed_left.key
    right_key = parsed_right.key

    if operator == "==":
        if has_local:
            return left_key == right_key
        return right_key <= left_key <= _after_locals(parsed_right)
    if operator == "!=":
        if has_local:
            return left_key != right_key
        return not (right_key <= left_key <= _after_locals(parsed_right))
    if operator == "~=":
        prefix = parsed_right.release[:-1]
        upper = _next_prefix_dev0(prefix, parsed_right.epoch)
        return right_key <= left_key < upper.key
    if operator == "<=":
        return left_key <= _after_locals(parsed_right)
    if operator == "<":
        if parsed_right.is_prerelease:
            return left_key < right_key
        bound = _Version(
            parsed_right.epoch,
            parsed_right.release,
            parsed_right.pre,
            parsed_right.post,
            0,
            None,
        )
        return left_key < bound.key
    if operator == ">=":
        return left_key >= right_key
    if operator == ">":
        if parsed_right.dev is not None:
            lower = _Version(
                parsed_right.epoch,
                parsed_right.release,
                parsed_right.pre,
                parsed_right.post,
                parsed_right.dev + 1,
                None,
            )
            return left_key >= lower.key
        if parsed_right.post is not None:
            lower = _Version(
                parsed_right.epoch,
                parsed_right.release,
                parsed_right.pre,
                parsed_right.post + 1,
                0,
                None,
            )
            return left_key >= lower.key
        return left_key > _after_posts(parsed_right)
    return None


def _evaluate_operator(left: str, operator: str, right: str) -> bool:
    """Evaluate a version-aware marker comparison."""
    comparison = _compare_versions(left, right, operator)
    return bool(comparison)


def evaluate_marker(marker: str, environment: Mapping[str, str] | None = None) -> bool:
    """Evaluate a :pep:`508` environment marker.

    :param marker: marker expression, for example ``python_version >= "3.12"``
    :param environment: marker environment; defaults to the current interpreter
    :raises InvalidCommand: if the marker cannot be parsed
    """
    expression = _parse_marker(marker)
    merged = _marker_environment(environment)
    merged["extra"] = (
        canonicalize_name(str(environment["extra"]))
        if environment is not None and "extra" in environment
        else ""
    )
    return expression.evaluate(merged)


# --------------------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Release:
    """A distribution release, identified by canonical name and version."""

    distribution: str
    """Distribution identity, canonicalized according to :pep:`503`."""

    version: str
    """Version string of the release."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "distribution", canonicalize_name(self.distribution))
        object.__setattr__(self, "version", self.version.strip())

    @property
    def key(self) -> ReleaseKey:
        """The identity used to match this release."""
        return self.distribution, self.version

    @property
    def name(self) -> str:
        """The canonical distribution identity."""
        return self.distribution

    def __str__(self) -> str:
        return f"{self.distribution}=={self.version}"


@dataclass(frozen=True)
class CommandProvider:
    """One entry point declaration, scoped to the release that declared it.

    ``extras`` and ``marker`` are the declaration conditions.  An entry that
    declares extras is only active when at least one of them is requested; an
    entry with a marker is only active when the marker evaluates to true.
    """

    distribution: str
    """Distribution identity, canonicalized according to :pep:`503`."""

    version: str
    """Version of the release that declared this entry point."""

    group: str
    """Entry point group, such as ``console_scripts`` or ``gui_scripts``."""

    name: str
    """Command name, as declared in ``entry_points.txt``."""

    module: str
    """Module path, to load the entry point from."""

    attr: str = ""
    """Final attribute access, for the entry point."""

    extras: tuple[str, ...] = ()
    """Extras that must be requested for this declaration to apply."""

    marker: str | None = None
    """Environment marker that must evaluate to true for this declaration."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "distribution", canonicalize_name(self.distribution))
        object.__setattr__(self, "version", self.version.strip())
        object.__setattr__(self, "group", normalize_group(self.group))
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "module", self.module.strip())
        object.__setattr__(self, "attr", self.attr.strip())
        object.__setattr__(self, "extras", _split_extras(",".join(self.extras)))

        marker = self.marker.strip() if self.marker else None
        if marker:
            _parse_marker(marker)
        object.__setattr__(self, "marker", marker or None)

    @property
    def release(self) -> Release:
        """The release that declared this entry point."""
        return Release(self.distribution, self.version)

    @property
    def section(self) -> str:
        """The short section name understood by :mod:`installer.scripts`.

        ``console_scripts`` becomes ``"console"`` and ``gui_scripts`` becomes
        ``"gui"``.  Other groups are returned unchanged.
        """
        return _SHORT_GROUPS.get(self.group, self.group)

    @property
    def entry_point(self) -> str:
        """The ``module:attr`` object reference."""
        if self.attr:
            return f"{self.module}:{self.attr}"
        return self.module

    @property
    def condition(self) -> str:
        """A printable representation of the declaration conditions."""
        parts = []
        if self.extras:
            parts.append("extras=" + ",".join(self.extras))
        if self.marker:
            parts.append(self.marker)
        return "; ".join(parts)

    @property
    def declaration(
        self,
    ) -> tuple[str, str, str, str, str, tuple[str, ...], str | None]:
        """The full declaration identity within a release.

        This covers distribution identity, version, group, command name,
        object reference, extras, and marker.
        """
        return (
            self.distribution,
            self.version,
            self.group,
            self.name,
            self.entry_point,
            self.extras,
            self.marker,
        )

    def is_active(
        self,
        environment: Mapping[str, str] | None = None,
        extras: Iterable[str] | None = None,
    ) -> bool:
        """Return whether this declaration applies to an environment.

        :param environment: marker environment, defaulting to the current one
        :param extras: requested extras; defaults to any extras found in
            ``environment`` (under ``"extras"`` or ``"extra"``)
        """
        requested = _requested_extras(environment, extras)

        if self.extras and not set(self.extras).intersection(requested):
            return False
        if not self.marker:
            return True

        expression = _parse_marker(self.marker)
        merged = _marker_environment(environment)
        if _references_variable(expression, "extra"):
            candidates = requested or ("",)
            return any(
                expression.evaluate({**merged, "extra": extra}) for extra in candidates
            )
        return expression.evaluate({**merged, "extra": ""})

    def as_row(self) -> dict[str, Any]:
        """Return a JSON-friendly representation of this declaration."""
        return {
            "distribution": self.distribution,
            "version": self.version,
            "group": self.group,
            "name": self.name,
            "module": self.module,
            "attr": self.attr,
            "extras": list(self.extras),
            "marker": self.marker,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "CommandProvider":
        """Build a declaration from a mapping produced by :meth:`as_row`."""
        raw_extras = row.get("extras") or ()
        if isinstance(raw_extras, str):
            extras = _split_extras(raw_extras)
        else:
            extras = tuple(str(value) for value in raw_extras)

        marker = row.get("marker")
        return cls(
            distribution=str(row["distribution"]),
            version=str(row["version"]),
            group=str(row["group"]),
            name=str(row["name"]),
            module=str(row["module"]),
            attr=str(row.get("attr") or ""),
            extras=extras,
            marker=None if marker is None else str(marker),
        )

    def __str__(self) -> str:
        return (
            f"{self.distribution}=={self.version} "
            f"[{self.group}] {self.name} = {self.entry_point}"
        )


@dataclass(frozen=True)
class QueryResult:
    """Providers matching a command lookup, grouped by entry point group.

    ``ambiguous`` is true when a single group has more than one matching
    provider.  A GUI provider and a console provider with the same name do not
    make the lookup ambiguous.
    """

    name: str
    """The command name that was looked up."""

    matches: tuple[tuple[str, tuple[CommandProvider, ...]], ...]
    """Matching providers, grouped by entry point group."""

    @property
    def providers(self) -> tuple[CommandProvider, ...]:
        """All matching providers, across all groups."""
        return tuple(
            provider for _group, providers in self.matches for provider in providers
        )

    @property
    def groups(self) -> dict[str, tuple[CommandProvider, ...]]:
        """Matching providers keyed by entry point group."""
        return dict(self.matches)

    @property
    def ambiguous(self) -> bool:
        """Whether any group has more than one matching provider."""
        return any(len(providers) > 1 for _group, providers in self.matches)

    @property
    def ambiguities(self) -> dict[str, tuple[CommandProvider, ...]]:
        """The groups that have more than one matching provider."""
        return {
            group: providers for group, providers in self.matches if len(providers) > 1
        }

    def for_group(self, group: str) -> tuple[CommandProvider, ...]:
        """Return matching providers in one group."""
        return self.groups.get(normalize_group(group), ())

    def unique(self, group: str | None = None) -> CommandProvider:
        """Return the single provider for ``group``.

        :raises CommandNotFound: if there are no matching providers
        :raises AmbiguousCommand: if there is more than one matching provider
        """
        if group is not None:
            providers = self.for_group(group)
            normalized = normalize_group(group)
        else:
            providers = self.providers
            normalized = providers[0].group if providers else ""

        if not providers:
            raise CommandNotFound(self.name, normalized)
        if len(providers) > 1:
            raise AmbiguousCommand(self.name, normalized, providers)
        return providers[0]

    def __bool__(self) -> bool:
        return bool(self.providers)

    def __len__(self) -> int:
        return len(self.providers)

    def __iter__(self) -> Iterator[CommandProvider]:
        return iter(self.providers)

    @property
    def provider(self) -> CommandProvider:
        """The single matching provider.

        :raises CommandNotFound: if there are no matching providers
        :raises AmbiguousCommand: if there is more than one matching provider
        """
        return self.unique()


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------
def parse_entry_points(
    text: str,
    distribution: str = "",
    version: str = "",
    *,
    groups: Iterable[str] | None = None,
) -> tuple[CommandProvider, ...]:
    """Parse the contents of an ``entry_points.txt`` file.

    Unlike :func:`installer.utils.parse_entrypoints`, this keeps the requested
    extras and environment marker attached to each declaration.

    :param text: entire contents of the file
    :param distribution: distribution identity for the declarations
    :param version: release version for the declarations
    :param groups: if provided, only parse these entry point groups
    :raises InvalidCommand: if a declaration cannot be parsed
    """
    config = ConfigParser(delimiters="=", interpolation=None, strict=False)
    config.optionxform = str  # type: ignore[assignment, method-assign]
    try:
        config.read_string(text)
    except Exception as exc:
        raise InvalidCommand(f"Invalid entry_points.txt: {exc}") from exc

    selected_groups = (
        {normalize_group(group) for group in groups} if groups is not None else None
    )

    result = []
    for section in config.sections():
        group = normalize_group(section)
        if selected_groups is not None and group not in selected_groups:
            continue

        for name, value in config.items(section):
            match = _ENTRY_POINT_REGEX.match(value)
            if match is None:
                raise InvalidCommand(
                    f"Invalid entry point for {name!r} in group {section!r}: {value!r}"
                )

            attrs = match.group("attrs")
            attr = attrs if attrs is not None else ""
            if group in SCRIPT_GROUPS and not attr:
                raise InvalidCommand(
                    f"Entry point {name!r} in group {section!r} has no callable"
                )

            result.append(
                CommandProvider(
                    distribution=distribution,
                    version=version,
                    group=group,
                    name=name,
                    module=match.group("module"),
                    attr=attr,
                    extras=_split_extras(match.group("extras")),
                    marker=match.group("marker"),
                )
            )

    return tuple(result)


def parse_entry_points_file(
    path: str | os.PathLike[str],
    distribution: str = "",
    version: str = "",
    *,
    groups: Iterable[str] | None = None,
) -> tuple[CommandProvider, ...]:
    """Parse an ``entry_points.txt`` file from disk."""
    text = Path(path).read_text(encoding="utf-8")
    return parse_entry_points(text, distribution, version, groups=groups)


# --------------------------------------------------------------------------------------
# Release selection
# --------------------------------------------------------------------------------------
def _release_key(distribution: str, version: str) -> ReleaseKey:
    return canonicalize_name(distribution), version.strip()


def _parse_release_spec(spec: str) -> tuple[str, str | None]:
    text = spec.strip()
    if not text:
        raise ValueError("Empty release specification")
    if "==" in text:
        distribution, version = text.split("==", 1)
        if not distribution.strip() or not version.strip():
            raise ValueError(f"Invalid release specification: {spec!r}")
        return distribution.strip(), version.strip()
    return text, None


@dataclass(frozen=True)
class _Selection:
    exact: frozenset[ReleaseKey]
    all_versions: frozenset[str]

    def contains(self, release: Release) -> bool:
        return release.key in self.exact or release.distribution in self.all_versions


def _normalize_selection(releases: ReleaseSelection) -> _Selection | None:
    """Normalize a release selection into exact releases and distributions."""
    if releases is None:
        return None

    exact: set[ReleaseKey] = set()
    all_versions: set[str] = set()

    def add(distribution: str, version: str | None) -> None:
        if version is None:
            all_versions.add(canonicalize_name(distribution))
        else:
            exact.add(_release_key(distribution, version))

    if isinstance(releases, Release):
        add(releases.distribution, releases.version)
    elif isinstance(releases, str):
        distribution, version = _parse_release_spec(releases)
        add(distribution, version)
    elif isinstance(releases, Mapping):
        for distribution, versions in releases.items():
            if versions is None:
                add(str(distribution), None)
            elif isinstance(versions, str):
                add(str(distribution), versions)
            else:
                version_list = list(versions)
                if not version_list:
                    add(str(distribution), None)
                for version in version_list:
                    add(str(distribution), str(version))
    else:
        selection: Any = releases
        if isinstance(selection, tuple) and len(selection) == 2:
            selection = [selection]
        for value in selection:
            if isinstance(value, Release):
                add(value.distribution, value.version)
            elif isinstance(value, str):
                distribution, version = _parse_release_spec(value)
                add(distribution, version)
            elif isinstance(value, tuple) and len(value) == 2:
                add(str(value[0]), str(value[1]))
            else:
                raise TypeError(f"Invalid release selection: {value!r}")

    return _Selection(frozenset(exact), frozenset(all_versions))


# --------------------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------------------
class CommandCatalog:
    """A searchable, release-scoped catalog of entry point declarations.

    Declarations are stored per exact release.  Registering a release replaces
    any declarations previously stored for that release, so an entry point that
    only appeared in an older version cannot survive through stale state.
    Queries only consider the releases selected by the caller.
    """

    def __init__(self, entries: Iterable[CommandProvider] = ()) -> None:
        """Create an empty catalog, optionally pre-populated with entries."""
        self._entries: dict[ReleaseKey, tuple[CommandProvider, ...]] = {}
        self._releases: dict[ReleaseKey, Release] = {}
        self.add_entries(entries)

    # -- ingestion --------------------------------------------------------------------
    def add_release(
        self,
        distribution: str,
        version: str,
        entry_points: str,
        *,
        groups: Iterable[str] | None = None,
    ) -> tuple[CommandProvider, ...]:
        """Parse and store one release's ``entry_points.txt`` contents.

        Any declarations stored earlier for the same release are replaced.

        :param distribution: distribution name (canonicalized on storage)
        :param version: release version
        :param entry_points: entire contents of the release's entry_points.txt
        :param groups: if provided, only record these entry point groups
        :raises InvalidCommand: if a declaration cannot be parsed
        """
        release = Release(distribution, version)
        entries = parse_entry_points(
            entry_points,
            release.distribution,
            release.version,
            groups=groups,
        )
        return self._store_release(release, entries)

    register_release = add_release

    def add_release_file(
        self,
        distribution: str,
        version: str,
        path: str | os.PathLike[str],
        *,
        groups: Iterable[str] | None = None,
    ) -> tuple[CommandProvider, ...]:
        """Parse and store an ``entry_points.txt`` file from disk."""
        text = Path(path).read_text(encoding="utf-8")
        return self.add_release(distribution, version, text, groups=groups)

    def add_source(
        self,
        source: "WheelSource",
        *,
        groups: Iterable[str] | None = None,
    ) -> tuple[CommandProvider, ...]:
        """Record the entry points declared by a :class:`WheelSource`."""
        release = Release(source.distribution, source.version)
        if "entry_points.txt" in source.dist_info_filenames:
            text = source.read_dist_info("entry_points.txt")
        else:
            text = ""
        entries = parse_entry_points(
            text,
            release.distribution,
            release.version,
            groups=groups,
        )
        return self._store_release(release, entries)

    def add_wheel(
        self,
        path: str | os.PathLike[str],
        *,
        groups: Iterable[str] | None = None,
    ) -> tuple[CommandProvider, ...]:
        """Record the entry points declared by a wheel file."""
        from installer.sources import WheelFile

        with WheelFile.open(Path(path)) as source:
            return self.add_source(source, groups=groups)

    def add_entry(self, entry: CommandProvider) -> None:
        """Store a single declaration."""
        key = entry.release.key
        self._releases.setdefault(key, entry.release)
        existing = self._entries.get(key, ())
        if entry not in existing:
            self._entries[key] = (*existing, entry)

    def add_entries(self, entries: Iterable[CommandProvider]) -> None:
        """Store multiple declarations."""
        for entry in entries:
            self.add_entry(entry)

    def _store_release(
        self, release: Release, entries: Iterable[CommandProvider]
    ) -> tuple[CommandProvider, ...]:
        stored = self._deduplicate(entries)
        self._releases[release.key] = release
        self._entries[release.key] = stored
        return stored

    @staticmethod
    def _deduplicate(
        entries: Iterable[CommandProvider],
    ) -> tuple[CommandProvider, ...]:
        result = []
        seen = set()
        for entry in entries:
            if entry not in seen:
                seen.add(entry)
                result.append(entry)
        return tuple(result)

    def remove_release(self, distribution: str, version: str) -> int:
        """Remove a release and all of its declarations.

        :return: number of declarations removed
        """
        key = _release_key(distribution, version)
        self._releases.pop(key, None)
        return len(self._entries.pop(key, ()))

    # -- introspection ----------------------------------------------------------------
    @property
    def releases(self) -> tuple[Release, ...]:
        """All registered releases, in registration order."""
        return tuple(self._releases.values())

    def has_release(self, distribution: str, version: str) -> bool:
        """Whether the exact release is registered."""
        return _release_key(distribution, version) in self._releases

    def release_versions(self, distribution: str) -> tuple[str, ...]:
        """All registered versions for a distribution."""
        name = canonicalize_name(distribution)
        return tuple(
            release.version
            for release in self._releases.values()
            if release.distribution == name
        )

    def latest_release(self, distribution: str) -> Release | None:
        """Return the highest registered version of a distribution.

        Versions that cannot be parsed numerically sort by their string value.
        """

        def sort_key(release: Release) -> tuple[int, Any]:
            parsed = _parse_version(release.version)
            if parsed is None:
                return 1, release.version
            return 0, parsed.key

        name = canonicalize_name(distribution)
        candidates = [
            release
            for release in self._releases.values()
            if release.distribution == name
        ]
        if not candidates:
            return None
        return max(candidates, key=sort_key)

    def entries(
        self,
        *,
        releases: ReleaseSelection = None,
        distribution: str | None = None,
        group: str | None = None,
    ) -> tuple[CommandProvider, ...]:
        """Return stored declarations, filtered by release and group."""
        selection = _normalize_selection(releases)
        distribution_name = (
            canonicalize_name(distribution) if distribution is not None else None
        )
        group_name = normalize_group(group) if group is not None else None

        result = []
        for release in self._releases.values():
            if selection is not None and not selection.contains(release):
                continue
            if (
                distribution_name is not None
                and release.distribution != distribution_name
            ):
                continue
            for entry in self._entries.get(release.key, ()):
                if group_name is not None and entry.group != group_name:
                    continue
                result.append(entry)
        return tuple(result)

    def rows(self) -> tuple[dict[str, Any], ...]:
        """Return all declarations as JSON-friendly rows."""
        return tuple(entry.as_row() for entry in self.entries())

    table = rows

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]]) -> "CommandCatalog":
        """Build a catalog from rows produced by :meth:`rows`."""
        catalog = cls()
        for row in rows:
            catalog.add_entry(CommandProvider.from_row(row))
        return catalog

    # -- queries ----------------------------------------------------------------------
    def find_providers(
        self,
        name: str,
        *,
        group: str | None = None,
        releases: ReleaseSelection = None,
        distribution: str | None = None,
        environment: Mapping[str, str] | None = None,
        extras: Iterable[str] | None = None,
    ) -> tuple[CommandProvider, ...]:
        """Return every provider matching ``name`` under the given conditions.

        Providers are only returned when declared by a selected release and
        when their extras and marker conditions hold in ``environment``.
        """
        result = []
        for entry in self.entries(
            releases=releases,
            distribution=distribution,
            group=group,
        ):
            if entry.name != name:
                continue
            if not entry.is_active(environment, extras):
                continue
            result.append(entry)
        return tuple(result)

    providers = find_providers

    def search(
        self,
        name: str | None = None,
        *,
        group: str | None = None,
        releases: ReleaseSelection = None,
        distribution: str | None = None,
        environment: Mapping[str, str] | None = None,
        extras: Iterable[str] | None = None,
        include_inactive: bool = False,
    ) -> tuple[CommandProvider, ...]:
        """Search declarations by command name.

        :param name: exact command name; if omitted, every name is returned
        :param group: restrict to an entry point group
        :param releases: selected releases; defaults to every registered release
        :param distribution: restrict to one distribution
        :param environment: marker environment used to evaluate declarations
        :param extras: requested extras used to evaluate declarations
        :param include_inactive: also return declarations whose conditions fail
        """
        result = []
        for entry in self.entries(
            releases=releases,
            distribution=distribution,
            group=group,
        ):
            if name is not None and entry.name != name:
                continue
            if not include_inactive and not entry.is_active(environment, extras):
                continue
            result.append(entry)
        return tuple(result)

    commands = search

    def query(
        self,
        name: str,
        *,
        group: str | None = None,
        releases: ReleaseSelection = None,
        distribution: str | None = None,
        environment: Mapping[str, str] | None = None,
        extras: Iterable[str] | None = None,
    ) -> QueryResult:
        """Return matching providers, grouped, with ambiguity information.

        A provider that is declared by a selected release but whose extras or
        marker conditions do not hold is not returned.
        """
        providers = self.find_providers(
            name,
            group=group,
            releases=releases,
            distribution=distribution,
            environment=environment,
            extras=extras,
        )

        grouped: dict[str, list[CommandProvider]] = {}
        for provider in providers:
            grouped.setdefault(provider.group, []).append(provider)

        return QueryResult(
            name=name,
            matches=tuple(
                (group_name, tuple(group_providers))
                for group_name, group_providers in sorted(grouped.items())
            ),
        )

    lookup = query

    def resolve(
        self,
        name: str,
        *,
        group: str = CONSOLE_GROUP,
        releases: ReleaseSelection = None,
        environment: Mapping[str, str] | None = None,
        extras: Iterable[str] | None = None,
    ) -> CommandProvider:
        """Return the unique provider for a command.

        :raises CommandNotFound: if no provider satisfies the conditions
        :raises AmbiguousCommand: if two or more providers satisfy the conditions
        """
        return self.query(
            name,
            group=group,
            releases=releases,
            environment=environment,
            extras=extras,
        ).unique(group)

    def ambiguities(
        self,
        *,
        releases: ReleaseSelection = None,
        environment: Mapping[str, str] | None = None,
        extras: Iterable[str] | None = None,
        groups: Iterable[str] | None = None,
    ) -> dict[tuple[str, str], tuple[CommandProvider, ...]]:
        """Find every command with multiple active providers in one group.

        :return: a mapping of ``(command name, group)`` to the providers
        """
        selected_groups = (
            {normalize_group(group) for group in groups} if groups is not None else None
        )
        grouped: dict[tuple[str, str], list[CommandProvider]] = {}
        for entry in self.search(
            releases=releases,
            environment=environment,
            extras=extras,
        ):
            if selected_groups is not None and entry.group not in selected_groups:
                continue
            grouped.setdefault((entry.name, entry.group), []).append(entry)

        return {
            key: tuple(providers)
            for key, providers in grouped.items()
            if len(providers) > 1
        }


def build_catalog(
    releases: Iterable["tuple[str, str, str] | Mapping[str, str] | CommandProvider"],
) -> CommandCatalog:
    """Build a catalog from release descriptions.

    Each item may be a ``(distribution, version, entry_points_text)`` tuple, a
    mapping with ``distribution``, ``version`` and ``entry_points`` keys, or a
    :class:`CommandProvider`.
    """
    catalog = CommandCatalog()
    for item in releases:
        if isinstance(item, CommandProvider):
            catalog.add_entry(item)
        elif isinstance(item, Mapping):
            catalog.add_release(
                str(item["distribution"]),
                str(item["version"]),
                str(item["entry_points"]),
            )
        else:
            distribution, version, entry_points = item
            catalog.add_release(distribution, version, entry_points)
    return catalog


def query_providers(
    catalog: CommandCatalog,
    name: str,
    *,
    group: str | None = None,
    releases: ReleaseSelection = None,
    environment: Mapping[str, str] | None = None,
    extras: Iterable[str] | None = None,
) -> tuple[CommandProvider, ...]:
    """Return the providers for a command from ``catalog``."""
    return catalog.find_providers(
        name,
        group=group,
        releases=releases,
        environment=environment,
        extras=extras,
    )


# Alternate names for the catalog vocabulary.
Catalog = CommandCatalog
EntryPointCatalog = CommandCatalog
CommandEntry = CommandProvider
EntryPoint = CommandProvider
Provider = CommandProvider
LookupResult = QueryResult
CONSOLE_SECTION = CONSOLE_GROUP
GUI_SECTION = GUI_GROUP
