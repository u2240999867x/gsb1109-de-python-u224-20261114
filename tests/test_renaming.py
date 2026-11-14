from typing import Any

import pytest

import bibtexparser
from bibtexparser.exceptions import RenamingFailedError
from bibtexparser.model import Entry
from bibtexparser.model import Field


def _entry(key: str, fields: list[Field] | None = None, entry_type: str = "article") -> Entry:
    return Entry(entry_type=entry_type, key=key, fields=fields or [])


def _field(key: str, value: Any, enclosing: str | None = None) -> Field:
    field = Field(key=key, value=value)
    if enclosing is not None:
        field.enclosing = enclosing
    return field


def _parse(bib: str) -> bibtexparser.Library:
    return bibtexparser.parse_string(bib)


def test_swap_two_keys():
    """A->B and B->A in one call must exchange the keys, not collapse them."""
    entry_a = _entry("A", [_field("title", "title A")])
    entry_b = _entry("B", [_field("title", "title B")])
    library = bibtexparser.Library([entry_a, entry_b])

    library.rename_entries({"A": "B", "B": "A"})

    assert list(library.entries_dict.keys()) == ["B", "A"]
    assert [entry.key for entry in library.entries] == ["B", "A"]
    assert library.entries_dict["B"].fields_dict["title"].value == "title A"
    assert library.entries_dict["A"].fields_dict["title"].value == "title B"


def test_swap_updates_crossref_both_directions():
    """Each crossref must keep pointing at the same work after a swap."""
    child = _entry("child", [_field("crossref", "A", enclosing="no-enclosing")])
    entry_a = _entry("A", [_field("crossref", "B", enclosing="no-enclosing")])
    entry_b = _entry("B")
    library = bibtexparser.Library([child, entry_a, entry_b])

    library.rename_entries({"A": "B", "B": "A"})

    by_key = library.entries_dict
    # child pointed at the work formerly known as A, now B
    assert by_key["child"].fields_dict["crossref"].value == "B"
    # A (now B) pointed at the work formerly known as B, now A
    assert by_key["B"].fields_dict["crossref"].value == "A"


def test_enclosed_crossref_is_updated():
    """crossref written with braces/quotes is still a reference, not a literal."""
    library = _parse("@article{child, crossref = {A}, title = {t}}\n@book{A}")

    library.rename_entries({"A": "B"})

    assert library.entries_dict["child"].fields_dict["crossref"].value == "B"


def test_declared_reference_list_is_updated():
    """A field declared as a citation list has every key token rewritten."""
    library = _parse(
        "@article{x, related = {A, B, C}, crossref = {A}, title = {A and B}}\n"
        "@book{A}\n"
        "@book{B}"
    )

    library.rename_entries({"A": "B", "B": "A"}, extra_reference_field_keys=["related"])

    fields = library.entries_dict["x"].fields_dict
    assert fields["related"].value == "B, A, C"
    assert fields["crossref"].value == "B"
    # The title is not a declared reference field and stays untouched.
    assert fields["title"].value == "A and B"


def test_hash_concatenation_is_updated_and_separators_preserved():
    library = _parse(
        "@article{x, crossref = A # B, title = {A # B}}\n"
        "@book{A}\n"
        "@book{B}"
    )

    library.rename_entries({"A": "B", "B": "A"})

    fields = library.entries_dict["x"].fields_dict
    assert fields["crossref"].value == "B # A"
    assert fields["title"].value == "A # B"


def test_unenclosed_reference_stays_unenclosed_after_rename():
    """Rewriting an unenclosed reference must not make the writer enclose it."""
    library = _parse(
        "@article{x, crossref = A # B, related = A}\n"
        "@book{A}\n"
        "@book{B}"
    )

    library.rename_entries({"A": "B", "B": "A"}, extra_reference_field_keys=["related"])

    crossref = library.entries_dict["x"].fields_dict["crossref"]
    related = library.entries_dict["x"].fields_dict["related"]
    assert crossref.enclosing == "no-enclosing"
    assert related.enclosing == "no-enclosing"

    written = bibtexparser.write_string(library)
    assert "crossref = B # A" in written
    assert "related = B" in written
    reparsed = _parse(written)
    assert reparsed.entries_dict["x"].fields_dict["crossref"].enclosing == "no-enclosing"


def test_list_separators_and_whitespace_are_preserved_exactly():
    library = _parse(
        "@article{x, crossref = {A,B}, related = {  A ,  B  }}\n"
        "@book{A}\n"
        "@book{B}"
    )

    library.rename_entries({"A": "B", "B": "A"}, extra_reference_field_keys=["related"])

    fields = library.entries_dict["x"].fields_dict
    assert fields["crossref"].value == "B,A"
    assert fields["related"].value == "  B ,  A  "


def test_keys_with_punctuation_are_matched_as_whole_tokens():
    library = bibtexparser.Library(
        [
            _entry(
                "a.b",
                [_field("crossref", "x1 # a.b", enclosing="no-enclosing")],
            ),
            _entry("x1"),
            _entry("c:d"),
        ]
    )

    library.rename_entries({"a.b": "newab", "x1": "a.b"})

    by_key = library.entries_dict
    assert by_key["newab"].fields_dict["crossref"].value == "a.b # newab"
    # The entry not part of the renaming is untouched.
    assert "c:d" in by_key


def test_literal_fields_and_strings_are_untouched():
    """Titles, notes, comments and @string values never change on a rename."""
    bib = """
    @string{acme = {A publisher}}

    @article{A, title = {Study of A}, note = {see A}, crossref = {B}}
    @book{B, title = {B}}
    % a comment mentioning A and B
    """
    library = _parse(bib)
    string_before = library.strings_dict["acme"].value
    comments_before = [block.comment for block in library.comments]

    library.rename_entries({"A": "B", "B": "A"})

    entry_now_b = library.entries_dict["B"]
    assert entry_now_b.fields_dict["title"].value == "Study of A"
    assert entry_now_b.fields_dict["note"].value == "see A"
    assert library.strings_dict["acme"].value == string_before
    assert [block.comment for block in library.comments] == comments_before


def test_reference_to_non_renamed_key_is_preserved():
    library = _parse("@article{x, crossref = {Z}}\n@book{A}")

    library.rename_entries({"A": "B"})

    assert library.entries_dict["x"].fields_dict["crossref"].value == "Z"


def test_chain_rename_keeps_relationships_on_same_works():
    bib = """
    @article{A, crossref = {B}, related = {A, B, C}}
    @incollection{B, crossref = {C}}
    @book{C, related = {A}}
    """
    library = _parse(bib)

    library.rename_entries({"A": "B", "B": "C", "C": "A"}, extra_reference_field_keys=["related"])

    by_key = library.entries_dict
    # The work formerly A is now B and still references the work formerly B (now C).
    assert by_key["B"].fields_dict["crossref"].value == "C"
    assert by_key["B"].fields_dict["related"].value == "B, C, A"
    # The work formerly B is now C and still references the work formerly C (now A).
    assert by_key["C"].fields_dict["crossref"].value == "A"
    # The work formerly C is now A and still references the work formerly A (now B).
    assert by_key["A"].fields_dict["related"].value == "B"


def test_target_occupied_by_third_entry_fails_atomically():
    library = _parse(
        "@article{A, crossref = {C}}\n"
        "@article{C, title = {occupier}}\n"
        "@article{D, title = {d}}"
    )
    before = bibtexparser.write_string(library)

    with pytest.raises(RenamingFailedError) as exc_info:
        library.rename_entries({"A": "C"})

    error = exc_info.value
    assert "C" in error.occupiers
    assert error.occupiers["C"].key == "C"
    # Nothing was modified.
    assert [entry.key for entry in library.entries] == ["A", "C", "D"]
    assert library.entries_dict["A"].fields_dict["crossref"].value == "C"
    assert bibtexparser.write_string(library) == before


def test_missing_source_fails_atomically():
    library = bibtexparser.Library([_entry("A")])

    with pytest.raises(RenamingFailedError) as exc_info:
        library.rename_entries({"A": "X", "ghost": "Y"})

    assert exc_info.value.missing_keys == ["ghost"]
    assert library.entries_dict["A"].key == "A"


def test_duplicate_target_fails_atomically():
    library = bibtexparser.Library([_entry("A"), _entry("B")])

    with pytest.raises(RenamingFailedError) as exc_info:
        library.rename_entries({"A": "Z", "B": "Z"})

    assert exc_info.value.colliding_pairs == [("A", "B")]
    assert {entry.key for entry in library.entries} == {"A", "B"}


def test_self_rename_and_empty_mapping_are_noops():
    library = _parse("@article{A, crossref = {B}}\n@book{B}")

    library.rename_entries({"A": "A"})
    assert set(library.entries_dict) == {"A", "B"}

    library.rename_entries({})
    assert set(library.entries_dict) == {"A", "B"}


def test_field_key_matching_is_case_insensitive():
    library = _parse(
        "@article{x, Crossref = {A}, RELATED = {A}}\n@book{A}"
    )

    library.rename_entries({"A": "B"}, extra_reference_field_keys=["related"])

    fields = library.entries_dict["x"].fields_dict
    assert fields["Crossref"].value == "B"
    assert fields["RELATED"].value == "B"


def test_roundtrip_preserves_original_relationships():
    """After write + re-parse every relationship lands on the original work."""
    bib = """
    @inproceedings{A, title = {Work A}, crossref = {B}, related = {A, B}}
    @proceedings{B, title = {Work B}}
    @article{C, title = {Work C}, crossref = A # B}
    """
    library = _parse(bib)

    library.rename_entries({"A": "B", "B": "A"}, extra_reference_field_keys=["related"])

    reparsed = _parse(bibtexparser.write_string(library))
    by_key = reparsed.entries_dict

    # Work A (now key B) still cross-references work B (now key A).
    assert by_key["B"].fields_dict["title"].value == "Work A"
    assert by_key["B"].fields_dict["crossref"].value == "A"
    assert by_key["B"].fields_dict["related"].value == "B, A"
    # Work C still references both original works, under their new keys.
    assert by_key["C"].fields_dict["crossref"].value == "B # A"


def test_non_string_field_values_are_left_alone():
    entry = _entry("A", [_field("year", 2001)])
    library = bibtexparser.Library([entry])

    library.rename_entries({"A": "B"})

    assert library.entries_dict["B"].fields_dict["year"].value == 2001


def test_rename_keeps_index_consistent_for_further_operations():
    library = bibtexparser.Library([_entry("A"), _entry("B")])

    library.rename_entries({"A": "B", "B": "A"})

    # The rebuilt index must support a second, independent rename.
    library.rename_entries({"B": "C"})
    assert set(library.entries_dict) == {"C", "A"}
    assert library.entries_dict["C"].key == "C"
