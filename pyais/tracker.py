"""AIS tracking functionality.The AISTracker maintains as collections
of known tracks and maintains the state of each vessel. This is
necessary because several messages can give different information
about a ship. In addition, the data changes constantly (position, speed).
Each track (or vessel) is solely identified by its MMSI.
"""
from enum import Enum
import typing
import time
import dataclasses
from pyais.messages import ANY_MESSAGE, AISSentence
from pyais.history import FieldObservation, TrackHistory, TrackHistoryTable, TrackState, is_unavailable


def now() -> float:
    """Current time as UNIX time (milliseconds)"""
    return time.time()


class AISTrackEvent(Enum):
    CREATED = 'created'
    UPDATED = 'updated'
    DELETED = 'deleted'


class AISUpdateBroker:
    """This class propagates updates to subscribers via callbacks."""

    def __init__(self) -> None:
        self._callbacks: typing.List[typing.Tuple[AISTrackEvent, typing.Any]] = []

    def attach(self, event: AISTrackEvent, callback: typing.Any) -> None:
        """Attach a new subscriber"""
        if callback not in self._callbacks:
            self._callbacks.append((event, callback))

    def detach(self, event: AISTrackEvent, callback: typing.Any) -> None:
        """Detach a subscriber"""
        try:
            self._callbacks.remove((event, callback))
        except ValueError:
            pass

    def propagate(self, track: 'AISTrack', event: AISTrackEvent) -> None:
        """Propagate a track event"""
        for destination, callback in self._callbacks:
            if event == destination:
                callback(track)


@dataclasses.dataclass(eq=True, order=True)
class AISTrack:
    """Each track holds some consolidated information about a vessel.
    Each vessel is uniquely identified by its MMSI. Tracks typically hold
    information from multiple messages that were received."""
    mmsi: int = dataclasses.field(compare=True)
    turn: typing.Optional[float] = dataclasses.field(compare=False, default=None)
    speed: typing.Optional[float] = dataclasses.field(compare=False, default=None)
    lon: typing.Optional[float] = dataclasses.field(compare=False, default=None)
    lat: typing.Optional[float] = dataclasses.field(compare=False, default=None)
    course: typing.Optional[float] = dataclasses.field(compare=False, default=None)
    heading: typing.Optional[int] = dataclasses.field(compare=False, default=None)
    imo: typing.Optional[int] = dataclasses.field(compare=False, default=None)
    callsign: typing.Optional[str] = dataclasses.field(compare=False, default=None)
    shipname: typing.Optional[str] = dataclasses.field(compare=False, default=None)
    ship_type: typing.Optional[int] = dataclasses.field(compare=False, default=None)
    to_bow: typing.Optional[int] = dataclasses.field(compare=False, default=None)
    to_stern: typing.Optional[int] = dataclasses.field(compare=False, default=None)
    to_port: typing.Optional[int] = dataclasses.field(compare=False, default=None)
    to_starboard: typing.Optional[int] = dataclasses.field(compare=False, default=None)
    destination: typing.Optional[str] = dataclasses.field(compare=False, default=None)
    last_updated: float = dataclasses.field(compare=False, default_factory=now)
    name: typing.Optional[str] = dataclasses.field(compare=False, default=None)
    ais_version: typing.Optional[int] = dataclasses.field(compare=False, default=None)
    ais_type: typing.Optional[str] = dataclasses.field(compare=False, default=None)
    status: typing.Optional[str] = dataclasses.field(compare=False, default=None)


# compute a set of all fields only once
FIELDS = dataclasses.fields(AISTrack)


def msg_to_track(msg: ANY_MESSAGE, ts_epoch_ms: typing.Optional[float] = None) -> AISTrack:
    """Convert a AIS message into a AISTrack.
    Only fields known to class AISTrack are considered.
    Depending on the type of the message, the implementation varies.
    ts_epoch_ms can be used as a timestamp for when the message was initially received.
    :param msg:         any decoded AIS message of type AISMessage.
    :param ts_epoch_ms: optional timestamp for the message. If None (default) current time is used."""
    if ts_epoch_ms is None:
        track = AISTrack(mmsi=msg.mmsi)
    else:
        track = AISTrack(mmsi=msg.mmsi, last_updated=ts_epoch_ms)

    for field in FIELDS:
        if not hasattr(msg, field.name):
            continue
        val = getattr(msg, field.name)
        if val is not None:
            setattr(track, field.name, val)

    return track


def update_track(old: AISTrack, new: AISTrack) -> AISTrack:
    """Updates all fields of old with the values of new.
    :param old: the old AISTrack to update.
    :param new: the new AISTrack to update old with."""
    for field in FIELDS:
        new_val = getattr(new, field.name)
        if new_val is not None:
            setattr(old, field.name, new_val)
    return old


def _tag_block_source(tag_block: typing.Any) -> typing.Optional[str]:
    """Best effort extraction of the source station from a tag block."""
    try:
        tag_block.init()
        return typing.cast(typing.Optional[str], tag_block.source_station)
    except Exception:
        return None


def poplast(dictionary: typing.Dict[typing.Any, typing.Any]) -> typing.Any:
    """Get the last item of a dict non-destructively (in terms of insertion order)."""
    # On Python3.8+ reversed(dict.items()) would do the job.
    # But by doing so, support for Python3.7 would have to be dropped.
    # The fastest and most memory efficient solution for that, is this little hack.
    key, latest = dictionary.popitem()
    dictionary[key] = latest
    return latest


class AISTracker:
    """
    An AIS tracker receives AIS messages and maintains a collection of known tracks.
    Everything is stored in memory.
    Messages are cleaned up regularly, if they expire (receive timestamp older than threshold).
    Set ttl_in_seconds to None to never clean up.
    Unlike most other trackers, this class handles out of order reception of messages.
    This means that it is possible to pass messages to update() whose timestamp is
    older that of the message before. The latter is useful when working with multiple stations
    and/or different kinds of metadata.
    """

    def __init__(
        self,
        ttl_in_seconds: typing.Optional[int] = 600,
        stream_is_ordered: bool = False,
        *,
        keep_history: bool = True,
        backfill: typing.Optional[bool] = None,
    ) -> None:
        """Creates a new tracker instance.
        :param ttl_in_seconds:      the ttl in seconds before expired tracks are pruned.
        :param stream_is_ordered:   set to True if the stream of messages arrives in order.
                                    This greatly increases the efficiency of cleanup() and n_latest_tracks().
                                    By default, both methods take O(N * log(N)) time.
                                    When stream_is_ordered is True, they take O(k) with k<=N time.
                                    So if you know that your messages are ordered after their timestamps,
                                    set stream_is_ordered to True.
        :param keep_history:        record every observation per field so that the effective
                                    state at any observation time can be queried. Enabled by default.
        :param backfill:            accept messages that are older than the newest observation of
                                    a track. Their fields fill in missing/older history, but never
                                    replace values that were observed later. ``None`` (default)
                                    accepts an older message only if it adds information (for
                                    example a field that was never observed before). Set to
                                    ``True`` to always backfill, or ``False`` to reject every older
                                    message.
        """
        self._tracks: typing.Dict[int, AISTrack] = {}  # { mmsi: AISTrack(), ...}
        self._history: typing.Dict[int, TrackHistory] = {}
        self.ttl_in_seconds: typing.Optional[int] = ttl_in_seconds  # in seconds or None
        self.stream_is_ordered: bool = stream_is_ordered
        self.keep_history: bool = keep_history
        self.backfill: typing.Optional[bool] = backfill
        self.oldest_timestamp: typing.Optional[float] = None
        self._broker = AISUpdateBroker()

    def __enter__(self) -> "AISTracker":
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        return None

    def __set_oldest_timestamp(self, ts: float) -> None:
        if self.oldest_timestamp is None:
            self.oldest_timestamp = ts
        else:
            self.oldest_timestamp = min(self.oldest_timestamp, ts)

    def _tracks_ordered_after_insertion(
        self
    ) -> typing.Union[typing.List[AISTrack], typing.Iterable[AISTrack]]:
        tracks: typing.Union[typing.List[AISTrack], typing.Iterable[AISTrack]]
        if self.stream_is_ordered:
            # From Python3.7 keys are ordered after insertion in dicts.
            # No need to sort.
            tracks = self._tracks.values()
        else:
            tracks = reversed(sorted(self._tracks.values(), key=lambda track: track.last_updated))
        return tracks

    @property
    def tracks(self) -> typing.List[AISTrack]:
        """Returns a list of all known tracks."""
        return list(self._tracks.values())

    def register_callback(
        self, event: AISTrackEvent, callback: typing.Callable[[AISTrack], typing.Any]
    ) -> None:
        """Register a callback that is called every time a specific event happens.
        The callback should be function that takes an AISTrack as a single argument."""
        self._broker.attach(event, callback)

    def remove_callback(
        self, event: AISTrackEvent, callback: typing.Callable[[AISTrack], typing.Any]
    ) -> None:
        """Remove a callback. Every callback is identified by its event and callback-function."""
        self._broker.detach(event, callback)

    def update(
        self,
        msg: typing.Union[AISSentence, ANY_MESSAGE],
        ts_epoch_ms: typing.Optional[float] = None,
        source: typing.Optional[str] = None,
    ) -> None:
        """Updates a track. If the track does not yet exist, a new track is created.
        NOTE: accepts raw AISSentences as well as decoded messages.

        :param msg: the message to add to the track.
        :param ts_epoch_ms: an optional timestamp to tell when the message was originally received.
        :param source: an optional source (for example the receiving station). If omitted,
                       the tag block source station of an ``AISSentence`` is used."""
        self._update(msg, ts_epoch_ms, source, None)

    def import_message(
        self,
        msg: typing.Union[AISSentence, ANY_MESSAGE],
        ts_epoch_ms: typing.Optional[float] = None,
        source: typing.Optional[str] = None,
    ) -> None:
        """Alias for :meth:`merge`."""
        self.merge(msg, ts_epoch_ms, source)

    def add(
        self,
        msg: typing.Union[AISSentence, ANY_MESSAGE],
        ts_epoch_ms: typing.Optional[float] = None,
        source: typing.Optional[str] = None,
    ) -> None:
        """Alias for :meth:`merge`."""
        self.merge(msg, ts_epoch_ms, source)

    def merge(
        self,
        msg: typing.Union[AISSentence, ANY_MESSAGE],
        ts_epoch_ms: typing.Optional[float] = None,
        source: typing.Optional[str] = None,
    ) -> None:
        """Import a message even if it is older than the newest observation.

        This is the permissive counterpart of :meth:`update`. Every field is
        merged independently by observation time: an older message may add
        history and fill in fields that were never seen, but it can never move
        a newer position or overwrite a newer value.
        """
        self._update(msg, ts_epoch_ms, source, True)

    def _update(
        self,
        msg: typing.Union[AISSentence, ANY_MESSAGE],
        ts_epoch_ms: typing.Optional[float],
        source: typing.Optional[str],
        backfill: typing.Optional[bool],
    ) -> None:
        tag_block = None
        if isinstance(msg, AISSentence):
            tag_block = msg.tag_block
            msg = msg.decode()

        if source is None and tag_block is not None:
            source = _tag_block_source(tag_block)

        mmsi = int(msg.mmsi)
        track = msg_to_track(msg, ts_epoch_ms)
        self.ensure_timestamp_constraints(track.last_updated, backfill)

        existing = self._tracks.get(mmsi)
        if existing is not None and track.last_updated < existing.last_updated:
            allowed = self._allows_backfill(mmsi, msg, track, backfill)
            if not allowed:
                raise ValueError('cannot update track with older message')

        # A message that is only accepted because it can add history must not
        # move the track to the end of an ordered stream or replace newer values.
        if existing is not None and track.last_updated < existing.last_updated:
            backfill = True

        if self.keep_history:
            self._record_message(mmsi, msg, track.last_updated, source)

        self.insert_or_update(mmsi, track, backfill=backfill)
        self.cleanup()

    def _allows_backfill(
        self,
        mmsi: int,
        msg: ANY_MESSAGE,
        new: AISTrack,
        backfill: typing.Optional[bool],
    ) -> bool:
        if backfill is not None:
            return backfill
        if self.backfill is not None:
            return self.backfill

        # Automatic mode: an older message is accepted when it can add
        # history, i.e. when one of its fields differs from the current
        # effective value. Replaying an identical message still raises.
        history = self._history.get(mmsi)
        if history is not None:
            state = history.state_at(None)
            for field in FIELDS:
                if field.name in ('mmsi', 'last_updated'):
                    continue
                if not hasattr(msg, field.name):
                    continue
                value = getattr(msg, field.name)
                if value is None:
                    continue

                # Any value that differs from the current effective value adds
                # history for its own observation time. This includes a field
                # that was never seen before and an older conflicting position.
                if field.name not in state:
                    return True
                incoming_unavailable = is_unavailable(
                    field.name, value, getattr(msg, 'msg_type', None)
                )
                if state.is_unavailable(field.name):
                    if not incoming_unavailable:
                        return True
                    continue
                if incoming_unavailable:
                    return True
                if value != state.raw_value(field.name):
                    return True
            return False

        # History disabled: only fill gaps that are currently None.
        old = self._tracks[mmsi]
        for field in FIELDS:
            if field.name in ('mmsi', 'last_updated'):
                continue
            if getattr(new, field.name) is not None and getattr(old, field.name) is None:
                return True
        return False

    def _record_message(
        self,
        mmsi: int,
        msg: ANY_MESSAGE,
        observed_at: float,
        source: typing.Optional[str],
    ) -> None:
        history = self._history.get(mmsi)
        if history is None:
            history = TrackHistory(mmsi)
            self._history[mmsi] = history
        history.merge_message(msg, observed_at=observed_at, source=source)

    @property
    def history_table(self) -> TrackHistoryTable:
        """The per-field history of every known vessel."""
        return TrackHistoryTable(self._history)

    @property
    def histories(self) -> TrackHistoryTable:
        """Alias for :attr:`history_table`."""
        return self.history_table

    def history(self, mmsi: typing.Union[str, int]) -> typing.Optional[TrackHistory]:
        """Return the field level history of a vessel, or None if it is unknown."""
        return self._history.get(int(mmsi))

    def get_history(self, mmsi: typing.Union[str, int]) -> typing.Optional[TrackHistory]:
        """Alias for :meth:`history`."""
        return self.history(mmsi)

    def state_at(self, mmsi: typing.Union[str, int], at: typing.Optional[float] = None) -> typing.Optional[TrackState]:
        """Return the effective field values of a vessel at observation time *at*.

        Fields first observed after *at* are omitted. Fields that were explicitly
        reported as unavailable are present with a value of ``None``. Conflicting
        observations from different sources are retained in ``TrackState.observations``.
        """
        history = self.history(mmsi)
        if history is None:
            return None
        return history.state_at(at)

    def values_at(
        self, mmsi: typing.Union[str, int], at: typing.Optional[float] = None
    ) -> typing.Optional[typing.Dict[str, typing.Any]]:
        """Return the effective field values of a vessel at observation time *at*."""
        state = self.state_at(mmsi, at)
        if state is None:
            return None
        return state.asdict()

    def observations_at(
        self, mmsi: typing.Union[str, int], at: typing.Optional[float] = None
    ) -> typing.Dict[str, typing.Tuple[FieldObservation, ...]]:
        """Return the effective observations of a vessel at observation time *at*.

        All observations that share the effective observation time are kept,
        so contradictory reports from different stations remain visible.
        """
        history = self.history(mmsi)
        if history is None:
            return {}
        return history.effective_observations_at(at)

    def get_observations(
        self, mmsi: typing.Union[str, int], at: typing.Optional[float] = None
    ) -> typing.Dict[str, typing.Tuple[FieldObservation, ...]]:
        """Alias for :meth:`observations_at`."""
        return self.observations_at(mmsi, at)

    def exact_observations_at(
        self, mmsi: typing.Union[str, int], at: float
    ) -> typing.Dict[str, typing.Tuple[FieldObservation, ...]]:
        """Return only observations taken exactly at observation time *at*."""
        history = self.history(mmsi)
        if history is None:
            return {}
        return history.exact_observations_at(at)

    def latest_state(self, mmsi: typing.Union[str, int]) -> typing.Optional[TrackState]:
        """Return the effective field values of a vessel after all observations."""
        return self.state_at(mmsi, None)

    def states_at(self, at: typing.Optional[float] = None) -> typing.Dict[int, TrackState]:
        """Return the effective state of every vessel at observation time *at*."""
        return self.history_table.states_at(at)

    def track_at(self, mmsi: typing.Union[str, int], at: typing.Optional[float] = None) -> typing.Optional[AISTrack]:
        """Return a consolidated track as it was visible at observation time *at*."""
        state = self.state_at(mmsi, at)
        if state is None:
            return None
        track = AISTrack(mmsi=int(mmsi))
        for field, value in state.items():
            if field in ('mmsi', 'last_updated'):
                continue
            setattr(track, field, value)
        if state.last_updated is not None:
            track.last_updated = state.last_updated
        return track

    def ensure_timestamp_constraints(self, ts_epoch_ms: float, backfill: typing.Optional[bool] = None) -> None:
        """Ensures that tracks are ordered. Only relevant is stream_is_ordered is True."""
        if not self.stream_is_ordered or not self._tracks:
            return
        if self.backfill is True or backfill is True:
            return

        # Get the newest track
        latest = poplast(self._tracks)

        if ts_epoch_ms < latest.last_updated:
            # The new track must be inserted after the latest one.
            raise ValueError(
                'can not insert an older timestamp in a ordered stream.'
                f' {ts_epoch_ms} < {latest.last_updated}.'
                ' consider setting stream_is_ordered to False.'
            )

    def get_track(self, mmsi: typing.Union[str, int]) -> typing.Optional[AISTrack]:
        """Get a track by mmsi. Returns None if the track does not exist."""
        try:
            return self._tracks[int(mmsi)]
        except KeyError:
            return None

    def pop_track(self, mmsi: typing.Union[str, int]) -> typing.Optional[AISTrack]:
        """Pop a track by mmsi. Returns the track and deletes it, if it exist. Otherwise returns None."""
        try:
            mmsi = int(mmsi)
            track = self._tracks[mmsi]
            del self._tracks[mmsi]
            self._history.pop(mmsi, None)
            self._broker.propagate(track, AISTrackEvent.DELETED)
            return track
        except KeyError:
            return None

    def n_latest_tracks(self, n: int) -> typing.List[AISTrack]:
        """Return the latest N tracks. These are the tracks with the youngest timestamps.
        E.g. the tracks that were updated most recently."""
        n_latest = []
        n = min(n, len(self._tracks))

        tracks = self._tracks_ordered_after_insertion()

        for i, track in enumerate(tracks):
            if n <= i:
                break
            n_latest.append(track)

        return n_latest

    def insert_or_update(self, mmsi: int, track: AISTrack, backfill: typing.Optional[bool] = None) -> None:
        """Insert or update a track."""
        # Does the track already exist?
        if mmsi in self._tracks:
            self.update_track(mmsi, track, backfill=backfill)
        else:
            self.insert_track(mmsi, track)
        self.__set_oldest_timestamp(track.last_updated)

    def insert_track(self, mmsi: int, new: AISTrack) -> None:
        """Creates a new track records in memory"""
        self._tracks[mmsi] = new
        self._broker.propagate(new, AISTrackEvent.CREATED)

    def update_track(
        self,
        mmsi: int,
        new: AISTrack,
        backfill: typing.Optional[bool] = None,
    ) -> None:
        """Updates an existing track in memory.

        A message that is older than the newest observation of the track is
        only accepted when ``backfill`` is enabled (or the tracker was created
        with ``backfill=True``). In that case it fills in fields that are
        missing (or older), but it can never replace a value that was observed
        later. The position/state of the track is always derived from the
        per-field history, so a late message can not move a newer position.
        """
        old = self._tracks[mmsi]
        if new.last_updated < old.last_updated:
            allowed = self.backfill if backfill is None else backfill
            if not allowed:
                raise ValueError('cannot update track with older message')
            updated = self._merge_backfill(mmsi, old, new)
        else:
            updated = update_track(old, new)

        # Neat little trick to keep tracks ordered after timestamp.
        # A backfilled observation must not move the track to the end.
        if new.last_updated >= old.last_updated:
            del self._tracks[mmsi]
        self._tracks[mmsi] = updated
        self._broker.propagate(updated, AISTrackEvent.UPDATED)

    def _merge_backfill(self, mmsi: int, old: AISTrack, new: AISTrack) -> AISTrack:
        """Merge an older message into a track without moving newer values."""
        history = self._history.get(mmsi)
        if history is None:
            # History is disabled: an older message may only fill in gaps.
            for field in FIELDS:
                if field.name in ('mmsi', 'last_updated'):
                    continue
                if getattr(old, field.name) is None:
                    value = getattr(new, field.name)
                    if value is not None:
                        setattr(old, field.name, value)
            return old

        state = history.state_at(None)
        last_updated = old.last_updated
        if state.last_updated is not None:
            last_updated = max(last_updated, state.last_updated)
        merged = AISTrack(mmsi=old.mmsi, last_updated=last_updated)
        for field in FIELDS:
            if field.name in ('mmsi', 'last_updated'):
                continue
            if field.name not in state.primary:
                continue
            setattr(merged, field.name, state.raw_value(field.name))
        return merged

    def cleanup(self) -> None:
        """Delete all records whose last update is older than ttl."""
        if self.ttl_in_seconds is None or self.oldest_timestamp is None:
            return

        t = now()
        # the oldest track is still younger than the ttl
        if (t - self.ttl_in_seconds) < self.oldest_timestamp:
            return

        to_be_deleted = set()
        tracks = self._tracks_ordered_after_insertion()

        for track in tracks:
            if (t - track.last_updated) < self.ttl_in_seconds:
                self.oldest_timestamp = track.last_updated
                break
            # ttl is over. delete it.
            to_be_deleted.add(track.mmsi)

        for mmsi in to_be_deleted:
            self.pop_track(mmsi)
