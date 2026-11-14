from .. import documents, results
from .xmlparser import XmlElement


def read_header_index(body_element, even_and_odd_headers, read_header_part):
    section_elements = _find_section_properties(body_element)

    resolved_sections = []
    messages = []
    last_declared = {
        documents.HEADER_SLOT_FIRST: None,
        documents.HEADER_SLOT_ODD: None,
        documents.HEADER_SLOT_EVEN: None,
    }

    for section_index, section_element in enumerate(section_elements):
        title_page = _is_on(section_element.find_child("w:titlePg"))
        references = _read_header_references(section_element)

        slot_sources = {}
        for slot in documents.HEADER_SLOTS:
            enabled = _slot_is_enabled(slot, title_page, even_and_odd_headers)
            relationship_id = references.get(slot)

            if not enabled:
                slot_sources[slot] = documents.header_source(
                    section_index=section_index,
                    slot=slot,
                    enabled=False,
                    origin=documents.HEADER_ORIGIN_DISABLED,
                    detail=_disabled_detail(slot),
                )
                continue

            if relationship_id is not None:
                source, part_messages = _read_declared_source(
                    section_index=section_index,
                    slot=slot,
                    relationship_id=relationship_id,
                    read_header_part=read_header_part,
                )
                messages.extend(part_messages)
                last_declared[slot] = source
                slot_sources[slot] = source
            elif last_declared[slot] is not None:
                slot_sources[slot] = _inherited_source(section_index, slot, last_declared[slot])
            else:
                slot_sources[slot] = documents.header_source(
                    section_index=section_index,
                    slot=slot,
                    enabled=True,
                    origin=documents.HEADER_ORIGIN_NONE,
                    detail="no header is declared for this section or any previous section",
                )

        resolved_sections.append(documents.section_headers(
            section_index=section_index,
            title_page=title_page,
            first=slot_sources[documents.HEADER_SLOT_FIRST],
            odd=slot_sources[documents.HEADER_SLOT_ODD],
            even=slot_sources[documents.HEADER_SLOT_EVEN],
        ))

    if not _has_any_declaration(resolved_sections):
        index = documents.header_index([], even_and_odd_headers=even_and_odd_headers)
    else:
        index = documents.header_index(resolved_sections, even_and_odd_headers=even_and_odd_headers)

    return results.Result(index, messages)


def _find_section_properties(body_element):
    section_elements = []
    for child in body_element.children:
        if not isinstance(child, XmlElement):
            continue
        if child.name == "w:p":
            properties = child.find_child("w:pPr")
            if properties is not None:
                section = properties.find_child("w:sectPr")
                if section is not None:
                    section_elements.append(section)
        elif child.name == "w:sectPr":
            section_elements.append(child)
    return section_elements


def _read_header_references(section_element):
    references = {}
    for reference in section_element.find_children("w:headerReference"):
        relationship_id = reference.attributes.get("r:id")
        slot = _slot_from_type(reference.attributes.get("w:type"))
        references[slot] = relationship_id
    return references


def _slot_from_type(value):
    if value == "first":
        return documents.HEADER_SLOT_FIRST
    if value == "even":
        return documents.HEADER_SLOT_EVEN
    return documents.HEADER_SLOT_ODD


def _slot_is_enabled(slot, title_page, even_and_odd_headers):
    if slot == documents.HEADER_SLOT_FIRST:
        return title_page
    if slot == documents.HEADER_SLOT_EVEN:
        return even_and_odd_headers
    return True


def _is_on(element):
    if element is None:
        return False
    value = element.attributes.get("w:val")
    return value not in ["false", "0"]


def _disabled_detail(slot):
    if slot == documents.HEADER_SLOT_FIRST:
        return "first-page header is disabled because this section has no title page (w:titlePg)"
    return "even-page headers are disabled because even and odd headers (w:evenAndOddHeaders) is off"


def _read_declared_source(section_index, slot, relationship_id, read_header_part):
    part_result = read_header_part(relationship_id)
    messages = list(part_result.messages)
    part = part_result.value

    if part is None:
        detail = "header reference '{0}' points to a part that is missing from the package".format(
            relationship_id,
        )
        source = documents.header_source(
            section_index=section_index,
            slot=slot,
            enabled=True,
            origin=documents.HEADER_ORIGIN_MISSING,
            source_section_index=section_index,
            relationship_id=relationship_id,
            detail=detail,
        )
        return source, messages

    if part.is_empty:
        origin = documents.HEADER_ORIGIN_EMPTY
        detail = "header reference '{0}' resolves to explicitly empty part '{1}'".format(
            relationship_id, part.part_name,
        )
    else:
        origin = documents.HEADER_ORIGIN_EXPLICIT
        detail = "header reference '{0}' resolves to part '{1}'".format(
            relationship_id, part.part_name,
        )

    source = documents.header_source(
        section_index=section_index,
        slot=slot,
        enabled=True,
        origin=origin,
        source_section_index=section_index,
        relationship_id=relationship_id,
        part_name=part.part_name,
        text=part.text,
        detail=detail,
    )
    return source, messages


def _inherited_source(section_index, slot, declared_source):
    if declared_source.origin == documents.HEADER_ORIGIN_MISSING:
        origin = documents.HEADER_ORIGIN_MISSING
    else:
        origin = documents.HEADER_ORIGIN_INHERITED

    detail = "inherited from section {0}: {1}".format(
        declared_source.source_section_index,
        declared_source.detail,
    )

    return documents.header_source(
        section_index=section_index,
        slot=slot,
        enabled=True,
        origin=origin,
        source_section_index=declared_source.source_section_index,
        relationship_id=declared_source.relationship_id,
        part_name=declared_source.part_name,
        text=declared_source.text,
        detail=detail,
    )


def _has_any_declaration(resolved_sections):
    for section in resolved_sections:
        for slot in documents.HEADER_SLOTS:
            source = section.find_slot(slot)
            if source.origin != documents.HEADER_ORIGIN_DISABLED and source.origin != documents.HEADER_ORIGIN_NONE:
                return True
    return False
