"""
This module contains the generic :class:`LogReader` as
well as :class:`MessageSync` which plays back messages
in the recorded order and time intervals.
"""

import bisect
import gzip
import math
import pathlib
import time
from collections.abc import Generator, Iterable, Sequence
from dataclasses import dataclass
from typing import (
    Any,
    Final,
    NamedTuple,
)

from .._entry_points import read_entry_points
from ..message import Message
from ..typechecking import StringPathLike
from .asc import ASCReader
from .blf import BLFReader
from .canutils import CanutilsLogReader
from .csv import CSVReader
from .generic import BinaryIOMessageReader, MessageReader, TextIOMessageReader
from .mf4 import MF4Reader
from .sqlite import SqliteReader
from .trc import TRCReader

#: A map of file suffixes to their corresponding
#: :class:`can.io.generic.MessageReader` class
MESSAGE_READERS: Final[dict[str, type[MessageReader]]] = {
    ".asc": ASCReader,
    ".blf": BLFReader,
    ".csv": CSVReader,
    ".db": SqliteReader,
    ".log": CanutilsLogReader,
    ".mf4": MF4Reader,
    ".trc": TRCReader,
}


def _update_reader_plugins() -> None:
    """Update available message reader plugins from entry points."""
    for entry_point in read_entry_points("can.io.message_reader"):
        if entry_point.key in MESSAGE_READERS:
            continue

        reader_class = entry_point.load()
        if issubclass(reader_class, MessageReader):
            MESSAGE_READERS[entry_point.key] = reader_class


def _get_logger_for_suffix(suffix: str) -> type[MessageReader]:
    """Find MessageReader class for given suffix."""
    try:
        return MESSAGE_READERS[suffix]
    except KeyError:
        raise ValueError(f'No read support for unknown log format "{suffix}"') from None


def _decompress(filename: StringPathLike, **kwargs: Any) -> MessageReader:
    """
    Return the suffix and io object of the decompressed file.
    """
    suffixes = pathlib.Path(filename).suffixes
    if len(suffixes) != 2:
        raise ValueError(f'No read support for unknown log format "{"".join(suffixes)}"')

    real_suffix = suffixes[-2].lower()
    reader_type = _get_logger_for_suffix(real_suffix)

    if issubclass(reader_type, TextIOMessageReader):
        return reader_type(gzip.open(filename, mode="rt"), **kwargs)
    elif issubclass(reader_type, BinaryIOMessageReader):
        return reader_type(gzip.open(filename, mode="rb"), **kwargs)

    raise ValueError(f'No read support for unknown log format "{"".join(suffixes)}"')


def LogReader(filename: StringPathLike, **kwargs: Any) -> MessageReader:  # noqa: N802
    """Find and return the appropriate :class:`~can.io.generic.MessageReader` instance
    for a given file suffix.

    The format is determined from the file suffix which can be one of:
      * .asc :class:`can.ASCReader`
      * .blf :class:`can.BLFReader`
      * .csv :class:`can.CSVReader`
      * .db :class:`can.SqliteReader`
      * .log :class:`can.CanutilsLogReader`
      * .mf4 :class:`can.MF4Reader`
        (optional, depends on `asammdf <https://github.com/danielhrisca/asammdf>`_)
      * .trc :class:`can.TRCReader`

    Gzip compressed files can be used as long as the original
    files suffix is one of the above (e.g. filename.asc.gz).


    Exposes a simple iterator interface, to use simply::

        for msg in can.LogReader("some/path/to/my_file.log"):
            print(msg)

    :param filename:
        the filename/path of the file to read from
    :raises ValueError:
        if the filename's suffix is of an unknown file type

    .. note::
        There are no time delays, if you want to reproduce the measured
        delays between messages look at the :class:`can.MessageSync` class.

    .. note::
        This function itself is just a dispatcher, and any positional and keyword
        arguments are passed on to the returned instance.
    """

    _update_reader_plugins()

    suffix = pathlib.PurePath(filename).suffix.lower()
    if suffix == ".gz":
        return _decompress(filename)

    reader_type = _get_logger_for_suffix(suffix)
    return reader_type(file=filename, **kwargs)


_LOOKUP_EPSILON = 1e-12


class RecordedRange(NamedTuple):
    """A span of recorded sample times.

    A playback instant that maps to a single sample time has ``start == end``.
    A playback instant that represents a compressed silence expands to the
    complete affected recorded interval instead.
    """

    start: float
    end: float

    @property
    def duration(self) -> float:
        """Length of the range in the recorded timeline."""
        return self.end - self.start

    @property
    def recorded_start(self) -> float:
        """Alias for :attr:`start`."""
        return self.start

    @property
    def recorded_end(self) -> float:
        """Alias for :attr:`end`."""
        return self.end

    @property
    def is_instant(self) -> bool:
        """Whether the range describes a single sample time."""
        return self.start == self.end

    @property
    def expands_compressed_window(self) -> bool:
        """Whether this range represents a compressed silence."""
        return self.start != self.end


@dataclass(frozen=True, slots=True)
class PlaybackWindow:
    """A span of recorded silence that was shortened during playback.

    ``recorded_start`` and ``recorded_end`` are absolute message timestamps.
    ``playback_start`` and ``playback_end`` are offsets from the beginning of
    playback.  The omitted part of the pause is represented with
    ``playback_start == playback_end``.
    """

    recorded_start: float
    recorded_end: float
    playback_start: float
    playback_end: float

    @property
    def compressed(self) -> bool:
        """Whether this window was collapsed to a single playback instant."""
        return self.playback_start == self.playback_end

    @property
    def playback_time(self) -> float:
        """Playback instant at which this window begins."""
        return self.playback_start

    @property
    def playback(self) -> float:
        """Alias for :attr:`playback_time`."""
        return self.playback_start

    @property
    def point(self) -> float:
        """Alias for :attr:`playback_time`."""
        return self.playback_start

    @property
    def start(self) -> float:
        """Alias for :attr:`recorded_start`."""
        return self.recorded_start

    @property
    def end(self) -> float:
        """Alias for :attr:`recorded_end`."""
        return self.recorded_end

    @property
    def recorded_duration(self) -> float:
        """Duration of the window in the recorded timeline."""
        return self.recorded_end - self.recorded_start

    @property
    def playback_duration(self) -> float:
        """Duration of the window in the playback timeline."""
        return self.playback_end - self.playback_start


class PlaybackTimeline:
    """Shared mapping between recorded sample time and replayed time.

    The timeline is piecewise linear.  Ordinary pauses are kept in full, so
    recorded time and playback time advance at the same rate.  A pause longer
    than ``skip`` is shortened: at most ``skip`` seconds of it are replayed
    linearly and the remainder is represented by a :class:`PlaybackWindow`
    that maps to a single playback instant.  Every window is retained,
    including consecutive ones.

    The same object answers both directions:

    * :meth:`to_playback_time` maps a recorded sample time to the earliest
      playback instant at which it is replayed.
    * :meth:`to_recorded_range` maps a playback instant back to the recorded
      span it represents.  For a compressed silence this is the original
      interval, not merely its first frame.

    :class:`MessageSync` schedules playback through this object and exposes it
    as :attr:`MessageSync.timeline`, so playback and analysis cannot drift
    apart.
    """

    def __init__(
        self,
        recorded_start: float | None = None,
        skip: float | None = None,
        *,
        timestamps: bool = True,
        gap: float = 0.0001,
    ) -> None:
        """
        :param recorded_start:
            Timestamp of the first recorded message.  When omitted, the first
            call to :meth:`add_frame` establishes it.
        :param skip:
            Longest pause that is retained between two frames, in seconds.
            ``None`` disables compression, ``0`` collapses every non-empty
            pause completely, and a positive value clamps each pause to that
            many seconds.  :class:`MessageSync` treats its own ``skip=0`` as
            "disabled" for backwards compatibility.
        :param timestamps:
            Whether recorded timestamps drive the timeline.  If ``False``,
            playback advances by *gap* per frame instead.
        :param gap:
            Playback step used when *timestamps* is ``False``.
        """
        if recorded_start is not None and not math.isfinite(recorded_start):
            raise ValueError("recorded_start must be finite")
        if skip is not None and (skip < 0 or not math.isfinite(skip)):
            raise ValueError("skip must be a finite, non-negative number")
        if gap < 0 or not math.isfinite(gap):
            raise ValueError("gap must be a finite, non-negative number")

        self.recorded_start = recorded_start
        self.playback_origin = 0.0
        self.skip = skip
        self.timestamps = timestamps
        self.gap = gap

        self._windows: list[PlaybackWindow] = []
        self._window_starts: list[float] = []
        self._window_ends: list[float] = []
        self._window_playback: list[float] = []
        #: Total compressed duration before each window (prefix sums).
        self._omitted_prefix: list[float] = [0.0]
        #: Frames that move backwards in recorded time need an exact lookup.
        self._exact_recorded: dict[float, float] = {}
        #: Playback instants shared by several frames, for reverse lookup.
        self._shared_playback: dict[float, tuple[float, float]] = {}
        self._shared_backward: dict[float, tuple[float, float]] = {}

        self._frame_count = 0
        self._max_recorded = recorded_start if recorded_start is not None else 0.0
        self._first_recorded: float | None = None
        self._previous_playback: float | None = None
        self._previous_recorded: float | None = None
        self._last_playback = 0.0

    @property
    def windows(self) -> tuple[PlaybackWindow, ...]:
        """All shortened silence windows, in recorded order."""
        return tuple(self._windows)

    @property
    def compressed_windows(self) -> tuple[PlaybackWindow, ...]:
        """Alias for :attr:`windows`; every window represents shortened silence."""
        return tuple(self._windows)

    @property
    def frame_count(self) -> int:
        """Number of frames added to this timeline."""
        return self._frame_count

    @property
    def playback_duration(self) -> float:
        """Playback offset of the most recently added frame."""
        return self._last_playback

    @property
    def recorded_duration(self) -> float:
        """Recorded span covered by this timeline, in seconds."""
        if self.recorded_start is None or self._frame_count == 0:
            return 0.0
        return self._max_recorded - self.recorded_start

    @classmethod
    def from_messages(cls, messages: Iterable[Message], **kwargs: Any) -> "PlaybackTimeline":
        """Build a timeline from an iterable of messages without replaying them."""
        timeline = cls(**kwargs)
        for message in messages:
            timeline.add_frame(message.timestamp)
        return timeline

    def add_frame(self, sample_time: float) -> float:
        """Record one frame and return its playback offset in seconds.

        Frames that share a sample time receive the same playback time and keep
        their input order.  A frame that moves backwards in recorded time never
        rewinds the playback clock.

        :param sample_time:
            Absolute timestamp of the frame in the recording.
        :return:
            Playback time in seconds, relative to the first frame.
        """
        if not math.isfinite(sample_time):
            raise ValueError("sample_time must be finite")

        if not self.timestamps:
            return self._add_gap_frame(sample_time)

        if self._frame_count == 0:
            if self.recorded_start is None:
                self.recorded_start = sample_time
            # Playback time is measured from the first replayed frame, which
            # may differ from an explicitly supplied recorded_start.
            self._first_recorded = sample_time
            self.playback_origin = sample_time - self.recorded_start
            self._max_recorded = sample_time
            return self._finish_frame(sample_time, 0.0)

        if sample_time < self._max_recorded:
            # Out-of-order frames are replayed immediately, without rewinding
            # the shared playback clock.
            playback = self._last_playback
            self._exact_recorded.setdefault(sample_time, playback)
            current = self._shared_backward.get(playback)
            if current is None:
                self._shared_backward[playback] = (sample_time, sample_time)
            else:
                self._shared_backward[playback] = (
                    min(current[0], sample_time),
                    max(current[1], sample_time),
                )
        elif sample_time == self._max_recorded:
            # Consecutive frames at one sample time keep their input order.
            playback = self._last_playback
        else:
            gap = sample_time - self._max_recorded
            if self.skip is None or gap <= self.skip:
                playback = self._last_playback + gap
            elif self.skip == 0:
                self._add_window(self._max_recorded, sample_time, self._last_playback)
                playback = self._last_playback
            else:
                # The first ``skip`` seconds are replayed normally; the
                # remainder of the pause is the compressed window.
                playback = self._last_playback + self.skip
                self._add_window(self._max_recorded + self.skip, sample_time, playback)
            self._max_recorded = sample_time

        return self._finish_frame(sample_time, playback)

    def _add_gap_frame(self, sample_time: float) -> float:
        """Add a frame when playback is driven by a fixed gap."""
        if self.recorded_start is None:
            self.recorded_start = sample_time
        self._max_recorded = max(self._max_recorded, sample_time)
        # Fixed-gap playback waits before every frame, including the first.
        playback = self._last_playback + self.gap
        return self._finish_frame(sample_time, playback)

    def _finish_frame(self, sample_time: float, playback: float) -> float:
        if self._previous_playback is not None and self._previous_recorded is not None:
            if playback == self._previous_playback:
                current = self._shared_playback.get(
                    playback, (self._previous_recorded, self._previous_recorded)
                )
                self._shared_playback[playback] = (
                    min(current[0], sample_time),
                    max(current[1], sample_time),
                )
        self._previous_playback = playback
        self._previous_recorded = sample_time
        self._last_playback = playback
        self._frame_count += 1
        if self.recorded_start is None:
            self.recorded_start = sample_time
        if self._first_recorded is None:
            self._first_recorded = sample_time
        return playback

    def _add_window(self, recorded_start: float, recorded_end: float, playback: float) -> None:
        omitted_before = self._omitted_prefix[-1]
        self._windows.append(
            PlaybackWindow(
                recorded_start=recorded_start,
                recorded_end=recorded_end,
                playback_start=playback,
                playback_end=playback,
            )
        )
        self._window_starts.append(recorded_start)
        self._window_ends.append(recorded_end)
        self._window_playback.append(playback)
        self._omitted_prefix.append(omitted_before + (recorded_end - recorded_start))

    def _omitted_before(self, recorded: float) -> float:
        """Total omitted duration before an absolute recorded time."""
        index = bisect.bisect_right(self._window_starts, recorded) - 1
        if index < 0:
            return 0.0
        if recorded <= self._window_ends[index]:
            # Inside a compressed window: nothing linear can be omitted there.
            return self._omitted_prefix[index] + (recorded - self._window_starts[index])
        return self._omitted_prefix[index + 1]

    def to_playback_time(self, recorded_time: float) -> float | None:
        """Map a recorded sample time to its earliest playback instant.

        :param recorded_time:
            Absolute timestamp in the recording, using the same clock as the
            messages that were added to this timeline.
        :return:
            Playback offset in seconds, or ``None`` if *recorded_time* lies
            after the recorded data or playback is driven by
            ``timestamps=False``.  Times before the recording start map to
            ``0.0``.
        :raises ValueError:
            If *recorded_time* is not finite.
        """
        if not math.isfinite(recorded_time):
            raise ValueError("recorded_time must be finite")
        if not self.timestamps:
            # With fixed-gap playback there is no function of recorded time:
            # only frame positions carry a playback instant.
            return None
        if self.recorded_start is None or self._frame_count == 0:
            return None
        if self._first_recorded is None:
            return None

        backward = self._exact_recorded.get(recorded_time)
        if backward is not None:
            return backward

        if recorded_time <= self._first_recorded:
            return 0.0
        if recorded_time > self._max_recorded:
            return None

        index = bisect.bisect_right(self._window_starts, recorded_time) - 1
        if index >= 0 and recorded_time <= self._window_ends[index]:
            return self._window_playback[index]

        return (
            recorded_time
            - self.recorded_start
            - self.playback_origin
            - self._omitted_before(recorded_time)
        )

    def to_recorded_range(self, playback_time: float) -> RecordedRange | None:
        """Map a playback instant back to the recorded span it represents.

        A compressed silence returns its complete recorded interval even though
        it occupies a single playback instant.  An ordinary instant returns a
        range whose start and end are equal.

        :param playback_time:
            Playback offset in seconds, relative to the first frame.
        :return:
            The locatable recorded range, or ``None`` if the playback instant
            is outside the timeline, belongs to a backward-moving frame, or
            playback is driven by ``timestamps=False``.
        :raises ValueError:
            If *playback_time* is not finite.
        """
        if not math.isfinite(playback_time):
            raise ValueError("playback_time must be finite")
        if not self.timestamps:
            return None
        if self.recorded_start is None or self._frame_count == 0:
            return None
        if playback_time < -_LOOKUP_EPSILON:
            return None
        if playback_time > self._last_playback + _LOOKUP_EPSILON:
            return None

        # All windows that collapse onto this instant describe one recorded
        # interval.  Consecutive collapsed pauses therefore merge instead of
        # arbitrarily selecting the first frame.  This takes precedence over
        # point frames so a fully compressed pause never shrinks to its first
        # frame.
        index = bisect.bisect_left(self._window_playback, playback_time)
        while (
            index < len(self._window_playback)
            and abs(self._window_playback[index] - playback_time) <= _LOOKUP_EPSILON
        ):
            start = self._window_starts[index]
            end = self._window_ends[index]
            while (
                index + 1 < len(self._window_playback)
                and abs(self._window_playback[index + 1] - playback_time) <= _LOOKUP_EPSILON
            ):
                index += 1
                end = max(end, self._window_ends[index])
            shared = self._shared_playback.get(playback_time)
            if shared is not None:
                start = min(start, shared[0])
                end = max(end, shared[1])
            backward = self._shared_backward.get(playback_time)
            if backward is not None:
                start = min(start, backward[0])
                end = max(end, backward[1])
            return RecordedRange(start, end)

        # Frames that share a playback instant keep a single locatable point.
        shared = self._shared_playback.get(playback_time)
        if shared is not None:
            return RecordedRange(shared[0], shared[1])
        backward = self._shared_backward.get(playback_time)
        if backward is not None:
            return RecordedRange(backward[0], backward[1])

        omitted = 0.0
        earlier = bisect.bisect_right(self._window_playback, playback_time)
        if earlier:
            omitted = self._omitted_prefix[earlier]
        recorded = self.recorded_start + self.playback_origin + playback_time + omitted
        return RecordedRange(recorded, recorded)

    def window_at(self, playback_time: float) -> PlaybackWindow | None:
        """Return the shortened pause active at a playback instant, if any."""
        if not math.isfinite(playback_time):
            raise ValueError("playback_time must be finite")
        index = bisect.bisect_left(self._window_playback, playback_time)
        if index < len(self._window_playback) and (
            abs(self._window_playback[index] - playback_time) <= _LOOKUP_EPSILON
        ):
            return self._windows[index]
        return None

    def recorded_to_playback(self, recorded_time: float) -> float | None:
        """Alias for :meth:`to_playback_time`."""
        return self.to_playback_time(recorded_time)

    def playback_to_recorded(self, playback_time: float) -> RecordedRange | None:
        """Alias for :meth:`to_recorded_range`."""
        return self.to_recorded_range(playback_time)


class MessageSync:
    """
    Used to iterate over some given messages in the recorded time.
    """

    def __init__(
        self,
        messages: Iterable[Message],
        timestamps: bool = True,
        gap: float = 0.0001,
        skip: float = 60.0,
    ) -> None:
        """Creates an new **MessageSync** instance.

        :param messages: An iterable of :class:`can.Message` instances.
        :param timestamps: Use the messages' timestamps. If False, uses the *gap* parameter
                           as the time between messages.
        :param gap: Minimum time between sent messages in seconds
        :param skip: Skip periods of inactivity greater than this (in seconds).

        Example::

            import can

            with can.LogReader("my_logfile.asc") as reader, can.Bus(interface="virtual") as bus:
                for msg in can.MessageSync(messages=reader):
                    print(msg)
                    bus.send(msg)

        """
        self.raw_messages = messages
        self.timestamps = timestamps
        self.gap = gap
        self.skip = skip
        self._timeline: PlaybackTimeline | None = None
        self._prepared_messages: list[Message] | None = None
        self._prepared_schedule: list[float] | None = None
        if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes, bytearray)):
            # A concrete playback list can be analyzed before it is replayed.
            # Build the timeline once and reuse the computed schedule.
            prepared = list(messages)
            timeline = self._new_timeline()
            schedule = [timeline.add_frame(message.timestamp) for message in prepared]
            self._prepared_messages = prepared
            self._prepared_schedule = schedule
            self._timeline = timeline

    @property
    def timeline(self) -> PlaybackTimeline:
        """The mapping used for the most recent or upcoming playback.

        The timeline exposes the played schedule together with the window of
        every shortened pause.  It is the same object used for both the
        recorded-to-playback and playback-to-recorded directions.

        For a concrete sequence such as a list, the mapping is available
        immediately without consuming the playback iterator.
        """
        if self._timeline is None:
            self._timeline = self._new_timeline()
        return self._timeline

    @property
    def windows(self) -> tuple[PlaybackWindow, ...]:
        """Every shortened pause, in recorded order."""
        return self.timeline.windows

    @property
    def mapping(self) -> PlaybackTimeline:
        """Alias for :attr:`timeline`."""
        return self.timeline

    @property
    def compressed_windows(self) -> tuple[PlaybackWindow, ...]:
        """Alias for :attr:`windows`."""
        return self.timeline.windows

    def to_playback_time(self, recorded_time: float) -> float | None:
        """Map a recorded sample time to its playback instant."""
        return self.timeline.to_playback_time(recorded_time)

    def to_recorded_range(self, playback_time: float) -> RecordedRange | None:
        """Map a playback instant back to its locatable recorded range."""
        return self.timeline.to_recorded_range(playback_time)

    def recorded_to_playback(self, recorded_time: float) -> float | None:
        """Alias for :meth:`to_playback_time`."""
        return self.timeline.to_playback_time(recorded_time)

    def playback_to_recorded(self, playback_time: float) -> RecordedRange | None:
        """Alias for :meth:`to_recorded_range`."""
        return self.timeline.to_recorded_range(playback_time)

    def window_at(self, playback_time: float) -> PlaybackWindow | None:
        """Return the compressed pause active at a playback instant, if any."""
        return self.timeline.window_at(playback_time)

    def _new_timeline(self) -> PlaybackTimeline:
        # ``skip=0`` has always meant "do not skip any pauses" for MessageSync.
        return PlaybackTimeline(
            skip=self.skip or None,
            timestamps=self.timestamps,
            gap=self.gap,
        )

    def __iter__(self) -> Generator[Message, None, None]:
        if self._prepared_messages is not None and self._prepared_schedule is not None:
            prepared = zip(self._prepared_messages, self._prepared_schedule, strict=True)
            pairs: Iterable[tuple[Message, float]] = prepared
        else:
            # A new pass always starts from a fresh mapping.  Keeping the
            # previous pass's frames would make repeated iteration drift.
            timeline = self._timeline = self._new_timeline()
            pairs = (
                (message, timeline.add_frame(message.timestamp)) for message in self.raw_messages
            )

        playback_start_time = time.perf_counter()

        for message, playback in pairs:
            sleep_period = playback_start_time + playback - time.perf_counter()

            if sleep_period > 1e-4:
                time.sleep(sleep_period)

            yield message
