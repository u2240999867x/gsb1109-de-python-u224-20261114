"""条款对齐：在两份 DOCX 的条款列表之间建立对应关系。

规则（对应工单要求）：
1. 人工确认的对应关系（confirmations）优先，作为对齐锚点；
   旧条款拆成两个且人工确认两者均承接时，保留两条对应（status="split"），
   锚点之后的条款继续按内容对齐，不会全部被判为改写。
2. 对齐在同一父项内递归进行，严格维护先后顺序（不允许交叉匹配）。
3. 相同短句（归一化后长度 <= SHORT_PHRASE_MAX）若在任一文档中出现多次，
   证据不唯一，不自动匹配，标记为 "ambiguous"，工单不自动迁移。
4. 自动匹配分两级：归一化文本完全相等且唯一 -> same/renumbered；
   相似度足够且与次优候选拉开差距 -> modified。
"""

import difflib
import re
from dataclasses import dataclass, field

SHORT_PHRASE_MAX = 20
FUZZY_THRESHOLD = 0.6
FUZZY_MARGIN = 0.15
SAME_NUMBER_THRESHOLD = 0.45


def normalize(text):
    return re.sub(r"\s+", "", text or "")


@dataclass
class Mapping:
    old: str          # 旧条款编号；"added" 记录为 None
    new: list         # 新条款编号列表；"removed"/"ambiguous" 可能为空
    status: str       # same|renumbered|modified|split|confirmed|added|removed|ambiguous
    auto: bool        # 是否可据此自动迁移工单
    note: str = ""


def _children_map(clauses):
    children = {}
    for c in clauses:
        children.setdefault(c.parent, []).append(c)
    return children


def _text_counts(clauses):
    counts = {}
    for c in clauses:
        norm = normalize(c.text)
        if norm:
            counts[norm] = counts.get(norm, 0) + 1
    return counts


def align_documents(old_clauses, new_clauses, confirmations=None):
    """返回 Mapping 列表。confirmations: [{"old": "5.2", "new": ["5.3", "5.4"]}]"""
    confirmations = confirmations or []
    old_by_no = {c.number: c for c in old_clauses}
    new_by_no = {c.number: c for c in new_clauses}
    old_children = _children_map(old_clauses)
    new_children = _children_map(new_clauses)
    old_counts = _text_counts(old_clauses)
    new_counts = _text_counts(new_clauses)

    def is_ambiguous_text(norm):
        if not norm:
            return False
        if len(norm) <= SHORT_PHRASE_MAX:
            return old_counts.get(norm, 0) > 1 or new_counts.get(norm, 0) > 1
        return False

    mappings = []
    matched_old = set()
    matched_new = set()

    def emit(old_clause, new_clauses, status, auto, note=""):
        mappings.append(Mapping(
            old=old_clause.number if old_clause else None,
            new=[c.number for c in new_clauses],
            status=status, auto=auto, note=note,
        ))
        if old_clause:
            matched_old.add(old_clause.number)
        matched_new.update(c.number for c in new_clauses)

    def try_match_one(old_clause, candidates):
        """在保持顺序的候选集中为 old_clause 找唯一对应；返回 (new_clause, status) 或 None。"""
        norm = normalize(old_clause.text)
        if not norm:
            return None
        exact = [c for c in candidates if normalize(c.text) == norm]
        if exact:
            # 证据唯一才自动匹配：候选唯一、旧侧唯一、非重复短句
            if (len(exact) == 1 and old_counts.get(norm, 0) == 1
                    and not is_ambiguous_text(norm)):
                c = exact[0]
                return c, "same" if c.number == old_clause.number else "renumbered"
            mappings.append(Mapping(
                old=old_clause.number,
                new=[c.number for c in exact],
                status="ambiguous", auto=False,
                note="相同短句出现多次，证据不唯一，需人工确认",
            ))
            matched_old.add(old_clause.number)
            return "ambiguous", None
        scored = sorted(
            ((difflib.SequenceMatcher(None, norm, normalize(c.text)).ratio(), c)
             for c in candidates if normalize(c.text)),
            key=lambda item: item[0], reverse=True,
        )
        if not scored:
            return None
        best, best_c = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        if best >= FUZZY_THRESHOLD and best - second >= FUZZY_MARGIN:
            return best_c, "modified"
        if best_c.number == old_clause.number and best >= SAME_NUMBER_THRESHOLD:
            return best_c, "modified"
        return None

    def match_segment(old_seg, new_seg):
        """同一父项内、两锚点之间的顺序保持对齐。"""
        available = [c for c in new_seg if c.number not in matched_new]
        for o in old_seg:
            if o.number in matched_old:
                continue
            result = try_match_one(o, available)
            if not result:
                continue
            clause, status = result
            if clause == "ambiguous":
                continue
            emit(o, [clause], status, auto=True)
            available = [c for c in available
                         if c.number not in matched_new
                         and c.para_index > clause.para_index]
            recurse(o, [clause])

    def recurse(old_clause, new_clauses):
        olds = old_children.get(old_clause.number, [])
        news = []
        for n in new_clauses:
            news.extend(new_children.get(n.number, []))
        if olds or news:
            align_group(olds, news)

    def align_group(old_group, new_group):
        # 人工确认的锚点（含拆分），按旧文档顺序切分段落
        anchors = []
        for o in old_group:
            for conf in confirmations:
                if conf["old"] == o.number:
                    ns = [new_by_no[x] for x in conf["new"] if x in new_by_no]
                    anchors.append((o, ns))
        old_pos = new_pos = 0
        for o, ns in anchors:
            if not ns or any(n not in new_group for n in ns):
                # 跨父项移动的人工确认：直接记录，不参与分段
                if o.number not in matched_old and ns:
                    status = "split" if len(ns) > 1 else "confirmed"
                    emit(o, ns, status, auto=True, note="人工确认")
                    recurse(o, ns)
                continue
            oi = old_group.index(o)
            ni = new_group.index(ns[0])
            if any(new_group.index(n) != ni + k for k, n in enumerate(ns)):
                continue  # 拆分结果不连续，交给人工，不做猜测
            match_segment(old_group[old_pos:oi], new_group[new_pos:ni])
            if o.number not in matched_old:
                status = "split" if len(ns) > 1 else "confirmed"
                emit(o, ns, status, auto=True, note="人工确认")
                recurse(o, ns)
            old_pos, new_pos = oi + 1, ni + len(ns)
        match_segment(old_group[old_pos:], new_group[new_pos:])

    align_group(old_children.get("", []), new_children.get("", []))

    # 父项未能对齐时，人工确认仍然生效（人工证据优先）
    for conf in confirmations:
        o = old_by_no.get(conf["old"])
        ns = [new_by_no[x] for x in conf["new"] if x in new_by_no]
        if o is not None and ns and o.number not in matched_old:
            status = "split" if len(ns) > 1 else "confirmed"
            emit(o, ns, status, auto=True, note="人工确认")
            recurse(o, ns)

    # 兜底：未匹配的旧条款 = removed；未匹配的新条款 = added（重复短句标 ambiguous）
    ambiguous_norms = {normalize(c.text) for c in old_clauses + new_clauses
                       if is_ambiguous_text(normalize(c.text))}
    for c in old_clauses:
        if c.number not in matched_old:
            emit(c, [], "removed", auto=False)
    for c in new_clauses:
        if c.number not in matched_new:
            if normalize(c.text) in ambiguous_norms:
                emit(None, [c], "ambiguous", auto=False,
                     note="相同短句出现多次，证据不唯一，需人工确认")
            else:
                emit(None, [c], "added", auto=False)

    order = {c.number: i for i, c in enumerate(old_clauses)}
    new_order = {c.number: i for i, c in enumerate(new_clauses)}
    mappings.sort(key=lambda m: (
        order.get(m.old, len(order)) if m.old else len(order),
        min((new_order.get(n, len(new_order)) for n in m.new), default=len(new_order)),
    ))
    return mappings


_REF_RE = re.compile(r"第\s*([0-9]+(?:\.[0-9]+)*)\s*项")


def migrate_references(mappings, refs):
    """按对应关系迁移工单引用的条款号。仅在证据唯一（或经人工确认）时自动迁移。"""
    by_old = {}
    for m in mappings:
        if m.old is not None:
            by_old.setdefault(m.old, m)
    results = []
    for ref in refs:
        m = by_old.get(ref)
        if m is None:
            results.append({"ref": ref, "auto": False, "new": [],
                            "reason": "旧文档中不存在该条款编号"})
        elif m.status in ("same", "renumbered", "modified") and len(m.new) == 1:
            results.append({"ref": ref, "auto": True, "new": list(m.new),
                            "status": m.status})
        elif m.status in ("split", "confirmed"):
            results.append({"ref": ref, "auto": True, "new": list(m.new),
                            "status": m.status, "reason": "人工确认"})
        elif m.status == "ambiguous":
            results.append({"ref": ref, "auto": False, "new": [],
                            "candidates": list(m.new),
                            "reason": "证据不唯一，需人工确认"})
        else:  # removed
            results.append({"ref": ref, "auto": False, "new": [],
                            "reason": "旧条款在新版中未找到对应"})
    return results


def migrate_text(text, mappings):
    """改写工单正文中的“见第 X 项”引用；无法自动迁移的保持原样。"""
    refs = set(_REF_RE.findall(text))
    decisions = {r["ref"]: r for r in migrate_references(mappings, sorted(refs))}

    def repl(match):
        decision = decisions.get(match.group(1))
        if decision and decision["auto"] and decision["new"]:
            return "第 {} 项".format("、".join(decision["new"]))
        return match.group(0)

    return _REF_RE.sub(repl, text)
