"""
Reconstruction and alignment of two independently compressed record volumes.

A device writes the time column and the value column into two independent
compressed volumes. Each volume has its own boundaries, so the number of rows
in the two volumes may differ. Every row only carries a one-byte wrapping
counter in the range 0..255; volume headers occasionally carry a complete,
absolute sequence number called an *anchor*. Rows may be missing (dropped) or
retransmitted (duplicated), independently on each side, as long as the number
of consecutive missing rows stays within a configurable budget (``max_gap``).

The machinery in this module joins the two columns back into events in a
single sequential pass. Only the rows of the current unanchored frame (at most
one counter cycle, plus retransmissions) are buffered, so the inputs may be
lazy iterators, for example generators reading from compressed files opened
with :mod:`xopen`.

Sequence numbers are reconstructed from counter *deltas* rather than from the
counter value itself:

* a counter that repeats (delta 0) is a retransmission of the same absolute
  sequence number, so the position does not advance -- a duplicated ``255``
  therefore cannot shift the following wrap by one position;
* a counter advance of ``j`` (1 <= j <= max_gap + 1) means ``j - 1`` missing
  rows;
* a larger jump cannot be explained within the budget: the affected region is
  flagged for review until the next anchor resynchronises the stream.

An unanchored frame shorter than one full counter cycle has a unique
alignment: when the next anchor arrives, every buffered row receives its
absolute sequence number. Once a frame spans a full counter cycle without an
anchor, two different wrap counts are both legal, so the frame is released
immediately with status :data:`Event.AMBIGUOUS` for manual review and a fresh
frame starts at the following row. A later anchor therefore always restores
unambiguous reconstruction for the rows after the ambiguous region.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import ClassVar, Deque, Iterable, Iterator, Literal

__all__ = [
    "MISSING",
    "AnchorMismatch",
    "Event",
    "Observation",
    "Row",
    "SequenceReconstructor",
    "align_events",
]

MOD = 256

RowStatus = Literal["ok", "ambiguous"]
EventStatus = Literal[
    "ok", "missing_time", "missing_value", "ambiguous", "retransmitted"
]


class _Missing:
    """Sentinel for a column that has no counterpart in the other volume."""

    _instance: "_Missing | None" = None

    def __new__(cls) -> "_Missing":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()


class AnchorMismatch(ValueError):
    """Raised when an anchor contradicts the counter stream or an earlier anchor."""


@dataclass
class Observation:
    """One row as read sequentially from a compressed volume.

    ``counter`` is the one-byte wrapping counter (0..255), ``value`` is the
    column payload (a timestamp or a measurement) and ``anchor`` is the
    complete absolute sequence number when this row is an anchor row,
    otherwise ``None``.
    """

    counter: int
    value: object = None
    anchor: int | None = None


@dataclass
class Row:
    """A reconstructed row from one volume.

    ``seq`` is the absolute sequence number. It is ``None`` only for rows of a
    frame that never saw an anchor, whose absolute offset therefore cannot be
    determined at all; such rows always have ``status`` "ambiguous".
    """

    seq: int | None
    counter: int
    value: object
    status: RowStatus = "ok"
    anchor: int | None = None
    note: str | None = None


@dataclass
class Event:
    """One aligned event joining the time and the value columns."""

    seq: int | None
    time: object
    value: object
    status: EventStatus
    note: str | None = None

    OK: ClassVar[EventStatus] = "ok"
    MISSING_TIME: ClassVar[EventStatus] = "missing_time"
    MISSING_VALUE: ClassVar[EventStatus] = "missing_value"
    AMBIGUOUS: ClassVar[EventStatus] = "ambiguous"
    RETRANSMITTED: ClassVar[EventStatus] = "retransmitted"


def _validate_counter(counter: int) -> None:
    if not isinstance(counter, int) or not 0 <= counter < MOD:
        raise ValueError(f"counter must be an integer in 0..255, got {counter!r}")


class SequenceReconstructor:
    """Turn the wrapping counters of one volume into absolute sequence numbers.

    Feed the rows of one volume in arrival order via :meth:`feed`, which
    returns the rows that have become final. Rows are final immediately while
    the stream is anchored; before the first anchor (and after an
    unexplainable gap) they are buffered internally and released in order when
    the next anchor resolves them or when :meth:`finish` is called at the end
    of the volume.
    """

    def __init__(self, max_gap: int):
        if not isinstance(max_gap, int) or max_gap < 0:
            raise ValueError("max_gap must be a non-negative integer")
        if max_gap >= MOD - 1:
            raise ValueError(
                "max_gap must be smaller than 255 to keep alignments unique"
            )
        self.max_gap = max_gap
        self._locked = False
        self._seq: int | None = None
        self._prev_counter: int | None = None
        self._relative = 0
        self._buffer: Deque[Row] = deque()

    @property
    def buffered(self) -> int:
        """Number of rows held while awaiting an anchor."""
        return len(self._buffer)

    def _delta(self, counter: int) -> int | None:
        """Counter delta to the next row, or ``None`` if it exceeds the budget."""
        assert self._prev_counter is not None
        jump = (counter - self._prev_counter) % MOD
        if jump == 0:
            return 0
        if jump <= self.max_gap + 1:
            return jump
        return None

    def _open_frame(self, counter: int) -> None:
        """Start a fresh unanchored frame at ``counter``."""
        self._locked = False
        self._relative = 0
        self._prev_counter = counter
        self._buffer.append(Row(seq=None, counter=counter, value=None, status="ok"))

    def _release_ambiguous(self, note: str) -> list[Row]:
        """Release the current frame as a region that cannot be aligned."""
        for row in self._buffer:
            row.status = "ambiguous"
            if row.note is None:
                row.note = note
        return self._finalise_buffer()

    def _finalise_buffer(self) -> list[Row]:
        """Release every buffered row; callers mark its status beforehand."""
        rows: list[Row] = []
        while self._buffer:
            rows.append(self._buffer.popleft())
        return rows

    def _assign_buffered(self, anchor: int, anchor_relative: int) -> list[Row]:
        """Give every buffered row its absolute sequence number from the anchor.

        ``anchor_relative`` is the walked position of the anchor row inside the
        current frame, so the frame offset is ``anchor - anchor_relative``.
        Frames reaching this method are always shorter than one counter cycle
        (longer frames are released for review as soon as they cross it), so
        the resulting alignment is unique.
        """
        offset = anchor - anchor_relative
        walked = 0
        previous: int | None = None
        rows: list[Row] = []
        while self._buffer:
            row = self._buffer.popleft()
            if previous is not None:
                walked += (row.counter - previous) % MOD
            row.seq = offset + walked
            row.status = "ok"
            row.note = None
            rows.append(row)
            previous = row.counter
        return rows

    def _anchor(self, obs: Observation) -> list[Row]:
        anchor = obs.anchor
        assert anchor is not None
        if not isinstance(anchor, int) or anchor < 0:
            raise AnchorMismatch(
                f"anchor must be a non-negative integer, got {anchor!r}"
            )
        if anchor % MOD != obs.counter:
            raise AnchorMismatch(
                f"anchor {anchor} is inconsistent with counter {obs.counter}"
            )

        released: list[Row] = []
        if self._prev_counter is None:
            self._locked = True
            self._seq = anchor
            self._prev_counter = obs.counter
            return [Row(anchor, obs.counter, obs.value, "ok", anchor=anchor)]

        delta = self._delta(obs.counter)
        if self._locked:
            assert self._seq is not None
            if delta is None or self._seq + delta != anchor:
                raise AnchorMismatch(
                    f"anchor {anchor} does not continue the established sequence "
                    f"(last sequence number {self._seq})"
                )
            self._seq = anchor
            self._prev_counter = obs.counter
            return [Row(anchor, obs.counter, obs.value, "ok", anchor=anchor)]

        if delta is None:
            # The anchor itself is separated by a gap beyond the budget: it
            # cannot resolve the rows before the gap. Release them for review
            # and start a fresh locked frame at the anchor.
            released.extend(
                self._release_ambiguous(
                    "unresolved frame: next anchor is beyond max_gap"
                )
            )
            self._locked = True
            self._seq = anchor
            self._relative = 0
            self._prev_counter = obs.counter
            released.append(
                Row(
                    anchor,
                    obs.counter,
                    obs.value,
                    "ok",
                    anchor=anchor,
                    note="resynchronised after a gap beyond max_gap",
                )
            )
            return released

        anchor_relative = self._relative + delta
        released.extend(self._assign_buffered(anchor, anchor_relative))
        self._locked = True
        self._seq = anchor
        self._relative = 0
        self._prev_counter = obs.counter
        released.append(Row(anchor, obs.counter, obs.value, "ok", anchor=anchor))
        return released

    def feed(self, obs: Observation) -> list[Row]:
        """Process one :class:`Observation`; return the rows that are now final."""
        _validate_counter(obs.counter)
        if obs.anchor is not None:
            return self._anchor(obs)

        counter = obs.counter
        if self._prev_counter is None:
            self._open_frame(counter)
            self._buffer[-1].value = obs.value
            return []

        delta = self._delta(counter)
        if delta is None:
            jump = (counter - self._prev_counter) % MOD
            note = (
                f"counter jump of {jump} implies {jump - 1} missing rows, "
                f"exceeding max_gap={self.max_gap}; awaiting next anchor"
            )
            released: list[Row] = []
            if self._buffer:
                released.extend(
                    self._release_ambiguous("frame interrupted by a gap beyond max_gap")
                )
            self._open_frame(counter)
            self._buffer[-1].value = obs.value
            self._buffer[-1].note = note
            return released

        if self._locked:
            assert self._seq is not None
            self._seq += delta
            self._prev_counter = counter
            return [Row(self._seq, counter, obs.value, "ok")]

        released: list[Row] = []
        self._relative += delta
        self._prev_counter = counter
        if self._relative >= MOD:
            # Two wrap counts now fit every observed counter, so the frame can
            # never be aligned uniquely. Release it for review and keep this
            # row as the first row of a fresh frame, which the next anchor can
            # still resolve.
            released.extend(
                self._release_ambiguous("unanchored frame spans a full counter cycle")
            )
            self._open_frame(counter)
        else:
            self._buffer.append(
                Row(seq=None, counter=counter, value=obs.value, status="ok")
            )
            return released
        self._buffer[-1].value = obs.value
        return released

    def finish(self) -> list[Row]:
        """Release all rows still held at the end of the volume."""
        if not self._buffer:
            return []
        for row in self._buffer:
            row.status = "ambiguous"
            if row.note is None:
                row.note = "no anchor before end of volume"
        return self._finalise_buffer()


class _Channel:
    """Lazy adapter: observations in, final rows out."""

    def __init__(self, observations: Iterable[Observation], max_gap: int):
        self._iterator = iter(observations)
        self.reconstructor = SequenceReconstructor(max_gap)
        self.buffer: Deque[Row] = deque()
        self._finished = False

    def _refill(self) -> None:
        while not self.buffer and not self._finished:
            try:
                observation = next(self._iterator)
            except StopIteration:
                self.buffer.extend(self.reconstructor.finish())
                self._finished = True
                return
            self.buffer.extend(self.reconstructor.feed(observation))

    def head(self) -> Row | None:
        self._refill()
        return self.buffer[0] if self.buffer else None

    def pop(self) -> Row:
        return self.buffer.popleft()


def _single_side_event(row: Row, side: Literal["time", "value"]) -> Event:
    ambiguous = row.status == "ambiguous"
    if side == "time":
        status = Event.AMBIGUOUS if ambiguous else Event.MISSING_VALUE
        return Event(row.seq, row.value, MISSING, status, row.note)
    status = Event.AMBIGUOUS if ambiguous else Event.MISSING_TIME
    return Event(row.seq, MISSING, row.value, status, row.note)


def _drain(channel: _Channel, side: Literal["time", "value"]) -> Iterator[Event]:
    while channel.head() is not None:
        yield _single_side_event(channel.pop(), side)


def _merge_locked(time_channel: _Channel, value_channel: _Channel) -> Iterator[Event]:
    """Merge one step where both head rows carry absolute sequence numbers."""
    time_row = time_channel.head()
    value_row = value_channel.head()
    assert time_row is not None and value_row is not None
    assert time_row.seq is not None and value_row.seq is not None

    if time_row.seq < value_row.seq:
        yield _single_side_event(time_channel.pop(), "time")
        return
    if time_row.seq > value_row.seq:
        yield _single_side_event(value_channel.pop(), "value")
        return

    seq = time_row.seq
    time_group: list[Row] = []
    value_group: list[Row] = []
    while (row := time_channel.head()) is not None and row.seq == seq:
        time_group.append(time_channel.pop())
    while (row := value_channel.head()) is not None and row.seq == seq:
        value_group.append(value_channel.pop())

    notes = [row.note for row in time_group + value_group if row.note]
    note = "; ".join(notes) if notes else None
    yield Event(seq, time_group[0].value, value_group[0].value, Event.OK, note)
    for row in time_group[1:]:
        yield Event(
            seq,
            row.value,
            MISSING,
            Event.RETRANSMITTED,
            row.note,
        )
    for row in value_group[1:]:
        yield Event(
            seq,
            MISSING,
            row.value,
            Event.RETRANSMITTED,
            row.note,
        )


def _take_ambiguous_run(channel: _Channel) -> list[Row]:
    run: list[Row] = []
    while (row := channel.head()) is not None and row.status == "ambiguous":
        run.append(channel.pop())
    return run


def _walk_positions(rows: list[Row]) -> list[int]:
    positions: list[int] = []
    walked = 0
    previous: int | None = None
    for row in rows:
        if previous is not None:
            walked += (row.counter - previous) % MOD
        positions.append(walked)
        previous = row.counter
    return positions


def _merge_ambiguous(
    time_channel: _Channel, value_channel: _Channel
) -> Iterator[Event]:
    """Emit ambiguous frame runs on either side, pairing rows that walk together."""
    time_rows = _take_ambiguous_run(time_channel)
    value_rows = _take_ambiguous_run(value_channel)
    time_positions = _walk_positions(time_rows)
    value_positions = _walk_positions(value_rows)
    time_index = 0
    value_index = 0

    while time_index < len(time_rows) or value_index < len(value_rows):
        if time_index >= len(time_rows):
            yield _single_side_event(value_rows[value_index], "value")
            value_index += 1
            continue
        if value_index >= len(value_rows):
            yield _single_side_event(time_rows[time_index], "time")
            time_index += 1
            continue

        time_row = time_rows[time_index]
        value_row = value_rows[value_index]
        if (
            time_row.counter == value_row.counter
            and time_positions[time_index] == value_positions[value_index]
        ):
            seq = time_row.seq if time_row.seq is not None else value_row.seq
            notes = [note for note in (time_row.note, value_row.note) if note]
            yield Event(
                seq,
                time_row.value,
                value_row.value,
                Event.AMBIGUOUS,
                "; ".join(notes) if notes else None,
            )
            time_index += 1
            value_index += 1
            continue
        if time_positions[time_index] <= value_positions[value_index]:
            yield _single_side_event(time_row, "time")
            time_index += 1
        else:
            yield _single_side_event(value_row, "value")
            value_index += 1


def align_events(
    times: Iterable[Observation],
    values: Iterable[Observation],
    max_gap: int,
) -> Iterator[Event]:
    """Join the time and value volumes into a stream of :class:`Event` objects.

    ``times`` and ``values`` are iterables of :class:`Observation` in the order
    in which the rows were written to the respective volume. ``max_gap`` is the
    maximum number of consecutive missing rows that may be explained on either
    side. The function performs a single sequential pass over both iterables
    and only buffers the current unanchored frame (less than one counter
    cycle) per volume.
    """
    time_channel = _Channel(times, max_gap)
    value_channel = _Channel(values, max_gap)

    while True:
        time_row = time_channel.head()
        value_row = value_channel.head()
        if time_row is None and value_row is None:
            return
        if time_row is None:
            yield from _drain(value_channel, "value")
            return
        if value_row is None:
            yield from _drain(time_channel, "time")
            return

        if time_row.status == "ambiguous" or value_row.status == "ambiguous":
            yield from _merge_ambiguous(time_channel, value_channel)
        else:
            yield from _merge_locked(time_channel, value_channel)
