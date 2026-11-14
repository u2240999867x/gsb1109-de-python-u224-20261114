from .. import documents
from .xmlparser import XmlElement


_ALIGNMENTS = {
    "start": "left",
    "left": "left",
    "center": "center",
    "end": "right",
    "right": "right",
    "decimal": "decimal",
}


def read_tabs_element(element):
    """Read a w:tabs element into (tab_stops, cleared_positions).

    tab_stops is a list of documents.TabStop. cleared_positions is the
    list of positions at which a w:tab element with w:val="clear" cancels
    an inherited tab stop.
    """
    tab_stops = []
    cleared_positions = []
    for child in element.children:
        if not (isinstance(child, XmlElement) and child.name == "w:tab"):
            continue
        position = _read_position(child)
        if position is None:
            continue
        val = child.attributes.get("w:val")
        if val == "clear":
            cleared_positions.append(position)
        elif val == "bar":
            # Bar tabs draw a vertical rule rather than aligning text.
            continue
        else:
            alignment = _ALIGNMENTS.get(val, "left")
            tab_stops.append(documents.tab_stop(position=position, alignment=alignment))
    return tab_stops, cleared_positions


def _read_position(element):
    raw_position = element.attributes.get("w:pos")
    if raw_position is None:
        return None
    try:
        return int(raw_position)
    except ValueError:
        return None
