import io
import textwrap
import zipfile

from mammoth.docx.part_relationships import (
    PartRelationship,
    read_part_relationships,
)
from ..testing import assert_equal


_RELATIONSHIPS_TYPE = "http://schemas.openxmlformats.org/package/2006/relationships"
_IMAGE_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
_HYPERLINK_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"

_DOCUMENT_PART = "word/document.xml"
_FOOTNOTES_PART = "word/footnotes.xml"

_IMAGE_BYTES = b"\x89PNG\r\n\x1a\n"


def _relationships_xml(relationships):
    body = "".join(
        '<Relationship Id="{id}" Type="{type}" Target="{target}"{mode}/>'.format(
            id=relationship.get("id"),
            type=relationship.get("type"),
            target=relationship.get("target"),
            mode=' TargetMode="External"' if relationship.get("external") else "",
        )
        for relationship in relationships
    )
    return textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
        <Relationships xmlns="{namespace}">{body}</Relationships>
    """).format(namespace=_RELATIONSHIPS_TYPE, body=body)


_DOCUMENT_XML = textwrap.dedent("""\
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
        <w:body>
            <w:p>
                <w:r>
                    <w:drawing>
                        <a:blip r:embed="rId2"/>
                    </w:drawing>
                </w:r>
            </w:p>
        </w:body>
    </w:document>
""")


_FOOTNOTES_XML = textwrap.dedent("""\
    <w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
        <w:footnote w:id="1">
            <w:p>
                <w:hyperlink r:id="rId2">
                    <w:r><w:t>Example</w:t></w:r>
                </w:hyperlink>
            </w:p>
        </w:footnote>
    </w:footnotes>
""")


def _package_files():
    return {
        "[Content_Types].xml": "<Types/>",
        "_rels/.rels": _relationships_xml([
            {"id": "rId1", "type": _HYPERLINK_TYPE, "target": "http://start.example.com#top", "external": True},
        ]),
        "word/document.xml": _DOCUMENT_XML,
        "word/_rels/document.xml.rels": _relationships_xml([
            {"id": "rId2", "type": _IMAGE_TYPE, "target": "media/image1.png"},
            {"id": "rId3", "type": _IMAGE_TYPE, "target": "media/missing.png"},
        ]),
        "word/footnotes.xml": _FOOTNOTES_XML,
        "word/_rels/footnotes.xml.rels": _relationships_xml([
            {"id": "rId2", "type": _HYPERLINK_TYPE, "target": "http://example.com/path#section", "external": True},
        ]),
        "customXml/item1.xml": "<item/>",
        "customXml/_rels/item1.xml.rels": _relationships_xml([
            {"id": "rId1", "type": _IMAGE_TYPE, "target": "../word/media/image1.png"},
        ]),
        "word/media/image1.png": _IMAGE_BYTES,
    }


def _create_zip(files, reverse=False):
    fileobj = io.BytesIO()
    archive = zipfile.ZipFile(fileobj, "w")
    try:
        for name in sorted(files, reverse=reverse):
            archive.writestr(name, files[name])
    finally:
        archive.close()
    fileobj.seek(0)
    return fileobj


def _read(files, reverse=False):
    return read_part_relationships(_create_zip(files, reverse=reverse))


def test_body_and_footnote_share_rid2_but_resolve_to_their_own_targets():
    relationships = _read(_package_files())

    assert_equal(PartRelationship(
        part=_DOCUMENT_PART,
        relationship_id="rId2",
        relationship_type=_IMAGE_TYPE,
        target="word/media/image1.png",
        target_mode="Internal",
        is_external=False,
        is_dangling=False,
    ), relationships.find(_DOCUMENT_PART, "rId2"))

    assert_equal(PartRelationship(
        part=_FOOTNOTES_PART,
        relationship_id="rId2",
        relationship_type=_HYPERLINK_TYPE,
        target="http://example.com/path#section",
        target_mode="External",
        is_external=True,
        is_dangling=False,
    ), relationships.find(_FOOTNOTES_PART, "rId2"))


def test_external_target_keeps_its_fragment():
    relationships = _read(_package_files())

    package_link = relationships.find("", "rId1")
    assert_equal("http://start.example.com#top", package_link.target)
    assert_equal(True, package_link.is_external)


def test_relative_target_is_resolved_from_the_declaring_part():
    relationships = _read(_package_files())

    custom_xml_link = relationships.find("customXml/item1.xml", "rId1")
    assert_equal("word/media/image1.png", custom_xml_link.target)
    assert_equal(False, custom_xml_link.is_dangling)


def test_internal_target_with_missing_part_is_recorded_as_dangling():
    relationships = _read(_package_files())

    missing_image = relationships.find(_DOCUMENT_PART, "rId3")
    assert_equal("word/media/missing.png", missing_image.target)
    assert_equal(True, missing_image.is_dangling)
    assert_equal(False, missing_image.is_external)


def test_swap_relationship_part_order_is_order_independent():
    files = _package_files()

    assert_equal(
        list(_read(files, reverse=False)),
        list(_read(files, reverse=True)),
    )
