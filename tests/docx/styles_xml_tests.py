from mammoth.docx.xmlparser import element as xml_element
from mammoth.docx.styles_xml import read_styles_xml_element
from ..testing import assert_equal


def test_paragraph_style_is_null_if_no_style_with_that_id_exists():
    element = xml_element("w:styles")
    styles = read_styles_xml_element(element)
    assert_equal(None, styles.find_paragraph_style_by_id("Heading1"))


def test_paragraph_style_can_be_found_by_id():
    element = xml_element("w:styles", {}, [
        _paragraph_style_element("Heading1", "Heading 1"),
    ])
    styles = read_styles_xml_element(element)
    assert_equal(
        "Heading1",
        styles.find_paragraph_style_by_id("Heading1").style_id
    )


def test_character_style_can_be_found_by_id():
    element = xml_element("w:styles", {}, [
        _character_style_element("Heading1Char", "Heading 1 Char"),
    ])
    styles = read_styles_xml_element(element)
    assert_equal(
        "Heading1Char",
        styles.find_character_style_by_id("Heading1Char").style_id
    )


def test_table_style_can_be_found_by_id():
    element = xml_element("w:styles", {}, [
        _table_style_element("TableNormal", "Normal Table"),
    ])
    styles = read_styles_xml_element(element)
    assert_equal(
        "TableNormal",
        styles.find_table_style_by_id("TableNormal").style_id
    )


def test_paragraph_and_character_styles_are_distinct():
    element = xml_element("w:styles", {}, [
        _paragraph_style_element("Heading1", "Heading 1"),
        _character_style_element("Heading1Char", "Heading 1 Char"),
    ])
    styles = read_styles_xml_element(element)
    assert_equal(None, styles.find_character_style_by_id("Heading1"))
    assert_equal(None, styles.find_paragraph_style_by_id("Heading1Char"))


def test_styles_include_names():
    element = xml_element("w:styles", {}, [
        _paragraph_style_element("Heading1", "Heading 1"),
    ])
    styles = read_styles_xml_element(element)
    assert_equal(
        "Heading 1",
        styles.find_paragraph_style_by_id("Heading1").name
    )


def test_style_name_is_none_if_name_element_does_not_exist():
    element = xml_element("w:styles", {}, [
        _style_without_name_element("paragraph", "Heading1"),
        _style_without_name_element("character", "Heading1Char")
    ])
    styles = read_styles_xml_element(element)
    assert_equal(None, styles.find_paragraph_style_by_id("Heading1").name)
    assert_equal(None, styles.find_character_style_by_id("Heading1Char").name)


def test_numbering_style_is_none_if_no_style_with_that_id_exists():
    element = xml_element("w:styles", {}, [])
    styles = read_styles_xml_element(element)
    assert_equal(None, styles.find_numbering_style_by_id("List1"))


def test_numbering_style_has_none_num_id_if_style_has_no_paragraph_properties():
    element = xml_element("w:styles", {}, [
        xml_element("w:style", {"w:type": "numbering", "w:styleId": "List1"}),
    ])
    styles = read_styles_xml_element(element)
    assert_equal(None, styles.find_numbering_style_by_id("List1").num_id)


def test_numbering_style_has_num_id_read_from_paragraph_properties():
    element = xml_element("w:styles", {}, [
        xml_element("w:style", {"w:type": "numbering", "w:styleId": "List1"}, [
            xml_element("w:pPr", {}, [
                xml_element("w:numPr", {}, [
                    xml_element("w:numId", {"w:val": "42"})
                ]),
            ]),
        ]),
    ])
    styles = read_styles_xml_element(element)
    assert_equal("42", styles.find_numbering_style_by_id("List1").num_id)


def test_when_multiple_style_elements_have_same_style_id_then_only_first_element_is_used():
    element = xml_element("w:styles", {}, [
        _table_style_element("TableNormal", "Normal Table"),
        _table_style_element("TableNormal", "Table Normal"),
    ])
    styles = read_styles_xml_element(element)
    assert_equal(
        "Normal Table",
        styles.find_table_style_by_id("TableNormal").name
    )


def _paragraph_style_element(style_id, name):
    return _style_element("paragraph", style_id, name)

def _character_style_element(style_id, name):
    return _style_element("character", style_id, name)

def _table_style_element(style_id, name):
    return _style_element("table", style_id, name)

def _style_element(element_type, style_id, name):
    children = [xml_element("w:name", {"w:val": name}, [])]
    return _style_element_with_children(element_type, style_id, children)

def _style_without_name_element(element_type, style_id):
    return _style_element_with_children(element_type, style_id, [])

def _style_element_with_children(element_type, style_id, children):
    attributes = {"w:type": element_type, "w:styleId": style_id}
    return xml_element("w:style", attributes, children)


def test_paragraph_outline_level_is_inherited_through_based_on_chain():
    element = xml_element("w:styles", {}, [
        _paragraph_style_with_outline_level("Root", 0),
        _paragraph_style_with_based_on("Middle", "Root"),
        _paragraph_style_with_based_on("Leaf", "Middle"),
    ])
    styles = read_styles_xml_element(element)

    outline_level = styles.find_paragraph_outline_level("Leaf")

    assert_equal(1, outline_level.level)
    assert_equal((), outline_level.cycle)


def test_paragraph_outline_level_of_no_style_is_none():
    styles = read_styles_xml_element(xml_element("w:styles"))
    assert_equal(None, styles.find_paragraph_outline_level(None).level)


def test_paragraph_outline_level_of_outline_level_9_is_none():
    element = xml_element("w:styles", {}, [
        _paragraph_style_with_outline_level("Body", 9),
    ])
    styles = read_styles_xml_element(element)

    assert_equal(None, styles.find_paragraph_outline_level("Body").level)


def test_paragraph_styles_that_inherit_from_each_other_report_cycle():
    element = xml_element("w:styles", {}, [
        _paragraph_style_with_based_on("First", "Second"),
        _paragraph_style_with_based_on("Second", "First"),
    ])
    styles = read_styles_xml_element(element)

    outline_level = styles.find_paragraph_outline_level("First")

    assert_equal(None, outline_level.level)
    assert_equal(("First", "Second", "First"), outline_level.cycle)


def test_paragraph_outline_level_reads_based_on_and_outline_level_attributes():
    element = xml_element("w:styles", {}, [
        xml_element("w:style", {"w:type": "paragraph", "w:styleId": "Heading1"}, [
            xml_element("w:name", {"w:val": "Heading 1"}),
            xml_element("w:basedOn", {"w:val": "Normal"}),
            xml_element("w:pPr", {}, [
                xml_element("w:outlineLvl", {"w:val": "2"}),
            ]),
        ]),
    ])
    style = read_styles_xml_element(element).find_paragraph_style_by_id("Heading1")

    assert_equal("Normal", style.based_on)
    assert_equal(2, style.outline_level)


def _paragraph_style_with_outline_level(style_id, outline_level):
    return xml_element("w:style", {"w:type": "paragraph", "w:styleId": style_id}, [
        xml_element("w:name", {"w:val": style_id}),
        xml_element("w:pPr", {}, [
            xml_element("w:outlineLvl", {"w:val": str(outline_level)}),
        ]),
    ])


def _paragraph_style_with_based_on(style_id, based_on):
    return xml_element("w:style", {"w:type": "paragraph", "w:styleId": style_id}, [
        xml_element("w:name", {"w:val": style_id}),
        xml_element("w:basedOn", {"w:val": based_on}),
    ])
