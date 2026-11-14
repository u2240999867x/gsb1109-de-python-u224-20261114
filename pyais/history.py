"""Field level state history for AIS tracks.

A late (out of order) message must not overwrite information that was
observed later.  At the same time it may still fill in fields that have
never been seen before.  ``TrackHistory`` therefore stores observations
per field instead of storing a single consolidated track record.

Every observation keeps:

* the *observation time* (the time at which the value was valid),
* the *source* (for example the receiving station),
* whether the message explicitly marked the value as unavailable.

A field that is absent from a message does not create an observation at
all.  This is deliberately different from a field that is present but
carries an AIS "not available" sentinel value.
"""
import dataclasses
import time
import typing

from pyais.messages import ANY_MESSAGE

if typing.TYPE_CHECKING:
    from pyais.tracker import AISTrack


class _Unavailable:
    """Sentinel used to record an explicitly unavailable value."""

    __slots__ = ()

    def __repr__(self) -> str:
        return 'UNAVAILABLE'

    def __bool__(self) -> bool:
        return False


#: Sentinel for records that explicitly say "this field is not available".
UNAVAILABLE = _Unavailable()


# Fields in which an empty string is the AIS representation of
# "no information available" instead of a valid value.
_TEXT_TRACK_FIELDS = frozenset({
    'callsign',
    'destination',
    'name',
    'shipname',
})

# Fields where zero is the AIS representation of "not available".
_ZERO_UNAVAILABLE_FIELDS = frozenset({
    'imo',
    'ship_type',
    'to_bow',
    'to_stern',
    'to_port',
    'to_starboard',
})


def classify_availability(field: str, value: typing.Any, msg_type: typing.Optional[int] = None) -> str:
    """Return ``'available'``, ``'unavailable'`` or ``'absent'`` for a decoded field.

    ``None`` means that the field is not part of the message at all.
    """
    if value is None:
        return 'absent'
    if value is UNAVAILABLE:
        return 'unavailable'
    return 'unavailable' if is_unavailable(field, value, msg_type) else 'available'


def is_unavailable(
    field: str,
    value: typing.Any,
    msg_type: typing.Optional[int] = None,
) -> bool:
    """Return True if *value* is an explicit AIS "not available" marker.

    ``None`` means that the field is not part of the message at all and is
    therefore reported as absent rather than as explicitly unavailable.
    ``msg_type`` optionally selects message-type specific sentinels (e.g.
    speed is encoded in knots instead of deciknots for message type 9).
    """
    if value is None:
        return False

    if field in _TEXT_TRACK_FIELDS:
        return isinstance(value, str) and value.strip() == ''

    if field == 'status':
        try:
            return int(value) == 15
        except (TypeError, ValueError):
            return False

    if field == 'turn':
        try:
            return abs(float(value)) >= 127.0
        except (TypeError, ValueError):
            return False

    if field == 'speed':
        try:
            speed = float(value)
        except (TypeError, ValueError):
            return False
        if msg_type == 27:
            return speed >= 63.0
        if msg_type == 9:
            return speed >= 1023.0
        return speed >= 102.3

    if field == 'lon':
        try:
            return abs(float(value)) > 180.0
        except (TypeError, ValueError):
            return False

    if field == 'lat':
        try:
            return abs(float(value)) > 90.0
        except (TypeError, ValueError):
            return False

    if field == 'course':
        try:
            return float(value) >= 360.0
        except (TypeError, ValueError):
            return False

    if field == 'heading':
        try:
            return int(value) == 511
        except (TypeError, ValueError):
            return False

    if field in _ZERO_UNAVAILABLE_FIELDS:
        try:
            return int(value) == 0
        except (TypeError, ValueError):
            return False

    if field == 'ais_version':
        # 0 is a valid AIS version, not a "not available" marker.
        return False

    return False


@dataclasses.dataclass(frozen=True)
class FieldObservation:
    """A single observed value of one field.

    ``observed_at`` is the time at which the value was valid.  ``received_at``
    and ``sequence`` describe when the observation arrived and are only used
    to resolve competing observations that share the same observation time.
    """
    field: str
    value: typing.Any
    observed_at: float
    source: typing.Optional[str] = None
    available: bool = True
    received_at: float = dataclasses.field(default_factory=time.time)
    sequence: int = 0

    @property
    def effective_at(self) -> float:
        """Alias for the observation time."""
        return self.observed_at

    @property
    def observation_time(self) -> float:
        """Alias for the observation time."""
        return self.observed_at

    @property
    def station(self) -> typing.Optional[str]:
        """Alias for :attr:`source`."""
        return self.source

    @property
    def unavailable(self) -> bool:
        """Alias for :attr:`is_unavailable`."""
        return not self.available

    @property
    def valid(self) -> bool:
        """Whether the observation carries a usable value."""
        return self.available

    @property
    def availability(self) -> str:
        """``'available'`` or ``'unavailable'`` for this observation."""
        return 'available' if self.available else 'unavailable'

    @property
    def field_value(self) -> typing.Any:
        """Alias for :attr:`value`."""
        return self.value

    @property
    def normalized(self) -> typing.Any:
        """The value as it should appear in a state snapshot.

        Explicitly unavailable values are represented by ``None``.  The raw
        AIS sentinel is still available through :attr:`value`.
        """
        return self.value if self.available else None

    @property
    def is_unavailable(self) -> bool:
        """Whether the message explicitly marked this field as unavailable."""
        return not self.available

    def asdict(self) -> typing.Dict[str, typing.Any]:
        """Return a plain dictionary representation."""
        return {
            'field': self.field,
            'value': self.value,
            'observed_at': self.observed_at,
            'source': self.source,
            'available': self.available,
            'received_at': self.received_at,
            'sequence': self.sequence,
        }


# ``Observation`` is a convenient short alias.
Observation = FieldObservation


class TrackState(typing.Dict[str, typing.Any]):
    """The effective field values of a track at one observation time.

    The object behaves like a normal ``dict`` of field name to value.
    Fields that the track never carried do not appear at all.  Fields that
    were explicitly reported as unavailable appear with a value of ``None``
    and are listed in :attr:`unavailable`.  If multiple sources reported
    conflicting values at the effective observation time, all observations
    are retained in :attr:`observations` and reported by :attr:`conflicts`.
    """

    def __init__(
        self,
        values: typing.Optional[typing.Mapping[str, typing.Any]] = None,
        *,
        mmsi: typing.Optional[int] = None,
        requested_at: typing.Optional[float] = None,
        last_updated: typing.Optional[float] = None,
        primary: typing.Optional[typing.Dict[str, FieldObservation]] = None,
        observations: typing.Optional[typing.Dict[str, typing.Tuple[FieldObservation, ...]]] = None,
        unavailable: typing.FrozenSet[str] = frozenset(),
    ) -> None:
        super().__init__(values or {})
        self.mmsi = mmsi
        self.requested_at = requested_at
        self.last_updated = last_updated
        self.primary: typing.Dict[str, FieldObservation] = primary or {}
        self.observations: typing.Dict[str, typing.Tuple[FieldObservation, ...]] = observations or {}
        self.unavailable: typing.FrozenSet[str] = unavailable

    def __repr__(self) -> str:
        return f"TrackState<{dict(self)!r}>"

    @property
    def provenance(self) -> typing.Dict[str, typing.Tuple[FieldObservation, ...]]:
        """Alias for :attr:`observations`."""
        return self.observations

    @property
    def conflicts(self) -> typing.Dict[str, typing.Tuple[FieldObservation, ...]]:
        """Fields that have more than one distinct effective value.

        Competing observations that carry the same value but originate from
        different stations are not conflicts.  Contradictory valid values are
        never discarded: they are returned here together with their sources.
        """
        result: typing.Dict[str, typing.Tuple[FieldObservation, ...]] = {}
        for field, observations in self.observations.items():
            valid = [observation for observation in observations if observation.available]
            if len(valid) < 2:
                continue
            first = valid[0].normalized
            if any(observation.normalized != first for observation in valid[1:]):
                result[field] = observations
        return result

    def all(self, field: str) -> typing.Tuple[FieldObservation, ...]:
        """All effective observations for *field* at this time."""
        return self.observations.get(field, ())

    def field(self, field: str) -> FieldObservation:
        """The primary observation for *field*."""
        return self.primary[field]

    def source(self, field: str) -> typing.Optional[str]:
        """The source of the primary observation for *field*."""
        return self.primary[field].source

    def sources(self, field: str) -> typing.Tuple[typing.Optional[str], ...]:
        """All sources that reported an effective value for *field*."""
        return tuple(observation.source for observation in self.observations.get(field, ()))

    def is_unavailable(self, field: str) -> bool:
        """Whether *field* is explicitly unavailable (but present) in this state."""
        return field in self.unavailable

    def availability(self, field: str) -> str:
        """Return ``'available'``, ``'unavailable'`` or ``'absent'`` for *field*.

        This makes the difference between a message that explicitly marked a
        value as "not available" and a message that simply did not carry the
        field at all.
        """
        if field not in self:
            return 'absent'
        if field in self.unavailable:
            return 'unavailable'
        return 'available'

    @property
    def unavailable_fields(self) -> typing.FrozenSet[str]:
        """All fields that are explicitly unavailable in this state."""
        return self.unavailable

    @property
    def available(self) -> typing.Dict[str, typing.Any]:
        """Only the fields with a usable value."""
        return {field: value for field, value in self.items() if field not in self.unavailable}

    def has(self, field: str) -> bool:
        """Whether *field* was observed at all (available or explicitly unavailable)."""
        return field in self

    @property
    def absent(self) -> typing.FrozenSet[str]:
        """Track fields that were never observed up to the requested time.

        These fields differ from entries in :attr:`unavailable`: an absent field
        is not part of any message seen so far, whereas an unavailable field
        was explicitly reported as "not available" by the sender.
        """
        from pyais.tracker import FIELDS

        known = {field.name for field in FIELDS} - {'mmsi', 'last_updated'}
        return frozenset(known - set(self))

    @property
    def raw(self) -> typing.Dict[str, typing.Any]:
        """The raw observed values, including explicit AIS sentinels."""
        return {field: observation.value for field, observation in self.primary.items()}

    def raw_value(self, field: str) -> typing.Any:
        """The raw observed value of *field*, including explicit AIS sentinels."""
        return self.primary[field].value

    def entries(self, field: str) -> typing.Tuple[typing.Tuple[typing.Any, typing.Optional[str]], ...]:
        """All effective (value, source) pairs for *field*.

        This makes conflicting reports from different stations directly
        inspectable without discarding either provenance.
        """
        return tuple(
            (observation.normalized, observation.source)
            for observation in self.observations.get(field, ())
        )

    def asdict(self) -> typing.Dict[str, typing.Any]:
        """Return the effective field values as a plain dictionary."""
        return dict(self)

    def primary_value(self, field: str) -> typing.Any:
        """The normalized effective value of *field* (``None`` if unavailable)."""
        return self.primary[field].normalized


class TrackHistory:
    """A field level history of the state of one vessel (one MMSI).

    Observations are merged per field.  The observation with the youngest
    ``observed_at`` wins for that field, no matter in which order the
    observations arrived.  Observations with the same observation time are
    all kept, so that contradictory reports from different stations remain
    visible.
    """

    def __init__(self, mmsi: typing.Optional[int] = None) -> None:
        self.mmsi: typing.Optional[int] = int(mmsi) if mmsi is not None else None
        self._fields: typing.Dict[str, typing.List[FieldObservation]] = {}
        self._sequence = 0
        self._revision = 0
        self._state_cache: typing.Dict[typing.Optional[float], TrackState] = {}

    def __repr__(self) -> str:
        return f"TrackHistory<mmsi={self.mmsi}, fields={self.fields}>"

    def __len__(self) -> int:
        return len(self._fields)

    def __iter__(self) -> typing.Iterator[str]:
        return iter(self.fields)

    def __contains__(self, field: object) -> bool:
        return field in self._fields

    def __getitem__(self, field: str) -> typing.Tuple[FieldObservation, ...]:
        return self.history(field)

    @property
    def fields(self) -> typing.Tuple[str, ...]:
        """Names of all fields that have at least one observation."""
        return tuple(self._fields)

    @property
    def last_updated(self) -> typing.Optional[float]:
        """The youngest observation time across all fields."""
        latest: typing.Optional[float] = None
        for observations in self._fields.values():
            if observations:
                value = observations[-1].observed_at
                if latest is None or value > latest:
                    latest = value
        return latest

    def _check_mmsi(self, mmsi: typing.Union[int, str]) -> int:
        value = int(mmsi)
        if self.mmsi is None:
            self.mmsi = value
        elif self.mmsi != value:
            raise ValueError(f"history belongs to MMSI {self.mmsi}, not {value}")
        return value

    def record(
        self,
        field: str,
        value: typing.Any,
        observed_at: typing.Optional[float] = None,
        source: typing.Optional[str] = None,
        available: typing.Optional[bool] = None,
        received_at: typing.Optional[float] = None,
    ) -> FieldObservation:
        """Record one observation of *field*.

        ``available`` may be used to override the automatic detection of AIS
        "not available" sentinel values.  Passing ``value=None`` with
        ``available=True`` records an ordinary value; a field that is not
        part of a message must simply not be recorded.
        """
        if observed_at is None:
            observed_at = time.time()
        if value is UNAVAILABLE:
            value = None
            available = False
        if available is None:
            available = not is_unavailable(field, value)

        observations = self._fields.setdefault(field, [])
        insert_at = self._insertion_index(observations, observed_at)

        # Replaying the same sample is idempotent: an identical observation
        # from the same source at the same time is not stored twice.
        index = insert_at - 1
        while index >= 0 and observations[index].observed_at == observed_at:
            existing = observations[index]
            if existing.source == source and existing.available == available and existing.value == value:
                return existing
            index -= 1

        observation = FieldObservation(
            field=field,
            value=value,
            observed_at=float(observed_at),
            source=source,
            available=bool(available),
            received_at=time.time() if received_at is None else float(received_at),
            sequence=self._sequence,
        )
        self._sequence += 1
        observations.insert(insert_at, observation)
        if self._state_cache:
            self._state_cache.clear()
        self._revision += 1
        return observation

    @staticmethod
    def _insertion_index(observations: typing.List[FieldObservation], observed_at: float) -> int:
        """Find the index after the last observation with observed_at <= the new time."""
        low = 0
        high = len(observations)
        while low < high:
            middle = (low + high) // 2
            if observations[middle].observed_at <= observed_at:
                low = middle + 1
            else:
                high = middle
        return low

    def merge_values(
        self,
        values: typing.Mapping[str, typing.Any],
        observed_at: typing.Optional[float] = None,
        source: typing.Optional[str] = None,
        available: typing.Optional[bool] = None,
        *,
        explicit_none: bool = False,
    ) -> typing.Dict[str, FieldObservation]:
        """Merge a mapping of field names to values.

        By default keys with a value of ``None`` are ignored because they
        represent fields that are not part of the message.  With
        ``explicit_none=True`` a present key with a ``None`` value records an
        explicitly unavailable value instead.
        """
        result: typing.Dict[str, FieldObservation] = {}
        for field, value in values.items():
            available_for_field: typing.Optional[bool] = available
            if value is None:
                if not explicit_none:
                    continue
                available_for_field = False
            elif value is UNAVAILABLE:
                available_for_field = False
            result[field] = self.record(field, value, observed_at, source, available_for_field)
        return result

    def add(
        self,
        record: typing.Mapping[str, typing.Any],
        source: typing.Optional[str] = None,
        observed_at: typing.Optional[float] = None,
    ) -> FieldObservation:
        """Add one field-level record.

        The record must contain a ``field`` and a ``value`` key. ``mmsi``,
        ``observed_at``, ``source`` and ``available`` are optional; explicitly
        passed arguments take precedence. A value of :data:`UNAVAILABLE` marks
        the field as explicitly not available.
        """
        field = record['field']
        value = record.get('value')
        at = record.get('observed_at', observed_at)
        station = record.get('source', source)
        available = record.get('available')
        if at is None:
            at = time.time()
        return self.record(
            str(field),
            value,
            at,
            station,
            available,
            record.get('received_at'),
        )

    def merge_message(
        self,
        msg: ANY_MESSAGE,
        observed_at: typing.Optional[float] = None,
        source: typing.Optional[str] = None,
    ) -> typing.Dict[str, FieldObservation]:
        """Merge all track-relevant fields that are present on a decoded message."""
        # Imported lazily to avoid a circular import at module load time.
        from pyais.tracker import FIELDS

        self._check_mmsi(msg.mmsi)
        result: typing.Dict[str, FieldObservation] = {}
        for field in FIELDS:
            if field.name in ('mmsi', 'last_updated'):
                continue
            if not hasattr(msg, field.name):
                continue
            value = getattr(msg, field.name)
            if value is None:
                continue
            available = not is_unavailable(field.name, value, getattr(msg, 'msg_type', None))
            result[field.name] = self.record(field.name, value, observed_at, source, available)
        return result

    def merge_track(
        self,
        track: 'AISTrack',
        observed_at: typing.Optional[float] = None,
        source: typing.Optional[str] = None,
    ) -> typing.Dict[str, FieldObservation]:
        """Merge all fields of a consolidated :class:`~pyais.tracker.AISTrack`.

        Merging a track can not distinguish "absent" from "not available",
        because a consolidated track uses ``None`` for both. Fields that are
        ``None`` are therefore skipped rather than recorded as unavailable.
        """
        from pyais.tracker import FIELDS

        self._check_mmsi(track.mmsi)
        if observed_at is None:
            observed_at = track.last_updated
        result: typing.Dict[str, FieldObservation] = {}
        for field in FIELDS:
            if field.name in ('mmsi', 'last_updated'):
                continue
            value = getattr(track, field.name)
            if value is None:
                continue
            result[field.name] = self.record(field.name, value, observed_at, source)
        return result

    def merge(
        self,
        observations: typing.Union[
            FieldObservation,
            typing.Mapping[str, typing.Any],
            typing.Iterable[FieldObservation],
            'AISTrack',
            ANY_MESSAGE,
        ],
        observed_at: typing.Optional[float] = None,
        source: typing.Optional[str] = None,
        available: typing.Optional[bool] = None,
        *,
        explicit_none: bool = False,
    ) -> typing.Dict[str, FieldObservation]:
        """Merge observations from a mapping, an iterable, a message or a track."""
        if isinstance(observations, FieldObservation):
            recorded = self.record(
                observations.field,
                observations.value,
                observations.observed_at,
                observations.source,
                observations.available,
                observations.received_at,
            )
            return {observations.field: recorded}

        if hasattr(observations, 'mmsi') and hasattr(observations, 'last_updated'):
            return self.merge_track(typing.cast('AISTrack', observations), observed_at, source)

        if hasattr(observations, 'mmsi') and hasattr(observations, 'msg_type'):
            return self.merge_message(typing.cast(ANY_MESSAGE, observations), observed_at, source)

        if isinstance(observations, typing.Mapping):
            return self.merge_values(typing.cast(typing.Mapping[str, typing.Any], observations),
                                     observed_at, source, available, explicit_none=explicit_none)

        merged: typing.Dict[str, FieldObservation] = {}
        for observation in observations:
            merged[observation.field] = self.record(
                observation.field,
                observation.value,
                observation.observed_at,
                observation.source,
                observation.available,
                observation.received_at,
            )
        return merged

    def add_sample(self, sample: typing.Mapping[str, typing.Any]) -> typing.Dict[str, FieldObservation]:
        """Import one de-identified sample row.

        A sample looks like::

            {'mmsi': 123456789, 'observed_at': 1000.0, 'source': 'station-a',
             'fields': {'lat': 1.0, 'lon': 2.0, 'status': None}}

        Fields inside ``fields`` are merged per field.  A field that is not
        present at all stays unknown; a field with a ``None`` value (or the
        :data:`UNAVAILABLE` sentinel) is explicitly recorded as unavailable.
        """
        content = sample.get('fields', sample)
        at = sample.get('observed_at', sample.get('timestamp', sample.get('ts')))
        station = sample.get('source', sample.get('station'))
        if 'mmsi' in sample:
            self._check_mmsi(sample['mmsi'])
        if at is None:
            at = time.time()
        return self.merge_values(content, at, station, explicit_none=True)

    def history(self, field: str) -> typing.Tuple[FieldObservation, ...]:
        """All observations of *field* ordered by observation time."""
        return tuple(self._fields.get(field, ()))

    def field_history(self, field: str) -> typing.Tuple[FieldObservation, ...]:
        """Alias for :meth:`history`."""
        return self.history(field)

    def observations(self, field: str) -> typing.Tuple[FieldObservation, ...]:
        """Alias for :meth:`history`."""
        return self.history(field)

    def _rightmost_index(self, observations: typing.List[FieldObservation], at: float) -> int:
        """Index of the last observation with observed_at <= at, or -1."""
        low = 0
        high = len(observations)
        while low < high:
            middle = (low + high) // 2
            if observations[middle].observed_at <= at:
                low = middle + 1
            else:
                high = middle
        return low - 1

    def effective_observations_at(
        self, at: typing.Optional[float] = None
    ) -> typing.Dict[str, typing.Tuple[FieldObservation, ...]]:
        """Return the effective observations of every field at observation time *at*.

        Every entry contains all observations that share the youngest
        observation time not later than *at*.  Hence conflicting reports from
        multiple stations remain available.  Fields first observed after *at*
        are omitted.
        """
        if at is None:
            at = float('inf')
        result: typing.Dict[str, typing.Tuple[FieldObservation, ...]] = {}
        for field, observations in self._fields.items():
            index = self._rightmost_index(observations, at)
            if index < 0:
                continue
            latest_at = observations[index].observed_at
            start = index
            while start > 0 and observations[start - 1].observed_at == latest_at:
                start -= 1
            result[field] = tuple(observations[start:index + 1])
        return result

    def observations_at(
        self, at: typing.Optional[float] = None
    ) -> typing.Dict[str, typing.Tuple[FieldObservation, ...]]:
        """Alias for :meth:`effective_observations_at`."""
        return self.effective_observations_at(at)

    def exact_observations_at(self, at: float) -> typing.Dict[str, typing.Tuple[FieldObservation, ...]]:
        """Return only observations taken exactly at *at*, grouped by field."""
        result: typing.Dict[str, typing.Tuple[FieldObservation, ...]] = {}
        for field, observations in self._fields.items():
            matching = tuple(observation for observation in observations if observation.observed_at == at)
            if matching:
                result[field] = matching
        return result

    def values_at(self, at: typing.Optional[float] = None) -> typing.Dict[str, typing.Any]:
        """Return the effective value of every field at observation time *at*.

        This is the plain "what did we see at that moment" answer.  Use
        :meth:`state_at` for the same values together with their provenance,
        or :meth:`effective_observations_at` to keep conflicting reports.
        """
        return self.state_at(at).asdict()

    def state_at(self, at: typing.Optional[float] = None) -> TrackState:
        """Return the consolidated state visible at observation time *at*.

        Each field is resolved independently.  If several observations share
        the effective observation time, the most recently received available
        value becomes the primary value.  All observations are still returned
        via :attr:`TrackState.observations`.
        """
        key = None if at is None else float(at)
        cached = self._state_cache.get(key)
        if cached is not None:
            return cached

        effective = self.effective_observations_at(at)
        values: typing.Dict[str, typing.Any] = {}
        primary: typing.Dict[str, FieldObservation] = {}
        unavailable: typing.Set[str] = set()

        for field, observations in effective.items():
            available = [observation for observation in observations if observation.available]
            pool = available if available else observations
            chosen = max(pool, key=lambda observation: (observation.sequence, observation.source or ''))
            primary[field] = chosen
            values[field] = chosen.normalized
            if not available:
                unavailable.add(field)

        last_updated: typing.Optional[float] = None
        for observations in effective.values():
            value = observations[-1].observed_at
            if last_updated is None or value > last_updated:
                last_updated = value

        state = TrackState(
            values,
            mmsi=self.mmsi,
            requested_at=None if at == float('inf') else at,
            last_updated=last_updated,
            primary=primary,
            observations=effective,
            unavailable=frozenset(unavailable),
        )
        self._state_cache[key] = state
        return state

    def at(self, at: typing.Optional[float] = None) -> TrackState:
        """Alias for :meth:`state_at`."""
        return self.state_at(at)

    def as_of(self, at: typing.Optional[float] = None) -> TrackState:
        """Alias for :meth:`state_at`."""
        return self.state_at(at)

    def effective_at(self, at: typing.Optional[float] = None) -> TrackState:
        """Alias for :meth:`state_at`."""
        return self.state_at(at)

    def snapshot_at(self, at: typing.Optional[float] = None) -> TrackState:
        """Alias for :meth:`state_at`."""
        return self.state_at(at)

    def snapshot(self, at: typing.Optional[float] = None) -> TrackState:
        """Alias for :meth:`state_at`."""
        return self.state_at(at)

    def query(self, at: typing.Optional[float] = None) -> TrackState:
        """Alias for :meth:`state_at`."""
        return self.state_at(at)

    def latest(self) -> TrackState:
        """Return the state after all observations."""
        return self.state_at(None)

    def availability(self, field: str, at: typing.Optional[float] = None) -> str:
        """Return ``'available'``, ``'unavailable'`` or ``'absent'`` for *field* at *at*."""
        return self.state_at(at).availability(field)

    def to_track(self) -> 'AISTrack':
        """Build a consolidated :class:`~pyais.tracker.AISTrack` from this history."""
        if self.mmsi is None:
            raise ValueError("can not build a track without an MMSI")
        from pyais.tracker import AISTrack

        state = self.latest()
        track = AISTrack(mmsi=self.mmsi)
        for field, value in state.items():
            if field in ('mmsi', 'last_updated'):
                continue
            setattr(track, field, value)
        if state.last_updated is not None:
            track.last_updated = state.last_updated
        return track


# A descriptive alias used by some callers.
AISTrackHistory = TrackHistory


class TrackHistoryTable(typing.Mapping[int, TrackHistory]):
    """A collection of per-MMSI :class:`TrackHistory` instances.

    This is the "history state table": messages can be merged in any order
    and the effective state of any vessel can be queried for any observation
    time.
    """

    def __init__(self, tracks: typing.Optional[typing.Dict[int, TrackHistory]] = None) -> None:
        self._tracks: typing.Dict[int, TrackHistory] = {} if tracks is None else tracks

    def __repr__(self) -> str:
        return f"TrackHistoryTable<vessels={len(self._tracks)}>"

    def __getitem__(self, mmsi: typing.Union[int, str]) -> TrackHistory:
        return self._tracks[int(mmsi)]

    def __iter__(self) -> typing.Iterator[int]:
        return iter(self._tracks)

    def __len__(self) -> int:
        return len(self._tracks)

    def __call__(
        self, mmsi: typing.Union[int, str], at: typing.Optional[float] = None
    ) -> typing.Union[TrackHistory, TrackState, None]:
        """Return the history of a vessel, or its effective state at *at*."""
        if at is None:
            return self.history(mmsi)
        return self.state_at(mmsi, at)

    def get_or_create(self, mmsi: typing.Union[int, str]) -> TrackHistory:
        """Return the history of a vessel, creating it if necessary."""
        key = int(mmsi)
        history = self._tracks.get(key)
        if history is None:
            history = TrackHistory(key)
            self._tracks[key] = history
        return history

    def get(self, mmsi: typing.Union[int, str], default: typing.Any = None) -> typing.Any:
        return self._tracks.get(int(mmsi), default)

    def history(self, mmsi: typing.Union[int, str]) -> typing.Optional[TrackHistory]:
        """Return the history of one vessel, or None if it is unknown."""
        return self._tracks.get(int(mmsi))

    def get_history(self, mmsi: typing.Union[str, int]) -> typing.Optional[TrackHistory]:
        """Alias for :meth:`history`."""
        return self.history(mmsi)

    def mmsis(self) -> typing.Tuple[int, ...]:
        """All MMSIs with at least one observation."""
        return tuple(self._tracks)

    def add(
        self,
        record: typing.Mapping[str, typing.Any],
        source: typing.Optional[str] = None,
        observed_at: typing.Optional[float] = None,
    ) -> FieldObservation:
        """Add one field-level record (a single de-identified sample row).

        The record must contain an ``mmsi``, a ``field`` and a ``value``.
        ``observed_at``, ``source`` and ``available`` are optional. A value of
        :data:`UNAVAILABLE` records an explicitly unavailable value, which is
        different from omitting the field entirely.
        """
        if 'mmsi' not in record:
            raise ValueError("a field record requires an MMSI")
        key = int(record['mmsi'])
        history = self._tracks.get(key)
        if history is None:
            history = TrackHistory(key)
            self._tracks[key] = history
        return history.add(record, source, observed_at)

    def upsert(
        self,
        mmsi: typing.Union[int, str],
        observations: typing.Any,
        observed_at: typing.Optional[float] = None,
        source: typing.Optional[str] = None,
    ) -> typing.Dict[str, FieldObservation]:
        """Merge observations for a vessel (see :meth:`merge`)."""
        return self.merge(mmsi, observations, observed_at, source)

    def snapshot_at(self, at: typing.Optional[float] = None) -> typing.Dict[int, TrackState]:
        """Return the effective state of every vessel at observation time *at*."""
        return self.states_at(at)

    def query(self, at: typing.Optional[float] = None) -> typing.Dict[int, TrackState]:
        """Alias for :meth:`states_at`."""
        return self.states_at(at)

    def add_sample(self, sample: typing.Mapping[str, typing.Any]) -> typing.Dict[str, FieldObservation]:
        """Import one de-identified sample row into the history table."""
        if 'mmsi' not in sample:
            raise ValueError("a sample requires an MMSI")
        history = self.get_or_create(sample['mmsi'])
        return history.add_sample(sample)

    def import_sample(self, sample: typing.Mapping[str, typing.Any]) -> typing.Dict[str, FieldObservation]:
        """Alias for :meth:`add_sample`."""
        return self.add_sample(sample)

    def merge(
        self,
        mmsi: typing.Optional[typing.Union[int, str]] = None,
        observations: typing.Any = None,
        observed_at: typing.Optional[float] = None,
        source: typing.Optional[str] = None,
        available: typing.Optional[bool] = None,
    ) -> typing.Dict[str, FieldObservation]:
        """Merge observations into the history of a vessel.

        The MMSI can either be given explicitly or is taken from a message or
        track. ``observations`` may be a message, a track, a mapping of field
        names to values, a single :class:`FieldObservation` or an iterable of
        observations.
        """
        if observations is None and hasattr(mmsi, 'mmsi'):
            observations = mmsi
            mmsi = None

        if mmsi is None:
            if observations is None or not hasattr(observations, 'mmsi'):
                raise ValueError("an MMSI or an object with an MMSI is required")
            mmsi = observations.mmsi

        key = int(typing.cast(typing.Union[int, str], mmsi))
        history = self._tracks.get(key)
        if history is None:
            history = TrackHistory(key)
            self._tracks[key] = history
        return history.merge(observations, observed_at, source, available)

    def merge_message(
        self,
        msg: ANY_MESSAGE,
        observed_at: typing.Optional[float] = None,
        source: typing.Optional[str] = None,
    ) -> typing.Dict[str, FieldObservation]:
        """Merge a decoded message."""
        return self.merge(msg.mmsi, msg, observed_at, source)

    def merge_track(
        self,
        track: 'AISTrack',
        observed_at: typing.Optional[float] = None,
        source: typing.Optional[str] = None,
    ) -> typing.Dict[str, FieldObservation]:
        """Merge a consolidated track."""
        return self.merge(track.mmsi, track, observed_at, source)

    def state_at(self, mmsi: typing.Union[int, str], at: typing.Optional[float] = None) -> typing.Optional[TrackState]:
        """Return the effective state of one vessel at observation time *at*."""
        history = self._tracks.get(int(mmsi))
        if history is None:
            return None
        return history.state_at(at)

    def values_at(
        self, mmsi: typing.Union[int, str], at: typing.Optional[float] = None
    ) -> typing.Optional[typing.Dict[str, typing.Any]]:
        """Return the effective field values of one vessel at observation time *at*."""
        history = self._tracks.get(int(mmsi))
        if history is None:
            return None
        return history.values_at(at)

    def observations_at(
        self, mmsi: typing.Union[int, str], at: typing.Optional[float] = None
    ) -> typing.Dict[str, typing.Tuple[FieldObservation, ...]]:
        """Return the effective observations of one vessel at observation time *at*."""
        history = self._tracks.get(int(mmsi))
        if history is None:
            return {}
        return history.effective_observations_at(at)

    def exact_observations_at(
        self, mmsi: typing.Union[int, str], at: float
    ) -> typing.Dict[str, typing.Tuple[FieldObservation, ...]]:
        """Return only observations taken exactly at *at* for one vessel."""
        history = self._tracks.get(int(mmsi))
        if history is None:
            return {}
        return history.exact_observations_at(at)

    def states_at(self, at: typing.Optional[float] = None) -> typing.Dict[int, TrackState]:
        """Return the effective state of every vessel at observation time *at*."""
        result: typing.Dict[int, TrackState] = {}
        for mmsi, history in self._tracks.items():
            result[mmsi] = history.state_at(at)
        return result

    def state(self, mmsi: typing.Union[int, str], at: typing.Optional[float] = None) -> typing.Optional[TrackState]:
        """Alias for :meth:`state_at`."""
        return self.state_at(mmsi, at)

    def as_of(self, at: typing.Optional[float] = None) -> typing.Dict[int, TrackState]:
        """Return the effective state of every vessel at observation time *at*."""
        return self.states_at(at)

    def values_at_all(self, at: typing.Optional[float] = None) -> typing.Dict[int, typing.Dict[str, typing.Any]]:
        """Return the effective plain field values of every vessel at *at*."""
        return {mmsi: state.asdict() for mmsi, state in self.states_at(at).items()}

    def tracks_at(self, at: typing.Optional[float] = None) -> typing.Dict[int, 'AISTrack']:
        """Return a consolidated track for every vessel at observation time *at*.

        Vessels without any observation at or before *at* are omitted.
        """
        from pyais.tracker import AISTrack

        result: typing.Dict[int, 'AISTrack'] = {}
        for mmsi, history in self._tracks.items():
            state = history.state_at(at)
            if state is None or not state:
                continue
            track = AISTrack(mmsi=mmsi)
            for field, value in state.items():
                if field in ('mmsi', 'last_updated'):
                    continue
                setattr(track, field, value)
            if state.last_updated is not None:
                track.last_updated = state.last_updated
            result[mmsi] = track
        return result

    def pop(self, mmsi: typing.Union[int, str], default: typing.Any = None) -> typing.Any:
        """Remove and return the history of a vessel."""
        return self._tracks.pop(int(mmsi), default)

    def clear(self) -> None:
        """Forget all histories."""
        self._tracks.clear()


# Descriptive aliases used by callers that prefer a different wording.
AISTrackHistoryTable = TrackHistoryTable
AISStateHistory = TrackHistoryTable
StateHistory = TrackHistoryTable
TrackStateHistory = TrackHistoryTable
StateTable = TrackHistoryTable
HistoryTable = TrackHistoryTable
AISTrackStateHistory = TrackHistoryTable
HistoricalState = TrackState
FieldValue = FieldObservation
ObservationRecord = FieldObservation
