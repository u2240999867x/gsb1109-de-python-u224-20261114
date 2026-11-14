"""
Open compressed files transparently.
"""

__all__ = [
    "Handoff",
    "_PipedCompressionProgram",
    "__version__",
    "handoff",
    "xopen",
]

import bz2
import dataclasses
import gzip
import io
import lzma
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import ModuleType
from typing import IO, BinaryIO, Callable, Literal, overload

from ._version import version as __version__

# 128K buffer size also used by cat, pigz etc. It is faster than the 8K default.
BUFFER_SIZE = max(io.DEFAULT_BUFFER_SIZE, 128 * 1024)

XOPEN_DEFAULT_GZIP_COMPRESSION = 1
XOPEN_DEFAULT_BZ2_COMPRESSION = 9
XOPEN_DEFAULT_XZ_COMPRESSION = 6
XOPEN_DEFAULT_ZST_COMPRESSION = 3

igzip: ModuleType | None
isal_zlib: ModuleType | None
igzip_threaded: ModuleType | None
zlib_ng: ModuleType | None
gzip_ng: ModuleType | None
gzip_ng_threaded: ModuleType | None

try:
    from isal import igzip, igzip_threaded, isal_zlib
except ImportError:
    igzip = None
    isal_zlib = None
    igzip_threaded = None

try:
    from zlib_ng import gzip_ng, gzip_ng_threaded, zlib_ng
except ImportError:
    gzip_ng = None
    gzip_ng_threaded = None
    zlib_ng = None

if sys.version_info >= (3, 14):
    from compression import zstd
else:
    from backports import zstd

try:
    import lz4.frame  # type: ignore
except ImportError:
    lz4 = None

# The lz4 binary accepts nonnegative integer compression levels and clips them to [1, 12].
# python-lz4 accepts both positive (clipped at 16) and negative (unlimited; converted to --fast) compression levels.
# => The shared nonnegative range of [0, 16] works with both.
if lz4 is None:
    _LZ4_LEVEL_MIN, _LZ4_LEVEL_MAX = 0, 16
else:
    _LZ4_LEVEL_MIN = lz4.frame.COMPRESSIONLEVEL_MIN
    _LZ4_LEVEL_MAX = lz4.frame.COMPRESSIONLEVEL_MAX
XOPEN_DEFAULT_LZ4_COMPRESSION = _LZ4_LEVEL_MIN

try:
    with open("/proc/sys/fs/pipe-max-size", "rt", encoding="ascii") as f:
        _MAX_PIPE_SIZE = int(f.read())
except OSError:
    # Catches file not found and permission errors. Possible other errors too.
    # -1 is the default for the Popen pipesize parameter.
    _MAX_PIPE_SIZE = -1


FilePath = str | bytes | os.PathLike
FileOrPath = FilePath | IO


@dataclasses.dataclass
class _ProgramSettings:
    program_args: tuple[str, ...]
    acceptable_compression_levels: tuple[int, ...] = tuple(range(1, 10))
    threads_flag: str | None = None
    # This exit code is not interpreted as an error when terminating the process
    allowed_exit_code: int | None = -signal.SIGTERM
    # If this message is printed on stderr on terminating the process,
    # it is not interpreted as an error
    allowed_exit_message: bytes | None = None


_PROGRAM_SETTINGS: dict[str, _ProgramSettings] = {
    "pbzip2": _ProgramSettings(
        ("pbzip2",),
        tuple(range(1, 10)),
        "-p",
        allowed_exit_code=None,
        allowed_exit_message=b"\n *Control-C or similar caught [sig=15], quitting...",
    ),
    "xz": _ProgramSettings(("xz",), tuple(range(10)), "-T"),
    "zstd": _ProgramSettings(("zstd",), tuple(range(1, 20)), "-T"),
    "pigz": _ProgramSettings(("pigz", "--no-name"), tuple(range(10)) + (11,), "-p"),
    "gzip": _ProgramSettings(("gzip", "--no-name"), tuple(range(1, 10))),
    "lz4": _ProgramSettings(("lz4",), tuple(range(_LZ4_LEVEL_MIN, _LZ4_LEVEL_MAX + 1))),
}


def _available_cpu_count() -> int:
    """
    Number of available virtual or physical CPUs on this system
    Adapted from http://stackoverflow.com/a/1006301/715090
    """
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        pass
    import re

    try:
        with open("/proc/self/status") as f:
            status = f.read()
        m = re.search(r"(?m)^Cpus_allowed:\s*(.*)$", status)
        if m:
            res = int(m.group(1).replace(",", ""), 16).bit_count()
            if res > 0:
                return res
    except OSError:
        pass
    count = os.cpu_count()
    return 1 if count is None else count


class _PipedCompressionProgram(io.IOBase):
    """
    Read and write compressed files by running an external process and piping into it.
    """

    def __init__(
        self,
        filename: FileOrPath,
        mode="rb",
        compresslevel: int | None = None,
        threads: int | None = None,
        program_settings: _ProgramSettings = _ProgramSettings(("gzip", "--no-name")),
    ):
        """
        mode -- one of 'w', 'wb', 'a', 'ab'
        compresslevel -- compression level
        threads_flag -- which flag is used to denote the number of threads in the program.
            If set to none, program will be called without threads flag.
        threads (int) -- number of threads. If this is set to None, a reasonable default is
            used. At the moment, this means that the number of available CPU cores is used, capped
            at four to avoid creating too many threads. Use 0 to use all available cores.
        """
        self._error_raised = False
        self._program_args = list(program_settings.program_args)
        self._allowed_exit_code = program_settings.allowed_exit_code
        self._allowed_exit_message = program_settings.allowed_exit_message
        if mode not in ("r", "rb", "w", "wb", "a", "ab"):
            raise ValueError(
                f"Mode is '{mode}', but it must be 'r', 'rb', 'w', 'wb', 'a', or 'ab'"
            )
        if "b" not in mode:
            mode += "b"
        if (
            compresslevel is not None
            and compresslevel not in program_settings.acceptable_compression_levels
        ):
            raise ValueError(
                f"compresslevel must be in {program_settings.acceptable_compression_levels}."
            )
        self._compresslevel = compresslevel
        self.fileobj, self.closefd = _file_or_path_to_binary_stream(filename, mode)
        self._path = _filepath_from_path_or_filelike(filename)
        self.name: str = str(self._path)
        self._mode: str = mode
        self._stderr = tempfile.TemporaryFile("w+b")
        self._threads_flag: str | None = program_settings.threads_flag

        if threads is None:
            if "r" in mode:
                # Reading occurs single threaded by default. This has the least
                # amount of overhead and is fast enough for most use cases.
                threads = 1
            else:
                threads = min(_available_cpu_count(), 4)
        self._threads = threads

        self._open_process()

    def _open_process(self):
        if self._threads != 0 and self._threads_flag is not None:
            self._program_args += [f"{self._threads_flag}{self._threads}"]

        # Setting close_fds to True in the Popen arguments is necessary due to
        # <http://bugs.python.org/issue12786>.
        # However, close_fds is not supported on Windows. See
        # <https://github.com/marcelm/cutadapt/issues/315>.
        close_fds = False
        if sys.platform != "win32":
            close_fds = True

        self.in_pipe = None
        self.in_thread = None
        self._feeding = True
        if "r" in self._mode:
            self._program_args += ["-c", "-d"]  # type: ignore
            stdout = subprocess.PIPE
        else:
            if self._compresslevel is not None:
                self._program_args += ["-" + str(self._compresslevel)]
            stdout = self.fileobj  # type: ignore
        try:
            self.process = subprocess.Popen(
                self._program_args,
                stderr=self._stderr,
                stdout=stdout,
                stdin=subprocess.PIPE,
                close_fds=close_fds,
                pipesize=_MAX_PIPE_SIZE,
            )  # type: ignore
        except OSError:
            if self.closefd:
                self.fileobj.close()
            raise
        assert self.process.stdin is not None
        if "r" in self._mode:
            self.in_pipe = self.process.stdin
            # A python subprocess can read and write from pipes, but not from
            # Python in-memory objects. In order for a program to read from an
            # in-memory object, a pipe must be created. This pipe must be fed
            # data from the in-memory object. This must be done in a separate
            # thread, because IO operations will block when the pipe is full
            # when writing, or empty when reading. Since the quantity of output
            # data generated by a certain amount of input data is unknown, the
            # only way to prevent a blocking application is to write
            # data continuously to the process stdin on another thread.
            self.in_thread = threading.Thread(target=self._feed_pipe)
            self.in_thread.start()
            self._process_explicitly_terminated = False
            self._file: BinaryIO = self.process.stdout  # type: ignore
            self._wait_for_output_or_process_exit()
            self._raise_if_error()
        else:
            self._file = self.process.stdin  # type: ignore

    def __repr__(self):
        return (
            f"{self.__class__.__name__}"
            f"('{self.name}', mode='{self._mode}', "
            f"program='{' '.join(self._program_args)}', "
            f"threads={self._threads})"
        )

    def _feed_pipe(self):
        try:
            while self._feeding:
                chunk = self.fileobj.read(BUFFER_SIZE)
                if chunk == b"":
                    self.in_pipe.close()
                    return
                try:
                    self.in_pipe.write(chunk)
                except BrokenPipeError:
                    if not self._process_explicitly_terminated:
                        raise
        finally:
            self.in_pipe.close()

    def write(self, arg: bytes) -> int:
        return self._file.write(arg)

    def read(self, *args) -> bytes:
        return self._file.read(*args)

    def readinto(self, *args):
        return self._file.readinto(*args)

    def readline(self, *args) -> bytes:
        return self._file.readline(*args)

    def seekable(self) -> bool:
        return False

    def peek(self, n: int | None = None):
        return self._file.peek(n)  # type: ignore

    def close(self) -> None:
        if self.closed:
            return
        super().close()
        if not hasattr(self, "process"):
            # Exception was raised during __init__
            if hasattr(self, "_stderr"):
                self._stderr.close()
            return
        check_allowed_code_and_message = False
        if "r" in self._mode:
            retcode = self.process.poll()
            if retcode is None:
                # still running
                self._process_explicitly_terminated = True
                self.process.terminate()
                check_allowed_code_and_message = True
                self.process.wait()
            self._feeding = False
            if self.in_thread:
                self.in_thread.join()
            self._file.close()
        else:
            self._file.close()
            self.process.wait()
        if self.closefd:
            self.fileobj.close()
        stderr_message = self._read_error_message()
        self._stderr.close()
        if not self._error_raised:
            # Only check for errors if none have been found earlier.
            self._raise_if_error(check_allowed_code_and_message, stderr_message)

    def _wait_for_output_or_process_exit(self):
        """
        Wait for the process to produce at least some output, or has exited.
        """
        # The program may crash due to a non-existing file, internal error etc.
        # In that case we need to check. However the 'time-to-crash' differs
        # between programs. Some crash faster than others.
        # Therefore we peek the first character(s) of stdout. Peek will return at
        # least one byte of data, unless the buffer is empty or at EOF. If at EOF,
        # we should wait for the program to exit. This way we ensure the program
        # has at least decompressed some output, or stopped before we continue.

        # stdout is io.BufferedReader if set to PIPE
        while True:
            first_output = self.process.stdout.peek(1)
            exit_code = self.process.poll()
            if first_output or exit_code is not None:
                break
            time.sleep(0.01)

    def _raise_if_error(
        self, check_allowed_code_and_message: bool = False, stderr_message: bytes = b""
    ) -> None:
        """
        Raise OSError if process is not running anymore and the exit code is
        nonzero. If check_allowed_code_and_message is set, OSError is not raised when
        (1) the exit value of the process is equal to the value of the allowed_exit_code
        attribute or (2) the allowed_exit_message attribute is set and it matches with
        stderr_message.
        """
        retcode = self.process.poll()

        if sys.platform == "win32" and retcode == 1 and stderr_message == b"":
            # Special case for Windows. Winapi terminates processes with exit code 1
            # and an empty error message.
            return

        if retcode is None:
            # process still running
            return
        if retcode == 0:
            # process terminated successfully
            return

        if check_allowed_code_and_message:
            if retcode == self._allowed_exit_code:
                # terminated with allowed exit code
                return
            if self._allowed_exit_message and stderr_message.startswith(
                self._allowed_exit_message
            ):
                # terminated with another exit code, but message is allowed
                return

        if not stderr_message:
            stderr_message = self._read_error_message()

        self._file.close()
        self._error_raised = True
        raise OSError(f"{stderr_message!r} (exit code {retcode})")

    def _read_error_message(self):
        if self._stderr.closed:
            return b""
        self._stderr.flush()
        self._stderr.seek(0)
        return self._stderr.read()

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        return self._file.__next__()

    def readable(self):
        return self._file.readable()

    def writable(self):
        return self._file.writable()

    def flush(self) -> None:
        return None


# Number of business bytes requested from a reader's buffer when taking over a
# stream. The actual amount returned depends on the underlying buffer, but the
# handoff always drains exactly as many bytes as it captured, so this only
# controls how much read-ahead data is surfaced.
HANDOFF_PEEK_SIZE = 1024 * 1024


def _noop_close() -> None:
    # Used to neutralize a TextIOWrapper after its buffer has been detached.
    return None


class _ManagedStream(io.BufferedIOBase):
    """
    Wrap a binary stream while making its ownership explicit.

    Every operation is forwarded to the wrapped stream. ``close()`` only shuts
    the wrapped stream when ``own`` is True (take-over); with ``own`` False the
    handle is *borrowed* and kept alive so an abnormal reader exit or an explicit
    close cannot close the caller's handle. This makes the ownership contract
    identical for plain and compressed, binary and text, piped and in-process
    backends (some of which never close a file object they are given).
    """

    def __init__(self, stream: BinaryIO, own: bool):
        super().__init__()
        self._stream = stream
        self._own = own
        self._is_closed = False

    def readable(self) -> bool:
        return self._stream.readable()

    def writable(self) -> bool:
        return self._stream.writable()

    def seekable(self) -> bool:
        return self._stream.seekable()

    def read(self, *args) -> bytes:
        return self._stream.read(*args)

    def read1(self, *args) -> bytes:
        stream = self._stream
        read1 = getattr(stream, "read1", None)
        if read1 is not None:
            return read1(*args)
        return stream.read(*args)

    def readinto(self, *args):
        return self._stream.readinto(*args)

    def readline(self, *args) -> bytes:
        return self._stream.readline(*args)

    def write(self, arg: bytes) -> int:
        return self._stream.write(arg)

    def flush(self) -> None:
        self._stream.flush()

    def tell(self):
        return self._stream.tell()

    def seek(self, *args):
        return self._stream.seek(*args)

    def fileno(self):
        return self._stream.fileno()

    @property
    def name(self):
        return getattr(self._stream, "name", "")

    @property
    def closed(self) -> bool:
        return self._is_closed

    def close(self) -> None:
        if self._is_closed:
            return
        self._is_closed = True
        if self._own:
            # Take-over: we are responsible for the underlying external handle.
            self._stream.close()
        # Borrowed stream: detach ourselves but keep the wrapped handle alive.

    def __getattr__(self, item):
        # Forward any attribute we did not override (e.g. ``peek``, ``raw``).
        return getattr(self._stream, item)


class _OwningReader(_ManagedStream):
    """
    Take-over wrapper for a backend reader that does not close its file object.

    Native decompressors (and the piped program wrapper in non-owning mode) do
    not close a file object handed to them. This wrapper closes that reader and
    then the managed stream underneath, so a taken-over external handle is
    released exactly once when the top-level reader is closed.
    """

    def __init__(self, reader: BinaryIO, managed: "_ManagedStream"):
        super().__init__(reader, own=True)
        self._managed = managed

    def close(self) -> None:
        if self._is_closed:
            return
        self._is_closed = True
        try:
            self._stream.close()  # close the backend reader
        finally:
            self._managed.close()  # close the external handle it owns


class _ChainedReader(io.BufferedIOBase):
    """
    Present a block of already buffered business bytes followed by the rest of
    a reader as one continuous binary stream.

    The first reader has typically decompressed more bytes than it consumed.
    Those bytes are passed in as ``prefix`` and are yielded before any byte read
    from ``remainder``, so a successor sees an uninterrupted business stream
    starting exactly at the handoff point.
    """

    def __init__(
        self,
        prefix: bytes,
        remainder: BinaryIO,
        closer: Callable[[], None] | None = None,
    ):
        super().__init__()
        self._head = bytearray(prefix)
        self._remainder = remainder
        self._closer = closer
        self._is_closed = False

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def _read_one(self, size: int) -> bytes:
        # Prefer read1 to avoid pulling more than requested, but some streams
        # (e.g. the piped program wrapper) only expose read().
        read1 = getattr(self._remainder, "read1", None)
        if read1 is not None:
            return read1(size)
        return self._remainder.read(size)

    def read(self, size: int | None = -1) -> bytes:
        if size is None or size < 0:
            data = bytes(self._head) + self._remainder.read()
            self._head.clear()
            return data
        if len(self._head) >= size:
            data = bytes(self._head[:size])
            del self._head[:size]
            return data
        data = bytes(self._head)
        self._head.clear()
        if size > len(data):
            data += self._remainder.read(size - len(data))
        return data

    def read1(self, size: int | None = -1) -> bytes:
        # Serve buffered bytes first; once exhausted, forward a single read.
        if self._head:
            if size is None or size < 0:
                data = bytes(self._head)
                self._head.clear()
                return data
            data = bytes(self._head[:size])
            del self._head[:size]
            return data
        if size is None or size < 0:
            read1 = getattr(self._remainder, "read1", None)
            return read1() if read1 is not None else self._remainder.read()
        return self._read_one(size)

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        length = len(data)
        buffer[:length] = data
        return length

    def readline(self, size: int | None = -1) -> bytes:
        limit = -1 if size is None else size
        newline = self._head.find(b"\n")
        if newline != -1:
            line_len = newline + 1
            if limit >= 0:
                line_len = min(line_len, limit)
            data = bytes(self._head[:line_len])
            del self._head[:line_len]
            return data
        if limit >= 0 and len(self._head) >= limit:
            data = bytes(self._head[:limit])
            del self._head[:limit]
            return data
        parts = [bytes(self._head)]
        self._head.clear()
        remaining = -1 if limit < 0 else limit - len(parts[0])
        if remaining != 0:
            parts.append(self._remainder.readline(remaining))
        return b"".join(parts)

    def peek(self, size: int | None = None) -> bytes:
        wanted = BUFFER_SIZE if size is None or size < 0 else max(size, 1)
        if len(self._head) < wanted:
            extra = self._read_one(wanted - len(self._head))
            if extra:
                self._head.extend(extra)
        if size is None or size < 0:
            return bytes(self._head)
        return bytes(self._head[:size])

    @property
    def closed(self) -> bool:
        return self._is_closed

    def close(self) -> None:
        if self._is_closed:
            return
        self._is_closed = True
        # Taking over: the successor now owns the remainder reader's lifecycle.
        # ``_closer`` is the reader's original close (the instance close itself
        # may have been disarmed so the first reader's cleanup cannot tear us
        # down). Borrow mode still guarantees this never closes the external
        # handle.
        try:
            if self._closer is not None:
                self._closer()
            else:
                self._remainder.close()
        finally:
            super().close()


@dataclasses.dataclass
class Handoff:
    """
    Result of handing a partially consumed stream from one reader to the next.

    ``stream`` is a binary reader that yields, in order, the read-ahead but not
    yet consumed business bytes (``pending``) and then the remainder of the
    underlying stream. Reading it from the handoff point and prepending the
    bytes already consumed by the first reader reproduces the original input
    byte for byte.

    When the first reader was opened in text mode, ``encoding``, ``errors`` and
    ``newline`` capture that configuration so ``open()`` can recreate an
    equivalent text reader.
    """

    stream: BinaryIO
    pending: bytes
    encoding: str | None = None
    errors: str | None = None
    newline: str | None = None

    @property
    def is_text(self) -> bool:
        return self.encoding is not None

    def open(self) -> IO:
        if not self.is_text:
            return self.stream
        return io.TextIOWrapper(
            self.stream,
            encoding=self.encoding,
            errors=self.errors,
            newline=self.newline,
        )


def handoff(reader: IO) -> Handoff:
    """
    Hand a partially consumed reader over to a successor.

    The reader may be a binary stream or a :class:`io.TextIOWrapper`. Any
    business bytes the reader has already buffered (for example inflated by a
    decompressor) but not yet handed out are captured as ``pending`` and a new
    binary stream is returned that serves those bytes first and then the rest
    of the original reader. The handoff works identically for plain and
    compressed input and never loses or duplicates a byte.

    Binary handoff is fully streaming: only the bytes already held in the
    reader's buffer are materialized. Text handoff must additionally recover the
    decoded characters cached inside the :class:`io.TextIOWrapper` (which
    ``detach`` would otherwise discard), so the remaining text is read once and
    re-encoded to business bytes.

    Opening the reader in borrow mode (the default for a caller-provided file
    object) ensures the underlying external handle is not closed here, even if
    the first reader terminates abnormally.
    """
    if isinstance(reader, io.TextIOWrapper):
        return _handoff_text(reader)
    stream, pending = _handoff_binary(reader)
    return Handoff(stream=stream, pending=pending)


def _handoff_binary(reader: BinaryIO) -> tuple[BinaryIO, bytes]:
    # The successor keeps reading from this same reader, so disarm its close:
    # tearing the first reader down (normal cleanup or an abnormal exit) must
    # not close the source the successor depends on. The real closer is handed
    # to the successor so lifecycle ownership is preserved (borrow mode already
    # guarantees that closer never shuts the external handle).
    real_close = reader.close
    reader.close = _noop_close  # type: ignore[method-assign]

    # Peek only reports buffered bytes; it never advances the logical position.
    # Streams without a buffer (and thus without peek) have no read-ahead surplus
    # to hand over, so the successor simply continues reading them directly.
    peek = getattr(reader, "peek", None)
    if peek is None:
        pending = b""
    else:
        pending = peek(HANDOFF_PEEK_SIZE)
        if pending:
            # Consume exactly the captured bytes so the remainder starts after them.
            drained = reader.read(len(pending))
            if drained != pending:
                # Defensive: trust what the reader actually gave up.
                pending = drained
    return _ChainedReader(pending, reader, closer=real_close), pending


def _handoff_text(reader: io.TextIOWrapper) -> Handoff:
    encoding = reader.encoding
    errors = reader.errors
    # TextIOWrapper does not expose the newline argument used to construct it;
    # xopen stashes it, otherwise fall back to universal newlines.
    newline = getattr(reader, "_xopen_newline", None)
    # Read the remaining decoded text to the end. This also pulls in any
    # characters the wrapper decoded but had not yet returned; calling detach()
    # before this point would silently discard that decoded cache.
    remaining_text = reader.read()
    # Recover the equivalent business bytes through the same codec.
    prefix = remaining_text.encode(encoding, errors or "strict")
    detached = reader.detach()
    # A detached wrapper raises ValueError on close(). Neutralize it so an
    # abnormal exit of the first (text) reader stays harmless.
    reader.close = _noop_close  # type: ignore[method-assign]
    # The successor takes over the detached binary buffer.
    real_close = detached.close
    detached.close = _noop_close  # type: ignore[method-assign]
    stream = _ChainedReader(prefix, detached, closer=real_close)
    return Handoff(
        stream=stream,
        pending=prefix,
        encoding=encoding,
        errors=errors,
        newline=newline,
    )


def _open_stdin_or_out(mode: str) -> BinaryIO:
    assert mode in ("rb", "ab", "wb")
    std = sys.stdin if mode == "rb" else sys.stdout
    return open(std.fileno(), mode=mode, closefd=False)  # type: ignore


def _open_bz2(
    filename: FileOrPath,
    mode: str,
    compresslevel: int | None,
    threads: int | None,
):
    assert mode in ("rb", "ab", "wb")
    if compresslevel is None:
        compresslevel = XOPEN_DEFAULT_BZ2_COMPRESSION
    if threads != 0:
        try:
            # pbzip2 can compress using multiple cores.
            return _PipedCompressionProgram(
                filename,
                mode,
                compresslevel,
                threads=threads,
                program_settings=_PROGRAM_SETTINGS["pbzip2"],
            )
        except OSError:
            pass  # We try without threads.

    bz2_file = bz2.open(filename, mode, compresslevel)
    if "r" in mode:
        return bz2_file
    # Buffer writes on bz2.open to mitigate overhead of small writes
    return io.BufferedWriter(bz2_file)  # type: ignore


def _open_xz(
    filename: FileOrPath,
    mode: str,
    compresslevel: int | None,
    threads: int | None,
):
    assert mode in ("rb", "ab", "wb")
    if compresslevel is None:
        compresslevel = XOPEN_DEFAULT_XZ_COMPRESSION

    if threads != 0:
        try:
            # xz can compress using multiple cores.
            return _PipedCompressionProgram(
                filename,
                mode,
                compresslevel,
                threads,
                _PROGRAM_SETTINGS["xz"],
            )
        except OSError:
            pass  # We try without threads.

    if "r" in mode:
        return lzma.open(filename, mode)
    # Buffer writes on lzma.open to mitigate overhead of small writes
    return io.BufferedWriter(lzma.open(filename, mode, preset=compresslevel))  # type: ignore


def _open_zst(
    filename: FileOrPath,
    mode: str,
    compresslevel: int | None,
    threads: int | None,
):
    assert mode in ("rb", "ab", "wb")
    assert compresslevel != 0
    if compresslevel is None:
        compresslevel = XOPEN_DEFAULT_ZST_COMPRESSION
    if zstd:
        max_window_bits = zstd.DecompressionParameter.window_log_max.bounds()[1]
    else:
        max_window_bits = 31
    if threads != 0:
        try:
            # zstd can compress using multiple cores
            program_args: tuple[str, ...] = ("zstd",)
            if "r" in mode:
                # Only use --long=31 for decompression. Using it for
                # compression overrides level settings for window size and
                # forces other zstd users to use `--long=31` to decompress any
                # archive that has been compressed by xopen.
                program_args += (f"--long={max_window_bits}",)
            return _PipedCompressionProgram(
                filename,
                mode,
                compresslevel,
                threads,
                _ProgramSettings(program_args, tuple(range(1, 20)), "-T"),
            )
        except OSError:
            if zstd is None:
                # No fallback available
                raise

    if zstd is None:
        raise ImportError("zstd module not available")

    if "r" in mode:
        level = None
        options = {
            zstd.DecompressionParameter.window_log_max: max_window_bits,
        }
    else:
        level = compresslevel
        options = None
    f = zstd.open(filename, mode, options=options, level=level)  # type: ignore
    if mode == "rb":
        return io.BufferedReader(f)
    return io.BufferedWriter(f)  # mode "ab" and "wb"


def _open_lz4(
    filename: FileOrPath,
    mode: str,
    compresslevel: int | None,
    threads: int | None,
):
    assert mode in ("rb", "ab", "wb")
    if compresslevel is None:
        compresslevel = XOPEN_DEFAULT_LZ4_COMPRESSION
    if compresslevel not in _PROGRAM_SETTINGS["lz4"].acceptable_compression_levels:
        raise ValueError(
            f"LZ4 compresslevel must be in range "
            f"{_LZ4_LEVEL_MIN}-{_LZ4_LEVEL_MAX}, got {compresslevel}."
        )

    if lz4 is not None and (mode == "rb" or threads == 0):
        return lz4.frame.LZ4FrameFile(
            filename, mode, compression_level=compresslevel, content_checksum=True
        )
    if threads == 0:
        raise ImportError("LZ4 support with threads=0 requires xopen[lz4]")

    try:
        # Older lz4 binaries lack -T; add version-aware threading only if needed.
        return _PipedCompressionProgram(
            filename, mode, compresslevel, threads, _PROGRAM_SETTINGS["lz4"]
        )
    except OSError as error:
        if lz4 is None:
            raise ImportError(
                "LZ4 support requires xopen[lz4] or the lz4 program"
            ) from error
        return lz4.frame.LZ4FrameFile(
            filename, mode, compression_level=compresslevel, content_checksum=True
        )


def _open_gz(
    filename: FileOrPath,
    mode: str,
    compresslevel: int | None,
    threads: int | None,
):
    """
    Open a gzip file. The ISA-L library is preferred when applicable because
    it is the fastest. Then zlib-ng which is not as fast, but supports all
    compression levels. After that comes pigz, which can utilize multiple
    threads and is more efficient than gzip, even on one core. gzip is chosen
    when none of the alternatives are available. Despite it being able to use
    only one core, it still finishes faster than using the builtin gzip library
    as the (de)compression is moved to another thread.
    """
    assert mode in ("rb", "ab", "wb")
    if compresslevel is None:
        # Force the same compression level on every tool regardless of
        # library defaults
        compresslevel = XOPEN_DEFAULT_GZIP_COMPRESSION
    if compresslevel not in range(10):
        # Level 0-9 are supported regardless of backend support
        # (zlib_ng supports -1, pigz supports 11 etc.)
        raise ValueError(
            f"gzip compresslevel must be in range 0-9, got {compresslevel}."
        )

    if threads != 0:
        # Igzip level 0 does not output uncompressed deflate blocks as zlib does
        # and level 3 is slower but does not compress better than level 1 and 2.
        if igzip_threaded and (compresslevel in (1, 2) or "r" in mode):
            return igzip_threaded.open(  # type: ignore
                filename,
                mode,
                compresslevel,
                threads=1,
            )
        if gzip_ng_threaded and zlib_ng:
            return gzip_ng_threaded.open(
                filename,
                mode,
                # zlib-ng level 1 is 50% bigger than zlib level 1. Level
                # 2 gives a size close to expectations.
                compresslevel=2 if compresslevel == 1 else compresslevel,
                threads=threads or max(_available_cpu_count(), 4),
            )

        for program in ("pigz", "gzip"):
            try:
                return _PipedCompressionProgram(
                    filename,
                    mode,
                    compresslevel,
                    threads,
                    _PROGRAM_SETTINGS[program],
                )
            # ValueError when compresslevel is not supported. i.e. gzip and level 0
            except (OSError, ValueError):
                pass  # We try without threads.
    return _open_reproducible_gzip(filename, mode=mode, compresslevel=compresslevel)


def _open_reproducible_gzip(filename, mode: str, compresslevel: int):
    """
    Open a gzip file for writing (without external processes)
    that has neither mtime nor the file name in the header
    (equivalent to gzip --no-name)
    """
    assert mode in ("rb", "wb", "ab")
    assert compresslevel is not None
    fileobj, closefd = _file_or_path_to_binary_stream(filename, mode)
    # Neither gzip.open nor igzip.open have an mtime option, and they will
    # always write the file name, so we need to open the file separately
    # and pass it to gzip.GzipFile/igzip.IGzipFile.
    kwargs = dict(
        fileobj=fileobj,
        filename="",
        mode=mode,
        mtime=0,
    )
    # Igzip level 0 does not output uncompressed deflate blocks as zlib does
    # and level 3 is slower but does not compress better than level 1 and 2.
    if igzip is not None and (compresslevel in (1, 2) or "r" in mode):
        gzip_file = igzip.IGzipFile(**kwargs, compresslevel=compresslevel)
    elif gzip_ng is not None:
        # Zlib-ng level 1 creates much bigger files than zlib level 1.
        gzip_file = gzip_ng.GzipNGFile(
            **kwargs, compresslevel=2 if compresslevel == 1 else compresslevel
        )
    else:
        gzip_file = gzip.GzipFile(**kwargs, compresslevel=compresslevel)  # type: ignore
    # When (I)GzipFile is created with a fileobj instead of a filename,
    # the passed file object is not closed when (I)GzipFile.close()
    # is called. This forces it to be closed.
    if closefd:
        gzip_file.myfileobj = fileobj
    if sys.version_info < (3, 12) and "r" not in mode:
        # From version 3.12 onwards, gzip is properly internally buffered for writing.
        return io.BufferedWriter(gzip_file)  # type: ignore
    return gzip_file


def _detect_format_from_content(filename: FileOrPath) -> str | None:
    """
    Attempts to detect file format from the content by reading the first
    6 bytes. Returns None if no format could be detected.
    """
    fileobj, closefd = _file_or_path_to_binary_stream(filename, "rb")
    try:
        if not fileobj.readable():
            return None
        if hasattr(fileobj, "peek"):
            bs = fileobj.peek(6)
        elif hasattr(fileobj, "seekable") and fileobj.seekable():
            current_pos = fileobj.tell()
            bs = fileobj.read(6)
            fileobj.seek(current_pos)
        else:
            return None

        if bs[:2] == b"\x1f\x8b":
            # https://tools.ietf.org/html/rfc1952#page-6
            return "gz"
        elif bs[:3] == b"\x42\x5a\x68":
            # https://en.wikipedia.org/wiki/List_of_file_signatures
            return "bz2"
        elif bs[:6] == b"\xfd\x37\x7a\x58\x5a\x00":
            # https://tukaani.org/xz/xz-file-format.txt
            return "xz"
        elif bs[:4] == b"\x28\xb5\x2f\xfd":
            # https://datatracker.ietf.org/doc/html/rfc8478#section-3.1.1
            return "zst"
        elif bs[:4] == b"\x04\x22\x4d\x18":
            # https://github.com/lz4/lz4/blob/dev/doc/lz4_Frame_format.md
            return "lz4"

        return None
    finally:
        if closefd:
            fileobj.close()


def _detect_format_from_extension(filename: str | bytes) -> str | None:
    """
    Attempt to detect file format from the filename extension.
    Return None if no format could be detected.
    """
    for ext in ("bz2", "xz", "gz", "zst", "lz4"):
        if isinstance(filename, bytes):
            if filename.endswith(b"." + ext.encode()):
                return ext
        else:
            if filename.endswith("." + ext):
                return ext
    return None


def _file_or_path_to_binary_stream(
    file_or_path: FileOrPath, binary_mode: str
) -> tuple[BinaryIO, bool]:
    assert binary_mode in ("rb", "wb", "ab")
    if isinstance(file_or_path, (str, bytes)) or hasattr(file_or_path, "__fspath__"):
        return open(os.fspath(file_or_path), binary_mode), True  # type: ignore
    if isinstance(file_or_path, io.TextIOWrapper):
        return file_or_path.buffer, False
    if hasattr(file_or_path, "readinto") or hasattr(file_or_path, "write"):
        # Very lenient fallback for all filelike objects. If the filelike
        # object is not binary, this will crash at a later point.
        return file_or_path, False  # type: ignore
    raise TypeError(
        f"Unsupported type for {file_or_path}, {file_or_path.__class__.__name__}."
    )


def _filepath_from_path_or_filelike(fileorpath: FileOrPath) -> str:
    try:
        return os.fspath(fileorpath)  # type: ignore
    except TypeError:
        pass
    if hasattr(fileorpath, "name"):
        name = fileorpath.name
        if isinstance(name, str):
            return name
        elif isinstance(name, bytes):
            return name.decode()
    return ""


def _file_is_a_socket_or_pipe(filepath):
    try:
        mode = os.stat(filepath).st_mode
        # Treat anything that is not a regular file as special
        return not stat.S_ISREG(mode)
    except (OSError, TypeError):  # Type error for unexpected types in stat.
        return False


@overload
def xopen(
    filename: FileOrPath,
    mode: Literal["r", "w", "a", "rt", "wt", "at"] = ...,
    compresslevel: int | None = ...,
    threads: int | None = ...,
    *,
    encoding: str = ...,
    errors: str | None = ...,
    newline: str | None = ...,
    format: str | None = ...,
    closefd: bool | None = ...,
) -> io.TextIOWrapper: ...


@overload
def xopen(
    filename: FileOrPath,
    mode: Literal["rb", "wb", "ab"],
    compresslevel: int | None = ...,
    threads: int | None = ...,
    *,
    encoding: str = ...,
    errors: None = ...,
    newline: None = ...,
    format: str | None = ...,
    closefd: bool | None = ...,
) -> BinaryIO: ...


def xopen(
    filename: FileOrPath,
    mode: Literal["r", "w", "a", "rt", "rb", "wt", "wb", "at", "ab"] = "r",
    compresslevel: int | None = None,
    threads: int | None = None,
    *,
    encoding: str = "utf-8",
    errors: str | None = None,
    newline: str | None = None,
    format: str | None = None,
    closefd: bool | None = None,
) -> IO:
    """
    A replacement for the "open" function that can also read and write
    compressed files transparently. The supported compression formats are gzip,
    bzip2, xz, zstandard and LZ4. If the filename is '-', standard output (mode 'w') or
    standard input (mode 'r') is returned. Filename can be a string or a
    file object. (See https://docs.python.org/3/glossary.html#term-file-object.)

    When writing, the file format is chosen based on the file name extension:
    - .gz uses gzip compression
    - .bz2 uses bzip2 compression
    - .xz uses xz/lzma compression
    - .zst uses zstandard compression
    - .lz4 uses lz4 compression
    - otherwise, no compression is used

    When reading, if a file name extension is available, the format is detected
    using it, but if not, the format is detected from the contents.

    mode can be: 'rt', 'rb', 'at', 'ab', 'wt', or 'wb'. Also, the 't' can be omitted,
    so instead of 'rt', 'wt' and 'at', the abbreviations 'r', 'w' and 'a' can be used.

    compresslevel is the compression level for writing to gzip, xz, zst and lz4 files.
    This parameter is ignored for the other compression formats.
    If set to None, a default depending on the format is used:
    gzip: 6, xz: 6, zstd: 3, lz4: 0.

    When threads is None (the default), compressed file formats are read or written
    using a pipe to a subprocess running an external tool such as,
    ``pbzip2``, ``gzip`` etc., see PipedGzipWriter, PipedGzipReader etc.
    If the external tool supports multiple threads, *threads* can be set to an int
    specifying the number of threads to use.
    If no external tool supporting the compression format is available, the file is
    opened calling the appropriate Python function
    (that is, no subprocess is spawned).

    Set threads to 0 to force opening the file without using a subprocess.

    encoding, errors and newline are used when opening a file in text mode.
    The parameters have the same meaning as in the built-in open function,
    except that the default encoding is always UTF-8 instead of the
    preferred locale encoding.

    format overrides the autodetection of input and output formats. This can be
    useful when compressed output needs to be written to a file without an
    extension. Possible values are "gz", "xz", "bz2", "zst", "lz4".

    closefd controls ownership of a caller-provided file object. With False the
    file object is *borrowed*: xopen reads or writes through it but never closes
    it, so an abnormal reader exit (or an explicit close) leaves the external
    handle usable for a successor. With True the file object is *taken over* and
    closed when the returned reader is closed. It defaults to True for file name
    arguments and False for file objects. Passing closefd=False together with a
    file name is not possible because there is no external handle to borrow.
    """
    if mode in ("r", "w", "a"):
        mode += "t"  # type: ignore
    if mode not in ("rt", "rb", "wt", "wb", "at", "ab"):
        raise ValueError(f"Mode '{mode}' not supported")
    binary_mode = mode[0] + "b"
    filepath = _filepath_from_path_or_filelike(filename)
    is_path = isinstance(filename, (str, bytes)) or hasattr(filename, "__fspath__")

    if closefd is None:
        # File names are taken over; caller-provided file objects are borrowed.
        closefd = is_path
    elif not closefd and is_path:
        raise ValueError(
            "closefd=False requires a file object, not a file name: "
            "there is no external handle to borrow"
        )

    # Open non-regular files such as pipes and sockets here to force opening
    # them once. These keep their existing lifecycle and are not wrapped.
    managed = None
    if filename == "-":
        filename = _open_stdin_or_out(binary_mode)
    elif _file_is_a_socket_or_pipe(filename):
        filename = open(filename, binary_mode)  # type: ignore
    elif not is_path:
        # A genuine caller-provided file object. Make its ownership explicit by
        # routing every layer below through a managed stream whose close() only
        # releases the external handle in take-over mode.
        if isinstance(filename, io.TextIOWrapper):
            target = filename.buffer  # type: ignore
        elif hasattr(filename, "readinto") or hasattr(filename, "write"):
            target = filename
        else:
            raise TypeError(
                f"Unsupported type for {filename}, {filename.__class__.__name__}."
            )
        managed = _ManagedStream(target, own=closefd)
        filename = managed

    if format not in (None, "gz", "xz", "bz2", "zst", "lz4"):
        raise ValueError(
            f"Format not supported: {format}. "
            f"Choose one of: 'gz', 'xz', 'bz2', 'zst', 'lz4'."
        )
    detected_format = format or _detect_format_from_extension(filepath)
    if detected_format is None and "r" in mode:
        detected_format = _detect_format_from_content(filename)

    if detected_format == "gz":
        opened_file = _open_gz(filename, binary_mode, compresslevel, threads)
    elif detected_format == "xz":
        opened_file = _open_xz(filename, binary_mode, compresslevel, threads)
    elif detected_format == "bz2":
        opened_file = _open_bz2(filename, binary_mode, compresslevel, threads)
    elif detected_format == "zst":
        opened_file = _open_zst(filename, binary_mode, compresslevel, threads)
    elif detected_format == "lz4":
        opened_file = _open_lz4(filename, binary_mode, compresslevel, threads)
    else:
        opened_file, _ = _file_or_path_to_binary_stream(filename, binary_mode)

    if (
        managed is not None
        and closefd
        and detected_format is not None
    ):
        # Compressed backends do not close a file object they are given, so in
        # take-over mode add a wrapper that releases both reader and handle.
        opened_file = _OwningReader(opened_file, managed)

    if "t" in mode:
        text_reader = io.TextIOWrapper(opened_file, encoding, errors, newline)
        # Remember the configured newline so a later handoff can recreate it.
        text_reader._xopen_newline = newline
        return text_reader
    return opened_file
