import io
import unittest
from datetime import UTC, datetime

import can
from can.io.asc_timeline import ASCTimeline


def _ts(hour, minute=0, second=0):
    return datetime(2026, 11, 14, hour, minute, second, tzinfo=UTC).timestamp()


T0 = _ts(10)


THREE_BLOCKS = """date Sat Nov 14 09:59:00.000 2026
base hex  timestamps absolute
internal events logged
Begin Triggerblock Sat Nov 14 10:00:00.000 2026
   0.000000 Start of measurement
   0.100000 1  100  Rx d 1 01
   2.500000 1  101  Rx d 1 02
End TriggerBlock
Begin Triggerblock
   0.050000 1  200  Rx d 1 03
   1.000000 1  201  Rx d 1 04
End TriggerBlock
Begin Triggerblock Sat Nov 14 10:01:00.000 2026
   0.200000 1  300  Rx d 1 05
End TriggerBlock
"""

CONTRADICTING_ANCHORS = """date Sat Nov 14 09:59:00.000 2026
base hex  timestamps absolute
internal events logged
Begin Triggerblock Sat Nov 14 10:00:00.000 2026
   0.000000 1  100  Rx d 1 01
   5.000000 1  101  Rx d 1 02
  10.000000 1  102  Rx d 1 03
End TriggerBlock
Begin Triggerblock Sat Nov 14 10:00:08.000 2026
   0.000000 1  200  Rx d 1 04
   1.000000 1  201  Rx d 1 05
End TriggerBlock
"""

BROKEN_CONTINUITY = """date Sat Nov 14 09:59:00.000 2026
base hex  timestamps absolute
internal events logged
Begin Triggerblock Sat Nov 14 10:00:00.000 2026
   0.100000 1  100  Rx d 1 01
   0.200000 1  101  Rx d 1 02
   0.050000 1  102  Rx d 1 03
   0.300000 1  103  Rx d 1 04
End TriggerBlock
"""

RELATIVE_TIMESTAMPS = """date Sat Nov 14 09:59:00.000 2026
base hex  timestamps relative
internal events logged
Begin Triggerblock Sat Nov 14 10:00:00.000 2026
   0.100000 1  100  Rx d 1 01
   0.100000 1  101  Rx d 1 02
End TriggerBlock
"""

IMPLICIT_BLOCK = """date Sat Nov 14 10:00:00.000 2026
base hex  timestamps absolute
internal events logged
   0.100000 1  100  Rx d 1 01
"""


def _parse(text):
    return ASCTimeline.from_file(io.StringIO(text), tz=UTC)


class TestPartiallyAnchoredBlocks(unittest.TestCase):
    def setUp(self):
        self.timeline = _parse(THREE_BLOCKS)

    def test_block_anchors(self):
        blocks = self.timeline.blocks
        self.assertEqual(len(blocks), 3)
        self.assertEqual(blocks[0].anchor, T0)
        self.assertIsNone(blocks[1].anchor)
        self.assertEqual(blocks[2].anchor, T0 + 60)

    def test_anchored_frames_are_determined(self):
        frame = self.timeline.blocks[0].frames[0]
        self.assertAlmostEqual(frame.timestamp, T0 + 0.1)
        self.assertTrue(frame.is_determined)

    def test_unanchored_block_has_no_absolute_time(self):
        for frame in self.timeline.blocks[1].frames:
            self.assertIsNone(frame.timestamp)
            self.assertFalse(frame.is_determined)

    def test_file_order_and_block_numbers_preserved(self):
        # the first frame of block 1 (0.05) is numerically smaller than the
        # last frame of block 0 (2.5); order and block numbers must stay
        frames = self.timeline.frames
        self.assertEqual([f.block_index for f in frames], [0, 0, 1, 1, 2])
        self.assertEqual([f.offset for f in frames], [0.1, 2.5, 0.05, 1.0, 0.2])

    def test_event_time_query_returns_only_determined_frames(self):
        frames = self.timeline.frames_by_event_time(T0, T0 + 3)
        self.assertEqual([f.offset for f in frames], [0.1, 2.5])
        frames = self.timeline.frames_by_event_time(T0 + 59, T0 + 61)
        self.assertEqual([f.block_index for f in frames], [2])

    def test_unanchored_frames_never_mix_into_event_time(self):
        # removing the middle anchor must not let estimates leak into the
        # certain times: the middle block contributes nothing, not even
        # with include_undetermined
        frames = self.timeline.frames_by_event_time(T0, T0 + 120, include_undetermined=True)
        self.assertEqual([f.block_index for f in frames], [0, 0, 2])

    def test_block_time_query(self):
        frames = self.timeline.frames_by_block_time(1, 0.0, 0.5)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].offset, 0.05)
        self.assertEqual(frames[0].message.arbitration_id, 0x200)
        all_frames = self.timeline.frames_by_block_time(1)
        self.assertEqual(len(all_frames), 2)

    def test_block_time_query_unknown_block(self):
        with self.assertRaises(IndexError):
            self.timeline.frames_by_block_time(3)
        with self.assertRaises(IndexError):
            self.timeline.frames_by_block_time(-1)

    def test_event_time_query_rejects_inverted_interval(self):
        with self.assertRaises(ValueError):
            self.timeline.frames_by_event_time(T0 + 1, T0)


class TestContradictingAnchors(unittest.TestCase):
    def setUp(self):
        self.timeline = _parse(CONTRADICTING_ANCHORS)

    def test_undetermined_segment_is_reported(self):
        self.assertEqual(len(self.timeline.undetermined_segments), 1)
        start, end = self.timeline.undetermined_segments[0]
        self.assertEqual(start, T0 + 8)
        self.assertEqual(end, T0 + 10)

    def test_frames_in_undetermined_segment_are_not_determined(self):
        determined = [f.is_determined for f in self.timeline.frames]
        self.assertEqual(determined, [True, True, False, False, False])

    def test_nominal_timestamp_is_kept(self):
        frame = self.timeline.blocks[1].frames[0]
        self.assertEqual(frame.timestamp, T0 + 8)
        self.assertFalse(frame.is_determined)

    def test_event_time_query_skips_undetermined_segment(self):
        frames = self.timeline.frames_by_event_time(T0 + 8.5, T0 + 9.5)
        self.assertEqual(frames, [])
        frames = self.timeline.frames_by_event_time(T0 + 8.5, T0 + 9.5, include_undetermined=True)
        self.assertEqual([(f.block_index, f.offset) for f in frames], [(1, 1.0)])

    def test_event_time_query_outside_undetermined_segment(self):
        frames = self.timeline.frames_by_event_time(T0, T0 + 6)
        self.assertEqual([f.offset for f in frames], [0.0, 5.0])


class TestProvablyContinuousBoundary(unittest.TestCase):
    def setUp(self):
        self.timeline = _parse(BROKEN_CONTINUITY)

    def test_frames_after_backward_step_are_not_determined(self):
        determined = [f.is_determined for f in self.timeline.frames]
        self.assertEqual(determined, [True, True, False, False])

    def test_continuous_boundary_limits_block_interval(self):
        block = self.timeline.blocks[0]
        self.assertEqual(block.continuous_end, 0.2)
        self.assertEqual(block.absolute_interval, (T0, T0 + 0.2))


class TestTimestampFormats(unittest.TestCase):
    def test_relative_timestamps_accumulate_within_block(self):
        timeline = _parse(RELATIVE_TIMESTAMPS)
        offsets = [f.offset for f in timeline.frames]
        self.assertEqual(offsets, [0.1, 0.2])
        timestamps = [f.timestamp for f in timeline.frames]
        self.assertEqual(timestamps, [T0 + 0.1, T0 + 0.2])
        self.assertTrue(all(f.is_determined for f in timeline.frames))

    def test_implicit_block_uses_file_date_as_anchor(self):
        timeline = _parse(IMPLICIT_BLOCK)
        self.assertEqual(len(timeline.blocks), 1)
        self.assertEqual(timeline.blocks[0].anchor, T0)
        self.assertEqual(timeline.frames[0].timestamp, T0 + 0.1)
        self.assertTrue(timeline.frames[0].is_determined)


class TestMessageContent(unittest.TestCase):
    def test_message_is_parsed(self):
        timeline = _parse(THREE_BLOCKS)
        msg = timeline.frames[0].message
        self.assertEqual(msg.arbitration_id, 0x100)
        self.assertEqual(list(msg.data), [0x01])
        self.assertEqual(msg.channel, 0)
        # the message carries its block-relative timestamp
        self.assertEqual(msg.timestamp, 0.1)

    def test_reader_exported_from_can_io(self):
        self.assertIs(can.io.ASCTimeline, ASCTimeline)


if __name__ == "__main__":
    unittest.main()
