from copy import deepcopy
from textwrap import dedent

import pytest

from bibtexparser import Library
from bibtexparser import parse_string
from bibtexparser import write_string
from bibtexparser.middlewares.crossref import CrossrefSource
from bibtexparser.middlewares.crossref import ResolveCrossrefMiddleware
from bibtexparser.middlewares.crossref import resolve_crossref
from bibtexparser.model import Entry
from bibtexparser.model import Field

PROCEEDINGS = dedent("""\
    @proceedings{proc2020,
      title = {Proceedings of the 2020 Test Conference},
      publisher = {Test Press},
      year = {2020},
    }""")

ARTICLE_WITH_CROSSREF = dedent("""\
    @inproceedings{paper2020,
      author = {A. Author},
      crossref = {proc2020},
      year = {2020},
    }""")

ARTICLE_WITH_OWN_BOOKTITLE = dedent("""\
    @inproceedings{paper2020,
      author = {A. Author},
      title = {A Paper},
      booktitle = {Own Workshop},
      crossref = {proc2020},
      year = {2020},
    }""")


def _resolved(bibtex: str) -> Library:
    """Parse with the default stack, then resolve crossrefs."""
    library = parse_string(bibtex)
    return ResolveCrossrefMiddleware().transform(library)


def _fields(entry: Entry) -> dict:
    return {field.key: field.value for field in entry.fields}


def _provenance(entry: Entry) -> dict:
    return {
        key: (source.entry_key, source.field_key, source.path, source.level)
        for key, source in ResolveCrossrefMiddleware.get_provenance(entry).items()
    }


def test_child_inherits_parent_title_as_booktitle_and_other_fields():
    library = _resolved(PROCEEDINGS + "\n" + ARTICLE_WITH_CROSSREF)
    child = library.entries_dict["paper2020"]

    fields = _fields(child)
    assert fields["booktitle"] == "Proceedings of the 2020 Test Conference"
    assert fields["publisher"] == "Test Press"
    # The child's own year wins over the parent's year.
    assert fields["year"] == "2020"
    # The crossref field itself is not inherited into the parent.
    assert "crossref" not in _fields(library.entries_dict["proc2020"])


def test_own_field_is_kept_instead_of_inherited():
    library = _resolved(PROCEEDINGS + "\n" + ARTICLE_WITH_OWN_BOOKTITLE)
    child = library.entries_dict["paper2020"]

    assert _fields(child)["booktitle"] == "Own Workshop"
    assert "booktitle" not in ResolveCrossrefMiddleware.get_provenance(child)
    # Fields the child does not declare are still inherited.
    assert _fields(child)["publisher"] == "Test Press"


@pytest.mark.parametrize(
    "own_booktitle",
    ["booktitle = {},", 'booktitle = "",'],
    ids=["empty_braces", "empty_quotes"],
)
def test_explicitly_empty_field_counts_as_declaration(own_booktitle):
    bibtex = dedent(f"""\
        @inproceedings{{paper2020,
          author = {{A. Author}},
          {own_booktitle}
          crossref = {{proc2020}},
          year = {{2020}},
        }}
        """)

    library = _resolved(PROCEEDINGS + "\n" + bibtex)
    child = library.entries_dict["paper2020"]

    assert "booktitle" in _fields(child)
    assert _fields(child)["booktitle"] == ""
    assert _provenance(child) == {"publisher": ("proc2020", "publisher", ("proc2020",), 1)}


def test_nearest_ancestor_wins_and_provenance_records_the_level():
    bibtex = dedent("""\
        @inproceedings{paper,
          crossref = {child},
          year = {2024},
        }
        @proceedings{child,
          crossref = {parent},
          publisher = {Child Press},
        }
        @proceedings{parent,
          title = {Parent Conference},
          publisher = {Parent Press},
        }""")

    library = _resolved(bibtex)
    paper = library.entries_dict["paper"]

    # The nearest declaration of `publisher` is on the direct parent ...
    assert _fields(paper)["publisher"] == "Child Press"
    assert _provenance(paper)["publisher"] == ("child", "publisher", ("child",), 1)
    # ... while the only title is two levels up, so it is inherited as booktitle.
    assert _fields(paper)["booktitle"] == "Parent Conference"
    assert _provenance(paper)["booktitle"] == (
        "parent",
        "title",
        ("child", "parent"),
        2,
    )


def test_missing_parent_leaves_fields_unresolved_without_failing():
    bibtex = dedent("""\
        @inproceedings{paper,
          crossref = {doesNotExist},
          year = {2020},
        }""")

    library = _resolved(bibtex)
    paper = library.entries_dict["paper"]

    assert _fields(paper) == {"crossref": "doesNotExist", "year": "2020"}
    assert ResolveCrossrefMiddleware.get_provenance(paper) == {}


def test_cycle_only_blocks_unresolvable_fields_and_keeps_own_year():
    """In a crossref cycle, fields declared in the cycle stay usable.

    Only fields that cannot be found before the walk repeats are left
    unresolved. The middleware must not fail, recurse forever, or drop fields
    the entry declared itself (such as its own year).
    """
    bibtex = dedent("""\
        @inproceedings{a,
          crossref = {b},
          year = {1999},
        }
        @proceedings{b,
          crossref = {c},
          publisher = {Cycle Press},
        }
        @proceedings{c,
          crossref = {b},
          title = {Cycle Conference},
          year = {2000},
        }""")

    library = _resolved(bibtex)
    a = library.entries_dict["a"]
    b = library.entries_dict["b"]
    c = library.entries_dict["c"]

    # `a` reaches `b` and `c` before the walk would repeat `b`.
    assert _fields(a)["year"] == "1999"
    assert _fields(a)["publisher"] == "Cycle Press"
    assert _fields(a)["booktitle"] == "Cycle Conference"
    assert _provenance(a)["booktitle"][1] == "title"

    # `b` reaches `c` before the walk would repeat the cycle at `b`, so `c`'s
    # fields (including its year) remain usable.
    assert _fields(b)["publisher"] == "Cycle Press"
    assert _fields(b)["booktitle"] == "Cycle Conference"
    assert _fields(b)["year"] == "2000"
    # `a` is not reachable from `b`, as the walk would repeat the cycle at `b`;
    # `a`'s own year must not be used for `b`.
    assert _provenance(b)["year"][0] == "c"

    # Every entry keeps its own fields, independent of the cycle.
    assert _fields(c)["title"] == "Cycle Conference"
    assert _fields(c)["year"] == "2000"
    assert _fields(c)["publisher"] == "Cycle Press"


def test_cycle_own_value_is_not_replaced_by_a_value_from_the_cycle():
    bibtex = dedent("""\
        @proceedings{a,
          crossref = {b},
          title = {A Conference},
          year = {2001},
        }
        @proceedings{b,
          crossref = {a},
          title = {B Conference},
          year = {2002},
        }""")

    library = _resolved(bibtex)
    a = library.entries_dict["a"]
    b = library.entries_dict["b"]

    assert _fields(a)["year"] == "2001"
    assert _fields(a)["booktitle"] == "B Conference"
    assert _fields(b)["year"] == "2002"
    assert _fields(b)["booktitle"] == "A Conference"


def test_rerun_is_idempotent():
    library = _resolved(PROCEEDINGS + "\n" + ARTICLE_WITH_CROSSREF)
    first_fields = _fields(library.entries_dict["paper2020"])
    first_provenance = _provenance(library.entries_dict["paper2020"])

    ResolveCrossrefMiddleware().transform(library)
    ResolveCrossrefMiddleware().transform(library)

    assert _fields(library.entries_dict["paper2020"]) == first_fields
    assert _provenance(library.entries_dict["paper2020"]) == first_provenance
    assert [f.key for f in library.entries_dict["paper2020"].fields].count("booktitle") == 1


def test_result_is_independent_of_entry_order():
    entries = [PROCEEDINGS, ARTICLE_WITH_CROSSREF]
    forward = _resolved("\n".join(entries))
    backward = _resolved("\n".join(reversed(entries)))

    assert _fields(forward.entries_dict["paper2020"]) == _fields(
        backward.entries_dict["paper2020"]
    )
    assert _provenance(forward.entries_dict["paper2020"]) == _provenance(
        backward.entries_dict["paper2020"]
    )


def _effective_fields(library: Library) -> dict[str, dict]:
    return {entry.key: _fields(entry) for entry in library.entries}


def _effective_provenance(library: Library) -> dict[str, dict]:
    return {entry.key: _provenance(entry) for entry in library.entries}


def test_rerun_after_parent_rename_matches_fresh_rebuild():
    """Renaming the parent and its crossref target must recompute inherited values."""
    library = _resolved(PROCEEDINGS + "\n" + ARTICLE_WITH_CROSSREF)

    renamed_parent = deepcopy(library.entries_dict["proc2020"])
    renamed_parent.key = "proc2020_renamed"
    library.replace(library.entries_dict["proc2020"], renamed_parent)
    child = library.entries_dict["paper2020"]
    child.fields_dict["crossref"].value = "proc2020_renamed"

    ResolveCrossrefMiddleware().transform(library)

    fresh_bibtex = dedent("""\
        @proceedings{proc2020_renamed,
          title = {Proceedings of the 2020 Test Conference},
          publisher = {Test Press},
          year = {2020},
        }
        @inproceedings{paper2020,
          author = {A. Author},
          crossref = {proc2020_renamed},
          year = {2020},
        }""")
    fresh = _resolved(fresh_bibtex)

    assert _effective_fields(library) == _effective_fields(fresh)
    assert _effective_provenance(library) == _effective_provenance(fresh)


def test_rerun_after_parent_title_change_matches_fresh_rebuild():
    library = _resolved(PROCEEDINGS + "\n" + ARTICLE_WITH_CROSSREF)
    library.entries_dict["proc2020"].fields_dict["title"].value = "Revised Proceedings"

    ResolveCrossrefMiddleware().transform(library)

    fresh = _resolved(
        PROCEEDINGS.replace(
            "Proceedings of the 2020 Test Conference", "Revised Proceedings"
        )
        + "\n"
        + ARTICLE_WITH_CROSSREF
    )

    assert _fields(library.entries_dict["paper2020"]) == _fields(
        fresh.entries_dict["paper2020"]
    )
    assert _effective_provenance(library) == _effective_provenance(fresh)


@pytest.mark.parametrize("remove_parent_first", [True, False], ids=["parent_gone", "crossref_gone"])
def test_rerun_after_parent_deletion_matches_fresh_rebuild(remove_parent_first):
    """Deleting the parent (or the child's crossref) removes stale inherited fields."""
    library = _resolved(PROCEEDINGS + "\n" + ARTICLE_WITH_CROSSREF)

    if remove_parent_first:
        library.remove(library.entries_dict["proc2020"])
        fresh_bibtex = ARTICLE_WITH_CROSSREF
    else:
        library.entries_dict["paper2020"].pop("crossref")
        fresh_bibtex = PROCEEDINGS + "\n" + ARTICLE_WITH_CROSSREF.replace(
            "  crossref = {proc2020},\n", ""
        )

    ResolveCrossrefMiddleware().transform(library)
    fresh = _resolved(fresh_bibtex)

    assert _effective_fields(library) == _effective_fields(fresh)
    assert _effective_provenance(library) == _effective_provenance(fresh)
    assert "booktitle" not in _fields(library.entries_dict["paper2020"])


def test_removing_parent_entry_from_library_removes_inherited_fields_on_rerun():
    library = _resolved(PROCEEDINGS + "\n" + ARTICLE_WITH_CROSSREF)
    child = library.entries_dict["paper2020"]
    assert "booktitle" in _fields(child)

    library.remove(library.entries_dict["proc2020"])
    ResolveCrossrefMiddleware().transform(library)

    assert _fields(child) == {
        "author": "A. Author",
        "crossref": "proc2020",
        "year": "2020",
    }
    assert ResolveCrossrefMiddleware.get_provenance(child) == {}


def test_user_replaced_inherited_field_survives_rerun_and_acts_as_declaration():
    library = _resolved(PROCEEDINGS + "\n" + ARTICLE_WITH_CROSSREF)
    child = library.entries_dict["paper2020"]

    # Replacing the field (as done by `set_field`/`entry[...] = ...`) creates a
    # new Field instance, which marks it as the child's own declaration.
    child["booktitle"] = "Manually Chosen"
    ResolveCrossrefMiddleware().transform(library)

    assert _fields(child)["booktitle"] == "Manually Chosen"
    assert "booktitle" not in ResolveCrossrefMiddleware.get_provenance(child)
    assert _fields(child)["publisher"] == "Test Press"


def test_allow_inplace_modification_false_leaves_input_untouched():
    original = parse_string(PROCEEDINGS + "\n" + ARTICLE_WITH_CROSSREF)
    original_fields = _effective_fields(original)

    changed = ResolveCrossrefMiddleware(allow_inplace_modification=False).transform(original)

    assert changed is not original
    assert _effective_fields(original) == original_fields
    assert "booktitle" not in _fields(original.entries_dict["paper2020"])
    assert _fields(changed.entries_dict["paper2020"])["booktitle"] == (
        "Proceedings of the 2020 Test Conference"
    )


def test_enclosing_demand_of_the_declaring_field_is_preserved():
    bibtex = dedent("""\
        @proceedings{proc2020,
          title = {A Conference},
        }
        @inproceedings{paper2020,
          crossref = {proc2020},
        }""")

    library = parse_string(bibtex)
    parent = library.entries_dict["proc2020"]
    parent.fields_dict["title"].enclosing = '"'

    ResolveCrossrefMiddleware().transform(library)

    child = library.entries_dict["paper2020"]
    assert child.fields_dict["booktitle"].value == "A Conference"
    assert child.fields_dict["booktitle"].enclosing == '"'


def test_provenance_is_exposed_via_metadata_and_helpers():
    library = _resolved(PROCEEDINGS + "\n" + ARTICLE_WITH_CROSSREF)
    child = library.entries_dict["paper2020"]
    middleware = ResolveCrossrefMiddleware()

    provenance = middleware.get_provenance(child)
    assert provenance == ResolveCrossrefMiddleware.get_inherited_fields(child)
    assert child.parser_metadata["ResolveCrossref"] == provenance
    assert child.parser_metadata["crossref"] == provenance
    assert child.parser_metadata["resolve_crossref"] == provenance

    source = provenance["booktitle"]
    assert isinstance(source, CrossrefSource)
    assert source == "proc2020"
    assert source.entry_key == "proc2020"
    assert source.field_key == "title"
    assert source.field == "title"
    assert source.parent == "proc2020"
    assert source.path == ("proc2020",)
    assert source.level == 1
    assert source["source"] == "proc2020"
    assert source.as_dict()["field"] == "title"

    # Convenient provenance representations compare equal as well.
    assert source == {"source": "proc2020"}
    assert source == {"parent": "proc2020"}
    assert source == {"level": 1}


def test_get_dependents_reports_entries_including_transitive_ones():
    bibtex = dedent("""\
        @inproceedings{grandchild,
          crossref = {child},
        }
        @proceedings{child,
          crossref = {parent},
        }
        @proceedings{parent,
          title = {Parent Conference},
          publisher = {Parent Press},
        }""")

    library = _resolved(bibtex)
    middleware = ResolveCrossrefMiddleware()

    assert middleware.get_dependents(library, "parent") == ["grandchild", "child"]
    assert middleware.get_dependents(library, "child") == ["grandchild"]
    assert middleware.get_dependents(library, "grandchild") == []
    assert middleware.get_affected_entries(library, "parent") == ["grandchild", "child"]


def test_get_fields_inherited_from_reports_the_affected_records_and_fields():
    library = _resolved(PROCEEDINGS + "\n" + ARTICLE_WITH_CROSSREF)
    middleware = ResolveCrossrefMiddleware()

    assert middleware.get_fields_inherited_from(library, "proc2020") == {
        "paper2020": ["booktitle", "publisher"]
    }
    assert middleware.get_affected_fields(library, "proc2020") == {
        "paper2020": ["booktitle", "publisher"]
    }

    # The child's own fields are not reported, and unknown keys yield nothing.
    assert middleware.get_fields_inherited_from(library, "paper2020") == {}
    assert middleware.get_fields_inherited_from(library, "missing") == {}


def test_resolve_crossref_convenience_function_and_aliases():
    library = parse_string(PROCEEDINGS + "\n" + ARTICLE_WITH_CROSSREF)
    changed = resolve_crossref(library)

    assert changed is library
    assert _fields(library.entries_dict["paper2020"])["booktitle"] == (
        "Proceedings of the 2020 Test Conference"
    )

    # The module-level aliases construct the same middleware.
    from bibtexparser.middlewares import CrossrefMiddleware
    from bibtexparser.middlewares import ResolveCrossrefsMiddleware

    assert CrossrefMiddleware is ResolveCrossrefMiddleware
    assert ResolveCrossrefsMiddleware is ResolveCrossrefMiddleware


def test_metadata_is_not_written_to_bibtex():
    library = _resolved(PROCEEDINGS + "\n" + ARTICLE_WITH_CROSSREF)

    written = write_string(library)

    assert "bibtexparser_crossref_inherited_fields" not in written
    assert "ResolveCrossref" not in written
    assert "booktitle = {Proceedings of the 2020 Test Conference}" in written


def test_programmatically_created_entries_are_supported():
    library = Library(
        [
            Entry(
                "inproceedings",
                "paper",
                [Field("author", "A. Author"), Field("crossref", "proc")],
            ),
            Entry(
                "proceedings",
                "proc",
                [Field("title", "A Conference"), Field("publisher", "A Press")],
            ),
        ]
    )

    ResolveCrossrefMiddleware().transform(library)

    assert _fields(library.entries_dict["paper"])["booktitle"] == "A Conference"
    assert _fields(library.entries_dict["paper"])["publisher"] == "A Press"


def test_crossref_without_target_entry_is_ignored():
    library = _resolved("@inproceedings{paper, crossref = {missing}, year = {2020}}")

    assert _fields(library.entries_dict["paper"])["year"] == "2020"
    assert ResolveCrossrefMiddleware.get_provenance(library.entries_dict["paper"]) == {}


def test_crossref_target_is_matched_case_sensitively():
    """Citation keys are case-sensitive, hence `Proc` must not match `proc`."""
    bibtex = dedent("""\
        @proceedings{proc,
          title = {A Conference},
        }
        @inproceedings{paper,
          crossref = {Proc},
        }""")

    library = _resolved(bibtex)

    assert "booktitle" not in _fields(library.entries_dict["paper"])


def test_child_title_does_not_prevent_parent_title_from_becoming_booktitle():
    """A paper's own title is kept; the parent's title is inherited as booktitle."""
    bibtex = dedent("""\
        @proceedings{proc2020,
          title = {A Conference},
        }
        @inproceedings{paper2020,
          title = {A Paper},
          crossref = {proc2020},
        }""")

    library = _resolved(bibtex)
    paper = library.entries_dict["paper2020"]

    assert _fields(paper)["title"] == "A Paper"
    assert _fields(paper)["booktitle"] == "A Conference"
    assert _provenance(paper)["booktitle"] == ("proc2020", "title", ("proc2020",), 1)


def test_parent_booktitle_takes_precedence_over_parent_title():
    bibtex = dedent("""\
        @proceedings{proc2020,
          title = {A Conference},
          booktitle = {A Book Title},
        }
        @inproceedings{paper2020,
          crossref = {proc2020},
        }""")

    library = _resolved(bibtex)
    paper = library.entries_dict["paper2020"]

    assert _fields(paper)["booktitle"] == "A Book Title"
    assert _provenance(paper)["booktitle"] == ("proc2020", "booktitle", ("proc2020",), 1)


def test_crossref_target_is_unwrapped_without_default_stack():
    """The middleware also works when enclosings were not removed by the parse stack."""
    library = parse_string(
        PROCEEDINGS + "\n" + ARTICLE_WITH_CROSSREF,
        parse_stack=[],
    )
    ResolveCrossrefMiddleware().transform(library)

    paper = library.entries_dict["paper2020"]
    assert paper.fields_dict["booktitle"].value == "{Proceedings of the 2020 Test Conference}"


def test_self_crossref_does_not_duplicate_fields():
    library = _resolved("@proceedings{a, crossref = {a}, title = {A}, year = {2000}}")
    entry = library.entries_dict["a"]

    assert _fields(entry) == {"crossref": "a", "title": "A", "year": "2000"}
    assert ResolveCrossrefMiddleware.get_provenance(entry) == {}


def test_rerun_after_parent_field_removal_matches_fresh_rebuild():
    library = _resolved(PROCEEDINGS + "\n" + ARTICLE_WITH_CROSSREF)
    library.entries_dict["proc2020"].pop("publisher")

    ResolveCrossrefMiddleware().transform(library)

    fresh = _resolved(
        PROCEEDINGS.replace("  publisher = {Test Press},\n", "") + "\n" + ARTICLE_WITH_CROSSREF
    )
    assert _fields(library.entries_dict["paper2020"]) == _fields(
        fresh.entries_dict["paper2020"]
    )
    assert "publisher" not in _fields(library.entries_dict["paper2020"])


def test_incremental_parse_resolves_crossref_against_pre_existing_entries():
    """Parsing into an existing library must resolve crossrefs across both parts."""
    from bibtexparser import parse_string
    from bibtexparser.middlewares.crossref import ResolveCrossrefMiddleware

    library = parse_string(PROCEEDINGS)
    parse_string(
        ARTICLE_WITH_CROSSREF,
        library=library,
        append_middleware=[ResolveCrossrefMiddleware()],
    )

    assert _fields(library.entries_dict["paper2020"])["booktitle"] == (
        "Proceedings of the 2020 Test Conference"
    )
    assert _provenance(library.entries_dict["paper2020"])["booktitle"] == (
        "proc2020",
        "title",
        ("proc2020",),
        1,
    )


def test_incremental_parse_matches_parsing_at_once_for_crossref():
    from bibtexparser import parse_string
    from bibtexparser.middlewares.crossref import ResolveCrossrefMiddleware

    stepwise = parse_string(PROCEEDINGS)
    parse_string(
        ARTICLE_WITH_CROSSREF,
        library=stepwise,
        append_middleware=[ResolveCrossrefMiddleware()],
    )

    at_once = parse_string(
        PROCEEDINGS + "\n" + ARTICLE_WITH_CROSSREF,
        append_middleware=[ResolveCrossrefMiddleware()],
    )

    assert _effective_fields(stepwise) == _effective_fields(at_once)
    assert _effective_provenance(stepwise) == _effective_provenance(at_once)


def test_incremental_parse_into_existing_library_keeps_old_blocks_untouched_without_crossref():
    """Without the middleware, parsing into an existing library is unchanged."""
    from bibtexparser import parse_string

    library = parse_string(PROCEEDINGS)
    parent = library.entries_dict["proc2020"]
    parse_string(ARTICLE_WITH_CROSSREF, library=library)

    assert library.entries_dict["proc2020"] is parent
    assert "booktitle" not in _fields(library.entries_dict["paper2020"])


def test_incremental_parse_with_copying_middleware_instance_still_updates_library():
    """A copying instance in the parse stack must not silently skip the merge re-run."""
    from bibtexparser import parse_string

    library = parse_string(PROCEEDINGS)
    parse_string(
        ARTICLE_WITH_CROSSREF,
        library=library,
        append_middleware=[ResolveCrossrefMiddleware(allow_inplace_modification=False)],
    )

    assert _fields(library.entries_dict["paper2020"])["booktitle"] == (
        "Proceedings of the 2020 Test Conference"
    )


def test_child_with_crossref_to_entry_added_later_is_resolved_on_rerun():
    """Incremental workflow: child first, parent later, then re-run."""
    from bibtexparser import parse_string

    library = parse_string(ARTICLE_WITH_CROSSREF)
    assert "booktitle" not in _fields(library.entries_dict["paper2020"])

    parse_string(PROCEEDINGS, library=library)
    ResolveCrossrefMiddleware().transform(library)

    assert _fields(library.entries_dict["paper2020"])["booktitle"] == (
        "Proceedings of the 2020 Test Conference"
    )
    assert _provenance(library.entries_dict["paper2020"])["booktitle"][0] == "proc2020"


def test_name_splitting_after_crossref_resolution_applies_to_inherited_fields():
    """Crossref should run before value-changing middleware to inherit its results."""
    from bibtexparser import parse_string
    from bibtexparser.middlewares import SeparateCoAuthors

    bibtex = dedent("""\
        @proceedings{proc2020,
          editor = {A. Editor and B. Editor},
          title = {A Conference},
        }
        @inproceedings{paper2020,
          crossref = {proc2020},
        }""")

    library = parse_string(
        bibtex,
        append_middleware=[ResolveCrossrefMiddleware(), SeparateCoAuthors()],
    )

    assert library.entries_dict["paper2020"]["editor"] == ["A. Editor", "B. Editor"]
