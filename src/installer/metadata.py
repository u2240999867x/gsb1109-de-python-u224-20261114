"""Lossless parsing and version-aware interpretation of distribution metadata.

The ``METADATA``, ``PKG-INFO`` and ``WHEEL`` files carried by distributions use
the :rfc:`5322` header-and-body syntax described in the Python core metadata
specifications. Real-world files exercise a few corners that a plain
:mod:`email` parse does not retain:

- fields such as ``Requires-Dist`` may occur repeatedly and their occurrence
  order is meaningful;
- older packages put the long description in a folded ``Description`` header,
  while newer packages put it in the message body;
- text in the body that merely looks like a header (e.g. a line reading
  ``Requires-Dist: ...``) must never be interpreted as a field.

This module separates two concerns:

- :func:`parse_metadata` performs a *lossless* structural parse. Every field
  occurrence keeps its order, its unfolded value, the raw value including the
  fold line breaks, the physical line range and the character offsets, while
  the body boundary is recorded separately. The original text can be
  reconstructed from the result.
- :func:`interpret_metadata` builds on that parse and explains the contents
  according to the declared ``Metadata-Version``, exposing every available
  description together with the provenance needed to cross-check it.
"""

import re
from dataclasses import dataclass
from email.message import Message
from email.parser import FeedParser
from email.policy import default
from typing import Literal

__all__ = [
    "DescriptionSource",
    "DisplayDescription",
    "InterpretedMetadata",
    "MetadataField",
    "ParsedMetadata",
    "interpret_metadata",
    "parse_metadata",
]

#: The first core metadata version whose description lives in the message body.
_BODY_DESCRIPTION_VERSION = (2, 1)


@dataclass(frozen=True)
class MetadataField:
    """One occurrence of a header field.

    Duplicate field names produce one instance per occurrence, in the order in
    which they appear in the file.
    """

    name: str
    """Field name, exactly as written (case is preserved)."""

    value: str
    """Field value with :rfc:`5322` folding unfolded, ready for display."""

    raw_value: str
    """Field value as written, from after the colon to the end of the field."""

    order: int
    """Zero-based occurrence index of this field among all header fields."""

    line_start: int
    """Index of the field's first physical line."""

    line_end: int
    """Index one past the field's last physical line."""

    span: tuple[int, int]
    """Character offsets ``[start, end)`` of the whole field in the source."""

    def text(self) -> str:
        """Reconstruct the field's original text, including fold lines."""
        return f"{self.name}:{self.raw_value}"


@dataclass(frozen=True)
class ParsedMetadata:
    """Lossless structural parse of a metadata file.

    The structured data is sufficient to reconstruct well-formed input; the
    original :attr:`contents` are retained as the ultimate source of truth.
    """

    contents: str
    """The complete, unmodified file contents."""

    fields: tuple[MetadataField, ...]
    """Every header field occurrence, in occurrence order."""

    body_span: tuple[int, int] | None
    """Character offsets ``[start, end)`` of the body, or ``None`` without one."""

    body_line_start: int | None
    """Index of the first body line, or ``None`` when there is no separator."""

    body_line_end: int | None
    """Index one past the last body line, or ``None`` when there is no body."""

    def get_all(self, name: str) -> tuple[str, ...]:
        """Return unfolded values for every occurrence of ``name``.

        Field name matching is case-insensitive, per :rfc:`5322`.

        :param name: the field name to look up
        :return: the values, in occurrence order
        """
        wanted = name.lower()
        return tuple(
            field.value for field in self.fields if field.name.lower() == wanted
        )

    def body(self) -> str | None:
        """Return the verbatim body text, or ``None`` when no body exists."""
        if self.body_span is None:
            return None
        start, end = self.body_span
        return self.contents[start:end]

    def to_source(self) -> str:
        """Reconstruct the file text from the structured parse.

        For well-formed metadata this is identical to the original contents,
        including duplicate fields, fold line breaks and the body boundary.
        """
        header_section = "".join(field.text() for field in self.fields)
        if self.body_span is None:
            return header_section
        # Keep the original separator line (including its line ending), so CRLF
        # and LF inputs both round-trip exactly.
        header_end = self.fields[-1].span[1] if self.fields else 0
        separator = self.contents[header_end : self.body_span[0]]
        return f"{header_section}{separator}{self.body()}"


@dataclass(frozen=True)
class DescriptionSource:
    """Provenance for a description value."""

    kind: Literal["header", "body"]
    """Where the description was found: a folded header or the message body."""

    order: int | None
    """Header field occurrence index, or ``None`` for the body."""

    line_start: int
    """Index of the first physical line backing this description."""

    line_end: int
    """Index one past the last physical line backing this description."""

    span: tuple[int, int]
    """Character offsets ``[start, end)`` of the source in the file."""


@dataclass(frozen=True)
class DisplayDescription:
    """A description value ready for display, with its provenance."""

    text: str
    """The description text interpreted for display."""

    source: DescriptionSource
    """Where this text came from in the original file."""


@dataclass(frozen=True)
class InterpretedMetadata:
    """Metadata interpreted according to its declared ``Metadata-Version``."""

    raw: ParsedMetadata
    """The lossless parse this interpretation was derived from."""

    message: Message
    """The file parsed with the default :mod:`email` policy."""

    metadata_version: str | None
    """The declared ``Metadata-Version`` value, if present."""

    description_content_type: str | None
    """The declared ``Description-Content-Type`` value, if present."""

    descriptions: tuple[DisplayDescription, ...]
    """Every description available in the file, with provenance."""

    def display_description(self) -> str | None:
        """Return the description to show for the declared metadata version.

        Core metadata 2.1 and newer use the message body; older versions use
        the (possibly folded) ``Description`` header. When the preferred
        location does not contain a description, the other one is used.
        """
        if not self.descriptions:
            return None
        if _version_at_least(self.metadata_version, _BODY_DESCRIPTION_VERSION):
            preferred = "body"
        else:
            preferred = "header"
        for description in self.descriptions:
            if description.source.kind == preferred:
                return description.text
        return self.descriptions[0].text


def _unfold(raw_value: str) -> str:
    """Unfold a field value per :rfc:`5322`, matching the default email policy."""
    lines = raw_value.splitlines(keepends=True)
    pieces = []
    for line in lines:
        if line.endswith("\r\n"):
            line = line[:-2]
        elif line.endswith(("\n", "\r")):
            line = line[:-1]
        pieces.append(line)
    return "".join(pieces).lstrip(" \t")


def parse_metadata(contents: str) -> ParsedMetadata:
    """Parse metadata file contents without losing structural information.

    :param contents: the complete contents of a ``METADATA``, ``PKG-INFO`` or
        ``WHEEL`` file
    :return: a lossless parse retaining field occurrence order, fold ranges and
        the body boundary
    """
    lines = contents.splitlines(keepends=True)

    line_offsets: list[int] = []
    offset = 0
    for line in lines:
        line_offsets.append(offset)
        offset += len(line)

    fields: list[MetadataField] = []
    body_line_start: int | None = None

    index = 0
    while index < len(lines):
        line = lines[index]
        bare_line = line.rstrip("\r\n")

        if bare_line == "":
            # The empty line separates the header section from the body.
            body_line_start = index + 1
            break
        if line[:1] in (" ", "\t"):
            # An orphan continuation line is a parse defect in the email
            # parser and never starts a field; skip it here as well.
            index += 1
            continue

        colon_at = line.find(":")
        if colon_at < 0:
            # A line without a field separator ends the header section; the
            # email parser treats the remainder as the body.
            body_line_start = index
            break

        line_start = index
        span_start = line_offsets[index]
        index += 1
        while index < len(lines) and lines[index][:1] in (" ", "\t"):
            index += 1
        line_end = index
        span_end = line_offsets[line_end - 1] + len(lines[line_end - 1])

        field_text = contents[span_start:span_end]
        raw_value = field_text[colon_at + 1 :]
        fields.append(
            MetadataField(
                name=line[:colon_at],
                value=_unfold(raw_value),
                raw_value=raw_value,
                order=len(fields),
                line_start=line_start,
                line_end=line_end,
                span=(span_start, span_end),
            )
        )

    if body_line_start is None:
        body_span: tuple[int, int] | None = None
        body_line_end: int | None = None
    else:
        # body_line_start may equal len(lines) for a separator with an empty
        # body; in that case the body starts (and ends) at the end of the file.
        body_start = (
            line_offsets[body_line_start]
            if body_line_start < len(lines)
            else len(contents)
        )
        body_span = (body_start, len(contents))
        body_line_end = len(lines)

    return ParsedMetadata(
        contents=contents,
        fields=tuple(fields),
        body_span=body_span,
        body_line_start=body_line_start,
        body_line_end=body_line_end,
    )


def _version_at_least(version: str | None, minimum: tuple[int, ...]) -> bool:
    """Check whether a dotted ``Metadata-Version`` is at least ``minimum``."""
    if not version:
        return False
    numbers: list[int] = []
    for piece in re.split(r"[.\s-]", version.strip()):
        if not piece.isdigit():
            break
        numbers.append(int(piece))
    return tuple(numbers) >= minimum


def interpret_metadata(parsed: ParsedMetadata) -> InterpretedMetadata:
    """Interpret a lossless parse according to its metadata version.

    Header-like lines inside the body are never turned into fields; they are
    only reachable through :meth:`ParsedMetadata.body`. When both a header
    description and a body description exist, each is retained separately with
    its own provenance so the two sources can be cross-checked.

    :param parsed: the result of :func:`parse_metadata`
    :return: the version-aware interpretation
    """
    feed_parser = FeedParser(policy=default)
    feed_parser.feed(parsed.contents)
    message = feed_parser.close()

    descriptions: list[DisplayDescription] = []
    for field in parsed.fields:
        if field.name.lower() != "description":
            continue
        descriptions.append(
            DisplayDescription(
                text=field.value,
                source=DescriptionSource(
                    kind="header",
                    order=field.order,
                    line_start=field.line_start,
                    line_end=field.line_end,
                    span=field.span,
                ),
            )
        )

    body = parsed.body()
    if body:
        assert parsed.body_span is not None
        assert parsed.body_line_start is not None
        assert parsed.body_line_end is not None
        descriptions.append(
            DisplayDescription(
                text=body,
                source=DescriptionSource(
                    kind="body",
                    order=None,
                    line_start=parsed.body_line_start,
                    line_end=parsed.body_line_end,
                    span=parsed.body_span,
                ),
            )
        )

    return InterpretedMetadata(
        raw=parsed,
        message=message,
        metadata_version=message.get("Metadata-Version"),
        description_content_type=message.get("Description-Content-Type"),
        descriptions=tuple(descriptions),
    )
