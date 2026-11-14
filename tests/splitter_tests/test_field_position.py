"""Tests for the exact, per-field source positions captured by the splitter.

Positions make duplicate fields (e.g. both ``pages`` and ``PAGES``) individually
addressable even when their values contain commas and nested braces.
"""

from textwrap import dedent

from bibtexparser.model import DuplicateFieldKeyBlock
from bibtexparser.model import Field
from bibtexparser.splitter import Splitter


def _parse_entry(bibtex_str: str):
    library = Splitter(bibtex_str).split()
    assert len(library.failed_blocks) == 0
    return library.entries[0]


def test_position_slices_round_trip_through_entry_raw():
    bibtex_str = dedent("""\
        @article{test,
            title = {A title},
            year = {2020}
        }""")
    entry = _parse_entry(bibtex_str)
    for field in entry.fields:
        position = field.position
        assert position is not None
        assert position.key_text(entry.raw).strip() == field.key
        assert position.value_text(entry.raw).strip() == field.value


def test_raw_value_keeps_whitespace_and_enclosing():
    bibtex_str = dedent("""\
        @article{test,
            year = {2020}
        }""")
    entry = _parse_entry(bibtex_str)
    field = entry.fields_dict["year"]
    # The trimmed value is the public one; the raw value preserves spacing/newline.
    assert field.value == "{2020}"
    assert field.raw_value.strip() == "{2020}"
    assert field.raw_value == entry.raw[field.position.value_start : field.position.value_end]


def test_duplicate_fields_have_distinct_precise_positions():
    bibtex_str = dedent("""\
        @article{test,
            title = {First},
            title = {Second},
            title = {Third}
        }""")
    library = Splitter(bibtex_str).split()
    assert len(library.failed_blocks) == 1
    block = library.failed_blocks[0]
    assert isinstance(block, DuplicateFieldKeyBlock)
    entry = block.ignore_error_block

    title_fields = [field for field in entry.fields if field.key == "title"]
    assert len(title_fields) == 3
    spans = [field.position.value_text(entry.raw).strip() for field in title_fields]
    assert spans == ["{First}", "{Second}", "{Third}"]
    # Positions are ordered and do not overlap.
    starts = [field.position.value_start for field in title_fields]
    assert starts == sorted(starts)
    assert len(set(starts)) == 3


def test_position_handles_commas_and_nested_braces_in_value():
    # The commas/braces live *inside* the value, so the span must cover all of it.
    bibtex_str = dedent("""\
        @article{test,
            author = {Doe, John and {Braces, Inc.}},
            note = {a, b, {c, d}, e},
            pages = {1--10}
        }""")
    entry = _parse_entry(bibtex_str)
    note = entry.fields_dict["note"]
    assert note.position.value_text(entry.raw).strip() == "{a, b, {c, d}, e}"
    author = entry.fields_dict["author"]
    assert author.position.value_text(entry.raw).strip() == "{Doe, John and {Braces, Inc.}}"
    # The field after the brace-heavy values is still located correctly.
    assert entry.fields_dict["pages"].position.value_text(entry.raw).strip() == "{1--10}"


def test_case_distinct_duplicate_keys_are_both_positioned():
    # ``pages`` and ``PAGES`` are not duplicates to the case-sensitive splitter,
    # but both must carry an exact, independent position.
    bibtex_str = dedent("""\
        @article{test,
            pages = {1--10},
            PAGES = {1-10}
        }""")
    entry = _parse_entry(bibtex_str)
    assert [field.key for field in entry.fields] == ["pages", "PAGES"]
    spans = [field.position.value_text(entry.raw).strip() for field in entry.fields]
    assert spans == ["{1--10}", "{1-10}"]


def test_position_is_none_for_programmatic_field():
    field = Field("pages", "{1--10}")
    assert field.position is None
    assert field.raw_value is None


def test_parsed_field_equals_constructed_field_despite_position():
    bibtex_str = "@article{test, pages = {1--10}}"
    entry = _parse_entry(bibtex_str)
    parsed = entry.fields_dict["pages"]
    constructed = Field("pages", "{1--10}", start_line=parsed.start_line)
    # Provenance is excluded from equality, so parsed and constructed match.
    assert parsed == constructed
    assert hash(parsed) == hash(constructed)


def test_position_in_parenthesis_delimited_entry():
    bibtex_str = dedent("""\
        @article(test,
            pages = {1--10},
            PAGES = {1-10}
        )""")
    entry = _parse_entry(bibtex_str)
    spans = [field.position.value_text(entry.raw).strip() for field in entry.fields]
    assert spans == ["{1--10}", "{1-10}"]
    assert [field.position.key_text(entry.raw).strip() for field in entry.fields] == [
        "pages",
        "PAGES",
    ]
