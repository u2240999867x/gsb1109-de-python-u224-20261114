"""
Reader and writer for can logging files in peak trc format

See https://www.peak-system.com/produktcd/Pdf/English/PEAK_CAN_TRC_File_Format.pdf
for file format description

Version 1.1 will be implemented as it is most commonly used
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from io import TextIOWrapper
from typing import Any, TextIO

from ..message import Message
from ..typechecking import StringPathLike
from ..util import channel2int, len2dlc
from .generic import TextIOMessageReader, TextIOMessageWriter

logger = logging.getLogger("can.io.trc")


@dataclass(frozen=True)
class TRCRecord(Mapping[str, Any]):
    """A successfully parsed data record from a TRC file.

    :param message: the decoded CAN message
    :param line_number: 1-based line number of the record in the source file
    :param line: the original source line with surrounding whitespace removed
    :param direction: the direction token read from the file (``"Rx"`` or
        ``"Tx"``), or ``None`` if the format does not carry a direction
    :param declared_length: the length value written in the record, i.e. the
        number of data bytes if an ``l`` column is declared, otherwise the
        ``L`` (DLC) column value
    :param data_bytes: the payload bytes actually present in the record
    :param file_version: the version active for the record's header block
    :param declared_columns: the column declaration active for the record
    """

    message: Message
    line_number: int
    line: str
    direction: str | None
    declared_length: int
    data_bytes: bytes
    file_version: TRCFileVersion | None = None
    declared_columns: tuple[str, ...] = ()

    @property
    def direction_is_rx(self) -> bool | None:
        """The direction as a boolean, or ``None`` if it is unavailable."""
        if self.direction is None:
            return None
        return self.direction == "Rx"

    @property
    def is_rx(self) -> bool | None:
        """Alias for :attr:`direction_is_rx`."""
        return self.direction_is_rx

    @property
    def declared_dlc(self) -> int:
        """The DLC carried by the decoded message."""
        return self.message.dlc

    @property
    def data(self) -> bytes:
        """Alias for :attr:`data_bytes`."""
        return self.data_bytes

    @property
    def length(self) -> int:
        """Alias for :attr:`declared_length`."""
        return self.declared_length

    @property
    def lineno(self) -> int:
        """Alias for :attr:`line_number`."""
        return self.line_number

    @property
    def source_line(self) -> str:
        """Alias for :attr:`line`."""
        return self.line

    @property
    def source_line_number(self) -> int:
        """Alias for :attr:`line_number`."""
        return self.line_number

    @property
    def payload(self) -> bytes:
        """Alias for :attr:`data_bytes`."""
        return self.data_bytes

    @property
    def actual_bytes(self) -> bytes:
        """Alias for :attr:`data_bytes`."""
        return self.data_bytes

    @property
    def data_length(self) -> int:
        """Number of actual payload bytes in the record."""
        return len(self.data_bytes)

    @property
    def actual_length(self) -> int:
        """Alias for :attr:`data_length`."""
        return len(self.data_bytes)

    @property
    def raw_line(self) -> str:
        """Alias for :attr:`line`."""
        return self.line

    def __getitem__(self, key: str) -> Any:
        if key in self.keys():
            return getattr(self, key)
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys())

    def __len__(self) -> int:
        return len(self.keys())

    @staticmethod
    def keys() -> tuple[str, ...]:
        return (
            "message",
            "line_number",
            "line",
            "direction",
            "declared_length",
            "data_bytes",
            "file_version",
            "declared_columns",
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the record fields as a plain dictionary."""
        return {
            "message": self.message,
            "line_number": self.line_number,
            "line": self.line,
            "direction": self.direction,
            "declared_length": self.declared_length,
            "data_bytes": self.data_bytes,
            "file_version": self.file_version,
            "declared_columns": self.declared_columns,
        }


@dataclass(frozen=True)
class TRCSegmentError:
    """A contiguous span of source lines that could not be interpreted.

    :param first_line: 1-based first affected line
    :param last_line: 1-based last affected line
    :param reason: short, stable cause identifier
    :param detail: human-readable explanation
    :param line: the offending declaration or record line, if applicable
    :param exception: the original exception if parsing raised one
    """

    first_line: int
    last_line: int
    reason: str
    detail: str
    line: str | None = None
    exception: Exception | None = field(default=None, repr=False)

    @property
    def start_line(self) -> int:
        """Alias for :attr:`first_line`."""
        return self.first_line

    @property
    def end_line(self) -> int:
        """Alias for :attr:`last_line`."""
        return self.last_line

    @property
    def start(self) -> int:
        """Alias for :attr:`first_line`."""
        return self.first_line

    @property
    def end(self) -> int:
        """Alias for :attr:`last_line`."""
        return self.last_line

    @property
    def lines(self) -> tuple[int, int]:
        """The affected line range as ``(first_line, last_line)``."""
        return (self.first_line, self.last_line)

    @property
    def span(self) -> tuple[int, int]:
        """Alias for :attr:`lines`."""
        return self.lines

    @property
    def kind(self) -> str:
        """Alias for :attr:`reason`."""
        return self.reason

    @property
    def message(self) -> str:
        """Alias for :attr:`detail`."""
        return self.detail

    def __contains__(self, line_number: object) -> bool:
        if not isinstance(line_number, int):
            return False
        return self.first_line <= line_number <= self.last_line

    def __str__(self) -> str:
        if self.first_line == self.last_line:
            location = f"line {self.first_line}"
        else:
            location = f"lines {self.first_line}-{self.last_line}"
        return f"{location}: {self.detail}"


class TRCFileVersion(Enum):
    UNKNOWN = 0
    V1_0 = 100
    V1_1 = 101
    V1_2 = 102
    V1_3 = 103
    V2_0 = 200
    V2_1 = 201

    def __ge__(self, other: Any) -> bool:
        if isinstance(other, TRCFileVersion):
            return self.value >= other.value
        return NotImplemented


class TRCReader(TextIOMessageReader):
    """
    Iterator of CAN messages from a TRC logging file.

    Column positions are taken from the ``;$COLUMNS`` declaration of the
    header block that precedes each group of records. The declaration may be
    reordered freely, including moving the direction column to the end; fields
    are matched by name, never by a fixed offset. A new ``;$FILEVERSION``
    directive after records starts a new shard with fresh header state, so a
    shard never inherits a neighbouring shard's version, start time or columns.

    Successfully parsed records are collected in :attr:`records`. Header
    blocks with duplicate or missing key columns, and malformed records, are
    reported as contiguous line ranges in :attr:`segment_errors`; records that
    were parsed before the affected segment are still yielded.
    """

    def __init__(
        self,
        file: StringPathLike | TextIO,
        **kwargs: Any,
    ) -> None:
        """
        :param file: a path-like object or as file-like object to read from
                     If this is a file-like object, is has to opened in text
                     read mode, not binary read mode.
        """
        super().__init__(file, mode="r")
        self.file_version = TRCFileVersion.UNKNOWN
        self._start_time: float | None = None
        self.columns: dict[str, int] = {}
        self._declared_columns: list[str] = []
        self._num_columns = -1

        if not self.file:
            raise ValueError("The given file cannot be None")

        self._parse_cols: Callable[
            [tuple[str, ...]], tuple[Message, str | None, int, bytes] | None
        ] = lambda x: None

        #: Successfully parsed records, in file order.
        self.records: list[TRCRecord] = []
        #: Contiguous line ranges that could not be interpreted.
        self.segment_errors: list[TRCSegmentError] = []

        self._line_number = 0
        self._shard_start_line = 1
        self._last_line_in_shard = 0
        self._header_seen = False
        self._header_closed = False
        self._has_data = False
        self._header_error: TRCSegmentError | None = None
        self._pending_record_error: TRCSegmentError | None = None
        self._finished = False

    @property
    def parse_errors(self) -> list[TRCSegmentError]:
        """Alias for :attr:`segment_errors`."""
        return self.segment_errors

    @property
    def errors(self) -> list[TRCSegmentError]:
        """Alias for :attr:`segment_errors`."""
        return self.segment_errors

    @property
    def record_count(self) -> int:
        """Number of successfully parsed records."""
        return len(self.records)

    @property
    def record_errors(self) -> list[TRCSegmentError]:
        """Alias for :attr:`segment_errors`."""
        return self.segment_errors

    @property
    def segments(self) -> list[TRCSegmentError]:
        """Alias for :attr:`segment_errors`."""
        return self.segment_errors

    @property
    def start_time(self) -> datetime | None:
        if self._start_time:
            return datetime.fromtimestamp(self._start_time, UTC)
        return None

    @staticmethod
    def _is_header_directive(line: str) -> bool:
        return line.startswith((";$FILEVERSION", ";$STARTTIME", ";$COLUMNS"))

    def _reset_shard(self, start_line: int) -> None:
        """Discard all header state so shards cannot borrow a neighbour's."""
        self.file_version = TRCFileVersion.UNKNOWN
        self._start_time = None
        self.columns = {}
        self._declared_columns = []
        self._num_columns = -1
        self._shard_start_line = start_line
        self._last_line_in_shard = start_line - 1
        self._header_seen = False
        self._header_closed = False
        self._has_data = False
        self._header_error = None
        self._parse_cols = lambda cols: None

    def _consume_header_line(self, line: str) -> None:
        self._header_seen = True
        if line.startswith(";$FILEVERSION"):
            logger.debug("TRCReader: Found file version '%s'", line)
            try:
                file_version = line.split("=", maxsplit=1)[1].strip()
            except IndexError:
                logger.debug("TRCReader: Failed to parse version")
                self._set_header_error(
                    "missing_version",
                    "the ;$FILEVERSION directive has no value",
                    line,
                )
                return
            versions = {
                "1.0": TRCFileVersion.V1_0,
                "1.1": TRCFileVersion.V1_1,
                "1.2": TRCFileVersion.V1_2,
                "1.3": TRCFileVersion.V1_3,
                "2.0": TRCFileVersion.V2_0,
                "2.1": TRCFileVersion.V2_1,
            }
            self.file_version = versions.get(file_version, TRCFileVersion.UNKNOWN)
        elif line.startswith(";$STARTTIME"):
            logger.debug("TRCReader: Found start time '%s'", line)
            try:
                self._start_time = (
                    datetime(1899, 12, 30, tzinfo=UTC)
                    + timedelta(days=float(line.split("=", maxsplit=1)[1]))
                ).timestamp()
            except (IndexError, ValueError):
                logger.debug("TRCReader: Failed to parse start time")
                self._set_header_error(
                    "invalid_start_time",
                    "the ;$STARTTIME value is not a valid timestamp",
                    line,
                )
        elif line.startswith(";$COLUMNS"):
            logger.debug("TRCReader: Found columns '%s'", line)
            try:
                declared = line.split("=", maxsplit=1)[1]
            except IndexError:
                logger.debug("TRCReader: Failed to parse columns")
                self._set_header_error(
                    "missing_column",
                    "the ;$COLUMNS directive has no value",
                    line,
                )
                return
            self._declared_columns = [
                column.strip() for column in declared.split(",") if column.strip()
            ]
            self.columns = {
                column: index for index, column in enumerate(self._declared_columns)
            }
            self._num_columns = len(self._declared_columns) - 1

    def _set_header_error(
        self, reason: str, detail: str, line: str | None = None
    ) -> None:
        if self._header_error is None:
            self._header_error = TRCSegmentError(
                first_line=self._shard_start_line,
                last_line=max(self._shard_start_line, self._line_number),
                reason=reason,
                detail=detail,
                line=line,
            )

    def _finish_header(self) -> None:
        if self._header_closed:
            return
        self._header_closed = True

        if self.file_version >= TRCFileVersion.V1_1 and self._start_time is None:
            self._set_header_error(
                "missing_start_time",
                "shard has no ;$STARTTIME information",
            )

        if self.file_version >= TRCFileVersion.V2_0:
            self._validate_v2_columns()
            if self._header_error is None:
                self._parse_cols = self._parse_cols_v2_x
        elif self.file_version == TRCFileVersion.UNKNOWN and self._declared_columns:
            # A ;$COLUMNS directive identifies the v2 layout even if the
            # vendor omitted ;$FILEVERSION.
            self.file_version = TRCFileVersion.V2_0
            self._validate_v2_columns()
            if self._header_error is None:
                self._parse_cols = self._parse_cols_v2_x
        elif self.file_version in {
            TRCFileVersion.V1_1,
            TRCFileVersion.V1_2,
            TRCFileVersion.V1_3,
        }:
            self._parse_cols = self._parse_cols_v1_x
        elif self.file_version == TRCFileVersion.V1_0:
            self._parse_cols = self._parse_msg_v1_0
        else:
            logger.info(
                "TRCReader: No file version was found, so version 1.0 is assumed"
            )
            self.file_version = TRCFileVersion.UNKNOWN
            self._parse_cols = self._parse_msg_v1_0

    def _validate_v2_columns(self) -> None:
        if not self._declared_columns:
            self._set_header_error(
                "missing_column",
                "shard has no ;$COLUMNS declaration",
            )
            return

        duplicates = sorted(
            {
                column
                for column in self._declared_columns
                if self._declared_columns.count(column) > 1
            }
        )
        if duplicates:
            self._set_header_error(
                "duplicate_column",
                "declared column names are not unique: " + ", ".join(duplicates),
            )
            return

        required = {"O", "T", "I", "d", "D"}
        missing = sorted(required.difference(self.columns))
        if "l" not in self.columns and "L" not in self.columns:
            missing.append("l/L")
        if missing:
            self._set_header_error(
                "missing_column",
                "declared columns are missing required field(s): "
                + ", ".join(missing),
            )
            return

    def _split_v2_fields(self, tokens: tuple[str, ...]) -> dict[str, Any]:
        """Match tokens to the declared columns for this shard's version.

        All columns consume exactly one token except ``D`` (data), which takes
        every token between the fixed-width columns declared before it and
        those declared after it. Those after ``D`` are matched from the end of
        the record, so an empty data field (for example a remote frame) does
        not shift a trailing direction token into ``D``. This keeps the result
        identical no matter where the declaration places the direction column.
        """
        if (
            tokens
            and self._is_direction(tokens[-1])
            and "d" in self.columns
            and self.columns["d"] < len(tokens)
            and not self._is_direction(tokens[self.columns["d"]])
        ):
            # A vendor moved only the direction value to the end without
            # updating the declaration. Match every other field as declared.
            declared = [
                column for column in self._declared_columns if column != "d"
            ]
            fields = self._match_declared_fields(tokens[:-1], declared)
            fields["d"] = tokens[-1]
            return fields
        return self._match_declared_fields(tokens, self._declared_columns)

    @staticmethod
    def _match_declared_fields(
        tokens: tuple[str, ...], declared: list[str]
    ) -> dict[str, Any]:
        try:
            data_position = declared.index("D")
        except ValueError:
            raise ValueError("no data column is declared") from None

        before = declared[:data_position]
        after = declared[data_position + 1 :]
        if len(tokens) < len(before) + len(after):
            raise ValueError("record has fewer fields than declared")

        fields: dict[str, Any] = {}
        for column, token in zip(before, tokens):
            fields[column] = token
        end = len(tokens) - len(after)
        fields["D"] = tokens[len(before) : end]
        for column, token in zip(after, tokens[end:]):
            fields[column] = token
        return fields

    @staticmethod
    def _parse_byte_tokens(tokens: Iterable[str]) -> bytes:
        return bytes(int(token, 16) for token in tokens)

    @staticmethod
    def _is_direction(token: str) -> bool:
        return token in ("Rx", "Tx")

    def _parse_msg_v1_0(
        self, cols: tuple[str, ...]
    ) -> tuple[Message, str | None, int, bytes] | None:
        arbit_id = cols[2]
        if arbit_id == "FFFFFFFF":
            logger.info("TRCReader: Dropping bus info line")
            return None

        declared_length = int(cols[3])
        is_remote = len(cols) > 4 and cols[4] == "RTR"
        data = (
            b""
            if is_remote
            else self._parse_byte_tokens(cols[4 : 4 + declared_length])
        )

        msg = Message()
        msg.timestamp = float(cols[1]) / 1000
        msg.arbitration_id = int(arbit_id, 16)
        msg.is_extended_id = len(arbit_id) > 4
        msg.channel = 1
        msg.dlc = declared_length
        if is_remote:
            msg.is_remote_frame = True
        else:
            msg.data = bytearray(data)
        return msg, None, declared_length, data

    def _parse_msg_v1_1(
        self, cols: tuple[str, ...], direction_last: bool
    ) -> tuple[Message, str | None, int, bytes]:
        if direction_last:
            direction = cols[-1]
            core = cols[:-1]
            if len(core) > 2 and self._is_direction(core[2]):
                id_index, length_index, data_index = 3, 4, 5
            else:
                id_index, length_index, data_index = 2, 3, 4
        else:
            direction = cols[2]
            core = cols
            id_index, length_index, data_index = 3, 4, 5

        arbit_id = core[id_index]
        declared_length = int(core[length_index])
        is_remote = len(core) > data_index and core[data_index] == "RTR"
        data = (
            b""
            if is_remote
            else self._parse_byte_tokens(
                core[data_index : data_index + declared_length]
            )
        )

        msg = Message()
        msg.timestamp = float(cols[1]) / 1000 + (self._start_time or 0)
        msg.arbitration_id = int(arbit_id, 16)
        msg.is_extended_id = len(arbit_id) > 4
        msg.channel = 1
        msg.dlc = declared_length
        if is_remote:
            msg.is_remote_frame = True
        else:
            msg.data = bytearray(data)
        msg.is_rx = direction == "Rx"
        return msg, direction, declared_length, data

    def _parse_msg_v1_3(
        self, cols: tuple[str, ...], direction_last: bool
    ) -> tuple[Message, str | None, int, bytes]:
        duplicate_direction = direction_last and cols[3] in ("Rx", "Tx")
        if direction_last:
            direction = cols[-1]
            core = cols[:-1]
            if duplicate_direction:
                bus_index, id_index, length_index, data_index = 2, 4, 6, 7
            else:
                bus_index, id_index, length_index, data_index = 2, 3, 5, 6
        else:
            direction = cols[3]
            core = cols
            bus_index, id_index, length_index, data_index = 2, 4, 6, 7

        arbit_id = core[id_index]
        declared_length = int(core[length_index])
        is_remote = len(core) > data_index and core[data_index] == "RTR"
        data = (
            b""
            if is_remote
            else self._parse_byte_tokens(
                core[data_index : data_index + declared_length]
            )
        )

        msg = Message()
        msg.timestamp = float(cols[1]) / 1000 + (self._start_time or 0)
        msg.arbitration_id = int(arbit_id, 16)
        msg.is_extended_id = len(arbit_id) > 4
        msg.channel = int(core[bus_index])
        msg.dlc = declared_length
        if is_remote:
            msg.is_remote_frame = True
        else:
            msg.data = bytearray(data)
        msg.is_rx = direction == "Rx"
        return msg, direction, declared_length, data

    def _parse_msg_v2_x(
        self, fields: dict[str, Any]
    ) -> tuple[Message, str | None, int, bytes]:
        type_ = fields["T"]
        direction = fields["d"]

        if "l" in self.columns:
            declared_length = int(fields["l"])
            dlc = len2dlc(declared_length)
        else:
            declared_length = int(fields["L"])
            dlc = declared_length

        arbit_id = fields["I"]
        is_remote = type_ == "RR"
        data = b"" if is_remote else self._parse_byte_tokens(fields["D"])

        msg = Message()
        msg.timestamp = float(fields["O"]) / 1000 + (self._start_time or 0)
        msg.arbitration_id = int(arbit_id, 16)
        msg.is_extended_id = len(arbit_id) > 4
        msg.channel = int(fields["B"]) if "B" in self.columns else 1
        msg.dlc = dlc
        if is_remote:
            msg.is_remote_frame = True
        else:
            msg.data = bytearray(data)
        msg.is_rx = direction == "Rx"
        msg.is_fd = type_ in {"FD", "FB", "FE", "BI"}
        msg.bitrate_switch = type_ in {"FB", "FE"}
        msg.error_state_indicator = type_ in {"FE", "BI"}
        return msg, direction, declared_length, data

    def _parse_cols_v1_x(
        self, cols: tuple[str, ...]
    ) -> tuple[Message, str | None, int, bytes] | None:
        direction_last = bool(cols) and self._is_direction(cols[-1])
        if self.file_version == TRCFileVersion.V1_3:
            if not direction_last:
                if len(cols) < 4 or not self._is_direction(cols[3]):
                    logger.info(
                        "TRCReader: Unsupported type '%s'",
                        cols[3] if len(cols) > 3 else "",
                    )
                    return None
            return self._parse_msg_v1_3(cols, direction_last)

        if self.file_version in (TRCFileVersion.V1_1, TRCFileVersion.V1_2):
            if not direction_last:
                if len(cols) < 3 or not self._is_direction(cols[2]):
                    logger.info(
                        "TRCReader: Unsupported type '%s'",
                        cols[2] if len(cols) > 2 else "",
                    )
                    return None
            return self._parse_msg_v1_1(cols, direction_last)

        logger.info("TRCReader: Unsupported type '%s'", cols[2])
        return None

    def _parse_cols_v2_x(
        self, cols: tuple[str, ...]
    ) -> tuple[Message, str | None, int, bytes] | None:
        fields = self._split_v2_fields(cols)
        dtype = fields["T"]
        if dtype in {"DT", "FD", "FB", "FE", "BI", "RR"}:
            return self._parse_msg_v2_x(fields)
        logger.info("TRCReader: Unsupported type '%s'", dtype)
        return None

    def _add_record_error(self, line_number: int, exception: Exception) -> None:
        if (
            self._pending_record_error is not None
            and self._pending_record_error.last_line == line_number - 1
        ):
            self._pending_record_error = TRCSegmentError(
                first_line=self._pending_record_error.first_line,
                last_line=line_number,
                reason=self._pending_record_error.reason,
                detail=self._pending_record_error.detail,
                exception=exception,
            )
            return
        self._flush_record_error()
        self._pending_record_error = TRCSegmentError(
            first_line=line_number,
            last_line=line_number,
            reason="invalid_record",
            detail=f"could not parse record: {exception}",
            exception=exception,
        )

    def _flush_record_error(self) -> None:
        if self._pending_record_error is not None:
            self.segment_errors.append(self._pending_record_error)
            self._pending_record_error = None

    def _parse_line(self, line: str, line_number: int) -> TRCRecord | None:
        logger.debug("TRCReader: Parse '%s'", line)
        if self._header_error is not None:
            return None
        try:
            cols = tuple(line.split())
            parsed = self._parse_cols(cols)
        except Exception as exception:
            logger.warning("TRCReader: Failed to parse message '%s'", line)
            self._add_record_error(line_number, exception)
            return None
        if parsed is None:
            return None

        message, direction, declared_length, data = parsed
        record = TRCRecord(
            message=message,
            line_number=line_number,
            line=line,
            direction=direction,
            declared_length=declared_length,
            data_bytes=data,
            file_version=self.file_version,
            declared_columns=tuple(self._declared_columns),
        )
        self._flush_record_error()
        self.records.append(record)
        return record

    def _finish_shard(self) -> None:
        self._flush_record_error()
        if not self._header_closed and self._header_seen:
            self._finish_header()
        if self._header_error is not None:
            error = TRCSegmentError(
                first_line=self._header_error.first_line,
                last_line=max(self._header_error.last_line, self._last_line_in_shard),
                reason=self._header_error.reason,
                detail=self._header_error.detail,
                line=self._header_error.line,
                exception=self._header_error.exception,
            )
            logger.warning("TRCReader: %s", error)
            self.segment_errors.append(error)
            self._header_error = None

    def _iter_records(self) -> Generator[TRCRecord, None, None]:
        if self._finished:
            yield from self.records
            return

        for raw_line in self.file:
            self._line_number += 1
            line = raw_line.strip()

            starts_new_shard = self._is_header_directive(line) and (
                self._has_data
                or (line.startswith(";$FILEVERSION") and self._header_seen)
            )
            if self._is_header_directive(line) and starts_new_shard:
                self._finish_shard()
                self._reset_shard(self._line_number)
                self._consume_header_line(line)
                continue

            if self._is_header_directive(line) and not self._header_closed:
                self._consume_header_line(line)
                continue

            if not line or line.startswith(";"):
                continue

            if not self._header_closed:
                self._finish_header()

            self._last_line_in_shard = self._line_number
            self._has_data = True
            record = self._parse_line(line, self._line_number)
            if record is not None:
                yield record

        self._finish_shard()
        self._finished = True
        self.segment_errors.sort(key=lambda error: (error.first_line, error.last_line))
        self.stop()

    def iter_records(self) -> Generator[TRCRecord, None, None]:
        """Iterate over records, including source location and raw fields."""
        yield from self._iter_records()

    def read_records(self) -> list[TRCRecord]:
        """Consume the file and return all successfully parsed records."""
        list(self._iter_records())
        return self.records

    def read(self) -> list[TRCRecord]:
        """Alias for :meth:`read_records`."""
        return self.read_records()

    def iter_messages(self) -> Generator[Message, None, None]:
        """Iterate over messages only, without record metadata."""
        yield from self

    def __iter__(self) -> Generator[Message, None, None]:
        for record in self._iter_records():
            yield record.message

        self.segment_errors.sort(key=lambda error: (error.first_line, error.last_line))


class TRCWriter(TextIOMessageWriter):
    """Logs CAN data to text file (.trc).

    The measurement starts with the timestamp of the first registered message.
    If a message has a timestamp smaller than the previous one or None,
    it gets assigned the timestamp that was written for the last message.
    If the first message does not have a timestamp, it is set to zero.
    """

    FORMAT_MESSAGE = (
        "{msgnr:>7} {time:13.3f} DT {channel:>2} {id:>8} {dir:>2} -  {dlc:<4} {data}"
    )
    FORMAT_MESSAGE_V1_0 = "{msgnr:>6}) {time:7.0f} {id:>8} {dlc:<1} {data}"

    def __init__(
        self,
        file: StringPathLike | TextIO | TextIOWrapper,
        channel: int = 1,
        **kwargs: Any,
    ) -> None:
        """
        :param file: a path-like object or as file-like object to write to
                     If this is a file-like object, is has to opened in text
                     write mode, not binary write mode.
        :param channel: a default channel to use when the message does not
                        have a channel set
        """
        super().__init__(file, mode="w")
        self.channel = channel

        if hasattr(self.file, "reconfigure"):
            self.file.reconfigure(newline="\r\n")
        else:
            raise TypeError("File must be opened in text mode.")

        self.filepath = os.path.abspath(self.file.name)
        self.header_written = False
        self.msgnr = 0
        self.first_timestamp: float | None = None
        self.file_version = TRCFileVersion.V2_1
        self._msg_fmt_string = self.FORMAT_MESSAGE_V1_0
        self._format_message = self._format_message_init

    def _write_header_v1_0(self, start_time: datetime) -> None:
        lines = [
            ";##########################################################################",
            f";   {self.filepath}",
            ";",
            ";    Generated by python-can TRCWriter",
            f";    Start time: {start_time}",
            ";    PCAN-Net: N/A",
            ";",
            ";    Columns description:",
            ";    ~~~~~~~~~~~~~~~~~~~~~",
            ";    +-current number in actual sample",
            ";    |     +time offset of message (ms",
            ";    |     |        +ID of message (hex",
            ";    |     |        |    +data length code",
            ";    |     |        |    |  +data bytes (hex ...",
            ";    |     |        |    |  |",
            ";----+- ---+--- ----+--- + -+ -- -- ...",
        ]
        self.file.writelines(line + "\n" for line in lines)

    def _write_header_v2_1(self, start_time: datetime) -> None:
        header_time = start_time - datetime(year=1899, month=12, day=30, tzinfo=UTC)
        lines = [
            ";$FILEVERSION=2.1",
            f";$STARTTIME={header_time/timedelta(days=1)}",
            ";$COLUMNS=N,O,T,B,I,d,R,L,D",
            ";",
            f";   {self.filepath}",
            ";",
            f";   Start time: {start_time}",
            ";   Generated by python-can TRCWriter",
            ";-------------------------------------------------------------------------------",
            ";   Bus   Name            Connection               Protocol",
            ";   N/A   N/A             N/A                      N/A",
            ";-------------------------------------------------------------------------------",
            ";   Message   Time    Type    ID     Rx/Tx",
            ";   Number    Offset  |  Bus  [hex]  |  Reserved",
            ";   |         [ms]    |  |    |      |  |  Data Length Code",
            ";   |         |       |  |    |      |  |  |    Data [hex] ...",
            ";   |         |       |  |    |      |  |  |    |",
            ";---+-- ------+------ +- +- --+----- +- +- +--- +- -- -- -- -- -- -- --",
        ]
        self.file.writelines(line + "\n" for line in lines)

    def _format_message_by_format(self, msg: Message, channel: int) -> str:
        if msg.is_extended_id:
            arb_id = f"{msg.arbitration_id:07X}"
        else:
            arb_id = f"{msg.arbitration_id:04X}"

        data = [f"{byte:02X}" for byte in msg.data]

        if self.first_timestamp is None:
            raise ValueError
        serialized = self._msg_fmt_string.format(
            msgnr=self.msgnr,
            time=(msg.timestamp - self.first_timestamp) * 1000,
            channel=channel,
            id=arb_id,
            dir="Rx" if msg.is_rx else "Tx",
            dlc=msg.dlc,
            data=" ".join(data),
        )
        return serialized

    def _format_message_init(self, msg: Message, channel: int) -> str:
        if self.file_version == TRCFileVersion.V1_0:
            self._format_message = self._format_message_by_format
            self._msg_fmt_string = self.FORMAT_MESSAGE_V1_0
        elif self.file_version == TRCFileVersion.V2_1:
            self._format_message = self._format_message_by_format
            self._msg_fmt_string = self.FORMAT_MESSAGE
        else:
            raise NotImplementedError("File format is not supported")

        return self._format_message_by_format(msg, channel)

    def write_header(self, timestamp: float) -> None:
        # write start of file header
        start_time = datetime.fromtimestamp(timestamp, UTC)

        if self.file_version == TRCFileVersion.V1_0:
            self._write_header_v1_0(start_time)
        elif self.file_version == TRCFileVersion.V2_1:
            self._write_header_v2_1(start_time)
        else:
            raise NotImplementedError("File format is not supported")
        self.header_written = True

    def log_event(self, message: str, timestamp: float) -> None:
        if not self.header_written:
            self.write_header(timestamp)

        self.file.write(message + "\n")

    def on_message_received(self, msg: Message) -> None:
        if self.first_timestamp is None:
            self.first_timestamp = msg.timestamp

        if msg.is_error_frame:
            logger.warning("TRCWriter: Logging error frames is not implemented")
            return

        if msg.is_remote_frame:
            logger.warning("TRCWriter: Logging remote frames is not implemented")
            return

        channel = channel2int(msg.channel)
        if channel is None:
            channel = self.channel
        else:
            # Many interfaces start channel numbering at 0 which is invalid
            channel += 1

        if msg.is_fd:
            logger.warning("TRCWriter: Logging CAN FD is not implemented")
            return

        serialized = self._format_message(msg, channel)
        self.msgnr += 1
        self.log_event(serialized, msg.timestamp)
