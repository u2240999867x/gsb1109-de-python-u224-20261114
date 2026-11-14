"""
Tests for memory-bounded line reconciliation across compressed files.
"""

import bz2
import glob
import os
import random
import sys
from collections import Counter

import pytest

from xopen import xopen
from xopen.reconcile import (
    DEFAULT_MAX_OCCURRENCES,
    LineDifference,
    ReconcileError,
    iter_line_differences,
    reconcile_lines,
)

if sys.version_info >= (3, 14):
    from compression import zstd
else:
    from backports import zstd


TINY_BUDGET = 2 * 1024 * 1024


def write_compressed(path, lines):
    with xopen(path, "wb", threads=0) as f:
        f.write(b"".join(lines))


def write_empty(path, extension):
    if extension == ".zst":
        data = zstd.compress(b"")
    elif extension == ".bz2":
        data = bz2.compress(b"")
    else:
        raise AssertionError
    with open(path, "wb") as f:
        f.write(data)


def truncate_archive(path):
    """Cut the tail off an archive so its end-of-stream marker is missing."""
    size = os.stat(path).st_size
    with open(path, "r+b") as f:
        f.truncate(max(1, size // 2))


@pytest.fixture
def bz2_zst_pair(tmp_path):
    def _create(left_lines, right_lines):
        left_path = tmp_path / "left.txt.bz2"
        right_path = tmp_path / "right.txt.zst"
        write_compressed(left_path, left_lines)
        write_compressed(right_path, right_lines)
        return left_path, right_path

    return _create


def test_identical_content_different_formats_and_order(bz2_zst_pair):
    lines = [f"record {i}\n".encode() for i in range(500)]
    shuffled = list(lines)
    random.Random(1).shuffle(shuffled)
    left_path, right_path = bz2_zst_pair(lines, shuffled)
    summary = reconcile_lines(left_path, right_path)
    assert summary.identical
    assert summary.left_line_total == summary.right_line_total == 500
    assert summary.left_missing_total == summary.right_missing_total == 0
    assert summary.differences == ()


def test_multiplicity_is_counted_exactly(bz2_zst_pair):
    left_lines = [b"a\n", b"a\n", b"b\n", b"c\n"]
    right_lines = [b"a\n", b"b\n", b"b\n", b"d\n"]
    left_path, right_path = bz2_zst_pair(left_lines, right_lines)
    summary = reconcile_lines(left_path, right_path)
    assert not summary.identical
    assert summary.left_line_total == 4
    assert summary.right_line_total == 4
    # a: one extra copy on the left; b: one extra on the right;
    # c exists only on the left, d only on the right.
    assert summary.right_missing_total == 2
    assert summary.left_missing_total == 2
    by_line = {d.line: d for d in summary.differences}
    assert by_line[b"a\n"].left_count == 2
    assert by_line[b"a\n"].right_count == 1
    assert by_line[b"a\n"].right_missing == 1
    assert by_line[b"b\n"].left_missing == 1
    assert by_line[b"c\n"].left_count == 1
    assert by_line[b"c\n"].right_count == 0
    assert by_line[b"d\n"].left_count == 0
    assert by_line[b"d\n"].right_count == 1
    assert all(isinstance(d, LineDifference) for d in summary.differences)


def test_raw_byte_identity_is_preserved(bz2_zst_pair):
    # Bytes that are invalid UTF-8 must still be compared without collapsing.
    lines = [b"\xff\xfe\x00\n", b"tab\tdata\n", b"\xff\xfe\x00\n"]
    left_path, right_path = bz2_zst_pair(lines, [b"\xff\xfe\x00\n", b"other\n"])
    summary = reconcile_lines(left_path, right_path)
    by_line = {d.line: d for d in summary.differences}
    assert by_line[b"\xff\xfe\x00\n"].left_count == 2
    assert by_line[b"\xff\xfe\x00\n"].right_count == 1


def test_newline_equivalence_is_callers_choice(bz2_zst_pair):
    left_lines = [b"line\r\n", b"line\r\n", b"other\n"]
    right_lines = [b"line\n", b"line\n", b"other\n"]
    left_path, right_path = bz2_zst_pair(left_lines, right_lines)

    strict = reconcile_lines(left_path, right_path, newline_equivalent=False)
    strict_lines = {d.line for d in strict.differences}
    assert b"line\r\n" in strict_lines
    assert b"line\n" in strict_lines

    equivalent = reconcile_lines(left_path, right_path, newline_equivalent=True)
    assert equivalent.identical


def test_occurrence_positions_are_reviewable(bz2_zst_pair):
    left_lines = [b"x\n", b"target\n", b"y\n", b"target\n", b"target\n"]
    right_lines = [b"target\n", b"z\n"]
    left_path, right_path = bz2_zst_pair(left_lines, right_lines)
    summary = reconcile_lines(left_path, right_path)
    target = next(d for d in summary.differences if d.line == b"target\n")
    # Offsets: x\n=0..1, target\n=2..8, y\n=9..10, target\n=11..17, ...
    assert [(o.line_number, o.byte_offset) for o in target.left_occurrences] == [
        (2, 2),
        (4, 11),
        (5, 18),
    ]
    assert target.right_occurrences[0].line_number == 1
    assert target.right_occurrences[0].byte_offset == 0


def test_occurrence_cap_keeps_counts_exact(bz2_zst_pair):
    frequent = b"frequent\n"
    left_lines = [frequent] * (DEFAULT_MAX_OCCURRENCES + 25)
    right_lines = [frequent] * 3
    left_path, right_path = bz2_zst_pair(left_lines, right_lines)
    summary = reconcile_lines(left_path, right_path, max_occurrences=10)
    diff = summary.differences[0]
    assert diff.left_count == DEFAULT_MAX_OCCURRENCES + 25
    assert diff.right_count == 3
    assert len(diff.left_occurrences) == 10
    assert diff.left_occurrences_truncated
    assert not diff.right_occurrences_truncated
    assert diff.right_missing == DEFAULT_MAX_OCCURRENCES + 22


def test_final_line_without_newline(bz2_zst_pair):
    left_path, right_path = bz2_zst_pair([b"a\n", b"dangling"], [b"a\n", b"dangling"])
    assert reconcile_lines(left_path, right_path).identical
    # The extra line is inserted before the final newline-less line so that
    # the dangling line keeps the same byte identity on both sides.
    left_path, right_path = bz2_zst_pair(
        [b"a\n", b"dangling"], [b"a\n", b"extra\n", b"dangling"]
    )
    summary = reconcile_lines(left_path, right_path)
    assert [d.line for d in summary.differences] == [b"extra\n"]


def test_empty_inputs(tmp_path, bz2_zst_pair):
    left_path = tmp_path / "empty.bz2"
    right_path = tmp_path / "empty.zst"
    write_empty(left_path, ".bz2")
    write_empty(right_path, ".zst")
    summary = reconcile_lines(left_path, right_path)
    assert summary.identical
    assert summary.left_line_total == summary.right_line_total == 0


def test_spilling_matches_in_memory_multiset(tmp_path):
    rng = random.Random(42)
    pool = [f"record-{i:05d}-{'x' * 32}\n".encode() for i in range(2000)]
    left = [rng.choice(pool) for _ in range(80000)]
    right = list(left)
    rng.shuffle(right)
    right.extend([pool[11]] * 4)
    del right[0:7]
    left_path = tmp_path / "large.bz2"
    right_path = tmp_path / "large.zst"
    write_compressed(left_path, left)
    write_compressed(right_path, right)

    summary = reconcile_lines(
        left_path, right_path, memory_budget=TINY_BUDGET, tmp_dir=tmp_path
    )
    left_counter, right_counter = Counter(left), Counter(right)
    expected = {
        key: (left_counter[key], right_counter[key])
        for key in set(left_counter) | set(right_counter)
        if left_counter[key] != right_counter[key]
    }
    got = {d.line: (d.left_count, d.right_count) for d in summary.differences}
    assert got == expected
    assert summary.left_missing_total == sum(
        max(0, right_counter[k] - left_counter[k]) for k in expected
    )
    assert summary.right_missing_total == sum(
        max(0, left_counter[k] - right_counter[k]) for k in expected
    )
    # All spill files and work directories are cleaned up.
    assert glob.glob(str(tmp_path / "xreconcile-*")) == []
    assert glob.glob(str(tmp_path / "**" / "*.run"), recursive=True) == []


def test_truncated_zstd_raises_and_reports_nothing(tmp_path, bz2_zst_pair):
    left_path, good_right = bz2_zst_pair([b"a\n"], [b"a\n"])
    truncated = tmp_path / "truncated.zst"
    write_compressed(truncated, [f"line-{i}\n".encode() for i in range(5000)])
    truncate_archive(truncated)

    iterator = iter_line_differences(left_path, truncated, tmp_dir=tmp_path)
    with pytest.raises(ReconcileError) as exc_info:
        next(iterator)
    assert exc_info.value.side == "right"
    assert isinstance(exc_info.value.original, EOFError)
    assert glob.glob(str(tmp_path / "xreconcile-*")) == []


def test_truncated_bz2_raises(tmp_path):
    left_path = tmp_path / "truncated.bz2"
    write_compressed(
        left_path, [f"line-{i}\n".encode() for i in range(5000)]
    )
    truncate_archive(left_path)
    right_path = tmp_path / "good.zst"
    write_compressed(right_path, [b"line\n"])
    with pytest.raises(ReconcileError) as exc_info:
        reconcile_lines(left_path, right_path)
    assert exc_info.value.side == "left"


def test_iterator_cleans_up_on_early_close(tmp_path, bz2_zst_pair):
    left_path, right_path = bz2_zst_pair([b"a\n"] * 100, [b"b\n"] * 100)
    iterator = iter_line_differences(
        left_path, right_path, memory_budget=TINY_BUDGET, tmp_dir=tmp_path
    )
    next(iterator)
    iterator.close()
    assert glob.glob(str(tmp_path / "xreconcile-*")) == []


def test_invalid_options(bz2_zst_pair):
    left_path, right_path = bz2_zst_pair([b"a\n"], [b"a\n"])
    with pytest.raises(ValueError):
        reconcile_lines(left_path, right_path, memory_budget=1024)
    with pytest.raises(ValueError):
        reconcile_lines(left_path, right_path, max_occurrences=0)
    with pytest.raises(ValueError):
        reconcile_lines(left_path, right_path, max_occurrences=70000)
    with pytest.raises(TypeError):
        reconcile_lines(left_path, right_path, memory_budget="2 MiB")


def test_spilling_actually_occurs(tmp_path):
    from xopen.reconcile import _SpillBuilder

    path = tmp_path / "spilled.bz2"
    lines = [f"record-{i:05d}\n".encode() for i in range(20000)]
    write_compressed(path, lines)
    builder = _SpillBuilder(
        "left",
        path,
        newline_equivalent=False,
        memory_budget=2 * 1024 * 1024,
        max_occurrences=10,
        work_dir=str(tmp_path),
    )
    builder.build()
    try:
        assert builder._run_paths  # at least one spill run was written
        records = dict((key, count) for key, count, _, _ in builder.sorted_records())
        assert records == Counter(lines)
        assert builder.line_total == len(lines)
    finally:
        builder.cleanup()


def test_tiered_consolidation_with_tiny_fanout(tmp_path, monkeypatch):
    import xopen.reconcile

    # Force multi-tier merging so the bounded-descriptor path is exercised.
    monkeypatch.setattr(xopen.reconcile, "_default_merge_fanout", lambda: 4)
    rng = random.Random(99)
    pool = [f"record-{i:05d}\n".encode() for i in range(1000)]
    left = [rng.choice(pool) for _ in range(40000)]
    right = list(left)
    rng.shuffle(right)
    left_path = tmp_path / "tiered.bz2"
    right_path = tmp_path / "tiered.zst"
    write_compressed(left_path, left)
    write_compressed(right_path, right)
    summary = reconcile_lines(
        left_path, right_path, memory_budget=2 * 1024 * 1024, tmp_dir=tmp_path
    )
    assert summary.identical
    assert glob.glob(str(tmp_path / "**" / "*.run"), recursive=True) == []


def test_carriage_return_inside_line_keeps_identity(bz2_zst_pair):
    left_path, right_path = bz2_zst_pair(
        [b"a\rb\n", b"end\r\n"], [b"ab\n", b"end\n"]
    )
    # Only the trailing CRLF is normalized; the CR embedded in "a\rb" stays.
    summary = reconcile_lines(left_path, right_path, newline_equivalent=True)
    differing = {d.line for d in summary.differences}
    assert b"a\rb\n" in differing
    assert b"ab\n" in differing
    assert b"end\r\n" not in differing
    assert b"end\n" not in differing


def test_open_binary_streams_are_accepted(bz2_zst_pair):
    left_path, right_path = bz2_zst_pair([b"a\n", b"b\n"], [b"a\n", b"c\n"])
    with open(left_path, "rb") as left_stream, open(right_path, "rb") as right_stream:
        summary = reconcile_lines(left_stream, right_stream)
    assert summary.left_path.endswith("left.txt.bz2")
    assert [d.line for d in summary.differences] == [b"b\n", b"c\n"]
