import bibtexparser
from bibtexparser.middlewares import ExtractEntriesMiddleware
from bibtexparser.middlewares import MissingDependency
from bibtexparser.splitter import Splitter

bibtex_string = """
@string{jacm = "Journal of the ACM"}
@string{springer = "Springer"}
@string{intro = "Introduction to"}
@string{fulltitle = intro # " Testing"}
@string{shadowed = "Shadowed String"}
@string{unused = "Unused String"}

@book{series,
  title     = {Lecture Notes},
  publisher = springer,
}

@proceedings{conf,
  title     = {Proc. of the Conference},
  crossref  = series,
  publisher = springer,
  year      = 2024,
}

@inproceedings{inproc,
  author    = {Doe, Jane},
  title     = fulltitle,
  crossref  = conf,
  pages     = {1--10},
}

@article{article1,
  author    = {Smith, John},
  title     = {A Test Article},
  journal   = jacm,
  year      = 2022,
  note      = jacm # "-supplement",
}

@article{article2,
  author    = {Public, Joe},
  title     = {Another Article},
  journal   = {shadowed},
  year      = 2023,
}

@article{uncited,
  author    = {Ghost, Gary},
  title     = {Not Cited},
  journal   = unused,
}
"""


def _extract(library, keys):
    middleware = ExtractEntriesMiddleware(keys=keys)
    fragment = middleware.transform(library)
    return middleware, fragment


def test_extract_includes_cited_entries_and_transitive_parents():
    _, fragment = _extract(Splitter(bibtex_string).split(), ["inproc", "article1", "article2"])

    assert set(fragment.entries_dict) == {"inproc", "article1", "article2", "conf", "series"}


def test_extract_includes_only_actually_used_strings():
    _, fragment = _extract(Splitter(bibtex_string).split(), ["inproc", "article1", "article2"])

    # `shadowed` is only used as a local literal (`journal = {shadowed}`) and
    # `unused` is only used by the uncited entry: neither is taken along.
    # `intro` is included indirectly, via the `fulltitle` concatenation.
    assert set(fragment.strings_dict) == {"jacm", "springer", "intro", "fulltitle"}


def test_extract_preserves_original_block_order():
    _, fragment = _extract(Splitter(bibtex_string).split(), ["inproc"])

    assert [type(block).__name__ for block in fragment.blocks] == ["String"] * 3 + ["Entry"] * 3
    assert [block.key for block in fragment.strings] == ["springer", "intro", "fulltitle"]
    assert [entry.key for entry in fragment.entries] == ["series", "conf", "inproc"]


def test_extract_does_not_modify_original_library():
    library = Splitter(bibtex_string).split()
    _, fragment = _extract(library, ["inproc"])

    fragment_entry = fragment.entries_dict["inproc"]
    original_entry = library.entries_dict["inproc"]
    assert fragment_entry is not original_entry
    assert all(field.enclosing is None for field in original_entry.fields)
    assert set(library.entries_dict) == {
        "series", "conf", "inproc", "article1", "article2", "uncited"
    }
    assert set(library.strings_dict) == {
        "jacm", "springer", "intro", "fulltitle", "shadowed", "unused"
    }


def test_string_references_are_case_insensitive():
    library = Splitter(bibtex_string).split()
    library.entries_dict["article1"].fields_dict["journal"].value = "JACM"
    _, fragment = _extract(library, ["article1"])

    assert set(fragment.strings_dict) == {"jacm"}


def test_xdata_parents_are_included():
    library = Splitter(
        """
        @book{parent1, title = {One}}
        @book{parent2, title = {Two}}
        @book{parent3, title = {Three}}
        @article{cited, xdata = {parent1, parent2}, title = {T}}
        """
    ).split()
    _, fragment = _extract(library, ["cited"])

    assert set(fragment.entries_dict) == {"cited", "parent1", "parent2"}


def _effective_fields(library):
    return {
        key: {field.key: field.value for field in entry.fields}
        for key, entry in library.entries_dict.items()
    }


def test_fragment_roundtrips_to_same_effective_fields():
    # The effective fields of the original library (default parse stack).
    original = bibtexparser.parse_string(bibtex_string)
    keys = ["inproc", "article1", "article2"]

    _, fragment = _extract(Splitter(bibtex_string).split(), keys)
    assert not fragment.failed_blocks

    # Re-read the fragment from scratch, i.e. with an empty string table.
    reparsed = bibtexparser.parse_string(bibtexparser.write_string(fragment))
    assert not reparsed.failed_blocks

    assert set(reparsed.entries_dict) == {"inproc", "article1", "article2", "conf", "series"}
    expected = _effective_fields(original)
    actual = _effective_fields(reparsed)
    for key in actual:
        assert actual[key] == expected[key]
    # The strings themselves round-trip, too.
    original_strings = {s.key: s.value for s in original.strings}
    assert {s.key: s.value for s in reparsed.strings} == {
        key: original_strings[key] for key in fragment.strings_dict
    }


def test_fragment_roundtrips_when_extracted_after_default_parse():
    # Extracting from a library parsed with the default stack (strings already
    # interpolated, enclosing removed) still yields a re-readable fragment.
    original = bibtexparser.parse_string(bibtex_string)
    keys = ["inproc", "article1", "article2"]

    _, fragment = _extract(original, keys)
    reparsed = bibtexparser.parse_string(bibtexparser.write_string(fragment))

    expected = _effective_fields(original)
    for key, fields in _effective_fields(reparsed).items():
        assert fields == expected[key]


def test_missing_dependencies_report_path_to_gap():
    library = Splitter(
        """
        @string{combo = prefix # " combined"}

        @inproceedings{cited,
          author   = {Doe, Jane},
          crossref = ghost-conf,
          note     = undefined_note,
          title    = combo,
        }
        """
    ).split()
    middleware, fragment = _extract(library, ["cited", "nope"])

    # Defined dependencies are taken along; only the gaps are reported.
    assert set(fragment.entries_dict) == {"cited"}
    assert set(fragment.strings_dict) == {"combo"}
    assert middleware.missing_dependencies == [
        MissingDependency(kind="entry", key="ghost-conf", path=("cited",), field="crossref"),
        MissingDependency(kind="string", key="undefined_note", path=("cited",), field="note"),
        MissingDependency(kind="entry", key="nope", path=(), field=None),
        MissingDependency(
            kind="string", key="prefix", path=("cited",), field="title", via_strings=("combo",)
        ),
    ]


def test_missing_dependency_of_transitive_parent_reports_full_chain():
    library = Splitter(
        """
        @proceedings{conf, crossref = ghost-series}
        @inproceedings{cited, crossref = conf}
        """
    ).split()
    middleware, _ = _extract(library, ["cited"])

    assert middleware.missing_dependencies == [
        MissingDependency(
            kind="entry", key="ghost-series", path=("cited", "conf"), field="crossref"
        )
    ]


def test_crossref_cycles_do_not_recurse_endlessly():
    library = Splitter(
        """
        @book{a, crossref = b}
        @book{b, crossref = a}
        """
    ).split()
    middleware, fragment = _extract(library, ["a"])

    assert set(fragment.entries_dict) == {"a", "b"}
    assert middleware.missing_dependencies == []
