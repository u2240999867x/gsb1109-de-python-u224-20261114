import io
import textwrap
import zipfile

from mammoth import documents, results
from mammoth import docx as docx_package
from mammoth.docx import sections_xml
from mammoth.docx.xmlparser import element as xml_element
from ..testing import assert_equal


# --- XML construction helpers --------------------------------------------

def _header_reference(slot, relationship_id):
    reference_type = {
        documents.HEADER_SLOT_ODD: "default",
        documents.HEADER_SLOT_FIRST: "first",
        documents.HEADER_SLOT_EVEN: "even",
    }[slot]
    return xml_element("w:headerReference", {
        "w:type": reference_type,
        "r:id": relationship_id,
    })


def _sect_pr(references, title_page=False):
    children = list(references)
    if title_page:
        children.append(xml_element("w:titlePg", {}))
    return xml_element("w:sectPr", {}, children)


def _paragraph_section(references=(), title_page=False):
    properties = xml_element("w:pPr", {}, [_sect_pr(references, title_page)])
    return xml_element("w:p", {}, [properties])


def _body(sections, final_section=None):
    children = list(sections)
    if final_section is not None:
        children.append(final_section)
    return xml_element("w:body", {}, children)


def _part(part_name, text="", is_empty=None):
    text = text.strip()
    if is_empty is None:
        is_empty = text == ""
    return documents.header_part(part_name=part_name, is_empty=is_empty, text=text)


def _part_reader(parts):
    def read_header_part(relationship_id):
        if relationship_id in parts:
            return results.success(parts[relationship_id])
        return results.Result(None, [results.warning("missing part for " + relationship_id)])

    return read_header_part


def _read_index(body, even_and_odd_headers=False, parts=None):
    return sections_xml.read_header_index(
        body,
        even_and_odd_headers,
        _part_reader(parts or {}),
    )


# --- Explicit, empty and missing declarations ----------------------------

def test_explicit_header_reference_resolves_to_named_part():
    body = _body([], final_section=_sect_pr([
        _header_reference(documents.HEADER_SLOT_ODD, "rId1"),
    ]))

    result = _read_index(body, parts={"rId1": _part("word/header1.xml", text="Section One\n\n")})
    odd = result.value.find_section(0).odd

    assert_equal(documents.HEADER_ORIGIN_EXPLICIT, odd.origin)
    assert_equal(True, odd.enabled)
    assert_equal(0, odd.source_section_index)
    assert_equal("rId1", odd.relationship_id)
    assert_equal("word/header1.xml", odd.part_name)
    assert_equal("Section One", odd.text)


def test_explicitly_empty_header_part_is_distinct_from_missing_declaration():
    body = _body([], final_section=_sect_pr([
        _header_reference(documents.HEADER_SLOT_ODD, "rIdEmpty"),
    ]))

    result = _read_index(body, parts={"rIdEmpty": _part("word/header3.xml", text="")})
    odd = result.value.find_section(0).odd

    assert_equal(documents.HEADER_ORIGIN_EMPTY, odd.origin)
    assert_equal("word/header3.xml", odd.part_name)
    assert_equal("", odd.text)


def test_referenced_part_that_is_absent_is_reported_as_missing_with_warning():
    body = _body([], final_section=_sect_pr([
        _header_reference(documents.HEADER_SLOT_ODD, "rIdGhost"),
    ]))

    result = _read_index(body, parts={})
    odd = result.value.find_section(0).odd

    assert_equal(documents.HEADER_ORIGIN_MISSING, odd.origin)
    assert_equal(None, odd.part_name)
    assert_equal("rIdGhost", odd.relationship_id)
    assert_equal(1, len(result.messages))


def test_slot_without_any_declaration_is_none_not_empty():
    references = [_header_reference(documents.HEADER_SLOT_ODD, "rIdOdd")]
    body = _body([], final_section=_sect_pr(references))
    parts = {"rIdOdd": _part("word/headerOdd.xml", text="Odd\n\n")}

    result = _read_index(body, even_and_odd_headers=True, parts=parts)
    even = result.value.find_section(0).even

    assert_equal(True, even.enabled)
    assert_equal(documents.HEADER_ORIGIN_NONE, even.origin)
    assert_equal(None, even.part_name)
    assert_equal(None, even.source_section_index)


# --- Inheritance across sections -----------------------------------------

def test_section_without_reference_inherits_previous_section_header():
    first = _paragraph_section([_header_reference(documents.HEADER_SLOT_ODD, "rId1")])
    second = _paragraph_section([])
    final = _sect_pr([])
    body = _body([first, second], final_section=final)

    result = _read_index(body, parts={"rId1": _part("word/header1.xml", text="Section One\n\n")})

    section_zero = result.value.find_section(0).odd
    section_one = result.value.find_section(1).odd

    assert_equal(documents.HEADER_ORIGIN_EXPLICIT, section_zero.origin)
    assert_equal(documents.HEADER_ORIGIN_INHERITED, section_one.origin)
    assert_equal(0, section_one.source_section_index)
    assert_equal("rId1", section_one.relationship_id)
    assert_equal("Section One", section_one.text)


def test_explicit_empty_header_overrides_inheritance_and_hides_earlier_text():
    first = _paragraph_section([_header_reference(documents.HEADER_SLOT_ODD, "rId1")])
    second = _paragraph_section([])
    third = _sect_pr([_header_reference(documents.HEADER_SLOT_ODD, "rId3")])
    body = _body([first, second], final_section=third)

    result = _read_index(body, parts={
        "rId1": _part("word/header1.xml", text="Section One\n\n"),
        "rId3": _part("word/header3.xml", text=""),
    })

    section_two = result.value.find_section(2).odd

    assert_equal(documents.HEADER_ORIGIN_EMPTY, section_two.origin)
    assert_equal(2, section_two.source_section_index)
    assert_equal("rId3", section_two.relationship_id)
    assert_equal("word/header3.xml", section_two.part_name)
    assert_equal("", section_two.text)
    assert_equal(False, "Section One" in section_two.text)


def test_missing_header_is_inherited_as_missing_until_overridden():
    first = _paragraph_section([_header_reference(documents.HEADER_SLOT_ODD, "rIdGhost")])
    second = _paragraph_section([])
    body = _body([first], final_section=second)

    result = _read_index(body, parts={})

    assert_equal(documents.HEADER_ORIGIN_MISSING, result.value.find_section(0).odd.origin)
    inherited = result.value.find_section(1).odd
    assert_equal(documents.HEADER_ORIGIN_MISSING, inherited.origin)
    assert_equal(0, inherited.source_section_index)


# --- Document-level and section-level toggles ----------------------------

def test_first_page_header_is_disabled_without_title_page():
    references = [_header_reference(documents.HEADER_SLOT_ODD, "rIdOdd")]
    body = _body([], final_section=_sect_pr(references, title_page=False))
    parts = {"rIdOdd": _part("word/headerOdd.xml", text="Odd\n\n")}

    result = _read_index(body, parts=parts)
    first = result.value.find_section(0).first

    assert_equal(False, first.enabled)
    assert_equal(documents.HEADER_ORIGIN_DISABLED, first.origin)


def test_first_page_header_is_enabled_by_title_page_and_none_when_undeclared():
    references = [_header_reference(documents.HEADER_SLOT_ODD, "rIdOdd")]
    body = _body([], final_section=_sect_pr(references, title_page=True))
    parts = {"rIdOdd": _part("word/headerOdd.xml", text="Odd\n\n")}

    result = _read_index(body, parts=parts)
    first = result.value.find_section(0).first

    assert_equal(True, first.enabled)
    assert_equal(documents.HEADER_ORIGIN_NONE, first.origin)


def test_explicit_title_page_off_value_disables_first_page_header():
    section = xml_element("w:sectPr", {}, [
        _header_reference(documents.HEADER_SLOT_ODD, "rIdOdd"),
        xml_element("w:titlePg", {"w:val": "0"}),
    ])
    body = _body([], final_section=section)
    parts = {"rIdOdd": _part("word/headerOdd.xml", text="Odd\n\n")}

    result = _read_index(body, parts=parts)
    first = result.value.find_section(0).first

    assert_equal(False, first.enabled)
    assert_equal(documents.HEADER_ORIGIN_DISABLED, first.origin)


def test_settings_even_and_odd_toggle_respects_explicit_off_value():
    from mammoth.docx.settings_xml import read_settings_xml_element

    on = xml_element("w:settings", {}, [xml_element("w:evenAndOddHeaders", {})])
    off = xml_element("w:settings", {}, [xml_element("w:evenAndOddHeaders", {"w:val": "0"})])
    absent = xml_element("w:settings", {}, [])

    assert_equal(True, read_settings_xml_element(on))
    assert_equal(False, read_settings_xml_element(off))
    assert_equal(False, read_settings_xml_element(absent))


def test_even_page_header_reflects_document_even_and_odd_toggle():
    references = [
        _header_reference(documents.HEADER_SLOT_ODD, "rIdOdd"),
        _header_reference(documents.HEADER_SLOT_EVEN, "rIdEven"),
    ]
    parts = {
        "rIdOdd": _part("word/headerOdd.xml", text="Odd\n\n"),
        "rIdEven": _part("word/headerEven.xml", text="Even\n\n"),
    }

    disabled = _read_index(
        _body([], final_section=_sect_pr(references)),
        even_and_odd_headers=False,
        parts=parts,
    )
    assert_equal(documents.HEADER_ORIGIN_DISABLED, disabled.value.find_section(0).even.origin)

    enabled = _read_index(
        _body([], final_section=_sect_pr(references)),
        even_and_odd_headers=True,
        parts=parts,
    )
    even = enabled.value.find_section(0).even
    assert_equal(True, even.enabled)
    assert_equal(documents.HEADER_ORIGIN_EXPLICIT, even.origin)
    assert_equal("Even", even.text)


def test_odd_header_is_always_enabled():
    references = [_header_reference(documents.HEADER_SLOT_EVEN, "rIdEven")]
    parts = {"rIdEven": _part("word/headerEven.xml", text="Even\n\n")}

    result = _read_index(
        _body([], final_section=_sect_pr(references)),
        even_and_odd_headers=True,
        parts=parts,
    )
    assert_equal(True, result.value.find_section(0).odd.enabled)


def test_index_is_empty_when_document_declares_no_headers_anywhere():
    body = _body([_paragraph_section([])], final_section=_sect_pr([]))

    result = _read_index(body, even_and_odd_headers=True)

    assert_equal([], result.value.sections)
    assert_equal(documents.header_index([], even_and_odd_headers=True), result.value)


# --- End-to-end package reading ------------------------------------------

_RELATIONSHIP_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


def test_reading_a_docx_builds_a_traceable_three_section_header_index():
    fileobj = _create_zip({
        "_rels/.rels": _package_relationships(),
        "word/document.xml": _document_xml(),
        "word/_rels/document.xml.rels": _document_relationships(),
        "word/settings.xml": textwrap.dedent("""\
            <?xml version="1.0" encoding="utf-8"?>
            <w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                <w:evenAndOddHeaders/>
            </w:settings>
        """),
        "word/header1.xml": _header_xml("SECTION ONE HEADER"),
        "word/header3.xml": _header_xml(""),
    })

    result = docx_package.read(fileobj=fileobj)
    assert_equal([], result.messages)

    index = result.value.sections
    assert_equal(True, index.even_and_odd_headers)
    assert_equal(3, len(index.sections))

    first = index.find_section(0).odd
    second = index.find_section(1).odd
    third = index.find_section(2).odd

    assert_equal(documents.HEADER_ORIGIN_EXPLICIT, first.origin)
    assert_equal("word/header1.xml", first.part_name)
    assert_equal(True, "SECTION ONE HEADER" in first.text)

    assert_equal(documents.HEADER_ORIGIN_INHERITED, second.origin)
    assert_equal(0, second.source_section_index)
    assert_equal(True, "SECTION ONE HEADER" in second.text)

    assert_equal(documents.HEADER_ORIGIN_EMPTY, third.origin)
    assert_equal("word/header3.xml", third.part_name)
    assert_equal("", third.text.strip())
    assert_equal(False, "SECTION ONE HEADER" in third.text)


def test_missing_referenced_header_part_is_warned_and_traceable():
    document_xml = textwrap.dedent("""\
        <?xml version="1.0" encoding="utf-8"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
            <w:body>
                <w:p/>
                <w:sectPr>
                    <w:headerReference w:type="default" r:id="rIdGhost"/>
                </w:sectPr>
            </w:body>
        </w:document>
    """)
    relationships = textwrap.dedent("""\
        <?xml version="1.0" encoding="utf-8"?>
        <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
            <Relationship Type="{0}header" Target="header-ghost.xml" Id="rIdGhost"/>
        </Relationships>
    """).format(_RELATIONSHIP_TYPE)

    fileobj = _create_zip({
        "_rels/.rels": _package_relationships(),
        "word/document.xml": document_xml,
        "word/_rels/document.xml.rels": relationships,
    })

    result = docx_package.read(fileobj=fileobj)

    assert_equal(1, len(result.messages))
    odd = result.value.sections.find_section(0).odd
    assert_equal(documents.HEADER_ORIGIN_MISSING, odd.origin)
    assert_equal("rIdGhost", odd.relationship_id)
    assert_equal(None, odd.part_name)
    assert_equal(True, "missing" in odd.detail)


def _create_zip(files):
    fileobj = io.BytesIO()
    with zipfile.ZipFile(fileobj, "w") as archive:
        for name, contents in files.items():
            archive.writestr(name, contents)
    fileobj.seek(0)
    return fileobj


def _package_relationships():
    return textwrap.dedent("""\
        <?xml version="1.0" encoding="utf-8"?>
        <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
            <Relationship Type="{0}officeDocument" Target="/word/document.xml" Id="rIdDoc"/>
        </Relationships>
    """).format(_RELATIONSHIP_TYPE)


def _document_relationships():
    return textwrap.dedent("""\
        <?xml version="1.0" encoding="utf-8"?>
        <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
            <Relationship Type="{0}settings" Target="settings.xml" Id="rIdSettings"/>
            <Relationship Type="{0}header" Target="header1.xml" Id="rIdH1"/>
            <Relationship Type="{0}header" Target="header3.xml" Id="rIdH3"/>
        </Relationships>
    """).format(_RELATIONSHIP_TYPE)


def _document_xml():
    return textwrap.dedent("""\
        <?xml version="1.0" encoding="utf-8"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
            <w:body>
                <w:p>
                    <w:pPr>
                        <w:sectPr>
                            <w:headerReference w:type="default" r:id="rIdH1"/>
                        </w:sectPr>
                    </w:pPr>
                </w:p>
                <w:p>
                    <w:pPr>
                        <w:sectPr/>
                    </w:pPr>
                </w:p>
                <w:p/>
                <w:sectPr>
                    <w:headerReference w:type="default" r:id="rIdH3"/>
                </w:sectPr>
            </w:body>
        </w:document>
    """)


def _header_xml(text):
    return textwrap.dedent("""\
        <?xml version="1.0" encoding="utf-8"?>
        <w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
            <w:p>
                <w:r>
                    <w:t>{0}</w:t>
                </w:r>
            </w:p>
        </w:hdr>
    """).format(text)
