#!/usr/bin/env python3
"""把台架两侧的 candump 日志合成一张发送次数表。

原理
----
* 两侧日志记录的是同一物理发送序列,但两台钟存在固定偏差,且任一侧都可能丢帧。
* 标记文件给出若干已配对的锚点帧(两侧帧号)以及匹配容差;由锚点推出固定
  钟差,并把两条日志切成对齐的区段(锚点之间的帧只能与同区段的帧配对)。
* 同一 ID、同一载荷短时间内可能确实连发,因此仅当"观察顺序 + 锚点区段 +
  时间容差"能唯一确定配对时才合成一对;其余保留候选范围并报告歧义。
  例如一侧有两条相同帧、另一侧只有一条且落在中间时,不凭读取顺序挑一条,
  而是报告一处歧义,该 (ID, 载荷) 的次数记为区间。

输入格式
--------
candump 日志行(candump -l / -L 风格)::

    (1600000000.000000) can0 123#DEADBEEF
    (1600000000.100000) can0 123##1DEADBEEF   # CAN FD 也接受

标记文件::

    tolerance 0.005        # 匹配容差(秒),也可用 tolerance_ms 5
    anchor 12 34           # 左侧第 12 帧与右侧第 34 帧是同一物理帧(帧号从 1 起)

用法
----
    python3 merge_candump.py LEFT.log RIGHT.log --markers markers.txt [--csv]
"""

from __future__ import annotations

import argparse
import bisect
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field

LOG_LINE_RE = re.compile(
    r"^\s*\((?P<ts>\d+(?:\.\d+)?)\)\s+(?P<iface>\S+)\s+(?P<frame>\S+)\s*$"
)


@dataclass
class Frame:
    ts: float
    arb_id: str
    payload: str
    number: int  # 1-based,按在日志中出现的顺序编号(锚点文件引用此编号)

    @property
    def key(self) -> tuple[str, str]:
        return (self.arb_id, self.payload)


def parse_frame_token(token: str) -> tuple[str, str]:
    """把 '123#DEADBEEF' / '123##1DEADBEEF' 解析成 (ID, 载荷) 键。"""
    parts = token.split("#")
    arb_id = parts[0].upper()
    if not arb_id or not re.fullmatch(r"[0-9A-F]+", arb_id):
        raise ValueError(f"无效的帧 ID: {token!r}")
    if len(parts) == 2:
        payload = parts[1].upper()
    elif len(parts) == 3:  # CAN FD: ID##flags data,flags 并入载荷键
        payload = (parts[1] + parts[2]).upper()
    else:
        raise ValueError(f"无效的帧: {token!r}")
    return arb_id, payload


def parse_candump(path: str) -> list[Frame]:
    frames: list[Frame] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            match = LOG_LINE_RE.match(line)
            if not match:
                raise ValueError(f"{path}:{lineno}: 无法解析的 candump 行: {line!r}")
            arb_id, payload = parse_frame_token(match.group("frame"))
            frames.append(
                Frame(float(match.group("ts")), arb_id, payload, len(frames) + 1)
            )
    return frames


@dataclass
class Markers:
    tolerance: float  # 秒
    anchors: list[tuple[int, int]]  # (左侧帧号, 右侧帧号),均从 1 起


def parse_markers(path: str) -> Markers:
    tolerance: float | None = None
    anchors: list[tuple[int, int]] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            key = parts[0].lower()
            try:
                if key == "tolerance":
                    tolerance = float(parts[1])
                elif key == "tolerance_ms":
                    tolerance = float(parts[1]) / 1000.0
                elif key == "anchor" and len(parts) == 3:
                    anchors.append((int(parts[1]), int(parts[2])))
                else:
                    raise ValueError
            except (ValueError, IndexError):
                raise ValueError(f"{path}:{lineno}: 无法解析的标记行: {raw.strip()!r}")
    if tolerance is None:
        raise ValueError(f"{path}: 缺少 tolerance 行")
    if not anchors:
        raise ValueError(f"{path}: 至少需要一对 anchor")
    return Markers(tolerance, anchors)


@dataclass
class Ambiguity:
    arb_id: str
    payload: str
    left_numbers: list[int]
    right_numbers: list[int]
    count_min: int
    count_max: int


@dataclass
class GroupResult:
    arb_id: str
    payload: str
    count_min: int
    count_max: int
    pairs: list[tuple[int, int]]  # 已确认配对 (左帧号, 右帧号),含锚点
    solo_left: list[int]
    solo_right: list[int]
    ambiguities: list[Ambiguity] = field(default_factory=list)


@dataclass
class MergeResult:
    offset: float
    tolerance: float
    anchor_count: int
    groups: list[GroupResult]
    ambiguities: list[Ambiguity]


def merge_logs(left: list[Frame], right: list[Frame], markers: Markers) -> MergeResult:
    tol = markers.tolerance
    n_left, n_right = len(left), len(right)
    for left_no, right_no in markers.anchors:
        if not (1 <= left_no <= n_left) or not (1 <= right_no <= n_right):
            raise ValueError(f"锚点 ({left_no}, {right_no}) 超出日志帧数范围")

    anchor_left = [a - 1 for a, _ in markers.anchors]
    anchor_right = [b - 1 for _, b in markers.anchors]
    if any(x >= y for x, y in zip(anchor_left, anchor_left[1:])) or any(
        x >= y for x, y in zip(anchor_right, anchor_right[1:])
    ):
        raise ValueError("锚点顺序与日志观察顺序不一致")

    # 固定钟差:右钟 - 左钟,取中位数并用容差校验所有锚点
    deltas = [right[b].ts - left[a].ts for a, b in zip(anchor_left, anchor_right)]
    offset = statistics.median(deltas)
    bad = [
        (no_l, no_r, d)
        for (no_l, no_r), d in zip(markers.anchors, deltas)
        if abs(d - offset) > tol
    ]
    if bad:
        detail = ", ".join(f"({a}, {b}) 偏差 {d:+.6f}s" for (a, b, d) in bad)
        raise ValueError(f"锚点与固定钟差 {offset:+.6f}s 的矛盾超出容差: {detail}")

    for a, b in zip(anchor_left, anchor_right):
        if left[a].key != right[b].key:
            print(
                f"警告: 锚点 ({left[a].number}, {right[b].number}) 的 ID/载荷不一致",
                file=sys.stderr,
            )

    def segment_of(anchor_idx: list[int]):
        def seg(i: int) -> int:
            return bisect.bisect_right(anchor_idx, i)

        return seg

    seg_left = segment_of(anchor_left)
    seg_right = segment_of(anchor_right)
    anchor_left_set = set(anchor_left)
    anchor_right_set = set(anchor_right)

    def feasible(li: int, ri: int) -> bool:
        return (
            seg_left(li) == seg_right(ri)
            and abs(right[ri].ts - left[li].ts - offset) <= tol
        )

    groups_left: dict[tuple[str, str], list[int]] = defaultdict(list)
    groups_right: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, frame in enumerate(left):
        if i not in anchor_left_set:
            groups_left[frame.key].append(i)
    for i, frame in enumerate(right):
        if i not in anchor_right_set:
            groups_right[frame.key].append(i)

    # 锚点本身也是已确认的发送,计入对应 (ID, 载荷) 组
    anchor_pairs: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    for a, b in zip(anchor_left, anchor_right):
        anchor_pairs[left[a].key].append((left[a].number, right[b].number))

    keys = sorted(
        set(groups_left) | set(groups_right) | set(anchor_pairs),
        key=lambda k: (int(k[0], 16), k[1]),
    )

    results: list[GroupResult] = []
    all_ambiguities: list[Ambiguity] = []
    for key in keys:
        l_ids = groups_left.get(key, [])
        r_ids = groups_right.get(key, [])

        # 反复合并"双方唯一可选"的配对,直到没有新的唯一配对
        un_l = set(l_ids)
        un_r = set(r_ids)
        pairs: list[tuple[int, int]] = []
        while True:
            forward = {li: [ri for ri in sorted(un_r) if feasible(li, ri)] for li in un_l}
            backward: dict[int, list[int]] = {ri: [] for ri in un_r}
            for li, cands in forward.items():
                for ri in cands:
                    backward[ri].append(li)
            unique = [
                (li, cands[0])
                for li, cands in forward.items()
                if len(cands) == 1 and len(backward[cands[0]]) == 1
            ]
            if not unique:
                break
            for li, ri in unique:
                pairs.append((li, ri))
                un_l.discard(li)
                un_r.discard(ri)

        # 剩余帧按可行性连通分量归类:孤立帧是确定的发送,含边的分量是歧义
        rem_l = sorted(un_l)
        rem_r = sorted(un_r)
        parent: dict[tuple[str, int], tuple[str, int]] = {}

        def find(x: tuple[str, int]) -> tuple[str, int]:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: tuple[str, int], b: tuple[str, int]) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for li in rem_l:
            find(("L", li))
        for ri in rem_r:
            find(("R", ri))
        for li in rem_l:
            for ri in rem_r:
                if feasible(li, ri):
                    union(("L", li), ("R", ri))

        components: dict[tuple[str, int], dict[str, list[int]]] = defaultdict(
            lambda: {"L": [], "R": []}
        )
        for li in rem_l:
            components[find(("L", li))]["L"].append(li)
        for ri in rem_r:
            components[find(("R", ri))]["R"].append(ri)

        solo_left: list[int] = []
        solo_right: list[int] = []
        ambiguities: list[Ambiguity] = []
        count_min = count_max = len(pairs) + len(anchor_pairs.get(key, []))
        for comp in components.values():
            comp_l, comp_r = comp["L"], comp["R"]
            if not comp_r:
                solo_left.extend(comp_l)
                count_min += len(comp_l)
                count_max += len(comp_l)
                continue
            if not comp_l:
                solo_right.extend(comp_r)
                count_min += len(comp_r)
                count_max += len(comp_r)
                continue
            # 歧义分量:真实配对数 P 介于 0 与最大匹配之间,
            # 发送次数 = a + b - P ∈ [a + b - P_max, a + b]
            used: set[int] = set()
            p_max = 0
            for li in comp_l:
                for ri in comp_r:
                    if ri not in used and feasible(li, ri):
                        used.add(ri)
                        p_max += 1
                        break
            a, b = len(comp_l), len(comp_r)
            amb = Ambiguity(
                arb_id=key[0],
                payload=key[1],
                left_numbers=[left[i].number for i in comp_l],
                right_numbers=[right[i].number for i in comp_r],
                count_min=a + b - p_max,
                count_max=a + b,
            )
            ambiguities.append(amb)
            count_min += amb.count_min
            count_max += amb.count_max

        results.append(
            GroupResult(
                arb_id=key[0],
                payload=key[1],
                count_min=count_min,
                count_max=count_max,
                pairs=sorted(
                    [(left[l].number, right[r].number) for l, r in pairs]
                    + anchor_pairs.get(key, [])
                ),
                solo_left=[left[i].number for i in solo_left],
                solo_right=[right[i].number for i in solo_right],
                ambiguities=ambiguities,
            )
        )
        all_ambiguities.extend(ambiguities)

    return MergeResult(
        offset=offset,
        tolerance=tol,
        anchor_count=len(markers.anchors),
        groups=results,
        ambiguities=all_ambiguities,
    )


def format_report(result: MergeResult) -> str:
    lines = []
    lines.append(
        f"固定钟差(右-左): {result.offset:+.6f} s   容差: {result.tolerance:.6f} s   "
        f"锚点: {result.anchor_count} 对"
    )
    confirmed = sum(len(g.pairs) for g in result.groups)
    solo_l = sum(len(g.solo_left) for g in result.groups)
    solo_r = sum(len(g.solo_right) for g in result.groups)
    lines.append(
        f"已确认配对: {confirmed}   仅左侧: {solo_l}   仅右侧: {solo_r}   "
        f"歧义: {len(result.ambiguities)} 处"
    )
    lines.append("")
    lines.append(f"{'ID':<8}{'载荷':<20}{'发送次数'}")
    for g in result.groups:
        count = (
            str(g.count_min)
            if g.count_min == g.count_max
            else f"{g.count_min}..{g.count_max}"
        )
        flag = "  [歧义]" if g.ambiguities else ""
        lines.append(f"{g.arb_id:<8}{g.payload or '(空)':<20}{count}{flag}")
    if result.ambiguities:
        lines.append("")
        lines.append("歧义明细:")
        for idx, amb in enumerate(result.ambiguities, 1):
            left_part = ",".join(str(n) for n in amb.left_numbers) or "-"
            right_part = ",".join(str(n) for n in amb.right_numbers) or "-"
            lines.append(
                f"  #{idx} {amb.arb_id}#{amb.payload}: "
                f"左帧 {left_part} <-> 右帧 {right_part}, "
                f"该处次数 {amb.count_min}..{amb.count_max}"
            )
    return "\n".join(lines)


def format_csv(result: MergeResult) -> str:
    lines = ["id,payload,count_min,count_max,ambiguous"]
    for g in result.groups:
        lines.append(
            f"{g.arb_id},{g.payload},{g.count_min},{g.count_max},"
            f"{1 if g.ambiguities else 0}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把台架两侧的 candump 日志合成发送次数表"
    )
    parser.add_argument("left", help="左侧 candump 日志")
    parser.add_argument("right", help="右侧 candump 日志")
    parser.add_argument("--markers", required=True, help="锚点与容差文件")
    parser.add_argument("--csv", action="store_true", help="输出 CSV 而非文本表")
    args = parser.parse_args(argv)

    try:
        left = parse_candump(args.left)
        right = parse_candump(args.right)
        markers = parse_markers(args.markers)
        result = merge_logs(left, right, markers)
    except (OSError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    print(format_csv(result) if args.csv else format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
