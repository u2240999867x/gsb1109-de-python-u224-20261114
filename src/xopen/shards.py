"""
Reassemble logical lines from independently compressed byte shards.

A supplier has split the original UTF-8 encoded byte stream into shards and
compressed each shard on its own. The cut points do not respect any
boundaries: a multibyte character, a CRLF sequence or a single byte may be
split across two adjacent shards. Shards may also be empty (zero bytes) and
the stream does not have to end with a newline.

The functions in this module decompress the shards in manifest order and
yield the logical lines of the reconstructed stream. Every yielded line
reports which shards contributed bytes to it and the byte offsets (relative
to each shard's decompressed content) that it occupies.

The text of the lines and their numbering are identical to what you get when
you concatenate the decompressed shard bytes in order and read the result
with :func:`open` in text mode with universal newlines. Invalid bytes are
neither skipped nor duplicated: with the default ``errors="surrogateescape"``
each invalid byte becomes one surrogate character (one decoded character per
source byte) and is attributed exactly to the shard span that physically
contains it.
"""

from __future__ import annotations

__all__ = [
    "LogicalLine",
    "ShardSpan",
    "iter_logical_lines",
    "read_logical_lines",
]

import codecs
import dataclasses
import io
import os
from typing import IO, Iterator, Sequence

from xopen import xopen

ShardSource = str | bytes | os.PathLike[str] | os.PathLike[bytes] | IO[bytes]


@dataclasses.dataclass(frozen=True)
class ShardSpan:
    """
    A half-open byte range ``[start, end)`` inside one decompressed shard.

    ``shard`` is the zero-based position of the shard in the manifest.
    Empty shards never produce a span because they contain no bytes.
    """

    shard: int
    start: int
    end: int


@dataclasses.dataclass(frozen=True)
class LogicalLine:
    """
    One logical line of the reconstructed byte stream.

    ``line_number`` is one-based. ``text`` keeps the translated line ending
    (``"\\n"``); the final line of a stream without a trailing newline has
    none. ``start`` and ``end`` are the half-open global byte offsets of the
    line in the concatenation of all decompressed shards. ``spans`` are the
    per-shard byte ranges that together cover exactly ``[start, end)``.
    """

    line_number: int
    text: str
    start: int
    end: int
    spans: tuple[ShardSpan, ...]


def _read_compressed_shard(source: ShardSource) -> bytes:
    if isinstance(source, (str, bytes)) or hasattr(source, "__fspath__"):
        with open(os.fspath(source), "rb") as f:
            return f.read()
    data = source.read()
    if not isinstance(data, bytes):
        raise TypeError(
            f"Shard source must yield bytes, got {data.__class__.__name__}"
        )
    return data


def _decompress_shard(raw: bytes, format: str | None) -> bytes:
    # An empty (zero-byte) shard is valid and simply contributes no bytes.
    if not raw:
        return b""
    with xopen(io.BytesIO(raw), "rb", threads=0, format=format) as f:
        return f.read()


def iter_logical_lines(
    shards: Sequence[ShardSource] | Iterator[ShardSource],
    *,
    encoding: str = "utf-8",
    errors: str = "surrogateescape",
    format: str | None = None,
) -> Iterator[LogicalLine]:
    """
    Yield the logical lines of independently compressed byte shards.

    ``shards`` are processed in manifest order; each entry may be a path
    (str, bytes or os.PathLike, opened and read in binary mode) or a binary
    file-like object and is decompressed on its own via :func:`xopen`. A
    shard whose compressed payload is empty, or that decompresses to empty
    bytes, contributes nothing but still occupies its manifest position.

    Line splitting uses universal newline translation (``"\\r\\n"``, lone
    ``"\\r"`` and ``"\\n"`` all become ``"\\n"``), exactly like text-mode
    :func:`open` with ``newline=None``. With the default
    ``errors="surrogateescape"``, invalid UTF-8 bytes survive round-tripping
    and each one is charged only to the shard span that physically contains
    it. Exact per-shard offset attribution relies on one decoded character
    representing a fixed number of source bytes, so use a byte-preserving
    error handler (the default); other handlers still produce the same line
    text as :func:`open`, but the reported offsets are not meaningful for the
    affected characters.
    """
    decoder = codecs.getincrementaldecoder(encoding)(errors=errors)

    # Global half-open byte range of every decompressed shard, in order.
    shard_ranges: list[tuple[int, int, int]] = []
    # First shard range not fully before the current line; ranges are sorted
    # and lines are emitted in order, so this cursor only moves forward.
    range_cursor = 0
    global_position = 0
    # Number of source bytes represented by the characters emitted so far.
    # Incomplete multibyte sequences buffered inside the decoder are not
    # counted yet, so this is also the global offset of the next character.
    decoded_position = 0
    line_start = 0
    parts: list[str] = []
    line_number = 0
    carriage_return_pending = False

    def spans_for(start: int, end: int) -> tuple[ShardSpan, ...]:
        nonlocal range_cursor
        while (
            range_cursor < len(shard_ranges)
            and shard_ranges[range_cursor][1] <= start
        ):
            range_cursor += 1
        spans: list[ShardSpan] = []
        index = range_cursor
        while index < len(shard_ranges):
            shard_start, shard_end, shard_index = shard_ranges[index]
            if shard_start >= end:
                break
            local_start = max(start, shard_start) - shard_start
            local_end = min(end, shard_end) - shard_start
            if local_start < local_end:
                spans.append(ShardSpan(shard_index, local_start, local_end))
            index += 1
        return tuple(spans)

    def make_line(end: int) -> LogicalLine:
        nonlocal line_number
        line_number += 1
        return LogicalLine(
            line_number=line_number,
            text="".join(parts),
            start=line_start,
            end=end,
            spans=spans_for(line_start, end),
        )

    def feed(text: str) -> Iterator[LogicalLine]:
        nonlocal decoded_position, line_start, parts, carriage_return_pending

        def close_line() -> Iterator[LogicalLine]:
            nonlocal line_start, parts
            yield make_line(decoded_position)
            line_start = decoded_position
            parts = []

        for char in text:
            if carriage_return_pending:
                # The previous character was "\r". Close that line first;
                # a following "\n" completes CRLF and is part of the line.
                if char == "\n":
                    decoded_position += 1
                    yield from close_line()
                    carriage_return_pending = False
                    continue
                yield from close_line()
                carriage_return_pending = False
                # Fall through so the current character is processed normally.
            if char == "\r" or char == "\n":
                parts.append("\n")
                decoded_position += 1
                if char == "\n":
                    yield from close_line()
                else:
                    # Wait for the next character to distinguish CR from CRLF.
                    carriage_return_pending = True
            else:
                parts.append(char)
                decoded_position += len(
                    char.encode(encoding, errors=errors)
                )

    for shard_index, source in enumerate(shards):
        raw = _read_compressed_shard(source)
        decompressed = _decompress_shard(raw, format)
        shard_ranges.append(
            (global_position, global_position + len(decompressed), shard_index)
        )
        global_position += len(decompressed)
        yield from feed(decoder.decode(decompressed, final=False))

    yield from feed(decoder.decode(b"", final=True))

    if carriage_return_pending:
        # A trailing lone CR is itself a line ending.
        yield make_line(decoded_position)
        line_start = decoded_position
        parts = []
    if parts:
        # A final line without a trailing newline.
        yield make_line(decoded_position)


def read_logical_lines(
    shards: Sequence[ShardSource] | Iterator[ShardSource],
    *,
    encoding: str = "utf-8",
    errors: str = "surrogateescape",
    format: str | None = None,
) -> list[LogicalLine]:
    """
    Eagerly collect all :class:`LogicalLine` objects from
    :func:`iter_logical_lines`.
    """
    return list(
        iter_logical_lines(
            shards, encoding=encoding, errors=errors, format=format
        )
    )
