from . import documents


# The historical raw text format puts two newlines after every paragraph.
# Comment ranges index into text where each paragraph separator is exactly
# one code point, matching the user-facing annotated plain text format.
_LEGACY_PARAGRAPH_SEPARATOR = "\n\n"
_PARAGRAPH_SEPARATOR = "\n"


def extract_raw_text_from_element(element):
    data = _extract(element, _LEGACY_PARAGRAPH_SEPARATOR)
    return RawText(
        data.text,
        _annotated_comments(element, data.ranges),
        data.ranges,
    )


def extract_raw_text_with_comments_from_element(element, paragraph_separator=None):
    return _raw_text_data(
        element,
        _PARAGRAPH_SEPARATOR if paragraph_separator is None else paragraph_separator,
    )


extract_raw_text_with_comment_ranges_from_element = extract_raw_text_with_comments_from_element
extract_raw_text_with_annotations_from_element = extract_raw_text_with_comments_from_element
extract_raw_text_with_ranges_from_element = extract_raw_text_with_comments_from_element
extract_annotated_raw_text_from_element = extract_raw_text_with_comments_from_element
extract_plain_text_with_comments_from_element = extract_raw_text_with_comments_from_element


def extract_raw_text_and_comments_from_element(element, paragraph_separator=None):
    data = extract_raw_text_with_comments_from_element(element, paragraph_separator)
    return data.text, data.comments


def extract_raw_text_and_comment_ranges_from_element(element, paragraph_separator=None):
    data = extract_raw_text_with_comments_from_element(element, paragraph_separator)
    return data.text, data.comment_ranges


def extract_raw_text_and_annotations_from_element(element, paragraph_separator=None):
    data = extract_raw_text_with_comments_from_element(element, paragraph_separator)
    return data.text, data.annotations


def extract_comments_from_element(element, paragraph_separator=None):
    data = _extract(
        element,
        _PARAGRAPH_SEPARATOR if paragraph_separator is None else paragraph_separator,
    )
    return _annotated_comments(element, data.ranges)


extract_annotations_from_element = extract_comments_from_element


def extract_comment_ranges_from_element(element, paragraph_separator=None):
    data = _extract(
        element,
        _PARAGRAPH_SEPARATOR if paragraph_separator is None else paragraph_separator,
    )
    return data.ranges


extract_ranges_from_element = extract_comment_ranges_from_element


def annotate_comments(element, comments, paragraph_separator=None):
    ranges = dict(
        (range_.comment_id, range_)
        for range_ in extract_comment_ranges_from_element(element, paragraph_separator)
    )
    return [
        _comment_with_range(comment, ranges.get(comment.comment_id))
        for comment in comments
    ]


def annotate_document_comments(document):
    if not document.comments:
        return document
    return document.copy(comments=annotate_comments(document, document.comments))


class RawText(str):
    """Plain text together with the comment ranges that index into it.

    Subclassing ``str`` keeps the existing raw text API intact: the value
    compares equal to the plain text and ``value[start:end]`` selects exactly
    the anchored source text. ``comment_ranges`` holds one half-open range per
    anchored comment, and ``comments`` holds the comments with ``start`` and
    ``end`` attributes populated.
    """

    def __new__(cls, value, comments=(), comment_ranges=()):
        text = str.__new__(cls, value)
        text.text = str(value)
        text.comments = list(comments)
        text.annotations = text.comments
        text.comment_ranges = documents.comment_ranges(comment_ranges)
        text.ranges = text.comment_ranges
        return text

    def __init__(self, value, comments=(), comment_ranges=()):
        pass

    def slice(self, comment):
        if isinstance(comment, str):
            comment = self.comment_for(comment)
        comment_range = getattr(comment, "comment_range", None) or comment
        if comment_range is None or comment_range.start is None:
            return None
        return self.text[comment_range.start:comment_range.end]

    selected_text = slice

    def comment_for(self, comment_id):
        for comment in self.comments:
            if comment.comment_id == comment_id:
                return comment

    @property
    def comments_by_id(self):
        return dict((comment.comment_id, comment) for comment in self.comments)

    @property
    def ranges_by_id(self):
        return dict((range_.comment_id, range_) for range_ in self.comment_ranges)

    @property
    def annotations_by_id(self):
        return self.ranges_by_id


class _RawTextData(object):
    def __init__(self, text, ranges):
        self.text = text
        self.ranges = ranges


def _raw_text_data(element, paragraph_separator):
    data = _extract(element, paragraph_separator)
    return RawText(
        data.text,
        _annotated_comments(element, data.ranges),
        data.ranges,
    )


def _extract(element, paragraph_separator):
    chunks = []
    position = [0]
    starts = {}
    ends = {}
    references = {}

    def visit(value):
        if isinstance(value, documents.Text):
            chunks.append(value.value)
            position[0] += len(value.value)
        elif isinstance(value, documents.Tab):
            chunks.append("\t")
            position[0] += 1
        elif isinstance(value, documents.CommentRangeStart):
            starts.setdefault(value.comment_id, position[0])
        elif isinstance(value, documents.CommentRangeEnd):
            ends.setdefault(value.comment_id, position[0])
        elif isinstance(value, documents.CommentReference):
            references.setdefault(value.comment_id, position[0])
        elif isinstance(value, documents.Paragraph):
            for child in value.children:
                visit(child)
            chunks.append(paragraph_separator)
            position[0] += len(paragraph_separator)
        else:
            for child in _children(value):
                visit(child)

    visit(element)
    return _RawTextData(
        "".join(chunks),
        _ranges_from_positions(starts, ends, references),
    )


def _ranges_from_positions(starts, ends, references):
    comment_ids = list(starts)
    comment_ids += [
        comment_id
        for comment_id in ends
        if comment_id not in starts
    ]
    comment_ids += [
        comment_id
        for comment_id in references
        if comment_id not in starts and comment_id not in ends
    ]

    ranges = []
    for comment_id in comment_ids:
        start = starts.get(comment_id)
        end = ends.get(comment_id)
        reference = references.get(comment_id)

        if start is None and end is None:
            start = end = reference
        elif start is None:
            start = end
        elif end is None:
            end = reference if reference is not None and reference >= start else start

        if start is None or end is None:
            continue
        if end < start:
            start, end = end, start

        ranges.append(documents.comment_range(comment_id, start, end))

    return documents.comment_ranges(ranges)


def _children(element):
    children = getattr(element, "children", None)
    if children is None:
        return []
    return children


def _comments_of(element):
    return list(getattr(element, "comments", None) or [])


def _annotated_comments(element, ranges):
    ranges_by_id = dict(
        (range_.comment_id, range_)
        for range_ in ranges
    )
    return [
        _comment_with_range(comment, ranges_by_id.get(comment.comment_id))
        for comment in _comments_of(element)
    ]


def _comment_with_range(comment, comment_range):
    if comment_range is None:
        return comment
    else:
        return comment.copy(
            start=comment_range.start,
            end=comment_range.end,
        )
