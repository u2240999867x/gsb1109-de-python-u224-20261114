"""Tests for the consolidated EXIF/XMP rights table."""

from pathlib import Path

import exifread
from exifread.rights import (
    DEFAULT_LANGUAGE,
    PENDING_REVIEW,
    SOURCE_EXIF,
    SOURCE_XMP,
    RightsEntry,
    build_rights_table,
)

RESOURCES_ROOT = Path(__file__).parent / "resources"

PACKET_A = """<?xpacket begin=""?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">
   <dc:rights>
    <rdf:Alt>
     <rdf:li xml:lang="x-default">Default rights statement</rdf:li>
     <rdf:li xml:lang="en">English rights statement</rdf:li>
     <rdf:li xml:lang="en-US">American English rights statement</rdf:li>
     <rdf:li xml:lang="zh-CN">中文版权声明</rdf:li>
    </rdf:Alt>
   </dc:rights>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""

PACKET_B = """<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
      xmlns:dc="http://purl.org/dc/elements/1.1/"
      xmlns:xmpRights="http://ns.adobe.com/xap/1.0/rights/">
   <dc:rights>
    <rdf:Alt>
     <rdf:li xml:lang="en">Conflicting English rights statement</rdf:li>
     <rdf:li xml:lang="fr">Declaration des droits en francais</rdf:li>
    </rdf:Alt>
   </dc:rights>
   <xmpRights:UsageTerms>
    <rdf:Alt>
     <rdf:li xml:lang="de">Deutsche Nutzungsbedingungen</rdf:li>
    </rdf:Alt>
   </xmpRights:UsageTerms>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>"""

PACKET_ATTRIBUTE = """<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
      xmlns:dc="http://purl.org/dc/elements/1.1/"
      dc:rights="Plain rights attribute"/>
 </rdf:RDF>
</x:xmpmeta>"""

PACKET_NO_RIGHTS = """<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">
   <dc:title><rdf:Alt><rdf:li xml:lang="x-default">A title</rdf:li></rdf:Alt></dc:title>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>"""


def test_exif_copyright_only():
    table = build_rights_table(exif_copyright="(c) 2026 Acme Inc.")
    assert table is not None

    entry = table.lookup("en")
    assert isinstance(entry, RightsEntry)
    assert entry.source == SOURCE_EXIF
    assert entry.language == DEFAULT_LANGUAGE
    assert entry.text == "(c) 2026 Acme Inc."

    assert table.lookup(None).source == SOURCE_EXIF
    assert table.as_table() == [
        {
            "language": DEFAULT_LANGUAGE,
            "source": SOURCE_EXIF,
            "text": "(c) 2026 Acme Inc.",
        }
    ]


def test_exif_copyright_accepts_tag_like_and_bytes():
    class FakeTag:
        def __init__(self, values):
            self.values = values

    table = build_rights_table(exif_copyright=FakeTag(b"  Byte copyright \x00"))
    assert table.lookup(None).text == "Byte copyright"

    table = build_rights_table(exif_copyright=b"Raw bytes copyright")
    assert table.lookup(None).text == "Raw bytes copyright"


def test_xmp_localized_lookup_and_fallback_chain():
    table = build_rights_table(xmp_data=PACKET_A)
    assert table is not None

    exact = table.lookup("zh-CN")
    assert exact.language == "zh-CN"
    assert exact.source == SOURCE_XMP
    assert exact.text == "中文版权声明"

    # Case-insensitive language tag matching.
    assert table.lookup("ZH-cn").text == "中文版权声明"

    # Region-specific tag wins over the region-less variant.
    assert table.lookup("en-US").text == "American English rights statement"

    # Unknown region falls back to the region-less variant of the same language.
    assert table.lookup("en-GB").text == "English rights statement"
    assert table.lookup("en-AU").language == "en"

    # Completely unknown language falls back to x-default.
    assert table.lookup("ja").text == "Default rights statement"
    assert table.lookup(None).text == "Default rights statement"
    assert table.lookup("   ").text == "Default rights statement"


def test_entries_keep_language_source_and_original_text():
    table = build_rights_table(
        exif_copyright="EXIF copyright sentence", xmp_data=PACKET_A
    )
    languages = {entry.language for entry in table.entries}
    assert languages == {"x-default", "en", "en-US", "zh-CN"}
    assert all(entry.source == SOURCE_XMP for entry in table.entries)
    original = next(entry for entry in table.entries if entry.language == "zh-CN")
    assert original.text == "中文版权声明"

    rows = {(row["language"], row["source"]) for row in table.as_table()}
    assert (DEFAULT_LANGUAGE, SOURCE_EXIF) in rows
    assert ("zh-CN", SOURCE_XMP) in rows


def test_usage_terms_and_attribute_forms():
    table = build_rights_table(xmp_data=PACKET_B)
    assert table.lookup("de").text == "Deutsche Nutzungsbedingungen"
    assert table.lookup("de").source == SOURCE_XMP

    attribute_table = build_rights_table(xmp_data=PACKET_ATTRIBUTE)
    entry = attribute_table.lookup("it")
    assert entry.language == DEFAULT_LANGUAGE
    assert entry.text == "Plain rights attribute"


def test_conflicting_xmp_statements_mark_cell_pending():
    table = build_rights_table(xmp_data=PACKET_A + PACKET_B)

    # Both original sentences are preserved in the raw entries.
    english_entries = [entry for entry in table.entries if entry.language == "en"]
    assert len(english_entries) == 2
    assert {entry.text for entry in english_entries} == {
        "English rights statement",
        "Conflicting English rights statement",
    }

    assert table.is_disputed("en")
    cell = table.lookup("en")
    assert cell.text == PENDING_REVIEW
    assert cell.source == SOURCE_XMP

    # A region variant falls back to the disputed region-less language.
    assert table.is_disputed("en-GB")
    assert table.lookup("en-GB").text == PENDING_REVIEW

    # The display table holds a single pending cell for the language.
    en_rows = [row for row in table.as_table() if row["language"] == "en"]
    assert en_rows == [{"language": "en", "source": SOURCE_XMP, "text": PENDING_REVIEW}]


def test_other_languages_unaffected_by_dispute():
    table = build_rights_table(xmp_data=PACKET_A + PACKET_B)

    assert not table.is_disputed("fr")
    assert table.lookup("fr").text == "Declaration des droits en francais"
    assert table.lookup("zh-CN").text == "中文版权声明"
    assert table.lookup("de").text == "Deutsche Nutzungsbedingungen"
    assert table.lookup("en-US").text == "American English rights statement"
    # No default dispute: x-default still resolves to its unique statement.
    assert table.lookup("ja").text == "Default rights statement"


def test_identical_statements_are_not_disputed():
    repeated = PACKET_A.replace(
        '<rdf:li xml:lang="zh-CN">中文版权声明</rdf:li>',
        '<rdf:li xml:lang="zh-CN">中文版权声明</rdf:li>',
    )
    table = build_rights_table(xmp_data=PACKET_A + repeated)
    assert not table.disputed
    zh_entries = [e for e in table.entries if e.language == "zh-CN"]
    assert len(zh_entries) == 1
    assert table.lookup("zh-CN").text == "中文版权声明"


def test_default_falls_back_to_exif_when_xmp_has_no_default():
    table = build_rights_table(
        exif_copyright="Fallback EXIF copyright", xmp_data=PACKET_B
    )
    # PACKET_B has no x-default rights entry.
    assert table.lookup("ja").source == SOURCE_EXIF
    assert table.lookup("ja").text == "Fallback EXIF copyright"
    # Localized XMP entries still win over the EXIF default.
    assert table.lookup("fr").source == SOURCE_XMP


def test_no_rights_returns_none():
    assert build_rights_table() is None
    assert build_rights_table(exif_copyright="   ", xmp_data=PACKET_NO_RIGHTS) is None
    assert build_rights_table(xmp_data=None) is None


def test_malformed_xmp_keeps_exif_rights():
    malformed = b"<x:xmpmeta>not valid xml</x:xmpmeta>"
    assert build_rights_table(xmp_data=malformed) is None
    table = build_rights_table(
        exif_copyright="Still available copyright", xmp_data=malformed
    )
    assert table.lookup("en").text == "Still available copyright"


def test_process_file_integration(tmp_path):
    source = RESOURCES_ROOT / "jpg/Samsung_Digimax_i50_MP3.jpg"
    image = tmp_path / "sample.jpg"
    image.write_bytes(source.read_bytes() + PACKET_A.encode("utf-8"))

    with open(image, "rb") as fh:
        tags = exifread.process_file(fh=fh, details=False)
    table = tags["Image RightsTable"]
    assert table.lookup("zh-CN").text == "中文版权声明"
    # An unknown language falls back to the XMP default statement.
    assert table.lookup("ja").source == SOURCE_XMP
    assert table.lookup("ja").text == "Default rights statement"
    # The EXIF copyright sentence is still kept verbatim in the table.
    exif_rows = [row for row in table.as_table() if row["source"] == SOURCE_EXIF]
    assert exif_rows == [
        {
            "language": DEFAULT_LANGUAGE,
            "source": SOURCE_EXIF,
            "text": "COPYRIGHT, 2005",
        }
    ]

    with open(image, "rb") as fh:
        serialized = exifread.process_file(fh=fh, details=False, builtin_types=True)
    payload = serialized["Image RightsTable"]
    assert "entries" in payload and "disputed" in payload and "table" in payload
    assert any(row["language"] == "zh-CN" for row in payload["table"])


def test_process_file_dispute_integration(tmp_path):
    source = RESOURCES_ROOT / "jpg/Samsung_Digimax_i50_MP3.jpg"
    image = tmp_path / "dispute.jpg"
    image.write_bytes(
        source.read_bytes() + PACKET_A.encode("utf-8") + PACKET_B.encode("utf-8")
    )

    with open(image, "rb") as fh:
        tags = exifread.process_file(fh=fh, details=False)
    table = tags["Image RightsTable"]
    assert table.lookup("en").text == PENDING_REVIEW
    assert table.lookup("fr").text == "Declaration des droits en francais"


def test_process_file_conflict_detected_in_debug_mode(tmp_path):
    source = RESOURCES_ROOT / "jpg/Samsung_Digimax_i50_MP3.jpg"
    image = tmp_path / "debug_dispute.jpg"
    image.write_bytes(
        source.read_bytes() + PACKET_A.encode("utf-8") + PACKET_B.encode("utf-8")
    )

    with open(image, "rb") as fh:
        tags = exifread.process_file(fh=fh, details=True, debug=True)
    table = tags["Image RightsTable"]
    assert table.is_disputed("en")
    assert table.lookup("en").text == PENDING_REVIEW


def test_process_file_can_opt_out():
    file_path = RESOURCES_ROOT / "jpg/Samsung_Digimax_i50_MP3.jpg"
    with open(file_path, "rb") as fh:
        tags = exifread.process_file(fh=fh, details=False, rights_table=False)
    assert "Image RightsTable" not in tags


def test_real_resource_with_conflicting_rights_properties():
    # This image carries different x-default wording in dc:rights and
    # xmpRights:UsageTerms inside the same XMP packet.
    file_path = RESOURCES_ROOT / "jpg/tests/46_UnicodeEncodeError.jpg"
    with open(file_path, "rb") as fh:
        tags = exifread.process_file(fh=fh, details=False)
    table = tags["Image RightsTable"]

    assert table.is_disputed(DEFAULT_LANGUAGE)
    assert table.lookup("en").text == PENDING_REVIEW

    originals = {
        entry.text
        for entry in table.entries
        if entry.language == DEFAULT_LANGUAGE and entry.source == SOURCE_XMP
    }
    assert originals == {
        "Copyright © 2012 Robert Muckley",
        "No reproduction of any kind  without prior written permission",
    }
    # The EXIF copyright remains the final, distinct fallback row.
    exif_rows = [row for row in table.as_table() if row["source"] == SOURCE_EXIF]
    assert exif_rows == [
        {
            "language": DEFAULT_LANGUAGE,
            "source": SOURCE_EXIF,
            "text": "Copyright © 2012 Robert Muckley",
        }
    ]
