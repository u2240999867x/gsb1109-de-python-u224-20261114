#!/usr/bin/env python
"""Tests for SQLite log compatibility and batched migration."""

import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import can
from can.io.sqlite import (
    ALL_COLUMNS,
    SqliteMigrator,
    ensure_schema,
    migrate_sqlite,
)


def create_legacy_db(path, rows, table_name="messages"):
    conn = sqlite3.connect(path)
    conn.execute(
        f"""CREATE TABLE {table_name} (
            ts REAL,
            arbitration_id INTEGER,
            extended INTEGER,
            remote INTEGER,
            error INTEGER,
            dlc INTEGER,
            data BLOB
        )"""
    )
    conn.executemany(
        f"INSERT INTO {table_name} VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn


class TestLegacySqliteCompatibility(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_legacy_flags_are_unknown(self):
        rows = [
            (1.0, 0x123, 0, 0, 0, 2, memoryview(b"\x01\x02")),
            (2.0, 0x123, 0, 0, 0, 2, memoryview(b"\x01\x02")),
        ]
        with create_legacy_db(self.path, rows):
            pass

        with can.SqliteReader(self.path) as reader:
            messages = list(reader)

        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].channel, None)
        self.assertIsNone(messages[0].is_fd)
        self.assertIsNone(messages[0].bitrate_switch)
        self.assertIsNone(messages[0].error_state_indicator)
        # Data, order and duplicate frames are preserved.
        self.assertEqual(messages[0].data, bytearray(b"\x01\x02"))
        self.assertEqual(messages[1].data, bytearray(b"\x01\x02"))

    def test_writer_upgrades_old_table_without_reordering(self):
        old = [
            (2.0, 0x10, 0, 0, 0, 1, memoryview(b"\xaa")),
            (1.0, 0x10, 0, 0, 0, 1, memoryview(b"\xaa")),
        ]
        with create_legacy_db(self.path, old):
            pass

        with can.SqliteWriter(self.path) as writer:
            writer(
                can.Message(
                    timestamp=3.0,
                    arbitration_id=0x20,
                    is_extended_id=False,
                    is_fd=True,
                    channel="vcan0",
                    data=[1, 2, 3],
                )
            )

        with can.SqliteReader(self.path) as reader:
            messages = list(reader)

        self.assertEqual(len(messages), 3)
        self.assertEqual([m.timestamp for m in messages[:2]], [2.0, 1.0])
        self.assertIsNone(messages[0].is_fd)
        self.assertIsNone(messages[1].is_fd)
        self.assertFalse(messages[2].is_fd is None)
        self.assertTrue(messages[2].is_fd)
        self.assertEqual(messages[2].channel, "vcan0")

    def test_classic_frame_from_new_writer_is_false_not_unknown(self):
        with can.SqliteWriter(self.path) as writer:
            writer(
                can.Message(
                    timestamp=1.0,
                    arbitration_id=0x1,
                    is_extended_id=False,
                )
            )
        with can.SqliteReader(self.path) as reader:
            message = next(iter(reader))
        self.assertIs(message.is_fd, False)
        self.assertIs(message.bitrate_switch, False)
        self.assertIs(message.error_state_indicator, False)

    def test_textual_zero_flag_is_not_true(self):
        # Some tools write flags as text rather than integers.
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                """CREATE TABLE messages (
                    ts REAL, arbitration_id INTEGER, extended INTEGER,
                    remote INTEGER, error INTEGER, dlc INTEGER, data BLOB,
                    channel, is_fd, bitrate_switch, error_state_indicator,
                    source_db, source_row
                )"""
            )
            conn.execute(
                "INSERT INTO messages VALUES (1, 1, 0, 0, 0, 1, x'01', "
                "NULL, '0', '0', '0', NULL, NULL)"
            )
            conn.commit()
        finally:
            conn.close()
        with can.SqliteReader(self.path) as reader:
            message = next(iter(reader))
        self.assertIs(message.is_fd, False)
        self.assertIs(message.bitrate_switch, False)
        self.assertIs(message.error_state_indicator, False)


class TestMigration(unittest.TestCase):
    def setUp(self):
        fd, self.source = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.source)
        fd, self.target = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.target)
        self.rows = [
            (10.0, 0x100, 0, 0, 0, 1, memoryview(b"\x01")),
            (11.0, 0x101, 0, 0, 0, 1, memoryview(b"\x02")),
            (11.0, 0x101, 0, 0, 0, 1, memoryview(b"\x02")),
            (12.0, 0x102, 0, 0, 0, 1, memoryview(b"\x03")),
            (13.0, 0x103, 0, 1, 0, 0, memoryview(b"")),
            (14.0, 0x104, 1, 0, 1, 3, memoryview(b"\x04\x05\x06")),
        ]

    def tearDown(self):
        for path in (self.source, self.target):
            if os.path.exists(path):
                os.remove(path)

    def _create_source(self):
        conn = create_legacy_db(self.source, self.rows)
        conn.close()

    def test_batches_can_be_interrupted_and_resumed(self):
        self._create_source()
        first = SqliteMigrator(self.source, self.target, batch_size=2)
        progress = first.step()
        self.assertEqual(progress.batch, 2)
        self.assertEqual(progress.migrated, 2)
        self.assertFalse(progress.done)

        # Simulate a process restart: a fresh migrator must resume, not
        # duplicate or skip rows.
        resumed = SqliteMigrator(self.source, self.target, batch_size=2)
        self.assertEqual(resumed.rows_migrated, 2)
        resumed.run()

        with can.SqliteReader(self.target) as reader:
            messages = list(reader)
        self.assertEqual(len(messages), len(self.rows))
        self.assertEqual(
            [m.arbitration_id for m in messages],
            [row[1] for row in self.rows],
        )
        self.assertEqual(messages[1].data, bytearray(b"\x02"))
        self.assertEqual(messages[2].data, bytearray(b"\x02"))
        self.assertEqual(messages[5].is_error_frame, True)

    def test_failed_batch_is_retried_without_duplication(self):
        self._create_source()
        migrator = SqliteMigrator(self.source, self.target, batch_size=3)
        self.assertEqual(migrator.step().batch, 3)

        # A new migrator reads only committed progress.
        resumed = SqliteMigrator(self.source, self.target, batch_size=3)
        remaining = resumed.step()
        self.assertEqual(remaining.migrated, 6)
        self.assertTrue(remaining.done)

        with can.SqliteReader(self.target) as reader:
            messages = list(reader)
        self.assertEqual([m.timestamp for m in messages], [r[0] for r in self.rows])

    def test_provenance_lookup_survives_appending(self):
        self._create_source()
        SqliteMigrator(self.source, self.target, batch_size=3).run()

        with can.SqliteWriter(self.target) as writer:
            writer(
                can.Message(
                    timestamp=100.0,
                    arbitration_id=0x999,
                    is_extended_id=False,
                    is_fd=True,
                    channel=7,
                    data=[9],
                )
            )

        with can.SqliteReader(self.target) as reader:
            originals = list(reader.find(self.source, 3))
            triples = list(reader.iter_with_origin())

        self.assertEqual(len(originals), 1)
        self.assertEqual(originals[0].arbitration_id, 0x101)
        self.assertEqual(originals[0].data, bytearray(b"\x02"))
        self.assertIsNone(originals[0].is_fd)
        # New row does not carry old provenance.
        self.assertEqual(triples[-1][1], None)
        self.assertEqual(triples[-1][2], None)

    def test_in_place_migration_keeps_original_rowids_and_flags(self):
        self._create_source()
        migrator = SqliteMigrator(self.source, batch_size=2)
        migrator.run()

        conn = sqlite3.connect(self.source)
        try:
            rowids = conn.execute("SELECT rowid FROM messages ORDER BY rowid").fetchall()
            columns = [row[1] for row in conn.execute("PRAGMA table_info(messages)")]
        finally:
            conn.close()
        self.assertEqual([r[0] for r in rowids], list(range(1, len(self.rows) + 1)))
        self.assertEqual(columns, list(ALL_COLUMNS))

        # Read back: old FD flags still unknown, metadata present.
        with can.SqliteReader(self.source) as reader:
            messages = list(reader.iter_with_origin())
        self.assertEqual(messages[0][0].arbitration_id, 0x100)
        self.assertEqual(messages[0][1], self.source)
        self.assertEqual(messages[0][2], 1)
        self.assertIsNone(messages[0][0].is_fd)

    def test_same_file_different_tables_copies_without_touching_source(self):
        self._create_source()
        SqliteMigrator(
            self.source,
            self.source,
            table_name="migrated",
            source_table_name="messages",
            batch_size=3,
        ).run()

        conn = sqlite3.connect(self.source)
        try:
            source_columns = [
                row[1] for row in conn.execute("PRAGMA table_info(messages)")
            ]
            target_count = conn.execute("SELECT COUNT(*) FROM migrated").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(source_columns, ["ts", "arbitration_id", "extended",
                                          "remote", "error", "dlc", "data"])
        self.assertEqual(target_count, len(self.rows))

    def test_origin_lookup_does_not_match_same_basename(self):
        self._create_source()
        SqliteMigrator(self.source, self.target, batch_size=2).run()

        # A different archive with the same filename stem must not be
        # mistaken for the original.
        other_dir = tempfile.mkdtemp()
        other = os.path.join(other_dir, os.path.basename(self.source))
        try:
            with can.SqliteReader(self.target) as reader:
                self.assertEqual(list(reader.find(other, 1)), [])
                self.assertEqual(len(list(reader.find(self.source, 1))), 1)
        finally:
            if os.path.exists(other):
                os.remove(other)
            os.rmdir(other_dir)

    def test_migrate_convenience_function_is_idempotent(self):
        self._create_source()
        self.assertEqual(
            migrate_sqlite(self.source, self.target, batch_size=4),
            len(self.rows),
        )
        self.assertEqual(migrate_sqlite(self.source, self.target), 0)
        with can.SqliteReader(self.target) as reader:
            self.assertEqual(len(list(reader)), len(self.rows))

    def test_numeric_archive_id_is_preserved(self):
        self._create_source()
        SqliteMigrator(
            self.source,
            self.target,
            batch_size=2,
            source_db=42,
        ).run()
        with can.SqliteReader(self.target) as reader:
            triple = next(reader.iter_with_origin())
            found = reader.find_one(source_db=42, source_row=1)
        self.assertEqual(triple[1], 42)
        self.assertEqual(triple[2], 1)
        self.assertEqual(found.arbitration_id, 0x100)

    def test_duplicate_frames_keep_order_across_batches(self):
        self._create_source()
        migrator = SqliteMigrator(self.source, self.target, batch_size=1)
        while not migrator.step().done:
            pass
        with can.SqliteReader(self.target) as reader:
            messages = list(reader)
        # rows[1] and rows[2] are intentional duplicate frames
        self.assertEqual(messages[1].timestamp, messages[2].timestamp)
        self.assertEqual(messages[1].data, messages[2].data)
        self.assertEqual(messages[1].arbitration_id, messages[2].arbitration_id)
        self.assertEqual(
            [m.timestamp for m in messages],
            [row[0] for row in self.rows],
        )

    def test_partial_schema_upgrade_can_continue(self):
        self._create_source()
        conn = sqlite3.connect(self.source)
        try:
            # Simulate an interrupted upgrade after one added column.
            conn.execute("ALTER TABLE messages ADD COLUMN channel")
            conn.commit()
        finally:
            conn.close()

        migrator = SqliteMigrator(self.source, batch_size=3)
        migrator.run()
        with can.SqliteReader(self.source) as reader:
            messages = list(reader)
        self.assertEqual(len(messages), len(self.rows))
        self.assertIsNone(messages[0].channel)
        self.assertIsNone(messages[0].is_fd)

    def test_malformed_source_does_not_modify_target(self):
        conn = sqlite3.connect(self.source)
        try:
            conn.execute("CREATE TABLE messages (not_a_can_log TEXT)")
            conn.commit()
        finally:
            conn.close()
        before = os.path.getsize(self.target) if os.path.exists(self.target) else None

        with self.assertRaises(ValueError):
            SqliteMigrator(self.source, self.target).run()

        after = os.path.getsize(self.target) if os.path.exists(self.target) else None
        self.assertEqual(before, after)

    def test_failed_transaction_rolls_back_batch(self):
        self._create_source()
        migrator = SqliteMigrator(self.source, self.target, batch_size=3)
        with mock.patch.object(
            migrator, "_write_state", side_effect=RuntimeError("simulated crash")
        ):
            with self.assertRaises(RuntimeError):
                migrator.step()

        with can.SqliteReader(self.target) as reader:
            self.assertEqual(len(list(reader)), 0)

        resumed = SqliteMigrator(self.source, self.target, batch_size=3)
        self.assertEqual(resumed.run(), len(self.rows))
        with can.SqliteReader(self.target) as reader:
            self.assertEqual(len(list(reader)), len(self.rows))

    def test_migrated_target_can_be_migrated_again_without_losing_origin(self):
        self._create_source()
        SqliteMigrator(self.source, self.target, batch_size=2).run()

        chain_fd, chain = tempfile.mkstemp(suffix=".db")
        os.close(chain_fd)
        os.remove(chain)
        try:
            SqliteMigrator(self.target, chain, batch_size=4).run()
            with can.SqliteReader(chain) as reader:
                triple = next(reader.iter_with_origin())
            # The immediate source of the second migration did not record
            # which database the first one came from, so preserve the first
            # migration's provenance when it exists.
            self.assertEqual(triple[1], self.source)
            self.assertEqual(triple[2], 1)
        finally:
            if os.path.exists(chain):
                os.remove(chain)


if __name__ == "__main__":
    unittest.main()
