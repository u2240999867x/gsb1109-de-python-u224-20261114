import pytest

import bibtexparser
from bibtexparser.works import build_work_index
from bibtexparser.works import canonicalize_doi
from bibtexparser.works import normalize_title


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("10.1000/XYZ.abc", "10.1000/xyz.abc"),
        ("  10.1000/xyz.abc  ", "10.1000/xyz.abc"),
        ("doi:10.1000/xyz.abc", "10.1000/xyz.abc"),
        ("DOI:10.1000/xyz.abc", "10.1000/xyz.abc"),
        ("https://doi.org/10.1000/xyz.abc", "10.1000/xyz.abc"),
        ("http://dx.doi.org/10.1000/XYZ.abc", "10.1000/xyz.abc"),
        ("doi.org/10.1000/xyz.abc", "10.1000/xyz.abc"),
        ("https://doi.org/10.1000/xyz.abc/", "10.1000/xyz.abc"),
        ("not-a-doi", None),
        ("", None),
        (None, None),
    ],
)
def test_canonicalize_doi(raw, expected):
    assert canonicalize_doi(raw) == expected


def test_normalize_title_strips_erratum_prefix_and_markup():
    assert normalize_title("Erratum to: Quantum {W}idgets") == "quantum widgets"
    assert normalize_title("Correction to Quantum Widgets") == "quantum widgets"
    assert normalize_title("Quantum Widgets") == "quantum widgets"
    assert normalize_title(None) == ""


def test_different_doi_spellings_form_one_work():
    library = bibtexparser.parse_string(
        """
        @article{a, title={Quantum Widgets}, doi={10.1000/XYZ.ABC}, year={2020}}
        @article{b, title={Quantum Widgets}, doi={https://doi.org/10.1000/xyz.abc}, year={2021}}
        """
    )
    index = build_work_index(library)

    assert len(index.works) == 1
    assert index.pending == []
    work = index.works[0]
    assert work.canonical_doi == "10.1000/xyz.abc"
    assert {entry.key for entry in work.entries} == {"a", "b"}
    # Every source record is preserved, untouched, in the library.
    assert len(library.entries) == 2


def test_explicit_erratum_relation_via_related_field():
    library = bibtexparser.parse_string(
        """
        @article{orig, title={Quantum Widgets}, doi={10.1000/xyz}}
        @article{fix, title={Erratum to: Quantum Widgets}, doi={10.1000/xyz-err},
                 relatedtype={erratum}, related={orig}}
        """
    )
    index = build_work_index(library)

    # The erratum is not merged into the corrected work, despite the close title.
    assert len(index.works) == 2
    orig_work = index.work_of(library.entries_dict["orig"])
    fix_work = index.work_of(library.entries_dict["fix"])
    assert orig_work is not fix_work
    assert [entry.key for entry in orig_work.entries] == ["orig"]

    relations = index.errata_of(orig_work)
    assert len(relations) == 1
    relation = relations[0]
    assert relation.kind == "erratum"
    assert relation.source is fix_work
    assert relation.target is orig_work
    assert relation.target_ref == "orig"

    fix_version = fix_work.versions[0]
    assert fix_version.role == "erratum"


def test_explicit_erratum_relation_via_doi_field():
    library = bibtexparser.parse_string(
        """
        @article{orig, title={Quantum Widgets}, doi={10.1000/xyz}}
        @article{fix, title={Correction to Quantum Widgets}, doi={10.1000/e2},
                 erratumof={https://doi.org/10.1000/XYZ}}
        """
    )
    index = build_work_index(library)

    orig_work = index.work_of(library.entries_dict["orig"])
    relations = index.errata_of(orig_work)
    assert len(relations) == 1
    assert relations[0].target is orig_work
    assert relations[0].source is index.work_of(library.entries_dict["fix"])


def test_erratum_entry_type_without_target_is_marked_but_unrelated():
    library = bibtexparser.parse_string(
        """
        @article{orig, title={Quantum Widgets}, doi={10.1000/xyz}}
        @erratum{fix, title={Erratum to: Quantum Widgets}, doi={10.1000/xyz-err}}
        """
    )
    index = build_work_index(library)

    fix_work = index.work_of(library.entries_dict["fix"])
    assert fix_work.versions[0].role == "erratum"
    assert index.relations == []


def test_candidate_without_doi_matches_by_title():
    library = bibtexparser.parse_string(
        """
        @article{a, title={Quantum Widgets}, doi={10.1000/xyz}}
        @misc{preprint, title={Quantum {W}idgets}, note={arxiv preprint}}
        """
    )
    index = build_work_index(library)

    assert len(index.works) == 1
    work = index.works[0]
    version = [v for v in work.versions if v.entry.key == "preprint"][0]
    assert version.link == "title-candidate"
    assert index.pending == []


def test_record_without_doi_or_title_match_forms_singleton_work():
    library = bibtexparser.parse_string(
        """
        @article{a, title={Quantum Widgets}, doi={10.1000/xyz}}
        @misc{lonely, title={Something Else Entirely}}
        """
    )
    index = build_work_index(library)

    assert len(index.works) == 2
    lonely_work = index.work_of(library.entries_dict["lonely"])
    assert lonely_work.canonical_doi is None
    assert lonely_work.versions[0].link == "singleton"


def test_ambiguous_title_candidate_goes_to_pending():
    library = bibtexparser.parse_string(
        """
        @article{a, title={Quantum Widgets}, doi={10.1000/one}}
        @article{b, title={Quantum Widgets}, doi={10.2000/two}}
        @misc{c, title={Quantum Widgets}}
        """
    )
    index = build_work_index(library)

    assert len(index.works) == 2
    assert len(index.pending) == 1
    record = index.pending[0]
    assert record.entry.key == "c"
    assert record.reason == "ambiguous-title"
    assert len(record.candidate_works) == 2


def test_bridge_record_goes_to_pending_without_merging_works():
    library = bibtexparser.parse_string(
        """
        @article{a, title={First Work}, doi={10.1000/one}}
        @article{b, title={Second Work}, doi={10.2000/two}}
        @article{bridge, title={A Confused Record}, doi={10.1000/one; 10.2000/two}}
        """
    )
    index = build_work_index(library)

    # The two originally independent works remain separate.
    assert len(index.works) == 2
    work_a = index.work_of(library.entries_dict["a"])
    work_b = index.work_of(library.entries_dict["b"])
    assert work_a is not work_b
    assert work_a.entries == [library.entries_dict["a"]]
    assert work_b.entries == [library.entries_dict["b"]]

    # The bridge record is pending and part of no work.
    assert len(index.pending) == 1
    record = index.pending[0]
    assert record.entry.key == "bridge"
    assert record.reason == "conflicting-identifiers"
    assert record.identifiers == ("10.1000/one", "10.2000/two")
    assert set(record.candidate_works) == {work_a, work_b}
    assert index.work_of(record.entry) is None

    # Every source record is preserved and accounted for.
    assert len(library.entries) == 3
    assert {id(e) for e in index.indexed_entries} == {id(e) for e in library.entries}


def test_bridge_record_seen_before_its_ends_still_does_not_merge():
    library = bibtexparser.parse_string(
        """
        @article{bridge, title={A Confused Record}, doi={10.1000/one, 10.2000/two}}
        @article{a, title={First Work}, doi={10.1000/one}}
        @article{b, title={Second Work}, doi={10.2000/two}}
        """
    )
    index = build_work_index(library)

    assert len(index.works) == 2
    assert len(index.pending) == 1
    assert index.pending[0].reason == "conflicting-identifiers"
    assert index.pending[0].candidate_works == ()


def test_duplicate_doi_spellings_on_one_record_are_not_a_bridge():
    library = bibtexparser.parse_string(
        """
        @article{a, title={Quantum Widgets},
                 doi={10.1000/xyz, https://doi.org/10.1000/XYZ}}
        """
    )
    index = build_work_index(library)

    assert len(index.works) == 1
    assert index.pending == []
    assert index.works[0].canonical_doi == "10.1000/xyz"
