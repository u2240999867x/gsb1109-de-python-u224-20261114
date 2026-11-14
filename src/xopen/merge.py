"""
Merge sorted (optionally compressed) files with a bounded number of open files.

The merge is stable: records with equal keys are ordered by the position of
their source in the ``inputs`` sequence and then by their one-based line
number within that source.  Duplicate keys are not collapsed.

When there are more inputs than the open-file budget allows, intermediate
runs are spilled to temporary files and merged in several passes.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import pickle
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator, Literal, Sequence

from . import xopen

__all__ = [
    "MergeError",
    "MergeResult",
    "merge_files",
    "merge_sorted",
    "merge_sorted_files",
]

Line = str | bytes
KeyFunction = Callable[[Line], Any]

_MISSING = object()


class MergeError(OSError):
    """
    An error tied to a particular input source and line.

    ``source`` is the file that caused the failure, ``line_number`` is the
    one-based row in that file (or None if the failure happened before/after
    any row), and ``source_index`` is the position of the source in the
    ``inputs`` sequence.  The original exception, if any, is available as
    ``original`` and through ``__cause__``.  ``MergeError`` derives from
    ``OSError`` so existing code that catches ``IOError`` keeps working.
    """

    def __init__(
        self,
        message: str,
        *,
        source: str | None = None,
        source_index: int | None = None,
        line_number: int | None = None,
        reason: str | None = None,
        original: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.source = source
        self.source_index = source_index
        self.line_number = line_number
        self.reason = reason
        self.original = original

    @property
    def row(self) -> int | None:
        """Alias for ``line_number``."""
        return self.line_number

    @property
    def line(self) -> int | None:
        """Alias for ``line_number``."""
        return self.line_number

    @property
    def filename(self) -> str | None:
        """Alias for ``source``, for callers used to OSError."""
        return self.source

    @property
    def path(self) -> str | None:
        """Alias for ``source``."""
        return self.source

    @property
    def input(self) -> str | None:
        """Alias for ``source``."""
        return self.source

    def __str__(self) -> str:
        location = self.source if self.source is not None else "<unknown source>"
        if self.line_number is not None:
            location = f"{location}:{self.line_number}"
        return f"{location}: {self.message}"


@dataclass(frozen=True)
class MergeResult:
    """Summary of a completed merge."""

    output: str
    sources: tuple[str, ...]
    row_counts: tuple[int, ...]
    total_rows: int
    distinct_keys: int
    duplicate_key_rows: int
    duplicate_line_rows: int
    passes: int
    manifest_path: str | None = None
    completed: bool = True

    @property
    def duplicate_rows(self) -> int:
        """Number of rows whose key occurred earlier in the merged output."""
        return self.duplicate_key_rows

    @property
    def duplicate_count(self) -> int:
        """Alias for ``duplicate_rows``."""
        return self.duplicate_key_rows

    @property
    def duplicates(self) -> int:
        """Alias for ``duplicate_rows``."""
        return self.duplicate_key_rows

    @property
    def exact_duplicate_rows(self) -> int:
        """Number of byte-identical rows after their first occurrence."""
        return self.duplicate_line_rows

    @property
    def line_count(self) -> int:
        """Total rows written, including duplicates."""
        return self.total_rows

    @property
    def source_count(self) -> int:
        """Number of inputs, including repeated paths."""
        return len(self.sources)

    @property
    def status(self) -> str:
        """``"complete"`` for a returned result."""
        return "complete" if self.completed else "failed"


@dataclass(frozen=True)
class _Record:
    key: Any
    source_index: int
    row: int
    line: Line


@dataclass(frozen=True)
class _Run:
    path: str
    kind: Literal["source", "temp"]
    source_index: int | None


@dataclass(frozen=True)
class _Context:
    key: KeyFunction
    binary: bool
    encoding: str
    errors: str | None
    newline: str | None
    threads: int | None
    source_count: int
    sources: tuple[str, ...]


def _as_text_path(path: os.PathLike | str | bytes) -> str:
    return os.fsdecode(os.fspath(path))


def _short_repr(value: Any, limit: int = 120) -> str:
    text = repr(value)
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


def _error(
    message: str,
    *,
    source: str | None,
    source_index: int | None,
    line_number: int | None,
    reason: str,
    original: BaseException | None = None,
) -> MergeError:
    return MergeError(
        message,
        source=source,
        source_index=source_index,
        line_number=line_number,
        reason=reason,
        original=original,
    )


def _wrap_read_error(
    exc: BaseException,
    *,
    source: str,
    source_index: int | None,
    line_number: int | None,
    reason: str,
) -> MergeError:
    if isinstance(exc, MergeError):
        return exc
    message = f"{type(exc).__name__}: {exc}"
    return _error(
        message,
        source=source,
        source_index=source_index,
        line_number=line_number,
        reason=reason,
        original=exc,
    )


def _safe_close(stream: Any) -> BaseException | None:
    try:
        stream.close()
    except BaseException as exc:  # noqa: BLE001 - reported by the caller
        return exc
    return None


def _close_quietly(iterator: Iterator[_Record]) -> None:
    try:
        iterator.close()  # type: ignore[attr-defined]
    except BaseException:  # noqa: BLE001 - do not mask the original failure
        pass


def _line_fingerprint(line: Line) -> bytes:
    if isinstance(line, bytes):
        data = line
    else:
        data = line.encode("utf-8", "surrogatepass")
    return hashlib.blake2b(data, digest_size=16).digest()


def _keys_equal(left: Any, right: Any) -> bool:
    try:
        return bool(left == right)
    except Exception:  # noqa: BLE001 - custom key types may only be orderable
        try:
            return not (left < right) and not (right < left)
        except Exception:  # noqa: BLE001
            return False


def _source_records(run: _Run, context: _Context) -> Iterator[_Record]:
    assert run.kind == "source"
    assert run.source_index is not None
    mode = "rb" if context.binary else "rt"
    kwargs: dict[str, Any] = {"threads": context.threads}
    if not context.binary:
        kwargs.update(
            encoding=context.encoding,
            errors=context.errors,
            newline=context.newline,
        )
    try:
        stream = xopen(run.path, mode, **kwargs)  # type: ignore[call-overload]
    except BaseException as exc:  # noqa: BLE001
        raise _wrap_read_error(
            exc,
            source=run.path,
            source_index=run.source_index,
            line_number=None,
            reason="open_error",
        ) from exc

    row = 0
    previous_key: Any = _MISSING
    try:
        for line in stream:
            row += 1
            try:
                key = context.key(line)
            except BaseException as exc:  # noqa: BLE001
                raise _wrap_read_error(
                    exc,
                    source=run.path,
                    source_index=run.source_index,
                    line_number=row,
                    reason="key_error",
                ) from exc
            if previous_key is not _MISSING:
                try:
                    out_of_order = key < previous_key
                except BaseException as exc:  # noqa: BLE001
                    raise _wrap_read_error(
                        exc,
                        source=run.path,
                        source_index=run.source_index,
                        line_number=row,
                        reason="key_error",
                    ) from exc
                if out_of_order:
                    raise _error(
                        "input is not sorted: "
                        f"key {_short_repr(key)} follows {_short_repr(previous_key)}",
                        source=run.path,
                        source_index=run.source_index,
                        line_number=row,
                        reason="not_sorted",
                    )
            previous_key = key
            yield _Record(key, run.source_index, row, line)
    except GeneratorExit:
        _safe_close(stream)
        raise
    except MergeError:
        _safe_close(stream)
        raise
    except BaseException as exc:  # noqa: BLE001
        _safe_close(stream)
        raise _wrap_read_error(
            exc,
            source=run.path,
            source_index=run.source_index,
            line_number=row,
            reason="read_error",
        ) from exc
    else:
        close_error = _safe_close(stream)
        if close_error is not None:
            raise _wrap_read_error(
                close_error,
                source=run.path,
                source_index=run.source_index,
                line_number=row,
                reason="close_error",
            ) from close_error


def _temp_records(run: _Run, context: _Context) -> Iterator[_Record]:
    assert run.kind == "temp"
    try:
        stream = open(run.path, "rb")
    except BaseException as exc:  # noqa: BLE001
        raise _wrap_read_error(
            exc,
            source=run.path,
            source_index=None,
            line_number=None,
            reason="temp_open_error",
        ) from exc

    row = 0
    try:
        while True:
            try:
                raw = pickle.load(stream)
            except EOFError:
                break
            except BaseException as exc:  # noqa: BLE001
                raise _wrap_read_error(
                    exc,
                    source=run.path,
                    source_index=None,
                    line_number=row,
                    reason="temp_read_error",
                ) from exc
            row += 1
            source_index, source_row, line = raw
            try:
                key = context.key(line)
            except BaseException as exc:  # noqa: BLE001
                raise _wrap_read_error(
                    exc,
                    source=run.path,
                    source_index=None,
                    line_number=row,
                    reason="key_error",
                ) from exc
            yield _Record(key, source_index, source_row, line)
    except GeneratorExit:
        _safe_close(stream)
        raise
    except MergeError:
        _safe_close(stream)
        raise
    except BaseException as exc:  # noqa: BLE001
        _safe_close(stream)
        raise _wrap_read_error(
            exc,
            source=run.path,
            source_index=None,
            line_number=row,
            reason="temp_read_error",
        ) from exc
    else:
        close_error = _safe_close(stream)
        if close_error is not None:
            raise _wrap_read_error(
                close_error,
                source=run.path,
                source_index=None,
                line_number=row,
                reason="temp_close_error",
            ) from close_error


def _iter_run(run: _Run, context: _Context) -> Iterator[_Record]:
    if run.kind == "source":
        return _source_records(run, context)
    return _temp_records(run, context)


def _run_origin(run: _Run, record: _Record, context: _Context) -> str:
    if run.kind == "source" and run.source_index is not None:
        return run.path
    return context.sources[record.source_index]


def _next_record(iterator: Iterator[_Record]) -> _Record | None:
    try:
        return next(iterator)
    except StopIteration:
        return None


class _TempSink:
    def __init__(self, path: str) -> None:
        self.path = path
        self.stream = open(path, "wb")

    def write(self, record: _Record) -> None:
        pickle.dump(
            (record.source_index, record.row, record.line),
            self.stream,
            protocol=pickle.HIGHEST_PROTOCOL,
        )

    def close(self) -> None:
        self.stream.close()


class _OutputSink:
    def __init__(
        self,
        path: str,
        *,
        binary: bool,
        encoding: str,
        errors: str | None,
        newline: str | None,
        threads: int | None,
        format: str | None,
        compresslevel: int | None,
    ) -> None:
        self.path = path
        mode = "wb" if binary else "wt"
        kwargs: dict[str, Any] = {
            "threads": threads,
            "format": format,
            "compresslevel": compresslevel,
        }
        if not binary:
            kwargs.update(encoding=encoding, errors=errors, newline=newline)
        self.stream = xopen(path, mode, **kwargs)  # type: ignore[call-overload]

    def write(self, record: _Record) -> None:
        self.stream.write(record.line)

    def close(self) -> None:
        self.stream.close()


def _merge_into(
    runs: Sequence[_Run],
    *,
    make_sink: Callable[[], _TempSink | _OutputSink],
    context: _Context,
    collect_stats: bool,
) -> tuple[int, int, int, int, tuple[int, ...], tuple[int, ...]]:
    """
    Merge runs into a sink, returning row and duplicate statistics.

    At most ``len(runs) + 1`` streams are open at any time.  Callers choose
    group sizes so this does not exceed their open-file budget.
    """
    sink = make_sink()
    iterators: list[Iterator[_Record]] = []
    heap: list[tuple[Any, int, int, int, _Record, Iterator[_Record]]] = []
    total_rows = 0
    distinct_keys = 0
    row_counts = [0] * context.source_count
    duplicate_by_source = [0] * context.source_count
    duplicate_line_rows = 0
    seen_lines: set[bytes] = set()
    previous_key: Any = _MISSING
    previous_source_key: list[Any] = [_MISSING] * context.source_count

    try:
        for sequence, run in enumerate(runs):
            iterator = _iter_run(run, context)
            iterators.append(iterator)
            record = _next_record(iterator)
            if record is not None:
                try:
                    heapq.heappush(
                        heap,
                        (
                            record.key,
                            record.source_index,
                            record.row,
                            sequence,
                            record,
                            iterator,
                        ),
                    )
                except BaseException as exc:  # noqa: BLE001
                    raise _wrap_read_error(
                        exc,
                        source=_run_origin(run, record, context),
                        source_index=record.source_index,
                        line_number=record.row,
                        reason="key_error",
                    ) from exc

        while heap:
            try:
                key, source_index, _row, sequence, record, iterator = heapq.heappop(
                    heap
                )
            except BaseException as exc:  # noqa: BLE001
                raise _error(
                    f"{type(exc).__name__}: {exc}",
                    source=None,
                    source_index=None,
                    line_number=None,
                    reason="key_error",
                    original=exc,
                ) from exc

            try:
                sink.write(record)
            except BaseException as exc:  # noqa: BLE001
                raise _wrap_read_error(
                    exc,
                    source=sink.path,
                    source_index=None,
                    line_number=total_rows + 1,
                    reason="output_error",
                ) from exc

            if collect_stats:
                total_rows += 1
                row_counts[source_index] += 1
                if previous_key is _MISSING or not _keys_equal(key, previous_key):
                    distinct_keys += 1
                    seen_lines.clear()
                if previous_source_key[source_index] is not _MISSING and _keys_equal(
                    key, previous_source_key[source_index]
                ):
                    duplicate_by_source[source_index] += 1
                fingerprint = _line_fingerprint(record.line)
                if fingerprint in seen_lines:
                    duplicate_line_rows += 1
                else:
                    seen_lines.add(fingerprint)
                previous_key = key
                previous_source_key[source_index] = key

            following = _next_record(iterator)
            if following is not None:
                try:
                    heapq.heappush(
                        heap,
                        (
                            following.key,
                            following.source_index,
                            following.row,
                            sequence,
                            following,
                            iterator,
                        ),
                    )
                except BaseException as exc:  # noqa: BLE001
                    raise _wrap_read_error(
                        exc,
                        source=_run_origin(run, following, context),
                        source_index=following.source_index,
                        line_number=following.row,
                        reason="key_error",
                    ) from exc
    except BaseException:
        for iterator in iterators:
            _close_quietly(iterator)
        _safe_close(sink)
        raise
    else:
        try:
            sink.close()
        except BaseException as exc:  # noqa: BLE001
            raise _wrap_read_error(
                exc,
                source=sink.path,
                source_index=None,
                line_number=total_rows,
                reason="output_error",
            ) from exc

    duplicate_rows = total_rows - distinct_keys if collect_stats else 0
    return (
        total_rows,
        distinct_keys,
        duplicate_rows,
        duplicate_line_rows if collect_stats else 0,
        tuple(row_counts),
        tuple(duplicate_by_source),
    )


def _merge_in_memory(
    sources: Sequence[str],
    *,
    make_output_sink: Callable[[], _OutputSink],
    context: _Context,
) -> tuple[int, int, int, int, tuple[int, ...], tuple[int, ...]]:
    """
    Merge with only one source open at a time, at the cost of memory.

    This is used when the open-file budget is too small for an external
    two-input merge (which needs an input, a temporary run and an output).
    """
    records: list[_Record] = []
    for index, path in enumerate(sources):
        run = _Run(path, "source", index)
        records.extend(_source_records(run, context))

    records.sort(key=lambda record: (record.key, record.source_index, record.row))
    output_sink = make_output_sink()
    total_rows = 0
    distinct_keys = 0
    row_counts = [0] * len(sources)
    duplicate_by_source = [0] * len(sources)
    duplicate_line_rows = 0
    seen_lines: set[bytes] = set()
    previous_key: Any = _MISSING
    previous_source_key: list[Any] = [_MISSING] * len(sources)

    try:
        for record in records:
            output_sink.write(record)
            total_rows += 1
            row_counts[record.source_index] += 1
            if previous_key is _MISSING or not _keys_equal(record.key, previous_key):
                distinct_keys += 1
                seen_lines.clear()
            if previous_source_key[record.source_index] is not _MISSING and _keys_equal(
                record.key, previous_source_key[record.source_index]
            ):
                duplicate_by_source[record.source_index] += 1
            fingerprint = _line_fingerprint(record.line)
            if fingerprint in seen_lines:
                duplicate_line_rows += 1
            else:
                seen_lines.add(fingerprint)
            previous_key = record.key
            previous_source_key[record.source_index] = record.key
    except BaseException:
        _safe_close(output_sink)
        raise
    else:
        try:
            output_sink.close()
        except BaseException as exc:  # noqa: BLE001
            raise _wrap_read_error(
                exc,
                source=output_sink.path,
                source_index=None,
                line_number=total_rows,
                reason="output_error",
            ) from exc

    duplicate_rows = total_rows - distinct_keys
    return (
        total_rows,
        distinct_keys,
        duplicate_rows,
        duplicate_line_rows,
        tuple(row_counts),
        tuple(duplicate_by_source),
    )


def _chunks(values: Sequence[_Run], size: int) -> Iterator[Sequence[_Run]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _output_temp_path(output: str) -> str:
    directory = os.path.dirname(os.path.abspath(output))
    basename = os.path.basename(output)
    suffix = os.path.splitext(basename)[1]
    descriptor, path = tempfile.mkstemp(
        prefix=f".{basename}.", suffix=suffix, dir=directory
    )
    os.close(descriptor)
    return path


def _stage_json(path: str, payload: dict[str, Any]) -> str:
    directory = os.path.dirname(os.path.abspath(path))
    basename = os.path.basename(path)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{basename}.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return temporary


def _write_json_atomic(path: str, payload: dict[str, Any]) -> None:
    temporary = _stage_json(path, payload)
    try:
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _error_payload(error: BaseException) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": type(error).__name__,
        "message": str(error),
    }
    if isinstance(error, MergeError):
        payload.update(
            source=error.source,
            source_index=error.source_index,
            line_number=error.line_number,
            reason=error.reason,
        )
        if error.original is not None:
            payload["original_type"] = type(error.original).__name__
    return payload


def _manifest_sources(sources: Sequence[str]) -> list[dict[str, Any]]:
    return [
        {"index": index, "path": path} for index, path in enumerate(sources)
    ]


def merge_sorted_files(
    inputs: Iterable[os.PathLike | str | bytes],
    output: os.PathLike | str | bytes,
    *,
    key: KeyFunction | None = None,
    max_open_files: int = 4,
    manifest: os.PathLike | str | bytes | bool | None = None,
    manifest_path: os.PathLike | str | bytes | None = None,
    binary: bool = False,
    encoding: str = "utf-8",
    errors: str | None = None,
    newline: str | None = None,
    threads: int | None = None,
    format: str | None = None,
    compresslevel: int | None = None,
) -> MergeResult:
    """
    Merge sorted inputs into ``output`` while keeping at most
    ``max_open_files`` file streams open.

    Parameters
    ----------
    inputs:
        Iterable of paths.  The iteration order is the source order used to
        break ties between equal keys.  A path may be repeated.
    output:
        Destination path.  Its extension selects the compression format, just
        like :func:`xopen.xopen`.  The destination is replaced atomically only
        after every input has been read and closed successfully.
    key:
        Callable returning the sort key for a line.  It receives ``str`` lines
        by default and ``bytes`` lines when ``binary=True``.  The default is
        the identity function, so records are compared lexicographically.
    max_open_files:
        Maximum number of simultaneously open streams, including temporary
        runs and the output.  Values below 3 (including the minimum of 1) use
        an in-memory merge that keeps only one source open at a time, at the
        cost of memory; the result is identical.
    manifest:
        Optional path for a JSON status file.  It is written atomically.  A
        ``"complete"`` status is written only after all inputs have been read,
        every stream has closed without error, and the output has been
        installed.  On failure a ``"failed"`` manifest with the source and
        line of the error is written instead when possible.  ``True`` selects
        ``<output>.manifest.json``.
    manifest_path:
        Alias for ``manifest``; takes precedence when both are given.
    binary, encoding, errors, newline, threads, format, compresslevel:
        Passed to :func:`xopen.xopen`.  ``format`` and ``compresslevel`` apply
        to the output only.

    Returns
    -------
    MergeResult
        Row counts, duplicate-key counts and the number of merge passes.

    Notes
    -----
    Inputs must be non-decreasing according to ``key``.  Equal keys are
    allowed and are preserved; they are ordered by source position and then by
    line number.  A decrease raises :class:`MergeError` identifying the source
    and line.  Duplicate rows are never removed.
    """
    if isinstance(inputs, (str, bytes, os.PathLike)):
        raise TypeError("inputs must be an iterable of paths, not a single path")
    source_list = [_as_text_path(path) for path in inputs]
    output_path = _as_text_path(output)

    if max_open_files < 1:
        raise ValueError("max_open_files must be at least 1")
    if type(max_open_files) is not int:
        raise TypeError("max_open_files must be an integer")
    manifest_target: str | None
    if manifest_path is not None:
        manifest_target = _as_text_path(manifest_path)
    elif manifest is True:
        manifest_target = output_path + ".manifest.json"
    elif manifest is False or manifest is None:
        manifest_target = None
    else:
        manifest_target = _as_text_path(manifest)

    if manifest_target is not None:
        manifest_absolute = os.path.abspath(manifest_target)
        if manifest_absolute == os.path.abspath(output_path):
            raise ValueError("manifest path must differ from the output path")
        for source in source_list:
            if manifest_absolute == os.path.abspath(source):
                raise ValueError("manifest path must differ from every input path")

    key_function: KeyFunction = key if key is not None else (lambda line: line)
    context = _Context(
        key=key_function,
        binary=binary,
        encoding=encoding,
        errors=errors,
        newline=newline,
        threads=threads,
        source_count=len(source_list),
        sources=tuple(source_list),
    )
    manifest_base = {
        "output": output_path,
        "sources": _manifest_sources(source_list),
        "max_open_files": max_open_files,
    }

    if manifest_target is not None:
        _write_json_atomic(
            manifest_target,
            {
                **manifest_base,
                "status": "running",
                "completed": False,
            },
        )

    output_temporary: str | None = None
    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    try:
        output_temporary = _output_temp_path(output_path)
        def make_output_sink() -> _OutputSink:
            assert output_temporary is not None
            return _OutputSink(
                output_temporary,
                binary=binary,
                encoding=encoding,
                errors=errors,
                newline=newline,
                threads=threads,
                format=format,
                compresslevel=compresslevel,
            )

        if max_open_files < 3:
            (
                total_rows,
                distinct_keys,
                duplicate_rows,
                duplicate_line_rows,
                row_counts,
                duplicate_by_source,
            ) = _merge_in_memory(
                source_list,
                make_output_sink=make_output_sink,
                context=context,
            )
            passes = 1
        else:
            fan_in = max_open_files - 1
            runs = [
                _Run(path, "source", index)
                for index, path in enumerate(source_list)
            ]
            passes = 0
            temporary_directory = tempfile.TemporaryDirectory(
                prefix="xopen-merge-"
            )
            staged_runs: list[_Run] = []
            while len(runs) > fan_in:
                staged_runs = []
                for group in _chunks(runs, fan_in):
                    descriptor, temporary_run = tempfile.mkstemp(
                        prefix="run-", suffix=".pickle", dir=temporary_directory.name
                    )
                    os.close(descriptor)
                    def make_temp_sink(path: str = temporary_run) -> _TempSink:
                        return _TempSink(path)

                    _merge_into(
                        group,
                        make_sink=make_temp_sink,
                        context=context,
                        collect_stats=False,
                    )
                    staged_runs.append(_Run(temporary_run, "temp", None))
                for run in runs:
                    if run.kind == "temp":
                        try:
                            os.unlink(run.path)
                        except OSError:
                            pass
                runs = staged_runs
                passes += 1

            (
                total_rows,
                distinct_keys,
                duplicate_rows,
                duplicate_line_rows,
                row_counts,
                duplicate_by_source,
            ) = _merge_into(
                runs,
                make_sink=make_output_sink,
                context=context,
                collect_stats=True,
            )
            passes += 1

        result = MergeResult(
            output=output_path,
            sources=tuple(source_list),
            row_counts=row_counts,
            total_rows=total_rows,
            distinct_keys=distinct_keys,
            duplicate_key_rows=duplicate_rows,
            duplicate_line_rows=duplicate_line_rows,
            passes=passes,
            manifest_path=manifest_target,
        )

        staged_manifest: str | None = None
        if manifest_target is not None:
            manifest_payload = {
                **manifest_base,
                "status": "complete",
                "completed": True,
                "total_rows": total_rows,
                "distinct_keys": distinct_keys,
                "duplicate_key_rows": duplicate_rows,
                "duplicate_line_rows": duplicate_line_rows,
                "duplicate_rows": duplicate_rows,
                "exact_duplicate_rows": duplicate_line_rows,
                "row_counts": list(row_counts),
                "duplicate_key_rows_by_source": list(duplicate_by_source),
                "passes": passes,
            }
            try:
                staged_manifest = _stage_json(manifest_target, manifest_payload)
            except BaseException as exc:  # noqa: BLE001
                raise _wrap_read_error(
                    exc,
                    source=manifest_target,
                    source_index=None,
                    line_number=None,
                    reason="manifest_error",
                ) from exc

        try:
            os.replace(output_temporary, output_path)
        except BaseException as exc:  # noqa: BLE001
            if staged_manifest is not None:
                try:
                    os.unlink(staged_manifest)
                except OSError:
                    pass
            raise _wrap_read_error(
                exc,
                source=output_path,
                source_index=None,
                line_number=None,
                reason="output_error",
            ) from exc
        output_temporary = None

        if staged_manifest is not None:
            assert manifest_target is not None
            try:
                os.replace(staged_manifest, manifest_target)
            except BaseException as exc:  # noqa: BLE001
                raise _wrap_read_error(
                    exc,
                    source=manifest_target,
                    source_index=None,
                    line_number=None,
                    reason="manifest_error",
                ) from exc

        return result
    except BaseException as exc:
        if manifest_target is not None:
            failure_payload = {
                **manifest_base,
                "status": "failed",
                "completed": False,
                "error": _error_payload(exc),
            }
            try:
                _write_json_atomic(manifest_target, failure_payload)
            except BaseException:  # noqa: BLE001 - preserve the merge error
                pass
        if output_temporary is not None:
            try:
                os.unlink(output_temporary)
            except OSError:
                pass
        raise
    finally:
        if temporary_directory is not None:
            temporary_directory.cleanup()


def merge_sorted(
    inputs: Iterable[os.PathLike | str | bytes],
    output: os.PathLike | str | bytes,
    **kwargs: Any,
) -> MergeResult:
    """Alias for :func:`merge_sorted_files`."""
    return merge_sorted_files(inputs, output, **kwargs)


def merge_files(
    inputs: Iterable[os.PathLike | str | bytes],
    output: os.PathLike | str | bytes,
    **kwargs: Any,
) -> MergeResult:
    """Alias for :func:`merge_sorted_files`."""
    return merge_sorted_files(inputs, output, **kwargs)


def _key_from_column(
    column: int, delimiter: str, binary: bool
) -> KeyFunction:
    if binary:
        delimiter_bytes = delimiter.encode("utf-8")

        def binary_key(line: Line) -> bytes:
            if not isinstance(line, bytes):
                raise TypeError("binary merge expected bytes lines")
            return line.split(delimiter_bytes, column + 1)[column]

        return binary_key

    def text_key(line: Line) -> str:
        if not isinstance(line, str):
            raise TypeError("text merge expected str lines")
        return line.split(delimiter, column + 1)[column]

    return text_key


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="xopen-merge",
        description=(
            "Merge sorted compressed or plain files with a bounded number of "
            "open streams."
        ),
    )
    parser.add_argument("output", help="destination file (extension selects format)")
    parser.add_argument("inputs", nargs="+", help="sorted input files")
    parser.add_argument(
        "--max-open-files",
        type=int,
        default=4,
        help="maximum number of simultaneously open streams (default: 4)",
    )
    parser.add_argument(
        "--key-column",
        type=int,
        default=None,
        metavar="N",
        help="zero-based column used as the sort key (default: whole line)",
    )
    parser.add_argument(
        "--delimiter",
        default="\t",
        help="field delimiter used with --key-column (default: TAB)",
    )
    parser.add_argument(
        "--binary",
        action="store_true",
        help="read and write bytes instead of text",
    )
    parser.add_argument("--encoding", default="utf-8", help="text encoding")
    parser.add_argument(
        "--manifest",
        default=None,
        help="manifest path (default: OUTPUT.manifest.json)",
    )
    parser.add_argument(
        "--no-manifest", action="store_true", help="do not write a manifest"
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=None,
        help="threads passed to xopen (0 disables external processes)",
    )
    parser.add_argument("--format", default=None, help="output format override")
    parser.add_argument(
        "--compresslevel", type=int, default=None, help="output compression level"
    )
    arguments = parser.parse_args(argv)

    key: KeyFunction | None = None
    if arguments.key_column is not None:
        if arguments.key_column < 0:
            parser.error("--key-column must be non-negative")
        key = _key_from_column(
            arguments.key_column, arguments.delimiter, arguments.binary
        )

    manifest: str | None
    if arguments.no_manifest:
        manifest = None
    elif arguments.manifest is not None:
        manifest = arguments.manifest
    else:
        manifest = arguments.output + ".manifest.json"

    try:
        merge_sorted_files(
            arguments.inputs,
            arguments.output,
            key=key,
            max_open_files=arguments.max_open_files,
            manifest=manifest,
            binary=arguments.binary,
            encoding=arguments.encoding,
            threads=arguments.threads,
            format=arguments.format,
            compresslevel=arguments.compresslevel,
        )
    except (MergeError, OSError, ValueError, TypeError) as exc:
        print(f"xopen-merge: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
