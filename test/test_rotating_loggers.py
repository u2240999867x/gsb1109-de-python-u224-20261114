#!/usr/bin/env python

"""
Test rotating loggers
"""

import os
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

import pytest

import can
from can.io.generic import FileIOMessageWriter
from can.io.logger import RotatedLogSegment
from can.typechecking import StringPathLike

from .data.example_data import generate_message


class TestBaseRotatingLogger:
    @staticmethod
    def _get_instance(file: StringPathLike) -> can.io.BaseRotatingLogger:
        class SubClass(can.io.BaseRotatingLogger):
            """Subclass that implements abstract methods for testing."""

            _supported_formats = {".asc", ".blf", ".csv", ".log", ".txt"}

            def __init__(self, file: StringPathLike, **kwargs) -> None:
                super().__init__(**kwargs)
                suffix = Path(file).suffix.lower()
                if suffix not in self._supported_formats:
                    raise ValueError(f"Unsupported file format: {suffix}")
                self._writer = can.Logger(filename=file)

            @property
            def writer(self) -> FileIOMessageWriter:
                return cast(FileIOMessageWriter, self._writer)

            def should_rollover(self, msg: can.Message) -> bool:
                return False

            def do_rollover(self): ...

        return SubClass(file=file)

    def test_import(self):
        assert hasattr(can.io, "BaseRotatingLogger")

    def test_attributes(self):
        assert issubclass(can.io.BaseRotatingLogger, can.Listener)
        assert hasattr(can.io.BaseRotatingLogger, "namer")
        assert hasattr(can.io.BaseRotatingLogger, "rotator")
        assert hasattr(can.io.BaseRotatingLogger, "rollover_count")
        assert hasattr(can.io.BaseRotatingLogger, "writer")
        assert hasattr(can.io.BaseRotatingLogger, "rotation_filename")
        assert hasattr(can.io.BaseRotatingLogger, "rotate")
        assert hasattr(can.io.BaseRotatingLogger, "on_message_received")
        assert hasattr(can.io.BaseRotatingLogger, "stop")
        assert hasattr(can.io.BaseRotatingLogger, "should_rollover")
        assert hasattr(can.io.BaseRotatingLogger, "do_rollover")

    def test_get_new_writer(self, tmp_path):
        with self._get_instance(tmp_path / "file.ASC") as logger_instance:
            assert isinstance(logger_instance.writer, can.ASCWriter)

        with self._get_instance(tmp_path / "file.BLF") as logger_instance:
            assert isinstance(logger_instance.writer, can.BLFWriter)

        with self._get_instance(tmp_path / "file.CSV") as logger_instance:
            assert isinstance(logger_instance.writer, can.CSVWriter)

        with self._get_instance(tmp_path / "file.LOG") as logger_instance:
            assert isinstance(logger_instance.writer, can.CanutilsLogWriter)

        with self._get_instance(tmp_path / "file.TXT") as logger_instance:
            assert isinstance(logger_instance.writer, can.Printer)

    def test_rotation_filename(self, tmp_path):
        with self._get_instance(tmp_path / "__unused.txt") as logger_instance:
            default_name = "default"
            assert logger_instance.rotation_filename(default_name) == "default"

            logger_instance.namer = lambda x: x + "_by_namer"
            assert logger_instance.rotation_filename(default_name) == "default_by_namer"

    def test_rotate_without_rotator(self, tmp_path):
        source = str(tmp_path / "source.txt")
        dest = str(tmp_path / "dest.txt")

        assert os.path.exists(source) is False
        assert os.path.exists(dest) is False

        with self._get_instance(source) as logger_instance:
            # use context manager to create `source` file and close it
            pass

        assert os.path.exists(source) is True
        assert os.path.exists(dest) is False

        logger_instance.rotate(source, dest)

        assert os.path.exists(source) is False
        assert os.path.exists(dest) is True

    def test_rotate_with_rotator(self, tmp_path):
        source = str(tmp_path / "source.txt")
        dest = str(tmp_path / "dest.txt")

        assert os.path.exists(source) is False
        assert os.path.exists(dest) is False

        with self._get_instance(source) as logger_instance:
            # use context manager to create `source` file and close it
            pass

        rotator_func = Mock()
        logger_instance.rotator = rotator_func
        logger_instance._writer = logger_instance._get_new_writer(source)
        logger_instance.stop()

        assert os.path.exists(source) is True
        assert os.path.exists(dest) is False

        logger_instance.rotate(source, dest)
        rotator_func.assert_called_with(source, dest)

        # assert that no rotation was performed since rotator_func
        # does not do anything
        assert os.path.exists(source) is True
        assert os.path.exists(dest) is False

    def test_stop(self, tmp_path):
        """Test if stop() method of writer is called."""
        with self._get_instance(tmp_path / "file.ASC") as logger_instance:
            # replace stop method of writer with Mock
            mock_stop = Mock(side_effect=logger_instance.writer.stop)
            logger_instance.writer.stop = mock_stop

            logger_instance.stop()
            mock_stop.assert_called()

    def test_on_message_received(self, tmp_path):
        with self._get_instance(tmp_path / "file.ASC") as logger_instance:
            # Test without rollover
            should_rollover = Mock(return_value=False)
            do_rollover = Mock()
            writers_on_message_received = Mock()

            logger_instance.should_rollover = should_rollover
            logger_instance.do_rollover = do_rollover
            logger_instance.writer.on_message_received = writers_on_message_received

            msg = generate_message(0x123)
            logger_instance.on_message_received(msg)

            should_rollover.assert_called_with(msg)
            do_rollover.assert_not_called()
            writers_on_message_received.assert_called_with(msg)

            # Test with rollover
            should_rollover = Mock(return_value=True)
            do_rollover = Mock()
            writers_on_message_received = Mock()

            logger_instance.should_rollover = should_rollover
            logger_instance.do_rollover = do_rollover
            logger_instance.writer.on_message_received = writers_on_message_received

            msg = generate_message(0x123)
            logger_instance.on_message_received(msg)

            should_rollover.assert_called_with(msg)
            do_rollover.assert_called()
            writers_on_message_received.assert_called_with(msg)

    def test_issue_1792(self, tmp_path):
        filepath = tmp_path / "2017_Jeep_Grand_Cherokee_3.6L_V6.log"
        with self._get_instance(filepath) as logger_instance:
            assert isinstance(logger_instance.writer, can.CanutilsLogWriter)


class TestSizedRotatingLogger:
    def test_import(self):
        assert hasattr(can.io, "SizedRotatingLogger")
        assert hasattr(can, "SizedRotatingLogger")

    def test_attributes(self):
        assert issubclass(can.SizedRotatingLogger, can.io.BaseRotatingLogger)
        assert hasattr(can.SizedRotatingLogger, "namer")
        assert hasattr(can.SizedRotatingLogger, "rotator")
        assert hasattr(can.SizedRotatingLogger, "should_rollover")
        assert hasattr(can.SizedRotatingLogger, "do_rollover")

    def test_create_instance(self, tmp_path):
        base_filename = "mylogfile.ASC"
        max_bytes = 512

        with can.SizedRotatingLogger(
            base_filename=tmp_path / base_filename, max_bytes=max_bytes
        ) as logger_instance:
            assert Path(logger_instance.base_filename).name == base_filename
            assert logger_instance.max_bytes == max_bytes
            assert logger_instance.rollover_count == 0
            assert isinstance(logger_instance.writer, can.ASCWriter)

    def test_should_rollover(self, tmp_path):
        base_filename = "mylogfile.ASC"
        max_bytes = 512

        with can.SizedRotatingLogger(
            base_filename=tmp_path / base_filename, max_bytes=max_bytes
        ) as logger_instance:
            msg = generate_message(0x123)
            do_rollover = Mock()
            logger_instance.do_rollover = do_rollover

            logger_instance.writer.file.tell = Mock(return_value=511)
            assert logger_instance.should_rollover(msg) is False
            logger_instance.on_message_received(msg)
            do_rollover.assert_not_called()

            logger_instance.writer.file.tell = Mock(return_value=512)
            assert logger_instance.should_rollover(msg) is True
            logger_instance.on_message_received(msg)
            do_rollover.assert_called()

    def test_logfile_size(self, tmp_path):
        base_filename = "mylogfile.ASC"
        max_bytes = 1024
        msg = generate_message(0x123)

        with can.SizedRotatingLogger(
            base_filename=tmp_path / base_filename, max_bytes=max_bytes
        ) as logger_instance:
            for _ in range(128):
                logger_instance.on_message_received(msg)

            for file_path in os.listdir(tmp_path):
                assert os.path.getsize(tmp_path / file_path) <= 1100

    def test_logfile_size_context_manager(self, tmp_path):
        base_filename = "mylogfile.ASC"
        max_bytes = 1024
        msg = generate_message(0x123)

        with can.SizedRotatingLogger(
            base_filename=tmp_path / base_filename, max_bytes=max_bytes
        ) as logger_instance:
            for _ in range(128):
                logger_instance.on_message_received(msg)

            for file_path in os.listdir(tmp_path):
                assert os.path.getsize(os.path.join(tmp_path, file_path)) <= 1100

    @staticmethod
    def _force_rollover(logger_instance: can.SizedRotatingLogger, msg) -> None:
        """Deliver one message while the size threshold is always exceeded."""
        with patch.object(type(logger_instance.writer), "file_size", return_value=10**9):
            logger_instance.on_message_received(msg)

    def test_no_completed_segments_before_rollover(self, tmp_path):
        with can.SizedRotatingLogger(
            base_filename=tmp_path / "mylogfile.ASC", max_bytes=512
        ) as logger_instance:
            logger_instance.on_message_received(generate_message(0x123))
            assert logger_instance.claim_completed_segments() == ()

    def test_rolled_over_segments_are_claimable_in_order(self, tmp_path):
        msg = generate_message(0x123)
        with can.SizedRotatingLogger(
            base_filename=tmp_path / "mylogfile.ASC", max_bytes=512
        ) as logger_instance:
            logger_instance.on_message_received(msg)
            logger_instance.on_message_received(msg)
            self._force_rollover(logger_instance, msg)
            self._force_rollover(logger_instance, msg)

            segments = logger_instance.claim_completed_segments()
            assert len(segments) == 2
            assert all(isinstance(segment, RotatedLogSegment) for segment in segments)
            assert [segment.index for segment in segments] == [0, 1]
            assert all(segment.rolled_over for segment in segments)
            assert [segment.message_count for segment in segments] == [2, 1]
            for segment in segments:
                assert os.path.isabs(segment.filename)
                assert os.path.exists(segment.filename)

    def test_duplicate_notification_acknowledges_segment_once(self, tmp_path):
        msg = generate_message(0x123)
        with can.SizedRotatingLogger(
            base_filename=tmp_path / "mylogfile.ASC", max_bytes=512
        ) as logger_instance:
            self._force_rollover(logger_instance, msg)

            first = logger_instance.claim_completed_segments()
            # the same notification is redelivered until it is acknowledged
            assert logger_instance.claim_completed_segments() == first
            assert len(first) == 1

            index = first[0].index
            assert logger_instance.acknowledge_segment(index) is True
            # a duplicated notification can never confirm the same volume twice
            assert logger_instance.acknowledge_segment(index) is False
            assert logger_instance.acknowledge_segment(index + 99) is False
            assert logger_instance.claim_completed_segments() == ()

    def test_empty_segment_is_owned_on_stop(self, tmp_path):
        base_filename = tmp_path / "empty.ASC"
        with can.SizedRotatingLogger(
            base_filename=base_filename, max_bytes=512
        ) as logger_instance:
            pass

        segments = logger_instance.claim_completed_segments()
        assert len(segments) == 1
        segment = segments[0]
        assert segment.index == 0
        assert segment.message_count == 0
        assert segment.rolled_over is False
        assert segment.filename == os.path.abspath(base_filename)
        assert os.path.exists(segment.filename)

    def test_empty_rolled_over_segment_is_owned(self, tmp_path):
        msg = generate_message(0x123)
        with can.SizedRotatingLogger(
            base_filename=tmp_path / "mylogfile.ASC", max_bytes=512
        ) as logger_instance:
            # the first segment never receives a message before rolling over
            self._force_rollover(logger_instance, msg)

        segments = logger_instance.claim_completed_segments()
        assert len(segments) == 2
        empty_segment, last_segment = segments
        assert empty_segment.message_count == 0
        assert empty_segment.rolled_over is True
        assert os.path.exists(empty_segment.filename)
        assert last_segment.message_count == 1
        assert last_segment.rolled_over is False

    def test_undersized_last_segment_is_owned_on_stop(self, tmp_path):
        msg = generate_message(0x123)
        with can.SizedRotatingLogger(
            base_filename=tmp_path / "mylogfile.ASC", max_bytes=512
        ) as logger_instance:
            for _ in range(3):
                logger_instance.on_message_received(msg)
            self._force_rollover(logger_instance, msg)

        segments = logger_instance.claim_completed_segments()
        assert len(segments) == 2
        rolled_segment, last_segment = segments
        assert rolled_segment.rolled_over is True
        assert last_segment.rolled_over is False
        assert last_segment.message_count == 1
        assert os.path.exists(last_segment.filename)

    def test_stop_is_idempotent(self, tmp_path):
        with can.SizedRotatingLogger(
            base_filename=tmp_path / "mylogfile.ASC", max_bytes=512
        ) as logger_instance:
            logger_instance.stop()
            logger_instance.stop()

        assert len(logger_instance.claim_completed_segments()) == 1

    def test_failed_last_frame_is_not_announced(self, tmp_path):
        msg = generate_message(0x123)
        with can.SizedRotatingLogger(
            base_filename=tmp_path / "mylogfile.ASC", max_bytes=512
        ) as logger_instance:
            failing_write = Mock(
                side_effect=[None, OSError("cannot write final frame")]
            )

            logger_instance.writer.on_message_received = failing_write
            logger_instance.on_message_received(msg)
            with pytest.raises(OSError, match="final frame"):
                logger_instance.on_message_received(msg)
            failing_write.assert_called_with(msg)

            # closing and rotating the broken volume must not announce it
            logger_instance.do_rollover()
            assert logger_instance.claim_completed_segments() == ()

            # the rollover created a fresh, healthy writer
            logger_instance.on_message_received(msg)

        segments = logger_instance.claim_completed_segments()
        assert len(segments) == 1
        assert segments[0].rolled_over is False
        assert segments[0].message_count == 1

    def test_failed_writer_close_is_not_announced(self, tmp_path):
        logger_instance = can.SizedRotatingLogger(
            base_filename=tmp_path / "mylogfile.ASC", max_bytes=512
        )
        logger_instance.writer.stop = Mock(side_effect=OSError("close failed"))
        with pytest.raises(OSError, match="close failed"):
            logger_instance.do_rollover()

        assert logger_instance.claim_completed_segments() == ()

        # the broken volume stays unannounced even when the logger is stopped
        logger_instance.writer.stop = Mock()
        logger_instance.stop()
        assert logger_instance.claim_completed_segments() == ()

    def test_claimed_segment_can_be_read_and_acknowledged(self, tmp_path):
        msg = generate_message(0x123)
        with can.SizedRotatingLogger(
            base_filename=tmp_path / "mylogfile.ASC", max_bytes=512
        ) as logger_instance:
            for _ in range(3):
                logger_instance.on_message_received(msg)
            self._force_rollover(logger_instance, msg)

            segment = logger_instance.claim_completed_segments()[0]
            with can.LogReader(segment.filename) as reader:
                frames = list(reader)
            assert len(frames) == segment.message_count == 3

            # reader reconnects after confirming: nothing is redelivered
            assert logger_instance.acknowledge_segment(segment.index) is True
            assert logger_instance.claim_completed_segments() == ()
