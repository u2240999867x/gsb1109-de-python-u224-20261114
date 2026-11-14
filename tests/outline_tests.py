# coding=utf-8

from __future__ import unicode_literals

import io
import zipfile

import mammoth
from mammoth import documents
from mammoth.outline import extract_section_tree_from_document
from .testing import assert_equal


def test_custom_style_chain_creates_three_level_section_tree():
    tree = _tree_from_docx(
        paragraphs=[
            _paragraph("Part", "Part"),
            _paragraph("Chapter", "Chapter"),
            _paragraph("Section", "Section"),
            _paragraph("Subsection", "Subsection"),
        ],
        styles={
            "Part": {"outline_level": 0},
            "Chapter": {"based_on": "Part", "outline_level": 1},
            "Section": {"based_on": "Chapter", "outline_level": 2},
            "Subsection": {"based_on": "Section"},
        },
    )

    assert_equal(1, len(tree))
    part = tree[0]
    assert_equal("Part", part.title)
    assert_equal(1, part.level)
    assert_equal(0, part.paragraph_index)

    assert_equal(["Chapter"], [section.title for section in part.children])
    chapter = part.children[0]
    assert_equal(2, chapter.level)
    assert_equal(1, chapter.paragraph_index)

    assert_equal(["Section", "Subsection"], [section.title for section in chapter.children])
    section = chapter.children[0]
    assert_equal(3, section.level)
    assert_equal(2, section.paragraph_index)

    subsection = chapter.children[1]
    assert_equal(3, subsection.level)
    assert_equal(3, subsection.paragraph_index)


def test_direct_outline_level_overrides_inherited_level():
    tree = _tree_from_docx(
        paragraphs=[
            _paragraph("Top", "Top"),
            _paragraph("Promoted", "Section", outline_level=0),
        ],
        styles={
            "Top": {"outline_level": 0},
            "Section": {"based_on": "Top", "outline_level": 1},
        },
    )

    assert_equal(["Top", "Promoted"], [section.title for section in tree])
    assert_equal([1, 1], [section.level for section in tree])
    assert_equal(True, tree[1].paragraph.is_outline_level_explicit)


def test_paragraph_without_outline_level_is_not_in_tree():
    tree = _tree_from_docx(
        paragraphs=[
            _paragraph("Plain", "Plain"),
            _paragraph("Top", "Top"),
        ],
        styles={
            "Plain": {},
            "Top": {"outline_level": 0},
        },
    )

    assert_equal(["Top"], [section.title for section in tree])
    assert_equal([1], [section.paragraph_index for section in tree])


def test_direct_outline_level_9_removes_paragraph_from_tree():
    tree = _tree_from_docx(
        paragraphs=[
            _paragraph("Top", "Top"),
            _paragraph("Body", "Top", outline_level=9),
            _paragraph("Next", "Top"),
        ],
        styles={
            "Top": {"outline_level": 0},
        },
    )

    assert_equal(["Top", "Next"], [section.title for section in tree])
    assert_equal([0, 2], [section.paragraph_index for section in tree])


def test_direct_outline_level_0_overrides_style_body_text_level_9():
    tree = _tree_from_docx(
        paragraphs=[
            _paragraph("Promoted", "Body", outline_level=0),
        ],
        styles={
            "Body": {"outline_level": 9},
        },
    )

    assert_equal(["Promoted"], [section.title for section in tree])
    assert_equal(1, tree[0].level)


def test_style_outline_level_9_removes_inherited_headings_from_tree():
    tree = _tree_from_docx(
        paragraphs=[
            _paragraph("Heading", "Heading"),
            _paragraph("Body text", "Body"),
        ],
        styles={
            "Heading": {"outline_level": 0},
            "Body": {"based_on": "Heading", "outline_level": 9},
        },
    )

    assert_equal(["Heading"], [section.title for section in tree])


def test_styles_inheriting_from_each_other_are_unresolved_and_following_heading_starts_new_chapter():
    tree = _tree_from_docx(
        paragraphs=[
            _paragraph("Before", "Top"),
            _paragraph("Ambiguous", "CycleA"),
            _paragraph("After", "Top"),
        ],
        styles={
            "Top": {"outline_level": 0},
            "CycleA": {"based_on": "CycleB"},
            "CycleB": {"based_on": "CycleA"},
        },
    )

    assert_equal(["Before", "After"], [section.title for section in tree])
    assert_equal(0, tree[0].paragraph_index)
    assert_equal(2, tree[1].paragraph_index)
    assert_equal(1, tree[1].level)

    assert_equal(1, len(tree.unresolved))
    unresolved = tree.unresolved[0]
    assert_equal("Ambiguous", unresolved.title)
    assert_equal(1, unresolved.paragraph_index)
    assert_equal(("CycleA", "CycleB", "CycleA"), unresolved.style_cycle)
    assert "paragraph styles inherit from each other" in unresolved.reason


def test_cycle_is_reported_with_paragraph_position_as_warning():
    result = mammoth.extract_section_tree(_docx(
        paragraphs=[
            _paragraph("Ambiguous", "CycleA"),
            _paragraph("After", "Top"),
        ],
        styles={
            "Top": {"outline_level": 0},
            "CycleA": {"based_on": "CycleB"},
            "CycleB": {"based_on": "CycleA"},
        },
    ))

    assert_equal(1, len(result.messages))
    assert_equal(
        'Paragraph 1 ("Ambiguous") has no reliable outline level: '
        'paragraph styles inherit from each other: CycleA -> CycleB -> CycleA',
        result.messages[0].message,
    )


def test_sections_expose_style_and_explicit_flag():
    tree = _tree_from_docx(
        paragraphs=[
            _paragraph("Inherited", "Top"),
            _paragraph("Direct", "Top", outline_level=0),
        ],
        styles={"Top": {"outline_level": 0}},
    )

    assert_equal(False, tree[0].is_outline_level_explicit)
    assert_equal(True, tree[1].is_outline_level_explicit)
    assert_equal("Top", tree[0].style_id)
    assert_equal("Top", tree[0].style_name)


def test_level_jumps_are_nested_under_nearest_shallower_heading():
    tree = _tree_from_docx(
        paragraphs=[
            _paragraph("Top", "Top"),
            _paragraph("Deep", "Deep"),
            _paragraph("Sibling", "Deep"),
        ],
        styles={
            "Top": {"outline_level": 0},
            "Deep": {"outline_level": 3},
        },
    )

    assert_equal(["Top"], [section.title for section in tree])
    assert_equal(["Deep", "Sibling"], [section.title for section in tree[0].children])
    assert_equal(4, tree[0].children[0].level)


def test_tree_levels_are_clamped_to_nine():
    tree = _tree_from_docx(
        paragraphs=[_paragraph("Deep", "Deep")],
        styles={"Deep": {"outline_level": 8}},
    )

    assert_equal(9, tree[0].level)


def test_table_paragraphs_are_included_with_document_order_positions():
    document = documents.document([
        documents.paragraph(
            [documents.run([documents.text("Top")])],
            style_id="Top",
            style_name="Top",
            outline_level=1,
        ),
        documents.table([
            documents.table_row([
                documents.table_cell([
                    documents.paragraph(
                        [documents.run([documents.text("Inside")])],
                        style_id="Top",
                        style_name="Top",
                        outline_level=2,
                    )
                ])
            ])
        ]),
        documents.paragraph(
            [documents.run([documents.text("After")])],
            style_id="Top",
            style_name="Top",
            outline_level=1,
        ),
    ])

    result = extract_section_tree_from_document(document)

    assert_equal(["Top", "After"], [section.title for section in result.value])
    assert_equal(["Inside"], [section.title for section in result.value[0].children])
    assert_equal([0, 2], [section.paragraph_index for section in result.value])
    assert_equal(1, result.value[0].children[0].paragraph_index)


def test_tree_can_be_serialised_with_paragraph_positions():
    tree = _tree_from_docx(
        paragraphs=[_paragraph("Top", "Top")],
        styles={"Top": {"outline_level": 0}},
    )

    assert_equal(
        [{
            "title": "Top",
            "level": 1,
            "paragraph_index": 0,
            "paragraph_number": 1,
            "children": [],
        }],
        tree.to_dict(),
    )
    assert_equal([], tree.unresolved)


def test_tree_is_iterable_as_a_sequence_of_root_sections():
    tree = _tree_from_docx(
        paragraphs=[
            _paragraph("First", "Top"),
            _paragraph("Second", "Top"),
        ],
        styles={"Top": {"outline_level": 0}},
    )

    assert_equal(["First", "Second"], [section.title for section in tree])
    assert_equal("Second", tree[1].title)
    assert_equal(["First", "Second"], [section.title for section in tree.roots])


def _tree_from_docx(paragraphs, styles):
    return mammoth.extract_section_tree(_docx(paragraphs, styles)).value


def _paragraph(text, style=None, outline_level=None):
    return {"text": text, "style": style, "outline_level": outline_level}


def _docx(paragraphs, styles):
    fileobj = io.BytesIO()
    zip_file = zipfile.ZipFile(fileobj, "w")
    zip_file.writestr("word/document.xml", _document_xml(paragraphs))
    zip_file.writestr("word/styles.xml", _styles_xml(styles))
    zip_file.writestr(
        "_rels/.rels",
        '<?xml version="1.0" encoding="utf-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        '</Relationships>',
    )
    zip_file.close()
    fileobj.seek(0)
    return fileobj


def _document_xml(paragraphs):
    body = "".join(_paragraph_xml(paragraph) for paragraph in paragraphs)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body>' + body + '</w:body>'
        '</w:document>'
    )


def _paragraph_xml(paragraph):
    properties = ""
    if paragraph["style"] is not None or paragraph["outline_level"] is not None:
        properties = "<w:pPr>"
        if paragraph["style"] is not None:
            properties += '<w:pStyle w:val="{0}"/>'.format(paragraph["style"])
        if paragraph["outline_level"] is not None:
            properties += '<w:outlineLvl w:val="{0}"/>'.format(paragraph["outline_level"])
        properties += "</w:pPr>"

    return (
        '<w:p>' + properties +
        '<w:r><w:t>{0}</w:t></w:r>'.format(paragraph["text"]) +
        '</w:p>'
    )


def _styles_xml(styles):
    style_elements = "".join(
        _style_xml(style_id, attributes)
        for style_id, attributes in styles.items()
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        + style_elements +
        '</w:styles>'
    )


def _style_xml(style_id, attributes):
    properties = ""
    if attributes.get("outline_level") is not None:
        properties = (
            '<w:pPr><w:outlineLvl w:val="{0}"/></w:pPr>'.format(
                attributes["outline_level"]
            )
        )

    based_on = ""
    if attributes.get("based_on") is not None:
        based_on = '<w:basedOn w:val="{0}"/>'.format(attributes["based_on"])

    return (
        '<w:style w:type="paragraph" w:styleId="{0}">'.format(style_id) +
        '<w:name w:val="{0}"/>'.format(attributes.get("name", style_id)) +
        based_on +
        properties +
        '</w:style>'
    )
