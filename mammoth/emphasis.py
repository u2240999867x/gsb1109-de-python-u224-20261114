import collections

import cobble

from . import documents


# Character properties that can be emphasised. Each layer stores a tri-state
# switch: None means the layer does not mention the property, while True and
# False turn the inherited value on or off respectively.
PROPERTY_NAMES = (
    "bold",
    "italic",
    "underline",
    "strikethrough",
    "all_caps",
    "small_caps",
)


EffectiveProperties = collections.namedtuple("EffectiveProperties", PROPERTY_NAMES)
EffectiveProperties.__new__.__defaults__ = (False,) * len(PROPERTY_NAMES)


@cobble.data
class EmphasisSpan(object):
    paragraph_index = cobble.field()
    start = cobble.field()
    end = cobble.field()
    text = cobble.field()
    properties = cobble.field()
    sources = cobble.field()

    @property
    def is_emphasised(self):
        return any(self.properties)


@cobble.data
class EmphasisResult(object):
    spans = cobble.field()

    @property
    def emphasis_spans(self):
        return [span for span in self.spans if span.is_emphasised]

    def segment_at(self, paragraph_index, offset):
        for span in self.spans:
            if span.paragraph_index != paragraph_index:
                continue
            if span.start <= offset < span.end:
                return span
        return None


def extract_emphasis(element, styles):
    """Return the character segments of every paragraph in the element.

    Document defaults, paragraph and character style chains, paragraph mark
    properties and direct run properties are combined using Word's toggle
    rules: each layer only overrides the properties it explicitly sets, so an
    explicit off suppresses an inherited on. Adjacent text split across runs
    or hyperlinks is merged into a single segment only when its effective
    properties match.

    Every merged segment is retained, including segments that resolve to plain
    text, so the recorded sources still explain how cancelling flips were
    resolved. Use ``EmphasisResult.emphasis_spans`` to keep only the segments
    that end up emphasised.

    ``start`` and ``end`` are half-open character offsets into a paragraph's
    concatenated text. Non-text content such as tabs and images does not
    consume offsets but does break adjacency, so segments never span it.
    """
    spans = []
    for paragraph_index, paragraph in enumerate(_find_paragraphs(element)):
        spans.extend(
            extract_segments_from_paragraph(paragraph, styles, paragraph_index),
        )
    return EmphasisResult(spans=spans)


def extract_segments_from_paragraph(paragraph, styles, paragraph_index=0):
    spans = []
    offset = 0
    current = None

    for token in _collect_tokens(paragraph.children):
        if token[0] == "gap":
            current = None
            continue

        _, value, run = token
        if not value:
            continue

        properties, sources = effective_character_properties(paragraph, run, styles)
        start = offset
        end = offset + len(value)
        offset = end

        if current is not None and current.properties == properties:
            current = cobble.copy(current,
                end=end,
                text=current.text + value,
                sources=_merge_sources(current.sources, sources),
            )
            spans[-1] = current
        else:
            current = EmphasisSpan(
                paragraph_index=paragraph_index,
                start=start,
                end=end,
                text=value,
                properties=properties,
                sources=sources,
            )
            spans.append(current)

    return spans


def effective_character_properties(paragraph, run, styles):
    """Resolve the toggles for a run and record where each decision came from.

    The returned sources map lists, for every property, the style layers and
    direct formatting that flipped it, in application order. This keeps the
    judgment basis available even when two flips cancel out to plain text.
    """
    properties = dict((name, False) for name in PROPERTY_NAMES)
    sources = dict((name, []) for name in PROPERTY_NAMES)

    def apply_layer(layer, label):
        for name in PROPERTY_NAMES:
            value = layer.get(name)
            if value is None:
                continue
            properties[name] = value
            sources[name].append(label)

    apply_layer(_find_default_character_properties(styles), "document-default")

    if paragraph is not None and paragraph.style_id is not None:
        for style in _style_chain(
            styles.find_paragraph_style_by_id,
            paragraph.style_id,
        ):
            apply_layer(
                style.character_properties,
                "paragraph-style:" + style.style_id,
            )

    if paragraph is not None:
        apply_layer(paragraph.character_properties or {}, "paragraph")

    if run is not None and run.style_id is not None:
        for style in _style_chain(
            styles.find_character_style_by_id,
            run.style_id,
        ):
            apply_layer(
                style.character_properties,
                "character-style:" + style.style_id,
            )

    if run is not None:
        apply_layer(run.character_properties or {}, "run")

    return EffectiveProperties(**properties), sources


def _style_chain(find_style_by_id, style_id):
    chain = []
    seen = set()
    current_id = style_id

    while current_id is not None and current_id not in seen:
        seen.add(current_id)
        style = find_style_by_id(current_id)
        if style is None:
            break
        chain.append(style)
        current_id = style.based_on

    chain.reverse()
    return chain


def _find_default_character_properties(styles):
    find_default = getattr(styles, "find_default_character_properties", None)
    if find_default is None:
        return {}
    return find_default() or {}


def _find_paragraphs(element):
    paragraphs = []

    def visit(node):
        if isinstance(node, documents.Paragraph):
            paragraphs.append(node)
        for child in getattr(node, "children", []):
            visit(child)

    visit(element)
    return paragraphs


def _collect_tokens(elements, run=None):
    for element in elements:
        if isinstance(element, documents.Text):
            yield ("text", element.value, run)
        elif isinstance(element, documents.Run):
            for token in _collect_tokens(element.children, run=element):
                yield token
        elif isinstance(element, documents.Hyperlink):
            # Hyperlinks do not interrupt adjacency by themselves: text on
            # either side may still merge when its effective properties match.
            for token in _collect_tokens(element.children, run=run):
                yield token
        elif isinstance(element, documents.Bookmark):
            # Bookmarks carry no text and do not break character adjacency.
            continue
        else:
            # Tabs, breaks, images and other non-text content split the run of
            # adjacent characters.
            yield ("gap",)


def _merge_sources(left, right):
    merged = {}
    for name in PROPERTY_NAMES:
        labels = list(left.get(name, []))
        for label in right.get(name, []):
            if label not in labels:
                labels.append(label)
        merged[name] = labels
    return merged
