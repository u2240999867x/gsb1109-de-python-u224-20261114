"""
Locate frames of multi-trigger-block ASC exports in absolute (event) time
and block-relative time.

Some ASC exports restart their timestamps at zero after every trigger, so
each trigger block has its own relative time axis and only some blocks carry
an absolute start time in their ``Begin Triggerblock`` line. This module
parses such files into blocks and places frames on the absolute time axis
only where this is provable:

- a block with an absolute start time (an *anchor*) places its frames at
  ``anchor + offset`` as long as the block offsets increase monotonically
  (the provably continuous boundary of the block),
- a block without an anchor contributes no absolute timestamps at all, so
  no estimates are interpolated from neighbouring blocks,
- if the anchored intervals of two blocks overlap, the anchors contradict
  each other and the overlapping stretch is reported as an
  *undetermined segment*; frames falling into it are not considered
  determined.

Frames always keep their original file order and block numbers, even though
the first frame of a later block carries a smaller numeric timestamp than
the last frame of an earlier block.
"""

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import tzinfo
from typing import Final, TextIO

from ..message import Message
from ..typechecking import StringPathLike
from .asc import _LOCAL_TZ, ASC_MESSAGE_REGEX, ASCReader

logger = logging.getLogger("can.io.asc_timeline")

ASC_TRIGGER_START_REGEX: Final = re.compile(
    r"begin\s+triggerblock(?:\s+\w+\s+(?P<datetime_string>.+))?\s*$", re.IGNORECASE
)
ASC_TRIGGER_END_REGEX: Final = re.compile(r"end\s+triggerblock\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class TimelineFrame:
    """A single CAN frame located on the block and absolute time axes.

    :ivar block_index: number of the trigger block the frame belongs to,
        counted in file order starting at 0.
    :ivar offset: timestamp of the frame within its block, in seconds.
    :ivar timestamp: anchored absolute timestamp (epoch seconds) of the
        frame, or ``None`` if its block carries no anchor. For frames
        inside an undetermined segment this still holds the nominal value
        claimed by the block anchor; check :attr:`is_determined` before
        relying on it.
    :ivar is_determined: ``True`` only if the absolute timestamp is
        provable: the block is anchored, the block offsets are continuous
        up to this frame, and the timestamp does not fall into an
        undetermined segment.
    :ivar message: the parsed CAN message.
    """

    block_index: int
    offset: float
    timestamp: float | None
    is_determined: bool
    message: Message


@dataclass(frozen=True)
class TimelineBlock:
    """A trigger block of an ASC export.

    :ivar index: block number in file order, starting at 0.
    :ivar anchor: absolute start time of the block (epoch seconds) taken
        from its ``Begin Triggerblock`` line, or ``None`` if the block
        carries no absolute start time.
    :ivar frames: frames of the block, in file order.
    :ivar continuous_end: block offset up to which the offsets increase
        monotonically, i.e. the provably continuous boundary of the block.
    """

    index: int
    anchor: float | None
    frames: tuple[TimelineFrame, ...]
    continuous_end: float

    @property
    def absolute_interval(self) -> tuple[float, float] | None:
        """The absolute interval provably covered by this block, or
        ``None`` if the block carries no anchor."""
        if self.anchor is None:
            return None
        return (self.anchor, self.anchor + self.continuous_end)


class _ASCTimelineReader(ASCReader):
    """ASCReader variant that preserves the trigger-block structure."""

    def iter_block_frames(
        self,
    ) -> Iterator[tuple[int, float | None, float, bool, Message]]:
        """Yield ``(block_index, anchor, offset, continuous, message)``
        tuples in file order."""
        header_anchor: float | None = None
        block_index = -1
        anchor: float | None = None
        in_block = False
        seen_block = False
        last_offset = 0.0
        continuous = True

        for _line in self.file:
            line = _line.strip()

            if re.match(r"//", line):
                continue

            datetime_match = re.match(r"date\s+\w+\s+(?P<datetime_string>.+)", line, re.IGNORECASE)
            if datetime_match and not seen_block:
                self.date = datetime_match.group("datetime_string")
                try:
                    header_anchor = self._datetime_to_timestamp(self.date, self._timezone)
                except ValueError:
                    logger.warning(
                        "cannot parse file date %r, not using it as an anchor",
                        self.date,
                    )
                continue

            base_match = re.match(
                r"base\s+(?P<base>hex|dec)(?:\s+timestamps\s+"
                r"(?P<timestamp_format>absolute|relative))?",
                line,
                re.IGNORECASE,
            )
            if base_match:
                self.base = base_match.group("base")
                self._converted_base = self._check_base(self.base)
                self.timestamps_format = base_match.group("timestamp_format") or "absolute"
                continue

            events_match = re.match(
                r"(?P<no_events>no)?\s*internal\s+events\s+logged",
                line,
                re.IGNORECASE,
            )
            if events_match:
                self.internal_events_logged = events_match.group("no_events") is None
                continue

            trigger_start_match = ASC_TRIGGER_START_REGEX.match(line)
            if trigger_start_match:
                block_index += 1
                seen_block = True
                in_block = True
                last_offset = 0.0
                continuous = True
                datetime_string = trigger_start_match.group("datetime_string")
                if datetime_string is None:
                    anchor = None
                else:
                    try:
                        anchor = self._datetime_to_timestamp(datetime_string, self._timezone)
                    except ValueError:
                        logger.warning(
                            "cannot parse trigger block start %r, treating block %d as unanchored",
                            datetime_string,
                            block_index,
                        )
                        anchor = None
                continue

            if ASC_TRIGGER_END_REGEX.match(line):
                in_block = False
                anchor = None
                continue

            # Handle the "Start of measurement" line
            if re.match(r"^\d+\.\d+\s+Start of measurement", line):
                continue

            if not ASC_MESSAGE_REGEX.match(line):
                # line might be a comment, chip status,
                # J1939 message or some other unsupported event
                continue

            msg_kwargs: dict[str, float | bool | int] = {}
            try:
                raw_timestamp, channel, rest_of_message = line.split(None, 2)
                if self.timestamps_format == "relative":
                    # deltas from the preceding event within the block
                    offset = last_offset + float(raw_timestamp)
                else:
                    # offsets from the start of the trigger block
                    offset = float(raw_timestamp)
                msg_kwargs["timestamp"] = offset
                if channel == "CANFD":
                    msg_kwargs["is_fd"] = True
                elif channel.isdigit():
                    # See ASCWriter
                    msg_kwargs["channel"] = int(channel) - 1
                else:
                    # Not a CAN message. Possible values include "statistic",
                    # J1939TP
                    continue
            except ValueError:
                # Some other unprocessed or unknown format
                continue

            if not in_block:
                # frames outside any trigger block form an implicit block;
                # only the very first one may use the file date as anchor
                block_index += 1
                in_block = True
                last_offset = 0.0
                continuous = True
                anchor = header_anchor if not seen_block else None
                seen_block = True

            if offset < last_offset:
                # the block offsets went backwards, so the provably
                # continuous boundary of this block ends here
                continuous = False
            last_offset = offset

            if "is_fd" not in msg_kwargs:
                msg = self._process_classic_can_frame(rest_of_message, msg_kwargs)
            else:
                msg = self._process_fd_can_frame(rest_of_message, msg_kwargs)
            if msg is not None:
                yield block_index, anchor, offset, continuous, msg

        self.stop()


def _find_undetermined_segments(
    intervals: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Return the stretches of the time axis where the given anchored
    intervals contradict each other.

    Intervals connected by overlap form a cluster. Within a cluster the
    anchors cannot all be true at the same time, so every timestamp from
    the first point of contradiction (the second-earliest interval start)
    up to the furthest claimed interval end is undetermined.
    """
    if len(intervals) < 2:
        return []
    sorted_intervals = sorted(intervals)
    segments: list[tuple[float, float]] = []
    cluster_starts = [sorted_intervals[0][0]]
    cluster_end = sorted_intervals[0][1]
    for start, end in sorted_intervals[1:]:
        if start < cluster_end:
            cluster_starts.append(start)
            cluster_end = max(cluster_end, end)
        else:
            if len(cluster_starts) >= 2:
                segments.append((cluster_starts[1], cluster_end))
            cluster_starts = [start]
            cluster_end = end
    if len(cluster_starts) >= 2:
        segments.append((cluster_starts[1], cluster_end))
    return segments


class ASCTimeline:
    """Absolute and block-relative view of a multi-trigger-block ASC file.

    Use :meth:`from_file` to parse a file, then query frames either by
    absolute event time with :meth:`frames_by_event_time` or by
    block-relative time with :meth:`frames_by_block_time`.

    :ivar blocks: the trigger blocks of the file, in file order.
    :ivar frames: all frames of the file, in original file order and with
        their original block numbers.
    :ivar undetermined_segments: stretches of the absolute time axis where
        block anchors contradict each other, as ``(start, end)`` pairs of
        epoch seconds.
    """

    def __init__(
        self,
        blocks: tuple[TimelineBlock, ...],
        frames: tuple[TimelineFrame, ...],
        undetermined_segments: tuple[tuple[float, float], ...],
    ) -> None:
        self.blocks = blocks
        self.frames = frames
        self.undetermined_segments = undetermined_segments

    @classmethod
    def from_file(
        cls,
        file: StringPathLike | TextIO,
        base: str = "hex",
        tz: tzinfo | None = _LOCAL_TZ,
    ) -> "ASCTimeline":
        """Parse an ASC file into an :class:`ASCTimeline`.

        :param file:
            a path-like object or a file-like object to read from.
            If this is a file-like object, it must be opened in text
            read mode, not binary read mode.
        :param base:
            Select the base ('hex' or 'dec') for CAN IDs and data bytes.
            If the header of the ASC file contains base information,
            this value will be overwritten. Default is "hex".
        :param tz:
            Timezone for the absolute block start times. Defaults to the
            local timezone.
        """
        reader = _ASCTimelineReader(file, base=base, tz=tz)
        anchors: list[float | None] = []
        raw_blocks: list[list[tuple[float, bool, Message]]] = []
        for block_index, anchor, offset, continuous, msg in reader.iter_block_frames():
            while len(raw_blocks) <= block_index:
                raw_blocks.append([])
                anchors.append(None)
            anchors[block_index] = anchor
            raw_blocks[block_index].append((offset, continuous, msg))

        continuous_ends = []
        for raw_frames in raw_blocks:
            continuous_end = 0.0
            for offset, continuous, _msg in raw_frames:
                if continuous:
                    continuous_end = offset
            continuous_ends.append(continuous_end)

        intervals = [
            (anchor, anchor + continuous_end)
            for anchor, continuous_end in zip(anchors, continuous_ends, strict=True)
            if anchor is not None
        ]
        segments = _find_undetermined_segments(intervals)

        blocks = []
        frames = []
        for block_index, raw_frames in enumerate(raw_blocks):
            anchor = anchors[block_index]
            block_frames = []
            for offset, continuous, msg in raw_frames:
                timestamp = None if anchor is None else anchor + offset
                is_determined = (
                    timestamp is not None
                    and continuous
                    and not any(start <= timestamp <= end for start, end in segments)
                )
                frame = TimelineFrame(
                    block_index=block_index,
                    offset=offset,
                    timestamp=timestamp,
                    is_determined=is_determined,
                    message=msg,
                )
                block_frames.append(frame)
                frames.append(frame)
            blocks.append(
                TimelineBlock(
                    index=block_index,
                    anchor=anchor,
                    frames=tuple(block_frames),
                    continuous_end=continuous_ends[block_index],
                )
            )

        return cls(
            blocks=tuple(blocks),
            frames=tuple(frames),
            undetermined_segments=tuple(segments),
        )

    def frames_by_event_time(
        self,
        start: float,
        end: float,
        *,
        include_undetermined: bool = False,
    ) -> list[TimelineFrame]:
        """Return the frames located in the absolute time interval
        ``[start, end]`` (epoch seconds), in original file order.

        Only frames with a provable absolute timestamp are returned.
        Frames of unanchored blocks never appear here, since no absolute
        time can be determined for them. Frames whose anchors contradict
        each other (they fall into an undetermined segment) are skipped
        unless *include_undetermined* is set.

        :param start: start of the interval, in epoch seconds.
        :param end: end of the interval, in epoch seconds.
        :param include_undetermined: also return frames whose nominal
            timestamp falls into an undetermined segment.
        """
        if end < start:
            raise ValueError(f"end ({end}) must not be smaller than start ({start})")
        return [
            frame
            for frame in self.frames
            if frame.timestamp is not None
            and (include_undetermined or frame.is_determined)
            and start <= frame.timestamp <= end
        ]

    def frames_by_block_time(
        self,
        block_index: int,
        start: float | None = None,
        end: float | None = None,
    ) -> list[TimelineFrame]:
        """Return the frames of block *block_index* whose block-relative
        offset lies in ``[start, end]`` (seconds), in original file order.

        ``None`` bounds are open. The block-relative offsets are available
        for every block, including unanchored ones.

        :param block_index: number of the trigger block, in file order
            starting at 0.
        :param start: first block offset to include, or ``None``.
        :param end: last block offset to include, or ``None``.
        :raises IndexError: if there is no block with the given number.
        """
        if not 0 <= block_index < len(self.blocks):
            raise IndexError(f"unknown block index {block_index}")
        return [
            frame
            for frame in self.blocks[block_index].frames
            if (start is None or frame.offset >= start) and (end is None or frame.offset <= end)
        ]
