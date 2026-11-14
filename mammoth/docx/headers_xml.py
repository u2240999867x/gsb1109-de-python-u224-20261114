from .. import documents
from ..raw_text import extract_raw_text_from_element


def read_header_xml_element(element, body_reader, part_name):
    return body_reader.read_all(element.children).map(lambda children: documents.header_part(
        part_name=part_name,
        is_empty=_is_empty(children),
        text=_extract_text(children),
    ))


def _extract_text(children):
    return "".join(extract_raw_text_from_element(child) for child in children).strip()


def _is_empty(children):
    has_text = _extract_text(children) != ""
    has_media = any(
        _has_descendant_of_type(child, documents.Image)
        for child in children
    )
    return not has_text and not has_media


def _has_descendant_of_type(element, element_type):
    if isinstance(element, element_type):
        return True
    return any(
        _has_descendant_of_type(child, element_type)
        for child in getattr(element, "children", [])
    )
