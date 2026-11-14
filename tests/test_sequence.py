"""Tests for the two-volume wrapping-counter alignment machinery."""

import pytest

from xopen import xopen
from xopen.sequence import (
    MISSING,
    AnchorMismatch,
    Event,
    Observation,
    SequenceReconstructor,
    align_events,
)


class TestSequenceReconstructor:
    def test_locked_stream_emits_immediately(self):
        reconstructor = SequenceReconstructor(max_gap=2)
        rows = []
        rows += reconstructor.feed(Observation(10, anchor=10))
        rows += reconstructor.feed(Observation(11, "a"))
        rows += reconstructor.feed(Observation(12, "b"))
        assert [(row.seq, row.value) for row in rows] == [
            (10, None),
            (11, "a"),
            (12, "b"),
        ]
        assert reconstructor.buffered == 0

    def test_wrapping_counter_continues_absolute_sequence(self):
        reconstructor = SequenceReconstructor(max_gap=1)
        reconstructor.feed(Observation(254, anchor=254))
        reconstructor.feed(Observation(255))
        row = reconstructor.feed(Observation(0))[0]
        assert row.seq == 256
        row = reconstructor.feed(Observation(1))[0]
        assert row.seq == 257

    def test_missing_rows_are_skipped_within_budget(self):
        reconstructor = SequenceReconstructor(max_gap=3)
        reconstructor.feed(Observation(0, anchor=0))
        # Jump of 3 means two missing rows (1 and 2).
        row = reconstructor.feed(Observation(3))[0]
        assert row.seq == 3

    def test_missing_rows_beyond_budget_break_the_frame(self):
        reconstructor = SequenceReconstructor(max_gap=1)
        reconstructor.feed(Observation(0, anchor=0))
        reconstructor.feed(Observation(1))
        # Rows 0 and 1 were already final; the unexplainable gap opens a new
        # frame that cannot be aligned until the next anchor.
        released = reconstructor.feed(Observation(4))  # jump 3 > max_gap + 1
        assert released == []
        anchor_rows = reconstructor.feed(Observation(5, anchor=261))
        # The anchor reaches the new frame within budget, so it resolves the
        # buffered counter-4 row uniquely before locking the stream again.
        assert [(row.seq, row.status) for row in anchor_rows] == [
            (260, "ok"),
            (261, "ok"),
        ]

    def test_duplicate_counter_is_a_retransmission_not_an_advance(self):
        reconstructor = SequenceReconstructor(max_gap=1)
        reconstructor.feed(Observation(255, anchor=255))
        duplicate = reconstructor.feed(Observation(255))[0]
        assert duplicate.seq == 255
        wrapped = reconstructor.feed(Observation(0))[0]
        assert wrapped.seq == 256
        again = reconstructor.feed(Observation(0))[0]
        assert again.seq == 256

    def test_unanchored_rows_resolve_at_first_anchor(self):
        reconstructor = SequenceReconstructor(max_gap=1)
        assert reconstructor.feed(Observation(5, "a")) == []
        assert reconstructor.feed(Observation(6, "b")) == []
        assert reconstructor.buffered == 2
        rows = reconstructor.feed(Observation(7, "c", anchor=263))
        assert [(row.seq, row.value, row.status) for row in rows] == [
            (261, "a", "ok"),
            (262, "b", "ok"),
            (263, "c", "ok"),
        ]

    def test_unanchored_frame_with_missing_rows_resolves_at_anchor(self):
        reconstructor = SequenceReconstructor(max_gap=2)
        reconstructor.feed(Observation(0))  # relative 0
        reconstructor.feed(Observation(2))  # one missing, relative 2
        rows = reconstructor.feed(Observation(5, anchor=5))  # relative 5
        assert [(row.seq, row.counter) for row in rows] == [
            (0, 0),
            (2, 2),
            (5, 5),
        ]

    def test_frame_spanning_a_full_cycle_is_ambiguous_even_at_anchor(self):
        reconstructor = SequenceReconstructor(max_gap=0)
        for counter in range(256):
            assert reconstructor.feed(Observation(counter)) == []
        # The next full step pushes the walked distance past one cycle: the
        # first frame is released immediately for review and a fresh frame
        # starts at this row.
        released = reconstructor.feed(Observation(0))
        assert len(released) == 256
        assert all(row.status == "ambiguous" for row in released)
        assert reconstructor.buffered == 1
        # A later anchor uniquely resolves the fresh frame.
        rows = reconstructor.feed(Observation(1, anchor=513))
        assert [(row.seq, row.status) for row in rows] == [
            (512, "ok"),
            (513, "ok"),
        ]

    def test_ambiguous_flush_is_bounded_to_one_counter_cycle(self):
        reconstructor = SequenceReconstructor(max_gap=0)
        for counter in range(2000):
            reconstructor.feed(Observation(counter % 256))
        # Only the current frame (less than one cycle) may be retained.
        assert reconstructor.buffered < 256

    def test_retransmission_inside_unanchored_frame_resolves_at_anchor(self):
        reconstructor = SequenceReconstructor(max_gap=1)
        reconstructor.feed(Observation(9, "a"))
        reconstructor.feed(Observation(9, "a-dup"))
        reconstructor.feed(Observation(10, "b"))
        rows = reconstructor.feed(Observation(11, "c", anchor=267))
        assert [(row.seq, row.value) for row in rows] == [
            (265, "a"),
            (265, "a-dup"),
            (266, "b"),
            (267, "c"),
        ]

    def test_gap_beyond_budget_in_locked_stream_recovers_at_next_anchor(self):
        reconstructor = SequenceReconstructor(max_gap=1)
        reconstructor.feed(Observation(0, anchor=0))
        reconstructor.feed(Observation(1, "a"))
        # Jump of 4 cannot be explained: the stream goes floating until an anchor.
        assert reconstructor.feed(Observation(5, "b")) == []
        rows = reconstructor.feed(Observation(6, "c", anchor=262))
        assert [(row.seq, row.value, row.status) for row in rows] == [
            (261, "b", "ok"),
            (262, "c", "ok"),
        ]

    def test_anchor_beyond_budget_resyncs_and_flags_prior_frame(self):
        reconstructor = SequenceReconstructor(max_gap=1)
        reconstructor.feed(Observation(4, "a"))
        reconstructor.feed(Observation(5, "b"))
        # The anchor is three jumps away from counter 5, beyond max_gap: it
        # cannot resolve the buffered frame, which goes to review, while the
        # anchor row itself starts a fresh locked stream.
        rows = reconstructor.feed(Observation(8, "c", anchor=264))
        assert [(row.value, row.status) for row in rows] == [
            ("a", "ambiguous"),
            ("b", "ambiguous"),
            ("c", "ok"),
        ]
        assert rows[-1].seq == 264
        follow_up = reconstructor.feed(Observation(9, "d"))[0]
        assert (follow_up.seq, follow_up.value, follow_up.status) == (
            265,
            "d",
            "ok",
        )

    def test_unanchored_tail_is_flagged_at_finish(self):
        reconstructor = SequenceReconstructor(max_gap=1)
        reconstructor.feed(Observation(3, "a"))
        reconstructor.feed(Observation(4, "b"))
        rows = reconstructor.finish()
        assert [row.status for row in rows] == ["ambiguous", "ambiguous"]
        assert all(row.seq is None for row in rows)
        assert reconstructor.finish() == []

    def test_anchor_inconsistent_with_counter_raises(self):
        reconstructor = SequenceReconstructor(max_gap=1)
        reconstructor.feed(Observation(0, anchor=0))
        with pytest.raises(AnchorMismatch):
            reconstructor.feed(Observation(2, anchor=11))  # 11 % 256 != 2

    def test_anchor_contradicting_established_stream_raises(self):
        reconstructor = SequenceReconstructor(max_gap=1)
        reconstructor.feed(Observation(0, anchor=0))
        reconstructor.feed(Observation(1))
        with pytest.raises(AnchorMismatch):
            reconstructor.feed(Observation(2, anchor=50))

    def test_invalid_counter_rejected(self):
        reconstructor = SequenceReconstructor(max_gap=1)
        with pytest.raises(ValueError):
            reconstructor.feed(Observation(256))
        with pytest.raises(ValueError):
            reconstructor.feed(Observation(-1))

    def test_invalid_max_gap_rejected(self):
        with pytest.raises(ValueError):
            SequenceReconstructor(max_gap=-1)
        with pytest.raises(ValueError):
            SequenceReconstructor(max_gap=255)


class TestAlignEvents:
    def test_perfect_pairing(self):
        times = [
            Observation(i, f"t{i}", anchor=0 if i == 0 else None) for i in range(5)
        ]
        values = [
            Observation(i, f"v{i}", anchor=0 if i == 0 else None) for i in range(5)
        ]
        events = list(align_events(times, values, max_gap=1))
        assert len(events) == 5
        assert all(event.status == Event.OK for event in events)
        assert [(event.seq, event.time, event.value) for event in events] == [
            (i, f"t{i}", f"v{i}") for i in range(5)
        ]

    def test_independent_volume_boundaries_missing_value(self):
        times = [
            Observation(i, f"t{i}", anchor=0 if i == 0 else None) for i in range(4)
        ]
        values = [
            Observation(i, f"v{i}", anchor=0 if i == 0 else None) for i in range(2)
        ]
        events = list(align_events(times, values, max_gap=1))
        assert [event.status for event in events] == [
            Event.OK,
            Event.OK,
            Event.MISSING_VALUE,
            Event.MISSING_VALUE,
        ]
        assert events[2].value is MISSING

    def test_independent_volume_boundaries_missing_time(self):
        times = [
            Observation(i, f"t{i}", anchor=0 if i == 0 else None) for i in range(2)
        ]
        values = [
            Observation(i, f"v{i}", anchor=0 if i == 0 else None) for i in range(3)
        ]
        events = list(align_events(times, values, max_gap=1))
        assert [event.status for event in events] == [
            Event.OK,
            Event.OK,
            Event.MISSING_TIME,
        ]

    def test_missing_row_on_one_side_still_joins(self):
        # Time side skips sequence 2; value side has all four rows.
        times = [
            Observation(0, "t0", anchor=0),
            Observation(1, "t1"),
            Observation(3, "t3"),
        ]
        values = [
            Observation(0, "v0", anchor=0),
            Observation(1, "v1"),
            Observation(2, "v2"),
            Observation(3, "v3"),
        ]
        events = list(align_events(times, values, max_gap=2))
        assert [(event.seq, event.status) for event in events] == [
            (0, Event.OK),
            (1, Event.OK),
            (2, Event.MISSING_TIME),
            (3, Event.OK),
        ]

    def test_duplicate_255_does_not_shift_the_next_cycle(self):
        times = [
            Observation(254, "t254", anchor=254),
            Observation(255, "t255"),
            Observation(255, "t255-dup"),
            Observation(0, "t256"),
            Observation(1, "t257"),
        ]
        values = [
            Observation(254, "v254", anchor=254),
            Observation(255, "v255"),
            Observation(0, "v256"),
            Observation(1, "v257"),
        ]
        events = list(align_events(times, values, max_gap=1))
        joined = [event for event in events if event.status != Event.RETRANSMITTED]
        assert [(event.seq, event.time, event.value) for event in joined] == [
            (254, "t254", "v254"),
            (255, "t255", "v255"),
            (256, "t256", "v256"),
            (257, "t257", "v257"),
        ]
        retransmitted = [
            event for event in events if event.status == Event.RETRANSMITTED
        ]
        assert len(retransmitted) == 1
        assert retransmitted[0].seq == 255

    def test_ambiguous_region_is_reviewed_then_anchor_recovers(self):
        # Both volumes start without an anchor and walk a full counter cycle,
        # then an anchor resynchronises them.
        times = [Observation(i % 256, f"t{i}") for i in range(260)]
        values = [Observation(i % 256, f"v{i}") for i in range(260)]
        times.append(Observation(4, "anchor-time", anchor=260))
        values.append(Observation(4, "anchor-value", anchor=260))
        events = list(align_events(iter(times), iter(values), max_gap=0))

        ambiguous = [event for event in events if event.status == Event.AMBIGUOUS]
        recovered = [
            event for event in events if event.seq == 260 and event.status == Event.OK
        ]
        assert ambiguous, "the over-long unanchored frame must be flagged for review"
        assert len(recovered) == 1
        assert recovered[0].time == "anchor-time"
        assert recovered[0].value == "anchor-value"

    def test_anchor_in_one_volume_resolves_pairing(self):
        # The time volume anchors early; the value volume only anchors later.
        times = [
            Observation(i % 256, f"t{i}", anchor=256 if i == 0 else None)
            for i in range(4)
        ]
        values = [Observation(i % 256, f"v{i}") for i in range(3)]
        values.append(Observation(3, "v3", anchor=259))
        events = list(align_events(iter(times), iter(values), max_gap=1))
        assert all(event.status == Event.OK for event in events)
        assert [event.seq for event in events] == [256, 257, 258, 259]

    def test_streaming_pass_keeps_only_a_small_buffer(self):
        def lazy(rows):
            for row in rows:
                yield row

        times = lazy(
            [
                Observation(i % 256, f"t{i}", anchor=0 if i == 0 else None)
                for i in range(1000)
            ]
        )
        values = lazy(
            [
                Observation(i % 256, f"v{i}", anchor=0 if i == 0 else None)
                for i in range(1000)
            ]
        )
        events = align_events(times, values, max_gap=2)
        first = next(events)
        assert first.seq == 0
        consumed = 1
        for event in events:
            consumed += 1
            assert event.seq <= consumed
        assert consumed == 1000

    def test_empty_volumes(self):
        assert list(align_events(iter(()), iter(()), max_gap=1)) == []

    def test_one_empty_volume(self):
        values = [Observation(0, "v0", anchor=0), Observation(1, "v1")]
        events = list(align_events(iter(()), values, max_gap=1))
        assert [event.status for event in events] == [
            Event.MISSING_TIME,
            Event.MISSING_TIME,
        ]

    def test_ambiguity_on_one_side_does_not_block_the_other(self):
        # The time volume walks a full cycle without an anchor; the value
        # volume is anchored and stays unambiguous.
        times = [Observation(i % 256, f"t{i}") for i in range(260)]
        values = [
            Observation(i % 256, f"v{i}", anchor=0 if i == 0 else None)
            for i in range(260)
        ]
        events = list(align_events(iter(times), iter(values), max_gap=0))
        ambiguous = [event for event in events if event.status == Event.AMBIGUOUS]
        assert ambiguous
        # Every ambiguous time row keeps its position in the stream and the
        # anchored value column is never lost.
        assert all(
            event.value is not MISSING or event.time is not None for event in events
        )

    def test_retransmission_on_both_sides_groups_by_sequence(self):
        times = [
            Observation(0, "t0", anchor=0),
            Observation(0, "t0r"),
            Observation(1, "t1"),
        ]
        values = [
            Observation(0, "v0", anchor=0),
            Observation(0, "v0r"),
            Observation(0, "v0r2"),
            Observation(1, "v1"),
        ]
        events = list(align_events(times, values, max_gap=1))
        assert events[0].status == Event.OK
        assert events[0].time == "t0"
        assert events[0].value == "v0"
        retransmitted = [
            event for event in events if event.status == Event.RETRANSMITTED
        ]
        assert len(retransmitted) == 3
        assert all(event.seq == 0 for event in retransmitted)
        last = events[-1]
        assert (last.seq, last.time, last.value, last.status) == (
            1,
            "t1",
            "v1",
            Event.OK,
        )


def _write_volume(path, rows):
    with xopen(path, "wt") as handle:
        for observation in rows:
            line = f"{observation.counter},{observation.value}"
            if observation.anchor is not None:
                line += f",{observation.anchor}"
            handle.write(line + "\n")


def _read_volume(path):
    with xopen(path, "rt") as handle:
        for line in handle:
            parts = line.rstrip("\n").split(",")
            anchor = int(parts[2]) if len(parts) > 2 else None
            yield Observation(int(parts[0]), parts[1], anchor)


class TestCompressedVolumes:
    def test_end_to_end_through_gzip_volumes(self, tmp_path):
        total = 300
        time_path = tmp_path / "times.gz"
        value_path = tmp_path / "values.gz"
        time_rows = []
        value_rows = []
        for seq in range(total):
            counter = seq % 256
            anchor = seq if counter == 0 else None
            if seq != 10:  # the time volume is missing one row
                time_rows.append(Observation(counter, f"t{seq}", anchor))
            if seq == 255:  # a duplicated 255 on the time side
                time_rows.append(Observation(255, f"t{seq}-dup"))
            if seq != 20:  # the value volume has its own independent boundary
                value_rows.append(Observation(counter, f"v{seq}", anchor))
        _write_volume(time_path, time_rows)
        _write_volume(value_path, value_rows)

        events = list(
            align_events(_read_volume(time_path), _read_volume(value_path), max_gap=2)
        )

        assert not any(event.status == Event.AMBIGUOUS for event in events)
        joined = [event for event in events if event.status != Event.RETRANSMITTED]
        assert [event.seq for event in joined] == list(range(total))
        assert joined[10].status == Event.MISSING_TIME
        assert joined[10].time is MISSING
        assert joined[20].status == Event.MISSING_VALUE
        assert joined[20].value is MISSING

        # The duplicated 255 must not shift the following wrap: seq 256 (a
        # fresh counter 0 anchored by the volume header) pairs cleanly.
        wrapped = joined[256]
        assert (wrapped.seq, wrapped.time, wrapped.value) == (
            256,
            "t256",
            "v256",
        )
        retransmitted = [
            event for event in events if event.status == Event.RETRANSMITTED
        ]
        assert len(retransmitted) == 1
        assert retransmitted[0].seq == 255
        assert retransmitted[0].time == "t255-dup"
