import selectors
import time
import typing
import pathlib
import queue
from collections import deque
from dataclasses import dataclass
from abc import ABC, abstractmethod
from socket import AF_INET, SO_REUSEADDR, SOCK_DGRAM, SOCK_STREAM, SOL_SOCKET, socket
from typing import BinaryIO, Generator, Generic, Iterable, List, TypeVar, cast

from pyais.exceptions import InvalidNMEAMessageException, UnknownMessageException
from pyais.messages import AISSentence, GatehouseSentence, NMEAMessage, NMEASentence, NMEASentenceFactory

T = TypeVar("T")
F = TypeVar("F", BinaryIO, socket, None)
DOLLAR_SIGN = ord("$")
EXCLAMATION_POINT = ord("!")
BACKSLASH = ord("\\")


def should_parse(byte_str: bytes) -> bool:
    """Return True if a given byte string seems to be NMEA message.
    This method does **NOT** validate the message, but uses a heuristic
    approach to check (or guess) if byte string is a valid nmea_message.
    """
    # The byte sequence is not empty and starts with a $ or a ! or \
    return len(byte_str) > 0 and byte_str[0] in (DOLLAR_SIGN, EXCLAMATION_POINT, BACKSLASH)


class PreprocessorProtocol(typing.Protocol):
    """
    Protocol for preprocessing lines of bytes into NMEA0183 format.

    This protocol must be implemented to support various message formats in the pyais library.
    Implementing classes should provide a `process` method that transforms input lines of bytes
    into NMEA0183 compliant messages.

    Methods:
    --------
    process(line: bytes) -> bytes:
        Processes an input line of bytes and returns it in NMEA0183 format.
    """

    def process(self, line: bytes) -> bytes:
        """
        Process an input line of bytes and return it in NMEA0183 format.

        Parameters:
        -----------
        line : bytes
            The input line of bytes to be processed.

        Returns:
        --------
        bytes
            The processed line in bytes, conforming to the NMEA0183 format.
        """
        pass


class TagBlockGroupResult(list):  # type: ignore[type-arg]
    """The list of sentences of one outer tag block group.

    It behaves like the plain list that :class:`TagBlockQueue` used to return,
    but also records which receiving station and group the sentences belong
    to, their original line numbers and which group sentences never arrived.
    """

    def __init__(
        self,
        sentences: typing.Iterable[NMEASentence],
        station: typing.Optional[str] = None,
        group_id: typing.Optional[int] = None,
        sentence_total: typing.Optional[int] = None,
        sentence_numbers: typing.Optional[typing.Iterable[int]] = None,
        line_numbers: typing.Optional[typing.Iterable[typing.Optional[int]]] = None,
        missing_lines: typing.Iterable[int] = (),
    ) -> None:
        super().__init__(sentences)
        self.station = station
        self.group_id = group_id
        self.sentence_total = sentence_total
        self.sentence_numbers = tuple(sentence_numbers or ())
        self.line_numbers = tuple(line_numbers or ())
        self.missing_lines = tuple(missing_lines)

    @property
    def complete(self) -> bool:
        return not self.missing_lines

    @property
    def missing_sentence_numbers(self) -> typing.Tuple[int, ...]:
        return self.missing_lines

    @property
    def messages(self) -> typing.List[typing.Any]:
        """The inner AIS messages assembled from this group's sentences.

        Complete messages remain decodable even when the group itself is
        missing a line. Uses the same matching rules as the replay layer.
        """
        from pyais.replay import ReplayLine, assemble_messages
        lines = [
            ReplayLine(
                sentence=sentence,
                source=getattr(sentence, 'source', None),
                line_number=getattr(sentence, 'line_number', 0) or 0,
                station=self.station,
                group_id=self.group_id,
                group_sentence_num=getattr(sentence, 'group_sentence_num', None),
                group_sentence_total=self.sentence_total,
            )
            for sentence in self
        ]
        return assemble_messages(lines)

    @property
    def complete_messages(self) -> typing.List[typing.Any]:
        return [message for message in self.messages if message.complete]

    @property
    def incomplete_messages(self) -> typing.List[typing.Any]:
        return [message for message in self.messages if not message.complete]

    @property
    def missing_fragments(self) -> typing.Dict[typing.Any, typing.Tuple[int, ...]]:
        return {
            (message.station, message.group_id, message.line_numbers): message.missing_fragments
            for message in self.incomplete_messages
        }


class TagBlockQueue(queue.Queue):  # type: ignore

    def __init__(self, maxsize: int = 0) -> None:
        # Outer groups are scoped by receiving station, so two stations that
        # reuse the same group id never exchange sentences.
        self.groups: typing.Dict[typing.Tuple[typing.Optional[str], int], typing.Dict[str, object]] = {}
        super().__init__(maxsize)

    @staticmethod
    def _scope_of(sentence: NMEASentence, station: typing.Optional[str]) -> typing.Optional[str]:
        if station is not None:
            return station
        return getattr(sentence, 'source', None)

    def put_sentence(self, sentence: NMEASentence) -> None:
        if not sentence.tag_block:
            # No NMEA 4.10 tag block
            # Returns a single line immediately.
            super().put([sentence,])
            return

        tb = sentence.tag_block
        tb.init()

        if not tb.group:
            # No NMEA 4.10 tag block 'g'.
            super().put([sentence,])
            return

        station = typing.cast(typing.Optional[str], tb.source_station)
        scope = self._scope_of(sentence, station)
        key = (scope, tb.group.group_id)
        sentence_total = int(tb.group.sentence_tot)
        sentence_num = int(tb.group.sentence_num)

        if sentence_total == 1:
            # Group of a single sentence. It is complete on its own, but still
            # carries its outer-group attribution.
            super().put(TagBlockGroupResult(
                [sentence],
                station=station,
                group_id=tb.group.group_id,
                sentence_total=1,
                sentence_numbers=[sentence_num],
                line_numbers=[typing.cast(typing.Optional[int], getattr(sentence, 'line_number', None))],
            ))
            return
        group = self.groups.get(key)

        if group is None:
            # A sentence without an ``s:`` station still belongs to the one
            # group of that id if that group is unambiguous. A repeated
            # sentence number is a new incarnation, and an explicitly tagged
            # sentence never joins a group of another station.
            candidates: typing.List[typing.Tuple[typing.Tuple[typing.Optional[str], int], typing.Dict[str, object]]] = []
            for candidate_key, candidate in self.groups.items():
                if candidate_key[1] != tb.group.group_id:
                    continue
                candidate_members: typing.Dict[int, NMEASentence] = candidate['sentences']  # type: ignore
                if sentence_num in candidate_members:
                    continue
                other_station = typing.cast(typing.Optional[str], candidate['station'])
                if station is not None and other_station is not None and station != other_station:
                    continue
                if station is None and scope is not None:
                    sources = {getattr(line, 'source', None) for line in candidate_members.values()}
                    if scope not in sources:
                        continue
                candidates.append((candidate_key, candidate))

            if len(candidates) == 1:
                key, group = candidates[0]
                if station is not None and group['station'] is None:
                    group['station'] = station
                    group['scope'] = station
                    del self.groups[key]
                    key = (station, tb.group.group_id)
                    self.groups[key] = group
                elif station is None:
                    station = typing.cast(typing.Optional[str], group['station'])
                    scope = typing.cast(typing.Optional[str], group['scope'])

        if group is None:
            group = {
                'station': station,
                'scope': scope,
                'group_id': tb.group.group_id,
                'sentence_total': sentence_total,
                'sentences': {},
            }
            self.groups[key] = group
        else:
            group['sentence_total'] = sentence_total

        members: typing.Dict[int, NMEASentence] = group['sentences']  # type: ignore
        # A repeated sentence number starts a new incarnation of a reused id.
        # Emit the old incarnation first: its complete messages still count.
        if sentence_num == 1 and 1 in members:
            self._put_group(key, group)
            group = {
                'station': station,
                'scope': scope,
                'group_id': tb.group.group_id,
                'sentence_total': sentence_total,
                'sentences': {},
            }
            self.groups[key] = group
            members = group['sentences']  # type: ignore

        members.setdefault(sentence_num, sentence)

        if sentence_total != len(members):
            return

        self._put_group(key, group)
        del self.groups[key]

    def _put_group(self,
                   key: typing.Tuple[typing.Optional[str], int],
                   group: typing.Dict[str, object]) -> TagBlockGroupResult:
        members: typing.Dict[int, NMEASentence] = group['sentences']  # type: ignore
        sentence_total = int(group['sentence_total'])  # type: ignore
        # Preserve arrival order, as the queue always has.
        numbers = list(members)
        missing = tuple(n for n in range(1, sentence_total + 1) if n not in members)
        result = TagBlockGroupResult(
            (members[n] for n in numbers),
            station=group['station'],  # type: ignore
            group_id=group['group_id'],  # type: ignore
            sentence_total=sentence_total,
            sentence_numbers=numbers,
            line_numbers=[typing.cast(typing.Optional[int], getattr(members[n], 'line_number', None)) for n in numbers],
            missing_lines=missing,
        )
        # Keep the historical return type: a list of NMEASentence objects.
        super().put(result)
        return result

    def flush(self) -> typing.List[TagBlockGroupResult]:
        """Enqueue every pending group, complete messages included.

        This is meant for end-of-recording cleanup. A group that is missing
        one line is still emitted: callers can decode the complete inner
        messages it contains and inspect ``missing_lines`` for the rest.
        The returned list holds the emitted groups in input order.
        """
        results: typing.List[TagBlockGroupResult] = []
        for key in list(self.groups):
            results.append(self._put_group(key, self.groups[key]))
            del self.groups[key]
        return results


class NMEAAssembler:
    """Turns raw lines into complete AIS sentences, one line at a time.

    Call ``feed()`` for every line as it arrives. It returns the complete
    sentence once a single-line message or the last fragment of a multipart
    message has been fed, and ``None`` otherwise. Pending fragments are kept
    between calls, so lines can come from any source: a callback, an event
    loop or batches read at different times.

    Multipart messages are only assembled from fragments that arrive in order,
    starting with fragment 1. Orphaned or out-of-order fragments are dropped
    instead of being combined with unrelated fragments.

    Instances are not thread-safe. Use one assembler per input stream.
    """

    def __init__(self, tbq: typing.Optional[TagBlockQueue] = None) -> None:
        self.wrapper_msg: typing.Optional[GatehouseSentence] = None
        self.tbq: typing.Optional[TagBlockQueue] = tbq
        self.buffer: typing.Dict[typing.Tuple[typing.Any, ...], typing.List[typing.Optional[NMEAMessage]]] = {}
        # Provenance of buffered fragments, keyed like ``buffer``.
        self.origins: typing.Dict[typing.Tuple[typing.Any, ...], typing.List[typing.Optional[typing.Tuple[typing.Optional[str], int]]]] = {}
        self._line_number = 0
        self._source: typing.Optional[str] = None

    def reset(self) -> None:
        """Discard pending fragments and any pending Gatehouse wrapper,
        e.g. after the underlying connection was interrupted."""
        self.buffer.clear()
        self.origins.clear()
        self.wrapper_msg = None

    def __set_last_wrapper_msg(self, wrapper_msg: GatehouseSentence) -> None:
        self.wrapper_msg = wrapper_msg

    def __get_last_wrapper_msg(self) -> typing.Optional[GatehouseSentence]:
        wrapper_msg = self.wrapper_msg
        self.wrapper_msg = None
        return wrapper_msg

    def __insert_wrapper_msg(self, msg: AISSentence) -> AISSentence:
        wrapper_msg = self.__get_last_wrapper_msg()
        if wrapper_msg:
            msg.wrapper_msg = wrapper_msg
        return msg

    def __add_to_tbq(self, sentence: NMEASentence) -> None:
        if not self.tbq:
            # Tag Block Queue not defined. Do nothing.
            return
        self.tbq.put_sentence(sentence)

    def feed(self, line: bytes, source: typing.Optional[str] = None,
             line_number: typing.Optional[int] = None) -> typing.Optional[NMEAMessage]:
        """Feed one raw line. Returns a complete sentence or None.

        ``source`` and ``line_number`` are optional provenance for replayed
        recordings. They are attached to the parsed sentence and to an
        assembled multipart message, and they take part in the buffer key so
        that two stations sharing a sequence number cannot exchange fragments.
        """
        buffer = self.buffer
        origins = self.origins
        msg: AISSentence

        if line_number is None:
            self._line_number += 1
            line_number = self._line_number
        if source is not None:
            self._source = source

        try:
            sentence = NMEASentenceFactory.produce(line)
            sentence.line_number = line_number
            if sentence.source is None:
                sentence.source = self._source
            self.__add_to_tbq(sentence)
            if sentence.TYPE == GatehouseSentence.TYPE:
                sentence = typing.cast(GatehouseSentence, sentence)
                self.__set_last_wrapper_msg(sentence)
                return None
        except (InvalidNMEAMessageException, UnknownMessageException):
            # Be gentle and just skip invalid messages
            return None

        if not sentence.TYPE == AISSentence.TYPE:
            return None
        msg = typing.cast(AISSentence, sentence)

        if msg.is_single:
            return self.__insert_wrapper_msg(msg)

        # Instead of None use -1 as a seq_id
        seq_id = msg.seq_id
        if seq_id is None:
            seq_id = -1

        # A unique stream is identified by the following tuple. The station
        # (tag block ``s:`` or recording source) is part of it: sequence
        # numbers are only unique per station.
        slot = (msg.station, seq_id, msg.channel, msg.talker_id, msg.type, msg.frag_cnt)

        if msg.frag_num == 1:
            # start (overwrites any pending)
            buffer[slot] = [msg] + [None] * (msg.frag_cnt - 1)
            origins[slot] = [(source, line_number)] + [None] * (msg.frag_cnt - 1)
        elif slot in buffer:
            if msg.frag_num == sum(p is not None for p in buffer[slot]) + 1:
                # strictly contiguous
                buffer[slot][msg.frag_num - 1] = msg
                origins[slot][msg.frag_num - 1] = (source, line_number)
            else:
                # drop out-of-order fragment
                return None
        else:
            # orphan fragment (no start seen)
            return None

        # Check if all fragments are found
        not_none_parts = [m for m in buffer[slot] if m is not None]
        if len(not_none_parts) < msg.frag_cnt:
            return None

        fragment_origins = [o for o in origins[slot] if o is not None]
        del buffer[slot]
        del origins[slot]
        msg = NMEAMessage.assemble_from_iterable(not_none_parts)
        if any(number is not None for _, number in fragment_origins):
            msg.line_numbers = [number for _, number in fragment_origins]
        return self.__insert_wrapper_msg(msg)


class AssembleMessages(ABC):
    """
    Base class that assembles multiline messages.
    Offers a iterator like interface.
    """

    def __init__(self, tbq: typing.Optional[TagBlockQueue] = None) -> None:
        # Owned by the stream rather than by one iteration, so fragments
        # survive between next() calls.
        self.tbq = tbq
        self.assembler = NMEAAssembler(tbq)
        self._line_number = 0
        self._source: typing.Optional[str] = None

    def __enter__(self) -> "AssembleMessages":
        # Enables use of with statement
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        return None

    def __iter__(self) -> Generator[NMEAMessage, None, None]:
        return self._assemble_messages()

    def __next__(self) -> NMEAMessage:
        """Returns the next decoded NMEA message."""
        return next(iter(self))

    def _assemble_messages(self) -> Generator[NMEAMessage, None, None]:
        for source, line in self._iter_sources():
            self._line_number += 1
            msg = self.assembler.feed(line, source=source, line_number=self._line_number)
            if msg is not None:
                yield msg

    def _iter_sources(self) -> Generator[typing.Tuple[typing.Optional[str], bytes], None, None]:
        """Yield ``(source, line)`` pairs. Default source is ``None``."""
        for line in self._iter_messages():
            yield self._source, line

    @abstractmethod
    def _iter_messages(self) -> Generator[bytes, None, None]:
        raise NotImplementedError("Implement me!")

    def feed_replay(self, lines: typing.Iterable[typing.Tuple[typing.Optional[str], int, bytes]]) -> typing.List[NMEAMessage]:
        """Feed ``(source, line_number, raw)`` tuples and return all finished messages.

        Line numbers are taken from the tuples instead of from the stream
        position, so the result is independent of the order in which several
        recordings are read.
        """
        finished: typing.List[NMEAMessage] = []
        for source, line_number, line in lines:
            msg = self.assembler.feed(line, source=source, line_number=line_number)
            if msg is not None:
                finished.append(msg)
        return finished


class IterMessages(AssembleMessages):

    def __init__(self, messages: Iterable[bytes], tbq: typing.Optional[TagBlockQueue] = None):
        super().__init__(tbq=tbq)
        # If the user passes a single byte string make it into a list
        if isinstance(messages, bytes):
            messages = [messages, ]
        self.messages: Iterable[bytes] = messages

    @classmethod
    def from_strings(cls, messages: Iterable[str], ignore_encoding_errors: bool = False,
                     encoding: str = "utf-8") -> "IterMessages":
        # If the users passes a single message as string, make it a list
        if isinstance(messages, str):
            messages = [messages, ]

        encoded: List[bytes] = []
        for message in messages:
            message = message.strip()
            if not message:
                continue
            try:
                encoded.append(message.encode(encoding))
            except UnicodeEncodeError as e:
                if ignore_encoding_errors:
                    # Just skip and carry on
                    continue
                raise e

        return IterMessages(encoded)

    def _iter_messages(self) -> Generator[bytes, None, None]:
        # Transform self.messages into a generator
        yield from (message for message in self.messages)

    def _iter_sources(self) -> Generator[typing.Tuple[typing.Optional[str], bytes], None, None]:
        # Items may be (source, line_number, line) tuples for replay work.
        source = self._source
        for item in self.messages:
            if isinstance(item, tuple) and len(item) == 3:
                item_source, line_number, line = item
                source = item_source
                self._line_number = line_number - 1
                yield source, line
            else:
                yield source, item


class Stream(AssembleMessages, Generic[F], ABC):

    def __init__(
        self, fobj: F,
        preprocessor: typing.Optional[PreprocessorProtocol] = None,
        tbq: typing.Optional[TagBlockQueue] = None
    ) -> None:
        """
        Create a new Stream-like object.
        @param fobj: A file-like or socket object.
        @param preprocessor: An optional preprocessor
        """
        super().__init__(tbq=tbq)
        self._fobj: F = fobj
        self.preprocessor = preprocessor

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    def _iter_messages(self) -> Generator[bytes, None, None]:
        # Do not parse lines, that are obviously not NMEA messages
        for line in self.read():
            if len(line) <= 10:
                continue
            if self.preprocessor is not None:
                line = self.preprocessor.process(line)
            if should_parse(line):
                yield line

    def _iter_sources(self) -> Generator[typing.Tuple[typing.Optional[str], bytes], None, None]:
        # Count physical lines, so line numbers refer to the original file
        # even when comments or garbage lines are skipped.
        for number, line in enumerate(self.read(), 1):
            if len(line) <= 10:
                continue
            if self.preprocessor is not None:
                line = self.preprocessor.process(line)
            if should_parse(line):
                self._line_number = number - 1
                yield self._source, line

    def close(self) -> None:
        if self._fobj is not None:
            self._fobj.close()

    @abstractmethod
    def read(self) -> Generator[bytes, None, None]:
        raise NotImplementedError()


class BinaryIOStream(Stream[BinaryIO]):
    """Read messages from a file-like object"""

    def __init__(
        self,
        file: BinaryIO,
        preprocessor: typing.Optional[PreprocessorProtocol] = None,
        tbq: typing.Optional[TagBlockQueue] = None
    ) -> None:
        super().__init__(file, preprocessor=preprocessor, tbq=tbq)

    def read(self) -> Generator[bytes, None, None]:
        yield from self._fobj


class FileReaderStream(BinaryIOStream):
    """
    Read NMEA messages from file
    """

    def __init__(
        self,
        filename: typing.Union[str, pathlib.Path],
        mode: str = "rb",
        preprocessor: typing.Optional[PreprocessorProtocol] = None,
        tbq: typing.Optional[TagBlockQueue] = None
    ) -> None:
        self.filename: typing.Union[str, pathlib.Path] = filename
        self.mode: str = mode
        # Try to open file
        try:
            file = open(self.filename, mode=self.mode)
            file = cast(BinaryIO, file)
        except Exception as e:
            raise FileNotFoundError(f"Could not open file {self.filename}") from e
        super().__init__(file, preprocessor=preprocessor, tbq=tbq)
        # Untagged lines are scoped by recording, never mixed across files.
        self._source = str(self.filename)


class ByteStream(Stream[None]):
    """
    Takes a iterable that contains ais messages as bytes and assembles them.
    """

    def __init__(
        self,
        iterable: Iterable[bytes],
        preprocessor: typing.Optional[PreprocessorProtocol] = None,
        tbq: typing.Optional[TagBlockQueue] = None
    ) -> None:
        self.iterable: Iterable[bytes] = iterable
        # Keep each byte stream isolated from other byte streams.
        self._source = f'byte-stream-{id(self)}'
        super().__init__(None, preprocessor=preprocessor, tbq=tbq)

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        return

    def read(self) -> Generator[bytes, None, None]:
        yield from self.iterable


class SocketStream(Stream[socket]):
    BUF_SIZE = 4096
    MAX_PARTIAL_SIZE = 16384

    def recv(self) -> bytes:
        return self._fobj.recv(self.BUF_SIZE)

    def read(self) -> Generator[bytes, None, None]:
        partial: bytes = b''
        while True:
            body = self.recv()

            # Server closed connection.
            # Drop unterminated final line.
            if not body:
                return None

            # Prevent unbounded growth of partial
            if len(partial) > self.MAX_PARTIAL_SIZE:
                partial = b''
                _, sep, body = body.partition(b'\n')
                if not sep or not body:
                    continue

            lines = (partial + body).splitlines(keepends=True)

            if lines[-1].endswith(b'\n'):
                # all lines are complete
                yield from lines
                partial = b''
            else:
                # the last line was only partially received
                yield from lines[:-1]
                partial = lines[-1]


class UDPReceiver(SocketStream):

    def __init__(
        self,
        host: str,
        port: int,
        preprocessor: typing.Optional[PreprocessorProtocol] = None,
        tbq: typing.Optional[TagBlockQueue] = None
    ) -> None:
        sock: socket = socket(AF_INET, SOCK_DGRAM)
        sock.bind((host, port))
        super().__init__(sock, preprocessor=preprocessor, tbq=tbq)

    def recv(self) -> bytes:
        return self._fobj.recvfrom(self.BUF_SIZE)[0]


class TCPConnection(SocketStream):
    """
    Read AIS data from a remote TCP server
    https://en.wikipedia.org/wiki/NMEA_0183
    """

    def recv(self) -> bytes:
        return self._fobj.recv(self.BUF_SIZE)

    def __init__(
        self,
        host: str,
        port: int = 80,
        preprocessor: typing.Optional[PreprocessorProtocol] = None,
        tbq: typing.Optional[TagBlockQueue] = None
    ) -> None:
        sock: socket = socket(AF_INET, SOCK_STREAM)
        try:
            sock.connect((host, port))
        except ConnectionRefusedError as e:
            sock.close()
            raise ConnectionRefusedError(f"Failed to connect to {host}:{port}") from e
        super().__init__(sock, preprocessor=preprocessor, tbq=tbq)


@dataclass
class ClientConnection:
    """Represents a connected client with its associated data."""
    addr: tuple[str, int]
    partial_buffer: bytes = b''


class TCPServer(SocketStream):
    """
    This is a TCP server capable of handling multiple concurrent client connections.
    Built on Python's selectors module for efficient I/O multiplexing,
    it accepts connections on a specified host and port, manages per-client buffering
    to handle partial messages, and queues complete messages from all clients for processing.
    The server automatically handles connection lifecycle management, including accepting
    new connections, processing client data with proper message boundary detection,
    and cleaning up closed connections, making it suitable for applications requiring
    concurrent TCP communication with multiple clients.

    Args:
        host:           The hostname or IP address to bind to.
        port:           The port number to bind to. Defaults to 80.
        preprocessor:   Optional preprocessor for handling incoming data.
                        If None, no preprocessing will be applied.
        tbq:            Optional tag block queue for managing tagged data blocks.
                        If None, no tag block queuing will be used.
        timeout:        Connection timeout in seconds. Use -1 for no timeout.
                        Defaults to -1.
    """

    def __init__(
        self,
        host: str,
        port: int = 80,
        preprocessor: typing.Optional[PreprocessorProtocol] = None,
        tbq: typing.Optional[TagBlockQueue] = None,
        timeout: float = -1
    ) -> None:
        # Create a socket and bind it
        server_sock = socket(AF_INET, SOCK_STREAM)
        server_sock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
        server_sock.bind((host, port))
        server_sock.listen()
        server_sock.setblocking(False)

        # Use the builtin selector for efficient I/O multiplexing
        self.sel = selectors.DefaultSelector()
        self.sel.register(server_sock, selectors.EVENT_READ)

        # Queue for complete messages from all clients
        self._message_queue: deque[bytes] = deque()

        # Keep track of client sockets to clean them up later
        self._client_sockets: typing.Set[socket] = set()
        self._client_last_activity: typing.Dict[socket, float] = {}
        self.timeout = timeout

        super().__init__(server_sock, preprocessor=preprocessor, tbq=tbq)

    def recv(self) -> bytes:
        """Override recv to handle multiple client connections with per-connection buffering"""
        # First, return any queued complete messages
        if self._message_queue:
            return self._message_queue.popleft()

        last_cleanup = time.time()
        while True:
            for key, mask in self.sel.select(timeout=1.0):
                if key.data is None:
                    self.accept(key.fileobj)  # type: ignore
                else:
                    self.service(key, mask)
                    # Check if we have any complete messages after processing
                    if self._message_queue:
                        return self._message_queue.popleft()

            # Periodic cleanup for connection timeouts
            if self.timeout > 0 and time.time() - last_cleanup >= self.timeout:
                self._cleanup_idle_connections()
                last_cleanup = time.time()

    def _cleanup_idle_connections(self) -> None:
        current_time = time.time()
        for sock, last_activity in self._client_last_activity.copy().items():
            if current_time - last_activity > self.timeout:
                self._close_client(sock)

    def _close_client(self, client_sock: socket) -> None:
        try:
            self.sel.unregister(client_sock)
        except (KeyError, ValueError):
            pass
        self._client_last_activity.pop(client_sock, None)
        self._client_sockets.discard(client_sock)
        client_sock.close()

    def read(self) -> typing.Generator[bytes, None, None]:
        """Use custom implementation that handles multiple clients properly"""
        try:
            while True:
                data = self.recv()
                if data:
                    yield data
        finally:
            self.sel.close()

    def accept(self, sock: socket) -> None:
        client_sock, addr = sock.accept()
        client_sock.setblocking(False)
        self._client_sockets.add(client_sock)

        data = ClientConnection(
            addr=addr,
            partial_buffer=b''
        )
        self.sel.register(client_sock, selectors.EVENT_READ, data=data)
        self._client_last_activity[client_sock] = time.time()

    def service(self, key: selectors.SelectorKey, mask: int) -> None:
        """Handle client data with per-connection buffering"""
        client_sock: socket = typing.cast(socket, key.fileobj)
        data = key.data

        if mask & selectors.EVENT_READ:
            try:
                recv_data = client_sock.recv(self.BUF_SIZE)
                if recv_data:
                    # Process the received data with the connection's buffer
                    self._process_client_data(data, recv_data)
                else:
                    # close
                    self._close_client(client_sock)
            except ConnectionResetError:
                self._close_client(client_sock)

        self._client_last_activity[client_sock] = time.time()

    def _process_client_data(self, client_data: ClientConnection, new_data: bytes) -> None:
        """Process data from a specific client, handling partial messages"""
        # Prevent unbounded growth of partial
        if len(client_data.partial_buffer) > self.MAX_PARTIAL_SIZE:
            client_data.partial_buffer = b''
            new_data = new_data.partition(b'\n')[2]

        # Combine with any partial data from previous receives
        full_data = client_data.partial_buffer + new_data

        # Split into lines
        lines = full_data.splitlines(keepends=True)

        if not lines:
            return

        # Check if the last line is complete (ends with newline)
        if lines[-1].endswith(b'\n'):
            # All lines are complete
            for line in lines:
                self._message_queue.append(line)
            client_data.partial_buffer = b''
        else:
            # Last line is incomplete, save it for next time
            for line in lines[:-1]:
                self._message_queue.append(line)
            client_data.partial_buffer = lines[-1]

    def close(self) -> None:
        """Properly close all connections and selector"""
        for client_sock in self._client_sockets.copy():
            self._close_client(client_sock)
        self.sel.close()
        super().close()
