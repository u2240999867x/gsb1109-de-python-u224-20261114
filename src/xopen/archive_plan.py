"""
Budget-constrained planning for archive write schemes.

Each group of files has one or more measured write schemes. A scheme records
the size of the produced artifact, an upper bound for the time needed to write
it and the receivers that can read it. At most two copies may be kept per
group, and every retained copy contributes its own size and write time.

The planner picks one cover per group (one or two schemes whose readable
receiver sets together cover every receiver) and chooses the combination with
the smallest total artifact size that fits into the total write-time limit.
"""

__all__ = [
    "CopyChoice",
    "GroupChoice",
    "PlanInputError",
    "PlanResult",
    "ReceiverPath",
    "format_plan_json",
    "format_plan_markdown",
    "format_plan_text",
    "plan_archive",
    "plan_from_json",
    "plan_manifest",
    "plan_to_dict",
    "main",
]

import argparse
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import combinations
from pathlib import Path
from typing import Any, TextIO

_FORMAT_ALIASES = {
    "bz2": "bz2",
    "bzip": "bz2",
    "bzip2": "bz2",
    "gz": "gz",
    "gzip": "gz",
    "lz4": "lz4",
    "lzma": "xz",
    "*": "*",
    "all": "*",
    "any": "*",
    "none": "none",
    "plain": "none",
    "raw": "none",
    "uncompressed": "none",
    "xz": "xz",
    "zst": "zst",
    "zstd": "zst",
    "zstandard": "zst",
    "zip": "zip",
    "7z": "7z",
    "rar": "rar",
    "tar": "tar",
}

_RECEIVER_KEYS = ("receivers", "readers", "clients", "targets")
_GROUP_KEYS = ("groups", "file_groups", "filegroups", "batches")
_SCHEME_KEYS = (
    "schemes",
    "scheme",
    "options",
    "plans",
    "candidates",
    "encodings",
    "variants",
)
_ROW_GROUP_KEYS = ("group", "group_id", "file_group", "filegroup", "batch", "archive")
_ROW_SCHEME_KEYS = ("scheme", "scheme_id", "option", "plan", "candidate", "name", "id")
_TIME_LIMIT_KEYS = (
    "time_limit",
    "time_limit_s",
    "total_time_limit",
    "total_time_limit_s",
    "write_time_limit",
    "total_write_time_limit",
    "write_time_budget",
    "time_budget",
    "max_time",
    "limit",
    "budget",
    "deadline",
)
_TIME_KEYS = (
    "write_time",
    "write_time_upper",
    "time_upper",
    "time",
    "duration",
    "seconds",
    "write_seconds",
    "write_time_s",
    "time_s",
    "duration_s",
    "write_time_upper_bound",
    "time_upper_bound",
    "max_write_time",
    "upper_bound",
)
_SIZE_KEYS = (
    "size",
    "bytes",
    "nbytes",
    "size_bytes",
    "total_bytes",
    "artifact_size",
    "artifact_bytes",
    "output_size",
    "product_size",
    "output_bytes",
    "encoded_size",
)
_READER_KEYS = (
    "readers",
    "receivers",
    "readable_by",
    "compatible_receivers",
    "compatible_with",
    "supported_by",
    "works_with",
    "can_read",
    "read_by",
)
_FORMAT_KEYS = ("format", "compression", "method", "codec", "encoding", "ext")
_ARTIFACT_KEYS = ("artifact", "artifact_path", "path", "output", "filename", "file")


class PlanInputError(ValueError):
    """Raised when an archive-plan manifest is malformed."""


@dataclass(frozen=True)
class _Receiver:
    id: str
    formats: frozenset[str] | None


@dataclass(frozen=True)
class _Scheme:
    id: str
    size: int
    time: Fraction
    format: str | None
    artifact: str | None
    readers: frozenset[str] | None


@dataclass(frozen=True)
class _Group:
    id: str
    schemes: tuple[_Scheme, ...]


@dataclass(frozen=True)
class _Candidate:
    schemes: tuple[_Scheme, ...]
    size: int
    time: Fraction

    @property
    def copies(self) -> int:
        return len(self.schemes)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(scheme.id for scheme in self.schemes)

    @property
    def key(self) -> tuple[int, Fraction, int, tuple[str, ...]]:
        return (self.size, self.time, self.copies, self.ids)


@dataclass(frozen=True)
class _State:
    groups: tuple[tuple[str, _Candidate], ...]

    @property
    def time(self) -> Fraction:
        return sum((candidate.time for _, candidate in self.groups), Fraction(0))

    @property
    def size(self) -> int:
        return sum(candidate.size for _, candidate in self.groups)

    @property
    def copies(self) -> int:
        return sum(candidate.copies for _, candidate in self.groups)

    @property
    def key(
        self,
    ) -> tuple[int, Fraction, int, tuple[tuple[str, tuple[str, ...]]]]:
        return (
            self.size,
            self.time,
            self.copies,
            tuple((group_id, candidate.ids) for group_id, candidate in self.groups),
        )


@dataclass(frozen=True)
class CopyChoice:
    """One artifact copy retained for a group."""

    group: str
    scheme: str
    size: int
    time: Fraction
    format: str | None
    artifact: str | None


@dataclass(frozen=True)
class GroupChoice:
    """The one or two copies selected for a group."""

    group: str
    copies: tuple[CopyChoice, ...]

    @property
    def size(self) -> int:
        return sum(copy.size for copy in self.copies)

    @property
    def time(self) -> Fraction:
        return sum((copy.time for copy in self.copies), Fraction(0))


@dataclass(frozen=True)
class ReceiverPath:
    """How one receiver obtains every group's data."""

    receiver: str
    groups: tuple[tuple[str, tuple[str, ...]], ...]

    @property
    def primary(self) -> tuple[tuple[str, str], ...]:
        return tuple((group, schemes[0]) for group, schemes in self.groups)

    def alternatives(self, group: str) -> tuple[str, ...]:
        """Schemes this receiver can read for one group, cheapest first."""
        for group_id, schemes in self.groups:
            if group_id == group:
                return schemes
        raise KeyError(group)

    def route(self) -> tuple[tuple[str, str], ...]:
        """Pick the cheapest readable copy for every group."""
        return self.primary


@dataclass(frozen=True)
class PlanResult:
    """
    Outcome of archive-plan optimization.

    ``status`` is ``"ok"`` if a plan fits, ``"over_budget"`` if every
    full-coverage plan exceeds the limit and ``"uncoverable"`` if some receiver
    cannot read any scheme of a group. For ``over_budget``, ``groups`` holds
    the fastest covering plan and ``minimum_extra_time`` is the exact number of
    additional time units needed.
    """

    status: str
    time_limit: Fraction | None
    time_unit: str
    total_size: int
    total_time: Fraction
    budget_margin: Fraction | None
    minimum_cover_time: Fraction | None
    minimum_extra_time: Fraction | None
    groups: tuple[GroupChoice, ...]
    receivers: tuple[ReceiverPath, ...]
    missing_by_group: tuple[tuple[str, tuple[str, ...]], ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def feasible(self) -> bool:
        """Whether a complete plan fits into the time limit."""
        return self.status == "ok"

    @property
    def total_bytes(self) -> int:
        """Alias for :attr:`total_size`."""
        return self.total_size

    @property
    def remaining_time(self) -> Fraction | None:
        """Alias for :attr:`budget_margin`."""
        return self.budget_margin

    @property
    def minimum_shortfall(self) -> Fraction | None:
        """Alias for :attr:`minimum_extra_time`."""
        return self.minimum_extra_time

    @property
    def selection(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """Selected scheme ids as ``(group, (scheme, ...))`` tuples."""
        return tuple(
            (choice.group, tuple(copy.scheme for copy in choice.copies))
            for choice in self.groups
        )

    def copies_for(self, group: str) -> tuple[CopyChoice, ...]:
        """Return the retained copies of one group."""
        for choice in self.groups:
            if choice.group == group:
                return choice.copies
        raise KeyError(group)

    def read_path(self, receiver: str) -> ReceiverPath:
        """Return the read path of one receiver."""
        for path in self.receivers:
            if path.receiver == receiver:
                return path
        raise KeyError(receiver)

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-ready representation."""
        return plan_to_dict(self)

    def format(self, fmt: str = "text") -> str:
        """Render this result as ``text``, ``json`` or ``markdown``."""
        if fmt == "json":
            return format_plan_json(self)
        if fmt in ("markdown", "md"):
            return format_plan_markdown(self)
        if fmt == "text":
            return format_plan_text(self)
        raise ValueError(f"unknown report format: {fmt!r}")


def _first(
    mapping: Mapping[str, Any], keys: Iterable[str], *, what: str, required: bool
) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    if required:
        raise PlanInputError(f"{what} is missing (expected one of: {', '.join(keys)})")
    return None


_TIME_UNITS = {
    "ns": Fraction(1, 10**9),
    "us": Fraction(1, 10**6),
    "µs": Fraction(1, 10**6),
    "ms": Fraction(1, 10**3),
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hour": 3600,
    "hours": 3600,
}


def _parse_measurement(value: Any, *, what: str) -> tuple[Fraction, str | None]:
    """Split a number with an optional unit suffix into (amount, unit)."""
    if isinstance(value, bool) or value is None:
        raise PlanInputError(f"{what} must be a number")
    if isinstance(value, Fraction):
        return value, None
    if isinstance(value, (int, float)):
        return Fraction(str(value)), None
    if not isinstance(value, str):
        raise PlanInputError(f"{what} must be a number, got {value!r}")
    text = value.strip().lower().replace(" ", "").replace(",", "")
    number = ""
    unit = ""
    for index, char in enumerate(text):
        if char.isdigit() or char in ".,+-/":
            number += char
        else:
            unit = text[index:]
            break
    try:
        amount = Fraction(number) if number else Fraction(0)
    except (ValueError, ZeroDivisionError) as error:
        raise PlanInputError(f"{what} must be a number, got {value!r}") from error
    if amount < 0:
        raise PlanInputError(f"{what} must not be negative")
    return amount, (unit or None)


def _as_fraction(value: Any, *, what: str) -> Fraction:
    amount, unit = _parse_measurement(value, what=what)
    if unit is None:
        return amount
    try:
        return amount * _TIME_UNITS[unit]
    except KeyError as error:
        raise PlanInputError(
            f"{what} has an unsupported time unit: {value!r}"
        ) from error


_SIZE_UNITS = {
    "": 1,
    "b": 1,
    "byte": 1,
    "bytes": 1,
    "kb": 10**3,
    "kib": 2**10,
    "mb": 10**6,
    "mib": 2**20,
    "gb": 10**9,
    "gib": 2**30,
    "tb": 10**12,
    "tib": 2**40,
}


def _as_bytes(value: Any, *, what: str) -> int:
    amount, unit = _parse_measurement(value, what=what)
    try:
        multiplier = _SIZE_UNITS[unit or ""]
    except KeyError as error:
        raise PlanInputError(
            f"{what} has an unsupported size unit: {value!r}"
        ) from error
    result = amount * multiplier
    if result.denominator != 1:
        raise PlanInputError(f"{what} must be a whole number of bytes, got {value!r}")
    if result < 0:
        raise PlanInputError(f"{what} must not be negative")
    return result.numerator


def _normalize_format(value: Any, *, what: str) -> str:
    if not isinstance(value, str):
        raise PlanInputError(f"{what} must be a string")
    text = value.strip().lower().lstrip(".")
    try:
        return _FORMAT_ALIASES[text]
    except KeyError as error:
        raise PlanInputError(f"unsupported format {value!r} for {what}") from error


def _format_from_artifact(artifact: str | None) -> str | None:
    if artifact is None:
        return None
    suffix = Path(artifact).suffix.lower().lstrip(".")
    return _FORMAT_ALIASES.get(suffix)


_NAME_SEPARATORS = ",;|/"


def _split_names(value: Any, *, what: str) -> list[str]:
    if isinstance(value, str):
        text = value
        for separator in _NAME_SEPARATORS:
            text = text.replace(separator, " ")
        return [part for part in text.split() if part]
    if isinstance(value, Mapping):
        names: list[str] = []
        for name, enabled in value.items():
            if not isinstance(name, str):
                raise PlanInputError(f"{what} must contain receiver names")
            if enabled:
                names.append(name)
        return names
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        names = []
        for item in value:
            if not isinstance(item, str):
                raise PlanInputError(f"{what} must contain receiver names")
            names.append(item)
        return names
    raise PlanInputError(f"{what} must be a list of receiver names")


def _normalize_receivers(value: Any) -> list[_Receiver]:
    if value is None:
        return []
    entries: list[tuple[Any, Any]]
    if isinstance(value, Mapping):
        entries = list(value.items())
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        entries = []
        for item in value:
            if isinstance(item, str):
                entries.append((item, None))
            elif isinstance(item, Mapping):
                receiver_id = _first(
                    item,
                    ("id", "receiver", "name", "reader"),
                    what="receiver id",
                    required=True,
                )
                entries.append((receiver_id, item))
            else:
                raise PlanInputError("receivers must be names or objects")
    elif isinstance(value, str):
        entries = [(name, None) for name in _split_names(value, what="receivers")]
    else:
        raise PlanInputError("receivers must be a list or mapping")

    result: list[_Receiver] = []
    seen: set[str] = set()
    for receiver_id_value, details in entries:
        if not isinstance(receiver_id_value, str) or not receiver_id_value:
            raise PlanInputError("receiver ids must be non-empty strings")
        if receiver_id_value in seen:
            raise PlanInputError(f"duplicate receiver id: {receiver_id_value}")
        seen.add(receiver_id_value)
        formats: frozenset[str] | None = None
        if isinstance(details, bool):
            if not details:
                continue
            details = None
        elif isinstance(details, (list, tuple, set, frozenset, str)):
            details = {"formats": details}
        if details is not None:
            if not isinstance(details, Mapping):
                raise PlanInputError(
                    f"receiver {receiver_id_value} details must be an object"
                )
            formats_value = _first(
                details,
                ("formats", "readable_formats", "supports", "codecs", "methods"),
                what=f"receiver {receiver_id_value} formats",
                required=False,
            )
            if formats_value is not None:
                if isinstance(formats_value, str):
                    raw_formats: list[Any] = [
                        part for part in formats_value.replace(",", " ").split() if part
                    ]
                elif isinstance(formats_value, Sequence):
                    raw_formats = list(formats_value)
                else:
                    raise PlanInputError(
                        f"receiver {receiver_id_value} formats must be a list of strings"
                    )
                formats = frozenset(
                    _normalize_format(
                        item, what=f"receiver {receiver_id_value} formats"
                    )
                    for item in raw_formats
                )
        result.append(_Receiver(receiver_id_value, formats))
    return result


def _normalize_schemes(group_id: str, value: Any) -> tuple[_Scheme, ...]:
    if isinstance(value, Mapping):
        entries: list[tuple[Any, Any]] = list(value.items())
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        entries = []
        for item in value:
            if not isinstance(item, Mapping):
                raise PlanInputError(f"schemes of group {group_id} must be objects")
            entries.append((None, item))
    else:
        raise PlanInputError(f"schemes of group {group_id} must be a list or mapping")

    result: list[_Scheme] = []
    seen: set[str] = set()
    for scheme_id_value, details in entries:
        if not isinstance(details, Mapping):
            raise PlanInputError(f"scheme of group {group_id} must be an object")
        scheme_id = scheme_id_value or _first(
            details,
            ("id", "scheme", "name", "scheme_id"),
            what=f"scheme id in group {group_id}",
            required=True,
        )
        if not isinstance(scheme_id, str) or not scheme_id:
            raise PlanInputError(
                f"scheme ids in group {group_id} must be non-empty strings"
            )
        if scheme_id in seen:
            raise PlanInputError(
                f"duplicate scheme id {scheme_id!r} in group {group_id}"
            )
        seen.add(scheme_id)
        size = _as_bytes(
            _first(details, _SIZE_KEYS, what=f"scheme {scheme_id} size", required=True),
            what=f"scheme {scheme_id} size",
        )
        time = _as_fraction(
            _first(
                details,
                _TIME_KEYS,
                what=f"scheme {scheme_id} write time",
                required=True,
            ),
            what=f"scheme {scheme_id} write time",
        )
        format_value = _first(
            details, _FORMAT_KEYS, what=f"scheme {scheme_id} format", required=False
        )
        artifact = _first(
            details, _ARTIFACT_KEYS, what=f"scheme {scheme_id} artifact", required=False
        )
        if artifact is not None and not isinstance(artifact, str):
            raise PlanInputError(f"scheme {scheme_id} artifact must be a string")
        if artifact == "":
            artifact = None
        scheme_format = (
            _normalize_format(format_value, what=f"scheme {scheme_id} format")
            if format_value not in (None, "")
            else _format_from_artifact(artifact)
        )

        readers_value = _first(
            details, _READER_KEYS, what=f"scheme {scheme_id} readers", required=False
        )
        readers = (
            frozenset(_split_names(readers_value, what=f"scheme {scheme_id} readers"))
            if readers_value not in (None, "")
            else None
        )
        result.append(
            _Scheme(
                id=scheme_id,
                size=size,
                time=time,
                format=scheme_format,
                artifact=artifact,
                readers=readers,
            )
        )
    return tuple(result)


def _normalize_groups(value: Any) -> list[_Group]:
    if isinstance(value, Mapping):
        entries: list[tuple[Any, Any]] = list(value.items())
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        entries = []
        for item in value:
            if not isinstance(item, Mapping):
                raise PlanInputError("groups must be objects")
            group_id = _first(
                item,
                ("id", "group", "name", "group_id"),
                what="group id",
                required=True,
            )
            entries.append((group_id, item))
    else:
        raise PlanInputError("groups must be a list or mapping")

    result: list[_Group] = []
    seen: set[str] = set()
    for group_id_value, details in entries:
        if not isinstance(group_id_value, str) or not group_id_value:
            raise PlanInputError("group ids must be non-empty strings")
        if group_id_value in seen:
            raise PlanInputError(f"duplicate group id: {group_id_value}")
        seen.add(group_id_value)
        if isinstance(details, Mapping):
            schemes_value = _first(
                details,
                _SCHEME_KEYS,
                what=f"schemes of group {group_id_value}",
                required=True,
            )
        elif isinstance(details, list):
            schemes_value = details
        else:
            raise PlanInputError(f"group {group_id_value} must be an object or list")
        schemes = _normalize_schemes(group_id_value, schemes_value)
        if not schemes:
            raise PlanInputError(f"group {group_id_value} has no schemes")
        result.append(_Group(group_id_value, schemes))
    return result


def _scheme_readers(scheme: _Scheme, receivers: Sequence[_Receiver]) -> frozenset[str]:
    if scheme.readers is not None:
        return scheme.readers
    result = set()
    for receiver in receivers:
        if receiver.formats is None or "*" in receiver.formats:
            result.add(receiver.id)
        elif scheme.format == "*":
            result.add(receiver.id)
        elif scheme.format is not None and scheme.format in receiver.formats:
            result.add(receiver.id)
    return frozenset(result)


def _build_candidates(
    group: _Group,
    receiver_ids: frozenset[str],
    readers: Mapping[str, frozenset[str]],
) -> list[_Candidate]:
    covers: list[_Candidate] = []
    for count in (1, 2):
        for chosen in combinations(group.schemes, count):
            covered: set[str] = set()
            for scheme in chosen:
                covered.update(readers[scheme.id])
            if covered != set(receiver_ids):
                continue
            covers.append(
                _Candidate(
                    schemes=chosen,
                    size=sum(scheme.size for scheme in chosen),
                    time=sum((scheme.time for scheme in chosen), Fraction(0)),
                )
            )
    return _pareto_candidates(covers)


def _pareto_candidates(candidates: Iterable[_Candidate]) -> list[_Candidate]:
    ordered = sorted(candidates, key=lambda candidate: candidate.key)
    kept: list[_Candidate] = []
    for candidate in ordered:
        if any(
            other.size <= candidate.size
            and other.time <= candidate.time
            and (
                other.size < candidate.size
                or other.time < candidate.time
                or other.copies < candidate.copies
            )
            for other in kept
        ):
            continue
        kept.append(candidate)
    return kept


def _pareto_states(states: Iterable[_State]) -> list[_State]:
    best_by_size: dict[int, _State] = {}
    for state in states:
        current = best_by_size.get(state.size)
        if current is None or state.key < current.key:
            best_by_size[state.size] = state
    ordered = sorted(
        best_by_size.values(), key=lambda state: (state.size, state.time, state.key)
    )
    kept: list[_State] = []
    best_time = None
    for state in ordered:
        if best_time is not None and state.time >= best_time:
            continue
        kept.append(state)
        best_time = state.time
    return kept


def _select(
    groups: Sequence[_Group],
    candidates_by_group: Sequence[Sequence[_Candidate]],
    limit: Fraction | None,
) -> _State | None:
    states = [_State(groups=())]
    for group, candidates in zip(groups, candidates_by_group):
        if not candidates:
            return None
        next_states: list[_State] = []
        for state in states:
            for candidate in candidates:
                time = state.time + candidate.time
                if limit is not None and time > limit:
                    continue
                next_states.append(_State(state.groups + ((group.id, candidate),)))
        states = _pareto_states(next_states)
        if not states:
            return None
    return min(states, key=lambda state: state.key)


def _fastest_state(
    groups: Sequence[_Group],
    candidates_by_group: Sequence[Sequence[_Candidate]],
) -> _State:
    selected: list[tuple[str, _Candidate]] = []
    for group, candidates in zip(groups, candidates_by_group):
        if not candidates:
            raise AssertionError(f"group {group.id} has no cover")
        selected.append(
            (group.id, min(candidates, key=lambda c: (c.time, c.size, c.key)))
        )
    return _State(tuple(selected))


def _group_choices(state: _State) -> tuple[GroupChoice, ...]:
    result = []
    for group_id, candidate in state.groups:
        result.append(
            GroupChoice(
                group=group_id,
                copies=tuple(
                    CopyChoice(
                        group=group_id,
                        scheme=scheme.id,
                        size=scheme.size,
                        time=scheme.time,
                        format=scheme.format,
                        artifact=scheme.artifact,
                    )
                    for scheme in candidate.schemes
                ),
            )
        )
    return tuple(result)


def _best_two_copy_coverage(
    group: _Group, readers: Mapping[str, frozenset[str]]
) -> frozenset[str]:
    best: frozenset[str] = frozenset()
    for count in (1, 2):
        for chosen in combinations(group.schemes, count):
            covered: set[str] = set()
            for scheme in chosen:
                covered.update(readers[scheme.id])
            if len(covered) > len(best):
                best = frozenset(covered)
    return best


def _missing_by_group(
    groups: Sequence[_Group],
    receiver_ids: frozenset[str],
    readers_by_group: Mapping[str, Mapping[str, frozenset[str]]],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    result = []
    for group in groups:
        covered = _best_two_copy_coverage(group, readers_by_group[group.id])
        missing = tuple(sorted(receiver_ids - covered))
        if missing:
            result.append((group.id, missing))
    return tuple(result)


def _read_paths(
    receiver_ids: Sequence[str],
    state: _State,
    readers_by_group: Mapping[str, Mapping[str, frozenset[str]]],
) -> tuple[ReceiverPath, ...]:
    paths = []
    for receiver_id in receiver_ids:
        route: list[tuple[str, tuple[str, ...]]] = []
        for group_id, candidate in state.groups:
            readable = [
                scheme
                for scheme in candidate.schemes
                if receiver_id in readers_by_group[group_id][scheme.id]
            ]
            if not readable:
                raise AssertionError(
                    f"selected plan does not cover {receiver_id} in {group_id}"
                )
            ordered = sorted(
                readable, key=lambda scheme: (scheme.size, scheme.time, scheme.id)
            )
            route.append((group_id, tuple(scheme.id for scheme in ordered)))
        paths.append(ReceiverPath(receiver=receiver_id, groups=tuple(route)))
    return tuple(paths)


def _rows_to_manifest(rows: Sequence[Any]) -> dict[str, Any]:
    """Group flat scheme rows into a manifest."""
    groups: dict[str, list[dict[str, Any]]] = {}
    receivers: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise PlanInputError(f"row {index} must be an object")
        group_id = _first(
            row, _ROW_GROUP_KEYS, what=f"row {index} group", required=True
        )
        if not isinstance(group_id, str) or not group_id:
            raise PlanInputError(f"row {index} group must be a non-empty string")
        scheme_id = _first(
            row, _ROW_SCHEME_KEYS, what=f"row {index} scheme", required=True
        )
        entry = {key: value for key, value in row.items()}
        entry["id"] = scheme_id
        groups.setdefault(group_id, []).append(entry)
        readers_value = _first(
            row, _READER_KEYS, what=f"row {index} readers", required=False
        )
        if readers_value is not None:
            for name in _split_names(readers_value, what=f"row {index} readers"):
                if name not in receivers:
                    receivers.append(name)
    manifest: dict[str, Any] = {
        "groups": [
            {"id": group_id, "schemes": schemes} for group_id, schemes in groups.items()
        ]
    }
    if receivers:
        manifest["receivers"] = receivers
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        for key in _TIME_LIMIT_KEYS:
            if key in row:
                manifest["time_limit"] = row[key]
                break
        else:
            continue
        break
    for key in ("time_unit",):
        for row in rows:
            if isinstance(row, Mapping) and key in row:
                manifest[key] = row[key]
                break
    return manifest


def _read_rows(text: str, suffix: str) -> list[dict[str, Any]]:
    import csv
    import io

    delimiter = "\t" if suffix == ".tsv" else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if reader.fieldnames is None:
        raise PlanInputError("CSV/TSV input is empty")
    rows: list[dict[str, Any]] = []
    for raw in reader:
        row = {
            str(key).strip(): (value.strip() if isinstance(value, str) else value)
            for key, value in raw.items()
            if key is not None
        }
        if any(value not in (None, "") for value in row.values()):
            rows.append(row)
    return rows


def _coerce_manifest(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        if value and all(isinstance(item, Mapping) for item in value):
            first = value[0]
            if any(key in first for key in _ROW_GROUP_KEYS):
                return _rows_to_manifest(value)
        return {"groups": value}
    if isinstance(value, Mapping):
        rows_value = _first(
            value,
            ("rows", "schemes", "entries", "records"),
            what="rows",
            required=False,
        )
        if (
            rows_value is not None
            and _first(value, _GROUP_KEYS, what="", required=False) is None
        ):
            converted = _rows_to_manifest(rows_value)
            for key, item in value.items():
                if key not in ("rows", "schemes", "entries", "records"):
                    converted.setdefault(key, item)
            return converted
    return value


def plan_archive(manifest: Mapping[str, Any]) -> PlanResult:
    """Plan archive writes from an already parsed manifest."""
    if not isinstance(manifest, Mapping):
        raise PlanInputError("manifest must be an object")
    groups_value = _first(manifest, _GROUP_KEYS, what="groups", required=True)
    groups = _normalize_groups(groups_value)
    if not groups:
        raise PlanInputError("manifest must contain at least one group")

    limit_value = _first(
        manifest, _TIME_LIMIT_KEYS, what="total time limit", required=False
    )
    limit = (
        _as_fraction(limit_value, what="total time limit")
        if limit_value is not None
        else None
    )
    time_unit_value = manifest.get("time_unit", "same as manifest times")
    if not isinstance(time_unit_value, str):
        raise PlanInputError("time_unit must be a string")

    receivers_value = _first(manifest, _RECEIVER_KEYS, what="receivers", required=False)
    receivers = _normalize_receivers(receivers_value)
    explicit_reader_names: set[str] = set()
    for group in groups:
        for scheme in group.schemes:
            if scheme.readers is not None:
                explicit_reader_names.update(scheme.readers)
    if receivers:
        receiver_ids = frozenset(receiver.id for receiver in receivers)
        unknown = explicit_reader_names - receiver_ids
        if unknown:
            raise PlanInputError(
                "schemes refer to unknown receivers: " + ", ".join(sorted(unknown))
            )
    else:
        receiver_ids = frozenset(explicit_reader_names)
        receivers = [
            _Receiver(receiver_id, None) for receiver_id in sorted(receiver_ids)
        ]

    if not receiver_ids:
        raise PlanInputError(
            "manifest has no receivers; list receivers or name readable receivers per scheme"
        )

    readers_by_group: dict[str, dict[str, frozenset[str]]] = {}
    warnings: list[str] = []
    for group in groups:
        group_readers: dict[str, frozenset[str]] = {}
        for scheme in group.schemes:
            scheme_readers = _scheme_readers(scheme, receivers)
            unknown = scheme_readers - receiver_ids
            if unknown:
                raise PlanInputError(
                    f"scheme {scheme.id} refers to unknown receivers: "
                    + ", ".join(sorted(unknown))
                )
            group_readers[scheme.id] = scheme_readers
            if scheme.readers is None and scheme.format is None:
                warnings.append(
                    f"scheme {scheme.id} in group {group.id} has neither readers nor a format; "
                    "it only covers receivers without format restrictions"
                )
        readers_by_group[group.id] = group_readers

    missing = _missing_by_group(groups, receiver_ids, readers_by_group)
    if missing:
        return PlanResult(
            status="uncoverable",
            time_limit=limit,
            time_unit=time_unit_value,
            total_size=0,
            total_time=Fraction(0),
            budget_margin=None,
            minimum_cover_time=None,
            minimum_extra_time=None,
            groups=(),
            receivers=(),
            missing_by_group=missing,
            warnings=tuple(warnings),
        )

    candidates_by_group = [
        _build_candidates(group, receiver_ids, readers_by_group[group.id])
        for group in groups
    ]
    state = _select(groups, candidates_by_group, limit)
    fastest = _fastest_state(groups, candidates_by_group)
    minimum_cover_time = fastest.time
    minimum_extra_time = (
        max(Fraction(0), minimum_cover_time - limit) if limit is not None else None
    )

    if state is None:
        state = fastest
        status = "over_budget"
    else:
        status = "ok"

    margin = limit - state.time if limit is not None else None
    return PlanResult(
        status=status,
        time_limit=limit,
        time_unit=time_unit_value,
        total_size=state.size,
        total_time=state.time,
        budget_margin=margin,
        minimum_cover_time=minimum_cover_time,
        minimum_extra_time=minimum_extra_time,
        groups=_group_choices(state),
        receivers=_read_paths(sorted(receiver_ids), state, readers_by_group),
        missing_by_group=(),
        warnings=tuple(warnings),
    )


def plan_manifest(manifest: Mapping[str, Any]) -> PlanResult:
    """Alias for :func:`plan_archive`."""
    return plan_archive(manifest)


def plan_from_json(source: str | Path | TextIO | Any) -> PlanResult:
    """Read a manifest from a path, an open text stream or JSON/CSV text."""
    suffix = ""
    if isinstance(source, Path):
        text = source.read_text(encoding="utf-8")
        suffix = source.suffix.lower()
    elif isinstance(source, str):
        if source.lstrip()[:1] in ("{", "["):
            text = source
        else:
            source_path = Path(source)
            text = source_path.read_text(encoding="utf-8")
            suffix = source_path.suffix.lower()
    else:
        text = source.read()
    if suffix in (".csv", ".tsv"):
        rows = _read_rows(text, suffix)
        return plan_archive(_coerce_manifest(rows))
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as error:
        raise PlanInputError(f"invalid JSON: {error}") from error
    return plan_archive(_coerce_manifest(manifest))


def _number(value: Fraction | None) -> str | None:
    if value is None:
        return None
    if value.denominator == 1:
        return str(value.numerator)
    text = format(float(value), ".15g")
    if Fraction(text) == value:
        return text
    return f"{value.numerator}/{value.denominator}"


def plan_to_dict(result: PlanResult) -> dict[str, Any]:
    """Convert a plan result to JSON-ready data with exact time strings."""
    return {
        "schema_version": 1,
        "status": result.status,
        "time_limit": _number(result.time_limit),
        "time_unit": result.time_unit,
        "total_size": result.total_size,
        "total_bytes": result.total_size,
        "total_time": _number(result.total_time),
        "budget_margin": _number(result.budget_margin),
        "remaining_time": _number(result.budget_margin),
        "minimum_cover_time": _number(result.minimum_cover_time),
        "minimum_extra_time": _number(result.minimum_extra_time),
        "shortfall": _number(result.minimum_extra_time),
        "selection": [
            {"group": group, "schemes": list(schemes)}
            for group, schemes in result.selection
        ],
        "groups": [
            {
                "group": group.group,
                "size": group.size,
                "time": _number(group.time),
                "copies": [
                    {
                        "group": copy.group,
                        "scheme": copy.scheme,
                        "size": copy.size,
                        "time": _number(copy.time),
                        "format": copy.format,
                        "artifact": copy.artifact,
                    }
                    for copy in group.copies
                ],
            }
            for group in result.groups
        ],
        "receivers": [
            {
                "receiver": path.receiver,
                "primary": [
                    {"group": group, "scheme": scheme} for group, scheme in path.primary
                ],
                "groups": [
                    {"group": group, "readable_schemes": list(schemes)}
                    for group, schemes in path.groups
                ],
            }
            for path in result.receivers
        ],
        "missing_by_group": [
            {"group": group, "receivers": list(receivers)}
            for group, receivers in result.missing_by_group
        ],
        "warnings": list(result.warnings),
    }


def format_plan_json(result: PlanResult, *, indent: int = 2) -> str:
    """Format a plan result as JSON."""
    return json.dumps(plan_to_dict(result), ensure_ascii=False, indent=indent)


def _unit(result: PlanResult) -> str:
    return (
        "" if result.time_unit == "same as manifest times" else f" {result.time_unit}"
    )


def format_plan_text(result: PlanResult) -> str:
    """Format a plan result as a compact human-readable checklist."""
    if result.status == "ok":
        title = "归档写出方案清单"
    elif result.status == "over_budget":
        title = "归档写出方案清单（限额不足）"
    else:
        title = "归档写出方案清单（无法覆盖）"
    lines = [title, "=" * 32, f"状态: {result.status}"]
    if result.time_limit is not None:
        lines.append(f"总写入时间限额: {_number(result.time_limit)}{_unit(result)}")
    total_label = "参考方案" if result.status == "over_budget" else "选用方案"
    lines.append(f"{total_label}总字节: {result.total_size} B")
    lines.append(
        f"{total_label}总写入时间: {_number(result.total_time)}{_unit(result)}"
    )
    if result.budget_margin is not None:
        lines.append(f"预算余量: {_number(result.budget_margin)}{_unit(result)}")
    if result.status == "over_budget":
        lines.append(
            f"最少还差: {_number(result.minimum_extra_time)}{_unit(result)}"
            f"（最快全覆盖需要 {_number(result.minimum_cover_time)}{_unit(result)}）"
        )
    if result.status == "uncoverable":
        lines.append("以下分组在最多两份副本限制下无法覆盖全部接收方：")
        for group, receivers in result.missing_by_group:
            lines.append(f"- {group}: 缺少 {'、'.join(receivers)}")
    else:
        heading = (
            "最快全覆盖参考（已超限）" if result.status == "over_budget" else "分组选用"
        )
        lines.extend(["", heading])
        for group in result.groups:
            copies = "；".join(
                f"{copy.scheme}"
                + (f" [{copy.format}]" if copy.format else "")
                + f" {copy.size} B / {_number(copy.time)}{_unit(result)}"
                + (f" -> {copy.artifact}" if copy.artifact else "")
                for copy in group.copies
            )
            lines.append(
                f"- {group.group}: {copies}（{len(group.copies)} 份，小计 {group.size} B）"
            )
        lines.extend(["", "接收方读取途径"])
        for path in result.receivers:
            route = "；".join(
                f"{group} -> {schemes[0]}" for group, schemes in path.groups
            )
            alternate = [
                f"{group}: {'/'.join(schemes[1:])}"
                for group, schemes in path.groups
                if len(schemes) > 1
            ]
            if alternate:
                route += f"（备选 {'；'.join(alternate)}）"
            lines.append(f"- {path.receiver}: {route}")
    if result.warnings:
        lines.extend(["", "提示"])
        lines.extend(f"- {warning}" for warning in result.warnings)
    return "\n".join(lines) + "\n"


def format_plan_markdown(result: PlanResult) -> str:
    """Format a plan result as a Markdown checklist."""
    unit = _unit(result)
    if result.status == "ok":
        title = "归档写出方案清单"
    elif result.status == "over_budget":
        title = "归档写出方案清单（限额不足）"
    else:
        title = "归档写出方案清单（无法覆盖）"
    lines = [f"# {title}", "", f"- 状态：`{result.status}`"]
    if result.time_limit is not None:
        lines.append(f"- 总写入时间限额：{_number(result.time_limit)}{unit}")
    total_label = "参考方案" if result.status == "over_budget" else "选用方案"
    lines.append(f"- {total_label}总字节：{result.total_size} B")
    lines.append(f"- {total_label}总写入时间：{_number(result.total_time)}{unit}")
    if result.budget_margin is not None:
        lines.append(f"- 预算余量：{_number(result.budget_margin)}{unit}")
    if result.status == "over_budget":
        lines.append(
            f"- 最少还差：{_number(result.minimum_extra_time)}{unit} "
            f"（最快全覆盖需要 {_number(result.minimum_cover_time)}{unit}）"
        )
    if result.status == "uncoverable":
        lines.extend(["", "## 无法覆盖"])
        for group, receivers in result.missing_by_group:
            lines.append(f"- `{group}`：缺少 {'、'.join(receivers)}")
    else:
        heading = (
            "最快全覆盖参考（已超限）" if result.status == "over_budget" else "分组选用"
        )
        lines.extend(
            [
                "",
                f"## {heading}",
                "| 分组 | 方案 | 格式 | 字节 | 写入时间 | 产物 |",
                "| --- | --- | --- | ---: | ---: | --- |",
            ]
        )
        for group in result.groups:
            for copy in group.copies:
                lines.append(
                    f"| {group.group} | {copy.scheme} | {copy.format or ''} | {copy.size} "
                    f"| {_number(copy.time)}{unit} | {copy.artifact or ''} |"
                )
        lines.extend(["", "## 接收方读取途径"])
        for path in result.receivers:
            route = "；".join(
                f"{group} → {schemes[0]}" for group, schemes in path.groups
            )
            lines.append(f"- **{path.receiver}**：{route}")
    if result.warnings:
        lines.extend(["", "## 提示"])
        lines.extend(f"- {warning}" for warning in result.warnings)
    return "\n".join(lines) + "\n"


_DEFAULT_MANIFEST_NAMES = (
    "archive_plan.json",
    "archive-plan.json",
    "manifest.json",
    "input.json",
)


def _find_default_manifest() -> str:
    for name in _DEFAULT_MANIFEST_NAMES:
        if Path(name).is_file():
            return name
    return "-"


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="xopen-archive-plan",
        description="Select archive write schemes under a total write-time limit.",
    )
    parser.add_argument(
        "manifest",
        nargs="?",
        default=None,
        help="JSON manifest (default: archive_plan.json if present, else stdin)",
    )
    parser.add_argument(
        "-o", "--output", help="write the report to a file instead of stdout"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    group.add_argument(
        "--markdown", action="store_true", help="emit a Markdown checklist"
    )
    group.add_argument(
        "--format",
        choices=("text", "json", "markdown"),
        help="report format (same as --json/--markdown)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit with status 3 unless a complete plan fits the budget",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="only report whether a complete plan fits; exit 0 or 3",
    )
    args = parser.parse_args(argv)
    if args.manifest is None:
        args.manifest = _find_default_manifest()

    try:
        if args.manifest == "-":
            result = plan_from_json(sys.stdin)
        else:
            result = plan_from_json(args.manifest)
    except (OSError, PlanInputError) as error:
        print(f"错误: {error}", file=sys.stderr)
        return 2

    if args.check:
        summary = (
            f"{result.status} total_size={result.total_size} "
            f"total_time={_number(result.total_time)}"
        )
        if result.status == "over_budget" and result.minimum_extra_time is not None:
            summary += f" minimum_extra_time={_number(result.minimum_extra_time)}"
        sys.stdout.write(summary + "\n")
        return 0 if result.status == "ok" else 3

    if args.format is not None:
        report_format = args.format
    elif args.json:
        report_format = "json"
    elif args.markdown:
        report_format = "markdown"
    else:
        report_format = "text"
    if report_format == "json":
        report = format_plan_json(result)
    elif report_format == "markdown":
        report = format_plan_markdown(result)
    else:
        report = format_plan_text(result)
    if not report.endswith("\n"):
        report += "\n"

    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
    else:
        sys.stdout.write(report)

    if args.strict and result.status != "ok":
        return 3
    return 0


def cli() -> None:
    """Console-script wrapper around :func:`main`."""
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
