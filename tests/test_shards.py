"""
Tests for reassembling logical lines from independently compressed shards.
"""

import bz2
import gzip
import io
import lzma
import random
import sys

import pytest

from xopen import LogicalLine, ShardSpan, iter_logical_lines, read_logical_lines

if sys.version_info >= (3, 14):
    from compression import zstd
else:
    from backports import zstd


COMPRESSORS = {
    "gz": lambda data: gzip.compress(data, mtime=0),
    "bz2": bz2.compress,
    "xz": lambda data: lzma.compress(data, format=lzma.FORMAT_XZ),
    "zst": zstd.compress,
}


def reference_lines(data: bytes) -> list[str]:
    """Universal-newline UTF-8 decoding with surrogateescape, like open()."""
    wrapper = io.TextIOWrapper(
        io.BytesIO(data), encoding="utf-8", errors="surrogateescape"
    )
    return wrapper.readlines()


def compress_shards(chunks: list[bytes], fmt: str) -> list[bytes]:
    if fmt is None:
        return list(chunks)
    return [COMPRESSORS[fmt](chunk) for chunk in chunks]


def make_shards(data: bytes, cuts: list[int]) -> list[bytes]:
    cuts = sorted(set([0, *cuts, len(data)]))
    return [data[left:right] for left, right in zip(cuts, cuts[1:])]


def assert_spans_match_bytes(
    lines: list[LogicalLine], chunks: list[bytes], data: bytes
) -> None:
    assert lines[0].start == 0
    for previous, current in zip(lines, lines[1:]):
        assert current.start == previous.end
    assert lines[-1].end == len(data)
    for line in lines:
        assert line.line_number == lines.index(line) + 1
        covered = b""
        previous_shard = -1
        for span in line.spans:
            assert isinstance(span, ShardSpan)
            assert span.shard > previous_shard
            previous_shard = span.shard
            assert span.start < span.end
            covered += chunks[span.shard][span.start:span.end]
        assert covered == data[line.start:line.end]
        # The spans cover the line's global range exactly, nothing more.
        joined = [
            (span.shard, span.start, span.end) for span in line.spans
        ]
        assert joined == sorted(joined)


def test_equivalent_to_concatenated_stream():
    rng = random.Random(1234)
    tokens = [
        b"a",
        b"z",
        b" ",
        "中文".encode(),
        "€".encode(),
        b"\xff",
        b"\x80",
        b"\xe2",
        b"\r",
        b"\n",
        b"\r\n",
    ]
    for fmt in [*COMPRESSORS, None]:
        for _ in range(20):
            data = b"".join(rng.choice(tokens) for _ in range(rng.randint(1, 200)))
            cut_count = rng.randint(0, len(data) + 2)
            cuts = [rng.randint(0, len(data)) for _ in range(cut_count)]
            chunks = make_shards(data, cuts)
            payloads = compress_shards(chunks, fmt)
            lines = read_logical_lines(
                [io.BytesIO(payload) for payload in payloads],
                format=fmt,
            )
            assert [line.text for line in lines] == reference_lines(data)
            assert_spans_match_bytes(lines, chunks, data)


@pytest.mark.parametrize("fmt", [*COMPRESSORS, None])
def test_multibyte_character_split_across_shards(fmt):
    data = "a€b\n".encode()
    chunks = make_shards(data, [2])  # Cut through the euro sign (e2|82 ac)
    assert chunks == [b"a\xe2", b"\x82\xacb\n"]
    lines = read_logical_lines(
        [io.BytesIO(payload) for payload in compress_shards(chunks, fmt)],
        format=fmt,
    )
    assert len(lines) == 1
    line = lines[0]
    assert line.text == "a€b\n"
    assert line.spans == (ShardSpan(0, 0, 2), ShardSpan(1, 0, 4))


@pytest.mark.parametrize("fmt", [*COMPRESSORS, None])
def test_crlf_split_across_shards(fmt):
    data = b"a\r\nb\n"
    chunks = make_shards(data, [2])  # b"a\r" | b"\nb\n"
    lines = read_logical_lines(
        [io.BytesIO(payload) for payload in compress_shards(chunks, fmt)],
        format=fmt,
    )
    assert [line.text for line in lines] == ["a\n", "b\n"]
    # Both CRLF bytes belong to the first line, one on each side of the cut.
    assert lines[0].end == 3
    assert lines[0].spans == (ShardSpan(0, 0, 2), ShardSpan(1, 0, 1))
    assert lines[1].start == 3
    assert lines[1].spans == (ShardSpan(1, 1, 3),)


def test_lone_carriage_return_is_translated():
    lines = read_logical_lines([io.BytesIO(b"a\rb")])
    assert [line.text for line in lines] == ["a\n", "b"]
    assert lines[1].end == 3
    assert not lines[1].text.endswith("\n")


def test_trailing_carriage_return_emits_line():
    lines = read_logical_lines([io.BytesIO(b"tail\r")])
    assert [line.text for line in lines] == ["tail\n"]
    assert lines[0].spans == (ShardSpan(0, 0, 5),)


def test_carriage_return_split_from_non_newline_across_shards():
    # Shard 0 ends with CR; shard 1 does not start with LF, so it is a lone
    # CR line ending and the first line must still cover the CR byte.
    chunks = [b"a\r", b"b"]
    payloads = [gzip.compress(chunk, mtime=0) for chunk in chunks]
    lines = read_logical_lines([io.BytesIO(p) for p in payloads], format="gz")
    assert [line.text for line in lines] == ["a\n", "b"]
    assert lines[0].end == 2
    assert lines[0].spans == (ShardSpan(0, 0, 2),)
    assert lines[1].spans == (ShardSpan(1, 0, 1),)


def test_no_trailing_newline():
    lines = read_logical_lines([io.BytesIO(b"only one line")])
    assert len(lines) == 1
    assert lines[0].line_number == 1
    assert lines[0].text == "only one line"
    assert (lines[0].start, lines[0].end) == (0, 13)


def test_empty_shards_are_skipped_but_keep_manifest_position():
    chunks = [b"", b"line one\n", b"", b"line two\n", b""]
    # Mix truly empty payloads with valid archives of empty content.
    payloads = [
        b"",
        gzip.compress(chunks[1], mtime=0),
        b"",
        gzip.compress(chunks[3], mtime=0),
        b"",
    ]
    lines = read_logical_lines([io.BytesIO(p) for p in payloads], format="gz")
    assert [line.text for line in lines] == ["line one\n", "line two\n"]
    assert lines[0].spans == (ShardSpan(1, 0, 9),)
    assert lines[1].spans == (ShardSpan(3, 0, 9),)


def test_bad_byte_attributed_only_to_owning_shard():
    # 0xff sits at the end of shard 0; it must not leak into shard 1.
    chunks = [b"ok\xff", b"rest\n"]
    payloads = [gzip.compress(chunk, mtime=0) for chunk in chunks]
    lines = read_logical_lines([io.BytesIO(p) for p in payloads], format="gz")
    assert len(lines) == 1
    line = lines[0]
    assert line.text == "ok\udcffrest\n"
    assert line.spans == (ShardSpan(0, 0, 3), ShardSpan(1, 0, 5))


def test_truncated_lead_byte_not_blamed_on_next_shard():
    # Shard 0 ends with a dangling lead byte; shard 1 starts with an ASCII
    # byte, so the lead byte is invalid and belongs to shard 0 alone.
    chunks = [b"a\xe2", b"Xb\n"]
    payloads = [gzip.compress(chunk, mtime=0) for chunk in chunks]
    lines = read_logical_lines([io.BytesIO(p) for p in payloads], format="gz")
    assert [line.text for line in lines] == ["a\udce2Xb\n"]
    assert lines[0].spans == (ShardSpan(0, 0, 2), ShardSpan(1, 0, 3))


def test_paths_and_filelike_sources(tmp_path):
    data = "第一行\nsecond line\n".encode()
    chunks = make_shards(data, [7])
    path = tmp_path / "shard-0.gz"
    path.write_bytes(gzip.compress(chunks[0], mtime=0))
    sources = [path, io.BytesIO(gzip.compress(chunks[1], mtime=0))]
    lines = read_logical_lines(sources, format="gz")
    assert [line.text for line in lines] == reference_lines(data)
    assert_spans_match_bytes(lines, chunks, data)


def test_iter_and_read_helpers_agree():
    payloads = [gzip.compress(b"a\n", mtime=0), gzip.compress(b"b", mtime=0)]
    sources = [io.BytesIO(p) for p in payloads]
    streamed = list(iter_logical_lines(sources, format="gz"))
    assert streamed == read_logical_lines(
        [io.BytesIO(p) for p in payloads], format="gz"
    )
    assert [line.line_number for line in streamed] == [1, 2]


def test_surrogateescape_roundtrip_through_spans():
    data = b"\xff\x80 good\n"
    lines = read_logical_lines([io.BytesIO(data)])
    line = lines[0]
    assert line.text.encode("utf-8", errors="surrogateescape") == data
    assert line.spans == (ShardSpan(0, 0, len(data)),)


def test_empty_stream_yields_no_lines():
    assert read_logical_lines([]) == []
    assert read_logical_lines(
        [io.BytesIO(b""), io.BytesIO(b""), io.BytesIO(gzip.compress(b""))],
        format="gz",
    ) == []


def test_shard_consisting_of_newlines():
    lines = read_logical_lines([io.BytesIO(b"\n\n")])
    assert [line.text for line in lines] == ["\n", "\n"]
    assert [(line.start, line.end) for line in lines] == [(0, 1), (1, 2)]


def test_non_bytes_filelike_is_rejected():
    class TextSource:
        def read(self):
            return "not bytes"

    with pytest.raises(TypeError):
        read_logical_lines([TextSource()])
