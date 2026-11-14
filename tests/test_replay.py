"""Replay of historical AIS text with nested tag groups and AIS fragments."""

import pathlib
import tempfile
import unittest

from pyais.messages import NMEASentenceFactory
from pyais.replay import parse_replay, read_replay
from pyais.stream import IterMessages, TagBlockQueue
from pyais.util import checksum, compute_checksum


def tag_block(fields: str) -> bytes:
    return b"\\" + fields.encode() + b"*" + f"{checksum(fields.encode()):02X}".encode() + b"\\"


def sentence(body: str) -> bytes:
    return f"{body}*{compute_checksum(body):02X}".encode()


# One type 1 message and one type 5 message split into two fragments.
SINGLE_1 = "!AIVDM,1,1,,A,13HOI:0P0000VOHLCnHQKwvL05Ip,0"
TYPE5_1 = "!AIVDM,2,1,1,A,55?MbV02;H;s<HtKR20EHE:0@T4@Dn2222222216L961O5Gf0NSQEp6ClRp8,0"
TYPE5_2 = "!AIVDM,2,2,1,A,88888888880,2"
# A second, distinct type 5 message that reuses sequence number 1.
OTHER_5_1 = "!AIVDM,2,1,1,B,53nFBv01SJ<thHp6220H4heHTf2222222222221?50:454o<`9QSlUDp,0"
OTHER_5_2 = "!AIVDM,2,2,1,B,88888888880,2"

# One tag group on station STA: a single message and a multipart message.
GROUP_A = [
    tag_block("g:1-3-100,s:STA") + sentence(SINGLE_1),
    tag_block("g:2-3-100,s:STA") + sentence(TYPE5_1),
    tag_block("g:3-3-100,s:STA") + sentence(TYPE5_2),
]
# The same group id on station STB: only its single message and fragment 2.
GROUP_B = [
    tag_block("g:1-3-100,s:STB") + sentence(SINGLE_1),
    tag_block("g:2-3-100,s:STB") + sentence(TYPE5_2),
]


class ReplayGroupTestCase(unittest.TestCase):

    def test_group_can_hold_several_messages(self):
        result = parse_replay([(None, i + 1, line) for i, line in enumerate(GROUP_A)])

        self.assertEqual(len(result.groups), 1)
        group = result.groups[0]
        self.assertEqual(group.station, "STA")
        self.assertEqual(group.group_id, 100)
        self.assertTrue(group.complete)
        self.assertEqual(len(group.messages), 2)

        single, multipart = group.messages
        self.assertTrue(single.complete)
        self.assertEqual(single.line_numbers, (1,))
        self.assertTrue(multipart.complete)
        self.assertEqual(multipart.line_numbers, (2, 3))
        self.assertEqual(multipart.message.line_numbers, [2, 3])
        self.assertEqual(multipart.decode().msg_type, 5)

    def test_same_group_id_on_different_stations_is_kept_apart(self):
        lines = [(("sta", i + 1, line)) for i, line in enumerate(GROUP_A)]
        lines += [(("stb", i + 1, line)) for i, line in enumerate(GROUP_B)]
        result = parse_replay(lines)

        self.assertEqual({(g.station, g.group_id) for g in result.groups},
                         {("STA", 100), ("STB", 100)})
        sta = next(group for group in result.groups if group.station == "STA")
        stb = next(group for group in result.groups if group.station == "STB")

        # Nothing was borrowed between the two stations.
        self.assertEqual(sta.line_numbers, (1, 2, 3))
        self.assertEqual(stb.line_numbers, (1, 2))
        self.assertTrue(sta.complete)
        self.assertFalse(stb.complete)
        self.assertEqual(stb.missing_lines, (3,))
        self.assertEqual([m.complete for m in stb.messages], [True, False])

    def test_two_stations_using_sequence_number_one_do_not_merge(self):
        lines = [
            tag_block("g:1-2-1,s:A") + sentence(TYPE5_1),
            tag_block("g:1-2-1,s:B") + sentence(OTHER_5_1),
            tag_block("g:2-2-1,s:A") + sentence(TYPE5_2),
            tag_block("g:2-2-1,s:B") + sentence(OTHER_5_2),
        ]
        result = parse_replay([(None, i + 1, line) for i, line in enumerate(lines)])

        self.assertEqual(len(result.groups), 2)
        messages = {group.station: group.complete_messages[0] for group in result.groups}
        self.assertEqual(set(messages), {"A", "B"})
        self.assertEqual(messages["A"].decode().msg_type, 5)
        self.assertEqual(messages["B"].decode().msg_type, 5)
        self.assertNotEqual(messages["A"].raw, messages["B"].raw)
        self.assertEqual(messages["A"].line_numbers, (1, 3))
        self.assertEqual(messages["B"].line_numbers, (2, 4))

    def test_missing_group_line_keeps_complete_standalone_messages(self):
        result = parse_replay([(None, i + 1, line) for i, line in enumerate(GROUP_B)])

        group = result.groups[0]
        self.assertFalse(group.complete)
        self.assertEqual(group.missing_lines, (3,))
        # The single sentence in the group is complete and decodable.
        self.assertEqual(len(group.complete_messages), 1)
        self.assertEqual(group.complete_messages[0].decode().msg_type, 1)
        # The multipart inner message reports the fragment it is missing.
        self.assertEqual(len(group.incomplete_messages), 1)
        self.assertEqual(group.incomplete_messages[0].missing_fragments, (1,))
        self.assertEqual(group.incomplete_messages[0].line_numbers, (2,))
        self.assertEqual(result.missing_fragments, [{
            'station': "STB",
            'group_id': 100,
            'line_numbers': [2],
            'origins': [[None, 2]],
            'missing_fragments': [1],
        }])

    def test_missing_outer_line_still_yields_the_complete_message(self):
        # The stream is not blocked on an incomplete outer tag group: the
        # single message inside it is emitted and decodable.
        messages = list(IterMessages(GROUP_B))
        self.assertEqual([msg.decode().msg_type for msg in messages], [1])
        self.assertEqual(messages[0].line_number, 1)

        # The tag group reports what it is missing without inventing lines.
        tbq = TagBlockQueue()
        list(IterMessages(GROUP_B, tbq=tbq))
        group = tbq.flush()[0]
        self.assertEqual(group.missing_lines, (3,))
        self.assertEqual([m.decode().msg_type for m in group.complete_messages], [1])

    def test_line_numbers_survive_a_multipart_message(self):
        result = parse_replay([(None, i + 1, line) for i, line in enumerate(GROUP_A)])
        multipart = result.groups[0].messages[1]
        self.assertEqual(multipart.message.line_numbers, [2, 3])
        self.assertEqual(multipart.message.line_number, 2)

    def test_file_order_does_not_change_attribution_or_missing_list(self):
        a_lines = [(str("a.ais"), i + 1, line) for i, line in enumerate(GROUP_A)]
        b_lines = [(str("b.ais"), i + 1, line) for i, line in enumerate(GROUP_B)]

        forward = parse_replay(a_lines + b_lines)
        reverse = parse_replay(b_lines + a_lines)

        def snapshot(result):
            return (
                [(g.station, g.group_id, g.line_numbers, g.missing_lines,
                  [(m.line_numbers, m.missing_fragments, m.complete) for m in g.messages])
                 for g in result.groups],
                result.missing_fragments,
                sorted((str(key), value['station'], value['group_id'])
                       for key, value in result.attribution.items()),
            )

        self.assertEqual(snapshot(forward), snapshot(reverse))

    def test_read_replay_reads_several_files_with_line_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = pathlib.Path(tmp) / "a.ais"
            second = pathlib.Path(tmp) / "b.ais"
            first.write_bytes(b"\n".join(GROUP_A) + b"\n")
            second.write_bytes(b"\n".join(GROUP_B) + b"\n")

            forward = read_replay([first, second])
            reverse = read_replay([second, first])

        self.assertEqual(
            [(g.station, g.group_id, g.line_numbers, g.missing_lines) for g in forward.groups],
            [(g.station, g.group_id, g.line_numbers, g.missing_lines) for g in reverse.groups],
        )
        self.assertEqual(forward.missing_fragments, reverse.missing_fragments)


class TagBlockQueueReplayTestCase(unittest.TestCase):

    def test_queue_separates_stations_with_equal_group_ids(self):
        tbq = TagBlockQueue()
        for number, line in enumerate(GROUP_A + GROUP_B, 1):
            parsed = NMEASentenceFactory.produce(line)
            parsed.line_number = number
            tbq.put_sentence(parsed)

        sta = tbq.get_nowait()
        results = tbq.flush()
        self.assertEqual([(g.station, g.group_id) for g in results], [("STB", 100)])
        stb = results[0]
        self.assertTrue(sta.complete)
        self.assertEqual(stb.missing_lines, (3,))
        self.assertEqual(stb.line_numbers, (4, 5))

    def test_incomplete_group_is_flushed_with_its_complete_messages(self):
        tbq = TagBlockQueue()
        for line in GROUP_B:
            tbq.put_sentence(NMEASentenceFactory.produce(line))
        self.assertTrue(tbq.empty())

        results = tbq.flush()
        self.assertEqual(len(results), 1)
        group = results[0]
        self.assertEqual(group.missing_lines, (3,))
        self.assertEqual(len(group), 2)
        self.assertEqual(tbq.get_nowait(), group)

    def test_stream_attaches_line_numbers_and_isolates_untagged_stations(self):
        stream = IterMessages(GROUP_A)
        messages = list(stream)
        self.assertEqual([m.line_number for m in messages], [1, 2])
        self.assertEqual(messages[1].line_numbers, [2, 3])
        self.assertEqual(messages[1].decode().msg_type, 5)

    def test_streams_keep_incomplete_groups_for_explicit_flush(self):
        tbq = TagBlockQueue()
        list(IterMessages(GROUP_B, tbq=tbq))
        # An incomplete outer group is not reported as if it were complete.
        self.assertTrue(tbq.empty())
        group = tbq.flush()[0]
        self.assertEqual(group.station, "STB")
        self.assertEqual(group.missing_lines, (3,))


if __name__ == "__main__":
    unittest.main()
