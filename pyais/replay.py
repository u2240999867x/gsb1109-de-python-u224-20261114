"""Replay historical AIS text while keeping both levels of membership.

A recording often contains two nested groupings:

* the NMEA 4.10 ``g:`` tag group of a single receiving station, and
* the AIS fragments of each (possibly multipart) message inside that group.

The same station may reuse a group id or a sequence number over time, and two
stations may even use the same numbers at the same moment. This module keeps
those namespaces apart and records the original line number of every fragment.
It also reports lines and fragments that never arrived, without discarding
complete messages that happen to share a group with an incomplete one.
"""

import dataclasses
import pathlib
import typing

from pyais.messages import AISSentence, NMEASentence, NMEASentenceFactory

LineInput = typing.Union[bytes, str, typing.Tuple[typing.Optional[str], int, typing.Union[bytes, str]]]
Origin = typing.Tuple[typing.Optional[str], int]


@dataclasses.dataclass
class ReplayLine:
    """One parsed line of a recording and its provenance."""

    sentence: NMEASentence
    source: typing.Optional[str]
    line_number: int
    station: typing.Optional[str]
    group_id: typing.Optional[int]
    group_sentence_num: typing.Optional[int]
    group_sentence_total: typing.Optional[int]

    @property
    def origin(self) -> Origin:
        return (self.source, self.line_number)

    @property
    def scope(self) -> typing.Optional[str]:
        """The namespace this line belongs to.

        The receiving station when the tag block names one, otherwise the
        recording it came from. This keeps two untagged files apart without
        pretending a file path is a station name.
        """
        return self.station if self.station is not None else self.source

    @property
    def is_ais(self) -> bool:
        return isinstance(self.sentence, AISSentence)

    @property
    def raw(self) -> bytes:
        return self.sentence.raw

    @property
    def group(self) -> typing.Any:
        return self.sentence.group


@dataclasses.dataclass
class ReplayMessage:
    """An inner AIS message and the lines/fragments that make it up."""

    sentences: typing.List[AISSentence]
    station: typing.Optional[str]
    group_id: typing.Optional[int]
    line_numbers: typing.Tuple[int, ...]
    missing_fragments: typing.Tuple[int, ...] = ()
    message: typing.Optional[AISSentence] = None
    origins: typing.Tuple[Origin, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing_fragments and self.message is not None

    @property
    def parts(self) -> typing.List[AISSentence]:
        """Alias for :attr:`sentences`."""
        return self.sentences

    @property
    def fragment_count(self) -> int:
        return self.sentences[0].frag_cnt if self.sentences else 0

    @property
    def fragment_numbers(self) -> typing.Tuple[int, ...]:
        return tuple(s.frag_num for s in self.sentences)

    @property
    def missing_fragment_numbers(self) -> typing.Tuple[int, ...]:
        """Alias for :attr:`missing_fragments`."""
        return self.missing_fragments

    @property
    def seq_id(self) -> typing.Optional[int]:
        return self.sentences[0].seq_id if self.sentences else None

    @property
    def channel(self) -> typing.Optional[str]:
        return self.sentences[0].channel if self.sentences else None

    @property
    def key(self) -> typing.Tuple[typing.Any, ...]:
        first = self.sentences[0]
        return (first.seq_id, first.channel, first.talker_id, first.type, first.frag_cnt)

    @property
    def raw(self) -> bytes:
        if self.message is not None:
            return self.message.raw
        return b'\n'.join(s.raw for s in self.sentences)

    def decode(self) -> typing.Any:
        if self.message is None:
            raise ValueError('cannot decode an incomplete replay message')
        return self.message.decode()

    def asdict(self) -> typing.Dict[str, typing.Any]:
        return {
            'station': self.station,
            'group_id': self.group_id,
            'line_numbers': list(self.line_numbers),
            'origins': [list(origin) for origin in self.origins],
            'fragments': list(self.fragment_numbers),
            'missing_fragments': list(self.missing_fragments),
            'complete': self.complete,
        }


@dataclasses.dataclass
class ReplayGroup:
    """An outer receiving-station tag group.

    ``lines`` holds every member line, ``messages`` the assembled inner AIS
    messages. A group can be incomplete (``missing_lines``) while still
    containing complete messages; those remain available and decodable.
    """

    station: typing.Optional[str]
    group_id: int
    sentence_total: int
    lines: typing.List[ReplayLine]
    messages: typing.List[ReplayMessage]
    missing_lines: typing.Tuple[int, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing_lines

    @property
    def members(self) -> typing.List[ReplayLine]:
        """Alias for :attr:`lines`."""
        return self.lines

    @property
    def line_numbers(self) -> typing.Tuple[int, ...]:
        return tuple(line.line_number for line in self.lines)

    @property
    def origins(self) -> typing.Tuple[Origin, ...]:
        return tuple(line.origin for line in self.lines)

    @property
    def sentence_numbers(self) -> typing.Tuple[int, ...]:
        numbers = [line.group_sentence_num for line in self.lines]
        return tuple(n for n in numbers if n is not None)

    @property
    def missing_sentence_numbers(self) -> typing.Tuple[int, ...]:
        """Alias for :attr:`missing_lines`."""
        return self.missing_lines

    @property
    def sentences(self) -> typing.List[NMEASentence]:
        return [line.sentence for line in self.lines]

    @property
    def complete_messages(self) -> typing.List[ReplayMessage]:
        return [message for message in self.messages if message.complete]

    @property
    def incomplete_messages(self) -> typing.List[ReplayMessage]:
        return [message for message in self.messages if not message.complete]

    @property
    def missing_fragments(self) -> typing.Dict[typing.Tuple[typing.Any, ...], typing.Tuple[int, ...]]:
        return {
            (message.station, message.group_id, message.line_numbers): message.missing_fragments
            for message in self.incomplete_messages
        }

    def asdict(self) -> typing.Dict[str, typing.Any]:
        return {
            'station': self.station,
            'group_id': self.group_id,
            'sentence_total': self.sentence_total,
            'line_numbers': list(self.line_numbers),
            'origins': [list(origin) for origin in self.origins],
            'sentence_numbers': list(self.sentence_numbers),
            'missing_lines': list(self.missing_lines),
            'complete': self.complete,
            'messages': [message.asdict() for message in self.messages],
        }


@dataclasses.dataclass
class ReplayResult:
    """Everything recovered from a replay, in a deterministic order."""

    groups: typing.List[ReplayGroup]
    messages: typing.List[ReplayMessage]
    standalone: typing.List[ReplayMessage]
    lines: typing.List[ReplayLine]

    @property
    def complete_messages(self) -> typing.List[ReplayMessage]:
        return [message for message in self.messages if message.complete]

    @property
    def incomplete_messages(self) -> typing.List[ReplayMessage]:
        return [message for message in self.messages if not message.complete]

    @property
    def missing_fragments(self) -> typing.List[typing.Dict[str, typing.Any]]:
        return [
            {
                'station': message.station,
                'group_id': message.group_id,
                'line_numbers': list(message.line_numbers),
                'origins': [list(origin) for origin in message.origins],
                'missing_fragments': list(message.missing_fragments),
            }
            for message in self.incomplete_messages
        ]

    @property
    def missing_lines(self) -> typing.List[typing.Dict[str, typing.Any]]:
        return [
            {
                'station': group.station,
                'group_id': group.group_id,
                'missing_lines': list(group.missing_lines),
            }
            for group in self.groups
            if group.missing_lines
        ]

    @property
    def attribution(self) -> typing.Dict[typing.Any, typing.Dict[str, typing.Any]]:
        """Map each original line to its station and outer group.

        The key is the line number when it is unique. If two source files
        reuse the same line number, those lines are keyed by
        ``(source, line_number)`` so nothing is overwritten and the result
        does not depend on the order in which the files were read.
        """
        counts: typing.Dict[int, int] = {}
        for line in self.lines:
            counts[line.line_number] = counts.get(line.line_number, 0) + 1

        result: typing.Dict[typing.Any, typing.Dict[str, typing.Any]] = {}
        for line in self.lines:
            entry = {
                'station': line.station,
                'group_id': line.group_id,
                'source': line.source,
            }
            if counts[line.line_number] == 1:
                result[line.line_number] = entry
            else:
                result[(line.source, line.line_number)] = entry
        return result

    def for_station(self, station: typing.Optional[str]) -> typing.List[ReplayGroup]:
        """Return the outer groups of one receiving station."""
        return [group for group in self.groups if group.station == station]

    def asdict(self) -> typing.Dict[str, typing.Any]:
        return {
            'groups': [group.asdict() for group in self.groups],
            'messages': [message.asdict() for message in self.messages],
            'missing_fragments': self.missing_fragments,
            'missing_lines': self.missing_lines,
        }


def _parse_line(raw: typing.Union[bytes, str]) -> typing.Optional[NMEASentence]:
    data = raw.encode('utf-8') if isinstance(raw, str) else raw
    try:
        return NMEASentenceFactory.produce(data)
    except Exception:
        return None


def _make_line(sentence: NMEASentence, source: typing.Optional[str],
               line_number: int) -> ReplayLine:
    station = sentence.source_station
    group = sentence.group
    sentence.line_number = line_number
    sentence.source = source
    return ReplayLine(
        sentence=sentence,
        source=source,
        line_number=line_number,
        station=station,
        group_id=group.group_id if group is not None else None,
        group_sentence_num=group.sentence_num if group is not None else None,
        group_sentence_total=group.sentence_tot if group is not None else None,
    )


def assemble_messages(lines: typing.Sequence[ReplayLine]) -> typing.List[ReplayMessage]:
    """Assemble inner AIS messages from lines that already share a station.

    Fragments are matched by sequence number, channel, talker, type and count.
    Reusing any of those numbers for another message opens a new bucket, so
    equal sequence numbers never merge unrelated fragments.
    """
    messages: typing.List[ReplayMessage] = []
    single: typing.List[ReplayLine] = []
    multipart: typing.List[typing.Dict[str, typing.Any]] = []

    for line in lines:
        if not line.is_ais:
            continue
        sentence = typing.cast(AISSentence, line.sentence)
        if sentence.frag_cnt == 1:
            single.append(line)
            continue

        key = (sentence.seq_id, sentence.channel, sentence.talker_id, sentence.type, sentence.frag_cnt)
        bucket = None
        for candidate in multipart:
            if candidate['key'] != key:
                continue
            fragments = candidate['fragments']
            if sentence.frag_num in fragments:
                # A repeated fragment belongs to no bucket; a later message
                # may legitimately reuse the same sequence number.
                continue
            # Prefer the bucket that is waiting for exactly this fragment.
            # This keeps two messages with the same sequence number apart
            # even when their fragments are interleaved.
            if sentence.frag_num == 1 or candidate['expected'] == sentence.frag_num:
                bucket = candidate
                break
        if bucket is None:
            bucket = {'key': key, 'lines': [], 'fragments': set(), 'expected': 1}
            multipart.append(bucket)
        bucket['lines'].append(line)
        bucket['fragments'].add(sentence.frag_num)
        bucket['expected'] = sentence.frag_num + 1

    for line in single:
        sentence = typing.cast(AISSentence, line.sentence)
        messages.append(ReplayMessage(
            sentences=[sentence],
            station=line.station,
            group_id=line.group_id,
            line_numbers=(line.line_number,),
            message=sentence,
            origins=(line.origin,),
        ))

    for bucket in multipart:
        bucket_lines: typing.List[ReplayLine] = bucket['lines']
        first = typing.cast(AISSentence, bucket_lines[0].sentence)
        total = first.frag_cnt
        present = {typing.cast(AISSentence, line.sentence).frag_num for line in bucket_lines}
        missing = tuple(number for number in range(1, total + 1) if number not in present)
        ordered = sorted(bucket_lines, key=lambda item: typing.cast(AISSentence, item.sentence).frag_num)
        sentences = [typing.cast(AISSentence, line.sentence) for line in ordered]
        assembled = None if missing else AISSentence.assemble_from_iterable(sentences)
        messages.append(ReplayMessage(
            sentences=sentences,
            station=first_station(bucket_lines),
            group_id=bucket_lines[0].group_id,
            line_numbers=tuple(line.line_number for line in ordered),
            missing_fragments=missing,
            message=assembled,
            origins=tuple(line.origin for line in ordered),
        ))

    messages.sort(key=lambda message: (_origin_sort_key(message.origins), message.key))
    return messages


def first_station(lines: typing.Sequence[ReplayLine]) -> typing.Optional[str]:
    for line in lines:
        if line.station is not None:
            return line.station
    return None


def _origin_sort_key(origins: typing.Sequence[Origin]) -> typing.Tuple[typing.Any, ...]:
    return tuple((source or '', number) for source, number in origins)


class ReplayParser:
    """Incrementally parse lines from one or more recordings.

    Feed lines with :meth:`feed`; call :meth:`finish` once all input has been
    consumed. Lines may come from several files and in any file order. Outer
    groups are keyed by receiving station and group id, inner messages by the
    station/group plus the AIS fragment key.
    """

    def __init__(self) -> None:
        self.lines: typing.List[ReplayLine] = []
        self._groups: typing.Dict[typing.Tuple[typing.Optional[str], int], typing.Dict[str, typing.Any]] = {}
        self._completed: typing.List[ReplayGroup] = []
        self._standalone: typing.List[ReplayLine] = []
        self._finished = False
        self._input_line = 0

    def reset(self) -> None:
        self.lines.clear()
        self._groups.clear()
        self._completed.clear()
        self._standalone.clear()
        self._finished = False
        self._input_line = 0

    def feed(self, line: typing.Union[bytes, str], source: typing.Optional[str] = None,
             line_number: typing.Optional[int] = None) -> typing.Optional[ReplayLine]:
        """Feed one raw line. Returns its provenance when it could be parsed."""
        self._input_line += 1
        if line_number is None:
            line_number = self._input_line
        sentence = _parse_line(line)
        if sentence is None:
            return None
        parsed = _make_line(sentence, source, line_number)
        self.lines.append(parsed)

        if parsed.group_id is None:
            self._standalone.append(parsed)
            return parsed

        key = (parsed.scope, parsed.group_id)
        group = self._groups.get(key)
        if group is None:
            # Lines of one group may carry the ``s:`` station only on some of
            # them. Join the single compatible pending group of this id when
            # the sentence number is not already present and the station does
            # not contradict it.
            candidates = []
            for candidate_key, candidate in self._groups.items():
                if candidate_key[1] != parsed.group_id:
                    continue
                if any(member.group_sentence_num == parsed.group_sentence_num
                       for member in candidate['lines']):
                    continue
                other_station = candidate['station']
                stations_conflict = all((
                    parsed.station is not None,
                    other_station is not None,
                    parsed.station != other_station,
                ))
                if stations_conflict:
                    continue
                if parsed.station is None and parsed.source is not None:
                    sources = {member.source for member in candidate['lines']}
                    if parsed.source not in sources:
                        continue
                candidates.append((candidate_key, candidate))
            if len(candidates) == 1:
                key, group = candidates[0]
                if parsed.station is not None and group['station'] is None:
                    group['station'] = parsed.station
                    group['scope'] = parsed.station
                    del self._groups[key]
                    key = (parsed.station, parsed.group_id)
                    self._groups[key] = group
            else:
                group = self._new_group(parsed)
                self._groups[key] = group

        if parsed.group_sentence_total is not None:
            group['sentence_total'] = parsed.group_sentence_total

        # A repeated first sentence starts a new incarnation of a reused id.
        repeats_start = (parsed.group_sentence_num == 1 and any(
            member.group_sentence_num == 1 for member in group['lines']))
        if repeats_start:
            self._completed.append(self._finalize_group(key, group))
            group = self._new_group(parsed)
            self._groups[key] = group

        if not any(member.group_sentence_num == parsed.group_sentence_num for member in group['lines']):
            group['lines'].append(parsed)

        received = {member.group_sentence_num for member in group['lines']}
        if all(number in received for number in range(1, group['sentence_total'] + 1)):
            self._completed.append(self._finalize_group(key, group))
        return parsed

    @staticmethod
    def _new_group(line: ReplayLine) -> typing.Dict[str, typing.Any]:
        return {
            'station': line.station,
            'scope': line.scope,
            'group_id': line.group_id,
            'sentence_total': line.group_sentence_total or 1,
            'lines': [],
        }

    def _finalize_group(self, key: typing.Tuple[typing.Optional[str], int],
                        group: typing.Dict[str, typing.Any]) -> ReplayGroup:
        lines: typing.List[ReplayLine] = sorted(
            group['lines'],
            key=lambda line: (line.group_sentence_num or 0, _origin_sort_key((line.origin,))),
        )
        received = {line.group_sentence_num for line in lines}
        missing = tuple(number for number in range(1, group['sentence_total'] + 1)
                        if number not in received)
        messages = assemble_messages(lines)
        result = ReplayGroup(
            station=group['station'],
            group_id=group['group_id'],
            sentence_total=group['sentence_total'],
            lines=lines,
            messages=messages,
            missing_lines=missing,
        )
        self._groups.pop(key, None)
        return result

    def finish(self) -> ReplayResult:
        """Finalize incomplete groups and return all recovered data."""
        groups: typing.List[ReplayGroup] = list(self._completed)
        for key in list(self._groups):
            groups.append(self._finalize_group(key, self._groups[key]))
        self._groups.clear()

        # Untagged lines are still scoped by station ("source" when no tag
        # block is present), otherwise two recordings could fill each other.
        standalone: typing.List[ReplayMessage] = []
        by_station: typing.Dict[typing.Optional[str], typing.List[ReplayLine]] = {}
        for line in self._standalone:
            by_station.setdefault(line.scope, []).append(line)
        for station_lines in by_station.values():
            standalone.extend(assemble_messages(station_lines))

        groups.sort(key=lambda group: (group.station or '', group.group_id,
                                       _origin_sort_key(group.origins)))
        standalone.sort(key=lambda message: (message.station or '', _origin_sort_key(message.origins)))
        messages = [message for group in groups for message in group.messages] + standalone
        messages.sort(key=lambda message: (message.station or '', message.group_id if message.group_id is not None else -1,
                                           _origin_sort_key(message.origins)))
        self._finished = True
        return ReplayResult(groups=groups, messages=messages, standalone=standalone, lines=list(self.lines))


def parse_replay(lines: typing.Iterable[LineInput], *,
                 source: typing.Optional[str] = None) -> ReplayResult:
    """Parse an iterable of raw lines and return nested replay data.

    Items may be ``bytes``/``str`` or ``(source, line_number, line)`` tuples,
    which is convenient when replaying several station files at once.
    """
    parser = ReplayParser()
    for item in lines:
        if isinstance(item, tuple):
            item_source, item_number, item_line = item
            parser.feed(item_line, source=item_source, line_number=item_number)
        else:
            parser.feed(item, source=source)
    return parser.finish()


def read_replay(paths: typing.Union[str, pathlib.Path, typing.Iterable[typing.Union[str, pathlib.Path]]], *,
                encoding: str = 'utf-8') -> ReplayResult:
    """Read one or more recordings, keeping per-file line numbers.

    Pass the station files in any order: groups are attributed by the tag
    block station, not by the order in which the files were read.
    """
    if isinstance(paths, (str, pathlib.Path)):
        paths = [paths]
    parser = ReplayParser()
    for path in paths:
        name = str(path)
        with open(path, 'rb') as handle:
            for number, raw in enumerate(handle, 1):
                parser.feed(raw, source=name, line_number=number)
    return parser.finish()
