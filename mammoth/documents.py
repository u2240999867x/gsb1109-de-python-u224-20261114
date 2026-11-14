import cobble


class Element(object):
    def copy(self, **kwargs):
        return cobble.copy(self, **kwargs)


class HasChildren(Element):
    children = cobble.field()


@cobble.data
class Document(HasChildren):
    notes = cobble.field()
    comments = cobble.field()
    sections = cobble.field()

@cobble.data
class Paragraph(HasChildren):
    style_id = cobble.field()
    style_name = cobble.field()
    numbering = cobble.field()
    alignment = cobble.field()
    indent = cobble.field()


@cobble.data
class ParagraphIndent(object):
    start = cobble.field()
    end = cobble.field()
    first_line = cobble.field()
    hanging = cobble.field()


@cobble.data
class Indent(object):
    left = cobble.field()
    right = cobble.field()
    first_line = cobble.field()
    hanging = cobble.field()


@cobble.data
class Run(HasChildren):
    style_id = cobble.field()
    style_name = cobble.field()
    is_bold = cobble.field()
    is_italic = cobble.field()
    is_underline = cobble.field()
    is_strikethrough = cobble.field()
    is_all_caps = cobble.field()
    is_small_caps = cobble.field()
    vertical_alignment = cobble.field()
    font = cobble.field()
    font_size = cobble.field()
    highlight = cobble.field()

@cobble.data
class Text(Element):
    value = cobble.field()

@cobble.data
class Hyperlink(HasChildren):
    href = cobble.field()
    anchor = cobble.field()
    target_frame = cobble.field()

@cobble.data
class Checkbox(Element):
    checked = cobble.field()

checkbox = Checkbox

@cobble.data
class Table(HasChildren):
    style_id = cobble.field()
    style_name = cobble.field()

@cobble.data
class TableRow(HasChildren):
    is_header = cobble.field()

@cobble.data
class TableCell(HasChildren):
    colspan = cobble.field()
    rowspan = cobble.field()

@cobble.data
class TableCellUnmerged:
    children = cobble.field()
    colspan = cobble.field()
    rowspan = cobble.field()
    vmerge = cobble.field()

    def _accept1(self, visitor, arg0):
        return visitor.visit_table_cell(self, arg0)

    def copy(self, **kwargs):
        return cobble.copy(self, **kwargs)

@cobble.data
class Break(Element):
    break_type = cobble.field()

line_break = Break("line")
page_break = Break("page")
column_break = Break("column")


@cobble.data
class Tab(Element):
    pass


@cobble.data
class Image(Element):
    alt_text = cobble.field()
    content_type = cobble.field()
    open = cobble.field()


def document(children, notes=None, comments=None, sections=None):
    if notes is None:
        notes = Notes({})
    if comments is None:
        comments = []
    if sections is None:
        sections = header_index([])
    return Document(children, notes, comments=comments, sections=sections)

def paragraph(children, style_id=None, style_name=None, numbering=None, alignment=None, indent=None):
    if indent is None:
        indent = paragraph_indent()

    return Paragraph(children, style_id, style_name, numbering, alignment=alignment, indent=indent)

def paragraph_indent(start=None, end=None, first_line=None, hanging=None):
    return ParagraphIndent(start=start, end=end, first_line=first_line, hanging=hanging)

def run(
    children,
    style_id=None,
    style_name=None,
    is_bold=None,
    is_italic=None,
    is_underline=None,
    is_strikethrough=None,
    is_all_caps=None,
    is_small_caps=None,
    vertical_alignment=None,
    font=None,
    font_size=None,
    highlight=None,
):
    if vertical_alignment is None:
        vertical_alignment = VerticalAlignment.baseline
    return Run(
        children=children,
        style_id=style_id,
        style_name=style_name,
        is_bold=bool(is_bold),
        is_italic=bool(is_italic),
        is_underline=bool(is_underline),
        is_strikethrough=bool(is_strikethrough),
        is_all_caps=bool(is_all_caps),
        is_small_caps=bool(is_small_caps),
        vertical_alignment=vertical_alignment,
        font=font,
        font_size=font_size,
        highlight=highlight,
    )

class VerticalAlignment(object):
    baseline = "baseline"
    superscript = "superscript"
    subscript = "subscript"

text = Text

_tab = Tab()

def tab():
    return _tab


image = Image

def hyperlink(children, href=None, anchor=None, target_frame=None):
    return Hyperlink(href=href, anchor=anchor, target_frame=target_frame, children=children)


@cobble.data
class Bookmark(Element):
    name = cobble.field()

bookmark = Bookmark


def table(children, style_id=None, style_name=None):
    return Table(children=children, style_id=style_id, style_name=style_name)

def table_row(children, is_header=None):
    return TableRow(children=children, is_header=bool(is_header))

def table_cell(children, colspan=None, rowspan=None):
    if colspan is None:
        colspan = 1
    if rowspan is None:
        rowspan = 1
    return TableCell(children=children, colspan=colspan, rowspan=rowspan)

def table_cell_unmerged(children, colspan, rowspan, vmerge):
    return TableCellUnmerged(children=children, colspan=colspan, rowspan=rowspan, vmerge=vmerge)

def numbering_level(level_index, is_ordered):
    return _NumberingLevel(str(level_index), bool(is_ordered))

@cobble.data
class _NumberingLevel(object):
    level_index = cobble.field()
    is_ordered = cobble.field()

@cobble.data
class Note(Element):
    note_type = cobble.field()
    note_id = cobble.field()
    body = cobble.field()


note = Note


class Notes(object):
    def __init__(self, notes):
        self._notes = notes

    def find_note(self, note_type, note_id):
        return self._notes[(note_type, note_id)]

    def resolve(self, reference):
        return self.find_note(reference.note_type, reference.note_id)

    def __eq__(self, other):
        return isinstance(other, Notes) and self._notes == other._notes

    def __ne__(self, other):
        return not (self == other)

def notes(notes_list):
    return Notes(dict(
        (_note_key(note), note)
        for note in notes_list
    ))

def _note_key(note):
    return (note.note_type, note.note_id)

@cobble.data
class NoteReference(Element):
    note_type = cobble.field()
    note_id = cobble.field()

note_reference = NoteReference


@cobble.data
class Comment(object):
    comment_id = cobble.field()
    body = cobble.field()
    author_name = cobble.field()
    author_initials = cobble.field()

def comment(comment_id, body, author_name=None, author_initials=None):
    return Comment(
        comment_id=comment_id,
        body=body,
        author_name=author_name,
        author_initials=author_initials,
    )

@cobble.data
class CommentReference(Element):
    comment_id = cobble.field()

comment_reference = CommentReference

def element_visitor(args):
    return cobble.visitor(Element, args=args)


# --- Section header index -------------------------------------------------
#
# The header index only records the headers declared by each section's
# w:sectPr; it does not paginate the document. Every resolved header slot can
# be traced back to the declaration that supplies it, or to a part that was
# referenced but is missing from the package.

HEADER_SLOT_FIRST = "first"
HEADER_SLOT_ODD = "odd"
HEADER_SLOT_EVEN = "even"
HEADER_SLOTS = (HEADER_SLOT_FIRST, HEADER_SLOT_ODD, HEADER_SLOT_EVEN)

# A section explicitly references a header part.
HEADER_ORIGIN_EXPLICIT = "explicit"
# A section explicitly references a header part that contains no content.
HEADER_ORIGIN_EMPTY = "empty"
# A section has no declaration of its own and inherits an earlier section.
HEADER_ORIGIN_INHERITED = "inherited"
# A headerReference exists but the referenced part cannot be found.
HEADER_ORIGIN_MISSING = "missing"
# The slot is switched off (even/odd or first-page toggle), so no header applies.
HEADER_ORIGIN_DISABLED = "disabled"
# The slot is active but neither this section nor any previous section declares it.
HEADER_ORIGIN_NONE = "none"


@cobble.data
class HeaderPart(object):
    part_name = cobble.field()
    is_empty = cobble.field()
    text = cobble.field()


@cobble.data
class HeaderSource(object):
    section_index = cobble.field()
    slot = cobble.field()
    enabled = cobble.field()
    origin = cobble.field()
    source_section_index = cobble.field()
    relationship_id = cobble.field()
    part_name = cobble.field()
    text = cobble.field()
    detail = cobble.field()


@cobble.data
class SectionHeaders(object):
    section_index = cobble.field()
    title_page = cobble.field()
    first = cobble.field()
    odd = cobble.field()
    even = cobble.field()

    def find_slot(self, slot):
        return getattr(self, slot)


@cobble.data
class HeaderIndex(object):
    even_and_odd_headers = cobble.field()
    sections = cobble.field()

    def find_section(self, section_index):
        return self.sections[section_index]


def header_part(part_name, is_empty, text):
    return HeaderPart(part_name=part_name, is_empty=bool(is_empty), text=text)


def header_source(
        section_index,
        slot,
        enabled,
        origin,
        detail,
        source_section_index=None,
        relationship_id=None,
        part_name=None,
        text=None):
    if text is None:
        text = ""
    return HeaderSource(
        section_index=section_index,
        slot=slot,
        enabled=bool(enabled),
        origin=origin,
        source_section_index=source_section_index,
        relationship_id=relationship_id,
        part_name=part_name,
        text=text,
        detail=detail,
    )


def section_headers(section_index, title_page, first, odd, even):
    return SectionHeaders(
        section_index=section_index,
        title_page=bool(title_page),
        first=first,
        odd=odd,
        even=even,
    )


def header_index(sections, even_and_odd_headers=False):
    return HeaderIndex(
        even_and_odd_headers=bool(even_and_odd_headers),
        sections=sections,
    )
