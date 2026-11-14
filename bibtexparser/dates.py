"""Precision-preserving date ranges, season calendars, and date-source reconciliation.

This module solves a cataloging problem that plain ``datetime`` values cannot
express: bibliographic dates such as ``"Spring 1998"``, ``"1997/98"`` or a
monthly ``year``/``month`` pair carry different *precisions* and must not all
be collapsed onto January 1st.

The central type is :class:`DateRange`, a bounded or unbounded interval whose
endpoints are :class:`PartialDate` values tagged with a :class:`DatePrecision`.
Season strings are only turned into bounded ranges through a
:class:`SeasonCalendar` provided by the caller; without a matching calendar
entry (or without a year) a season remains an explicitly *unbounded* range.
Ranges known to span a year boundary (academic years, meteorological winter)
keep an end date in the following year instead of being clipped to December.
"""

import calendar
import re
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import Optional


class DateError(ValueError):
    """Base class for all date parsing and reconciliation errors."""


class InvalidDateError(DateError):
    """Raised when date components do not form a valid calendar date."""


class UnparseableDateError(DateError):
    """Raised when a free-text date cannot be matched by any known pattern."""


class SeasonNotInCalendarError(DateError):
    """Raised when a season has no entry in the calendar used for resolution."""


class DatePrecision(Enum):
    """The granularity at which a date or range is known.

    The precision is original information: ``"1998"`` means a whole year,
    not January 1st, even though its bounding range starts on that day.
    """

    DAY = "day"
    MONTH = "month"
    YEAR = "year"
    SEASON = "season"
    ACADEMIC_YEAR = "academic-year"
    UNBOUNDED = "unbounded"


# Coarsest precision last, used when intersecting ranges of mixed precision.
_PRECISION_ORDER = (
    DatePrecision.DAY,
    DatePrecision.MONTH,
    DatePrecision.YEAR,
    DatePrecision.SEASON,
    DatePrecision.ACADEMIC_YEAR,
    DatePrecision.UNBOUNDED,
)


class Season(Enum):
    """The four seasons. The alias ``"fall"`` normalizes to :attr:`AUTUMN`."""

    WINTER = "winter"
    SPRING = "spring"
    SUMMER = "summer"
    AUTUMN = "autumn"


_SEASON_ALIASES: dict[str, Season] = {
    "winter": Season.WINTER,
    "win": Season.WINTER,
    "wint": Season.WINTER,
    "spring": Season.SPRING,
    "spr": Season.SPRING,
    "summer": Season.SUMMER,
    "sum": Season.SUMMER,
    "autumn": Season.AUTUMN,
    "fall": Season.AUTUMN,
    "aut": Season.AUTUMN,
}

_MONTH_ALIASES: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _last_day_of_month(year: int, month: int) -> int:
    """The last valid day of the given month (handles leap-year February)."""
    return calendar.monthrange(year, month)[1]


@dataclass(frozen=True)
class PartialDate:
    """A calendar date whose day, month or even year may be unspecified.

    ``year`` is ``None`` only for unbounded anchors (e.g. a bare season name
    without an anchor year). When ``day`` is ``None`` the value describes a
    whole month or year; the bounding endpoints are derived on demand.
    """

    year: Optional[int]
    month: Optional[int] = None
    day: Optional[int] = None
    precision: DatePrecision = DatePrecision.YEAR

    def __post_init__(self) -> None:
        if self.year is None:
            if self.month is not None or self.day is not None:
                raise InvalidDateError("A partial date without a year cannot have a month or day")
            return
        if self.month is not None and not 1 <= self.month <= 12:
            raise InvalidDateError(f"Month must be between 1 and 12, got {self.month}")
        if self.day is not None:
            if self.month is None:
                raise InvalidDateError("A day without a month is not a valid partial date")
            last_day = _last_day_of_month(self.year, self.month)
            if not 1 <= self.day <= last_day:
                raise InvalidDateError(
                    f"Day must be between 1 and {last_day} for "
                    f"{self.year}-{self.month:02d}, got {self.day}"
                )

    @property
    def sort_key(self) -> Optional[tuple[int, int, int]]:
        """A comparable ``(year, month, day)`` tuple, or ``None`` if unbounded."""
        if self.year is None:
            return None
        return (self.year, self.month or 1, self.day or 1)

    def to_iso(self) -> Optional[str]:
        """The most precise ISO-8601 representation the components support.

        A year renders as ``"1998"``, a month as ``"1998-03"`` and a day as
        ``"1998-03-01"``. Unbounded dates render as ``None``.
        """
        if self.year is None:
            return None
        if self.month is None:
            return f"{self.year:04d}"
        if self.day is None:
            return f"{self.year:04d}-{self.month:02d}"
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"


@dataclass(frozen=True)
class DateRange:
    """A bounded or unbounded interval with preserved precision.

    ``bounded`` is ``False`` for intervals whose real extent is unknown,
    typically a season that could not be resolved via a calendar. Such ranges
    carry :attr:`DatePrecision.UNBOUNDED` endpoints and must not be silently
    treated as spanning January 1st to December 31st.

    ``cross_year`` marks ranges whose end falls in a later calendar year than
    their start (academic years and cross-year seasons). The end date is kept
    in the following year and is never clipped to December 31st.
    """

    start: PartialDate
    end: PartialDate
    precision: DatePrecision
    bounded: bool = True
    cross_year: bool = False
    label: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.bounded:
            return
        start_key = self.start.sort_key
        end_key = self.end.sort_key
        if start_key is None or end_key is None:
            raise InvalidDateError("A bounded range needs fully anchored endpoints")
        if start_key > end_key:
            raise InvalidDateError(
                f"Range start {self.start.to_iso()} is after end {self.end.to_iso()}"
            )

    @classmethod
    def unbounded(cls, precision: DatePrecision, label: Optional[str] = None) -> "DateRange":
        """Create an explicitly unresolved range of the given precision."""
        anchor = PartialDate(year=None, precision=DatePrecision.UNBOUNDED)
        return cls(
            start=anchor,
            end=anchor,
            precision=precision,
            bounded=False,
            label=label,
        )

    @classmethod
    def for_year(cls, year: int, label: Optional[str] = None) -> "DateRange":
        """The full calendar-year range (Jan 1 through Dec 31)."""
        return cls(
            start=PartialDate(year, 1, 1, DatePrecision.YEAR),
            end=PartialDate(year, 12, 31, DatePrecision.YEAR),
            precision=DatePrecision.YEAR,
            label=label,
        )

    @classmethod
    def for_month(cls, year: int, month: int, label: Optional[str] = None) -> "DateRange":
        """The full calendar-month range, ending on the month's last day."""
        return cls(
            start=PartialDate(year, month, 1, DatePrecision.MONTH),
            end=PartialDate(year, month, _last_day_of_month(year, month), DatePrecision.MONTH),
            precision=DatePrecision.MONTH,
            label=label,
        )

    @classmethod
    def for_day(cls, year: int, month: int, day: int, label: Optional[str] = None) -> "DateRange":
        """A single-day range, at day precision."""
        anchor = PartialDate(year, month, day, DatePrecision.DAY)
        return cls(start=anchor, end=anchor, precision=DatePrecision.DAY, label=label)

    @classmethod
    def for_academic_year(cls, start_year: int, label: Optional[str] = None) -> "DateRange":
        """An academic-year range, Sep 1 of ``start_year`` through Aug 31 next year."""
        return cls(
            start=PartialDate(start_year, 9, 1, DatePrecision.ACADEMIC_YEAR),
            end=PartialDate(start_year + 1, 8, 31, DatePrecision.ACADEMIC_YEAR),
            precision=DatePrecision.ACADEMIC_YEAR,
            cross_year=True,
            label=label,
        )

    def intersection(self, other: "DateRange") -> Optional["DateRange"]:
        """The overlapping interval of two ranges, or ``None`` if disjoint.

        Unbounded ranges do not constrain the intersection. The result keeps
        the coarsest precision of the bounded inputs and the cross-year flag
        when the overlap itself still spans a year boundary.

        An explicitly cross-year range (an academic year, a cross-year season)
        is never clipped by a coarser calendar-year box: a ``year`` field that
        only pins the start year must not truncate a ``"1997/98"`` statement at
        December 31st. Finer, month- or day-precision sources still constrain
        the overlap, and fully disjoint ranges keep returning ``None``.
        """
        bounded = [r for r in (self, other) if r.bounded]
        if not bounded:
            return DateRange.unbounded(
                precision=max((r.precision for r in (self, other)), key=_precision_rank),
                label=self.label or other.label,
            )
        starts = [r.start.sort_key for r in bounded]
        ends = [r.end.sort_key for r in bounded]
        overlap_start_key = max(starts)
        overlap_end_key = min(ends)
        if overlap_start_key > overlap_end_key:
            return None

        if any(r.cross_year for r in bounded):
            # Calendar-year boxes only pin a year; they must not cut an
            # explicit cross-year statement at the December boundary.
            edge_providers = [r for r in bounded if r.precision is not DatePrecision.YEAR]
            if edge_providers:
                overlap_start_key = max(r.start.sort_key for r in edge_providers)
                overlap_end_key = min(r.end.sort_key for r in edge_providers)

        # Precision follows the sources that actually bound the overlap (the
        # ones supplying one of its edges), taking the finest such precision.
        # A broad calendar-year box containing a tighter season must not
        # downgrade the season precision of the shared interval.
        edge_sources = [
            r
            for r in bounded
            if r.start.sort_key == overlap_start_key or r.end.sort_key == overlap_end_key
        ]
        precision = min((r.precision for r in edge_sources), key=_precision_rank)
        start = _partial_date_from_key(overlap_start_key, precision)
        end = _partial_date_from_key(overlap_end_key, precision)
        cross_year = overlap_end_key[0] > overlap_start_key[0]
        return DateRange(
            start=start,
            end=end,
            precision=precision,
            bounded=True,
            cross_year=cross_year,
        )

    def is_disjoint(self, other: "DateRange") -> bool:
        """Whether two ranges share no bounded overlap."""
        return self.intersection(other) is None

    def to_dict(self) -> dict:
        """A JSON-serializable representation, used in parser metadata."""
        return {
            "start": self.start.to_iso(),
            "end": self.end.to_iso(),
            "precision": self.precision.value,
            "bounded": self.bounded,
            "cross_year": self.cross_year,
            "label": self.label,
        }


def _precision_rank(precision: DatePrecision) -> int:
    """Rank a precision, coarsest first for use with :func:`max`."""
    return _PRECISION_ORDER.index(precision)


def _partial_date_from_key(key: tuple[int, int, int], precision: DatePrecision) -> PartialDate:
    """Rebuild a :class:`PartialDate` from a comparable sort-key tuple."""
    year, month, day = key
    return PartialDate(year, month, day, precision)


@dataclass(frozen=True)
class SeasonSpan:
    """The bounding rule of one season within a :class:`SeasonCalendar`.

    Month/day pairs are anchored on the season's nominal year; the year
    offsets move a boundary into the previous or next year. ``end_day`` may be
    ``None``, meaning the last day of ``end_month`` (so February ends on the
    28th or 29th depending on the leap year).
    """

    start_month: int
    start_day: int
    start_year_offset: int
    end_month: int
    end_day: Optional[int]
    end_year_offset: int


@dataclass(frozen=True)
class SeasonCalendar:
    """Maps seasons to concrete bounding rules, as supplied by the query.

    A calendar is deliberately explicit: seasons are only converted to bounded
    ranges when the calendar queried for the data defines them. Construct one
    with :meth:`meteorological` for the standard Northern-Hemisphere
    meteorological seasons, or via :meth:`from_mapping` for custom tables.
    """

    spans: dict[Season, SeasonSpan] = field(default_factory=dict)

    def __contains__(self, season: Season) -> bool:
        """Whether this calendar defines a bounding rule for the season."""
        return season in self.spans

    @classmethod
    def meteorological(cls) -> "SeasonCalendar":
        """The Northern-Hemisphere meteorological calendar.

        Winter spans Dec 1 through the end of February and therefore crosses
        the year boundary; spring, summer and autumn stay within one year.
        """
        return cls(
            spans={
                Season.WINTER: SeasonSpan(12, 1, 0, 2, None, 1),
                Season.SPRING: SeasonSpan(3, 1, 0, 5, 31, 0),
                Season.SUMMER: SeasonSpan(6, 1, 0, 8, 31, 0),
                Season.AUTUMN: SeasonSpan(9, 1, 0, 11, 30, 0),
            }
        )

    @classmethod
    def from_mapping(cls, mapping: dict[str, dict]) -> "SeasonCalendar":
        """Build a calendar from plain data, e.g. parsed from JSON or YAML.

        Each key is a season name or alias (``"fall"`` accepted); each value
        has ``"start"`` and ``"end"`` entries of the form
        ``[month, day, year_offset]`` where ``day`` may be ``null`` for the
        last day of the month.
        """
        spans: dict[Season, SeasonSpan] = {}
        for raw_season, raw_span in mapping.items():
            season = parse_season(raw_season)
            start_month, start_day, start_offset = raw_span["start"]
            end_month, end_day, end_offset = raw_span["end"]
            spans[season] = SeasonSpan(
                start_month=int(start_month),
                start_day=int(start_day),
                start_year_offset=int(start_offset),
                end_month=int(end_month),
                end_day=None if end_day is None else int(end_day),
                end_year_offset=int(end_offset),
            )
        return cls(spans=spans)

    def season_range(
        self, season: Season, year: Optional[int], label: Optional[str] = None
    ) -> DateRange:
        """Resolve a season to a bounded range for the given year.

        Raises :class:`SeasonNotInCalendarError` when the calendar does not
        define the season, and returns an unbounded season range when ``year``
        is ``None`` because no anchor year is available.
        """
        if year is None:
            return DateRange.unbounded(precision=DatePrecision.SEASON, label=label)
        if season not in self.spans:
            raise SeasonNotInCalendarError(
                f"Season {season.value!r} is not defined in this season calendar"
            )
        span = self.spans[season]
        start_year = year + span.start_year_offset
        end_year = year + span.end_year_offset
        end_day = span.end_day
        if end_day is None:
            end_day = _last_day_of_month(end_year, span.end_month)
        return DateRange(
            start=PartialDate(start_year, span.start_month, span.start_day, DatePrecision.SEASON),
            end=PartialDate(end_year, span.end_month, end_day, DatePrecision.SEASON),
            precision=DatePrecision.SEASON,
            cross_year=end_year > start_year,
            label=label,
        )


@dataclass(frozen=True)
class DateSource:
    """One independent date statement about the same record.

    ``name`` is the provenance (a bibtex field name such as ``"date"`` or the
    synthetic ``"year-month"`` source); ``date_range`` is what it states.
    """

    name: str
    date_range: DateRange


@dataclass(frozen=True)
class DateConflict:
    """A pair (or set) of sources whose date ranges do not intersect."""

    sources: tuple[DateSource, ...]
    reason: str

    def to_dict(self) -> dict:
        """A JSON-serializable representation for metadata and conflict lists."""
        return {
            "sources": [source.name for source in self.sources],
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ReconciliationResult:
    """The outcome of reconciling several independent date sources.

    When every source agrees on a common interval, ``overlap`` is that
    interval and ``conflicts`` is empty. A partial overlap still yields the
    shared interval, with all sources retained as provenance. Completely
    disjoint sources leave ``overlap`` as ``None`` and populate
    :attr:`conflicts` instead.
    """

    sources: tuple[DateSource, ...]
    overlap: Optional[DateRange]
    conflicts: tuple[DateConflict, ...]

    @property
    def reconciled(self) -> bool:
        """Whether all sources share a non-empty overlap."""
        return self.overlap is not None and not self.conflicts

    def to_dict(self) -> dict:
        """A JSON-serializable representation for parser metadata."""
        return {
            "reconciled": self.reconciled,
            "overlap": None if self.overlap is None else self.overlap.to_dict(),
            "sources": [
                {"name": source.name, **source.date_range.to_dict()} for source in self.sources
            ],
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
        }


def reconcile_sources(sources: list[DateSource]) -> ReconciliationResult:
    """Reconcile independent date sources from the same record.

    Partially overlapping sources are intersected, keeping every source as
    provenance of the two sides of the overlap. Fully disjoint source pairs
    are reported as conflicts instead of being force-merged. Unbounded sources
    never constrain the overlap but stay listed as provenance.
    """
    if not sources:
        raise ValueError("At least one date source is required for reconciliation")

    conflicts: list[DateConflict] = []
    for index, left in enumerate(sources):
        for right in sources[index + 1 :]:
            if left.date_range.is_disjoint(right.date_range):
                conflicts.append(
                    DateConflict(
                        sources=(left, right),
                        reason=(
                            f"Date sources {left.name!r} and {right.name!r} do not intersect: "
                            f"{left.date_range.to_dict()} vs {right.date_range.to_dict()}"
                        ),
                    )
                )

    overlap: Optional[DateRange] = None
    bounded_sources = [source for source in sources if source.date_range.bounded]
    if bounded_sources:
        overlap = bounded_sources[0].date_range
        for source in bounded_sources[1:]:
            assert overlap is not None
            overlap = overlap.intersection(source.date_range)
            if overlap is None:
                break
    else:
        overlap = DateRange.unbounded(
            precision=max((source.date_range.precision for source in sources), key=_precision_rank)
        )

    return ReconciliationResult(
        sources=tuple(sources),
        overlap=None if conflicts else overlap,
        conflicts=tuple(conflicts),
    )


_ACADEMIC_YEAR_PATTERN = re.compile(r"^(\d{4})\s*[/\-–]\s*(\d{2,4})$")
_ISO_DATE_PATTERN = re.compile(r"^(\d{4})(?:[-/](\d{1,2}))?(?:[-/](\d{1,2}))?$")
_SEASON_FIRST_PATTERN = re.compile(r"^([A-Za-z]+)\s+(\d{4})$")
_SEASON_ONLY_PATTERN = re.compile(r"^([A-Za-z]+)$")
_YEAR_FIRST_SEASON_PATTERN = re.compile(r"^(\d{4})\s+([A-Za-z]+)$")


def parse_season(text: str) -> Season:
    """Parse a season name, accepting common abbreviations and ``"fall"``."""
    normalized = text.strip().lower()
    try:
        return _SEASON_ALIASES[normalized]
    except KeyError as exc:
        raise UnparseableDateError(f"Not a season name: {text!r}") from exc


def parse_month(text: str | int) -> int:
    """Parse a month given as number, abbreviation or full name."""
    if isinstance(text, int):
        month = text
    else:
        stripped = text.strip().lower()
        if stripped.isdigit():
            month = int(stripped)
        else:
            try:
                month = _MONTH_ALIASES[stripped]
            except KeyError as exc:
                raise UnparseableDateError(f"Not a month: {text!r}") from exc
    if not 1 <= month <= 12:
        raise InvalidDateError(f"Month must be between 1 and 12, got {month}")
    return month


def _try_parse_academic_year(text: str) -> Optional[DateRange]:
    """Parse ``"1997/98"`` / ``"1997-1998"``, or return ``None`` if it is not one.

    A two- or four-digit end year is only accepted as an academic year when it
    names the year immediately after the start year. This keeps ``"1997-12"``
    an ISO month instead of misreading it as a (nonsensical) academic year.
    """
    match = _ACADEMIC_YEAR_PATTERN.match(text)
    if match is None:
        return None
    start_year = int(match.group(1))
    raw_end = match.group(2)
    # An academic year always ends in the calendar year after its start year,
    # so derive the end year from the start instead of century arithmetic
    # (the latter breaks the millennium rollover, e.g. "1999/00").
    expected_end_year = start_year + 1
    if len(raw_end) == 4:
        if int(raw_end) != expected_end_year:
            return None
    elif int(raw_end) != expected_end_year % 100:
        return None
    return DateRange.for_academic_year(start_year, label=text)


def parse_date_text(
    text: str,
    calendar: Optional[SeasonCalendar] = None,
    anchor_year: Optional[int] = None,
) -> DateRange:
    """Parse a free-text date into a precision-preserving bounded range.

    Supported shapes include plain years (``"1998"``), calendar dates
    (``"1998-03"``, ``"1998-03-01"``, slash-separated as well), academic years
    (``"1997/98"`` or ``"1997-1998"``) and seasons (``"Spring 1998"`` or
    ``"1998 Spring"``).

    Seasons require a ``calendar``. Without one, or when the calendar lacks the
    season, the season resolves to an unbounded season range rather than a
    fabricated January 1st. ``anchor_year`` anchors a bare season name.
    """
    cleaned = text.strip()

    academic_range = _try_parse_academic_year(cleaned)
    if academic_range is not None:
        return academic_range

    iso_match = _ISO_DATE_PATTERN.match(cleaned)
    if iso_match:
        year = int(iso_match.group(1))
        raw_month = iso_match.group(2)
        raw_day = iso_match.group(3)
        if raw_month is None:
            return DateRange.for_year(year, label=cleaned)
        month = int(raw_month)
        if raw_day is None:
            if not 1 <= month <= 12:
                raise InvalidDateError(f"Month must be between 1 and 12, got {month}")
            return DateRange.for_month(year, month, label=cleaned)
        day = int(raw_day)
        return DateRange.for_day(year, month, day, label=cleaned)

    season_first = _SEASON_FIRST_PATTERN.match(cleaned)
    if season_first:
        season = parse_season(season_first.group(1))
        year = int(season_first.group(2))
        return _resolve_season(season, year, cleaned, calendar)

    year_first = _YEAR_FIRST_SEASON_PATTERN.match(cleaned)
    if year_first:
        season = parse_season(year_first.group(2))
        year = int(year_first.group(1))
        return _resolve_season(season, year, cleaned, calendar)

    season_only = _SEASON_ONLY_PATTERN.match(cleaned)
    if season_only:
        season = parse_season(season_only.group(1))
        return _resolve_season(season, anchor_year, cleaned, calendar)

    raise UnparseableDateError(f"Could not parse date text: {text!r}")


def _resolve_season(
    season: Season,
    year: Optional[int],
    label: str,
    calendar: Optional[SeasonCalendar],
) -> DateRange:
    """Resolve a season against the calendar, or return an unbounded range."""
    if calendar is None or season not in calendar:
        # The queried calendar defines no rule for this season: the extent
        # stays unknown instead of being silently pinned to January 1st.
        return DateRange.unbounded(precision=DatePrecision.SEASON, label=label)
    return calendar.season_range(season, year, label=label)
