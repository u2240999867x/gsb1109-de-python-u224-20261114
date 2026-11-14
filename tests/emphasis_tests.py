from mammoth import documents, emphasis
from mammoth.docx.styles_xml import Styles, Style
from .testing import assert_equal


def _styles(**kwargs):
    return Styles.create(**kwargs)


def _paragraph(children, **kwargs):
    return documents.paragraph(children, **kwargs)


def _run(text, **kwargs):
    return documents.run([documents.text(text)], **kwargs)


class EffectiveCharacterPropertiesTests(object):
    def test_document_defaults_are_used_when_no_other_layer_sets_a_property(self):
        styles = _styles(default_character_properties={"bold": True})
        paragraph = _paragraph([_run("Hello")])

        properties, sources = emphasis.effective_character_properties(
            paragraph, paragraph.children[0], styles,
        )

        assert_equal(True, properties.bold)
        assert_equal(["document-default"], sources["bold"])

    def test_paragraph_style_chain_is_applied_before_character_style_chain(self):
        styles = _styles(
            paragraph_styles={
                "P": Style(style_id="P", name="P", character_properties={"bold": True}),
            },
            character_styles={
                "Base": Style(style_id="Base", name="Base", character_properties={"italic": True}),
                "Derived": Style(
                    style_id="Derived",
                    name="Derived",
                    based_on="Base",
                    character_properties={"bold": False},
                ),
            },
        )
        paragraph = _paragraph([_run("Hello", style_id="Derived")], style_id="P")

        properties, sources = emphasis.effective_character_properties(
            paragraph, paragraph.children[0], styles,
        )

        assert_equal(False, properties.bold)
        assert_equal(True, properties.italic)
        assert_equal(
            ["paragraph-style:P", "character-style:Derived"],
            sources["bold"],
        )
        assert_equal(["character-style:Base"], sources["italic"])

    def test_paragraph_mark_properties_override_paragraph_style(self):
        styles = _styles(
            paragraph_styles={
                "P": Style(style_id="P", name="P", character_properties={"bold": True}),
            },
        )
        paragraph = _paragraph(
            [_run("Hello")],
            style_id="P",
            character_properties={"bold": False},
        )

        properties, _ = emphasis.effective_character_properties(
            paragraph, paragraph.children[0], styles,
        )

        assert_equal(False, properties.bold)

    def test_direct_run_formatting_overrides_every_inherited_layer(self):
        styles = _styles(
            default_character_properties={"bold": True},
            paragraph_styles={
                "P": Style(style_id="P", name="P", character_properties={"bold": True}),
            },
            character_styles={
                "C": Style(style_id="C", name="C", character_properties={"bold": True}),
            },
        )
        paragraph = _paragraph(
            [_run("Hello", style_id="C", character_properties={"bold": False})],
            style_id="P",
        )

        properties, sources = emphasis.effective_character_properties(
            paragraph, paragraph.children[0], styles,
        )

        assert_equal(False, properties.bold)
        assert_equal(
            ["document-default", "paragraph-style:P", "character-style:C", "run"],
            sources["bold"],
        )

    def test_layers_that_do_not_mention_a_property_leave_it_unchanged(self):
        styles = _styles(
            default_character_properties={"bold": True},
            character_styles={
                "C": Style(style_id="C", name="C", character_properties={"italic": True}),
            },
        )
        paragraph = _paragraph([_run("Hello", style_id="C")])

        properties, _ = emphasis.effective_character_properties(
            paragraph, paragraph.children[0], styles,
        )

        assert_equal(True, properties.bold)
        assert_equal(True, properties.italic)


class ExtractEmphasisTests(object):
    def test_emphasised_run_is_returned_as_a_ranged_span(self):
        paragraph = _paragraph([
            documents.run([]),
            _run("Hello ", character_properties={"bold": True}),
            _run("world"),
        ])

        result = emphasis.extract_emphasis(documents.document([paragraph]), _styles())

        assert_equal(1, len(result.emphasis_spans))
        span = result.emphasis_spans[0]
        assert_equal(0, span.start)
        assert_equal(6, span.end)
        assert_equal("Hello ", span.text)
        assert_equal(emphasis.EffectiveProperties(bold=True), span.properties)

    def test_adjacent_runs_with_the_same_effective_properties_are_merged(self):
        paragraph = _paragraph([
            _run("Hello", character_properties={"bold": True}),
            _run(" world", character_properties={"bold": True}),
        ])

        result = emphasis.extract_emphasis(documents.document([paragraph]), _styles())

        assert_equal(1, len(result.spans))
        assert_equal((0, 11), (result.spans[0].start, result.spans[0].end))
        assert_equal("Hello world", result.spans[0].text)

    def test_adjacent_runs_with_different_properties_are_split(self):
        paragraph = _paragraph([
            _run("Hello", character_properties={"bold": True}),
            _run(" world", character_properties={"italic": True}),
        ])

        result = emphasis.extract_emphasis(documents.document([paragraph]), _styles())

        assert_equal(["Hello", " world"], [span.text for span in result.spans])

    def test_text_separated_by_a_hyperlink_is_merged_when_properties_match(self):
        paragraph = _paragraph([
            _run("Hello", character_properties={"bold": True}),
            documents.hyperlink(children=[
                _run(" there", character_properties={"bold": True}),
            ], href="http://example.com"),
            _run(" world", character_properties={"bold": True}),
        ])

        result = emphasis.extract_emphasis(documents.document([paragraph]), _styles())

        assert_equal(1, len(result.emphasis_spans))
        assert_equal("Hello there world", result.emphasis_spans[0].text)
        assert_equal((0, 17), (result.emphasis_spans[0].start, result.emphasis_spans[0].end))

    def test_text_separated_by_a_hyperlink_is_split_when_properties_differ(self):
        paragraph = _paragraph([
            _run("Hello", character_properties={"bold": True}),
            documents.hyperlink(children=[
                _run(" there", character_properties={"italic": True}),
            ], href="http://example.com"),
        ])

        result = emphasis.extract_emphasis(documents.document([paragraph]), _styles())

        assert_equal(["Hello", " there"], [span.text for span in result.emphasis_spans])

    def test_non_text_content_breaks_adjacency_but_bookmarks_do_not(self):
        paragraph_with_tab = _paragraph([
            _run("ab", character_properties={"bold": True}),
            documents.tab(),
            _run("cd", character_properties={"bold": True}),
        ])
        result = emphasis.extract_emphasis(
            documents.document([paragraph_with_tab]), _styles(),
        )
        assert_equal(["ab", "cd"], [span.text for span in result.emphasis_spans])

        paragraph_with_bookmark = _paragraph([
            _run("ab", character_properties={"bold": True}),
            documents.bookmark("anchor"),
            _run("cd", character_properties={"bold": True}),
        ])
        result = emphasis.extract_emphasis(
            documents.document([paragraph_with_bookmark]), _styles(),
        )
        assert_equal(1, len(result.emphasis_spans))
        assert_equal("abcd", result.emphasis_spans[0].text)

    def test_double_flip_returns_to_plain_text_but_keeps_the_judgment_basis(self):
        styles = _styles(
            paragraph_styles={
                "P": Style(style_id="P", name="P", character_properties={"bold": True}),
            },
            character_styles={
                "C": Style(style_id="C", name="C", character_properties={"bold": False}),
            },
        )
        paragraph = _paragraph([_run("Hello", style_id="C")], style_id="P")

        result = emphasis.extract_emphasis(documents.document([paragraph]), styles)

        assert_equal(0, len(result.emphasis_spans))
        assert_equal(1, len(result.spans))
        span = result.spans[0]
        assert_equal(False, span.is_emphasised)
        assert_equal(False, span.properties.bold)
        assert_equal(
            ["paragraph-style:P", "character-style:C"],
            span.sources["bold"],
        )
        assert_equal(span, result.segment_at(0, 0))

    def test_paragraph_index_is_recorded_for_each_paragraph(self):
        first = _paragraph([_run("one", character_properties={"bold": True})])
        second = _paragraph([_run("two", character_properties={"bold": True})])

        result = emphasis.extract_emphasis(
            documents.document([first, second]), _styles(),
        )

        assert_equal([0, 1], [span.paragraph_index for span in result.emphasis_spans])
        assert_equal(0, result.segment_at(1, 0).start)

    def test_segment_at_returns_none_outside_of_any_segment(self):
        paragraph = _paragraph([_run("Hello", character_properties={"bold": True})])

        result = emphasis.extract_emphasis(documents.document([paragraph]), _styles())

        assert_equal(None, result.segment_at(0, 99))
        assert_equal(None, result.segment_at(1, 0))
