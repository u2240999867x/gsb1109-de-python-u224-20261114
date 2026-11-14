"""Tests for installer.metadata"""

import textwrap
from email.parser import FeedParser
from email.policy import default

import pytest

from installer.metadata import (
    DescriptionSource,
    DisplayDescription,
    InterpretedMetadata,
    MetadataField,
    ParsedMetadata,
    interpret_metadata,
    parse_metadata,
)


class TestParseMetadata:
    def test_returns_parsed_metadata(self):
        parsed = parse_metadata("Name: package\n")
        assert isinstance(parsed, ParsedMetadata)

    def test_keeps_duplicate_field_order(self):
        contents = textwrap.dedent(
            """\
            Metadata-Version: 2.1
            Name: package
            Requires-Dist: first
            Requires-Dist: second
            Requires-Dist: third
            """
        )
        parsed = parse_metadata(contents)

        requires_dist = [
            field for field in parsed.fields if field.name == "Requires-Dist"
        ]
        assert [field.order for field in requires_dist] == [2, 3, 4]
        assert [field.value for field in requires_dist] == [
            "first",
            "second",
            "third",
        ]
        assert parsed.get_all("requires-dist") == ("first", "second", "third")

    def test_get_all_is_case_insensitive(self):
        parsed = parse_metadata("Name: package\nNAME: other\n")
        assert parsed.get_all("name") == ("package", "other")

    def test_folded_header_keeps_raw_and_unfolded_value(self):
        contents = textwrap.dedent(
            """\
            Description: old style
             folded with space
            \tfolded with tab
            Name: package
            """
        )
        parsed = parse_metadata(contents)
        description = parsed.fields[0]

        assert isinstance(description, MetadataField)
        assert description.name == "Description"
        assert description.value == "old style folded with space\tfolded with tab"
        assert description.raw_value == (
            " old style\n folded with space\n\tfolded with tab\n"
        )
        assert (description.line_start, description.line_end) == (0, 3)
        assert parsed.contents[description.span[0] : description.span[1]] == (
            "Description: old style\n folded with space\n\tfolded with tab\n"
        )

    def test_field_text_reconstructs_original_field(self):
        contents = "Description: old style\n folded line\nName: package\n\nbody\n"
        parsed = parse_metadata(contents)
        assert parsed.fields[0].text() == "Description: old style\n folded line\n"
        assert parsed.fields[1].text() == "Name: package\n"

    def test_body_boundary_is_recorded(self):
        contents = textwrap.dedent(
            """\
            Name: package
            Version: 1.0

            Long description
            spanning lines.
            """
        )
        parsed = parse_metadata(contents)

        assert parsed.body_line_start == 3
        assert parsed.body_line_end == 5
        assert parsed.body() == "Long description\nspanning lines.\n"
        assert parsed.contents[parsed.body_span[0] : parsed.body_span[1]] == (
            "Long description\nspanning lines.\n"
        )

    def test_body_keeps_header_like_lines_as_text(self):
        contents = textwrap.dedent(
            """\
            Metadata-Version: 2.1
            Name: package

            # README
            Requires-Dist: not-a-field
            Name: also-not-a-field
            """
        )
        parsed = parse_metadata(contents)

        assert [field.name for field in parsed.fields] == [
            "Metadata-Version",
            "Name",
        ]
        body = parsed.body()
        assert "Requires-Dist: not-a-field" in body
        assert parsed.get_all("Requires-Dist") == ()

    def test_without_body(self):
        parsed = parse_metadata("Name: package\nVersion: 1.0\n")
        assert parsed.body() is None
        assert parsed.body_span is None
        assert parsed.body_line_start is None
        assert parsed.body_line_end is None

    def test_empty_body_keeps_separator_boundary(self):
        parsed = parse_metadata("Name: package\n\n")
        assert parsed.body() == ""
        assert parsed.body_span == (len("Name: package\n\n"), len("Name: package\n\n"))
        assert (parsed.body_line_start, parsed.body_line_end) == (2, 2)

    @pytest.mark.parametrize(
        "contents",
        [
            "Name: package\nVersion: 1.0\n",
            "Name: package\nVersion: 1.0",
            "Name: package\nFold: a\n b\n\nbody\n",
            "Name: package\r\nFold: a\r\n b\r\n\r\nbody\r\n",
            "Metadata-Version: 2.1\nName: p\nRequires-Dist: a\nRequires-Dist: b\n",
            "Name: p\n\n",
            "",
        ],
    )
    def test_to_source_round_trips(self, contents):
        parsed = parse_metadata(contents)
        assert parsed.to_source() == contents

    def test_matches_email_parser_on_well_formed_input(self):
        contents = textwrap.dedent(
            """\
            Metadata-Version: 2.1
            Name: package
            Requires-Dist: a
            Requires-Dist: b
            Description: folded
             second line
            Classifier: c

            body text
            """
        )
        parsed = parse_metadata(contents)

        feed_parser = FeedParser(policy=default)
        feed_parser.feed(contents)
        message = feed_parser.close()

        assert [(field.name, field.value) for field in parsed.fields] == list(
            message.items()
        )

    def test_line_without_colon_ends_header_section(self):
        contents = "Name: package\njunk line\nVersion: 1\n\nbody\n"
        parsed = parse_metadata(contents)

        assert [field.name for field in parsed.fields] == ["Name"]
        assert parsed.body() == "junk line\nVersion: 1\n\nbody\n"

    def test_orphan_continuation_does_not_start_a_field(self):
        contents = " orphan\nName: package\n\nbody\n"
        parsed = parse_metadata(contents)

        assert [field.name for field in parsed.fields] == ["Name"]
        assert parsed.body() == "body\n"


class TestInterpretMetadata:
    def test_returns_interpreted_metadata(self):
        interpreted = interpret_metadata(parse_metadata("Name: package\n"))
        assert isinstance(interpreted, InterpretedMetadata)

    def test_metadata_version_and_content_type(self):
        contents = textwrap.dedent(
            """\
            Metadata-Version: 2.1
            Description-Content-Type: text/markdown
            Name: package
            """
        )
        interpreted = interpret_metadata(parse_metadata(contents))
        assert interpreted.metadata_version == "2.1"
        assert interpreted.description_content_type == "text/markdown"

    def test_header_description_from_old_metadata(self):
        contents = textwrap.dedent(
            """\
            Metadata-Version: 1.2
            Name: package
            Description: header only
             folded
            """
        )
        interpreted = interpret_metadata(parse_metadata(contents))

        assert len(interpreted.descriptions) == 1
        description = interpreted.descriptions[0]
        assert isinstance(description, DisplayDescription)
        assert description.text == "header only folded"
        assert description.source == DescriptionSource(
            kind="header",
            order=2,
            line_start=2,
            line_end=4,
            span=(len("Metadata-Version: 1.2\nName: package\n"), len(contents)),
        )
        assert (
            contents[description.source.span[0] : description.source.span[1]]
            == "Description: header only\n folded\n"
        )
        assert interpreted.display_description() == "header only folded"

    def test_body_description_from_new_metadata(self):
        contents = textwrap.dedent(
            """\
            Metadata-Version: 2.1
            Name: package

            body description
            """
        )
        interpreted = interpret_metadata(parse_metadata(contents))

        assert len(interpreted.descriptions) == 1
        description = interpreted.descriptions[0]
        assert description.text == "body description\n"
        assert description.source.kind == "body"
        assert description.source.order is None
        assert interpreted.display_description() == "body description\n"

    def test_body_header_like_lines_never_become_fields(self):
        contents = textwrap.dedent(
            """\
            Metadata-Version: 2.1
            Name: package

            Requires-Dist: sneaky
            """
        )
        interpreted = interpret_metadata(parse_metadata(contents))

        assert interpreted.message.get_all("Requires-Dist") is None
        assert interpreted.raw.get_all("Requires-Dist") == ()
        assert "Requires-Dist: sneaky" in interpreted.display_description()

    def test_two_descriptions_have_separate_provenance(self):
        contents = textwrap.dedent(
            """\
            Metadata-Version: 2.1
            Name: package
            Description: header text

            body text
            """
        )
        interpreted = interpret_metadata(parse_metadata(contents))

        assert len(interpreted.descriptions) == 2
        header, body = interpreted.descriptions
        assert header.source.kind == "header"
        assert header.text == "header text"
        assert body.source.kind == "body"
        assert body.text == "body text\n"
        assert header.source.span != body.source.span
        assert header.source.line_start < body.source.line_start

        # Each value can be traced back to the original file.
        assert (
            interpreted.raw.contents[header.source.span[0] : header.source.span[1]]
            == "Description: header text\n"
        )
        assert (
            interpreted.raw.contents[body.source.span[0] : body.source.span[1]]
            == "body text\n"
        )

    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            ("1.0", "header text"),
            ("1.2", "header text"),
            ("2.0", "header text"),
            ("2.1", "body text\n"),
            ("2.2", "body text\n"),
            ("2.1.0", "body text\n"),
            ("2.1rc1", "header text"),
            ("2.1-rc1", "body text\n"),
            (None, "header text"),
        ],
    )
    def test_display_description_follows_metadata_version(self, version, expected):
        version_line = f"Metadata-Version: {version}\n" if version is not None else ""
        contents = (
            f"{version_line}Name: package\nDescription: header text\n\nbody text\n"
        )
        interpreted = interpret_metadata(parse_metadata(contents))
        assert interpreted.display_description() == expected

    def test_display_description_falls_back_to_other_location(self):
        body_only = interpret_metadata(
            parse_metadata("Metadata-Version: 1.2\nName: p\n\nbody only\n")
        )
        assert body_only.display_description() == "body only\n"

        header_only = interpret_metadata(
            parse_metadata("Metadata-Version: 2.1\nName: p\nDescription: header only\n")
        )
        assert header_only.display_description() == "header only"

    def test_no_description(self):
        interpreted = interpret_metadata(
            parse_metadata("Metadata-Version: 2.1\nName: package\n")
        )
        assert interpreted.descriptions == ()
        assert interpreted.display_description() is None

    def test_header_description_provenance_marks_fold_range(self):
        contents = textwrap.dedent(
            """\
            Metadata-Version: 1.0
            Description: line one
             line two
             line three
            Name: package
            """
        )
        interpreted = interpret_metadata(parse_metadata(contents))
        source = interpreted.descriptions[0].source

        assert source.kind == "header"
        assert (source.line_start, source.line_end) == (1, 4)
        assert interpreted.raw.contents[source.span[0] : source.span[1]] == (
            "Description: line one\n line two\n line three\n"
        )
