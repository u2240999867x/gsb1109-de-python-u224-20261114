"""
Implements an SQL database writer and reader for storing CAN messages.

.. note:: The database schema is given in the documentation of the loggers.
"""

import hashlib
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Generator, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, TypeAlias

from can.listener import BufferedReader
from can.message import Message

from ..typechecking import StringPathLike
from .generic import MessageReader, MessageWriter

log = logging.getLogger("can.io.sqlite")

_MessageTuple: TypeAlias = "tuple[float, int, bool, bool, bool, int, memoryview[int]]"
_Columns: TypeAlias = "tuple[float, int, bool, bool, bool, int, bytes]"


@dataclass(frozen=True)
class SqliteCheckpoint:
    """A resumable position in a SQLite message log.

    The position deliberately does not use timestamps: recorded timestamps may
    be duplicated or move backwards.  It is instead based on the SQLite
    ``rowid`` high-water mark, plus enough information to tell apart two
    databases that were written to the same path at different times.

    :param str generation: identity of the database file the checkpoint belongs to
    :param int last_rowid: highest rowid that has been processed and committed
    :param str anchor: fingerprint of that row, used to detect an in-place
                       replacement of the database file
    """

    generation: str
    last_rowid: int
    anchor: str = ""

    def to_dict(self) -> dict[str, str | int]:
        """Return a JSON-serializable representation of this checkpoint."""
        return {
            "generation": self.generation,
            "last_rowid": self.last_rowid,
            "anchor": self.anchor,
        }

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> "SqliteCheckpoint":
        """Create a checkpoint from :meth:`to_dict` output."""
        try:
            generation = str(values["generation"])
            last_rowid = int(values["last_rowid"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid SQLite checkpoint") from error
        anchor = str(values.get("anchor", ""))
        if last_rowid < 0:
            raise ValueError("checkpoint rowid must not be negative")
        return cls(generation=generation, last_rowid=last_rowid, anchor=anchor)


@dataclass(frozen=True)
class SqliteBatch:
    """One incrementally read batch of messages.

    ``boundary`` is the highest rowid that was visible when the batch was
    started.  Rows appended while the batch was being read are therefore left
    for the next batch.  A batch only becomes the reader's committed progress
    after :meth:`SqliteReader.commit` is called.
    """

    messages: tuple[Message, ...]
    checkpoint: SqliteCheckpoint
    start_rowid: int
    end_rowid: int
    visible_boundary: int
    first_rowid: int | None
    last_rowid: int | None
    replaced: bool = False
    previous_checkpoint: SqliteCheckpoint | None = None
    superseded: bool = False

    @property
    def count(self) -> int:
        """Number of messages in this batch."""
        return len(self.messages)

    @property
    def boundary(self) -> int:
        """Highest rowid visible when this batch was started."""
        return self.visible_boundary

    @property
    def next_rowid(self) -> int:
        """Rowid immediately after the progress committed by this batch."""
        return self.checkpoint.last_rowid + 1

    def __iter__(self) -> Iterator[Message]:
        return iter(self.messages)

    def __len__(self) -> int:
        return len(self.messages)

    def __getitem__(self, index: int | slice) -> Message | tuple[Message, ...]:
        return self.messages[index]


class SqliteReader(MessageReader):
    """
    Reads recorded CAN messages from a simple SQL database.

    This class can be iterated over or used to fetch all messages in the
    database with :meth:`~SqliteReader.read_all`.

    For a log that is still being written, use :meth:`~SqliteReader.read_new`
    to read a bounded batch and :meth:`~SqliteReader.commit` to advance the
    resumable checkpoint after the batch has been persisted.  The checkpoint
    can be serialized with :meth:`SqliteCheckpoint.to_dict` and passed back via
    the ``checkpoint`` constructor argument.

    Calling :func:`len` on this object might not run in constant time.

    :attr str table_name: the name of the database table used for storing the messages
    :attr SqliteCheckpoint | None checkpoint: the last committed position

    .. note:: The database schema is given in the documentation of the loggers.
    """

    def __init__(
        self,
        file: StringPathLike,
        table_name: str = "messages",
        checkpoint: SqliteCheckpoint | Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """
        :param file: a `str`  path like object that points
                     to the database file to use
        :param str table_name: the name of the table to look for the messages
        :param checkpoint: an optional previously committed position

        .. warning:: In contrary to all other readers/writers the Sqlite handlers
                     do not accept file-like objects as the `file` parameter.
                     It also runs in ``append=True`` mode all the time.
        """
        self._file = os.fspath(file)
        self.table_name = table_name
        self._lock = threading.Lock()
        self._checkpoint: SqliteCheckpoint | None = None
        self._pending_batch: SqliteBatch | None = None
        if checkpoint is not None:
            self.set_checkpoint(checkpoint)

    def _connect(self) -> sqlite3.Connection:
        if not os.path.exists(self._file):
            raise FileNotFoundError(self._file)
        return sqlite3.connect(self._file)

    def _file_identity(self) -> str:
        stat = os.stat(self._file)
        return f"{stat.st_dev}:{stat.st_ino}"

    def _identity_matches(self, identity: str) -> bool:
        try:
            return self._file_identity() == identity
        except FileNotFoundError:
            return False

    @staticmethod
    def _row_anchor(row: _Columns) -> str:
        values = tuple(
            bytes(value) if isinstance(value, memoryview) else value for value in row
        )
        return hashlib.sha256(repr(values).encode("utf-8")).hexdigest()

    @staticmethod
    def _select_clause() -> str:
        return "ts, arbitration_id, extended, remote, error, dlc, data"

    def _check_table(self, conn: sqlite3.Connection) -> None:
        columns = conn.execute(f"PRAGMA table_info({self.table_name})").fetchall()
        if not columns:
            raise ValueError(
                f'SQLite database does not contain a "{self.table_name}" table'
            )

    def __iter__(self) -> Generator[Message, None, None]:
        conn = self._connect()
        try:
            self._check_table(conn)
            for frame_data in conn.execute(f"SELECT * FROM {self.table_name}"):
                yield SqliteReader._assemble_message(frame_data)
        finally:
            conn.close()

    @staticmethod
    def _assemble_message(frame_data: _MessageTuple) -> Message:
        timestamp, can_id, is_extended, is_remote, is_error, dlc, data = frame_data
        return Message(
            timestamp=timestamp,
            is_remote_frame=bool(is_remote),
            is_extended_id=bool(is_extended),
            is_error_frame=bool(is_error),
            arbitration_id=can_id,
            dlc=dlc,
            data=data,
        )

    def __len__(self) -> int:
        # this might not run in constant time
        conn = self._connect()
        try:
            self._check_table(conn)
            result = conn.execute(f"SELECT COUNT(*) FROM {self.table_name}")
            return int(result.fetchone()[0])
        finally:
            conn.close()

    def read_all(self) -> Iterator[Message]:
        """Fetches all messages in the database.

        :rtype: Generator[can.Message]
        """
        conn = self._connect()
        try:
            self._check_table(conn)
            result = conn.execute(f"SELECT * FROM {self.table_name}").fetchall()
        finally:
            conn.close()
        return (SqliteReader._assemble_message(frame) for frame in result)

    @property
    def checkpoint(self) -> SqliteCheckpoint | None:
        """The last committed position, if any."""
        return self._checkpoint

    @property
    def pending_batch(self) -> SqliteBatch | None:
        """The batch returned by :meth:`read_new` but not committed yet."""
        return self._pending_batch

    def set_checkpoint(
        self, checkpoint: SqliteCheckpoint | Mapping[str, Any] | None
    ) -> None:
        """Replace the committed position without reading any rows."""
        if checkpoint is not None and not isinstance(checkpoint, SqliteCheckpoint):
            checkpoint = SqliteCheckpoint.from_dict(checkpoint)
        with self._lock:
            self._checkpoint = checkpoint
            self._pending_batch = None

    def read_new(self, max_messages: int | None = None) -> SqliteBatch:
        """Read one batch up to the currently visible database boundary.

        The maximum rowid is determined in a read transaction before any rows
        are fetched, so rows appended concurrently are only visible to a later
        call.  Calling this method again before :meth:`commit` returns the same
        pending batch, which makes retrying a failed persist step safe.

        :param max_messages: optionally limit this batch to at most this many
                             rows; the checkpoint then advances only to the last
                             row actually returned
        :return: the bounded batch, with enough metadata to distinguish old
                 rows, later appends and a replaced database file
        """
        if max_messages is not None and max_messages <= 0:
            raise ValueError("max_messages must be positive or None")
        with self._lock:
            if self._pending_batch is not None:
                return self._pending_batch

            identity = self._file_identity()
            previous_checkpoint = self._checkpoint
            replaced = False
            conn = self._connect()
            try:
                # A single read transaction provides one consistent snapshot for
                # both the boundary and the rows in this batch.
                conn.execute("BEGIN")
                try:
                    self._check_table(conn)
                    visible_boundary = int(
                        conn.execute(
                            f"SELECT COALESCE(MAX(rowid), 0) FROM {self.table_name}"
                        ).fetchone()[0]
                    )
                    if (
                        previous_checkpoint is not None
                        and not self._checkpoint_matches(
                            conn, previous_checkpoint, visible_boundary, identity
                        )
                    ):
                        replaced = True

                    start_rowid = (
                        0
                        if previous_checkpoint is None or replaced
                        else previous_checkpoint.last_rowid
                    )
                    query = (
                        f"SELECT rowid, {self._select_clause()} FROM {self.table_name} "
                        "WHERE rowid > ? AND rowid <= ? ORDER BY rowid"
                    )
                    params: tuple[int, ...] = (start_rowid, visible_boundary)
                    if max_messages is not None:
                        query += " LIMIT ?"
                        params += (max_messages,)
                    rows = conn.execute(query, params).fetchall()
                    boundary = rows[-1][0] if rows else start_rowid
                    boundary_row = (
                        conn.execute(
                            f"SELECT {self._select_clause()} FROM {self.table_name} "
                            "WHERE rowid = ?",
                            (boundary,),
                        ).fetchone()
                        if boundary
                        else None
                    )
                finally:
                    conn.rollback()
            finally:
                conn.close()

            anchor = (
                self._row_anchor(boundary_row) if boundary_row is not None else ""
            )
            checkpoint = SqliteCheckpoint(
                generation=identity, last_rowid=boundary, anchor=anchor
            )
            batch = SqliteBatch(
                messages=tuple(self._assemble_message(row[1:]) for row in rows),
                checkpoint=checkpoint,
                start_rowid=start_rowid,
                end_rowid=boundary,
                visible_boundary=visible_boundary,
                first_rowid=rows[0][0] if rows else None,
                last_rowid=rows[-1][0] if rows else None,
                replaced=replaced,
                previous_checkpoint=previous_checkpoint if replaced else None,
                superseded=not self._identity_matches(identity),
            )
            self._pending_batch = batch
            return batch

    def _checkpoint_matches(
        self,
        conn: sqlite3.Connection,
        checkpoint: SqliteCheckpoint,
        boundary: int,
        identity: str,
    ) -> bool:
        """Check that a checkpoint still describes the current database state."""
        if checkpoint.generation != identity:
            return False
        if boundary < checkpoint.last_rowid:
            return False
        if checkpoint.last_rowid == 0:
            return True
        if not checkpoint.anchor:
            return False

        row = conn.execute(
            f"SELECT {self._select_clause()} FROM {self.table_name} WHERE rowid = ?",
            (checkpoint.last_rowid,),
        ).fetchone()
        return row is not None and self._row_anchor(row) == checkpoint.anchor

    def commit(self, batch: SqliteBatch | None = None) -> SqliteCheckpoint:
        """Advance the committed checkpoint after a batch has been persisted.

        The database is checked again before progress advances.  If the file
        was replaced or rewritten in place after the batch was read, the batch
        is dropped and no checkpoint is committed.

        :param batch: optionally pass the batch that was persisted; a different
                      batch than the pending one is rejected
        :return: the newly committed checkpoint
        :raises RuntimeError: if no batch is pending, another batch is given,
                              or the database changed after the batch was read
        """
        with self._lock:
            if self._pending_batch is None:
                raise RuntimeError("no pending SQLite batch to commit")
            if batch is not None and batch is not self._pending_batch:
                raise RuntimeError("the given batch is not the pending SQLite batch")
            if not self._batch_still_valid(self._pending_batch):
                self._pending_batch = None
                raise RuntimeError(
                    "the SQLite database changed after the batch was read; "
                    "discard the batch and read it again"
                )
            self._checkpoint = self._pending_batch.checkpoint
            self._pending_batch = None
            return self._checkpoint

    def _batch_still_valid(self, batch: SqliteBatch) -> bool:
        try:
            identity = self._file_identity()
        except FileNotFoundError:
            return False
        if identity != batch.checkpoint.generation:
            return False
        committed_rowid = batch.checkpoint.last_rowid
        if committed_rowid == 0:
            return True
        if not batch.checkpoint.anchor:
            return False

        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT {self._select_clause()} FROM {self.table_name} WHERE rowid = ?",
                (committed_rowid,),
            ).fetchone()
        finally:
            conn.close()
        return row is not None and self._row_anchor(row) == batch.checkpoint.anchor

    def discard(self) -> None:
        """Drop the pending batch without advancing the committed checkpoint."""
        with self._lock:
            self._pending_batch = None

    def stop(self) -> None:
        """Release resources held by the reader."""
        with self._lock:
            self._pending_batch = None


class SqliteWriter(MessageWriter, BufferedReader):
    """Logs received CAN data to a simple SQL database.

    The sqlite database may already exist, otherwise it will
    be created when the first message arrives.

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
            f"INSERT INTO {self.table_name} VALUES (?, ?, ?, ?, ?, ?, ?)"
        )

    @staticmethod
    def _create_db(file: StringPathLike, table_name: str) -> sqlite3.Connection:
        """Creates a new databae or opens a connection to an existing one.

        .. note::
            You can't share sqlite3 connections between threads (by default)
            hence we setup the db here. It has the upside of running async.
        """
        log.debug("Creating sqlite database")
        conn = sqlite3.connect(file)

        # create table structure
        conn.cursor().execute(f"""CREATE TABLE IF NOT EXISTS {table_name}
            (
              ts REAL,
              arbitration_id INTEGER,
              extended INTEGER,
              remote INTEGER,
              error INTEGER,
              dlc INTEGER,
              data BLOB
            )""")
        conn.commit()

        return conn

    def _db_writer_thread(self) -> None:
        conn = SqliteWriter._create_db(self._db_filename, self.table_name)

        try:
            while True:
                messages: list[_MessageTuple] = []  # reset buffer

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
