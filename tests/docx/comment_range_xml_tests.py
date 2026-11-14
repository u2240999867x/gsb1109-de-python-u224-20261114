from mammoth import documents
from mammoth.docx.xmlparser import element as xml_element, text as xml_text
from mammoth.docx import body_xml
from ..testing import assert_equal


def test_comment_range_start_has_id_read():
    element = xml_element("w:commentRangeStart", {"w:id": "7"})
    document = _read_and_get_document_xml_element(element)
    assert_equal(documents.CommentRangeStart("7"), document)


def test_comment_range_end_has_id_read():
    element = xml_element("w:commentRangeEnd", {"w:id": "7"})
    document = _read_and_get_document_xml_element(element)
    assert_equal(documents.CommentRangeEnd("7"), document)


def test_comment_range_start_is_preserved_inside_paragraph():
    paragraph = xml_element("w:p", {}, [
        xml_element("w:commentRangeStart", {"w:id": "7"}),
        xml_element("w:r", {}, [xml_element("w:t", {}, [xml_text("Hello")])]),
        xml_element("w:commentRangeEnd", {"w:id": "7"}),
    ])

    document = _read_and_get_document_xml_element(paragraph)

    assert_equal(
        documents.paragraph([
            documents.CommentRangeStart("7"),
            documents.run([documents.text("Hello")]),
            documents.CommentRangeEnd("7"),
        ]),
        document,
    )


def test_comment_range_markers_contribute_no_text():
    paragraph = xml_element("w:p", {}, [
        xml_element("w:commentRangeStart", {"w:id": "7"}),
        xml_element("w:r", {}, [xml_element("w:t", {}, [xml_text("Hello")])]),
        xml_element("w:commentRangeEnd", {"w:id": "7"}),
    ])

    document = _read_and_get_document_xml_element(paragraph)

    from mammoth.raw_text import extract_raw_text_from_element
    assert_equal("Hello\n\n", extract_raw_text_from_element(document))


def _read_and_get_document_xml_element(element):
    reader = body_xml.reader()
    result = reader.read_all([element])
    assert_equal([], result.messages)
    values = result.value
    if len(values) == 0:
        return None
    elif len(values) == 1:
        return values[0]
    else:
        raise AssertionError("Had {0} elements".format(len(values)))
