"""Resolve AIS voyage ETAs that carry no year.

AIS message type 5 reports an estimated time of arrival as four separate
fields: month, day, hour and minute.  The year is not transmitted, and each
field has its own "not available" value (``0`` for month and day, ``24`` for
hour and ``60`` for minute).  Several binary application messages use the same
four fields for scheduled times.

An ETA therefore describes a window rather than a single instant.  For an
archival record the window is narrowed by the time the message was received
and by a business supplied look-back/look-ahead window.  This module keeps the
reported precision and returns **every** civil date that is compatible with
the archive, sorted chronologically.  It never picks a single date when more
than one is possible.  That matters at year boundaries (``01-01`` received on
31 December) and when a window is wide enough to contain the same date in two
different years, including two leap days.

Example::

    >>> from datetime import datetime, timedelta, timezone
    >>> archive = ETAArchive.from_ais(
    ...     received_at=datetime(2024, 12, 31, 23, 30, tzinfo=timezone.utc),
    ...     month=1, day=1, hour=24, minute=60,
    ...     window_before=timedelta(days=2),
    ...     window_after=timedelta(days=2),
    ... )
    >>> archive.precision
    <ETAPrecision.DAY: 'day'>
    >>> archive.civil_dates
    (datetime.date(2025, 1, 1),)
"""

import dataclasses
import datetime
import enum
import json
import typing

__all__ = (
    'ETAPrecision',
    'ETACandidate',
    'ETAArchive',
    'ETA_NOT_AVAILABLE',
    'resolve_eta',
    'resolve_eta_candidates',
)

#: The AIS "not available" value of each ETA field.
ETA_NOT_AVAILABLE: typing.Dict[str, int] = {
    'month': 0,
    'day': 0,
    'hour': 24,
    'minute': 60,
}

#: The valid range of each ETA field, excluding its "not available" value.
_FIELD_RANGES: typing.Dict[str, typing.Tuple[int, int]] = {
    'month': (1, 12),
    'day': (1, 31),
    'hour': (0, 23),
    'minute': (0, 59),
}

_ONE_MINUTE = datetime.timedelta(minutes=1)
_ONE_HOUR = datetime.timedelta(hours=1)
_ONE_DAY = datetime.timedelta(days=1)


class ETAPrecision(str, enum.Enum):
    """The precision of the least significant ETA field that was reported."""

    UNKNOWN = 'unknown'
    MONTH = 'month'
    DAY = 'day'
    HOUR = 'hour'
    MINUTE = 'minute'


def _normalize(field: str, value: typing.Optional[int]) -> typing.Optional[int]:
    """Return a usable field value or ``None`` for "not available"/invalid."""
    if value is None:
        return None
    value = int(value)
    if value == ETA_NOT_AVAILABLE[field]:
        return None
    low, high = _FIELD_RANGES[field]
    if low <= value <= high:
        return value
    return None


def _precision_of(
    month: typing.Optional[int],
    day: typing.Optional[int],
    hour: typing.Optional[int],
    minute: typing.Optional[int],
) -> ETAPrecision:
    """Return the precision implied by the reported fields.

    A year cannot be resolved without a month, so a missing month makes the
    whole ETA unusable even if the other fields are present.  A missing day
    leaves month precision, a missing hour leaves day precision and a missing
    minute leaves hour precision.  In particular, an unreported minute is not
    silently turned into ``:00``.
    """
    if month is None:
        return ETAPrecision.UNKNOWN
    if day is None:
        return ETAPrecision.MONTH
    if hour is None:
        return ETAPrecision.DAY
    if minute is None:
        return ETAPrecision.HOUR
    return ETAPrecision.MINUTE


@dataclasses.dataclass(frozen=True)
class ETACandidate:
    """One possible civil date for an ETA.

    ``date`` is the possible civil date.  ``start`` (inclusive) and ``end``
    (exclusive) delimit the uncertainty implied by the reported precision:

    - minute precision: one minute
    - hour precision: one hour
    - day precision: one day
    - month precision: the whole month; every day of that month is a separate
      candidate sharing the same uncertainty interval

    ``hour`` and ``minute`` are ``None`` when those fields were not reported,
    so the candidate never pretends that an unreported value was zero.
    """

    date: datetime.date
    start: datetime.datetime
    end: datetime.datetime
    precision: ETAPrecision

    @property
    def year(self) -> int:
        return self.date.year

    @property
    def month(self) -> int:
        return self.date.month

    @property
    def day(self) -> typing.Optional[int]:
        """The reported day, or ``None`` when only month precision is known."""
        if self.precision is ETAPrecision.MONTH:
            return None
        return self.date.day

    @property
    def hour(self) -> typing.Optional[int]:
        """The reported hour, or ``None`` below hour precision."""
        if self.precision in (ETAPrecision.HOUR, ETAPrecision.MINUTE):
            return self.start.hour
        return None

    @property
    def minute(self) -> typing.Optional[int]:
        """The reported minute, or ``None`` below minute precision."""
        if self.precision is ETAPrecision.MINUTE:
            return self.start.minute
        return None

    @property
    def civil_date(self) -> datetime.date:
        """Alias of :attr:`date`."""
        return self.date

    @property
    def duration(self) -> datetime.timedelta:
        """The width of the unresolved part of the ETA."""
        return self.end - self.start

    def overlaps(self, start: datetime.datetime, end: datetime.datetime) -> bool:
        """Whether this candidate's uncertainty interval overlaps ``[start, end]``.

        ``end`` is inclusive so that a candidate touching the very end of the
        business window is still reported.
        """
        return self.start <= end and self.end > start

    def asdict(self) -> typing.Dict[str, typing.Any]:
        return {
            'date': self.date.isoformat(),
            'year': self.year,
            'month': self.month,
            'day': self.day,
            'hour': self.hour,
            'minute': self.minute,
            'precision': self.precision.value,
            'start': self.start.isoformat(),
            'end': self.end.isoformat(),
        }


@dataclasses.dataclass(frozen=True)
class ETAArchive:
    """An ETA together with every civil date that is compatible with it.

    The object is self contained and deliberately carries no vessel identity,
    so it can be stored for anonymised samples.
    """

    received_at: datetime.datetime
    reported: typing.Dict[str, typing.Optional[int]]
    month: typing.Optional[int]
    day: typing.Optional[int]
    hour: typing.Optional[int]
    minute: typing.Optional[int]
    precision: ETAPrecision
    window_before: datetime.timedelta
    window_after: datetime.timedelta
    candidates: typing.Tuple[ETACandidate, ...]

    @property
    def window_start(self) -> datetime.datetime:
        return self.received_at - self.window_before

    @property
    def window_end(self) -> datetime.datetime:
        return self.received_at + self.window_after

    @property
    def is_resolvable(self) -> bool:
        """Whether at least one civil date matches the reported fields."""
        return bool(self.candidates)

    @property
    def is_ambiguous(self) -> bool:
        """Whether more than one civil date is possible."""
        return len(self.candidates) > 1

    @property
    def civil_dates(self) -> typing.Tuple[datetime.date, ...]:
        """All possible civil dates, in chronological order.

        Every candidate names exactly one civil date.  A month precision
        candidate therefore does not contribute a month object but one
        candidate per day of that month, so the dates remain explicit.
        """
        return tuple(candidate.date for candidate in self.candidates)

    @property
    def years(self) -> typing.Tuple[int, ...]:
        """The distinct years of all possible civil dates."""
        return tuple(sorted({candidate.year for candidate in self.candidates}))

    @property
    def field_precision(self) -> typing.Dict[str, typing.Optional[ETAPrecision]]:
        """The precision contributed by each ETA field.

        ``None`` means the field was not reported (or held its "not
        available" sentinel).  This keeps the precision of each field rather
        than only the precision of the least significant one.

        A minute is only meaningful when the hour is known, and an hour is
        only meaningful when the day is known.  The lower-order entries are
        therefore cleared when a higher-order field is missing, matching the
        precision that was actually used for the candidates.
        """
        month = ETAPrecision.MONTH if self.month is not None else None
        day = ETAPrecision.DAY if month is not None and self.day is not None else None
        hour = ETAPrecision.HOUR if day is not None and self.hour is not None else None
        minute = ETAPrecision.MINUTE if hour is not None and self.minute is not None else None
        return {
            'month': month,
            'day': day,
            'hour': hour,
            'minute': minute,
        }

    @classmethod
    def from_ais(
        cls,
        *,
        received_at: datetime.datetime,
        month: typing.Optional[int] = ETA_NOT_AVAILABLE['month'],
        day: typing.Optional[int] = ETA_NOT_AVAILABLE['day'],
        hour: typing.Optional[int] = ETA_NOT_AVAILABLE['hour'],
        minute: typing.Optional[int] = ETA_NOT_AVAILABLE['minute'],
        window_before: datetime.timedelta,
        window_after: datetime.timedelta,
    ) -> 'ETAArchive':
        """Build an archive from the raw AIS ETA fields.

        ``month``, ``day``, ``hour`` and ``minute`` are the values as they
        appeared in the message, including the AIS "not available" sentinels.
        ``window_before`` and ``window_after`` define the business window
        around ``received_at``.
        """
        if not isinstance(received_at, datetime.datetime):
            raise TypeError('received_at must be a datetime.datetime')
        before = _validate_window(window_before, 'window_before')
        after = _validate_window(window_after, 'window_after')

        norm_month = _normalize('month', month)
        norm_day = _normalize('day', day)
        norm_hour = _normalize('hour', hour)
        norm_minute = _normalize('minute', minute)
        precision = _precision_of(norm_month, norm_day, norm_hour, norm_minute)

        candidates = _resolve(
            received_at=received_at,
            month=norm_month,
            day=norm_day,
            hour=norm_hour,
            minute=norm_minute,
            precision=precision,
            window_before=before,
            window_after=after,
        )

        return cls(
            received_at=received_at,
            reported={
                'month': month,
                'day': day,
                'hour': hour,
                'minute': minute,
            },
            month=norm_month,
            day=norm_day,
            hour=norm_hour,
            minute=norm_minute,
            precision=precision,
            window_before=before,
            window_after=after,
            candidates=candidates,
        )

    @classmethod
    def from_message(
        cls,
        message: typing.Any,
        *,
        received_at: datetime.datetime,
        window_before: datetime.timedelta,
        window_after: datetime.timedelta,
    ) -> 'ETAArchive':
        """Build an archive from any message with ``month``/``day``/``hour``/``minute``.

        Several AIS application messages carry a UTC month/day/hour/minute
        schedule in the same four fields as the type 5 voyage ETA.  Reading
        them through this helper applies the same year resolution and
        precision rules everywhere.
        """
        values: typing.Dict[str, typing.Optional[int]] = {}
        for field in ('month', 'day', 'hour', 'minute'):
            try:
                values[field] = getattr(message, field)
            except AttributeError as err:
                raise ValueError(f'message has no ETA field {field!r}') from err
        return cls.from_ais(
            received_at=received_at,
            window_before=window_before,
            window_after=window_after,
            **values,
        )

    @classmethod
    def from_sentence(
        cls,
        sentence: typing.Any,
        *,
        window_before: datetime.timedelta,
        window_after: datetime.timedelta,
        received_at: typing.Optional[datetime.datetime] = None,
    ) -> 'ETAArchive':
        """Build an archive directly from a decoded NMEA AIS sentence.

        The reception time is taken from ``received_at`` when provided,
        otherwise from the NMEA 4.10 tag block's ``c`` field (UNIX seconds) or
        from a Gatehouse wrapper timestamp.  The sentence is decoded and its
        month/day/hour/minute fields are resolved with the same rules as
        :meth:`from_ais`.
        """
        moment = received_at if received_at is not None else _reception_time(sentence)
        return cls.from_message(
            sentence.decode(),
            received_at=moment,
            window_before=window_before,
            window_after=window_after,
        )

    def asdict(self) -> typing.Dict[str, typing.Any]:
        """Return a JSON friendly representation for archival storage."""
        return {
            'received_at': self.received_at.isoformat(),
            'reported': dict(self.reported),
            'normalized': {
                'month': self.month,
                'day': self.day,
                'hour': self.hour,
                'minute': self.minute,
            },
            'precision': self.precision.value,
            'field_precision': {
                name: precision.value if precision is not None else None
                for name, precision in self.field_precision.items()
            },
            'window': {
                'before_seconds': self.window_before.total_seconds(),
                'after_seconds': self.window_after.total_seconds(),
                'start': self.window_start.isoformat(),
                'end': self.window_end.isoformat(),
            },
            'candidates': [candidate.asdict() for candidate in self.candidates],
            'civil_dates': [date.isoformat() for date in self.civil_dates],
            'years': list(self.years),
            'is_ambiguous': self.is_ambiguous,
        }

    def to_json(self, **kwargs: typing.Any) -> str:
        """Serialise the archive to JSON."""
        return json.dumps(self.asdict(), **kwargs)

    @classmethod
    def fromdict(cls, data: typing.Mapping[str, typing.Any]) -> 'ETAArchive':
        """Rebuild an archive from :meth:`asdict` output.

        The candidates are recomputed from the reported fields and the window,
        so the stored record stays the single source of truth.
        """
        reported = typing.cast(typing.Mapping[str, typing.Any], data.get('reported', {}))
        window = typing.cast(typing.Mapping[str, typing.Any], data.get('window', {}))
        received_at = _parse_datetime(data['received_at'])
        return cls.from_ais(
            received_at=received_at,
            month=reported.get('month', ETA_NOT_AVAILABLE['month']),
            day=reported.get('day', ETA_NOT_AVAILABLE['day']),
            hour=reported.get('hour', ETA_NOT_AVAILABLE['hour']),
            minute=reported.get('minute', ETA_NOT_AVAILABLE['minute']),
            window_before=datetime.timedelta(seconds=float(window.get('before_seconds', 0))),
            window_after=datetime.timedelta(seconds=float(window.get('after_seconds', 0))),
        )


def resolve_eta(
    *,
    received_at: datetime.datetime,
    month: typing.Optional[int] = ETA_NOT_AVAILABLE['month'],
    day: typing.Optional[int] = ETA_NOT_AVAILABLE['day'],
    hour: typing.Optional[int] = ETA_NOT_AVAILABLE['hour'],
    minute: typing.Optional[int] = ETA_NOT_AVAILABLE['minute'],
    window_before: datetime.timedelta,
    window_after: datetime.timedelta,
) -> ETAArchive:
    """Resolve an AIS ETA to an :class:`ETAArchive`."""
    return ETAArchive.from_ais(
        received_at=received_at,
        month=month,
        day=day,
        hour=hour,
        minute=minute,
        window_before=window_before,
        window_after=window_after,
    )


def resolve_eta_candidates(
    *,
    received_at: datetime.datetime,
    month: typing.Optional[int] = ETA_NOT_AVAILABLE['month'],
    day: typing.Optional[int] = ETA_NOT_AVAILABLE['day'],
    hour: typing.Optional[int] = ETA_NOT_AVAILABLE['hour'],
    minute: typing.Optional[int] = ETA_NOT_AVAILABLE['minute'],
    window_before: datetime.timedelta,
    window_after: datetime.timedelta,
) -> typing.Tuple[ETACandidate, ...]:
    """Return every civil date compatible with an ETA and a receipt window.

    The candidate years are taken from the calendar years touched by the
    window, so a date near a year boundary can resolve into the following
    year.  A candidate is returned when its precision interval overlaps the
    window ``[received_at - window_before, received_at + window_after]``.
    """
    return resolve_eta(
        received_at=received_at,
        month=month,
        day=day,
        hour=hour,
        minute=minute,
        window_before=window_before,
        window_after=window_after,
    ).candidates


def _validate_window(value: datetime.timedelta, name: str) -> datetime.timedelta:
    if not isinstance(value, datetime.timedelta):
        raise TypeError(f'{name} must be a datetime.timedelta')
    if value < datetime.timedelta(0):
        raise ValueError(f'{name} must not be negative')
    return value


def _parse_datetime(value: typing.Any) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        return value
    if not isinstance(value, str):
        raise TypeError('received_at must be an ISO 8601 string or a datetime.datetime')
    if value.endswith(('Z', 'z')):
        value = value[:-1] + '+00:00'
    return datetime.datetime.fromisoformat(value)


def _reception_time(sentence: typing.Any) -> datetime.datetime:
    """Return when a sentence was received, preferring explicit metadata.

    NMEA 4.10 tag block ``c`` is the receiver timestamp in UNIX seconds.  A
    Gatehouse wrapper carries a full UTC timestamp.  Both are optional; when
    neither is present the caller has to pass ``received_at`` explicitly.
    """
    tag_block = getattr(sentence, 'tag_block', None)
    if tag_block is not None:
        if not getattr(tag_block, 'initialized', False):
            tag_block.init()
        receiver_timestamp = tag_block.receiver_timestamp
        if receiver_timestamp:
            return datetime.datetime.fromtimestamp(
                int(receiver_timestamp), tz=datetime.timezone.utc
            )

    wrapper = getattr(sentence, 'wrapper_msg', None)
    if wrapper is not None and getattr(wrapper, 'timestamp', None) is not None:
        moment = wrapper.timestamp
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.timezone.utc)
        return typing.cast(datetime.datetime, moment)

    raise ValueError(
        'no reception time available: pass received_at or use a sentence with '
        'a tag block receiver timestamp or Gatehouse wrapper timestamp'
    )


def _day_bounds(
    date: datetime.date,
    tzinfo: typing.Optional[datetime.tzinfo],
) -> typing.Tuple[datetime.datetime, datetime.datetime]:
    """Return the local midnight bounds of a civil date.

    Adding ``timedelta(days=1)`` to a midnight would be wrong across a DST
    transition because the local day is not always 24 hours.  Constructing
    the next midnight explicitly keeps the boundary aligned with the civil
    calendar.
    """
    start = datetime.datetime.combine(date, datetime.time(), tzinfo=tzinfo)
    next_date = date + datetime.timedelta(days=1)
    end = datetime.datetime.combine(next_date, datetime.time(), tzinfo=tzinfo)
    return start, end


def _resolve(
    *,
    received_at: datetime.datetime,
    month: typing.Optional[int],
    day: typing.Optional[int],
    hour: typing.Optional[int],
    minute: typing.Optional[int],
    precision: ETAPrecision,
    window_before: datetime.timedelta,
    window_after: datetime.timedelta,
) -> typing.Tuple[ETACandidate, ...]:
    if precision is ETAPrecision.UNKNOWN:
        return ()

    window_start = received_at - window_before
    window_end = received_at + window_after
    # A candidate interval, even at month precision, never extends beyond the
    # calendar year in which it starts.  The window's own years are therefore
    # enough to find every overlap.
    candidates: typing.List[ETACandidate] = []
    for year in range(window_start.year, window_end.year + 1):
        candidates.extend(
            _candidates_for_year(
                year=year,
                month=month,
                day=day,
                hour=hour,
                minute=minute,
                precision=precision,
                window_start=window_start,
                window_end=window_end,
                tzinfo=received_at.tzinfo,
            )
        )
    candidates.sort(key=lambda candidate: (candidate.date, candidate.start))
    return tuple(candidates)


def _candidates_for_year(
    *,
    year: int,
    month: typing.Optional[int],
    day: typing.Optional[int],
    hour: typing.Optional[int],
    minute: typing.Optional[int],
    precision: ETAPrecision,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
    tzinfo: typing.Optional[datetime.tzinfo],
) -> typing.List[ETACandidate]:
    if month is None:
        return []

    if precision is ETAPrecision.MONTH:
        return _month_candidates(
            year=year,
            month=month,
            window_start=window_start,
            window_end=window_end,
            tzinfo=tzinfo,
        )
    if day is None:
        return []

    try:
        if precision is ETAPrecision.MINUTE:
            if hour is None or minute is None:
                return []
            start = datetime.datetime(year, month, day, hour, minute, tzinfo=tzinfo)
            end = start + _ONE_MINUTE
        elif precision is ETAPrecision.HOUR:
            if hour is None:
                return []
            start = datetime.datetime(year, month, day, hour, tzinfo=tzinfo)
            end = start + _ONE_HOUR
        else:
            start, end = _day_bounds(datetime.date(year, month, day), tzinfo)
    except ValueError:
        # The month/day combination does not exist in this year.  This is
        # expected for 29 February in a non-leap year and is not an error:
        # another year in the window may still provide a candidate.
        return []

    candidate = ETACandidate(date=start.date(), start=start, end=end, precision=precision)
    if not candidate.overlaps(window_start, window_end):
        return []
    return [candidate]


def _month_candidates(
    *,
    year: int,
    month: int,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
    tzinfo: typing.Optional[datetime.tzinfo],
) -> typing.List[ETACandidate]:
    """Return the possible civil dates of a month-precision ETA.

    Only the month was reported, so every day of that month is a possible
    civil date.  Days outside the business window are not possible and are
    omitted.  Each candidate keeps MONTH precision and carries the whole
    month as its uncertainty interval; the window is not allowed to turn
    month precision into day precision.
    """
    start = datetime.datetime(year, month, 1, tzinfo=tzinfo)
    if month == 12:
        end = datetime.datetime(year + 1, 1, 1, tzinfo=tzinfo)
    else:
        end = datetime.datetime(year, month + 1, 1, tzinfo=tzinfo)

    candidates: typing.List[ETACandidate] = []
    date = start.date()
    while date < end.date():
        day_start, day_end = _day_bounds(date, tzinfo)
        if day_start <= window_end and day_end > window_start:
            candidates.append(
                ETACandidate(date=date, start=start, end=end, precision=ETAPrecision.MONTH)
            )
        date += _ONE_DAY
    return candidates
