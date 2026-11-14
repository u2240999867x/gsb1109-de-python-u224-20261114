import pytest

from bibtexparser.middlewares.stringdefs import STRING_SOURCE_KEY
from bibtexparser.middlewares.stringdefs import StringDefinitions
from bibtexparser.middlewares.stringdefs import _Resolver
from bibtexparser.middlewares.stringdefs import _split_concatenation
from bibtexparser.model import Entry
from bibtexparser.model import Field
from bibtexparser.model import String
from bibtexparser.library import Library


def _definitions(*declarations, sources=None):
    definitions = StringDefinitions()
    for index, (key, value) in enumerate(declarations):
        source = None if sources is None else sources[index]
        definitions.add(key=key, value=value, source=source)
    return definitions


class TestStringDefinitions:
    def test_lookup_is_case_insensitive(self):
        definitions = _definitions(("J", '"Journal"'))
        assert definitions.declaration("j").value == '"Journal"'
        assert definitions.declaration("J").value == '"Journal"'

    def test_redeclaration_within_a_source_overrides_in_order(self):
        definitions = _definitions(("j", '"First"'), ("j", '"Second"'))
        assert definitions.declaration("j").value == '"Second"'

    def test_own_source_takes_precedence(self):
        definitions = _definitions(
            ("j", '"Shared"'),
            ("j", '"Local"'),
            sources=["shared", "local"],
        )
        # A lookup from `local` sees the local override, never the shared one.
        assert definitions.declaration("j", source="local").value == '"Local"'
        # A lookup from `shared` still sees its own declaration untouched.
        assert definitions.declaration("j", source="shared").value == '"Shared"'

    def test_other_sources_act_as_shared_vocabulary_most_recent_first(self):
        definitions = _definitions(
            ("pub", '"Old Press"'),
            ("pub", '"New Press"'),
            sources=["old", "new"],
        )
        # A third source that does not define `pub` gets the newest declaration.
        assert definitions.declaration("pub", source="third").value == '"New Press"'

    def test_unknown_key_returns_none(self):
        definitions = _definitions(("a", '"A"'))
        assert definitions.declaration("missing") is None

    def test_from_library_collects_only_active_strings_and_their_sources(self):
        own = String("own", '"Own"')
        own.set_parser_metadata(STRING_SOURCE_KEY, "A")
        foreign = String("foreign", '"Foreign"')
        foreign.set_parser_metadata(STRING_SOURCE_KEY, "B")
        entry = Entry("article", "e", [Field("title", "T")])
        library = Library([own, foreign, entry])

        definitions = StringDefinitions.from_library(library)
        assert definitions.declaration("own", source="A").value == '"Own"'
        assert definitions.declaration("foreign", source="B").value == '"Foreign"'
        assert definitions.sources == ["A", "B"]


class TestSplitConcatenation:
    def test_splits_bare_references_and_literals(self):
        assert _split_concatenation('a # "text" # {braced} # 2023') == [
            ("ref", "a", None),
            ("lit", "text", '"'),
            ("lit", "braced", "{"),
            ("num", "2023", None),
        ]

    def test_keeps_hash_inside_braces_and_quotes(self):
        assert _split_concatenation(r'{a # b} # "c # d"') == [
            ("lit", "a # b", "{"),
            ("lit", "c # d", '"'),
        ]

    def test_single_reference(self):
        assert _split_concatenation("jan") == [("ref", "jan", None)]


class TestResolver:
    def test_resolves_plain_reference_keeping_enclosing(self):
        definitions = _definitions(("note", '"A note."'))
        result = _Resolver(definitions).resolve_value("note")
        assert result.complete
        assert result.unresolved == ()
        assert result.render() == '"A note."'

    def test_resolves_braced_reference(self):
        definitions = _definitions(("note", "{A note.}"))
        assert _Resolver(definitions).resolve_value("note").render() == "{A note.}"

    def test_resolves_concatenation_token_by_token(self):
        definitions = _definitions(("a", '"AA"'))
        result = _Resolver(definitions).resolve_value('"pre" # a # {post}')
        assert result.complete
        assert result.render() == '"pre" # "AA" # {post}'

    def test_nested_macros_are_expanded(self):
        definitions = _definitions(("a", '"AA"'), ("j", 'a # " Journal"'))
        result = _Resolver(definitions).resolve_value("j")
        assert result.complete
        assert result.render() == '"AA" # " Journal"'

    def test_undefined_macro_keeps_resolved_fragments_and_open_chain(self):
        definitions = _definitions(("a", '"AA"'))
        result = _Resolver(definitions).resolve_value('"pre" # a # missing')
        assert not result.complete
        assert result.unresolved == ("missing",)
        assert result.render() == '"pre" # "AA" # missing'

    def test_bare_number_is_a_literal_not_a_reference(self):
        definitions = _definitions(("a", '"AA"'))
        result = _Resolver(definitions).resolve_value("2023 # a")
        assert result.complete
        assert result.render() == '2023 # "AA"'

    def test_mutual_recursion_terminates_and_keeps_the_open_link(self):
        definitions = _definitions(("p", 'q # "P"'), ("q", 'p # "Q"'))
        resolver = _Resolver(definitions)
        result_p = resolver.resolve_value("p")
        result_q = resolver.resolve_value("q")
        assert not result_p.complete
        assert not result_q.complete
        assert result_p.unresolved == ("p",)
        assert result_q.unresolved == ("q",)
        # The resolved fragments are preserved, the cyclic macro stays bare.
        assert result_p.render() == 'p # "Q" # "P"'
        assert result_q.render() == 'q # "P" # "Q"'

    def test_self_recursion_terminates(self):
        definitions = _definitions(("s", 's # "!"'))
        result = _Resolver(definitions).resolve_value("s")
        assert not result.complete
        assert result.unresolved == ("s",)
        assert result.render() == 's # "!"'

    def test_unrelated_macros_still_resolve_next_to_a_cyclic_one(self):
        definitions = _definitions(
            ("p", 'q # "P"'),
            ("q", 'p # "Q"'),
            ("good", '"Fine"'),
        )
        resolver = _Resolver(definitions)
        assert resolver.resolve_value("good").render() == '"Fine"'
        assert not resolver.resolve_value("p").complete

    def test_source_override_does_not_leak_between_sources(self):
        definitions = _definitions(
            ("j", '"Journal of A"'),
            ("j", '"Journal of B"'),
            sources=["A", "B"],
        )
        resolver = _Resolver(definitions)
        assert resolver.resolve_value("j", source="A").render() == '"Journal of A"'
        assert resolver.resolve_value("j", source="B").render() == '"Journal of B"'

    def test_shared_vocabulary_is_referenced_when_source_has_no_own_definition(self):
        definitions = _definitions(
            ("pub", '"Shared Press"'),
            ("j", '"Journal of B"'),
            sources=["A", "B"],
        )
        resolver = _Resolver(definitions)
        assert resolver.resolve_value("pub", source="B").render() == '"Shared Press"'

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ('"a" # b', '"a" # "BB"'),
            ("{a} # b", '{a} # "BB"'),
            ('a # {b} # c', '"AA" # {b} # "CC"'),
            ('a # "b"', '"AA" # "b"'),
        ],
    )
    def test_render_keeps_literal_enclosing_and_resolves_references(self, value, expected):
        definitions = _definitions(("a", '"AA"'), ("b", '"BB"'), ("c", '"CC"'))
        assert _Resolver(definitions).resolve_value(value).render() == expected
