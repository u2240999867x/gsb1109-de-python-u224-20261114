"""生成最小 DOCX 的测试工具。

paragraphs: [{"text": str, "style": "Heading1"|None, "num": (numId, ilvl)|None}]
带 "num" 的段落使用自动编号实例（numbering.xml 中预置三级十进制编号）。
"""

import zipfile

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{ns}">
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:pPr><w:outlineLvl w:val="0"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/>
    <w:pPr><w:outlineLvl w:val="1"/></w:pPr>
  </w:style>
</w:styles>""".format(ns=W_NS)

_NUMBERING = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="{ns}">
  <w:abstractNum w:abstractNumId="10">
    <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>
    <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2."/></w:lvl>
    <w:lvl w:ilvl="2"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2.%3."/></w:lvl>
  </w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="10"/></w:num>
</w:numbering>""".format(ns=W_NS)


def _paragraph_xml(spec):
    ppr = ""
    inner = ""
    if spec.get("style"):
        inner += f'<w:pStyle w:val="{spec["style"]}"/>'
    if spec.get("num"):
        num_id, ilvl = spec["num"]
        inner += (f'<w:numPr><w:ilvl w:val="{ilvl}"/>'
                  f'<w:numId w:val="{num_id}"/></w:numPr>')
    if inner:
        ppr = f"<w:pPr>{inner}</w:pPr>"
    return f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{spec["text"]}</w:t></w:r></w:p>'


def build_docx(path, paragraphs):
    body = "".join(_paragraph_xml(p) for p in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W_NS}"><w:body>{body}</w:body></w:document>'
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/numbering.xml", _NUMBERING)
        zf.writestr("word/styles.xml", _STYLES)
    return path


def numbered(text, ilvl):
    return {"text": text, "num": ("1", ilvl)}


def literal(text, style=None):
    return {"text": text, "style": style}
