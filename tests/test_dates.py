import pytest

from bibtexparser.dates import DateConflict
from bibtexparser.dates import DateError
from bibtexparser.dates import DatePrecision
from bibtexparser.dates import DateRange
from bibtexparser.dates import DateSource
from bibtexparser.dates import InvalidDateError
from bibtexparser.dates import PartialDate
from bibtexparser.dates import Season
from bibtexparser.dates import SeasonCalendar
from bibtexparser.dates import SeasonNotInCalendarError
from bibtexparser.dates import UnparseableDateError
from bibtexparser.dates import parse_date_text
from bibtexparser.dates import parse_month
from bibtexparser.dates import parse_season
from bibtexparser.dates import reconcile_sources


class TestPartialDate:
    def test_components_and_iso_rendering(self):
        assert PartialDate(1998).to_iso() == "1998"
        assert PartialDate(1998, 3).to_iso() == "1998-03"
        assert PartialDate(1998, 3, 1).to_iso() == "1998-03-01"
        assert PartialDate(None).to_iso() is None

    def test_invalid_month_and_day_rejected(self):
        with pytest.raises(InvalidDateError):
            PartialDate(1998, 13)
        with pytest.raises(InvalidDateError):
            PartialDate(1998, 2, 30)
        with pytest.raises(InvalidDateError):
            PartialDate(1998, None, 1)

    def test_leap_year_february_accepted(self):
        assert PartialDate(2000, 2, 29).day == 29
        with pytest.raises(InvalidDateError):
            PartialDate(1999, 2, 29)

    def test_unbounded_anchor_cannot_carry_month_or_day(self):
        with pytest.raises(InvalidDateError):
            PartialDate(None, 1)

    def test_sort_key(self):
        assert PartialDate(1998, 1, 1).sort_key == (1998, 1, 1)
        assert PartialDate(1998).sort_key == (1998, 1, 1)
        assert PartialDate(None).sort_key is None


class TestDateRange:
    def test_year_range_is_full_year_not_january_only(self):
        year_range = DateRange.for_year(1998)
        assert year_range.start.to_iso() == "1998-01-01"
        assert year_range.end.to_iso() == "1998-12-31"
        assert year_range.precision is DatePrecision.YEAR

    def test_month_range_uses_real_month_end(self):
        assert DateRange.for_month(1998, 2).end.to_iso() == "1998-02-28"
        assert DateRange.for_month(2000, 2).end.to_iso() == "2000-02-29"
        assert DateRange.for_month(1998, 4).end.to_iso() == "1998-04-30"

    def test_day_range(self):
        day_range = DateRange.for_day(1998, 3, 15)
        assert day_range.start.to_iso() == day_range.end.to_iso() == "1998-03-15"
        assert day_range.precision is DatePrecision.DAY

    def test_academic_year_crosses_boundary_and_is_not_clipped(self):
        academic = DateRange.for_academic_year(1997)
        assert academic.start.to_iso() == "1997-09-01"
        assert academic.end.to_iso() == "1998-08-31"
        assert academic.cross_year is True
        assert academic.precision is DatePrecision.ACADEMIC_YEAR

    def test_start_after_end_rejected(self):
        with pytest.raises(InvalidDateError):
            DateRange(
                start=PartialDate(1998, 6, 1),
                end=PartialDate(1998, 1, 1),
                precision=DatePrecision.DAY,
            )

    def test_unbounded_range_has_no_anchor(self):
        unresolved = DateRange.unbounded(DatePrecision.SEASON, label="Spring 1998")
        assert unresolved.bounded is False
        assert unresolved.start.year is None
        assert unresolved.end.year is None
        assert unresolved.to_dict()["bounded"] is False


class TestSeasonCalendar:
    def test_meteorological_spring_is_bounded(self):
        calendar = SeasonCalendar.meteorological()
        spring = calendar.season_range(Season.SPRING, 1998)
        assert spring.bounded is True
        assert spring.start.to_iso() == "1998-03-01"
        assert spring.end.to_iso() == "1998-05-31"
        assert spring.precision is DatePrecision.SEASON

    def test_meteorological_winter_crosses_year_boundary(self):
        winter = SeasonCalendar.meteorological().season_range(Season.WINTER, 1998)
        assert winter.start.to_iso() == "1998-12-01"
        assert winter.end.to_iso() == "1999-02-28"
        assert winter.cross_year is True

    def test_missing_season_raises_on_direct_calendar_lookup(self):
        calendar = SeasonCalendar(spans={})
        with pytest.raises(SeasonNotInCalendarError):
            calendar.season_range(Season.SPRING, 1998)

    def test_season_without_year_is_unbounded(self):
        season_range = SeasonCalendar.meteorological().season_range(Season.SPRING, None)
        assert season_range.bounded is False
        assert season_range.precision is DatePrecision.SEASON

    def test_custom_calendar_from_mapping(self):
        calendar = SeasonCalendar.from_mapping(
            {
                "spring": {"start": [4, 1, 0], "end": [6, None, 0]},
            }
        )
        spring = calendar.season_range(Season.SPRING, 1998)
        assert spring.start.to_iso() == "1998-04-01"
        assert spring.end.to_iso() == "1998-06-30"

    def test_fall_alias_resolves_to_autumn(self):
        assert parse_season("fall") is Season.AUTUMN
        assert parse_season("Spr") is Season.SPRING


class TestParseDateText:
    def test_plain_year_keeps_year_precision(self):
        parsed = parse_date_text("1998")
        assert parsed.precision is DatePrecision.YEAR
        assert parsed.start.to_iso() == "1998-01-01"
        assert parsed.end.to_iso() == "1998-12-31"

    def test_monthly_publication_dates(self):
        assert parse_date_text("1998-03").precision is DatePrecision.MONTH
        assert parse_date_text("1998/03").start.to_iso() == "1998-03-01"
        assert parse_date_text("1998-03-15").precision is DatePrecision.DAY

    def test_spring_resolved_through_query_calendar(self):
        calendar = SeasonCalendar.meteorological()
        spring = parse_date_text("Spring 1998", calendar=calendar)
        assert spring.start.to_iso() == "1998-03-01"
        assert spring.end.to_iso() == "1998-05-31"
        reordered = parse_date_text("1998 Spring", calendar=calendar)
        assert reordered.start == spring.start
        assert reordered.end == spring.end
        # The original wording is retained as the range label.
        assert spring.label == "Spring 1998"

    def test_spring_without_calendar_stays_unbounded(self):
        unresolved = parse_date_text("Spring 1998")
        assert unresolved.bounded is False
        assert unresolved.precision is DatePrecision.SEASON
        assert unresolved.start.year is None

    def test_spring_unknown_to_calendar_stays_unbounded(self):
        summer_span = SeasonCalendar.meteorological().spans[Season.SUMMER]
        calendar = SeasonCalendar(spans={Season.SUMMER: summer_span})
        unresolved = parse_date_text("Spring 1998", calendar=calendar)
        assert unresolved.bounded is False

    def test_bare_season_uses_anchor_year(self):
        calendar = SeasonCalendar.meteorological()
        anchored = parse_date_text("Spring", calendar=calendar, anchor_year=1998)
        assert anchored.start.to_iso() == "1998-03-01"
        bare = parse_date_text("Spring", calendar=calendar)
        assert bare.bounded is False

    def test_academic_year_notation_keeps_end_in_next_year(self):
        for notation in ("1997/98", "1997-1998", "1997–98"):
            parsed = parse_date_text(notation)
            assert parsed.start.to_iso() == "1997-09-01"
            assert parsed.end.to_iso() == "1998-08-31"
            assert parsed.cross_year is True

    def test_two_digit_academic_year_handles_millennium_rollover(self):
        parsed = parse_date_text("1999/00")
        assert parsed.start.to_iso() == "1999-09-01"
        assert parsed.end.to_iso() == "2000-08-31"
        assert parsed.cross_year is True

    def test_non_adjacent_slash_years_are_not_academic_years(self):
        # "1997/99" does not name two consecutive years, so it is not an
        # academic year; the slash form is then read as an (invalid) month.
        with pytest.raises(DateError):
            parse_date_text("1997/99")

    def test_iso_month_is_not_misread_as_academic_year(self):
        parsed = parse_date_text("1997-12")
        assert parsed.precision is DatePrecision.MONTH
        assert parsed.start.to_iso() == "1997-12-01"

    def test_unparseable_text_raises(self):
        with pytest.raises(UnparseableDateError):
            parse_date_text("not a date")
        with pytest.raises(UnparseableDateError):
            parse_season("january")

    def test_invalid_calendar_date_raises(self):
        with pytest.raises(InvalidDateError):
            parse_date_text("1998-13-01")

    def test_month_parsing_accepts_names_and_numbers(self):
        assert parse_month("jan") == 1
        assert parse_month("August") == 8
        assert parse_month(12) == 12
        with pytest.raises(InvalidDateError):
            parse_month(0)
        with pytest.raises(UnparseableDateError):
            parse_month("xyz")


class TestReconcile:
    @staticmethod
    def _source(name: str, date_range: DateRange) -> DateSource:
        return DateSource(name=name, date_range=date_range)

    def test_contained_ranges_reconcile_to_overlap(self):
        year = DateRange.for_year(1998)
        spring = SeasonCalendar.meteorological().season_range(Season.SPRING, 1998)
        result = reconcile_sources([
            self._source("date", spring),
            self._source("year/month", year),
        ])
        assert result.reconciled is True
        assert result.overlap.start.to_iso() == "1998-03-01"
        assert result.overlap.end.to_iso() == "1998-05-31"
        assert {source.name for source in result.sources} == {"date", "year/month"}

    def test_partial_overlap_exports_intersection_and_both_provenances(self):
        spring = SeasonCalendar.meteorological().season_range(Season.SPRING, 1998)
        april = DateRange.for_month(1998, 4)
        result = reconcile_sources([
            self._source("date", spring),
            self._source("year/month", april),
        ])
        assert result.reconciled is True
        assert result.overlap.start.to_iso() == "1998-04-01"
        assert result.overlap.end.to_iso() == "1998-04-30"
        assert len(result.sources) == 2
        assert result.conflicts == ()

    def test_disjoint_sources_go_to_conflict_list(self):
        spring_1998 = SeasonCalendar.meteorological().season_range(Season.SPRING, 1998)
        winter_1999 = SeasonCalendar.meteorological().season_range(Season.WINTER, 1999)
        result = reconcile_sources([
            self._source("date", spring_1998),
            self._source("year/month", winter_1999),
        ])
        assert result.overlap is None
        assert result.reconciled is False
        assert len(result.conflicts) == 1
        conflict = result.conflicts[0]
        assert isinstance(conflict, DateConflict)
        assert {source.name for source in conflict.sources} == {"date", "year/month"}
        assert result.to_dict()["conflicts"][0]["sources"] == ["date", "year/month"]

    def test_unbounded_source_does_not_constrain_overlap(self):
        unresolved = DateRange.unbounded(DatePrecision.SEASON)
        year = DateRange.for_year(1998)
        result = reconcile_sources([
            self._source("date", unresolved),
            self._source("year/month", year),
        ])
        assert result.reconciled is True
        assert result.overlap.start.to_iso() == "1998-01-01"
        assert result.overlap.end.to_iso() == "1998-12-31"

    def test_only_unbounded_sources_stay_unbounded(self):
        result = reconcile_sources([
            self._source("date", DateRange.unbounded(DatePrecision.SEASON)),
        ])
        assert result.overlap is not None
        assert result.overlap.bounded is False

    def test_requires_sources(self):
        with pytest.raises(ValueError):
            reconcile_sources([])

    def test_intersection_keeps_cross_year_flag_on_spanning_overlap(self):
        academic = DateRange.for_academic_year(1997)
        winter = SeasonCalendar.meteorological().season_range(Season.WINTER, 1997)
        overlap = academic.intersection(winter)
        assert overlap is not None
        assert overlap.start.to_iso() == "1997-12-01"
        assert overlap.end.to_iso() == "1998-02-28"
        assert overlap.cross_year is True

    def test_season_inside_year_box_keeps_season_precision(self):
        spring = SeasonCalendar.meteorological().season_range(Season.SPRING, 1998)
        overlap = spring.intersection(DateRange.for_year(1998))
        assert overlap is not None
        assert overlap.precision is DatePrecision.SEASON
        assert overlap.start.to_iso() == "1998-03-01"
        assert overlap.end.to_iso() == "1998-05-31"

    def test_cross_year_winter_is_not_clipped_by_year_box(self):
        winter = SeasonCalendar.meteorological().season_range(Season.WINTER, 1998)
        overlap = winter.intersection(DateRange.for_year(1998))
        assert overlap is not None
        assert overlap.start.to_iso() == "1998-12-01"
        assert overlap.end.to_iso() == "1999-02-28"
        assert overlap.cross_year is True
