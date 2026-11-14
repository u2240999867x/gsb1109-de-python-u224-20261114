"""Tests for collecting title display and search forms."""

import pytest

import bibtexparser
from bibtexparser.middlewares import TitleFormsMiddleware
from bibtexparser.middlewares import get_title_forms
from bibtexparser.titles import collect_title_forms
from bibtexparser.titles import normalize_search_key
from bibtexparser.titles import TitleForms
from bibtexparser.titles import UnrecognizedCommand


@pytest.mark.parametrize(
    "value,expected_display",
    [
        pytest.param(r"The {H2O} Molecule", "The H2O Molecule", id="chemical-formula"),
        pytest.param(r"{H2O} and {NASA}", "H2O and NASA", id="several-protected-groups"),
        pytest.param(
            r"The {H2O}-{NASA} Mission", "The H2O-NASA Mission", id="protected-compound"
        ),
        pytest.param(r"An {AMAZING} title", "An AMAZING Title", id="protected-uppercase"),
        pytest.param(
            r"{Walther Andreas} Muller", "Walther Andreas Muller", id="protected-person-name"
        ),
        pytest.param(r"{a {nested} brace}", "A nested Brace", id="nested-groups"),
        pytest.param(
            r"state-of-the-art {H2O} sensors",
            "State-of-the-Art H2O Sensors",
            id="hyphen",
        ),
        pytest.param(r"A Title of the Art", "A Title of the Art", id="small-words"),
        pytest.param(
            r"DNA and McDonald and iPhone", "DNA and McDonald and iPhone", id="acronyms"
        ),
        pytest.param(
            r"The {H2O} Effect: A {NASA} Study", "The H2O Effect: A NASA Study", id="after-colon"
        ),
        pytest.param(r"der Einfluss von {H2O}", "Der Einfluss von H2O", id="non-english"),
        pytest.param(r"关于 {H2O} 的研究", "关于 H2O 的研究", id="non-latin"),
    ],
)
def test_display_preserves_case_protected_content(value, expected_display):
    """Display decoding removes LaTeX braces, but keeps protected case intact."""
    forms = collect_title_forms(value)

    assert forms.display == expected_display
    assert forms.raw == value


def test_title_casing_can_be_disabled():
    forms = collect_title_forms(r"the {H2O} molecule", title_case=False)

    assert forms.display == "the H2O molecule"


@pytest.mark.parametrize(
    "protected_value",
    [
        r"{H2O}",
        r"{NASA}",
        r"{DNA}",
        r"{pH}",
        r"{mRNA}",
        r"{LaTeX}",
    ],
)
def test_protected_case_is_never_changed_by_title_casing(protected_value):
    """Casing the title must not touch content of brace-protected groups."""
    forms = collect_title_forms(f"the {protected_value} story", title_case=True)

    assert protected_value[1:-1] in forms.display


def test_protected_spans_point_at_protected_display_text():
    """Protected ranges allow consumers to re-apply protection downstream."""
    forms = collect_title_forms(r"the {H2O} and {NASA} story")

    protected = [forms.display[start:end] for start, end in forms.protected_spans]
    assert protected == ["H2O", "NASA"]


def test_protected_display_wraps_protected_ranges():
    """`protected_display` re-adds braces around protected content."""
    forms = collect_title_forms(r"the {H2O} and {NASA} story", title_case=True)

    assert forms.protected_display == "The {H2O} and {NASA} Story"


def test_non_latin_text_is_not_case_changed():
    """Title casing only applies Latin-style casing, leaving CJK text intact."""
    forms = collect_title_forms(r"关于 {H2O} 的研究")

    assert forms.display == "关于 H2O 的研究"


def test_display_keeps_case_when_title_casing_is_disabled():
    forms = collect_title_forms(r"the {H2O} molecule", title_case=False)

    assert forms.display == "the H2O molecule"


@pytest.mark.parametrize(
    "accented,unicode",
    [
        pytest.param(r"M{\"u}ller", "Müller", id="umlaut"),
        pytest.param(r"M\"uller", "Müller", id="umlaut-without-group"),
        pytest.param(r"M\"{u}ller", "Müller", id="umlaut-braced-letter"),
        pytest.param(r"caf\'e", "café", id="acute"),
        pytest.param(r"p\^{a}t\'{e}", "pâté", id="circumflex-and-acute"),
        pytest.param(r"{\v{S}}koda", "Škoda", id="caron"),
        pytest.param(r"C\H{o}", "Cő", id="double-acute"),
        pytest.param(r"Kristoffer H\o{}gsbro Rose", "Kristoffer Høgsbro Rose", id="stroke"),
        pytest.param(r"\AA ngstr\"om", "Ångström", id="ring-and-umlaut"),
    ],
)
def test_accent_commands_and_unicode_spellings_share_a_search_key(accented, unicode):
    """LaTeX accent commands and their Unicode spelling are interchangeable."""
    accented_forms = collect_title_forms(accented)
    unicode_forms = collect_title_forms(unicode)

    assert accented_forms.display == unicode_forms.display
    assert accented_forms.search_key == unicode_forms.search_key
    assert normalize_search_key(accented) == normalize_search_key(unicode)


def test_search_key_is_case_and_accent_folded():
    forms = collect_title_forms(r"The {H2O} Molecule of M{\"u}ller")

    assert forms.search_key == "the h2o molecule of muller"
    assert forms.search_key == forms.search_key.casefold()


@pytest.mark.parametrize(
    "value,expected_key",
    [
        pytest.param(r"$\alpha$-helix", "alpha helix", id="greek-letter"),
        pytest.param(r"Stra\ss e", "strasse", id="eszett"),
        pytest.param(r"{\ae}on", "aeon", id="ae-ligature"),
        pytest.param(r"100\% \& more", "100 more", id="escaped-specials"),
        pytest.param(r"a~b", "a b", id="non-breaking-space"),
    ],
)
def test_search_key_normalization(value, expected_key):
    assert normalize_search_key(value) == expected_key


def test_empty_title_has_empty_forms():
    forms = collect_title_forms("")

    assert forms == TitleForms(raw="", display="", search_key="")


def test_outer_enclosing_is_not_treated_as_case_protection():
    """A pair enclosing the whole field value is value enclosing, not protection."""
    braced = collect_title_forms("{The {H2O} Molecule}")
    quoted = collect_title_forms('"The {H2O} Molecule"')

    assert braced.display == "The H2O Molecule"
    assert braced.protected_spans == ((4, 7),)
    assert quoted.display == braced.display
    assert quoted.protected_spans == braced.protected_spans


def test_unknown_command_is_reported_with_its_range():
    value = r"The \unknown{foo} and {H2O}"
    forms = collect_title_forms(value)

    assert len(forms.unrecognized) == 1
    command = forms.unrecognized[0]
    assert isinstance(command, UnrecognizedCommand)
    assert command.name == "unknown"
    assert value[command.start : command.end] == r"\unknown"
    assert value[command.span[0] : command.span[1]] == r"\unknown"
    assert value[command.argument_span[0] : command.argument_span[1]] == "{foo}"
    start, end = command.full_span
    assert value[start:end] == r"\unknown{foo}"


def test_unknown_command_does_not_break_later_accents():
    """A known accent after an unknown command must still reach the search key."""
    value = r"\unknown{foo} and caf\'e"

    forms = collect_title_forms(value)

    assert forms.unrecognized[0].name == "unknown"
    assert "Café" in forms.display
    assert forms.search_key == "foo and cafe"


def test_unknown_commands_are_reported_in_source_order():
    value = r"\quux{a} and \foobar{b} caf\'e"

    forms = collect_title_forms(value)

    assert [command.name for command in forms.unrecognized] == ["quux", "foobar"]
    assert [command.start for command in forms.unrecognized] == sorted(
        command.start for command in forms.unrecognized
    )
    assert forms.search_key == "a and b cafe"


def test_unknown_command_arguments_are_still_displayed():
    forms = collect_title_forms(r"\ce{H2O} and caf\'e")

    assert forms.display == "H2O and Café"
    assert forms.has_unrecognized


def test_unknown_command_ranges_refer_to_the_raw_field_value():
    value = r'  {\unknown{foo}}  '
    forms = collect_title_forms(value)

    command = forms.unrecognized[0]
    assert value[command.start : command.end] == r"\unknown"


def test_known_formatting_commands_are_not_reported_as_unrecognized():
    value = r"\textbf{NASA} and \textit{the {H2O} story}"

    forms = collect_title_forms(value)

    assert forms.unrecognized == ()
    # Macro arguments are brace groups in BibTeX and protect their case.
    assert forms.display == "NASA and the H2O story"


def test_type_error_for_non_string_values():
    with pytest.raises(TypeError):
        collect_title_forms(None)


def test_middleware_attaches_forms_without_changing_the_title():
    bibtex = r"""@article{k,
        title = {The {H2O} Molecule of M{\"u}ller},
        author = {Some Author},
    }"""
    library = bibtexparser.parse_string(
        bibtex, append_middleware=[TitleFormsMiddleware()]
    )

    entry = library.entries[0]
    forms = get_title_forms(entry)

    assert entry["title"] == r"The {H2O} Molecule of M{\"u}ller"
    assert forms.raw == entry["title"]
    assert forms.display == "The H2O Molecule of Müller"
    assert forms.search_key == "the h2o molecule of muller"


def test_middleware_keeps_bibtex_roundtrip_intact():
    bibtex = r"""@article{k,
        title = {The {H2O} Molecule of M{\"u}ller},
    }"""
    library = bibtexparser.parse_string(
        bibtex, append_middleware=[TitleFormsMiddleware()]
    )

    written = bibtexparser.write_string(library)

    assert r"title = {The {H2O} Molecule of M{\"u}ller}" in written


def test_middleware_protects_explicit_group_after_enclosing_removal():
    """`title = {{H2O} Molecule}` protects `H2O`, the outer braces only enclose."""
    library = bibtexparser.parse_string(
        r"@article{k, title = {{H2O} Molecule}}",
        append_middleware=[TitleFormsMiddleware()],
    )

    forms = get_title_forms(library.entries[0])

    assert library.entries[0]["title"] == "{H2O} Molecule"
    assert forms.display == "H2O Molecule"
    assert forms.protected_spans == ((0, 3),)
    assert forms.enclosing == "{"
    assert forms.bibtex == "{{H2O} Molecule}"


def test_bibtex_property_restores_missing_enclosing():
    forms = collect_title_forms("The {H2O} Molecule")

    assert forms.bibtex == "{The {H2O} Molecule}"


def test_middleware_considers_configured_fields():
    bibtex = r"""@inproceedings{k,
        title = {A {Paper}},
        booktitle = {Proc. of the {H2O} Conf.},
    }"""
    library = bibtexparser.parse_string(
        bibtex,
        append_middleware=[TitleFormsMiddleware(fields=("title", "booktitle"))],
    )

    entry = library.entries[0]
    assert get_title_forms(entry, "title").display == "A Paper"
    assert get_title_forms(entry, "booktitle").display == "Proc. of the H2O Conf."


def test_middleware_ignores_missing_and_non_string_fields():
    bibtex = r"""@article{no_title, year = {2024}}"""
    library = bibtexparser.parse_string(
        bibtex, append_middleware=[TitleFormsMiddleware()]
    )

    assert get_title_forms(library.entries[0]) is None


def test_get_title_forms_without_middleware_returns_none():
    library = bibtexparser.parse_string("@article{k, title = {A title}}")

    assert get_title_forms(library.entries[0]) is None


def test_middleware_respects_inplace_flag():
    bibtex = r"@article{k, title = {The {H2O} Molecule}}"
    library = bibtexparser.parse_string(bibtex, parse_stack=[])

    transformed = TitleFormsMiddleware(allow_inplace_modification=False).transform(library)

    assert transformed.entries[0] is not library.entries[0]
    assert get_title_forms(transformed.entries[0]).display == "The H2O Molecule"
    assert get_title_forms(library.entries[0]) is None


def test_build_title_index_groups_accent_variants():
    """Entries whose titles differ only in accent notation get the same key."""
    bibtex = r"""
    @article{a, title = {The {H2O} Molecule of M{\"u}ller}}
    @article{b, title = {The {H2O} molecule of Muller}}
    @article{c, title = {Caf\'e Society}}
    """
    library = bibtexparser.parse_string(bibtex)

    index = bibtexparser.build_title_index(library.entries)

    assert [entry.key for entry in index["the h2o molecule of muller"]] == ["a", "b"]
    assert [entry.key for entry in index["cafe society"]] == ["c"]


def test_build_title_index_uses_collected_forms():
    bibtex = r"@article{a, title = {The {H2O} Molecule of M{\"u}ller}}"
    library = bibtexparser.parse_string(
        bibtex, append_middleware=[TitleFormsMiddleware()]
    )

    index = bibtexparser.build_title_index(library.entries)

    assert list(index) == ["the h2o molecule of muller"]


def test_display_and_search_helpers():
    assert bibtexparser.display_title(r"\ce{H2O} and caf\'e") == "H2O and Café"
    assert bibtexparser.title_search_key(r"caf\'e") == "cafe"
