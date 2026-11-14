"""Reconstruct which tab stop each field of a line aligns to.

Old price lists are often one paragraph per line, with the item name,
quantity and prices aligned using Word tab stops rather than a table.
Given the effective tab stops of a paragraph (see
:func:`effective_tab_stops`) and the measured width of each text segment
between tabs, this module replays Word's tab layout to work out which
stop each field aligned to, leaving stops with no field as unfilled
columns. When the measured widths are insufficient to decide where a
field lands, the affected columns are marked as uncertain.
"""

import collections

from . import documents


# Word falls back to implicit tab stops every half inch (720 twips) when
# no custom tab stop can be reached.
DEFAULT_TAB_INTERVAL = 720


# A field of a line: the text between two tabs.
#
# width: measured total width of the text in twips, or None if unknown.
# decimal_anchor: measured width in twips of the text up to and including
# the decimal separator, used to check the fit against decimal tab stops.
# None if unknown.
Field = collections.namedtuple("Field", ["width", "decimal_anchor"], defaults=[None, None])


def field(width, decimal_anchor=None):
    return Field(width=width, decimal_anchor=decimal_anchor)


# The placement of one field on the line.
#
# field_index: index of the field in the line.
# stop: the tab stop the field aligned to, or None for the first field of
# the line and for fields that fell back to Word's default tab interval.
# start / end: position of the field in twips. end is None when the field
# width is unknown.
# uncertain: True when the width data was insufficient to determine this
# placement for sure.
Placement = collections.namedtuple("Placement", ["field_index", "stop", "start", "end", "uncertain"])


# A tab stop of the line together with the field aligned to it, if any.
Column = collections.namedtuple("Column", ["stop", "field_index", "uncertain"])


def effective_tab_stops(style_stops=(), paragraph_stops=(), cleared_positions=()):
    """Combine the tab stops declared by a paragraph style with the direct
    tab stops of a paragraph.

    A w:tab element with w:val="clear" in the paragraph cancels the stop
    inherited from the style at the same position, and a stop declared
    directly on the paragraph replaces an inherited stop at the same
    position.
    """
    cleared = set(cleared_positions)
    overridden = set(stop.position for stop in paragraph_stops)
    stops = [
        stop for stop in style_stops
        if stop.position not in cleared and stop.position not in overridden
    ]
    stops.extend(paragraph_stops)
    return sorted(stops, key=lambda stop: stop.position)


def layout_fields(tab_stops, fields, start=0, default_tab_interval=DEFAULT_TAB_INTERVAL):
    """Replay Word's tab layout for one line.

    tab_stops: the effective tab stops of the paragraph.
    fields: a Field for each text segment between tabs, in order.
    start: position of the start of the line in twips (e.g. the paragraph
    indent).
    """
    stops = sorted(tab_stops, key=lambda stop: stop.position)
    placements = []
    position = start
    uncertain = False
    for field_index, fld in enumerate(fields):
        if field_index == 0:
            stop = None
            field_start = position
        else:
            stop, field_start, fits = _place_after_tab(stops, position, fld, default_tab_interval)
            uncertain = uncertain or not fits

        if fld.width is None:
            # The width of this field is unknown, so neither its extent nor
            # anything after it can be determined for sure.
            uncertain = True
            field_end = None
            # Best-effort guess for the following fields: treat the field
            # as zero width.
            position = field_start
        else:
            field_end = field_start + fld.width
            position = field_end

        placements.append(Placement(field_index, stop, field_start, field_end, uncertain))

    return placements


def _place_after_tab(stops, position, fld, default_tab_interval):
    """Find the stop that the tab before fld actually reaches.

    Returns (stop, field_start, fits). stop is None when no custom stop
    can be reached and the tab falls back to Word's default tab interval.
    fits is False when the width data is insufficient to verify the
    placement.
    """
    for stop in stops:
        field_start = _aligned_start(stop, fld)
        if field_start is None:
            # The measurements needed for this alignment are missing, so
            # whether the field fits before this stop cannot be checked.
            if stop.position > position - _EPSILON:
                return stop, stop.position, False
        elif field_start >= position - _EPSILON:
            return stop, field_start, True

    intervals = int(position // default_tab_interval) + 1
    return None, intervals * default_tab_interval, True


def _aligned_start(stop, fld):
    """Start position of fld when aligned to stop, or None when the
    measurements needed for this alignment are missing."""
    if stop.alignment == "left":
        return stop.position
    if stop.alignment == "right":
        return None if fld.width is None else stop.position - fld.width
    if stop.alignment == "center":
        return None if fld.width is None else stop.position - fld.width / 2
    if stop.alignment == "decimal":
        return None if fld.decimal_anchor is None else stop.position - fld.decimal_anchor
    raise ValueError("unknown tab stop alignment: {0}".format(stop.alignment))


_EPSILON = 1e-6


def columns(tab_stops, placements):
    """Map every tab stop to the field aligned to it.

    Stops that no field reached are left as unfilled columns
    (field_index None).
    """
    placements_by_position = {}
    for placement in placements:
        if placement.stop is not None:
            placements_by_position.setdefault(placement.stop.position, placement)

    result = []
    for stop in sorted(tab_stops, key=lambda stop: stop.position):
        placement = placements_by_position.get(stop.position)
        if placement is None:
            result.append(Column(stop=stop, field_index=None, uncertain=False))
        else:
            result.append(Column(stop=stop, field_index=placement.field_index, uncertain=placement.uncertain))
    return result


def reconstruct_columns(tab_stops, fields, start=0, default_tab_interval=DEFAULT_TAB_INTERVAL):
    """Lay out fields and map every tab stop to its field in one step."""
    placements = layout_fields(
        tab_stops,
        fields,
        start=start,
        default_tab_interval=default_tab_interval,
    )
    return columns(tab_stops, placements)


def paragraph_fields(paragraph):
    """Split a documents.Paragraph into the text segments between tabs."""
    segments = [[]]

    def add(node):
        if isinstance(node, documents.Tab):
            segments.append([])
        elif isinstance(node, documents.Text):
            segments[-1].append(node.value)
        elif isinstance(node, documents.HasChildren):
            for child in node.children:
                add(child)

    for child in paragraph.children:
        add(child)

    return ["".join(segment) for segment in segments]
