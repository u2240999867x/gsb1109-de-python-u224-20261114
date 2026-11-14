import pytest

from bibtexparser.middlewares.enclosing import RemoveEnclosingMiddleware
from bibtexparser.middlewares.interpolate import UNRESOLVED_REFERENCES_KEY
from bibtexparser.middlewares.interpolate import ResolveStringReferencesMiddleware
from bibtexparser.middlewares.stringdefs import STRING_SOURCE_KEY
from bibtexparser.splitter import Splitter

bibtex_string = """
@string{test_note = "This is a test note."}

@article{test_article,
  author  = "Smith, John",
  title   = "A Test Article",
  journal = "Journal of Testing",
  year    = "2022",
  note    = test_note
}

"""


def test_string_interpolation_middleware_interpolates_string():
    original_lib = Splitter(bibtex_string).split()

    # Prerequisite
    assert original_lib.entries_dict["test_article"].fields_dict["note"].value == "test_note"

    # Apply middleware
    m = ResolveStringReferencesMiddleware(allow_inplace_modification=False)
    changed_library = m.transform(original_lib)

    assert original_lib is not changed_library
    assert (
        changed_library.entries_dict["test_article"].fields_dict["note"].value
        == '"This is a test note."'
    )


def test_string_interpolation_is_case_insensitive():
    bibtex = """
    @string{lowercase = "Lower Case Note."}
    @string{UPPERCASE = "Upper Case Note."}

    @article{test_article,
      note    = LOWERCASE,
      comment = uppercase
    }
    """
    library = Splitter(bibtex).split()

    m = ResolveStringReferencesMiddleware()
    library = m.transform(library)

    fields = library.entries_dict["test_article"].fields_dict
    assert fields["note"].value == '"Lower Case Note."'
    assert fields["comment"].value == '"Upper Case Note."'


def test_warning_is_raised_if_enclosings_are_removed():
    original_lib = Splitter(bibtex_string).split()
    m = RemoveEnclosingMiddleware(allow_inplace_modification=False)
    no_enclosing_library = m.transform(original_lib)

    with pytest.warns(UserWarning) as record:
        m = ResolveStringReferencesMiddleware(allow_inplace_modification=False)
        m.transform(no_enclosing_library)

    assert len(record) == 1
    assert "RemoveEnclosing" in record[0].message.args[0]


def _resolve(bibtex):
    library = Splitter(bibtex).split()
    return ResolveStringReferencesMiddleware().transform(library)


def test_concatenation_is_resolved_token_by_token():
    bibtex = """
    @string{a = "AA"}
    @string{j = a # " Journal"}
    @article{e,
      journal = j,
      note = "pre" # a # {post}
    }
    """
    library = _resolve(bibtex)
    fields = library.entries_dict["e"].fields_dict
    assert fields["journal"].value == '"AA" # " Journal"'
    assert fields["note"].value == '"pre" # "AA" # {post}'


def test_undefined_reference_keeps_resolved_fragments_and_open_chain():
    bibtex = """
    @string{a = "AA"}
    @article{e, note = "pre" # a # missing}
    """
    library = _resolve(bibtex)
    entry = library.entries_dict["e"]
    assert entry.fields_dict["note"].value == '"pre" # "AA" # missing'
    assert entry.parser_metadata[UNRESOLVED_REFERENCES_KEY] == {"note": ["missing"]}


def test_bare_number_is_not_treated_as_reference():
    bibtex = """
    @string{a = "AA"}
    @article{e, year = 2023, vol = 7 # a}
    """
    library = _resolve(bibtex)
    entry = library.entries_dict["e"]
    assert entry.fields_dict["year"].value == "2023"
    assert entry.fields_dict["vol"].value == '7 # "AA"'
    assert UNRESOLVED_REFERENCES_KEY not in entry.parser_metadata


def test_mutual_recursion_terminates_without_failing_other_entries():
    bibtex = """
    @string{p = q # "P"}
    @string{q = p # "Q"}
    @string{good = "Fine"}
    @article{healthy, title = good}
    @article{cyclic, journal = p}
    @article{partial, note = "x" # p # "z" # missing}
    """
    library = _resolve(bibtex)
    assert library.failed_blocks == []
    # The enclosing middleware has not run yet, so resolved literals keep theirs.
    assert library.entries_dict["healthy"].fields_dict["title"].value == '"Fine"'
    cyclic = library.entries_dict["cyclic"]
    assert cyclic.fields_dict["journal"].value == 'p # "Q" # "P"'
    assert cyclic.parser_metadata[UNRESOLVED_REFERENCES_KEY] == {"journal": ["p"]}
    partial = library.entries_dict["partial"]
    assert partial.fields_dict["note"].value == '"x" # p # "Q" # "P" # "z" # missing'
    assert partial.parser_metadata[UNRESOLVED_REFERENCES_KEY] == {"note": ["p", "missing"]}


def test_string_definition_blocks_are_kept_verbatim():
    bibtex = """
    @string{a = "AA"}
    @string{j = a # " Journal"}
    @string{p = q # "P"}
    @string{q = p # "Q"}
    """
    library = _resolve(bibtex)
    values = {s.key: s.value for s in library.strings}
    assert values == {
        "a": '"AA"',
        "j": 'a # " Journal"',
        "p": 'q # "P"',
        "q": 'p # "Q"',
    }


def test_source_scoped_overrides_and_shared_vocabulary():
    bibtex = """
    @string{j = "Journal of A"}
    @string{pub = "Shared Press"}
    @article{a1, journal = j, publisher = pub, note = "a" # j # ghost}
    """
    library = Splitter(bibtex).split()
    for block in library.blocks:
        if hasattr(block, "parser_metadata"):
            block.set_parser_metadata(STRING_SOURCE_KEY, "A")
    library = ResolveStringReferencesMiddleware().transform(library)

    entry = library.entries_dict["a1"]
    assert entry.fields_dict["journal"].value == '"Journal of A"'
    assert entry.fields_dict["publisher"].value == '"Shared Press"'
    assert entry.fields_dict["note"].value == '"a" # "Journal of A" # ghost'
    assert entry.parser_metadata[UNRESOLVED_REFERENCES_KEY] == {"note": ["ghost"]}
