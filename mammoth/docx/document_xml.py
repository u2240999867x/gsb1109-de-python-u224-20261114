from .. import documents, results
from . import sections_xml


def read_document_xml_element(
        element,
        body_reader,
        notes=None,
        comments=None,
        even_and_odd_headers=False,
        read_header_part=None):

    if notes is None:
        notes = []
    if comments is None:
        comments = []
    if read_header_part is None:
        read_header_part = _missing_header_part_reader

    body_element = element.find_child("w:body")

    if body_element is None:
        raise ValueError("Could not find the body element: are you sure this is a docx file?")

    body_result = body_reader.read_all(body_element.children)
    header_result = sections_xml.read_header_index(
        body_element,
        even_and_odd_headers,
        read_header_part,
    )

    return results.combine([body_result, header_result]) \
        .map(lambda values: documents.document(
            values[0],
            notes=documents.notes(notes),
            comments=comments,
            sections=values[1],
        ))


def _missing_header_part_reader(relationship_id):
    return results.Result(None, [])
