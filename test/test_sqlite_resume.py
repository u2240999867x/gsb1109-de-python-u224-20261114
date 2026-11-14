"""Tests for resumable, bounded reads of SQLite message logs."""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from can import Message, SqliteReader, SqliteWriter


class SqliteResumeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "messages.db"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _write(self, messages: list[Message], path: Path | None = None) -> None:
        with SqliteWriter(path or self.db) as writer:
            for message in messages:
                writer(message)

    def _messages(self, *timestamps: float) -> list[Message]:
        return [
            Message(timestamp=timestamp, arbitration_id=index, data=[index])
            for index, timestamp in enumerate(timestamps)
        ]

    def test_boundary_is_fixed_before_reading(self) -> None:
        self._write(self._messages(1.0, 2.0, 3.0))
        reader = SqliteReader(self.db)

        batch = reader.read_new()
        # Rows appended after the boundary was determined belong to the next batch.
        self._write(self._messages(4.0, 5.0))
        self.assertEqual([message.timestamp for message in batch], [1.0, 2.0, 3.0])
        self.assertEqual(batch.boundary, batch.last_rowid)

        reader.commit()
        appended = reader.read_new()
        self.assertEqual([message.timestamp for message in appended], [4.0, 5.0])
        self.assertEqual(appended.start_rowid, 3)

    def test_progress_only_advances_on_commit(self) -> None:
        self._write(self._messages(1.0, 2.0))
        reader = SqliteReader(self.db)

        first = reader.read_new(max_messages=1)
        self.assertEqual(reader.checkpoint, None)
        self.assertIs(reader.read_new(max_messages=1), first)
        reader.commit(first)
        self.assertEqual(reader.checkpoint.last_rowid, 1)

        second = reader.read_new(max_messages=1)
        self.assertEqual(second.start_rowid, 1)
        self.assertEqual([message.timestamp for message in second], [2.0])

        # Reading again before committing must not skip or duplicate messages.
        self.assertIs(reader.read_new(max_messages=1), second)
        reader.commit(second)
        self.assertEqual(reader.read_new().count, 0)

    def test_uncommitted_batch_can_be_discarded(self) -> None:
        self._write(self._messages(1.0, 2.0))
        reader = SqliteReader(self.db)

        batch = reader.read_new(max_messages=1)
        reader.discard()
        self.assertEqual(reader.checkpoint, None)

        retry = reader.read_new(max_messages=1)
        self.assertIsNot(retry, batch)
        self.assertEqual(retry.first_rowid, 1)

    def test_commit_rejects_a_foreign_batch(self) -> None:
        self._write(self._messages(1.0))
        reader = SqliteReader(self.db)
        reader.read_new()

        other = SqliteReader(self.db).read_new()
        with self.assertRaises(RuntimeError):
            reader.commit(other)

    def test_checkpoint_round_trips_through_json(self) -> None:
        self._write(self._messages(1.0, 2.0))
        reader = SqliteReader(self.db)
        reader.commit(reader.read_new())
        saved = json.loads(json.dumps(reader.checkpoint.to_dict()))

        resumed = SqliteReader(self.db, checkpoint=saved)
        self.assertIsNone(resumed.read_new().first_rowid)
        self.assertEqual(resumed.checkpoint.last_rowid, 2)

    def test_timestamps_may_repeat_or_move_backwards(self) -> None:
        self._write(self._messages(5.0, 5.0, 3.0, 1.0))
        reader = SqliteReader(self.db)

        first = reader.read_new(max_messages=2)
        reader.commit()
        second = reader.read_new(max_messages=2)
        reader.commit()

        self.assertEqual([message.timestamp for message in first], [5.0, 5.0])
        self.assertEqual([message.timestamp for message in second], [3.0, 1.0])
        self.assertEqual(second.start_rowid, 2)

    def test_append_between_batches_keeps_tail_separate(self) -> None:
        self._write(self._messages(1.0, 2.0))
        reader = SqliteReader(self.db)
        first = reader.read_new()
        reader.commit()

        # The writer continues in the same database while the consumer sleeps.
        self._write(self._messages(3.0, 4.0, 5.0))
        tail = reader.read_new()

        self.assertEqual([message.timestamp for message in first], [1.0, 2.0])
        self.assertEqual([message.timestamp for message in tail], [3.0, 4.0, 5.0])
        self.assertEqual(tail.start_rowid, 2)

    def test_replaced_database_starts_from_the_new_first_row(self) -> None:
        self._write(self._messages(1.0, 2.0, 3.0))
        reader = SqliteReader(self.db)
        reader.commit(reader.read_new())

        replacement = Path(self._dir.name) / "replacement.db"
        self._write(self._messages(10.0, 11.0), replacement)
        os.replace(replacement, self.db)

        batch = reader.read_new()
        self.assertTrue(batch.replaced)
        self.assertEqual(batch.start_rowid, 0)
        self.assertEqual([message.timestamp for message in batch], [10.0, 11.0])
        self.assertEqual(batch.first_rowid, 1)
        # The old checkpoint is retained as context, not silently overwritten.
        self.assertIsNotNone(batch.previous_checkpoint)
        self.assertEqual(batch.previous_checkpoint.last_rowid, 3)

    def test_same_inode_file_rewrite_is_detected(self) -> None:
        self._write(self._messages(1.0, 2.0, 3.0))
        reader = SqliteReader(self.db)
        reader.commit(reader.read_new())

        # Rewrite the file in place: new content, rowids restart at 1.
        with sqlite3.connect(self.db) as conn:
            conn.execute("DELETE FROM messages")
        self._write(self._messages(20.0, 21.0))

        batch = reader.read_new()
        self.assertTrue(batch.replaced)
        self.assertEqual([message.timestamp for message in batch], [20.0, 21.0])
        self.assertEqual(batch.first_rowid, 1)

    def test_same_inode_rewrite_with_same_row_count_is_detected(self) -> None:
        self._write(self._messages(1.0, 2.0, 3.0))
        reader = SqliteReader(self.db)
        reader.commit(reader.read_new())

        with sqlite3.connect(self.db) as conn:
            conn.execute("DELETE FROM messages")
        self._write(self._messages(30.0, 31.0, 32.0))

        batch = reader.read_new()
        self.assertTrue(batch.replaced)
        self.assertEqual([message.timestamp for message in batch], [30.0, 31.0, 32.0])
        self.assertEqual(batch.first_rowid, 1)

    def test_rehearsal_two_batches_append_three_then_replace(self) -> None:
        """Old tail, later appends and the new database's first row stay separate."""
        self._write(self._messages(1.0, 2.0, 3.0, 4.0, 5.0))
        reader = SqliteReader(self.db)

        first = reader.read_new(max_messages=2)
        reader.commit(first)
        self.assertEqual([message.timestamp for message in first], [1.0, 2.0])

        # Production continues writing between the two batches: three more frames.
        self._write(self._messages(6.0, 7.0, 8.0))

        # The second batch reports the wider visible boundary but still starts
        # after the committed row.
        old_tail = reader.read_new(max_messages=3)
        reader.commit(old_tail)
        self.assertEqual([message.timestamp for message in old_tail], [3.0, 4.0, 5.0])
        self.assertEqual(old_tail.start_rowid, 2)
        self.assertEqual(old_tail.first_rowid, 3)
        self.assertEqual(old_tail.last_rowid, 5)
        self.assertEqual(old_tail.boundary, 8)

        appended = reader.read_new()
        reader.commit(appended)
        self.assertEqual([message.timestamp for message in appended], [6.0, 7.0, 8.0])
        self.assertEqual(appended.start_rowid, 5)
        self.assertEqual(appended.first_rowid, 6)

        # The same path is now a different database whose rowids start at 1.
        replacement = Path(self._dir.name) / "next-night.db"
        self._write(self._messages(9.0, 10.0), replacement)
        os.replace(replacement, self.db)

        new_db = reader.read_new()
        self.assertTrue(new_db.replaced)
        self.assertEqual(new_db.start_rowid, 0)
        self.assertEqual(new_db.first_rowid, 1)
        self.assertEqual([message.timestamp for message in new_db], [9.0, 10.0])
        self.assertEqual(new_db.previous_checkpoint, appended.checkpoint)
        self.assertNotEqual(new_db.checkpoint.generation, appended.checkpoint.generation)

    def test_superseded_batch_cannot_advance_progress(self) -> None:
        self._write(self._messages(1.0, 2.0))
        reader = SqliteReader(self.db)
        batch = reader.read_new()

        replacement = Path(self._dir.name) / "replacement.db"
        self._write(self._messages(3.0, 4.0), replacement)
        os.replace(replacement, self.db)

        with self.assertRaises(RuntimeError):
            reader.commit(batch)
        self.assertIsNone(reader.checkpoint)
        self.assertIsNone(reader.pending_batch)

    def test_checkpoint_from_another_database_is_not_reused(self) -> None:
        self._write(self._messages(1.0, 2.0))
        reader = SqliteReader(self.db)
        reader.commit(reader.read_new())

        other = Path(self._dir.name) / "other.db"
        self._write(self._messages(8.0, 9.0), other)
        resumed = SqliteReader(other, checkpoint=reader.checkpoint)

        batch = resumed.read_new()
        self.assertTrue(batch.replaced)
        self.assertEqual([message.timestamp for message in batch], [8.0, 9.0])

    def test_checkpoint_can_be_loaded_from_a_plain_dict(self) -> None:
        self._write(self._messages(1.0, 2.0, 3.0))
        reader = SqliteReader(self.db)
        reader.commit(reader.read_new(max_messages=1))

        saved = reader.checkpoint.to_dict()
        resumed = SqliteReader(self.db, checkpoint=saved)
        batch = resumed.read_new()

        self.assertEqual([message.timestamp for message in batch], [2.0, 3.0])
        self.assertEqual(batch.start_rowid, 1)

    def test_invalid_checkpoint_values_are_rejected(self) -> None:
        self._write(self._messages(1.0))
        for values in (
            {},
            {"generation": "x"},
            {"generation": "x", "last_rowid": -1},
            {"generation": "x", "last_rowid": "not-a-number"},
        ):
            with self.assertRaises(ValueError):
                SqliteReader(self.db, checkpoint=values)

    def test_invalid_max_messages_is_rejected(self) -> None:
        self._write(self._messages(1.0))
        reader = SqliteReader(self.db)
        for value in (0, -1):
            with self.assertRaises(ValueError):
                reader.read_new(max_messages=value)

    def test_unknown_table_is_reported(self) -> None:
        self._write(self._messages(1.0))
        reader = SqliteReader(self.db, table_name="missing")
        with self.assertRaises(ValueError):
            reader.read_new()

    def test_missing_file_is_reported(self) -> None:
        reader = SqliteReader(Path(self._dir.name) / "missing.db")
        with self.assertRaises(FileNotFoundError):
            reader.read_new()

    def test_deleted_file_invalidates_a_pending_batch(self) -> None:
        self._write(self._messages(1.0, 2.0))
        reader = SqliteReader(self.db)
        batch = reader.read_new()
        self.db.unlink()

        with self.assertRaises(RuntimeError):
            reader.commit(batch)
        self.assertIsNone(reader.checkpoint)

    def test_existing_iteration_still_reads_everything(self) -> None:
        messages = self._messages(1.0, 2.0, 3.0)
        self._write(messages)
        reader = SqliteReader(self.db)

        self.assertEqual([message.timestamp for message in reader], [1.0, 2.0, 3.0])
        self.assertEqual(len(reader), 3)
        self.assertEqual(
            [message.timestamp for message in reader.read_all()], [1.0, 2.0, 3.0]
        )


if __name__ == "__main__":
    unittest.main()
