#!/usr/bin/env python

"""
This module tests :class:`can.MessageSync`.
"""

import gc
import time
import unittest
from copy import copy

import pytest

from can import Message, MessageSync, PlaybackTimeline

from .config import IS_CI, IS_GITHUB_ACTIONS, IS_LINUX, IS_OSX, IS_TRAVIS
from .data.example_data import TEST_MESSAGES_BASE
from .message_helper import ComparingMessagesTestCase

TEST_FEWER_MESSAGES = TEST_MESSAGES_BASE[::2]


def inc(value):
    """Makes the test boundaries give some more space when run on the CI server."""
    if IS_CI:
        return value * 1.5
    else:
        return value


skip_on_unreliable_platforms = unittest.skipIf(
    (IS_TRAVIS and IS_OSX) or (IS_GITHUB_ACTIONS and not IS_LINUX),
    "this environment's timings are too unpredictable",
)


@skip_on_unreliable_platforms
class TestMessageSync(unittest.TestCase, ComparingMessagesTestCase):
    def __init__(self, *args, **kwargs):
        unittest.TestCase.__init__(self, *args, **kwargs)
        ComparingMessagesTestCase.__init__(self)

    def setup_method(self, _):
        # disabling the garbage collector makes the time readings more reliable
        gc.disable()

    def teardown_method(self, _):
        # we need to reenable the garbage collector again
        gc.enable()

    def test_general(self):
        messages = [
            Message(timestamp=50.0),
            Message(timestamp=50.0),
            Message(timestamp=50.0 + 0.05),
            Message(timestamp=50.0 + 0.13),
            Message(timestamp=50.0),  # back in time
        ]
        sync = MessageSync(messages, gap=0.0, skip=0.0)

        t_start = time.perf_counter()
        collected = []
        timings = []
        for message in sync:
            t_now = time.perf_counter()
            collected.append(message)
            timings.append(t_now - t_start)

        self.assertMessagesEqual(messages, collected)
        self.assertEqual(len(timings), len(messages), "programming error in test code")

        self.assertTrue(0.0 <= timings[0] < 0.0 + inc(0.02), str(timings[0]))
        self.assertTrue(0.0 <= timings[1] < 0.0 + inc(0.02), str(timings[1]))
        self.assertTrue(0.045 <= timings[2] < 0.05 + inc(0.02), str(timings[2]))
        self.assertTrue(0.125 <= timings[3] < 0.13 + inc(0.02), str(timings[3]))
        self.assertTrue(0.125 <= timings[4] < 0.13 + inc(0.02), str(timings[4]))

    def test_skip(self):
        messages = copy(TEST_FEWER_MESSAGES)
        sync = MessageSync(messages, skip=0.005, gap=0.0)

        before = time.perf_counter()
        collected = list(sync)
        after = time.perf_counter()
        took = after - before

        # the handling of the messages itself also takes some time:
        # ~0.001 s/message on a ThinkPad T560 laptop (Ubuntu 18.04, i5-6200U)
        assert 0 < took < inc(len(messages) * (0.005 + 0.003)), f"took: {took}s"

        self.assertMessagesEqual(messages, collected)


@skip_on_unreliable_platforms
@pytest.mark.parametrize("timestamp_1,timestamp_2", [(0.0, 0.0), (0.0, 0.01), (0.01, 1.5)])
def test_gap(timestamp_1, timestamp_2):
    """This method is alone so it can be parameterized."""
    messages = [
        Message(arbitration_id=0x1, timestamp=timestamp_1),
        Message(arbitration_id=0x2, timestamp=timestamp_2),
    ]
    sync = MessageSync(messages, timestamps=False, gap=0.1)

    gc.disable()
    before = time.perf_counter()
    collected = list(sync)
    after = time.perf_counter()
    gc.enable()
    took = after - before

    assert 0.195 <= took < 0.2 + inc(0.02)
    assert messages == collected


class TestPlaybackTimeline(unittest.TestCase):
    """Tests for the mapping shared by playback and analysis."""

    def test_same_timestamp_frames_keep_input_order(self):
        first = Message(arbitration_id=0x1, timestamp=10.0)
        second = Message(arbitration_id=0x2, timestamp=10.0)
        third = Message(arbitration_id=0x3, timestamp=10.0)
        sync = MessageSync([first, second, third], skip=1.0)
        self.assertEqual(list(sync), [first, second, third])
        self.assertAlmostEqual(sync.to_playback_time(10.0), 0.0)
        self.assertEqual(sync.to_recorded_range(0.0).start, 10.0)
        self.assertEqual(sync.to_recorded_range(0.0).end, 10.0)

    def test_forward_mapping_keeps_short_pause(self):
        timeline = PlaybackTimeline(recorded_start=0.0, skip=5.0)
        timeline.add_frame(0.0)
        timeline.add_frame(2.0)
        self.assertAlmostEqual(timeline.to_playback_time(0.0), 0.0)
        self.assertAlmostEqual(timeline.to_playback_time(1.0), 1.0)
        self.assertAlmostEqual(timeline.to_playback_time(2.0), 2.0)
        self.assertEqual(timeline.windows, ())

    def test_forward_mapping_attributes_retained_skip_before_window(self):
        timeline = PlaybackTimeline(recorded_start=0.0, skip=3.0)
        timeline.add_frame(0.0)
        timeline.add_frame(10.0)  # 3 seconds retained, 7 seconds compressed
        self.assertAlmostEqual(timeline.to_playback_time(1.0), 1.0)
        self.assertAlmostEqual(timeline.to_playback_time(3.0), 3.0)
        self.assertAlmostEqual(timeline.to_playback_time(5.0), 3.0)
        self.assertAlmostEqual(timeline.to_playback_time(10.0), 3.0)

        (window,) = timeline.windows
        self.assertEqual(window.recorded_start, 3.0)
        self.assertEqual(window.recorded_end, 10.0)
        self.assertEqual(window.playback_start, 3.0)
        self.assertEqual(window.playback_end, 3.0)

        location = timeline.to_recorded_range(3.0)
        self.assertEqual((location.start, location.end), (3.0, 10.0))
        self.assertTrue(location.expands_compressed_window)

    def test_reverse_mapping_is_range_not_first_frame(self):
        timeline = PlaybackTimeline(recorded_start=100.0, skip=2.0)
        timeline.add_frame(100.0)
        timeline.add_frame(110.0)
        timeline.add_frame(110.0)

        location = timeline.to_recorded_range(2.0)
        self.assertEqual(location.start, 102.0)
        self.assertEqual(location.end, 110.0)
        self.assertNotEqual(location.start, location.end)

    def test_consecutive_compressed_pauses_are_retained(self):
        timeline = PlaybackTimeline(recorded_start=0.0, skip=1.0)
        for timestamp in (0.0, 10.0, 20.0):
            timeline.add_frame(timestamp)

        self.assertEqual(len(timeline.windows), 2)
        self.assertEqual(
            [(w.recorded_start, w.recorded_end) for w in timeline.windows],
            [(1.0, 10.0), (11.0, 20.0)],
        )
        for window in timeline.windows:
            self.assertEqual(window.playback_start, window.playback_end)

    def test_reverse_mapping_without_compression_is_instant(self):
        timeline = PlaybackTimeline(recorded_start=0.0, skip=1.0)
        timeline.add_frame(0.0)
        timeline.add_frame(0.5)
        location = timeline.to_recorded_range(0.25)
        self.assertEqual((location.start, location.end), (0.25, 0.25))
        self.assertFalse(location.expands_compressed_window)

    def test_unlocatable_playback_time_returns_none(self):
        timeline = PlaybackTimeline(recorded_start=0.0, skip=1.0)
        timeline.add_frame(0.0)
        timeline.add_frame(5.0)
        self.assertIsNone(timeline.to_recorded_range(-1.0))
        self.assertIsNone(timeline.to_recorded_range(2.0))

    def test_eager_mapping_is_queryable_before_playback(self):
        messages = [Message(timestamp=0.0), Message(timestamp=50.0)]
        sync = MessageSync(messages, skip=2.0)
        self.assertEqual(len(sync.windows), 1)
        self.assertEqual(sync.to_playback_time(25.0), 2.0)
        self.assertEqual(sync.to_recorded_range(2.0), (2.0, 50.0))
        self.assertEqual(list(sync), messages)
        self.assertEqual(sync.to_recorded_range(2.0), (2.0, 50.0))

    def test_backward_frame_stays_at_shared_playback_instant(self):
        timeline = PlaybackTimeline(recorded_start=0.0, skip=1.0)
        for timestamp in (0.0, 5.0, 2.0):
            timeline.add_frame(timestamp)
        self.assertEqual(timeline.to_playback_time(5.0), 1.0)
        self.assertEqual(timeline.to_playback_time(2.0), 1.0)

    def test_explicit_start_offset_does_not_shift_first_frame(self):
        timeline = PlaybackTimeline(recorded_start=100.0, skip=2.0)
        timeline.add_frame(102.0)
        timeline.add_frame(120.0)
        self.assertEqual(timeline.to_playback_time(102.0), 0.0)
        self.assertEqual(timeline.to_playback_time(118.0), 2.0)
        self.assertEqual(timeline.to_recorded_range(0.0), (102.0, 102.0))
        self.assertEqual(timeline.to_recorded_range(2.0), (104.0, 120.0))

    def test_time_before_first_frame_clamps_to_playback_start(self):
        timeline = PlaybackTimeline(recorded_start=100.0, skip=2.0)
        timeline.add_frame(102.0)
        timeline.add_frame(120.0)
        self.assertEqual(timeline.to_playback_time(100.0), 0.0)
        self.assertEqual(timeline.to_playback_time(101.0), 0.0)
        self.assertEqual(timeline.to_playback_time(102.0), 0.0)

    def test_fixed_gap_has_no_recorded_time_mapping(self):
        timeline = PlaybackTimeline(timestamps=False, gap=1.0)
        timeline.add_frame(10.0)
        timeline.add_frame(10.0)
        self.assertIsNone(timeline.to_playback_time(10.0))
        self.assertIsNone(timeline.to_recorded_range(1.0))

    def test_timeline_is_shared_with_iteration(self):
        messages = [Message(timestamp=0.0), Message(timestamp=10.0)]
        sync = MessageSync(messages, skip=1.0)
        self.assertEqual(list(sync), messages)
        timeline = sync.timeline
        self.assertEqual(timeline.frame_count, 2)
        self.assertEqual(len(timeline.windows), 1)
        self.assertEqual(sync.to_playback_time(10.0), 1.0)
        self.assertEqual(sync.to_recorded_range(1.0).start, 1.0)
        self.assertEqual(sync.to_recorded_range(1.0).end, 10.0)


if __name__ == "__main__":
    unittest.main()
