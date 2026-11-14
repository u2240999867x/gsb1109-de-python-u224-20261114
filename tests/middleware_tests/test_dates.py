import bibtexparser
from bibtexparser.dates import DatePrecision
from bibtexparser.dates import SeasonCalendar
from bibtexparser.middlewares.dates import DateRangeMiddleware
from bibtexparser.middlewares.dates import YEAR_MONTH_SOURCE
from bibtexparser.middlewares.dates import collect_date_conflicts


def _library(bibtex: str):
    # Remove enclosing first so free-text values are plain strings.
    return bibtexparser.parse_string(bibtex)


def _middleware(calendar=None, **kwargs):
    return DateRangeMiddleware(season_calendar=calendar, **kwargs)


class TestSeasonAndAcademicYear:
    def test_spring_resolved_against_query_calendar_and_reconciled_with_year(self):
        library = _library(
            """
            @article{a,
              title = {Quarterly report},
              date  = {Spring 1998},
              year  = {1998}
            }
            """
        )
        library = _middleware(SeasonCalendar.meteorological()).transform(library)
        entry = library.entries_dict["a"]
        assert entry["date"] == "1998-03-01/1998-05-31"

        metadata = entry.get_parser_metadata(DateRangeMiddleware.metadata_key())
        overlap = metadata["overlap"]
        assert overlap["start"] == "1998-03-01"
        assert overlap["end"] == "1998-05-31"
        assert overlap["precision"] == DatePrecision.SEASON.value
        source_names = {source["name"] for source in metadata["sources"]}
        assert source_names == {"date", YEAR_MONTH_SOURCE}

    def test_season_without_calendar_stays_unbounded_instead_of_january_first(self):
        library = _library(
            """
            @article{a,
              date = {Spring 1998},
              year = {1998}
            }
            """
        )
        library = _middleware().transform(library)
        entry = library.entries_dict["a"]
        metadata = entry.get_parser_metadata(DateRangeMiddleware.metadata_key())
        date_source = next(source for source in metadata["sources"] if source["name"] == "date")
        assert date_source["bounded"] is False
        assert date_source["start"] is None
        # The year/month source still anchors the reconciled overlap to 1998.
        assert metadata["overlap"]["start"] == "1998-01-01"
        assert metadata["overlap"]["end"] == "1998-12-31"
        assert "unbounded" in entry["date-sources"]

    def test_academic_year_is_not_clipped_to_december(self):
        library = _library(
            """
            @article{a,
              date = {1997/98},
              year = {1997}
            }
            """
        )
        library = _middleware(SeasonCalendar.meteorological()).transform(library)
        entry = library.entries_dict["a"]
        assert entry["date"] == "1997-09-01/1998-08-31"
        overlap = entry.get_parser_metadata(DateRangeMiddleware.metadata_key())["overlap"]
        assert overlap["cross_year"] is True
        assert overlap["end"].startswith("1998")

    def test_winter_season_keeps_end_in_following_year(self):
        library = _library(
            """
            @article{a,
              date = {Winter 1998},
              year = {1998}
            }
            """
        )
        library = _middleware(SeasonCalendar.meteorological()).transform(library)
        entry = library.entries_dict["a"]
        metadata = entry.get_parser_metadata(DateRangeMiddleware.metadata_key())
        winter = next(source for source in metadata["sources"] if source["name"] == "date")
        assert winter["start"] == "1998-12-01"
        assert winter["end"] == "1999-02-28"
        assert winter["cross_year"] is True


class TestMonthlyRegistrations:
    def test_year_and_month_fields_form_a_month_precision_source(self):
        library = _library(
            """
            @article{a,
              title = {Monthly bulletin},
              year  = {1998},
              month = {3}
            }
            """
        )
        library = _middleware(SeasonCalendar.meteorological()).transform(library)
        entry = library.entries_dict["a"]
        metadata = entry.get_parser_metadata(DateRangeMiddleware.metadata_key())
        source = next(
            source for source in metadata["sources"] if source["name"] == YEAR_MONTH_SOURCE
        )
        assert source["start"] == "1998-03-01"
        assert source["end"] == "1998-03-31"
        assert source["precision"] == DatePrecision.MONTH.value

    def test_named_month_field_is_accepted(self):
        library = _library(
            """
            @article{a,
              year  = {1998},
              month = {November}
            }
            """
        )
        library = _middleware(SeasonCalendar.meteorological()).transform(library)
        entry = library.entries_dict["a"]
        assert entry["date"] == "1998-11-01/1998-11-30"

    def test_year_only_source_keeps_year_precision(self):
        library = _library("@article{a, year = {1998}}")
        library = _middleware().transform(library)
        entry = library.entries_dict["a"]
        metadata = entry.get_parser_metadata(DateRangeMiddleware.metadata_key())
        source = metadata["sources"][0]
        assert source["precision"] == DatePrecision.YEAR.value
        assert entry["date"] == "1998-01-01/1998-12-31"


class TestReconciliation:
    def test_partial_overlap_exports_intersection_with_both_sources(self):
        library = _library(
            """
            @article{a,
              date  = {Spring 1998},
              year  = {1998},
              month = {4}
            }
            """
        )
        library = _middleware(SeasonCalendar.meteorological()).transform(library)
        entry = library.entries_dict["a"]
        assert entry["date"] == "1998-04-01/1998-04-30"
        metadata = entry.get_parser_metadata(DateRangeMiddleware.metadata_key())
        assert metadata["reconciled"] is True
        assert metadata["conflicts"] == []
        sources_text = entry["date-sources"]
        assert "date:" in sources_text
        assert YEAR_MONTH_SOURCE in sources_text

    def test_fully_disjoint_sources_go_to_conflict_list(self):
        library = _library(
            """
            @article{a,
              date  = {Spring 1998},
              year  = {1999},
              month = {1}
            }
            """
        )
        library = _middleware(SeasonCalendar.meteorological()).transform(library)
        entry = library.entries_dict["a"]
        metadata = entry.get_parser_metadata(DateRangeMiddleware.metadata_key())
        assert metadata["reconciled"] is False
        assert len(metadata["conflicts"]) == 1
        assert metadata["conflicts"][0]["sources"] == ["date", YEAR_MONTH_SOURCE]
        # A conflict must not overwrite the original date with a fabricated merge.
        assert entry["date"] == "Spring 1998"
        assert "dateconflicts" in entry.fields_dict

    def test_conflicts_collected_library_wide(self):
        library = _library(
            """
            @article{good,
              date = {Spring 1998},
              year = {1998}
            }
            @article{bad,
              date = {Spring 1998},
              year = {2001}
            }
            """
        )
        library = _middleware(SeasonCalendar.meteorological()).transform(library)
        conflicts = collect_date_conflicts(library)
        assert [item["entry"] for item in conflicts] == ["bad"]
        assert conflicts[0]["conflicts"][0]["sources"] == ["date", YEAR_MONTH_SOURCE]

    def test_unparseable_date_is_recorded_but_keeps_other_sources(self):
        library = _library(
            """
            @article{a,
              date = {sometime last year},
              year = {1998}
            }
            """
        )
        library = _middleware(SeasonCalendar.meteorological()).transform(library)
        entry = library.entries_dict["a"]
        metadata = entry.get_parser_metadata(DateRangeMiddleware.metadata_key())
        assert metadata["unparseable"]
        assert entry["date"] == "1998-01-01/1998-12-31"

    def test_entry_without_dates_is_untouched(self):
        library = _library("@article{a, title = {No dates here}}")
        transformed = _middleware().transform(library)
        entry = transformed.entries_dict["a"]
        assert entry.get_parser_metadata(DateRangeMiddleware.metadata_key()) is None
        assert "date" not in entry.fields_dict


class TestOutputControlAndWriting:
    def test_output_fields_can_be_disabled(self):
        library = _library(
            """
            @article{a,
              date = {1998-03},
              year = {1998}
            }
            """
        )
        middleware = DateRangeMiddleware(
            season_calendar=SeasonCalendar.meteorological(),
            date_output_field=None,
            source_output_field=None,
            conflict_output_field=None,
        )
        library = middleware.transform(library)
        entry = library.entries_dict["a"]
        assert entry["date"] == "1998-03"
        assert "date-sources" not in entry.fields_dict
        metadata = entry.get_parser_metadata(DateRangeMiddleware.metadata_key())
        assert metadata["overlap"]["start"] == "1998-03-01"

    def test_reconciled_library_round_trips_through_writer(self):
        library = _library(
            """
            @article{a,
              date = {Spring 1998},
              year = {1998}
            }
            """
        )
        middleware = _middleware(SeasonCalendar.meteorological())
        library = middleware.transform(library)
        written = bibtexparser.write_string(library)
        assert "1998-03-01/1998-05-31" in written
        assert "date-sources" in written

    def test_multiple_independent_date_fields_are_all_sources(self):
        library = _library(
            """
            @article{a,
              date      = {1998-04-15},
              eventdate = {1998-04},
              year      = {1998},
              month     = {4}
            }
            """
        )
        middleware = DateRangeMiddleware(
            season_calendar=SeasonCalendar.meteorological(),
            date_fields=("date", "eventdate"),
        )
        library = middleware.transform(library)
        entry = library.entries_dict["a"]
        metadata = entry.get_parser_metadata(DateRangeMiddleware.metadata_key())
        names = {source["name"] for source in metadata["sources"]}
        assert names == {"date", "eventdate", YEAR_MONTH_SOURCE}
        # The day-precision source narrows the month box to a single day,
        # which renders as one ISO date rather than a start/end pair.
        assert entry["date"] == "1998-04-15"

    def test_distinct_single_day_sources_are_disjoint_conflicts(self):
        library = _library(
            """
            @article{a,
              date      = {1998-04-15},
              eventdate = {1998-04-20},
              year      = {1998}
            }
            """
        )
        middleware = DateRangeMiddleware(
            season_calendar=SeasonCalendar.meteorological(),
            date_fields=("date", "eventdate"),
        )
        library = middleware.transform(library)
        entry = library.entries_dict["a"]
        metadata = entry.get_parser_metadata(DateRangeMiddleware.metadata_key())
        assert metadata["reconciled"] is False
        assert [tuple(conflict["sources"]) for conflict in metadata["conflicts"]] == [
            ("date", "eventdate")
        ]
        assert entry["date"] == "1998-04-15"
