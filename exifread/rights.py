"""
Consolidated licensing/rights table.

Merges the EXIF ``Copyright`` tag with the localized rights statements
found in XMP packets (``dc:rights`` and ``xmpRights:UsageTerms``) into a
single table that keeps, for every statement, the language, the source
(``exif`` or ``xmp``) and the original sentence.

Lookups by language return a unique statement: the exact localized entry
wins, then the region-less variant of the same language, then the
``x-default`` XMP entry and finally the EXIF copyright. When two XMP
statements disagree for the same language and no ordering information
allows telling which one is newer, the cell is marked as pending review
(:data:`PENDING_REVIEW`) while every other language stays usable.
"""

import re
from dataclasses import dataclass
from pyexpat import ExpatError
from typing import Any, Dict, List, Optional, Set, Union
from xml.dom.minidom import Node, parseString

from exifread.exif_log import get_logger

logger = get_logger()

SOURCE_EXIF = "exif"
SOURCE_XMP = "xmp"

#: Language tag used by XMP for the default, non-localized statement.
DEFAULT_LANGUAGE = "x-default"

#: Value shown in a cell when conflicting XMP statements cannot be resolved.
PENDING_REVIEW = "待核"

_DC_NS = "http://purl.org/dc/elements/1.1/"
_XMP_RIGHTS_NS = "http://ns.adobe.com/xap/1.0/rights/"
_RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_XML_NS = "http://www.w3.org/XML/1998/namespace"

_PACKET_RE = re.compile(rb"<x:xmpmeta\b.*?</x:xmpmeta>", re.DOTALL)


@dataclass
class RightsEntry:
    """A single rights statement with its language, source and original text."""

    language: str
    source: str
    text: str

    def as_dict(self) -> Dict[str, str]:
        """Return the entry as a plain, JSON-serializable dictionary."""
        return {"language": self.language, "source": self.source, "text": self.text}


class RightsTable:
    """
    Table of every rights statement found in EXIF and XMP metadata.

    ``entries`` holds the raw statements, including every conflicting
    sentence, so the original wording is never lost. ``disputed`` lists the
    language tags for which conflicting XMP statements were found.
    """

    def __init__(
        self,
        entries: Optional[List[RightsEntry]] = None,
        disputed: Optional[Set[str]] = None,
        exif_default: Optional[RightsEntry] = None,
    ) -> None:
        self.entries: List[RightsEntry] = list(entries or [])
        self.disputed: Set[str] = set(disputed or set())
        self.default_language: str = DEFAULT_LANGUAGE
        self._exif_default = exif_default
        # Locale tag (as written in the XMP) -> collected XMP entries.
        self._buckets: Dict[str, List[RightsEntry]] = {}
        for entry in self.entries:
            if entry.source == SOURCE_XMP:
                self._buckets.setdefault(entry.language, []).append(entry)
        # Lower-cased locale tag -> locale tag as written in the XMP.
        self._index: Dict[str, str] = {
            language.lower(): language for language in self._buckets
        }

    def lookup(self, language: Optional[str]) -> Optional[RightsEntry]:
        """
        Return the unique statement for ``language``.

        Resolution order:

        1. the exact localized entry (case-insensitive language tag);
        2. the region-less variant of the same language, e.g. ``en`` for
           ``en-GB``;
        3. the ``x-default`` XMP entry;
        4. the EXIF copyright statement.

        A disputed cell is returned as a single entry whose text is
        :data:`PENDING_REVIEW`. ``None`` is returned when no statement exists.
        """
        if language is None:
            return self._default()

        resolved = self._resolve_bucket(language)
        if resolved is not None:
            return self._cell(resolved)
        return self._default()

    def is_disputed(self, language: Optional[str]) -> bool:
        """
        Return ``True`` when the statement resolved for ``language`` is
        disputed. The same fallback chain as :meth:`lookup` applies.
        """
        resolved = self._resolve_bucket(language)
        if resolved is None:
            resolved = self._index.get(DEFAULT_LANGUAGE)
        return resolved in self.disputed if resolved is not None else False

    def _resolve_bucket(self, language: Optional[str]) -> Optional[str]:
        """Resolve a language tag to an XMP bucket key, or ``None`` for the default."""
        if language is None:
            return None
        requested = language.strip()
        if not requested:
            return None

        exact = self._index.get(requested.lower())
        if exact is not None:
            return exact

        base = requested.split("-", 1)[0].strip().lower()
        if base and base != requested.lower():
            return self._index.get(base)
        return None

    def as_table(self) -> List[Dict[str, str]]:
        """
        Return the display table as a list of ``language/source/text`` rows.

        A disputed language occupies a single row whose text is
        :data:`PENDING_REVIEW`. The conflicting originals remain available in
        :attr:`entries`.
        """
        rows: List[Dict[str, str]] = []
        for language in self._buckets:
            if language in self.disputed:
                rows.append(
                    {
                        "language": language,
                        "source": SOURCE_XMP,
                        "text": PENDING_REVIEW,
                    }
                )
            else:
                rows.append(self._buckets[language][0].as_dict())
        if self._exif_default is not None:
            rows.append(self._exif_default.as_dict())
        return rows

    def _cell(self, language: str) -> RightsEntry:
        if language in self.disputed:
            return RightsEntry(
                language=language, source=SOURCE_XMP, text=PENDING_REVIEW
            )
        return self._buckets[language][0]

    def _default(self) -> Optional[RightsEntry]:
        default_language = self._index.get(DEFAULT_LANGUAGE)
        if default_language is not None:
            return self._cell(default_language)
        return self._exif_default


def build_rights_table(
    exif_copyright: Any = None,
    xmp_data: Union[bytes, str, None] = None,
) -> Optional[RightsTable]:
    """
    Build a :class:`RightsTable` from EXIF copyright and raw XMP data.

    ``exif_copyright`` accepts the raw EXIF tag value (an ``IfdTag``-like
    object, ``str`` or ``bytes``); ``xmp_data`` accepts the raw XMP packet
    bytes, including files containing several XMP packets. ``None`` is
    returned when neither source carries any rights statement.
    """
    entries: List[RightsEntry] = []

    for packet in _parse_xmp_packets(xmp_data):
        entries.extend(_extract_xmp_rights(packet))

    # Merge the XMP statements per language tag, dropping exact duplicates
    # and flagging languages with genuinely different wording as disputed.
    buckets: Dict[str, List[RightsEntry]] = {}
    order: List[str] = []
    for entry in entries:
        bucket = buckets.setdefault(entry.language, [])
        if entry.language not in order:
            order.append(entry.language)
        if any(existing.text == entry.text for existing in bucket):
            continue
        bucket.append(entry)

    merged: List[RightsEntry] = []
    disputed: Set[str] = set()
    for language in order:
        bucket = buckets[language]
        if len(bucket) > 1:
            disputed.add(language)
        merged.extend(bucket)

    exif_text = _clean_text(exif_copyright)
    exif_default: Optional[RightsEntry] = None
    if exif_text is not None:
        exif_default = RightsEntry(
            language=DEFAULT_LANGUAGE, source=SOURCE_EXIF, text=exif_text
        )

    if not merged and exif_default is None:
        return None

    return RightsTable(entries=merged, disputed=disputed, exif_default=exif_default)


def _parse_xmp_packets(xmp_data: Union[bytes, str, None]) -> List[Any]:
    """Parse every XMP packet found in ``xmp_data`` into DOM documents."""
    if not xmp_data:
        return []
    if isinstance(xmp_data, str):
        raw = xmp_data.encode("utf-8", "replace")
    else:
        raw = xmp_data

    packets = _PACKET_RE.findall(raw)
    if not packets and b"<rdf:RDF" in raw:
        packets = [raw]

    documents = []
    for packet in packets:
        try:
            documents.append(parseString(packet))
        except ExpatError as err:
            logger.warning("XMP rights: cannot parse packet: %s", err)
    return documents


def _extract_xmp_rights(document: Any) -> List[RightsEntry]:
    """Collect every localized rights statement from one XMP document."""
    statements: List[RightsEntry] = []
    for element in document.getElementsByTagName("*"):
        if element.nodeType != Node.ELEMENT_NODE:
            continue
        if _is_rights_property(element):
            statements.extend(_read_literal_container(element))
        for attribute in _iter_attributes(element):
            if _is_rights_property(attribute):
                text = (attribute.value or "").strip()
                if text:
                    statements.append(
                        RightsEntry(
                            language=DEFAULT_LANGUAGE,
                            source=SOURCE_XMP,
                            text=text,
                        )
                    )
    return statements


def _is_rights_property(node: Any) -> bool:
    namespace = node.namespaceURI
    local_name = node.localName
    if namespace == _DC_NS and local_name == "rights":
        return True
    if namespace == _XMP_RIGHTS_NS and local_name == "UsageTerms":
        return True
    # Fallback for parsers/inputs without namespace information.
    return node.nodeName in ("dc:rights", "xmpRights:UsageTerms")


def _read_literal_container(element: Any) -> List[RightsEntry]:
    """Read the ``rdf:li`` alternatives of a rights property, or its text."""
    items = _get_elements_by_ns(element, _RDF_NS, "li", "rdf:li")
    if not items:
        text = _text_content(element)
        if text:
            return [
                RightsEntry(
                    language=_get_language(element) or DEFAULT_LANGUAGE,
                    source=SOURCE_XMP,
                    text=text,
                )
            ]
        return []

    statements = []
    for item in items:
        text = _text_content(item)
        if not text:
            continue
        statements.append(
            RightsEntry(
                language=_get_language(item) or DEFAULT_LANGUAGE,
                source=SOURCE_XMP,
                text=text,
            )
        )
    return statements


def _get_elements_by_ns(
    root: Any, namespace: str, local_name: str, qualified_name: str
) -> List[Any]:
    elements = root.getElementsByTagNameNS(namespace, local_name)
    if elements:
        return elements
    return [
        node
        for node in root.getElementsByTagName(qualified_name)
        if node.nodeType == Node.ELEMENT_NODE
    ]


def _iter_attributes(element: Any) -> List[Any]:
    attributes = getattr(element, "attributes", None)
    if not attributes:
        return []
    return [attributes.item(index) for index in range(attributes.length)]


def _get_language(node: Any) -> Optional[str]:
    language = node.getAttributeNS(_XML_NS, "lang")
    if not language:
        language = node.getAttribute("xml:lang")
    language = language.strip()
    return language or None


def _text_content(node: Any) -> str:
    parts = []
    for child in node.childNodes:
        if child.nodeType in (Node.TEXT_NODE, Node.CDATA_SECTION_NODE):
            parts.append(child.data)
    return "".join(parts).strip()


def _clean_text(value: Any) -> Optional[str]:
    """Normalize an EXIF copyright value into a stripped string."""
    if value is None:
        return None
    if hasattr(value, "values"):
        value = value.values
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            value = value.decode("latin-1")
    if not isinstance(value, str):
        value = str(value)
    value = value.strip().strip("\x00").strip()
    return value or None
