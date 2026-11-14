"""从 DOCX 中提取条款（编号、层级、正文）。

编号来源优先级：
1. 自动编号实例（w:numPr，结合 word/numbering.xml 还原 "4.2" 这类编号）；
2. 标题样式（w:pStyle 指向带 outlineLvl 的样式）中的字面编号；
3. 正文开头的字面编号（如 "4.2 付款方式"、"三、……" 中的 "4、"）。

只依赖标准库，直接读取 docx 包内 XML。
"""

import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@dataclass
class Clause:
    number: str       # 条款编号，如 "4.2"
    level: int        # 层级，0 为顶层
    text: str         # 条款正文（不含编号）
    para_index: int   # 在文档段落流中的序号，用于保持先后顺序
    source: str       # "numbering" | "literal"
    parent: str = ""  # 父条款编号，顶层为 ""


# 字面编号：4 / 4.2 / 4.2.1，后跟 、．. 或空白
_LITERAL_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s*([、．.]|\s)?")


def _read_numbering(zf):
    """返回 (numId -> abstractNumId, abstractNumId -> {ilvl: (start, numFmt, lvlText)})"""
    try:
        root = ET.fromstring(zf.read("word/numbering.xml"))
    except KeyError:
        return {}, {}
    abstract = {}
    for absnum in root.findall(f"{W}abstractNum"):
        aid = absnum.get(f"{W}abstractNumId")
        levels = {}
        for lvl in absnum.findall(f"{W}lvl"):
            ilvl = int(lvl.get(f"{W}ilvl"))
            start = lvl.find(f"{W}start")
            numfmt = lvl.find(f"{W}numFmt")
            lvltext = lvl.find(f"{W}lvlText")
            levels[ilvl] = (
                int(start.get(f"{W}val")) if start is not None else 1,
                numfmt.get(f"{W}val") if numfmt is not None else "decimal",
                lvltext.get(f"{W}val") if lvltext is not None else f"%{ilvl + 1}.",
            )
        abstract[aid] = levels
    nums = {}
    for num in root.findall(f"{W}num"):
        ref = num.find(f"{W}abstractNumId")
        if ref is not None:
            nums[num.get(f"{W}numId")] = ref.get(f"{W}val")
    return nums, abstract


def _read_styles(zf):
    """返回 styleId -> {"name": str, "outline": int|None}"""
    try:
        root = ET.fromstring(zf.read("word/styles.xml"))
    except KeyError:
        return {}
    styles = {}
    for style in root.findall(f"{W}style"):
        sid = style.get(f"{W}styleId")
        name_el = style.find(f"{W}name")
        name = name_el.get(f"{W}val") if name_el is not None else ""
        outline = None
        ppr = style.find(f"{W}pPr")
        if ppr is not None:
            ol = ppr.find(f"{W}outlineLvl")
            if ol is not None:
                outline = int(ol.get(f"{W}val"))
        if outline is None:
            m = re.match(r"^[Hh]eading\s*(\d+)$", sid or "") or re.match(
                r"^heading (\d+)$", name or ""
            )
            if m:
                outline = int(m.group(1)) - 1
        styles[sid] = {"name": name, "outline": outline}
    return styles


class _NumberingState:
    """按文档顺序为每个编号实例 (numId) 维护计数器，还原可见编号。"""

    def __init__(self, nums, abstract):
        self.nums = nums
        self.abstract = abstract
        self.counters = {}  # numId -> {ilvl: current value}

    def next_label(self, num_id, ilvl):
        levels = self.abstract.get(self.nums.get(num_id), {})
        counters = self.counters.setdefault(num_id, {})
        # 更深的层级归零
        for lvl in list(counters):
            if lvl > ilvl:
                del counters[lvl]
        # 浅层若尚未出现，按 start 补齐
        for lvl in range(ilvl + 1):
            if lvl not in counters:
                start = levels.get(lvl, (1, "decimal", ""))[0]
                counters[lvl] = start - 1 if lvl == ilvl else start
        counters[ilvl] += 1
        lvltext = levels.get(ilvl, (1, "decimal", f"%{ilvl + 1}."))[2]
        label = lvltext
        for lvl in range(ilvl + 1):
            label = label.replace(f"%{lvl + 1}", str(counters[lvl]))
        return label.strip().rstrip(".")


def _literal_clause(text, is_heading):
    """从段落文本开头识别字面编号。为减少误判，纯空格分隔的一级编号
    （如 "2026 年……"）在非标题段落中不视为条款。"""
    m = _LITERAL_RE.match(text)
    if not m:
        return None
    number, sep = m.group(1), m.group(2)
    dotted = "." in number
    if not (is_heading or dotted or sep in ("、", "．", ".")):
        return None
    return number, text[m.end():].strip()


def extract_clauses(path):
    """解析 DOCX，返回按文档顺序排列的 Clause 列表。"""
    with zipfile.ZipFile(path) as zf:
        nums, abstract = _read_numbering(zf)
        styles = _read_styles(zf)
        root = ET.fromstring(zf.read("word/document.xml"))

    state = _NumberingState(nums, abstract)
    clauses = []
    for idx, p in enumerate(root.iter(f"{W}p")):
        ppr = p.find(f"{W}pPr")
        style_id = num_id = None
        ilvl = 0
        if ppr is not None:
            ps = ppr.find(f"{W}pStyle")
            if ps is not None:
                style_id = ps.get(f"{W}val")
            numpr = ppr.find(f"{W}numPr")
            if numpr is not None:
                numid_el = numpr.find(f"{W}numId")
                ilvl_el = numpr.find(f"{W}ilvl")
                if numid_el is not None:
                    num_id = numid_el.get(f"{W}val")
                    ilvl = int(ilvl_el.get(f"{W}val")) if ilvl_el is not None else 0
        text = "".join(t.text or "" for t in p.iter(f"{W}t")).strip()
        if not text:
            continue
        style = styles.get(style_id, {})
        is_heading = style.get("outline") is not None

        if num_id is not None and num_id in nums:
            label = state.next_label(num_id, ilvl)
            clauses.append(Clause(label, ilvl, text, idx, "numbering"))
            continue
        literal = _literal_clause(text, is_heading)
        if literal:
            number, body = literal
            clauses.append(Clause(number, number.count("."), body, idx, "literal"))

    # 用层级栈推导父项，保证同一父项下的先后顺序可用 para_index 还原
    stack = []
    for clause in clauses:
        while stack and stack[-1].level >= clause.level:
            stack.pop()
        clause.parent = stack[-1].number if stack else ""
        stack.append(clause)
    return clauses
