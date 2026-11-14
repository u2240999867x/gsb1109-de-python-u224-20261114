"""
Estimation of bus occupancy from historical frame records.

Only the frame records (timestamps, identifiers, DLC and flags) are available,
never the bit-level waveform. The exact number of dynamically inserted bit
stuffing bits is therefore unknown, so every estimate is given as an interval
with a lower and an upper bound.

The calculations follow the frame formats defined in ISO 11898-1. The
inter-frame space (three nominal bit times) is attributed to the preceding
frame, as is customary for bus-load calculations.

.. note::
    Remote frames never carry a data field: their DLC describes the requested
    response length only and must not be counted as data bytes. CAN FD frames
    with the bit-rate-switch flag set transmit their data phase at the data
    bit rate; if no data bit rate is configured the data-phase contribution is
    reported as undetermined instead of being guessed.
"""

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from can.message import Message

__all__ = [
    "BucketOccupancy",
    "BusLoadEstimator",
    "FrameBitLengths",
    "UndeterminedContribution",
    "estimate_bus_load",
]

# Fixed (non-stuffable) frame overhead in nominal bit times, including SOF,
# arbitration/control, CRC sequence and delimiter, ACK, EOF and the 3-bit IFS.
_CLASSICAL_BASE_FIXED_BITS = 47
_CLASSICAL_EXTENDED_FIXED_BITS = 67

# Bits from SOF through the CRC sequence that are subject to bit stuffing.
_CLASSICAL_BASE_STUFFABLE_BITS = 34
_CLASSICAL_EXTENDED_STUFFABLE_BITS = 54

# CAN FD arbitration phase, from SOF up to and including the BRS bit.
# Subject to the same dynamic bit stuffing as classical CAN.
_FD_BASE_ARBITRATION_BITS = 16
_FD_EXTENDED_ARBITRATION_BITS = 35
_FD_IFS_BITS = 3

#: Data length threshold above which the 21-bit CRC is used instead of 17 bits.
_FD_CRC21_DATA_THRESHOLD = 16


def _fd_data_phase_bits(num_data_bytes: int) -> int:
    """Deterministic data-phase length in bit times (fixed bit stuffing).

    Covers ESI, DLC, the data field, the stuff-count field, the CRC sequence
    with its fixed stuff bits, CRC delimiter, ACK and EOF. Reproduces the
    ISO 11898-1 lengths, e.g. 41 bits for zero data bytes and 660 bits for
    64 data bytes (data phase only, base identifier).
    """
    # ESI (1) + DLC (4) + data; a fixed stuff bit follows every fifth bit.
    stuffed_region = 5 + 8 * num_data_bytes
    data_fixed_stuff_bits = stuffed_region // 5

    crc_bits = 17 if num_data_bytes <= _FD_CRC21_DATA_THRESHOLD else 21
    crc_fixed_stuff_bits = crc_bits // 5

    return (
        stuffed_region
        + data_fixed_stuff_bits
        + 5  # stuff-count field (4 bits) plus its parity bit
        + crc_bits
        + crc_fixed_stuff_bits
        + 1  # CRC delimiter
        + 2  # ACK slot and ACK delimiter
        + 7  # end of frame
    )


def _max_stuff_bits(stuffable_bits: int) -> int:
    """Worst-case number of dynamically inserted stuffing bits."""
    return max(0, (stuffable_bits - 1) // 4)


@dataclass(frozen=True)
class FrameBitLengths:
    """Number of bit times a frame occupies on the wire.

    Bit stuffing is only bounded, not known exactly, so the nominal-phase
    counts are given as intervals. The CAN FD data phase uses fixed stuffing
    bits only and therefore has a deterministic length.

    :ivar nominal_bits_min: minimum bit times transmitted at the nominal rate
    :ivar nominal_bits_max: maximum bit times transmitted at the nominal rate
    :ivar data_bits: bit times transmitted at the data rate (CAN FD with
        bit-rate switching only, zero otherwise)
    :ivar num_data_bytes: data bytes actually carried (zero for remote frames)
    :ivar is_fd: whether the frame is a CAN FD frame
    :ivar is_remote_frame: whether the frame is a remote frame
    :ivar is_extended_id: whether the 29-bit identifier format is used
    """

    nominal_bits_min: int
    nominal_bits_max: int
    data_bits: int
    num_data_bytes: int
    is_fd: bool
    is_remote_frame: bool
    is_extended_id: bool

    @property
    def total_bits_min(self) -> int:
        return self.nominal_bits_min + self.data_bits

    @property
    def total_bits_max(self) -> int:
        return self.nominal_bits_max + self.data_bits


@dataclass(frozen=True)
class UndeterminedContribution:
    """A frame contribution whose duration cannot be bounded.

    :ivar frame_index: index of the frame in the analyzed sequence
    :ivar arbitration_id: identifier of the affected frame
    :ivar is_extended_id: whether the frame uses the extended identifier
    :ivar data_bits: number of data-phase bit times with an unknown rate
    """

    frame_index: int
    arbitration_id: int
    is_extended_id: bool
    data_bits: int


@dataclass
class BucketOccupancy:
    """Occupancy estimate for a single time bucket.

    :ivar index: zero-based bucket index relative to the analysis start
    :ivar start_time: bucket start in seconds (absolute timestamps)
    :ivar end_time: bucket end in seconds
    :ivar min_duration: lower bound of the occupied time in seconds
    :ivar max_duration: upper bound of the occupied time in seconds
    :ivar nominal_duration: midpoint estimate, in seconds
    :ivar frame_count: number of frames touching the bucket
    :ivar undetermined: contributions with an unknown duration
    """

    index: int
    start_time: float
    end_time: float
    min_duration: float = 0.0
    max_duration: float = 0.0
    nominal_duration: float = 0.0
    frame_count: int = 0
    undetermined: list[UndeterminedContribution] = field(default_factory=list)

    @property
    def width(self) -> float:
        return self.end_time - self.start_time

    @property
    def is_complete(self) -> bool:
        """True when every contribution could be bounded numerically."""
        return not self.undetermined

    @property
    def min_load(self) -> float:
        """Lower bound of the bus load as a fraction of the bucket width."""
        return self.min_duration / self.width

    @property
    def max_load(self) -> float:
        """Upper bound of the bus load as a fraction of the bucket width."""
        return self.max_duration / self.width

    @property
    def nominal_load(self) -> float:
        """Midpoint bus load as a fraction of the bucket width."""
        return self.nominal_duration / self.width


def frame_bit_lengths(message: "Message") -> FrameBitLengths | None:
    """Compute the on-wire bit counts for a single frame.

    :param message: the recorded frame
    :return: the bit-length intervals, or ``None`` for error frames whose
        on-wire length is not defined by the frame record
    :raises ValueError: if an unsupported DLC is encountered
    """
    if message.is_error_frame:
        return None

    if message.is_fd:
        return _fd_bit_lengths(message)
    return _classical_bit_lengths(message)


def _classical_bit_lengths(message: "Message") -> FrameBitLengths:
    # Remote frames carry no data bytes regardless of their DLC.
    num_data_bytes = 0 if message.is_remote_frame else min(message.dlc, 8)

    if message.is_extended_id:
        fixed_bits = _CLASSICAL_EXTENDED_FIXED_BITS + 8 * num_data_bytes
        stuffable_bits = _CLASSICAL_EXTENDED_STUFFABLE_BITS + 8 * num_data_bytes
    else:
        fixed_bits = _CLASSICAL_BASE_FIXED_BITS + 8 * num_data_bytes
        stuffable_bits = _CLASSICAL_BASE_STUFFABLE_BITS + 8 * num_data_bytes

    return FrameBitLengths(
        nominal_bits_min=fixed_bits,
        nominal_bits_max=fixed_bits + _max_stuff_bits(stuffable_bits),
        data_bits=0,
        num_data_bytes=num_data_bytes,
        is_fd=False,
        is_remote_frame=message.is_remote_frame,
        is_extended_id=message.is_extended_id,
    )


def _fd_bit_lengths(message: "Message") -> FrameBitLengths:
    # python-can stores the data length (0..64), not the raw 4-bit DLC code.
    num_data_bytes = message.dlc
    if not 0 <= num_data_bytes <= 64:
        raise ValueError(f"CAN FD data length must be in range 0..64, got {num_data_bytes}")

    arbitration_bits = (
        _FD_EXTENDED_ARBITRATION_BITS if message.is_extended_id else _FD_BASE_ARBITRATION_BITS
    )
    fixed_nominal_bits = arbitration_bits + _FD_IFS_BITS
    nominal_bits_min = fixed_nominal_bits
    nominal_bits_max = fixed_nominal_bits + _max_stuff_bits(arbitration_bits)

    data_phase_bits = _fd_data_phase_bits(num_data_bytes)

    if message.bitrate_switch:
        nominal_bits = (nominal_bits_min, nominal_bits_max)
        data_bits = data_phase_bits
    else:
        # Without bit-rate switching the whole frame runs at the nominal rate.
        nominal_bits = (
            nominal_bits_min + data_phase_bits,
            nominal_bits_max + data_phase_bits,
        )
        data_bits = 0

    return FrameBitLengths(
        nominal_bits_min=nominal_bits[0],
        nominal_bits_max=nominal_bits[1],
        data_bits=data_bits,
        num_data_bytes=num_data_bytes,
        is_fd=True,
        is_remote_frame=False,
        is_extended_id=message.is_extended_id,
    )


class BusLoadEstimator:
    """Estimates per-bucket bus occupancy for a sequence of recorded frames.

    :param nominal_bitrate: nominal (arbitration phase) bit rate in bit/s
    :param data_bitrate: data-phase bit rate in bit/s, required for CAN FD
        frames using bit-rate switching
    :param bucket_width: width of the analysis time buckets in seconds
    :raises ValueError: if a bit rate or bucket width is not positive
    """

    def __init__(
        self,
        nominal_bitrate: float,
        data_bitrate: float | None = None,
        bucket_width: float = 1.0,
    ) -> None:
        if nominal_bitrate <= 0:
            raise ValueError("nominal_bitrate must be positive")
        if data_bitrate is not None and data_bitrate <= 0:
            raise ValueError("data_bitrate must be positive")
        if bucket_width <= 0:
            raise ValueError("bucket_width must be positive")
        self.nominal_bitrate = float(nominal_bitrate)
        self.data_bitrate = None if data_bitrate is None else float(data_bitrate)
        self.bucket_width = float(bucket_width)

    def frame_durations(self, message: "Message") -> tuple[float, float, float, int]:
        """Return the ``(min, nominal, max)`` frame duration in seconds.

        The fourth value is the number of data-phase bit times whose duration
        is undetermined because no data bit rate is configured.

        :raises ValueError: if the frame is an error frame
        """
        bits = frame_bit_lengths(message)
        if bits is None:
            raise ValueError("error frames have no defined on-wire duration")

        min_duration = bits.nominal_bits_min / self.nominal_bitrate
        max_duration = bits.nominal_bits_max / self.nominal_bitrate
        undetermined_bits = 0

        if bits.data_bits:
            if self.data_bitrate is None:
                undetermined_bits = bits.data_bits
            else:
                data_duration = bits.data_bits / self.data_bitrate
                min_duration += data_duration
                max_duration += data_duration

        nominal_duration = (min_duration + max_duration) / 2.0
        return min_duration, nominal_duration, max_duration, undetermined_bits

    def estimate(
        self,
        messages: "Sequence[Message] | Iterable[Message]",
        start_time: float | None = None,
    ) -> list[BucketOccupancy]:
        """Split the frames into time buckets and estimate their occupancy.

        Frames crossing a bucket boundary are split according to the interval
        they may occupy in each bucket. Per-frame lower and upper bounds are
        summed independently, so the returned intervals always bracket the
        true occupancy even though the exact bit-stuffing pattern is unknown.

        :param messages: recorded frames; their order does not change the sums
        :param start_time: timestamp of the zero-bucket boundary; defaults to
            the timestamp of the first frame. Frames earlier than this are
            assigned negative bucket indices.
        :return: one :class:`BucketOccupancy` per touched bucket, ordered by
            time
        """
        frames = list(messages)
        if not frames:
            return []
        if start_time is None:
            start_time = frames[0].timestamp

        buckets: dict[int, BucketOccupancy] = {}

        for frame_index, message in enumerate(frames):
            bits = frame_bit_lengths(message)
            if bits is None:
                # Error frames do not have a length defined by the records.
                continue

            min_duration, nominal_duration, max_duration, undetermined_bits = self.frame_durations(
                message
            )

            touched_buckets = self._allocate(
                buckets,
                frame_start=message.timestamp,
                durations=(min_duration, nominal_duration, max_duration),
                start_time=start_time,
            )

            if undetermined_bits:
                contribution = UndeterminedContribution(
                    frame_index=frame_index,
                    arbitration_id=message.arbitration_id,
                    is_extended_id=message.is_extended_id,
                    data_bits=undetermined_bits,
                )
                for bucket_index in touched_buckets:
                    buckets[bucket_index].undetermined.append(contribution)

        return [buckets[index] for index in sorted(buckets)]

    def _allocate(
        self,
        buckets: dict[int, BucketOccupancy],
        frame_start: float,
        durations: tuple[float, float, float],
        start_time: float,
    ) -> list[int]:
        # A frame may reach further at its maximum duration, so the set of
        # touched buckets is the union over all three duration scenarios.
        scenario_buckets: list[list[tuple[int, float]]] = []
        touched: set[int] = set()
        for duration in durations:
            frame_end = frame_start + duration
            first_bucket = math.floor((frame_start - start_time) / self.bucket_width)
            last_bucket = math.floor((frame_end - start_time) / self.bucket_width)

            overlaps: list[tuple[int, float]] = []
            for bucket_index in range(first_bucket, last_bucket + 1):
                bucket_start = start_time + bucket_index * self.bucket_width
                bucket_end = bucket_start + self.bucket_width
                overlap = min(frame_end, bucket_end) - max(frame_start, bucket_start)
                if overlap <= 0:
                    continue
                overlaps.append((bucket_index, overlap))
                touched.add(bucket_index)
            scenario_buckets.append(overlaps)

        for bucket_index in sorted(touched):
            if bucket_index not in buckets:
                bucket_start = start_time + bucket_index * self.bucket_width
                buckets[bucket_index] = BucketOccupancy(
                    index=bucket_index,
                    start_time=bucket_start,
                    end_time=bucket_start + self.bucket_width,
                )

        for scenario_index, overlaps in enumerate(scenario_buckets):
            for bucket_index, overlap in overlaps:
                bucket = buckets[bucket_index]
                if scenario_index == 0:
                    bucket.min_duration += overlap
                elif scenario_index == 1:
                    bucket.nominal_duration += overlap
                else:
                    bucket.max_duration += overlap

        for bucket_index in touched:
            buckets[bucket_index].frame_count += 1

        return sorted(touched)


def estimate_bus_load(
    messages: "Sequence[Message] | Iterable[Message]",
    nominal_bitrate: float,
    data_bitrate: float | None = None,
    bucket_width: float = 1.0,
    start_time: float | None = None,
) -> list[BucketOccupancy]:
    """Convenience wrapper around :class:`BusLoadEstimator`.

    :param messages: recorded frames
    :param nominal_bitrate: nominal bit rate in bit/s
    :param data_bitrate: optional data-phase bit rate in bit/s
    :param bucket_width: bucket width in seconds
    :param start_time: optional timestamp of the first bucket boundary
    :return: occupancy estimates for every touched bucket
    """
    estimator = BusLoadEstimator(
        nominal_bitrate=nominal_bitrate,
        data_bitrate=data_bitrate,
        bucket_width=bucket_width,
    )
    return estimator.estimate(messages, start_time=start_time)
