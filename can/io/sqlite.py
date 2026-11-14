"""
Implements an SQL database writer and reader for storing CAN messages.

.. note:: The database schema is given in the documentation of the loggers.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Generator, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

from can.listener import BufferedReader
from can.message import Message

from ..typechecking import StringPathLike
from .generic import MessageReader, MessageWriter

log = logging.getLogger("can.io.sqlite")

_MessageTuple: TypeAlias = "tuple[float, int, bool, bool, bool, int, memoryview[int]]"

#: Columns of the original SQLite log format.
LEGACY_COLUMNS: tuple[str, ...] = (
    "ts",
    "arbitration_id",
    "extended",
    "remote",
    "error",
    "dlc",
    "data",
)
#: Columns appended by the compatibility schema. ``NULL`` means that the
#: original database did not record a value; it is not equivalent to ``0``.
EXTENDED_COLUMNS: tuple[str, ...] = (
    "channel",
    "is_fd",
    "bitrate_switch",
    "error_state_indicator",
    "source_db",
    "source_row",
)

ALL_COLUMNS: tuple[str, ...] = LEGACY_COLUMNS + EXTENDED_COLUMNS

_CREATE_TABLE_SQL = """CREATE TABLE IF NOT EXISTS {table} (
    ts REAL,
    arbitration_id INTEGER,
    extended INTEGER,
    remote INTEGER,
    error INTEGER,
    dlc INTEGER,
    data BLOB,
    channel,
    is_fd INTEGER,
    bitrate_switch INTEGER,
    error_state_indicator INTEGER,
    source_db,
    source_row INTEGER
)"""

_CREATE_STATE_SQL = """CREATE TABLE IF NOT EXISTS {table} (
    source_db,
    source_table TEXT NOT NULL,
    target_table TEXT NOT NULL,
    next_row INTEGER NOT NULL,
    total INTEGER NOT NULL,
    migrated INTEGER NOT NULL,
    PRIMARY KEY (source_db, source_table, target_table)
)"""

_STATE_TABLE = "can_sqlite_migration_state"

_CHANNEL_JSON_PREFIX = "__can_channel_json__:"


class SqliteReader(MessageReader):
    """
    Reads recorded CAN messages from a simple SQL database.

    This class can be iterated over or used to fetch all messages in the
    database with :meth:`~SqliteReader.read_all`.

    Databases written by older versions of this module contain no channel or
    CAN FD information. For those rows this reader returns ``channel=None``
    and, importantly, ``is_fd=None``, ``bitrate_switch=None`` and
    ``error_state_indicator=None`` to indicate *unknown*. Rows written with
    this version explicitly record classic frames as ``False``, so an
    unknown row can be distinguished from a confirmed classic frame.

    Calling :func:`len` on this object might not run in constant time.

    :attr str table_name: the name of the database table used for storing the messages

    .. note:: The database schema is given in the documentation of the loggers.
    """

    def __init__(
        self,
        file: StringPathLike,
        table_name: str = "messages",
        **kwargs: Any,
    ) -> None:
        """
        :param file: a `str` path like object that points
                     to the database file to use
        :param str table_name: the name of the table to look for the messages

        .. warning:: In contrary to all other readers/writers the Sqlite handlers
                     do not accept file-like objects as the `file` parameter.
                     It also runs in ``append=True`` mode all the time.
        """
        self._conn = sqlite3.connect(file)
        self._cursor = self._conn.cursor()
        self.table_name = table_name

    @property
    def columns(self) -> tuple[str, ...]:
        """The column names of the table, in declaration order."""
        return tuple(_table_columns(self._conn, self.table_name))

    def _select_expressions(self) -> str:
        """Build a select list that also works for old seven-column tables."""
        columns = set(self.columns)
        if not columns:
            raise ValueError(f"table {self.table_name!r} does not exist")
        missing = [name for name in LEGACY_COLUMNS if name not in columns]
        if missing:
            raise ValueError(
                f"table {self.table_name!r} is missing columns: " + ", ".join(missing)
            )
        expressions = ["rowid"] + [
            _quote_identifier(column) if column in columns else "NULL"
            for column in ALL_COLUMNS
        ]
        return ", ".join(expressions)

    def _select(self) -> str:
        return (
            f"SELECT {self._select_expressions()} "
            f"FROM {_quote_identifier(self.table_name)} ORDER BY rowid"
        )

    def __iter__(self) -> Generator[Message, None, None]:
        for frame_data in self._cursor.execute(self._select()):
            yield SqliteReader._assemble_message(frame_data)

    @staticmethod
    def _assemble_message(frame_data: Sequence[Any]) -> Message:
        """Create a message from a stored row.

        This accepts the historical seven-value row as well as the extended
        thirteen-value row (optionally prefixed with the SQLite row id).
        Missing channel/FD values remain ``None``.
        """
        values = list(frame_data)
        if len(values) == len(ALL_COLUMNS) + 1:
            # Internal SELECT also returns the row id first.
            values = values[1:]
        elif len(values) == len(LEGACY_COLUMNS) + 1:
            # A SELECT * from an old table, including the row id.
            values = values[1:]
        if len(values) == len(LEGACY_COLUMNS):
            channel = None
            ts, can_id, extended, remote, error, dlc, data = values
            is_fd = bitrate_switch = error_state_indicator = None
            return SqliteReader._make_message(
                ts,
                can_id,
                extended,
                remote,
                error,
                dlc,
                data,
                channel,
                is_fd,
                bitrate_switch,
                error_state_indicator,
            )
        if len(values) != len(ALL_COLUMNS):
            raise ValueError(
                f"expected {len(LEGACY_COLUMNS)} or {len(ALL_COLUMNS)} "
                f"values, got {len(values)}"
            )

        (
            ts,
            can_id,
            extended,
            remote,
            error,
            dlc,
            data,
            channel,
            is_fd,
            bitrate_switch,
            error_state_indicator,
            _source_db,
            _source_row,
        ) = values
        return SqliteReader._make_message(
            ts,
            can_id,
            extended,
            remote,
            error,
            dlc,
            data,
            channel,
            is_fd,
            bitrate_switch,
            error_state_indicator,
        )

    @staticmethod
    def _make_message(
        ts: float,
        can_id: int,
        extended: Any,
        remote: Any,
        error: Any,
        dlc: int,
        data: Any,
        channel: Any,
        is_fd: Any,
        bitrate_switch: Any,
        error_state_indicator: Any,
    ) -> Message:
        msg = Message(
            timestamp=ts,
            arbitration_id=can_id,
            is_extended_id=bool(_flag_value(extended)),
            is_remote_frame=bool(_flag_value(remote)),
            is_error_frame=bool(_flag_value(error)),
            dlc=dlc,
            data=data,
            channel=_decode_channel(channel),
        )
        if is_fd is None:
            # ``None`` is intentional: the legacy schema did not record
            # whether the frame was classic CAN or CAN FD.
            msg.is_fd = None  # type: ignore[assignment]
            msg.bitrate_switch = None  # type: ignore[assignment]
            msg.error_state_indicator = None  # type: ignore[assignment]
        else:
            msg.is_fd = bool(_flag_value(is_fd))
            msg.bitrate_switch = (
                bool(_flag_value(bitrate_switch))
                if bitrate_switch is not None
                else False
            )
            msg.error_state_indicator = (
                bool(_flag_value(error_state_indicator))
                if error_state_indicator is not None
                else False
            )
        return msg

    def __len__(self) -> int:
        # this might not run in constant time
        result = self._cursor.execute(
            f"SELECT COUNT(*) FROM {_quote_identifier(self.table_name)}"
        )
        return int(result.fetchone()[0])

    def read_all(self) -> Iterator[Message]:
        """Fetches all messages in the database.

        :rtype: Generator[can.Message]
        """
        result = self._cursor.execute(self._select()).fetchall()
        return (SqliteReader._assemble_message(frame) for frame in result)

    def find(
        self,
        source_db: Any = None,
        source_row: int | None = None,
        *,
        database_id: Any = None,
        db_id: Any = None,
    ) -> Iterator[Message]:
        """Yield messages migrated from a row of another database.

        :param source_db: the original database name as passed to
                          :class:`SqliteMigrator`
        :param source_row: the SQLite ``rowid`` in that original database
        :return: an iterator over matching messages, in target row order

        ``database_id`` and ``db_id`` are aliases for ``source_db``. Both
        filters are optional. If the table has not been migrated, the result
        is empty.
        """
        if source_db is None:
            source_db = database_id if database_id is not None else db_id
        columns = set(self.columns)
        if not {"source_db", "source_row"}.issubset(columns):
            return iter(())

        clauses: list[str] = []
        params: list[Any] = []
        if source_db is not None:
            names = _source_name_variants(source_db)
            clauses.append(
                "source_db IN (" + ", ".join("?" for _ in names) + ")"
            )
            params.extend(names)
        if source_row is not None:
            clauses.append("source_row = ?")
            params.append(int(source_row))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = self._conn.cursor()
        result = cursor.execute(
            f"SELECT {self._select_expressions()} "
            f"FROM {_quote_identifier(self.table_name)}{where} "
            "ORDER BY rowid",
            params,
        )
        return (
            SqliteReader._assemble_message(row)
            for row in result
        )

    # More explicit aliases for callers that prefer the terminology from the
    # migration API.
    def find_by_origin(
        self,
        source_db: Any = None,
        source_row: int | None = None,
        *,
        database_id: Any = None,
        db_id: Any = None,
    ) -> Iterator[Message]:
        """Alias for :meth:`find`."""
        return self.find(
            source_db=source_db,
            source_row=source_row,
            database_id=database_id,
            db_id=db_id,
        )

    def find_origin(
        self,
        source_db: Any,
        source_row: int,
    ) -> Iterator[Message]:
        """Alias for :meth:`find` with both origin fields required."""
        return self.find(source_db=source_db, source_row=source_row)

    def find_one(
        self,
        source_db: Any = None,
        source_row: int | None = None,
        *,
        database_id: Any = None,
        db_id: Any = None,
    ) -> Message | None:
        """Return the first matching message, or ``None``."""
        return next(
            self.find(
                source_db=source_db,
                source_row=source_row,
                database_id=database_id,
                db_id=db_id,
            ),
            None,
        )

    def iter_with_origin(
        self,
    ) -> Iterator[tuple[Message, str | None, int | None]]:
        """Yield ``(message, source_db, source_row)`` triples in row order."""
        columns = set(self.columns)
        if not {"source_db", "source_row"}.issubset(columns):
            for msg in self:
                yield msg, None, None
            return
        expressions = [
            _quote_identifier(column) if column in columns else "NULL"
            for column in ALL_COLUMNS
        ]
        for frame in self._cursor.execute(
            "SELECT " + ", ".join(expressions)
            + f" FROM {_quote_identifier(self.table_name)} ORDER BY rowid"
        ):
            values = list(frame)
            source_db = values[11]
            source_row = values[12]
            yield (
                SqliteReader._assemble_message(values),
                source_db,
                int(source_row) if source_row is not None else None,
            )

    def stop(self) -> None:
        """Closes the connection to the database."""
        self._conn.close()


class SqliteWriter(MessageWriter, BufferedReader):
    """Logs received CAN data to a simple SQL database.

    The sqlite database may already exist, otherwise it will
    be created when the first message arrives. Existing databases in the
    original seven-column format are upgraded additively when the writer
    starts; their rows are neither reordered nor changed.

    Messages are internally buffered and written to the SQL file in a background
    thread. Ensures that all messages that are added before calling :meth:`~can.SqliteWriter.stop()`
    are actually written to the database after that call returns. Thus, calling
    :meth:`~can.SqliteWriter.stop()` may take a while.

    :attr str table_name: the name of the database table used for storing the messages
    :attr int num_frames: the number of frames actually written to the database, this
                          excludes messages that are still buffered
    :attr float last_write: the last time a message war actually written to the database,
                            as given by ``time.time()``

    .. note::

        When the listener's :meth:`~SqliteWriter.stop` method is called the
        thread writing to the database will continue to receive and internally
        buffer messages if they continue to arrive before the
        :attr:`~SqliteWriter.GET_MESSAGE_TIMEOUT`.

        If the :attr:`~SqliteWriter.GET_MESSAGE_TIMEOUT` expires before a message
        is received, the internal buffer is written out to the database file.

        However if the bus is still saturated with messages, the Listener
        will continue receiving until the :attr:`~can.SqliteWriter.MAX_TIME_BETWEEN_WRITES`
        timeout is reached or more than
        :attr:`~can.SqliteWriter.MAX_BUFFER_SIZE_BEFORE_WRITES` messages are buffered.

    .. note:: The database schema is given in the documentation of the loggers.

    """

    GET_MESSAGE_TIMEOUT = 0.25
    """Number of seconds to wait for messages from internal queue"""

    MAX_TIME_BETWEEN_WRITES = 5.0
    """Maximum number of seconds to wait between writes to the database"""

    MAX_BUFFER_SIZE_BEFORE_WRITES = 500
    """Maximum number of messages to buffer before writing to the database"""

    def __init__(
        self,
        file: StringPathLike,
        table_name: str = "messages",
        **kwargs: Any,
    ) -> None:
        """
        :param file: a `str` or path like object that points
                     to the database file to use
        :param str table_name: the name of the table to store messages in

        .. warning:: In contrary to all other readers/writers the Sqlite handlers
                     do not accept file-like objects as the `file` parameter.
        """
        if kwargs.get("append", False):
            raise ValueError(
                f"The append argument should not be used in "
                f"conjunction with the {self.__class__.__name__}."
            )
        BufferedReader.__init__(self)
        self.table_name = table_name
        self._db_filename = file
        self._stop_running_event = threading.Event()
        self._writer_thread = threading.Thread(target=self._db_writer_thread)
        self._writer_thread.start()
        self.num_frames = 0
        self.last_write = time.time()
        self._insert_template = (
            f"INSERT INTO {_quote_identifier(self.table_name)} "
            f"({', '.join(_quote_identifier(column) for column in ALL_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in ALL_COLUMNS)})"
        )

    @staticmethod
    def _create_db(file: StringPathLike, table_name: str) -> sqlite3.Connection:
        """Creates a new database or opens a connection to an existing one.

        .. note::
            You can't share sqlite3 connections between threads (by default)
            hence we setup the db here. It has the upside of running async.
        """
        log.debug("Creating sqlite database")
        conn = sqlite3.connect(file)
        try:
            ensure_schema(conn, table_name)
        except BaseException:
            conn.close()
            raise
        conn.commit()
        return conn

    def _db_writer_thread(self) -> None:
        conn = SqliteWriter._create_db(self._db_filename, self.table_name)

        try:
            while True:
                messages: list[tuple[Any, ...]] = []  # reset buffer

                msg = self.get_message(self.GET_MESSAGE_TIMEOUT)
                while msg is not None:
                    # log.debug("SqliteWriter: buffering message")

                    messages.append(
                        (
                            msg.timestamp,
                            msg.arbitration_id,
                            msg.is_extended_id,
                            msg.is_remote_frame,
                            msg.is_error_frame,
                            msg.dlc,
                            memoryview(msg.data),
                            _encode_channel(msg.channel),
                            _flag_value(msg.is_fd),
                            _flag_value(msg.bitrate_switch),
                            _flag_value(msg.error_state_indicator),
                            None,
                            None,
                        )
                    )

                    if (
                        time.time() - self.last_write > self.MAX_TIME_BETWEEN_WRITES
                        or len(messages) > self.MAX_BUFFER_SIZE_BEFORE_WRITES
                    ):
                        break

                    # just go on
                    msg = self.get_message(self.GET_MESSAGE_TIMEOUT)

                count = len(messages)
                if count > 0:
                    with conn:
                        # log.debug("Writing %d frames to db", count)
                        conn.executemany(self._insert_template, messages)
                        conn.commit()  # make the changes visible to the entire database
                    self.num_frames += count
                    self.last_write = time.time()

                # check if we are still supposed to run and go back up if yes
                if self._stop_running_event.is_set():
                    break

        finally:
            conn.close()
            log.info("Stopped sqlite writer after writing %d messages", self.num_frames)

    def stop(self) -> None:
        """Stops the reader an writes all remaining messages to the database. Thus, this
        might take a while and block.
        """
        BufferedReader.stop(self)
        self._stop_running_event.set()
        self._writer_thread.join()


@dataclass(frozen=True)
class MigrationProgress:
    """Progress returned by :meth:`SqliteMigrator.step`."""

    migrated: int
    """Total rows migrated so far."""

    total: int
    """Rows present in the source when migration started."""

    next_row: int | None
    """Source row id to process next, or ``None`` when complete."""

    batch: int = 0
    """Rows migrated by this particular call."""

    @property
    def done(self) -> bool:
        return self.next_row is None

    @property
    def remaining(self) -> int:
        return 0 if self.done else max(self.total - self.migrated, 0)


class SqliteMigrator:
    """Migrate an old SQLite CAN log into the compatibility schema.

    Migration is performed in batches and is restartable. A separate
    ``can_sqlite_migration_state`` table records the next source ``rowid``; the
    batch and the state update are committed atomically. If the process
    stops at any point, constructing a new migrator with the same arguments
    resumes exactly after the last committed batch.

    Source rows are visited in ``rowid`` order. They are inserted in that same
    order, so existing rows and duplicate frames keep their relative order.

    ``target`` may be omitted, in which case the source is upgraded in place.
    In-place migration keeps every original ``rowid`` and only fills the new
    columns. With a separate target the source is left untouched.

    Every migrated row carries ``source_db`` and ``source_row``. Use
    :meth:`SqliteReader.find` (or ``find_by_origin``) to retrieve the exact
    sampling later, even after new rows have been appended.

    Values that the old schema did not record stay ``NULL``. In particular,
    old frames do not become false classic frames: they read back with
    ``is_fd=None``. A frame explicitly written as classic has ``is_fd=0``.
    """

    def __init__(
        self,
        source: StringPathLike,
        target: StringPathLike | None = None,
        table_name: str = "messages",
        source_table_name: str | None = None,
        batch_size: int = 500,
        *,
        chunk_size: int | None = None,
        source_db: Any = None,
        **kwargs: Any,
    ) -> None:
        del kwargs
        if chunk_size is not None:
            batch_size = chunk_size
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self.source = Path(source)
        self.target = Path(target) if target is not None else self.source
        self.table_name = table_name
        self.source_table_name = source_table_name or table_name
        self.batch_size = batch_size
        self._next_row: int | None = 0
        self._total = 0
        self._migrated = 0
        self._started = False
        self._done = False
        # Callers may supply a stable database identifier (for example an
        # integer archive number). Otherwise the source path is used.
        self._source_name: Any = str(source) if source_db is None else source_db
        self.source_db = self._source_name

    @property
    def in_place(self) -> bool:
        """Whether migration upgrades the source table itself."""
        return (
            self.source.resolve() == self.target.resolve()
            and self.source_table_name == self.table_name
        )

    @property
    def rows_migrated(self) -> int:
        self._prepare()
        return self._migrated

    @property
    def migrated(self) -> int:
        self._prepare()
        return self._migrated

    @property
    def total(self) -> int:
        self._prepare()
        return self._total

    def _prepare(self) -> None:
        if self._started:
            return
        source_conn = sqlite3.connect(self.source)
        try:
            # Refuse unrelated schemas before creating the state table or
            # altering the target.
            source_columns = _table_columns(source_conn, self.source_table_name)
            if not source_columns:
                raise ValueError(
                    f"table {self.source_table_name!r} does not exist in {self.source}"
                )
            missing = [
                column for column in LEGACY_COLUMNS if column not in source_columns
            ]
            if missing:
                raise ValueError(
                    f"table {self.source_table_name!r} is missing columns: "
                    + ", ".join(missing)
                )
            total_row = source_conn.execute(
                f"SELECT COUNT(*) FROM {_quote_identifier(self.source_table_name)}"
            ).fetchone()
            self._total = int(total_row[0])
        finally:
            source_conn.close()

        conn = sqlite3.connect(self.target)
        try:
            # An existing target table must be a CAN log before we modify it.
            if _table_columns(conn, self.table_name):
                self._assert_table_is_log(conn, self.table_name)
            ensure_schema(conn, self.table_name)
            ensure_state_table(conn)
            conn.commit()
            self._read_state(conn)
        finally:
            conn.close()
        self._started = True

    def _assert_table_is_log(self, conn: sqlite3.Connection, table: str) -> None:
        """Reject unrelated tables before adding compatibility columns."""
        columns = _table_columns(conn, table)
        if not columns:
            raise ValueError(f"table {table!r} does not exist")
        if not set(LEGACY_COLUMNS).issubset(columns):
            raise ValueError(f"table {table!r} is not a CAN SQLite log table")

    def _read_state(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            f"SELECT next_row, total, migrated FROM {_quote_identifier(_STATE_TABLE)} "
            "WHERE source_db = ? AND source_table = ? AND target_table = ?",
            (self._source_name, self.source_table_name, self.table_name),
        ).fetchone()
        if row is None:
            self._next_row = 0
            self._done = False
            return
        next_row, total, migrated = row
        self._next_row = None if int(next_row) < 0 else int(next_row)
        self._done = next_row is None or int(next_row) < 0
        self._total = max(self._total, int(total))
        self._migrated = int(migrated)

    def _write_state(
        self,
        conn: sqlite3.Connection,
        next_row: int | None,
        migrated: int | None = None,
    ) -> None:
        stored_next = -1 if next_row is None else int(next_row)
        if migrated is None:
            migrated = self._migrated
        conn.execute(
            f"INSERT INTO {_quote_identifier(_STATE_TABLE)} "
            "(source_db, source_table, target_table, next_row, total, migrated) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source_db, source_table, target_table) DO UPDATE SET "
            "next_row = excluded.next_row, total = excluded.total, "
            "migrated = excluded.migrated",
            (
                self._source_name,
                self.source_table_name,
                self.table_name,
                stored_next,
                self._total,
                migrated,
            ),
        )

    def _source_select(self, conn: sqlite3.Connection) -> str:
        columns = _table_columns(conn, self.source_table_name)
        expressions = ["rowid"]
        for column in ALL_COLUMNS:
            expressions.append(
                _quote_identifier(column) if column in columns else "NULL"
            )
        return ", ".join(expressions)

    def step(self, batch_size: int | None = None) -> MigrationProgress:
        """Migrate the next batch and commit it.

        :param batch_size: override the batch size for this call.
        :return: progress after the committed batch.

        Calling this method after completion is harmless and returns
        ``done=True``.
        """
        self._prepare()
        if self._done:
            return MigrationProgress(self._migrated, self._total, None, 0)

        limit = self.batch_size if batch_size is None else batch_size
        if limit < 1:
            raise ValueError("batch_size must be at least 1")

        if self.in_place:
            conn = sqlite3.connect(self.target)
            try:
                return self._step_connection(conn, conn, limit)
            finally:
                conn.close()
        else:
            source_conn = sqlite3.connect(self.source)
            target_conn = sqlite3.connect(self.target)
            try:
                return self._step_connection(source_conn, target_conn, limit)
            finally:
                source_conn.close()
                target_conn.close()

    def _step_connection(
        self,
        source_conn: sqlite3.Connection,
        target_conn: sqlite3.Connection,
        limit: int,
    ) -> MigrationProgress:
        assert self._next_row is not None
        select = self._source_select(source_conn)
        rows = source_conn.execute(
            f"SELECT {select} FROM {_quote_identifier(self.source_table_name)} "
            "WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (self._next_row, limit),
        ).fetchall()

        if not rows:
            # The source may have grown since its row count was read. It
            # cannot grow after migration completed, because later appends
            # are new logs and must not be reclassified as migrated rows.
            max_row = source_conn.execute(
                f"SELECT MAX(rowid) FROM {_quote_identifier(self.source_table_name)}"
            ).fetchone()[0]
            if max_row is not None:
                self._total = max(self._total, int(max_row))
            with target_conn:
                self._next_row = None
                self._done = True
                self._write_state(target_conn, None, migrated=self._migrated)
            return MigrationProgress(self._migrated, self._total, None, 0)
        prepared = [self._prepare_row(row) for row in rows]
        if self.in_place:
            template = self._update_template()
        else:
            template = self._insert_template()

        last_row = int(rows[-1][0])
        remaining = source_conn.execute(
            f"SELECT 1 FROM {_quote_identifier(self.source_table_name)} "
            "WHERE rowid > ? LIMIT 1",
            (last_row,),
        ).fetchone()
        next_row: int | None = last_row if remaining else None
        migrated = self._migrated + len(rows)

        # Only advance the in-memory cursor after the transaction commits.
        # If executemany or commit raises, a restarted migrator will read the
        # last committed next_row from the state table and retry this batch.
        with target_conn:
            target_conn.executemany(template, prepared)
            self._write_state(target_conn, next_row, migrated=migrated)
        self._migrated = migrated
        self._next_row = next_row
        self._done = next_row is None

        return MigrationProgress(
            self._migrated, self._total, self._next_row, len(rows)
        )

    def _prepare_row(self, row: Sequence[Any]) -> tuple[Any, ...]:
        (
            rowid,
            ts,
            can_id,
            extended,
            remote,
            error,
            dlc,
            data,
            channel,
            is_fd,
            bitrate_switch,
            error_state_indicator,
            source_db,
            source_row,
        ) = row

        if source_db is not None and source_row is not None:
            origin_db = source_db
            origin_row = int(source_row)
        else:
            # A row without complete provenance is being adopted by this
            # migration; rows that already have both values retain them.
            origin_db = self._source_name
            origin_row = int(rowid)

        # Without an is_fd value the old schema cannot tell a classic frame
        # from a CAN FD frame. Keep NULL rather than inventing ``False``.
        values = (
            ts,
            can_id,
            extended,
            remote,
            error,
            dlc,
            data,
            _encode_channel(channel),
            _flag_value(is_fd),
            _flag_value(bitrate_switch),
            _flag_value(error_state_indicator),
            origin_db,
            origin_row,
        )
        if self.in_place:
            return (*values, int(rowid))
        return values

    def _insert_template(self) -> str:
        columns = ", ".join(_quote_identifier(column) for column in ALL_COLUMNS)
        placeholders = ", ".join("?" for _ in ALL_COLUMNS)
        return (
            f"INSERT INTO {_quote_identifier(self.table_name)} "
            f"({columns}) VALUES ({placeholders})"
        )

    def _update_template(self) -> str:
        assignments = ", ".join(
            f"{_quote_identifier(column)} = ?"
            for column in ALL_COLUMNS
        )
        return (
            f"UPDATE {_quote_identifier(self.table_name)} SET {assignments} "
            "WHERE rowid = ?"
        )

    def run(self, batch_size: int | None = None) -> int:
        """Run batches until all rows present at completion are migrated.

        :return: the number of rows newly migrated by this invocation. Use
                 the :attr:`migrated` property for the cumulative count,
                 including rows committed before a restart.
        """
        self._prepare()
        start = self._migrated
        while not self._done:
            self.step(batch_size)
        return self._migrated - start

    def migrate(self, batch_size: int | None = None) -> int:
        """Alias for :meth:`run`; migration is complete when it returns."""
        return self.run(batch_size=batch_size)

    def migrate_batch(self, batch_size: int | None = None) -> MigrationProgress:
        """Alias for :meth:`step`."""
        return self.step(batch_size=batch_size)

    def resume(self) -> int:
        """Continue a previously interrupted migration to completion."""
        return self.run()

    def is_done(self) -> bool:
        """Return whether all the originally known rows have been migrated."""
        self._prepare()
        return self._done


def ensure_state_table(conn: sqlite3.Connection) -> None:
    """Create the table used to resume interrupted migrations."""
    conn.execute(_CREATE_STATE_SQL.format(table=_quote_identifier(_STATE_TABLE)))


def ensure_schema(
    conn: sqlite3.Connection,
    table_name: str = "messages",
) -> tuple[str, ...]:
    """Create or additively upgrade a SQLite log table.

    The original seven columns are never rewritten. Missing compatibility
    columns are appended, which leaves existing row ids, row contents and
    duplicates untouched. New columns are ``NULL`` for old rows to signal
    that their values are unknown.
    """
    quoted = _quote_identifier(table_name)
    columns = _table_columns(conn, table_name)
    if not columns:
        conn.execute(_CREATE_TABLE_SQL.format(table=quoted))
        return ALL_COLUMNS

    missing_legacy = [name for name in LEGACY_COLUMNS if name not in columns]
    if missing_legacy:
        raise ValueError(
            f"table {table_name!r} is missing columns: " + ", ".join(missing_legacy)
        )

    for name in EXTENDED_COLUMNS:
        if name not in columns:
            # SQLite cannot add a column with no declared type using a
            # portable ALTER TABLE statement, so use an empty affinity as a
            # BLOB-ish placeholder. Values remain dynamically typed.
            sql_type = _column_type(name)
            if sql_type:
                conn.execute(
                    f"ALTER TABLE {quoted} ADD COLUMN "
                    f"{_quote_identifier(name)} {sql_type}"
                )
            else:
                conn.execute(
                    f"ALTER TABLE {quoted} ADD COLUMN {_quote_identifier(name)}"
                )
            columns.append(name)
    return tuple(columns)


def migrate_sqlite(
    source: StringPathLike,
    target: StringPathLike | None = None,
    table_name: str = "messages",
    batch_size: int = 500,
    *,
    source_table_name: str | None = None,
) -> int:
    """Convenience wrapper around :class:`SqliteMigrator`.

    :return: the number of rows newly migrated by this run (``0`` if the
             migration had already completed).
    """
    migrator = SqliteMigrator(
        source,
        target,
        table_name,
        source_table_name,
        batch_size,
    )
    before = migrator.rows_migrated
    migrator.run()
    return migrator.rows_migrated - before


def _table_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    """Return column names, excluding any view when a table is requested."""
    return [
        str(row[1])
        for row in conn.execute(
            f"PRAGMA table_info({_quote_identifier(table_name)})"
        )
    ]


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _column_type(name: str) -> str:
    return {
        "is_fd": "INTEGER",
        "bitrate_switch": "INTEGER",
        "error_state_indicator": "INTEGER",
        "source_row": "INTEGER",
    }.get(name, "")


def _flag_value(value: Any) -> int | None:
    """Return ``1``/``0`` for a recorded flag and ``None`` if unrecorded."""
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "0", "false", "no", "off"}:
            return 0
        if normalized in {"1", "true", "yes", "on"}:
            return 1
    return int(bool(value))


def _encode_channel(channel: Any) -> Any:
    if channel is None or isinstance(channel, (int, str)):
        return channel
    try:
        return _CHANNEL_JSON_PREFIX + json.dumps(channel)
    except (TypeError, ValueError):
        return _CHANNEL_JSON_PREFIX + json.dumps(list(channel))


def _decode_channel(channel: Any) -> Any:
    if isinstance(channel, str) and channel.startswith(_CHANNEL_JSON_PREFIX):
        return json.loads(channel[len(_CHANNEL_JSON_PREFIX):])
    return channel


def _source_name_variants(source_db: Any) -> tuple[Any, ...]:
    """Match a source identifier as stored or by its resolved path.

    The basename is deliberately not used: two archive directories can
    contain a file with the same name, and returning rows from the wrong
    database would break the original sample's identity.
    """
    if not isinstance(source_db, (str, bytes, Path)):
        return (source_db,)
    name = str(source_db)
    variants: list[Any] = [name]
    try:
        resolved = str(Path(source_db).resolve())
    except (OSError, TypeError, ValueError):
        resolved = ""
    if resolved and resolved not in variants:
        variants.append(resolved)
    return tuple(variants)
