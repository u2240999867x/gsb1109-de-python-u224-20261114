from mammoth.docx.xmlparser import element as xml_element
from mammoth.docx.relationships_xml import read_relationships_xml_element
from ..testing import assert_equal


def test_relationship_targets_can_be_found_by_id():
    element = xml_element("relationships:Relationships", {}, [
        xml_element("relationships:Relationship", {
            "Id": "rId8",
            "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
            "Target": "http://example.com",
        }),
        xml_element("relationships:Relationship", {
            "Id": "rId2",
            "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
            "Target": "http://example.net",
        }),
    ])
    relationships = read_relationships_xml_element(element)
    assert_equal(
        "http://example.com",
        relationships.find_target_by_relationship_id("rId8"),
    )


def test_relationship_targets_can_be_found_by_type():
    element = xml_element("relationships:Relationships", {}, [
        xml_element("relationships:Relationship", {
            "Id": "rId2",
            "Target": "docProps/core.xml",
            "Type": "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties",
        }),
        xml_element("relationships:Relationship", {
            "Id": "rId1",
            "Target": "word/document.xml",
            "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
        }),
        xml_element("relationships:Relationship", {
            "Id": "rId3",
            "Target": "word/document2.xml",
            "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
        }),
    ])
    relationships = read_relationships_xml_element(element)
    assert_equal(
        ["word/document.xml", "word/document2.xml"],
        relationships.find_targets_by_type("http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"),
    )


def test_when_there_are_no_relationships_of_requested_type_then_empty_list_is_returned():
    element = xml_element("relationships:Relationships", {}, [])
    relationships = read_relationships_xml_element(element)
    assert_equal(
        [],
        relationships.find_targets_by_type("http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"),
    )


def test_target_is_returned_verbatim_without_resolution():
    element = xml_element("relationships:Relationships", {}, [
        _relationship("rId2", _image_type(), "media/image1.png"),
        _relationship("rId3", _image_type(), "/word/image3.png"),
    ])

    relationships = read_relationships_xml_element(element)

    assert_equal("media/image1.png", relationships.find_target_by_relationship_id("rId2"))
    assert_equal("/word/image3.png", relationships.find_target_by_relationship_id("rId3"))


def test_target_mode_defaults_to_internal_and_external_mode_is_captured():
    element = xml_element("relationships:Relationships", {}, [
        _relationship("rId2", _image_type(), "media/image1.png"),
        _relationship(
            "rId3",
            _hyperlink_type(),
            "http://example.com/path#section",
            target_mode="External",
        ),
    ])

    relationships = read_relationships_xml_element(element)
    by_id = dict(
        (relationship.relationship_id, relationship)
        for relationship in relationships.relationships
    )

    assert_equal("Internal", by_id["rId2"].target_mode)
    assert_equal("External", by_id["rId3"].target_mode)


def test_relationships_are_exposed_for_collection():
    element = xml_element("relationships:Relationships", {}, [
        _relationship("rId2", _image_type(), "media/image1.png"),
    ])

    relationships = read_relationships_xml_element(element)

    assert_equal(["rId2"], [
        relationship.relationship_id for relationship in relationships.relationships
    ])


def _relationship(relationship_id, relationship_type, target, target_mode=None):
    attributes = {
        "Id": relationship_id,
        "Type": relationship_type,
        "Target": target,
    }
    if target_mode is not None:
        attributes["TargetMode"] = target_mode
    return xml_element("relationships:Relationship", attributes)


def _image_type():
    return "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"


def _hyperlink_type():
    return "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
