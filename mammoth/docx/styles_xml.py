import collections


class Styles(object):
    @staticmethod
    def create(paragraph_styles=None, character_styles=None, table_styles=None, numbering_styles=None, default_character_properties=None):
        if paragraph_styles is None:
            paragraph_styles = {}
        if character_styles is None:
            character_styles = {}
        if table_styles is None:
            table_styles = {}
        if numbering_styles is None:
            numbering_styles = {}
        if default_character_properties is None:
            default_character_properties = {}

        return Styles(
            paragraph_styles=paragraph_styles,
            character_styles=character_styles,
            table_styles=table_styles,
            numbering_styles=numbering_styles,
            default_character_properties=default_character_properties,
        )

    def __init__(self, paragraph_styles, character_styles, table_styles, numbering_styles, default_character_properties=None):
        self._paragraph_styles = paragraph_styles
        self._character_styles = character_styles
        self._table_styles = table_styles
        self._numbering_styles = numbering_styles
        self._default_character_properties = default_character_properties or {}

    def find_paragraph_style_by_id(self, style_id):
        return self._paragraph_styles.get(style_id)

    def find_character_style_by_id(self, style_id):
        return self._character_styles.get(style_id)

    def find_table_style_by_id(self, style_id):
        return self._table_styles.get(style_id)

    def find_numbering_style_by_id(self, style_id):
        return self._numbering_styles.get(style_id)

    def find_default_character_properties(self):
        return self._default_character_properties


Styles.EMPTY = Styles(
    paragraph_styles={},
    character_styles={},
    table_styles={},
    numbering_styles={},
    default_character_properties={},
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

    default_character_properties = _read_default_character_properties(element)

    return Styles(
        paragraph_styles=paragraph_styles,
        character_styles=character_styles,
        table_styles=table_styles,
        numbering_styles=numbering_styles,
        default_character_properties=default_character_properties,
    )


Style = collections.namedtuple("Style", ["style_id", "name", "based_on", "character_properties"])
Style.__new__.__defaults__ = (None, {})


def _read_style_element(element):
    style_id = _read_style_id(element)
    name = element.find_child_or_null("w:name").attributes.get("w:val")
    based_on = element \
        .find_child_or_null("w:basedOn") \
        .attributes.get("w:val")
    character_properties = _read_character_properties(
        element.find_child_or_null("w:rPr"),
    )
    return Style(
        style_id=style_id,
        name=name,
        based_on=based_on,
        character_properties=character_properties,
    )


NumberingStyle = collections.namedtuple("NumberingStyle", ["style_id", "num_id"])


def _read_numbering_style_element(element):
    style_id = _read_style_id(element)

    num_id = element \
        .find_child_or_null("w:pPr") \
        .find_child_or_null("w:numPr") \
        .find_child_or_null("w:numId") \
        .attributes.get("w:val")

    return NumberingStyle(style_id=style_id, num_id=num_id)


def _read_default_character_properties(element):
    doc_defaults = element.find_child("w:docDefaults")
    if doc_defaults is None:
        return {}

    run_properties_default = doc_defaults \
        .find_child_or_null("w:rPrDefault") \
        .find_child_or_null("w:rPr")

    return _read_character_properties(run_properties_default)


def _read_character_properties(properties):
    if properties is None:
        return {}

    return {
        name: value
        for name, value in [
            ("bold", _read_boolean_switch(properties.find_child("w:b"))),
            ("italic", _read_boolean_switch(properties.find_child("w:i"))),
            ("underline", _read_underline_switch(properties.find_child("w:u"))),
            ("strikethrough", _read_boolean_switch(properties.find_child("w:strike"))),
            ("all_caps", _read_boolean_switch(properties.find_child("w:caps"))),
            ("small_caps", _read_boolean_switch(properties.find_child("w:smallCaps"))),
        ]
        if value is not None
    }


def _read_boolean_switch(element):
    if element is None:
        return None
    else:
        return element.attributes.get("w:val") not in ["false", "0"]


def _read_underline_switch(element):
    if element is None:
        return None
    else:
        return element.attributes.get("w:val") not in [None, "false", "0", "none"]


def _read_style_id(element):
    return element.attributes["w:styleId"]
