# coding=utf-8

from __future__ import unicode_literals

import cobble

from . import documents, results


@cobble.data
class Section(object):
    title = cobble.field()
    level = cobble.field()
    paragraph_index = cobble.field()
    paragraph = cobble.field()
    children = cobble.field()
    style_id = cobble.field(default=None)
    style_name = cobble.field(default=None)
    is_outline_level_explicit = cobble.field(default=None)

    @property
    def text(self):
        return self.title

    @property
    def paragraph_number(self):
        return self.paragraph_index + 1

    @property
    def position(self):
        return self.paragraph_number

    @property
    def index(self):
        return self.paragraph_index

    @property
    def heading_level(self):
        return self.level

    @property
    def sections(self):
        return self.children

    @property
    def subsections(self):
        return self.children

    def to_dict(self):
        return {
            "title": self.title,
            "level": self.level,
            "paragraph_index": self.paragraph_index,
            "paragraph_number": self.paragraph_number,
            "children": [child.to_dict() for child in self.children],
        }

    as_dict = to_dict

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return getattr(self, key)
            except AttributeError:
                raise KeyError(key)
        return self.children[key]

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __iter__(self):
        return iter(self.children)


@cobble.data
class UnresolvedParagraph(object):
    paragraph_index = cobble.field()
    paragraph = cobble.field()
    style_id = cobble.field()
    style_name = cobble.field()
    style_cycle = cobble.field()

    @property
    def title(self):
        return _paragraph_text(self.paragraph)

    text = title

    @property
    def paragraph_number(self):
        return self.paragraph_index + 1

    @property
    def position(self):
        return self.paragraph_number

    @property
    def index(self):
        return self.paragraph_index

    @property
    def reason(self):
        return "paragraph styles inherit from each other: {0}".format(
            " -> ".join(self.style_cycle)
        )

    def to_dict(self):
        return {
            "title": self.title,
            "paragraph_index": self.paragraph_index,
            "paragraph_number": self.paragraph_number,
            "style_id": self.style_id,
            "style_name": self.style_name,
            "style_cycle": list(self.style_cycle),
            "reason": self.reason,
        }

    as_dict = to_dict

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return getattr(self, key)
            except AttributeError:
                raise KeyError(key)
        raise TypeError("UnresolvedParagraph is not a sequence")

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


class SectionTree(list):
    """The root sections of a document, in document order.

    The tree is a list of root sections, so it can be iterated and indexed
    directly. Paragraphs whose style inheritance has no reliable outline level
    are available from ``unresolved`` and are also reported as warnings.
    """

    def __init__(self, roots, unresolved):
        super(SectionTree, self).__init__(roots)
        self.unresolved = unresolved

    @property
    def roots(self):
        return self

    @property
    def chapters(self):
        return self

    def to_dict(self):
        return [section.to_dict() for section in self]

    as_dict = to_dict


def extract_section_tree(fileobj, **kwargs):
    from . import docx

    if isinstance(fileobj, documents.Document):
        return extract_section_tree_from_document(fileobj)

    return docx.read(fileobj, **kwargs).bind(extract_section_tree_from_document)


extract_outline = extract_section_tree


def extract_section_tree_from_document(document):
    roots = []
    unresolved = []
    warnings = []
    stack = []

    for paragraph_index, paragraph in enumerate(_iter_paragraphs(document)):
        level = paragraph.outline_level
        if level is None:
            cycle = getattr(paragraph, "outline_style_cycle", ()) or ()
            if cycle:
                unresolved_paragraph = UnresolvedParagraph(
                    paragraph_index=paragraph_index,
                    paragraph=paragraph,
                    style_id=paragraph.style_id,
                    style_name=paragraph.style_name,
                    style_cycle=tuple(cycle),
                )
                unresolved.append(unresolved_paragraph)
                warnings.append(results.warning(_unresolved_message(unresolved_paragraph)))
            continue

        while stack and stack[-1].level >= level:
            stack.pop()

        section = Section(
            title=_paragraph_text(paragraph),
            level=level,
            paragraph_index=paragraph_index,
            paragraph=paragraph,
            children=[],
            style_id=paragraph.style_id,
            style_name=paragraph.style_name,
            is_outline_level_explicit=bool(
                getattr(paragraph, "is_outline_level_explicit", False)
            ),
        )

        if stack:
            stack[-1].children.append(section)
        else:
            roots.append(section)

        stack.append(section)

    return results.Result(
        SectionTree(roots, unresolved),
        warnings,
    )


extract_outline_from_document = extract_section_tree_from_document


def _iter_paragraphs(element):
    for child in getattr(element, "children", []):
        if isinstance(child, documents.Paragraph):
            yield child
        elif isinstance(child, documents.HasChildren):
            for paragraph in _iter_paragraphs(child):
                yield paragraph


def _paragraph_text(paragraph):
    text = "".join(_extract_text(child) for child in paragraph.children)
    return text.strip()


def _extract_text(element):
    if isinstance(element, documents.Text):
        return element.value
    elif isinstance(element, documents.Tab):
        return "\t"
    elif isinstance(element, documents.Break):
        return " "
    else:
        return "".join(
            _extract_text(child)
            for child in getattr(element, "children", [])
        )


def _unresolved_message(unresolved_paragraph):
    title = unresolved_paragraph.title
    if title:
        description = 'Paragraph {0} ("{1}")'.format(
            unresolved_paragraph.paragraph_number,
            title,
        )
    else:
        description = "Paragraph {0}".format(unresolved_paragraph.paragraph_number)

    return "{0} has no reliable outline level: {1}".format(
        description,
        unresolved_paragraph.reason,
    )
