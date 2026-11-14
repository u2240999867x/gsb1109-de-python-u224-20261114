#!/usr/bin/env python

"""Tests for the historical bus-occupancy estimation in :mod:`can.bus_load`."""

import pytest

import can
from can.bus_load import (
    BusLoadEstimator,
    FrameBitLengths,
    estimate_bus_load,
    frame_bit_lengths,
)


def make_message(**kwargs):
    kwargs.setdefault("is_extended_id", False)
    return can.Message(check=True, **kwargs)


class TestFrameBitLengths:
    def test_classical_base_data_frame(self):
        message = make_message(dlc=8, data=bytes(range(8)))
        bits = frame_bit_lengths(message)
        assert isinstance(bits, FrameBitLengths)
        # 47 fixed bits + 64 data bits; up to 24 stuffing bits.
        assert bits.nominal_bits_min == 111
        assert bits.nominal_bits_max == 135
        assert bits.data_bits == 0
        assert bits.num_data_bytes == 8
        assert not bits.is_fd

    def test_classical_extended_data_frame(self):
        message = make_message(dlc=8, data=bytes(range(8)), is_extended_id=True)
        bits = frame_bit_lengths(message)
        # 67 fixed bits + 64 data bits; up to 29 stuffing bits.
        assert bits.nominal_bits_min == 131
        assert bits.nominal_bits_max == 160

    def test_remote_frame_dlc_does_not_add_data_bytes(self):
        message = make_message(dlc=8, is_remote_frame=True)
        bits = frame_bit_lengths(message)
        assert bits.num_data_bytes == 0
        # Same length as an empty data frame regardless of DLC.
        assert bits.nominal_bits_min == 47
        assert bits.nominal_bits_max == 55
        assert bits.is_remote_frame

    def test_classical_zero_length_frame(self):
        bits = frame_bit_lengths(make_message(dlc=0))
        assert bits.nominal_bits_min == 47
        assert bits.nominal_bits_max == 55

    def test_fd_anchor_lengths(self):
        # ISO 11898-1 totals excluding IFS are 57, 133, 254 and 676 bits.
        expected = {0: 57, 8: 133, 20: 254, 64: 676}
        for num_data_bytes, reference in expected.items():
            message = make_message(
                is_fd=True,
                dlc=num_data_bytes,
                data=bytes(num_data_bytes),
                bitrate_switch=True,
            )
            bits = frame_bit_lengths(message)
            # IFS adds three nominal bit times.
            assert bits.nominal_bits_min + bits.data_bits == reference + 3
            assert bits.total_bits_min == reference + 3

    def test_fd_extended_identifier_adds_arbitration_bits(self):
        base = frame_bit_lengths(make_message(is_fd=True, dlc=0))
        extended = frame_bit_lengths(make_message(is_fd=True, dlc=0, is_extended_id=True))
        # Arbitration grows from 16 to 35 bits; the data phase is unchanged.
        assert extended.nominal_bits_min - base.nominal_bits_min == 19
        assert extended.data_bits == base.data_bits

    def test_fd_without_bitrate_switch_uses_nominal_rate(self):
        message = make_message(is_fd=True, dlc=64, data=bytes(64), bitrate_switch=False)
        bits = frame_bit_lengths(message)
        assert bits.data_bits == 0
        assert bits.nominal_bits_min == 19 + 660

    def test_fd_with_bitrate_switch_separates_data_phase(self):
        message = make_message(is_fd=True, dlc=8, data=bytes(8), bitrate_switch=True)
        bits = frame_bit_lengths(message)
        assert bits.nominal_bits_min == 19  # 16 arbitration bits + 3 IFS
        assert bits.data_bits == 117  # 133 total minus 16 arbitration bits

    def test_fd_invalid_length_raises(self):
        message = make_message(is_fd=True, dlc=64, data=bytes(64))
        message.dlc = 65
        with pytest.raises(ValueError, match=r"0\.\.64"):
            frame_bit_lengths(message)

    def test_error_frame_has_no_length(self):
        assert frame_bit_lengths(make_message(is_error_frame=True)) is None


class TestBusLoadEstimator:
    def test_invalid_configuration_raises(self):
        with pytest.raises(ValueError, match="nominal_bitrate"):
            BusLoadEstimator(nominal_bitrate=0)
        with pytest.raises(ValueError, match="data_bitrate"):
            BusLoadEstimator(nominal_bitrate=500_000, data_bitrate=-1)
        with pytest.raises(ValueError, match="bucket_width"):
            BusLoadEstimator(nominal_bitrate=500_000, bucket_width=0)

    def test_empty_sequence(self):
        estimator = BusLoadEstimator(nominal_bitrate=1000)
        assert estimator.estimate([]) == []

    def test_single_frame_single_bucket(self):
        message = make_message(timestamp=0.1, dlc=8, data=bytes(range(8)))
        estimator = BusLoadEstimator(nominal_bitrate=100, bucket_width=2.0)
        buckets = estimator.estimate([message], start_time=0.0)
        assert len(buckets) == 1
        bucket = buckets[0]
        assert bucket.index == 0
        assert bucket.start_time == pytest.approx(0.0)
        assert bucket.end_time == pytest.approx(2.0)
        assert bucket.frame_count == 1
        assert bucket.min_duration == pytest.approx(111 / 100)
        assert bucket.max_duration == pytest.approx(135 / 100)
        assert bucket.nominal_duration == pytest.approx((111 + 135) / 2 / 100)
        assert bucket.min_load <= bucket.nominal_load <= bucket.max_load
        assert bucket.is_complete

    def test_frame_crossing_bucket_boundary_is_split(self):
        # At 100 bit/s one bit takes 0.01 s: a base frame spans 0.47..0.55 s.
        message = make_message(timestamp=0.0, dlc=0)
        estimator = BusLoadEstimator(nominal_bitrate=100, bucket_width=0.5)
        buckets = estimator.estimate([message], start_time=0.0)
        assert [bucket.index for bucket in buckets] == [0, 1]

        first, second = buckets
        assert first.min_duration == pytest.approx(0.47)
        assert first.max_duration == pytest.approx(0.50)
        # The minimum scenario ends before the boundary, so bucket 1 only
        # receives part of the maximum-duration scenario.
        assert second.min_duration == pytest.approx(0.0)
        assert second.max_duration == pytest.approx(0.05)
        assert first.frame_count == second.frame_count == 1

        # Splitting must preserve each scenario's total frame duration.
        assert first.min_duration + second.min_duration == pytest.approx(0.47)
        assert first.max_duration + second.max_duration == pytest.approx(0.55)

    def test_bounds_hold_across_multiple_frames(self):
        messages = [
            make_message(timestamp=0.0, dlc=0),
            make_message(timestamp=0.2, dlc=4, data=bytes(4)),
        ]
        estimator = BusLoadEstimator(nominal_bitrate=100, bucket_width=10.0)
        buckets = estimator.estimate(messages)
        assert len(buckets) == 1
        bucket = buckets[0]
        assert bucket.frame_count == 2
        assert bucket.min_duration < bucket.max_duration
        assert bucket.min_load <= 1.0

    def test_fd_without_data_bitrate_is_undetermined(self):
        message = make_message(
            timestamp=0.0,
            is_fd=True,
            dlc=8,
            data=bytes(8),
            bitrate_switch=True,
        )
        estimator = BusLoadEstimator(nominal_bitrate=100, data_bitrate=None, bucket_width=1.0)
        buckets = estimator.estimate([message])
        bucket = buckets[0]
        assert not bucket.is_complete
        assert len(bucket.undetermined) == 1
        contribution = bucket.undetermined[0]
        assert contribution.frame_index == 0
        assert contribution.data_bits == 117
        # The bounded arbitration/IFS portion is still reported.
        assert bucket.min_duration == pytest.approx(19 / 100)

    def test_fd_with_data_bitrate_is_complete(self):
        message = make_message(
            timestamp=0.0,
            is_fd=True,
            dlc=8,
            data=bytes(8),
            bitrate_switch=True,
        )
        estimator = BusLoadEstimator(nominal_bitrate=100, data_bitrate=200, bucket_width=1.0)
        buckets = estimator.estimate([message])
        bucket = buckets[0]
        assert bucket.is_complete
        # Arbitration (19..22 nominal bits) plus the deterministic data phase.
        assert bucket.min_duration == pytest.approx(19 / 100 + 117 / 200)
        assert bucket.max_duration == pytest.approx(22 / 100 + 117 / 200)

    def test_error_frames_are_skipped(self):
        messages = [
            make_message(timestamp=0.0, is_error_frame=True),
            make_message(timestamp=0.0, dlc=0),
        ]
        estimator = BusLoadEstimator(nominal_bitrate=100, bucket_width=1.0)
        buckets = estimator.estimate(messages)
        assert len(buckets) == 1
        assert buckets[0].frame_count == 1

    def test_default_start_time_uses_first_frame(self):
        message = make_message(timestamp=5.0, dlc=0)
        estimator = BusLoadEstimator(nominal_bitrate=100, bucket_width=1.0)
        buckets = estimator.estimate([message])
        assert buckets[0].start_time == pytest.approx(5.0)
        assert buckets[0].index == 0

    def test_convenience_wrapper(self):
        messages = [make_message(timestamp=0.0, dlc=0)]
        buckets = estimate_bus_load(messages, nominal_bitrate=100, bucket_width=1.0)
        assert len(buckets) == 1
        assert buckets[0].min_duration == pytest.approx(47 / 100)

    def test_frame_before_start_time_gets_negative_index(self):
        message = make_message(timestamp=0.2, dlc=0)
        estimator = BusLoadEstimator(nominal_bitrate=100, bucket_width=1.0)
        buckets = estimator.estimate([message], start_time=1.0)
        assert len(buckets) == 1
        assert buckets[0].index == -1
        assert buckets[0].start_time == pytest.approx(0.0)
        assert buckets[0].end_time == pytest.approx(1.0)

    def test_mixed_classical_and_fd_trace(self):
        messages = [
            make_message(timestamp=0.0, dlc=8, data=bytes(8)),
            make_message(
                timestamp=0.5,
                is_fd=True,
                dlc=64,
                data=bytes(64),
                bitrate_switch=True,
            ),
            make_message(timestamp=2.0, dlc=8, is_remote_frame=True),
        ]
        estimator = BusLoadEstimator(
            nominal_bitrate=500_000, data_bitrate=2_000_000, bucket_width=1.0
        )
        buckets = estimator.estimate(messages, start_time=0.0)
        # The short FD frame finishes inside the first one-second bucket.
        assert [bucket.index for bucket in buckets] == [0, 2]
        for bucket in buckets:
            assert bucket.is_complete
            assert bucket.min_duration <= bucket.nominal_duration <= bucket.max_duration
            assert bucket.min_load >= 0.0

        # First bucket holds the classical frame and the whole FD frame.
        assert buckets[0].frame_count == 2
        # Remote frame lands alone in the third bucket.
        assert buckets[1].frame_count == 1
        assert buckets[1].min_duration == pytest.approx(47 / 500_000)
