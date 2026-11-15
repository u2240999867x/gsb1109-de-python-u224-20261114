"""merge_candump 的核心规则测试。"""

import os
import tempfile
import unittest

from merge_candump import merge_logs, parse_candump, parse_markers


def write(dirpath, name, lines):
    path = os.path.join(dirpath, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def candump(entries):
    return [f"({ts:.6f}) can0 {frame}" for ts, frame in entries]


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_merge(self, left_lines, right_lines, marker_lines):
        left = parse_candump(write(self.tmp.name, "left.log", left_lines))
        right = parse_candump(write(self.tmp.name, "right.log", right_lines))
        markers = parse_markers(write(self.tmp.name, "markers.txt", marker_lines))
        return merge_logs(left, right, markers)

    def group(self, result, arb_id, payload):
        for g in result.groups:
            if g.arb_id == arb_id and g.payload == payload:
                return g
        self.fail(f"组 {arb_id}#{payload} 不存在")

    def test_exact_merge_with_offset(self):
        left = candump(
            [
                (100.000, "700#01"),  # 锚点
                (100.100, "123#AA"),
                (100.200, "123#AA"),
                (101.000, "700#02"),  # 锚点
                (101.100, "124#BB"),
            ]
        )
        right = candump(
            [
                (100.010, "700#01"),
                (100.111, "123#AA"),
                (100.212, "123#AA"),
                (101.010, "700#02"),
                (101.112, "124#BB"),
                (101.500, "125#CC"),  # 仅右侧
            ]
        )
        result = self.run_merge(
            left, right, ["tolerance 0.005", "anchor 1 1", "anchor 4 4"]
        )
        self.assertAlmostEqual(result.offset, 0.010, places=6)
        self.assertEqual(len(result.ambiguities), 0)
        self.assertEqual(self.group(result, "123", "AA").count_min, 2)
        self.assertEqual(self.group(result, "123", "AA").count_max, 2)
        self.assertEqual(self.group(result, "124", "BB").count_min, 1)
        self.assertEqual(self.group(result, "125", "CC").count_min, 1)
        self.assertEqual(self.group(result, "700", "01").count_min, 1)

    def test_two_against_one_in_middle_is_ambiguous(self):
        # 左侧两条相同帧,右侧一条落在中间:必须报告一处歧义,次数 2..3
        left = candump(
            [
                (100.000, "700#01"),
                (100.100, "123#AA"),
                (100.104, "123#AA"),
                (101.000, "700#02"),
            ]
        )
        right = candump(
            [
                (100.010, "700#01"),
                (100.112, "123#AA"),  # 校正后位于左帧 2、3 中间,两者都在容差内
                (101.010, "700#02"),
            ]
        )
        result = self.run_merge(
            left, right, ["tolerance 0.005", "anchor 1 1", "anchor 4 3"]
        )
        self.assertEqual(len(result.ambiguities), 1)
        amb = result.ambiguities[0]
        self.assertEqual(amb.left_numbers, [2, 3])
        self.assertEqual(amb.right_numbers, [2])
        self.assertEqual((amb.count_min, amb.count_max), (2, 3))
        group = self.group(result, "123", "AA")
        self.assertEqual((group.count_min, group.count_max), (2, 3))
        self.assertEqual(group.pairs, [])  # 不允许凭读取顺序挑一条

    def test_partial_overlap_pairs_uniquely(self):
        # 右侧帧只在左帧 2 的容差内:唯一配对,无歧义,次数恰为 2
        left = candump(
            [
                (100.000, "700#01"),
                (100.100, "123#AA"),
                (100.200, "123#AA"),
                (101.000, "700#02"),
            ]
        )
        right = candump(
            [
                (100.010, "700#01"),
                (100.112, "123#AA"),
                (101.010, "700#02"),
            ]
        )
        result = self.run_merge(
            left, right, ["tolerance 0.005", "anchor 1 1", "anchor 4 3"]
        )
        self.assertEqual(len(result.ambiguities), 0)
        group = self.group(result, "123", "AA")
        self.assertEqual((group.count_min, group.count_max), (2, 2))
        self.assertEqual(group.pairs, [(2, 2)])
        self.assertEqual(group.solo_left, [3])

    def test_repeated_identical_frames_pair_in_order(self):
        left = candump(
            [(100.000, "700#01")]
            + [(100.100 + i * 0.01, "123#AA") for i in range(3)]
            + [(101.000, "700#02")]
        )
        right = candump(
            [(100.010, "700#01")]
            + [(100.110 + i * 0.01, "123#AA") for i in range(3)]
            + [(101.010, "700#02")]
        )
        result = self.run_merge(
            left, right, ["tolerance 0.005", "anchor 1 1", "anchor 5 5"]
        )
        self.assertEqual(len(result.ambiguities), 0)
        group = self.group(result, "123", "AA")
        self.assertEqual((group.count_min, group.count_max), (3, 3))
        self.assertEqual(group.pairs, [(2, 2), (3, 3), (4, 4)])

    def test_anchor_beyond_tolerance_rejected(self):
        left = candump([(100.000, "700#01"), (101.000, "700#02")])
        right = candump([(100.010, "700#01"), (101.100, "700#02")])
        with self.assertRaises(ValueError):
            self.run_merge(left, right, ["tolerance 0.005", "anchor 1 1", "anchor 2 2"])

    def test_anchor_out_of_order_rejected(self):
        left = candump([(100.000, "700#01"), (101.000, "700#02")])
        right = candump([(100.010, "700#01"), (101.010, "700#02")])
        with self.assertRaises(ValueError):
            self.run_merge(left, right, ["tolerance 0.005", "anchor 2 1", "anchor 1 2"])

    def test_segments_keep_sides_of_anchor_separate(self):
        # 同 ID/载荷分别出现在锚点两侧,不能跨锚点配对
        left = candump(
            [
                (100.000, "123#AA"),
                (100.500, "700#01"),  # 锚点
                (100.900, "123#AA"),
            ]
        )
        right = candump(
            [
                (100.510, "700#01"),
                (100.912, "123#AA"),
            ]
        )
        result = self.run_merge(left, right, ["tolerance 0.005", "anchor 2 1"])
        self.assertEqual(len(result.ambiguities), 0)
        group = self.group(result, "123", "AA")
        self.assertEqual((group.count_min, group.count_max), (2, 2))
        self.assertEqual(group.pairs, [(3, 2)])
        self.assertEqual(group.solo_left, [1])


if __name__ == "__main__":
    unittest.main()
