"""Tests for duplicate-field conflict adjudication."""

from textwrap import dedent

import pytest

from bibtexparser.middlewares.field_conflicts import FIELD_CONFLICTS_METADATA_KEY
from bibtexparser.middlewares.field_conflicts import FieldConflict
from bibtexparser.middlewares.field_conflicts import ResolveFieldConflictsMiddleware
from bibtexparser.middlewares.field_conflicts import get_field_conflicts
from bibtexparser.middlewares.field_conflicts import normalize_page_range
from bibtexparser.model import DuplicateFieldKeyBlock
from bibtexparser.splitter import Splitter


def _resolve(bibtex_str: str, **kwargs):
    library = Splitter(bibtex_str).split()
    middleware = ResolveFieldConflictsMiddleware(**kwargs)
    return middleware.transform(library)


# --------------------------------------------------------------------------- #
# Page-range normalization
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1--10", "1--10"),
        ("1-10", "1--10"),
        ("1 – 10", "1--10"),
        ("1–10", "1--10"),
        ("1—10", "1--10"),
        ("{1--10}", "1--10"),
        ('"1-10"', "1--10"),
        (" 1 - 10 ", "1--10"),
        ("12", "12"),
        ("{12}", "12"),
        # Not a single, simple interval -> no automatic normalization.
        ("1,2,3", None),
        ("intro # outro", None),
        ("1--10--12", None),
        ("", None),
    ],
)
def test_normalize_page_range(raw, expected):
    assert normalize_page_range(raw) == expected


# --------------------------------------------------------------------------- #
# Typographical differences of the same interval merge
# --------------------------------------------------------------------------- #
def test_typographic_page_variants_merge_into_one_field():
    bibtex_str = dedent("""\
        @article{test,
            title = {A title},
            pages = {1--10},
            PAGES = {1-10},
            year = {2020}
        }""")
    library = _resolve(bibtex_str)
    assert len(library.failed_blocks) == 0
    entry = library.entries[0]

    assert [field.key for field in entry.fields] == ["title", "pages", "year"]
    assert entry["pages"] == "{1--10}"

    conflicts = get_field_conflicts(entry)
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.is_merged
    assert conflict.canonical_value == "1--10"
    assert conflict.candidates == ["1--10", "1-10"]
    # Other fields remain fully usable.
    assert entry["title"] == "{A title}"
    assert entry["year"] == "{2020}"


def test_identical_values_under_differently_cased_keys_merge():
    bibtex_str = dedent("""\
        @article{test,
            year = {2020},
            YEAR = {2020}
        }""")
    entry = _resolve(bibtex_str).entries[0]
    assert [field.key for field in entry.fields] == ["year"]
    conflict = get_field_conflicts(entry)[0]
    assert conflict.is_merged
    assert conflict.canonical_value is None


# --------------------------------------------------------------------------- #
# Contradictory values stay as candidates; other fields stay usable
# --------------------------------------------------------------------------- #
def test_contradictory_pages_keep_all_candidates():
    bibtex_str = dedent("""\
        @article{test,
            author = {Doe, John and {Braces, Inc.}},
            pages = {1--10},
            PAGES = {20--30},
            note = {a, b, {c, d}}
        }""")
    library = _resolve(bibtex_str)
    entry = library.entries[0]

    conflict = get_field_conflicts(entry)[0]
    assert conflict.is_open
    assert conflict.is_conflicting
    assert conflict.candidates == ["1--10", "20--30"]
    assert conflict.canonical_value is None

    # Both candidates remain present and independently positioned.
    page_fields = [field for field in entry.fields if field.key.lower() == "pages"]
    assert [field.value for field in page_fields] == ["{1--10}", "{20--30}"]
    # The rest of the entry is unaffected and usable.
    assert entry["note"] == "{a, b, {c, d}}"
    assert entry["author"] == "{Doe, John and {Braces, Inc.}}"


def test_conflict_positions_point_exactly_at_each_occurrence():
    bibtex_str = dedent("""\
        @article{test,
            pages = {1--10},
            PAGES = {20--30}
        }""")
    entry = _resolve(bibtex_str).entries[0]
    conflict = get_field_conflicts(entry)[0]
    assert len(conflict.positions) == 2
    assert [position.value_text(entry.raw).strip() for position in conflict.positions] == [
        "{1--10}",
        "{20--30}",
    ]
    assert [position.key_text(entry.raw).strip() for position in conflict.positions] == [
        "pages",
        "PAGES",
    ]


def test_exact_duplicate_key_failed_block_is_recovered():
    # The case-sensitive splitter flags identical keys; the middleware recovers them.
    bibtex_str = dedent("""\
        @article{test,
            pages = {1--10},
            pages = {1-10}
        }""")
    split = Splitter(bibtex_str).split()
    assert isinstance(split.failed_blocks[0], DuplicateFieldKeyBlock)

    library = ResolveFieldConflictsMiddleware().transform(split)
    assert len(library.failed_blocks) == 0
    entry = library.entries[0]
    assert get_field_conflicts(entry)[0].is_merged
    assert entry["pages"] == "{1--10}"


def test_non_page_contradictory_fields_are_not_auto_merged():
    bibtex_str = dedent("""\
        @article{test,
            volume = {1},
            VOLUME = {2}
        }""")
    entry = _resolve(bibtex_str).entries[0]
    conflict = get_field_conflicts(entry)[0]
    assert conflict.is_conflicting
    assert [field.value for field in entry.fields] == ["{1}", "{2}"]


# --------------------------------------------------------------------------- #
# Resolution and single-conflict revert
# --------------------------------------------------------------------------- #
def test_resolve_conflict_by_index_value_and_field():
    bibtex_str = dedent("""\
        @article{test,
            pages = {1--10},
            PAGES = {20--30}
        }""")

    # By index
    entry = _resolve(bibtex_str).entries[0]
    conflict = get_field_conflicts(entry)[0]
    chosen = conflict.resolve(entry, 1)
    assert chosen.value == "{20--30}"
    assert len([f for f in entry.fields if f.key.lower() == "pages"]) == 1
    assert conflict.resolved
    assert not conflict.is_open

    # By candidate value
    entry = _resolve(bibtex_str).entries[0]
    conflict = get_field_conflicts(entry)[0]
    conflict.resolve(entry, "1--10")
    assert entry["pages"] == "{1--10}"

    # By live field instance
    entry = _resolve(bibtex_str).entries[0]
    conflict = get_field_conflicts(entry)[0]
    live = conflict._live[0]
    conflict.resolve(entry, live)
    assert entry["pages"] == "{1--10}"


def test_resolve_invalid_choice_raises():
    bibtex_str = dedent("""\
        @article{test,
            pages = {1--10},
            PAGES = {20--30}
        }""")
    entry = _resolve(bibtex_str).entries[0]
    conflict = get_field_conflicts(entry)[0]
    with pytest.raises(IndexError):
        conflict.resolve(entry, 5)
    with pytest.raises(ValueError):
        conflict.resolve(entry, "999")
    with pytest.raises(ValueError):
        conflict.resolve(entry, object())


def test_revert_restores_only_this_conflict_after_an_edit():
    bibtex_str = dedent("""\
        @article{test,
            author = {Doe},
            pages = {1--10},
            PAGES = {20--30},
            year = {2020}
        }""")
    entry = _resolve(bibtex_str).entries[0]
    conflict = get_field_conflicts(entry)[0]

    chosen = conflict.resolve(entry, 0)
    # The user edits the single surviving page number.
    chosen.value = "{99--99}"
    assert entry["pages"] == "{99--99}"

    conflict.revert(entry)

    # Both original occurrences are back, the edit is gone, other fields intact.
    page_fields = [field for field in entry.fields if field.key.lower() == "pages"]
    assert [field.value for field in page_fields] == ["{1--10}", "{20--30}"]
    assert conflict.is_open
    assert [field.key for field in entry.fields] == ["author", "pages", "PAGES", "year"]
    assert entry["author"] == "{Doe}"
    assert entry["year"] == "{2020}"
    # Restored occurrences keep their exact provenance.
    assert all(position is not None for position in conflict.positions)


def test_revert_an_automatically_merged_conflict():
    bibtex_str = dedent("""\
        @article{test,
            pages = {1--10},
            PAGES = {1-10}
        }""")
    entry = _resolve(bibtex_str).entries[0]
    conflict = get_field_conflicts(entry)[0]
    assert conflict.is_merged

    conflict.revert(entry)
    assert conflict.is_open
    assert [field.value for field in entry.fields if field.key.lower() == "pages"] == [
        "{1--10}",
        "{1-10}",
    ]
    # The adjudication record itself is retained.
    assert conflict.canonical_value == "1--10"


def test_multiple_conflicts_are_reverted_independently():
    bibtex_str = dedent("""\
        @article{test,
            pages = {1--10},
            PAGES = {1-10},
            volume = {1},
            VOLUME = {2}
        }""")
    entry = _resolve(bibtex_str).entries[0]
    by_key = {conflict.key: conflict for conflict in get_field_conflicts(entry)}
    assert set(by_key) == {"pages", "volume"}

    by_key["volume"].resolve(entry, 1)
    assert [field.key for field in entry.fields] == ["pages", "VOLUME"]

    by_key["volume"].revert(entry)
    assert [field.key for field in entry.fields] == ["pages", "volume", "VOLUME"]
    # The pages merge was never touched and stays merged.
    assert by_key["pages"].is_merged


def test_resolve_twice_requires_revert():
    bibtex_str = dedent("""\
        @article{test,
            pages = {1--10},
            PAGES = {20--30}
        }""")
    entry = _resolve(bibtex_str).entries[0]
    conflict = get_field_conflicts(entry)[0]
    conflict.resolve(entry, 0)
    with pytest.raises(ValueError):
        conflict.resolve(entry, 1)


def test_resolve_and_revert_survive_entry_deepcopy():
    # Copying an entry (as the writing pipeline does) clones its fields; the
    # conflict record must re-bind to the copies instead of going stale.
    from copy import deepcopy

    bibtex_str = dedent("""\
        @article{test,
            pages = {1--10},
            PAGES = {20--30}
        }""")
    entry = _resolve(bibtex_str).entries[0]
    copied = deepcopy(entry)
    conflict = get_field_conflicts(copied)[0]

    chosen = conflict.resolve(copied, 1)
    assert chosen.value == "{20--30}"
    assert len(copied.fields) == 1
    conflict.revert(copied)
    assert [field.value for field in copied.fields] == ["{1--10}", "{20--30}"]
    assert conflict.is_open


# --------------------------------------------------------------------------- #
# Middleware mechanics
# --------------------------------------------------------------------------- #
def test_non_inplace_mode_does_not_mutate_source_library():
    bibtex_str = dedent("""\
        @article{test,
            pages = {1--10},
            pages = {1-10}
        }""")
    split = Splitter(bibtex_str).split()
    ResolveFieldConflictsMiddleware(allow_inplace_modification=False).transform(split)
    # The split library still holds the failed block with both occurrences.
    assert isinstance(split.failed_blocks[0], DuplicateFieldKeyBlock)
    assert len(split.failed_blocks[0].ignore_error_block.fields) == 2


def test_entry_without_duplicates_is_passed_through():
    bibtex_str = "@article{test, pages = {1--10}, year = {2020}}"
    library = _resolve(bibtex_str)
    entry = library.entries[0]
    assert get_field_conflicts(entry) == []
    assert entry.get_parser_metadata(FIELD_CONFLICTS_METADATA_KEY) is None


def test_custom_page_fields_are_honored():
    bibtex_str = dedent("""\
        @article{test,
            eid = {1-10},
            EID = {1--10}
        }""")
    # By default, `eid` is not a page field, so the values conflict.
    assert get_field_conflicts(_resolve(bibtex_str).entries[0])[0].is_conflicting
    # Opting in makes the typographic variants merge.
    entry = _resolve(bibtex_str, page_fields=("pages", "eid")).entries[0]
    assert get_field_conflicts(entry)[0].is_merged


def test_field_conflict_is_a_dataclass_record():
    conflict = FieldConflict(
        key="pages",
        occurrences=[],
        candidates=["1--10"],
        merged=False,
        canonical_value=None,
    )
    assert conflict.is_open
    assert conflict.is_conflicting
    assert conflict.chosen is None
