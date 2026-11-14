"""Middleware turning free-text bibliographic dates into bounded date ranges.

The :class:`DateRangeMiddleware` treats every independent date statement on an
entry (a free-text ``date`` field and the entry's own ``year``/``month``
fields) as a separate :class:`~bibtexparser.dates.DateSource`, resolves them
with the season calendar supplied by the query, and reconciles them:

* partially overlapping sources are intersected, exporting the shared overlap
  together with the provenance of every source on both sides;
* completely disjoint sources are not force-merged but reported in a conflict
  list, retrievable per entry or for the whole library;
* seasons that the calendar cannot resolve stay explicitly unbounded instead
  of being pinned to January 1st, and cross-year ranges keep their end date in
  the following year.

The structured result is always stored in the entry's ``parser_metadata``;
human-readable bibtex fields (the reconciled ``date``, its sources and any
conflicts) are added on top and can be renamed or switched off.
"""

from bibtexparser.dates import DateRange
from bibtexparser.dates import DateSource
from bibtexparser.dates import InvalidDateError
from bibtexparser.dates import ReconciliationResult
from bibtexparser.dates import SeasonCalendar
from bibtexparser.dates import UnparseableDateError
from bibtexparser.dates import parse_date_text
from bibtexparser.dates import parse_month
from bibtexparser.dates import reconcile_sources
from bibtexparser.library import Library
from bibtexparser.model import Block
from bibtexparser.model import Entry
from bibtexparser.model import Field

from .middleware import BlockMiddleware

#: Provenance label used for the range derived from an entry's year/month fields.
YEAR_MONTH_SOURCE = "year/month"


def _interval_string(date_range: DateRange) -> str:
    """Render a range as an ISO-8601 interval, or its label when unbounded."""
    if not date_range.bounded:
        return date_range.label or "unbounded"
    start = date_range.start.to_iso()
    end = date_range.end.to_iso()
    if start == end:
        return start
    return f"{start}/{end}"


def _source_line(source: DateSource) -> str:
    """Render one provenance entry, e.g. ``date: 1998-03/1998-05 (season)``."""
    date_range = source.date_range
    rendered = _interval_string(date_range)
    if date_range.bounded:
        return f"{source.name}: {rendered} ({date_range.precision.value})"
    return f"{source.name}: {rendered} (unbounded)"


def collect_date_conflicts(library: Library) -> list[dict]:
    """All date conflicts found in a library, one dict per conflicting entry.

    The entries are returned in library order, which doubles as the conflict
    list requested when independent date sources are fully disjoint.
    """
    conflicts: list[dict] = []
    for entry in library.entries:
        metadata = entry.get_parser_metadata(DateRangeMiddleware.metadata_key())
        if metadata and metadata.get("conflicts"):
            conflicts.append({"entry": entry.key, **metadata})
    return conflicts


class DateRangeMiddleware(BlockMiddleware):
    """Resolve entry dates into precision-preserving, reconciled date ranges.

    :param season_calendar: The calendar used to convert season names (e.g.
        ``"Spring 1998"``) into bounded ranges. When ``None`` (or when the
        calendar lacks a season), season dates remain unbounded.
    :param date_fields: Field names holding free-text dates. Each becomes an
        independent date source.
    :param year_field: Field holding the entry's catalog year.
    :param month_field: Field holding the entry's catalog month (optional).
    :param date_output_field: Name of the field the reconciled overlap is
        written to. Set to ``None`` to skip writing it.
    :param source_output_field: Name of the field listing the provenance of
        every reconciled source. Set to ``None`` to skip writing it.
    :param conflict_output_field: Name of the field listing conflicts. Set to
        ``None`` to keep conflicts only in ``parser_metadata`` and
        :func:`collect_date_conflicts`.
    :param allow_inplace_modification: See :class:`BlockMiddleware`.
    """

    def __init__(
        self,
        season_calendar: SeasonCalendar | None = None,
        date_fields: tuple[str, ...] = ("date",),
        year_field: str = "year",
        month_field: str = "month",
        date_output_field: str | None = "date",
        source_output_field: str | None = "date-sources",
        conflict_output_field: str | None = "dateconflicts",
        allow_inplace_modification: bool = True,
    ):
        super().__init__(
            allow_inplace_modification=allow_inplace_modification,
            allow_parallel_execution=True,
        )
        self._season_calendar = season_calendar
        self._date_fields = tuple(date_fields)
        self._year_field = year_field
        self._month_field = month_field
        self._date_output_field = date_output_field
        self._source_output_field = source_output_field
        self._conflict_output_field = conflict_output_field

    # docstr-coverage: inherited
    @classmethod
    def metadata_key(cls) -> str:
        return "DateRangeMiddleware"

    def _free_text_sources(self, entry: Entry, unparseable: list[str]) -> list[DateSource]:
        """Parse every configured free-text date field into a date source."""
        sources: list[DateSource] = []
        for field_name in self._date_fields:
            field = entry.fields_dict.get(field_name)
            if field is None:
                continue
            raw_value = field.value
            if isinstance(raw_value, int):
                # Unquoted bibtex numbers are parsed as ints; treat a bare
                # integer date field as a plain year statement.
                raw_text = str(raw_value)
            elif isinstance(raw_value, str):
                raw_text = raw_value
            else:
                unparseable.append(f"{field_name}: {raw_value!r} is not a text date")
                continue
            try:
                date_range = parse_date_text(raw_text, calendar=self._season_calendar)
            except (UnparseableDateError, InvalidDateError) as error:
                unparseable.append(f"{field_name}: {error}")
                continue
            sources.append(DateSource(name=field_name, date_range=date_range))
        return sources

    def _year_month_source(self, entry: Entry) -> DateSource | None:
        """Build the range stated by the entry's own year/month registration."""
        year_field = entry.fields_dict.get(self._year_field)
        if year_field is None:
            return None
        try:
            year = int(str(year_field.value).strip())
        except (TypeError, ValueError):
            return None

        month_field = entry.fields_dict.get(self._month_field)
        if month_field is None:
            return DateSource(
                name=YEAR_MONTH_SOURCE,
                date_range=DateRange.for_year(year, label=str(year_field.value)),
            )
        try:
            month = parse_month(month_field.value)
        except (UnparseableDateError, InvalidDateError):
            # The year is still a valid source; the unparseable month is ignored here
            # rather than downgrading the year-only evidence.
            return DateSource(
                name=YEAR_MONTH_SOURCE,
                date_range=DateRange.for_year(year, label=str(year_field.value)),
            )
        return DateSource(
            name=YEAR_MONTH_SOURCE,
            date_range=DateRange.for_month(year, month, label=f"{year}-{month:02d}"),
        )

    def _write_output_fields(
        self,
        entry: Entry,
        result: ReconciliationResult,
        unparseable: list[str],
    ) -> None:
        """Write the reconciled date, provenance and conflicts back to the entry."""
        if self._source_output_field is not None and result.sources:
            lines = [_source_line(source) for source in result.sources]
            if unparseable:
                lines.extend(f"unparseable: {note}" for note in unparseable)
            entry.set_field(Field(self._source_output_field, "; ".join(lines)))

        if result.conflicts and self._conflict_output_field is not None:
            entry.set_field(
                Field(
                    self._conflict_output_field,
                    "; ".join(conflict.reason for conflict in result.conflicts),
                )
            )

        if self._date_output_field is None or result.conflicts:
            # Never overwrite a date field with a fabricated merge on conflict.
            return
        if result.overlap is not None:
            entry.set_field(
                Field(self._date_output_field, _interval_string(result.overlap))
            )

    # docstr-coverage: inherited
    def transform_entry(self, entry: Entry, library: "Library") -> Block:
        unparseable: list[str] = []
        sources = self._free_text_sources(entry, unparseable)
        year_month_source = self._year_month_source(entry)
        if year_month_source is not None:
            sources.append(year_month_source)

        if not sources:
            # Nothing date-like to reconcile; surface unparseable notes but
            # otherwise leave the entry untouched.
            if unparseable:
                entry.parser_metadata[self.metadata_key()] = {
                    "reconciled": False,
                    "unparseable": unparseable,
                }
                if self._source_output_field is not None:
                    entry.set_field(
                        Field(
                            self._source_output_field,
                            "; ".join(f"unparseable: {note}" for note in unparseable),
                        )
                    )
            return entry

        result = reconcile_sources(sources)
        entry.parser_metadata[self.metadata_key()] = {
            **result.to_dict(),
            "unparseable": unparseable,
        }
        self._write_output_fields(entry, result, unparseable)
        return entry
