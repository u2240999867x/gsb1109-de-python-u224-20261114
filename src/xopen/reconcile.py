"""
Memory-bounded reconciliation of logical lines in two compressed files.

The two inputs may use different compression formats (bzip2 and Zstandard
among others, see :func:`xopen.xopen`). Lines are compared as a multiset:
their order does not matter, but the number of copies of every distinct
line is accounted for exactly, even when the data is far larger than the
available memory budget.

Line identity is kept as raw bytes: no text decoding is performed and
distinct byte sequences are never collapsed. Whether CRLF and LF line
endings are equivalent is controlled by the caller via the
``newline_equivalent`` option.

When a decompression error occurs on either side, a :class:`ReconcileError`
is raised and no difference report is produced. A truncated archive can
therefore never be mistaken for a complete data set.
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_MAX_OCCURRENCES",
    "DEFAULT_MEMORY_BUDGET",
    "LineDifference",
    "LineOccurrence",
    "ReconcileError",
    "ReconcileSummary",
    "iter_line_differences",
    "reconcile_lines",
]

import dataclasses
import heapq
import os
import shutil
import struct
import tempfile
from collections.abc import Iterator
from typing import BinaryIO

from xopen import xopen

try:
    import resource
except ImportError:  # pragma: no cover - Windows has no resource module
    resource = None

#: Default ceiling for the memory used by the in-memory hash tables.
DEFAULT_MEMORY_BUDGET = 256 * 1024 * 1024

#: Default number of occurrence positions retained per line and side.
DEFAULT_MAX_OCCURRENCES = 100

#: Smallest accepted memory budget (one MiB).
_MIN_MEMORY_BUDGET = 1 << 20

#: Smallest amount of budget ever handed to one hash table.
_MIN_TABLE_BYTES = 1 << 19

#: Fixed headroom for read buffers and other allocations outside the tables.
_IO_ALLOWANCE = 4 * 1024 * 1024

#: Fraction of the remaining budget that may be charged to a side's table.
_TABLE_LOAD_FRACTION = 0.85

#: Conservative bookkeeping charge for one hash table entry and its key.
_ENTRY_BASE_COST = 300

#: Conservative bookkeeping charge for one retained occurrence position.
_OCCURRENCE_COST = 72

#: Hard upper bound for retained positions, dictated by the on-disk format.
_MAX_OCCURRENCES_LIMIT = 65535

_READ_CHUNK_SIZE = 128 * 1024

_RUN_MAGIC = b"XRCN0001"
_RECORD_HEADER = struct.Struct("<IQBH")  # key length, count, truncated, n positions
_POSITION_PAIR = struct.Struct("<QQ")  # line number, byte offset


def _default_merge_fanout() -> int:
    """Largest number of run files opened simultaneously during merging."""
    if resource is None:
        return 16
    try:
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    except OSError:
        return 16
    if soft == resource.RLIM_INFINITY:
        soft = 1024
    # Reserve descriptors for the input archives, output run and runtime use.
    return max(4, min(64, (soft - 16) // 4))


def _path_label(path) -> str:
    """Human-readable label for a path or open file-like object."""
    try:
        return os.fspath(path)
    except TypeError:
        name = getattr(path, "name", None)
        if name is not None:
            return str(name)
        return repr(path)


class ReconcileError(Exception):
    """Raised when an input cannot be decompressed and read completely."""

    def __init__(self, side: str, path, original: BaseException):
        self.side = side
        self.path = _path_label(path)
        self.original = original
        super().__init__(
            f"Failed to fully read the {side} file {self.path!r}: "
            f"{type(original).__name__}: {original}"
        )


@dataclasses.dataclass(frozen=True, slots=True)
class LineOccurrence:
    """A reviewable position of one line inside the decompressed stream."""

    #: 1-based line number in the decompressed data.
    line_number: int
    #: Byte offset of the start of the line in the decompressed data.
    byte_offset: int


@dataclasses.dataclass(frozen=True, slots=True)
class LineDifference:
    """Multiplicity mismatch for one distinct line."""

    #: Raw bytes of the line (normalized line ending when equivalence is on).
    line: bytes
    left_count: int
    right_count: int
    #: Copies present on the right but missing on the left.
    left_missing: int
    #: Copies present on the left but missing on the right.
    right_missing: int
    left_occurrences: tuple[LineOccurrence, ...]
    right_occurrences: tuple[LineOccurrence, ...]
    left_occurrences_truncated: bool
    right_occurrences_truncated: bool


@dataclasses.dataclass(frozen=True, slots=True)
class ReconcileSummary:
    """Result of a full reconciliation."""

    left_path: str
    right_path: str
    left_line_total: int
    right_line_total: int
    left_missing_total: int
    right_missing_total: int
    differences: tuple[LineDifference, ...]

    @property
    def identical(self) -> bool:
        return not self.differences


@dataclasses.dataclass(slots=True)
class _Entry:
    count: int = 0
    positions: list[LineOccurrence] = dataclasses.field(default_factory=list)
    truncated: bool = False


def _line_identity(raw_line: bytes, newline_equivalent: bool) -> bytes:
    if newline_equivalent and raw_line.endswith(b"\r\n"):
        return raw_line[:-2] + b"\n"
    return raw_line


def _iter_raw_lines(stream: BinaryIO):
    """Yield ``(raw_line, line_number, byte_offset)`` triples.

    Lines are split on LF only, so every other byte (including CR) is part
    of the line identity. A final chunk without a trailing LF is reported
    as its own line; an empty stream yields nothing.
    """
    line_number = 0
    byte_offset = 0
    pending = b""
    while True:
        chunk = stream.read(_READ_CHUNK_SIZE)
        if not chunk:
            if pending:
                line_number += 1
                yield pending, line_number, byte_offset
            return
        chunk_start = 0
        while True:
            newline_index = chunk.find(b"\n", chunk_start)
            if newline_index < 0:
                pending += chunk[chunk_start:]
                break
            line_number += 1
            raw_line = pending + chunk[chunk_start : newline_index + 1]
            yield raw_line, line_number, byte_offset
            byte_offset += len(raw_line)
            pending = b""
            chunk_start = newline_index + 1


def _verify_clean_eof(stream: BinaryIO, side: str, path) -> None:
    """Make sure the decompressor consumed the complete archive."""
    if stream.read(1) != b"":
        raise ReconcileError(
            side,
            path,
            EOFError("unexpected trailing data after line scanning"),
        )
    raw = stream
    while hasattr(raw, "raw"):
        raw = raw.raw
    eof = getattr(raw, "eof", None)
    if eof is False:
        raise ReconcileError(
            side,
            path,
            EOFError("decompressor did not reach the end of the stream"),
        )


class _SpillBuilder:
    """Collects line multiplicities, spilling sorted runs under memory pressure."""

    def __init__(
        self,
        side: str,
        path,
        *,
        newline_equivalent: bool,
        memory_budget: int,
        max_occurrences: int,
        work_dir: str,
    ):
        self.side = side
        self.path = path
        self._newline_equivalent = newline_equivalent
        self._max_occurrences = max_occurrences
        self._work_dir = work_dir
        self._threshold = max(
            _MIN_TABLE_BYTES,
            int((memory_budget - _IO_ALLOWANCE) * _TABLE_LOAD_FRACTION),
        )
        self._table: dict[bytes, _Entry] = {}
        self._charged = 0
        self._run_paths: list[str] = []
        self.line_total = 0

    def build(self) -> None:
        try:
            with xopen(self.path, "rb", threads=0) as stream:
                for raw_line, line_number, byte_offset in _iter_raw_lines(stream):
                    self._add(raw_line, line_number, byte_offset)
                _verify_clean_eof(stream, self.side, self.path)
        except ReconcileError:
            raise
        except Exception as error:
            raise ReconcileError(self.side, self.path, error) from error
        self._consolidate_runs()

    def _add(self, raw_line: bytes, line_number: int, byte_offset: int) -> None:
        key = _line_identity(raw_line, self._newline_equivalent)
        entry = self._table.get(key)
        if entry is None:
            entry = _Entry()
            self._table[key] = entry
            self._charged += _ENTRY_BASE_COST + len(key)
        entry.count += 1
        self.line_total += 1
        if len(entry.positions) < self._max_occurrences:
            entry.positions.append(LineOccurrence(line_number, byte_offset))
            self._charged += _OCCURRENCE_COST
        else:
            entry.truncated = True
        if self._charged >= self._threshold:
            self._spill()

    def _spill(self) -> None:
        if not self._table:
            return
        fd, run_path = tempfile.mkstemp(
            prefix=f"xreconcile-{self.side}-", suffix=".run", dir=self._work_dir
        )
        try:
            with os.fdopen(fd, "wb") as run:
                run.write(_RUN_MAGIC)
                for key in sorted(self._table):
                    entry = self._table[key]
                    self._write_record(
                        run, key, entry.count, entry.truncated, entry.positions
                    )
        except BaseException:
            os.unlink(run_path)
            raise
        self._run_paths.append(run_path)
        self._table.clear()
        self._charged = 0

    def _write_record(self, run, key, count, truncated, positions) -> None:
        run.write(
            _RECORD_HEADER.pack(len(key), count, truncated, len(positions))
        )
        run.write(key)
        for occurrence in positions:
            run.write(
                _POSITION_PAIR.pack(
                    occurrence.line_number, occurrence.byte_offset
                )
            )

    def _read_run(self, run_path: str):
        with open(run_path, "rb") as run:
            magic = run.read(len(_RUN_MAGIC))
            if magic != _RUN_MAGIC:
                raise ReconcileError(
                    self.side,
                    self.path,
                    ValueError(f"corrupt spill run {run_path!r}"),
                )
            while True:
                header = run.read(_RECORD_HEADER.size)
                if not header:
                    return
                key_length, count, truncated, position_count = _RECORD_HEADER.unpack(
                    header
                )
                key = run.read(key_length)
                positions = tuple(
                    LineOccurrence(
                        *_POSITION_PAIR.unpack(run.read(_POSITION_PAIR.size))
                    )
                    for _ in range(position_count)
                )
                yield key, count, bool(truncated), positions

    def _combine_runs(self, input_paths: list[str], output_path: str) -> None:
        """Merge sorted run files into one sorted run, aggregating equal keys."""
        streams = [self._read_run(path) for path in input_paths]
        merged = heapq.merge(*streams, key=lambda record: record[0])
        pending_key: bytes | None = None
        count = 0
        truncated = False
        positions: list[LineOccurrence] = []
        with open(output_path, "wb") as out:
            out.write(_RUN_MAGIC)

            def flush() -> None:
                self._write_record(out, pending_key, count, truncated, positions)

            for key, key_count, key_truncated, key_positions in merged:
                if key != pending_key:
                    if pending_key is not None:
                        flush()
                    pending_key = key
                    count = key_count
                    truncated = key_truncated
                    positions = list(key_positions)
                else:
                    count += key_count
                    truncated = truncated or key_truncated
                    room = self._max_occurrences - len(positions)
                    if room > 0:
                        positions.extend(key_positions[:room])
            if pending_key is not None:
                flush()

    def _consolidate_runs(self) -> None:
        """Tiered merge so the final number of open run files stays bounded."""
        fanout = _default_merge_fanout()
        current = self._run_paths
        while len(current) > fanout:
            next_level: list[str] = []
            for start in range(0, len(current), fanout):
                group = current[start : start + fanout]
                fd, output_path = tempfile.mkstemp(
                    prefix=f"xreconcile-{self.side}-merged-",
                    suffix=".run",
                    dir=self._work_dir,
                )
                os.close(fd)
                try:
                    self._combine_runs(group, output_path)
                except BaseException:
                    os.unlink(output_path)
                    raise
                next_level.append(output_path)
                for path in group:
                    os.unlink(path)
            current = next_level
        self._run_paths = current

    def _in_memory_records(self):
        for key in sorted(self._table):
            entry = self._table[key]
            yield key, entry.count, entry.truncated, tuple(entry.positions)

    def sorted_records(self):
        """Yield ``(key, count, truncated, positions)`` sorted by key."""
        # Run count is bounded by _consolidate_runs, so open files stay bounded.
        iterables = [self._read_run(path) for path in self._run_paths]
        if self._table:
            iterables.append(self._in_memory_records())
        if not iterables:
            return
        yield from heapq.merge(*iterables, key=lambda record: record[0])

    def cleanup(self) -> None:
        for run_path in self._run_paths:
            try:
                os.unlink(run_path)
            except FileNotFoundError:
                pass
        self._run_paths.clear()


def _merge_builders(
    left: _SpillBuilder,
    right: _SpillBuilder,
    max_occurrences: int,
) -> Iterator[LineDifference]:
    def tag(builder: _SpillBuilder):
        for key, count, truncated, positions in builder.sorted_records():
            yield key, builder.side, count, truncated, positions

    merged = heapq.merge(tag(left), tag(right), key=lambda record: record[0])

    current_key: bytes | None = None
    counts = [0, 0]
    truncated = [False, False]
    positions: list[list[LineOccurrence]] = [[], []]

    def flush_group() -> LineDifference | None:
        left_count, right_count = counts
        if left_count == right_count:
            return None
        left_missing = max(0, right_count - left_count)
        right_missing = max(0, left_count - right_count)
        return LineDifference(
            line=current_key,
            left_count=left_count,
            right_count=right_count,
            left_missing=left_missing,
            right_missing=right_missing,
            left_occurrences=tuple(positions[0]),
            right_occurrences=tuple(positions[1]),
            left_occurrences_truncated=truncated[0]
            or left_count > len(positions[0]),
            right_occurrences_truncated=truncated[1]
            or right_count > len(positions[1]),
        )

    for key, side, count, side_truncated, side_positions in merged:
        if key != current_key:
            if current_key is not None:
                difference = flush_group()
                if difference is not None:
                    yield difference
            current_key = key
            counts = [0, 0]
            truncated = [False, False]
            positions = [[], []]
        side_index = 0 if side == "left" else 1
        counts[side_index] += count
        truncated[side_index] = truncated[side_index] or side_truncated
        room = max_occurrences - len(positions[side_index])
        if room > 0:
            positions[side_index].extend(side_positions[:room])

    if current_key is not None:
        difference = flush_group()
        if difference is not None:
            yield difference


def _validate_options(
    memory_budget: int,
    max_occurrences: int,
) -> tuple[int, int]:
    if isinstance(memory_budget, bool) or not isinstance(memory_budget, int):
        raise TypeError("memory_budget must be an int number of bytes")
    if memory_budget < _MIN_MEMORY_BUDGET:
        raise ValueError(
            f"memory_budget must be at least {_MIN_MEMORY_BUDGET} bytes "
            f"(one MiB), got {memory_budget}"
        )
    if isinstance(max_occurrences, bool) or not isinstance(max_occurrences, int):
        raise TypeError("max_occurrences must be an int")
    if not 1 <= max_occurrences <= _MAX_OCCURRENCES_LIMIT:
        raise ValueError(
            "max_occurrences must be between 1 and "
            f"{_MAX_OCCURRENCES_LIMIT}, got {max_occurrences}"
        )
    return memory_budget, max_occurrences


class _Reconciliation(Iterator):
    """Iterator over differences with total line counts filled in on first use."""

    def __init__(
        self,
        left_path,
        right_path,
        *,
        newline_equivalent: bool,
        memory_budget: int,
        max_occurrences: int,
        tmp_dir,
    ):
        self._left_path = left_path
        self._right_path = right_path
        self._newline_equivalent = newline_equivalent
        self._memory_budget = memory_budget
        self._max_occurrences = max_occurrences
        self._tmp_dir = tmp_dir
        self._work_dir: str | None = None
        self._builders: list[_SpillBuilder] = []
        self._differences: Iterator[LineDifference] | None = None
        #: Total number of lines seen on each side (None before iteration).
        self.left_line_total: int | None = None
        self.right_line_total: int | None = None

    def __iter__(self) -> "_Reconciliation":
        return self

    def __next__(self) -> LineDifference:
        if self._differences is None:
            self._start()
        try:
            return next(self._differences)
        except StopIteration:
            self.close()
            raise
        except BaseException:
            self.close()
            raise

    def _start(self) -> None:
        self._work_dir = tempfile.mkdtemp(prefix="xreconcile-", dir=self._tmp_dir)
        try:
            left = _SpillBuilder(
                "left",
                self._left_path,
                newline_equivalent=self._newline_equivalent,
                memory_budget=self._memory_budget,
                max_occurrences=self._max_occurrences,
                work_dir=self._work_dir,
            )
            right = _SpillBuilder(
                "right",
                self._right_path,
                newline_equivalent=self._newline_equivalent,
                memory_budget=self._memory_budget,
                max_occurrences=self._max_occurrences,
                work_dir=self._work_dir,
            )
            self._builders = [left, right]
            # Build both sides completely before emitting anything, so a
            # decompression failure never produces a partial difference report.
            left.build()
            right.build()
            self.left_line_total = left.line_total
            self.right_line_total = right.line_total
            self._differences = _merge_builders(
                left, right, self._max_occurrences
            )
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for builder in self._builders:
            builder.cleanup()
        self._builders = []
        if self._work_dir is not None:
            shutil.rmtree(self._work_dir, ignore_errors=True)
            self._work_dir = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def iter_line_differences(
    left_path,
    right_path,
    *,
    newline_equivalent: bool = False,
    memory_budget: int = DEFAULT_MEMORY_BUDGET,
    max_occurrences: int = DEFAULT_MAX_OCCURRENCES,
    tmp_dir: str | os.PathLike | None = None,
) -> Iterator[LineDifference]:
    """
    Yield :class:`LineDifference` objects for lines whose multiplicity
    differs between the two files.

    The comparison is order-independent but multiplicity-exact. Processing
    is bounded by ``memory_budget`` bytes: hash tables exceeding the budget
    are spilled as sorted temporary runs under ``tmp_dir`` and merged again
    at the end. Occurrence positions per line and side are capped at
    ``max_occurrences`` while counts always stay exact.

    A :class:`ReconcileError` is raised, before any difference is yielded,
    if either side cannot be decompressed completely.
    """
    memory_budget, max_occurrences = _validate_options(
        memory_budget, max_occurrences
    )
    return _Reconciliation(
        left_path,
        right_path,
        newline_equivalent=newline_equivalent,
        memory_budget=memory_budget,
        max_occurrences=max_occurrences,
        tmp_dir=tmp_dir,
    )


def reconcile_lines(
    left_path,
    right_path,
    *,
    newline_equivalent: bool = False,
    memory_budget: int = DEFAULT_MEMORY_BUDGET,
    max_occurrences: int = DEFAULT_MAX_OCCURRENCES,
    tmp_dir: str | os.PathLike | None = None,
) -> ReconcileSummary:
    """
    Reconcile two compressed files and return a :class:`ReconcileSummary`.

    For result sets that may themselves exceed the memory budget, use
    :func:`iter_line_differences` instead.
    """
    reconciliation = iter_line_differences(
        left_path,
        right_path,
        newline_equivalent=newline_equivalent,
        memory_budget=memory_budget,
        max_occurrences=max_occurrences,
        tmp_dir=tmp_dir,
    )
    differences = tuple(reconciliation)
    left_missing_total = sum(item.left_missing for item in differences)
    right_missing_total = sum(item.right_missing for item in differences)
    return ReconcileSummary(
        left_path=_path_label(left_path),
        right_path=_path_label(right_path),
        left_line_total=reconciliation.left_line_total,
        right_line_total=reconciliation.right_line_total,
        left_missing_total=left_missing_total,
        right_missing_total=right_missing_total,
        differences=differences,
    )
