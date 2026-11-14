import collections


class Styles(object):
    @staticmethod
    def create(paragraph_styles=None, character_styles=None, table_styles=None, numbering_styles=None):
        if paragraph_styles is None:
            paragraph_styles = {}
        if character_styles is None:
            character_styles = {}
        if table_styles is None:
            table_styles = {}
        if numbering_styles is None:
            numbering_styles = {}

        return Styles(
            paragraph_styles=paragraph_styles,
            character_styles=character_styles,
            table_styles=table_styles,
            numbering_styles=numbering_styles,
        )

    def __init__(self, paragraph_styles, character_styles, table_styles, numbering_styles):
        self._paragraph_styles = paragraph_styles
        self._character_styles = character_styles
        self._table_styles = table_styles
        self._numbering_styles = numbering_styles

    def find_paragraph_style_by_id(self, style_id):
        return self._paragraph_styles.get(style_id)

    def find_character_style_by_id(self, style_id):
        return self._character_styles.get(style_id)

    def find_table_style_by_id(self, style_id):
        return self._table_styles.get(style_id)

    def find_numbering_style_by_id(self, style_id):
        return self._numbering_styles.get(style_id)

    def find_paragraph_outline_level(self, style_id):
        return resolve_paragraph_outline_level(style_id, self.find_paragraph_style_by_id)


Styles.EMPTY = Styles(
    paragraph_styles={},
    character_styles={},
    table_styles={},
    numbering_styles={},
)


def read_styles_xml_element(element):
    paragraph_styles = {}
    character_styles = {}
    table_styles = {}
    numbering_styles = {}
    styles = {
        "paragraph": paragraph_styles,
        "character": character_styles,
        "table": table_styles,
        "numbering": numbering_styles,
    }

    for style_element in element.find_children("w:style"):
        element_type = style_element.attributes["w:type"]
        if element_type == "numbering":
            style = _read_numbering_style_element(style_element)
        else:
            style = _read_style_element(style_element)

        style_set = styles.get(element_type)

        # Per 17.7.4.17 style (Style Definition) of ECMA-376 4th edition Part 1:
        #
        # > If multiple style definitions each declare the same value for their
        # > styleId, then the first such instance shall keep its current
        # > identifier with all other instances being reassigned in any manner
        # > desired.
        #
        # For the purpose of conversion, there's no point holding onto styles
        # with reassigned style IDs, so we ignore such style definitions.

        if style_set is not None and style.style_id not in style_set:
            style_set[style.style_id] = style

    return Styles(
        paragraph_styles=paragraph_styles,
        character_styles=character_styles,
        table_styles=table_styles,
        numbering_styles=numbering_styles,
    )


Style = collections.namedtuple(
    "Style",
    ["style_id", "name", "based_on", "outline_level"],
    defaults=(None, None),
)


OutlineLevel = collections.namedtuple("OutlineLevel", ["level", "cycle"])


def resolve_paragraph_outline_level(style_id, find_paragraph_style_by_id):
    if style_id is None:
        return OutlineLevel(level=None, cycle=())

    visited = []
    current_style_id = style_id
    while current_style_id is not None:
        if current_style_id in visited:
            cycle = tuple(visited[visited.index(current_style_id):] + [current_style_id])
            return OutlineLevel(level=None, cycle=cycle)

        visited.append(current_style_id)
        style = find_paragraph_style_by_id(current_style_id)
        if style is None:
            return OutlineLevel(level=None, cycle=())

        outline_level = getattr(style, "outline_level", None)
        if outline_level is not None:
            # 17.3.1.20 outlineLvl: 0 to 8 are outline levels, and 9 means
            # that the paragraph is body text.
            if outline_level == 9:
                return OutlineLevel(level=None, cycle=())
            elif 0 <= outline_level <= 8:
                return OutlineLevel(level=outline_level + 1, cycle=())
            else:
                return OutlineLevel(level=None, cycle=())

        current_style_id = getattr(style, "based_on", None)

    return OutlineLevel(level=None, cycle=())


def _read_style_element(element):
    style_id = _read_style_id(element)
    name = element.find_child_or_null("w:name").attributes.get("w:val")
    based_on = element.find_child_or_null("w:basedOn").attributes.get("w:val")
    outline_level = _read_int(
        element
        .find_child_or_null("w:pPr")
        .find_child_or_null("w:outlineLvl")
        .attributes.get("w:val")
    )
    return Style(
        style_id=style_id,
        name=name,
        based_on=based_on,
        outline_level=outline_level,
    )


def _read_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


NumberingStyle = collections.namedtuple("NumberingStyle", ["style_id", "num_id"])


def _read_numbering_style_element(element):
    style_id = _read_style_id(element)

    num_id = element \
        .find_child_or_null("w:pPr") \
        .find_child_or_null("w:numPr") \
        .find_child_or_null("w:numId") \
        .attributes.get("w:val")

    return NumberingStyle(style_id=style_id, num_id=num_id)


def _read_style_id(element):
    return element.attributes["w:styleId"]
