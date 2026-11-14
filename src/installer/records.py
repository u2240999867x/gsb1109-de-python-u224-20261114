"""Provides an object-oriented model for handling :pep:`376` RECORD files."""

import base64
import csv
import hashlib
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, cast

from installer.utils import copyfileobj_with_hashing, get_stream_length

__all__ = [
    "ConflictingRecordEntry",
    "Hash",
    "InvalidRecordEntry",
    "RecordEntry",
    "RecordGroup",
    "RecordOccurrence",
    "RecordRow",
    "RecordStatus",
    "RecordValidationResult",
    "group_record_rows",
    "parse_record_file",
    "parse_record_rows",
    "validate_record_entry",
]


@dataclass
class InvalidRecordEntry(Exception):
    """Raised when a RecordEntry is not valid, due to improper element values or count."""

    elements: Iterable[str]
    issues: Iterable[str]

    def __post_init__(self) -> None:
        super().__init__(", ".join(self.issues))


class ConflictingRecordEntry(InvalidRecordEntry):
    """Raised when RECORD contains different declarations for the same path.

    A RECORD file may contain the same entry more than once (some tools emit
    duplicate rows). Repeating the same declaration is harmless, but different
    declarations for one path are ambiguous. This error keeps every occurrence
    instead of silently choosing the last row.
    """

    def __init__(self, path: str, occurrences: Iterable["RecordOccurrence"]) -> None:
        """Initialize from every occurrence of ``path`` in RECORD."""
        self.path = path
        self.occurrences = tuple(occurrences)
        declarations = sorted({o.declaration for o in self.occurrences})
        super().__init__(
            elements=tuple(o.raw_path for o in self.occurrences),
            issues=[
                f"conflicting RECORD entries for {path!r}: "
                + ", ".join(repr(d) for d in declarations),
            ],
        )


class RecordStatus(str, Enum):
    """Outcome of validating one RECORD declaration against file contents.

    ``ABSENT`` means the declaration does not state the value (a blank hash or
    size cell), which is intentionally different from a declared zero. ``VERIFIED``
    means every declaration that is present matches the file. ``MISMATCH`` means
    at least one present declaration does not match.
    """

    ABSENT = "absent"
    VERIFIED = "verified"
    MISMATCH = "mismatch"

    # Readable aliases for callers that describe the same three states differently.
    DECLARATION_ABSENT = "absent"  # noqa: PIE796
    NOT_DECLARED = "absent"  # noqa: PIE796
    MISSING_DECLARATION = "absent"  # noqa: PIE796
    MATCHED = "verified"  # noqa: PIE796
    MISMATCHED = "mismatch"  # noqa: PIE796


@dataclass(frozen=True)
class RecordRow:
    """One raw row from a RECORD file, before path normalization.

    ``raw_path`` is exactly what appeared in the CSV cell. ``path`` is the
    normalized value used for matching files, so callers can report the original
    spelling without losing the canonical path.
    """

    elements: tuple[str, str, str]
    row_index: int = 0

    @classmethod
    def from_elements(cls, elements: Iterable[str], row_index: int = 0) -> "RecordRow":
        """Build a row from already-split CSV elements."""
        values = tuple(elements)
        if len(values) != 3:
            message = f"Row Index {row_index}: expected 3 elements, got {len(values)}"
            raise InvalidRecordEntry(elements=values, issues=[message])
        return cls((values[0], values[1], values[2]), row_index)

    @property
    def raw_path(self) -> str:
        """Path exactly as written in RECORD."""
        return self.elements[0]

    @property
    def path(self) -> str:
        """Path normalized for comparisons (Windows separators become ``/``)."""
        return self.raw_path.replace("\\", "/")

    @property
    def hash_element(self) -> str:
        """Raw hash cell."""
        return self.elements[1]

    @property
    def size_element(self) -> str:
        """Raw size cell."""
        return self.elements[2]

    @property
    def hash_declared(self) -> bool:
        """Whether a hash is declared (blank cells mean absent)."""
        return bool(self.hash_element.strip())

    @property
    def size_declared(self) -> bool:
        """Whether a size is declared; ``"0"`` is a declaration, ``""`` is not."""
        return bool(self.size_element.strip())

    @property
    def declaration(self) -> tuple[str, str]:
        """Canonical declaration, with blank cells represented as empty strings.

        Numeric sizes are canonicalized so ``"0"`` and ``"00"`` are the same
        declaration; invalid values are kept verbatim so they can still be
        grouped and reported.
        """
        hash_value = self.hash_element.strip() if self.hash_declared else ""

        if not self.size_declared:
            size_value = ""
        else:
            try:
                size_value = str(int(self.size_element))
            except ValueError:
                size_value = self.size_element.strip()

        return hash_value, size_value

    @property
    def line_number(self) -> int:
        """1-based line number for user-facing messages."""
        return self.row_index + 1

    def to_entry(self) -> "RecordEntry":
        """Parse this row into a :class:`RecordEntry`."""
        entry = RecordEntry.from_elements(
            self.path, self.hash_element, self.size_element
        )
        entry.raw_path = self.raw_path
        return entry


@dataclass(frozen=True)
class RecordOccurrence:
    """One place where a declaration for a path appeared in RECORD."""

    row: RecordRow

    @property
    def path(self) -> str:
        """Normalized path."""
        return self.row.path

    @property
    def raw_path(self) -> str:
        """Path as written in this occurrence."""
        return self.row.raw_path

    @property
    def declaration(self) -> tuple[str, str]:
        """Declaration made by this occurrence."""
        return self.row.declaration

    @property
    def line_number(self) -> int:
        """1-based line number of this occurrence."""
        return self.row.line_number

    def to_entry(self) -> "RecordEntry":
        """Parse this occurrence into a :class:`RecordEntry`."""
        return self.row.to_entry()


@dataclass(frozen=True)
class RecordGroup:
    """All occurrences of one normalized path in a RECORD file."""

    path: str
    occurrences: tuple[RecordOccurrence, ...]

    @property
    def raw_paths(self) -> tuple[str, ...]:
        """Original spellings, in file order, without duplicates."""
        return tuple(dict.fromkeys(o.raw_path for o in self.occurrences))

    @property
    def declarations(self) -> tuple[tuple[str, str], ...]:
        """Distinct declarations, in file order."""
        return tuple(dict.fromkeys(o.declaration for o in self.occurrences))

    @property
    def conflicting(self) -> bool:
        """Whether occurrences disagree about hash or size."""
        return len(self.declarations) > 1

    def require_entry(self) -> "RecordEntry":
        """Return the sole declaration, raising if occurrences conflict."""
        if self.conflicting:
            raise ConflictingRecordEntry(self.path, self.occurrences)
        return self.occurrences[0].to_entry()

    def origin(self) -> str:
        """Human-readable list of the lines where this path was declared."""
        return ", ".join(
            f"line {occurrence.line_number} ({occurrence.raw_path!r})"
            for occurrence in self.occurrences
        )


@dataclass(frozen=True)
class RecordValidationResult:
    """Structured result of validating one path against its RECORD declaration."""

    path: str
    raw_path: str
    status: RecordStatus
    entry: "RecordEntry | None" = None
    occurrences: tuple[RecordOccurrence, ...] = ()
    actual_hash: "Hash | None" = None
    actual_size: int | None = None
    hash_status: RecordStatus | None = None
    size_status: RecordStatus | None = None
    issues: tuple[str, ...] = ()

    @property
    def declared_hash(self) -> "Hash | None":
        """Hash declared in RECORD, if any."""
        return self.entry.hash_ if self.entry is not None else None

    @property
    def declared_size(self) -> int | None:
        """Size declared in RECORD, if any."""
        return self.entry.size if self.entry is not None else None

    @property
    def size_declared(self) -> bool:
        """Whether this path has a size declaration (zero is still declared)."""
        return bool(self.entry is not None and self.entry.size_declared)

    @property
    def hash_declared(self) -> bool:
        """Whether this path has a hash declaration."""
        return self.entry is not None and self.entry.hash_ is not None

    @property
    def verified(self) -> bool:
        """Whether all present declarations matched."""
        return self.status is RecordStatus.VERIFIED

    @property
    def declaration_absent(self) -> bool:
        """Whether at least one declaration is absent and none mismatch."""
        return self.status is RecordStatus.ABSENT

    @property
    def matched(self) -> bool:
        """Alias for :attr:`verified`."""
        return self.verified

    @property
    def mismatched(self) -> bool:
        """Whether a present declaration did not match."""
        return self.status is RecordStatus.MISMATCH

    @property
    def ok(self) -> bool:
        """Whether validation did not find a mismatch."""
        return self.status is not RecordStatus.MISMATCH

    @property
    def origins(self) -> tuple[RecordOccurrence, ...]:
        """Every declaration of this path, in file order."""
        return self.occurrences

    @property
    def origin(self) -> str:
        """Where the path was declared in RECORD."""
        return ", ".join(
            f"line {occurrence.line_number} ({occurrence.raw_path!r})"
            for occurrence in self.occurrences
        )

    @property
    def original_path(self) -> str:
        """Alias for :attr:`raw_path`."""
        return self.raw_path


@dataclass
class Hash:
    """Represents the "hash" element of a RecordEntry.

    Most consumers should use :py:meth:`Hash.parse` instead, since no
    validation or parsing is performed by this constructor.
    """

    name: str
    """Name of the hash function."""

    value: str
    """Hashed value."""

    def __str__(self) -> str:
        return f"{self.name}={self.value}"

    def validate(self, data: bytes) -> bool:
        """Validate that ``data`` matches this instance.

        :param data: Contents of the file.
        :return: Whether ``data`` matches the hashed value.
        """
        digest = hashlib.new(self.name, data).digest()
        value = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return self.value == value

    @classmethod
    def parse(cls, h: str) -> "Hash":
        """Build a Hash object, from a "name=value" string.

        This accepts a string of the format for the second element in a record,
        as described in :pep:`376`.

        Typical usage::

            Hash.parse("sha256=Y0sCextp4SQtQNU-MSs7SsdxD1W-gfKJtUlEbvZ3i-4")

        :param h: a name=value string
        """
        name, value = h.split("=", 1)
        return cls(name, value)


@dataclass
class RecordEntry:
    """Represents a single record in a RECORD file.

    A list of :py:class:`RecordEntry` objects fully represents a RECORD file.

    Most consumers should use :py:meth:`RecordEntry.from_elements`, since no
    validation or parsing is performed by this constructor.
    """

    path: str
    """File's path."""

    hash_: Hash | None
    """Hash of the file's contents."""

    size: int | None
    """File's size in bytes."""

    raw_path: str | None = None
    """Path as originally written in RECORD, before separator normalization."""

    @property
    def original_path(self) -> str:
        """Original spelling of the path, falling back to the normalized path."""
        return self.raw_path if self.raw_path is not None else self.path

    @property
    def hash_declared(self) -> bool:
        """Whether this entry declares a hash.

        A blank hash cell means the hash is absent, not that it is an empty hash.
        """
        return self.hash_ is not None

    @property
    def size_declared(self) -> bool:
        """Whether this entry declares a size.

        This is the distinction between a blank size cell (``None``) and a real
        empty file (``0``). Inventory-style consumers must use this property
        rather than treating ``size is None`` as zero bytes.
        """
        return self.size is not None

    def to_row(self, path_prefix: str | None = None) -> tuple[str, str, str]:
        """Convert this into a 3-element tuple that can be written in a RECORD file.

        :param path_prefix: A prefix to attach to the path -- must end in `/`
        :return: a (path, hash, size) row
        """
        if path_prefix is not None:
            assert path_prefix.endswith("/")
            path = path_prefix + self.path
        else:
            path = self.path

        # Convert Windows paths to use / for consistency
        if os.sep == "\\":
            path = path.replace("\\", "/")  # pragma: no cover

        return (
            path,
            str(self.hash_ or ""),
            str(self.size) if self.size is not None else "",
        )

    def __repr__(self) -> str:
        return (
            f"RecordEntry(path={self.path!r}, hash_={self.hash_!r}, size={self.size!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RecordEntry):
            return NotImplemented
        return (
            self.path == other.path
            and self.hash_ == other.hash_
            and self.size == other.size
        )

    def validate(self, data: bytes) -> bool:
        """Validate that ``data`` matches this instance.

        .. attention::
            .. deprecated:: 1.0.0
                Use :py:meth:`validate_stream` instead, with ``BytesIO(data)``.

        :param data: Contents of the file corresponding to this instance.
        :return: whether ``data`` matches hash and size.
        """
        if self.size is not None and len(data) != self.size:
            return False

        if self.hash_:
            return self.hash_.validate(data)

        return True

    def validation_result(self, data: bytes) -> "RecordValidationResult":
        """Validate ``data`` and return a structured result.

        :param data: Contents of the file corresponding to this instance.
        """
        return self.validate_stream_result(BytesIO(data))

    def validate_stream_result(self, stream: BinaryIO) -> "RecordValidationResult":
        """Validate data read from ``stream`` and return a structured result.

        The result separately records whether the size and hash were declared,
        and whether each present declaration matched. A blank cell is
        ``RecordStatus.ABSENT``; it is never interpreted as a declared zero.
        A mismatch in either present declaration makes the overall status
        ``RecordStatus.MISMATCH``, even when the file is zero bytes long.

        :param stream: Representing the contents of the file.
        """
        if self.hash_ is not None:
            with Path(os.devnull).open("wb") as new_target:
                actual_hash, actual_size = copyfileobj_with_hashing(
                    stream, cast("BinaryIO", new_target), self.hash_.name
                )
            actual_hash_value: Hash | None = Hash(self.hash_.name, actual_hash)
        else:
            actual_hash_value = None
            actual_size = get_stream_length(stream)

        hash_status, size_status = self._field_statuses(actual_hash_value, actual_size)

        if RecordStatus.MISMATCH in (hash_status, size_status):
            status = RecordStatus.MISMATCH
        elif RecordStatus.ABSENT in (hash_status, size_status):
            status = RecordStatus.ABSENT
        else:
            status = RecordStatus.VERIFIED

        issues = []
        if hash_status is RecordStatus.MISMATCH:
            issues.append("hash does not match RECORD")
        if size_status is RecordStatus.MISMATCH:
            issues.append(f"size does not match RECORD (expected {self.size})")

        return RecordValidationResult(
            path=self.path,
            raw_path=self.original_path,
            status=status,
            entry=self,
            actual_hash=actual_hash_value,
            actual_size=actual_size,
            hash_status=hash_status,
            size_status=size_status,
            issues=tuple(issues),
        )

    def _field_statuses(
        self, actual_hash: "Hash | None", actual_size: int
    ) -> tuple[RecordStatus, RecordStatus]:
        if self.hash_ is None:
            hash_status = RecordStatus.ABSENT
        elif self.hash_ == actual_hash:
            hash_status = RecordStatus.VERIFIED
        else:
            hash_status = RecordStatus.MISMATCH

        if self.size is None:
            size_status = RecordStatus.ABSENT
        elif self.size == actual_size:
            size_status = RecordStatus.VERIFIED
        else:
            size_status = RecordStatus.MISMATCH

        return hash_status, size_status

    def validate_stream(self, stream: BinaryIO) -> bool:
        """Validate that data read from stream matches this instance.

        :param stream: Representing the contents of the file.
        :return: Whether data read from stream matches hash and size.
        """
        return self.validate_stream_result(stream).status is not RecordStatus.MISMATCH

    @classmethod
    def from_elements(cls, path: str, hash_: str, size: str) -> "RecordEntry":
        r"""Build a RecordEntry object, from values of the elements.

        Typical usage::

            for row in parse_record_file(f):
                record = RecordEntry.from_elements(row[0], row[1], row[2])

        Meaning of each element is specified in :pep:`376`.

        :param path: first element (file's path)
        :param hash\_: second element (hash of the file's contents)
        :param size: third element (file's size in bytes)
        :raises InvalidRecordEntry: if any element is invalid
        """
        # Validate the passed values.
        issues = []

        if not path:
            issues.append("`path` cannot be empty")

        hash_value: Hash | None = None
        if hash_:
            try:
                hash_value = Hash.parse(hash_)
                if hash_value.name not in hashlib.algorithms_available:
                    issues.append(f"invalid hash algorithm '{hash_value.name}'")
                    hash_value = None
            except ValueError:
                issues.append("`hash` does not follow the required format")

        size_value: int | None = None
        if size:
            try:
                size_value = int(size)
            except ValueError:
                issues.append("`size` cannot be non-integer")

        if issues:
            raise InvalidRecordEntry(elements=(path, hash_, size), issues=issues)

        return cls(path=path, hash_=hash_value, size=size_value, raw_path=path)


def validate_record_entry(
    entry: RecordEntry,
    stream: BinaryIO,
    *,
    raw_path: str | None = None,
    occurrences: Iterable[RecordOccurrence] = (),
) -> RecordValidationResult:
    """Validate one entry, preserving its original path and origins.

    This is the per-entry counterpart to
    :py:meth:`RecordEntry.validate_stream_result`, for callers that already
    parsed :class:`RecordRow` objects and want the raw path and duplicate
    origins included in the result.
    """
    result = entry.validate_stream_result(stream)
    return RecordValidationResult(
        path=result.path,
        raw_path=raw_path if raw_path is not None else result.raw_path,
        status=result.status,
        entry=result.entry,
        occurrences=tuple(occurrences),
        actual_hash=result.actual_hash,
        actual_size=result.actual_size,
        hash_status=result.hash_status,
        size_status=result.size_status,
        issues=result.issues,
    )


def parse_record_rows(rows: Iterable[str]) -> Iterator[RecordRow]:
    """Parse a :pep:`376` RECORD, preserving each raw row.

    Unlike :func:`parse_record_file`, this does not normalize the path in place;
    use :attr:`RecordRow.raw_path` for the original spelling and
    :attr:`RecordRow.path` for matching.

    :param rows: iterator providing lines of a RECORD (no trailing newlines).
    """
    reader = csv.reader(rows, delimiter=",", quotechar='"', lineterminator="\n")
    for row_index, elements in enumerate(reader):
        yield RecordRow.from_elements(elements, row_index)


def parse_record_file(rows: Iterable[str]) -> Iterator[tuple[str, str, str]]:
    """Parse a :pep:`376` RECORD.

    Returns an iterable of 3-value tuples, that can be passed to
    :any:`RecordEntry.from_elements`. Commas inside quoted paths are part of the
    path because CSV quoting is honored.

    :param rows: iterator providing lines of a RECORD (no trailing newlines).
    """
    for row in parse_record_rows(rows):
        yield (row.path, row.hash_element, row.size_element)


def group_record_rows(
    rows: Iterable[RecordRow],
) -> dict[str, RecordGroup]:
    """Group rows by normalized path, preserving every occurrence.

    Repeated, identical declarations remain separate occurrences so callers can
    report every place the path was declared. Different declarations are
    retained as well; :attr:`RecordGroup.conflicting` identifies them so that a
    later row never silently overrides an earlier one.

    :param rows: rows from :func:`parse_record_rows`.
    """
    groups: dict[str, list[RecordOccurrence]] = {}
    for row in rows:
        groups.setdefault(row.path, []).append(RecordOccurrence(row))

    return {
        path: RecordGroup(path=path, occurrences=tuple(occurrences))
        for path, occurrences in groups.items()
    }
